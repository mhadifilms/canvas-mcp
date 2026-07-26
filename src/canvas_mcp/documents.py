"""Turning course materials into text.

Downloading a PDF is only half of "help me study from the lecture slides" - the
assistant has to be able to read it. Each format's library is an optional extra,
so a student who only ever opens PDFs doesn't need the Word and PowerPoint ones.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .errors import CanvasMCPError
from .formatting import html_to_text

PLAIN_SUFFIXES = {".txt", ".md", ".markdown", ".rtf", ".log", ".py", ".r", ".csv", ".tsv", ".json"}
HTML_SUFFIXES = {".html", ".htm"}

INSTALL_HINT = {
    ".pdf": "pip install pypdf",
    ".docx": "pip install python-docx",
    ".pptx": "pip install python-pptx",
}


def supported_suffixes() -> set[str]:
    return PLAIN_SUFFIXES | HTML_SUFFIXES | {".pdf", ".docx", ".pptx"}


def available_readers() -> dict[str, bool]:
    """Which formats this installation can actually read right now."""
    return {
        "pdf": _module_present("pypdf"),
        "docx": _module_present("docx"),
        "pptx": _module_present("pptx"),
        "text/html/csv": True,
    }


def _module_present(name: str) -> bool:
    try:
        __import__(name)
    except Exception:
        return False
    return True


def extract_text(path: Path, *, limit: int | None = 20000) -> str:
    """Read a course file as text. Raises CanvasMCPError with a fix for what's missing."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        text = _read_pdf(path)
    elif suffix == ".docx":
        text = _read_docx(path)
    elif suffix == ".pptx":
        text = _read_pptx(path)
    elif suffix in HTML_SUFFIXES:
        text = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    elif suffix in (".csv", ".tsv"):
        text = _read_delimited(path, "\t" if suffix == ".tsv" else ",")
    elif suffix in PLAIN_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        readable = ", ".join(sorted(supported_suffixes()))
        raise CanvasMCPError(
            f"I can't read {suffix or 'that file type'} as text. Readable formats: {readable}. "
            f"The file is saved at {path} if you want to open it yourself."
        )

    text = text.strip()
    if not text:
        raise CanvasMCPError(
            f"{path.name} has no extractable text. If it's a scanned document it would need OCR, "
            "which this server doesn't do."
        )
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + f"\n\n... [truncated, {len(text) - limit} more characters]"
    return text


def _require(module: str, suffix: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise CanvasMCPError(
            f"Reading {suffix} files needs an extra package: {INSTALL_HINT.get(suffix, 'see the README')}\n"
            "Or install everything at once: pip install 'canvas-mcp[documents]'"
        ) from exc


def _read_pdf(path: Path) -> str:
    pypdf = _require("pypdf", ".pdf")
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:
        raise CanvasMCPError(f"Could not open {path.name} as a PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")  # Many course PDFs carry an empty owner password.
        except Exception:
            raise CanvasMCPError(f"{path.name} is password protected.") from None

    pages: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            content = page.extract_text() or ""
        except Exception:
            content = ""
        if content.strip():
            pages.append(f"--- page {number} ---\n{content.strip()}")
    return "\n\n".join(pages)


def _read_docx(path: Path) -> str:
    docx = _require("docx", ".docx")
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise CanvasMCPError(f"Could not open {path.name} as a Word document: {exc}") from exc

    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pptx(path: Path) -> str:
    pptx = _require("pptx", ".pptx")
    try:
        deck = pptx.Presentation(str(path))
    except Exception as exc:
        raise CanvasMCPError(f"Could not open {path.name} as a PowerPoint file: {exc}") from exc

    slides: list[str] = []
    for number, slide in enumerate(deck.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
        notes = ""
        if getattr(slide, "has_notes_slide", False):
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        if lines or notes:
            block = f"--- slide {number} ---\n" + "\n".join(lines)
            if notes:
                block += f"\n[speaker notes] {notes}"
            slides.append(block)
    return "\n\n".join(slides)


def _read_delimited(path: Path, delimiter: str) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows if any(row))
