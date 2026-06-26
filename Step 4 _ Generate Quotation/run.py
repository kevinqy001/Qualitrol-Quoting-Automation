"""CLI for Step 4 - Generate Standard Quotation Document.

STANDARD: the output MUST match the official Qualitrol quotation template
(``Gemba Samples/1/1/773306/3. QUOTE/108704-749714.docx``) exactly — every
page, section, style/format and the full legal Terms & Conditions. The
generator clones that template and only fills the dynamic regions; pricing is
left blank until the Step 3 pricing layer exists. Override the template with
the QUALITROL_QUOTATION_TEMPLATE environment variable.

Usage (from the repo root):
    # Auto-locate Step 1/2 outputs under outputs/<project_id>/
    python "Step 4 _ Generate Quotation/run.py" --project-id IBRI-FINAL

    # With header metadata (recommended for a presentable draft)
    python "Step 4 _ Generate Quotation/run.py" --project-id IBRI-FINAL \
        --customer "GCCIA" --project-name "Ibri 400kV Substation" \
        --location "Oman" --tender-ref "Tender 319/2024" --sfdc 773306

    # Explicit JSON paths
    python "Step 4 _ Generate Quotation/run.py" \
        --step1 outputs/IBRI-FINAL/step1_extract_info.json \
        --step2 outputs/IBRI-FINAL/step2_create_boq.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pipeline  # noqa: E402  (sibling module)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualitrol Step 4 - Generate Quotation")
    parser.add_argument("--project-id", default=None,
                        help="Locate Step 1/2 outputs under outputs/<project_id>/.")
    parser.add_argument("--step1", default=None, help="Path to Step 1 JSON.")
    parser.add_argument("--step2", default=None, help="Path to Step 2 JSON.")
    parser.add_argument("--out", default=None, help="Output directory.")
    parser.add_argument("--name", default=None, help="Output .docx file name.")

    # Optional header metadata.
    parser.add_argument("--quote-number", default=None)
    parser.add_argument("--customer", default=None)
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--location", default=None)
    parser.add_argument("--tender-ref", default=None)
    parser.add_argument("--sfdc", dest="sfdc_number", default=None)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--validity-days", type=int, default=90)
    parser.add_argument("--sales-name", dest="sales_name", default=None)
    parser.add_argument("--sales-email", dest="sales_email", default=None)

    args = parser.parse_args(argv)

    if not (args.project_id or (args.step1 and args.step2)):
        parser.error("Provide --project-id, or both --step1 and --step2.")

    meta_keys = (
        "quote_number", "customer", "project_name", "location",
        "tender_ref", "sfdc_number", "currency", "validity_days",
        "sales_name", "sales_email",
    )
    meta_overrides = {}
    for key in meta_keys:
        val = getattr(args, key, None)
        # currency/validity_days always have defaults; others only if set.
        if val is not None:
            meta_overrides[key] = val

    result = pipeline.run(
        project_id=args.project_id,
        step1_path=args.step1,
        step2_path=args.step2,
        output_dir=args.out,
        output_name=args.name,
        meta_overrides=meta_overrides,
    )

    print(f"\n=== Step 4 - Generate Quotation :: project {result['project_id']} ===")
    print(f"BOQ lines      : {result['boq_lines']}")
    print(f"Open questions : {result['open_questions']}")
    print(f"Step 2 decision: {result['decision']}")
    print(f"Pricing        : {result['pricing_status']}")
    print(f"\nQuotation written to: {result['_output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
