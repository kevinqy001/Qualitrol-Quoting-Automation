"""Step 1 - Extract Valuable Information.

Implements the left branch of the process map:

    Input Parsing Layer
      -> Extract Evidence Text        (Scenario Master + Synonym Mapping)
      -> Detect Related Application Scenarios
      -> Map to Scenario ID
      -> Extract Asset Type / Asset Tag   (+ Drawing -> Asset List)
      -> Extract Key Metrics & Parameter Values  (Metric Dictionary)
      -> Generate Structured Requirements Table

Inputs : a customer submission folder (Project Specification, Raw Email,
         Circuit Drawing / SLD ...).
Outputs: Extracted Evidence (sheet 12), Drawing Asset List (sheet 14) and
         Structured Requirements (sheet 13), written as JSON under outputs/.

Rules-first and offline by default; the optional LLM hook only adds semantic
extraction / explanations on top (see qualitrol_core.llm).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the shared core importable when run as a standalone script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import config, constants, io_utils, llm, llm_extract, matching  # noqa: E402
from qualitrol_core.data_package import DataPackage, load_data_package  # noqa: E402
from qualitrol_core.document_parser import (  # noqa: E402
    ParsedDocument,
    parse_project_folder,
)
from qualitrol_core.drawing_assets import extract_drawing_assets  # noqa: E402
from qualitrol_core.schemas import (  # noqa: E402
    DrawingAsset,
    Evidence,
    Requirement,
    Scenario,
)

MAX_EVIDENCE_PER_SCENARIO_PER_DOC = 4

# Operator-editable, plain-text rules injected into the Step 1 LLM prompts.
# Edit this file to add domain rules/constraints without touching code; an env
# var override lets you point at a different rule set per run. Empty/missing =
# no extra instructions (pure default behaviour).
EXTRACTION_RULES_FILE = config.STEP1_DIR / "extraction_rules.md"


def load_extraction_rules() -> str:
    """Return operator-provided extra LLM instructions for Step 1 (or "")."""
    override = os.getenv("QUALITROL_STEP1_RULES_FILE")
    path = Path(override) if override else EXTRACTION_RULES_FILE
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------- #
# 1. Extract evidence text  (Scenario Master + Synonym Mapping)
# --------------------------------------------------------------------------- #
def _strong_terms_by_scenario(dp: DataPackage) -> dict[str, set[str]]:
    """Per-scenario set of *specific* terms used to corroborate weak, ambiguous
    keyword hits (its non-ambiguous keywords plus its mapped synonym terms)."""
    strong: dict[str, set[str]] = {}
    for scenario in dp.scenarios.values():
        terms = {
            kw.strip().lower()
            for kw in scenario.keywords
            if kw.strip() and not matching.is_ambiguous_keyword(kw)
        }
        strong[scenario.scenario_id] = terms
    for syn in dp.synonyms:
        if syn.scenario_id and syn.raw_term:
            strong.setdefault(syn.scenario_id, set()).add(syn.raw_term.strip().lower())
    return strong


def extract_evidence(
    docs: list[ParsedDocument], dp: DataPackage, project_id: str
) -> list[Evidence]:
    raw_hits: list[Evidence] = []
    strong_terms = _strong_terms_by_scenario(dp)

    for doc in docs:
        for seg in doc.segments:
            text = seg.text
            text_lower = text.lower()

            # (a) High-value controlled synonyms -> scenario.
            for syn in dp.synonyms:
                if not syn.scenario_id:
                    continue
                idx = matching.find_term(text_lower, syn.raw_term)
                if idx < 0:
                    continue
                scenario = dp.scenarios.get(syn.scenario_id)
                raw_hits.append(
                    Evidence(
                        evidence_id="",
                        project_id=project_id,
                        source_document=doc.file_name,
                        location=seg.location,
                        evidence_text=matching.snippet(text, idx),
                        scenario_id=syn.scenario_id,
                        scenario=scenario.application_scenario if scenario else "",
                        asset_type=scenario.asset_type if scenario else syn.asset_context,
                        asset_tag="",
                        confidence=matching.priority_to_confidence(syn.priority),
                        notes=f"Matched synonym '{syn.raw_term}'.",
                    )
                )

            # (b) Scenario evidence keywords (broader, lower confidence).
            #     Short/generic keywords (e.g. "PD") are weighted down because
            #     they are ambiguous across scenarios.
            for scenario in dp.scenarios.values():
                for kw in scenario.keywords:
                    idx = matching.find_term(text_lower, kw)
                    if idx < 0:
                        continue
                    # Generic keywords (e.g. "relay") must be corroborated by a
                    # scenario-specific term in the same segment, else skip.
                    if matching.is_ambiguous_keyword(kw) and not matching.has_corroborating_term(
                        text_lower, kw, strong_terms.get(scenario.scenario_id, set())
                    ):
                        continue
                    kw_len = len(kw.strip())
                    conf = 0.45 if kw_len <= 3 else (0.52 if kw_len <= 6 else 0.6)
                    raw_hits.append(
                        Evidence(
                            evidence_id="",
                            project_id=project_id,
                            source_document=doc.file_name,
                            location=seg.location,
                            evidence_text=matching.snippet(text, idx),
                            scenario_id=scenario.scenario_id,
                            scenario=scenario.application_scenario,
                            asset_type=scenario.asset_type,
                            asset_tag="",
                            confidence=conf,
                            notes=f"Matched scenario keyword '{kw}'.",
                        )
                    )

    return _dedup_evidence(raw_hits)


def _dedup_evidence(hits: list[Evidence]) -> list[Evidence]:
    # Keep the best-confidence hit per (scenario, document, snippet); then cap
    # the number of evidences per scenario per document to keep output focused.
    best: dict[tuple, Evidence] = {}
    for hit in hits:
        if hit.confidence < config.SETTINGS.thresholds.min_evidence_confidence:
            continue
        key = (hit.scenario_id, hit.source_document, hit.evidence_text[:60])
        if key not in best or hit.confidence > best[key].confidence:
            best[key] = hit

    grouped: dict[tuple, list[Evidence]] = {}
    for ev in best.values():
        grouped.setdefault((ev.scenario_id, ev.source_document), []).append(ev)

    result: list[Evidence] = []
    for group in grouped.values():
        group.sort(key=lambda e: e.confidence, reverse=True)
        result.extend(group[:MAX_EVIDENCE_PER_SCENARIO_PER_DOC])

    result.sort(key=lambda e: (-e.confidence, e.scenario_id))
    for i, ev in enumerate(result, start=1):
        ev.evidence_id = f"EVD-{i:04d}"
    return result


# --------------------------------------------------------------------------- #
# 2. Detect scenarios / map to Scenario ID
# --------------------------------------------------------------------------- #
def _asset_corroborated(scenario: Scenario, corpus_lower: str) -> bool:
    """True if a specific asset phrase for the scenario appears in the corpus.

    Uses multi-word asset phrases (e.g. 'gas insulated switchgear',
    'power transformer') so that, say, CT/VT mentions of 'transformer' don't
    falsely corroborate a power-transformer scenario.
    """
    asset = (scenario.asset_type or "").lower()
    phrases = [p.strip() for p in asset.replace("/", ",").split(",") if p.strip()]
    for phrase in phrases:
        if len(phrase.split()) >= 2 and phrase in corpus_lower:
            return True
    # Allow a distinctive single token like "gis".
    if "gis" in asset.split() and "gis" in corpus_lower:
        return True
    return False


def detect_scenarios(
    evidence: list[Evidence], dp: DataPackage, corpus_lower: str
) -> list[dict]:
    by_scenario: dict[str, list[Evidence]] = {}
    for ev in evidence:
        by_scenario.setdefault(ev.scenario_id, []).append(ev)

    detected: list[dict] = []
    for sid, evs in by_scenario.items():
        if not sid:
            continue
        max_conf = max(e.confidence for e in evs)
        # Multiple independent evidences increase confidence slightly.
        boost = min(0.1, 0.02 * (len(evs) - 1))
        scenario = dp.scenarios.get(sid)
        corroborated = bool(scenario) and _asset_corroborated(scenario, corpus_lower)
        if corroborated:
            boost += 0.12
        confidence = round(min(0.97, max_conf + boost), 3)
        detected.append(
            {
                "scenario_id": sid,
                "scenario": evs[0].scenario,
                "asset_type": evs[0].asset_type,
                "confidence": confidence,
                "asset_corroborated": corroborated,
                "evidence_count": len(evs),
                "evidence_ids": [e.evidence_id for e in evs],
            }
        )
    detected.sort(key=lambda d: d["confidence"], reverse=True)
    return detected


# --------------------------------------------------------------------------- #
# 3. Extract key metrics & parameter values  (Metric Dictionary)
# --------------------------------------------------------------------------- #
def _relevant_metric_ids(scenario: Scenario, dp: DataPackage) -> list[str]:
    """Metrics a scenario actually cares about.

    Tight by design (avoids pulling unrelated metrics like DGA onto a PD
    scenario): synonym-mapped metrics + the scenario's quantity count metric +
    metrics whose standard name appears in the scenario's own interest text
    (typical metrics / requirement output fields / keywords).
    """
    metric_ids: list[str] = []

    for syn in dp.synonyms:
        if syn.scenario_id == scenario.scenario_id and syn.metric_id:
            metric_ids.append(syn.metric_id)

    rule = dp.quantity_rule_for_scenario(scenario.scenario_id)
    if rule and rule.count_field:
        mapped = constants.COUNT_FIELD_TO_METRIC.get(rule.count_field)
        if mapped:
            metric_ids.append(mapped)

    interest = " ".join(
        [
            scenario.typical_metrics,
            " ".join(scenario.requirement_output_fields),
            " ".join(scenario.keywords),
        ]
    ).lower()
    for metric in dp.metrics.values():
        name = metric.standard_name.lower()
        if name and name in interest:
            metric_ids.append(metric.metric_id)

    seen: set[str] = set()
    ordered: list[str] = []
    for mid in metric_ids:
        if mid and mid not in seen:
            seen.add(mid)
            ordered.append(mid)
    return ordered


def _search_metric(metric, docs: list[ParsedDocument]):
    """Return (found, value, unit, source_doc, location, has_value).

    Value selection is unit-aware: for unit-bearing metrics (e.g. voltage in kV)
    only values with a compatible unit are accepted, which prevents picking up
    stray identification numbers. Text/list metrics report the matched term
    (e.g. 'UHF', 'Modbus') as the value.
    """
    terms = list(metric.synonyms)
    if metric.standard_name:
        terms.append(metric.standard_name)

    expected_units = matching.alpha_tokens(metric.unit)
    is_text = (
        "text" in metric.data_type.lower()
        or "list" in metric.data_type.lower()
        or metric.unit.strip().lower() == "text"
    )
    found_term_only = None

    for doc in docs:
        for seg in doc.segments:
            text = seg.text
            text_lower = text.lower()
            for term in terms:
                idx = matching.find_term(text_lower, term)
                if idx < 0:
                    continue

                if metric.metric_id == "MET_PQ_CLASS":
                    if matching.find_class_a(text_lower):
                        return (True, "Class A", "", doc.file_name, seg.location, True)
                    continue

                if is_text:
                    return (True, matching.normalize(term), "", doc.file_name,
                            seg.location, True)

                for vh in matching.find_values_near(text, idx):
                    if matching.unit_compatible(vh.unit, expected_units, False):
                        return (True, vh.number, vh.unit, doc.file_name,
                                seg.location, True)

                if found_term_only is None:
                    found_term_only = (doc.file_name, seg.location)

    if found_term_only:
        return (True, "", "", found_term_only[0], found_term_only[1], False)
    return (False, "", "", "", "", False)


def _requirement_type(metric, has_value: bool) -> str:
    if metric.metric_id in constants.COUNT_METRIC_IDS:
        return "Quantity Basis"
    if metric.metric_id == "MET_PQ_CLASS":
        return "Must-have"
    req = metric.required_for_matching.lower()
    if "yes" in req:
        return "Must-have"
    if "preferred" in req:
        return "Preferred"
    return "Reference"


def extract_requirements(
    docs: list[ParsedDocument],
    evidence: list[Evidence],
    detected: list[dict],
    drawing_assets: list[DrawingAsset],
    dp: DataPackage,
    project_id: str,
) -> list[Requirement]:
    requirements: list[Requirement] = []
    counter = 0

    # Quick lookup: best evidence id per scenario (for traceability).
    ev_for_scenario: dict[str, str] = {}
    for ev in sorted(evidence, key=lambda e: -e.confidence):
        ev_for_scenario.setdefault(ev.scenario_id, ev.evidence_id)

    # Drawing-derived counts per asset type (used to fill quantity-basis values).
    asset_counts: dict[str, float] = {}
    for asset in drawing_assets:
        if asset.quantity:
            asset_counts[asset.asset_type] = max(
                asset_counts.get(asset.asset_type, 0.0), asset.quantity
            )

    def _count_from_assets(metric_id: str):
        for atype in constants.METRIC_TO_ASSET_TYPES.get(metric_id, []):
            if atype in asset_counts:
                return int(asset_counts[atype]), atype
        return None, ""

    for det in detected:
        scenario = dp.scenarios.get(det["scenario_id"])
        if not scenario:
            continue
        ev_id = ev_for_scenario.get(scenario.scenario_id, "")

        for mid in _relevant_metric_ids(scenario, dp):
            metric = dp.metrics.get(mid)
            if not metric:
                continue

            value, unit, missing = "", "", ""
            has_value = False
            confidence = round(det["confidence"] * 0.5, 3)

            if mid in constants.COUNT_METRIC_IDS:
                # Counts come from the drawing asset list, not from spec text.
                count, atype = _count_from_assets(mid)
                if count is not None:
                    value, unit, has_value = str(count), "count", True
                    confidence = round(min(det["confidence"], 0.55), 3)
                    missing = (
                        f"Quantity inferred from drawing asset list ({atype}); "
                        "verify against SLD / customer."
                    )
                else:
                    missing = "Asset count not available; raise clarification question."
            else:
                found, value, unit, _src, _loc, has_value = _search_metric(metric, docs)
                if has_value:
                    confidence = round(min(det["confidence"], 0.75), 3)
                elif not found:
                    # Surface only must-have gaps to avoid noise.
                    if _requirement_type(metric, False) != "Must-have":
                        continue
                    missing = "Not stated in documents; raise clarification question."
                else:
                    missing = "Mentioned but value not stated; confirm value."

            counter += 1
            requirements.append(
                Requirement(
                    requirement_id=f"REQ-{counter:04d}",
                    project_id=project_id,
                    scenario_id=scenario.scenario_id,
                    scenario=scenario.application_scenario,
                    asset_type=scenario.asset_type,
                    asset_tag="",
                    metric_id=metric.metric_id,
                    metric_name=metric.standard_name,
                    parameter_value=value,
                    unit=unit or (metric.unit if has_value else ""),
                    requirement_type=_requirement_type(metric, has_value),
                    evidence_id=ev_id,
                    confidence=confidence,
                    missing_or_assumption=missing,
                )
            )
    return requirements


# --------------------------------------------------------------------------- #
# LLM merge helpers (augmentation layer; safe no-ops when LLM disabled)
# --------------------------------------------------------------------------- #
def _merge_scenarios(detected: list[dict], refinement: list[dict],
                     dp: DataPackage) -> tuple[list[dict], list[dict]]:
    """Apply LLM scenario judgments to the rules-detected list.

    Returns (in_scope_detected, dropped). LLM confidence/rationale override the
    rules values; LLM may add a scenario the rules missed.
    """
    by_llm = {r["scenario_id"]: r for r in refinement}
    merged: dict[str, dict] = {d["scenario_id"]: dict(d) for d in detected}

    dropped: list[dict] = []
    for sid, judgment in by_llm.items():
        scenario = dp.scenarios.get(sid)
        entry = merged.get(sid)
        if entry is None:
            if not judgment["in_scope"] or scenario is None:
                continue
            entry = {
                "scenario_id": sid,
                "scenario": scenario.application_scenario,
                "asset_type": scenario.asset_type,
                "confidence": judgment["confidence"],
                "asset_corroborated": False,
                "evidence_count": 0,
                "evidence_ids": [],
            }
            merged[sid] = entry
        entry["confidence"] = round(judgment["confidence"], 3)
        entry["llm_in_scope"] = judgment["in_scope"]
        entry["llm_rationale"] = judgment["rationale"]

    in_scope: list[dict] = []
    for sid, entry in merged.items():
        if entry.get("llm_in_scope") is False:
            dropped.append(entry)
        else:
            in_scope.append(entry)
    in_scope.sort(key=lambda d: d["confidence"], reverse=True)
    return in_scope, dropped


def _merge_requirements(rules_reqs: list[Requirement], llm_reqs: list[dict],
                        detected: list[dict], evidence: list[Evidence],
                        dp: DataPackage, project_id: str) -> list[Requirement]:
    """Fill empty values from the LLM and add LLM-only requirements."""
    in_scope_ids = {d["scenario_id"] for d in detected}
    by_key = {(r.scenario_id, r.metric_id): r for r in rules_reqs}
    ev_for_scenario: dict[str, str] = {}
    for ev in sorted(evidence, key=lambda e: -e.confidence):
        ev_for_scenario.setdefault(ev.scenario_id, ev.evidence_id)

    counter = len(rules_reqs)
    for item in llm_reqs:
        sid, mid = item["scenario_id"], item["metric_id"]
        if sid not in in_scope_ids:
            continue
        metric = dp.metrics.get(mid)
        existing = by_key.get((sid, mid))
        note = f"Extracted by LLM: {item['evidence']}" if item.get("evidence") else "LLM-extracted."
        if existing is not None:
            if not existing.parameter_value and item["value"]:
                existing.parameter_value = item["value"]
                existing.unit = item["unit"] or existing.unit
                existing.confidence = max(existing.confidence, item["confidence"])
                existing.missing_or_assumption = note
            continue
        scenario = dp.scenarios.get(sid)
        counter += 1
        req = Requirement(
            requirement_id=f"REQ-{counter:04d}",
            project_id=project_id,
            scenario_id=sid,
            scenario=scenario.application_scenario if scenario else "",
            asset_type=scenario.asset_type if scenario else "",
            asset_tag="",
            metric_id=mid,
            metric_name=metric.standard_name if metric else mid,
            parameter_value=item["value"],
            unit=item["unit"] or (metric.unit if metric else ""),
            requirement_type=item["requirement_type"],
            evidence_id=ev_for_scenario.get(sid, ""),
            confidence=item["confidence"],
            missing_or_assumption=note,
        )
        rules_reqs.append(req)
        by_key[(sid, mid)] = req
    return rules_reqs


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(
    project_dir: str | Path,
    project_id: str | None = None,
    output_dir: str | Path | None = None,
    sld_filenames: set[str] | None = None,
) -> dict:
    project_dir = Path(project_dir)
    if not project_dir.exists():
        raise FileNotFoundError(f"Project folder not found: {project_dir}")
    project_id = project_id or project_dir.name
    output_dir = Path(output_dir) if output_dir else config.OUTPUT_DIR / project_id

    dp = load_data_package()
    docs = parse_project_folder(project_dir, sld_filenames=sld_filenames)

    corpus_lower = "\n".join(d.full_text for d in docs).lower()
    evidence = extract_evidence(docs, dp, project_id)
    detected = detect_scenarios(evidence, dp, corpus_lower)

    # --- LLM augmentation: refine scenarios (precision) before requirements ---
    client = llm.get_client()
    extra_rules = load_extraction_rules()
    llm_used = False
    llm_dropped: list[dict] = []
    if client.available:
        refinement = llm_extract.refine_scenarios(
            client, dp, evidence, detected, extra_instructions=extra_rules
        )
        if refinement:
            llm_used = True
            detected, llm_dropped = _merge_scenarios(detected, refinement, dp)

    drawing_assets = extract_drawing_assets(docs, project_id, llm_client=client)
    requirements = extract_requirements(
        docs, evidence, detected, drawing_assets, dp, project_id
    )

    # --- LLM augmentation: fill / add requirement values ---
    if client.available:
        llm_reqs = llm_extract.extract_requirements(
            client, dp, detected, docs, extra_instructions=extra_rules
        )
        if llm_reqs:
            llm_used = True
            requirements = _merge_requirements(
                requirements, llm_reqs, detected, evidence, dp, project_id
            )

    result = {
        "project_id": project_id,
        "step": "1_extract_info",
        "llm": {
            "enabled": config.SETTINGS.use_llm,
            "available": client.available,
            "used": llm_used,
            "provider": config.SETTINGS.llm_provider,
            "model": config.SETTINGS.llm_deployment if client.available else None,
            "extra_rules_applied": bool(extra_rules),
            "scenarios_dropped_by_llm": llm_dropped,
        },
        "documents": [
            {"file_name": d.file_name, "doc_type": d.doc_type,
             "segments": len(d.segments)}
            for d in docs
        ],
        "detected_scenarios": detected,
        "extracted_evidence": io_utils.rows_to_dicts(evidence),
        "drawing_asset_list": io_utils.rows_to_dicts(drawing_assets),
        "structured_requirements": io_utils.rows_to_dicts(requirements),
    }

    out_path = io_utils.write_json(Path(output_dir) / "step1_extract_info.json", result)
    result["_output_path"] = str(out_path)
    return result
