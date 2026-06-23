"""Step 0 - Tavily Product-Catalog Research.

Runs BEFORE Step 1/Step 2 to pre-populate the controlled product layer that the
data package leaves mostly empty:

    Product Family Master      (sheet 06)  - verify / enrich families
    Product Master Template    (sheet 07)  - discover real Qualitrol models
    Product Parameter Template (sheet 08)  - pull key parameters per model

Flow per family:
    build Tavily queries (grounded in sheet 06)
      -> Tavily search        (find official pages / datasheets)
      -> Tavily extract       (pull datasheet text)
      -> LLM structure        (Claude Opus 4.8 -> rows mapped to Metric IDs)
      -> aggregate catalog

Degradation:
  * No Tavily key  -> emits the full query PLAN only (still useful to run by hand).
  * No LLM key     -> keeps raw search results but cannot structure them.

Outputs (under outputs/_product_catalog/):
  * step0_product_catalog.json     - families/products/parameters + run metadata
  * Qualitrol_Product_Catalog.xlsx - candidate 06/07/08 sheets for human review
                                     (NEVER overwrites the master data package)
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import (  # noqa: E402
    config,
    io_utils,
    llm,
    product_research as pr,
    tavily_client,
)
from qualitrol_core.data_package import DataPackage, load_data_package  # noqa: E402

import catalog_excel  # noqa: E402  (sibling module)


def _research_family(tv, llm_client, family, dp: DataPackage,
                     primary_domain: str) -> dict:
    """Search + extract + structure one product family."""
    queries = pr.build_family_queries(family, primary_domain)
    merged_search = {"results": []}
    for q in queries:
        resp = tv.search(q["query"], include_domains=q.get("include_domains") or None)
        merged_search["results"].extend(resp.get("results", []) or [])

    urls = pr.collect_urls(merged_search, primary_domain,
                           limit=config.SETTINGS.tavily_max_urls_per_family)
    extract_resp = tv.extract(urls) if urls else {"results": []}

    context = pr._gather_text(merged_search, extract_resp)
    structured = pr.structure_family(llm_client, family, dp, context)
    return {
        "family_id": family.family_id,
        "family_name": family.family_name,
        "queries": [q["query"] for q in queries],
        "urls_used": urls,
        "structured": structured,
    }


def run(output_dir: str | Path | None = None,
        only_families: list[str] | None = None,
        write_excel: bool = True) -> dict:
    output_dir = Path(output_dir) if output_dir else config.OUTPUT_DIR / "_product_catalog"
    dp = load_data_package()
    primary = config.SETTINGS.tavily_primary_domain

    tv = tavily_client.get_client()
    llm_client = llm.get_client()

    families = list(dp.families.values())
    if only_families:
        wanted = set(only_families)
        families = [f for f in families if f.family_id in wanted]

    query_plan = pr.build_full_query_plan(dp, primary)

    products: list[dict] = []
    parameters: list[dict] = []
    per_family: list[dict] = []

    executed = tv.available
    if executed:
        for family in families:
            res = _research_family(tv, llm_client, family, dp, primary)
            per_family.append({
                "family_id": res["family_id"],
                "family_name": res["family_name"],
                "urls_used": res["urls_used"],
                "models_found": (
                    len(res["structured"]["products"]) if res["structured"] else 0
                ),
            })
            if res["structured"]:
                products.extend(res["structured"]["products"])
                parameters.extend(res["structured"]["parameters"])

    # Families pass-through (sheet 06 is already curated; we keep it as the base).
    families_out = [
        {
            "family_id": f.family_id,
            "family_name": f.family_name,
            "applicable_scenarios": f.applicable_scenarios,
            "primary_asset_type": f.primary_asset_type,
            "typical_capabilities": f.typical_capabilities,
            "default_quantity_rule_id": f.default_quantity_rule_id,
            "dependencies": f.dependencies,
            "notes": f.notes,
        }
        for f in dp.families.values()
    ]

    result = {
        "step": "0_tavily_search",
        "tavily": {
            "available": tv.available,
            "executed": executed,
            "primary_domain": primary,
            "search_depth": config.SETTINGS.tavily_search_depth,
        },
        "llm": {
            "available": llm_client.available,
            "model": config.SETTINGS.llm_deployment if llm_client.available else None,
        },
        "query_plan": query_plan,
        "summary": {
            "families": len(families_out),
            "families_researched": len(per_family),
            "products_found": len(products),
            "parameters_found": len(parameters),
        },
        "per_family": per_family,
        "product_families": families_out,
        "products": products,
        "product_parameters": parameters,
    }

    out_path = io_utils.write_json(
        Path(output_dir) / "step0_product_catalog.json", result
    )
    result["_output_path"] = str(out_path)

    if write_excel:
        xlsx_path = catalog_excel.write_catalog_workbook(
            Path(output_dir) / "Qualitrol_Product_Catalog.xlsx",
            families_out, products, parameters, query_plan,
        )
        result["_excel_path"] = str(xlsx_path)

    return result
