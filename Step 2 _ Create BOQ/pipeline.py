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

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import (  # noqa: E402
    config,
    constants,
    io_utils,
    llm,
    llm_extract,
    matching,
)
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


# --------------------------------------------------------------------------- #
# User scope-exclusion filter
# --------------------------------------------------------------------------- #
# Free-text project context (e.g. "do not include trainings, service etc. in
# current draft scope") lets the engineer drop whole auto-added CATEGORIES from
# the draft BOQ. These service / training / spares / commissioning lines are
# added deterministically (base BOQ + MEA expansion) and are NOT scenario-
# driven, so the Step 1 LLM prompt cannot remove them — they are filtered here,
# where the BOQ lines actually exist. The user_context is carried in the Step 1
# output JSON, so no extra plumbing from the web layer is required.
_EXCLUSION_CUES = (
    "do not include", "don't include", "dont include", "do not add",
    "exclude", "excluding", "without", "omit", "drop ", "remove ",
    "not in scope", "out of scope", "not required", "no need",
)
# category -> (trigger words in the user's text, family ids to drop,
#              model/description substring hints to drop)
_SCOPE_CATEGORIES = {
    "service": {
        "triggers": ("service", "services"),
        "families": {"PF_SERVICES", "PF_COMMON"},
        # NOTE: no bare "service" substring here — it false-matches software whose
        # description happens to contain the word (e.g. "3yr subscription service").
        # The PF_SERVICES / PF_COMMON family match covers the real service lines.
        "hints": (
            "commissioning", "mobilis", "factory fat", "factory acceptance",
            "testing & commissioning", "energisation", "cybersecurity",
            "communication establishment",
        ),
    },
    "training": {"triggers": ("training", "trainings"), "families": set(),
                 "hints": ("training",)},
    "commissioning": {"triggers": ("commissioning",), "families": set(),
                      "hints": ("commissioning", "mobilis")},
    "spares": {"triggers": ("spare", "spares"), "families": set(),
               "hints": ("spare",)},
    "fat": {"triggers": ("fat", "factory acceptance"), "families": set(),
            "hints": ("factory fat", "factory acceptance")},
    "software": {"triggers": ("software", "licence", "license"),
                 "families": {"PF_SW_LIC"},
                 "hints": ("antivirus", "whitelisting", "backup software", "espdc")},
    "network": {"triggers": ("network", "switch", "firewall"),
                "families": {"PF_NET_SEC"}, "hints": ("l2 switch", "l3 switch", "firewall")},
    "timing": {"triggers": ("gps", "timing"), "families": {"PF_TIMING"},
               "hints": ("gps antenna", "gps splitter")},
    "panel": {"triggers": ("panel", "cubicle", "cabinet"),
              "families": {"PF_MON_PANEL"}, "hints": ("cubicle", "fms panel")},
}


def _parse_scope_exclusions(user_context: str) -> set[str]:
    """Return the set of BOQ categories the user asked to exclude.

    Requires BOTH an exclusion cue (e.g. "do not include", "exclude",
    "without") and a category trigger word (e.g. "training", "service") to be
    present in the context text, so ordinary mentions don't trigger a drop.
    """
    text = (user_context or "").lower()
    if not text or not any(cue in text for cue in _EXCLUSION_CUES):
        return set()
    excluded: set[str] = set()
    for category, spec in _SCOPE_CATEGORIES.items():
        if any(trig in text for trig in spec["triggers"]):
            excluded.add(category)
    return excluded


def _line_in_excluded_category(line: BOQLine, categories: set[str]) -> bool:
    fid = (line.family_id or "") if hasattr(line, "family_id") else ""
    blob = " ".join([
        getattr(line, "product_model", "") or "",
        getattr(line, "product_description", "") or "",
        getattr(line, "quantity_basis", "") or "",
    ]).lower()
    for category in categories:
        spec = _SCOPE_CATEGORIES[category]
        if fid and fid in spec["families"]:
            return True
        if any(hint in blob for hint in spec["hints"]):
            return True
    return False


def _apply_scope_exclusions(
    boq: list[BOQLine], excluded_categories: set[str]
) -> list[dict]:
    """Drop BOQ lines matching excluded categories; return the dropped lines.

    Mutates ``boq`` in place and renumbers the surviving lines.
    """
    if not excluded_categories:
        return []
    dropped: list[dict] = []
    kept: list[BOQLine] = []
    for line in boq:
        if _line_in_excluded_category(line, excluded_categories):
            dropped.append({
                "product_model": line.product_model,
                "product_description": line.product_description,
                "family_id": getattr(line, "family_id", ""),
            })
        else:
            kept.append(line)
    if dropped:
        boq[:] = kept
        for i, line in enumerate(boq, start=1):
            line.boq_line = i
    return dropped


def _exclusion_spec_from_directives(directives: list[dict]) -> tuple[set, set, set]:
    """Return (categories, family_ids, scenario_ids) from ``exclude`` directives."""
    cats, fams, scens = set(), set(), set()
    for d in directives or []:
        if d.get("type") != "exclude":
            continue
        if d.get("category"):
            cats.add(d["category"])
        if d.get("family_id"):
            fams.add(d["family_id"])
        if d.get("scenario_id"):
            scens.add(d["scenario_id"])
    return cats, fams, scens


