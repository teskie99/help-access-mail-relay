"""Turn an email attachment's bytes into plain text for Claude.

Pure functions, no network. Everything here treats the file as hostile: size caps, page and row caps,
a zip-bomb check for Office files, and no macro or script is ever executed.
"""

from __future__ import annotations

import csv
import io
import zipfile

TEXT_EXT = {"txt", "csv", "md"}
OFFICE_EXT = {"docx", "xlsx", "pptx"}
IMAGE_EXT = {"png", "jpg", "jpeg"}
READABLE_EXT = {"pdf"} | TEXT_EXT | OFFICE_EXT | IMAGE_EXT

LEGACY_EXT = {"doc", "xls", "ppt", "xlsm", "docm", "pptm", "rtf"}

MAX_PDF_PAGES = 80
MAX_SHEETS = 12
MAX_SHEET_ROWS = 400
MAX_SHEET_COLS = 40
MAX_UNZIPPED_BYTES = 60_000_000
MAX_ZIP_ENTRIES = 2000


class ReadError(Exception):
    """A failure with a message that is safe to show to Claude."""


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ReadError("This Office file has too many internal parts to open safely.")
            if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
                raise ReadError("This Office file expands to an unsafe size, so it was not opened.")
    except zipfile.BadZipFile:
        raise ReadError("The file is damaged or is not a real Office file.") from None


def _pdf(data: bytes) -> tuple[str, dict]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise ReadError("The PDF is password-protected, so it cannot be read.")
            except ReadError:
                raise
            except Exception:  # noqa: BLE001
                raise ReadError("The PDF is password-protected, so it cannot be read.") from None
        total = len(reader.pages)
        parts = []
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            txt = (page.extract_text() or "").strip()
            parts.append(f"--- page {i} ---\n{txt}")
    except ReadError:
        raise
    except Exception:  # noqa: BLE001
        raise ReadError("The PDF could not be opened (damaged or unusual format).") from None
    text = "\n\n".join(parts)
    meta = {"pages": total, "pages_read": min(total, MAX_PDF_PAGES)}
    if len("".join(p.split("\n", 1)[-1] for p in parts).strip()) < 20:
        meta["note"] = (
            "Almost no text was found. This is probably a scanned or image-only PDF. "
            "Ask the sender for a text version, or for the figures in the email body."
        )
    return text, meta


def _docx(data: bytes) -> tuple[str, dict]:
    import docx

    _check_zip(data)
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        raise ReadError("The Word file could not be opened.") from None
    out = [p.text for p in d.paragraphs if p.text.strip()]
    for ti, t in enumerate(d.tables, start=1):
        out.append(f"--- table {ti} ---")
        for row in t.rows:
            out.append(" | ".join(c.text.strip().replace("\n", " ") for c in row.cells))
    return "\n".join(out), {"tables": len(d.tables)}


def _xlsx(data: bytes) -> tuple[str, dict]:
    import openpyxl

    _check_zip(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        raise ReadError("The Excel file could not be opened.") from None
    out, truncated = [], False
    names = wb.sheetnames
    for name in names[:MAX_SHEETS]:
        ws = wb[name]
        out.append(f"--- sheet: {name} ---")
        for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if r > MAX_SHEET_ROWS:
                truncated = True
                out.append(f"[... more rows not shown; first {MAX_SHEET_ROWS} rows only]")
                break
            cells = ["" if v is None else str(v).replace("\n", " ") for v in row[:MAX_SHEET_COLS]]
            while cells and cells[-1] == "":
                cells.pop()
            if cells:
                out.append(f"{r}: " + " | ".join(cells))
    wb.close()
    meta = {"sheets": len(names), "sheets_read": min(len(names), MAX_SHEETS)}
    if truncated:
        meta["rows_truncated"] = True
    meta["note"] = "Cell values shown are the saved results of formulas. Row numbers are shown before each row."
    return "\n".join(out), meta


def _pptx(data: bytes) -> tuple[str, dict]:
    from pptx import Presentation

    _check_zip(data)
    try:
        prs = Presentation(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        raise ReadError("The PowerPoint file could not be opened.") from None
    out = []
    for i, slide in enumerate(prs.slides, start=1):
        out.append(f"--- slide {i} ---")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    out.append(" | ".join(c.text.strip().replace("\n", " ") for c in row.cells))
    return "\n".join(out), {"slides": len(prs.slides)}


def _text(data: bytes, ext: str) -> tuple[str, dict]:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            s = data.decode(enc)
            break
        except UnicodeError:
            continue
    else:
        s = data.decode("latin-1", errors="replace")
    if ext == "csv":
        rows = []
        for r, row in enumerate(csv.reader(io.StringIO(s)), start=1):
            if r > MAX_SHEET_ROWS * 5:
                rows.append("[... more rows not shown]")
                break
            rows.append(f"{r}: " + " | ".join(row[:MAX_SHEET_COLS]))
        return "\n".join(rows), {}
    return s, {}


def extract_text(name: str, data: bytes) -> tuple[str, dict]:
    """Return (text, meta). Raises ReadError for unsupported or unreadable files."""
    ext = extension(name)
    if ext in LEGACY_EXT:
        raise ReadError(
            f"Old or macro-enabled .{ext} files are not opened for safety. "
            "Ask the sender to resend it as PDF, .docx, .xlsx or .pptx."
        )
    if ext == "pdf":
        if not data.startswith(b"%PDF"):
            raise ReadError("The file is named .pdf but is not a real PDF.")
        return _pdf(data)
    if ext == "docx":
        return _docx(data)
    if ext == "xlsx":
        return _xlsx(data)
    if ext == "pptx":
        return _pptx(data)
    if ext in TEXT_EXT:
        return _text(data, ext)
    raise ReadError(
        f"Files of type .{ext or '(none)'} cannot be read. Readable types: "
        + ", ".join(sorted(READABLE_EXT))
        + "."
    )
