"""Input Parsing Layer (first node of the process map).

Turns customer-submitted files (Project Specification, Raw Email, Circuit
Drawing / SLD) into a normalized ``ParsedDocument`` with page/segment-level
text so downstream evidence extraction can cite a location.

Supported: .pdf (pypdf), .docx (python-docx), .txt/.eml/.msg/.md (plain text),
           .xlsx/.xlsm (openpyxl), .pptx (python-pptx), .csv (stdlib).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config


@dataclass
class DocSegment:
    """A locatable chunk of text (a PDF page, or a docx block)."""

    location: str  # e.g. "page 2" or "paragraph 14" or "table 1 row 3"
    text: str


@dataclass
class ParsedDocument:
    file_name: str
    file_path: str
    doc_type: str  # Project Specification / Raw Email / Drawing / SLD / Other
    segments: list[DocSegment] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join(seg.text for seg in self.segments)


# --------------------------------------------------------------------------- #
# Document role inference (maps to 11_Input_Document_Index "Document Role")
# --------------------------------------------------------------------------- #
def infer_doc_type(file_name: str, text: str = "") -> str:
    name = file_name.lower()
    blob = text.lower()
    if any(k in name for k in ("sld", "gsld", "drawing", "diagram", "_dwg")):
        return "Drawing / SLD"
    if name.endswith((".eml", ".msg")) or "raw email" in name or "from:" in blob[:500]:
        return "Raw Email"
    if any(k in name for k in ("spec", "tender", "scope", "requirement")):
        return "Project Specification"
    if any(k in name for k in ("boq", "quantity", "schedule")):
        return "Equipment List / BOQ"
    if name.endswith(".pdf") and any(
        k in blob for k in ("single line diagram", "legend", "circuit breaker")
    ):
        return "Drawing / SLD"
    return "Supporting"


# --------------------------------------------------------------------------- #
# Per-format parsers
# --------------------------------------------------------------------------- #
def _parse_pdf(path: Path) -> list[DocSegment]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    segments: list[DocSegment] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # pragma: no cover - corrupt page
            text = ""
        if text.strip():
            segments.append(DocSegment(location=f"page {i}", text=text))
    return segments


def _parse_docx(path: Path) -> list[DocSegment]:
    import docx

    document = docx.Document(str(path))
    segments: list[DocSegment] = []
    for idx, para in enumerate(document.paragraphs, start=1):
        if para.text.strip():
            segments.append(DocSegment(location=f"paragraph {idx}", text=para.text))
    for t_idx, table in enumerate(document.tables, start=1):
        for r_idx, row in enumerate(table.rows, start=1):
            cells = [c.text.strip() for c in row.cells]
            line = " | ".join(c for c in cells if c)
            if line:
                segments.append(
                    DocSegment(location=f"table {t_idx} row {r_idx}", text=line)
                )
    return segments


def _parse_xlsx(path: Path) -> list[DocSegment]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), data_only=True, read_only=True)
    segments: list[DocSegment] = []
    for sheet in workbook.worksheets:
        for r_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = [str(v).strip() for v in row if v not in (None, "")]
            if values:
                segments.append(
                    DocSegment(
                        location=f"sheet '{sheet.title}' row {r_idx}",
                        text=" | ".join(values),
                    )
                )
    workbook.close()
    return segments


def _parse_pptx(path: Path) -> list[DocSegment]:
    try:
        from pptx import Presentation
    except ImportError:
        return _parse_text_fallback(path)

    prs = Presentation(str(path))
    segments: list[DocSegment] = []
    for s_idx, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                texts.append(shape.text.strip())
        if texts:
            segments.append(
                DocSegment(location=f"slide {s_idx}", text="\n".join(texts))
            )
    return segments


def _parse_csv(path: Path) -> list[DocSegment]:
    import csv

    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    segments: list[DocSegment] = []
    reader = csv.reader(raw.splitlines())
    for r_idx, row in enumerate(reader, start=1):
        line = " | ".join(cell.strip() for cell in row if cell.strip())
        if line:
            segments.append(DocSegment(location=f"row {r_idx}", text=line))
    return segments


def _parse_eml(path: Path) -> list[DocSegment]:
    """Parse .eml email files, extracting headers + plain-text body."""
    from email import policy
    from email.parser import BytesParser

    raw = path.read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    header_lines = [
        f"From: {message.get('from', '')}",
        f"To: {message.get('to', '')}",
        f"Subject: {message.get('subject', '')}",
        f"Date: {message.get('date', '')}",
    ]
    body_part = message.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""

    segments: list[DocSegment] = []
    if header_lines:
        segments.append(
            DocSegment(location="headers", text="\n".join(h for h in header_lines if h.split(": ", 1)[-1]))
        )
    if body.strip():
        blocks = [b.strip() for b in body.split("\n\n") if b.strip()]
        for b_idx, block in enumerate(blocks, start=1):
            segments.append(DocSegment(location=f"body block {b_idx}", text=block))
    return segments


def _parse_text_fallback(path: Path) -> list[DocSegment]:
    """Read any unrecognised file as plain text."""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return []
    return [DocSegment(location="full", text=raw.strip())] if raw.strip() else []


def _parse_text(path: Path) -> list[DocSegment]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    segments: list[DocSegment] = []
    # Split into reasonably sized blocks on blank lines to keep locations useful.
    blocks = [b.strip() for b in raw.split("\n\n")]
    line_no = 1
    for block in blocks:
        n_lines = block.count("\n") + 1
        if block:
            segments.append(
                DocSegment(location=f"lines {line_no}-{line_no + n_lines - 1}", text=block)
            )
        line_no += n_lines + 1
    if not segments and raw.strip():
        segments.append(DocSegment(location="full", text=raw.strip()))
    return segments


def parse_document(path: str | Path) -> Optional[ParsedDocument]:
    """Parse a single file into a ParsedDocument, or None if unsupported/empty."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in config.SUPPORTED_DOC_EXTENSIONS:
        return None

    try:
        if ext == ".pdf":
            segments = _parse_pdf(path)
        elif ext == ".docx":
            segments = _parse_docx(path)
        elif ext in {".xlsx", ".xlsm"}:
            segments = _parse_xlsx(path)
        elif ext == ".pptx":
            segments = _parse_pptx(path)
        elif ext == ".csv":
            segments = _parse_csv(path)
        elif ext == ".eml":
            segments = _parse_eml(path)
        else:
            segments = _parse_text(path)
    except Exception as exc:  # pragma: no cover - defensive
        segments = [DocSegment(location="error", text=f"[parse error] {exc}")]

    if not segments:
        return None

    doc_type = infer_doc_type(path.name, segments[0].text if segments else "")
    return ParsedDocument(
        file_name=path.name,
        file_path=str(path),
        doc_type=doc_type,
        segments=segments,
    )


def parse_project_folder(folder: str | Path) -> list[ParsedDocument]:
    """Parse every supported document in a customer submission folder."""
    folder = Path(folder)
    docs: list[ParsedDocument] = []
    for file in sorted(folder.rglob("*")):
        if file.is_file() and file.suffix.lower() in config.SUPPORTED_DOC_EXTENSIONS:
            parsed = parse_document(file)
            if parsed:
                docs.append(parsed)
    return docs
