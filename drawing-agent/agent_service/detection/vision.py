"""Tiled Claude-vision detection returning bounding boxes.

Large A0 SLDs (~5000px) exceed the vision model's effective resolution, so we
split into overlapping tiles, detect per tile, map boxes back to full-image
coordinates and de-duplicate. Works over whichever Claude transport is
configured (Bedrock / Foundry / direct) via ``agent_service.llm``.
"""
from __future__ import annotations

import json

from PIL import Image

from .. import llm
from .taxonomy import build_prompt
from .tiling import dedupe, encode_png, make_tiles


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        text = text[a : b + 1]
    try:
        return json.loads(text)
    except Exception:
        return {}


SYSTEM = (
    "You are an electrical drawing take-off assistant reading a GIS single-line "
    "diagram for a Qualitrol monitoring quotation. Respond with strict JSON only."
)


def detect(image_path: str, region: list | None = None) -> list[dict]:
    """Run tiled detection. ``region`` = [x,y,w,h] in full-image px to restrict to."""
    client = llm.get_client()
    if not client.available:
        raise RuntimeError(
            f"Claude vision not configured ({llm.config.claude_transport()}). "
            "Set the AWS Bedrock env (CLAUDE_CODE_USE_BEDROCK / AWS creds / "
            "BEDROCK_MODEL_ID) to enable live detection."
        )

    prompt = build_prompt()
    img = Image.open(image_path).convert("RGB")
    if region:
        rx, ry, rw, rh = [int(v) for v in region]
        crop = img.crop((rx, ry, rx + rw, ry + rh))
        tmp = image_path + ".region.png"
        crop.save(tmp)
        tiles, _ = make_tiles(tmp)
        ox, oy = rx, ry
    else:
        tiles, _ = make_tiles(image_path)
        ox, oy = 0, 0

    out: list[dict] = []
    for t in tiles:
        text = client.vision(SYSTEM, prompt, encode_png(t.image), max_tokens=4000)
        parsed = _extract_json(text)
        for d in parsed.get("detections", []):
            try:
                bx, by, bw, bh = d["bbox"]
            except Exception:
                continue
            out.append(
                {
                    "type": d.get("type", "gis_bay"),
                    "label": d.get("label", d.get("type", "component")),
                    "bbox": [ox + t.x + bx, oy + t.y + by, bw, bh],
                    "confidence": round(float(d.get("confidence", 0.7)), 2),
                    "status": "pending",
                    "source": f"claude:{client.model}",
                    "props": {},
                    "note": f"Detected by {client.model} ({client.transport})",
                }
            )
    return dedupe(out)