def _apply_context_exclusions(
    boq: list[BOQLine], directives: list[dict], dp: DataPackage, fallback_context: str
) -> tuple[list[dict], dict]:
    """Drop BOQ lines per structured exclude directives (or, when none are
    available, a deterministic keyword parse of the raw context).

    Matching is by: excluded category (family set + description hints), excluded
    family_id (resolved via the line's product), or excluded scenario_id.
    Returns (dropped_lines, applied_summary).
    """
    cats, fam_ids, scen_ids = _exclusion_spec_from_directives(directives)
    used_fallback = False
    if not (cats or fam_ids or scen_ids):
        cats = _parse_scope_exclusions(fallback_context)
        used_fallback = bool(cats)
    if not (cats or fam_ids or scen_ids):
        return [], {}

    # Expand category-mapped families into the family exclusion set.
    fam_all = set(fam_ids)
    for c in cats:
        fam_all |= _SCOPE_CATEGORIES.get(c, {}).get("families", set())

    dropped: list[dict] = []
    kept: list[BOQLine] = []
    for line in boq:
        prod = dp.products.get(getattr(line, "product_id", "") or "")
        fid = prod.family_id if prod else ""
        drop = False
        if fid and fid in fam_all:
            drop = True
        elif line.scenario_id and line.scenario_id in scen_ids:
            drop = True
        elif cats and _line_in_excluded_category(line, cats):
            drop = True
        if drop:
            dropped.append({"product_model": line.product_model,
                            "product_description": line.product_description,
                            "family_id": fid})
        else:
            kept.append(line)
    if dropped:
        boq[:] = kept
        for i, line in enumerate(boq, start=1):
            line.boq_line = i
    summary = {
        "categories": sorted(cats), "family_ids": sorted(fam_ids),
        "scenario_ids": sorted(scen_ids),
        "source": "keyword-fallback" if used_fallback else "llm-directives",
        "lines": len(dropped),
    }
    return dropped, summary


def _apply_quantity_hints(
    asset_counts: dict, directives: list[dict], dp: DataPackage
) -> list[dict]:
    """Seed/override drawing asset counts from user ``quantity_hint`` directives.

    A hint maps to a drawing asset type either directly (``asset_type``) or via a
    ``count_field`` (using ``COUNT_FIELD_TO_ASSET_TYPE``). The user-stated value
    overrides the drawing-derived count so sizing reflects what the operator
    explicitly provided. Returns the list of applied hints for transparency.
    """
    applied: list[dict] = []
    for d in directives or []:
        if d.get("type") != "quantity_hint":
            continue
        value = d.get("value") or 0
        if not value:
            continue
        atypes: list[str] = []
        if d.get("asset_type"):
            atypes = [d["asset_type"]]
        elif d.get("count_field"):
            atypes = constants.COUNT_FIELD_TO_ASSET_TYPE.get(d["count_field"], [])
        if not atypes:
            continue
        target = atypes[0]
        asset_counts[target] = float(value)
        applied.append({"asset_type": target, "value": float(value),
                        "count_field": d.get("count_field", ""),
                        "rationale": d.get("rationale", "")})
    return applied


def _leading_number(value) -> float | None:
    """Parse the first integer/decimal out of a free-text value ('3 count' -> 3)."""
    if value is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def _seed_counts_from_requirements(
    asset_counts: dict, requirements: list[dict], dp: DataPackage
) -> list[dict]:
    """Seed ``asset_counts`` from document-extracted 'Quantity Basis' requirements.

    CBM / transformer projects usually have no SLD at the BOQ stage, so the only
    count source is the tender / technical-spec text. Step 1 captures these as
    "Quantity Basis" requirements (e.g. Asset Count = 3). Each is mapped to its
    target drawing-asset type via the scenario's quantity-rule ``count_field``
    and used to seed the count so Step 2 can size the BOQ without a drawing.
    A real drawing-derived count (already present in ``asset_counts``) always
    wins over text; among competing text values the largest is kept.
    """
    drawing_provided = set(asset_counts)
    seeded: dict[str, dict] = {}
    for r in requirements:
        if (r.get("requirement_type") or "") != "Quantity Basis":
            continue
        val = _leading_number(r.get("parameter_value"))
        if not val:
            continue
        rule = dp.quantity_rule_for_scenario(r.get("scenario_id", ""))
        if not rule or not rule.count_field:
            continue
        atypes = constants.COUNT_FIELD_TO_ASSET_TYPE.get(rule.count_field, [])
        if not atypes:
            continue
        target = atypes[0]
        if target in drawing_provided:
            continue  # a real drawing count wins over free text
        if val > seeded.get(target, {}).get("value", 0):
            seeded[target] = {
                "asset_type": target, "value": float(val),
                "scenario_id": r.get("scenario_id", ""),
                "count_field": rule.count_field,
                "metric": r.get("metric_name", ""),
                "source": "document text (no drawing)",
            }
    for t, info in seeded.items():
        asset_counts[t] = info["value"]
    return list(seeded.values())


# Transformer sub-families that are folded into the integrated QTMS system when
# the CBM umbrella scenario (TR_CBM_001) is detected. Their scope is emitted as
# the per-bank config template by ``expand_cbm_config`` instead of as separate
# standalone family lines (avoids double-quoting; matches the real per-bank BOM).
_CBM_FOLDED_FAMILIES = {
    "PF_TR_TEMP", "PF_OLTC", "PF_AUX_SENSOR", "PF_BUSHING",
    "PF_DGA", "PF_TR_PD", "PF_PD_TRANSFORMER",
}

