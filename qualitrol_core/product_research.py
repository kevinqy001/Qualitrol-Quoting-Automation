"""Product-catalog research helpers for Step 0.

Two responsibilities:
  1. Build the Tavily query plan (grounded in the controlled Product Family
     Master, sheet 06) used to discover real Qualitrol product models and their
     datasheet parameters.
  2. Structure the retrieved web text into rows for sheets 06/07/08
     (ProductFamily / Product / ProductParameter) using the LLM layer, mapping
     every parameter back to a controlled Metric ID (sheet 04).

All LLM steps fail safe (return None/empty) so Step 0 degrades to "query plan
only" when either Tavily or the LLM is unavailable.
"""

from __future__ import annotations

import json
from typing import Optional

from .data_package import DataPackage
from .schemas import Product, ProductFamily, ProductParameter

_PRIMARY = "qualitrolcorp.com"
_VALID_STATUS = {"Verified", "Candidate", "TBD"}


# --------------------------------------------------------------------------- #
# 1. Query plan (the drafted Tavily queries)
# --------------------------------------------------------------------------- #
def build_discovery_queries(primary_domain: str = _PRIMARY) -> list[dict]:
    """Broad portfolio-level queries to enumerate families & flagship products."""
    return [
        {"purpose": "Full product portfolio",
         "query": "Qualitrol full product portfolio list transformer and substation "
                  "condition monitoring products",
         "include_domains": [primary_domain]},
        {"purpose": "Product A-Z / catalog page",
         "query": "Qualitrol products list all monitoring product families",
         "include_domains": [primary_domain]},
        {"purpose": "Partial discharge range",
         "query": "Qualitrol partial discharge monitoring products GIS transformer "
                  "switchgear generator models",
         "include_domains": [primary_domain]},
        {"purpose": "DGA / oil monitoring range",
         "query": "Qualitrol dissolved gas analysis DGA online transformer oil "
                  "monitor product models",
         "include_domains": [primary_domain]},
        {"purpose": "Fault/PQ/PMU recorders",
         "query": "Qualitrol digital fault recorder power quality PMU disturbance "
                  "recorder product models",
         "include_domains": [primary_domain]},
        # One cross-web sweep (no domain lock) to catch distributor / datasheet PDFs.
        {"purpose": "Datasheets across the web",
         "query": "Qualitrol transformer monitoring product datasheet pdf specifications",
         "include_domains": []},
    ]


def build_family_queries(family: ProductFamily,
                         primary_domain: str = _PRIMARY) -> list[dict]:
    """Per-family queries: find the actual model(s) and a datasheet."""
    cap = (family.typical_capabilities or "").split(";")
    cap_hint = cap[0].strip() if cap and cap[0].strip() else family.family_name
    name = family.family_name
    return [
        {"purpose": f"{name}: model discovery",
         "family_id": family.family_id,
         "query": f"Qualitrol {name} product model name {cap_hint}",
         "include_domains": [primary_domain]},
        {"purpose": f"{name}: datasheet & specifications",
         "family_id": family.family_id,
         "query": f"Qualitrol {name} datasheet specifications {cap_hint}",
         "include_domains": []},
    ]


def build_parameter_query(model_or_family: str,
                          primary_domain: str = _PRIMARY) -> dict:
    """Query to pull the detailed parameter list for a specific model."""
    return {
        "purpose": f"{model_or_family}: parameters",
        "query": f"Qualitrol {model_or_family} technical specifications parameters "
                 f"channels protocols standards range",
        "include_domains": [],
    }


def build_full_query_plan(dp: DataPackage,
                          primary_domain: str = _PRIMARY) -> dict:
    """The complete drafted query plan (used both to run and to preview)."""
    families = list(dp.families.values())
    return {
        "primary_domain": primary_domain,
        "discovery_queries": build_discovery_queries(primary_domain),
        "family_queries": [
            q for fam in families for q in build_family_queries(fam, primary_domain)
        ],
        "family_count": len(families),
    }


# --------------------------------------------------------------------------- #
# 2. URL selection from Tavily search responses
# --------------------------------------------------------------------------- #
def collect_urls(search_response: dict, primary_domain: str = _PRIMARY,
                 limit: int = 4) -> list[str]:
    """Pick the most promising URLs, preferring the official domain & datasheets."""
    results = search_response.get("results", []) or []

    def score(r: dict) -> float:
        url = (r.get("url") or "").lower()
        s = float(r.get("score") or 0.0)
        if primary_domain in url:
            s += 1.0
        if url.endswith(".pdf") or "datasheet" in url or "/product" in url:
            s += 0.5
        return s

    ranked = sorted(results, key=score, reverse=True)
    urls: list[str] = []
    for r in ranked:
        url = r.get("url")
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _gather_text(search_response: dict, extract_response: dict,
                 max_chars: int = 12000) -> str:
    """Concatenate snippet + extracted content into a bounded context."""
    parts: list[str] = []
    for r in search_response.get("results", []) or []:
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        if content:
            parts.append(f"[SEARCH] {title} <{url}>\n{content}")
    for r in extract_response.get("results", []) or []:
        url = r.get("url", "")
        raw = r.get("raw_content") or r.get("content") or ""
        if raw:
            parts.append(f"[PAGE] <{url}>\n{raw}")
    blob = "\n\n".join(parts)
    return blob[:max_chars]


