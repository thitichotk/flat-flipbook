from __future__ import annotations

import concurrent.futures
import io
import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urljoin, urlparse, urlunparse


ProgressCallback = Callable[[str, int, int, str], None]

CONFIG_TIMEOUT_SECONDS = 30
PAGE_TIMEOUT_SECONDS = 45
MAX_CONFIG_BYTES = 5 * 1024 * 1024
MAX_PAGE_BYTES = 25 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
SAFE_URL_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
# shortcut: fixed cap so one request can't fill a small Streamlit Cloud box, raise when real books exceed it.
MAX_PAGES = 2000
MAX_REDIRECTS = 5


class AnyFlipDownloadError(Exception):
    """Raised when a book cannot be prepared, downloaded, or converted."""


@dataclass(frozen=True)
class DownloadOptions:
    threads: int = 4
    retries: int = 1
    retry_delay_seconds: float = 1.0
    verify_tls: bool = True

    def normalized(self) -> "DownloadOptions":
        return DownloadOptions(
            threads=max(1, min(int(self.threads), 12)),
            retries=max(0, min(int(self.retries), 10)),
            retry_delay_seconds=max(0.0, float(self.retry_delay_seconds)),
            verify_tls=bool(self.verify_tls),
        )


@dataclass(frozen=True)
class BookMetadata:
    source_url: str
    normalized_url: str
    title: str
    file_name: str
    page_count: int
    page_urls: tuple[str, ...]


@dataclass(frozen=True)
class PageDownloadRecord:
    page_number: int
    url: str
    path: str


@dataclass(frozen=True)
class DownloadResult:
    title: str
    file_name: str
    normalized_url: str
    page_count: int
    downloaded_pages: int
    pdf_path: str
    elapsed_seconds: float
    status_log: list[str] = field(default_factory=list)

    @property
    def file_size_bytes(self) -> int:
        return Path(self.pdf_path).stat().st_size


def emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    completed: int,
    total: int,
    message: str,
) -> None:
    if callback:
        callback(stage, completed, total, message)


def is_anyflip_host(url: str, *, require_https: bool = False) -> bool:
    parsed = urlparse(url)
    if require_https:
        if parsed.scheme != "https":
            return False
        if parsed.port not in (None, 443):
            return False
    elif parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()
    return hostname == "anyflip.com" or hostname.endswith(".anyflip.com")


def _build_session(threads: int):
    try:
        import requests
        from requests.adapters import HTTPAdapter
    except ImportError as exc:
        raise AnyFlipDownloadError("ไม่พบแพ็กเกจ requests กรุณาติดตั้ง dependencies จาก requirements.txt") from exc

    session = requests.Session()
    pool_size = max(1, threads)
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    return session


def _read_bounded_content(response, max_bytes: int) -> bytes:
    if hasattr(response, "iter_content"):
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise AnyFlipDownloadError(
                        f"ไฟล์ตอบกลับจาก AnyFlip มีขนาดเกินขีดจำกัด ({max_bytes // (1024 * 1024)} MB)"
                    )
                chunks.append(chunk)
        finally:
            if hasattr(response, "close"):
                response.close()
        data = b"".join(chunks)
        response._content = data
        return data

    data = response.content
    if len(data) > max_bytes:
        raise AnyFlipDownloadError(
            f"ไฟล์ตอบกลับจาก AnyFlip มีขนาดเกินขีดจำกัด ({max_bytes // (1024 * 1024)} MB)"
        )
    return data


