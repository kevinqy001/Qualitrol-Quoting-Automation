"""Root entry point for the Qualitrol Drawing Agent service.

    python app.py                 # local dev (http://127.0.0.1:8080)
    uvicorn app:app --reload      # autoreload
    gunicorn app:app -k uvicorn.workers.UvicornWorker   # production (see startup.sh)
"""
from __future__ import annotations

import os

from agent_service.server import app  # noqa: F401  (exposed for gunicorn/uvicorn)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent_service.server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
    )
