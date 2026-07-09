"""Append-only KB augmentation from 2026-07 Gemba real-case scan.

Adds net-new products, synonyms, one new scenario (OLTC/tap-changer monitoring)
+ its family/quantity rule, a few metrics, quantity rules and one advisory
compatibility rule. Every new row is flagged for review in its Notes column.
Existing rows are never modified. A timestamped backup is written first.
"""
import os
import shutil
import datetime
import openpyxl

XLSX = "Qualitrol_BOQ_Matching_Data_Package.xlsx"
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
REVIEW = f"Added {STAMP[:8]} from Gemba real-case scan (Random samples from Y). REVIEW before use."

FIRSTCOL = {
    "03_Scenario_Master": "Scenario ID",
    "04_Metric_Dictionary": "Metric ID",
    "05_Synonym_Mapping": "Raw Term / Phrase",
    "06_Product_Family_Master": "Product Family ID",
    "07_Product_Master_Template": "Product ID",
    "09_Quantity_Rules": "Quantity Rule ID",
    "10_Compatibility_Rules": "Rule ID",
}


def header_of(ws, first_col):
    for row in ws.iter_rows(values_only=True):
        if row and row[0] is not None and str(row[0]).strip() == first_col:
            hdr = [("" if c is None else str(c)) for c in row]
            while hdr and hdr[-1] == "":
                hdr.pop()
            return hdr
    raise ValueError(f"header {first_col} not found in {ws.title}")


def row_from(hdr, d):
    """Build a row list positioned to the sheet header from a {header: value} dict."""
    unknown = set(d) - set(hdr)
    if unknown:
        raise KeyError(f"unknown headers {unknown} for {hdr[0]}")
    return [d.get(h, "") for h in hdr]


# --------------------------------------------------------------------------- #
# Data to add (keyed by header name; missing columns -> blank)
# --------------------------------------------------------------------------- #
SCENARIOS = [
    {
        "Scenario ID": "TAPCHG_001",
        "Scenario Category": "Transformer",
        "Application Scenario": "On-load tap changer (OLTC) monitoring",
        "Asset Type": "On-load tap changer (OLTC)",
        "Typical Metrics / Requirements": "Tap position; drive motor current; tap operation count; switching/operating time; contact wear; OLTC oil temperature",
        "Common Evidence Keywords / Synonyms": "tap changer; OLTC; on-load tap changer; tap position; drive motor current; contact wear; tap operation count; LTC monitoring",
        "Related Product Families": "PF_OLTC",
        "Quantity Basis": "1 monitoring set per OLTC / tap changer (typically 1 per transformer with OLTC)",
        "Drawing Dependency": "Optional",
        "Requirement Output Fields": "",
        "Review Notes": REVIEW + " Sourced from QTMS LTC module (TSEA 796922 QTMS Pricing).",
    },
]

METRICS = [
    {"Metric ID": "MET_TAP_POSITION", "Standard Metric Name": "Tap Position",
     "Synonyms / Raw Terms": "tap position; tap step; OLTC position", "Standard Unit": "count",
     "Data Type": "integer", "Applies To": "OLTC", "Used For": "OLTC monitoring",
     "Example Values": "1..21", "Required for Matching": "No", "Normalization Notes": REVIEW},
    {"Metric ID": "MET_DRIVE_MOTOR_CURRENT", "Standard Metric Name": "OLTC Drive Motor Current",
     "Synonyms / Raw Terms": "drive motor current; motor current; OLTC motor current", "Standard Unit": "A",
     "Data Type": "number", "Applies To": "OLTC", "Used For": "OLTC mechanism diagnostics",
     "Example Values": "", "Required for Matching": "No", "Normalization Notes": REVIEW},
    {"Metric ID": "MET_OIL_LEVEL", "Standard Metric Name": "Oil Level",
     "Synonyms / Raw Terms": "oil level; conservator level; LLG; liquid level gauge; level gauge", "Standard Unit": "text",
     "Data Type": "text", "Applies To": "Transformer", "Used For": "Transformer condition monitoring",
     "Example Values": "main tank; LTC tank", "Required for Matching": "No", "Normalization Notes": REVIEW},
    {"Metric ID": "MET_TANK_PRESSURE", "Standard Metric Name": "Tank Pressure",
     "Synonyms / Raw Terms": "tank pressure; pressure transducer", "Standard Unit": "bar",
     "Data Type": "number", "Applies To": "Transformer", "Used For": "Transformer condition monitoring",
     "Example Values": "", "Required for Matching": "No", "Normalization Notes": REVIEW},
    {"Metric ID": "MET_FO_WINDING_TEMP", "Standard Metric Name": "Fibre-Optic Winding Temperature",
     "Synonyms / Raw Terms": "fiber optic winding temp; direct winding temperature; FO winding sensor", "Standard Unit": "degC",
     "Data Type": "number", "Applies To": "Transformer", "Used For": "Direct winding temperature monitoring",
     "Example Values": "", "Required for Matching": "No", "Normalization Notes": REVIEW},
]

