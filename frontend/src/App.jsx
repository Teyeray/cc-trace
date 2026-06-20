import { useCallback, useEffect, useMemo, useState } from 'react';
import TraceGraph from './components/trace-graph/TraceGraph.jsx';
import DetailPanel from './components/detail-panel/DetailPanel.jsx';
import TerminalPane from './components/terminal/TerminalPane.jsx';
import { useTraceStream } from './lib/useTraceStream.js';
import { useTranscript } from './lib/useTranscript.js';
import { buildGraph } from './lib/graph.js';

const SESSION_POLL_MS = 4000;

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [showTerminal, setShowTerminal] = useState(true);
  const [termConnected, setTermConnected] = useState(false);
  const { events, connected } = useTraceStream(sessionId);
  const turns = useTranscript(sessionId);

  const handleTermStatus = useCallback((isConnected) => {
    setTermConnected(isConnected);
  }, []);

  const { nodes, edges } = useMemo(
    () => buildGraph(events, turns),
    [events, turns],
  );

  // Derive selection from the live graph so the panel updates as the call
  // resolves (running -> success/failed, output arrives).
  const selected = useMemo(
    () => nodes.find((n) => n.id === selectedId)?.data ?? null,
    [nodes, selectedId],
  );

  // Poll the session list so newly-created sessions appear without a reload.
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

  // Reset selection when switching sessions.
  useEffect(() => setSelectedId(null), [sessionId]);

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
            <div className="terminal-pane__bar">
              <span className="terminal-pane__title">claude session</span>
              <span className="terminal-pane__hint">
                type <code>claude</code> to start — tool calls appear on the right →
              </span>
            </div>
            <TerminalPane onStatusChange={handleTermStatus} />
          </section>
        ) : null}
        <section className="app__canvas">
          {sessionId ? (
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
        {selected ? (
          <DetailPanel detail={selected} onClose={() => setSelectedId(null)} />
        ) : null}
      </main>
    </div>
  );
}
