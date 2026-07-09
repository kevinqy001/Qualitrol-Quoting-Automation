# Qualitrol Drawing Agent — container image for Azure App Service for Containers
# / Azure Container Apps. The Python claude-agent-sdk bundles the Claude Code
# CLI binary, so no Node.js install is required.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable config/transcript dir for Agent SDK sessions (mount a volume in prod
# for cross-restart / multi-instance persistence).
ENV CLAUDE_CONFIG_DIR=/app/data/.claude
RUN mkdir -p /app/data/.claude /app/data/sessions

EXPOSE 8080
CMD ["bash", "startup.sh"]

# --- Fallback: if a claude-agent-sdk build on your platform does NOT bundle the
# CLI, add Node + the CLI instead of relying on the bundled binary:
#   RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
#       && apt-get install -y nodejs && npm i -g @anthropic-ai/claude-code
