"""Append-only JSONL persistence for raw hook events.

Design rules (see plan, Phase 1):
- Hot path does *only* an append. No parsing, no indexing.
- One file per session: ``.data/raw_events/{session_id}.jsonl``.
- Each line is a self-describing envelope so the schema can evolve safely.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

# Restrict session ids to a safe charset so they can never escape RAW_EVENTS_DIR.
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_session_id(session_id: str | None) -> str:
    """Return a filesystem-safe session id, falling back to a constant."""
    if not session_id:
        return config.UNKNOWN_SESSION_ID
    cleaned = _SAFE_SESSION_RE.sub("_", session_id).strip("._-")
    return cleaned or config.UNKNOWN_SESSION_ID


def _session_path(session_id: str | None) -> Path:
    config.RAW_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    return config.RAW_EVENTS_DIR / f"{_safe_session_id(session_id)}.jsonl"


def append_jsonl(payload: dict[str, Any]) -> dict[str, Any]:
    """Append one raw hook payload as a JSONL envelope. Returns the envelope.

    The envelope is ``{id, received_at, raw}``; ``raw`` is the verbatim
    Claude Code hook payload so nothing is lost to schema drift.
    """
    envelope = {
        "id": str(uuid.uuid4()),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw": payload,
    }
    path = _session_path(payload.get("session_id"))
    line = json.dumps(envelope, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return envelope
