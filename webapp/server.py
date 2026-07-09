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
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import json as _json

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
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

from qualitrol_core import config, io_utils, spec_review  # noqa: E402
from qualitrol_core.document_parser import parse_project_folder  # noqa: E402
from webapp.docgen import generate_quotation_docx  # noqa: E402

# Step 3 (Configure & Quote / Margin Calculator) lives in its own root folder;
# load its modules by adding that directory to sys.path (the name has spaces).
if str(config.STEP3_DIR) not in sys.path:
    sys.path.insert(0, str(config.STEP3_DIR))
import catalog as margin_catalog  # noqa: E402  (Step 3 _ Configure & Quote/catalog.py)
from margin_calc import compute_margins  # noqa: E402
from margin_excel import generate_margin_xlsx  # noqa: E402

# Warm the product-catalog cache at import so the first /margin/catalog request
# returns immediately instead of parsing the JSON on the request path.
try:
    margin_catalog.load_catalog()
except Exception:  # pragma: no cover - defensive; endpoint will retry lazily
    pass

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


REQUIREMENTS_FEEDBACK_FILE = "requirements_feedback.json"


def _requirements_feedback_path(project_id: str) -> Path:
    return config.OUTPUT_DIR / project_id / REQUIREMENTS_FEEDBACK_FILE


def _load_requirements_feedback(project_id: str) -> dict:
    """Return stored per-item BOQ-line feedback as {key: {feedback, comments}}."""
    if not project_id:
        return {}
    path = _requirements_feedback_path(project_id)
    if not path.exists():
        return {}
    try:
        rec = io_utils.read_json(path)
    except Exception:
        return {}
    items = rec.get("items") if isinstance(rec, dict) else None
    return items if isinstance(items, dict) else {}


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
    # Per-item feedback (👍/👎 + comments) is stored per case and restored onto
    # each BOQ line so ratings survive reload/History revisits.
    line_feedback = _load_requirements_feedback(project_id)

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
        # Stable per-line key used to persist/restore this line's feedback.
        fb_key = f"L{boq.get('boq_line')}"
        stored_fb = line_feedback.get(fb_key, {})
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
                "quantityBasis": boq.get("quantity_basis") or "",
                "assumption": boq.get("assumption") or "",
                "confidence": boq.get("confidence", 0.0),
                "notes": boq.get("notes") or "",
                # Per-item feedback (mirrors the Draft BOQ "Rate this draft"):
                # 👍/👎 label + optional user comment, restored from storage.
                "feedbackKey": fb_key,
                "feedback": stored_fb.get("feedback", ""),
                "comments": stored_fb.get("comments", ""),
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
    context_notes: str = "",
):
    """Run Step 1 then Step 2 off the event loop (they may call the LLM)."""
    step1 = await asyncio.to_thread(
        step1_pipeline.run, upload_dir, project_id, output_dir, sld_filenames,
        context_notes,
    )
    step1_path = output_dir / "step1_extract_info.json"
    step2 = await asyncio.to_thread(step2_pipeline.run, step1_path, output_dir)
    return step1, step2


# --------------------------------------------------------------------------- #
# Background ingestion jobs (avoid the cloud gateway request timeout)
# --------------------------------------------------------------------------- #
# Analysis (LLM + SLD vision) can run for minutes, longer than Azure App
# Service's inbound request timeout (~230s), which would drop the connection
# ("Failed to fetch" in the browser). We therefore run it as a background task
# and let the client poll for the result. Job status is persisted on disk so a
# poll served by any gunicorn worker can read it.
_INGEST_JOBS: set = set()  # keep background task refs alive (avoid GC)


def _job_paths(output_dir: Path) -> tuple[Path, Path]:
    return output_dir / "_job.json", output_dir / "_result.json"


def _build_ingest_response(step1, step2, upload_dir, saved, skipped, files_count,
                           total_bytes, first_ext, project_id, output_dir, started):
    preview_text = _preview_for_folder(upload_dir)
    file_label = (
        saved[0] if len(saved) == 1 else f"{len(saved)} files"
    ) if saved else f"{files_count} files"
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
        "fileCount": files_count,
        "files": [{"fileName": n, "status": "parsed"} for n in saved]
        + [{"fileName": n, "status": "skipped"} for n in skipped],
        "extraction": extraction,
        "boq": extraction,
        "ingestedAt": _now(),
        "outputDir": str(output_dir),
    }


