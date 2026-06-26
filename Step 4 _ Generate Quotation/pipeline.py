"""Step 4 - Generate Standard Quotation Document.

Consumes the Step 1 (extract info) and Step 2 (create BOQ) JSON outputs and
renders a customer-facing Qualitrol quotation Word document that follows the
standard MEA quotation layout.

    Step 1 JSON  ┐
                 ├─>  assemble quotation model  ->  quotation_docgen  ->  .docx
    Step 2 JSON  ┘

Pricing (Step 3) is not yet implemented, so all monetary values are emitted as
"TBD"; every other section is populated from the real run results.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import config, io_utils  # noqa: E402
from qualitrol_core.quotation_docgen import QuotationMeta, generate_quotation  # noqa: E402


def _load_step(path: Path, required_key: str, label: str) -> dict:
    data = io_utils.read_json(path)
    if required_key not in data:
        raise ValueError(f"{path} does not look like a {label} output "
                         f"(missing '{required_key}').")
    return data


def run(
    project_id: str | None = None,
    step1_path: str | Path | None = None,
    step2_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    output_name: str | None = None,
    meta_overrides: dict | None = None,
) -> dict:
    """Generate the quotation .docx.

    Args:
        project_id: used to auto-locate Step 1 / Step 2 outputs under outputs/.
        step1_path / step2_path: explicit JSON paths (override project_id).
        output_dir: where to write the .docx (defaults to the project folder).
        output_name: file name (defaults to ``Quotation-<project_id>.docx``).
        meta_overrides: dict of QuotationMeta kwargs (customer, project_name, ...).

    Returns:
        Summary dict including the output path.
    """
    if not (project_id or (step1_path and step2_path)):
        raise ValueError("Provide either project_id or both step1_path and step2_path.")

    base = config.OUTPUT_DIR / project_id if project_id else None
    s1 = Path(step1_path) if step1_path else base / "step1_extract_info.json"
    s2 = Path(step2_path) if step2_path else base / "step2_create_boq.json"

    if not s1.exists():
        raise FileNotFoundError(f"Step 1 output not found: {s1}")
    if not s2.exists():
        raise FileNotFoundError(f"Step 2 output not found: {s2}")

    step1 = _load_step(s1, "structured_requirements", "Step 1")
    step2 = _load_step(s2, "draft_boq", "Step 2")

    resolved_pid = step2.get("project_id") or step1.get("project_id") or project_id or "PROJECT"
    output_dir = Path(output_dir) if output_dir else (base or s2.parent)
    output_name = output_name or f"Quotation-{resolved_pid}.docx"
    out_path = Path(output_dir) / output_name

    meta = QuotationMeta(project_id=resolved_pid, **(meta_overrides or {}))
    written = generate_quotation(step1, step2, out_path, meta=meta)

    return {
        "project_id": resolved_pid,
        "step": "4_generate_quotation",
        "step1_path": str(s1),
        "step2_path": str(s2),
        "boq_lines": len(step2.get("draft_boq", [])),
        "open_questions": len(step2.get("missing_info_questions", [])),
        "decision": step2.get("decision", ""),
        "pricing_status": "TBD (Step 3 pricing layer not implemented)",
        "_output_path": str(written),
    }