# Per-transformer-bank CBM configuration template (from the CBM KB real cases -
# the "融合配置后" 6U per-bank config). (product_id, qty_per_bank, group). The QTMS
# base chassis itself is emitted by the PF_TR_CBM family line; this expands its
# modules, sensors, DGA, bushing, PD and power-meter scope.
_CBM_PER_BANK_TEMPLATE = [
    ("GMB_QTMS_AI", 2, "QTMS module"),
    ("CBM_RTD_PT100", 5, "QTMS analog sensor (RTD)"),
    ("CBM_CLAMP_CT", 8, "QTMS analog sensor (CT: fan/pump/wind/OLTC)"),
    ("CBM_TAP_INPUT", 1, "QTMS analog input (tap position)"),
    ("GMB_QTMS_DI", 1, "QTMS module"),
    ("GMB_QTMS_RO", 1, "QTMS module"),
    ("GMB_QTMS_FO", 1, "QTMS module (fibre-optic)"),
    ("CBM_FO_INTERNAL", 5, "Direct winding temperature"),
    ("CBM_FO_EXTERNAL", 5, "Direct winding temperature"),
    ("PROD_PDF_TWP_TANK_WALL_PLATE", 1, "Direct winding temperature"),
    ("CBM_NXP611", 1, "Direct winding temperature"),
    ("PROD_PF_DGA_03", 1, "DGA monitor (multi-gas, Serveron TM8)"),
    ("CBM_TM_MOIST", 1, "DGA accessory"),
    ("CBM_TM_ACCESS", 1, "DGA accessory"),
    ("CBM_QBM_ADAPT", 3, "Bushing monitoring"),
    ("CBM_QBM_TPCABLE", 3, "Bushing monitoring"),
    ("DMS-QPDM-6", 1, "Partial discharge"),
    ("DMS-CPL-IC44", 6, "Partial discharge (couplers)"),
    ("CBM_POWER_METER", 2, "Power meter"),
]


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
                # Source-neutral: the count may come from the drawing asset list
                # OR (for no-SLD CBM projects) from the tender/spec text.
                f"{rule.quantity_basis} (asset count: {atype}={int(qty)})",
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
# Weight a requirement contributes to a model's parameter-fit score.
_PARAM_WEIGHT = {"Must-have": 3.0, "Preferred": 1.0, "Quantity Basis": 1.0,
                 "Reference": 0.5}
# Commercial safety default: prefer a validated model over an unverified one.
_STATUS_RANK = {"verified": 0, "candidate": 1}

# Model-name hints that indicate a SOFTWARE / master-station product (which must
# never be sized per-DAU as if it were recorder hardware).
_SOFTWARE_MODEL_HINTS = (
    "iq+", "tmview", "master station", "software", "antivirus",
    "whitelisting", "backup", "espdc",
)


def _is_software_model(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in _SOFTWARE_MODEL_HINTS)


def _unit_for(family_id: str, family_name: str, model: str) -> str:
    """Category-aware BOQ unit (instead of a blanket 'set').

    - Services            -> 'lot'   (day-rate / scope; days confirmed by engineer)
    - Software / licences  -> 'license'
    - Everything else (DAU hardware, panels, network, timing, accessories) -> 'ea'
    """
    fid = family_id or ""
    fn = (family_name or "").lower()
    m = (model or "").lower()
    if fid == "PF_SERVICES" or "service" in fn:
        return "lot"
    if (fid == "PF_SW_LIC" or "software" in fn or "platform" in fn
            or "licen" in fn or "licen" in m):
        return "license"
    return "ea"


def _score_product(product, scenario_reqs: list[dict], dp: DataPackage) -> dict:
    """Score one product model against a scenario's extracted requirements.

    Compares each requirement's extracted value to the product's parameter rows
    (08_Product_Parameter_Template) and classifies it as confirmed / violated /
    unconfirmed, producing a parameter-fit score used to rank models within a
    family. Requirements with no extracted value (TBD) can't be checked and are
    counted as unconfirmed rather than penalised.
    """
    by_metric: dict[str, list] = {}
    for p in dp.parameters_for_product(product.product_id):
        by_metric.setdefault(p.metric_id, []).append(p)

    confirmed: list[str] = []
    violated: list[str] = []
    unconfirmed: list[str] = []
    points = 0.0
    order = {"pass": 0, "unknown": 1, "fail": 2}

    for req in scenario_reqs:
        weight = _PARAM_WEIGHT.get(req["requirement_type"], 0.5)
        params = by_metric.get(req["metric_id"])
        value = req.get("parameter_value") or ""
        if not params:
            # Product doesn't spec this metric; only note it if the customer
            # actually stated a must-have value we'd have wanted to confirm.
            if value and req["requirement_type"] == "Must-have":
                unconfirmed.append(req["metric_name"])
            continue
        verdict = sorted(
            (matching.match_parameter_value(value, p.min_value, p.max_value,
                                            p.supported_value) for p in params),
            key=lambda v: order[v],
        )[0]
        if verdict == "pass":
            confirmed.append(req["metric_name"])
            points += weight
        elif verdict == "fail":
            violated.append(req["metric_name"])
            points -= weight * 1.5
        elif value:
            unconfirmed.append(req["metric_name"])

    must_violated = any(
        r["requirement_type"] == "Must-have" and r["metric_name"] in violated
        for r in scenario_reqs
    )
    if (product.status or "").lower() == "verified":
        points += 0.5
    return {
        "product": product,
        "confirmed": confirmed,
        "violated": violated,
        "unconfirmed": unconfirmed,
        "points": round(points, 2),
        "must_violated": must_violated,
        "n_params": len(by_metric),
    }


