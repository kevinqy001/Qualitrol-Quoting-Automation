# Contributing & Deployment Guide

This guide explains how to develop, review, and deploy the **Qualitrol Quote
Accelerator** safely. It is written for **both people and coding agents** —
follow it as a checklist. If you are an AI agent, also read [`AGENTS.md`](AGENTS.md)
for the short, must-follow rules.

The companion doc [`docs/AZURE_DEPLOYMENT_HANDOFF.md`](docs/AZURE_DEPLOYMENT_HANDOFF.md)
has the full infrastructure inventory (resource names, regions, endpoints,
open items). This guide is the day-to-day workflow.

---

## 1. Architecture in one minute

- **App:** FastAPI (ASGI) web app. Entry point `app.py` exposes `app` from
  `webapp/server.py`. Served in production by gunicorn + uvicorn worker
  (`startup.sh`).
- **Engine:** `qualitrol_core/` is a rules-first library. The pipeline **always
  runs offline**; the LLM layer (Claude Opus 4.8 via Azure AI Foundry) is
  *optional augmentation* that **fails safe** to the rules result.
- **Pipelines:** `Step 0/1/2/...` folders each have a `pipeline.py` + `run.py` CLI.
- **`drawing-agent/`** is a **separate standalone service** and is **NOT deployed**
  by this repo's pipeline. Leave it offline unless explicitly tasked to host it.

---

## 2. Local setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                      # http://127.0.0.1:8000
# or with autoreload:  uvicorn app:app --reload --port 8000
```

The app runs **without any LLM credentials** (rules-only). To exercise the LLM
layer locally, set the Foundry env vars (see §7) or create the gitignored
`qualitrol_core/llm_config.local.json`. **Never commit credentials.**

---

## 3. Environments

| Env | Branch | App (region) | URL |
|---|---|---|---|
| **Dev** | `dev` | `qtc-quote-accelerator-dev` (UK West) | https://qtc-quote-accelerator-dev.azurewebsites.net |
| **Prod** | `main` | `qtc-quote-accelerator-prod` (UK West) | https://qtc-quote-accelerator-prod.azurewebsites.net |

Deployment is **fully automated** via GitHub Actions
(`.github/workflows/deploy-appservice.yml`):

- **Push to `dev`** → builds and deploys to the **Dev** app.
- **Merge a PR into `main`** → builds and deploys to the **Prod** app.

You do **not** run any `az`/deploy commands by hand for a normal release — git is
the deploy trigger.

---

## 4. The development workflow (follow in order)

1. **Branch off `dev`** (never commit directly to `main`):
   ```bash
   git checkout dev && git pull
   git checkout -b feature/<short-description>
   ```
2. **Make the change.** Keep it focused; small PRs review faster and are safer.
3. **Verify locally** (see §5). The app must start and the pipelines must run.
4. **Commit** with a clear message (see §6).
5. **Open a PR into `dev`.** Get it reviewed, merge → auto-deploys to **Dev**.
6. **Validate on Dev** (open the Dev URL, run a real submission through it).
7. **Promote to Prod:** open a PR from `dev` → `main`. Requires **1 approving
   review** (branch protection is enforced, even for admins). Merge → auto-deploys
   to **Prod**.
8. **Verify Prod** at the Prod URL.

> **Golden rule:** code reaches Prod only through `dev` → validated → PR to
> `main` → review → merge. No direct pushes to `main` (they are blocked).

---

## 5. Verify before you commit (quality gate)

Run these locally. A change is not "done" until they pass:

```bash
# 1. The app imports and starts (catches syntax/import/wiring errors)
python -c "import app; print('app import OK')"

# 2. The pipelines still run end-to-end on a sample (rules-only is fine)
python "Step 1 _ Extract Info/run.py"  --help
python "Step 2 _ Create BOQ/run.py"    --help

