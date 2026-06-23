"""CLI for Step 0 - Tavily Product-Catalog Research.

Usage (from the repo root):
    python "Step 0 _ Tavily Search/run.py"                 # all families
    python "Step 0 _ Tavily Search/run.py" --families PF_DGA PF_GIS_PD
    python "Step 0 _ Tavily Search/run.py" --plan-only     # just print the queries
    python "Step 0 _ Tavily Search/run.py" --no-excel

Configure Tavily first (one of):
    setx TAVILY_API_KEY "tvly-..."           # then restart the shell
    or create qualitrol_core/tavily_config.local.json  {"api_key":"tvly-..."}
The Anthropic Foundry LLM (shared with Step 1/2) structures the results.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import config  # noqa: E402
from qualitrol_core.data_package import load_data_package  # noqa: E402
from qualitrol_core import product_research as pr  # noqa: E402

import pipeline  # noqa: E402  (sibling module)


def _print_plan() -> None:
    dp = load_data_package()
    plan = pr.build_full_query_plan(dp, config.SETTINGS.tavily_primary_domain)
    print(f"\n=== Step 0 query plan (primary domain: {plan['primary_domain']}) ===")
    print(f"\nDiscovery queries ({len(plan['discovery_queries'])}):")
    for q in plan["discovery_queries"]:
        dom = ", ".join(q["include_domains"]) or "(open web)"
        print(f"  - [{q['purpose']}] {q['query']}  ::  {dom}")
    print(f"\nPer-family queries ({len(plan['family_queries'])}):")
    for q in plan["family_queries"]:
        dom = ", ".join(q["include_domains"]) or "(open web)"
        print(f"  - {q['family_id']:<16} {q['query']}  ::  {dom}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualitrol Step 0 - Tavily Search")
    parser.add_argument("--families", nargs="*", default=None,
                        help="Limit to specific Product Family IDs.")
    parser.add_argument("--plan-only", action="store_true",
                        help="Only print the drafted Tavily query plan and exit.")
    parser.add_argument("--no-excel", action="store_true",
                        help="Skip writing the candidate .xlsx workbook.")
    parser.add_argument("--out", default=None, help="Output directory.")
    args = parser.parse_args(argv)

    if args.plan_only:
        _print_plan()
        return 0

    if not config.SETTINGS.tavily_available:
        print("\n[!] No Tavily API key configured - emitting the query PLAN only.")
        print("    Set TAVILY_API_KEY or create "
              "qualitrol_core/tavily_config.local.json to execute searches.\n")

    result = pipeline.run(
        output_dir=args.out, only_families=args.families,
        write_excel=not args.no_excel,
    )

    tv, s = result["tavily"], result["summary"]
    print(f"\n=== Step 0 - Tavily Search ===")
    print(f"Tavily available: {tv['available']} | executed: {tv['executed']} | "
          f"LLM: {result['llm']['available']}")
    print(f"Families: {s['families']} | researched: {s['families_researched']} | "
          f"products found: {s['products_found']} | parameters: {s['parameters_found']}")

    if result["products"]:
        print(f"\nDiscovered product models:")
        for p in result["products"]:
            print(f"  - [{p['status']:<9}] {p['family_id']:<16} {p['model']}")
            if p.get("datasheet_url"):
                print(f"      datasheet: {p['datasheet_url']}")

    if not tv["executed"]:
        plan = result["query_plan"]
        n = len(plan["discovery_queries"]) + len(plan["family_queries"])
        print(f"\nQuery plan ready: {n} queries "
              f"({len(plan['discovery_queries'])} discovery + "
              f"{len(plan['family_queries'])} per-family).")
        print("Run with --plan-only to see them, or configure Tavily to execute.")

    print(f"\nJSON written to: {result['_output_path']}")
    if result.get("_excel_path"):
        print(f"Candidate workbook: {result['_excel_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