def _select_best_product(products: list, scenario_reqs: list[dict],
                         dp: DataPackage) -> dict | None:
    """Rank all models in a family and return the best-scoring one.

    Ranking policy (tunable): (1) never rank a model that violates a must-have
    parameter on top; (2) prefer Verified over Candidate; (3) then by
    parameter-fit points. Ties preserve the catalog order (stable sort), so a
    model is only promoted above the family's first-listed one when there is a
    real discriminating signal — otherwise today's default choice is kept.
    """
    if not products:
        return None
    scored = [_score_product(p, scenario_reqs, dp) for p in products]
    scored.sort(key=lambda s: (
        s["must_violated"],
        _STATUS_RANK.get((s["product"].status or "").lower(), 2),
        -s["points"],
    ))
    return scored[0]


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
        # Accessory / panel / network / timing / software / service families are
        # produced by the MEA expansion pass (quantified by the ruleset), not by
        # this generic per-family matcher — skip them here to avoid duplicates.
        if family_id in constants.EXPANSION_FAMILY_IDS:
            continue
        family = dp.families[family_id]
        best_det = dets[0]
        sid = best_det["scenario_id"]
        scenario_conf = best_det["confidence"]
        scenario_reqs = reqs_by_scenario.get(sid, [])
        must_haves = [r for r in scenario_reqs if r["requirement_type"] == "Must-have"]

        # Score every model in the family and pick the best fit (replaces the
        # previous "take products[0]" behaviour).
        products = dp.products_for_family(family_id)
        # A recorder / DAU-sized family must be quoted as real DAU hardware
        # (IDM+ / Informa), never as software. A data-package artifact lists
        # iQ+ (master-station software) under the DFR family, which — sized by
        # the per-DAU formula — produced a bogus "iQ+ x14" line. Drop software
        # models from DAU-sized families when hardware alternatives exist.
        fam_rule = dp.quantity_rules.get(family.default_quantity_rule_id)
        is_dau_family = bool(
            fam_rule and (fam_rule.count_field or "").lower()
            in constants.DAU_SIZED_COUNT_FIELDS
        )
        if is_dau_family:
            hardware = [p for p in products if not _is_software_model(p.model)]
            if hardware:
                products = hardware
        best = _select_best_product(products, scenario_reqs, dp)
        product = best["product"] if best else None
        pid = product.product_id if product else f"{family_id}_TBD"
        model = product.model if product else ""
        status = product.status if product else "TBD"

        confirmed = best["confirmed"] if best else []
        violated = best["violated"] if best else []
        must_violated = best["must_violated"] if best else False
        # Only list parameters whose extracted value was actually confirmed
        # against the chosen model (not merely "the product specs this metric").
        matched_display = confirmed

        tentative = scenario_conf < review_thr
        # A product is considered "capability known" if it has a real model name.
        # Status "TBD" is treated the same as "Candidate"/"Verified" for matching
        # purposes — TBD only blocks when there is *also* no model name.
        capability_known = bool(model)
        # "Matched" only when every must-have has been positively confirmed
        # against the chosen model (TBD must-haves keep it at "Partial").
        all_must_confirmed = (
            bool(must_haves)
            and all(r["metric_name"] in confirmed for r in must_haves)
        )

        if capability_known and must_violated:
            # The best available model still violates a hard requirement.
            param_result = "Mismatch"
            score = round(min(0.5, scenario_conf), 2)
            status_label = "Needs Review"
            gap = "Parameter conflict: " + ", ".join(violated) + "."
            recommendation = "Parameter conflict; review product selection or catalog."
        elif capability_known:
            param_result = "Matched" if all_must_confirmed else "Partial"
            score = round(min(0.95, scenario_conf), 2)
            status_label = (
                "Recommended"
                if score >= config.SETTINGS.thresholds.recommend_score
                and param_result == "Matched" and not tentative
                else "Needs Review"
            )
            if param_result == "Matched":
                gap = ""
            else:
                unmet = [r["metric_name"] for r in must_haves
                         if r["metric_name"] not in confirmed]
                gap = "Must-have parameters unconfirmed" + (
                    ": " + ", ".join(unmet) if unmet else "") + "."
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
                matched_parameters="; ".join(matched_display),
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
                 dp: DataPackage, project_id: str,
                 fold_families: set[str] | None = None) -> tuple[list[BOQLine], dict, list[dict]]:
    boq: list[BOQLine] = []
    quantity_gaps: list[dict] = []

    # Families to suppress as standalone lines.
    #  * ``fold_families``: folded into an integrated system (e.g. CBM QTMS) and
    #    emitted by a dedicated expansion pass instead.
    #  * PD de-duplication: when the QPDM family (PF_TR_PD) is matched for the
    #    same transformer-PD need, drop the legacy PF_PD_TRANSFORMER line.
    skip_families: set[str] = set(fold_families or set())
    matched_families = {m.family_id for m in matches}
    if "PF_TR_PD" in matched_families and "PF_PD_TRANSFORMER" in matched_families:
        skip_families.add("PF_PD_TRANSFORMER")

    # High-severity triggered guardrails per scenario block the line to review.
    blocked: dict[str, list[str]] = {}
    for f in compat_flags:
        if f["triggered"] and f["severity"] == "High":
            blocked.setdefault(f["scenario_id"], []).append(f["rule_id"])

    line_no = 0
    for match in matches:
        if match.family_id in skip_families:
            continue
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
        # An in-scope family whose count could not be derived must not read as a
        # real "0" (that looks like "not required" and silently drops the line's
        # value, e.g. the SF6 GDM sensor count). Flag it explicitly as TBD so a
        # reviewer fills the quantity; the paired clarification question carries
        # the ask.
        if not derivable and not qty:
            notes.append(
                "QUANTITY TBD — not derivable from the documents/drawings provided; "
                "confirm count before quoting (do not treat as zero)."
            )
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
            unit=_unit_for(match.family_id, match.family_name, match.candidate_model),
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


