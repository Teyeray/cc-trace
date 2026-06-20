"""Phase 7 — LLM Distiller via auditable PatchOps.

The LLM never emits WorkflowIR directly (it would be an opaque, unverifiable
rewrite). Instead it proposes ``PatchOp`` objects — ``{op, args, confidence}`` —
and a **deterministic reducer** applies them to produce the next WorkflowIR. So
every change is small, reviewable, attributable, and reversible.

This module ships:
- the ``PatchOp`` schema and the reducer (``apply_patches``) — the trustworthy core;
- ``heuristic_distill`` — a deterministic, no-API-key distiller that proposes
  obvious parameterizations (e.g. file-path inputs -> workflow parameters), so
  the pipeline is testable offline;
- ``distill_with_llm`` — an injection point that takes a callable returning raw
  patch dicts (wire a Claude call here) and routes them through the same reducer.

Reducer contract: pure. It deep-copies the input workflow, applies ops in order,
and returns ``(new_workflow, applied, warnings)``. Unknown/invalid ops are
skipped into ``warnings`` rather than raising, so one bad suggestion can't void a
whole batch.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .workflow_ir import (
    Provenance,
    RetryPolicy,
    WorkflowEdge,
    WorkflowIR,
    WorkflowNode,
    WorkflowParameter,
)

# Path-like heuristic: inputs whose key or value smells like a filesystem path.
_PATH_KEYS = {"file_path", "path", "cwd", "directory", "filename", "dir"}


@dataclass
class PatchOp:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PatchOp":
        return cls(
            op=data["op"],
            args=data.get("args", {}),
            confidence=float(data.get("confidence", 1.0)),
        )


def _find_node(workflow: WorkflowIR, node_id: str) -> WorkflowNode | None:
    return next((n for n in workflow.nodes if n.id == node_id), None)


# ---- reducer ------------------------------------------------------------
def apply_patches(
    workflow: WorkflowIR, patches: list[PatchOp]
) -> tuple[WorkflowIR, list[PatchOp], list[str]]:
    """Apply PatchOps deterministically. Pure: returns a new workflow.

    Returns ``(new_workflow, applied, warnings)``. Ops that reference unknown
    nodes or have bad args are recorded in ``warnings`` and skipped.
    """
    wf = copy.deepcopy(workflow)
    applied: list[PatchOp] = []
    warnings: list[str] = []

    for patch in patches:
        try:
            if _apply_one(wf, patch, warnings):
                applied.append(patch)
        except Exception as exc:  # one bad op must not void the batch
            warnings.append(f"{patch.op}: {exc}")

    return wf, applied, warnings


def _apply_one(wf: WorkflowIR, patch: PatchOp, warnings: list[str]) -> bool:
    op, args = patch.op, patch.args

    if op == "set_name":
        wf.name = str(args["name"])
        return True

    if op == "add_parameter":
        name = str(args["name"])
        if any(p.name == name for p in wf.parameters):
            warnings.append(f"add_parameter: '{name}' already exists")
            return False
        wf.parameters.append(
            WorkflowParameter(
                name=name,
                type=args.get("type", "string"),
                default=args.get("default"),
                description=args.get("description"),
            )
        )
        return True

    if op == "set_input_mapping":
        node = _find_node(wf, args["node_id"])
        if node is None:
            warnings.append(f"set_input_mapping: unknown node {args.get('node_id')}")
            return False
        node.input_mapping[str(args["key"])] = args["value"]
        return True

    if op == "set_retry":
        node = _find_node(wf, args["node_id"])
        if node is None:
            warnings.append(f"set_retry: unknown node {args.get('node_id')}")
            return False
        node.retry_policy = RetryPolicy(
            max_attempts=int(args.get("max_attempts", node.retry_policy.max_attempts)),
            backoff_ms=int(args.get("backoff_ms", node.retry_policy.backoff_ms)),
        )
        return True

    if op == "set_node_type":
        node = _find_node(wf, args["node_id"])
        if node is None:
            warnings.append(f"set_node_type: unknown node {args.get('node_id')}")
            return False
        node.type = args["type"]
        return True

    if op == "insert_approval_before":
        return _insert_approval_before(wf, args, warnings)

    if op == "remove_node":
        return _remove_node(wf, args, warnings)

    warnings.append(f"unknown op: {op}")
    return False


def _insert_approval_before(
    wf: WorkflowIR, args: dict[str, Any], warnings: list[str]
) -> bool:
    target_id = args["node_id"]
    if _find_node(wf, target_id) is None:
        warnings.append(f"insert_approval_before: unknown node {target_id}")
        return False
    approval_id = f"approval-{target_id}"
    if _find_node(wf, approval_id) is not None:
        warnings.append(f"insert_approval_before: {approval_id} already exists")
        return False
    wf.nodes.append(
        WorkflowNode(
            id=approval_id,
            type="approval",
            provenance=Provenance(note=f"gate before {target_id}"),
        )
    )
    # Rewire: every edge X->target becomes X->approval, plus approval->target.
    for edge in wf.edges:
        if edge.target == target_id:
            edge.target = approval_id
    wf.edges.append(WorkflowEdge(approval_id, target_id))
    return True


def _remove_node(wf: WorkflowIR, args: dict[str, Any], warnings: list[str]) -> bool:
    node_id = args["node_id"]
    if node_id in ("start", "end"):
        warnings.append("remove_node: cannot remove start/end")
        return False
    if _find_node(wf, node_id) is None:
        warnings.append(f"remove_node: unknown node {node_id}")
        return False
    preds = [e.source for e in wf.edges if e.target == node_id]
    succs = [e.target for e in wf.edges if e.source == node_id]
    wf.nodes = [n for n in wf.nodes if n.id != node_id]
    wf.edges = [e for e in wf.edges if node_id not in (e.source, e.target)]
    # Bridge predecessors to successors so the graph stays connected, then
    # dedup: the cross-product can re-create an edge that already exists (or be
    # produced twice when remove_node runs on adjacent nodes), which would make
    # the linear walker visit a node more than once.
    for p in preds:
        for s in succs:
            wf.edges.append(WorkflowEdge(p, s))
    seen: set[tuple[str, str, str | None]] = set()
    deduped: list[WorkflowEdge] = []
    for e in wf.edges:
        key = (e.source, e.target, e.condition)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    wf.edges = deduped
    return True


# ---- distillers ---------------------------------------------------------
def heuristic_distill(workflow: WorkflowIR) -> list[PatchOp]:
    """Deterministic, offline distiller proposing obvious parameterizations.

    Detects path-like inputs on tool_call nodes and proposes a workflow
    parameter plus an input mapping that references it. No LLM, so the whole
    PatchOps pipeline is testable without an API key.
    """
    patches: list[PatchOp] = []
    seen_params: set[str] = {p.name for p in workflow.parameters}
    counter = 0

    for node in workflow.nodes:
        if node.type != "tool_call":
            continue
        for key, value in node.input_mapping.items():
            looks_path = key in _PATH_KEYS or (
                isinstance(value, str) and value.startswith("/")
            )
            if not looks_path:
                continue
            if key not in seen_params:
                param_name = key
            else:
                param_name = f"{key}_{counter}"
                counter += 1
            seen_params.add(param_name)
            patches.append(
                PatchOp(
                    op="add_parameter",
                    args={
                        "name": param_name,
                        "type": "path",
                        "default": value if isinstance(value, str) else None,
                        "description": f"path input for {node.tool_name or node.id}",
                    },
                    confidence=0.6,
                )
            )
            patches.append(
                PatchOp(
                    op="set_input_mapping",
                    args={
                        "node_id": node.id,
                        "key": key,
                        "value": f"${{{param_name}}}",
                    },
                    confidence=0.6,
                )
            )
    return patches


def distill_with_llm(
    workflow: WorkflowIR,
    propose: Callable[[WorkflowIR], list[dict[str, Any]]],
) -> tuple[WorkflowIR, list[PatchOp], list[str]]:
    """Run an injected proposer (e.g. a Claude call) through the reducer.

    ``propose`` receives the current workflow and returns raw patch dicts. We
    parse them into PatchOps and apply via the same deterministic reducer, so an
    LLM never mutates the IR directly. Returns the reducer's
    ``(new_workflow, applied, warnings)``.
    """
    raw = propose(workflow)
    patches: list[PatchOp] = []
    warnings: list[str] = []
    for item in raw:
        try:
            patches.append(PatchOp.from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"malformed patch {item!r}: {exc}")
    wf, applied, reducer_warnings = apply_patches(workflow, patches)
    return wf, applied, warnings + reducer_warnings
