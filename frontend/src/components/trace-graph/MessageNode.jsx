import { Handle, Position } from '@xyflow/react';

const MAX_PREVIEW = 140;

/** @param {string} text */
function preview(text) {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > MAX_PREVIEW ? `${flat.slice(0, MAX_PREVIEW)}…` : flat;
}

/**
 * A conversation turn node (user prompt or assistant answer).
 *
 * @param {{ data: { role: 'user'|'assistant', text: string, model: string|null, blocks: Object, index: number } }} props
 */
export default function MessageNode({ data }) {
  const isUser = data.role === 'user';
  const toolUses = data.blocks?.tool_use ?? 0;

  return (
    <div className={`msg-node msg-node--${data.role}`}>
      <Handle type="target" position={Position.Top} />
      <div className="msg-node__head">
        <span className="msg-node__index">#{data.index}</span>
        <span className="msg-node__role">{isUser ? 'you' : 'claude'}</span>
        {!isUser && toolUses > 0 ? (
          <span className="msg-node__badge">⚒ {toolUses}</span>
        ) : null}
      </div>
      <p className="msg-node__text">{preview(data.text)}</p>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
