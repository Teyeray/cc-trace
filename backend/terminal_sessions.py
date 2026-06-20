"""Long-lived PTY sessions, decoupled from any single WebSocket.

The original terminal bridge created a PTY *inside* the WebSocket handler, so
closing the tab killed the shell. To support switching between multiple Claude
Code sessions (each with its own working directory) the PTY must **outlive** the
socket. This module owns that lifecycle:

- ``TerminalSession`` wraps one ``pty.fork()`` child plus a bounded scrollback
  buffer and an optional *attached* output queue (the currently-focused tab).
- A single background reader per session pumps PTY output into the scrollback
  AND, when a tab is attached, into that tab's queue. Detaching (tab switch /
  socket close) leaves the child running.
- ``registry`` is the process-wide ``{id: TerminalSession}`` map the router
  drives via create / list / attach / kill.

SECURITY: a PTY is remote code execution by design. Origin validation and
loopback-only binding live in ``terminal.py``; ``cwd`` is validated here only as
"exists and is a directory" — this is a local developer tool, not a sandbox.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config

logger = logging.getLogger("cc_trace.terminal")

_READ_BYTES = 65536

# Per-session scrollback cap. Replayed verbatim when a tab (re)attaches so the
# user sees history instead of a blank screen. Trimming from the front can clip
# an escape sequence; acceptable for scrollback fidelity.
_SCROLLBACK_MAX_BYTES = 256 * 1024

# Cap concurrent PTYs so runaway tabs can't exhaust file descriptors
# (macOS default ``ulimit -n`` is 256).
_MAX_SESSIONS = 10

# EOF sentinel pushed to an attached queue when the child exits.
EOF = None


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass  # window size is best-effort


def _terminate(pid: int) -> None:
    """Hang up, escalate to SIGKILL, and reap the shell's session group.

    Blocking — call via ``run_in_executor``. Poll ``waitpid`` after each signal
    so the child is actually reaped (no zombie) and escalate to SIGKILL only if
    the shell ignores SIGHUP.
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


def _resolve_cwd(cwd: str | None) -> Path:
    """Validate a requested working directory.

    Returns the resolved path. Raises ``ValueError`` if it does not exist or is
    not a directory. ``None``/empty falls back to the project root.
    """
    if not cwd or not cwd.strip():
        return config.PROJECT_ROOT
    path = Path(cwd).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:  # pragma: no cover - exotic FS errors
        raise ValueError(f"cannot resolve path: {cwd}") from exc
    if not path.exists():
        raise ValueError(f"directory does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    return path


@dataclass
class TerminalSession:
    """One PTY child plus its scrollback and (optional) attached output queue."""

    id: str
    pid: int
    master_fd: int
    cwd: str
    cmd: str
    rows: int = 24
    cols: int = 80
    alive: bool = True
    _scrollback: bytearray = field(default_factory=bytearray, repr=False)
    _out_queue: "asyncio.Queue[bytes | None] | None" = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    # ---- lifecycle ------------------------------------------------------
    def start_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        """Begin pumping PTY output into scrollback (+ attached queue)."""
        self._loop = loop
        loop.add_reader(self.master_fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master_fd, _READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""  # PTY closed (shell exited)
        if data:
            self._append_scrollback(data)
            if self._out_queue is not None:
                self._out_queue.put_nowait(data)
            return
        # EOF: child exited. Stop reading and notify any attached tab.
        self.alive = False
        if self._loop is not None:
            try:
                self._loop.remove_reader(self.master_fd)
            except (ValueError, OSError):
                pass
        if self._out_queue is not None:
            self._out_queue.put_nowait(EOF)

    def _append_scrollback(self, data: bytes) -> None:
        self._scrollback.extend(data)
        overflow = len(self._scrollback) - _SCROLLBACK_MAX_BYTES
        if overflow > 0:
            del self._scrollback[:overflow]

    # ---- attachment (single tab at a time) ------------------------------
    def attach(self, queue: "asyncio.Queue[bytes | None]") -> bytes:
        """Attach a tab's output queue and return scrollback to replay.

        Atomic (no ``await`` between snapshot and assignment) so the background
        reader cannot interleave and drop or duplicate bytes: everything up to
        now is in the returned snapshot; everything after flows via the queue.
        If another tab was attached, it is displaced.
        """
        snapshot = bytes(self._scrollback)
        self._out_queue = queue
        if not self.alive:
            queue.put_nowait(EOF)
        return snapshot

    def detach(self, queue: "asyncio.Queue[bytes | None]") -> None:
        """Detach a tab, but only if it is still the active one."""
        if self._out_queue is queue:
            self._out_queue = None

    def write(self, data: bytes) -> None:
        """Write input to the PTY (best-effort; non-blocking master)."""
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.master_fd, view[:4096])
                view = view[written:]
            except BlockingIOError:
                break  # kernel buffer full; drop the tail rather than block reader loop
            except OSError:
                self.alive = False
                break

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        _set_winsize(self.master_fd, rows, cols)

    def to_public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "cmd": self.cmd,
            "alive": self.alive,
            "attached": self._out_queue is not None,
        }


class TerminalRegistry:
    """Process-wide map of live terminal sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}

    def list(self) -> list[dict[str, object]]:
        return [s.to_public() for s in self._sessions.values()]

    def get(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    def create(self, cwd: str | None, cmd: str | None) -> TerminalSession:
        """Fork a PTY in ``cwd`` running ``cmd`` and register it.

        Raises ``ValueError`` on a bad cwd or when the session cap is reached.
        """
        # Must run on the event-loop thread: we register a PTY reader via
        # loop.add_reader. Fail fast and clearly if called without a loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "TerminalRegistry.create must be called from an async context"
            ) from exc
        if len(self._sessions) >= _MAX_SESSIONS:
            raise ValueError(
                f"too many terminal sessions open (max {_MAX_SESSIONS}); close one first"
            )
        resolved = _resolve_cwd(cwd)
        launch = (cmd or os.environ.get("SHELL") or "/bin/bash").strip()

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: become the shell/command. pty.fork() already called setsid().
            try:
                os.chdir(str(resolved))
            except OSError:
                pass
            # ``-l`` makes a shell a login shell so PATH/aliases resolve (and
            # ``claude`` is found). A non-shell command is exec'd directly.
            if launch.endswith(("bash", "zsh", "sh")):
                os.execvp(launch, [launch, "-l"])
            else:
                os.execvp(launch, [launch])
            os._exit(127)  # exec failed

        # Parent: non-blocking master so the reader never stalls the loop.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        session = TerminalSession(
            id=uuid.uuid4().hex[:12],
            pid=pid,
            master_fd=master_fd,
            cwd=str(resolved),
            cmd=launch,
        )
        session.start_reader(loop)
        self._sessions[session.id] = session
        logger.info(
            "terminal session %s started (pid=%d cwd=%s)", session.id, pid, resolved
        )
        return session

    async def kill(self, session_id: str) -> bool:
        """Terminate and remove a session. Returns False if unknown."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        if session._loop is not None:
            try:
                session._loop.remove_reader(session.master_fd)
            except (ValueError, OSError):
                pass
        # Close the master first so the kernel delivers SIGHUP to the foreground
        # group (reaches ``claude`` if it diverged), then reap off the loop.
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        await asyncio.get_running_loop().run_in_executor(None, _terminate, session.pid)
        session.alive = False
        logger.info("terminal session %s killed", session_id)
        return True

    async def shutdown(self) -> None:
        """Kill every session (called on app shutdown)."""
        for session_id in list(self._sessions):
            await self.kill(session_id)


registry = TerminalRegistry()
