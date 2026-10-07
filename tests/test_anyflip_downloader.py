from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anyflip_downloader import (
    AnyFlipDownloadError,
    DownloadOptions,
    _build_pdf,
    _page_image,
    build_page_urls,
    clean_download_url,
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


class AnyFlipDownloaderTest(unittest.TestCase):
    def test_normalize_anyflip_url_keeps_owner_and_book(self) -> None:
        normalized = normalize_anyflip_url("https://online.anyflip.com/abcd/efgh/mobile/index.html")
        self.assertEqual(normalized, "https://online.anyflip.com/abcd/efgh")

    def test_normalize_anyflip_url_rejects_other_domains(self) -> None:
        with self.assertRaises(AnyFlipDownloadError):
            normalize_anyflip_url("https://example.com/abcd/efgh")
        with self.assertRaises(AnyFlipDownloadError):
            normalize_anyflip_url("https://notanyflip.com/abcd/efgh")

    def test_parse_config_values(self) -> None:
        self.assertEqual(parse_book_title(CONFIG_JS), "Rice Market Report")
        self.assertEqual(parse_page_count(CONFIG_JS), 3)
        self.assertEqual(parse_page_file_names(CONFIG_JS), ["page-1.webp", "page-2.webp", "page-3.webp"])

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

    def test_clean_download_url_resolves_path_segments_and_duplicates(self) -> None:
        self.assertEqual(
            clean_download_url("https://online.anyflip.com/a/b/files/large/../files/mobile/1.jpg"),
            "https://online.anyflip.com/a/b/files/mobile/1.jpg",
        )

    def test_parse_book_title_decodes_unicode_escapes(self) -> None:
        config = 'bookConfig.bookTitle = "\\u0e23\\u0e32\\u0e22\\u0e07\\u0e32\\u0e19";'
        self.assertEqual(parse_book_title(config), "รายงาน")

    def test_image_extension_reads_signatures(self) -> None:
        self.assertEqual(image_extension(b"\xff\xd8\xff\xe0rest"), ".jpg")
        self.assertEqual(image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertIsNone(image_extension(b"<!DOCTYPE html><html>"))

    def test_http_get_refuses_redirect_off_anyflip(self) -> None:
        class Redirect:
            is_redirect = True
            headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        with patch("requests.get", return_value=Redirect()) as get:
            with self.assertRaises(AnyFlipDownloadError):
                http_get("https://online.anyflip.com/abcd/efgh/files/mobile/1.jpg")
        self.assertEqual(get.call_count, 1)  # the off-site URL is never requested

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

            reader, _ = _page_image(png)
            self.assertEqual(reader.getRGBData()[:3], b"\xff\xff\xff")  # white, not black

            jpeg_size = jpeg.stat().st_size
            pdf_path = Path(temp_dir) / "book.pdf"
            _build_pdf([jpeg, png], pdf_path, log)
            data = pdf_path.read_bytes()

        self.assertTrue(data.startswith(b"%PDF"))
        # The JPEG is embedded as-is; re-encoding the noise as raw pixels would be ~10x larger.
        self.assertLess(len(data), 2 * jpeg_size)
        self.assertTrue(messages)

if __name__ == "__main__":
    unittest.main()
