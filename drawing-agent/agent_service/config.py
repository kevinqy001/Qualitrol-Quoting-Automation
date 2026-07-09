"""Environment-driven settings for the Drawing Agent service.

Reuses the *same* Azure AI Foundry variable names as the main
Qualitrol-Quoting-Automation app so this service drops into the same tenant with
no new credentials:

    ANTHROPIC_FOUNDRY_ENDPOINT   e.g. https://<res>.services.ai.azure.com/anthropic/
    ANTHROPIC_FOUNDRY_API_KEY
    ANTHROPIC_FOUNDRY_DEPLOYMENT e.g. claude-opus-4-8

Falls back to a direct Anthropic key (ANTHROPIC_API_KEY) for local dev, and an
optional Azure OpenAI vision engine. With no credentials the service still runs
fully on the bundled Sample engine + a deterministic offline agent.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
WEB = BASE / "web"
DRAWINGS = WEB / "drawings"
SEED = WEB / "seed"
DATA = BASE / "data"
SESSIONS = DATA / "sessions"
SESSIONS.mkdir(parents=True, exist_ok=True)


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


# --- Claude via AWS Bedrock (primary — the AWS endpoint) --------------------
# The same env the Agent SDK reads (CLAUDE_CODE_USE_BEDROCK / AWS_REGION), plus
# a model id and an optional gateway base URL for a private AWS endpoint.
USE_BEDROCK = _env("CLAUDE_CODE_USE_BEDROCK") in ("1", "true", "True") or bool(
    _env("BEDROCK_MODEL_ID")
)
AWS_REGION = _env("AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1"
BEDROCK_MODEL_ID = (
    _env("BEDROCK_MODEL_ID", "ANTHROPIC_MODEL_BEDROCK", "ANTHROPIC_DEFAULT_OPUS_MODEL")
    # Set to the Bedrock model / cross-region inference profile enabled in YOUR
    # account (e.g. "us.anthropic.claude-opus-4-8"). Placeholder default:
    or "us.anthropic.claude-opus-4-8"
)
BEDROCK_BASE_URL = _env("BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL")

# --- Claude via Azure AI Foundry (in-tenant) or direct Anthropic ------------
FOUNDRY_ENDPOINT = _env("ANTHROPIC_FOUNDRY_ENDPOINT")
FOUNDRY_API_KEY = _env("ANTHROPIC_FOUNDRY_API_KEY")
FOUNDRY_DEPLOYMENT = _env("ANTHROPIC_FOUNDRY_DEPLOYMENT") or "claude-opus-4-8"
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL") or "claude-opus-4-8"

# --- Azure OpenAI (optional secondary vision engine) ---
AZURE_OPENAI_ENDPOINT = _env("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = _env("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT = _env("AZURE_OPENAI_DEPLOYMENT") or "gpt-4o"
AZURE_OPENAI_API_VERSION = _env("AZURE_OPENAI_API_VERSION") or "2024-08-01-preview"


def claude_available() -> bool:
    return bool(
        USE_BEDROCK or (FOUNDRY_ENDPOINT and FOUNDRY_API_KEY) or ANTHROPIC_API_KEY
    )


def azure_openai_available() -> bool:
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY)


def claude_transport() -> str:
    if USE_BEDROCK:
        return f"AWS Bedrock ({AWS_REGION})"
    if FOUNDRY_ENDPOINT and FOUNDRY_API_KEY:
        return "Azure AI Foundry (in-tenant)"
    if ANTHROPIC_API_KEY:
        return "Anthropic API (direct)"
    return "not configured"
