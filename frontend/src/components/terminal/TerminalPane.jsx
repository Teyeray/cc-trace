import { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

// Matches the app's dark theme tokens (kept literal — xterm needs hex/rgb).
const THEME = {
  background: '#16181f',
  foreground: '#e6e8ee',
  cursor: '#7aa2ff',
  selectionBackground: '#2c3140',
};

/**
 * A live terminal bridged to the backend PTY over WebSocket. Typing `claude`
 * here starts a real Claude Code session whose tool calls flow into the graph.
 *
 * @param {{ onStatusChange?: (connected: boolean) => void }} props
 */
export default function TerminalPane({ onStatusChange }) {
  const containerRef = useRef(null);

  useEffect(() => {
    const term = new Terminal({
      fontFamily: 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: true,
      theme: THEME,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(containerRef.current);
    fit.fit();

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${window.location.host}/terminal/ws`);
    ws.binaryType = 'arraybuffer';

    function sendResize() {
      fit.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      }
    }

    ws.onopen = () => {
      onStatusChange?.(true);
      sendResize();
      term.focus();
    };
    ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        term.write(event.data);
      } else {
        term.write(new Uint8Array(event.data));
      }
    };
    ws.onclose = () => {
      onStatusChange?.(false);
      term.write('\r\n\x1b[2m[terminal disconnected — restart the backend to reconnect]\x1b[0m\r\n');
    };

    const dataSub = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }));
      }
    });

    const observer = new ResizeObserver(() => sendResize());
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      dataSub.dispose();
      ws.close();
      term.dispose();
    };
  }, [onStatusChange]);

  return <div className="terminal-pane__screen" ref={containerRef} />;
}
