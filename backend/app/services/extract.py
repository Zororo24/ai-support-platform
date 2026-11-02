"""Turn an uploaded file into plain text/markdown for chunking.

File type is decided by extension AND content, so a renamed binary is rejected.
"""

import io
import zipfile
from pathlib import PurePath

ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}
MAX_PDF_PAGES = 500
MAX_TEXT_CHARS = 2_000_000
MIME_BY_EXTENSION = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class ExtractionError(Exception):
    """The file is unsupported, malformed, or has no extractable text."""


def file_extension(file_name: str) -> str:
    return PurePath(file_name).suffix.lower()


def validate_upload(file_name: str, data: bytes) -> str:
    """Return the mime type, or raise ExtractionError. Cheap checks only (done in the request)."""
    ext = file_extension(file_name)
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ExtractionError(f"Unsupported file type '{ext or file_name}'. Allowed: {allowed}")
    if not data:
        raise ExtractionError("The file is empty")
    if ext == ".pdf" and not data.startswith(b"%PDF-"):
        raise ExtractionError("File content is not a PDF")
    if ext == ".docx":
        if not data.startswith(b"PK"):
            raise ExtractionError("File content is not a DOCX document")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" not in zf.namelist():
                    raise ExtractionError("File content is not a DOCX document")
        except zipfile.BadZipFile as exc:
            raise ExtractionError("File content is not a DOCX document") from exc
    if ext in {".md", ".markdown", ".txt"} and b"\x00" in data[:4096]:
        raise ExtractionError("File looks binary, not text")
    return MIME_BY_EXTENSION[ext]


def extract_text(file_name: str, data: bytes) -> str:
    ext = file_extension(file_name)
    validate_upload(file_name, data)
    if ext == ".pdf":
        text = _pdf_text(data)
    elif ext == ".docx":
        text = _docx_text(data)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExtractionError("Text files must be UTF-8 encoded") from exc
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ExtractionError("No extractable text found (is it a scanned image?)")
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(f"Document is too long (over {MAX_TEXT_CHARS:,} characters)")
    return text


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("Password-protected PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(f"PDF has too many pages (limit {MAX_PDF_PAGES})")
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
    except ExtractionError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError) as exc:
        raise ExtractionError("Could not read the PDF (it may be corrupt)") from exc


def _docx_text(data: bytes) -> str:
    from docx import Document as DocxDocument
    from docx.opc.exceptions import PackageNotFoundError

    try:
        doc = DocxDocument(io.BytesIO(data))
    except (PackageNotFoundError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ExtractionError("Could not read the DOCX file") from exc

    lines: list[str] = []
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "") if paragraph.style is not None else ""
        if style.startswith("Heading ") and style[8:].isdigit():
            text = "#" * min(int(style[8:]), 6) + " " + text  # lets the chunker split on headings
        lines.append(text)
    for table in doc.tables:
        rows = [
            [cell.text.strip().replace("|", "/").replace("\n", " ") for cell in row.cells]
            for row in table.rows
        ]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        markdown = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        markdown += ["| " + " | ".join(r) + " |" for r in padded[1:]]
        lines.append("\n".join(markdown))
    return "\n\n".join(lines)
