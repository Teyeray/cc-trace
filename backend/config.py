"""Configuration and filesystem paths for the hook server.

All storage lives under ``DATA_DIR``; nothing is hardcoded at call sites.
"""

from __future__ import annotations

from pathlib import Path

# Project root = parent of the ``backend`` package directory.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Append-only raw event logs: .data/raw_events/{session_id}.jsonl
DATA_DIR: Path = PROJECT_ROOT / ".data"
RAW_EVENTS_DIR: Path = DATA_DIR / "raw_events"

# SQLite index over the raw logs, populated async by replaying JSONL (never on
# the hook hot path). See backend/index_store.py.
INDEX_DB: Path = DATA_DIR / "index.db"

# Saved, reusable workflows distilled from traces: .data/workflows/{id}.json
WORKFLOWS_DIR: Path = DATA_DIR / "workflows"

# Used when a hook payload arrives without a session id.
UNKNOWN_SESSION_ID: str = "unknown-session"

# curl --max-time bound (seconds) advertised to hook authors; informational here.
HOOK_TIMEOUT_SECONDS: int = 2
