"""Agent entry point used by the web server.

Streams events as dicts: {"type": "tool", ...}, {"type": "message", "text": ...},
{"type": "done", "changed": bool}. Uses the Claude Agent SDK loop when a Claude
transport is configured and the SDK is installed; otherwise the deterministic
offline agent. The server persists the session after the stream completes.
"""
from __future__ import annotations

from . import fallback


def _sdk_ready() -> bool:
    try:
        from .. import llm

        if not llm.get_client().available:
            return False
        import claude_agent_sdk  # noqa: F401

        return True
    except Exception:
        return False


async def stream(session: dict, message: str):
    session.setdefault("chat", []).append({"role": "user", "content": message})

    if _sdk_ready():
        from . import sdk_agent

        async for ev in sdk_agent.stream(session, message):
            yield ev
        return

    # ---- offline deterministic fallback ----
    res = fallback.handle(session, message)
    for name in res.get("actions", []):
        yield {"type": "tool", "name": name}
    yield {"type": "message", "text": res["reply"]}
    session["chat"].append({"role": "assistant", "content": res["reply"]})
    yield {"type": "done", "changed": res.get("changed", False), "mode": "offline"}
