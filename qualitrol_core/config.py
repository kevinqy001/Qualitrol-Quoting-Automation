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

SAMPLE_SUBMISSIONS_DIR: Path = REPO_ROOT / "Gemba Samples" / "3"

# Default location for pipeline run artifacts (JSON outputs).
OUTPUT_DIR: Path = REPO_ROOT / "outputs"

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
