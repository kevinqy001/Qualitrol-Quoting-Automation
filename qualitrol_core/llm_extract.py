"""LLM augmentation layer (Claude Opus 4.8 via Azure AI Foundry).

These helpers sit on top of the deterministic rules engine. The rules layer
provides recall (and grounding evidence); the LLM adds precision and
explanations. Every function:
  * is a no-op when the LLM is unavailable (returns None),
  * is grounded in the controlled vocabulary + rules-extracted evidence,
  * fails safe (any error -> None) so the pipeline always completes.

Used by:
  Step 1 -> refine_scenarios(), extract_requirements()
  Step 2 -> explain_matches(), suggest_missing_info()
"""

from __future__ import annotations

import json
from typing import Optional

from .document_parser import ParsedDocument

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
