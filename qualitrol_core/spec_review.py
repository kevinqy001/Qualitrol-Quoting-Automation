"""Spec Sections review helpers.

Builds a *spec-document-only* requirement list from a Step 1 output, locates
each item precisely in the source PDF (page + line), and renders a cropped
screenshot of that region for the "Review Spec Sections" modal.

This module is read-only with respect to BOQ generation — it only *reads* the
Step 1 evidence and the original source PDFs. SLD / drawing evidence is
deliberately excluded (the modal is spec-only).
"""

from __future__ import annotations

import re
from pathlib import Path

_TERM_RE = re.compile(r"'([^']+)'")
_PAGE_RE = re.compile(r"page\s+(\d+)", re.IGNORECASE)
_SLD_DOC_TYPE = "Drawing / SLD"


# --------------------------------------------------------------------------- #
# Small parsing helpers
# --------------------------------------------------------------------------- #
def _page_num(location: str) -> int:
    m = _PAGE_RE.search(location or "")
    return int(m.group(1)) if m else 0


def _term_from_notes(notes: str) -> str:
    """The exact matched keyword/synonym, e.g. notes "Matched synonym 'BCM'." """
    m = _TERM_RE.search(notes or "")
    return m.group(1) if m else ""


def _clean_snippet(text: str) -> str:
    return (text or "").strip().strip(".").strip()


