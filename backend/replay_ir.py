"""Phase 4 — ReplayIR: the real execution graph of a session.

Turns the ``NormalizedToolCall`` timeline into a node/edge graph describing what
actually happened: nodes are tool executions; edges encode *sequence*, *retry*
(a failed call immediately re-attempted with the same input), and
*parallel-candidate* (calls whose time windows overlap — they *could* run
concurrently in a workflow). This is facts, not abstraction — Phase 5 turns it
into a reusable WorkflowIR.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .normalize import NormalizedToolCall

EdgeKind = Literal["sequence", "retry", "parallel-candidate"]

_SUMMARY_MAX = 120


def _summarize(value: Any) -> str:
    """One-line, bounded summary of an input/output payload."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text[:_SUMMARY_MAX] + ("…" if len(text) > _SUMMARY_MAX else "")


def _overlaps(a: NormalizedToolCall, b: NormalizedToolCall) -> bool:
    """True if two calls' [started, ended] windows overlap (best-effort)."""
    if not (a.started_at and a.ended_at and b.started_at and b.ended_at):
        return False
    return a.started_at < b.ended_at and b.started_at < a.ended_at


@dataclass
class ReplayNode:
    id: str
    index: int
    tool_name: str | None
    status: str
    match_confidence: str
    input_summary: str
    output_summary: str
    duration_ms: float | None
    input_hash: str | None
    pre_event_id: str | None
    post_event_id: str | None
    # Structured tool input, carried through so WorkflowIR/runtime/distiller can
    # operate on real fields (file_path, command, ...) rather than a summary.
    tool_input: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayEdge:
    source: str
    target: str
    kind: EdgeKind

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayGraph:
    session_id: str | None
    nodes: list[ReplayNode] = field(default_factory=list)
    edges: list[ReplayEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }


def build_replay_ir(
    calls: list[NormalizedToolCall], session_id: str | None = None
) -> ReplayGraph:
    """Construct a ReplayGraph from a normalized timeline."""
    nodes = [
        ReplayNode(
            id=f"call-{call.index}",
            index=call.index,
            tool_name=call.tool_name,
            status=call.status,
            match_confidence=call.match_confidence,
            input_summary=_summarize(call.tool_input),
            output_summary=_summarize(call.tool_response),
            duration_ms=call.duration_ms,
            input_hash=call.input_hash,
            pre_event_id=call.pre_event_id,
            post_event_id=call.post_event_id,
            tool_input=call.tool_input,
        )
        for call in calls
    ]

    edges: list[ReplayEdge] = []
    for i in range(1, len(calls)):
        prev, cur = calls[i - 1], calls[i]
        # retry: same tool + identical input directly after a failure.
        if (
            prev.status == "failed"
            and prev.tool_name == cur.tool_name
            and prev.input_hash == cur.input_hash
        ):
            kind: EdgeKind = "retry"
        else:
            kind = "sequence"
        edges.append(ReplayEdge(f"call-{i - 1}", f"call-{i}", kind))

    # parallel-candidate: any pair of overlapping windows (besides the direct
    # sequence edge already added).
    for i in range(len(calls)):
        for j in range(i + 1, len(calls)):
            if j == i + 1:
                continue  # already a sequence/retry edge
            if _overlaps(calls[i], calls[j]):
                edges.append(
                    ReplayEdge(f"call-{i}", f"call-{j}", "parallel-candidate")
                )

    return ReplayGraph(session_id=session_id, nodes=nodes, edges=edges)