FAMILIES = [
    {"Product Family ID": "PF_OLTC", "Product Family": "OLTC / Tap Changer Monitor",
     "Applicable Scenario IDs": "TAPCHG_001", "Primary Asset Type": "On-load tap changer",
     "Typical Capabilities": "Tap position, drive-motor current, operating/switching time, contact wear, OLTC oil temperature; often delivered as a QTMS LTC/AI module.",
     "Default Quantity Rule ID": "QR_OLTC_001",
     "Dependencies / Required Inputs": "OLTC count; tap-position signal type; drive-motor CT",
     "Commercial / Engineering Notes": REVIEW},
]


def prod(pid, model, fid, fam, scen, asset, desc, proto="", rule="", notes=""):
    return {
        "Product ID": pid, "Product Model": model, "Product Family ID": fid, "Product Family": fam,
        "Applicable Scenario IDs": scen, "Primary Asset Type": asset, "Product Description": desc,
        "Supported Standards": "", "Communication Protocols": proto, "Default Quantity Rule ID": rule,
        "Required Accessories": "", "Optional Accessories": "", "Datasheet URL": "",
        "Source Owner": "Gemba scan 2026-07", "Status": "Review",
        "Notes": (notes + " " if notes else "") + REVIEW,
    }


BRK = ("PF_BREAKER", "Circuit Breaker Monitor", "BRK_HEALTH_001", "Circuit breaker")
SF6 = ("PF_GIS_SF6", "SF6 Gas Density Monitoring (iSGM / GDM)", "GIS_SF6_001", "Gas zone / GIS compartment")
TRT = ("PF_TR_TEMP", "Transformer Temperature Monitor", "TR_TEMP_001", "Transformer")
BSH = ("PF_BUSHING", "Bushing Monitor", "TR_BUSH_001", "Transformer bushing")
AUX = ("PF_AUX_SENSOR", "Transformer Auxiliary Sensors", "TR_AUX_001", "Transformer / cabinet")
OLTC = ("PF_OLTC", "OLTC / Tap Changer Monitor", "TAPCHG_001", "On-load tap changer")

