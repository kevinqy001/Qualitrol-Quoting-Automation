"""CLI for Step 1 - Extract Info.

Usage (from the repo root):
    python "Step 1 _ Extract Info/run.py" "Gemba Samples/Sample Customer Submissions/00796547"
    python "Step 1 _ Extract Info/run.py" <project_folder> --project-id 00796547 --out outputs/00796547

If no folder is given, it defaults to the 00796547 sample submission.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import config  # noqa: E402

import pipeline  # noqa: E402  (sibling module)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualitrol Step 1 - Extract Info")
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=str(config.SAMPLE_SUBMISSIONS_DIR / "00796547"),
        help="Customer submission folder (defaults to sample 00796547).",
    )
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--out", default=None, help="Output directory.")
    args = parser.parse_args(argv)

    result = pipeline.run(args.project_dir, args.project_id, args.out)

    print(f"\n=== Step 1 - Extract Info :: project {result['project_id']} ===")
    print(f"Documents parsed: {len(result['documents'])}")
    for doc in result["documents"]:
        print(f"  - {doc['file_name']}  [{doc['doc_type']}, {doc['segments']} segments]")

    print(f"\nDetected scenarios ({len(result['detected_scenarios'])}):")
    for det in result["detected_scenarios"]:
        print(
            f"  - {det['scenario_id']:<16} conf={det['confidence']:.2f} "
            f"({det['evidence_count']} evidence)  {det['scenario']}"
        )

    print(f"\nDrawing assets ({len(result['drawing_asset_list'])}):")
    for asset in result["drawing_asset_list"]:
        print(
            f"  - {asset['asset_type']:<12} qty={asset['quantity']:<6} "
            f"{asset['voltage_level']:<8} conf={asset['confidence']}  {asset['asset_tag']}"
        )

    print(f"\nStructured requirements ({len(result['structured_requirements'])}):")
    for req in result["structured_requirements"]:
        val = req["parameter_value"] or "(value TBD)"
        print(
            f"  - {req['requirement_id']} {req['scenario_id']:<16} "
            f"{req['metric_name']:<28} = {val} {req['unit']}  "
            f"[{req['requirement_type']}, conf={req['confidence']}]"
        )

    print(f"\nOutput written to: {result['_output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
