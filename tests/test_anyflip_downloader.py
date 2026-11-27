from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from anyflip_downloader import (
    MAX_PAGES,
    AnyFlipDownloadError,
    BookMetadata,
    DownloadOptions,
    _build_pdf,
    _download_pages,
    _page_image,
    build_page_urls,
    clean_download_url,
    download_book,
    http_get,
    image_extension,
    normalize_anyflip_url,
    parse_book_title,
    parse_page_count,
    parse_page_file_names,
    prepare_book,
    safe_pdf_filename,
)


CONFIG_JS = """
var bookConfig = {};
bookConfig.bookTitle = "Rice Market Report";
bookConfig.totalPageCount = "3";
var pages = [{"n":["page-1.webp"]},{"n":["page-2.webp"]},{"n":["page-3.webp"]}];
"""


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", text: str = "", headers=None, is_redirect: bool = False):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}
        self.is_redirect = is_redirect
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class AnyFlipDownloaderTest(unittest.TestCase):
    def test_normalize_anyflip_url_keeps_owner_and_book(self) -> None:
        normalized = normalize_anyflip_url("https://online.anyflip.com/abcd/efgh/mobile/index.html")
        self.assertEqual(normalized, "https://online.anyflip.com/abcd/efgh")

    def test_normalize_anyflip_url_rejects_other_domains(self) -> None:
        with self.assertRaises(AnyFlipDownloadError):
            normalize_anyflip_url("https://example.com/abcd/efgh")
        with self.assertRaises(AnyFlipDownloadError):
            normalize_anyflip_url("https://notanyflip.com/abcd/efgh")

    def test_normalize_anyflip_url_rejects_path_traversal_and_encoded_slashes(self) -> None:
        for bad in (
            "https://online.anyflip.com/owner/book%2F..%2Fadmin",
            "https://online.anyflip.com/../secret",
            "https://online.anyflip.com/owner/..",
            "https://online.anyflip.com/owner/book%20name",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(AnyFlipDownloadError):
                    normalize_anyflip_url(bad)

    def test_parse_config_values(self) -> None:
        self.assertEqual(parse_book_title(CONFIG_JS), "Rice Market Report")
        self.assertEqual(parse_page_count(CONFIG_JS), 3)
        self.assertEqual(parse_page_file_names(CONFIG_JS), ["page-1.webp", "page-2.webp", "page-3.webp"])

    def test_parse_book_title_decodes_unicode_and_escaped_quotes(self) -> None:
        config_thai = 'bookConfig.bookTitle = "\\u0e23\\u0e32\\u0e22\\u0e07\\u0e32\\u0e19";'
        self.assertEqual(parse_book_title(config_thai), "รายงาน")
        config_quotes = 'bookConfig.bookTitle = "Annual \\"2026\\" Report";'
        self.assertEqual(parse_book_title(config_quotes), 'Annual "2026" Report')

    def test_parse_page_count_enforces_bounds(self) -> None:
        with self.assertRaises(AnyFlipDownloadError):
            parse_page_count("var bookConfig = {};")
        with self.assertRaises(AnyFlipDownloadError):
            parse_page_count('bookConfig.totalPageCount = "0";')
        with self.assertRaises(AnyFlipDownloadError):
            parse_page_count(f'bookConfig.totalPageCount = "{MAX_PAGES + 1}";')

    def test_build_page_urls_uses_large_files_when_names_exist(self) -> None:
        urls = build_page_urls(
            "https://online.anyflip.com/abcd/efgh",
            2,
            ["p1.webp", "p2.webp"],
        )
        self.assertEqual(
            urls,
            (
                "https://online.anyflip.com/abcd/efgh/files/large/p1.webp",
                "https://online.anyflip.com/abcd/efgh/files/large/p2.webp",
            ),
        )

    def test_build_page_urls_falls_back_to_mobile_images(self) -> None:
        urls = build_page_urls("https://online.anyflip.com/abcd/efgh", 2, [])
        self.assertEqual(
            urls,
            (
                "https://online.anyflip.com/abcd/efgh/files/mobile/1.jpg",
                "https://online.anyflip.com/abcd/efgh/files/mobile/2.jpg",
            ),
        )

    def test_prepare_book_uses_title_override_and_safe_filename(self) -> None:
        with patch("anyflip_downloader.fetch_config_js", return_value=CONFIG_JS):
            metadata = prepare_book(
                "https://online.anyflip.com/abcd/efgh/",
                "Q1/Rice:Report",
                DownloadOptions(),
            )

        self.assertEqual(metadata.title, "Q1/Rice:Report")
        self.assertEqual(metadata.file_name, "Q1_Rice_Report.pdf")
        self.assertEqual(metadata.page_count, 3)

    def test_safe_pdf_filename_falls_back_for_invalid_titles(self) -> None:
        self.assertEqual(safe_pdf_filename("///"), "anyflip-download.pdf")

    def test_clean_download_url_fixes_duplicate_files_without_breaking_identical_owner_book(self) -> None:
        self.assertEqual(
            clean_download_url("https://online.anyflip.com/a/b/files/large/../files/mobile/1.jpg"),
            "https://online.anyflip.com/a/b/files/mobile/1.jpg",
        )
        self.assertEqual(
            clean_download_url("https://online.anyflip.com/demo/demo/files/large/1.jpg"),
            "https://online.anyflip.com/demo/demo/files/large/1.jpg",
        )

    def test_image_extension_reads_signatures(self) -> None:
        self.assertEqual(image_extension(b"\xff\xd8\xff\xe0rest"), ".jpg")
        self.assertEqual(image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertIsNone(image_extension(b"<!DOCTYPE html><html>"))

    def test_http_get_refuses_redirect_off_anyflip(self) -> None:
        redirect = FakeResponse(is_redirect=True, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        with patch("requests.get", return_value=redirect) as get:
            with self.assertRaises(AnyFlipDownloadError):
                http_get("https://online.anyflip.com/abcd/efgh/files/mobile/1.jpg")
        self.assertEqual(get.call_count, 1)
        self.assertTrue(redirect.closed)

    def test_http_get_refuses_https_to_http_downgrade_and_oversized_response(self) -> None:
        downgrade = FakeResponse(is_redirect=True, headers={"Location": "http://online.anyflip.com/abcd/efgh"})
        with patch("requests.get", return_value=downgrade):
            with self.assertRaises(AnyFlipDownloadError):
                http_get("https://online.anyflip.com/abcd/efgh")

        too_big = FakeResponse(status_code=200, content=b"x" * 200)
        with patch("requests.get", return_value=too_big):
            with self.assertRaises(AnyFlipDownloadError):
                http_get("https://online.anyflip.com/abcd/efgh/files/large/1.jpg", max_bytes=100)
        self.assertTrue(too_big.closed)

    def test_download_pages_retries_transient_failure_and_reports_persistent_errors(self) -> None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "blue").save(buf, format="JPEG")
        jpg_bytes = buf.getvalue()

        metadata = BookMetadata(
            source_url="https://online.anyflip.com/a/b",
            normalized_url="https://online.anyflip.com/a/b",
            title="Test",
            file_name="Test.pdf",
            page_count=2,
            page_urls=(
                "https://online.anyflip.com/a/b/files/large/1.jpg",
                "https://online.anyflip.com/a/b/files/large/2.jpg",
            ),
        )
        attempts = {"1.jpg": 0, "2.jpg": 0}

        def fake_get(url, **kwargs):
            key = "1.jpg" if url.endswith("1.jpg") else "2.jpg"
            attempts[key] += 1
            if key == "2.jpg" and attempts[key] == 1:
                return FakeResponse(status_code=503)
            return FakeResponse(status_code=200, content=jpg_bytes)

        milestones: list[str] = []
        ticks: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("anyflip_downloader.http_get", side_effect=fake_get):
                records = _download_pages(
                    metadata,
                    Path(tmp),
                    DownloadOptions(threads=2, retries=0, retry_delay_seconds=0),
                    lambda s, c, t, m: milestones.append(m),
                    lambda s, c, t, m: ticks.append(m),
                )
        self.assertEqual([r.page_number for r in records], [1, 2])
        self.assertEqual(attempts["2.jpg"], 2)  # failed parallel pass, succeeded on slow retry pass
        self.assertTrue(any("ลองดาวน์โหลดซ้ำ" in m for m in milestones))

    def test_download_book_end_to_end_keeps_status_log_concise_and_sets_pdf_title(self) -> None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (20, 30), "red").save(buf, format="WEBP")
        webp_bytes = buf.getvalue()

        def fake_get(url, **kwargs):
            if url.endswith("config.js"):
                return FakeResponse(status_code=200, text=CONFIG_JS, content=CONFIG_JS.encode("utf-8"))
            return FakeResponse(status_code=200, content=webp_bytes)

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            with patch("anyflip_downloader._build_session", return_value=mock_session), patch(
                "anyflip_downloader.http_get", side_effect=fake_get
            ):
                result = download_book(
                    "https://online.anyflip.com/abcd/efgh",
                    options=DownloadOptions(threads=2, retries=0, retry_delay_seconds=0),
                    output_dir=tmp,
                )
            pdf_bytes = Path(result.pdf_path).read_bytes()

        self.assertEqual(result.downloaded_pages, 3)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertIn(b"Rice Market Report", pdf_bytes)
        self.assertLessEqual(len(result.status_log), 8)

    def test_build_pdf_embeds_jpeg_as_is_and_flattens_transparency(self) -> None:
        from PIL import Image

        messages: list[str] = []

        def log(stage: str, completed: int, total: int, message: str) -> None:
            messages.append(message)

        with tempfile.TemporaryDirectory() as temp_dir:
            jpeg = Path(temp_dir) / "0000.jpg"
            Image.effect_noise((400, 600), 60).convert("RGB").save(jpeg, quality=85)
            png = Path(temp_dir) / "0001.png"
            Image.new("RGBA", (20, 30), (0, 0, 0, 0)).save(png)
            opaque_webp = Path(temp_dir) / "0002.webp"
            Image.new("RGB", (20, 30), (10, 20, 30)).save(opaque_webp, format="WEBP")

            reader, _ = _page_image(png)
            self.assertEqual(reader.getRGBData()[:3], b"\xff\xff\xff")  # white, not black

            opaque_reader, size = _page_image(opaque_webp)
            self.assertEqual(size, (20, 30))
            self.assertIsNotNone(opaque_reader)

            jpeg_size = jpeg.stat().st_size
            pdf_path = Path(temp_dir) / "book.pdf"
            _build_pdf([jpeg, png, opaque_webp], pdf_path, log, title="Sample Book")
            data = pdf_path.read_bytes()

        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn(b"Sample Book", data)
        # The JPEG is embedded as-is; re-encoding the noise as raw pixels would be ~10x larger.
        self.assertLess(len(data), 2 * jpeg_size)
        self.assertTrue(messages)


if __name__ == "__main__":
    unittest.main()
