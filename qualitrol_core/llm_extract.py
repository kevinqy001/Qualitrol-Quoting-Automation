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
        "power-transformer monitoring. Respond with STRICT JSON only."
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
                    matches: list[dict]) -> Optional[dict]:
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
                         existing_items: list[str]) -> Optional[list[dict]]:
    """Suggest additional clarification questions. Returns list of dicts or None."""
    if not client.available:
        return None
    system = (
        "You are a Qualitrol sales/application engineer. Suggest only clarification "
        "questions that are genuinely needed to finalize the BOQ and are NOT already "
        "covered. Be specific and few (max 4). Respond with STRICT JSON only."
    )
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
    # Extended coverage for wider Qualitrol monitoring scenarios. Keep these
    # strings in sync with COUNT_FIELD_TO_ASSET_TYPE in constants.py so that
    # quantity rules can size BOQ lines from them.
    "Reactor", "Transmission Line", "Cable", "Surge Arrester",
    "Instrument Transformer", "Tap Changer", "Capacitor Bank", "Cabinet",
}
_VALID_STATUS = {"New", "Existing", "Future", "Provision", "Unclear"}


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
        "  Switchgear Panel, PD Sensor, Sensor, Bushing, Channel, Measurement Point,\n"
        "  Reactor, Transmission Line, Cable, Surge Arrester, Instrument Transformer,\n"
        "  Tap Changer, Capacitor Bank, Cabinet\n\n"
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
