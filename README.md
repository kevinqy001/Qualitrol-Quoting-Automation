# Qualitrol Quoting Automation — Backend

Rules-first, AI-explainable backend that turns customer submissions (project
specifications, raw emails, circuit drawings / SLDs) into a draft Bill of
Quantities (BOQ) for Qualitrol monitoring products.

The whole pipeline runs **offline** using the controlled reference layer in
`Qualitrol_BOQ_Matching_Data_Package.xlsx`. When Azure AI Foundry credentials are
present, an optional LLM layer (Claude Opus 4.8) turns on automatically to refine
scenarios, fill requirement values, and explain matches — see
[LLM layer](#llm-layer-claude-opus-48-via-azure-ai-foundry).

## Architecture

```
qualitrol_core/              # shared library (the controlled reference + engine)
  config.py                  # paths + tunable thresholds (e.g. review confidence 0.70)
  data_package.py            # loads sheets 03-10 + 17 from the data package
  schemas.py                 # typed models mirroring the data-package sheet schemas
  document_parser.py         # Input Parsing Layer: pdf / docx / txt -> ParsedDocument
  matching.py                # synonym / keyword / value extraction (rules engine)
  drawing_assets.py          # SLD/GSLD -> structured Drawing Asset List (sheet 14)
  constants.py               # count_field <-> metric <-> asset-type lookups
  llm.py                     # LLM client (Anthropic Foundry; NullLLMClient fallback)
  llm_extract.py             # LLM augmentation: scenario/requirement/match/QA helpers
  tavily_client.py           # Tavily web-research client (NullTavilyClient fallback)
  product_research.py        # Step 0: Tavily query plan + LLM structuring (06/07/08)
  io_utils.py                # JSON read/write

Step 0 _ Tavily Search/      # web research -> product families / models / parameters
  pipeline.py                #   (pre-fills sheets 06/07/08 of the data package)
  catalog_excel.py           # writes a candidate workbook for human review
  run.py                     # CLI

Step 1 _ Extract Info/       # evidence -> scenarios -> assets -> metrics -> requirements
  pipeline.py
  run.py                     # CLI

Step 2 _ Create BOQ/         # candidate families -> matching -> compatibility ->
  pipeline.py                #   quantities -> draft BOQ / missing-info questions
  run.py                     # CLI

webapp/                      # web UI + ingestion API (upload -> Step 1 -> Step 2)
  server.py                  #   FastAPI app; loads Step 1/2 pipelines and adapts output
  templates/index.html       #   preserved Ralliant-branded frontend
  static/app.js              #   preserved frontend logic
app.py                       # root entry point: `python app.py` serves the web UI

outputs/<project_id>/        # generated JSON (step1_extract_info.json, step2_create_boq.json)
outputs/_product_catalog/    # Step 0 output (step0_product_catalog.json + candidate .xlsx)
```

Each module maps directly to a node in the process map (`mermaid-diagram.png`).

## Setup

```powershell
pip install -r requirements.txt
```

## Web App — upload documents in the browser

The web UI keeps the original frontend (file upload, requirement review with source
evidence) and wires everything from **document upload onward** to the new pipelines:
uploaded files are run through **Step 1 (Extract Info)** then **Step 2 (Create BOQ)**,
and the results are shown in the review screen (BOQ lines, requirement evidence, and
missing-information questions surfaced as warnings).

```powershell
python app.py
# or, with autoreload:  uvicorn app:app --reload --port 8000
```

Then open <http://127.0.0.1:8000>. Each upload is processed into
`outputs/WEB-XXXXXXXX/` (the parsed files plus `step1_extract_info.json` and
`step2_create_boq.json`). The LLM layer turns on automatically when Foundry
credentials are present; force rules-only with `QUALITROL_USE_LLM=0`.

## Step 0 — Tavily product-catalog research (one-off, optional)

Sheets 06/07/08 of the data package ship with families curated but real product
**models** and **parameters** left blank (`TBD`). Step 0 uses Tavily web search +
the LLM to discover real Qualitrol models and their key parameters, mapping each
parameter back to a controlled Metric ID (sheet 04). It runs **before** Step 1/2,
as a one-off catalog-build step.

```powershell
# Preview the drafted Tavily queries without calling anything
python "Step 0 _ Tavily Search/run.py" --plan-only

# Execute research for all families (needs a Tavily key + the LLM)
python "Step 0 _ Tavily Search/run.py"

# Limit to specific families
python "Step 0 _ Tavily Search/run.py" --families PF_DGA PF_GIS_PD
```

Configure the Tavily key (env var overrides the local file):

```powershell
$env:TAVILY_API_KEY = "tvly-..."
# or create qualitrol_core/tavily_config.local.json  ->  {"api_key": "tvly-..."}
```

Outputs land in `outputs/_product_catalog/`:
- `step0_product_catalog.json` — families + discovered products/parameters + the query plan
- `Qualitrol_Product_Catalog.xlsx` — candidate **06/07/08** sheets for human review

> Safety: Step 0 **never overwrites** `Qualitrol_BOQ_Matching_Data_Package.xlsx`.
> Review the candidate workbook, then paste verified rows into the master. Every
> product is marked `Verified`/`Candidate` and carries its datasheet URL. With no
> Tavily key, Step 0 still emits the full query plan so you can run it by hand.

## Run (end to end)

From the repository root. Step 2 consumes Step 1's JSON output.

```powershell
# Step 1 — extract info (defaults to the 00796547 sample submission)
python "Step 1 _ Extract Info/run.py"
python "Step 1 _ Extract Info/run.py" "Gemba Samples/Sample Customer Submissions/00796547"

# Step 2 — create BOQ (auto-locates the Step 1 output by project id)
python "Step 2 _ Create BOQ/run.py" --project-id 00796547
python "Step 2 _ Create BOQ/run.py" outputs/00796547/step1_extract_info.json
```

### What it produces

- **Step 1** → `outputs/<id>/step1_extract_info.json`
  - `detected_scenarios` (Scenario IDs + confidence + asset corroboration)
  - `extracted_evidence` (sheet 12, with source/location/confidence)
  - `drawing_asset_list` (sheet 14, from the SLD/GSLD)
  - `structured_requirements` (sheet 13, metric-normalized)
- **Step 2** → `outputs/<id>/step2_create_boq.json`
  - `product_matching` (sheet 15)
  - `compatibility_flags` (which guardrails from sheet 10 are triggered)
  - `draft_boq` (sheet 16, each line carries quantity basis + assumption + confidence)
  - `missing_info_questions` (sheet 17) and the `information_complete` decision gate

## Design principles (from the data package README)

- **Evidence first** — every requirement is traceable to source text + confidence.
- **Rules-first, AI-explained** — deterministic matching using the controlled
  tables; LLM is additive, never the sole source of truth.
- **Quantities from drawings, not images** — drawings are converted to a
  structured asset list, then quantity rules are applied.
- **Human review is mandatory** — low confidence (< 0.70, CR_013), missing
  layouts/counts (CR_004/CR_005/CR_010), and TBD product capability all route to
  review before quotation.

## LLM layer (Claude Opus 4.8 via Azure AI Foundry)

The pipeline is **rules-first**: it always runs offline using the data package.
When Foundry credentials are present, an **AI augmentation layer** turns on
automatically and is used in both steps:

| Step | LLM task | File |
|------|----------|------|
| Step 1 | Refine scenarios (drop CT/VT false positives, fix confidence, add missed ones, give rationale) | `llm_extract.refine_scenarios` |
| Step 1 | Extract / fill normalized requirement values from document text | `llm_extract.extract_requirements` |
| Step 2 | Explain each product match (recommendation + gap/risk) | `llm_extract.explain_matches` |
| Step 2 | Suggest additional clarification questions | `llm_extract.suggest_missing_info` |

Every call is grounded in the controlled vocabulary + rules-extracted evidence,
and **fails safe** — any error or empty response falls back to the rules result,
so the pipeline always completes.

**Install the optional dependency:**

```powershell
pip install anthropic
```

**Configure credentials** (either option works; env vars override the file):

1. Local file (gitignored) — `qualitrol_core/llm_config.local.json`:

```json
{
  "endpoint": "https://<resource>.services.ai.azure.com/anthropic/",
  "api_key": "<your-key>",
  "deployment": "claude-opus-4-8"
}
```

2. Environment variables:

```powershell
$env:ANTHROPIC_FOUNDRY_ENDPOINT = "https://<resource>.services.ai.azure.com/anthropic/"
$env:ANTHROPIC_FOUNDRY_API_KEY  = "<your-key>"
$env:ANTHROPIC_FOUNDRY_DEPLOYMENT = "claude-opus-4-8"
```

The LLM is enabled automatically when credentials are found. Force it off with
`$env:QUALITROL_USE_LLM = "0"` (rules-only) or on with `"1"`. Each step's JSON
output includes an `llm` block reporting `enabled / available / used / model`.

> Security: `llm_config.local.json` and `*.local.json` are git-ignored. Never
> commit API keys.

## Notes / next steps

- Product models and capability values in sheets 07/08 are placeholders (`TBD`);
  Step 2 correctly flags candidates as "Needs Review" until the product team
  fills in validated models, parameters, standards, and protocols.
- The SLD asset extractor is intentionally conservative (regex over the PDF text
  layer). For production accuracy on complex drawings, plug an LLM/vision step
  into `qualitrol_core/drawing_assets.py`.
```
