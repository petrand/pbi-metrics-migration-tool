"""Persistent store for TMDL upload + migration results.

Migration runs are kept only in process memory otherwise, so a page reload or
a server restart loses them. This module persists each run as a JSON file so it
can be listed and reopened later — the uploaded source model is stored alongside
the migration result, so both the upload and its migration output are
recoverable.

The store is a flat directory of ``<migration_id>.json`` files. Location is
``$MIGRATION_STORE_DIR`` (default ``data/migrations`` relative to the CWD).
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# migration_id is a uuid4; guard the URL-supplied value against path traversal.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _store_dir() -> Path:
    d = Path(os.environ.get("MIGRATION_STORE_DIR", "data/migrations"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(migration_id: str) -> Optional[Path]:
    if not migration_id or not _SAFE_ID.match(migration_id):
        return None
    return _store_dir() / f"{migration_id}.json"


def save(migration_id: str, record: dict) -> bool:
    """Atomically persist a migration record. Returns True on success.

    Persistence failures are logged and swallowed — they must never break the
    migration request itself, which already succeeded in memory.
    """
    path = _path(migration_id)
    if path is None:
        return False
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, default=str), encoding="utf-8")
        tmp.replace(path)  # atomic on same filesystem
        return True
    except OSError:
        logger.exception("Failed to persist migration %s", migration_id)
        return False


def get(migration_id: str) -> Optional[dict]:
    """Return a persisted migration record, or None if absent/unreadable."""
    path = _path(migration_id)
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read migration %s", migration_id)
        return None


def list_summaries() -> list:
    """Return lightweight summaries of all persisted migrations, newest first."""
    out = []
    for path in _store_dir().glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "migration_id": rec.get("migration_id"),
            "model_name": rec.get("model_name", ""),
            "status": rec.get("status", ""),
            "created_at": rec.get("created_at", ""),
            "catalog": rec.get("catalog", ""),
            "schema": rec.get("schema", ""),
            "tables": rec.get("tables", 0),
            "measures": rec.get("measures", 0),
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out
