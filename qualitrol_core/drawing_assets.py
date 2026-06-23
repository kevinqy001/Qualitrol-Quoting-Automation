"""Convert circuit drawings / SLDs into a structured asset list.

Implements the data-package rule: *"Do not calculate BOQ quantity directly from
images. First convert drawings into this structured asset list"* (sheet
14_Drawing_Asset_List). This is a conservative, regex-based extractor over the
text layer of an SLD/GSLD PDF plus any structured "sensor quantity" tables. Its
outputs always carry a confidence and a note flagging that GIS/SLD layouts must
be human-verified (Compatibility Rule CR_004).
"""

from __future__ import annotations

import re

from .document_parser import ParsedDocument
from .schemas import DrawingAsset

_VOLTAGE_RE = re.compile(r"(\d{2,4})\s*kV", re.IGNORECASE)
_GIS_BAY_RE = re.compile(r"=C\d{2}\b")
_PD_SENSOR_RE = re.compile(r"-PD\d{1,2}\.\d{1,2}\b")
_CB_MECH_RE = re.compile(r"\bSP3-1\b")
_TOTAL_QTY_RE = re.compile(r"(\d{2,5})")


def _dominant_voltage(text: str) -> str:
    counts: dict[str, int] = {}
    for m in _VOLTAGE_RE.finditer(text):
        kv = f"{int(m.group(1))} kV"
        counts[kv] = counts.get(kv, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _extract_from_sld_pdf(doc: ParsedDocument, project_id: str) -> list[DrawingAsset]:
    text = doc.full_text
    text_lower = text.lower()
    assets: list[DrawingAsset] = []

    voltage = _dominant_voltage(text)
    is_gis = "gis" in text_lower or "gas insulated" in text_lower

    if is_gis:
        bays = sorted(set(_GIS_BAY_RE.findall(text)))
        # =C00/=C01/=C02 are typically bus-section / general columns, not feeder bays.
        feeder_bays = [b for b in bays if b not in {"=C00", "=C01", "=C02"}]
        pd_sensors = sorted(set(_PD_SENSOR_RE.findall(text)))

        assets.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=doc.file_name,
                asset_tag="; ".join(feeder_bays) if feeder_bays else "GIS",
                asset_type="GIS Bay",
                voltage_level=voltage,
                rating="",
                quantity=float(len(feeder_bays)) if feeder_bays else 0.0,
                connected_to="",
                monitoring_zone="GIS lineup",
                source_location=f"{doc.file_name} (SLD)",
                confidence=0.45,
                notes=(
                    "Bay count derived from =Cxx labels on the SLD text layer; "
                    "verify against the GIS layout drawing (CR_004). "
                    "Excludes =C00/=C01/=C02 (bus/general columns)."
                ),
            )
        )
        if pd_sensors:
            assets.append(
                DrawingAsset(
                    project_id=project_id,
                    drawing_id=doc.file_name,
                    asset_tag=f"{len(pd_sensors)} monitored PD sensors",
                    asset_type="PD Sensor",
                    voltage_level=voltage,
                    rating="",
                    quantity=float(len(pd_sensors)),
                    connected_to="GIS",
                    monitoring_zone="Partial discharge",
                    source_location=f"{doc.file_name} (SLD, -PDxx.yy tags)",
                    confidence=0.4,
                    notes=(
                        "Counted distinct -PDxx.yy monitoring-sensor tags. "
                        "Sensitivity-check-only -PD tags are not counted. "
                        "Confirm monitored vs spare sensors with engineering."
                    ),
                )
            )
    elif voltage:
        assets.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=doc.file_name,
                asset_type="Bus / Feeder",
                voltage_level=voltage,
                source_location=doc.file_name,
                confidence=0.3,
                notes="Voltage level detected; asset breakdown needs human review.",
            )
        )
    return assets


def _extract_from_quantity_table(
    doc: ParsedDocument, project_id: str
) -> list[DrawingAsset]:
    """Pick up explicit 'Summary of Sensor Quantity' style tables in docx/text.

    The header (with 'Sensor Quantity') and the data rows live in separate
    segments, so we gate on the whole document and then read the data rows.
    """
    if "sensor quantity" not in doc.full_text.lower() and (
        "sensor qty" not in doc.full_text.lower()
    ):
        return []

    assets: list[DrawingAsset] = []
    for seg in doc.segments:
        low = seg.text.lower()
        if "gis" not in low:
            continue
        numbers = [int(n) for n in re.findall(r"\b\d{2,6}\b", seg.text)]
        if not numbers:
            continue
        voltage = _dominant_voltage(seg.text)
        total = max(numbers)  # the total sensor quantity is the largest figure
        assets.append(
            DrawingAsset(
                project_id=project_id,
                drawing_id=doc.file_name,
                asset_tag="GIS sensor quantity (customer-stated)",
                asset_type="PD Sensor",
                voltage_level=voltage,
                quantity=float(total),
                monitoring_zone="Partial discharge",
                source_location=f"{doc.file_name}::{seg.location}",
                confidence=0.75,
                notes=(
                    "Customer-provided sensor quantity table. Use to "
                    "cross-check SLD-derived counts."
                ),
            )
        )
    return assets


def extract_drawing_assets(
    docs: list[ParsedDocument], project_id: str
) -> list[DrawingAsset]:
    assets: list[DrawingAsset] = []
    for doc in docs:
        if doc.doc_type == "Drawing / SLD":
            assets.extend(_extract_from_sld_pdf(doc, project_id))
        assets.extend(_extract_from_quantity_table(doc, project_id))
    return assets
