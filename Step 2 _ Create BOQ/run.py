"""CLI for Step 2 - Create BOQ.

Usage (from the repo root):
    python "Step 2 _ Create BOQ/run.py" outputs/00796547/step1_extract_info.json
    python "Step 2 _ Create BOQ/run.py" --project-id 00796547   # auto-locate Step 1 output

If neither a path nor --project-id is given, it defaults to the 00796547 sample.
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


def _resolve_step1_path(args) -> Path:
    if args.step1_path:
        return Path(args.step1_path)
    project_id = args.project_id or "00796547"
    return config.OUTPUT_DIR / project_id / "step1_extract_info.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualitrol Step 2 - Create BOQ")
    parser.add_argument("step1_path", nargs="?", default=None,
                        help="Path to the Step 1 JSON output.")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--out", default=None, help="Output directory.")
    args = parser.parse_args(argv)

    step1_path = _resolve_step1_path(args)
    if not step1_path.exists():
        parser.error(
            f"Step 1 output not found: {step1_path}\n"
            "Run Step 1 first, e.g.:\n"
            '  python "Step 1 _ Extract Info/run.py"'
        )

    result = pipeline.run(step1_path, args.out)

    print(f"\n=== Step 2 - Create BOQ :: project {result['project_id']} ===")
    print(f"Decision: {result['decision']}")
    print(f"Information complete: {result['information_complete']}")
    s = result["boq_summary"]
    print(f"BOQ lines: {s['total_lines']} "
          f"(draft-ready={s['lines_draft_ready']}, "
          f"needs-review={s['lines_needing_review']})")

    print(f"\nProduct matching ({len(result['product_matching'])}):")
    for m in result["product_matching"]:
        print(f"  - {m['family_id']:<14} {m['candidate_product_id']:<22} "
              f"score={m['match_score']:<4} {m['match_status']:<12} "
              f"[{m['parameter_match_result']}]")

    print(f"\nDraft BOQ ({len(result['draft_boq'])}):")
    for b in result["draft_boq"]:
        print(f"  #{b['boq_line']} {b['product_description']:<42} "
              f"qty={b['quantity']:<6} {b['unit']:<5} "
              f"[{b['review_status']}] conf={b['confidence']}")
        print(f"      basis: {b['quantity_basis']}")
        if b["notes"]:
            print(f"      notes: {b['notes']}")

    print(f"\nCompatibility guardrails triggered:")
    for f in result["compatibility_flags"]:
        if f["triggered"]:
            print(f"  - {f['rule_id']} [{f['severity']}] {f['scenario_id']}: {f['action']}")

    print(f"\nMissing-info questions ({len(result['missing_info_questions'])}):")
    for q in result["missing_info_questions"]:
        print(f"  - [{q['priority']}] {q['scenario_id']} :: {q['missing_item']}")
        print(f"      Q: {q['question']}")

    print(f"\nOutput written to: {result['_output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
