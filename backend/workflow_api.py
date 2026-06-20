"""Read/build/run API for the normalize -> replay -> workflow -> run pipeline.

Kept separate from ``api.py`` (the live trace stream) so each router stays
focused. All endpoints are typed and return plain dicts the React views consume.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import config, index_store
from .distiller import apply_patches, heuristic_distill
from .normalize import normalize_session
from .replay_ir import build_replay_ir
from .runtime import WorkflowRuntime
from .workflow_ir import (
    WorkflowNode,
    build_workflow_ir,
    list_workflows,
    load_workflow,
    save_workflow,
)

router = APIRouter(tags=["workflow"])


@router.get("/normalize")
def get_normalized(session_id: str, limit: int = Query(5000, ge=1, le=20000)):
    calls = normalize_session(session_id, limit=limit)
    return {"calls": [c.to_dict() for c in calls]}


@router.get("/replay")
def get_replay(session_id: str, limit: int = Query(5000, ge=1, le=20000)):
    calls = normalize_session(session_id, limit=limit)
    return build_replay_ir(calls, session_id=session_id).to_dict()


@router.post("/workflows/build")
def build_from_session(
    session_id: str, name: str | None = None
) -> dict[str, Any]:
    calls = normalize_session(session_id)
    if not calls:
        raise HTTPException(status_code=404, detail="no events for session")
    replay = build_replay_ir(calls, session_id=session_id)
    workflow = build_workflow_ir(replay, name=name)
    save_workflow(workflow)
    return workflow.to_dict()


@router.get("/workflows")
def get_workflows() -> dict[str, list[dict[str, Any]]]:
    return {"workflows": list_workflows()}


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict[str, Any]:
    workflow = load_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="unknown workflow")
    return workflow.to_dict()


@router.post("/workflows/{workflow_id}/distill")
def distill_workflow(
    workflow_id: str, persist: bool = False
) -> dict[str, Any]:
    """Propose + apply heuristic parameterizations (offline, no API key).

    Returns the patches, any warnings, and the resulting workflow. Only writes
    back when ``persist`` is true, so distillation can be previewed first.
    """
    workflow = load_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="unknown workflow")
    patches = heuristic_distill(workflow)
    new_wf, applied, warnings = apply_patches(workflow, patches)
    if persist:
        save_workflow(new_wf)
    return {
        "patches": [p.to_dict() for p in applied],
        "warnings": warnings,
        "workflow": new_wf.to_dict(),
        "persisted": persist,
    }


class RunRequest(BaseModel):
    params: dict[str, Any] = {}
    workspace_root: str | None = None
    approve_bash: bool = False  # default-deny: bash only runs when opted in
    approve_gates: bool = False  # default-deny: human `approval` nodes


def _approval(approve_bash: bool, approve_gates: bool):
    """Route each action to its own switch so a bash opt-in cannot silently
    satisfy a human approval gate (they are distinct trust decisions)."""

    def approve(_node, action: str) -> bool:
        if action == "run_bash":
            return approve_bash
        if action == "approval":
            return approve_gates
        return False

    return approve


@router.post("/workflows/{workflow_id}/run")
def run_workflow(workflow_id: str, body: RunRequest) -> dict[str, Any]:
    workflow = load_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="unknown workflow")
    root = body.workspace_root or str(config.PROJECT_ROOT)
    try:
        runtime = WorkflowRuntime(
            workspace_root=root,
            approve=_approval(body.approve_bash, body.approve_gates),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return runtime.run(workflow, params=body.params).to_dict()


@router.post("/index/reindex")
def reindex() -> dict[str, Any]:
    return {"indexed": index_store.reindex_all()}


@router.get("/index/sessions")
def index_sessions() -> dict[str, list[dict[str, Any]]]:
    return {"sessions": index_store.list_sessions()}
