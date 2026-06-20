import { Handle, Position } from '@xyflow/react';

/**
 * @typedef {Object} TraceNodeData
 * @property {string} tool
 * @property {'running'|'success'|'failed'} status
 * @property {string} summary
 * @property {number} index
 *
 * @param {{ data: TraceNodeData }} props
 */
export default function TraceNode({ data }) {
  return (
    <div className={`trace-node trace-node--${data.status}`}>
      <Handle type="target" position={Position.Top} />
      <div className="trace-node__head">
        <span className="trace-node__index">{data.index}</span>
        <span className="trace-node__tool">{data.tool}</span>
        <span className={`trace-node__dot trace-node__dot--${data.status}`} />
      </div>
      {data.summary ? (
        <code className="trace-node__summary">{data.summary}</code>
      ) : null}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
