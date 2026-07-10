"""Step 0b - PDF Datasheet Extraction.

Turns the official Qualitrol product manuals downloaded under
``Preparation/Qualitrol Product/`` into candidate rows for the controlled
product layer of ``Qualitrol_BOQ_Matching_Data_Package.xlsx``:

    Product Master Template    (sheet 07)  - real models + description/standards/protocols
    Product Parameter Template (sheet 08)  - key parameters mapped to Metric IDs (sheet 04)

Unlike Step 0 (Tavily web search, lower accuracy), this reads the *authoritative*
datasheet text layer directly, so extracted models and parameters carry a
file-name + page-number evidence trail.

Flow per PDF:
    pypdf text extraction (page-tagged)
      -> LLM structuring     (GPT "bulk" role -> models + parameters JSON;
                              falls back to Claude when GPT is not configured)
      -> map to Metric IDs   (controlled sheet 04; unmapped -> Unmapped list)
      -> aggregate candidate catalog

Degradation:
  * No LLM key -> nothing to structure; the run reports which PDFs it *would*
    have processed so it is still useful as a plan.

Safety: NEVER overwrites the master data package. Everything is written to a new
candidate workbook under ``outputs/_pdf_catalog/`` with Status=Candidate and
Source=Datasheet PDF for human review before merge.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import config, io_utils, llm  # noqa: E402
from qualitrol_core.data_package import DataPackage, load_data_package  # noqa: E402
from qualitrol_core.document_parser import _parse_pdf  # noqa: E402

# Root folder of the downloaded product manuals.
PDF_ROOT = _REPO_ROOT / "Preparation" / "Qualitrol Product"

# Bound the text handed to the LLM per datasheet. Datasheets run ~3k-10k chars;
# 24k leaves generous room for the few multi-page ones without wasting tokens.
_MAX_CHARS_PER_PDF = 24000

_HASH_RE = re.compile(r"_[0-9a-f]{8}$")


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class PdfResult:
    """Everything extracted from one datasheet PDF."""

    source_file: str            # relative path under PDF_ROOT
    category: str               # parent folder (e.g. Transformer_Monitoring)
    file_hash: str              # 8-hex datasheet id embedded in the file name
    pages: int = 0
    products: list[dict] = field(default_factory=list)      # -> sheet 07
    parameters: list[dict] = field(default_factory=list)    # -> sheet 08 (mapped)
    unmapped: list[dict] = field(default_factory=list)       # params that don't map
    error: str = ""


# --------------------------------------------------------------------------- #
# PDF discovery / selection
# --------------------------------------------------------------------------- #
def _file_hash(name: str) -> str:
    stem = Path(name).stem
    m = _HASH_RE.search(stem)
    return m.group(0)[1:] if m else ""


def list_pdfs(category: Optional[str] = None) -> list[Path]:
    """All datasheet PDFs under the manual root (optionally one category)."""
    root = PDF_ROOT / category if category else PDF_ROOT
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.pdf") if p.is_file())


def dedupe_by_hash(paths: list[Path]) -> list[Path]:
    """Many products share one datasheet (same 8-hex id). Keep one file per id.

    Files without a recognisable hash are always kept (treated as unique).
    """
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        h = _file_hash(p.name)
        if h and h in seen:
            continue
        if h:
            seen.add(h)
        out.append(p)
    return out


# Curated pilot: a representative spread across every category & product type so
# the schema and accuracy can be validated before a full run.
PILOT_FILES = [
    r"Transformer_Monitoring\TM8_8bc233a4.pdf",                                   # online DGA monitor
    r"Transformer_Monitoring\AKM345_Gen3_Oil_and_Winding_Temperature_Indicator_09e05650.pdf",  # OTI/WTI
    r"Transformer_Monitoring\930_Electronic_Pressure_Monitor_f58007ab.pdf",       # electronic pressure monitor
    r"Transformer_Monitoring\ITM_509_369c448e.pdf",                               # intelligent transformer monitor
    r"Transformer_Monitoring\Neoptix_T2_Transformer_Temperature_Probe_7024aafc.pdf",  # fiber optic probe
    r"Transformer_Monitoring\LPRD_Large_Pressure_Relief_Devices_72428d97.pdf",    # mechanical PRD
    r"Bushings\QTMS_b041f478.pdf",                                                # bushing/transformer monitor system
    r"Breakers\QBCM_9a27f030.pdf",                                                # circuit breaker monitor
    r"Gas_Insulated_Switchgear\PDMG_-_RH_-_Gen_3_c5bdb2c6.pdf",                   # GIS PD monitor
    r"Generator_Solutions\EL_CID_8fe18258.pdf",                                   # core lamination tester
    r"Generator_Solutions\PDA-IV_e8515c86.pdf",                                   # PD analyzer
    r"The_Power_Grid\IDM+_22da82f9.pdf",                                          # fault/disturbance recorder
]


def resolve_pilot() -> list[Path]:
    out: list[Path] = []
    for rel in PILOT_FILES:
        p = PDF_ROOT / rel
        if p.exists():
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def extract_text(path: Path, max_chars: int = _MAX_CHARS_PER_PDF) -> tuple[str, int]:
    """Return (page-tagged text, page count) for a datasheet PDF."""
    segments = _parse_pdf(path)
    parts: list[str] = []
    for seg in segments:
        parts.append(f"[{seg.location.upper()}]\n{seg.text}")
    blob = "\n\n".join(parts)
    return blob[:max_chars], len(segments)


# --------------------------------------------------------------------------- #
# LLM structuring
# --------------------------------------------------------------------------- #
def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_catalog(dp: DataPackage) -> list[dict]:
    return [
        {"metric_id": m.metric_id, "name": m.standard_name,
         "unit": m.unit, "applies_to": m.applies_to}
        for m in dp.metrics.values()
    ]


def _family_catalog(dp: DataPackage) -> list[dict]:
    return [
        {"family_id": f.family_id, "family_name": f.family_name,
         "primary_asset_type": f.primary_asset_type,
         "capabilities": f.typical_capabilities}
        for f in dp.families.values()
    ]


_SYSTEM = (
    "You are a Qualitrol product data analyst. You are given the raw TEXT of ONE "
    "official Qualitrol product datasheet/manual (page-tagged), plus a controlled "
    "list of Product Family IDs and a controlled list of Metric IDs.\n\n"
    "Extract the product model(s) this datasheet documents and their key technical "
    "parameters. Rules:\n"
    "- Only use facts present in the datasheet text. NEVER invent models or values.\n"
    "- Prefer the concrete model/series name as printed (e.g. 'Serveron TM8', "
    "'AKM345', 'QBCM', 'ITM509'). If a datasheet covers several models, list each.\n"
    "- Choose the best family_id from the controlled families, or 'UNKNOWN' if none fit.\n"
    "- For every parameter, try to map it to one controlled metric_id. If none fits, "
    "set metric_id to 'UNMAPPED' and fill proposed_metric_name.\n"
    "- Capture numeric ranges as min_value/max_value when the datasheet gives a range; "
    "otherwise put the literal spec in supported_value.\n"
    "- For each parameter include the source page number (integer) and a short "
    "evidence quote (<=15 words) copied from the text.\n"
    "- Focus on quote-relevant specs: channels/inputs, measured quantities & ranges, "
    "accuracy, communication protocols, supported standards, power supply, enclosure/IP "
    "rating, operating temperature, mounting. Skip marketing prose.\n"
    "Respond with STRICT JSON only, no markdown."
)


def _build_user(text: str, families: list[dict], metrics: list[dict],
                file_name: str) -> str:
    return (
        f"Datasheet file name: {file_name}\n\n"
        "Controlled Product Family IDs:\n"
        + json.dumps(families, ensure_ascii=False)
        + "\n\nControlled Metric IDs (map parameters to these):\n"
        + json.dumps(metrics, ensure_ascii=False)
        + "\n\nDatasheet text (page-tagged):\n" + text
        + "\n\nReturn JSON exactly of the form:\n"
        '{"products":[{"model":"...","family_id":"...","description":"...",'
        '"supported_standards":"...","communication_protocols":"...",'
        '"parameters":[{"metric_id":"...","proposed_metric_name":"",'
        '"parameter_name":"...","min_value":null,"max_value":null,'
        '"supported_value":"...","unit":"...","page":1,"evidence":"..."}]}]}'
    )


def _slug(model: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_").upper()
    return s or "MODEL"


def structure_pdf(llm_client, dp: DataPackage, path: Path) -> PdfResult:
    """Extract + LLM-structure a single datasheet into candidate rows."""
    rel = str(path.relative_to(PDF_ROOT))
    category = path.parent.name
    result = PdfResult(source_file=rel, category=category,
                       file_hash=_file_hash(path.name))

    try:
        text, npages = extract_text(path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
        result.error = f"text extraction failed: {exc}"
        return result
    result.pages = npages
    if not text.strip():
        result.error = "no extractable text layer (possibly scanned image)"
        return result
    if not llm_client.available:
        result.error = "LLM unavailable"
        return result

    families = _family_catalog(dp)
    metrics = _metric_catalog(dp)
    user_prompt = _build_user(text, families, metrics, path.name)
    # First pass at a normal budget; retry once with a larger budget because a
    # parameter-heavy datasheet can truncate the JSON mid-array (-> unparseable).
    data = llm_client.complete_json(_SYSTEM, user_prompt, max_tokens=4096)
    if not isinstance(data, dict) or "products" not in data:
        data = llm_client.complete_json(_SYSTEM, user_prompt, max_tokens=8192)
    if not isinstance(data, dict) or "products" not in data:
        result.error = "LLM returned no structured products"
        return result

    valid_metric_ids = set(dp.metrics.keys())
    valid_family_ids = set(dp.families.keys())
    used_slugs: dict[str, int] = {}

    for prod in data.get("products", []):
        model = str(prod.get("model", "")).strip()
        if not model:
            continue
        slug = _slug(model)
        used_slugs[slug] = used_slugs.get(slug, 0) + 1
        product_id = f"PROD_PDF_{slug}"
        if used_slugs[slug] > 1:
            product_id = f"{product_id}_{used_slugs[slug]}"

        family_id = str(prod.get("family_id", "")).strip()
        if family_id not in valid_family_ids:
            family_id = "UNKNOWN"
        fam = dp.families.get(family_id)

        result.products.append({
            "product_id": product_id,
            "model": model,
            "family_id": family_id,
            "family_name": fam.family_name if fam else "",
            "applicable_scenarios": list(fam.applicable_scenarios) if fam else [],
            "primary_asset_type": fam.primary_asset_type if fam else "",
            "description": str(prod.get("description", "")).strip(),
            "supported_standards": str(prod.get("supported_standards", "")).strip(),
            "protocols": str(prod.get("communication_protocols", "")).strip(),
            "default_quantity_rule_id": fam.default_quantity_rule_id if fam else "",
            "datasheet_url": "",
            "source_file": rel,
            "status": "Candidate",
            "notes": f"Extracted from datasheet PDF '{rel}'; verify before quoting.",
        })

        for prm in prod.get("parameters", []):
            mid = str(prm.get("metric_id", "")).strip()
            pname = str(prm.get("parameter_name", "")).strip()
            row = {
                "product_id": product_id,
                "model": model,
                "family_id": family_id,
                "metric_id": mid,
                "parameter_name": pname,
                "min_value": _num(prm.get("min_value")),
                "max_value": _num(prm.get("max_value")),
                "supported_value": str(prm.get("supported_value", "")).strip(),
                "unit": str(prm.get("unit", "")).strip(),
                "page": prm.get("page"),
                "evidence": str(prm.get("evidence", "")).strip(),
                "source_file": rel,
            }
            if mid in valid_metric_ids:
                metric = dp.metrics.get(mid)
                if not row["parameter_name"]:
                    row["parameter_name"] = metric.standard_name if metric else mid
                if not row["unit"] and metric:
                    row["unit"] = metric.unit
                result.parameters.append(row)
            else:
                row["proposed_metric_name"] = str(
                    prm.get("proposed_metric_name", "")
                ).strip()
                result.unmapped.append(row)

    if not result.products:
        result.error = "no models extracted"
    return result


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(paths: list[Path], output_dir: str | Path | None = None,
        write_excel: bool = True) -> dict:
    """Extract a list of datasheet PDFs into a candidate catalog."""
    from catalog_excel import write_pdf_catalog_workbook  # sibling module

    output_dir = Path(output_dir) if output_dir else config.OUTPUT_DIR / "_pdf_catalog"
    dp = load_data_package()
    # High-volume, well-bounded structured extraction -> GPT ("bulk" role);
    # falls back to Claude when no GPT endpoint is configured.
    llm_client = llm.get_client(role="bulk")

    results: list[PdfResult] = []
    for i, path in enumerate(paths, start=1):
        rel = str(path.relative_to(PDF_ROOT))
        print(f"  [{i}/{len(paths)}] {rel}", flush=True)
        try:
            results.append(structure_pdf(llm_client, dp, path))
        except Exception as exc:  # noqa: BLE001 - final safety net per file
            results.append(PdfResult(
                source_file=rel, category=path.parent.name,
                file_hash=_file_hash(path.name), error=f"unexpected: {exc}",
            ))

    products = [p for r in results for p in r.products]
    parameters = [p for r in results for p in r.parameters]
    unmapped = [u for r in results for u in r.unmapped]
    source_index = [
        {"source_file": r.source_file, "category": r.category,
         "file_hash": r.file_hash, "pages": r.pages,
         "models": "; ".join(p["model"] for p in r.products),
         "n_models": len(r.products), "n_parameters": len(r.parameters),
         "n_unmapped": len(r.unmapped), "error": r.error}
        for r in results
    ]

    result = {
        "step": "0b_pdf_datasheets",
        "llm": {
            "available": llm_client.available,
            "model": getattr(llm_client, "deployment", None) if llm_client.available else None,
        },
        "summary": {
            "pdfs_processed": len(results),
            "pdfs_with_errors": sum(1 for r in results if r.error),
            "products_found": len(products),
            "parameters_mapped": len(parameters),
            "parameters_unmapped": len(unmapped),
        },
        "source_index": source_index,
        "products": products,
        "product_parameters": parameters,
        "unmapped_parameters": unmapped,
    }

    out_path = io_utils.write_json(
        Path(output_dir) / "step0b_pdf_catalog.json", result
    )
    result["_output_path"] = str(out_path)

    if write_excel:
        xlsx_path = write_pdf_catalog_workbook(
            Path(output_dir) / "Qualitrol_Datasheet_Catalog.xlsx",
            products, parameters, unmapped, source_index, result["summary"],
        )
        result["_excel_path"] = str(xlsx_path)

    return result
