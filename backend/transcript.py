"""Read and parse a session's Claude Code transcript.

The transcript is the full conversation log Claude Code writes per session as
JSONL. We never capture it directly — but every hook event we stored carries
``raw.transcript_path``, so we resolve the path from our own raw event log,
then read and extract the user / assistant *text* turns.

Only text is pulled out here: tool calls already flow through the hook
pipeline (``/hooks/*`` -> raw_events). This gives the graph its conversation
spine (your prompt -> ... -> Claude's answer) without duplicating tool nodes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, storage


def resolve_transcript_path(session_id: str) -> Path | None:
    """Find the transcript file for a session via our stored hook events.

    Reads the session's raw event log and returns the last ``transcript_path``
    seen. Returns ``None`` if unknown or the file is missing.
    """
    raw_log = config.RAW_EVENTS_DIR / f"{storage._safe_session_id(session_id)}.jsonl"
    if not raw_log.exists():
        return None

    transcript_path: str | None = None
    with raw_log.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate = (envelope.get("raw") or {}).get("transcript_path")
            if candidate:
                transcript_path = candidate

    if not transcript_path:
        return None
    path = Path(transcript_path)
    if path.is_file() and path.suffix == ".jsonl":
        return path
    return None


def _extract_text(content: Any) -> str:
    """Pull human-readable text out of a message ``content`` field.

    Content is either a plain string (typed user input) or a list of blocks
    (assistant output, tool results). We keep only ``text`` blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _counts(content: Any) -> dict[str, int]:
    """Count non-text block kinds (tool_use / tool_result / thinking)."""
    result: dict[str, int] = {}
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                kind = block.get("type")
                if kind and kind != "text":
                    result[kind] = result.get(kind, 0) + 1
    return result


def load_turns(session_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    """Return ordered conversation turns with extractable text.

    Each turn: ``{uuid, role, text, timestamp, model, blocks}``. User turns
    that are pure tool-result feedback (no text) are skipped — they are the
    model reading tool output, not a person speaking.
    """
    path = resolve_transcript_path(session_id)
    if path is None:
        return []

    turns: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("type")
            if role not in ("user", "assistant"):
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            text = _extract_text(content).strip()
            if not text:
                continue  # tool_use / tool_result only — no human-facing text
            turns.append(
                {
                    "uuid": obj.get("uuid"),
                    "role": role,
                    "text": text,
                    "timestamp": obj.get("timestamp"),
                    "model": message.get("model") if role == "assistant" else None,
                    "blocks": _counts(content),
                }
            )

    return turns[-limit:]