def _find_kb_product(dp: DataPackage, family_id: str, keyword: str):
    """First product in a family whose model/description contains ``keyword``."""
    kw = keyword.lower()
    for p in dp.products_for_family(family_id):
        blob = f"{p.model} {p.description}".lower()
        if kw in blob:
            return p
    return None


def expand_mea_config(boq: list[BOQLine], detected: list[dict], dp: DataPackage,
                      project_id: str) -> list[BOQLine]:
    """Append accessories / panels / software / services per the TAQA MEA ruleset.

    Runs after the base (recorder) BOQ. When recorder/DAU scope is present it
    sizes and adds the packaging the real engineered BOQ carries — GPS timing,
    test switches, EPG licences, network switches, field panels, LEV/PDC cabinet
    contents and services — using the ratios in ``constants`` (sourced from the
    CR_MEA_* rules) and product models from the reverse-populated KB families.
    """
    import math

    rec_lines = [b for b in boq if b.scenario_id in constants.RECORDER_SCENARIO_IDS]
    if not rec_lines:
        return []  # no recorder scope -> nothing to package

    det_ids = {d["scenario_id"] for d in detected}
    has_pmu_wams = bool({"PMU_001", "WAMS_001"} & det_ids)
    has_pmu = "PMU_001" in det_ids
    # DAU proxy: one physical recorder per monitored bay ~= the largest single
    # recorder line quantity (summing functions would triple-count the same bay).
    n_dau = max((int(b.quantity or 0) for b in rec_lines), default=0)
    if n_dau <= 0:
        n_dau = 1
    pmu_qty = max((int(b.quantity or 0) for b in rec_lines
                   if b.scenario_id == "PMU_001"), default=0)
    n_masters = math.ceil(n_dau / constants.MEA_DAUS_PER_GPS_MASTER) if has_pmu_wams else 0
    n_panels = max(1, math.ceil(n_dau / constants.MEA_DEVICES_PER_PANEL))

    # (family_id, model keyword, quantity, scenario_id, basis / rule)
    specs: list[tuple] = [
        ("PF_PANEL_ACC", "test", n_dau, "FMS_001",
         f"Test switch per device (CR_MEA_09): {n_dau} DAU"),
        ("PF_NET_SEC", "l2 switch", n_panels, "COMM_SCADA_001",
         f"Managed L2 switch per panel (CR_MEA_07): {n_panels} panel(s)"),
        ("PF_NET_SEC", "l3 switch", n_panels, "COMM_SCADA_001",
         f"Managed L3 switch per panel (CR_MEA_07): {n_panels} panel(s)"),
        ("PF_NET_SEC", "firewall", 2, "COMM_SCADA_001",
         "Firewall(s) for the monitoring LAN / OETC link (CR_MEA_11)"),
        ("PF_MON_PANEL", "fms panel", n_panels, "FMS_001",
         f"Field panel per ~{constants.MEA_DEVICES_PER_PANEL} DAU (CR_MEA_05): {n_panels} panel(s)"),
    ]
    if has_pmu_wams and n_masters:
        specs.append(
            ("PF_TIMING", "antenna", n_masters * constants.MEA_ANTENNAS_PER_MASTER,
             "PMU_001", f"{constants.MEA_ANTENNAS_PER_MASTER} antennas per GPS master (CR_MEA_06)"))
        # GPS 2-Way splitter: omitted by default (DAY-2 feedback: not required).
        if constants.MEA_INCLUDE_GPS_SPLITTER:
            specs.append(
                ("PF_TIMING", "splitter", n_masters, "PMU_001",
                 f"GPS splitter per master; 1 master / {constants.MEA_DAUS_PER_GPS_MASTER} DAU (CR_MEA_06)"))
    if has_pmu and pmu_qty:
        specs.append(
            ("PF_SW_LIC", "epg pmu", constants.MEA_EPG_LICENSES_PER_PMU * pmu_qty,
             "PMU_001", f"{constants.MEA_EPG_LICENSES_PER_PMU} EPG licences per PMU device (CR_MEA_08): {pmu_qty} device(s)"))

    # LEV cabinet + standard contents (one per system) — CR_MEA_11.
    lev_items = [
        ("PF_MON_PANEL", "lev cubicle", 1, "LEV cabinet (as per Transco spec)"),
        ("PF_MON_PANEL", "industrial rack", 1, "Industrial rack-mounted PC"),
        ("PF_SW_LIC", "iq+", 1, "iQ+ master-station software (1 per system)"),
        ("PF_SW_LIC", "trend micro", 1, "Trend Micro antivirus"),
        ("PF_SW_LIC", "trellix", 1, "Trellix whitelisting"),
        ("PF_SW_LIC", "acronis", 1, "Acronis backup"),
        ("PF_MON_PANEL", "monitor", 1, "Monitor & keyboard"),
        ("PF_MON_PANEL", "printer", 1, "Printer"),
        ("PF_MON_PANEL", "annunciator", 1, "Alarm annunciator"),
    ]
    for fid, kw, qty, basis in lev_items:
        specs.append((fid, kw, qty, "COMM_SCADA_001", basis + " (CR_MEA_11)"))
    # PDC cabinet extras (only when PMU/WAMS present) — CR_MEA_12.
    if has_pmu_wams:
        for fid, kw, basis in [
            ("PF_MON_PANEL", "pdc cubicle", "PDC cabinet (as per Transco spec)"),
            ("PF_SW_LIC", "espdc", "eSPDC phasor data concentrator software"),
            ("PF_MON_PANEL", "kvm", "KVM switch"),
        ]:
            specs.append((fid, kw, 1, "PMU_001", basis + " (CR_MEA_12)"))

    # Services & spares: omitted from the priced draft by default (DAY-2
    # feedback — they belong in the General notes, not as BOQ line items).
    if constants.MEA_INCLUDE_SERVICES:
        for p in dp.products_for_family("PF_SERVICES"):
            specs.append(("PF_SERVICES", p.model.lower()[:12], 1, "COMM_SCADA_001",
                          "Engineering / commissioning service (day-rate; QR_SERVICES_001)"))

    extra: list[BOQLine] = []
    line_no = max((b.boq_line for b in boq), default=0)
    seen: set = set()
    for fid, kw, qty, sid, basis in specs:
        prod = _find_kb_product(dp, fid, kw)
        if not prod or (prod.product_id in seen):
            continue
        seen.add(prod.product_id)
        line_no += 1
        extra.append(BOQLine(
            boq_line=line_no, project_id=project_id,
            product_id=prod.product_id, product_model=prod.model,
            product_description=prod.description or prod.family_name,
            scenario_id=sid, related_assets=f"DAU count={n_dau}",
            quantity=float(qty),
            unit=_unit_for(prod.family_id, prod.family_name, prod.model),
            quantity_basis=f"MEA ruleset — {basis}",
            assumption="Auto-added by TAQA MEA config expansion; confirm quantities.",
            confidence=0.55, review_status="Needs Review",
            notes="Added by MEA ruleset expansion (CR_MEA_*).",
        ))
    return extra


