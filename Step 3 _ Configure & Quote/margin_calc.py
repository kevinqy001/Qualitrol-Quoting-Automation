"""Step 3 — Margin calculator.

Implements the pricing / margin logic distilled from the customer price list
(``Gemba Samples/2/Pricing/2026-05-12 IP 2026 Price List.xlsx``). The per-system
sheets price each line as:

    Ext List (G)      = Unit List Price (D) x Quantity (A)
    Ext Material (M)  = Unit Material Cost (J) x Quantity (A)
    Material Margin % = 1 - Material / List

and the ``Overall`` / ``Pricing Overview`` sheets roll those up:

    Quoted / Target price = List x (1 - Discount)
    Discount %            = (List - Target) / List
    Margin (GM%)          = 1 - Material(COGS) / Selling price
    COGS                  = Material + Freight + Labour + Overheads + Field Service

This module is pure (no I/O): ``compute_margins(payload)`` takes the calculator
inputs and returns per-line results, a per-family breakdown, and the overall
summary. The web layer adds persistence and Excel export on top.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a possibly-stringy/None value to float, never raising."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value: Any, default: float = 0.0) -> float:
    """Coerce a percentage input (0..100). Clamps to a sane range."""
    v = _num(value, default)
    if v < -1000:
        v = -1000.0
    if v > 100:
        v = 100.0
    return v


def _round(value: float, ndigits: int = 2) -> float:
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return 0.0


def compute_line(line: dict, default_discount_pct: float) -> dict:
    """Compute a single BOQ/margin line.

    Input keys: description, family, qty, unitListPrice, unitCost, discountPct
    (discountPct is optional; falls back to the global default).
    """
    qty = _num(line.get("qty"), 0.0)
    unit_list = _num(line.get("unitListPrice"), 0.0)
    unit_cost = _num(line.get("unitCost"), 0.0)
    disc = line.get("discountPct")
    disc_pct = _pct(disc, default_discount_pct) if disc not in (None, "") else default_discount_pct

    ext_list = qty * unit_list
    net_unit = unit_list * (1 - disc_pct / 100.0)
    ext_net = qty * net_unit               # quoted / target price for the line
    ext_cost = qty * unit_cost             # material cost for the line

    # Margin at the (discounted) selling price, and at full list price.
    quoted_margin = (1 - ext_cost / ext_net) if ext_net > 0 else 0.0
    list_margin = (1 - ext_cost / ext_list) if ext_list > 0 else 0.0

    return {
        "description": str(line.get("description", "") or ""),
        "family": str(line.get("family", "") or ""),
        "productCode": str(line.get("productCode", "") or ""),
        "qty": _round(qty, 4),
        "unitListPrice": _round(unit_list),
        "unitCost": _round(unit_cost),
        "discountPct": _round(disc_pct, 2),
        "extListPrice": _round(ext_list),
        "netUnitPrice": _round(net_unit),
        "extNetPrice": _round(ext_net),
        "extCost": _round(ext_cost),
        "listMarginPct": _round(list_margin * 100, 1),
        "marginPct": _round(quoted_margin * 100, 1),
    }


def compute_margins(payload: dict) -> dict:
    """Compute per-line, per-family and overall margin results.

    Payload:
        currency: str (display only)
        globals: {discountPct, freight, labour, overheads, fieldService}
        lines:   list of line dicts (see ``compute_line``)
    """
    globals_in = payload.get("globals") or {}
    default_discount = _pct(globals_in.get("discountPct"), 0.0)

    freight = _num(globals_in.get("freight"), 0.0)
    labour = _num(globals_in.get("labour"), 0.0)
    overheads = _num(globals_in.get("overheads"), 0.0)
    field_service = _num(globals_in.get("fieldService"), 0.0)
    extra_cost = freight + labour + overheads + field_service

    lines = [compute_line(ln, default_discount) for ln in (payload.get("lines") or [])]

    total_list = sum(ln["extListPrice"] for ln in lines)
    total_net = sum(ln["extNetPrice"] for ln in lines)
    total_material = sum(ln["extCost"] for ln in lines)
    cogs = total_material + extra_cost

    overall_discount = ((total_list - total_net) / total_list * 100) if total_list > 0 else 0.0
    list_margin = (1 - cogs / total_list) if total_list > 0 else 0.0
    quoted_margin = (1 - cogs / total_net) if total_net > 0 else 0.0
    material_only_margin = (1 - total_material / total_net) if total_net > 0 else 0.0

    # Per-family rollup (mirrors the per-system sheets / Overall breakdown).
    families: dict[str, dict] = {}
    fam_order: list[str] = []
    for ln in lines:
        key = ln["family"] or "Unassigned"
        if key not in families:
            families[key] = {"family": key, "lines": 0, "extListPrice": 0.0,
                             "extNetPrice": 0.0, "extCost": 0.0}
            fam_order.append(key)
        f = families[key]
        f["lines"] += 1
        f["extListPrice"] += ln["extListPrice"]
        f["extNetPrice"] += ln["extNetPrice"]
        f["extCost"] += ln["extCost"]

    family_rows = []
    for key in fam_order:
        f = families[key]
        net = f["extNetPrice"]
        fam_margin = (1 - f["extCost"] / net) * 100 if net > 0 else 0.0
        fam_disc = ((f["extListPrice"] - net) / f["extListPrice"] * 100) if f["extListPrice"] > 0 else 0.0
        family_rows.append({
            "family": key,
            "lines": f["lines"],
            "extListPrice": _round(f["extListPrice"]),
            "extNetPrice": _round(net),
            "extCost": _round(f["extCost"]),
            "discountPct": _round(fam_disc, 1),
            "marginPct": _round(fam_margin, 1),
        })

    summary = {
        "currency": str(payload.get("currency", "USD") or "USD"),
        "totalListPrice": _round(total_list),
        "totalNetPrice": _round(total_net),
        "totalMaterialCost": _round(total_material),
        "freight": _round(freight),
        "labour": _round(labour),
        "overheads": _round(overheads),
        "fieldService": _round(field_service),
        "cogs": _round(cogs),
        "overallDiscountPct": _round(overall_discount, 1),
        "listMarginPct": _round(list_margin * 100, 1),
        "quotedMarginPct": _round(quoted_margin * 100, 1),
        "materialMarginPct": _round(material_only_margin * 100, 1),
        "lineCount": len(lines),
    }

    return {"currency": summary["currency"], "lines": lines,
            "families": family_rows, "summary": summary}
