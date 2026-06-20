import { useCallback, useEffect, useMemo, useState } from 'react';
import { ReactFlow, Background, Controls } from '@xyflow/react';
import { useWorkflows } from '../../lib/useWorkflows.js';

const NODE_GAP_Y = 90;
const NODE_X = 40;

/** Lay a WorkflowIR out vertically by walking edges from `start`. */
function layout(workflow) {
  if (!workflow) return { nodes: [], edges: [] };
  const rawNodes = workflow.nodes ?? [];
  const rawEdges = workflow.edges ?? [];
  if (rawNodes.length === 0) return { nodes: [], edges: [] };
  const byId = new Map(rawNodes.map((n) => [n.id, n]));
  const succ = new Map();
  for (const e of rawEdges) {
    if (!succ.has(e.source)) succ.set(e.source, []);
    succ.get(e.source).push(e.target);
  }
  // order: linear walk from start, then append any unreached nodes
  const order = [];
  const seen = new Set();
  let cursor = byId.has('start') ? 'start' : workflow.nodes[0]?.id;
  while (cursor && byId.has(cursor) && !seen.has(cursor)) {
    seen.add(cursor);
    order.push(cursor);
    cursor = (succ.get(cursor) ?? [])[0];
  }
  for (const n of workflow.nodes) if (!seen.has(n.id)) order.push(n.id);

  const pos = new Map(order.map((id, i) => [id, i]));
  const nodes = workflow.nodes.map((n) => {
    const label =
      n.type === 'tool_call' ? `${n.tool_name ?? '?'}` : n.type.toUpperCase();
    const params = Object.keys(n.input_mapping ?? {}).join(', ');
    return {
      id: n.id,
      position: { x: NODE_X, y: (pos.get(n.id) ?? 0) * NODE_GAP_Y },
      data: {
        label: params ? `${label}\n${params}`.slice(0, 60) : label,
      },
      className: `wf-node wf-node--${n.type}`,
      sourcePosition: 'bottom',
      targetPosition: 'top',
    };
  });
  const edges = workflow.edges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    animated: false,
  }));
  return { nodes, edges };
}

/**
 * Surfaces the workflow side of the pipeline: build a WorkflowIR from the
 * current trace session, view it as a graph, distill parameterizations, and
 * run it in the sandbox.
 *
 * @param {{ traceSessionId: string|null }} props
 */
export default function WorkflowPanel({ traceSessionId }) {
  const { workflows, build, get, distill, run } = useWorkflows();
  const [selectedId, setSelectedId] = useState(null);
  const [workflow, setWorkflow] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [approveBash, setApproveBash] = useState(false);

  // Load the selected workflow's full IR.
  useEffect(() => {
    if (!selectedId) {
      setWorkflow(null);
      return;
    }
    let active = true;
    get(selectedId)
      .then((wf) => active && setWorkflow(wf))
      .catch((e) => active && setError(e.message));
    return () => {
      active = false;
    };
  }, [selectedId, get]);

  const { nodes, edges } = useMemo(() => layout(workflow), [workflow]);

  const guard = useCallback(async (fn) => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const handleBuild = () =>
    guard(async () => {
      if (!traceSessionId) throw new Error('select a trace session first');
      const wf = await build(traceSessionId);
      setSelectedId(wf.id);
    });

  const handleDistill = () =>
    guard(async () => {
      const out = await distill(selectedId, true);
      setResult({ kind: 'distill', data: out });
      if (out.workflow) setWorkflow(out.workflow); // guard: don't blank the graph
    });

  const handleRun = () =>
    guard(async () => {
      const out = await run(selectedId, { approve_bash: approveBash });
      setResult({ kind: 'run', data: out });
    });

  return (
    <div className="wf">
      <div className="wf__bar">
        <select
          className="wf__select"
          value={selectedId ?? ''}
          onChange={(e) => setSelectedId(e.target.value || null)}
        >
          <option value="">
            {workflows.length ? 'select a workflow' : 'no workflows yet'}
          </option>
          {workflows.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name?.slice(0, 28) ?? w.id} · {w.node_count}n
            </option>
          ))}
        </select>
        <button type="button" onClick={handleBuild} disabled={busy}>
          build from trace
        </button>
        <button type="button" onClick={handleDistill} disabled={busy || !selectedId}>
          distill
        </button>
        <label className="wf__check">
          <input
            type="checkbox"
            checked={approveBash}
            onChange={(e) => setApproveBash(e.target.checked)}
          />
          approve bash
        </label>
        <button
          type="button"
          className="wf__run"
          onClick={handleRun}
          disabled={busy || !selectedId}
        >
          run
        </button>
      </div>

      {error ? <p className="wf__error">{error}</p> : null}

      <div className="wf__canvas">
        {workflow ? (
          <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        ) : (
          <div className="wf__empty">
            Build a workflow from the current trace session, or pick a saved one.
          </div>
        )}
      </div>

      {result ? <ResultPane result={result} /> : null}
    </div>
  );
}

function ResultPane({ result }) {
  if (result.kind === 'distill') {
    const { patches = [], warnings = [] } = result.data ?? {};
    return (
      <div className="wf__result">
        <h4>distilled · {patches.length} patch(es)</h4>
        <ul>
          {patches.map((p, i) => (
            <li key={i}>
              <code>{p.op}</code> {JSON.stringify(p.args)}
            </li>
          ))}
        </ul>
        {warnings.length ? <p className="wf__warn">{warnings.join('; ')}</p> : null}
      </div>
    );
  }
  const { status, steps = [] } = result.data ?? {};
  return (
    <div className="wf__result">
      <h4>
        run · <span className={`wf__status wf__status--${status}`}>{status}</span>
      </h4>
      <ul>
        {steps.map((s) => (
          <li key={s.node_id}>
            <span className={`wf__status wf__status--${s.status}`}>{s.status}</span>{' '}
            <code>{s.node_id}</code>
            {s.tool_name ? ` (${s.tool_name})` : ''}
            {s.error ? ` — ${s.error}` : ''}
          </li>
        ))}
      </ul>
    </div>
  );
}
