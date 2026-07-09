"""Claude Agent SDK loop (Bedrock-backed) for the drawing agent.

Registers the drawing-editing capabilities as an in-process MCP server ("app")
and runs a headless, auto-approved agent turn. Streams tool calls + assistant
text back as event dicts for the server's SSE endpoint. Conversation continuity
is preserved by resuming the SDK session id stored on our session.

Configured for AWS Bedrock via env (CLAUDE_CODE_USE_BEDROCK / AWS creds /
BEDROCK_MODEL_ID); the same tools work unchanged on any Claude transport.
"""
from __future__ import annotations

import json
import os

from .. import config, sessions  # noqa: F401
from . import tools_impl as T


def _txt(obj) -> dict:
    # Python in-process tools forward only `content` (not structured_content),
    # so return the result as JSON text.
    return {"content": [{"type": "text", "text": json.dumps(obj)[:6000]}]}


def _short(inp) -> str:
    try:
        return ", ".join(f"{k}={v}" for k, v in inp.items())[:80]
    except Exception:
        return ""


def _system_prompt(session: dict) -> str:
    s = T.summarize(session)
    counts = ", ".join(f"{v['total']} {k}" for k, v in s["by_type"].items()) or "none yet"
    return (
        "You are the Qualitrol Drawing Agent, helping an application engineer take off "
        "components from a GIS single-line diagram to build a monitoring BOQ.\n"
        f"Current drawing: {session['title']}. Detected so far: {counts}.\n\n"
        "Use the app tools to find, accept/reject, edit, add, or delete annotations, to "
        "re-run vision detection on a region, and to recompute the BOQ. Rules:\n"
        "- New detections are always 'pending' — a human confirms them; never auto-accept "
        "everything unless the user explicitly asks.\n"
        "- When you change annotations, briefly say what you changed and why.\n"
        "- Prefer find_assets before bulk actions so you act on the right set.\n"
        "- Be concise. You are editing a live drawing the user is watching."
    )


def _bedrock_env() -> dict:
    env = dict(os.environ)  # inherit AWS creds / region already in the container
    if config.USE_BEDROCK:
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        env.setdefault("AWS_REGION", config.AWS_REGION)
        if config.BEDROCK_BASE_URL:
            env["ANTHROPIC_BEDROCK_BASE_URL"] = config.BEDROCK_BASE_URL
    return env


async def stream(session: dict, message: str):
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,  # noqa: E501
                                  ResultMessage, TextBlock, ToolUseBlock,
                                  create_sdk_mcp_server, query, tool)

    # --- capabilities, bound to this session via closures ---
    @tool("find_assets", "Find annotations by natural-language query (type, function, or label).", {"query": str})
    async def find_assets(args):
        return _txt(T.find_assets(session, args.get("query", "")))

    @tool("set_status", "Accept/reject/reset annotations. ids_or_query = an id, a query like 'all bays', or 'all'. status = accepted|rejected|pending.", {"ids_or_query": str, "status": str})
    async def set_status(args):
        return _txt(T.set_status(session, args["ids_or_query"], args["status"]))

    @tool("update_annotation", "Edit one annotation's label, type, or function.", {"annotation_id": str, "label": str, "type": str, "function": str})
    async def update_annotation(args):
        patch = {k: args.get(k) for k in ("label", "type", "function") if args.get(k)}
        return _txt(T.update_annotation(session, args["annotation_id"], patch))

    @tool("add_annotation", "Add a new annotation. bbox=[x,y,w,h] in image pixels.", {"type": str, "label": str, "bbox": list})
    async def add_annotation(args):
        return _txt(T.add_annotation(session, args["type"], args.get("label", "New"), args["bbox"]))

    @tool("delete_annotation", "Delete annotations by id or query.", {"ids_or_query": str})
    async def delete_annotation(args):
        return _txt(T.delete_annotation(session, args["ids_or_query"]))

    @tool("run_detection", "Run tiled Claude-vision detection, optionally on a region [x,y,w,h]. Adds pending annotations.", {"region": list})
    async def run_detection(args):
        return _txt(T.run_detection(session, region=args.get("region")))

    @tool("recompute_boq", "Recompute the Qualitrol BOQ from accepted annotations.", {})
    async def recompute_boq(args):
        return _txt(T.recompute_boq(session))

    @tool("get_summary", "Summarise annotations by type and status.", {})
    async def get_summary(args):
        return _txt(T.summarize(session))

    server = create_sdk_mcp_server(
        name="app", version="1.0.0",
        tools=[find_assets, set_status, update_annotation, add_annotation,
               delete_annotation, run_detection, recompute_boq, get_summary],
    )

    opts = ClaudeAgentOptions(
        system_prompt=_system_prompt(session),
        mcp_servers={"app": server},
        allowed_tools=["mcp__app__*"],
        permission_mode="dontAsk",       # deny anything not our tools; never prompt
        setting_sources=[],              # ignore local ~/.claude settings on the host
        model=config.BEDROCK_MODEL_ID if config.USE_BEDROCK else None,
        env=_bedrock_env(),
        resume=session.get("sdk_session_id"),
        max_turns=12,
    )

    used_tool = False
    try:
        async for msg in query(prompt=message, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        session.setdefault("chat", []).append({"role": "assistant", "content": b.text})
                        yield {"type": "message", "text": b.text}
                    elif isinstance(b, ToolUseBlock):
                        used_tool = True
                        yield {"type": "tool", "name": b.name.replace("mcp__app__", ""),
                               "detail": _short(b.input)}
            elif isinstance(msg, ResultMessage):
                sid = getattr(msg, "session_id", None)
                if sid:
                    session["sdk_session_id"] = sid
    except Exception as e:  # fail safe — surface, don't crash the stream
        yield {"type": "message", "text": f"(agent error: {e})"}

    yield {"type": "done", "changed": used_tool, "mode": "sdk"}
