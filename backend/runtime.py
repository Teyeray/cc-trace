"""Phase 6 — Sandboxed workflow runtime.

Re-executes a WorkflowIR under hard safety rules so replaying a distilled
workflow can never wander outside its sandbox:

- **Workspace-root lock**: ``read_file`` paths resolve under one root; anything
  escaping it (``..``, absolute paths elsewhere, symlink-out) is rejected before
  it runs. NOTE: this confines the *process cwd* of ``run_bash`` to the root, but
  cannot constrain what an approved shell command itself references (``cat
  /etc/hosts`` still works) — hence run_bash is additionally approval-gated.
- **Approval gates**: ``approval`` nodes and every ``run_bash`` call must be
  granted by an injected ``approve(node, action)`` callback. The default callback
  **denies**, so nothing dangerous runs unless a caller explicitly opts in.
- **Tool allow-list**: only ``read_file`` (from ``Read``) and ``run_bash`` (from
  ``Bash``) are implemented; ``Edit``/``Write`` are intentionally not wired yet
  (gated for a later phase). Unknown tools are skipped, not guessed.
- **Retry loop**: a failed node is retried up to its ``retry_policy.max_attempts``.

We execute the IR with a small deterministic walker rather than pulling in the
full LangGraph dependency: the WorkflowIR already *is* the graph, and these
constraints (lock + gates + retry) are clearer and safer expressed directly. The
walker can be swapped for a LangGraph backend later without changing the IR.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .workflow_ir import WorkflowIR, WorkflowNode

logger = logging.getLogger("cc_trace.runtime")

# approve(node, action) -> bool. action is e.g. "run_bash" or "approval".
ApprovalFn = Callable[[WorkflowNode, str], bool]

_PARAM_REF = re.compile(r"^\$\{([^}]+)\}$")
_BASH_TIMEOUT_SECONDS = 60


def _deny(_node: WorkflowNode, _action: str) -> bool:
    return False


class SandboxError(Exception):
    """Raised when an action would escape the workspace root."""


@dataclass
class StepResult:
    node_id: str
    tool_name: str | None
    status: str  # success | failed | skipped | denied
    attempts: int = 0
    output: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    workflow_id: str
    status: str  # completed | failed
    steps: list[StepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
        }


class WorkflowRuntime:
    """Executes a WorkflowIR inside a locked workspace with approval gates."""

    def __init__(
        self,
        workspace_root: str | Path,
        approve: ApprovalFn | None = None,
        bash_timeout: int = _BASH_TIMEOUT_SECONDS,
    ) -> None:
        self.root = Path(workspace_root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace root is not a directory: {self.root}")
        self.approve: ApprovalFn = approve or _deny
        self.bash_timeout = bash_timeout

    # ---- sandbox helpers ----
    def _safe_path(self, raw: str) -> Path:
        """Resolve ``raw`` and ensure it stays within the workspace root."""
        candidate = (self.root / raw).expanduser()
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxError(f"path escapes workspace root: {raw}")
        return resolved

    def _resolve(self, value: Any, params: dict[str, Any]) -> Any:
        """Resolve a ``${param}`` reference against provided params."""
        if isinstance(value, str):
            m = _PARAM_REF.match(value.strip())
            if m:
                return params.get(m.group(1))
        return value

    # ---- tools ----
    def _read_file(self, node: WorkflowNode, params: dict[str, Any]) -> str:
        mapping = node.input_mapping
        raw = self._resolve(
            mapping.get("file_path") or mapping.get("path"), params
        )
        if not raw:
            raise ValueError("read_file: no path in input_mapping")
        path = self._safe_path(str(raw))
        return path.read_text(encoding="utf-8", errors="replace")[:10000]

    def _run_bash(self, node: WorkflowNode, params: dict[str, Any]) -> str:
        if not self.approve(node, "run_bash"):
            raise PermissionError("run_bash denied by approval gate")
        command = self._resolve(node.input_mapping.get("command"), params)
        if not command:
            raise ValueError("run_bash: no command in input_mapping")
        proc = subprocess.run(
            str(command),
            shell=True,  # re-running captured shell workflows requires a shell
            cwd=str(self.root),  # workspace-root lock for the process cwd
            capture_output=True,
            text=True,
            timeout=self.bash_timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}: {out[:500]}")
        return out[:10000]

    # ---- execution ----
    def _run_node(self, node: WorkflowNode, params: dict[str, Any]) -> StepResult:
        if node.type in ("start", "end"):
            return StepResult(node.id, None, "success")
        if node.type == "approval":
            granted = self.approve(node, "approval")
            return StepResult(
                node.id, None, "success" if granted else "denied"
            )
        if node.type != "tool_call":
            return StepResult(node.id, node.tool_name, "skipped")

        handler = {"Read": self._read_file, "Bash": self._run_bash}.get(
            node.tool_name or ""
        )
        if handler is None:
            return StepResult(
                node.id, node.tool_name, "skipped",
                error=f"unsupported tool: {node.tool_name}",
            )

        attempts = 0
        last_error: str | None = None
        max_attempts = max(1, node.retry_policy.max_attempts)
        while attempts < max_attempts:
            attempts += 1
            try:
                output = handler(node, params)
                return StepResult(
                    node.id, node.tool_name, "success", attempts, output
                )
            except PermissionError as exc:
                return StepResult(
                    node.id, node.tool_name, "denied", attempts, error=str(exc)
                )
            except Exception as exc:  # retryable
                last_error = str(exc)
        return StepResult(
            node.id, node.tool_name, "failed", attempts, error=last_error
        )

    def run(
        self, workflow: WorkflowIR, params: dict[str, Any] | None = None
    ) -> RunResult:
        """Execute the workflow's nodes in topological (linear) order.

        Stops at the first hard ``failed`` step (a denied bash gate also halts).
        ``skipped`` (unsupported tool) does not halt — it is recorded and we
        continue, so a partially-supported workflow still makes progress.
        """
        params = params or {}
        result = RunResult(workflow_id=workflow.id, status="completed")
        order = _linear_order(workflow)
        for node in order:
            step = self._run_node(node, params)
            result.steps.append(step)
            if step.status in ("failed", "denied"):
                result.status = "failed"
                break
        return result


def _linear_order(workflow: WorkflowIR) -> list[WorkflowNode]:
    """Order nodes by following edges from ``start`` (linear MVP).

    Falls back to node declaration order if no ``start`` edge chain is found, so
    a hand-edited or branched workflow still executes deterministically.
    """
    by_id = {n.id: n for n in workflow.nodes}
    succ: dict[str, list[str]] = {}
    for e in workflow.edges:
        succ.setdefault(e.source, []).append(e.target)

    ordered: list[WorkflowNode] = []
    seen: set[str] = set()
    cursor = "start" if "start" in by_id else (
        workflow.nodes[0].id if workflow.nodes else None
    )
    while cursor and cursor in by_id and cursor not in seen:
        seen.add(cursor)
        ordered.append(by_id[cursor])
        nexts = succ.get(cursor, [])
        if len(nexts) > 1:
            # Linear MVP: branching (condition/loop) is not yet executed. Make
            # the dropped branches visible instead of silently taking nexts[0].
            logger.warning(
                "node %s has %d successors; linear runtime follows only %s "
                "(branches %s not executed)",
                cursor, len(nexts), nexts[0], nexts[1:],
            )
        cursor = nexts[0] if nexts else None

    # Append any nodes not reached by the linear walk (branches/orphans).
    for node in workflow.nodes:
        if node.id not in seen:
            ordered.append(node)
    return ordered
