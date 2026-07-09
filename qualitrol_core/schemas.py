"""Typed data models for the Qualitrol BOQ matching pipeline.

The dataclasses mirror the controlled reference tables and the AI output
templates in ``Qualitrol_BOQ_Matching_Data_Package.xlsx`` so that pipeline
outputs can be serialized straight back into those sheet schemas.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


# --------------------------------------------------------------------------- #
# Controlled reference layer (loaded from the data package)
# --------------------------------------------------------------------------- #
@dataclass
class Scenario:
    """Row of 03_Scenario_Master."""

    scenario_id: str
    category: str = ""
    application_scenario: str = ""
    asset_type: str = ""
    typical_metrics: str = ""
    keywords: list[str] = field(default_factory=list)
    related_product_families: list[str] = field(default_factory=list)
    quantity_basis: str = ""
    drawing_dependency: str = ""
    requirement_output_fields: list[str] = field(default_factory=list)
    review_notes: str = ""


@dataclass
class Metric:
    """Row of 04_Metric_Dictionary."""

    metric_id: str
    standard_name: str = ""
    synonyms: list[str] = field(default_factory=list)
    unit: str = ""
    data_type: str = ""
    applies_to: str = ""
    used_for: str = ""
    required_for_matching: str = ""
    notes: str = ""


@dataclass
class SynonymEntry:
    """Row of 05_Synonym_Mapping."""

    raw_term: str
    scenario_id: str = ""
    metric_id: str = ""
    standard_meaning: str = ""
    asset_context: str = ""
    priority: str = "Medium"
    notes: str = ""


@dataclass
class ProductFamily:
    """Row of 06_Product_Family_Master."""

    family_id: str
    product_line: str = ""
    family_name: str = ""
    applicable_scenarios: list[str] = field(default_factory=list)
    primary_asset_type: str = ""
    typical_capabilities: str = ""
    default_quantity_rule_id: str = ""
    dependencies: str = ""
    notes: str = ""


@dataclass
class Product:
    """Row of 07_Product_Master_Template."""

    product_id: str
    model: str = ""
    family_id: str = ""
    family_name: str = ""
    applicable_scenarios: list[str] = field(default_factory=list)
    primary_asset_type: str = ""
    description: str = ""
    supported_standards: str = ""
    protocols: str = ""
    default_quantity_rule_id: str = ""
    status: str = "TBD"
    notes: str = ""


@dataclass
class ProductParameter:
    """Row of 08_Product_Parameter_Template (one row per product-parameter)."""

    product_id: str
    model: str = ""
    family_id: str = ""
    metric_id: str = ""
    parameter_name: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    supported_value: str = ""
    unit: str = ""
    match_type: str = ""
    match_priority: str = ""
    notes: str = ""


@dataclass
class QuantityRule:
    """Row of 09_Quantity_Rules."""

    rule_id: str
    scenario_ids: list[str] = field(default_factory=list)
    family_id: str = ""
    family_name: str = ""
    quantity_basis: str = ""
    description: str = ""
    need_drawing: str = ""
    need_asset_list: str = ""
    count_field: str = ""
    example: str = ""
    assumption: str = ""


@dataclass
class CompatibilityRule:
    """Row of 10_Compatibility_Rules."""

    rule_id: str
    rule_type: str = ""
    scenario_id: str = ""
    asset_type: str = ""
    condition: str = ""
    action: str = ""
    severity: str = "Medium"
    notes: str = ""


# --------------------------------------------------------------------------- #
# Step 1 outputs
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    """Row of 12_Extracted_Evidence."""

    evidence_id: str
    project_id: str = ""
    source_document: str = ""
    location: str = ""
    # 1-based line number of the matched term within its page/segment text
    # (0 = unknown). Additive field used by the Spec Sections review UI to cite
    # a precise "page N line M" location; does not affect BOQ generation.
    line: int = 0
    evidence_text: str = ""
    scenario_id: str = ""
    scenario: str = ""
    asset_type: str = ""
    asset_tag: str = ""
    confidence: float = 0.0
    notes: str = ""


@dataclass
class Requirement:
    """Row of 13_Structured_Requirements."""

    requirement_id: str
    project_id: str = ""
    scenario_id: str = ""
    scenario: str = ""
    asset_type: str = ""
    asset_tag: str = ""
    metric_id: str = ""
    metric_name: str = ""
    parameter_value: str = ""
    unit: str = ""
    requirement_type: str = "Unknown"
    evidence_id: str = ""
    confidence: float = 0.0
    missing_or_assumption: str = ""


@dataclass
class DrawingAsset:
    """Row of 14_Drawing_Asset_List."""

    project_id: str = ""
    drawing_id: str = ""
    asset_tag: str = ""
    asset_type: str = ""
    voltage_level: str = ""
    rating: str = ""
    quantity: float = 0
    connected_to: str = ""
    monitoring_zone: str = ""
    source_location: str = ""
    confidence: float = 0.0
    notes: str = ""
    # Scope / drawing-zone fields (populated by enhanced SLD extraction)
    drawing_area: str = ""       # e.g. "400kV GIS" / "33kV GIS" / "LVAC" / "Future Area"
    status: str = "Unclear"      # "New" / "Existing" / "Future" / "Provision" / "Unclear"
    scope_confirmed: bool = False  # set True after engineer review


# --------------------------------------------------------------------------- #
# Step 2 outputs
# --------------------------------------------------------------------------- #
@dataclass
class ProductMatch:
    """Row of 15_Product_Matching_Output."""

    project_id: str = ""
    requirement_id: str = ""
    candidate_product_id: str = ""
    candidate_model: str = ""
    family_id: str = ""
    family_name: str = ""
    scenario_match: str = ""
    asset_match: str = ""
    parameter_match_result: str = ""
    match_score: float = 0.0
    match_status: str = "Needs Review"
    matched_parameters: str = ""
    gap_or_risk: str = ""
    recommendation: str = ""


@dataclass
class BOQLine:
    """Row of 16_Draft_BOQ."""

    boq_line: int = 0
    project_id: str = ""
    product_id: str = ""
    product_model: str = ""
    product_description: str = ""
    scenario_id: str = ""
    related_assets: str = ""
    quantity: float = 0
    unit: str = "set"
    quantity_basis: str = ""
    assumption: str = ""
    confidence: float = 0.0
    review_status: str = "Draft"
    notes: str = ""


@dataclass
class MissingInfoQuestion:
    """Row of 17_Missing_Info_Questions."""

    project_id: str = ""
    missing_item: str = ""
    scenario_id: str = ""
    why_it_matters: str = ""
    question: str = ""
    priority: str = "Medium"
    owner: str = ""
    status: str = "Open"
    notes: str = ""


def to_dict(obj: Any) -> dict:
    """Serialize a dataclass instance to a plain dict (JSON-friendly)."""
    return asdict(obj)
