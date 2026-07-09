"""
Tiling helpers for running vision models on large engineering drawings.

A 132kV SLD renders at ~5000 x 3500 px. General-purpose vision models lose
small symbols when a drawing that size is downscaled to fit their input. The
robust pattern (the same one the solution plan calls for) is to split the
drawing into overlapping tiles, detect per tile, then map tile-local boxes
back to global image coordinates and de-duplicate across the overlaps.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image


@dataclass
class Tile:
    index: int
    x: int  # left offset in the full image
    y: int  # top offset in the full image
    w: int
    h: int
    image: Image.Image


def make_tiles(path: str, tile: int = 1400, overlap: int = 250) -> tuple[list[Tile], tuple[int, int]]:
    img = Image.open(path).convert("RGB")
    W, H = img.size
    step = tile - overlap
    tiles: list[Tile] = []
    i = 0
    y = 0
    while y < H:
        x = 0
        while x < W:
            w = min(tile, W - x)
            h = min(tile, H - y)
            tiles.append(Tile(i, x, y, w, h, img.crop((x, y, x + w, y + h))))
            i += 1
            if x + tile >= W:
                break
            x += step
        if y + tile >= H:
            break
        y += step
    return tiles, (W, H)


def encode_png(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(ax, bx)
    iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    iw = max(0, ix2 - ix)
    ih = max(0, iy2 - iy)
    inter = iw * ih
    if inter == 0:
        return 0.0
    return inter / (aw * ah + bw * bh - inter)


def dedupe(dets: list[dict], thresh: float = 0.4) -> list[dict]:
    """Greedy NMS across tile overlaps, keeping the higher-confidence box."""
    dets = sorted(dets, key=lambda d: d.get("confidence", 0), reverse=True)
    kept: list[dict] = []
    for d in dets:
        if all(
            not (k["type"] == d["type"] and iou(k["bbox"], d["bbox"]) > thresh)
            for k in kept
        ):
            kept.append(d)
    for i, d in enumerate(kept, 1):
        d["id"] = f"D{i:03d}"
    return kept
