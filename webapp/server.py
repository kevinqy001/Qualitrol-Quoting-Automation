"""FastAPI server for the Qualitrol Quotation web app.

The frontend (templates/index.html + static/app.js) is preserved from the
original POC. Everything from "document upload" onward is powered by the new
pipelines:

    upload  ->  Step 1 (Extract Info)  ->  Step 2 (Create BOQ)  ->  UI

Step 1 / Step 2 live in their own folders and share ``qualitrol_core``. We load
their ``pipeline.py`` modules by path (the folder names contain spaces) and call
``pipeline.run(...)`` directly, then adapt the rich JSON outputs into the shape
the existing frontend already knows how to render.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import json as _json

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Make the shared core + step pipelines importable
# --------------------------------------------------------------------------- #
WEBAPP_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEBAPP_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from qualitrol_core import config, io_utils  # noqa: E402
from qualitrol_core.document_parser import parse_project_folder  # noqa: E402
from webapp.docgen import generate_quotation_docx  # noqa: E402

TEMPLATES_DIR = WEBAPP_DIR / "templates"
STATIC_DIR = WEBAPP_DIR / "static"


def _load_pipeline(module_name: str, path: Path) -> ModuleType:
    """Load a Step pipeline module by file path (folder names contain spaces)."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"Cannot load pipeline module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


step1_pipeline = _load_pipeline(
    "qualitrol_step1_pipeline", config.STEP1_DIR / "pipeline.py"
)
step2_pipeline = _load_pipeline(
    "qualitrol_step2_pipeline", config.STEP2_DIR / "pipeline.py"
)

# --------------------------------------------------------------------------- #
# App + middleware + static/templates
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="Qualitrol Quotation Agent",
    description="AI-driven quotation generation for Qualitrol products "
    "(Step 1 Extract Info + Step 2 Create BOQ).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SAMPLE_PROJECT_ID = "00796547"

FILE_TYPE_LABELS = {
    ".pdf": "PDF",
    ".docx": "DOCX",
    ".xlsx": "Excel",
    ".xlsm": "Excel (macro)",
    ".pptx": "PowerPoint",
    ".csv": "CSV",
    ".txt": "TXT",
    ".eml": "EML",
    ".msg": "MSG",
    ".md": "MD",
}

