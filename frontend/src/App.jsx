import { useCallback, useEffect, useMemo, useState } from 'react';
import TraceGraph from './components/trace-graph/TraceGraph.jsx';
import DetailPanel from './components/detail-panel/DetailPanel.jsx';
import TerminalPane from './components/terminal/TerminalPane.jsx';
import TerminalTabs from './components/terminal/TerminalTabs.jsx';
import WorkflowPanel from './components/workflow/WorkflowPanel.jsx';
import { useTraceStream } from './lib/useTraceStream.js';
import { useTranscript } from './lib/useTranscript.js';
import { useTerminalSessions } from './lib/useTerminalSessions.js';
import { buildGraph } from './lib/graph.js';

const SESSION_POLL_MS = 4000;

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [showTerminal, setShowTerminal] = useState(true);
  const [termConnected, setTermConnected] = useState(false);
  const [view, setView] = useState('trace'); // 'trace' | 'workflow'

  // Terminal (PTY) sessions are a separate concept from trace sessions above:
  // these are live shells you interact with; trace sessions are captured logs.
  const {
    sessions: termSessions,
    create: createTerminal,
    remove: removeTerminal,
  } = useTerminalSessions();
  const [activeTerminalId, setActiveTerminalId] = useState(null);

  const { events, connected } = useTraceStream(sessionId);
  const turns = useTranscript(sessionId);

  const handleTermStatus = useCallback((isConnected) => {
    setTermConnected(isConnected);
  }, []);

  const { nodes, edges } = useMemo(
    () => buildGraph(events, turns),
    [events, turns],
  );

  const selected = useMemo(
    () => nodes.find((n) => n.id === selectedId)?.data ?? null,
    [nodes, selectedId],
  );

  // Keep an active terminal tab valid: adopt the first session when none is
  // selected, and drop the selection when its session disappears (killed/exit).
  useEffect(() => {
    setActiveTerminalId((current) => {
      if (current && termSessions.some((s) => s.id === current)) return current;
      return termSessions[0]?.id ?? null;
    });
  }, [termSessions]);

  // Poll the trace-session list so newly-created sessions appear without reload.
  useEffect(() => {
    let active = true;
    const load = () => {
      fetch('/sessions')
        .then((res) => res.json())
        .then((body) => {
          if (!active) return;
          const list = body.sessions ?? [];
          setSessions(list);
          setSessionId((current) => {
            if (current) return current;
            const busiest = [...list].sort(
              (a, b) => b.event_count - a.event_count,
            )[0];
            return busiest ? busiest.session_id : null;
          });
        })
        .catch(() => {});
    };
    load();
    const timer = setInterval(load, SESSION_POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => setSelectedId(null), [sessionId]);

  const defaultCwd = termSessions[0]?.cwd ?? '';

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__logo">cc</span>
          <div>
            <h1 className="app__title">trace</h1>
            <p className="app__subtitle">live Claude Code execution graph</p>
          </div>
        </div>

        <div className="app__controls">
          <div className="app__viewtoggle" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={view === 'trace'}
              className={`app__viewtab ${view === 'trace' ? 'app__viewtab--on' : ''}`}
              onClick={() => setView('trace')}
            >
              trace
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === 'workflow'}
              className={`app__viewtab ${view === 'workflow' ? 'app__viewtab--on' : ''}`}
              onClick={() => setView('workflow')}
            >
              workflow
            </button>
          </div>
          <button
            type="button"
            className={`app__toggle ${showTerminal ? 'app__toggle--on' : ''}`}
            onClick={() => setShowTerminal((v) => !v)}
            title="Toggle the embedded terminal"
          >
            ▌ terminal{' '}
            <span
              className={`app__dot app__dot--${termConnected ? 'live' : 'idle'}`}
            />
          </button>
          <label className="app__field">
            <span>session</span>
            <select
              value={sessionId ?? ''}
              onChange={(e) => setSessionId(e.target.value)}
            >
              {sessions.length === 0 ? (
                <option value="">no sessions yet</option>
              ) : null}
              {sessions.map((s) => (
                <option key={s.session_id} value={s.session_id}>
                  {s.session_id.slice(0, 8)}… · {s.event_count}
                </option>
              ))}
            </select>
          </label>
          <span
            className={`app__status app__status--${connected ? 'live' : 'idle'}`}
          >
            {connected ? 'live' : 'offline'}
          </span>
        </div>
      </header>

      <main className="app__body">
        {showTerminal ? (
          <section className="terminal-pane">
            <TerminalTabs
              sessions={termSessions}
              activeId={activeTerminalId}
              defaultCwd={defaultCwd}
              onSelect={setActiveTerminalId}
              onCreate={createTerminal}
              onClose={removeTerminal}
            />
            {activeTerminalId ? (
              <TerminalPane
                terminalId={activeTerminalId}
                onStatusChange={handleTermStatus}
              />
            ) : (
              <div className="terminal-pane__empty">
                No terminal session. Click <code>+</code> to start one — choose a
                working directory and run <code>claude</code>. Tool calls appear
                on the right →
              </div>
            )}
          </section>
        ) : null}
        <section className="app__canvas">
          {view === 'workflow' ? (
            <WorkflowPanel traceSessionId={sessionId} />
          ) : sessionId ? (
            <TraceGraph
              nodes={nodes}
              edges={edges}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          ) : (
            <div className="app__empty">
              Run a tool in a Claude Code session to see it appear here.
            </div>
          )}
        </section>
        {view === 'trace' && selected ? (
          <DetailPanel detail={selected} onClose={() => setSelectedId(null)} />
        ) : null}
      </main>
    </div>
  );
}
