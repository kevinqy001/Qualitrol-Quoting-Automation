"""Pure tool implementations over a session dict.

These are the *capabilities* the agent has. They are framework-agnostic: the
Claude Agent SDK wraps them as in-process MCP tools, the offline fallback calls
them directly, and the REST API reuses the same functions — one source of truth
for what an edit means. Every mutation is recorded to the session audit log
(the corrections->learning-loop feed).
"""
from __future__ import annotations

from .. import quote, sessions
from ..config import DRAWINGS

TYPE_KEYWORDS = {
    "power_transformer": ["transformer", "trafo", "xfmr", "power transformer"],
    "gis_bay": ["bay", "gis bay", "feeder bay"],
    "circuit_breaker": ["breaker", "circuit breaker", "cb"],
    "busbar": ["busbar", "bus bar", "bus"],
}
FUNCTION_KEYWORDS = {
    "trans_incomer": ["incomer", "transformer incomer"],
    "cable_feeder": ["cable feeder", "feeder"],
    "bus_coupler": ["bus coupler", "coupler"],
    "bus_section": ["bus section"],
    "cap_bank": ["capacitor", "cap bank", "capacitor bank"],
    "future": ["future", "spare"],
}


def summarize(session: dict) -> dict:
    by_type: dict[str, dict[str, int]] = {}
    for a in session["annotations"]:
        d = by_type.setdefault(a["type"], {"total": 0, "accepted": 0, "rejected": 0, "pending": 0})
        d["total"] += 1
        d[a.get("status", "pending")] = d.get(a.get("status", "pending"), 0) + 1
    return {"by_type": by_type, "total": len(session["annotations"])}


def find_assets(session: dict, query: str) -> list[dict]:
    q = (query or "").lower().strip()
    types = {t for t, kws in TYPE_KEYWORDS.items() if any(k in q for k in kws)}
    funcs = {f for f, kws in FUNCTION_KEYWORDS.items() if any(k in q for k in kws)}
    hits = []
    for a in session["annotations"]:
        fn = a.get("props", {}).get("function")
        match = False
        if types and a["type"] in types:
            match = True
        if funcs and fn in funcs:
            match = True
        if not types and not funcs and q and q in a["label"].lower():
            match = True
        if match:
            hits.append({"id": a["id"], "type": a["type"], "label": a["label"],
                         "status": a.get("status"), "function": fn})
    return hits


def _resolve_ids(session: dict, ids_or_query: str | list) -> list[str]:
    if isinstance(ids_or_query, list):
        return ids_or_query
    s = str(ids_or_query).strip()
    # explicit id?
    if sessions.find(session, s):
        return [s]
    # else treat as a query
    return [h["id"] for h in find_assets(session, s)]


def set_status(session: dict, ids_or_query, status: str, actor: str = "agent") -> dict:
    assert status in ("accepted", "rejected", "pending")
    ids = _resolve_ids(session, ids_or_query)
    changed = []
    for aid in ids:
        a = sessions.find(session, aid)
        if a:
            a["status"] = status
            changed.append(aid)
    sessions.audit(session, actor, f"set_status:{status}", f"{len(changed)} annotations: {changed}")
    return {"updated": changed, "status": status}


def update_annotation(session: dict, annotation_id: str, patch: dict, actor: str = "agent") -> dict:
    a = sessions.find(session, annotation_id)
    if not a:
        return {"error": f"no annotation {annotation_id}"}
    for k in ("label", "type", "status", "bbox", "confidence"):
        if k in patch and patch[k] is not None:
            a[k] = patch[k]
    if "function" in patch and patch["function"]:
        a.setdefault("props", {})["function"] = patch["function"]
    sessions.audit(session, actor, "update", f"{annotation_id}: {patch}")
    return {"updated": annotation_id, "annotation": a}


def add_annotation(session: dict, type: str, label: str, bbox: list,
                   props: dict | None = None, actor: str = "agent") -> dict:
    aid = sessions.next_annotation_id(session)
    a = {"id": aid, "type": type, "label": label, "bbox": [float(v) for v in bbox],
         "confidence": 1.0, "status": "pending", "source": actor,
         "props": props or {}, "note": f"Added by {actor}"}
    session["annotations"].append(a)
    sessions.audit(session, actor, "add", f"{aid}: {type} {label}")
    return {"added": aid, "annotation": a}


def delete_annotation(session: dict, ids_or_query, actor: str = "agent") -> dict:
    ids = set(_resolve_ids(session, ids_or_query))
    before = len(session["annotations"])
    session["annotations"] = [a for a in session["annotations"] if a["id"] not in ids]
    removed = before - len(session["annotations"])
    sessions.audit(session, actor, "delete", f"{removed} annotations: {sorted(ids)}")
    return {"deleted": sorted(ids), "removed": removed}


def run_detection(session: dict, region: list | None = None, actor: str = "agent") -> dict:
    from ..detection import vision  # local import (Pillow/LLM optional at import time)

    image_path = str(DRAWINGS / session["image"])
    found = vision.detect(image_path, region=region)
    # renumber into this session's id space and append as pending
    added = []
    for d in found:
        aid = sessions.next_annotation_id(session)
        d["id"] = aid
        session["annotations"].append(d)
        added.append(aid)
    sessions.audit(session, actor, "run_detection",
                   f"region={region} added {len(added)}")
    return {"added": added, "count": len(added)}


def recompute_boq(session: dict) -> dict:
    return quote.compute(session)
