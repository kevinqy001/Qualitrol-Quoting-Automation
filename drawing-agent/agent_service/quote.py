"""Qualitrol BOQ roll-up from accepted annotations.

Seeded from the 776060 BOQ configuration rules: one FMS device per monitored
bay; transformer incomers + bus sections/couplers -> PQM; feeders + futures ->
PMU; one iSGM set per transformer; 5 devices per panel. Illustrative, not the
production rules engine — the point is that every quantity traces to accepted,
on-drawing detections.
"""
from __future__ import annotations

import math


def compute(session: dict) -> dict:
    acc = [a for a in session["annotations"] if a.get("status") == "accepted"]
    bays = [a for a in acc if a["type"] == "gis_bay"]
    txf = [a for a in acc if a["type"] == "power_transformer"]

    def fn_count(fns):
        return sum(1 for b in bays if b.get("props", {}).get("function") in fns)

    fms = sum(1 for b in bays if b.get("props", {}).get("function") != "cap_bank")
    pqm = fn_count({"trans_incomer", "bus_coupler", "bus_section"})
    pmu = fn_count({"cable_feeder", "future"})
    n_txf = len(txf)

    def ceil5(x):
        return math.ceil(x / 5) if x else 0

    products = [
        ("FMS device — IDM+ 9A/32D", fms, "ea",
         f"1 per monitored bay · {fms} bays accepted (excl. cap-bank)"),
        ("FMS panel", ceil5(fms), "ea", f"ceil({fms}/5) — 5 devices/panel"),
        ("PQM device — Informa 9A/32D", pqm, "ea",
         f"Transformer incomers + bus sections · {pqm} accepted"),
        ("PQM panel", ceil5(pqm), "ea", f"ceil({pqm}/5)"),
        ("PMU / WAMS device — IDM+ 9A/32D", pmu, "ea",
         f"Cable feeders + future bays · {pmu} accepted"),
        ("WAMS panel", ceil5(pmu), "ea", f"ceil({pmu}/5)"),
        ("iSGM transformer sensor set", n_txf, "ea",
         f"1 per power transformer · {n_txf} accepted"),
        ("GPS antenna + amplifier kit", (1 if ceil5(fms) else 0) + (1 if ceil5(pmu) else 0),
         "ea", "1 per time-master panel group"),
        ("iQ+ DFR / recorder + LEV PC", 1 if fms else 0, "ea",
         "1 station recorder + LEV cubicle"),
    ]
    return {
        "counts": {"bays": len(bays), "transformers": n_txf,
                   "fms": fms, "pqm": pqm, "pmu": pmu},
        "products": [
            {"name": n, "qty": q, "unit": u, "basis": b}
            for (n, q, u, b) in products if q > 0
        ],
        "contrast": [
            {"line": "PMU (IDM+)", "text_only_ai": 60, "drawing": pmu},
            {"line": "Power-quality device", "text_only_ai": 60, "drawing": pqm or fms},
            {"line": "iQ+ recorder", "text_only_ai": 20, "drawing": 1 if fms else 0},
        ],
    }
