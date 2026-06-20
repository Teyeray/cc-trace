"""Interactive terminal over WebSocket, bridged to a PTY.

Spawns the user's shell in a pseudo-terminal and pipes it to the browser
(xterm.js). Running ``claude`` inside this terminal produces a real Claude Code
session whose tool calls flow through the existing hook pipeline -> graph. The
terminal provides *interaction*; the trace graph provides *observation*. Their
only coupling is the project's ``.claude/settings.json`` hooks.

SECURITY: a PTY-over-WebSocket bridge is remote code execution by design.
WebSockets are NOT subject to CORS, so a malicious page could otherwise open
``ws://localhost:8000/terminal/ws`` and run commands (cross-site WebSocket
hijacking). We defend by validating the ``Origin`` header against loopback
*before* spawning anything, and the server binds 127.0.0.1 only. This is a
LOCAL developer tool — do not expose the backend port to untrusted networks.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
import time
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import config

logger = logging.getLogger("cc_trace.terminal")

router = APIRouter(tags=["terminal"])

_READ_BYTES = 65536
_WRITE_CHUNK = 4096

# Loopback only. Note: a `null` origin (sandboxed iframe / file://) parses to
# '' via urlparse and is intentionally NOT in this set, so it is rejected.
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Cap concurrent PTYs so a runaway/reconnecting tab can't exhaust file
# descriptors (macOS default ulimit -n is 256).
_MAX_SESSIONS = 10
_active_sessions = 0

# What the terminal launches. Default to the user's shell; override to e.g.
# "claude" to drop straight into a session. ``-l`` makes it a login shell so
# PATH and aliases match a normal terminal (and `claude` is resolvable).
_LAUNCH_CMD = os.environ.get("CC_TRACE_TERMINAL_CMD") or os.environ.get(
    "SHELL", "/bin/bash"
)


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


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass  # window-size is best-effort


def _terminate(pid: int) -> None:
    """Hang up, escalate to SIGKILL, and reap the shell's session group.

    Blocking — call via ``run_in_executor``. We poll ``waitpid`` after each
    signal so the child is actually reaped (no zombie) and escalate to SIGKILL
    only if the shell ignores SIGHUP. Closing the PTY master beforehand already
    delivers SIGHUP to any diverged foreground process group (e.g. ``claude``).
    """
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, OSError):
            break  # group already gone
        for _ in range(20):  # poll up to ~1s before escalating
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return  # already reaped elsewhere
            if reaped:
                return
            time.sleep(0.05)
    try:
        os.waitpid(pid, 0)  # last-resort blocking reap
    except ChildProcessError:
        pass


@router.websocket("/terminal/ws")
async def terminal_ws(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket.headers.get("origin")):
        logger.warning(
            "rejected terminal WS from origin %r", websocket.headers.get("origin")
        )
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()

    global _active_sessions
    if _active_sessions >= _MAX_SESSIONS:
        await websocket.send_bytes(
            b"\r\n[cc-trace] too many terminal sessions open; close one and retry.\r\n"
        )
        await websocket.close(code=1013)  # try again later
        return
    _active_sessions += 1

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: become the shell. pty.fork() already called setsid().
        try:
            os.chdir(str(config.PROJECT_ROOT))
        except OSError:
            pass
        os.execvp(_LAUNCH_CMD, [_LAUNCH_CMD, "-l"])
        os._exit(127)  # exec failed

    # Parent: bridge master_fd <-> websocket.
    loop = asyncio.get_running_loop()
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    out_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, _READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""  # PTY closed (shell exited)
        if data:
            out_queue.put_nowait(data)
        else:
            loop.remove_reader(master_fd)
            out_queue.put_nowait(None)  # EOF sentinel

    loop.add_reader(master_fd, _on_readable)

    async def pump_out() -> None:
        while True:
            data = await out_queue.get()
            if data is None:
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
                # master_fd is non-blocking; write in chunks and yield on a
                # full kernel buffer so a large paste isn't silently dropped.
                view = memoryview(payload.get("data", "").encode("utf-8"))
                while view:
                    try:
                        written = os.write(master_fd, view[:_WRITE_CHUNK])
                        view = view[written:]
                    except BlockingIOError:
                        await asyncio.sleep(0.005)
                    except OSError:
                        break  # PTY gone
            elif kind == "resize":
                _set_winsize(
                    master_fd,
                    int(payload.get("rows", 24)),
                    int(payload.get("cols", 80)),
                )

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except WebSocketDisconnect:
        pass
    finally:
        for task in (out_task, in_task):
            task.cancel()
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
        # Close the PTY master FIRST: the kernel then delivers SIGHUP to the
        # foreground process group automatically, reaching subprocesses that
        # diverged into their own pgroup (e.g. `claude`). Then escalate to the
        # shell's session and reap off the event loop so we never block it.
        try:
            os.close(master_fd)
        except OSError:
            pass
        await loop.run_in_executor(None, _terminate, pid)
        _active_sessions -= 1
        logger.info("terminal session %d closed", pid)
