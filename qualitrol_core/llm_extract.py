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
from typing import Optional

from .document_parser import ParsedDocument
from .schemas import DrawingAsset

_VALID_REQ_TYPES = {"Must-have", "Preferred", "Reference", "Quantity Basis", "Unknown"}


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
# Step 1 - scenario refinement
# --------------------------------------------------------------------------- #
def refine_scenarios(client, dp, evidence: list, detected: list[dict],
                     docs: Optional[list[ParsedDocument]] = None,
                     extra_instructions: str = "") -> Optional[list[dict]]:
    """Confirm / drop / add application scenarios.

    ``docs`` (optional) gives the LLM the raw document context so it can detect
    scenarios directly from the text even when the deterministic keyword engine
    found nothing (e.g. a quantity-summary table that only says "380kV GIS").

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
    snippets: dict[str, list[str]] = {}
    for ev in evidence:
        snippets.setdefault(ev.scenario_id, [])
        if len(snippets[ev.scenario_id]) < 3:
            snippets[ev.scenario_id].append(ev.evidence_text[:160])

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
        "the evidence genuinely supports it. Watch for false positives, e.g. mentions "
        "of current/voltage transformers (CT/VT) in a switchgear drawing do NOT imply "
        "power-transformer monitoring. The keyword engine can MISS scenarios when a "
        "document uses only short/implicit terms (e.g. a sensor-quantity table that "
        "just says '380kV GIS' with sensor counts implies GIS partial-discharge "
        "monitoring); use the document text to recover those. Respond with STRICT "
        "JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    doc_context = build_context(docs) if docs else ""
    user = (
        "Controlled scenario catalog:\n"
        + json.dumps(catalog, ensure_ascii=False)
        + "\n\nRules-based candidate scenarios (with evidence snippets):\n"
        + json.dumps(candidates, ensure_ascii=False)
        + (("\n\nDocument text (authoritative source — use it to confirm, drop, or "
            "ADD scenarios the keyword engine missed):\n" + doc_context)
           if doc_context else "")
        + "\n\nTask: Decide which scenarios are truly in scope. Add a catalog "
        "scenario not in the candidates when the document text clearly implies it "
        "(cite the trigger in the rationale). Only include scenarios the evidence "
        "or document text genuinely supports. "
        'Return JSON: {"scenarios":[{"scenario_id":"...","in_scope":true,'
        '"confidence":0.0-1.0,"rationale":"one sentence"}]}'
    )

    data = client.complete_json(system, user)
    if not isinstance(data, dict) or "scenarios" not in data:
        return None
    # The model occasionally emits the SAME scenario_id twice (e.g. a real
    # judgment plus a junk "duplicate placeholder" row). Collapse duplicates,
    # keeping the strongest judgment: prefer in_scope=true, then higher
    # confidence — so a stray placeholder cannot drop a genuine scenario.
    best: dict[str, dict] = {}
    valid_ids = set(dp.scenarios.keys())
    for item in data.get("scenarios", []):
        sid = str(item.get("scenario_id", "")).strip()
        if sid not in valid_ids:
            continue
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        in_scope = bool(item.get("in_scope", True))
        candidate = {
            "scenario_id": sid,
            "in_scope": in_scope,
            "confidence": conf,
            "rationale": str(item.get("rationale", "")).strip(),
        }
        prev = best.get(sid)
        if prev is None or (in_scope, conf) > (prev["in_scope"], prev["confidence"]):
            best[sid] = candidate
    return list(best.values()) or None


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
                    matches: list[dict],
                    extra_instructions: str = "") -> Optional[dict]:
    """Return {family_id: {recommendation, gap_or_risk}} or None.

    ``extra_instructions`` (optional) injects operator-defined review rules into
    the system prompt (e.g. how aggressively to flag TBD capability, house style
    for recommendations).
    """
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
    """Suggest additional clarification questions. Returns list of dicts or None.

    ``extra_instructions`` (optional) injects operator-defined rules into the
    system prompt (e.g. preferred owners, question phrasing, topics to avoid).
    """
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
# Step 1 - SLD asset extraction via Claude Vision (optional VLM path)
# --------------------------------------------------------------------------- #

_VALID_ASSET_TYPES = {
    "Circuit Breaker", "Transformer", "GIS Bay", "Bus", "Feeder", "PCC",
    "Generator", "Motor", "Switchgear Panel", "PD Sensor", "Sensor",
    "Bushing", "Channel", "Measurement Point",
}
_VALID_STATUS = {"New", "Existing", "Future", "Provision", "Unclear"}


def extract_sld_assets_vlm(
    client,
    image_b64: str,
    drawing_id: str,
    project_id: str,
) -> Optional[list[DrawingAsset]]:
    """Analyse a base64-encoded SLD page image with Claude Vision.

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
        "  Switchgear Panel, PD Sensor, Sensor, Bushing, Channel, Measurement Point\n\n"
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
                    f"Identified by Claude Vision from SLD image. Evidence: {evidence}. "
                    "Confirm scope and quantity with engineering before use in BOQ."
                ),
            )
        )
    return out or None


