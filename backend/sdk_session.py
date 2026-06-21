"""Programmatic Claude Code sessions via the CLI's streaming SDK transport.

The embedded terminal drives Claude Code *interactively* (a human types into a
PTY) and relies on the project's curl hooks to capture tool calls. This module
adds the complementary *programmatic* path: the backend launches Claude Code
headless and reads its structured event stream directly — the same transport the
official Agent SDK uses.

We shell out to ``claude -p <prompt> --output-format stream-json --verbose``
rather than importing ``claude-agent-sdk`` because that package requires Python
>=3.10 and this project is pinned to 3.9; the CLI's stream-json mode exposes the
identical event stream (system/assistant/user/result) and reuses the user's
existing CLI auth.

Crucially, captured ``tool_use``/``tool_result`` blocks are synthesized into the
SAME hook-envelope shape the curl hooks produce and pushed through
``storage.append_jsonl`` + ``events.broadcaster``. So an SDK-driven session flows
into the existing normalize → replay → WorkflowIR pipeline and live trace graph
with zero changes downstream — hooks and workflow features are fully preserved;
this is an additional source, not a replacement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from . import storage
from .events import broadcaster
from .hooks import shape
from .terminal_sessions import _resolve_cwd

logger = logging.getLogger("cc_trace.sdk")

_CLAUDE_BIN = "claude"

# Explicit allowlist so a direct API caller can't pass an arbitrary value to the
# CLI's --permission-mode. bypassPermissions is included but is a deliberate,
# caller-driven opt-in (it lets tools run unprompted).
_ALLOWED_PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions"}


def _publish_envelope(raw: dict[str, Any]) -> None:
    """Append a synthesized hook payload and publish it to the live stream.

    Mirrors hooks._ingest so SDK-sourced tool calls are indistinguishable from
    curl-hook-sourced ones downstream.
    """
    try:
        envelope = storage.append_jsonl(raw)
        broadcaster.publish(shape(envelope))
    except Exception:  # never let capture failure abort the run
        logger.exception("failed to persist/publish SDK event")


def _normalize_result_content(content: Any, is_error: bool) -> Any:
    """Flatten a tool_result content into something the pipeline understands."""
    if isinstance(content, list):
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        content = text or content
    if is_error:
        return {"is_error": True, "content": content}
    return content


async def run_session(
    prompt: str,
    cwd: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one headless Claude Code turn, yielding lightweight UI events.

    As a side effect, every tool_use/tool_result is captured into the trace
    pipeline (so the graph updates live). Yields dicts like
    ``{"type": "assistant_text"|"tool_use"|"tool_result"|"result"|"error", ...}``.

    ``permission_mode`` is passed through to the CLI when provided (e.g.
    ``acceptEdits``); by default it is omitted, so the CLI's normal permission
    rules apply and nothing destructive runs unprompted.
    """
    resolved = _resolve_cwd(cwd)
    if permission_mode and permission_mode not in _ALLOWED_PERMISSION_MODES:
        raise ValueError(f"invalid permission_mode: {permission_mode!r}")
    args = [
        _CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if model:
        args += ["--model", model]
    if permission_mode:
        args += ["--permission-mode", permission_mode]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(resolved),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    logger.info("SDK session started (pid=%s cwd=%s)", proc.pid, resolved)

    session_id: str | None = None
    # tool_use_id -> (tool_name, tool_input), to fill in the PostToolUse envelope
    pending: dict[str, tuple[str, Any]] = {}

    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("subprocess pipes missing")

    # Drain stderr concurrently. If we only read it after the stdout loop, a
    # subprocess that fills the stderr pipe buffer would block on write while we
    # block on stdout — a classic pipe deadlock.
    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        except Exception:  # best-effort; never abort the run over stderr
            pass

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            session_id = event.get("session_id", session_id)

            if etype == "system":
                yield {"type": "system", "session_id": session_id}

            elif etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        yield {"type": "assistant_text", "text": block["text"]}
                    elif btype == "tool_use":
                        tuid = block.get("id")
                        name = block.get("name")
                        tinput = block.get("input", {})
                        pending[tuid] = (name, tinput)
                        _publish_envelope({
                            "session_id": session_id,
                            "hook_event_name": "PreToolUse",
                            "tool_name": name,
                            "tool_input": tinput,
                            "tool_use_id": tuid,
                        })
                        yield {"type": "tool_use", "id": tuid, "name": name}

            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") != "tool_result":
                        continue
                    tuid = block.get("tool_use_id")
                    name, tinput = pending.get(tuid, (None, None))
                    response = _normalize_result_content(
                        block.get("content"), bool(block.get("is_error"))
                    )
                    _publish_envelope({
                        "session_id": session_id,
                        "hook_event_name": "PostToolUse",
                        "tool_name": name,
                        "tool_input": tinput,
                        "tool_response": response,
                        "tool_use_id": tuid,
                    })
                    yield {
                        "type": "tool_result",
                        "id": tuid,
                        "is_error": bool(block.get("is_error")),
                    }

            elif etype == "result":
                yield {
                    "type": "result",
                    "session_id": session_id,
                    "is_error": bool(event.get("is_error")),
                    "text": event.get("result", ""),
                }

        await proc.wait()
        await stderr_task
        if proc.returncode not in (0, None):
            stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
            yield {"type": "error", "detail": stderr[:500] or f"exit {proc.returncode}"}
    finally:
        # If the caller abandons the generator (client disconnect), make sure the
        # child is actually gone — terminate, then wait, then kill as a backstop.
        stderr_task.cancel()
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