def http_get(url: str, *, session=None, max_bytes: int | None = None, **kwargs):
    """GET over HTTPS that follows redirects only while they stay on anyflip.com."""
    if session is None:
        try:
            import requests
        except ImportError as exc:
            raise AnyFlipDownloadError("ไม่พบแพ็กเกจ requests กรุณาติดตั้ง dependencies จาก requirements.txt") from exc
        getter = requests.get
    else:
        getter = session.get

    req_kwargs = dict(kwargs)
    if max_bytes is not None:
        req_kwargs["stream"] = True

    for _ in range(MAX_REDIRECTS + 1):
        if not is_anyflip_host(url, require_https=True):
            parsed = urlparse(url)
            raise AnyFlipDownloadError(
                f"ปฏิเสธการเชื่อมต่อที่ไม่ปลอดภัยหรือนอกโดเมน anyflip.com: {parsed.scheme}://{parsed.netloc}"
            )
        response = getter(url, allow_redirects=False, **req_kwargs)
        if not response.is_redirect:
            if max_bytes is not None:
                _read_bounded_content(response, max_bytes)
            return response

        location = response.headers.get("Location")
        if hasattr(response, "close"):
            response.close()
        if not location:
            raise AnyFlipDownloadError("AnyFlip ส่งการเปลี่ยนเส้นทาง (redirect) ที่ไม่สมบูรณ์")
        url = urljoin(url, location)

    raise AnyFlipDownloadError("AnyFlip เปลี่ยนเส้นทาง (redirect) มากเกินไป")


def normalize_anyflip_url(raw_url: str) -> str:
    candidate = raw_url.strip()
    if not candidate:
        raise AnyFlipDownloadError("กรุณาระบุ URL ของ AnyFlip")

    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if not is_anyflip_host(candidate):
        raise AnyFlipDownloadError("รองรับเฉพาะ URL จากโดเมน anyflip.com")

    raw_parts = [part for part in parsed.path.split("/") if part]
    if len(raw_parts) < 2:
        raise AnyFlipDownloadError("รูปแบบ URL ไม่ถูกต้อง ควรมี owner และรหัสหนังสือ")

    owner, book = unquote(raw_parts[0]), unquote(raw_parts[1])
    for segment in (owner, book):
        if segment in (".", "..") or not SAFE_URL_SEGMENT.match(segment):
            raise AnyFlipDownloadError("รูปแบบ owner หรือรหัสหนังสือใน URL ไม่ถูกต้อง")

    normalized_path = f"/{owner}/{book}"
    return urlunparse(("https", "online.anyflip.com", normalized_path, "", "", ""))


def safe_pdf_filename(title: str) -> str:
    filename = INVALID_FILENAME_CHARS.sub("_", title).strip().strip(".")
    filename = re.sub(r"\s+", " ", filename)
    if not filename or not filename.strip("._- "):
        filename = "anyflip-download"
    if len(filename) > 140:
        filename = filename[:140].rstrip(" ._")
    if not filename:
        filename = "anyflip-download"
    return f"{filename}.pdf"


def clean_download_url(raw_url: str) -> str:
    decoded = unquote(raw_url).replace("\\", "/")
    parsed = urlparse(decoded)
    if not parsed.scheme or not parsed.netloc:
        return decoded

    cleaned_parts: list[str] = []
    for part in parsed.path.split("/"):
        if part in ("", "."):
            if not cleaned_parts:
                cleaned_parts.append("")
            continue
        if part == "..":
            if len(cleaned_parts) > 1:
                cleaned_parts.pop()
            continue
        # Only collapse duplicate "files" from relative AnyFlip configs (e.g. files/large/../files/mobile/1.jpg),
        # never valid identical owner/book pairs like /demo/demo/.
        if part == "files" and cleaned_parts and cleaned_parts[-1] == "files":
            continue
        cleaned_parts.append(part)

    cleaned_path = "/".join(cleaned_parts) or "/"
    return parsed._replace(path=cleaned_path).geturl()


