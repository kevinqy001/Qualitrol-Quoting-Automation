# Qualitrol Drawing Agent

A **standalone, agentic drawing take-off** service for Qualitrol quoting. Upload
a GIS single-line diagram, let Claude detect the monitored components (with
bounding boxes drawn **on the drawing**), then work with an agent in chat to
find items, correct annotations, re-run detection on a region, and roll the
accepted counts into a Qualitrol BOQ.

It is intentionally **separate from the main
[Qualitrol-Quoting-Automation](https://github.com/RAL-Digital-CX/Qualitrol-Quoting-Automation)
app** — its own front end, its own API, its own deployable. It touches none of
the existing code, and is built so the main app can call it now and the two
experiences can be merged later.

> Status: working prototype. Ships with two real drawings (776060 KEZAD KB1A
> 132kV & 11kV SLDs) and curated seed detections so it demos with **zero
> credentials**; wire an AWS Bedrock endpoint to turn on live detection + the
> real Claude Agent SDK loop.

---

## Why this exists

The main app's `drawing_assets._extract_from_sld_vlm` reads a **single
downscaled page** and returns assets with **no coordinates**, in **batch**. This
service fills three gaps:

| Gap | This service |
|---|---|
| Small symbols lost on A0 drawings | **Overlapping tiling** — detect per tile, map boxes back, de-dupe |
| No spatial "show the work" | Every detection is a **bounding box on the drawing**, with an evidence crop, editable before export |
| Batch only | A **Claude Agent SDK** loop you chat with: find / accept / correct / re-detect / recompute |

Human-in-the-loop is preserved: the agent leaves new detections **pending**; a
person accepts/rejects; every change is written to the session audit log (the
corrections → learning-loop feed).

---

## Quickstart (local)

```bash
cd drawing-agent
python -m pip install -r requirements.txt

# Runs on the bundled Sample engine + offline agent — no credentials needed:
python app.py            # http://localhost:8080
```

### Turn on the real agent (AWS Bedrock)

```bash
cp .env.example .env     # fill in your Bedrock creds (see below)
./run-local.sh           # loads .env, starts on :8080
curl -s localhost:8080/health
# → {"status":"ok","claude":true,"transport":"AWS Bedrock (us-east-1)"}
```

The top-right badge flips to `Claude · AWS Bedrock (region)`. **Run detection**
now does live tiled vision; **Agent** chat runs the Claude Agent SDK tool-loop.

**Required env** (in `.env`):

| Var | Notes |
|---|---|
| `CLAUDE_CODE_USE_BEDROCK=1` | Enables the Bedrock transport (SDK + vision) |
| `AWS_REGION` | e.g. `us-east-1` |
| `BEDROCK_MODEL_ID` | The model / inference-profile **enabled in your account** (e.g. `us.anthropic.claude-opus-4-8`) |
| AWS auth | One of: `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY` (+`AWS_SESSION_TOKEN`), `AWS_PROFILE`, `AWS_BEARER_TOKEN_BEDROCK`, or an IAM role |
| `BEDROCK_BASE_URL` | Optional — private gateway/proxy in front of Bedrock |

> IAM needs `bedrock:InvokeModel` **and** `bedrock:InvokeModelWithResponseStream`
> with model access granted for that model/region. Wrong model id or missing
> access is the #1 setup issue; the service fails safe to Sample + offline agent.

Alternatives to Bedrock (auto-detected if set instead): Azure AI Foundry
(`ANTHROPIC_FOUNDRY_ENDPOINT/API_KEY/DEPLOYMENT` — same vars as the main app) or
direct Anthropic (`ANTHROPIC_API_KEY`).

---

## Architecture

```
web/ (own front end)                  agent_service/ (FastAPI)
  index.html / app.js / styles.css      server.py     REST + SSE, CORS-open for the main app
  drawings/  seed/                       sessions.py   session + annotation store (JSON files)
        │  HTTP + SSE                     quote.py      Qualitrol BOQ roll-up rules
        ▼                                 llm.py        Claude transport (Bedrock / Foundry / direct)
  drawing overlay + chat                  detection/    tiling.py · taxonomy.py · vision.py (tiled bbox)
                                          agent/
                                            tools_impl.py  find/accept/edit/add/delete/detect/recompute
                                            sdk_agent.py   Claude Agent SDK loop (in-process MCP tools)
                                            fallback.py    deterministic offline agent (no creds)
                                            runner.py      picks SDK vs fallback
```

Tool contracts are identical whether the loop runs on the Agent SDK, Managed
Agents, or the fallback — so the transport is swappable without touching the
front end. `tools_impl.py` is the single source of truth for what an edit means;
the SDK tools, the fallback, and the REST API all call it.

---

## API (for the main app to call)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness + Claude transport |
| `GET` | `/api/config` | drawings catalog + engine availability |
| `POST` | `/api/sessions` | create a session `{drawing_id, seed?}` |
| `GET` | `/api/sessions/{id}` | full session (annotations, chat, audit) |
| `GET` | `/api/sessions/{id}/annotations` | annotation list |
| `POST` | `/api/sessions/{id}/annotations` | add `{type,label,bbox,props}` |
| `PATCH` | `/api/sessions/{id}/annotations/{aid}` | update (label/type/status/bbox) |
| `DELETE` | `/api/sessions/{id}/annotations/{aid}` | delete |
| `POST` | `/api/sessions/{id}/detect` | tiled vision `{region?}` |
| `POST` | `/api/sessions/{id}/agent` | **SSE** agent chat `{message}` |
| `GET` | `/api/sessions/{id}/boq` | BOQ roll-up |
| `GET` | `/api/sessions/{id}/export.csv` | BOQ as CSV |

SSE events: `{"type":"tool",...}`, `{"type":"message","text":...}`,
`{"type":"done","changed":bool}`.

---

## Deploy (Azure App Service for Containers / Container Apps)

```bash
docker build -t qualitrol-drawing-agent .
docker run -p 8080:8080 --env-file .env qualitrol-drawing-agent
```

The `claude-agent-sdk` bundles the Claude Code CLI, so the image is Python-only
(no Node). Pass Bedrock creds as container env (Key Vault / managed identity).
For multi-instance session persistence, mount a volume at `CLAUDE_CONFIG_DIR`
and move `data/sessions/` to Azure SQL (see integration notes).

---

## Integration path

1. **Now** — the main app links out / iframes this service, or calls
   `POST /api/sessions` + `/detect` and reads back the annotation list to seed
   its `DrawingAsset` (sheet 14) with real bounding boxes.
2. **Next** — swap the JSON session store for Azure SQL; add Entra ID auth;
   feed accepted-annotation edits into the main app's existing feedback API.
3. **Later** — merge the drawing-overlay + chat into the main app's review UI as
   one experience.

## Limitations (prototype)

- A full-drawing detection fires ~15 Bedrock vision calls; add per-drawing
  caching before wide use.
- Session state is JSON files (single-instance). Fine local; use a DB for shared.
- Seed detections are curated for the two bundled 776060 drawings; other
  drawings rely on live detection.
