"""Root entry point for the Qualitrol Quotation web app.

Run the web UI (upload documents -> Step 1 Extract Info -> Step 2 Create BOQ):

    python app.py
    # or, with autoreload during development:
    uvicorn app:app --reload --port 8000

Then open http://127.0.0.1:8000 in a browser.
"""

from __future__ import annotations

from webapp.server import app  # noqa: F401  (exposed for `uvicorn app:app`)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webapp.server:app", host="127.0.0.1", port=8000, reload=False)