def parse_book_title(config_js: str) -> str | None:
    patterns = (
        r'"?(?:bookConfig\.)?bookTitle"?\s*=\s*"((?:[^"\\]|\\.)+)"',
        r'"bookTitle"\s*:\s*"((?:[^"\\]|\\.)+)"',
        r'"title"\s*:\s*"((?:[^"\\]|\\.)+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, config_js)
        if match:
            raw = match.group(1)
            try:
                raw = json.loads(f'"{raw}"')  # decode \uXXXX and \" escapes used in JS strings
            except ValueError:
                pass
            title = raw.strip()
            if title:
                return title
    return None


def parse_page_count(config_js: str) -> int:
    pattern = r'"?(?:bookConfig\.)?(?:total)?[Pp]ageCount"?\s*[:=]\s*"?(\d+)"?'
    match = re.search(pattern, config_js)
    if not match:
        raise AnyFlipDownloadError("ไม่พบจำนวนหน้าใน config.js")
    page_count = int(match.group(1))
    if page_count <= 0:
        raise AnyFlipDownloadError("จำนวนหน้าใน config.js ไม่ถูกต้อง")
    if page_count > MAX_PAGES:
        raise AnyFlipDownloadError(f"หนังสือมี {page_count:,} หน้า เกินขีดจำกัด {MAX_PAGES:,} หน้าต่อครั้ง")
    return page_count


def parse_page_file_names(config_js: str) -> list[str]:
    file_names: list[str] = []
    for match in re.finditer(r'"n"\s*:\s*\[(.*?)\]', config_js, flags=re.DOTALL):
        file_names.extend(re.findall(r'"([^"]+)"', match.group(1)))
    return file_names


def fetch_config_js(normalized_url: str, options: DownloadOptions, *, session=None) -> str:
    parsed = urlparse(normalized_url)
    config_url = urlunparse(
        (
            "https",
            "online.anyflip.com",
            f"{parsed.path}/mobile/javascript/config.js",
            "",
            "",
            "",
        )
    )
    response = http_get(
        config_url,
        session=session,
        max_bytes=MAX_CONFIG_BYTES,
        headers={"User-Agent": USER_AGENT},
        timeout=CONFIG_TIMEOUT_SECONDS,
        verify=options.verify_tls,
    )
    if response.status_code != 200:
        raise AnyFlipDownloadError(f"ไม่สามารถโหลด config.js ได้: HTTP {response.status_code}")
    return response.text


def build_page_urls(normalized_url: str, page_count: int, page_file_names: Iterable[str]) -> tuple[str, ...]:
    parsed = urlparse(normalized_url)
    base_path = parsed.path.rstrip("/")
    large_base = f"https://online.anyflip.com{base_path}/files/large/"
    names = list(page_file_names)

    if len(names) >= page_count:
        urls: list[str] = []
        for index in range(page_count):
            name = names[index].strip().replace("\\", "/")
            joined = urljoin(large_base, name)
            urls.append(clean_download_url(joined))
        return tuple(urls)

    return tuple(
        urlunparse(
            (
                "https",
                "online.anyflip.com",
                f"{base_path}/files/mobile/{page_number}.jpg",
                "",
                "",
                "",
            )
        )
        for page_number in range(1, page_count + 1)
    )


def prepare_book(
    raw_url: str,
    title_override: str | None,
    options: DownloadOptions | None = None,
    *,
    session=None,
) -> BookMetadata:
    safe_options = (options or DownloadOptions()).normalized()
    normalized_url = normalize_anyflip_url(raw_url)
    config_js = fetch_config_js(normalized_url, safe_options, session=session) if session is not None else fetch_config_js(normalized_url, safe_options)

    title = (title_override or "").strip() or parse_book_title(config_js) or Path(urlparse(normalized_url).path).name
    if not title:
        title = "anyflip-download"

    page_count = parse_page_count(config_js)
    page_urls = build_page_urls(normalized_url, page_count, parse_page_file_names(config_js))
    return BookMetadata(
        source_url=raw_url,
        normalized_url=normalized_url,
        title=title,
        file_name=safe_pdf_filename(title),
        page_count=page_count,
        page_urls=page_urls,
    )


def download_book(
    raw_url: str,
    title_override: str | None = None,
    options: DownloadOptions | None = None,
    progress_callback: ProgressCallback | None = None,
    output_dir: str | Path | None = None,
) -> DownloadResult:
    """Downloads every page and writes the PDF to `output_dir` (a new temp dir if omitted).

    The PDF goes to disk rather than memory so a large book is held once, not once per copy.
    """
    start_time = time.perf_counter()
    safe_options = (options or DownloadOptions()).normalized()
    status_log: list[str] = []

    def milestone(stage: str, completed: int, total: int, message: str) -> None:
        status_log.append(message)
        emit_progress(progress_callback, stage, completed, total, message)

    def tick(stage: str, completed: int, total: int, message: str) -> None:
        emit_progress(progress_callback, stage, completed, total, message)

    with _build_session(safe_options.threads) as session:
        milestone("prepare", 0, 1, "กำลังอ่านข้อมูลหนังสือจาก AnyFlip...")
        metadata = prepare_book(raw_url, title_override, safe_options, session=session)
        milestone("prepare", 1, 1, f"พบหนังสือ \"{metadata.title}\" จำนวน {metadata.page_count:,} หน้า")

        out_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="flat-flipbook-"))
        pdf_path = out_dir / metadata.file_name
        with tempfile.TemporaryDirectory(prefix="flat-flipbook-pages-") as temp_dir:
            records = _download_pages(metadata, Path(temp_dir), safe_options, milestone, tick, session=session)
            _build_pdf(
                [Path(record.path) for record in records],
                pdf_path,
                milestone,
                tick,
                title=metadata.title,
            )

    elapsed = time.perf_counter() - start_time
    milestone("done", 1, 1, "สร้างไฟล์ PDF เสร็จสมบูรณ์")
    return DownloadResult(
        title=metadata.title,
        file_name=metadata.file_name,
        normalized_url=metadata.normalized_url,
        page_count=metadata.page_count,
        downloaded_pages=len(records),
        pdf_path=str(pdf_path),
        elapsed_seconds=elapsed,
        status_log=status_log,
    )


