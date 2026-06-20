import { useState } from 'react';
import NewSessionDialog from './NewSessionDialog.jsx';

/** Short, readable label for a session tab: the cwd basename + a cmd hint. */
function tabLabel(session) {
  const base = session.cwd?.split('/').filter(Boolean).pop() || '/';
  const kind = session.cmd?.endsWith('claude') ? 'claude' : 'sh';
  return `${base} · ${kind}`;
}

/**
 * Tab strip across terminal sessions, with a "+" to launch a new one. Switching
 * tabs only changes which PTY the pane is attached to; background sessions keep
 * running.
 *
 * @param {{
 *   sessions: Array<{id: string, cwd: string, cmd: string, alive: boolean}>,
 *   activeId: string|null,
 *   defaultCwd?: string,
 *   onSelect: (id: string) => void,
 *   onCreate: (opts: {cwd: string, cmd: string}) => Promise<{id: string}>,
 *   onClose: (id: string) => void,
 * }} props
 */
export default function TerminalTabs({
  sessions,
  activeId,
  defaultCwd,
  onSelect,
  onCreate,
  onClose,
}) {
  const [showDialog, setShowDialog] = useState(false);

  const recentCwds = [...new Set(sessions.map((s) => s.cwd))];

  const handleCreate = async (opts) => {
    const session = await onCreate(opts);
    if (session?.id) onSelect(session.id);
  };

  return (
    <div className="term-tabs">
      <div className="term-tabs__list" role="tablist">
        {sessions.map((session) => (
          <div
            key={session.id}
            role="tab"
            aria-selected={session.id === activeId}
            tabIndex={0}
            className={`term-tab ${session.id === activeId ? 'term-tab--active' : ''} ${
              session.alive ? '' : 'term-tab--dead'
            }`}
            onClick={() => onSelect(session.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') onSelect(session.id);
            }}
            title={`${session.cwd} — ${session.cmd}`}
          >
            <span className="term-tab__label">{tabLabel(session)}</span>
            <button
              type="button"
              className="term-tab__close"
              title="Close session"
              onClick={(e) => {
                e.stopPropagation();
                onClose(session.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
        <button
          type="button"
          className="term-tabs__new"
          title="New terminal session"
          onClick={() => setShowDialog((v) => !v)}
        >
          +
        </button>
      </div>

      {showDialog ? (
        <NewSessionDialog
          defaultCwd={defaultCwd}
          recentCwds={recentCwds}
          onCreate={handleCreate}
          onClose={() => setShowDialog(false)}
        />
      ) : null}
    </div>
  );
}
