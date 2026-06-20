"""End-to-end tests for the trace -> normalize -> workflow -> run pipeline.

Each test redirects ``config`` storage paths to a tmp dir (every module reads
``config.X`` at call time, so monkeypatching the attribute is enough) and writes
a synthetic session log, then asserts on the normalized/replayed/workflow output.
"""

from __future__ import annotations

import json
import uuid

import pytest

from backend import config, index_store
from backend.distiller import (
    PatchOp,
    apply_patches,
    heuristic_distill,
)
from backend.normalize import normalize_session
from backend.replay_ir import build_replay_ir
from backend.runtime import SandboxError, WorkflowRuntime
from backend.workflow_ir import (
    WorkflowEdge,
    WorkflowNode,
    build_workflow_ir,
    load_workflow,
    save_workflow,
)


def _envelope(raw: dict, ts: str) -> dict:
    return {"id": uuid.uuid4().hex, "received_at": ts, "raw": raw}


@pytest.fixture
def session(tmp_path, monkeypatch):
    """Write a synthetic session JSONL and point config at the tmp dir."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_EVENTS_DIR", tmp_path / "raw_events")
    monkeypatch.setattr(config, "WORKFLOWS_DIR", tmp_path / "workflows")
    monkeypatch.setattr(config, "INDEX_DB", tmp_path / "index.db")
    config.RAW_EVENTS_DIR.mkdir(parents=True, exist_ok=True)

    sid = "sess-test"
    rows = [
        # exact pairing via tool_use_id
        _envelope({"session_id": sid, "hook_event_name": "PreToolUse",
                   "tool_name": "Bash", "tool_use_id": "t1",
                   "tool_input": {"command": "ls"}}, "2026-06-21T00:00:01Z"),
        _envelope({"session_id": sid, "hook_event_name": "PostToolUse",
                   "tool_name": "Bash", "tool_use_id": "t1",
                   "tool_input": {"command": "ls"},
                   "tool_response": "a\nb", "duration_ms": 12}, "2026-06-21T00:00:02Z"),
        # heuristic pairing (no tool_use_id)
        _envelope({"session_id": sid, "hook_event_name": "PreToolUse",
                   "tool_name": "Read",
                   "tool_input": {"file_path": "/tmp/x"}}, "2026-06-21T00:00:03Z"),
        _envelope({"session_id": sid, "hook_event_name": "PostToolUse",
                   "tool_name": "Read",
                   "tool_input": {"file_path": "/tmp/x"},
                   "tool_response": "data"}, "2026-06-21T00:00:04Z"),
        # failure then retry (same tool + input)
        _envelope({"session_id": sid, "hook_event_name": "PreToolUse",
                   "tool_name": "Bash", "tool_use_id": "t3",
                   "tool_input": {"command": "boom"}}, "2026-06-21T00:00:05Z"),
        _envelope({"session_id": sid, "hook_event_name": "PostToolUse",
                   "tool_name": "Bash", "tool_use_id": "t3",
                   "tool_input": {"command": "boom"},
                   "tool_response": {"is_error": True}}, "2026-06-21T00:00:06Z"),
        _envelope({"session_id": sid, "hook_event_name": "PreToolUse",
                   "tool_name": "Bash", "tool_use_id": "t4",
                   "tool_input": {"command": "boom"}}, "2026-06-21T00:00:07Z"),
        _envelope({"session_id": sid, "hook_event_name": "PostToolUse",
                   "tool_name": "Bash", "tool_use_id": "t4",
                   "tool_input": {"command": "boom"},
                   "tool_response": "ok"}, "2026-06-21T00:00:08Z"),
    ]
    path = config.RAW_EVENTS_DIR / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return sid


def test_normalize_pairs_and_confidence(session):
    calls = normalize_session(session)
    assert len(calls) == 4
    by_tool = [(c.tool_name, c.status, c.match_confidence) for c in calls]
    assert ("Bash", "success", "exact") in by_tool
    assert ("Read", "success", "heuristic") in by_tool
    # the failed attempt and its retry
    statuses = [c.status for c in calls if c.tool_name == "Bash"]
    assert "failed" in statuses and "success" in statuses


def test_replay_ir_marks_retry_edge(session):
    calls = normalize_session(session)
    replay = build_replay_ir(calls, session_id=session)
    assert len(replay.nodes) == 4
    kinds = {e.kind for e in replay.edges}
    assert "sequence" in kinds
    assert "retry" in kinds  # boom-fail -> boom-success


def test_build_and_persist_workflow(session):
    calls = normalize_session(session)
    replay = build_replay_ir(calls, session_id=session)
    wf = build_workflow_ir(replay, name="my flow")
    # start + 4 tool_calls + end
    assert len(wf.nodes) == 6
    assert wf.nodes[0].type == "start" and wf.nodes[-1].type == "end"
    # every tool_call has provenance back to a replay node
    for n in wf.nodes:
        if n.type == "tool_call":
            assert n.provenance.replay_node_id is not None
    save_workflow(wf)
    loaded = load_workflow(wf.id)
    assert loaded is not None and loaded.id == wf.id
    assert len(loaded.nodes) == len(wf.nodes)


def test_build_carries_real_input_and_distills(session):
    # Regression: WorkflowIR must carry structured tool_input (not just a
    # summary), or the distiller can't parameterize and the runtime can't run.
    calls = normalize_session(session)
    wf = build_workflow_ir(build_replay_ir(calls, session_id=session))
    read_node = next(n for n in wf.nodes if n.tool_name == "Read")
    assert read_node.input_mapping.get("file_path") == "/tmp/x"
    patches = heuristic_distill(wf)
    assert any(p.op == "add_parameter" for p in patches)


def test_index_store_roundtrip(session):
    n = index_store.index_session(session)
    assert n == 8
    # idempotent re-index inserts nothing new (honest count, not attempted rows)
    assert index_store.index_session(session) == 0
    sessions = index_store.list_sessions()
    assert any(s["session_id"] == session and s["event_count"] == 8 for s in sessions)
    events = index_store.session_events(session)
    assert len(events) == 8


def test_distiller_reducer_ops():
    # build a tiny workflow by hand
    from backend.workflow_ir import WorkflowIR
    wf = WorkflowIR(
        id="w1", name="n",
        nodes=[
            WorkflowNode(id="start", type="start"),
            WorkflowNode(id="a", type="tool_call", tool_name="Read",
                         input_mapping={"file_path": "/tmp/x"}),
            WorkflowNode(id="end", type="end"),
        ],
        edges=[WorkflowEdge("start", "a"), WorkflowEdge("a", "end")],
    )
    patches = [
        PatchOp("set_name", {"name": "renamed"}),
        PatchOp("add_parameter", {"name": "p", "type": "path"}),
        PatchOp("set_input_mapping", {"node_id": "a", "key": "file_path", "value": "${p}"}),
        PatchOp("insert_approval_before", {"node_id": "a"}),
        PatchOp("bogus", {}),
    ]
    new_wf, applied, warnings = apply_patches(wf, patches)
    assert new_wf.name == "renamed"
    assert any(p.name == "p" for p in new_wf.parameters)
    a = next(n for n in new_wf.nodes if n.id == "a")
    assert a.input_mapping["file_path"] == "${p}"
    # approval node inserted and rewired: start -> approval-a -> a
    assert any(n.id == "approval-a" and n.type == "approval" for n in new_wf.nodes)
    assert any(e.source == "start" and e.target == "approval-a" for e in new_wf.edges)
    assert any(e.source == "approval-a" and e.target == "a" for e in new_wf.edges)
    assert "unknown op: bogus" in warnings
    assert len(applied) == 4
    # purity: original untouched
    assert wf.name == "n"


def test_heuristic_distill_proposes_path_param():
    from backend.workflow_ir import WorkflowIR
    wf = WorkflowIR(
        id="w2", name="n",
        nodes=[WorkflowNode(id="a", type="tool_call", tool_name="Read",
                            input_mapping={"file_path": "/tmp/x"})],
    )
    patches = heuristic_distill(wf)
    assert any(p.op == "add_parameter" for p in patches)
    assert any(p.op == "set_input_mapping" for p in patches)


def test_runtime_sandbox_and_approval(tmp_path):
    (tmp_path / "hello.txt").write_text("hi there", encoding="utf-8")
    from backend.workflow_ir import WorkflowIR
    wf = WorkflowIR(
        id="w3", name="run",
        nodes=[
            WorkflowNode(id="start", type="start"),
            WorkflowNode(id="r", type="tool_call", tool_name="Read",
                         input_mapping={"file_path": "hello.txt"}),
            WorkflowNode(id="b", type="tool_call", tool_name="Bash",
                         input_mapping={"command": "echo ran"}),
            WorkflowNode(id="end", type="end"),
        ],
        edges=[WorkflowEdge("start", "r"), WorkflowEdge("r", "b"),
               WorkflowEdge("b", "end")],
    )

    # default-deny: bash is gated, run halts as failed/denied at bash
    denied = WorkflowRuntime(tmp_path).run(wf)
    statuses = {s.node_id: s.status for s in denied.steps}
    assert statuses["r"] == "success"
    assert statuses["b"] == "denied"
    assert denied.status == "failed"

    # approve bash: full run completes
    ok = WorkflowRuntime(tmp_path, approve=lambda n, a: True).run(wf)
    statuses = {s.node_id: s.status for s in ok.steps}
    assert statuses["b"] == "success"
    assert "ran" in next(s.output for s in ok.steps if s.node_id == "b")
    assert ok.status == "completed"


def test_runtime_rejects_path_escape(tmp_path):
    rt = WorkflowRuntime(tmp_path)
    with pytest.raises(SandboxError):
        rt._safe_path("../../etc/passwd")


def test_retry_policy_lands_on_failed_node(session):
    # The node that previously FAILED (and was retried) should carry the retry
    # policy, not the successful retry attempt.
    calls = normalize_session(session)
    replay = build_replay_ir(calls, session_id=session)
    wf = build_workflow_ir(replay)
    retry_edges = [e for e in replay.edges if e.kind == "retry"]
    assert retry_edges, "fixture should contain a retry"
    failed_node_id = retry_edges[0].source
    failed_node = next(n for n in wf.nodes if n.id == failed_node_id)
    succeeded_node = next(n for n in wf.nodes if n.id == retry_edges[0].target)
    assert failed_node.retry_policy.max_attempts == 3
    assert succeeded_node.retry_policy.max_attempts == 1


def test_remove_node_dedups_bridge_edges():
    from backend.workflow_ir import WorkflowIR
    # diamond: start -> a, start -> mid; a -> mid is not present. Removing `mid`
    # with 2 preds and 1 succ must not create duplicate bridge edges.
    wf = WorkflowIR(
        id="d", name="n",
        nodes=[
            WorkflowNode(id="start", type="start"),
            WorkflowNode(id="p1", type="tool_call", tool_name="Bash"),
            WorkflowNode(id="p2", type="tool_call", tool_name="Bash"),
            WorkflowNode(id="mid", type="tool_call", tool_name="Bash"),
            WorkflowNode(id="end", type="end"),
        ],
        edges=[
            WorkflowEdge("start", "p1"), WorkflowEdge("start", "p2"),
            WorkflowEdge("p1", "mid"), WorkflowEdge("p2", "mid"),
            WorkflowEdge("mid", "end"),
        ],
    )
    new_wf, applied, warnings = apply_patches(wf, [PatchOp("remove_node", {"node_id": "mid"})])
    pairs = [(e.source, e.target) for e in new_wf.edges]
    assert len(pairs) == len(set(pairs)), "bridge edges must be deduplicated"
    assert ("p1", "end") in pairs and ("p2", "end") in pairs
    assert all(e.source != "mid" and e.target != "mid" for e in new_wf.edges)
