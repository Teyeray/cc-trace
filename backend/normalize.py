"""Phase 3 — Trace Normalizer: raw hook events -> NormalizedToolCall timeline.

A raw session log interleaves ``PreToolUse`` and ``PostToolUse`` envelopes (plus
``Stop``). This module pairs them into one ``NormalizedToolCall`` per tool
execution, recording HOW confident the pairing is so downstream IR never
silently invents structure:

- ``exact``     paired by ``tool_use_id`` (Claude Code's own id; best case)
- ``heuristic`` paired by (tool_name + input hash) to the nearest still-open
  pre-event in the same session
- ``ambiguous`` a post matched more than one candidate; we pick the nearest but
  flag it
- ``unmatched`` a pre with no post (still running / crashed) or a post with no
  pre (log started mid-flight)

Pairing is best-effort by design: ``tool_use_id`` is not guaranteed in the hook
payload (see the plan's BLOCKER 2), so the heuristic is the real workhorse.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from . import config
from .hooks import shape

MatchConfidence = Literal["exact", "heuristic", "ambiguous", "unmatched"]
CallStatus = Literal["running", "success", "failed"]


def _input_hash(tool_input: Any) -> str:
    """Stable hash of a tool input for heuristic pairing."""
    try:
        blob = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(tool_input)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _looks_failed(tool_response: Any) -> bool:
    """Best-effort failure detection from a tool response payload."""
    if isinstance(tool_response, dict):
        if tool_response.get("is_error") or tool_response.get("error"):
            return True
        status = str(tool_response.get("status", "")).lower()
        if status in ("error", "failed"):
            return True
    if isinstance(tool_response, str):
        head = tool_response.strip().lower()
        return head.startswith("error") or head.startswith("traceback")
    return False


@dataclass
class NormalizedToolCall:
    """One paired tool execution on the session timeline."""

    index: int
    session_id: str | None
    tool_name: str | None
    tool_input: Any
    tool_response: Any
    status: CallStatus
    match_confidence: MatchConfidence
    tool_use_id: str | None = None
    input_hash: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float | None = None
    pre_event_id: str | None = None
    post_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _OpenPre:
    """A pre-event awaiting its post."""

    event: dict[str, Any]
    input_hash: str
    matched: bool = False


def load_shaped_events(session_id: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Read a session's JSONL and return shaped events in append order."""
    path = config.RAW_EVENTS_DIR / f"{session_id}.jsonl"
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
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
    return events


def normalize_events(events: list[dict[str, Any]]) -> list[NormalizedToolCall]:
    """Pair pre/post events into a NormalizedToolCall timeline.

    Single forward pass. Open pre-events are indexed by ``tool_use_id`` (for
    exact matching) and kept in insertion order (for heuristic nearest-open
    matching). Leftover opens at the end are emitted as ``running``/unmatched.
    """
    calls: list[NormalizedToolCall] = []
    open_by_id: dict[str, _OpenPre] = {}
    open_order: list[_OpenPre] = []  # insertion-ordered, for heuristic nearest

    def _emit(call: NormalizedToolCall) -> None:
        call.index = len(calls)
        calls.append(call)

    for event in events:
        hook = event.get("hook_event_name")
        if hook == "PreToolUse":
            ihash = _input_hash(event.get("tool_input"))
            pre = _OpenPre(event=event, input_hash=ihash)
            open_order.append(pre)
            tuid = event.get("tool_use_id")
            if tuid:
                open_by_id[tuid] = pre
        elif hook == "PostToolUse":
            _pair_post(event, open_by_id, open_order, _emit)
        # Stop / PreCompact / others carry no tool fields — skip for the timeline.

    # Unclosed pre-events: the tool never reported a result (still running or
    # the session ended mid-call).
    for pre in open_order:
        if pre.matched:
            continue
        ev = pre.event
        _emit(
            NormalizedToolCall(
                index=0,
                session_id=ev.get("session_id"),
                tool_name=ev.get("tool_name"),
                tool_input=ev.get("tool_input"),
                tool_response=None,
                status="running",
                match_confidence="unmatched",
                tool_use_id=ev.get("tool_use_id"),
                input_hash=pre.input_hash,
                started_at=ev.get("received_at"),
                pre_event_id=ev.get("id"),
            )
        )

    calls.sort(key=lambda c: (c.started_at or c.ended_at or "", c.index))
    for i, call in enumerate(calls):
        call.index = i
    return calls


def _pair_post(
    event: dict[str, Any],
    open_by_id: dict[str, "_OpenPre"],
    open_order: list["_OpenPre"],
    emit,
) -> None:
    """Match one post-event to an open pre and emit the paired call."""
    tuid = event.get("tool_use_id")
    confidence: MatchConfidence
    pre: _OpenPre | None = None

    # 1. Exact: tool_use_id.
    if tuid and tuid in open_by_id and not open_by_id[tuid].matched:
        pre = open_by_id[tuid]
        confidence = "exact"
    else:
        # 2. Heuristic: same tool + input hash, nearest still-open pre.
        ihash = _input_hash(event.get("tool_input"))
        candidates = [
            p
            for p in open_order
            if not p.matched
            and p.event.get("tool_name") == event.get("tool_name")
            and p.input_hash == ihash
        ]
        if candidates:
            pre = candidates[-1]  # nearest open (latest registered)
            confidence = "ambiguous" if len(candidates) > 1 else "heuristic"
        else:
            confidence = "unmatched"

    response = event.get("tool_response")
    status: CallStatus = "failed" if _looks_failed(response) else "success"

    if pre is None:
        # Post with no locatable pre: log likely started mid-flight.
        emit(
            NormalizedToolCall(
                index=0,
                session_id=event.get("session_id"),
                tool_name=event.get("tool_name"),
                tool_input=event.get("tool_input"),
                tool_response=response,
                status=status,
                match_confidence="unmatched",
                tool_use_id=tuid,
                input_hash=_input_hash(event.get("tool_input")),
                ended_at=event.get("received_at"),
                duration_ms=event.get("duration_ms"),
                post_event_id=event.get("id"),
            )
        )
        return

    pre.matched = True
    pre_ev = pre.event
    emit(
        NormalizedToolCall(
            index=0,
            session_id=pre_ev.get("session_id") or event.get("session_id"),
            tool_name=pre_ev.get("tool_name") or event.get("tool_name"),
            tool_input=pre_ev.get("tool_input"),
            tool_response=response,
            status=status,
            match_confidence=confidence,
            tool_use_id=tuid or pre_ev.get("tool_use_id"),
            input_hash=pre.input_hash,
            started_at=pre_ev.get("received_at"),
            ended_at=event.get("received_at"),
            duration_ms=event.get("duration_ms"),
            pre_event_id=pre_ev.get("id"),
            post_event_id=event.get("id"),
        )
    )


def normalize_session(session_id: str, limit: int = 5000) -> list[NormalizedToolCall]:
    """Convenience: load a session's events and normalize them."""
    return normalize_events(load_shaped_events(session_id, limit=limit))
