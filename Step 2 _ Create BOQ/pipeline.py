"""Step 2 - Generate Matched BOQ.

Implements the right branch of the process map, consuming the Step 1 output:

    Create Candidate Product Families   (Product Family Master)
      -> Match Product Models           (Product Master)
      -> Check Product Parameters       (Product Parameter Table)
      -> Apply Compatibility Rules      (Compatibility Rules)
      -> Read Drawing Asset List        (from Step 1)
      -> Apply Quantity Rules           (Quantity Rules)
      -> Generate Draft BOQ
      -> Is information complete?  --Yes--> Draft BOQ for Engineer Review
                                   --No --> Generate Missing Info Questions

Inputs : the Step 1 JSON (detected scenarios, requirements, drawing assets).
Outputs: Product Matching (sheet 15), Draft BOQ (sheet 16) and Missing Info
         Questions (sheet 17), written as JSON under outputs/.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import config, constants, io_utils, llm, llm_extract  # noqa: E402
from qualitrol_core.data_package import DataPackage, load_data_package  # noqa: E402
from qualitrol_core.schemas import (  # noqa: E402
    BOQLine,
    MissingInfoQuestion,
    ProductMatch,
)


# --------------------------------------------------------------------------- #
# Load Step 1 artifacts
# --------------------------------------------------------------------------- #
def _load_step1(step1_path: Path) -> dict:
    data = io_utils.read_json(step1_path)
    if "structured_requirements" not in data:
        raise ValueError(f"{step1_path} is not a Step 1 output file.")
    return data


# Asset statuses that should be excluded from BOQ quantity calculations by
# default.  They are preserved in drawing_asset_list for engineer review, and
# a MissingInfoQuestion is raised when all counted assets are in these states.
_EXCLUDED_SCOPE_STATUSES = {"Future", "Provision"}


def _asset_counts(drawing_assets: list[dict]) -> dict[str, float]:
    """Aggregate in-scope drawing asset quantities by asset_type.

    Assets whose ``status`` field is "Future" or "Provision" are excluded from
    the count (they remain in ``drawing_asset_list`` for review).  Assets with
    ``status`` "Unclear" are included conservatively – a missing-info question
    is generated separately to prompt engineer confirmation.
    """
    counts: dict[str, float] = {}
    for asset in drawing_assets:
        status = asset.get("status", "Unclear")
        if status in _EXCLUDED_SCOPE_STATUSES:
            continue
        qty = asset.get("quantity") or 0
        if qty:
            atype = asset.get("asset_type", "")
            # Sum across rows of the same type. The VLM path emits one row per
            # asset (quantity=1 each); the text path emits one grouped row per
            # (status, voltage) with quantity=count. Summing yields the correct
            # total in both cases (max would collapse VLM rows to 1).
            counts[atype] = counts.get(atype, 0.0) + float(qty)
    return counts


def _future_scope_questions(
    drawing_assets: list[dict],
    asset_counts: dict[str, float],
    dp: DataPackage,
    project_id: str,
) -> list[MissingInfoQuestion]:
    """Generate Medium-priority questions when Future/Provision assets were excluded.

    For each asset type that has Future/Provision records but produced no
    in-scope count (i.e. it would be a quantity gap), ask the engineer to
    confirm whether those assets should be included.
    """
    questions: list[MissingInfoQuestion] = []
    seen_types: set[str] = set()

    for asset in drawing_assets:
        status = asset.get("status", "Unclear")
        if status not in _EXCLUDED_SCOPE_STATUSES:
            continue
        atype = asset.get("asset_type", "")
        if not atype or atype in seen_types:
            continue
        # Only raise a question when this asset type is NOT already covered by
        # in-scope assets (i.e. it was the only source for this type).
        if atype in asset_counts:
            continue
        seen_types.add(atype)

        # Find which product families rely on this asset type.
        families = []
        for fam in dp.families.values():
            if fam.primary_asset_type and atype.lower() in fam.primary_asset_type.lower():
                families.append(fam.family_name)

        fam_str = ", ".join(families[:3]) if families else "Qualitrol products"
        questions.append(
            MissingInfoQuestion(
                project_id=project_id,
                scenario_id="",
                missing_item=f"Scope confirmation: {atype} (shown as Future/Provision)",
                why_it_matters=(
                    f"{atype} assets were identified in the SLD but marked as Future or "
                    f"Provision scope. They have been excluded from BOQ quantity. "
                    f"Relevant product families: {fam_str}."
                ),
                question=(
                    f"The SLD shows {atype} asset(s) as Future or Provision scope. "
                    "Should these be included in the current BOQ? "
                    "If yes, please confirm the in-scope quantity."
                ),
                priority="Medium",
                owner="Sales / Customer",
                status="Open",
                notes=(
                    f"Auto-generated: {atype} assets exist in drawing_asset_list "
                    f"with status {_EXCLUDED_SCOPE_STATUSES}; excluded from _asset_counts."
                ),
            )
        )
    return questions


# --------------------------------------------------------------------------- #
# Quantity calculation (Quantity Rules + Drawing Asset List)
# --------------------------------------------------------------------------- #
def _calc_quantity(rule, asset_counts: dict[str, float]):
    """Return (quantity, unit, basis, assumption, derivable).

    Sizing strategy (P1-B):
      1. System-level items (software / gateway / server / licences) are quoted
         once per substation/system — fixed quantity 1.
      2. Recorder/DAU families (channel_count / feeder_count) are sized from the
         feeder count via the IDM+ channel budget (≈``FEEDERS_PER_DAU`` feeders
         per DAU); falls back to a bus/measurement-point estimate (flagged for
         confirmation) when feeders weren't extracted.
      3. Everything else counts the mapped drawing asset type directly.
    """
    import math

    if rule is None:
        return 1.0, "set", "Default 1 per scope", "No quantity rule found.", False

    count_field = (rule.count_field or "").lower()

    # (1) System-level fixed-quantity items (1 per substation/system).
    if any(h in count_field for h in constants.FIXED_ONE_COUNT_FIELD_HINTS):
        return (
            1.0,
            "set",
            f"{rule.quantity_basis} (1 per substation/system)",
            (rule.assumption or "Quoted once per system; confirm licence/user tier "
             "and redundancy."),
            True,
        )

    # (2) Recorder / DAU families sized from feeders (IDM+ channel budget).
    if count_field in constants.DAU_SIZED_COUNT_FIELDS:
        feeders = asset_counts.get("Feeder", 0)
        if feeders:
            dau = max(1, math.ceil(feeders / constants.FEEDERS_PER_DAU))
            return (
                float(dau),
                "set",
                f"Recorder/DAU count = ceil({int(feeders)} feeders / "
                f"{constants.FEEDERS_PER_DAU} per DAU)",
                (f"Sized at ~{constants.CHANNELS_PER_FEEDER} analogue channels/feeder, "
                 f"{constants.CHANNELS_PER_DAU} per DAU; confirm channel list."),
                True,
            )
        # Fallback: estimate from buses / measurement points (needs confirmation).
        for atype in ("Bus", "Measurement Point", "PCC"):
            if atype in asset_counts:
                est = max(1, int(asset_counts[atype]))
                return (
                    float(est),
                    "set",
                    f"Estimated recorder count from {atype}={est} (feeder list "
                    "unavailable)",
                    (f"{count_field} not provided; estimated from {atype}. "
                     "Confirm feeder/channel list to finalise DAU count."),
                    False,
                )
        return (
            0.0, "set", rule.quantity_basis,
            f"{count_field} not available; {rule.assumption}", False,
        )

    # (3) Default: count the mapped drawing asset type directly.
    asset_types = constants.COUNT_FIELD_TO_ASSET_TYPE.get(rule.count_field, [])
    for atype in asset_types:
        if atype in asset_counts:
            qty = asset_counts[atype]
            return (
                float(qty),
                "set",
                f"{rule.quantity_basis} (from drawing asset list: {atype}={int(qty)})",
                rule.assumption,
                True,
            )
    return (
        0.0,
        "set",
        rule.quantity_basis,
        f"{rule.count_field} not available; {rule.assumption}",
        False,
    )


# --------------------------------------------------------------------------- #
# Product matching + parameter check
# --------------------------------------------------------------------------- #
def match_products(detected: list[dict], requirements: list[dict], dp: DataPackage,
                   project_id: str) -> list[ProductMatch]:
    """One candidate per product family, attributed to its strongest scenario."""
    matches: list[ProductMatch] = []
    review_thr = config.SETTINGS.thresholds.review_confidence

    reqs_by_scenario: dict[str, list[dict]] = {}
    for req in requirements:
        reqs_by_scenario.setdefault(req["scenario_id"], []).append(req)

    # Map each family to the detected scenarios it applies to (best first).
    fam_to_scenarios: dict[str, list[dict]] = {}
    for det in sorted(detected, key=lambda d: -d["confidence"]):
        for family in dp.families_for_scenario(det["scenario_id"]):
            fam_to_scenarios.setdefault(family.family_id, []).append(det)

    for family_id, dets in fam_to_scenarios.items():
        family = dp.families[family_id]
        best = dets[0]
        sid = best["scenario_id"]
        scenario_conf = best["confidence"]
        scenario_reqs = reqs_by_scenario.get(sid, [])
        must_haves = [r for r in scenario_reqs if r["requirement_type"] == "Must-have"]
        must_met = [r for r in must_haves if r["parameter_value"]]

        products = dp.products_for_family(family_id)
        product = products[0] if products else None
        pid = product.product_id if product else f"{family_id}_TBD"
        model = product.model if product else ""
        status = product.status if product else "TBD"

        covered = []
        if product:
            covered_ids = {p.metric_id for p in dp.parameters_for_product(product.product_id)}
            covered = [r["metric_name"] for r in must_haves if r["metric_id"] in covered_ids]

        tentative = scenario_conf < review_thr
        # A product is considered "capability known" if it has a real model name.
        # Status "TBD" is treated the same as "Candidate"/"Verified" for matching
        # purposes — TBD only blocks when there is *also* no model name.
        capability_known = bool(model)

        if capability_known:
            param_result = "Matched" if len(must_met) == len(must_haves) else "Partial"
            score = round(min(0.95, scenario_conf), 2)
            status_label = (
                "Recommended"
                if score >= config.SETTINGS.thresholds.recommend_score
                and param_result == "Matched" and not tentative
                else "Needs Review"
            )
            gap = "" if param_result == "Matched" else "Some must-have parameters unconfirmed."
            recommendation = status_label
        else:
            param_result = "Missing Data (product capability TBD)"
            score = round(min(0.6, scenario_conf), 2)
            status_label = "Needs Review"
            gap = "Product model/parameter values are TBD in the data package."
            recommendation = (
                "Candidate family confirmed; validate product model & capability "
                "data with product team."
            )
        if tentative:
            gap = (gap + " Low-confidence scenario; confirm application scope.").strip()

        matches.append(
            ProductMatch(
                project_id=project_id,
                requirement_id=";".join(r["requirement_id"] for r in scenario_reqs[:3]),
                candidate_product_id=pid,
                candidate_model=model,
                family_id=family_id,
                family_name=family.family_name,
                scenario_match="Yes" if not tentative else "Partial",
                asset_match="Yes",
                parameter_match_result=param_result,
                match_score=score,
                match_status=status_label,
                matched_parameters="; ".join(covered),
                gap_or_risk=gap,
                recommendation=recommendation,
            )
        )
    matches.sort(key=lambda m: -m.match_score)
    return matches


# --------------------------------------------------------------------------- #
# Compatibility guardrails (Compatibility Rules)
# --------------------------------------------------------------------------- #
def apply_compatibility(detected: list[dict], requirements: list[dict],
                        asset_counts: dict[str, float], dp: DataPackage) -> list[dict]:
    flags: list[dict] = []
    review_thr = config.SETTINGS.thresholds.review_confidence

    reqs_by_scenario: dict[str, list[dict]] = {}
    for req in requirements:
        reqs_by_scenario.setdefault(req["scenario_id"], []).append(req)

    for det in detected:
        sid = det["scenario_id"]
        scenario = dp.scenarios.get(sid)

        # CR_013 - low confidence -> needs review.
        if det["confidence"] < review_thr:
            flags.append({
                "rule_id": "CR_013", "scenario_id": sid, "severity": "High",
                "triggered": True, "rule_type": "Evidence",
                "action": (f"Scenario confidence {det['confidence']:.2f} < "
                           f"{review_thr:.2f}: mark Needs Review; do not use as "
                           "must-have criterion."),
            })

        # Scenario-specific guardrails from the Compatibility Rules sheet.
        for rule in dp.compatibility_rules:
            if rule.scenario_id != sid:
                continue
            triggered, detail = _evaluate_rule(rule, sid, reqs_by_scenario.get(sid, []),
                                                asset_counts, scenario)
            flags.append({
                "rule_id": rule.rule_id, "scenario_id": sid,
                "severity": rule.severity, "triggered": triggered,
                "rule_type": rule.rule_type,
                "action": detail or rule.action,
            })
    return flags


def _evaluate_rule(rule, sid, scenario_reqs, asset_counts, scenario):
    """Heuristically decide if a compatibility rule's condition is met."""
    rtype = rule.rule_type.lower()
    cond = rule.condition.lower()

    def metric_missing(metric_substr: str) -> bool:
        for r in scenario_reqs:
            if metric_substr in r["metric_name"].lower():
                return not r["parameter_value"]
        return True  # not extracted at all -> treat as missing

    # Quantity / Review rules typically fire when a count/value is unknown.
    if "count" in cond or rtype in ("quantity", "review"):
        if "gis bay" in cond or sid == "GIS_PD_001":
            unknown = not any(a in asset_counts for a in ("GIS Bay", "GIS"))
            return unknown, ("GIS bay layout/count not confirmed; do not finalize "
                             "PD quantity (CR_004).") if unknown else ""
        if "breaker" in cond:
            return ("Circuit Breaker" not in asset_counts), rule.action
        if "channel" in cond:
            return metric_missing("channel"), rule.action
    if rtype == "must-have":
        if "class a" in cond:
            has_class_a = any("class a" in (r["parameter_value"] or "").lower()
                              for r in scenario_reqs)
            return has_class_a, rule.action
        if "protocol" in cond or "iec 61850" in cond:
            return metric_missing("protocol"), rule.action
    if rtype == "exclusion" and "dry-type" in cond:
        return False, rule.action  # would need explicit dry-type evidence
    return False, rule.action