# --------------------------------------------------------------------------- #
# 3. LLM structuring into 06/07/08 rows
# --------------------------------------------------------------------------- #
def structure_family(llm_client, family: ProductFamily, dp: DataPackage,
                     context_text: str) -> Optional[dict]:
    """Turn retrieved web text into product + parameter rows for one family.

    Returns {"products": [...], "parameters": [...]} (raw dicts) or None.
    """
    if not llm_client.available or not context_text.strip():
        return None

    # Allowed metric vocabulary so parameters map to controlled IDs.
    metric_catalog = [
        {"metric_id": m.metric_id, "name": m.standard_name, "unit": m.unit}
        for m in dp.metrics.values()
    ]

    system = (
        "You are a Qualitrol product data analyst. From web search/datasheet text, "
        "extract REAL Qualitrol product models for the given product family and their "
        "key technical parameters. Map every parameter to one of the provided "
        "controlled Metric IDs; drop parameters that do not map. Never invent model "
        "names or values that are not supported by the text. Mark each product "
        "'Verified' only if a concrete model name appears in the text, else "
        "'Candidate'. Respond with STRICT JSON only."
    )
    user = (
        f"Product family: {family.family_name} (id={family.family_id})\n"
        f"Family capabilities: {family.typical_capabilities}\n"
        f"Applicable scenario IDs: {'; '.join(family.applicable_scenarios)}\n"
        f"Primary asset type: {family.primary_asset_type}\n\n"
        "Controlled Metric IDs (map parameters to these):\n"
        + json.dumps(metric_catalog, ensure_ascii=False)
        + "\n\nWeb text:\n" + context_text
        + "\n\nReturn JSON: {\"products\":[{\"product_model\":\"...\","
        "\"product_description\":\"...\",\"supported_standards\":\"...\","
        "\"communication_protocols\":\"...\",\"datasheet_url\":\"...\","
        "\"status\":\"Verified|Candidate\",\"parameters\":[{\"metric_id\":\"...\","
        "\"parameter_name\":\"...\",\"min_value\":null,\"max_value\":null,"
        "\"supported_value\":\"...\",\"unit\":\"...\"}]}]}"
    )

    data = llm_client.complete_json(system, user)
    if not isinstance(data, dict) or "products" not in data:
        return None

    valid_metric_ids = set(dp.metrics.keys())
    products: list[dict] = []
    parameters: list[dict] = []
    fam_seq = 0
    for prod in data.get("products", []):
        model = str(prod.get("product_model", "")).strip()
        if not model:
            continue
        fam_seq += 1
        product_id = f"PROD_{family.family_id}_{fam_seq:02d}"
        status = str(prod.get("status", "Candidate")).strip().title()
        if status not in _VALID_STATUS:
            status = "Candidate"
        products.append({
            "product_id": product_id,
            "model": model,
            "family_id": family.family_id,
            "family_name": family.family_name,
            "applicable_scenarios": list(family.applicable_scenarios),
            "primary_asset_type": family.primary_asset_type,
            "description": str(prod.get("product_description", "")).strip(),
            "supported_standards": str(prod.get("supported_standards", "")).strip(),
            "protocols": str(prod.get("communication_protocols", "")).strip(),
            "default_quantity_rule_id": family.default_quantity_rule_id,
            "datasheet_url": str(prod.get("datasheet_url", "")).strip(),
            "status": status,
            "notes": "Sourced via Tavily web research; verify before quoting.",
        })
        for prm in prod.get("parameters", []):
            mid = str(prm.get("metric_id", "")).strip()
            if mid not in valid_metric_ids:
                continue
            metric = dp.metrics.get(mid)
            parameters.append({
                "product_id": product_id,
                "model": model,
                "family_id": family.family_id,
                "metric_id": mid,
                "parameter_name": str(prm.get("parameter_name", "")).strip()
                or (metric.standard_name if metric else mid),
                "min_value": _num(prm.get("min_value")),
                "max_value": _num(prm.get("max_value")),
                "supported_value": str(prm.get("supported_value", "")).strip(),
                "unit": str(prm.get("unit", "")).strip()
                or (metric.unit if metric else ""),
                "match_type": "",
                "match_priority": "",
                "notes": "Tavily-sourced; verify against datasheet.",
            })
    if not products:
        return None
    return {"products": products, "parameters": parameters}


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_product(row: dict) -> Product:
    return Product(
        product_id=row["product_id"], model=row["model"],
        family_id=row["family_id"], family_name=row["family_name"],
        applicable_scenarios=row["applicable_scenarios"],
        primary_asset_type=row["primary_asset_type"],
        description=row["description"], supported_standards=row["supported_standards"],
        protocols=row["protocols"],
        default_quantity_rule_id=row["default_quantity_rule_id"],
        status=row["status"], notes=row["notes"],
    )


def to_parameter(row: dict) -> ProductParameter:
    return ProductParameter(
        product_id=row["product_id"], model=row["model"], family_id=row["family_id"],
        metric_id=row["metric_id"], parameter_name=row["parameter_name"],
        min_value=row["min_value"], max_value=row["max_value"],
        supported_value=row["supported_value"], unit=row["unit"],
        match_type=row["match_type"], match_priority=row["match_priority"],
        notes=row["notes"],
    )
