"""Phase 2 — SQLite index over the raw JSONL logs.

The hook hot path only appends JSONL (durable, fast). This module builds a
queryable index **off** that path by replaying the JSONL into SQLite. It is
never called from a hook handler — only on demand (API read) or by a background
reindex — so a slow disk write can never delay Claude Code.

WAL + ``synchronous=NORMAL`` favor concurrent reads with good-enough durability
for a local index that can always be rebuilt from the JSONL source of truth.
Rows are keyed by the envelope ``id`` (uuid4) and inserted idempotently, so
re-indexing the same log is safe and cheap.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_hook_events (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    hook_event_name TEXT,
    tool_name       TEXT,
    tool_use_id     TEXT,
    received_at     TEXT,
    raw             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON raw_hook_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_received ON raw_hook_events(received_at);
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.INDEX_DB)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()  # never close mid-transaction on a write failure
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _row_from_envelope(envelope: dict[str, Any]) -> tuple[Any, ...]:
    raw = envelope.get("raw", {})
    return (
        envelope.get("id"),
        raw.get("session_id"),
        raw.get("hook_event_name"),
        raw.get("tool_name"),
        raw.get("tool_use_id"),
        envelope.get("received_at"),
        json.dumps(envelope, ensure_ascii=False),
    )


def index_session(session_id: str) -> int:
    """Replay one session's JSONL into SQLite. Returns rows indexed.

    Idempotent: ``INSERT OR IGNORE`` on the envelope id means re-running only
    adds genuinely new rows.
    """
    path = config.RAW_EVENTS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return 0
    init_db()
    rows: list[tuple[Any, ...]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(_row_from_envelope(json.loads(line)))
            except json.JSONDecodeError:
                continue
    if not rows:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO raw_hook_events "
            "(id, session_id, hook_event_name, tool_name, tool_use_id, received_at, raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # INSERT OR IGNORE skips rows already indexed, so report rows actually
        # inserted, not rows attempted (keeps re-index counts honest).
        return conn.total_changes - before


def reindex_all() -> dict[str, int]:
    """Replay every known session's JSONL. Returns {session_id: rows}."""
    config.RAW_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, int] = {}
    for path in sorted(config.RAW_EVENTS_DIR.glob("*.jsonl")):
        out[path.stem] = index_session(path.stem)
    return out


def list_sessions() -> list[dict[str, Any]]:
    """Sessions with event counts and time bounds, from the index."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "SELECT session_id, COUNT(*) AS event_count, "
            "MIN(received_at) AS first_at, MAX(received_at) AS last_at "
            "FROM raw_hook_events GROUP BY session_id ORDER BY last_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]


def session_events(session_id: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Stored envelopes for one session, oldest first."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "SELECT raw FROM raw_hook_events WHERE session_id = ? "
            "ORDER BY received_at LIMIT ?",
            (session_id, limit),
        )
        return [json.loads(row["raw"]) for row in cur.fetchall()]