# --------------------------------------------------------------------------- #
# Step 1 - two-stage multi-sheet SLD understanding (classify -> reconcile)
# --------------------------------------------------------------------------- #
_VALID_PAGE_TYPES = {"diagram", "legend", "spec_table", "mixed", "other"}


def analyze_sld_page(
    client,
    image_b64: str,
    drawing_id: str,
    page_label: str,
    extra_instructions: str = "",
) -> Optional[dict]:
    """Stage 1: classify ONE SLD page and extract the payload matching its type.

    Returns a dict ``{page, page_type, title, voltage_level, assets, spec_rows,
    legend, notes}`` or ``None``. Diagram pages populate ``assets``; data/schedule
    pages populate ``spec_rows`` (parameters, NOT physical assets); key/notes
    pages populate ``legend`` + ``notes``. The downstream ``reconcile_sld_assets``
    fuses these across pages so a legend/spec sheet (e.g. a CT/VT schedule on the
    last page) informs and constrains the diagram pages instead of inflating the
    physical asset count.
    """
    if not client.available:
        return None

    asset_types = ", ".join(sorted(_VALID_ASSET_TYPES))
    system = (
        "You are a senior power-systems engineer analysing ONE page of a multi-sheet "
        "Single Line Diagram (SLD) set for a Qualitrol monitoring quotation. FIRST "
        "classify the page, THEN extract only the payload that matches its type.\n\n"
        "page_type (exactly one of): 'diagram' (single-line/gas schematic with equipment "
        "symbols), 'legend' (symbol key / general notes), 'spec_table' (tabular technical "
        "data: CT/VT/SA ratings, SF6 pressures, parameter schedules), 'mixed', 'other'.\n\n"
        f"DIAGRAM/MIXED -> list physical assets in 'assets' using EXACT asset_type strings: "
        f"{asset_types}. status one of New, Existing, Future, Provision, Unclear "
        "(greyed/dashed/'FUTURE'/'PROVISION' => Future/Provision).\n"
        "SPEC_TABLE/MIXED -> put the parameter schedule in 'spec_rows'; each row "
        "{item, category, applies_to, rating, class_or_type, quantity, unit}. These are "
        "PARAMETERS, not physical assets. 'applies_to' = the bay/feeder/asset it references.\n"
        "LEGEND/MIXED -> put symbol meanings in 'legend' {symbol, meaning} and general "
        "'notes'.\n\n"
        "Keep every string short. Respond with STRICT JSON only — no markdown."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        f"This is sheet '{page_label}'. Classify it and extract its payload. Return JSON: "
        '{"page_type":"...","title":"...","voltage_level":"...",'
        '"assets":[{"asset_tag":"...","asset_type":"...","voltage_level":"...",'
        '"drawing_area":"...","status":"...","quantity":1,"evidence":"..."}],'
        '"spec_rows":[{"item":"...","category":"...","applies_to":"...","rating":"...",'
        '"class_or_type":"...","quantity":1,"unit":"..."}],'
        '"legend":[{"symbol":"...","meaning":"..."}],"notes":["..."]}'
    )
    data = client.complete_json_with_image(system, user, image_b64, max_tokens=8192)
    if not isinstance(data, dict):
        return None
    page_type = str(data.get("page_type", "")).strip().lower()
    if page_type not in _VALID_PAGE_TYPES:
        page_type = "diagram"  # default: treat as a diagram so assets are kept
    data["page_type"] = page_type
    data.setdefault("page", page_label)
    return data