def _dga_model_for_count(requirements: list[dict]) -> tuple[str, str]:
    """Pick the Serveron DGA model from the extracted DGA gas count.

    TM8 (8-9 gas) / TM3 (3 gas) / TM1 (H2 only). Defaults to multi-gas TM8 for a
    main-transformer online DGA when the count is unknown. Returns (product_id,
    rationale).
    """
    n = None
    for r in requirements:
        if r.get("metric_id") == "MET_DGA_GAS_COUNT":
            n = _leading_number(r.get("parameter_value"))
            break
    if n is None:
        return "PROD_PF_DGA_03", "gas count not stated; default multi-gas TM8"
    if n <= 1:
        return "PROD_PF_DGA_01", f"{int(n)}-gas (H2) -> TM1"
    if n <= 3:
        return "PROD_PF_DGA_02", f"{int(n)}-gas -> TM3"
    return "PROD_PF_DGA_03", f"{int(n)}-gas -> TM8"


def expand_cbm_config(boq: list[BOQLine], detected: list[dict],
                      asset_counts: dict[str, float], dp: DataPackage,
                      project_id: str,
                      requirements: list[dict] | None = None) -> list[BOQLine]:
    """Expand the integrated transformer CBM system into its per-bank BOM.

    Triggered when the CBM umbrella scenario (TR_CBM_001) is detected. The
    PF_TR_CBM family line provides the QTMS base chassis; this pass adds the
    per-transformer-bank modules, sensors, DGA (multi-gas), bushing, PD and
    power-meter scope from the CBM KB template, scaled by the transformer count.
    Because CBM projects rarely give exact per-parameter counts at BOQ, the
    template is emitted as a clearly-flagged assumption so the BOQ is quotable
    rather than all-zero — the engineer trims/edits against the tender.
    """
    det_ids = {d["scenario_id"] for d in detected}
    if "TR_CBM_001" not in det_ids:
        return []

    n_tx = int(asset_counts.get("Transformer", 0))
    assumed = n_tx <= 0
    if assumed:
        n_tx = 1

    base_note = (
        "Per-transformer-bank CBM template (from CBM KB real cases, 6U config); "
        "qty = per-bank x N transformers. Confirm against the tender's "
        "monitored-parameter list (temperature/analog/RTD/CT/tap/DGA/bushing/PD)."
    )
    tx_note = ("Transformer count assumed = 1 (not stated in documents) - CONFIRM. "
               if assumed else "")

    # Keep the QTMS base (PF_TR_CBM) header line consistent with the per-bank
    # expansion: if its quantity is still TBD/0, apply the same (possibly
    # assumed) transformer count so header and modules agree.
    for b in boq:
        if (b.scenario_id == "TR_CBM_001"
                and b.product_id in ("CBM_QTMS_BASE_6U", "CBM_QTMS_BASE_3U")
                and (b.quantity or 0) == 0):
            b.quantity = float(n_tx)
            b.quantity_basis = (f"Transformer bank count = {n_tx}"
                                + (" (assumed - confirm)" if assumed else ""))
            if tx_note and "assumed" not in (b.assumption or "").lower():
                b.assumption = (tx_note + (b.assumption or "")).strip()

    # Choose the DGA model from the extracted gas count (default multi-gas TM8).
    dga_pid, dga_reason = _dga_model_for_count(requirements or [])

    out: list[BOQLine] = []
    line_no = max((b.boq_line for b in boq), default=0)
    for pid, per_bank, group in _CBM_PER_BANK_TEMPLATE:
        # Substitute the placeholder DGA model with the gas-count-driven choice.
        if pid == "PROD_PF_DGA_03":
            pid = dga_pid
            group = f"DGA monitor ({dga_reason})"
        prod = dp.products.get(pid)
        if not prod:
            continue
        line_no += 1
        out.append(BOQLine(
            boq_line=line_no, project_id=project_id,
            product_id=prod.product_id, product_model=prod.model,
            product_description=prod.description or prod.family_name,
            scenario_id="TR_CBM_001",
            related_assets=f"Transformer={n_tx}",
            quantity=float(per_bank * n_tx),
            unit=_unit_for(prod.family_id, prod.family_name, prod.model),
            quantity_basis=f"{per_bank} per transformer bank x {n_tx} transformer(s) [{group}]",
            assumption=tx_note + base_note,
            confidence=0.6, review_status="Needs Review",
            notes="Added by CBM per-bank config expansion (QR_TR_CBM_*).",
        ))
    return out


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
                          dp: DataPackage, project_id: str,
                          asset_counts: dict[str, float] | None = None) -> list[MissingInfoQuestion]:
    asset_counts = asset_counts or {}
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
        scenario = dp.scenarios.get(gap["scenario_id"])
        # CBM / transformer projects have no SLD at BOQ: ask for the count from
        # the tender parameter list rather than a drawing.
        no_drawing = bool(scenario) and "none at boq" in (scenario.drawing_dependency or "").lower()
        source_hint = ("from the tender / technical-spec parameter list"
                       if no_drawing else "e.g. from SLD / equipment list")
        add(MissingInfoQuestion(
            scenario_id=gap["scenario_id"],
            missing_item=f"Quantity basis: {gap['count_field']}",
            why_it_matters=f"Needed to size BOQ quantity for {gap['family_name']}.",
            question=(f"Please provide the {gap['count_field'].replace('_', ' ')} "
                      f"({source_hint}) for {gap['family_name']}."),
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
        # Skip quantity-basis asks whose count is already resolved in asset_counts
        # (from a drawing OR document-seeded text count). Avoids re-asking for a
        # transformer/asset count that Step 2 has already derived.
        if req["requirement_type"] == "Quantity Basis":
            rule = dp.quantity_rule_for_scenario(sid)
            atypes = (constants.COUNT_FIELD_TO_ASSET_TYPE.get(rule.count_field, [])
                      if rule else [])
            if any(a in asset_counts for a in atypes):
                continue
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


# Generic tokens that must not, on their own, match a requirement to a template
# (e.g. "count" would wrongly link the transformer "Asset Count" requirement to
# the "DGA gas count required" template).
_GENERIC_TEMPLATE_TOKENS = {
    "count", "number", "level", "type", "value", "rating", "class", "list",
    "point", "points",
}


def _best_template(templates: list[MissingInfoQuestion], metric_name: str):
    metric_low = metric_name.lower()
    tokens = [t for t in metric_low.split() if len(t) > 3]
    for tpl in templates:
        item = tpl.missing_item.lower()
        # Strong match: the full metric name appears in the template item.
        if metric_low and metric_low in item:
            return tpl
        # Otherwise require a distinctive (non-generic) shared token.
        if any(tok in item for tok in tokens
               if tok not in _GENERIC_TEMPLATE_TOKENS):
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
                              dp: DataPackage, client, project_summary: dict,
                              extra_instructions: str = "") -> bool:
    compact = []
    for m in matches:
        sid = _scenario_for_family(m.family_id, detected, dp)
        capability_known = bool(m.candidate_model) and "_TBD" not in m.candidate_product_id
        compact.append({
            "family_id": m.family_id, "family_name": m.family_name,
            "scenario_id": sid, "capability_known": capability_known,
            "match_score": m.match_score,
        })
    explanations = llm_extract.explain_matches(
        client, project_summary, compact, extra_instructions
    )
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
    directives = step1.get("context_directives", []) or []
    asset_counts = _asset_counts(drawing_assets)

    # Human prompt / project context (from File Ingestion or Spec Review). Also
    # injected into the Step 2 LLM augmentation so the reviewer's guidance shapes
    # the BOQ recommendations & clarification questions (logic otherwise unchanged).
    _user_context = (step1.get("user_context") or "").strip()
    step2_extra = (
        "The user provided the following project context / instructions — weigh "
        "it heavily:\n" + _user_context
    ) if _user_context else ""

    # User-provided quantity hints (e.g. "6 feeders", "273 gas zones") override
    # the drawing-derived counts before sizing.
    applied_hints = _apply_quantity_hints(asset_counts, directives, dp)

    # Seed counts from document-extracted "Quantity Basis" requirements so
    # CBM / no-drawing projects can be sized from the tender text (drawing counts
    # still win). This is what lets an extracted "3 transformers" reach the BOQ.
    seeded_counts = _seed_counts_from_requirements(asset_counts, requirements, dp)

    matches = match_products(detected, requirements, dp, project_id)
    compat_flags = apply_compatibility(detected, requirements, asset_counts, dp)

    # --- LLM augmentation: enrich match recommendations / risks before BOQ ---
    client = llm.get_client()
    llm_used = False
    project_summary = _build_project_summary(detected, requirements, asset_counts)
    if client.available and matches:
        llm_used = _apply_match_explanations(
            matches, detected, dp, client, project_summary, step2_extra
        ) or llm_used

    # When the integrated CBM scenario is present, fold the transformer
    # sub-families into the QTMS system (emitted as a per-bank template below)
    # so they are not also quoted as standalone lines.
    detected_ids = {d["scenario_id"] for d in detected}
    fold_families = set(_CBM_FOLDED_FAMILIES) if "TR_CBM_001" in detected_ids else set()
    # Recorder / FMS substation scope: the master-station software (iQ+) and any
    # gateway are supplied via the MEA packaging expansion below, so drop the
    # generic monitoring-software / gateway family lines to avoid the duplicate
    # "not required" entries the users flagged (DAY-2 feedback 775368 / 776060).
    if detected_ids & constants.RECORDER_SCENARIO_IDS:
        fold_families |= {"PF_SOFTWARE", "PF_GATEWAY"}

    boq, boq_summary, quantity_gaps = generate_boq(
        detected, matches, asset_counts, compat_flags, dp, project_id, fold_families
    )
    # CBM per-transformer-bank config expansion: turn the integrated QTMS system
    # into its module / sensor / DGA / bushing / PD / power-meter BOM.
    cbm_lines = expand_cbm_config(boq, detected, asset_counts, dp, project_id,
                                  requirements)
    if cbm_lines:
        boq.extend(cbm_lines)
        boq_summary["total_lines"] = len(boq)
        boq_summary["lines_needing_review"] = sum(
            1 for b in boq if b.review_status == "Needs Review"
        )
        boq_summary["cbm_expansion_lines"] = len(cbm_lines)
    if seeded_counts:
        boq_summary["counts_seeded_from_documents"] = seeded_counts

    # TAQA MEA ruleset expansion: add accessories / panels / software / services
    # (GPS, switches, EPG licences, test switches, LEV/PDC cabinet BoM, services)
    # sized from the recorder scope so the BOQ reflects the engineered package.
    mea_lines = expand_mea_config(boq, detected, dp, project_id)
    if mea_lines:
        boq.extend(mea_lines)
        boq_summary["total_lines"] = len(boq)
        boq_summary["lines_needing_review"] = sum(
            1 for b in boq if b.review_status == "Needs Review"
        )
        boq_summary["mea_expansion_lines"] = len(mea_lines)

    # Apply the user's scope exclusions to the fully assembled BOQ. Uses the
    # structured directives interpreted in Step 1 (exclude by category / family /
    # scenario); falls back to a deterministic keyword parse of the raw context
    # when the LLM was unavailable. These service / training / spares lines are
    # added deterministically above (not by the Step 1 prompt), so this is the
    # only place they can be removed.
    scope_dropped, scope_summary = _apply_context_exclusions(
        boq, directives, dp, step1.get("user_context", "")
    )
    if applied_hints:
        boq_summary["quantity_hints_applied"] = applied_hints
    if scope_dropped:
        boq_summary["total_lines"] = len(boq)
        boq_summary["lines_needing_review"] = sum(
            1 for b in boq if b.review_status == "Needs Review"
        )
        boq_summary["scope_excluded"] = scope_summary
        boq_summary["scope_excluded_detail"] = scope_dropped

    missing = generate_missing_info(
        detected, requirements, compat_flags, quantity_gaps, dp, project_id,
        asset_counts
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
        extra = llm_extract.suggest_missing_info(
            client, project_summary, existing_q, step2_extra
        )
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
    has_scope = len(boq) > 0

    # Never return a silent empty result: when no scope was detected, surface an
    # explicit High-priority clarification so the UI guides the user on what to
    # provide (esp. when only an unreadable / scanned drawing was submitted).
    if not has_scope:
        docs = step1.get("documents", [])
        drawing_docs = [d for d in docs if d.get("doc_type") == "Drawing / SLD"]
        only_drawings = bool(drawing_docs) and len(drawing_docs) == len(docs)
        if only_drawings:
            gap_item = "Readable specification / equipment list (or a text-based SLD)"
            why = ("Only drawing(s)/SLD were provided and no Qualitrol monitoring scope "
                   "could be read from them — the drawing may be a scan with no text layer, "
                   "or its labels were not legible to the drawing reader.")
            question = ("Please provide the project Scope of Work / specification / equipment "
                        "list, or a higher-resolution / text-based single-line diagram, so "
                        "requirements and a BOQ can be generated.")
        else:
            gap_item = "Project specification / SLD / equipment list"
            why = ("No Qualitrol-relevant monitoring scope was detected in the submitted "
                   "documents.")
            question = ("Please provide a project specification, single-line diagram, or "
                        "equipment list describing the monitoring scope.")
        missing.insert(0, MissingInfoQuestion(
            project_id=project_id, scenario_id="", missing_item=gap_item,
            why_it_matters=why, question=question, priority="High",
            owner="Sales / Customer", status="Open",
            notes="Auto-generated: no Qualitrol scope detected in the submission.",
        ))

    high_open = [q for q in missing if q.priority == "High"]
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
