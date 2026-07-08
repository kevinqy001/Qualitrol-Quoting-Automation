# Azure Deployment — Overnight Handoff

**App:** Qualitrol Quote Accelerator (FastAPI / Python ASGI)
**Repo:** https://github.com/RAL-Digital-CX/Qualitrol-Quoting-Automation
**Prepared:** 2026-07-08 by automated setup session
**Status:** 🟢 **Dev deployed to UK West.** The App Service VM quota increase landed in **UK West** and **West Europe** (NOT East US / UK South / North Europe). Dev app is provisioned in **UK West** for the UK-based team. Prod pending validation of Dev.

> **Region note:** The resource groups are tagged `eastus`, but a Web App can live in any region (the RG location is only metadata). Dev runs in **UK West**; Prod should follow (UK West or West Europe — both have confirmed quota).

---

## TL;DR for the next engineer

1. The app is a **FastAPI (ASGI) web app** meant for **Azure App Service Linux** (see `startup.sh`). A `dev` branch and a multi-environment GitHub Actions workflow (`.github/workflows/deploy-appservice.yml`) are ready.
2. **You cannot deploy to App Service yet:** the target subscription has an **App Service VM quota of 0** (all tiers, all regions tested). This needs a **subscription admin** to raise it. See Blocker #1.
3. **Container Apps compute IS available** in the same subscription (verified). If a quota increase is slow, the fastest path to a running Dev environment is **Azure Container Apps** — see [Alternative migration path](#alternative-migration-path--azure-container-apps).
4. The deploying identity (`Kunal.Dovedy@icgna.com`) is **Contributor on the two resource groups only** — no subscription or directory rights. This rules out OIDC and service-principal auth without admin help. Use **publish-profile secrets**.

---

## Target environment

| Item | Value |
|---|---|
| Subscription | `Qualitrol IRIS Power Azure Enterprise` (`e0d34744-870b-4bce-b997-052e7d90ea3f`) |
| Tenant | `ralliant.onmicrosoft.com` (`523cf7c2-a9b1-496c-b186-811193b6880f`) |
| Dev resource group | `QTC-ProjectQuoteAccelerator-Dev` (East US) |
| Prod resource group | `QTC-ProjectQuoteAccelerator-Prod` (East US) |
| Dev app | `qtc-quote-accelerator-dev` — **deployed in UK West** → https://qtc-quote-accelerator-dev.azurewebsites.net |
| Prod app | `qtc-quote-accelerator-prod` (name confirmed globally available; provision in UK West or West Europe) |
| App region | **UK West** (Dev). App region ≠ RG region — RGs are tagged East US but that doesn't constrain the app. |
| Runtime | `PYTHON:3.14` (confirmed available on App Service Linux) |
| Plan SKU | B1 (Basic) for both |
| Branch → env mapping | `dev` → Dev, `main` → Prod |

### Existing resources already in the RGs (NOT the quote app)
`QTC-ProjectQuoteAccelerator-Dev` already contains resources belonging to a **different project (`sensorcount`)** — do not disturb them:
- `sensorcount` — Storage account (eastus)
- `sensorcountrcs` — Cognitive Services account (eastus2)
- `QTC-ProjectQuoteAccelerator-Dev-resource-5487` — Cognitive Services account (eastus)
- `workspace-rojectuotecceleratorevNQUW` — Log Analytics workspace (eastus)

`QTC-ProjectQuoteAccelerator-Prod` is **empty**.

> A temporary probe environment `qtc-probe-cae` (Microsoft.App/managedEnvironments) was created during capability testing and deleted the same session. Confirm it is gone: `az resource list -g QTC-ProjectQuoteAccelerator-Dev -o table`.

---

## Limitations audit (complete)

### Blocker #1 — App Service VM quota (RESOLVED for UK West / West Europe)
Initially, creating **any** App Service Plan failed with `Current Limit (Total VMs): 0`. After a quota increase, availability was probed empirically (create a throwaway B1 plan, delete on success):

| Region | B1 result |
|---|---|
| East US | quota 0 ❌ |
| East US 2 | quota 0 ❌ |
| UK South | quota 0 ❌ |
| North Europe | quota 0 ❌ |
| **UK West** | ✅ available (Dev deployed here) |
| **West Europe** | ✅ available |
| France Central | temporary capacity shortage ⚠️ |

**Note:** `0` is a real hard limit, not "unlimited" — confirmed because creates succeed only where limit > 0. If you need App Service in **East US** specifically, that region still needs a separate quota increase.

### Blocker #2 — OIDC / service-principal auth not possible for this user
OIDC (and SP-based auth) needs an Azure AD app registration **plus a role assignment** on the target scope.
- ✅ The user *can* create app registrations (owns 4 already).
- ❌ The user **cannot create role assignments** — that requires **Owner** or **User Access Administrator**; the user only has **Contributor** on the RGs.

**Resolution:** Use **publish-profile secrets** (already wired in the workflow). Publish profiles are readable with Contributor and need no directory/role changes. If you want OIDC long-term, an admin must run the `az ad app` + `az role assignment create` steps once.

### Blocker #3 — No subscription-level read access
- `az role assignment list --scope /subscriptions/e0d34744-…` → empty (no sub-scope roles).
- `az vm list-usage --location eastus` → returns `[]` (cannot read compute quota).
- `az policy assignment list` at subscription scope → empty (cannot enumerate).
- `az provider register --namespace Microsoft.Web` → `AuthorizationFailed` (provider was already Registered, so harmless here — but you can't register new providers).

**Implication:** These providers are **NotRegistered** and the user **cannot register them**. If you need them, an admin must register:
- `Microsoft.ContainerInstance` — NotRegistered
- `Microsoft.ContainerService` (AKS) — NotRegistered
- `Microsoft.DBforPostgreSQL` — NotRegistered

Already **Registered** (usable): `Microsoft.Web`, `Microsoft.App` (Container Apps), `Microsoft.ContainerRegistry`, `Microsoft.Storage`, `Microsoft.KeyVault`, `Microsoft.CognitiveServices`, `Microsoft.OperationalInsights`, `Microsoft.Sql`.

### Non-blocker — GitHub CLI auth
Initially the `gh` keyring token was invalid. It was re-authenticated to account `Kunal-Dovedy_ralliant` with scopes `repo`, `workflow`, `read:org` — sufficient to push branches and set repo/environment secrets.

---

## What has been prepared (done)

- ✅ Repo cloned; app identified as FastAPI/ASGI for App Service Linux.
- ✅ `dev` branch created locally (not yet pushed — see step 1 below).
- ✅ **New workflow** `.github/workflows/deploy-appservice.yml` — builds on push to `dev`/`main`, deploys `dev`→Dev app and `main`→Prod app via publish-profile secrets, uses GitHub Environments (`development`/`production`) for gating.
- ✅ **Removed** the auto-generated single-app workflow `main_qualitrol-quoting-automation.yml` (per decision to replace with multi-env).
- ✅ Left `pages.yml` (GitHub Pages static build) untouched.

Secrets the workflow expects (set these once the apps exist):
- `AZURE_WEBAPP_PUBLISH_PROFILE_DEV`
- `AZURE_WEBAPP_PUBLISH_PROFILE_PROD`

---

## Runbook — finish the deployment (once quota is granted)

Run from an account with the same or greater access, after `az login --tenant ralliant.onmicrosoft.com` and `az account set --subscription e0d34744-870b-4bce-b997-052e7d90ea3f`.

### 1. Push the dev branch
```bash
cd Qualitrol-Quoting-Automation
git push -u origin dev
```

### 2. Create the App Service plans + web apps (after quota > 0)
```bash
# --- Dev (UK West) ---
az appservice plan create -g QTC-ProjectQuoteAccelerator-Dev \
  -n qtc-quote-accelerator-dev-plan --is-linux --sku B1 --location ukwest
az webapp create -g QTC-ProjectQuoteAccelerator-Dev \
  -p qtc-quote-accelerator-dev-plan -n qtc-quote-accelerator-dev \
  --runtime "PYTHON:3.14"

# --- Prod (UK West; West Europe also has quota) ---
az appservice plan create -g QTC-ProjectQuoteAccelerator-Prod \
  -n qtc-quote-accelerator-prod-plan --is-linux --sku B1 --location ukwest
az webapp create -g QTC-ProjectQuoteAccelerator-Prod \
  -p qtc-quote-accelerator-prod-plan -n qtc-quote-accelerator-prod \
  --runtime "PYTHON:3.14"
```

### 3. Configure startup command + build settings (both apps)
```bash
for RG_APP in \
  "QTC-ProjectQuoteAccelerator-Dev:qtc-quote-accelerator-dev" \
  "QTC-ProjectQuoteAccelerator-Prod:qtc-quote-accelerator-prod"; do
  RG="${RG_APP%%:*}"; APP="${RG_APP##*:}"
  az webapp config set -g "$RG" -n "$APP" --startup-file "startup.sh"
  az webapp config appsettings set -g "$RG" -n "$APP" --settings \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true WEBSITES_PORT=8000
done
```
`startup.sh` runs gunicorn with the uvicorn worker (correct for FastAPI/ASGI).

### 4. Optional app settings (LLM / research layers)
The pipeline runs rules-only without these. To enable the optional layers, set (per env):
```bash
az webapp config appsettings set -g <RG> -n <APP> --settings \
  ANTHROPIC_API_KEY=<...>  TAVILY_API_KEY=<...>
```
> Do NOT commit these; the repo `.gitignore` already excludes `*.local.json`. Store real secrets in Key Vault or app settings only.

### 5. Wire GitHub secrets (publish profiles)
```bash
# Dev
az webapp deployment list-publishing-profiles \
  -g QTC-ProjectQuoteAccelerator-Dev -n qtc-quote-accelerator-dev --xml \
  | gh secret set AZURE_WEBAPP_PUBLISH_PROFILE_DEV \
      --repo RAL-Digital-CX/Qualitrol-Quoting-Automation

# Prod
az webapp deployment list-publishing-profiles \
  -g QTC-ProjectQuoteAccelerator-Prod -n qtc-quote-accelerator-prod --xml \
  | gh secret set AZURE_WEBAPP_PUBLISH_PROFILE_PROD \
      --repo RAL-Digital-CX/Qualitrol-Quoting-Automation
```
(Optionally scope these to GitHub Environments `development` / `production` instead of repo-wide.)

### 6. Trigger + verify
```bash
git push -u origin dev          # triggers Dev deploy
# after validation, merge dev -> main to deploy Prod
curl -I https://qtc-quote-accelerator-dev.azurewebsites.net/
curl -I https://qtc-quote-accelerator-prod.azurewebsites.net/
```

---

## Alternative migration path — Azure Container Apps

Use this if the App Service quota increase is delayed. **Container Apps compute is confirmed available** in this subscription (Managed Environment quota: 1/50 used, Session Pools 50, Express Envs 500; `Microsoft.App` is Registered).

Sketch (Dev):
```bash
az extension add --name containerapp --upgrade

# 1. Build & push an image (need a container registry the user can push to;
#    Microsoft.ContainerRegistry is Registered, so an ACR can be created in the RG).
az acr create -g QTC-ProjectQuoteAccelerator-Dev -n qtcquoteacceleratoracr --sku Basic
az acr build -r qtcquoteacceleratoracr -t quote-accelerator:latest .   # needs a Dockerfile (see note)

# 2. Container Apps environment + app
az containerapp env create -g QTC-ProjectQuoteAccelerator-Dev \
  -n qtc-quote-accelerator-env --location eastus
az containerapp create -g QTC-ProjectQuoteAccelerator-Dev \
  -n qtc-quote-accelerator-dev --environment qtc-quote-accelerator-env \
  --image qtcquoteacceleratoracr.azurecr.io/quote-accelerator:latest \
  --target-port 8000 --ingress external --registry-server qtcquoteacceleratoracr.azurecr.io
```
Notes:
- The repo has **no Dockerfile** yet. Add one that installs `requirements.txt` and runs the same gunicorn/uvicorn command as `startup.sh` (bind `0.0.0.0:8000`).
- CI/CD for Container Apps uses `azure/container-apps-deploy-action` (OIDC or ACR creds) rather than a publish profile — the workflow would need adjustment.

---

## Decisions already made (for context)
- Region: **UK West** (quota available there; UK-based team). West Europe is the fallback.
- SKU: **B1** both envs.
- CI/CD auth: **publish-profile secrets** (OIDC not viable — Blocker #2).
- Existing `main_*` workflow: **replaced** with `deploy-appservice.yml`.
- Python: **3.14**.
- Rollout order: **Dev first**, then Prod.

## Open questions for the team
1. Who is the subscription admin who can raise the App Service quota (Blocker #1)?
2. Prefer waiting for App Service quota, or migrate to **Container Apps** now (needs a Dockerfile)?
3. Long-term, should we set up OIDC via an admin (removes stored publish-profile secrets)?
4. Any values for the optional `ANTHROPIC_API_KEY` / `TAVILY_API_KEY` per environment?
