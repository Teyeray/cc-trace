/**
 * @typedef {Object} ToolDetailData
 * @property {'tool'} kind
 * @property {string} tool
 * @property {'running'|'success'|'failed'} status
 * @property {number} index
 * @property {Object|null} input
 * @property {Object|null} output
 * @property {string|null} startedAt
 * @property {string|null} endedAt
 * @property {number|null} [durationMs]
 * @property {string|null} [toolUseId]
 * @property {string|null} [cwd]
 * @property {string|null} [permissionMode]
 * @property {Object|null} [effort]
 *
 * @typedef {Object} MessageDetailData
 * @property {'message'} kind
 * @property {'user'|'assistant'} role
 * @property {number} index
 * @property {string} text
 * @property {string|null} timestamp
 * @property {string|null} [model]
 * @property {Object} [blocks]
 */

/** @param {unknown} value */
function pretty(value) {
  if (value === null || value === undefined) return '—';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** @param {number} ms */
function formatMs(ms) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/**
 * @param {string|null} startedAt
 * @param {string|null} endedAt
 * @returns {string|null}
 */
function duration(startedAt, endedAt) {
  if (!startedAt || !endedAt) return null;
  const ms = new Date(endedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(ms)) return null;
  return formatMs(ms);
}

/** @param {{ detail: ToolDetailData }} props */
function ToolDetail({ detail }) {
  // Prefer the editor-measured runtime; fall back to receive-timestamp diff.
  const took =
    typeof detail.durationMs === 'number'
      ? formatMs(detail.durationMs)
      : duration(detail.startedAt, detail.endedAt);
  const effortLevel =
    detail.effort && typeof detail.effort === 'object'
      ? detail.effort.level
      : detail.effort;

  return (
    <>
      <header className="detail__head">
        <div className="detail__title">
          <span className="detail__index">#{detail.index}</span>
          <span className="detail__tool">{detail.tool}</span>
          <span className={`detail__status detail__status--${detail.status}`}>
            {detail.status}
          </span>
        </div>
      </header>

      <dl className="detail__meta">
        <div>
          <dt>started</dt>
          <dd>{detail.startedAt ?? '—'}</dd>
        </div>
        <div>
          <dt>duration</dt>
          <dd>{took ?? (detail.status === 'running' ? 'running…' : '—')}</dd>
        </div>
        {detail.permissionMode ? (
          <div>
            <dt>mode</dt>
            <dd>{detail.permissionMode}</dd>
          </div>
        ) : null}
        {effortLevel ? (
          <div>
            <dt>effort</dt>
            <dd>{effortLevel}</dd>
          </div>
        ) : null}
      </dl>

      <section className="detail__section">
        <h3>tool_input</h3>
        <pre className="detail__code">{pretty(detail.input)}</pre>
      </section>

      <section className="detail__section">
        <h3>tool_response</h3>
        <pre className="detail__code">
          {detail.status === 'running'
            ? 'awaiting result…'
            : pretty(detail.output)}
        </pre>
      </section>
    </>
  );
}

/** @param {{ detail: MessageDetailData }} props */
function MessageDetail({ detail }) {
  const isUser = detail.role === 'user';
  const blocks = detail.blocks ?? {};
  const blockSummary = Object.entries(blocks)
    .map(([kind, count]) => `${kind}×${count}`)
    .join('  ');

  return (
    <>
      <header className="detail__head">
        <div className="detail__title">
          <span className="detail__index">#{detail.index}</span>
          <span className="detail__tool">{isUser ? 'you' : 'claude'}</span>
          <span className={`detail__status detail__status--${detail.role}`}>
            {detail.role}
          </span>
        </div>
      </header>

      <dl className="detail__meta">
        <div>
          <dt>time</dt>
          <dd>{detail.timestamp ?? '—'}</dd>
        </div>
        {detail.model ? (
          <div>
            <dt>model</dt>
            <dd>{detail.model}</dd>
          </div>
        ) : null}
        {blockSummary ? (
          <div>
            <dt>blocks</dt>
            <dd>{blockSummary}</dd>
          </div>
        ) : null}
      </dl>

      <section className="detail__section">
        <h3>{isUser ? 'message' : 'response'}</h3>
        <pre className="detail__code detail__code--prose">{detail.text}</pre>
      </section>
    </>
  );
}

/**
 * @param {{ detail: ToolDetailData | MessageDetailData, onClose: () => void }} props
 */
export default function DetailPanel({ detail, onClose }) {
  return (
    <aside className="detail">
      <button className="detail__close" onClick={onClose} aria-label="Close">
        ×
      </button>
      {detail.kind === 'message' ? (
        <MessageDetail detail={detail} />
      ) : (
        <ToolDetail detail={detail} />
      )}
    </aside>
  );
}
