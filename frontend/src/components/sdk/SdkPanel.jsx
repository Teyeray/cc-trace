import { useEffect, useRef, useState } from 'react';
import FolderPicker from '../terminal/FolderPicker.jsx';

/**
 * Drive a Claude Code session programmatically via the CLI's stream-json
 * transport (POST /sdk/run, streamed). Tool calls captured during the run also
 * flow into the live trace graph, so switching to the trace view shows them
 * appear in real time.
 */
export default function SdkPanel() {
  const [prompt, setPrompt] = useState('');
  const [cwd, setCwd] = useState('');
  const [events, setEvents] = useState([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const abortRef = useRef(null);

  // Abort an in-flight run if the panel unmounts (e.g. switching view tabs),
  // so the fetch stream stops and the backend kills the subprocess.
  useEffect(() => () => abortRef.current?.abort(), []);

  const run = async () => {
    if (!prompt.trim() || running) return;
    setRunning(true);
    setError(null);
    setEvents([]);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const res = await fetch('/sdk/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, cwd: cwd || null }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new Error(`run failed (${res.status})`);

      // Parse the SSE stream from the fetch body reader.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          const line = frame.replace(/^data: /, '').trim();
          if (!line) continue;
          try {
            setEvents((prev) => [...prev, JSON.parse(line)]);
          } catch {
            /* ignore malformed frame */
          }
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') setError(e.message);
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  };

  const stop = () => abortRef.current?.abort();

  return (
    <div className="sdk">
      <div className="sdk__form">
        <div className="sdk__cwd">
          <span className="sdk__label">working directory</span>
          <FolderPicker value={cwd} onChange={setCwd} />
        </div>
        <textarea
          className="sdk__prompt"
          placeholder="Ask Claude Code to do something (runs headless via the SDK transport)…"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={3}
        />
        <div className="sdk__actions">
          {running ? (
            <button type="button" onClick={stop}>
              stop
            </button>
          ) : null}
          <button
            type="button"
            className="sdk__run"
            onClick={run}
            disabled={running || !prompt.trim()}
          >
            {running ? 'running…' : 'run'}
          </button>
        </div>
        {error ? <p className="sdk__error">{error}</p> : null}
      </div>

      <div className="sdk__stream">
        {events.map((e, i) => (
          <SdkEvent key={i} event={e} />
        ))}
        {events.length === 0 && !running ? (
          <div className="sdk__hint">
            Output streams here. Tool calls also appear in the trace graph live.
          </div>
        ) : null}
      </div>
    </div>
  );
}

function SdkEvent({ event }) {
  switch (event.type) {
    case 'assistant_text':
      return <p className="sdk__text">{event.text}</p>;
    case 'tool_use':
      return (
        <div className="sdk__tool">
          ▶ <code>{event.name}</code>
        </div>
      );
    case 'tool_result':
      return (
        <div className={`sdk__tool ${event.is_error ? 'sdk__tool--err' : ''}`}>
          {event.is_error ? '✖' : '✓'} result
        </div>
      );
    case 'result':
      return (
        <div className={`sdk__result ${event.is_error ? 'sdk__result--err' : ''}`}>
          {event.text || (event.is_error ? '(error)' : '(done)')}
        </div>
      );
    case 'error':
      return <div className="sdk__error">{event.detail}</div>;
    default:
      return null;
  }
}
