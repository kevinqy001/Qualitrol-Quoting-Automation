"""Convert circuit drawings / SLDs into a structured asset list.

Implements the data-package rule: *"Do not calculate BOQ quantity directly from
images. First convert drawings into this structured asset list"* (sheet
14_Drawing_Asset_List). This is a conservative, regex-based extractor over the
text layer of an SLD/GSLD PDF plus any structured "sensor quantity" tables. Its
outputs always carry a confidence and a note flagging that GIS/SLD layouts must
be human-verified (Compatibility Rule CR_004).

Enhanced SLD extraction (_extract_from_sld_text_enhanced) adds circuit breaker,
transformer, bus/PCC and feeder identification with scope-status detection
(New / Existing / Future / Provision / Unclear). An optional VLM path
(_extract_from_sld_vlm) renders the first SLD page to a PNG and asks Claude
Vision to identify assets with scope judgment; used when text confidence is low.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from .document_parser import DocSegment, ParsedDocument
from .schemas import DrawingAsset

_VOLTAGE_RE = re.compile(r"(\d{2,4})\s*kV", re.IGNORECASE)
_GIS_BAY_RE = re.compile(r"=C\d{2}\b")
_PD_SENSOR_RE = re.compile(r"-PD\d{1,2}\.\d{1,2}\b")
_CB_MECH_RE = re.compile(r"\bSP3-1\b")
_TOTAL_QTY_RE = re.compile(r"(\d{2,5})")

# --- Enhanced SLD patterns -------------------------------------------------- #
# Circuit breaker tags: 40CB7, 43CB4, 102CB1, etc.
_CB_TAG_RE = re.compile(r"\b\d{2,3}CB\d+\b", re.IGNORECASE)
# Transformer labels: SST-1, SST1, TR-1, TR1, TF-2, AT-1, GT-1, ICT-1.
# Intentionally excludes bare "T-1" / "T1" to avoid false positives (e.g. tap labels).
_TRANSFORMER_RE = re.compile(
    r"\b(?:SST|TR|TF|AT|GT|ICT|TX|XFMR|OLTC|TRAFO)-?\s*\d+[A-Z]?\b", re.IGNORECASE
)
# Bus / busbar labels: BUS-1, BUS1, BUSA, BB-1, BB1, BUSBAR-1.
_BUS_RE = re.compile(r"\b(?:BUSBAR|BUS|BB)-?\s*(?:\d+|[A-Z])\b", re.IGNORECASE)
# Feeder / bay labels: H01–H09, F01, L01, D01 etc. (letter + 2-3 digits).
# Only match when a voltage keyword is nearby (within same segment).
_FEEDER_LABEL_RE = re.compile(r"\b([A-Z])(\d{2,3})\b")
_FEEDER_VOLTAGE_NEARBY_RE = re.compile(r"\d{2,4}\s*kV", re.IGNORECASE)
# PCC labels: PCC-1, PCC1, PCC A.
_PCC_RE = re.compile(r"\bPCC-?\s*(?:\d+|[A-Z])\b", re.IGNORECASE)
# Voltage zone headings, e.g. "400kV GIS", "33kV Indoor", "11kV LVAC".
_VOLTAGE_ZONE_RE = re.compile(
    r"(\d{2,4})\s*kV\s+(?:GIS|switchgear|indoor|outdoor|bus|LVAC|incomer)",
    re.IGNORECASE,
)
# Scope status keywords.
_SCOPE_FUTURE_RE = re.compile(
    r"\b(?:future|fut\.|for future|future provision|future ext(?:ension)?)\b",
    re.IGNORECASE,
)
_SCOPE_PROVISION_RE = re.compile(r"\bprov(?:ision)?\b", re.IGNORECASE)
_SCOPE_EXISTING_RE = re.compile(r"\b(?:existing|exist\.)\b", re.IGNORECASE)
_SCOPE_NEW_RE = re.compile(
    r"\b(?:new\b|in[\s-]scope|under\s+contract|current\s+scope)", re.IGNORECASE
)


def _dominant_voltage(text: str) -> str:
    counts: dict[str, int] = {}
    for m in _VOLTAGE_RE.finditer(text):
        kv = f"{int(m.group(1))} kV"
        counts[kv] = counts.get(kv, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _scope_from_local_text(segment_text: str) -> str:
    """Infer a scope status string from a short text window around an asset tag.

    Returns one of: "New", "Existing", "Future", "Provision", "Unclear".
    """
    if _SCOPE_FUTURE_RE.search(segment_text):
        return "Future"
    if _SCOPE_PROVISION_RE.search(segment_text):
        return "Provision"
    if _SCOPE_EXISTING_RE.search(segment_text):
        return "Existing"
    if _SCOPE_NEW_RE.search(segment_text):
        return "New"
    return "Unclear"


def _drawing_zone_from_voltage(voltage_level: str) -> str:
    """Build a human-readable drawing area name from a voltage string."""
    if not voltage_level:
        return ""
    # e.g. "400 kV" -> "400kV Area", "33 kV" -> "33kV Area"
    kv = voltage_level.replace(" ", "")
    return f"{kv} Area"


def _extract_from_sld_text_enhanced(
    doc: ParsedDocument, project_id: str
) -> list[DrawingAsset]:
    """Identify circuit breakers, transformers, buses/PCCs and feeders from the
    SLD text layer using targeted regex patterns.

    Each asset is returned as a ``DrawingAsset`` with:
    - ``asset_type`` aligned to ``COUNT_FIELD_TO_ASSET_TYPE`` keys in constants.py
    - ``status`` inferred from local scope keywords (New / Existing / Future / …)
    - ``drawing_area`` from nearest voltage-zone heading
    - ``confidence`` ranging 0.45–0.65 (text-layer extraction is reliable for
      tag identification but scope status needs human confirmation)

    This function complements ``_extract_from_sld_pdf`` (GIS bay / PD sensor)
    and is always safe to call on any Drawing / SLD document.
    """
    text = doc.full_text
    drawing_id = doc.file_name

    # Build a segment-aware lookup: for each character position, which segment?
    # We use the segment text as context windows for scope detection.
    seg_texts = [seg.text for seg in doc.segments] if doc.segments else [text]

    # Map of voltage level -> drawing area (from zone headings found in full text).
    zone_map: dict[str, str] = {}
    for m in _VOLTAGE_ZONE_RE.finditer(text):
        kv = f"{int(m.group(1))} kV"
        zone_map[kv] = f"{kv.replace(' ', '')} GIS"

    def _area_for_voltage(vl: str) -> str:
        return zone_map.get(vl, _drawing_zone_from_voltage(vl))

    def _context_window(tag: str, window: int = 200) -> str:
        """Return the segment that contains ``tag``, or a short window in full text.

        Using the owning segment as context is more accurate than a fixed-width
        window in the concatenated full-text (avoids cross-segment scope bleed).
        """
        for seg in seg_texts:
            if tag in seg:
                return seg
        # Fall back to a window in full text if the tag is not in any segment.
        idx = text.find(tag)
        if idx == -1:
            return text[:400]
        start = max(0, idx - window)
        end = min(len(text), idx + len(tag) + window)
        return text[start:end]

    # ------------------------------------------------------------------ #
    # Helper: group individual tags by (asset_type, status) and emit one
    # DrawingAsset per group (quantity = count of in-group tags).
    # This is consistent with _extract_from_sld_pdf (GIS Bay pattern) and
    # lets Step 2 _asset_counts() correctly aggregate by taking max per type.
    # ------------------------------------------------------------------ #
    def _emit_grouped(
        tags: list[str],
        asset_type: str,
        base_confidence: float,
        tag_notes: str,
    ) -> list[DrawingAsset]:
        if not tags:
            return []
        dom_voltage = _dominant_voltage(text)
        # Group tags by their (status, voltage_level, drawing_area).
        groups: dict[tuple[str, str, str], list[str]] = {}
        for tag in tags:
            ctx = _context_window(tag)
            vl = _dominant_voltage(ctx) or dom_voltage
            status = _scope_from_local_text(ctx)
            area = _area_for_voltage(vl)
            key = (status, vl, area)
            groups.setdefault(key, []).append(tag)

        result: list[DrawingAsset] = []
        for (status, vl, area), group_tags in groups.items():
            tag_str = "; ".join(group_tags)
            result.append(
                DrawingAsset(
                    project_id=project_id,
                    drawing_id=drawing_id,
                    asset_tag=tag_str,
                    asset_type=asset_type,
                    voltage_level=vl,
                    quantity=float(len(group_tags)),
                    source_location=f"{drawing_id} (SLD text, {asset_type.lower()} tags)",
                    confidence=base_confidence,
                    drawing_area=area,
                    status=status,
                    notes=tag_notes.format(tags=tag_str, count=len(group_tags)),
                )
            )
        return result

    assets: list[DrawingAsset] = []

    # ------------------------------------------------------------------ #
    # 1. Circuit Breakers  (40CB7, 43CB4, 102CB1 …)
    # ------------------------------------------------------------------ #
    cb_tags = sorted(set(_CB_TAG_RE.findall(text)))
    assets.extend(_emit_grouped(
        cb_tags, "Circuit Breaker", 0.6,
        "{count} circuit breaker tag(s) ({tags}) identified from SLD text layer. "
        "Confirm in-scope breakers with engineering before use in BOQ.",
    ))

    # ------------------------------------------------------------------ #
    # 2. Transformers  (SST-1, TR-2, TF-1, AT-1, …)
    # ------------------------------------------------------------------ #
    tr_tags = sorted(set(_TRANSFORMER_RE.findall(text)))
    assets.extend(_emit_grouped(
        tr_tags, "Transformer", 0.55,
        "{count} transformer tag(s) ({tags}) identified from SLD text layer. "
        "Confirm oil/dry type and whether DGA / temperature monitoring is in scope.",
    ))

    # ------------------------------------------------------------------ #
    # 3. Buses / Busbars  (BUS-1, BUS-2, BUS A, BB-1 …)
    # ------------------------------------------------------------------ #
    bus_tags = sorted(set(_BUS_RE.findall(text)))
    assets.extend(_emit_grouped(
        bus_tags, "Bus", 0.5,
        "{count} bus label(s) ({tags}) identified from SLD text layer. "
        "Used as PCC / measurement point basis for PQ / DFR / PMU BOQ.",
    ))

    # ------------------------------------------------------------------ #
    # 4. PCCs  (PCC-1, PCC1, PCC A …)
    # ------------------------------------------------------------------ #
    pcc_tags = sorted(set(_PCC_RE.findall(text)))
    assets.extend(_emit_grouped(
        pcc_tags, "PCC", 0.55,
        "{count} PCC label(s) ({tags}) identified from SLD text layer. "
        "Used as measurement point basis for PQ / DFR / PMU BOQ.",
    ))

    # ------------------------------------------------------------------ #
    # 5. Feeders / Bays  (H01–H06, F01, L01, D01 … only when voltage nearby)
    # ------------------------------------------------------------------ #
    feeder_all: dict[str, tuple[str, str, str]] = {}  # tag -> (status, vl, area)
    seen_feeder_tags: set[str] = set()
    for seg in seg_texts:
        if not _FEEDER_VOLTAGE_NEARBY_RE.search(seg):
            continue
        feeder_matches = _FEEDER_LABEL_RE.findall(seg)
        vl = _dominant_voltage(seg)
        status = _scope_from_local_text(seg)
        area = _area_for_voltage(vl)
        for letter, num in feeder_matches:
            if letter.upper() not in ("H", "F", "L", "D", "J", "K"):
                continue
            ftag = f"{letter.upper()}{num}"
            if ftag not in seen_feeder_tags:
                seen_feeder_tags.add(ftag)
                feeder_all[ftag] = (status, vl, area)

    if feeder_all:
        # Group feeders by (status, vl, area).
        fgroups: dict[tuple[str, str, str], list[str]] = {}
        for ftag, key in feeder_all.items():
            fgroups.setdefault(key, []).append(ftag)
        for (status, vl, area), group_tags in fgroups.items():
            tag_str = "; ".join(sorted(group_tags))
            assets.append(
                DrawingAsset(
                    project_id=project_id,
                    drawing_id=drawing_id,
                    asset_tag=tag_str,
                    asset_type="Feeder",
                    voltage_level=vl,
                    quantity=float(len(group_tags)),
                    source_location=f"{drawing_id} (SLD text, feeder labels)",
                    confidence=0.45,
                    drawing_area=area,
                    status=status,
                    notes=(
                        f"{len(group_tags)} feeder/bay label(s) ({tag_str}) identified "
                        "near voltage reference in SLD text layer. "
                        "Confirm whether these feeders are PQ / DFR monitoring points."
                    ),
                )
            )

    return assets


def _extract_from_sld_pdf(doc: ParsedDocument, project_id: str) -> list[DrawingAsset]:
    text = doc.full_text
    text_lower = text.lower()
    assets: list[DrawingAsset] = []

    voltage = _dominant_voltage(text)
    is_gis = "gis" in text_lower or "gas insulated" in text_lower

    if is_gis:
        bays = sorted(set(_GIS_BAY_RE.findall(text)))
        # =C00/=C01/=C02 are typically bus-section / general columns, not feeder bays.
        feeder_bays = [b for b in bays if b not in {"=C00", "=C01", "=C02"}]
        pd_sensors = sorted(set(_PD_SENSOR_RE.findall(text)))

        assets.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=doc.file_name,
                asset_tag="; ".join(feeder_bays) if feeder_bays else "GIS",
                asset_type="GIS Bay",
                voltage_level=voltage,
                rating="",
                quantity=float(len(feeder_bays)) if feeder_bays else 0.0,
                connected_to="",
                monitoring_zone="GIS lineup",
                source_location=f"{doc.file_name} (SLD)",
                confidence=0.45,
                notes=(
                    "Bay count derived from =Cxx labels on the SLD text layer; "
                    "verify against the GIS layout drawing (CR_004). "
                    "Excludes =C00/=C01/=C02 (bus/general columns)."
                ),
            )
        )
        if pd_sensors:
            assets.append(
                DrawingAsset(
                    project_id=project_id,
                    drawing_id=doc.file_name,
                    asset_tag=f"{len(pd_sensors)} monitored PD sensors",
                    asset_type="PD Sensor",
                    voltage_level=voltage,
                    rating="",
                    quantity=float(len(pd_sensors)),
                    connected_to="GIS",
                    monitoring_zone="Partial discharge",
                    source_location=f"{doc.file_name} (SLD, -PDxx.yy tags)",
                    confidence=0.4,
                    notes=(
                        "Counted distinct -PDxx.yy monitoring-sensor tags. "
                        "Sensitivity-check-only -PD tags are not counted. "
                        "Confirm monitored vs spare sensors with engineering."
                    ),
                )
            )
    elif voltage:
        assets.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=doc.file_name,
                asset_type="Bus / Feeder",
                voltage_level=voltage,
                source_location=doc.file_name,
                confidence=0.3,
                notes="Voltage level detected; asset breakdown needs human review.",
            )
        )
    return assets


def _extract_from_quantity_table(
    doc: ParsedDocument, project_id: str
) -> list[DrawingAsset]:
    """Pick up explicit 'Summary of Sensor Quantity' style tables in docx/text.

    The header (with 'Sensor Quantity') and the data rows live in separate
    segments, so we gate on the whole document and then read the data rows.
    """
    if "sensor quantity" not in doc.full_text.lower() and (
        "sensor qty" not in doc.full_text.lower()
    ):
        return []

    assets: list[DrawingAsset] = []
    for seg in doc.segments:
        low = seg.text.lower()
        if "gis" not in low:
            continue
        numbers = [int(n) for n in re.findall(r"\b\d{2,6}\b", seg.text)]
        if not numbers:
            continue
        voltage = _dominant_voltage(seg.text)
        total = max(numbers)  # the total sensor quantity is the largest figure
        assets.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=doc.file_name,
                asset_tag="GIS sensor quantity (customer-stated)",
                asset_type="PD Sensor",
                voltage_level=voltage,
                quantity=float(total),
                monitoring_zone="Partial discharge",
                source_location=f"{doc.file_name}::{seg.location}",
                confidence=0.75,
                notes=(
                    "Customer-provided sensor quantity table. Use to "
                    "cross-check SLD-derived counts."
                ),
            )
        )
    return assets


def _render_pdf_page_to_b64(
    file_path: str, page_index: int = 0, dpi: int = 300, max_edge: int = 1568
) -> str:
    """Render a PDF page to a base64-encoded PNG string using PyMuPDF (fitz).

    Returns an empty string if PyMuPDF is not installed or rendering fails.

    Large-format SLDs (A1/A0) rendered at a fixed DPI become huge images that
    the vision model rejects (so the read silently fails). We therefore render
    at ``dpi`` but then cap the longest edge to ``max_edge`` pixels (Claude's
    recommended ~1568 px long-edge), scaling the zoom down as needed. This keeps
    big scanned drawings within the model's accepted image size.
    """
    try:
        import fitz  # type: ignore  # PyMuPDF (optional dependency)
    except ImportError:
        return ""
    try:
        import base64

        doc = fitz.open(file_path)
        if page_index >= len(doc):
            page_index = 0
        page = doc[page_index]
        zoom = dpi / 72.0  # 72 dpi is the PDF default
        rect = page.rect
        longest_px = max(rect.width, rect.height) * zoom
        if longest_px > max_edge and longest_px > 0:
            zoom *= max_edge / longest_px  # cap the long edge for the vision API
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")
        doc.close()
        return base64.b64encode(png_bytes).decode("ascii")
    except Exception:
        return ""


_PAGE_LOCATION_RE = re.compile(r"^page\s+(\d+)$", re.IGNORECASE)


def _page_text_map(doc: ParsedDocument) -> dict[int, str]:
    """Map 1-based PDF page numbers to their native extracted text."""
    result: dict[int, str] = {}
    for seg in doc.segments:
        match = _PAGE_LOCATION_RE.match(seg.location.strip())
        if match:
            result[int(match.group(1))] = seg.text
    return result


def _pdf_pages_requiring_vision(
    doc: ParsedDocument,
    *,
    low_text_chars: int = 180,
    min_image_ratio: float = 0.18,
) -> list[int]:
    """Return 0-based PDF pages likely to contain image-only requirements.

    A repeated logo must not trigger OCR. A page is selected only when it has a
    substantial image: either image coverage exceeds ``min_image_ratio``, or the
    native text layer is sparse and image coverage is at least 5%. The latter
    catches smaller screenshots while rejecting the ~1% letterhead logo seen in
    the supplied PD specification demo.
    """
    if not doc.file_path or not doc.file_path.lower().endswith(".pdf"):
        return []
    try:
        import fitz  # type: ignore
    except ImportError:
        return []

    native = _page_text_map(doc)
    already_ocr = {
        page_number
        for page_number, text in native.items()
        if "[VLM OCR of embedded image]" in text
    }
    candidates: list[int] = []
    try:
        pdf = fitz.open(doc.file_path)
        for page_index, page in enumerate(pdf):
            if page_index + 1 in already_ocr:
                continue
            page_area = page.rect.width * page.rect.height
            if page_area <= 0:
                continue
            image_area = 0.0
            xrefs = {image[0] for image in page.get_images(full=True)}
            for xref in xrefs:
                try:
                    image_area += sum(
                        rect.width * rect.height
                        for rect in page.get_image_rects(xref)
                    )
                except Exception:
                    continue
            image_ratio = min(1.0, image_area / page_area)
            text_chars = len(native.get(page_index + 1, "").strip())
            if image_ratio >= min_image_ratio or (
                text_chars < low_text_chars and image_ratio >= 0.05
            ):
                candidates.append(page_index)
        pdf.close()
    except Exception:
        return []
    return candidates


def _inject_page_ocr(doc: ParsedDocument, page_number: int, text: str) -> None:
    """Attach VLM text to its native page, preserving document order."""
    marker = "[VLM OCR of embedded image]"
    location = f"page {page_number}"
    for seg in doc.segments:
        if seg.location.lower() == location:
            if marker not in seg.text:
                seg.text = f"{seg.text.rstrip()}\n{marker}\n{text}"
            return

    new_segment = DocSegment(location=location, text=f"{marker}\n{text}")
    insert_at = len(doc.segments)
    for index, seg in enumerate(doc.segments):
        match = _PAGE_LOCATION_RE.match(seg.location.strip())
        if match and int(match.group(1)) > page_number:
            insert_at = index
            break
    doc.segments.insert(insert_at, new_segment)


def _extract_from_sld_vlm(
    doc: ParsedDocument, project_id: str, client
) -> list[DrawingAsset]:
    """Optional VLM extraction path: render SLD as image → Claude Vision.

    Uses ``_render_pdf_page_to_b64`` (requires PyMuPDF) to produce a PNG then
    calls ``llm_extract.extract_sld_assets_vlm``. Vision runs on the dedicated
    "vision" client (Claude Sonnet-5); the injected ``client`` is only a
    fallback (e.g. tests). Falls back to an empty list when no vision client is
    available, fitz is not installed, or rendering fails.
    """
    from . import llm_extract  # local import to avoid circular dependency
    from . import llm as _llm

    vision_client = _llm.get_client(role="vision")
    if not getattr(vision_client, "available", False):
        vision_client = client
    if not getattr(vision_client, "available", False):
        return []
    if not doc.file_path or not doc.file_path.lower().endswith(".pdf"):
        return []

    # Render at high DPI but cap the long edge (see _render_pdf_page_to_b64) so
    # large-format SLDs are still accepted by the vision model instead of
    # silently failing.
    image_b64 = _render_pdf_page_to_b64(doc.file_path, dpi=300)
    if not image_b64:
        return []

    result = llm_extract.extract_sld_assets_vlm(
        vision_client, image_b64, doc.file_name, project_id
    )
    return result or []


def augment_docs_with_image_text(docs: list[ParsedDocument], client) -> int:
    """Recover image-only specification pages and sparse drawing text via VLM.

    For ordinary PDFs, substantial embedded images are OCR'd page-by-page and
    injected beside the native page text. This handles mixed PDFs where only the
    key requirement pages are scans/screenshots. Sparse drawing-only projects
    retain the original first-page monitoring-label extraction.

    Calls are bounded and concurrent for predictable latency. All failures are
    safe no-ops; the deterministic text pipeline remains usable without VLM.
    Returns the number of pages/documents successfully augmented.
    """
    from . import llm as _llm
    from . import llm_extract

    if client is None or not getattr(client, "available", False):
        return 0
    vision_client = _llm.get_client(role="vision")
    if not getattr(vision_client, "available", False):
        vision_client = client
    if not getattr(vision_client, "available", False):
        return 0

    try:
        max_pages = max(1, int(os.getenv("QUALITROL_VLM_OCR_MAX_PAGES", "12")))
    except ValueError:
        max_pages = 12
    try:
        concurrency = max(1, int(os.getenv("QUALITROL_VLM_OCR_CONCURRENCY", "4")))
    except ValueError:
        concurrency = 4

    page_jobs: list[tuple[ParsedDocument, int]] = []
    for doc in docs:
        if doc.doc_type == "Drawing / SLD":
            continue
        candidates = _pdf_pages_requiring_vision(doc)
        if len(candidates) > max_pages:
            logging.warning(
                "VLM OCR capped at %d/%d image-heavy pages for %s; set "
                "QUALITROL_VLM_OCR_MAX_PAGES to raise the safety limit",
                max_pages, len(candidates), doc.file_name,
            )
        page_jobs.extend((doc, page) for page in candidates[:max_pages])

    def _read_page(job: tuple[ParsedDocument, int]) -> tuple[ParsedDocument, int, str]:
        doc, page_index = job
        image_b64 = _render_pdf_page_to_b64(
            doc.file_path, page_index=page_index, dpi=300
        )
        if not image_b64:
            return doc, page_index, ""
        text = llm_extract.extract_document_page_text_vlm(
            vision_client, image_b64, doc.file_name, page_index + 1
        )
        # If the dedicated vision deployment exhausts its retries, the judge
        # client (Claude Opus) is an independent fail-safe for this page.
        if (
            not text
            and client is not vision_client
            and getattr(client, "available", False)
        ):
            text = llm_extract.extract_document_page_text_vlm(
                client, image_b64, doc.file_name, page_index + 1
            )
        return doc, page_index, text or ""

    if concurrency > 1 and len(page_jobs) > 1:
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(page_jobs))
        ) as executor:
            page_results = list(executor.map(_read_page, page_jobs))
    else:
        page_results = [_read_page(job) for job in page_jobs]

    augmented = 0
    for doc, page_index, text in page_results:
        if text:
            _inject_page_ocr(doc, page_index + 1, text)
            augmented += 1
        else:
            logging.warning(
                "VLM OCR returned no text for %s page %d; native text remains",
                doc.file_name, page_index + 1,
            )

    # Preserve the original drawing-only augmentation. A real prose spec/email
    # means drawings do not need this extra scenario-detection OCR pass.
    prose_chars = sum(
        len(d.full_text)
        for d in docs
        if d.doc_type in ("Project Specification", "Raw Email")
    )
    if prose_chars < 500:
        for doc in docs:
            if doc.doc_type != "Drawing / SLD":
                continue
            if not doc.file_path or not doc.file_path.lower().endswith(".pdf"):
                continue
            image_b64 = _render_pdf_page_to_b64(doc.file_path, dpi=300)
            if not image_b64:
                continue
            text = llm_extract.extract_sld_text_vlm(
                vision_client, image_b64, doc.file_name
            )
            if not text:
                continue
            doc.segments.append(
                DocSegment(location="VLM drawing text (page 1)", text=text)
            )
            augmented += 1
    return augmented


def augment_docs_with_sld_text(docs: list[ParsedDocument], client) -> int:
    """Backward-compatible alias for the expanded image-text augmentation."""
    return augment_docs_with_image_text(docs, client)


def _merge_assets(
    text_assets: list[DrawingAsset], vlm_assets: list[DrawingAsset]
) -> list[DrawingAsset]:
    """Merge text-layer and VLM assets, deduplicating by (asset_type, asset_tag).

    When both sources produce the same asset tag, the VLM result takes
    precedence (higher confidence) and inherits the text-layer's notes if the
    VLM notes are empty.
    """
    if not vlm_assets:
        return text_assets
    if not text_assets:
        return vlm_assets

    # Index text assets for fast lookup.
    text_index: dict[tuple[str, str], DrawingAsset] = {}
    for a in text_assets:
        key = (a.asset_type.lower(), a.asset_tag.lower())
        text_index[key] = a

    merged = list(vlm_assets)
    vlm_keys = {(a.asset_type.lower(), a.asset_tag.lower()) for a in vlm_assets}

    for a in text_assets:
        key = (a.asset_type.lower(), a.asset_tag.lower())
        if key not in vlm_keys:
            merged.append(a)

    return merged


def _text_confidence_avg(assets: list[DrawingAsset]) -> float:
    """Average confidence of assets that came from text-layer extraction."""
    values = [a.confidence for a in assets if a.confidence > 0]
    return sum(values) / len(values) if values else 0.0


def extract_drawing_assets(
    docs: list[ParsedDocument],
    project_id: str,
    llm_client=None,
) -> list[DrawingAsset]:
    """Build the Drawing Asset List (sheet 14) from all project documents.

    Extraction strategy:
    1. Existing GIS bay / PD sensor extraction via ``_extract_from_sld_pdf``
       (regex on ``=Cxx`` / ``-PDxx.yy`` tags).
    2. Enhanced text extraction via ``_extract_from_sld_text_enhanced`` for
       circuit breakers, transformers, buses, PCCs and feeders.
    3. Customer-stated sensor quantity table via ``_extract_from_quantity_table``.
    4. Optional VLM path via ``_extract_from_sld_vlm`` when:
       - ``llm_client`` is provided and available, AND
       - the text-layer confidence is low (avg < 0.5) OR no CB/Transformer/Bus
         was found by the text extractor.
       VLM results are merged with text results (VLM takes precedence on same
       asset tags).

    Args:
        docs: List of ``ParsedDocument`` objects from the project folder.
        project_id: Project identifier string.
        llm_client: Optional LLM client from ``qualitrol_core.llm.get_client()``.
                    When ``None``, the VLM path is skipped silently.
    """
    assets: list[DrawingAsset] = []

    for doc in docs:
        if doc.doc_type == "Drawing / SLD":
            # Step 1: legacy GIS bay / PD sensor extraction.
            legacy = _extract_from_sld_pdf(doc, project_id)
            assets.extend(legacy)

            # Step 2: enhanced text extraction (CB / TR / Bus / PCC / Feeder).
            enhanced = _extract_from_sld_text_enhanced(doc, project_id)

            # Step 3: optional VLM augmentation.
            vlm: list[DrawingAsset] = []
            if llm_client is not None:
                # Use VLM when text confidence is low or no new asset types found.
                new_types = {a.asset_type for a in enhanced}
                important_types = {"Circuit Breaker", "Transformer", "Bus", "PCC"}
                low_confidence = _text_confidence_avg(enhanced) < 0.5
                missing_important = not (important_types & new_types)
                if low_confidence or missing_important:
                    vlm = _extract_from_sld_vlm(doc, project_id, llm_client)

            # Merge enhanced + VLM (VLM wins on duplicates).
            sld_assets = _merge_assets(enhanced, vlm)
            assets.extend(sld_assets)

        # Step 4: quantity tables in any document type.
        assets.extend(_extract_from_quantity_table(doc, project_id))

    return assets