# --------------------------------------------------------------------------- #
# Draft BOQ generation
# --------------------------------------------------------------------------- #
def generate_boq(detected: list[dict], matches: list[ProductMatch],
                 asset_counts: dict[str, float], compat_flags: list[dict],
                 dp: DataPackage, project_id: str) -> tuple[list[BOQLine], dict, list[dict]]:
    boq: list[BOQLine] = []
    quantity_gaps: list[dict] = []

    # High-severity triggered guardrails per scenario block the line to review.
    blocked: dict[str, list[str]] = {}
    for f in compat_flags:
        if f["triggered"] and f["severity"] == "High":
            blocked.setdefault(f["scenario_id"], []).append(f["rule_id"])

    line_no = 0
    for match in matches:
        scenario_id = _scenario_for_family(match.family_id, detected, dp)
        rule = dp.quantity_rules.get(_quantity_rule_id(match.family_id, scenario_id, dp))
        qty, unit, basis, assumption, derivable = _calc_quantity(rule, asset_counts)

        if not derivable and rule:
            quantity_gaps.append({
                "scenario_id": scenario_id,
                "count_field": rule.count_field,
                "family_name": match.family_name,
            })

        blockers = blocked.get(scenario_id, [])
        review_status = "Needs Review" if (blockers or not derivable
                                           or match.match_status != "Recommended") else "Draft"
        notes = []
        if blockers:
            notes.append("Guardrails: " + ", ".join(sorted(set(blockers))))
        if match.gap_or_risk:
            notes.append(match.gap_or_risk)

        line_no += 1
        boq.append(BOQLine(
            boq_line=line_no,
            project_id=project_id,
            product_id=match.candidate_product_id,
            product_model=match.candidate_model,
            product_description=match.family_name,
            scenario_id=scenario_id,
            related_assets=_related_assets(scenario_id, asset_counts),
            quantity=qty,
            unit=unit,
            quantity_basis=basis,
            assumption=assumption,
            confidence=match.match_score,
            review_status=review_status,
            notes=" | ".join(notes),
        ))

    summary = {
        "total_lines": len(boq),
        "lines_needing_review": sum(1 for b in boq if b.review_status == "Needs Review"),
        "lines_draft_ready": sum(1 for b in boq if b.review_status == "Draft"),
    }
    return boq, summary, quantity_gaps


