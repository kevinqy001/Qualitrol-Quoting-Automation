"""Central configuration: file-system paths and tunable thresholds.

Everything is resolved relative to the repository root so the backend works
regardless of the current working directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Repository root = parent of the qualitrol_core package directory.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

DATA_PACKAGE_PATH: Path = REPO_ROOT / "Qualitrol_BOQ_Matching_Data_Package.xlsx"

STEP0_DIR: Path = REPO_ROOT / "Step 0 _ Tavily Search"
STEP1_DIR: Path = REPO_ROOT / "Step 1 _ Extract Info"
STEP2_DIR: Path = REPO_ROOT / "Step 2 _ Create BOQ"
STEP3_DIR: Path = REPO_ROOT / "Step 3 _ Configure & Quote"
STEP4_DIR: Path = REPO_ROOT / "Step 4 _ Generate Quotation"

# Standard Qualitrol quotation template (Step 4). The quotation generator clones
# this file so every page, section, style and the legal Terms & Conditions match
# the official layout exactly; only the dynamic regions are filled in and prices
# (pending Step 3) are left blank. Override with QUALITROL_QUOTATION_TEMPLATE.
#
# This is a local, pre-built BLANK master template (project-specific content
# neutralised) so Step 4 no longer re-extracts from the Gemba sample on each run.
# Rebuild it with: python "Step 4 _ Generate Quotation/build_blank_template.py"
QUOTATION_TEMPLATE_PATH: Path = STEP4_DIR / "Quotation_Template.docx"


def quotation_template_path() -> Path:
    """Resolve the Step 4 quotation template (env override wins)."""
    override = os.getenv("QUALITROL_QUOTATION_TEMPLATE")
    return Path(override) if override else QUOTATION_TEMPLATE_PATH


# Standard BOQ Excel template (Step 2). The BOQ generator clones this blank
# template (derived from the official sample BOQ) and fills the draft BOQ lines.
# Rebuild it with: python "Step 2 _ Create BOQ/build_blank_boq_template.py"
BOQ_TEMPLATE_PATH: Path = STEP2_DIR / "BOQ_Template.xlsx"


def boq_template_path() -> Path:
    """Resolve the Step 2 BOQ Excel template (env override wins)."""
    override = os.getenv("QUALITROL_BOQ_TEMPLATE")
    return Path(override) if override else BOQ_TEMPLATE_PATH

SAMPLE_SUBMISSIONS_DIR: Path = REPO_ROOT / "Gemba Samples" / "3"

# Location for pipeline run artifacts (uploads, JSON outputs, generated docs).
# Env-driven so the demo deployment can point it at a durable share that
# survives redeploys. On Azure App Service set QUALITROL_DATA_DIR=/home/data
# (``/home`` is a plan-wide persistent share). Defaults to the in-repo
# ``outputs/`` folder for local development.
def _resolve_output_dir() -> Path:
    override = os.getenv("QUALITROL_DATA_DIR")
    return Path(override) if override else REPO_ROOT / "outputs"


OUTPUT_DIR: Path = _resolve_output_dir()

# File extensions the document parser knows how to read.
SUPPORTED_DOC_EXTENSIONS = {
    ".pdf", ".docx", ".txt", ".eml", ".msg", ".md",
    ".xlsx", ".xlsm", ".pptx", ".csv",
}


@dataclass
class Thresholds:
    """Tunable confidence thresholds (mirrors CR_013 in the data package)."""

    # Below this, an extracted item is flagged "Needs Review" and must not be
    # used as a must-have matching criterion (Compatibility Rule CR_013).
    review_confidence: float = 0.70
    # Minimum confidence to keep an evidence hit at all.
    min_evidence_confidence: float = 0.30
    # Match score (0-1) at/above which a product candidate is "Recommended".
    recommend_score: float = 0.60


# Local credential files (gitignored). Created via setup; env vars override them.
LLM_CONFIG_FILE: Path = Path(__file__).resolve().parent / "llm_config.local.json"
TAVILY_CONFIG_FILE: Path = Path(__file__).resolve().parent / "tavily_config.local.json"


def _load_local_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _resolve_llm() -> dict:
    """Resolve LLM credentials: environment variables override the local file.

    Accepts two naming conventions so both our config and the colleague's .env
    (which uses AI_FOUNDRY_* names) work without any changes:
      - ANTHROPIC_FOUNDRY_ENDPOINT  / AI_FOUNDRY_ENDPOINT
      - ANTHROPIC_FOUNDRY_API_KEY   / AI_FOUNDRY_API_KEY
      - ANTHROPIC_FOUNDRY_DEPLOYMENT / AI_FOUNDRY_DEPLOYMENT_NAME
    """
    local = _load_local_json(LLM_CONFIG_FILE)
    endpoint = (
        os.getenv("ANTHROPIC_FOUNDRY_ENDPOINT")
        or os.getenv("AI_FOUNDRY_ENDPOINT")
        or local.get("endpoint", "")
    )
    api_key = (
        os.getenv("ANTHROPIC_FOUNDRY_API_KEY")
        or os.getenv("AI_FOUNDRY_API_KEY")
        or local.get("api_key", "")
    )
    deployment = (
        os.getenv("ANTHROPIC_FOUNDRY_DEPLOYMENT")
        or os.getenv("AI_FOUNDRY_DEPLOYMENT_NAME")
        or local.get("deployment")
        or "claude-opus-4-8"
    )
    return {"endpoint": endpoint, "api_key": api_key, "deployment": deployment}


def _resolve_tavily() -> dict:
    """Resolve Tavily credentials: environment variables override the local file."""
    local = _load_local_json(TAVILY_CONFIG_FILE)
    api_key = os.getenv("TAVILY_API_KEY") or local.get("api_key", "")
    return {"api_key": api_key}


@dataclass
class Settings:
    """Runtime settings.

    The pipeline is rules-first and always works offline. When Anthropic Foundry
    credentials are available (env vars or qualitrol_core/llm_config.local.json),
    the LLM augmentation layer is enabled automatically. Force off with
    QUALITROL_USE_LLM=0, or force on with QUALITROL_USE_LLM=1.
    """

    thresholds: Thresholds = field(default_factory=Thresholds)

    llm_provider: str = "anthropic_foundry"
    llm_endpoint: str = field(default_factory=lambda: _resolve_llm()["endpoint"])
    llm_api_key: str = field(default_factory=lambda: _resolve_llm()["api_key"])
    llm_deployment: str = field(default_factory=lambda: _resolve_llm()["deployment"])
    llm_max_tokens: int = 2048
    # Opus 4.8 on Foundry rejects the temperature param; leave None to omit it.
    llm_temperature: float | None = None
    llm_timeout: float = 60.0

    # --- Per-role Claude deployments (same Anthropic Foundry endpoint/key) --- #
    # ``judge`` uses ``llm_deployment`` (Opus) for precision. The high-throughput
    # roles default to the faster Sonnet-5: ``analyze`` (grounded Step 1 locator
    # over large document chunks), ``vision`` (page-image OCR + SLD analysis) and
    # ``bulk`` (Step 0b datasheet extraction). Override any of them per env.
    analyze_deployment: str = field(
        default_factory=lambda: os.getenv("QUALITROL_ANALYZE_DEPLOYMENT")
        or "claude-sonnet-5"
    )
    vision_deployment: str = field(
        default_factory=lambda: os.getenv("QUALITROL_VISION_DEPLOYMENT")
        or "claude-sonnet-5"
    )
    bulk_deployment: str = field(
        default_factory=lambda: os.getenv("QUALITROL_BULK_DEPLOYMENT")
        or "claude-sonnet-5"
    )

    # --- Tavily web research (Step 0 product-catalog enrichment) --------- #
    tavily_api_key: str = field(default_factory=lambda: _resolve_tavily()["api_key"])
    # Qualitrol's official site; results are biased to (not locked to) this domain.
    tavily_primary_domain: str = "qualitrolcorp.com"
    tavily_search_depth: str = "advanced"   # ultra-fast | fast | basic | advanced
    tavily_max_results: int = 8
    tavily_extract_depth: str = "advanced"  # basic | advanced
    tavily_max_urls_per_family: int = 4
    tavily_timeout: float = 60.0

    @property
    def llm_credentials_present(self) -> bool:
        return bool(self.llm_endpoint and self.llm_api_key)

    @property
    def use_llm(self) -> bool:
        override = os.getenv("QUALITROL_USE_LLM")
        if override is not None:
            return override == "1"
        return self.llm_credentials_present

    @property
    def tavily_available(self) -> bool:
        return bool(self.tavily_api_key)


SETTINGS = Settings()
