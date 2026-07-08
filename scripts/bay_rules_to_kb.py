"""Append the bay-type -> monitoring-function mapping into the data package.

Source: the engineer's 'AE Notes' bay schedule in
'766481 LOT1 & 2 BAHIA SASN.xlsx' (consistent with the TAQA MEA ruleset), which
states which Qualitrol functions (FMS / PQM / PMU) each bay TYPE receives, plus
the critical scope caveat that the actual monitored set can be a SOW-limited
subset of the substation.

APPEND-ONLY: adds new Compatibility Rules; never edits/deletes existing rows;
dedups by Rule ID; backs up the workbook first.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import openpyxl

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "Qualitrol_BOQ_Matching_Data_Package.xlsx"
SRC = "Engineer AE bay schedule (766481 BAHIA SASN) + TAQA MEA ruleset; candidate — review before use."
CR_SHEET = "10_Compatibility_Rules"

# Rule ID, Rule Type, Scenario ID, Asset Type, Condition / Trigger,
# Recommended Action, Severity, Notes
RULES = [
    ("CR_BAY_OHL", "Selection", "FMS_001;PMU_001", "OHL bay",
     "Bay type = OHL (overhead line) feeder.",
     "Configure FMS + PMU on OHL bays. Do NOT add PQM by default.",
     "Medium", SRC),
    ("CR_BAY_TX", "Selection", "FMS_001;PQ_CLASSA_001", "Interbus / Power Transformer bay",
     "Bay type = interbus transformer (IBT) or power transformer bay.",
     "Configure FMS + PQM on transformer bays.",
     "Medium", SRC),
    ("CR_BAY_BCBS", "Selection", "FMS_001;PQ_CLASSA_001", "Bus Coupler / Bus Section",
     "Bay type = bus coupler (BC) or bus section (BS).",
     "Configure FMS + PQM on BC/BS bays; do NOT place PMU on bus coupler / bus section.",
     "Medium", SRC),
    ("CR_BAY_STATCOM", "Selection", "FMS_001;PQ_CLASSA_001;PMU_001", "STATCOM bay",
     "Bay type = STATCOM.",
     "Configure FMS + PQM + PMU on STATCOM bays.",
     "Medium", SRC),
    ("CR_BAY_CAP", "Selection", "FMS_001;PQ_CLASSA_001;PMU_001", "Capacitor Bank bay",
     "Bay type = capacitor bank.",
     "Configure FMS + PQM + PMU on capacitor bank bays.",
     "Medium", SRC),
    ("CR_BAY_IC", "Selection", "FMS_001;PMU_001", "Interconnector / Incomer bay",
     "Bay type = interconnector (IC) or transformer incomer.",
     "Configure FMS + PMU on IC / incomer bays (PMU per TAQA ruleset).",
     "Medium", SRC),
    ("CR_BAY_FUTURE", "Exclusion", "All", "Bay marked Future / Provision",
     "Bay is marked 'Future' or 'Provision' in the bay schedule / SLD.",
     "Exclude from BOQ quantity; list separately for engineer confirmation.",
     "High", SRC),
    ("CR_BAY_SCOPE", "Review", "All", "Monitored bay set",
     "The actual monitored set may be a SOW-limited subset (only specific "
     "feeders, e.g. capacitor bank / cable feeders), NOT the whole substation.",
     "Confirm the in-scope feeders/bays against the SOW before finalizing "
     "quantities; never assume every bay in the SLD is monitored.",
     "High", SRC),
]


def _header_row(rows, first_col):
    for i, r in enumerate(rows):
        if r and r[0] is not None and str(r[0]).strip().lower() == first_col.lower():
            return i
    raise RuntimeError(f"header {first_col!r} not found")


def main():
    wb = openpyxl.load_workbook(PKG)
    cr = wb[CR_SHEET]
    rows = list(cr.iter_rows(values_only=True))
    h = _header_row(rows, "Rule ID")
    existing = {str(r[0]).strip() for r in rows[h + 1:] if r and r[0]}
    added = skipped = 0
    for row in RULES:
        if row[0] in existing:
            skipped += 1
            continue
        cr.append(list(row))
        added += 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = PKG.with_name(f"Qualitrol_BOQ_Matching_Data_Package.backup_{stamp}.xlsx")
    shutil.copyfile(PKG, backup)
    wb.save(PKG)
    print("Backup:", backup.name)
    print(f"ADDED rules: {added} | SKIPPED: {skipped}")


if __name__ == "__main__":
    main()
