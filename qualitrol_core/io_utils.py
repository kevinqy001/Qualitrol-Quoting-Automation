"""Serialization helpers for pipeline artifacts (JSON)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def _default(obj: Any):
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def write_json(path: str | Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=_default)
    return path


def write_json_atomic(path: str | Path, payload: Any) -> Path:
    """Write arbitrary JSON so a concurrent reader never sees a partial file.

    The payload is serialized to a **unique** temp file in the same directory,
    then ``os.replace``'d onto the final path (an atomic rename on the same
    filesystem). Unique temp names — not a fixed ``<name>.tmp`` — mean two
    processes publishing the same target cannot clobber each other's temp file.

    This is the safe writer for any file that another worker process may read
    concurrently (e.g. the cross-worker poll files ``_job.json`` /
    ``_result.json``). Kept generic on purpose so other artifacts can reuse it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=_default)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def rows_to_dicts(rows: list) -> list[dict]:
    return [asdict(r) if is_dataclass(r) and not isinstance(r, type) else r for r in rows]