async def _process_job(upload_dir, project_id, output_dir, sld_set, saved, skipped,
                       files_count, total_bytes, first_ext, started,
                       context_notes=""):
    job_path, result_path = _job_paths(output_dir)
    try:
        step1, step2 = await _run_pipelines(
            upload_dir, project_id, output_dir, sld_filenames=sld_set or None,
            context_notes=context_notes,
        )
        payload = _build_ingest_response(
            step1, step2, upload_dir, saved, skipped, files_count,
            total_bytes, first_ext, project_id, output_dir, started,
        )
        result_path.write_text(_json.dumps(payload), encoding="utf-8")
        job_path.write_text(_json.dumps({"status": "done"}), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001  (surface a clean error to the UI)
        logging.error("Pipeline failed for project %s:\n%s", project_id,
                      traceback.format_exc())
        job_path.write_text(
            _json.dumps({"status": "error", "message": f"Pipeline failed: {exc}"}),
            encoding="utf-8",
        )


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
    context_notes: str = Form(""),
):
    """Ingest uploaded files and run the full Step 1 + Step 2 pipeline on them.

    Args:
        files: All uploaded files (project documents + SLD diagrams combined).
        sld_filenames: JSON-encoded list of filenames that came from the SLD
            upload zone.  Those files will have their ``doc_type`` forced to
            ``"Drawing / SLD"`` regardless of filename heuristics.
        context_notes: Free-text project context typed by the user in the Step 1
            UI. Passed to the Step 1 LLM extraction as extra context when
            generating requirements and evidence.
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

    # Kick off the analysis in the background and return a job id immediately so
    # the request stays well under the cloud gateway timeout. The client polls
    # GET /api/v1/ingest/result/{jobId} for the outcome.
    job_path, _ = _job_paths(output_dir)
    job_path.write_text(
        _json.dumps({"status": "processing", "startedAt": _now(),
                     "fileCount": len(files)}),
        encoding="utf-8",
    )
    task = asyncio.create_task(_process_job(
        upload_dir, project_id, output_dir, sld_set, saved, skipped,
        len(files), total_bytes, first_ext, started,
        context_notes=(context_notes or "").strip(),
    ))
    _INGEST_JOBS.add(task)
    task.add_done_callback(_INGEST_JOBS.discard)

    return {"jobId": project_id, "caseId": project_id, "status": "processing"}


@app.get("/api/v1/ingest/result/{job_id}")
async def ingest_result(job_id: str):
    """Poll a background ingestion job.

    Returns the full extraction payload when done, ``{"status": "processing"}``
    while it is still running, 404 for an unknown id, or 500 on failure.
    """
    safe = Path(job_id).name
    output_dir = config.OUTPUT_DIR / safe
    job_path, result_path = _job_paths(output_dir)
    if not job_path.exists():
        raise HTTPException(404, "Unknown job id.")
    try:
        job = _json.loads(job_path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "processing"}
    status = job.get("status")
    if status == "done" and result_path.exists():
        return _json.loads(result_path.read_text(encoding="utf-8"))
    if status == "error":
        raise HTTPException(500, job.get("message", "Pipeline failed."))
    return {"status": "processing"}


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
@lru_cache(maxsize=1)
def _load_sample() -> tuple[dict | None, str]:
    # Cached: the sample submission is static during a server run, but parsing
    # its source documents (parse_project_folder) costs several seconds. Without
    # this cache every /boq/sample and /spec/sample hit re-parsed from disk,
    # blocking the event loop and stalling other requests (e.g. feedback saves).
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
def get_sample_boq():
    # Sync def -> runs in the threadpool, so the (cached) sample parse never
    # blocks the async event loop / other in-flight requests.
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
def get_sample_spec():
    extraction, preview = _load_sample()
    file_name = (
        extraction["source"]["fileName"]
        if extraction
        else "sample_submission"
    )
    return {"fileName": file_name, "content": preview or "No sample source available."}


# --------------------------------------------------------------------------- #
# Spec Sections review  (spec-only requirement list, page/line, region image)
# --------------------------------------------------------------------------- #
def _safe_case_id(case_id: str) -> str:
    safe = "".join(c for c in case_id if c.isalnum() or c in ("-", "_"))
    if not safe:
        raise HTTPException(400, "Invalid case id.")
    return safe


def _resolve_case(case_id: str) -> tuple[str, Path, Path, list[Path]]:
    """Return (safe_id, step1_path, case_dir, pdf_search_dirs) for a case.

    Handles both real uploaded cases (outputs/<id>/uploads/*) and the bundled
    sample submission (its PDFs live under the sample submissions folder).
    """
    safe = _safe_case_id(case_id)
    case_dir = config.OUTPUT_DIR / safe
    step1_path = case_dir / "step1_extract_info.json"
    search_dirs = [case_dir / "uploads", case_dir]
    if safe == SAMPLE_PROJECT_ID:
        search_dirs.append(config.SAMPLE_SUBMISSIONS_DIR / SAMPLE_PROJECT_ID)
    if not step1_path.exists():
        raise HTTPException(404, "No stored analysis found for this case.")
    return safe, step1_path, case_dir, search_dirs


@app.get("/api/v1/spec/sections/{case_id}")
def get_spec_sections(case_id: str):
    """Spec-document-only requirement list for the 'Review Spec Sections' modal.

    Each item carries the related scenario, a short reason, its precise location
    (document / page / line) and a region-image URL. SLD evidence is excluded.
    """
    safe, step1_path, _case_dir, search_dirs = _resolve_case(case_id)
    step1 = io_utils.read_json(step1_path)
    items = spec_review.build_sections(step1)
    # Best-effort: refine line numbers + image availability from the source PDF.
    try:
        spec_review.enrich_lines(items, search_dirs)
    except Exception:  # noqa: BLE001 - never fail the list over image lookup
        for it in items:
            it.setdefault("hasImage", False)
    for it in items:
        it["imageUrl"] = f"/api/v1/spec/region/{safe}/{it['id']}"
    spec_docs = sorted(spec_review.spec_doc_names(step1))
    return {"caseId": safe, "documents": spec_docs, "items": items}


@app.get("/api/v1/spec/region/{case_id}/{evidence_id}")
def get_spec_region(case_id: str, evidence_id: str):
    """Cropped, highlighted screenshot of a spec requirement's source region."""
    safe, step1_path, _case_dir, search_dirs = _resolve_case(case_id)
    step1 = io_utils.read_json(step1_path)
    ev = next(
        (e for e in step1.get("extracted_evidence", [])
         if e.get("evidence_id") == evidence_id),
        None,
    )
    if not ev:
        raise HTTPException(404, "Unknown evidence id.")
    pdf = spec_review.find_source_pdf(ev.get("source_document", ""), search_dirs)
    if not pdf or pdf.suffix.lower() != ".pdf":
        raise HTTPException(404, "Source PDF not available for a region preview.")
    png = spec_review.render_region_png(
        pdf,
        spec_review._page_num(ev.get("location", "")),
        spec_review._term_from_notes(ev.get("notes", "")),
        ev.get("evidence_text", ""),
    )
    if not png:
        raise HTTPException(404, "Could not render a region preview for this item.")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "max-age=3600"})


def _rebuild_from_spec(safe: str, payload: dict) -> dict:
    """Regenerate the BOQ from user-edited spec requirements (+ SLD scope).

    Applies the reviewer's edits/deletions to the Step 1 evidence, drops any
    spec scenario that no longer has supporting evidence (unless it is also
    corroborated by the SLD), then re-runs Step 2 unchanged. Does NOT modify the
    BOQ generation logic — it only prunes/edits the Step 1 inputs and re-runs.
    """
    if safe == SAMPLE_PROJECT_ID:
        raise HTTPException(
            400,
            "The bundled sample can't be re-analysed. Upload your own documents "
            "to edit spec requirements and regenerate the BOQ.",
        )
    case_dir = config.OUTPUT_DIR / safe
    step1_path = case_dir / "step1_extract_info.json"
    if not step1_path.exists():
        raise HTTPException(404, "No stored analysis found for this case.")

    step1 = io_utils.read_json(step1_path)
    items = payload.get("items") or []
    deleted_ids = {i.get("id") for i in items if i.get("deleted")}
    edits = {i.get("id"): i for i in items if i.get("id") and not i.get("deleted")}

    # Scenarios that were supported by the spec BEFORE the reviewer's edits.
    original_spec_scenarios = spec_review.spec_scenario_ids(step1)
    sld_scenarios = spec_review.sld_scenario_ids(step1)

    # Preserve the pristine Step 1 once so the original analysis is recoverable.
    backup = case_dir / "step1_extract_info.original.json"
    if not backup.exists():
        try:
            io_utils.write_json(backup, step1)
        except Exception:
            pass

    # Apply edits + deletions to the evidence list.
    new_evidence: list[dict] = []
    for e in step1.get("extracted_evidence", []):
        eid = e.get("evidence_id")
        if eid in deleted_ids:
            continue
        ed = edits.get(eid)
        if ed:
            if ed.get("scenario"):
                e["scenario"] = str(ed["scenario"]).strip()
            if "assetType" in ed:
                e["asset_type"] = str(ed.get("assetType") or "").strip()
            if ed.get("snippet"):
                e["evidence_text"] = str(ed["snippet"]).strip()
            if ed.get("reason"):
                e["notes"] = str(ed["reason"]).strip()
        new_evidence.append(e)
    step1["extracted_evidence"] = new_evidence

    # Which spec scenarios survive the edit, and which should be dropped.
    surviving_spec = spec_review.spec_scenario_ids(step1)
    removed = {
        sid for sid in original_spec_scenarios
        if sid not in surviving_spec and sid not in sld_scenarios
    }

    if removed:
        step1["detected_scenarios"] = [
            d for d in step1.get("detected_scenarios", [])
            if d.get("scenario_id") not in removed
        ]
        step1["structured_requirements"] = [
            r for r in step1.get("structured_requirements", [])
            if r.get("scenario_id") not in removed
        ]

    io_utils.write_json(step1_path, step1)

    # Re-run Step 2 unchanged on the pruned/edited Step 1.
    step2 = step2_pipeline.run(step1_path, case_dir)

    uploads = case_dir / "uploads"
    preview = _preview_for_folder(uploads) if uploads.exists() else ""
    if not preview:
        preview = "\n\n".join(
            e.get("evidence_text", "") for e in step1.get("extracted_evidence", [])
        )
    extraction, _conf = build_extraction(
        step1, step2, preview,
        {"fileName": f"Case {safe}", "fileType": "revised"},
    )
    return {
        "status": "regenerated",
        "caseId": safe,
        "removedScenarios": sorted(removed),
        "extraction": extraction,
    }


@app.post("/api/v1/boq/{case_id}/rebuild-from-spec")
async def rebuild_boq_from_spec(case_id: str, request: Request):
    """Regenerate a case's BOQ from the reviewer-edited spec requirements."""
    safe = _safe_case_id(case_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return await asyncio.to_thread(_rebuild_from_spec, safe, payload)


# --------------------------------------------------------------------------- #
# Step 3 - Configure & Quote: margin calculator (compute / persist / export)
# --------------------------------------------------------------------------- #
MARGIN_DIR = config.OUTPUT_DIR / "_margin"


@app.get("/api/v1/margin/catalog")
async def margin_catalog_endpoint():
    """Product catalog (families -> models -> price/cost) for the calculator.

    Feeds the cascading family -> model pickers in Step 3 so selecting a model
    auto-fills its list price and material cost from the price list.
    """
    return margin_catalog.load_catalog()


@app.post("/api/v1/margin/calculate")
async def margin_calculate(request: Request):
    """Compute per-line, per-family and overall margins from calculator inputs."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a calculator payload.")
    return compute_margins(payload)


@app.post("/api/v1/margin/save")
async def margin_save(request: Request):
    """Persist a calculator record so it can be reloaded later."""
    try:
        record = await request.json()
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(record, dict):
        raise HTTPException(400, "Request body must be a calculator record.")

    record_id = str(record.get("id") or f"MGN-{uuid.uuid4().hex[:8].upper()}")
    record["id"] = record_id
    record["savedAt"] = _now()
    record["summary"] = compute_margins(record).get("summary", {})

    MARGIN_DIR.mkdir(parents=True, exist_ok=True)
    io_utils.write_json(MARGIN_DIR / f"{record_id}.json", record)
    return {"id": record_id, "savedAt": record["savedAt"], "summary": record["summary"]}


@app.get("/api/v1/margin/records")
async def margin_records():
    """List saved margin records (most recent first)."""
    if not MARGIN_DIR.exists():
        return {"records": []}
    out = []
    for path in MARGIN_DIR.glob("*.json"):
        try:
            rec = io_utils.read_json(path)
        except Exception:
            continue
        out.append({
            "id": rec.get("id", path.stem),
            "name": rec.get("name") or rec.get("caseReference") or path.stem,
            "caseReference": rec.get("caseReference", ""),
            "savedAt": rec.get("savedAt", ""),
            "summary": rec.get("summary", {}),
        })
    out.sort(key=lambda r: r.get("savedAt", ""), reverse=True)
    return {"records": out}


@app.get("/api/v1/margin/records/{record_id}")
async def margin_record(record_id: str):
    """Return a full saved margin record."""
    safe = "".join(c for c in record_id if c.isalnum() or c in ("-", "_"))
    path = MARGIN_DIR / f"{safe}.json"
    if not path.exists():
        raise HTTPException(404, f"Margin record '{record_id}' not found.")
    return io_utils.read_json(path)


@app.post("/api/v1/margin/export")
async def margin_export(request: Request):
    """Generate an .xlsx margin sheet (price-list layout) and return it."""
    try:
        record = await request.json()
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(record, dict):
        raise HTTPException(400, "Request body must be a calculator record.")

    ref = str(record.get("caseReference") or record.get("name") or record.get("id") or "DRAFT")
    safe_ref = "".join(c for c in ref if c.isalnum() or c in ("-", "_")) or "DRAFT"
    out_path = MARGIN_DIR / f"Qualitrol_Quote_{safe_ref}.xlsx"
    try:
        generated = await asyncio.to_thread(generate_margin_xlsx, record, out_path)
    except Exception as exc:
        logging.error("Margin export failed:\n%s", traceback.format_exc())
        raise HTTPException(500, f"Margin export failed: {exc}") from exc
    return FileResponse(
        path=str(generated),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=generated.name,
    )


@app.get("/api/v1/requirements/sample")
def get_sample_requirements():
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

    draft = step2.setdefault("draft_boq", [])
    existing_lines = {line.get("boq_line") for line in draft}

    for line in draft:
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

    # Append manually-added lines (a lineNumber that isn't an existing BOQ line).
    for ln, edit in edits.items():
        if ln in existing_lines:
            continue
        qty = edit.get("quantity")
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            pass
        draft.append({
            "boq_line": ln,
            "product_model": str(edit.get("productCode") or "").strip(),
            "product_description": "",
            "quantity": qty,
            "review_status": "Manual",
            "quantity_basis": "Manually added line",
        })

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


# --------------------------------------------------------------------------- #
# BOQ feedback (thumbs up/down + comments) — linked to the case / history ID
# so it can be collected later for ML / model tuning. Persisted server-side.
# --------------------------------------------------------------------------- #
FEEDBACK_DIR = config.OUTPUT_DIR / "_feedback"


@app.post("/api/v1/feedback/{case_id}")
async def submit_feedback(case_id: str, request: Request):
    """Store user feedback for a BOQ result, stamped onto every line item.

    Each item in the saved record carries a ``feedback`` (Positive/Negative) and
    ``comments`` field so the whole BOQ line list is ML-ready and traceable to
    this case (history) ID.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a feedback object.")

    safe = "".join(c for c in case_id if c.isalnum() or c in ("-", "_")) or "UNKNOWN"
    overall = str(payload.get("overallFeedback", "")).strip().title()
    if overall not in ("Positive", "Negative"):
        raise HTTPException(400, "overallFeedback must be 'Positive' or 'Negative'.")
    comments = str(payload.get("comments", "") or "").strip()

    items = []
    for it in (payload.get("items") or []):
        if not isinstance(it, dict):
            continue
        items.append({
            "lineNumber": it.get("lineNumber"),
            "productCode": it.get("productCode", ""),
            "description": it.get("description", ""),
            "quantity": it.get("quantity"),
            "unit": it.get("unit", ""),
            "feedback": overall,      # per-item label for ML / tuning
            "comments": comments,     # per-item comment
        })

    record = {
        "caseId": safe,
        "boqId": payload.get("boqId", f"BOQ-{safe}"),
        "overallFeedback": overall,
        "comments": comments,
        "submittedAt": _now(),
        "itemCount": len(items),
        "items": items,
    }

    # Latest feedback for this case, alongside its step1/step2 outputs.
    case_dir = config.OUTPUT_DIR / safe
    case_dir.mkdir(parents=True, exist_ok=True)
    io_utils.write_json(case_dir / "feedback.json", record)

    # Append to a global collection log (one JSON object per line) for ML.
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(FEEDBACK_DIR / "feedback_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass

    return {
        "status": "saved",
        "caseId": safe,
        "overallFeedback": overall,
        "submittedAt": record["submittedAt"],
    }


@app.get("/api/v1/feedback/{case_id}")
async def get_feedback(case_id: str):
    """Return the latest stored feedback for a case (to restore UI state)."""
    safe = "".join(c for c in case_id if c.isalnum() or c in ("-", "_"))
    path = config.OUTPUT_DIR / safe / "feedback.json"
    if not path.exists():
        return {"exists": False}
    try:
        rec = io_utils.read_json(path)
    except Exception:
        return {"exists": False}
    rec["exists"] = True
    return rec


# --------------------------------------------------------------------------- #
# Per-item Evidence List feedback (👍/👎 + comments) — mirrors the Draft BOQ
# "Rate this draft" feature, but scoped to each BOQ line so the whole evidence
# list is ML-ready and traceable to this case (history) ID.
# --------------------------------------------------------------------------- #
@app.post("/api/v1/requirements/{case_id}/feedback")
async def submit_requirement_feedback(case_id: str, request: Request):
    """Store 👍/👎 feedback (and optional comment) for a single BOQ line item."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a feedback object.")

    safe = "".join(c for c in case_id if c.isalnum() or c in ("-", "_")) or "UNKNOWN"
    key = str(payload.get("requirementId") or payload.get("feedbackKey") or "").strip()
    if not key:
        raise HTTPException(400, "requirementId (feedbackKey) is required.")
    feedback = str(payload.get("feedback", "")).strip().title()
    if feedback not in ("Positive", "Negative", ""):
        raise HTTPException(400, "feedback must be 'Positive', 'Negative' or ''.")
    comments = str(payload.get("comments", "") or "").strip()

    case_dir = config.OUTPUT_DIR / safe
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / REQUIREMENTS_FEEDBACK_FILE

    record: dict = {}
    if path.exists():
        try:
            record = io_utils.read_json(path)
        except Exception:
            record = {}
    if not isinstance(record, dict):
        record = {}
    record["caseId"] = safe
    items = record.get("items")
    if not isinstance(items, dict):
        items = {}
    entry = {
        "requirement_id": payload.get("requirementId", key),
        "requirement": payload.get("requirement", ""),
        "scenario_id": payload.get("scenarioId", ""),
        "feedback": feedback,
        "comments": comments,
        "updatedAt": _now(),
    }
    if feedback == "":
        items.pop(key, None)  # clearing the rating removes the stored entry
    else:
        items[key] = entry
    record["items"] = items
    record["updatedAt"] = _now()
    io_utils.write_json(path, record)

    # Append to a global collection log (one JSON object per line) for ML.
    if feedback:
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(
                FEEDBACK_DIR / "requirements_feedback_log.jsonl", "a", encoding="utf-8"
            ) as fh:
                fh.write(_json.dumps({"caseId": safe, **entry}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    return {"status": "saved", "caseId": safe, "requirementId": key,
            "feedback": feedback, "updatedAt": record["updatedAt"]}


@app.get("/api/v1/requirements/{case_id}/feedback")
async def get_requirement_feedback(case_id: str):
    """Return all stored per-item BOQ-line feedback for a case."""
    safe = "".join(c for c in case_id if c.isalnum() or c in ("-", "_"))
    items = _load_requirements_feedback(safe)
    return {"exists": bool(items), "caseId": safe, "items": items}


# --------------------------------------------------------------------------- #
# Regenerate a BOQ using the reviewer's per-line negative feedback (LLM re-pick)
# --------------------------------------------------------------------------- #
def _candidate_products_for_scenario(dp, scenario_id: str, limit: int = 14) -> list[dict]:
    """Catalog products the LLM may choose from for a scenario (deduped)."""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        families = dp.families_for_scenario(scenario_id)
    except Exception:
        families = []
    for fam in families:
        for prod in dp.products_for_family(fam.family_id):
            pid = prod.product_id
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append({
                "product_id": pid,
                "model": prod.model,
                "description": prod.description or fam.family_name,
                "family_name": fam.family_name,
            })
            if len(out) >= limit:
                return out
    return out


def _regenerate_boq_from_feedback(safe: str) -> dict:
    """Apply per-line negative feedback to a case's BOQ via the LLM re-pick.

    Returns a result dict; raises HTTPException on hard errors.
    """
    from qualitrol_core import llm, llm_extract, boq_excel
    from qualitrol_core.data_package import load_data_package

    case_dir = config.OUTPUT_DIR / safe
    step1_path = case_dir / "step1_extract_info.json"
    step2_path = case_dir / "step2_create_boq.json"
    if not step1_path.exists() or not step2_path.exists():
        raise HTTPException(404, "No stored analysis found for this case to regenerate.")

    step1 = io_utils.read_json(step1_path)
    step2 = io_utils.read_json(step2_path)
    feedback = _load_requirements_feedback(safe)

    draft = step2.get("draft_boq", [])
    # Collect lines that were thumbed-down (with the reviewer's comment).
    flagged: list[dict] = []
    dp = load_data_package()
    for line in draft:
        key = f"L{line.get('boq_line')}"
        fb = feedback.get(key) or {}
        if str(fb.get("feedback", "")).strip().title() != "Negative":
            continue
        sid = line.get("scenario_id", "")
        flagged.append({
            "feedbackKey": key,
            "product_id": line.get("product_id", ""),
            "product_model": line.get("product_model", ""),
            "product_description": line.get("product_description", ""),
            "scenario_id": sid,
            "scenario_name": _scenario_name(step1, sid),
            "quantity": line.get("quantity"),
            "unit": line.get("unit", ""),
            "quantity_basis": line.get("quantity_basis", ""),
            "feedback_comment": fb.get("comments", "") or "(no comment provided)",
            "candidates": _candidate_products_for_scenario(dp, sid),
        })

    if not flagged:
        raise HTTPException(
            400, "No 👎 feedback found on any BOQ line, so there is nothing to regenerate."
        )

    client = llm.get_client()
    if not client.available:
        raise HTTPException(
            400,
            "LLM is not configured, so feedback-based product re-selection is "
            "unavailable. Configure an LLM endpoint/key to use this feature.",
        )

    project_summary = {
        "scenarios": [
            {"scenario_id": d.get("scenario_id"), "name": d.get("scenario"),
             "confidence": d.get("confidence")}
            for d in step1.get("detected_scenarios", [])
        ],
    }
    decisions = llm_extract.regenerate_boq_lines(client, project_summary, flagged)
    if not decisions:
        raise HTTPException(502, "The LLM did not return a usable revision. Try again.")

    dec_by_key = {d["feedbackKey"]: d for d in decisions}
    prod_by_id = {p.product_id: p for p in dp.products.values()}

    revised: list[dict] = []
    changelog: list[dict] = []
    for line in draft:
        key = f"L{line.get('boq_line')}"
        dec = dec_by_key.get(key)
        if not dec:
            revised.append(line)
            continue
        action = dec["action"]
        rationale = dec.get("rationale", "")
        before = {"product_model": line.get("product_model"),
                  "quantity": line.get("quantity")}

        if action == "remove":
            changelog.append({"feedbackKey": key, "action": "remove",
                              "before": before, "after": None, "rationale": rationale})
            continue  # drop the line

        new_line = dict(line)
        if action == "replace":
            pid = dec.get("product_id", "")
            valid_ids = {c["product_id"] for c in
                         next((f["candidates"] for f in flagged if f["feedbackKey"] == key), [])}
            if pid and pid in valid_ids:
                prod = prod_by_id.get(pid)
                new_line["product_id"] = pid
                new_line["product_model"] = (prod.model if prod else dec.get("product_model")) or new_line.get("product_model")
                if prod and prod.description:
                    new_line["product_description"] = prod.description
            # else: keep current product (invalid/empty pick) but still note it.
        # Apply a corrected quantity whenever the LLM supplied one (both
        # 'adjust' and 'replace' can carry a revised quantity).
        if action in ("adjust", "replace") and dec.get("quantity") is not None:
            new_line["quantity"] = dec["quantity"]

        note = f"Revised from reviewer feedback ({action}): {rationale}".strip()
        new_line["assumption"] = note
        new_line["review_status"] = "Needs Review"
        existing_notes = (new_line.get("notes") or "").strip()
        new_line["notes"] = (existing_notes + " | " + note).strip(" |") if existing_notes else note
        revised.append(new_line)
        changelog.append({"feedbackKey": key, "action": action, "before": before,
                          "after": {"product_model": new_line.get("product_model"),
                                    "quantity": new_line.get("quantity")},
                          "rationale": rationale})

    # Renumber and refresh the summary.
    for i, line in enumerate(revised, start=1):
        line["boq_line"] = i
    step2["draft_boq"] = revised
    step2["boq_summary"] = {
        "total_lines": len(revised),
        "lines_needing_review": sum(1 for b in revised if b.get("review_status") == "Needs Review"),
        "lines_draft_ready": sum(1 for b in revised if b.get("review_status") == "Draft"),
    }
    step2["feedback_revision"] = {
        "revisedAt": _now(),
        "changes": changelog,
    }

    # Preserve the pristine original once, then persist the revised BOQ so
    # downstream (Excel export, Step 3 pricing, history) uses the revision.
    original_backup = case_dir / "step2_create_boq.original.json"
    if not original_backup.exists():
        try:
            io_utils.write_json(original_backup, io_utils.read_json(step2_path))
        except Exception:
            pass
    io_utils.write_json(step2_path, step2)

    # Append the revision to a global log for later ML/tuning.
    try:
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_DIR / "boq_regeneration_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"caseId": safe, "revisedAt": _now(),
                                  "changes": changelog}, ensure_ascii=False) + "\n")
    except OSError:
        pass

    # Consume the applied feedback so it isn't mis-attached to renumbered lines
    # on reload (the global logs above retain it for ML/tuning).
    fb_path = case_dir / REQUIREMENTS_FEEDBACK_FILE
    if fb_path.exists():
        try:
            rec = io_utils.read_json(fb_path)
            items = rec.get("items") if isinstance(rec, dict) else None
            if isinstance(items, dict):
                for f in flagged:
                    items.pop(f["feedbackKey"], None)
                rec["items"] = items
                rec["updatedAt"] = _now()
                io_utils.write_json(fb_path, rec)
        except Exception:
            pass

    # Re-export the finished BOQ Excel (best-effort).
    try:
        boq_excel.generate_boq_excel(step2, case_dir / f"BOQ-{safe}.xlsx")
    except Exception:
        pass

    # Rebuild the frontend extraction from the revised BOQ.
    uploads = case_dir / "uploads"
    preview = _preview_for_folder(uploads) if uploads.exists() else ""
    if not preview:
        preview = "\n\n".join(
            e.get("evidence_text", "") for e in step1.get("extracted_evidence", [])
        )
    extraction, _conf = build_extraction(
        step1, step2, preview, {"fileName": f"Case {safe}", "fileType": "revised"}
    )
    return {
        "status": "regenerated",
        "caseId": safe,
        "changes": changelog,
        "extraction": extraction,
    }


def _scenario_name(step1: dict, scenario_id: str) -> str:
    for d in step1.get("detected_scenarios", []):
        if d.get("scenario_id") == scenario_id:
            return d.get("scenario") or scenario_id
    return scenario_id


@app.post("/api/v1/boq/{case_id}/regenerate")
async def regenerate_boq(case_id: str):
    """Regenerate a case's BOQ by applying the reviewer's per-line 👎 feedback.

    Uses the LLM to remove out-of-scope lines, re-pick products from the catalog,
    or adjust quantities based on each line's feedback comment. Persists the
    revised BOQ (keeps the original as a backup) and re-exports the Excel.
    """
    safe = "".join(c for c in case_id if c.isalnum() or c in ("-", "_"))
    if not safe:
        raise HTTPException(400, "Invalid case id.")
    return await asyncio.to_thread(_regenerate_boq_from_feedback, safe)


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
