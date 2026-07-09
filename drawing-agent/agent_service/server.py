"""FastAPI app for the standalone Drawing Agent service.

Serves its own front end (drawing viewer + overlay + editable annotations +
agent chat) and a clean REST/SSE API so the existing Qualitrol app can call it
later. Touches none of the existing codebase.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import config, quote, sessions
from .agent import runner
from .agent import tools_impl as T

app = FastAPI(title="Qualitrol Drawing Agent", version="0.1.0")

# Allow the existing app (different origin) to call this service.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

app.mount("/web", StaticFiles(directory=str(config.WEB)), name="web")


@app.get("/")
def index():
    return FileResponse(str(config.WEB / "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "claude": config.claude_available(),
            "transport": config.claude_transport()}


@app.get("/api/config")
def api_config():
    return {
        "drawings": sessions.list_drawings(),
        "claude": {
            "available": config.claude_available(),
            "transport": config.claude_transport(),
        },
        "azure_openai": {"available": config.azure_openai_available()},
    }


@app.get("/api/drawings")
def api_drawings():
    return sessions.list_drawings()


@app.post("/api/sessions")
async def api_create_session(req: Request):
    body = await _json(req)
    drawing_id = body.get("drawing_id")
    seed = body.get("seed", True)
    try:
        return sessions.create_session(drawing_id, seed=seed)
    except KeyError:
        raise HTTPException(404, f"unknown drawing {drawing_id}")


@app.get("/api/sessions/{sid}")
def api_get_session(sid: str):
    s = sessions.load(sid)
    if not s:
        raise HTTPException(404, "no such session")
    return s


@app.get("/api/sessions/{sid}/annotations")
def api_annotations(sid: str):
    s = _require(sid)
    return {"annotations": s["annotations"]}


@app.post("/api/sessions/{sid}/annotations")
async def api_add_annotation(sid: str, req: Request):
    s = _require(sid)
    b = await _json(req)
    r = T.add_annotation(s, b["type"], b.get("label", b["type"]), b["bbox"],
                         b.get("props"), actor="engineer")
    sessions.save(s)
    return r


@app.patch("/api/sessions/{sid}/annotations/{aid}")
async def api_update_annotation(sid: str, aid: str, req: Request):
    s = _require(sid)
    patch = await _json(req)
    r = T.update_annotation(s, aid, patch, actor="engineer")
    if "error" in r:
        raise HTTPException(404, r["error"])
    sessions.save(s)
    return r


@app.delete("/api/sessions/{sid}/annotations/{aid}")
def api_delete_annotation(sid: str, aid: str):
    s = _require(sid)
    r = T.delete_annotation(s, [aid], actor="engineer")
    sessions.save(s)
    return r


@app.post("/api/sessions/{sid}/detect")
async def api_detect(sid: str, req: Request):
    s = _require(sid)
    b = await _json(req)
    try:
        r = T.run_detection(s, region=b.get("region"), actor="engineer")
    except Exception as e:
        raise HTTPException(502, str(e))
    sessions.save(s)
    return r


@app.get("/api/sessions/{sid}/boq")
def api_boq(sid: str):
    return quote.compute(_require(sid))


@app.get("/api/sessions/{sid}/export.csv")
def api_export(sid: str):
    q = quote.compute(_require(sid))
    rows = ["Product,Qty,Unit,Basis"]
    for p in q["products"]:
        basis = p["basis"].replace('"', '""')
        rows.append(f'"{p["name"]}",{p["qty"]},{p["unit"]},"{basis}"')
    return PlainTextResponse("\n".join(rows), media_type="text/csv")


@app.post("/api/sessions/{sid}/agent")
async def api_agent(sid: str, req: Request):
    s = _require(sid)
    body = await _json(req)
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "empty message")

    async def gen():
        try:
            async for ev in runner.stream(s, message):
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            sessions.save(s)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- helpers -----------------------------------------------------------------
def _require(sid: str) -> dict:
    s = sessions.load(sid)
    if not s:
        raise HTTPException(404, "no such session")
    return s


async def _json(req: Request) -> dict:
    try:
        return await req.json()
    except Exception:
        return {}
