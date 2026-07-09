# AGENTS.md

Guidance for AI coding agents working in this repo. Read this first, then
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow.

## What this app is
FastAPI (ASGI) web app. Entry: `app.py` → `webapp/server.py`. Rules-first engine
in `qualitrol_core/`; the Claude/Foundry LLM layer is **optional augmentation
that must fail safe** to the rules result.

## Non-negotiable rules
1. **Never commit directly to `main`.** It is branch-protected (PR + 1 review,
   enforced for admins). Work on a `feature/*` branch off `dev`.
2. **Deploy = git.** Push/merge to `dev` → Dev app. Merge PR to `main` → Prod app.
   Do **not** run manual `az`/deploy commands for a normal release.
3. **Never commit secrets** or customer data. `.gitignore` blocks `*.local.json`,
   `outputs/`, `Gemba Samples/`, backups. Config lives in App Service settings.
4. **Keep it offline-first & fail-safe.** New code must work with the LLM
   disabled (`QUALITROL_USE_LLM=0`). Wrap all network/LLM calls to fall back to
   rules on any error (see `qualitrol_core/llm.py`).
5. **`drawing-agent/` is NOT deployed.** Leave it offline unless explicitly asked.
6. **Don't silently expand scope.** New infra/secrets/quota needs must be flagged
   in the PR, not baked in.

## Before you finish a change (self-check)
```bash
python -c "import app; print('app import OK')"     # must pass
python "Step 1 _ Extract Info/run.py" --help        # pipelines still wire up
python "Step 2 _ Create BOQ/run.py"   --help
pytest -q                                            # if tests/ exists
```

## Standard flow
```bash
git checkout dev && git pull
git checkout -b feature/<desc>
# ...change + verify...
git commit -m "type(scope): summary"
# open PR into dev; after merge it auto-deploys to Dev
# validate on Dev, then open PR dev -> main for Prod (needs review)
```

## Environments
- Dev:  branch `dev`  → https://qtc-quote-accelerator-dev.azurewebsites.net
- Prod: branch `main` → https://qtc-quote-accelerator-prod.azurewebsites.net

Infra details (Foundry accounts, quotas, EasyAuth status):
[`docs/AZURE_DEPLOYMENT_HANDOFF.md`](docs/AZURE_DEPLOYMENT_HANDOFF.md).