def _download_pages(
    metadata: BookMetadata,
    image_dir: Path,
    options: DownloadOptions,
    milestone: ProgressCallback,
    tick: ProgressCallback | None = None,
    *,
    session=None,
) -> list[PageDownloadRecord]:
    emit_tick = tick or milestone
    milestone("download", 0, metadata.page_count, "กำลังดาวน์โหลดรูปภาพแต่ละหน้า...")
    records: list[PageDownloadRecord] = []
    failed: dict[int, Exception] = {}

    def fetch(page_index: int) -> PageDownloadRecord:
        return _download_page(
            page_index,
            metadata.page_urls[page_index],
            metadata.normalized_url,
            image_dir,
            options,
            session=session,
        )

    step = max(1, metadata.page_count // 100)
    with concurrent.futures.ThreadPoolExecutor(max_workers=options.threads) as executor:
        future_map = {executor.submit(fetch, index): index for index in range(metadata.page_count)}
        completed = 0
        for future in concurrent.futures.as_completed(future_map):
            page_index = future_map[future]
            try:
                records.append(future.result())
            except Exception as exc:
                failed[page_index] = exc
            completed += 1
            if completed % step == 0 or completed == metadata.page_count:
                emit_tick("download", completed, metadata.page_count, f"ดาวน์โหลดแล้ว {completed:,}/{metadata.page_count:,} หน้า")

    # One slower pass over the failures: parallel requests are what usually trips a rate limit.
    if failed:
        retry_total = len(failed)
        milestone("download", metadata.page_count - retry_total, metadata.page_count, f"ลองดาวน์โหลดซ้ำ {retry_total:,} หน้าที่ไม่สำเร็จ...")
        for retry_idx, page_index in enumerate(sorted(failed), start=1):
            try:
                records.append(fetch(page_index))
                del failed[page_index]
            except Exception as exc:
                failed[page_index] = exc
            emit_tick(
                "download",
                metadata.page_count - len(failed),
                metadata.page_count,
                f"ลองดาวน์โหลดซ้ำ ({retry_idx:,}/{retry_total:,}) — สำเร็จรวม {len(records):,}/{metadata.page_count:,} หน้า",
            )

    if failed:
        pages = ", ".join(str(index + 1) for index in sorted(failed)[:20])
        more = f" และอีก {len(failed) - 20} หน้า" if len(failed) > 20 else ""
        first_error = failed[min(failed)]
        raise AnyFlipDownloadError(f"ดาวน์โหลดไม่สำเร็จ {len(failed):,} หน้า: {pages}{more} ({first_error})")

    milestone("download", metadata.page_count, metadata.page_count, f"ดาวน์โหลดครบทั้ง {len(records):,} หน้า")
    records.sort(key=lambda record: record.page_number)
    return records


def image_extension(data: bytes) -> str | None:
    """File extension from the image's signature, or None when it is not an image."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _download_page(
    page_index: int,
    page_url: str,
    referer: str,
    image_dir: Path,
    options: DownloadOptions,
    *,
    session=None,
) -> PageDownloadRecord:
    cleaned_url = clean_download_url(page_url)
    last_error: Exception | None = None

    for attempt in range(options.retries + 1):
        try:
            response = http_get(
                cleaned_url,
                session=session,
                max_bytes=MAX_PAGE_BYTES,
                headers={"Referer": referer, "User-Agent": USER_AGENT},
                timeout=PAGE_TIMEOUT_SECONDS,
                verify=options.verify_tls,
            )
            if response.status_code != 200:
                raise AnyFlipDownloadError(f"HTTP {response.status_code}")

            extension = image_extension(response.content)
            if extension is None:
                raise AnyFlipDownloadError("ไฟล์ที่ได้ไม่ใช่รูปภาพ (AnyFlip อาจส่งหน้าเว็บแจ้งข้อผิดพลาดกลับมา)")
            path = image_dir / f"{page_index:04d}{extension}"
            path.write_bytes(response.content)
            return PageDownloadRecord(page_index + 1, cleaned_url, str(path))
        except Exception as exc:
            last_error = exc
            if attempt < options.retries and options.retry_delay_seconds > 0:
                time.sleep(options.retry_delay_seconds)

    raise AnyFlipDownloadError(f"หลังลอง {options.retries + 1} ครั้ง: {last_error}")


def _page_image(image_path: Path):
    """An ImageReader for one page. JPEG pages are embedded as they are; opaque other formats
    convert straight to RGB JPEG, and transparent pages are flattened onto white."""
    from PIL import Image, UnidentifiedImageError
    from reportlab.lib.utils import ImageReader

    try:
        with Image.open(image_path) as image:
            size = image.size
            if image.format == "JPEG" and image.mode in ("RGB", "L"):
                return ImageReader(str(image_path)), size
            has_alpha = "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)
            if has_alpha:
                rgba = image.convert("RGBA")
                page = Image.new("RGB", rgba.size, "white")
                page.paste(rgba, mask=rgba.getchannel("A"))
            else:
                page = image.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise AnyFlipDownloadError(f"รูปภาพหน้า {image_path.name} มีความละเอียดสูงเกินขีดจำกัดความปลอดภัย") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise AnyFlipDownloadError(f"ไม่สามารถอ่านไฟล์รูปภาพ {image_path.name} ได้: {exc}") from exc

    buffer = io.BytesIO()
    page.save(buffer, "JPEG", quality=90)
    buffer.seek(0)
    return ImageReader(buffer), size


def _build_pdf(
    image_paths: list[Path],
    pdf_path: Path,
    milestone: ProgressCallback,
    tick: ProgressCallback | None = None,
    *,
    title: str = "",
) -> None:
    if not image_paths:
        raise AnyFlipDownloadError("ไม่พบรูปภาพสำหรับสร้าง PDF")

    from reportlab.pdfgen import canvas

    emit_tick = tick or milestone
    pdf = canvas.Canvas(str(pdf_path))
    if title:
        pdf.setTitle(title)
    total = len(image_paths)
    milestone("pdf", 0, total, f"กำลังรวมรูปภาพเป็นไฟล์ PDF ({total:,} หน้า)...")
    for completed, image_path in enumerate(image_paths, start=1):
        reader, (width, height) = _page_image(image_path)
        pdf.setPageSize((width, height))
        pdf.drawImage(reader, 0, 0, width=width, height=height)
        pdf.showPage()
        if completed % 10 == 0 or completed == total:
            emit_tick("pdf", completed, total, f"แปลงเป็น PDF แล้ว {completed:,}/{total:,} หน้า")
    pdf.save()
    milestone("pdf", total, total, f"รวมไฟล์ PDF ครบ {total:,} หน้าแล้ว")