def reconcile_sld_assets(
    client,
    drawing_id: str,
    project_id: str,
    pages: list[dict],
    extra_instructions: str = "",
) -> Optional[list[DrawingAsset]]:
    """Stage 2: fuse all per-page payloads into ONE authoritative asset list.

    Uses the legend to interpret symbols, treats spec tables as parameters /
    cross-checks (not extra physical assets), deduplicates assets seen on several
    sheets, separates in-scope from Future/Provision, and emits per
    (asset_type, status) COUNT rows with a justification. Returns ``DrawingAsset``
    objects or ``None`` (caller falls back to per-page assets).
    """
    if not client.available or not pages:
        return None

    diagram_assets: list[dict] = []
    spec_rows: list[dict] = []
    legend: list[dict] = []
    notes: list[str] = []
    for p in pages:
        page = p.get("page")
        for a in p.get("assets", []) or []:
            diagram_assets.append({
                "page": page,
                "tag": str(a.get("asset_tag", ""))[:40],
                "type": str(a.get("asset_type", "")),
                "v": str(a.get("voltage_level", "")),
                "area": str(a.get("drawing_area", ""))[:40],
                "status": str(a.get("status", "Unclear")),
            })
        for s in p.get("spec_rows", []) or []:
            spec_rows.append({
                "page": page,
                "item": str(s.get("item", ""))[:40],
                "cat": str(s.get("category", ""))[:30],
                "applies_to": str(s.get("applies_to", ""))[:40],
                "rating": str(s.get("rating", ""))[:40],
                "type": str(s.get("class_or_type", ""))[:30],
                "qty": s.get("quantity"),
                "unit": str(s.get("unit", ""))[:12],
            })
        for lg in p.get("legend", []) or []:
            legend.append({
                "symbol": str(lg.get("symbol", ""))[:24],
                "meaning": str(lg.get("meaning", ""))[:80],
            })
        for n in (p.get("notes", []) or []):
            notes.append(str(n)[:140])

    asset_types = ", ".join(sorted(_VALID_ASSET_TYPES))
    system = (
        "You are a senior Qualitrol application engineer reconciling a multi-sheet SLD "
        "into ONE authoritative drawing asset list for BOQ sizing. You are given, pooled "
        "across all pages: raw diagram assets, spec-table rows, the legend, and notes.\n\n"
        "Rules:\n"
        "1. Use the LEGEND to interpret ambiguous symbols/abbreviations on diagram pages.\n"
        "2. Diagram bay/equipment symbols are the source of PHYSICAL counts. Spec tables "
        "are PARAMETERS and a cross-check — a CT/VT schedule listing many cores/classes "
        "does NOT mean that many physical assets; do NOT turn schedule rows into assets.\n"
        "3. Deduplicate the same physical asset appearing on multiple sheets.\n"
        "4. Separate in-scope (New/Existing) from Future/Provision into distinct rows.\n"
        "5. Attach a representative rating and monitoring_zone per asset type when the "
        "spec/legend supports it; map connected_to (e.g. which bay a sensor serves).\n"
        "6. Output per (asset_type, status) COUNT rows — NOT per tag. Use EXACT asset_type "
        f"strings: {asset_types}. status one of New, Existing, Future, Provision. In "
        "'basis', justify the count and flag any diagram-vs-spec discrepancy. Respond with "
        "STRICT JSON only."
    )
    system = _with_extra_rules(system, extra_instructions)
    user = (
        "Diagram assets (raw, per page):\n" + json.dumps(diagram_assets, ensure_ascii=False)
        + "\n\nSpec-table rows (parameters / schedules):\n" + json.dumps(spec_rows, ensure_ascii=False)
        + "\n\nLegend:\n" + json.dumps(legend, ensure_ascii=False)
        + "\n\nNotes:\n" + json.dumps(notes, ensure_ascii=False)
        + '\n\nReturn JSON: {"assets":[{"asset_type":"...","voltage_level":"...",'
        '"status":"New","quantity":0,"rating":"...","monitoring_zone":"...",'
        '"connected_to":"...","basis":"..."}]}'
    )
    data = client.complete_json(system, user, max_tokens=4096)
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
            qty = float(item.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        zone = str(item.get("monitoring_zone", "")).strip()
        out.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=drawing_id,
                asset_tag="",
                asset_type=atype,
                voltage_level=str(item.get("voltage_level", "")).strip(),
                rating=str(item.get("rating", "")).strip(),
                quantity=qty,
                connected_to=str(item.get("connected_to", "")).strip(),
                monitoring_zone=zone,
                source_location=f"{drawing_id} (VLM reconciled across {len(pages)} sheets)",
                confidence=0.7,
                drawing_area=zone,
                status=status,
                notes=(
                    "Reconciled from multi-sheet SLD (legend + spec tables applied). "
                    + str(item.get("basis", "")).strip()
                ),
            )
        )
    return out or None
