"""CLI for Step 0b - PDF Datasheet Extraction.

Examples (from repo root):

    # Pilot: ~12 representative datasheets across every category (default)
    python "Step 0b _ PDF Datasheets/run.py"

    # Just list what the pilot / a scope would process (no LLM calls)
    python "Step 0b _ PDF Datasheets/run.py" --plan-only

    # One full category (deduped by shared datasheet id)
    python "Step 0b _ PDF Datasheets/run.py" --category Transformer_Monitoring

    # Everything (deduped) - the full catalog build
    python "Step 0b _ PDF Datasheets/run.py" --all

    # Specific files (relative to Preparation/Qualitrol Product)
    python "Step 0b _ PDF Datasheets/run.py" --files "Breakers\\QBCM_9a27f030.pdf"

    # Cap the number of PDFs (handy for a quick smoke test)
    python "Step 0b _ PDF Datasheets/run.py" --all --limit 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Make sibling modules importable when invoked by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline  # noqa: E402


def _select(args) -> list[Path]:
    if args.files:
        out = []
        for rel in args.files:
            p = pipeline.PDF_ROOT / rel
            if p.exists():
                out.append(p)
            else:
                print(f"  ! not found, skipped: {rel}")
        return out
    if args.all:
        return pipeline.dedupe_by_hash(pipeline.list_pdfs())
    if args.category:
        return pipeline.dedupe_by_hash(pipeline.list_pdfs(args.category))
    return pipeline.resolve_pilot()


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract Qualitrol datasheet PDFs into a candidate catalog.")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="process all datasheets (deduped)")
    scope.add_argument("--category", help="process one category folder (deduped)")
    scope.add_argument("--files", nargs="+", help="specific files relative to the manual root")
    ap.add_argument("--limit", type=int, default=0, help="cap number of PDFs processed")
    ap.add_argument("--plan-only", action="store_true", help="list selected PDFs; make no LLM calls")
    ap.add_argument("--output-dir", help="override output directory")
    args = ap.parse_args()

    paths = _select(args)
    if args.limit and args.limit > 0:
        paths = paths[: args.limit]

    if not paths:
        print("No PDFs selected. Check --category / --files or the manual folder path.")
        return

    print(f"Selected {len(paths)} datasheet PDF(s):")
    for p in paths:
        print(f"  - {p.relative_to(pipeline.PDF_ROOT)}")

    if args.plan_only:
        print("\n--plan-only: no extraction performed.")
        return

    print("\nExtracting (this calls the LLM once per PDF)...")
    result = pipeline.run(paths, output_dir=args.output_dir)

    s = result["summary"]
    print("\n=== Summary ===")
    print(f"  LLM: available={result['llm']['available']} model={result['llm']['model']}")
    print(f"  PDFs processed   : {s['pdfs_processed']} (errors: {s['pdfs_with_errors']})")
    print(f"  Models found     : {s['products_found']}")
    print(f"  Params mapped    : {s['parameters_mapped']}")
    print(f"  Params unmapped  : {s['parameters_unmapped']}")
    print(f"\n  JSON : {result.get('_output_path')}")
    print(f"  Excel: {result.get('_excel_path')}")


if __name__ == "__main__":
    main()