# 3. If tests exist (see §8), run them
pytest -q         # only if a tests/ suite is present
```

If your change touches the LLM layer, also confirm it **degrades gracefully**
with no credentials (set `QUALITROL_USE_LLM=0`) — the pipeline must still
complete using rules only.

---

## 6. Commit & PR conventions

- **Commit messages:** `type(scope): summary`, e.g.
  `feat(step2): category-aware BOQ units`, `fix(webapp): handle empty upload`,
  `docs: update deployment guide`. Types: `feat`, `fix`, `docs`, `refactor`,
  `test`, `chore`, `ci`.
- **Explain the *why*** in the body when it isn't obvious.
- **PR description** should state: what changed, why, how you verified it, and any
  risk/rollback notes.
- **One logical change per PR.** Don't mix refactors with features.

---

## 7. Configuration & secrets

The app reads config from **App Service application settings** (env vars in prod)
or the gitignored local files. Key settings:

| Setting | Purpose |
|---|---|
| `ANTHROPIC_FOUNDRY_ENDPOINT` | Foundry Anthropic endpoint (`https://<acct>.services.ai.azure.com/anthropic`) |
| `ANTHROPIC_FOUNDRY_API_KEY` | Foundry account key |
| `ANTHROPIC_FOUNDRY_DEPLOYMENT` | Model deployment name (`claude-opus-4-8`) |
| `QUALITROL_USE_LLM` | Force LLM on (`1`) / off (`0`); auto-detects otherwise |
| `TAVILY_API_KEY` | Optional Step 0 web research |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` — Oryx builds deps on deploy |
| `WEBSITES_PORT` | `8000` |

**Secret rules (non-negotiable):**
- **Never commit secrets.** `.gitignore` already excludes `*.local.json`.
- Dev and Prod use **separate Foundry accounts** (clean per-env billing/tracking).
  Do not point Prod at the Dev endpoint or vice versa.
- To change a prod secret, update the **App Service setting** (or Key Vault) —
  not the code.

---

## 8. Testing (grow this suite)

There is no formal test suite yet. When adding logic, add tests under `tests/`:

```bash
pip install pytest
pytest -q
```

Prioritise tests for: `qualitrol_core/matching.py`, `spec_review.py`, BOQ
quantity logic, and any parser edge cases. LLM-dependent code should be tested
with the LLM **disabled** (rules path) so tests are deterministic and offline.

---

## 9. Production-quality code standards

- **Fail safe.** Anything that calls the LLM or network must catch errors and
  fall back to the deterministic result — never crash the pipeline. Mirror the
  existing pattern in `qualitrol_core/llm.py`.
- **Keep the engine offline-first.** New features should work rules-only; the LLM
  only *augments*.
- **No secrets, no large binaries, no customer data** in commits (`.gitignore`
  already blocks `Gemba Samples/`, `outputs/`, backups, `*.local.json`).
- **Type hints + docstrings** on new public functions. Match the existing style.
- **Don't widen scope silently.** If a change needs new infra (a new Azure
  resource, a new secret, a quota bump), call it out in the PR — don't bake it in.
- **Comments explain *why*, not *what*.** Avoid narrating the code.

---

## 10. Rollback

If a Prod deploy is bad:

1. **Fastest:** in GitHub Actions, re-run the last good `main` deployment, **or**
   revert the offending PR (`git revert`) and merge — this redeploys the previous
   good state.
2. **Azure-side:** the App Service keeps prior deployments; you can also redeploy
   a known-good commit by pushing a revert.
3. Investigate on **Dev** first; never hotfix directly on `main`.

Check logs: `az webapp log tail -g QTC-ProjectQuoteAccelerator-Prod -n qtc-quote-accelerator-prod`.

---

## 11. Quick reference

```bash
# Start a feature
git checkout dev && git pull && git checkout -b feature/x

# Deploy to Dev  (after PR into dev is merged)
#   -> automatic on push/merge to dev

# Promote to Prod
gh pr create --base main --head dev --title "Release: <summary>"
#   -> review + merge -> automatic Prod deploy

# Tail prod logs
az webapp log tail -g QTC-ProjectQuoteAccelerator-Prod -n qtc-quote-accelerator-prod
```

For infrastructure details (Foundry accounts, quotas, EasyAuth status, region
rationale), see [`docs/AZURE_DEPLOYMENT_HANDOFF.md`](docs/AZURE_DEPLOYMENT_HANDOFF.md).
