"""HTTP surface for programmatic (SDK-transport) Claude Code sessions.

POST /sdk/run streams the run as Server-Sent Events. We use POST (read by the
frontend via a fetch reader) rather than an EventSource GET so prompts are not
limited by URL length. Tool calls captured during the run also flow into the
live trace graph via the shared broadcaster, so the existing /events/stream
shows them with no extra wiring.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import sdk_session

logger = logging.getLogger("cc_trace.sdk")

router = APIRouter(tags=["sdk"])


class SdkRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=100_000)
    cwd: str | None = None
    model: str | None = None
    permission_mode: str | None = None


@router.post("/sdk/run")
async def sdk_run(body: SdkRunRequest) -> StreamingResponse:
    async def event_source() -> AsyncIterator[str]:
        try:
            async for event in sdk_session.run_session(
                prompt=body.prompt,
                cwd=body.cwd,
                model=body.model,
                permission_mode=body.permission_mode,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:  # bad cwd / invalid permission_mode
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
        except FileNotFoundError:
            yield (
                "data: "
                + json.dumps(
                    {"type": "error", "detail": "`claude` CLI not found on PATH"}
                )
                + "\n\n"
            )
        except Exception:  # last resort: surface a frame instead of a silent close
            logger.exception("unexpected error in sdk run")
            yield f"data: {json.dumps({'type': 'error', 'detail': 'internal error'})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
