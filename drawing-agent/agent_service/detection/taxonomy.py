"""Shared component taxonomy + detection prompt for the vision providers.

The taxonomy mirrors the legend on the TAQA GIS single-line diagrams and is
scoped to the components that drive Qualitrol DMS quantities (bays, circuit
breakers, transformers, busbars).
"""

COMPONENT_TYPES = {
    "gis_bay": "A complete GIS feeder bay — a vertical branch off the busbar with "
    "its own bay label (E01, E02, ... / K01, K02, ...). This is the primary "
    "quantity driver.",
    "power_transformer": "Power transformer symbol (two overlapping circles / "
    "windings), usually annotated with an MVA rating.",
    "circuit_breaker": "Circuit breaker symbol within a bay (e.g. Q0, rated 3150A).",
    "busbar": "Horizontal busbar running across the bays (e.g. BB1A, BB2A).",
}

DETECTION_PROMPT = """You are an electrical drawing take-off assistant reading a
GIS (gas-insulated switchgear) single-line diagram. This is ONE TILE of a larger
drawing. Detect every instance of these component types visible in this tile:

{types}

Return STRICT JSON only, no prose, in this shape:
{{"detections": [
  {{"type": "<one of: {keys}>",
    "label": "<short human label, include the bay/tag id if legible>",
    "bbox": [x, y, width, height],   // pixels within THIS tile, origin top-left
    "confidence": 0.0-1.0}}
]}}

Rules:
- Only report components you can actually see in this tile.
- bbox must be tight around the symbol / bay column.
- If a bay is partially cut off at a tile edge, still report it (it will be
  de-duplicated against the neighbouring tile).
- If nothing is present, return {{"detections": []}}.
"""


def build_prompt() -> str:
    types = "\n".join(f"- {k}: {v}" for k, v in COMPONENT_TYPES.items())
    return DETECTION_PROMPT.format(types=types, keys=", ".join(COMPONENT_TYPES))