def _phrases(snippet: str) -> list[str]:
    """Candidate search phrases (longest first) for locating text on a page."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/\.]+", _clean_snippet(snippet))
    out: list[str] = []
    for size in (8, 6, 4, 3):
        if len(words) >= size:
            mid = len(words) // 2
            start = max(0, mid - size // 2)
            phrase = " ".join(words[start : start + size])
            if phrase not in out:
                out.append(phrase)
    out.extend(w for w in words if len(w) >= 7)  # distinctive single words
    return out[:8]


# --------------------------------------------------------------------------- #
# Document classification
# --------------------------------------------------------------------------- #
def spec_doc_names(step1: dict) -> set[str]:
    """File names of non-SLD (spec / prose) documents."""
    return {
        d.get("file_name", "")
        for d in step1.get("documents", [])
        if d.get("doc_type") != _SLD_DOC_TYPE
    }


def sld_doc_names(step1: dict) -> set[str]:
    return {
        d.get("file_name", "")
        for d in step1.get("documents", [])
        if d.get("doc_type") == _SLD_DOC_TYPE
    }


def sld_scenario_ids(step1: dict) -> set[str]:
    """Scenario ids corroborated by SLD / drawing evidence (kept on rebuild)."""
    sld = sld_doc_names(step1)
    ids = {
        e.get("scenario_id", "")
        for e in step1.get("extracted_evidence", [])
        if e.get("source_document") in sld and e.get("scenario_id")
    }
    return {i for i in ids if i}


def spec_scenario_ids(step1: dict) -> set[str]:
    """Scenario ids that have at least one spec-document evidence."""
    specs = spec_doc_names(step1)
    ids = {
        e.get("scenario_id", "")
        for e in step1.get("extracted_evidence", [])
        if (not specs or e.get("source_document") in specs) and e.get("scenario_id")
    }
    return {i for i in ids if i}


# --------------------------------------------------------------------------- #
# Build the spec-only requirement list
# --------------------------------------------------------------------------- #
def build_sections(step1: dict) -> list[dict]:
    """Return one requirement item per spec-document evidence row."""
    specs = spec_doc_names(step1)
    rationale: dict[str, str] = {}
    for d in step1.get("detected_scenarios", []):
        sid = d.get("scenario_id")
        if sid:
            rationale[sid] = d.get("llm_rationale") or ""

    items: list[dict] = []
    for e in step1.get("extracted_evidence", []):
        doc = e.get("source_document", "")
        # Spec-only: skip SLD/drawing evidence (kept out of this modal).
        if specs and doc not in specs:
            continue
        sid = e.get("scenario_id", "")
        reason = (rationale.get(sid) or e.get("notes") or "").strip()
        items.append(
            {
                "id": e.get("evidence_id", ""),
                "scenarioId": sid,
                "scenario": e.get("scenario", "") or sid or "Requirement",
                "assetType": e.get("asset_type", ""),
                "reason": reason,
                "document": doc,
                "page": _page_num(e.get("location", "")),
                "line": int(e.get("line") or 0),
                "location": e.get("location", ""),
                "snippet": e.get("evidence_text", ""),
                "confidence": float(e.get("confidence") or 0.0),
                "term": _term_from_notes(e.get("notes", "")),
            }
        )

    items.sort(key=lambda x: (x["scenario"].lower(), -x["confidence"]))
    return items


# --------------------------------------------------------------------------- #
# PDF locating + rendering (PyMuPDF)
# --------------------------------------------------------------------------- #
def find_source_pdf(doc_name: str, search_dirs: list[Path]) -> Path | None:
    if not doc_name:
        return None
    for base in search_dirs:
        base = Path(base)
        if not base.exists():
            continue
        direct = base / doc_name
        if direct.exists():
            return direct
        for match in base.rglob(doc_name):
            if match.is_file():
                return match
    return None


def _line_index(page, rect) -> int:
    """1-based index of the text line containing ``rect`` on the page."""
    try:
        data = page.get_text("dict")
    except Exception:
        return 0
    tops: list[float] = []
    for block in data.get("blocks", []):
        for ln in block.get("lines", []):
            bbox = ln.get("bbox")
            if bbox:
                tops.append(round(bbox[1], 1))
    if not tops:
        return 0
    tops = sorted(set(tops))
    target = rect.y0
    above = sum(1 for t in tops if t <= target + 2.0)
    return max(1, above)


def _locate(page, term: str, snippet: str):
    """Return the list of rects for the best match on the page (or [])."""
    rects = []
    if term:
        try:
            rects = page.search_for(term)
        except Exception:
            rects = []
    if not rects and snippet:
        for phrase in _phrases(snippet):
            try:
                rects = page.search_for(phrase)
            except Exception:
                rects = []
            if rects:
                break
    return rects


def enrich_lines(items: list[dict], search_dirs: list[Path]) -> None:
    """Best-effort: refine each item's ``line`` and set ``hasImage`` in place.

    Opens each source PDF once. Falls back to the Step 1 ``line`` value when the
    text can't be located (or PyMuPDF isn't available).
    """
    try:
        import fitz  # type: ignore  # PyMuPDF
    except Exception:
        for it in items:
            it["hasImage"] = False
        return

    by_doc: dict[str, list[dict]] = {}
    for it in items:
        by_doc.setdefault(it.get("document", ""), []).append(it)

    for doc_name, its in by_doc.items():
        pdf = find_source_pdf(doc_name, search_dirs)
        if not pdf or pdf.suffix.lower() != ".pdf":
            for it in its:
                it["hasImage"] = False
            continue
        try:
            doc = fitz.open(str(pdf))
        except Exception:
            for it in its:
                it["hasImage"] = False
            continue
        try:
            for it in its:
                pno = (it.get("page") or 1) - 1
                if pno < 0 or pno >= doc.page_count:
                    it["hasImage"] = False
                    continue
                page = doc[pno]
                rects = _locate(page, it.get("term", ""), it.get("snippet", ""))
                if rects:
                    it["line"] = _line_index(page, rects[0])
                    it["hasImage"] = True
                else:
                    it["hasImage"] = bool(pdf)  # can still show the full page
        finally:
            doc.close()


def render_region_png(
    pdf_path: Path,
    page_num: int,
    term: str,
    snippet: str,
    zoom: float = 2.2,
    pad: float = 46.0,
) -> bytes | None:
    """Render a cropped, highlighted screenshot of the located region as PNG."""
    try:
        import fitz  # type: ignore
    except Exception:
        return None
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return None
    try:
        pno = (page_num or 1) - 1
        if pno < 0 or pno >= doc.page_count:
            pno = 0
        page = doc[pno]
        rects = _locate(page, term, snippet)
        mat = fitz.Matrix(zoom, zoom)
        if rects:
            r = rects[0]
            # Union the first few adjacent hit rects (multi-word wraps).
            for extra in rects[1:3]:
                if abs(extra.y0 - r.y0) < 40:
                    r = r | extra
            page_rect = page.rect
            clip = fitz.Rect(
                page_rect.x0,
                max(page_rect.y0, r.y0 - pad),
                page_rect.x1,
                min(page_rect.y1, r.y1 + pad),
            )
            try:
                page.draw_rect(
                    r, color=(0.85, 0.35, 0.0), width=1.4, fill=(1, 0.85, 0.4),
                    fill_opacity=0.28,
                )
            except Exception:
                pass
            pix = page.get_pixmap(matrix=mat, clip=clip)
        else:
            # No match — return a downscaled full page so the reviewer still has
            # visual context.
            pix = page.get_pixmap(matrix=fitz.Matrix(1.3, 1.3))
        return pix.tobytes("png")
    except Exception:
        return None
    finally:
        doc.close()