PRODUCTS = [
    # QBCM variants
    prod("GMB_QBCM_LT", "QBCM-LT", *BRK, "Qualitrol Breaker Condition Monitor - LT (base variant).", rule="QR_BRK_001", notes="QBCM LT/ST/IP variants from QBCM Pricing Tool."),
    prod("GMB_QBCM_ST", "QBCM-ST", *BRK, "Qualitrol Breaker Condition Monitor - ST variant (adds heater/motor current).", rule="QR_BRK_001"),
    prod("GMB_QBCM_IP", "QBCM-IP", *BRK, "Qualitrol Breaker Condition Monitor - IP variant (full: phase currents, SF6, travel).", rule="QR_BRK_001"),
    prod("GMB_QBCM_HALL", "Hall-Effect DC Current Sensor (TRA-042-1)", *BRK, "DC Hall-effect sensor 20A 1VDC for trip/close coil current; 3 or 9 per breaker by model.", rule="QR_BRK_SENSOR_001"),
    prod("GMB_QBCM_ACCT", "AC Current Sensor / CT (TRA-041 / TRA-017)", *BRK, "AC CT (150/100/50A) for phase currents; small CTs for heater/motor current.", rule="QR_BRK_SENSOR_001"),
    prod("GMB_QBCM_TRAVEL", "Rotary Encoder Travel Transducer (TRN-111-1)", *BRK, "Rotary encoder 1000PPR for contact travel/timing; 1 or 3 per breaker by model.", rule="QR_BRK_SENSOR_001"),
    prod("GMB_QBCM_TRIPCOIL", "3-Phase Trip Coil Monitor (TRN-113-1)", *BRK, "3-phase trip coil monitor accessory.", rule="QR_BRK_SENSOR_001"),
    prod("GMB_QBCM_TEMP", "Temperature Transducer RS-485 (TRN-114 / TRN-604)", *BRK, "RS-485 temperature transducer (mechanism/ambient).", rule="QR_BRK_SENSOR_001"),
    # SF6 GDM sensors
    prod("GMB_WIKA_GDT20", "WIKA GDT-20", *SF6, "SF6 gas density sensor, RS-485 Modbus, no humidity.", proto="RS-485 Modbus RTU", rule="QR_SF6_SNS"),
    prod("GMB_WIKA_GD10F", "WIKA GD-10F", *SF6, "SF6 gas density sensor, 4-20mA analog output.", proto="4-20mA", rule="QR_SF6_SNS"),
    prod("GMB_TRAFAG_SF6", "TRAFAG 8774 / 8775 / 8782 / 8783 series", *SF6, "SF6 density sensor family (4-20mA / RS-485 Modbus / hybrid switch+analog).", proto="4-20mA; RS-485 Modbus", rule="QR_SF6_SNS"),
    prod("GMB_QUALITROL_420", "Qualitrol-420", *SF6, "Qualitrol-420 SF6 gas density sensor (4-20mA).", proto="4-20mA", rule="QR_SF6_SNS"),
    # QGDM hardware
    prod("GMB_QGDM_DAU", "QGDM-DAU (RS-485)", *SF6, "GDM data acquisition unit, RS-485 (Modbus) to Ethernet.", proto="RS-485 Modbus; Ethernet", rule="QR_GDM_DAU_001"),
    prod("GMB_QGDM_AI", "QGDM-AI Module / Card", *SF6, "GDM analog input card for 4-20mA gas density sensors.", proto="4-20mA; Ethernet", rule="QR_GDM_DAU_001"),
    prod("GMB_QGDM_DC", "QGDM-DC Distribution Cabinet (Floor / Wall)", *SF6, "GDM distribution cabinet housing GDM-DAUs (Large floor <=5 DAU, Small floor <=2, wall mount per spec).", rule="QR_GDM_DC_001"),
    prod("GMB_GDM_ADAPT", "SF6 Sensor Adaptor (DN8 / DN20 / Malmquist)", *SF6, "Mechanical adaptor for SF6 density sensor to GIS gas port (DN8/DN20/Malmquist).", rule="QR_SF6_SNS"),
    # QTMS modules
    prod("GMB_QTMS_BASE", "QTMS Base Module 3U (Chassis + CPU + Display)", *TRT, "Modular transformer monitoring controller; hosts AI/DI/FO/Bushing/Relay/PSU modules.", proto="Modbus; IEC 61850", notes="QTMS modular BoM from TSEA 796922."),
    prod("GMB_QTMS_AI", "QTMS Analog Input Module", *TRT, "8-input analog module; RTD / CT / LTC / AC-voltage / tap-position / potentiometer cards.", notes="QTMS module."),
    prod("GMB_QTMS_DI", "QTMS Digital Input Module", *TRT, "14-input digital status module (cooling status, alarms).", notes="QTMS module."),
    prod("GMB_QTMS_FO", "QTMS Fibre-Optic Module (direct winding temperature)", *TRT, "Fibre-optic module for direct winding temperature sensors.", notes="QTMS module."),
    prod("GMB_QTMS_BM", "QTMS Bushing Module", *BSH, "6-input bushing module (capacitance / tan delta) with tap adaptor.", notes="QTMS module."),
    prod("GMB_QTMS_RO", "QTMS Output Relay Module", *TRT, "8-output relay module for alarm/trip outputs.", notes="QTMS module."),
    prod("GMB_QTMS_PSM", "QTMS Power Supply Module", *TRT, "CE power supply module for QTMS.", notes="QTMS module."),
    # QTMS sensors
    prod("GMB_QTMS_LLG", "LLG Oil Level Gauge with Potentiometer", *AUX, "Liquid level gauge with potentiometer for main-tank / LTC-tank oil level.", rule="QR_TR_AUX_001"),
    prod("GMB_QTMS_PRESS", "Tank Pressure Transducer (TRN-603-1)", *AUX, "Pressure transducer for transformer tank pressure.", rule="QR_TR_AUX_001"),
    prod("GMB_QTMS_FOPROBE", "Fibre-Optic Winding Temperature Probe", *TRT, "Internal fibre-optic probe for direct winding temperature (with OFT feedthrough).", notes="QTMS FO."),
    # OLTC family product
    prod("GMB_QTMS_LTC", "QTMS LTC / OLTC Monitoring (via AI + tap-position input)", *OLTC, "OLTC monitoring using QTMS AI module: drive-motor current, tap position (4-20mA), operating time, contact wear.", rule="QR_OLTC_001", notes="Delivered as QTMS LTC function."),
]

