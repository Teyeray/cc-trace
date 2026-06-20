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
 * A live terminal bridged to ONE backend PTY session over WebSocket. The
 * session is identified by `terminalId`; switching ids tears down the socket
 * and reconnects to the other session, whose scrollback the server replays on
 * attach. The PTY keeps running while detached, so tab-switching never kills
 * a `claude` session.
 *
 * @param {{ terminalId: string|null, onStatusChange?: (connected: boolean) => void }} props
 */
export default function TerminalPane({ terminalId, onStatusChange }) {
  const containerRef = useRef(null);

  useEffect(() => {
    if (!terminalId) {
      onStatusChange?.(false);
      return undefined;
    }

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
    const ws = new WebSocket(
      `${proto}://${window.location.host}/terminal/ws?id=${encodeURIComponent(terminalId)}`,
    );
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
      term.write(
        '\r\n\x1b[2m[disconnected — switch back or restart the backend]\x1b[0m\r\n',
      );
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
  }, [terminalId, onStatusChange]);

  return <div className="terminal-pane__screen" ref={containerRef} />;
}
