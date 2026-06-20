"""Read + stream API consumed by the React Flow frontend.

- GET /sessions               -> list known sessions with event counts
- GET /events/recent          -> replay a session's stored events (page load)
- GET /events/stream          -> live SSE feed of shaped events

The SSE feed reuses the same shape() as the hook path, so a page can load
history then append live events with identical schema.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from . import config, transcript
from .events import broadcaster
from .hooks import shape

router = APIRouter(tags=["api"])

# SSE keepalive so proxies/browsers don't drop an idle connection.
_KEEPALIVE_SECONDS = 15.0


@router.get("/sessions")
def list_sessions() -> dict[str, list[dict[str, Any]]]:
    config.RAW_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    sessions: list[dict[str, Any]] = []
    for path in sorted(config.RAW_EVENTS_DIR.glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            count = sum(1 for _ in fh)
        sessions.append({"session_id": path.stem, "event_count": count})
    return {"sessions": sessions}


@router.get("/events/recent")
def recent_events(
    session_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, list[dict[str, Any]]]:
    path = config.RAW_EVENTS_DIR / f"{session_id}.jsonl"
    events: list[dict[str, Any]] = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(shape(json.loads(line)))
            except json.JSONDecodeError:
                continue
    return {"events": events}


@router.get("/transcript")
def get_transcript(
    session_id: str,
    limit: int = Query(default=2000, ge=1, le=10000),
) -> dict[str, list[dict[str, Any]]]:
    """Conversation turns (user/assistant text) parsed from the CC transcript."""
    return {"turns": transcript.load_turns(session_id, limit=limit)}


@router.get("/events/stream")
async def stream_events(
    session_id: str | None = Query(default=None),
) -> StreamingResponse:
    queue = broadcaster.subscribe()

    async def event_source() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if session_id and event.get("session_id") != session_id:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