SYNONYMS = [
    ("QBCM-LT", "BRK_HEALTH_001", "", "QBCM LT (base) breaker condition monitor variant", "Circuit breaker", "High"),
    ("QBCM-ST", "BRK_HEALTH_001", "", "QBCM ST breaker condition monitor variant", "Circuit breaker", "High"),
    ("QBCM-IP", "BRK_HEALTH_001", "", "QBCM IP (full) breaker condition monitor variant", "Circuit breaker", "High"),
    ("WIKA GDT-20", "GIS_SF6_001", "", "SF6 gas density sensor, RS-485 Modbus (no humidity)", "Gas zone", "High"),
    ("WIKA GD-10F", "GIS_SF6_001", "", "SF6 gas density sensor, 4-20mA analog", "Gas zone", "High"),
    ("GDT-20", "GIS_SF6_001", "", "WIKA GDT-20 SF6 density sensor", "Gas zone", "Medium"),
    ("TRAFAG", "GIS_SF6_001", "", "TRAFAG SF6 gas density sensor series", "Gas zone", "Medium"),
    ("Qualitrol-420", "GIS_SF6_001", "", "Qualitrol-420 SF6 density sensor", "Gas zone", "Medium"),
    ("QGDM-DAU", "GIS_SF6_001", "", "GDM data acquisition unit (RS-485)", "Gas zone", "High"),
    ("QGDM-AI", "GIS_SF6_001", "", "GDM analog input card (4-20mA)", "Gas zone", "High"),
    ("QGDM-DC", "GIS_SF6_001", "", "GDM distribution cabinet", "Gas zone", "Medium"),
    ("Malmquist adaptor", "GIS_SF6_001", "", "SF6 sensor gas-port adaptor", "Gas zone", "Medium"),
    ("SmartGDM", "GIS_SF6_001", "", "SmartGDM software for iSGM SF6 monitoring", "Gas zone", "High"),
    ("on-load tap changer", "TAPCHG_001", "MET_TAP_POSITION", "On-load tap changer", "On-load tap changer", "High"),
    ("OLTC", "TAPCHG_001", "", "On-load tap changer", "On-load tap changer", "High"),
    ("tap changer monitoring", "TAPCHG_001", "", "OLTC / tap changer monitoring", "On-load tap changer", "High"),
    ("tap position", "TAPCHG_001", "MET_TAP_POSITION", "Tap position monitoring", "On-load tap changer", "High"),
    ("drive motor current", "TAPCHG_001", "MET_DRIVE_MOTOR_CURRENT", "OLTC drive motor current", "On-load tap changer", "Medium"),
    ("LTC monitoring", "TAPCHG_001", "", "Load tap changer monitoring", "On-load tap changer", "High"),
    ("QTMS", "TR_TEMP_001", "", "Qualitrol Transformer Monitoring System (modular)", "Transformer", "High"),
    ("QTMS-AI", "TR_TEMP_001", "", "QTMS analog input module", "Transformer", "Medium"),
    ("QTMS-BM", "TR_BUSH_001", "", "QTMS bushing module", "Transformer bushing", "Medium"),
    ("SmartITM", "SUB_SOFT_001", "", "QTMS SmartITM software module", "Transformer", "Medium"),
    ("SmartDGA", "SUB_SOFT_001", "", "QTMS SmartDGA software module", "Transformer", "Medium"),
    ("SmartBM", "SUB_SOFT_001", "", "QTMS SmartBM bushing software module", "Transformer", "Medium"),
    ("LLG", "TR_AUX_001", "MET_OIL_LEVEL", "Liquid level gauge (oil level)", "Transformer", "Medium"),
    ("fiber optic winding temperature", "TR_TEMP_001", "MET_FO_WINDING_TEMP", "Direct winding temperature (fibre optic)", "Transformer", "High"),
]


