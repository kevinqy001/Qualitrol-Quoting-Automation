"""Deterministic offline agent.

Used when no Claude transport is configured so the chat + tool interaction is
still demonstrable locally. Parses a handful of intents and drives the same
``tools_impl`` functions the real agent uses. When Bedrock/Foundry credentials
are present the Claude Agent SDK loop in ``sdk_agent`` takes over.
"""
from __future__ import annotations

import re

from . import tools_impl as T


def handle(session: dict, message: str) -> dict:
    m = message.lower().strip()
    actions = []

    def done(reply):
        return {"reply": reply, "actions": actions, "changed": bool(actions)}

    # recompute / quote
    if any(w in m for w in ("recompute", "boq", "quote", "roll-up", "rollup", "bill of")):
        q = T.recompute_boq(session)
        lines = "\n".join(f"  • {p['qty']}× {p['name']}" for p in q["products"]) or "  (nothing accepted yet)"
        actions.append("recompute_boq")
        return done(f"Derived BOQ from accepted detections:\n{lines}")

    # summary
    if any(w in m for w in ("summary", "summarise", "summarize", "what's on", "whats on", "overview")):
        s = T.summarize(session)
        parts = [f"{v['total']} {k} ({v.get('accepted',0)} accepted)" for k, v in s["by_type"].items()]
        return done("On this drawing: " + ", ".join(parts) + ".")

    # detection
    if "detect" in m or "re-run" in m or "rerun" in m or "scan" in m:
        try:
            r = T.run_detection(session)
            actions.append("run_detection")
            return done(f"Ran vision detection — added {r['count']} pending components for your review.")
        except Exception as e:
            return done(f"Live detection needs the AWS Bedrock endpoint configured. ({e})")

    # accept / reject / delete
    verb = None
    if re.search(r"\b(accept|approve|confirm)\b", m):
        verb = "accepted"
    elif re.search(r"\b(reject|dismiss)\b", m):
        verb = "rejected"
    is_delete = bool(re.search(r"\b(delete|remove)\b", m))

    if verb or is_delete:
        selectors = ("bay", "transformer", "breaker", "bus", "incomer",
                     "feeder", "future", "capacitor", "cap bank")
        has_selector = any(k in m for k in selectors)
        if has_selector:
            ids = T._resolve_ids(session, message)      # e.g. "accept all bays"
        elif "all" in m:
            ids = [a["id"] for a in session["annotations"]]
        else:
            ids = T._resolve_ids(session, message)      # explicit id / label
        if not ids:
            return done("I couldn't find anything matching that. Try e.g. “accept all bays” or an id like A007.")
        if is_delete:
            r = T.delete_annotation(session, ids)
            actions.append("delete")
            return done(f"Deleted {r['removed']} item(s): {', '.join(r['deleted'])}.")
        r = T.set_status(session, ids, verb)
        actions.append("set_status")
        return done(f"Marked {len(r['updated'])} item(s) as {verb}: {', '.join(r['updated'])}.")

    # find / count / list
    if any(w in m for w in ("find", "show", "list", "how many", "count", "where", "which")):
        hits = T.find_assets(session, message)
        if not hits:
            return done("No matching components found. I can find transformers, bays, breakers, busbars, incomers, feeders, or futures.")
        head = ", ".join(h["label"] for h in hits[:8])
        more = f" …and {len(hits) - 8} more" if len(hits) > 8 else ""
        return done(f"Found {len(hits)}: {head}{more}. Say “accept these” or click any on the drawing.")

    return done(
        "I can: find components (“find all transformer bays”), accept/reject/delete "
        "them (“accept all bays”, “delete A012”), re-run detection, and recompute the BOQ. "
        "What would you like?"
    )
