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

Two extraction modes (same output contract, so Step 2 is unchanged):
  * "grounded" (default when an analysis LLM is available) — the project GPT
    reads the documents against the controlled Product Family + Product Model
    catalog and pins down the in-scope products and valuable requirements
    directly, each anchored to a verbatim quote. Scenario IDs are then derived
    STRUCTURALLY from each family/model's applicable_scenarios (no keyword
    matching). This avoids the broad scenario/synonym keyword vocabulary that
    over-matches non-requirement fragments.
  * "keyword" (offline fallback / comparison baseline) — the original rules-first
    Scenario Master + Synonym Mapping keyword engine below.

Select with QUALITROL_STEP1_MODE=grounded|keyword. Rules-first and offline by
default: with the LLM disabled the keyword engine runs on its own.
"""

from __future__ import annotations

import os
import re
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
    strip_running_boilerplate,
)
from qualitrol_core.drawing_assets import (  # noqa: E402
    augment_docs_with_image_text,
    extract_drawing_assets,
)
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
            # Table-of-contents / index pages list WHERE requirements live, not
            # the requirements themselves; never mine them for evidence.
            if llm_extract.looks_like_table_of_contents(text):
                continue
            text_lower = text.lower()

            # (a) High-value controlled synonyms -> scenario.
            for syn in dp.synonyms:
                if not syn.scenario_id:
                    continue
                idx = matching.find_term(text_lower, syn.raw_term)
                if idx < 0:
                    continue
                # Skip hits the spec explicitly places out of supply scope
                # (future expansion / optional / another party's supply).
                if matching.in_exclusion_context(text, idx):
                    continue
                scenario = dp.scenarios.get(syn.scenario_id)
                raw_hits.append(
                    Evidence(
                        evidence_id="",
                        project_id=project_id,
                        source_document=doc.file_name,
                        location=seg.location,
                        line=text[:idx].count("\n") + 1,
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
                    # Skip hits the spec explicitly marks as out of supply scope
                    # (e.g. "…is not part of the scope of this description").
                    if matching.in_exclusion_context(text, idx):
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
                        line=text[:idx].count("\n") + 1,
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


# --------------------------------------------------------------------------- #
# DGA gas-species detection
# --------------------------------------------------------------------------- #
# Controlled set of dissolved gases: (canonical, formula regex, name synonyms).
# Both the chemical formula and the spelled-out name are accepted. CO2 is listed
# before CO; the word-boundary regexes keep them (and O2/N2) from matching inside
# one another (e.g. \bco\b does not match "co2").
_DGA_GAS_TABLE = [
    ("H2",   r"\bh2\b",   ("hydrogen",)),
    ("CH4",  r"\bch4\b",  ("methane",)),
    ("C2H2", r"\bc2h2\b", ("acetylene",)),
    ("C2H4", r"\bc2h4\b", ("ethylene", "ethene")),
    ("C2H6", r"\bc2h6\b", ("ethane",)),
    ("CO2",  r"\bco2\b",  ("carbon dioxide",)),
    ("CO",   r"\bco\b",   ("carbon monoxide",)),
    ("O2",   r"\bo2\b",   ("oxygen",)),
    ("N2",   r"\bn2\b",   ("nitrogen",)),
]
# Combustible "fault gases" used to size the DGA gas count (Serveron TM8=8/9,
# TM3=3, TM1=H2). Air components O2/N2 and moisture are excluded from the count.
_DGA_FAULT_GASES = {"H2", "CH4", "C2H2", "C2H4", "C2H6", "CO", "CO2"}


def _detect_dga_gases(corpus_lower: str) -> list[str]:
    """Enumerate the distinct DGA gas species mentioned anywhere in the corpus.

    Matches each gas by its chemical formula (word-bounded) or spelled-out name.
    The ambiguous bare formulas CO / O2 / N2 (which could false-match unrelated
    text) are only accepted on a formula-only hit when at least three
    unambiguous gases are also present, i.e. we are clearly inside a DGA gas
    list. Returns canonical gas symbols in a stable order.
    """
    hits: dict[str, str] = {}
    for canon, formula_re, names in _DGA_GAS_TABLE:
        by_name = any(n in corpus_lower for n in names)
        by_formula = bool(re.search(formula_re, corpus_lower))
        if by_name or by_formula:
            hits[canon] = "name" if by_name else "formula"
    strong = {g for g in hits if g not in ("CO", "O2", "N2")}
    for amb in ("CO", "O2", "N2"):
        if hits.get(amb) == "formula" and len(strong) < 3:
            del hits[amb]
    return [g for g, _re, _n in _DGA_GAS_TABLE if g in hits]


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
            # Sum across rows of the same type (VLM emits one row per asset with
            # quantity=1; text path emits grouped rows with quantity=count).
            asset_counts[asset.asset_type] = (
                asset_counts.get(asset.asset_type, 0.0) + asset.quantity
            )

    def _count_from_assets(metric_id: str):
        for atype in constants.METRIC_TO_ASSET_TYPES.get(metric_id, []):
            if atype in asset_counts:
                return int(asset_counts[atype]), atype
        return None, ""

    # DGA gas species / count are enumerated from the full document corpus once
    # (the metric-by-metric term search only captures the first gas, e.g. "H2").
    corpus_lower = "\n".join(
        seg.text for doc in docs for seg in doc.segments
    ).lower()
    dga_gases = _detect_dga_gases(corpus_lower)

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
            elif mid == "MET_DGA_GAS_SPECIES" and dga_gases:
                # Enumerate every gas mentioned, not just the first matched term.
                value, has_value = "; ".join(dga_gases), True
                confidence = round(min(det["confidence"], 0.8), 3)
                missing = f"{len(dga_gases)} gas species detected in the documents."
            elif mid == "MET_DGA_GAS_COUNT" and dga_gases:
                # Derive the fault-gas count that drives Serveron model selection
                # (TM8 8-9 gas / TM3 3 gas / TM1 H2 only).
                fault = [g for g in dga_gases if g in _DGA_FAULT_GASES]
                n = len(fault) if fault else len(dga_gases)
                value, unit, has_value = str(n), "count", True
                confidence = round(min(det["confidence"], 0.8), 3)
                missing = (
                    "Gas count inferred from species listed in the documents: "
                    f"{', '.join(dga_gases)}."
                )
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
def _apply_include_directives(
    detected: list[dict], directives: list[dict], dp: DataPackage
) -> None:
    """Add user-requested scenarios (``include`` directives) not already detected.

    Resolves a directive's ``scenario_id`` directly, or a ``family_id`` to that
    family's applicable scenarios. Added entries are flagged and given a
    Needs-Review confidence so they are surfaced (and clarified) rather than
    silently priced.
    """
    if not directives:
        return
    present = {d["scenario_id"] for d in detected}
    for direc in directives:
        if direc.get("type") != "include":
            continue
        sids: list[str] = []
        if direc.get("scenario_id"):
            sids.append(direc["scenario_id"])
        elif direc.get("family_id"):
            fam = dp.families.get(direc["family_id"])
            if fam:
                sids.extend(fam.applicable_scenarios)
        for sid in sids:
            scenario = dp.scenarios.get(sid)
            if not scenario or sid in present:
                continue
            present.add(sid)
            detected.append({
                "scenario_id": sid,
                "scenario": scenario.application_scenario,
                "asset_type": scenario.asset_type,
                "confidence": 0.5,
                "asset_corroborated": False,
                "evidence_count": 0,
                "evidence_ids": [],
                "user_requested": True,
            })


def _combine_instructions(extra_rules: str, context_notes: str) -> str:
    """Merge operator rule-file text with user-typed project context.

    ``context_notes`` is submission-specific free text (from the Step 1 UI). It
    is appended under its own header so the LLM treats it as project context
    that supplements — but does not override — the grounded data package.
    """
    context_notes = (context_notes or "").strip()
    if not context_notes:
        return extra_rules
    context_block = (
        "--- Project context provided by the sales / application engineer for "
        "this submission (use to disambiguate scope, quantities and voltage "
        "levels; do not invent items the evidence does not support) ---\n"
        + context_notes
    )
    return f"{extra_rules}\n\n{context_block}" if extra_rules else context_block


# --------------------------------------------------------------------------- #
# Grounded extraction mode (GPT + product family/model catalog, no scenario or
# synonym keyword matching). See qualitrol_core.llm_extract.locate_requirements_
# grounded. This is the default when the LLM is available; the keyword engine
# above remains the offline fallback (QUALITROL_USE_LLM=0) and the comparison
# baseline. Force a mode with QUALITROL_STEP1_MODE=grounded|keyword.
# --------------------------------------------------------------------------- #
def _locate_quote(quote: str, docs: list[ParsedDocument]):
    """Relocate a GPT verbatim quote in the parsed documents.

    Returns (source_document, location, line, snippet) or None. Whitespace is
    treated flexibly (PDF text often wraps mid-sentence) and progressively
    shorter prefixes are tried so a lightly paraphrased quote still anchors.
    """
    ql = (quote or "").strip()
    if len(ql) < 6:
        return None
    for length in (len(ql), 160, 100, 60, 40):
        frag = ql[:length].strip()
        words = [w for w in frag.split() if w]
        if len("".join(words)) < 6:
            continue
        try:
            pattern = re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)
        except re.error:
            continue
        for doc in docs:
            for seg in doc.segments:
                # Never anchor evidence to a table-of-contents / index page: those
                # list where requirements live, not the requirements themselves.
                if llm_extract.looks_like_table_of_contents(seg.text):
                    continue
                m = pattern.search(seg.text)
                if m:
                    idx = m.start()
                    line = seg.text[:idx].count("\n") + 1
                    return (doc.file_name, seg.location, line,
                            matching.snippet(seg.text, idx))
    return None


def _grounded_extract(
    client, dp: DataPackage, docs: list[ParsedDocument],
    project_id: str, extra_rules: str,
):
    """GPT-driven, catalog-grounded Step 1 extraction.

    Returns (detected, evidence, requirements, identified_products) in the SAME
    shapes the keyword engine produces (so Step 2 is unchanged), or None when the
    LLM returns nothing usable (caller falls back to the keyword engine).

    Scenario IDs are derived STRUCTURALLY from each identified family/model's
    ``applicable_scenarios`` (the family->scenario table), not from keyword
    matching — that is the only place scenarios re-enter, purely to keep the
    Step 1 -> Step 2 contract intact.
    """
    found = llm_extract.locate_requirements_grounded(
        client, dp, docs, extra_instructions=extra_rules
    )
    if not found:
        return None

    evidence: list[Evidence] = []
    ev_counter = 0
    scen_conf: dict[str, float] = {}
    scen_ev: dict[str, list[str]] = {}
    # A product-identification call and a requirement-extraction call often
    # cite the SAME sentence for the SAME scenario (e.g. "Class A PQ meters
    # required per feeder" justifies both the product and its parameter value).
    # Collapse those into one evidence row, keyed on the located (not raw quote)
    # position, so the Spec Review modal doesn't show the identical page/line/
    # snippet twice. A different scenario anchored to the same sentence is kept
    # as its own row — that sentence genuinely supports two distinct scenarios.
    located_evidence: dict[tuple[str, str, str, int], Evidence] = {}

    def _scenarios_for(fid: str, pid: str) -> list[str]:
        if pid and pid in dp.products and dp.products[pid].applicable_scenarios:
            return dp.products[pid].applicable_scenarios
        if fid and fid in dp.families:
            return dp.families[fid].applicable_scenarios
        return []

    def _add_evidence(quote, sid, conf, note):
        nonlocal ev_counter
        loc = _locate_quote(quote, docs)
        if loc:
            src, location, line, snippet = loc
        else:
            src, location, line, snippet = "", "LLM (unlocated)", 0, (quote or "")
        # Only dedupe when the quote was actually located: unlocated quotes all
        # share the placeholder ("", "LLM (unlocated)", 0) and must never merge
        # with each other just because none of them could be pinpointed.
        dup_key = (sid, src, location, line) if (sid and loc) else None
        if dup_key and dup_key in located_evidence:
            existing = located_evidence[dup_key]
            existing.confidence = round(max(existing.confidence, conf), 3)
            if note and note not in existing.notes:
                existing.notes = f"{existing.notes} | {note}".strip(" |")
            scen_conf[sid] = max(scen_conf.get(sid, 0.0), conf)
            return existing.evidence_id
        ev_counter += 1
        eid = f"EVD-{ev_counter:04d}"
        scen = dp.scenarios.get(sid)
        new_evidence = Evidence(
            evidence_id=eid, project_id=project_id, source_document=src,
            location=location, line=line, evidence_text=snippet or (quote or ""),
            scenario_id=sid, scenario=scen.application_scenario if scen else "",
            asset_type=scen.asset_type if scen else "", asset_tag="",
            confidence=round(conf, 3), notes=note,
        )
        evidence.append(new_evidence)
        if dup_key:
            located_evidence[dup_key] = new_evidence
        if sid:
            scen_conf[sid] = max(scen_conf.get(sid, 0.0), conf)
            scen_ev.setdefault(sid, []).append(eid)
        return eid

    # 1) Identified products -> evidence + candidate scenarios.
    identified_products: list[dict] = []
    for p in found["products"]:
        fid, pid = p["family_id"], p["product_id"]
        sids = _scenarios_for(fid, pid)
        fam = dp.families.get(fid)
        primary_sid = sids[0] if sids else ""
        _add_evidence(
            p["evidence_quote"], primary_sid, p["confidence"],
            f"GPT-identified product need. {p['rationale']}".strip(),
        )
        # Record every applicable scenario so Step 2 can match this family.
        for sid in sids:
            scen_conf[sid] = max(scen_conf.get(sid, 0.0), p["confidence"])
            scen_ev.setdefault(sid, [])
        identified_products.append({
            "product_id": pid,
            "model": dp.products[pid].model if pid in dp.products else "",
            "family_id": fid,
            "family_name": fam.family_name if fam else "",
            "scenario_ids": sids,
            "confidence": round(p["confidence"], 3),
            "evidence_quote": p["evidence_quote"],
            "rationale": p["rationale"],
        })

    # 2) Grounded requirements -> Requirement rows (+ their own evidence).
    requirements: list[Requirement] = []
    req_counter = 0
    for r in found["requirements"]:
        fid, pid, mid = r["family_id"], r["product_id"], r["metric_id"]
        sids = _scenarios_for(fid, pid)
        sid = next((s for s in sids if s in scen_conf), sids[0] if sids else "")
        scen = dp.scenarios.get(sid)
        metric = dp.metrics.get(mid) if mid else None
        eid = _add_evidence(
            r["evidence_quote"], sid, r["confidence"],
            f"GPT-grounded requirement. {r['rationale']}".strip(),
        )
        req_counter += 1
        requirements.append(Requirement(
            requirement_id=f"REQ-{req_counter:04d}", project_id=project_id,
            scenario_id=sid, scenario=scen.application_scenario if scen else "",
            asset_type=scen.asset_type if scen else "", asset_tag="",
            metric_id=mid, metric_name=metric.standard_name if metric else "",
            parameter_value=r["value"],
            unit=r["unit"] or (metric.unit if metric else ""),
            requirement_type=r["requirement_type"], evidence_id=eid,
            confidence=round(r["confidence"], 3),
            missing_or_assumption=(
                f"GPT-grounded: {r['rationale']}" if r["rationale"]
                else "GPT-grounded requirement."
            ),
        ))

    # 3) Build detected_scenarios from the structurally-derived scenario set.
    detected: list[dict] = []
    for sid, conf in scen_conf.items():
        scen = dp.scenarios.get(sid)
        if not scen:
            continue
        detected.append({
            "scenario_id": sid,
            "scenario": scen.application_scenario,
            "asset_type": scen.asset_type,
            "confidence": round(min(0.97, conf), 3),
            "asset_corroborated": True,
            "evidence_count": len(scen_ev.get(sid, [])),
            "evidence_ids": scen_ev.get(sid, []),
            "grounded": True,
        })
    detected.sort(key=lambda d: d["confidence"], reverse=True)

    if not detected and not requirements:
        return None
    return detected, evidence, requirements, identified_products


def run(
    project_dir: str | Path,
    project_id: str | None = None,
    output_dir: str | Path | None = None,
    sld_filenames: set[str] | None = None,
    context_notes: str | None = None,
) -> dict:
    project_dir = Path(project_dir)
    if not project_dir.exists():
        raise FileNotFoundError(f"Project folder not found: {project_dir}")
    project_id = project_id or project_dir.name
    output_dir = Path(output_dir) if output_dir else config.OUTPUT_DIR / project_id

    dp = load_data_package()
    docs = parse_project_folder(project_dir, sld_filenames=sld_filenames)

    # Drop running headers/footers (letterhead, tender/document number, footer
    # column titles) that repeat on most pages. They are not requirements and
    # otherwise pollute keyword evidence snippets and grounded chunks alike.
    stripped_boilerplate = strip_running_boilerplate(docs)

    # Judge client (Claude) for the keyword-mode augmentation + context directives;
    # analyze client (project GPT) for the grounded requirement/product locator.
    client = llm.get_client()
    analyze_client = llm.get_client(role="analyze")
    extra_rules = _combine_instructions(load_extraction_rules(), context_notes)

    # Extraction mode: grounded (GPT + family/model catalog, no scenario/synonym
    # keyword matching) is the default when an analysis LLM is available; the
    # keyword engine is the offline fallback and the comparison baseline. Force
    # with QUALITROL_STEP1_MODE=grounded|keyword.
    mode_env = (os.getenv("QUALITROL_STEP1_MODE") or "").strip().lower()
    if mode_env in ("grounded", "keyword"):
        requested_mode = mode_env
    else:
        requested_mode = "grounded" if analyze_client.available else "keyword"

    # --- P1-A: recover image-only requirement pages in mixed PDFs, and when a
    #     project supplies only drawings, read their monitoring labels. Injected
    #     VLM text feeds both grounded and keyword extraction. ---
    aug_client = client if client.available else analyze_client
    image_text_augmented = 0
    if aug_client.available:
        image_text_augmented = augment_docs_with_image_text(docs, aug_client)

    # --- Interpret the user's free-text context into structured directives that
    #     BOTH steps can act on (exclude / include / quantity_hint / note). ---
    context_directives: list[dict] = []
    ctx_client = client if client.available else analyze_client
    if ctx_client.available and (context_notes or "").strip():
        directives = llm_extract.interpret_context(
            ctx_client, dp, context_notes, extra_instructions=extra_rules
        )
        if directives:
            context_directives = directives

    llm_used = False
    llm_dropped: list[dict] = []
    used_mode = "keyword"
    identified_products: list[dict] = []

    # --- Grounded mode: GPT locates requirements/products from the family/model
    #     catalog; scenarios are derived structurally (no keyword matching). ---
    if requested_mode == "grounded" and analyze_client.available:
        grounded = _grounded_extract(analyze_client, dp, docs, project_id, extra_rules)
        if grounded:
            detected, evidence, requirements, identified_products = grounded
            used_mode = "grounded"
            llm_used = True

    # --- Keyword mode (fallback / forced): scenario + synonym matching. ---
    if used_mode == "keyword":
        corpus_lower = "\n".join(d.full_text for d in docs).lower()
        evidence = extract_evidence(docs, dp, project_id)
        detected = detect_scenarios(evidence, dp, corpus_lower)
        if client.available:
            refinement = llm_extract.refine_scenarios(
                client, dp, evidence, detected, extra_instructions=extra_rules
            )
            if refinement:
                llm_used = True
                detected, llm_dropped = _merge_scenarios(detected, refinement, dp)

    # Honour explicit user "include" directives in both modes.
    _apply_include_directives(detected, context_directives, dp)

    drawing_assets = extract_drawing_assets(docs, project_id, llm_client=aug_client)

    # Keyword mode fills requirement values from the rules + LLM metric search.
    # Grounded mode already produced requirements; quantities are derived
    # downstream in Step 2 from the drawing_asset_list + quantity rules.
    if used_mode == "keyword":
        requirements = extract_requirements(
            docs, evidence, detected, drawing_assets, dp, project_id
        )
        if client.available:
            llm_reqs = llm_extract.extract_requirements(
                client, dp, detected, docs, extra_instructions=extra_rules
            )
            if llm_reqs:
                llm_used = True
                requirements = _merge_requirements(
                    requirements, llm_reqs, detected, evidence, dp, project_id
                )

    active_client = analyze_client if used_mode == "grounded" else client
    result = {
        "project_id": project_id,
        "step": "1_extract_info",
        "extraction_mode": used_mode,
        "llm": {
            "enabled": config.SETTINGS.use_llm,
            "available": active_client.available,
            "used": llm_used,
            "provider": config.SETTINGS.llm_provider,
            "model": getattr(active_client, "deployment", None) if active_client.available else None,
            "extra_rules_applied": bool(extra_rules),
            "user_context_applied": bool((context_notes or "").strip()),
            "scenarios_dropped_by_llm": llm_dropped,
            # Keep the legacy key for consumers while reporting the broader,
            # page-level behavior under an accurate name.
            "sld_text_augmented_docs": image_text_augmented,
            "image_text_augmented_pages": image_text_augmented,
            "running_boilerplate_lines_removed": stripped_boilerplate,
        },
        "user_context": (context_notes or "").strip(),
        "context_directives": context_directives,
        "identified_products": identified_products,
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
