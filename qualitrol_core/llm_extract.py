"""LLM augmentation layer (Claude Opus 4.8 via Azure AI Foundry).

These helpers sit on top of the deterministic rules engine. The rules layer
provides recall (and grounding evidence); the LLM adds precision and
explanations. Every function:
  * is a no-op when the LLM is unavailable (returns None),
  * is grounded in the controlled vocabulary + rules-extracted evidence,
  * fails safe (any error -> None) so the pipeline always completes.

Used by:
  Step 1 -> refine_scenarios(), extract_requirements()
  Step 1 -> extract_sld_assets_vlm()  (optional VLM path for SLD drawings)
  Step 2 -> explain_matches(), suggest_missing_info()
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from . import matching
from .document_parser import ParsedDocument
from .schemas import DrawingAsset


def _int_env(name: str, default: int) -> int:
    """Read a small integer tuning knob from the environment (fail-safe)."""
    try:
        return int((os.getenv(name) or "").strip())
    except (TypeError, ValueError):
        return default

_VALID_REQ_TYPES = {"Must-have", "Preferred", "Reference", "Quantity Basis", "Unknown"}

# A table-of-contents / index line: a section number (or dotted leaders) followed
# by a title and a trailing page number, e.g. "3.3.1.16 HVAC system 39" or
# "2.2  General Requirements ......... 7". These pages list *where* requirements
# live, not the requirements themselves, so they must never become evidence.
_TOC_LINE_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+.+?\s+\d{1,4}\s*$")
_TOC_LEADER_RE = re.compile(r"\.{3,}\s*\d{1,4}\s*$")


def looks_like_table_of_contents(text: str) -> bool:
    """Heuristic: is this segment a table-of-contents / index page?

    A TOC page is dominated by "section title + page number" lines (optionally
    with dot leaders). Requirement body pages are prose and rarely trip this, so
    we require both a high ratio *and* an absolute count of such lines to avoid
    dropping a genuine content page that happens to contain a short numbered
    list. The explicit "table of contents" heading is a strong signal on its own
    once a few index-style lines are present.
    """
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 6:
        return False
    hits = sum(
        1 for ln in lines if _TOC_LINE_RE.match(ln) or _TOC_LEADER_RE.search(ln)
    )
    if "table of contents" in text.lower() and hits >= 3:
        return True
    return hits >= 6 and hits / len(lines) >= 0.5


def _with_extra_rules(system: str, extra_instructions: str) -> str:
    """Append operator-provided extra rules/constraints to a system prompt.

    The injected block is clearly delimited and scoped *below* the controlled
    catalog so it can tighten or clarify behaviour without letting free text
    override the grounded data package. Empty input is a no-op.
    """
    extra = (extra_instructions or "").strip()
    if not extra:
        return system
    return (
        system
        + "\n\n=== ADDITIONAL DOMAIN RULES (operator-provided) ===\n"
        "Apply the following rules when they do not contradict the controlled "
        "catalog or the grounded evidence. They refine precision; they must not "
        "invent scenarios, metrics, or values that the evidence does not support.\n"
        + extra
    )


def build_context(docs: list[ParsedDocument], max_chars: int = 9000) -> str:
    """Bounded, LLM-friendly text context.

    Prefers spec/email text; drawings are noisy so they are trimmed hard.
    """
    chunks: list[str] = []
    budget = max_chars
    # Non-drawing docs first (richer prose), then a trimmed drawing sample.
    ordered = sorted(docs, key=lambda d: d.doc_type == "Drawing / SLD")
    for doc in ordered:
        if budget <= 0:
            break
        per_doc = 1500 if doc.doc_type == "Drawing / SLD" else min(4000, budget)
        text = doc.full_text[:per_doc]
        block = f"\n----- DOCUMENT: {doc.file_name} ({doc.doc_type}) -----\n{text}"
        chunks.append(block)
        budget -= len(block)
    return "".join(chunks)


# --------------------------------------------------------------------------- #
# Step 1 (grounded mode) - locate requirements & products from the family/model
# catalog directly, WITHOUT scenario-keyword matching.
#
# Motivation: the scenario/synonym keyword vocabulary is broad and over-matches
# non-requirement fragments. This path hands the analysis LLM the
# controlled Product Family + Product Model catalog (plus the metric dictionary)
# and asks it to read the customer documents and pin down (1) which product
# families/models are genuinely in scope and (2) the specific, valuable stated
# requirements — each grounded in a verbatim quote so we can relocate it in the
# source for the Spec Review UI. Everything is validated against the catalog so
# free text can never inject un-grounded families/models/metrics.
# --------------------------------------------------------------------------- #
def _families_context(dp) -> list[dict]:
    return [
        {
            "family_id": f.family_id,
            "family_name": f.family_name,
            "product_line": f.product_line,
            "primary_asset_type": f.primary_asset_type,
            "capabilities": f.typical_capabilities,
            "applicable_scenarios": list(f.applicable_scenarios),
        }
        for f in dp.families.values()
    ]


def _products_context(dp) -> list[dict]:
    return [
        {
            "product_id": p.product_id,
            "model": p.model,
            "family_id": p.family_id,
            "description": p.description,
            "standards": p.supported_standards,
            "protocols": p.protocols,
        }
        for p in dp.products.values()
    ]


def _grounded_chunks(
    docs: list[ParsedDocument], chunk_chars: int = 40000
) -> list[str]:
    """Split the FULL customer documents into page/segment-aligned text chunks.

    Unlike ``build_context`` (which truncates each doc to a few thousand chars —
    enough to only ever see a long tender's cover + table of contents), this
    walks every segment so the whole body reaches the model across successive
    chunks. Cover/index noise is removed by dropping table-of-contents pages, and
    Drawing/SLD docs are trimmed hard (their content is read by the VLM path, not
    here). Chunks are never split mid-page so a verbatim quote can be relocated.
    """
    ordered = sorted(docs, key=lambda d: d.doc_type == "Drawing / SLD")
    chunks: list[str] = []
    for doc in ordered:
        header = f"----- DOCUMENT: {doc.file_name} ({doc.doc_type}) -----\n"
        if doc.doc_type == "Drawing / SLD":
            sample = doc.full_text[:1500].strip()
            if sample:
                chunks.append(header + sample)
            continue
        buf: list[str] = []
        buf_len = 0
        for seg in doc.segments:
            seg_text = seg.text.strip()
            if not seg_text or looks_like_table_of_contents(seg.text):
                continue
            if buf and buf_len + len(seg_text) > chunk_chars:
                chunks.append(header + "\n".join(buf))
                buf, buf_len = [], 0
            buf.append(seg_text)
            buf_len += len(seg_text) + 1
        if buf:
            chunks.append(header + "\n".join(buf))
    return chunks


# --------------------------------------------------------------------------- #
# Keyword-anchored region selection (hybrid prefilter for grounded mode)
# --------------------------------------------------------------------------- #
# Instead of reading every page of a long tender with the LLM locator, use the
# deterministic controlled-term net to LOCATE the candidate regions, expand each
# by a few neighbouring segments for context, drop table-of-contents pages, and
# only send those regions to the model. The keyword net is deliberately a HIGH-
# RECALL selector here (precision is still the model's job inside each region),
# so we cut the model's input volume / call count without dropping requirements.
# A coverage guard + empty-result fallback (in ``locate_requirements_grounded``)
# revert to reading the whole document whenever the prefilter would not help.
_ANCHOR_MIN_LEN = 2


def _anchor_terms(dp) -> list[str]:
    """Distinctive controlled terms used to anchor candidate regions.

    Mapped synonym raw-terms plus each scenario's NON-ambiguous keywords. Generic
    ambiguous keywords (relay/alarm/voltage/current/...) are excluded on purpose:
    as a region net we want recall, but a term that matches nearly every page
    (so it cannot discriminate a region) is worthless for locating one. Those
    weak terms are still judged by the model inside the selected regions.
    """
    terms: set[str] = set()
    for scenario in dp.scenarios.values():
        for kw in scenario.keywords:
            k = (kw or "").strip().lower()
            if len(k) >= _ANCHOR_MIN_LEN and not matching.is_ambiguous_keyword(k):
                terms.add(k)
    for syn in dp.synonyms:
        t = (syn.raw_term or "").strip().lower()
        if len(t) >= _ANCHOR_MIN_LEN:
            terms.add(t)
    # Longest first so a multi-word phrase anchors before its substrings.
    return sorted(terms, key=len, reverse=True)


def _find_anchor_term(text_lower: str, term: str) -> int:
    """Locate an anchor as a lexical term, allowing only a simple plural.

    ``matching.find_term`` intentionally uses substring matching for long terms,
    which is useful during broad evidence extraction but too noisy for region
    selection: the valid product name ``INFORMA`` would match ``information``.
    Anchors therefore require alphanumeric boundaries. A trailing ``s``/``es``
    remains acceptable so singular catalog terms still locate plural prose.
    """
    value = (term or "").strip().lower()
    if not value:
        return -1
    prefix = r"(?<![a-z0-9])" if value[0].isalnum() else ""
    suffix = r"(?:s|es)?(?![a-z0-9])" if value[-1].isalnum() else ""
    match = re.search(prefix + re.escape(value) + suffix, text_lower)
    return match.start() if match else -1


def _segment_is_anchor(text: str, terms: list[str]) -> bool:
    """True if a segment carries a distinctive term that is not out-of-scope."""
    text_lower = text.lower()
    for term in terms:
        idx = _find_anchor_term(text_lower, term)
        if idx >= 0 and not matching.in_exclusion_context(text, idx):
            return True
    return False


def make_page_prefilter(dp) -> Callable[[str], bool]:
    """Build a page-level screen (controlled-term net) for the parse layer.

    Returns a callable ``keep(text) -> bool`` used by ``document_parser`` to
    retain only pages that carry a distinctive catalog term (or recovered VLM
    OCR) when a document is very large. Same vocabulary as the grounded
    anchored prefilter, so selection is consistent across the two layers.
    """
    terms = _anchor_terms(dp)

    def _keep(text: str) -> bool:
        if not text or not text.strip():
            return False
        if "[VLM OCR of embedded image]" in text:
            return True
        return _segment_is_anchor(text, terms)

    return _keep


def _chunk_anchor_score(chunk: str, terms: list[str]) -> int:
    """Count distinct anchor terms present in a chunk (region relevance score)."""
    low = chunk.lower()
    score = 0
    for term in terms:
        if _find_anchor_term(low, term) >= 0:
            score += 1
    return score


def _merge_intervals(idxs: list[int], radius: int, n: int) -> list[tuple[int, int]]:
    """Expand each anchor index by +/- ``radius`` segments and merge overlaps."""
    if not idxs:
        return []
    spans = [(max(0, i - radius), min(n - 1, i + radius)) for i in sorted(idxs)]
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        plo, phi = merged[-1]
        if lo <= phi + 1:  # overlapping or directly adjacent -> one region
            merged[-1] = (plo, max(phi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _doc_context(seg_count: int, char_count: int, override: int | None) -> int:
    """Neighbouring-segment radius to keep around each anchor for THIS doc.

    Adaptive to segment granularity: a page-based doc (PDF) already carries a
    full page of context per segment, so radius 0 keeps only the signal pages; a
    paragraph-based doc (docx/txt) has tiny segments, so we widen the window to
    keep the sentences around each hit. An explicit env override wins.
    """
    if override is not None:
        return override
    avg = char_count / max(1, seg_count)
    return 0 if avg >= 1000 else 2


def _anchored_chunks(
    docs: list[ParsedDocument], dp, *, chunk_chars: int = 40000,
    context: int | None = None,
) -> tuple[list[str], dict]:
    """Keyword-anchored region chunks for the grounded locator.

    Returns ``(chunks, stats)`` where ``stats`` reports how much of the prose
    corpus survived the prefilter (so the caller can apply a coverage guard and
    log the reduction). Chunk packing mirrors ``_grounded_chunks`` so the model
    sees the same shape; only the *selection* of text differs. ``context`` is the
    neighbouring-segment radius kept around each anchor; ``None`` picks it per
    doc from segment granularity (see ``_doc_context``). Drawing/SLD docs keep
    the same trimmed sample (their content is read by the VLM path).
    """
    terms = _anchor_terms(dp)
    ordered = sorted(docs, key=lambda d: d.doc_type == "Drawing / SLD")
    chunks: list[str] = []
    stats = {
        "total_chars": 0, "selected_chars": 0,
        "total_segments": 0, "anchor_segments": 0, "selected_segments": 0,
    }
    for doc in ordered:
        header = f"----- DOCUMENT: {doc.file_name} ({doc.doc_type}) -----\n"
        if doc.doc_type == "Drawing / SLD":
            sample = doc.full_text[:1500].strip()
            if sample:
                chunks.append(header + sample)
            continue
        segs = doc.segments
        n = len(segs)
        doc_chars = sum(len(s.text) for s in segs)
        stats["total_segments"] += n
        stats["total_chars"] += doc_chars
        anchor_idx = [
            i for i, seg in enumerate(segs)
            if seg.text.strip() and (
                "[VLM OCR of embedded image]" in seg.text
                or _segment_is_anchor(seg.text, terms)
            )
        ]
        stats["anchor_segments"] += len(anchor_idx)
        if not anchor_idx:
            continue
        radius = _doc_context(n, doc_chars, context)
        buf: list[str] = []
        buf_len = 0
        for lo, hi in _merge_intervals(anchor_idx, radius, n):
            for i in range(lo, hi + 1):
                seg_text = segs[i].text.strip()
                if not seg_text or looks_like_table_of_contents(segs[i].text):
                    continue
                stats["selected_segments"] += 1
                stats["selected_chars"] += len(seg_text)
                if buf and buf_len + len(seg_text) > chunk_chars:
                    chunks.append(header + "\n".join(buf))
                    buf, buf_len = [], 0
                buf.append(seg_text)
                buf_len += len(seg_text) + 1
        if buf:
            chunks.append(header + "\n".join(buf))
    return chunks, stats


def _parse_grounded_response(data: dict, dp) -> tuple[list[dict], list[dict]]:
    """Validate one grounded LLM JSON response against the controlled catalog.

    Returns ``(products, requirements)`` with only catalog-valid family/product/
    metric ids kept. Shared by every chunk in the map-reduce locator.
    """
    valid_fam = set(dp.families.keys())
    valid_prod = set(dp.products.keys())
    valid_metric = set(dp.metrics.keys())

    def _conf(v, default=0.6) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return default

    out_products: list[dict] = []
    for item in data.get("products", []) or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("product_id", "")).strip()
        fid = str(item.get("family_id", "")).strip()
        if pid and pid not in valid_prod:
            pid = ""
        if pid and not fid:
            fid = dp.products[pid].family_id
        if fid and fid not in valid_fam:
            fid = ""
        if not fid and not pid:
            continue
        if item.get("in_scope") is False:
            continue
        out_products.append({
            "product_id": pid,
            "family_id": fid,
            "confidence": _conf(item.get("confidence")),
            "evidence_quote": str(item.get("evidence_quote", "")).strip()[:300],
            "rationale": str(item.get("rationale", "")).strip(),
        })

    out_reqs: list[dict] = []
    for item in data.get("requirements", []) or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("product_id", "")).strip()
        fid = str(item.get("family_id", "")).strip()
        if pid and pid not in valid_prod:
            pid = ""
        if pid and not fid:
            fid = dp.products[pid].family_id
        if fid and fid not in valid_fam:
            fid = ""
        mid = str(item.get("metric_id", "")).strip()
        if mid and mid not in valid_metric:
            mid = ""
        if not fid and not pid and not mid:
            continue
        # Mirror the products filter: an explicit self-check the model must make
        # per requirement, not just once per product. Catches borderline items
        # (plant/civil/commercial text that happens to name a valid metric) that
        # the "be precise, not exhaustive" prose rule alone did not exclude.
        if item.get("in_scope") is False:
            continue
        rtype = str(item.get("requirement_type", "")).strip()
        if rtype not in _VALID_REQ_TYPES:
            rtype = "Reference"
        out_reqs.append({
            "family_id": fid,
            "product_id": pid,
            "metric_id": mid,
            "value": str(item.get("value", "")).strip(),
            "unit": str(item.get("unit", "")).strip(),
            "requirement_type": rtype,
            "evidence_quote": str(item.get("evidence_quote", "")).strip()[:300],
            "confidence": _conf(item.get("confidence")),
            "rationale": str(item.get("rationale", "")).strip(),
        })

    return out_products, out_reqs


def _merge_grounded(
    products: list[dict], requirements: list[dict]
) -> tuple[list[dict], list[dict]]:
    """De-duplicate products/requirements gathered across chunks, keeping the
    highest-confidence instance of each (a product/requirement can legitimately
    be quoted on several pages)."""
    prod_by_key: dict[tuple, dict] = {}
    for p in products:
        key = (p["family_id"], p["product_id"])
        cur = prod_by_key.get(key)
        if cur is None or p["confidence"] > cur["confidence"]:
            prod_by_key[key] = p

    req_by_key: dict[tuple, dict] = {}
    for r in requirements:
        key = (r["family_id"], r["product_id"], r["metric_id"],
               r["value"].strip().lower())
        cur = req_by_key.get(key)
        if cur is None or r["confidence"] > cur["confidence"]:
            req_by_key[key] = r

    merged_products = sorted(prod_by_key.values(),
                             key=lambda x: -x["confidence"])
    merged_reqs = sorted(req_by_key.values(), key=lambda x: -x["confidence"])
    return merged_products, merged_reqs


def locate_requirements_grounded(
    client, dp, docs: list[ParsedDocument],
    extra_instructions: str = "", chunk_chars: int = 40000,
    stats_out: Optional[dict] = None,
) -> Optional[dict]:
    """LLM-driven, catalog-grounded requirement & product locator (Step 1).

    Reads the WHOLE of every document via a chunked map-reduce: each page/segment
    chunk is analysed against the full controlled catalog and the per-chunk
    findings are merged. This replaces the previous single truncated call, which
    only ever saw a long tender's cover + table of contents and therefore
    anchored all "evidence" to the index page.

    Returns ``{"products": [...], "requirements": [...]}`` validated against the
    controlled Product Family / Product Model / Metric catalogs, or ``None`` when
    the LLM is unavailable or nothing usable comes back (caller falls back to the
    keyword engine). Every item carries a verbatim ``evidence_quote`` the caller
    relocates in the source documents for traceability.
    """
    if not client.available:
        return None

    families = _families_context(dp)
    products = _products_context(dp)
    metrics = [
        {"metric_id": m.metric_id, "name": m.standard_name, "unit": m.unit}
        for m in dp.metrics.values()
    ]
    # Keyword-anchored prefilter (default on): locate candidate regions with the
    # controlled-term net and only send those to the LLM. Falls back to reading
    # the whole document when the prefilter finds nothing or would not meaningfully
    # reduce the volume (coverage guard), so recall is never sacrificed for speed.
    # Only large tenders are worth prefiltering: a small/medium spec is ~1 LLM
    # call already, so reading it whole is fast AND avoids the recall risk of a
    # keyword net (fragmented tables, junk-term hits, image-only pages) dropping
    # the wrong pages. Gate on an absolute prose size, not just a coverage ratio.
    prose_chars = sum(
        len(d.full_text) for d in docs if d.doc_type != "Drawing / SLD"
    )
    min_chars = _int_env("QUALITROL_GROUNDED_MIN_CHARS", 80000)
    # A "huge" tender (e.g. a 2000+ page reference standard) must never fall back
    # to reading the whole body: that produces 100+ LLM calls that throttle,
    # time out and blow the job's wall-clock / memory budget. Past this prose
    # size we force the anchored prefilter (regardless of coverage) and hard-cap
    # the number of chunks, trading a little recall for a job that completes.
    huge_chars = _int_env("QUALITROL_GROUNDED_HUGE_CHARS", 1_200_000)
    is_huge = prose_chars > huge_chars
    if is_huge:
        chunk_chars = _int_env("QUALITROL_GROUNDED_HUGE_CHUNK_CHARS", 80000)
    stats_meta = {"mode": "full", "prose_chars": prose_chars, "coverage": 1.0,
                  "is_huge": is_huge, "capped": False}
    chunks: list[str] = []
    if os.getenv("QUALITROL_GROUNDED_ANCHORED", "1") != "0" and prose_chars > min_chars:
        raw_ctx = (os.getenv("QUALITROL_GROUNDED_CONTEXT") or "").strip()
        context = max(0, int(raw_ctx)) if raw_ctx.isdigit() else None
        a_chunks, stats = _anchored_chunks(
            docs, dp, chunk_chars=chunk_chars, context=context
        )
        total = stats["total_chars"] or 1
        coverage = stats["selected_chars"] / total
        stats_meta["coverage"] = round(coverage, 3)
        # Normally the coverage guard reverts to full-read when the net keeps
        # nearly everything (recall safety); for a huge doc we keep the anchored
        # selection anyway (survival over the marginal recall of dead weight).
        if a_chunks and (coverage <= 0.80 or is_huge):
            chunks = a_chunks
            stats_meta["mode"] = "anchored"
            logging.info(
                "grounded prefilter=anchored: %d/%d segments, %d/%d chars "
                "(%.0f%% kept), %d LLM chunks%s",
                stats["selected_segments"], stats["total_segments"],
                stats["selected_chars"], stats["total_chars"], coverage * 100,
                len(a_chunks), " [huge: forced]" if is_huge else "",
            )
        else:
            logging.info(
                "grounded prefilter skipped (coverage=%.0f%%, anchored_chunks=%d)"
                " -> reading full document",
                coverage * 100, len(a_chunks),
            )
    if not chunks:
        chunks = _grounded_chunks(docs, chunk_chars=chunk_chars)
    if not chunks:
        return None

    # Hard cap on the number of LLM calls so an enormous tender cannot spawn an
    # unbounded fan-out. When exceeded, keep the most requirement-dense chunks
    # (ranked by how many distinct controlled terms they carry). 0 disables.
    max_chunks = _int_env("QUALITROL_GROUNDED_MAX_CHUNKS", 40)
    if max_chunks > 0 and len(chunks) > max_chunks:
        terms = _anchor_terms(dp)
        ranked = sorted(
            chunks, key=lambda c: _chunk_anchor_score(c, terms), reverse=True
        )
        chunks = ranked[:max_chunks]
        stats_meta["capped"] = True
        logging.info(
            "grounded chunks capped to %d most requirement-dense (of %d); "
            "raise QUALITROL_GROUNDED_MAX_CHUNKS to lift",
            max_chunks, len(ranked),
        )

    system = (
        "You are a senior Qualitrol application engineer doing quotation take-off. "
        "You are given a PORTION of the customer's project documents plus "
        "Qualitrol's CONTROLLED catalog of Product Families and Product Models "
        "(and a metric dictionary). "
        "Your job is to read the documents and pin down, precisely:\n"
        "  (1) which Qualitrol product families/models are GENUINELY required by "
        "THIS project, and\n"
        "  (2) the specific, valuable technical REQUIREMENTS stated in the documents "
        "that justify those products or map to a controlled metric.\n\n"
        "Hard rules:\n"
        "- Ground every item in the documents. Provide a short VERBATIM quote "
        "(copied exactly from the text, <=200 chars) as evidence for each item. "
        "Never invent products, metrics, or values.\n"
        "- Quote the ACTUAL requirement sentence from the body text. IGNORE the "
        "table of contents, indexes, cover/signature pages, and running "
        "headers/footers. NEVER use a line that is only a section title followed "
        "by a page number (e.g. '3.3.1.16 HVAC system 39') as evidence.\n"
        "- Use ONLY family_id / product_id / metric_id values from the provided "
        "catalog. If a requirement fits a family but no specific model, give the "
        "family_id and leave product_id empty.\n"
        "- Be precise, not exhaustive: include an item only if the text genuinely "
        "supports Qualitrol supplying/monitoring it in THIS project. IGNORE generic "
        "background, and IGNORE anything the text marks as out-of-scope, future, "
        "provision, optional, or supplied by another party.\n"
        "- If this portion contains nothing relevant, return empty lists.\n"
        "- A component of the plant being monitored (e.g. a breaker/CT/VT that is "
        "part of the GIS) is NOT itself a monitoring product unless the customer "
        "asks to MONITOR it.\n"
        "- For EVERY requirement, explicitly self-check and set in_scope=true only if "
        "the value/parameter genuinely belongs to a Qualitrol monitoring deliverable "
        "in THIS project (same bar as the product rule above). Set in_scope=false for "
        "requirements about the plant/asset itself (not the monitoring system), "
        "civil/electrical/mechanical works, commercial/warranty/service terms, or "
        "anything out-of-scope/future/optional/supplied-by-others — do not include "
        "those at all.\n"
        "Respond with STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)

    catalog_block = (
        "Controlled Product Family catalog:\n"
        + json.dumps(families, ensure_ascii=False)
        + "\n\nControlled Product Model catalog:\n"
        + json.dumps(products, ensure_ascii=False)
        + "\n\nControlled Metric dictionary (map requirement values to these):\n"
        + json.dumps(metrics, ensure_ascii=False)
    )
    return_form = (
        "\n\nReturn JSON exactly of this form:\n"
        '{"products":[{"product_id":"","family_id":"","in_scope":true,'
        '"confidence":0.0,"evidence_quote":"","rationale":"one sentence"}],'
        '"requirements":[{"family_id":"","product_id":"","metric_id":"","value":"",'
        '"unit":"","requirement_type":"Must-have|Preferred|Reference|Quantity Basis",'
        '"in_scope":true,"evidence_quote":"","confidence":0.0,'
        '"rationale":"one sentence"}]}'
    )

    total_chunks = len(chunks)

    def _run_chunk(job: tuple[int, str]) -> Optional[dict]:
        i, chunk = job
        user = (
            catalog_block
            + f"\n\nCustomer project documents (part {i} of {total_chunks}):\n"
            + chunk
            + return_form
        )
        try:
            data = client.complete_json(system, user, max_tokens=8192)
        except Exception:  # noqa: BLE001 - fail safe: skip this chunk, keep others
            return None
        # An empty string from the client (transient error / timeout after the
        # client's own retries) parses to None; treat that as a failed chunk so
        # the caller can surface partial-coverage instead of a silent gap.
        return data if isinstance(data, dict) else None

    # Chunks are independent, so fan them out concurrently (the Responses client
    # is synchronous but thread-safe for parallel requests) to cut wall-clock
    # time on long tenders. Set QUALITROL_GROUNDED_CONCURRENCY=1 to serialise.
    jobs = list(enumerate(chunks, start=1))
    max_workers = max(1, _int_env("QUALITROL_GROUNDED_CONCURRENCY", 4))
    if max_workers > 1 and total_chunks > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, total_chunks)) as ex:
            responses = list(ex.map(_run_chunk, jobs))
    else:
        responses = [_run_chunk(job) for job in jobs]

    all_products: list[dict] = []
    all_reqs: list[dict] = []
    failed = 0
    for data in responses:
        if not data:
            failed += 1
            continue
        prods, reqs = _parse_grounded_response(data, dp)
        all_products.extend(prods)
        all_reqs.extend(reqs)

    if failed:
        logging.warning(
            "grounded locator: %d/%d chunks returned nothing (throttle/timeout);"
            " results may be partial", failed, total_chunks,
        )
    if stats_out is not None:
        stats_meta.update({
            "chunks_total": total_chunks,
            "chunks_failed": failed,
            "chunk_chars": chunk_chars,
        })
        stats_out.update(stats_meta)

    merged_products, merged_reqs = _merge_grounded(all_products, all_reqs)
    if not merged_products and not merged_reqs:
        return None
    return {"products": merged_products, "requirements": merged_reqs}


# --------------------------------------------------------------------------- #
# Step 1 - scenario refinement
# --------------------------------------------------------------------------- #
def refine_scenarios(client, dp, evidence: list, detected: list[dict],
                     extra_instructions: str = "") -> Optional[list[dict]]:
    """Confirm / drop / add application scenarios.

    ``extra_instructions`` (optional) injects operator-defined precision rules
    into the system prompt (e.g. disambiguation guidance for noisy keywords).

    Returns a list of {scenario_id, in_scope, confidence, rationale} or None.
    """
    if not client.available:
        return None

    catalog = [
        {"scenario_id": s.scenario_id, "name": s.application_scenario,
         "asset_type": s.asset_type, "category": s.category}
        for s in dp.scenarios.values()
    ]

    # Group up to 3 evidence snippets per candidate scenario for grounding.
    # Keep a wide snippet so scope-qualifying language around the keyword
    # (e.g. "…is not part of the scope of this description") stays visible.
    snippets: dict[str, list[str]] = {}
    for ev in evidence:
        snippets.setdefault(ev.scenario_id, [])
        if len(snippets[ev.scenario_id]) < 3:
            snippets[ev.scenario_id].append(ev.evidence_text[:300])

    candidates = [
        {"scenario_id": d["scenario_id"], "name": d["scenario"],
         "rules_confidence": d["confidence"],
         "evidence": snippets.get(d["scenario_id"], [])}
        for d in detected
    ]

    system = (
        "You are a senior Qualitrol application engineer. You map customer power-"
        "grid monitoring documents (specs, emails, SLD/GIS drawings) to a CONTROLLED "
        "list of application scenarios. Be precise: only mark a scenario in scope if "
        "the evidence genuinely supports Qualitrol supplying that monitoring in THIS "
        "project. Apply these precision rules and set in_scope=false (with a short "
        "rationale) when they fire:\n"
        "1. SCOPE-EXCLUSION LANGUAGE: if the evidence says the item is 'not part of "
        "the scope', 'out of scope', 'optional', 'future', 'provision', a 'capability "
        "to expand', or supplied by another party, it is NOT in scope now.\n"
        "2. PLANT vs MONITORING: components of the switchgear/plant being monitored "
        "are not themselves monitoring scope. Circuit breakers, disconnectors, "
        "earthing switches, CTs/VTs, bushings described as GIS/switchgear parts do "
        "NOT imply breaker condition monitoring, transformer monitoring, etc. Mark "
        "breaker/transformer/etc. monitoring in scope only when the customer asks to "
        "MONITOR that asset (e.g. trip/close-coil current, operating-time, DGA).\n"
        "3. PROTOCOL vs PRODUCT: IEC 61850 / Modbus / DNP3 / SCADA mentioned as a "
        "data-output or integration requirement OF a monitoring system is a bundled "
        "output, NOT a standalone SCADA/gateway/software product line. Mark "
        "communication-integration in scope only when a separate gateway / SCADA "
        "integration / asset-platform deliverable is explicitly required.\n"
        "Respond with STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        "Controlled scenario catalog:\n"
        + json.dumps(catalog, ensure_ascii=False)
        + "\n\nRules-based candidate scenarios (with evidence snippets):\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\n\nTask: Decide which scenarios are truly in scope. You may add a catalog "
        "scenario not in the candidates if the evidence clearly implies it. "
        'Return JSON: {"scenarios":[{"scenario_id":"...","in_scope":true,'
        '"confidence":0.0-1.0,"rationale":"one sentence"}]}'
    )

    data = client.complete_json(system, user)
    if not isinstance(data, dict) or "scenarios" not in data:
        return None
    out: list[dict] = []
    valid_ids = set(dp.scenarios.keys())
    for item in data.get("scenarios", []):
        sid = str(item.get("scenario_id", "")).strip()
        if sid not in valid_ids:
            continue
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out.append({
            "scenario_id": sid,
            "in_scope": bool(item.get("in_scope", True)),
            "confidence": max(0.0, min(1.0, conf)),
            "rationale": str(item.get("rationale", "")).strip(),
        })
    return out or None


# --------------------------------------------------------------------------- #
# Step 1 - interpret the user's free-text project context into directives
# --------------------------------------------------------------------------- #
# Categories the BOQ generator (Step 2) knows how to exclude wholesale.
CONTEXT_EXCLUDE_CATEGORIES = (
    "service", "training", "commissioning", "spares", "fat",
    "software", "network", "timing", "panel",
)
_VALID_DIRECTIVE_TYPES = {"exclude", "include", "quantity_hint", "note"}


def interpret_context(client, dp, context_notes: str,
                      extra_instructions: str = "") -> Optional[list[dict]]:
    """Turn the operator's free-text context into STRUCTURED, validated directives.

    The Step 1 prompt box is free text and may carry very different intents:
    scope exclusions ("do not include training/service"), inclusions ("also add
    breaker monitoring"), quantity hints ("6 feeders", "273 gas zones"), scope
    clarifications, or plain background. This converts it — once — into a small
    list of directives that BOTH steps can act on deterministically:

      {"type":"exclude","category"|"scenario_id"|"family_id":..., "rationale":..}
      {"type":"include","scenario_id"|"family_id":..., "rationale":..}
      {"type":"quantity_hint","asset_type"|"count_field":..., "value":N, "rationale":..}
      {"type":"note","text":...}                       # non-actionable background

    Everything is validated against the controlled catalog (unknown ids dropped)
    so free text can never inject un-grounded scenarios/products. Returns the
    directive list, or None when the LLM is unavailable / nothing actionable.
    """
    from . import constants

    if not client.available or not (context_notes or "").strip():
        return None

    scen = [{"scenario_id": s.scenario_id, "name": s.application_scenario}
            for s in dp.scenarios.values()]
    fams = [{"family_id": f.family_id, "name": f.family_name}
            for f in dp.families.values()]
    count_fields = sorted(constants.COUNT_FIELD_TO_ASSET_TYPE.keys())

    system = (
        "You convert a sales/application engineer's free-text project note into a "
        "SMALL list of structured directives for a power-grid monitoring BOQ engine. "
        "Only use IDs / categories from the provided catalogs; never invent them. "
        "Classify each intent:\n"
        "- exclude: the user does not want something in the current draft (by "
        "category, scenario_id or family_id).\n"
        "- include: the user explicitly wants something added (scenario_id or family_id).\n"
        "- quantity_hint: the user states a countable quantity (map to a count_field "
        "or a drawing asset_type, with a numeric value).\n"
        "- note: background/context that is not directly actionable.\n"
        "If the text is only background, return a single note. Respond STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        "Scenario catalog:\n" + json.dumps(scen, ensure_ascii=False)
        + "\n\nFamily catalog:\n" + json.dumps(fams, ensure_ascii=False)
        + "\n\nExclude categories:\n" + json.dumps(list(CONTEXT_EXCLUDE_CATEGORIES))
        + "\n\nKnown count_fields:\n" + json.dumps(count_fields)
        + "\n\nUser project note:\n" + context_notes.strip()
        + '\n\nReturn JSON: {"directives":[{"type":"exclude|include|quantity_hint|note",'
        '"category":"","scenario_id":"","family_id":"","asset_type":"","count_field":"",'
        '"value":0,"text":"","rationale":"short"}]}'
    )
    try:
        data = client.complete_json(system, user)
    except Exception:  # noqa: BLE001 - fail safe
        return None
    if not isinstance(data, dict) or "directives" not in data:
        return None

    valid_scen = set(dp.scenarios.keys())
    valid_fam = set(dp.families.keys())
    valid_cat = set(CONTEXT_EXCLUDE_CATEGORIES)
    valid_cf = set(count_fields)
    out: list[dict] = []
    for item in data.get("directives", []):
        if not isinstance(item, dict):
            continue
        dtype = str(item.get("type", "")).strip().lower()
        if dtype not in _VALID_DIRECTIVE_TYPES:
            continue
        cat = str(item.get("category", "")).strip().lower()
        sid = str(item.get("scenario_id", "")).strip()
        fid = str(item.get("family_id", "")).strip()
        atype = str(item.get("asset_type", "")).strip()
        cfield = str(item.get("count_field", "")).strip()
        text = str(item.get("text", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        sid = sid if sid in valid_scen else ""
        fid = fid if fid in valid_fam else ""
        cat = cat if cat in valid_cat else ""
        cfield = cfield if cfield in valid_cf else ""

        if dtype == "exclude" and (cat or sid or fid):
            out.append({"type": "exclude", "category": cat, "scenario_id": sid,
                        "family_id": fid, "rationale": rationale})
        elif dtype == "include" and (sid or fid):
            out.append({"type": "include", "scenario_id": sid, "family_id": fid,
                        "rationale": rationale})
        elif dtype == "quantity_hint" and (cfield or atype):
            try:
                val = float(item.get("value", 0) or 0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                out.append({"type": "quantity_hint", "asset_type": atype,
                            "count_field": cfield, "value": val,
                            "rationale": rationale})
        elif dtype == "note" and text:
            out.append({"type": "note", "text": text})
    return out or None


# --------------------------------------------------------------------------- #
# Step 1 - requirement value extraction
# --------------------------------------------------------------------------- #
def extract_requirements(client, dp, scenarios: list[dict],
                         docs: list[ParsedDocument],
                         extra_instructions: str = "") -> Optional[list[dict]]:
    """Extract normalized metric values for the in-scope scenarios.

    ``extra_instructions`` (optional) injects operator-defined extraction rules
    into the system prompt (e.g. how to read counts, preferred units).

    Returns a list of {scenario_id, metric_id, value, unit, requirement_type,
    confidence, evidence} or None.
    """
    if not client.available or not scenarios:
        return None

    # Build the allowed (scenario, metric) space from the controlled metric dict.
    scenario_metrics = []
    allowed: set[tuple[str, str]] = set()
    for det in scenarios:
        sid = det["scenario_id"]
        scenario = dp.scenarios.get(sid)
        if not scenario:
            continue
        metric_ids = _scenario_metric_ids(scenario, dp)
        metrics = []
        for mid in metric_ids:
            m = dp.metrics.get(mid)
            if m:
                metrics.append({"metric_id": m.metric_id, "name": m.standard_name,
                                "unit": m.unit})
                allowed.add((sid, mid))
        scenario_metrics.append({"scenario_id": sid, "name": scenario.application_scenario,
                                 "metrics": metrics})

    if not allowed:
        return None

    system = (
        "You extract structured requirements from customer power-grid monitoring "
        "documents. Map values ONLY to the provided metric IDs. Normalize units to "
        "the metric's standard unit. If a value is not stated, omit that metric. "
        "Respond with STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        "In-scope scenarios and their allowed metrics:\n"
        + json.dumps(scenario_metrics, ensure_ascii=False)
        + "\n\nDocument text:\n"
        + build_context(docs)
        + "\n\nTask: Extract stated parameter values. "
        'Return JSON: {"requirements":[{"scenario_id":"...","metric_id":"...",'
        '"value":"...","unit":"...","requirement_type":"Must-have|Preferred|'
        'Reference|Quantity Basis","confidence":0.0-1.0,"evidence":"short quote"}]}'
    )

    data = client.complete_json(system, user)
    if not isinstance(data, dict) or "requirements" not in data:
        return None
    out: list[dict] = []
    for item in data.get("requirements", []):
        sid = str(item.get("scenario_id", "")).strip()
        mid = str(item.get("metric_id", "")).strip()
        if (sid, mid) not in allowed:
            continue
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        rtype = str(item.get("requirement_type", "")).strip()
        if rtype not in _VALID_REQ_TYPES:
            rtype = "Reference"
        try:
            conf = float(item.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        out.append({
            "scenario_id": sid, "metric_id": mid, "value": value,
            "unit": str(item.get("unit", "")).strip(),
            "requirement_type": rtype,
            "confidence": max(0.0, min(1.0, conf)),
            "evidence": str(item.get("evidence", "")).strip(),
        })
    return out or None


def _scenario_metric_ids(scenario, dp) -> list[str]:
    """Same tight relevance logic Step 1 uses, kept here to size the prompt."""
    from . import constants

    ids: list[str] = []
    for syn in dp.synonyms:
        if syn.scenario_id == scenario.scenario_id and syn.metric_id:
            ids.append(syn.metric_id)
    rule = dp.quantity_rule_for_scenario(scenario.scenario_id)
    if rule and rule.count_field:
        mapped = constants.COUNT_FIELD_TO_METRIC.get(rule.count_field)
        if mapped:
            ids.append(mapped)
    interest = " ".join([
        scenario.typical_metrics, " ".join(scenario.requirement_output_fields),
        " ".join(scenario.keywords),
    ]).lower()
    for metric in dp.metrics.values():
        name = metric.standard_name.lower()
        if name and name in interest:
            ids.append(metric.metric_id)
    seen, ordered = set(), []
    for mid in ids:
        if mid and mid not in seen:
            seen.add(mid)
            ordered.append(mid)
    return ordered


# --------------------------------------------------------------------------- #
# Step 2 - match explanation
# --------------------------------------------------------------------------- #
def explain_matches(client, project_summary: dict,
                    matches: list[dict], extra_instructions: str = "") -> Optional[dict]:
    """Return {family_id: {recommendation, gap_or_risk}} or None."""
    if not client.available or not matches:
        return None

    compact = [
        {"family_id": m["family_id"], "family_name": m["family_name"],
         "scenario_id": m.get("scenario_id", ""),
         "capability_known": m.get("capability_known", False),
         "rules_score": m["match_score"]}
        for m in matches
    ]
    system = (
        "You are a senior Qualitrol product engineer reviewing a draft BOQ. For each "
        "candidate product family, give a concise recommendation and the key gap/risk "
        "to resolve before quoting. Note when product model/capability data is TBD and "
        "must be validated. Respond with STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        "Project summary:\n" + json.dumps(project_summary, ensure_ascii=False)
        + "\n\nCandidate families:\n" + json.dumps(compact, ensure_ascii=False)
        + '\n\nReturn JSON: {"matches":[{"family_id":"...","recommendation":"...",'
        '"gap_or_risk":"..."}]}'
    )
    data = client.complete_json(system, user)
    if not isinstance(data, dict) or "matches" not in data:
        return None
    out: dict[str, dict] = {}
    for item in data.get("matches", []):
        fid = str(item.get("family_id", "")).strip()
        if not fid:
            continue
        out[fid] = {
            "recommendation": str(item.get("recommendation", "")).strip(),
            "gap_or_risk": str(item.get("gap_or_risk", "")).strip(),
        }
    return out or None


# --------------------------------------------------------------------------- #
# Step 2 - extra clarification questions
# --------------------------------------------------------------------------- #
def suggest_missing_info(client, project_summary: dict,
                         existing_items: list[str],
                         extra_instructions: str = "") -> Optional[list[dict]]:
    """Suggest additional clarification questions. Returns list of dicts or None."""
    if not client.available:
        return None
    system = (
        "You are a Qualitrol sales/application engineer. Suggest only clarification "
        "questions that are genuinely needed to finalize the BOQ and are NOT already "
        "covered. Be specific and few (max 4). Respond with STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        "Project summary:\n" + json.dumps(project_summary, ensure_ascii=False)
        + "\n\nQuestions already raised:\n" + json.dumps(existing_items, ensure_ascii=False)
        + '\n\nReturn JSON: {"questions":[{"scenario_id":"...","missing_item":"...",'
        '"question":"...","why_it_matters":"...","priority":"High|Medium|Low",'
        '"owner":"..."}]}'
    )
    data = client.complete_json(system, user)
    if not isinstance(data, dict) or "questions" not in data:
        return None
    out: list[dict] = []
    for item in data.get("questions", [])[:4]:
        q = str(item.get("question", "")).strip()
        if not q:
            continue
        prio = str(item.get("priority", "Medium")).strip().title()
        if prio not in {"High", "Medium", "Low"}:
            prio = "Medium"
        out.append({
            "scenario_id": str(item.get("scenario_id", "")).strip(),
            "missing_item": str(item.get("missing_item", "")).strip() or q[:60],
            "question": q,
            "why_it_matters": str(item.get("why_it_matters", "")).strip(),
            "priority": prio,
            "owner": str(item.get("owner", "")).strip() or "Sales / Application Engineer",
        })
    return out or None


# --------------------------------------------------------------------------- #
# Step 2 - regenerate BOQ lines from reviewer feedback
# --------------------------------------------------------------------------- #
_VALID_FEEDBACK_ACTIONS = {"keep", "remove", "replace", "adjust"}


def regenerate_boq_lines(
    client, project_summary: dict, flagged_lines: list[dict]
) -> Optional[list[dict]]:
    """Re-decide BOQ lines that received negative reviewer feedback.

    ``flagged_lines`` items:
      {feedbackKey, product_model, product_description, scenario_id,
       scenario_name, quantity, unit, quantity_basis, feedback_comment,
       candidates: [{product_id, model, description}]}

    Returns a list of decisions:
      {feedbackKey, action(keep|remove|replace|adjust), product_id,
       product_model, quantity, unit, rationale}
    or None when the LLM is unavailable / the response is unusable.

    The model may only pick a ``product_id`` from that line's ``candidates`` —
    it must not invent product models outside the catalog.
    """
    if not client.available or not flagged_lines:
        return None

    system = (
        "You are a senior Qualitrol application engineer REVISING a draft BOQ "
        "using a reviewer's written feedback for specific lines. For EACH flagged "
        "line choose exactly one action:\n"
        "  - 'remove': the item is out of scope / not supplied by Qualitrol / not "
        "needed (e.g. supplied with the GIS or transformer package).\n"
        "  - 'replace': the wrong product family/model was chosen; pick a better "
        "one ONLY from that line's 'candidates' list (use its product_id).\n"
        "  - 'adjust': the product is right but the quantity is wrong; set the "
        "corrected integer quantity.\n"
        "  - 'keep': feedback does not warrant a change.\n"
        "Rules: NEVER invent a product_id/model that is not in the line's "
        "candidates. Base your decision strictly on the reviewer feedback text. "
        "Give a short rationale citing the feedback. Respond with STRICT JSON only."
    )
    user = (
        "Project summary:\n" + json.dumps(project_summary, ensure_ascii=False)
        + "\n\nFlagged BOQ lines (with reviewer feedback and allowed candidates):\n"
        + json.dumps(flagged_lines, ensure_ascii=False)
        + '\n\nReturn JSON: {"lines":[{"feedbackKey":"...",'
        '"action":"keep|remove|replace|adjust","product_id":"...",'
        '"product_model":"...","quantity":<integer or null>,"unit":"...",'
        '"rationale":"..."}]}'
    )
    data = client.complete_json(system, user)
    if not isinstance(data, dict) or "lines" not in data:
        return None

    out: list[dict] = []
    for item in data.get("lines", []):
        key = str(item.get("feedbackKey", "")).strip()
        action = str(item.get("action", "")).strip().lower()
        if not key or action not in _VALID_FEEDBACK_ACTIONS:
            continue
        qty = item.get("quantity")
        try:
            qty = float(qty) if qty is not None and str(qty) != "" else None
        except (TypeError, ValueError):
            qty = None
        out.append({
            "feedbackKey": key,
            "action": action,
            "product_id": str(item.get("product_id", "")).strip(),
            "product_model": str(item.get("product_model", "")).strip(),
            "quantity": qty,
            "unit": str(item.get("unit", "")).strip(),
            "rationale": str(item.get("rationale", "")).strip(),
        })
    return out or None


# --------------------------------------------------------------------------- #
# Step 1 - SLD asset extraction via Claude Vision (optional VLM path)
# --------------------------------------------------------------------------- #

_VALID_ASSET_TYPES = {
    "Circuit Breaker", "Transformer", "GIS Bay", "Bus", "Feeder", "PCC",
    "Generator", "Motor", "Switchgear Panel", "PD Sensor", "Sensor",
    "Bushing", "Channel", "Measurement Point",
    # Extended coverage for wider Qualitrol monitoring scenarios. Keep these
    # strings in sync with COUNT_FIELD_TO_ASSET_TYPE in constants.py so that
    # quantity rules can size BOQ lines from them.
    "Reactor", "Transmission Line", "Cable", "Surge Arrester",
    "Instrument Transformer", "Tap Changer", "Capacitor Bank", "Cabinet",
    # GIS gas-zone vocabulary added in the 2026-07 DMS GIS SLD diagram review.
    # Gas compartments / density sensors size the SF6 GDHT-20 quantity; the
    # disconnector / earthing switches inform the UHF protector recommendation.
    "Gas Compartment", "Gas Density Sensor", "Disconnector Switch",
    "Earthing Switch",
}
_VALID_STATUS = {"New", "Existing", "Future", "Provision", "Unclear"}


def extract_document_page_text_vlm(
    client,
    image_b64: str,
    document_id: str,
    page_number: int,
) -> Optional[str]:
    """Faithfully transcribe a specification page whose content is image-only.

    This differs from ``extract_sld_text_vlm``: it preserves all scope,
    requirement, quantity, parameter and exclusion language instead of selecting
    drawing labels. The text is injected back into the parsed page so the normal
    grounded locator and rules engine can process it. Returns ``None`` on any
    failure.
    """
    if not client.available:
        return None

    system = (
        "You are performing OCR on one page of a customer technical document for "
        "a quotation workflow. Transcribe ALL visible substantive text faithfully, "
        "especially scope of supply, technical requirements, quantities, units, "
        "standards, product or asset names, exclusions, tables and notes. Preserve "
        "table rows in readable plain text and keep section/row order. Ignore only "
        "repeated company letterheads, page numbers and decorative elements. Do not "
        "summarize, interpret, translate or invent missing text. If text is unclear, "
        "mark the fragment [unclear]. Respond with STRICT JSON only."
    )
    user = (
        f"Document: {document_id}; page: {page_number}. "
        'Return JSON exactly as {"text":"faithful transcription"}'
    )
    try:
        data = client.complete_json_with_image(
            system, user, image_b64, max_tokens=8192
        )
    except Exception:  # noqa: BLE001 - optional augmentation must fail safe
        return None
    if not isinstance(data, dict):
        return None
    text = str(data.get("text", "")).strip()
    return text or None


def extract_sld_text_vlm(
    client,
    image_b64: str,
    drawing_id: str,
) -> Optional[str]:
    """Read the printed text/labels off a drawing image (VLM OCR).

    Used when a project supplies SLD/BLD drawings but little or no prose
    specification (the drawing's text layer is sparse). The returned text —
    panel titles, device/function labels, legends, scope notes — is injected
    back into the document so the normal text-driven scenario detection can
    match scenario keywords (e.g. "DFR", "PMU", "PQM", "FMS", "Fault Recorder",
    "Power Quality"). Returns ``None`` on any failure (fails safe).
    """
    if not client.available:
        return None

    system = (
        "You are reading a power-grid Single Line Diagram / Block Diagram for a "
        "Qualitrol monitoring quotation. Extract ONLY the text relevant to MONITORING "
        "SCOPE so a quoting engine can detect application scenarios. Include: "
        "monitoring/panel/function labels (DFR, DDR, PMU, PQM, FMS, WAMS, Fault "
        "Recorder, Fault Locator, Power Quality, Disturbance Recorder, SCADA, IEC 61850), "
        "asset types being monitored (transformer, GIS, circuit breaker, busbar, feeder, "
        "reactor, cable, tap changer / OLTC, surge arrester, capacitor bank, instrument "
        "transformer / CT / VT), voltage levels, feeder/bay names, and scope notes (FUTURE / PROVISION "
        "/ EXISTING). IGNORE cable sizes, ratings, title-block / client / consultant / "
        "drawing-number text. Do NOT invent text. Respond with STRICT JSON only."
    )
    user = (
        "List the monitoring-relevant labels you can read (max ~40 short items) plus a "
        "one-sentence summary of the monitoring functions shown. "
        'Return JSON: {"labels": ["...", "..."], "notes": "..."}'
    )
    try:
        # Generous token budget: a truncated response yields invalid JSON -> None.
        data = client.complete_json_with_image(system, user, image_b64, max_tokens=4000)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    labels = data.get("labels") or []
    notes = str(data.get("notes", "")).strip()
    parts: list[str] = []
    if isinstance(labels, list):
        parts.extend(str(x).strip() for x in labels if str(x).strip())
    if notes:
        parts.append(notes)
    text = "\n".join(parts).strip()
    return text or None


def extract_sld_assets_vlm(
    client,
    image_b64: str,
    drawing_id: str,
    project_id: str,
) -> Optional[list[DrawingAsset]]:
    """Analyse a base64-encoded SLD page image with a vision model.

    Returns a list of ``DrawingAsset`` objects or ``None`` when the LLM is
    unavailable or the response cannot be parsed. Fails safe: any error
    returns ``None`` so the text-layer extraction is used as fallback.

    The asset types produced are aligned to the ``COUNT_FIELD_TO_ASSET_TYPE``
    values in ``constants.py`` so that Step 2 quantity rules can consume them.
    """
    if not client.available:
        return None

    system = (
        "You are a senior power-systems engineer analysing a Single Line Diagram (SLD) "
        "for a Qualitrol monitoring quotation. Your task is to produce a structured asset "
        "list — NOT a BOQ. Extract the individual electrical assets visible in the drawing "
        "so that quantity rules can calculate BOQ quantities from the asset list.\n\n"
        "ASSET TYPES to identify (use these exact strings):\n"
        "  Circuit Breaker, Transformer, GIS Bay, Bus, Feeder, PCC, Generator, Motor,\n"
        "  Switchgear Panel, PD Sensor, Sensor, Bushing, Channel, Measurement Point,\n"
        "  Reactor, Transmission Line, Cable, Surge Arrester, Instrument Transformer,\n"
        "  Tap Changer, Capacitor Bank, Cabinet,\n"
        "  Gas Compartment, Gas Density Sensor, Disconnector Switch, Earthing Switch\n\n"
        "STATUS values (use these exact strings):\n"
        "  New        – in current project scope\n"
        "  Existing   – already installed, in scope for retrofit/monitoring\n"
        "  Future     – shown on drawing but not in current contract scope\n"
        "  Provision  – space/connection reserved only, not supplied now\n"
        "  Unclear    – cannot determine from drawing\n\n"
        "SCOPE HINTS:\n"
        "  Greyed-out, dashed, or hatched areas are typically Future or Provision.\n"
        "  Solid-line equipment with no qualifier is typically New or Existing.\n"
        "  Look for text labels: FUTURE, FOR FUTURE, PROVISION, EXISTING, NEW.\n\n"
        "For each asset provide: asset_tag (text label on drawing, e.g. '40CB7'), "
        "asset_type (from list above), voltage_level (e.g. '400 kV'), "
        "drawing_area (zone label, e.g. '400kV GIS Indoor'), "
        "status (from list above), quantity (integer, default 1), "
        "evidence (short description of what you see on the drawing).\n\n"
        "Keep each 'evidence' value under 10 words so the full JSON fits in the "
        "response. Respond with STRICT JSON only — no markdown, no commentary."
    )
    user = (
        "Please analyse this Single Line Diagram and extract all identifiable electrical "
        "assets. Pay close attention to:\n"
        "1. Circuit breaker tags (e.g. 40CB7, 43CB4)\n"
        "2. Transformer labels (e.g. SST-1, SST-2, TR-1)\n"
        "3. Bus labels (e.g. BUS-1, BUS-2)\n"
        "4. Feeder / bay labels (e.g. H01, H02, F01)\n"
        "5. GIS sections and their bay count\n"
        "6. Any areas shown as Future, Provision, or greyed out\n\n"
        'Return JSON: {"assets":[{"asset_tag":"...","asset_type":"...","voltage_level":"...",'
        '"drawing_area":"...","status":"...","quantity":1,"evidence":"..."}]}'
    )

    # Real substation SLDs contain dozens of assets; 4096 tokens truncates the
    # JSON mid-array (unrecoverable), so give the vision call ample room.
    data = client.complete_json_with_image(system, user, image_b64, max_tokens=8192)
    if not isinstance(data, dict) or "assets" not in data:
        return None

    out: list[DrawingAsset] = []
    for item in data.get("assets", []):
        atype = str(item.get("asset_type", "")).strip()
        if atype not in _VALID_ASSET_TYPES:
            continue
        status = str(item.get("status", "Unclear")).strip().title()
        if status not in _VALID_STATUS:
            status = "Unclear"
        try:
            qty = float(item.get("quantity", 1) or 1)
        except (TypeError, ValueError):
            qty = 1.0
        tag = str(item.get("asset_tag", "")).strip()
        vl = str(item.get("voltage_level", "")).strip()
        area = str(item.get("drawing_area", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        out.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=drawing_id,
                asset_tag=tag,
                asset_type=atype,
                voltage_level=vl,
                quantity=qty,
                source_location=f"{drawing_id} (VLM vision extraction)",
                confidence=0.7,
                drawing_area=area,
                status=status,
                notes=(
                    f"Identified by vision model from SLD image. Evidence: {evidence}. "
                    "Confirm scope and quantity with engineering before use in BOQ."
                ),
            )
        )
    return out or None
