"""Phase 5 — WorkflowIR: a reusable, parameterized abstraction over a trace.

ReplayIR is *what happened once*. WorkflowIR is *what to do again*: a typed graph
of nodes (start/end/tool_call/condition/loop/approval) with input/output
mappings, retry policy, and **mandatory provenance** — every node records the
replay node and source event ids it came from, so an abstraction can always be
traced back to facts and audited.

This module builds a linear WorkflowIR from a ReplayGraph (the deterministic
baseline), and persists/loads workflows as JSON under ``.data/workflows``. The
LLM distiller (Phase 7) edits this IR only through reviewable PatchOps — it never
writes the structure directly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from . import config
from .replay_ir import ReplayGraph

NodeType = Literal["start", "end", "tool_call", "condition", "loop", "approval"]
ParamType = Literal["path", "string", "number", "enum"]

WORKFLOW_SCHEMA_VERSION = 1


@dataclass
class Provenance:
    """Where a workflow node came from. Mandatory for traceability."""

    session_id: str | None = None
    replay_node_id: str | None = None
    pre_event_id: str | None = None
    post_event_id: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowParameter:
    name: str
    type: ParamType = "string"
    default: Any = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowNode:
    id: str
    type: NodeType
    tool_name: str | None = None
    # input_mapping: param/expr -> value or "${param}" reference.
    input_mapping: dict[str, Any] = field(default_factory=dict)
    output_mapping: dict[str, Any] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    provenance: Provenance = field(default_factory=Provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "tool_name": self.tool_name,
            "input_mapping": self.input_mapping,
            "output_mapping": self.output_mapping,
            "retry_policy": self.retry_policy.to_dict(),
            "provenance": self.provenance.to_dict(),
        }


@dataclass
class WorkflowEdge:
    source: str
    target: str
    condition: str | None = None  # for condition/loop branches

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowIR:
    id: str
    name: str
    schema_version: int = WORKFLOW_SCHEMA_VERSION
    parameters: list[WorkflowParameter] = field(default_factory=list)
    nodes: list[WorkflowNode] = field(default_factory=list)
    edges: list[WorkflowEdge] = field(default_factory=list)
    source_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "schema_version": self.schema_version,
            "parameters": [p.to_dict() for p in self.parameters],
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "source_session_id": self.source_session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowIR":
        return cls(
            id=data["id"],
            name=data.get("name", "untitled"),
            schema_version=data.get("schema_version", WORKFLOW_SCHEMA_VERSION),
            parameters=[
                WorkflowParameter(**p) for p in data.get("parameters", [])
            ],
            nodes=[_node_from_dict(n) for n in data.get("nodes", [])],
            edges=[WorkflowEdge(**e) for e in data.get("edges", [])],
            source_session_id=data.get("source_session_id"),
        )


def _node_from_dict(data: dict[str, Any]) -> WorkflowNode:
    return WorkflowNode(
        id=data["id"],
        type=data["type"],
        tool_name=data.get("tool_name"),
        input_mapping=data.get("input_mapping", {}),
        output_mapping=data.get("output_mapping", {}),
        retry_policy=RetryPolicy(**data.get("retry_policy", {})),
        provenance=Provenance(**data.get("provenance", {})),
    )


def build_workflow_ir(replay: ReplayGraph, name: str | None = None) -> WorkflowIR:
    """Build a linear WorkflowIR baseline from a ReplayGraph.

    Each replay tool node becomes a ``tool_call`` with literal inputs and full
    provenance. Retry edges raise the target's ``max_attempts``. Parallel
    candidates are not yet branched (linear MVP) — the distiller can introduce
    parallelism/conditions later via PatchOps.
    """
    wf_id = uuid.uuid4().hex[:12]
    nodes: list[WorkflowNode] = [WorkflowNode(id="start", type="start")]
    edges: list[WorkflowEdge] = []

    # A retry edge runs failed-call -> successful-retry, so the node that should
    # carry a retry policy is the *source* (the call that failed and had to be
    # re-attempted), not the target (the attempt that already succeeded).
    retry_targets = {e.source for e in replay.edges if e.kind == "retry"}

    prev_id = "start"
    for rn in replay.nodes:
        # Carry the real structured input so the runtime can execute it and the
        # distiller can parameterize actual fields. Non-dict inputs are wrapped.
        if isinstance(rn.tool_input, dict):
            input_mapping: dict[str, Any] = dict(rn.tool_input)
        elif rn.tool_input is not None:
            input_mapping = {"value": rn.tool_input}
        else:
            input_mapping = {}
        node = WorkflowNode(
            id=rn.id,
            type="tool_call",
            tool_name=rn.tool_name,
            input_mapping=input_mapping,
            retry_policy=RetryPolicy(max_attempts=3 if rn.id in retry_targets else 1),
            provenance=Provenance(
                session_id=replay.session_id,
                replay_node_id=rn.id,
                pre_event_id=rn.pre_event_id,
                post_event_id=rn.post_event_id,
                note=rn.input_summary,
            ),
        )
        nodes.append(node)
        edges.append(WorkflowEdge(prev_id, rn.id))
        prev_id = rn.id

    nodes.append(WorkflowNode(id="end", type="end"))
    edges.append(WorkflowEdge(prev_id, "end"))

    return WorkflowIR(
        id=wf_id,
        name=name or f"workflow from {replay.session_id or 'session'}",
        nodes=nodes,
        edges=edges,
        source_session_id=replay.session_id,
    )


# ---- persistence --------------------------------------------------------
def save_workflow(workflow: WorkflowIR) -> str:
    """Persist a workflow as JSON. Returns the file path."""
    config.WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.WORKFLOWS_DIR / f"{workflow.id}.json"
    path.write_text(
        json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


def load_workflow(workflow_id: str) -> WorkflowIR | None:
    path = config.WORKFLOWS_DIR / f"{workflow_id}.json"
    if not path.exists():
        return None
    return WorkflowIR.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_workflows() -> list[dict[str, Any]]:
    config.WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for path in sorted(config.WORKFLOWS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append(
            {
                "id": data.get("id", path.stem),
                "name": data.get("name"),
                "node_count": len(data.get("nodes", [])),
                "source_session_id": data.get("source_session_id"),
            }
        )
    return out
