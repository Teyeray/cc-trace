"""Terminal HTTP/WebSocket surface over the PTY session registry.

REST manages session lifecycle; the WebSocket is a thin attach/detach bridge to
a session that already exists (and keeps running when the socket closes):

- POST   /terminal/sessions            create a PTY in {cwd, cmd} -> {id, ...}
- GET    /terminal/sessions            list live sessions
- DELETE /terminal/sessions/{id}       kill + reap a session
- WS     /terminal/ws?id={id}          attach: replay scrollback, then bridge IO

Running ``claude`` inside a session produces a real Claude Code session whose
tool calls flow through the existing hook pipeline -> graph. The terminal
provides *interaction*; the trace graph provides *observation*. Their only
coupling is the project's ``.claude/settings.json`` hooks.

SECURITY: a PTY-over-WebSocket bridge is remote code execution by design.
WebSockets are NOT subject to CORS, so a malicious page could otherwise open
``ws://localhost:8000/terminal/ws`` and run commands (cross-site WebSocket
hijacking). We validate the ``Origin`` header against loopback *before*
attaching, and the server binds 127.0.0.1 only. This is a LOCAL developer tool —
do not expose the backend port to untrusted networks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .terminal_sessions import registry

logger = logging.getLogger("cc_trace.terminal")

router = APIRouter(tags=["terminal"])

# Loopback only. Note: a `null` origin (sandboxed iframe / file://) parses to
# '' via urlparse and is intentionally NOT in this set, so it is rejected.
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _origin_allowed(origin: str | None) -> bool:
    """Allow only loopback origins.

    A missing Origin is allowed for non-browser clients (CLI tools). A real
    browser always sends Origin on a cross-origin WebSocket, so this is not a
    CSWSH bypass — UNLESS the server is placed behind a proxy that strips the
    Origin header. Keep this server local and unproxied.
    """
    if not origin:
        return True
    return urlparse(origin).hostname in _ALLOWED_HOSTS


class CreateSessionRequest(BaseModel):
    cwd: str | None = None
    cmd: str | None = None  # e.g. "claude" or a shell path; defaults to $SHELL


@router.post("/terminal/sessions")
async def create_session(body: CreateSessionRequest) -> dict[str, Any]:
    # async so registry.create() runs on the event-loop thread: it calls
    # asyncio.get_running_loop() + loop.add_reader to wire the PTY reader. A
    # sync endpoint would run in a threadpool with no loop and raise.
    try:
        session = registry.create(cwd=body.cwd, cmd=body.cmd)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return session.to_public()


@router.get("/terminal/sessions")
def list_sessions() -> dict[str, list[dict[str, Any]]]:
    return {"sessions": registry.list()}


@router.delete("/terminal/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    killed = await registry.kill(session_id)
    if not killed:
        raise HTTPException(status_code=404, detail="unknown session")
    return {"killed": True}


@router.websocket("/terminal/ws")
async def terminal_ws(websocket: WebSocket, id: str | None = None) -> None:
    if not _origin_allowed(websocket.headers.get("origin")):
        logger.warning(
            "rejected terminal WS from origin %r", websocket.headers.get("origin")
        )
        await websocket.close(code=1008)  # policy violation
        return

    session = registry.get(id) if id else None
    if session is None:
        await websocket.accept()
        await websocket.send_bytes(
            b"\r\n[cc-trace] no such terminal session; create one first.\r\n"
        )
        await websocket.close(code=1011)
        return

    await websocket.accept()

    # Attach: snapshot scrollback (atomic with queue assignment), replay it,
    # then stream live output. Input/resize flow back into the shared session.
    out_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    snapshot = session.attach(out_queue)
    if snapshot:
        await websocket.send_bytes(snapshot)

    async def pump_out() -> None:
        while True:
            data = await out_queue.get()
            if data is None:  # EOF sentinel — child exited
                await websocket.send_bytes(b"\r\n\x1b[2m[session ended]\x1b[0m\r\n")
                break
            await websocket.send_bytes(data)

    async def pump_in() -> None:
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "input":
                session.write(payload.get("data", "").encode("utf-8"))
            elif kind == "resize":
                session.resize(
                    int(payload.get("rows", 24)), int(payload.get("cols", 80))
                )

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in (out_task, in_task):
            task.cancel()
        # Detach only — the PTY keeps running so the user can switch back to it.
        session.detach(out_queue)
        logger.info("terminal ws detached from session %s", id)