def _scenario_for_family(family_id: str, detected: list[dict], dp: DataPackage) -> str:
    for det in detected:
        fam = dp.families.get(family_id)
        if fam and det["scenario_id"] in fam.applicable_scenarios:
            return det["scenario_id"]
    return ""


def _quantity_rule_id(family_id: str, scenario_id: str, dp: DataPackage) -> str:
    fam = dp.families.get(family_id)
    if fam and fam.default_quantity_rule_id:
        return fam.default_quantity_rule_id
    rule = dp.quantity_rule_for_scenario(scenario_id)
    return rule.rule_id if rule else ""


def _related_assets(scenario_id: str, asset_counts: dict[str, float]) -> str:
    rule_field_assets = []
    for atype, qty in asset_counts.items():
        rule_field_assets.append(f"{atype}={int(qty)}")
    return "; ".join(rule_field_assets)


# --------------------------------------------------------------------------- #
# Missing information questions (completeness gate)
# --------------------------------------------------------------------------- #
def generate_missing_info(detected: list[dict], requirements: list[dict],
                          compat_flags: list[dict], quantity_gaps: list[dict],
                          dp: DataPackage, project_id: str) -> list[MissingInfoQuestion]:
    questions: list[MissingInfoQuestion] = []
    seen: set[tuple[str, str]] = set()
    review_thr = config.SETTINGS.thresholds.review_confidence

    def add(q: MissingInfoQuestion):
        key = (q.scenario_id, q.missing_item.lower())
        if key not in seen:
            seen.add(key)
            q.project_id = project_id
            questions.append(q)

    # 0. Low-confidence (tentative) scenarios: confirm application scope.
    for det in detected:
        if det["confidence"] < review_thr:
            add(MissingInfoQuestion(
                scenario_id=det["scenario_id"],
                missing_item=f"Confirm scope: {det['scenario']}",
                why_it_matters=("Detected with low confidence from ambiguous text; "
                                "must be confirmed before quoting."),
                question=(f"Is '{det['scenario']}' ({det['scenario_id']}) actually in "
                          "scope for this project?"),
                priority="High", owner="Sales / Application Engineer", status="Open",
                notes=f"Scenario confidence {det['confidence']:.2f} < {review_thr:.2f}.",
            ))

    # 0b. Quantity that could not be derived from the drawing/asset list.
    for gap in quantity_gaps:
        add(MissingInfoQuestion(
            scenario_id=gap["scenario_id"],
            missing_item=f"Quantity basis: {gap['count_field']}",
            why_it_matters=f"Needed to size BOQ quantity for {gap['family_name']}.",
            question=(f"Please provide the {gap['count_field'].replace('_', ' ')} "
                      f"(e.g. from SLD / equipment list) for {gap['family_name']}."),
            priority="High", owner="Sales / Customer", status="Open",
            notes="Quantity not derivable from current drawing asset list.",
        ))

    # 1. From unmet must-have / quantity-basis requirements, enriched with the
    #    controlled template questions where available.
    for req in requirements:
        if req["parameter_value"]:
            continue
        if req["requirement_type"] not in ("Must-have", "Quantity Basis"):
            continue
        sid = req["scenario_id"]
        template = _best_template(dp.missing_info_for_scenario(sid), req["metric_name"])
        if template:
            add(MissingInfoQuestion(
                scenario_id=sid, missing_item=template.missing_item,
                why_it_matters=template.why_it_matters, question=template.question,
                priority=template.priority, owner=template.owner, status="Open",
                notes=f"Linked to {req['requirement_id']} ({req['metric_name']}).",
            ))
        else:
            add(MissingInfoQuestion(
                scenario_id=sid,
                missing_item=req["metric_name"],
                why_it_matters=f"Required ({req['requirement_type']}) for "
                               f"{req['scenario']}.",
                question=f"Please provide {req['metric_name']} for {req['scenario']}.",
                priority="High" if req["requirement_type"] == "Must-have" else "Medium",
                owner="Sales / Product Engineer", status="Open",
                notes=f"Linked to {req['requirement_id']}.",
            ))

    # 2. From triggered high/medium compatibility guardrails.
    for f in compat_flags:
        if not f["triggered"] or f["rule_id"] == "CR_013":
            continue
        sid = f["scenario_id"]
        for tpl in dp.missing_info_for_scenario(sid):
            add(MissingInfoQuestion(
                scenario_id=sid, missing_item=tpl.missing_item,
                why_it_matters=tpl.why_it_matters, question=tpl.question,
                priority=tpl.priority, owner=tpl.owner, status="Open",
                notes=f"Triggered by compatibility rule {f['rule_id']}.",
            ))

    questions.sort(key=lambda q: {"High": 0, "Medium": 1, "Low": 2}.get(q.priority, 3))
    return questions


