"""Loader for the controlled reference layer.

Reads ``Qualitrol_BOQ_Matching_Data_Package.xlsx`` and exposes the reference
tables (scenarios, metrics, synonyms, product families/models/parameters,
quantity rules, compatibility rules) as typed objects with convenient indexes.

Each sheet starts with a title row and a description row, followed by a header
row and the data rows. We locate the header row by its first column name so the
loader is resilient to minor layout changes.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Iterable, Optional

import openpyxl

from . import config
from .schemas import (
    CompatibilityRule,
    Metric,
    MissingInfoQuestion,
    Product,
    ProductFamily,
    ProductParameter,
    QuantityRule,
    Scenario,
    SynonymEntry,
)

SHEET = {
    "scenario": "03_Scenario_Master",
    "metric": "04_Metric_Dictionary",
    "synonym": "05_Synonym_Mapping",
    "family": "06_Product_Family_Master",
    "product": "07_Product_Master_Template",
    "parameter": "08_Product_Parameter_Template",
    "quantity": "09_Quantity_Rules",
    "compatibility": "10_Compatibility_Rules",
    "missing_info": "17_Missing_Info_Questions",
}


def _split_list(value: Optional[str], sep: str = ";") -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(sep) if part.strip()]


def _to_float(value) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(ws) -> list[list]:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _find_header(rows: list[list], first_col: str) -> int:
    target = first_col.strip().lower()
    for idx, row in enumerate(rows):
        if not row:
            continue
        for cell in row:
            if cell is not None and str(cell).strip().lower() == target:
                return idx
    raise ValueError(f"Header row starting with {first_col!r} not found")


def _records(rows: list[list], header_idx: int) -> Iterable[dict]:
    headers = [str(h).strip() if h is not None else "" for h in rows[header_idx]]
    for row in rows[header_idx + 1 :]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        record = {}
        for col_idx, header in enumerate(headers):
            if not header:
                continue
            record[header] = row[col_idx] if col_idx < len(row) else None
        yield record


def _g(record: dict, key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value).strip()


class DataPackage:
    """In-memory view of the controlled reference layer with lookup indexes."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config.DATA_PACKAGE_PATH
        if not self.path.exists():
            raise FileNotFoundError(f"Data package not found: {self.path}")
        self._wb = openpyxl.load_workbook(self.path, read_only=True, data_only=True)

        self.scenarios: dict[str, Scenario] = {}
        self.metrics: dict[str, Metric] = {}
        self.synonyms: list[SynonymEntry] = []
        self.families: dict[str, ProductFamily] = {}
        self.products: dict[str, Product] = {}
        self.parameters: list[ProductParameter] = []
        self.quantity_rules: dict[str, QuantityRule] = {}
        self.compatibility_rules: list[CompatibilityRule] = []
        self.missing_info_templates: list[MissingInfoQuestion] = []

        self._load_all()
        self._wb.close()

    # -- loading --------------------------------------------------------- #
    def _sheet_rows(self, key: str) -> list[list]:
        return _rows(self._wb[SHEET[key]])

    def _load_all(self) -> None:
        self._load_scenarios()
        self._load_metrics()
        self._load_synonyms()
        self._load_families()
        self._load_products()
        self._load_parameters()
        self._load_quantity_rules()
        self._load_compatibility_rules()
        self._load_missing_info_templates()

    def _load_scenarios(self) -> None:
        rows = self._sheet_rows("scenario")
        head = _find_header(rows, "Scenario ID")
        for rec in _records(rows, head):
            sid = _g(rec, "Scenario ID")
            if not sid:
                continue
            self.scenarios[sid] = Scenario(
                scenario_id=sid,
                category=_g(rec, "Scenario Category"),
                application_scenario=_g(rec, "Application Scenario"),
                asset_type=_g(rec, "Asset Type"),
                typical_metrics=_g(rec, "Typical Metrics / Requirements"),
                keywords=_split_list(_g(rec, "Common Evidence Keywords / Synonyms")),
                related_product_families=_split_list(
                    _g(rec, "Related Product Families")
                ),
                quantity_basis=_g(rec, "Quantity Basis"),
                drawing_dependency=_g(rec, "Drawing Dependency"),
                requirement_output_fields=_split_list(
                    _g(rec, "Requirement Output Fields")
                ),
                review_notes=_g(rec, "Review Notes"),
            )

    def _load_metrics(self) -> None:
        rows = self._sheet_rows("metric")
        head = _find_header(rows, "Metric ID")
        for rec in _records(rows, head):
            mid = _g(rec, "Metric ID")
            if not mid:
                continue
            self.metrics[mid] = Metric(
                metric_id=mid,
                standard_name=_g(rec, "Standard Metric Name"),
                synonyms=_split_list(_g(rec, "Synonyms / Raw Terms")),
                unit=_g(rec, "Standard Unit"),
                data_type=_g(rec, "Data Type"),
                applies_to=_g(rec, "Applies To"),
                used_for=_g(rec, "Used For"),
                required_for_matching=_g(rec, "Required for Matching"),
                notes=_g(rec, "Normalization Notes"),
            )

    def _load_synonyms(self) -> None:
        rows = self._sheet_rows("synonym")
        head = _find_header(rows, "Raw Term / Phrase")
        for rec in _records(rows, head):
            term = _g(rec, "Raw Term / Phrase")
            if not term:
                continue
            self.synonyms.append(
                SynonymEntry(
                    raw_term=term,
                    scenario_id=_g(rec, "Mapped Scenario ID"),
                    metric_id=_g(rec, "Mapped Metric ID"),
                    standard_meaning=_g(rec, "Mapped Standard Meaning"),
                    asset_context=_g(rec, "Asset Context"),
                    priority=_g(rec, "Mapping Priority") or "Medium",
                    notes=_g(rec, "Notes"),
                )
            )

    def _load_families(self) -> None:
        rows = self._sheet_rows("family")
        head = _find_header(rows, "Product Family ID")
        for rec in _records(rows, head):
            fid = _g(rec, "Product Family ID")
            if not fid:
                continue
            self.families[fid] = ProductFamily(
                family_id=fid,
                product_line=_g(rec, "Product Line"),
                family_name=_g(rec, "Product Family"),
                applicable_scenarios=_split_list(_g(rec, "Applicable Scenario IDs")),
                primary_asset_type=_g(rec, "Primary Asset Type"),
                typical_capabilities=_g(rec, "Typical Capabilities"),
                default_quantity_rule_id=_g(rec, "Default Quantity Rule ID"),
                dependencies=_g(rec, "Dependencies / Required Inputs"),
                notes=_g(rec, "Commercial / Engineering Notes"),
            )

    def _load_products(self) -> None:
        rows = self._sheet_rows("product")
        head = _find_header(rows, "Product ID")
        for rec in _records(rows, head):
            pid = _g(rec, "Product ID")
            if not pid:
                continue
            self.products[pid] = Product(
                product_id=pid,
                model=_g(rec, "Product Model"),
                family_id=_g(rec, "Product Family ID"),
                family_name=_g(rec, "Product Family"),
                applicable_scenarios=_split_list(_g(rec, "Applicable Scenario IDs")),
                primary_asset_type=_g(rec, "Primary Asset Type"),
                description=_g(rec, "Product Description"),
                supported_standards=_g(rec, "Supported Standards"),
                protocols=_g(rec, "Communication Protocols"),
                default_quantity_rule_id=_g(rec, "Default Quantity Rule ID"),
                status=_g(rec, "Status") or "TBD",
                notes=_g(rec, "Notes"),
            )

    def _load_parameters(self) -> None:
        rows = self._sheet_rows("parameter")
        head = _find_header(rows, "Product ID")
        for rec in _records(rows, head):
            pid = _g(rec, "Product ID")
            if not pid:
                continue
            self.parameters.append(
                ProductParameter(
                    product_id=pid,
                    model=_g(rec, "Product Model"),
                    family_id=_g(rec, "Product Family ID"),
                    metric_id=_g(rec, "Parameter ID"),
                    parameter_name=_g(rec, "Parameter Name"),
                    min_value=_to_float(rec.get("Min Value")),
                    max_value=_to_float(rec.get("Max Value")),
                    supported_value=_g(rec, "Supported Value / Text"),
                    unit=_g(rec, "Unit"),
                    match_type=_g(rec, "Match Type"),
                    match_priority=_g(rec, "Match Priority"),
                    notes=_g(rec, "Notes"),
                )
            )

    def _load_quantity_rules(self) -> None:
        rows = self._sheet_rows("quantity")
        head = _find_header(rows, "Quantity Rule ID")
        for rec in _records(rows, head):
            rid = _g(rec, "Quantity Rule ID")
            if not rid:
                continue
            self.quantity_rules[rid] = QuantityRule(
                rule_id=rid,
                scenario_ids=_split_list(_g(rec, "Scenario ID")),
                family_id=_g(rec, "Product Family ID"),
                family_name=_g(rec, "Product Family"),
                quantity_basis=_g(rec, "Quantity Basis"),
                description=_g(rec, "Rule Description"),
                need_drawing=_g(rec, "Need Drawing"),
                need_asset_list=_g(rec, "Need Asset List"),
                count_field=_g(rec, "Count Field"),
                example=_g(rec, "Example"),
                assumption=_g(rec, "Assumption / Risk"),
            )

    def _load_compatibility_rules(self) -> None:
        rows = self._sheet_rows("compatibility")
        head = _find_header(rows, "Rule ID")
        for rec in _records(rows, head):
            rid = _g(rec, "Rule ID")
            if not rid:
                continue
            self.compatibility_rules.append(
                CompatibilityRule(
                    rule_id=rid,
                    rule_type=_g(rec, "Rule Type"),
                    scenario_id=_g(rec, "Scenario ID"),
                    asset_type=_g(rec, "Asset Type"),
                    condition=_g(rec, "Condition / Trigger"),
                    action=_g(rec, "Recommended Action"),
                    severity=_g(rec, "Severity") or "Medium",
                    notes=_g(rec, "Notes"),
                )
            )

    def _load_missing_info_templates(self) -> None:
        rows = self._sheet_rows("missing_info")
        try:
            head = _find_header(rows, "Project ID")
        except ValueError:
            return
        for rec in _records(rows, head):
            item = _g(rec, "Missing Information Item")
            if not item:
                continue
            self.missing_info_templates.append(
                MissingInfoQuestion(
                    missing_item=item,
                    scenario_id=_g(rec, "Related Scenario ID"),
                    why_it_matters=_g(rec, "Why It Matters"),
                    question=_g(rec, "Suggested Customer / Engineer Question"),
                    priority=_g(rec, "Priority") or "Medium",
                    owner=_g(rec, "Owner"),
                    status="Open",
                    notes=_g(rec, "Notes"),
                )
            )

    # -- convenience indexes --------------------------------------------- #
    def missing_info_for_scenario(self, scenario_id: str) -> list[MissingInfoQuestion]:
        out = []
        for tpl in self.missing_info_templates:
            related = [s.strip() for s in tpl.scenario_id.replace(";", " ").split()]
            if scenario_id in related:
                out.append(tpl)
        return out

    def families_for_scenario(self, scenario_id: str) -> list[ProductFamily]:
        return [
            fam
            for fam in self.families.values()
            if scenario_id in fam.applicable_scenarios
        ]

    def products_for_family(self, family_id: str) -> list[Product]:
        return [p for p in self.products.values() if p.family_id == family_id]

    def parameters_for_product(self, product_id: str) -> list[ProductParameter]:
        return [p for p in self.parameters if p.product_id == product_id]

    def quantity_rule_for_scenario(self, scenario_id: str) -> Optional[QuantityRule]:
        for rule in self.quantity_rules.values():
            if scenario_id in rule.scenario_ids:
                return rule
        return None

    def compatibility_rules_for_scenario(
        self, scenario_id: str
    ) -> list[CompatibilityRule]:
        return [
            r
            for r in self.compatibility_rules
            if r.scenario_id in (scenario_id, "All")
        ]


@functools.lru_cache(maxsize=4)
def load_data_package(path: Optional[str] = None) -> DataPackage:
    """Cached loader so repeated pipeline calls reuse one parsed workbook."""
    return DataPackage(Path(path) if path else None)