def qrule(rid, scen, fid, fam, basis, desc, count_field="", need_drawing="Optional",
          need_asset="Yes", example="", assumption=""):
    return {
        "Quantity Rule ID": rid, "Scenario ID": scen, "Product Family ID": fid, "Product Family": fam,
        "Quantity Basis": basis, "Rule Description": desc, "Need Drawing": need_drawing,
        "Need Asset List": need_asset, "Count Field": count_field, "Example": example,
        "Assumption / Risk": (assumption + " " if assumption else "") + REVIEW,
    }


QRULES = [
    qrule("QR_OLTC_001", "TAPCHG_001", "PF_OLTC", "OLTC / Tap Changer Monitor",
          "OLTC / tap changer count",
          "1 OLTC monitoring set per on-load tap changer (typically 1 per transformer with OLTC).",
          count_field="tap_changer_count", example="2 OLTC = 2 sets"),
    qrule("QR_GDM_DAU_001", "GIS_SF6_001", "PF_GIS_SF6", "SF6 Gas Density Monitoring (iSGM / GDM)",
          "GDM-DAU count from sensor count",
          "1 QGDM-DAU (RS-485) per group of Modbus sensors; QGDM-AI card per group of 4-20mA sensors (confirm sensors-per-DAU with product).",
          count_field="gas_zone_count", example="174 sensors -> ~18 QGDM-DAU"),
    qrule("QR_GDM_DC_001", "GIS_SF6_001", "PF_GIS_SF6", "SF6 Gas Density Monitoring (iSGM / GDM)",
          "Distribution cabinet count from DAU count",
          "QGDM-DC capacity: Large floor mount <=5 GDM-DAU, Small floor <=2, wall mount per spec; +1 media converter per DC on fibre.",
          count_field="", need_drawing="No", need_asset="No", example="18 DAU -> ~2 floor DC"),
    qrule("QR_BRK_SENSOR_001", "BRK_HEALTH_001", "PF_BREAKER", "Circuit Breaker Monitor",
          "Sensor count per breaker",
          "Coil-current (Hall) sensors 3 or 9 per breaker by model; phase-current sensors per breaker; travel transducer 1 or 3 per breaker.",
          count_field="breaker_count", example="1 breaker (IP) -> 9 coil-current + 3 phase + 3 travel"),
    qrule("QR_SVC_PDM_COMM_001", "GIS_PD_001", "PF_COMMON", "Panels, Network & Security, Timing, Licences, Services",
          "PDM commissioning days from OCU count",
          "OCU install 2/day; OCU commissioning 5/day (Gen3); HV test & sensitivity 5 OCU/day; +1 mobilisation per trip.",
          count_field="", need_drawing="No", need_asset="No", example="15 OCU -> ~3 commissioning days + 1 mobilisation"),
    qrule("QR_SVC_DAU_COMM_001", "DFR_DDR_001;PMU_001;PQ_CLASSA_001;FMS_001", "PF_DAU_REC",
          "Multi-function DAU / Recorder (IDM+ / Informa)",
          "Recorder commissioning days from channel count",
          "IDM+/INFORMA/Q-PMU: 9 & 18 channels = 1 day, 27 & 36 channels = 2 days; FL-1/FL-8 = 1 day each; +1 mobilisation per trip.",
          count_field="channel_count", need_drawing="No", example="IDM+ 36ch -> 2 commissioning days"),
    qrule("QR_SVC_QTMS_COMM_001", "TAPCHG_001;TR_TEMP_001", "PF_TR_TEMP", "Transformer Temperature Monitor",
          "QTMS / QPDM service days",
          "QTMS install 3-4 days, commissioning 2 days per base unit; QPDM commissioning 1 day; +1 mobilisation per trip.",
          count_field="", need_drawing="No", need_asset="No", example="1 QTMS -> 3-4 install + 2 commission days"),
]