# Feature flags shown as chips on the review screen (best-effort keyword scan).
FEATURE_KEYWORDS = {
    "dga_monitor": ["dga", "dissolved gas"],
    "temperature_monitor": ["temperature", "winding temp", "hot spot", "hot-spot"],
    "bushing_monitor": ["bushing"],
    "fiber_optic": ["fiber optic", "fibre optic", "fiber-optic", "fiber sensor"],
    "iec61850": ["61850"],
    "modbus_tcp": ["modbus"],
    "dnp3": ["dnp3", "dnp 3", "dnp-3"],
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _features_from_text(text: str) -> dict[str, bool]:
    low = text.lower()
    return {key: any(kw in low for kw in kws) for key, kws in FEATURE_KEYWORDS.items()}


def _overview_summary(step1: dict, step2: dict, preview_text: str) -> str:
    """Concise, plain-language project overview.

    Describes what the project is (voltage class / sector), the monitoring
    applications detected, and which Qualitrol product categories are involved.
    Deliberately omits clarification-question / review-status noise.
    """
    import re

    detected = step1.get("detected_scenarios", [])
    boq = step2.get("draft_boq", [])

    # Monitoring applications (detected scenarios, strongest first).
    apps: list[str] = []
    seen: set[str] = set()
    for d in sorted(detected, key=lambda x: -float(x.get("confidence", 0) or 0)):
        name = (d.get("scenario") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            apps.append(name)

    # Qualitrol product categories (distinct BOQ product/family descriptions).
    cats: list[str] = []
    seen_c: set[str] = set()
    for b in boq:
        desc = (b.get("product_description") or "").strip()
        if desc and desc.lower() not in seen_c:
            seen_c.add(desc.lower())
            cats.append(desc)

    if not apps and not cats:
        return (
            "No Qualitrol monitoring scope was detected in the uploaded documents. "
            "Upload a project specification and/or single-line diagram to extract "
            "requirements."
        )

    kvs = [int(m) for m in re.findall(r"(\d{2,4})\s*kV", preview_text or "", re.I)]
    voltage = f"**{max(kvs)} kV** " if kvs else ""

    apps_str = (
        "; ".join(f"**{a}**" for a in apps[:6])
        if apps
        else "general substation monitoring"
    )
    cats_str = (
        "; ".join(f"**{c}**" for c in cats[:8]) if cats else "to be confirmed"
    )

    return (
        f"This is a {voltage}power-grid substation monitoring project "
        f"(electric utility / transmission & distribution sector). "
        f"Monitoring applications identified: {apps_str}. "
        f"Qualitrol product categories involved: {cats_str} "
        f"(**{len(boq)} product line(s)** proposed)."
    )


def _preview_for_folder(folder: Path, limit: int = 6000) -> str:
    """Concatenate readable text from a parsed submission folder for the UI."""
    try:
        docs = parse_project_folder(folder)
    except Exception:  # pragma: no cover - defensive
        return ""
    parts = [f"# {d.file_name}\n{d.full_text}" for d in docs]
    return ("\n\n".join(parts))[:limit]


def build_extraction(
    step1: dict,
    step2: dict,
    preview_text: str,
    source_meta: dict,
) -> tuple[dict, float]:
    """Adapt Step 1 + Step 2 outputs into the frontend extraction/BOQ schema.

    Returns (extraction, confidence) where confidence is a 0..1 value used for
    the ingestion summary stat tile.
    """
    project_id = step1.get("project_id", "")
    evidence_by_id = {
        e.get("evidence_id"): e for e in step1.get("extracted_evidence", [])
    }

    # --- Requirement evidence list (Step 1 structured requirements) --------- #
    requirements: list[dict] = []
    for req in step1.get("structured_requirements", []):
        value = (req.get("parameter_value") or "").strip()
        unit = (req.get("unit") or "").strip()
        label = req.get("metric_name") or req.get("metric_id") or "Requirement"
        if value:
            label = f"{label} = {value} {unit}".strip()

        tech_params: dict = {}
        if req.get("requirement_type"):
            tech_params["type"] = req["requirement_type"]
        if req.get("asset_type"):
            tech_params["asset"] = req["asset_type"]

        ev = evidence_by_id.get(req.get("evidence_id"))
        evidence_text = (
            (ev.get("evidence_text") if ev else "")
            or req.get("missing_or_assumption")
            or "No evidence snippet returned."
        )

        requirements.append(
            {
                "category": req.get("scenario") or "Requirement",
                "productCode": req.get("scenario_id") or "",
                "requirement": label,
                "unit": unit,
                "technicalParams": tech_params,
                "confidence": req.get("confidence", 0.0),
                "evidence": evidence_text,
            }
        )

    # --- BOQ product lines (Step 2 draft BOQ) ------------------------------- #
    line_items: list[dict] = []
    for boq in step2.get("draft_boq", []):
        tech_params = {}
        if boq.get("scenario_id"):
            tech_params["scenario"] = boq["scenario_id"]
        if boq.get("related_assets"):
            tech_params["related"] = boq["related_assets"]
        if boq.get("review_status"):
            tech_params["review"] = boq["review_status"]
        if boq.get("quantity_basis"):
            tech_params["basis"] = boq["quantity_basis"]

        product_model = boq.get("product_model") or ""
        product_id = boq.get("product_id") or ""
        # Use the human-readable model name as productCode when available;
        # fall back to product_id (e.g. PROD_PF_DGA_01) only if no model.
        display_code = product_model or product_id or "TBD"
        line_items.append(
            {
                "lineNumber": boq.get("boq_line"),
                "productCode": display_code,
                "product_model": product_model,
                "product_id": product_id,
                "description": boq.get("product_description") or "",
                "quantity": boq.get("quantity"),
                "unit": boq.get("unit") or "",
                "technicalParams": tech_params,
                "review_status": boq.get("review_status") or "",
            }
        )

    # --- Warnings: only real pipeline alerts (not missing-info questions) --- #
    warnings: list[str] = []
    for flag in step2.get("compatibility_flags", []):
        if flag.get("triggered"):
            warnings.append(
                f"[{flag.get('severity', 'Info')}] {flag.get('rule_id', '')} - "
                f"{flag.get('action', '')}".strip()
            )

    missing_questions = step2.get("missing_info_questions", [])
    question_count = len(missing_questions)

    # --- Extraction summary: concise plain-language project overview -------- #
    info_complete = step2.get("information_complete", False)
    extraction_summary = _overview_summary(step1, step2, preview_text)

    detected = step1.get("detected_scenarios", [])
    if detected:
        confidence = round(
            sum(d.get("confidence", 0.0) for d in detected) / len(detected), 2
        )
    else:
        confidence = 0.0

    extraction = {
        "boqId": f"BOQ-{project_id}",
        "caseReference": project_id,
        "extractionSummary": extraction_summary,
        "extractionMode": "llm" if step1.get("llm", {}).get("used") else "rules",
        "itemCount": len(line_items),
        "features": _features_from_text(preview_text),
        "requirements": requirements,
        "lineItems": line_items,
        "source": {
            "fileName": source_meta.get("fileName", "uploaded"),
            "fileType": source_meta.get("fileType", "file"),
            "contentType": source_meta.get("contentType", ""),
            "warnings": warnings,
            "preview": preview_text[:3000],
        },
        # Extra payload (not required by the current UI but handy for callers).
        "detectedScenarios": detected,
        "informationComplete": info_complete,
        "missingInfoQuestions": missing_questions,
        "missingInfoCount": question_count,
        "productMatching": step2.get("product_matching", []),
        # Download URL for the generated BOQ Excel (when one exists on disk).
        "boqExcelUrl": (
            f"/api/v1/boq/excel/{project_id}"
            if project_id
            and (
                step2.get("_boq_excel_path")
                or (config.OUTPUT_DIR / project_id / f"BOQ-{project_id}.xlsx").exists()
            )
            else None
        ),
    }
    return extraction, confidence


async def _run_pipelines(
    upload_dir: Path,
    project_id: str,
    output_dir: Path,
    sld_filenames: set[str] | None = None,
):
    """Run Step 1 then Step 2 off the event loop (they may call the LLM)."""
    step1 = await asyncio.to_thread(
        step1_pipeline.run, upload_dir, project_id, output_dir, sld_filenames
    )
    step1_path = output_dir / "step1_extract_info.json"
    step2 = await asyncio.to_thread(step2_pipeline.run, step1_path, output_dir)
    return step1, step2


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(path=str(STATIC_DIR / "favicon.ico"), media_type="image/x-icon")


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "qualitrol-quotation-agent"}


# --------------------------------------------------------------------------- #
# Document ingestion  ->  Step 1 + Step 2
# --------------------------------------------------------------------------- #
@app.post("/api/v1/ingest")
async def ingest_document(file: UploadFile = File(...)):
    """Single-file ingestion (kept for compatibility); delegates to batch."""
    return await ingest_documents([file], sld_filenames="")


@app.post("/api/v1/ingest/batch")
async def ingest_documents(
    files: list[UploadFile] = File(...),
    sld_filenames: str = Form(""),
):
    """Ingest uploaded files and run the full Step 1 + Step 2 pipeline on them.

    Args:
        files: All uploaded files (project documents + SLD diagrams combined).
        sld_filenames: JSON-encoded list of filenames that came from the SLD
            upload zone.  Those files will have their ``doc_type`` forced to
            ``"Drawing / SLD"`` regardless of filename heuristics.
    """
    if not files:
        raise HTTPException(400, "No files uploaded.")

    # Parse the SLD filename list sent by the frontend.
    sld_set: set[str] = set()
    if sld_filenames:
        try:
            names = _json.loads(sld_filenames)
            sld_set = {Path(n).name.lower() for n in names if n}
        except Exception:
            pass

    started = time.perf_counter()
    project_id = f"WEB-{uuid.uuid4().hex[:8].upper()}"
    output_dir = config.OUTPUT_DIR / project_id
    upload_dir = output_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    skipped: list[str] = []
    total_bytes = 0
    first_ext = ""

    for file in files:
        raw = await file.read()
        total_bytes += len(raw)
        safe_name = Path(file.filename or "upload.bin").name
        dest = upload_dir / safe_name
        dest.write_bytes(raw)
        if dest.suffix.lower() in config.SUPPORTED_DOC_EXTENSIONS:
            saved.append(safe_name)
            first_ext = first_ext or dest.suffix.lower().lstrip(".")
        else:
            skipped.append(safe_name)

    try:
        step1, step2 = await _run_pipelines(
            upload_dir, project_id, output_dir, sld_filenames=sld_set or None
        )
    except Exception as exc:  # surface a clean error to the UI
        logging.error("Pipeline failed for project %s:\n%s", project_id, traceback.format_exc())
        raise HTTPException(500, f"Pipeline failed: {exc}") from exc

    preview_text = _preview_for_folder(upload_dir)
    file_label = (
        saved[0] if len(saved) == 1 else f"{len(saved)} files"
    ) if saved else f"{len(files)} files"
    source_meta = {
        "fileName": file_label,
        "fileType": first_ext or "file",
        "contentType": "multipart/form-data",
    }
    extraction, confidence = build_extraction(step1, step2, preview_text, source_meta)

    if skipped:
        extraction["source"]["warnings"].insert(
            0,
            "[Info] Unsupported file(s) ignored by the parser: "
            + ", ".join(skipped)
            + f". Supported types: {', '.join(sorted(config.SUPPORTED_DOC_EXTENSIONS))}.",
        )

    return {
        "caseId": project_id,
        "fileName": file_label,
        "contentType": "multipart/form-data",
        "fileSizeBytes": total_bytes,
        "status": "extracted",
        "confidence": confidence,
        "processingTimeMs": int((time.perf_counter() - started) * 1000),
        "fileCount": len(files),
        "files": [
            {"fileName": name, "status": "parsed"} for name in saved
        ]
        + [{"fileName": name, "status": "skipped"} for name in skipped],
        "extraction": extraction,
        "boq": extraction,
        "ingestedAt": _now(),
        "outputDir": str(output_dir),
    }


# --------------------------------------------------------------------------- #
# POC 1 runtime status (drives the "Extraction Runtime" panel)
# --------------------------------------------------------------------------- #
@app.get("/api/v1/poc1/status")
async def poc1_status():
    return {
        "focus": "Step 1 (Extract Info) + Step 2 (Create BOQ)",
        "supportedFileTypes": {
            ext: FILE_TYPE_LABELS.get(ext, ext.lstrip(".").upper())
            for ext in sorted(config.SUPPORTED_DOC_EXTENSIONS)
        },
        "llm": {
            "configured": config.SETTINGS.llm_credentials_present,
            "provider": config.SETTINGS.llm_provider,
            "endpointConfigured": bool(config.SETTINGS.llm_endpoint),
            "apiKeyConfigured": bool(config.SETTINGS.llm_api_key),
            "deploymentName": config.SETTINGS.llm_deployment,
        },
        "fallback": "local_rules",
        "targetOutputs": ["requirements", "lineItems", "features", "source.preview"],
    }


# --------------------------------------------------------------------------- #
# Sample data (pre-populates the review screen on first load)
# --------------------------------------------------------------------------- #
def _load_sample() -> tuple[dict | None, str]:
    sample_dir = config.OUTPUT_DIR / SAMPLE_PROJECT_ID
    step1_path = sample_dir / "step1_extract_info.json"
    step2_path = sample_dir / "step2_create_boq.json"
    if not step1_path.exists() or not step2_path.exists():
        return None, ""

    step1 = io_utils.read_json(step1_path)
    step2 = io_utils.read_json(step2_path)

    src_folder = config.SAMPLE_SUBMISSIONS_DIR / SAMPLE_PROJECT_ID
    preview = _preview_for_folder(src_folder) if src_folder.exists() else ""
    if not preview:
        preview = "\n\n".join(
            e.get("evidence_text", "") for e in step1.get("extracted_evidence", [])
        )

    source_meta = {
        "fileName": f"Sample submission {SAMPLE_PROJECT_ID}",
        "fileType": "sample",
    }
    extraction, _ = build_extraction(step1, step2, preview, source_meta)
    return extraction, preview


@app.get("/api/v1/boq/sample")
async def get_sample_boq():
    extraction, _ = _load_sample()
    if extraction is None:
        return {
            "boqId": "BOQ-SAMPLE",
            "extractionSummary": "No sample output found. Upload a document to start.",
            "features": {},
            "requirements": [],
            "lineItems": [],
            "source": {"warnings": [], "preview": ""},
        }
    return extraction


@app.get("/api/v1/spec/sample")
async def get_sample_spec():
    extraction, preview = _load_sample()
    file_name = (
        extraction["source"]["fileName"]
        if extraction
        else "sample_submission"
    )
    return {"fileName": file_name, "content": preview or "No sample source available."}


@app.get("/api/v1/requirements/sample")
async def get_sample_requirements():
    """Return the full structured requirements for the sample submission.

    Mirrors the original POC endpoint. Used by any caller that wants raw
    requirements data without going through a file upload.
    """
    extraction, preview = _load_sample()
    if extraction is None:
        return {
            "requirements": [],
            "lineItems": [],
            "extractionSummary": "No sample output available.",
            "source": {"fileName": "none", "preview": ""},
        }
    return {
        "requirements": extraction.get("requirements", []),
        "lineItems": extraction.get("lineItems", []),
        "features": extraction.get("features", {}),
        "extractionSummary": extraction.get("extractionSummary", ""),
        "detectedScenarios": extraction.get("detectedScenarios", []),
        "missingInfoQuestions": extraction.get("missingInfoQuestions", []),
        "source": extraction.get("source", {}),
    }


# --------------------------------------------------------------------------- #
# Auxiliary POC features (hidden tabs in the UI; kept functional)
# --------------------------------------------------------------------------- #
class PricingInput(BaseModel):
    cost: float = 10000.0
    grossMarginPercent: float = 25.0
    discountPercent: float = 12.0
    currency: str = "USD"


def _final_unit_price(cost: float, margin_pct: float, discount_pct: float) -> float:
    margin = margin_pct / 100
    discount = discount_pct / 100
    if margin >= 1.0:
        raise HTTPException(400, "Gross margin must be less than 100%.")
    return round(cost / (1 - margin) * (1 - discount), 2)


@app.post("/api/v1/pricing/calculate")
async def calculate_price(payload: PricingInput):
    """Excel-style pricing formula applied to the sample BOQ line items."""
    unit_price = _final_unit_price(
        payload.cost, payload.grossMarginPercent, payload.discountPercent
    )

    extraction, _ = _load_sample()
    base_line_items = extraction["lineItems"] if extraction else []

    priced_items = []
    subtotal = 0.0
    for item in base_line_items:
        qty = item.get("quantity") or 1
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = 1.0
        net_unit = round(unit_price * (1 - payload.discountPercent / 100), 2)
        line_total = round(net_unit * qty, 2)
        subtotal += line_total
        priced_items.append(
            {
                "productCode": item.get("productCode", ""),
                "description": item.get("description", ""),
                "quantity": qty,
                "unitPrice": unit_price,
                "discountPercent": payload.discountPercent,
                "netUnitPrice": net_unit,
                "lineTotal": line_total,
            }
        )

    priced_boq = {
        "currency": payload.currency,
        "lineItems": priced_items,
        "subtotal": round(subtotal, 2),
        "grandTotal": round(subtotal, 2),
        "validityDays": 90,
        "paymentTerms": "Net 30",
    }

    return {
        "formulaResult": {
            "cost": payload.cost,
            "grossMarginPercent": payload.grossMarginPercent,
            "discountPercent": payload.discountPercent,
            "finalUnitPrice": unit_price,
            "formula": "cost / (1 - margin) * (1 - discount)",
        },
        "pricedBoq": priced_boq,
        "calculatedAt": _now(),
    }


@app.get("/api/v1/sync/status")
async def sync_status():
    return {
        "salesforce": {
            "connected": True,
            "endpoint": "https://mock.salesforce.com/api/cases",
            "mode": "mock",
            "lastSyncAt": _now(),
            "case": {
                "caseId": "SF-DEMO",
                "subject": "Qualitrol monitoring quotation",
                "account": "Demo Utility",
                "priority": "High",
                "customerTier": "Strategic",
                "region": "APAC",
            },
        },
        "docgen": {
            "templateReady": True,
            "templatePath": "python-docx (programmatic)",
            "engine": "python-docx",
            "conditionalRules": [
                {"rule": "Append Open Clarification Questions section when missing info exists", "active": True},
                {"rule": "Include BOQ line items with pricing when available", "active": True},
            ],
        },
        "checkedAt": _now(),
    }


# --------------------------------------------------------------------------- #
# In-memory store for generated documents (maps doc_id -> file path)
# In production this would use persistent storage.
# --------------------------------------------------------------------------- #
_generated_docs: dict[str, Path] = {}


@app.post("/api/v1/docgen/generate")
async def generate_doc():
    """Generate a real Word .docx quotation from the sample BOQ + pricing."""
    extraction, _ = _load_sample()

    # Build a priced BOQ from the sample extraction
    line_items = extraction["lineItems"] if extraction else []
    priced_items = []
    subtotal = 0.0
    for item in line_items:
        qty = float(item.get("quantity") or 1)
        unit_price = 12000.0  # default placeholder price
        net_unit = round(unit_price * 0.88, 2)  # 12% discount
        line_total = round(net_unit * qty, 2)
        subtotal += line_total
        priced_items.append({
            **item,
            "unitPrice": unit_price,
            "discountPercent": 12,
            "netUnitPrice": net_unit,
            "lineTotal": line_total,
        })

    priced_boq = {
        "caseReference": extraction.get("caseReference", "DRAFT") if extraction else "DRAFT",
        "currency": "USD",
        "lineItems": priced_items,
        "subtotal": round(subtotal, 2),
        "tax": 0.0,
        "grandTotal": round(subtotal, 2),
        "validityDays": 90,
        "paymentTerms": "Net 30",
        "missingInfoQuestions": extraction.get("missingInfoQuestions", []) if extraction else [],
    }

    doc_id = f"DOC-{uuid.uuid4().hex[:6].upper()}"
    out_dir = config.OUTPUT_DIR / "_docgen"
    out_path = out_dir / f"Qualitrol_Quotation_{doc_id}.docx"

    try:
        generated = await asyncio.to_thread(generate_quotation_docx, priced_boq, out_path)
        _generated_docs[doc_id] = generated
        file_size = generated.stat().st_size
    except Exception as exc:
        raise HTTPException(500, f"Document generation failed: {exc}") from exc

    return {
        "documentId": doc_id,
        "status": "document_generated",
        "fileName": generated.name,
        "documentUrl": f"/api/v1/docgen/download/{doc_id}",
        "fileSizeBytes": file_size,
        "clausesStripped": [],
        "clausesIncluded": (
            ["BOQ Line Items", "Pricing Summary"]
            + (["Open Clarification Questions"] if priced_boq["missingInfoQuestions"] else [])
        ),
        "generatedAt": _now(),
        "message": f"Word document assembled with {len(priced_items)} BOQ line(s).",
    }


@app.get("/api/v1/boq/excel/{case_id}")
async def download_boq_excel(case_id: str):
    """Download the generated BOQ Excel for a case (Step 2 output)."""
    safe = Path(case_id).name
    path = config.OUTPUT_DIR / safe / f"BOQ-{safe}.xlsx"
    if not path.exists():
        raise HTTPException(
            404,
            f"BOQ Excel for '{case_id}' not found. Re-run the analysis to "
            "generate it.",
        )
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


class BoqEditPayload(BaseModel):
    """Manually-edited BOQ line overrides from the Edit BOQ dialog."""

    lineItems: list[dict] = []


@app.post("/api/v1/boq/excel/{case_id}/regenerate")
async def regenerate_boq_excel(case_id: str, payload: BoqEditPayload):
    """Re-generate the BOQ Excel after manual product-code / qty edits.

    Loads the case's Step 2 output, applies the edited product code (mapped to
    ``product_model``) and quantity per BOQ line, and rewrites the Excel using
    the standard template so the downloadable file reflects the edits.
    """
    safe = Path(case_id).name
    out_dir = config.OUTPUT_DIR / safe
    step2_path = out_dir / "step2_create_boq.json"
    if not step2_path.exists():
        raise HTTPException(404, f"Step 2 output for '{case_id}' not found.")

    step2 = io_utils.read_json(step2_path)

    edits: dict = {}
    for item in payload.lineItems:
        ln = item.get("lineNumber")
        if ln is not None:
            edits[ln] = item

    for line in step2.get("draft_boq", []):
        edit = edits.get(line.get("boq_line"))
        if not edit:
            continue
        if edit.get("productCode") is not None:
            line["product_model"] = str(edit["productCode"]).strip()
        if "quantity" in edit and edit["quantity"] is not None:
            try:
                line["quantity"] = float(edit["quantity"])
            except (TypeError, ValueError):
                line["quantity"] = edit["quantity"]

    from qualitrol_core import boq_excel

    out_path = out_dir / f"BOQ-{safe}.xlsx"
    try:
        await asyncio.to_thread(boq_excel.generate_boq_excel, step2, out_path)
    except Exception as exc:  # pragma: no cover - surface a clean error
        raise HTTPException(500, f"BOQ Excel regeneration failed: {exc}") from exc

    return {
        "status": "ok",
        "boqExcelUrl": f"/api/v1/boq/excel/{safe}",
        "fileName": out_path.name,
    }


@app.get("/api/v1/docgen/download/{doc_id}")
async def download_doc(doc_id: str):
    """Download a previously generated quotation document."""
    path = _generated_docs.get(doc_id)
    if not path or not path.exists():
        raise HTTPException(404, f"Document '{doc_id}' not found or has expired.")
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )
