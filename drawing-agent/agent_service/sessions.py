"""Session + annotation store.

A *session* is a working copy of a drawing plus its annotations (the drawing
asset list, with geometry) and the agent chat transcript. Backed by one JSON
file per session so the service is stateless-friendly on App Service; swap
``_path``/``save``/``load`` for Azure SQL without touching callers.
"""
from __future__ import annotations

import json
import time
import uuid

from . import config

# Bundled drawings (the 776060 KEZAD KB1A SLDs).
CATALOG = {
    "132kv": {
        "id": "132kv",
        "title": "132kV GIS Single Line Diagram — KEZAD (KB1A)",
        "project": "N-19876 · KIZAD-B1 · TAQA Transmission",
        "image": "132kv.png",
        "width": 4967,
        "height": 3509,
    },
    "11kv": {
        "id": "11kv",
        "title": "11kV GIS Single Line Diagram — KEZAD (KB1A)",
        "project": "N-19876 · KIZAD-B1 · TAQA Distribution",
        "image": "11kv.png",
        "width": 4967,
        "height": 3509,
    },
}


def list_drawings():
    return list(CATALOG.values())


def _path(session_id: str):
    return config.SESSIONS / f"{session_id}.json"


def _now() -> float:
    return time.time()


def _seed_annotations(drawing_id: str) -> list[dict]:
    seed = config.SEED / f"{drawing_id}.json"
    if not seed.exists():
        return []
    data = json.loads(seed.read_text())
    anns = []
    for d in data.get("detections", []):
        anns.append(
            {
                "id": d["id"],
                "type": d["type"],
                "label": d.get("label", d["type"]),
                "bbox": d["bbox"],
                "confidence": d.get("confidence", 0.7),
                "status": "pending",
                "source": "sample",
                "props": d.get("props", {}),
                "note": d.get("note", ""),
            }
        )
    return anns


def create_session(drawing_id: str, seed: bool = True) -> dict:
    if drawing_id not in CATALOG:
        raise KeyError(drawing_id)
    meta = CATALOG[drawing_id]
    sid = "S-" + uuid.uuid4().hex[:10]
    session = {
        "id": sid,
        "drawing_id": drawing_id,
        "title": meta["title"],
        "project": meta["project"],
        "image": meta["image"],
        "width": meta["width"],
        "height": meta["height"],
        "annotations": _seed_annotations(drawing_id) if seed else [],
        "chat": [],
        "audit": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    save(session)
    return session


def load(session_id: str) -> dict | None:
    p = _path(session_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save(session: dict) -> None:
    session["updated_at"] = _now()
    _path(session["id"]).write_text(json.dumps(session, indent=2))


def audit(session: dict, actor: str, action: str, detail: str) -> None:
    session.setdefault("audit", []).append(
        {"ts": _now(), "actor": actor, "action": action, "detail": detail}
    )


# --- annotation helpers ------------------------------------------------------
def next_annotation_id(session: dict) -> str:
    n = len(session["annotations"]) + 1
    existing = {a["id"] for a in session["annotations"]}
    while f"A{n:03d}" in existing:
        n += 1
    return f"A{n:03d}"


def find(session: dict, annotation_id: str) -> dict | None:
    for a in session["annotations"]:
        if a["id"] == annotation_id:
            return a
    return None
