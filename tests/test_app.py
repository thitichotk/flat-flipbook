from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from anyflip_downloader import AnyFlipDownloadError, DownloadResult


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
ASSETS_DIR = APP_PATH.parent / "assets"


def by_label(elements, label: str):
    return next(element for element in elements if element.label == label)


class AnyFlipAppTest(unittest.TestCase):
    def render(self) -> AppTest:
        return AppTest.from_file(APP_PATH, default_timeout=10).run()

    def test_initial_render(self) -> None:
        with patch("anyflip_downloader.download_book") as download_book:
            app = self.render()

        self.assertEqual(app.exception.len, 0)
        self.assertEqual(app.title.values, ["ระบบดาวน์โหลด AnyFlip เป็น PDF"])
        self.assertEqual(app.header.values, ["แปลงหนังสือออนไลน์เป็นไฟล์ PDF"])
        self.assertIn(
            "ขั้นตอน: (1) วางลิงก์ AnyFlip → (2) ระบุชื่อไฟล์หรือใช้ชื่อหนังสือ → "
            "(3) ยืนยันสิทธิ์ → (4) สร้างและดาวน์โหลด PDF",
            app.caption.values,
        )
        self.assertEqual(
            [element.label for element in app.text_input],
            ["ลิงก์ AnyFlip", "ชื่อไฟล์ PDF"],
        )
        # TLS disable toggle is removed from the public UI.
        self.assertEqual(
            [element.label for element in app.checkbox],
            ["ฉันยืนยันว่าเอกสารนี้อนุญาตให้ดาวน์โหลดเป็น PDF"],
        )
        self.assertEqual(
            by_label(app.button, "เริ่มดาวน์โหลดและสร้าง PDF").proto.type,
            "primary",
        )
        self.assertTrue((ASSETS_DIR / "anyflip.jpg").is_file())
        image_count = max(len(app.get("imgs")), len(app.get("image")))
        self.assertEqual(image_count, 1)
        download_book.assert_not_called()

    def test_empty_url_validation(self) -> None:
        with patch("anyflip_downloader.download_book") as download_book:
            app = self.render()
            by_label(app.button, "เริ่มดาวน์โหลดและสร้าง PDF").click().run()

        self.assertEqual(app.error.values, ["กรุณาระบุลิงก์ AnyFlip ก่อนเริ่มทำงาน"])
        self.assertEqual(app.exception.len, 0)
        download_book.assert_not_called()

    def test_permission_validation(self) -> None:
        with patch("anyflip_downloader.download_book") as download_book:
            app = self.render()
            by_label(app.text_input, "ลิงก์ AnyFlip").set_value(
                "https://online.anyflip.com/abcd/efgh"
            )
            by_label(app.button, "เริ่มดาวน์โหลดและสร้าง PDF").click().run()

        self.assertEqual(
            app.error.values,
            ["กรุณายืนยันสิทธิ์การดาวน์โหลดก่อนเริ่มทำงาน"],
        )
        self.assertEqual(app.exception.len, 0)
        download_book.assert_not_called()

    def test_successful_download_and_validation_error_preserves_previous_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_file = Path(tmp) / "Report.pdf"
            pdf_file.write_bytes(b"%PDF-1.4\n%test\n")
            fake_result = DownloadResult(
                title="Report",
                file_name="Report.pdf",
                normalized_url="https://online.anyflip.com/abcd/efgh",
                page_count=5,
                downloaded_pages=5,
                pdf_path=str(pdf_file),
                elapsed_seconds=1.2,
                status_log=["พบหนังสือ \"Report\" จำนวน 5 หน้า", "สร้างไฟล์ PDF เสร็จสมบูรณ์"],
            )

            with patch("anyflip_downloader.download_book", return_value=fake_result):
                app = self.render()
                by_label(app.text_input, "ลิงก์ AnyFlip").set_value("https://online.anyflip.com/abcd/efgh")
                by_label(app.checkbox, "ฉันยืนยันว่าเอกสารนี้อนุญาตให้ดาวน์โหลดเป็น PDF").set_value(True)
                by_label(app.button, "เริ่มดาวน์โหลดและสร้าง PDF").click().run()

                self.assertEqual(app.exception.len, 0)
                self.assertEqual([m.value for m in app.metric][:2], ["5", "5"])

                # Submitting invalid form later must NOT wipe the completed PDF in session state.
                by_label(app.text_input, "ลิงก์ AnyFlip").set_value("")
                by_label(app.button, "เริ่มดาวน์โหลดและสร้าง PDF").click().run()

                self.assertEqual(app.error.values, ["กรุณาระบุลิงก์ AnyFlip ก่อนเริ่มทำงาน"])
                self.assertIsNotNone(app.session_state["download_result"])
                self.assertTrue(pdf_file.is_file())

    def test_download_error_shows_clean_message(self) -> None:
        with patch("anyflip_downloader.download_book", side_effect=AnyFlipDownloadError("ไม่พบจำนวนหน้าใน config.js")):
            app = self.render()
            by_label(app.text_input, "ลิงก์ AnyFlip").set_value("https://online.anyflip.com/abcd/efgh")
            by_label(app.checkbox, "ฉันยืนยันว่าเอกสารนี้อนุญาตให้ดาวน์โหลดเป็น PDF").set_value(True)
            by_label(app.button, "เริ่มดาวน์โหลดและสร้าง PDF").click().run()

        self.assertEqual(app.exception.len, 0)
        self.assertEqual(app.error.values, ["ไม่พบจำนวนหน้าใน config.js"])


if __name__ == "__main__":
    unittest.main()
