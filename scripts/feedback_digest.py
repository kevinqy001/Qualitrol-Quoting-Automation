"""CLI: build the user-feedback digest.

Reads all feedback under ``OUTPUT_DIR/_feedback`` and the per-case feedback files
and writes a human-readable Markdown report to
``OUTPUT_DIR/_feedback/feedback_digest.md``.

Read-only: it never changes any feedback or case data. Run it on demand:

    python scripts/feedback_digest.py

Point it at the deployment's durable share (Azure ``/home/data``) via env:

    QUALITROL_DATA_DIR=/home/data python scripts/feedback_digest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qualitrol_core import feedback_digest  # noqa: E402


def main() -> None:
    stats, markdown = feedback_digest.build_digest()
    out = feedback_digest.write_digest(markdown)
    # Plain-ASCII summary (avoid emoji so Windows/GBK consoles don't choke).
    print(f"Wrote {out}")
    print(
        f"Cases with feedback: {len(stats['cases'])} | "
        f"spec negative={stats['spec']['negative']} | "
        f"BOQ line negative={stats['boqLines']['negative']} | "
        f"BOQ overall negative={stats['boqOverall']['negative']} | "
        f"regenerations={stats['regen']['total']}"
    )


if __name__ == "__main__":
    main()