COMPAT = [
    {"Rule ID": "CR_GMB_001", "Rule Type": "Advisory", "Scenario ID": "GIS_PD_001", "Asset Type": "GIS",
     "Condition / Trigger": "GIS partial-discharge monitoring project in scope",
     "Recommended Action": "Check whether SF6 gas density monitoring (GDM/iSGM) and/or breaker condition monitoring (QBCM) are also required - real GIS cases frequently add these via scope change.",
     "Severity": "Low",
     "Notes": "Advisory only, do NOT auto-add. e.g. Hyosung case cloned to add CB + GDM. " + REVIEW},
]


def append_rows(ws, first_col, dict_rows):
    hdr = header_of(ws, first_col)
    n = 0
    for d in dict_rows:
        ws.append(row_from(hdr, d))
        n += 1
    return n


def append_syn(ws):
    hdr = header_of(ws, "Raw Term / Phrase")
    for term, sid, mid, meaning, ctx, prio in SYNONYMS:
        d = {"Raw Term / Phrase": term, "Mapped Scenario ID": sid, "Mapped Metric ID": mid,
             "Mapped Standard Meaning": meaning, "Asset Context": ctx, "Mapping Priority": prio,
             "Notes": REVIEW}
        ws.append(row_from(hdr, d))
    return len(SYNONYMS)


def main():
    os.makedirs("backups", exist_ok=True)
    backup = os.path.join("backups", f"Qualitrol_BOQ_Matching_Data_Package.gemba_backup_{STAMP}.xlsx")
    shutil.copy2(XLSX, backup)
    print("backup ->", backup)

    wb = openpyxl.load_workbook(XLSX)  # keep formulas
    counts = {}
    counts["03 scenarios"] = append_rows(wb["03_Scenario_Master"], "Scenario ID", SCENARIOS)
    counts["04 metrics"] = append_rows(wb["04_Metric_Dictionary"], "Metric ID", METRICS)
    counts["05 synonyms"] = append_syn(wb["05_Synonym_Mapping"])
    counts["06 families"] = append_rows(wb["06_Product_Family_Master"], "Product Family ID", FAMILIES)
    counts["07 products"] = append_rows(wb["07_Product_Master_Template"], "Product ID", PRODUCTS)
    counts["09 quantity_rules"] = append_rows(wb["09_Quantity_Rules"], "Quantity Rule ID", QRULES)
    counts["10 compatibility"] = append_rows(wb["10_Compatibility_Rules"], "Rule ID", COMPAT)
    wb.save(XLSX)
    print("appended:", counts)


if __name__ == "__main__":
    main()