def _best_template(templates: list[MissingInfoQuestion], metric_name: str):
    metric_low = metric_name.lower()
    for tpl in templates:
        item = tpl.missing_item.lower()
        if any(tok in item for tok in metric_low.split() if len(tok) > 3):
            return tpl
    return None


# --------------------------------------------------------------------------- #
# LLM augmentation helpers
# --------------------------------------------------------------------------- #
def _build_project_summary(detected: list[dict], requirements: list[dict],
                           asset_counts: dict[str, float]) -> dict:
    key_reqs = [
        {"scenario_id": r["scenario_id"], "metric": r["metric_name"],
         "value": r["parameter_value"], "unit": r["unit"]}
        for r in requirements if r.get("parameter_value")
    ][:25]
    return {
        "scenarios": [
            {"scenario_id": d["scenario_id"], "name": d["scenario"],
             "confidence": d["confidence"]}
            for d in detected
        ],
        "asset_counts": {k: int(v) for k, v in asset_counts.items()},
        "key_requirements": key_reqs,
    }


def _apply_match_explanations(matches: list[ProductMatch], detected: list[dict],
                              dp: DataPackage, client, project_summary: dict) -> bool:
    compact = []
    for m in matches:
        sid = _scenario_for_family(m.family_id, detected, dp)
        capability_known = bool(m.candidate_model) and "_TBD" not in m.candidate_product_id
        compact.append({
            "family_id": m.family_id, "family_name": m.family_name,
            "scenario_id": sid, "capability_known": capability_known,
            "match_score": m.match_score,
        })
    explanations = llm_extract.explain_matches(client, project_summary, compact)
    if not explanations:
        return False
    for m in matches:
        exp = explanations.get(m.family_id)
        if not exp:
            continue
        if exp.get("recommendation"):
            m.recommendation = exp["recommendation"]
        if exp.get("gap_or_risk"):
            # Preserve the critical TBD signal if present.
            existing = m.gap_or_risk
            m.gap_or_risk = (exp["gap_or_risk"]
                             if not existing or exp["gap_or_risk"] in existing
                             else f"{existing} {exp['gap_or_risk']}").strip()
    return True


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(step1_path: str | Path, output_dir: str | Path | None = None) -> dict:
    step1_path = Path(step1_path)
    step1 = _load_step1(step1_path)
    project_id = step1["project_id"]
    output_dir = Path(output_dir) if output_dir else config.OUTPUT_DIR / project_id

    dp = load_data_package()
    detected = step1["detected_scenarios"]
    requirements = step1["structured_requirements"]
    drawing_assets = step1["drawing_asset_list"]
    asset_counts = _asset_counts(drawing_assets)

    matches = match_products(detected, requirements, dp, project_id)
    compat_flags = apply_compatibility(detected, requirements, asset_counts, dp)

    # --- LLM augmentation: enrich match recommendations / risks before BOQ ---
    client = llm.get_client()
    llm_used = False
    project_summary = _build_project_summary(detected, requirements, asset_counts)
    if client.available and matches:
        llm_used = _apply_match_explanations(
            matches, detected, dp, client, project_summary
        ) or llm_used

    boq, boq_summary, quantity_gaps = generate_boq(
        detected, matches, asset_counts, compat_flags, dp, project_id
    )
    missing = generate_missing_info(
        detected, requirements, compat_flags, quantity_gaps, dp, project_id
    )
    # Add questions for Future/Provision assets that were excluded from counting.
    future_qs = _future_scope_questions(drawing_assets, asset_counts, dp, project_id)
    seen_mi = {(q.scenario_id, q.missing_item.lower()) for q in missing}
    for fq in future_qs:
        key = (fq.scenario_id, fq.missing_item.lower())
        if key not in seen_mi:
            seen_mi.add(key)
            missing.append(fq)

    # --- LLM augmentation: suggest any additional clarification questions ---
    if client.available and matches:
        existing_q = [q.question for q in missing]
        extra = llm_extract.suggest_missing_info(client, project_summary, existing_q)
        if extra:
            llm_used = True
            seen = {(q.scenario_id, q.missing_item.lower()) for q in missing}
            for item in extra:
                key = (item["scenario_id"], item["missing_item"].lower())
                if key in seen:
                    continue
                seen.add(key)
                missing.append(MissingInfoQuestion(
                    project_id=project_id,
                    scenario_id=item["scenario_id"],
                    missing_item=item["missing_item"],
                    why_it_matters=item["why_it_matters"],
                    question=item["question"],
                    priority=item["priority"],
                    owner=item["owner"],
                    status="Open",
                    notes="Suggested by LLM review.",
                ))
            missing.sort(key=lambda q: {"High": 0, "Medium": 1, "Low": 2}.get(q.priority, 3))

    # "Is information complete?" gate: complete = at least one BOQ line and no
    # High-priority customer/engineer clarification outstanding (product-
    # capability TBD is an internal validation step, not a customer-side gap).
    high_open = [q for q in missing if q.priority == "High"]
    has_scope = len(boq) > 0
    information_complete = has_scope and not high_open

    if not has_scope:
        decision = ("No Qualitrol-relevant scope detected; request project "
                    "specification / SLD / equipment list from the customer.")
    elif information_complete:
        decision = "Draft BOQ for Engineer Review"
    else:
        decision = "Generate Missing Info Questions (human clarification first)"

    result = {
        "project_id": project_id,
        "step": "2_create_boq",
        "llm": {
            "enabled": config.SETTINGS.use_llm,
            "available": client.available,
            "used": llm_used,
            "provider": config.SETTINGS.llm_provider,
            "model": config.SETTINGS.llm_deployment if client.available else None,
        },
        "information_complete": information_complete,
        "decision": decision,
        "boq_summary": boq_summary,
        "product_matching": io_utils.rows_to_dicts(matches),
        "compatibility_flags": compat_flags,
        "draft_boq": io_utils.rows_to_dicts(boq),
        "missing_info_questions": io_utils.rows_to_dicts(missing),
    }

    out_path = io_utils.write_json(Path(output_dir) / "step2_create_boq.json", result)
    result["_output_path"] = str(out_path)

    # Generate the finished BOQ Excel from the standard template (best-effort).
    try:
        from qualitrol_core import boq_excel

        boq_path = boq_excel.generate_boq_excel(
            result, Path(output_dir) / f"BOQ-{project_id}.xlsx"
        )
        result["_boq_excel_path"] = str(boq_path)
    except Exception as exc:  # noqa: BLE001 - never fail the pipeline over the report
        result["_boq_excel_error"] = str(exc)

    return result
