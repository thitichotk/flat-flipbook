from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import streamlit as st

from anyflip_downloader import AnyFlipDownloadError, DownloadOptions, DownloadResult, download_book
from ui_components import format_bytes, load_custom_css, render_footer, render_header


BASE_DIR = Path(__file__).resolve().parent


st.set_page_config(
    page_title="Flat-Flipbook · ระบบดาวน์โหลด AnyFlip เป็น PDF",
    page_icon=":material/picture_as_pdf:",
    layout="wide",
    menu_items={
        "about": "Flat-Flipbook • ใช้งานเฉพาะเอกสารที่เจ้าของอนุญาตให้ดาวน์โหลดเป็น PDF",
    },
)

load_custom_css(BASE_DIR)
render_header(BASE_DIR)

if "download_result" not in st.session_state:
    st.session_state.download_result = None
if "output_dir" not in st.session_state:
    # ponytail: one temp folder per session, emptied before each new job; a session that just
    # disappears leaves its last PDF until the server restarts.
    st.session_state.output_dir = tempfile.mkdtemp(prefix="flat-flipbook-")


with st.container():
    st.markdown("### กำหนดลิงก์และตัวเลือกการดาวน์โหลด")
    st.caption(
        "ขั้นตอน: (1) วางลิงก์ AnyFlip → (2) ระบุชื่อไฟล์หรือใช้ชื่อหนังสือ → "
        "(3) ยืนยันสิทธิ์ → (4) สร้างและดาวน์โหลด PDF"
    )

    with st.form("anyflip_download_form", clear_on_submit=False):
        url = st.text_input(
            "ลิงก์ AnyFlip",
            placeholder="https://online.anyflip.com/owner/book/",
            help="รองรับลิงก์หนังสือจากโดเมน anyflip.com รวมถึงลิงก์หน้าสำหรับมือถือ",
        )
        title_override = st.text_input(
            "ชื่อไฟล์ PDF",
            placeholder="เว้นว่างเพื่อใช้ชื่อหนังสือจาก AnyFlip",
            help="ระบุเฉพาะชื่อไฟล์โดยไม่ต้องใส่นามสกุล .pdf",
        )

        with st.expander(":material/tune: ตัวเลือกขั้นสูง", expanded=False):
            st.caption(
                "ค่าเริ่มต้นเหมาะกับการใช้งานทั่วไป หากเครือข่ายไม่เสถียรให้ลดจำนวนงานพร้อมกัน "
                "หรือเพิ่มจำนวนครั้งลองซ้ำ"
            )
            col_threads, col_retries = st.columns(2)
            with col_threads:
                threads = st.slider("จำนวนงานดาวน์โหลดพร้อมกัน", 1, 12, 4)
            with col_retries:
                retries = st.number_input("จำนวนครั้งลองซ้ำต่อหน้า", min_value=0, max_value=10, value=1, step=1)
                retry_delay_seconds = st.number_input(
                    "เวลารอก่อนลองซ้ำ (วินาที)",
                    min_value=0.0,
                    max_value=30.0,
                    value=1.0,
                    step=0.5,
                )

            verify_tls = st.checkbox("ตรวจสอบใบรับรอง TLS", value=True)

        st.caption(
            "ไฟล์ PDF เก็บไว้ชั่วคราวสำหรับ session นี้เท่านั้น และถูกลบเมื่อเริ่มงานใหม่ "
            "ระบบไม่บันทึกประวัติหรือเก็บไฟล์ถาวร"
        )
        allowed = st.checkbox("ฉันยืนยันว่าเอกสารนี้อนุญาตให้ดาวน์โหลดเป็น PDF", value=False)
        submitted = st.form_submit_button(
            "เริ่มดาวน์โหลดและสร้าง PDF",
            type="primary",
            width="stretch",
        )


if submitted:
    st.session_state.download_result = None
    shutil.rmtree(st.session_state.output_dir, ignore_errors=True)
    Path(st.session_state.output_dir).mkdir(parents=True, exist_ok=True)

    if not url.strip():
        st.error("กรุณาระบุลิงก์ AnyFlip ก่อนเริ่มทำงาน")
        st.stop()

    if not allowed:
        st.error("กรุณายืนยันสิทธิ์การดาวน์โหลดก่อนเริ่มทำงาน")
        st.stop()

    options = DownloadOptions(
        threads=threads,
        retries=int(retries),
        retry_delay_seconds=float(retry_delay_seconds),
        verify_tls=verify_tls,
    )

    progress_bar = st.progress(0)
    status_text = st.empty()

    def update_progress(stage: str, completed: int, total: int, message: str) -> None:
        total = max(total, 1)
        ratio = max(0.0, min(float(completed) / float(total), 1.0))
        ranges = {
            "prepare": (0.00, 0.12),
            "download": (0.12, 0.70),
            "pdf": (0.82, 0.17),
            "done": (1.00, 0.00),
        }
        base, weight = ranges.get(stage, (0.0, 1.0))
        progress = int(max(0.0, min(base + (weight * ratio), 1.0)) * 100)
        progress_bar.progress(progress)
        status_text.markdown(message)

    try:
        result = download_book(
            raw_url=url,
            title_override=title_override,
            options=options,
            progress_callback=update_progress,
            output_dir=st.session_state.output_dir,
        )
        progress_bar.progress(100)
        status_text.markdown("สร้างไฟล์ PDF เสร็จสมบูรณ์")
        st.session_state.download_result = result
        st.success("สร้างไฟล์ PDF เสร็จสมบูรณ์ พร้อมดาวน์โหลดแล้ว")
    except AnyFlipDownloadError as exc:
        progress_bar.empty()
        status_text.empty()
        st.error(str(exc))
    except Exception as exc:
        progress_bar.empty()
        status_text.empty()
        st.error(f"เกิดข้อผิดพลาดที่ไม่คาดคิด: {exc}")


result: DownloadResult | None = st.session_state.download_result
if result and Path(result.pdf_path).is_file():
    st.markdown("### ผลลัพธ์และดาวน์โหลด")
    st.caption(f"แหล่งข้อมูล AnyFlip: {result.normalized_url}")
    st.markdown(f"**ชื่อไฟล์:** `{result.file_name}`")

    metric_cols = st.columns(4)
    metric_cols[0].metric("จำนวนหน้า", f"{result.page_count:,}")
    metric_cols[1].metric("ดาวน์โหลดสำเร็จ", f"{result.downloaded_pages:,}")
    metric_cols[2].metric("ขนาดไฟล์", format_bytes(result.file_size_bytes))
    metric_cols[3].metric("เวลาประมวลผล", f"{result.elapsed_seconds:,.1f} วินาที")

    button_col, _ = st.columns([1, 2])
    with button_col:
        st.download_button(
            label=":material/download: ดาวน์โหลด PDF",
            data=Path(result.pdf_path).read_bytes,  # read only when clicked, not on every rerun
            on_click="ignore",
            file_name=result.file_name,
            mime="application/pdf",
            type="primary",
            width="stretch",
        )

    with st.expander(":material/receipt_long: บันทึกการทำงาน", expanded=False):
        for line in result.status_log:
            st.write(line)

render_footer()
