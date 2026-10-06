# Flat-Flipbook

[![CI](https://github.com/thitichotk/flat-flipbook/actions/workflows/build.yml/badge.svg)](https://github.com/thitichotk/flat-flipbook/actions/workflows/build.yml)

**Live app: [flatflipbook.streamlit.app](https://flatflipbook.streamlit.app)**

Flat-Flipbook (ระบบดาวน์โหลดเอกสาร AnyFlip เป็น PDF) turns an AnyFlip flipbook into a flat PDF. You paste the
book's link. It reads the book's public viewer settings, downloads each page image, and binds the pages in order into
one PDF. The interface is in Thai.

## Use it only with permission

Only use Flat-Flipbook on documents whose owner explicitly allows downloading. You are responsible for copyright,
the publisher's terms and AnyFlip's terms of service. The app doesn't check your rights for you, and it doesn't get
around logins or access controls. It is not affiliated with AnyFlip; AnyFlip and its logo belong to their owner.

## What it does

- Accepts standard and mobile `anyflip.com` links, and keeps every request on `anyflip.com`, redirects included.
- Names the PDF after the book (Thai titles included), or after a name you type.
- Downloads up to 12 pages at a time with retries, then makes one slower pass over any pages that failed. If a page
  still fails, the error lists which pages.
- Embeds JPEG pages exactly as AnyFlip serves them, so the PDF is about the size of the images instead of ten times
  bigger. Other formats are converted to JPEG at quality 90, and transparent pages get a white background.
- Writes the PDF to a temporary file for your session rather than to memory, and deletes it when you start the next
  job. Books are capped at 2,000 pages.

## Run it locally

Python 3.12 is recommended.

```bash
git clone https://github.com/thitichotk/flat-flipbook.git
cd flat-flipbook
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py     # http://localhost:8501
```

Or with Docker:

```bash
docker build -t flat-flipbook .
docker run --rm -p 8501:8501 flat-flipbook
```

## Using it

1. Paste the book link, for example `https://online.anyflip.com/owner/book/`.
2. Type a file name if you want one.
3. Confirm that the owner allows downloading, then press **เริ่มดาวน์โหลดและสร้าง PDF**.
4. Download the PDF from the result.

Advanced options:

| Option | Default | What it does |
| --- | ---: | --- |
| Concurrent downloads | 4 (up to 12) | Number of pages fetched at the same time. Lower it on a shaky connection. |
| Retries per page | 1 | Extra attempts per page before the final slow pass |
| Retry delay | 1 second | Wait between attempts |
| TLS verification | On | Checks HTTPS certificates. Leave it on. |

## How it works

1. The link is reduced to its owner and book IDs on `online.anyflip.com`.
2. The viewer's `mobile/javascript/config.js` provides the title, page count and page file names.
3. Pages download in parallel into a temporary folder. Each response must actually be a JPEG, PNG or WebP image, which
   catches error pages that come back with status 200.
4. The pages are bound in order into an image-only PDF, and the page images are deleted.

| File | Role |
| --- | --- |
| `app.py` | Streamlit page: form, progress, result |
| `anyflip_downloader.py` | URL handling, config parsing, downloads with retries, PDF building. Entry point: `download_book()` |
| `ui_components.py` | Header, footer, stylesheet |
| `.streamlit/` | Theme and styling |
| `tests/` | Downloader unit tests and Streamlit UI tests (no live AnyFlip calls) |

## Tests

```bash
python -m unittest discover -s tests -v
```

## Limits

- Only public books whose viewer settings and page images can be fetched without logging in.
- The PDF contains images only: no selectable text, links or outline.
- If AnyFlip changes its viewer format, the parser may need updating.

## Credits and license

Flat-Flipbook started as a fork of [Lofter1/anyflip-downloader](https://github.com/Lofter1/anyflip-downloader), a Go
command-line tool. This version is a Python and Streamlit rewrite by Thitichot K. (2026), and it takes the same
approach: read `config.js`, then fetch the page images.

Licensed under the [GNU General Public License v3.0](LICENSE), like the original.

- Copyright (C) 2023 Lofter1 and contributors (original Go version)
- Copyright (C) 2026 Thitichot K. (modified: rewritten in Python with a Streamlit interface)
