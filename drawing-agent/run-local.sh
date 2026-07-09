#!/usr/bin/env bash
# Load .env (if present) and start the Drawing Agent locally on :8080.
#   cp .env.example .env   # then edit with your Bedrock creds
#   ./run-local.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a; source .env; set +a
  echo "Loaded .env  (CLAUDE_CODE_USE_BEDROCK=${CLAUDE_CODE_USE_BEDROCK:-unset}, AWS_REGION=${AWS_REGION:-unset})"
fi
PYTHON="$(command -v python3 || command -v python)"
exec "$PYTHON" app.py
