"""Build a static GitHub Pages site from the webapp + bundled sample data.

Usage:
    python scripts/build_github_pages.py

Output:
    docs/index.html
    docs/static/app.js
    docs/data/*.json
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SAMPLE_ID = "00796547"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from qualitrol_core import config, io_utils  # noqa: E402
from webapp.server import (  # noqa: E402
    FILE_TYPE_LABELS,
    build_extraction,
    _preview_for_folder,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_sample_payloads() -> tuple[dict, str]:
    bundled = REPO_ROOT / "scripts" / "github_pages_sample"
    sample_dir = config.OUTPUT_DIR / SAMPLE_ID
    step1_path = sample_dir / "step1_extract_info.json"
    step2_path = sample_dir / "step2_create_boq.json"
    if not step1_path.exists() or not step2_path.exists():
        step1_path = bundled / "step1_extract_info.json"
        step2_path = bundled / "step2_create_boq.json"
    if not step1_path.exists() or not step2_path.exists():
        raise FileNotFoundError(
            f"Sample outputs missing under {sample_dir} and {bundled}."
        )

    step1 = io_utils.read_json(step1_path)
    step2 = io_utils.read_json(step2_path)
    src_folder = config.SAMPLE_SUBMISSIONS_DIR / SAMPLE_ID
    preview = _preview_for_folder(src_folder) if src_folder.exists() else ""
    if not preview:
        preview = "\n\n".join(
            e.get("evidence_text", "") for e in step1.get("extracted_evidence", [])
        )
    source_meta = {
        "fileName": f"Sample submission {SAMPLE_ID}",
        "fileType": "sample",
    }
    extraction, _ = build_extraction(step1, step2, preview, source_meta)
    return extraction, preview


def _build_index_html() -> str:
    template = (REPO_ROOT / "webapp" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    banner = """
  <div style="background:#fff6df;border-bottom:1px solid #f0dca0;color:#9a5b00;padding:10px 32px;text-align:center;font-size:14px;">
    GitHub Pages demo — sample project 00796547. Upload &amp; full pipeline: clone repo and run <code style="background:#fff;padding:2px 6px;border-radius:4px;">python app.py</code>
  </div>
"""
    html = template.replace(
        '<html lang="en">',
        '<html lang="en" data-static-host="github-pages">',
    )
    html = html.replace(
        "Qualitrol Quotation Agent · FastAPI + local CSS · Demo Data via Harness Sandbox",
        "Qualitrol Quotation Agent · GitHub Pages static demo",
    )
    html = html.replace(
        '  <script src="/static/app.js"></script>',
        f"{banner}\n  <script src=\"static/app.js\"></script>",
    )
    return html


def build() -> Path:
    extraction, preview = _load_sample_payloads()

    if DOCS_DIR.exists():
        shutil.rmtree(DOCS_DIR, ignore_errors=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    (DOCS_DIR / "static").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "webapp" / "static" / "app.js", DOCS_DIR / "static" / "app.js")

    data_dir = DOCS_DIR / "data"
    _write_json(data_dir / "boq-sample.json", extraction)
    _write_json(
        data_dir / "spec-sample.json",
        {"fileName": extraction["source"]["fileName"], "content": preview or "No sample source available."},
    )
    _write_json(
        data_dir / "poc1-status.json",
        {
            "focus": "Step 1 (Extract Info) + Step 2 (Create BOQ)",
            "supportedFileTypes": {
                ext: FILE_TYPE_LABELS.get(ext, ext.lstrip(".").upper())
                for ext in sorted(config.SUPPORTED_DOC_EXTENSIONS)
            },
            "llm": {"configured": False},
            "fallback": "static_demo",
            "targetOutputs": ["requirements", "lineItems", "features", "source.preview"],
        },
    )
    _write_json(
        data_dir / "sync-status.json",
        {
            "salesforce": {
                "connected": True,
                "endpoint": "https://mock.salesforce.com/api/cases",
                "mode": "mock",
                "lastSyncAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
            "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    (DOCS_DIR / "index.html").write_text(_build_index_html(), encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"GitHub Pages site written to {DOCS_DIR}")
    return DOCS_DIR


if __name__ == "__main__":
    build()
