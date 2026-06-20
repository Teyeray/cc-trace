"""Hook ingestion endpoints.

Each handler does the minimum: read the JSON body, append it, return fast.
No computation on the hot path (see plan, Phase 1).

Failure policy: never raise back to Claude Code. If the body is unreadable we
still return a benign response so the editor is never blocked. The matching
``curl`` hooks are also time-boxed and fail-open on the client side.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from . import storage
from .events import broadcaster

logger = logging.getLogger("cc_trace.hooks")

router = APIRouter(prefix="/hooks", tags=["hooks"])


def shape(envelope: dict[str, Any]) -> dict[str, Any]:
    """Flatten a stored envelope into a lightweight event for the live stream.

    Pass through everything Claude Code gives us that is useful downstream.
    Notably ``tool_use_id`` enables EXACT Pre/Post pairing (no heuristic), and
    ``duration_ms`` is the editor-measured tool runtime (more accurate than
    diffing receive timestamps). Context fields (cwd, permission_mode, effort)
    are surfaced for the detail panel.
    """
    raw = envelope.get("raw", {})
    return {
        "id": envelope.get("id"),
        "received_at": envelope.get("received_at"),
        "session_id": raw.get("session_id"),
        "hook_event_name": raw.get("hook_event_name"),
        "tool_name": raw.get("tool_name"),
        "tool_input": raw.get("tool_input"),
        "tool_response": raw.get("tool_response"),
        # newly surfaced
        "tool_use_id": raw.get("tool_use_id"),
        "duration_ms": raw.get("duration_ms"),
        "cwd": raw.get("cwd"),
        "permission_mode": raw.get("permission_mode"),
        "effort": raw.get("effort"),
        "transcript_path": raw.get("transcript_path"),
        "trigger": raw.get("trigger"),  # Stop/PreCompact carry this
    }


async def _ingest(request: Request) -> dict[str, Any] | None:
    """Append the request body and publish it. Returns the shaped event."""
    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # malformed/empty body — do not block Claude Code
        logger.warning("hook payload was not valid JSON; ignoring")
        return None
    try:
        envelope = storage.append_jsonl(body)
    except Exception:  # storage failure must not surface to the editor
        logger.exception("failed to append hook event")
        return None
    event = shape(envelope)
    try:
        broadcaster.publish(event)
    except Exception:  # streaming is best-effort, never block the hook
        logger.exception("failed to publish hook event")
    return event


@router.post("/pre-tool")
async def pre_tool(request: Request) -> dict[str, Any]:
    """PreToolUse: observation only. Empty object == default allow."""
    await _ingest(request)
    return {}


@router.post("/post-tool")
async def post_tool(request: Request) -> dict[str, Any]:
    """PostToolUse: append the tool result."""
    await _ingest(request)
    return {}


@router.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    """Stop: session boundary marker (carries no tool fields)."""
    await _ingest(request)
    return {}
