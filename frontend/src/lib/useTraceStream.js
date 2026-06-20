import { useEffect, useRef, useState } from 'react';

/**
 * Load a session's recent events, then keep appending live ones over SSE.
 *
 * @param {string|null} sessionId
 * @returns {{ events: Array<Object>, connected: boolean }}
 */
export function useTraceStream(sessionId) {
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  const seenIds = useRef(new Set());

  useEffect(() => {
    if (!sessionId) {
      setEvents([]);
      seenIds.current = new Set();
      return undefined;
    }

    let cancelled = false;
    seenIds.current = new Set();
    setEvents([]);

    /** @param {Array<Object>} incoming */
    const addEvents = (incoming) => {
      const fresh = incoming.filter((e) => e?.id && !seenIds.current.has(e.id));
      if (fresh.length === 0) return;
      fresh.forEach((e) => seenIds.current.add(e.id));
      setEvents((prev) => [...prev, ...fresh]);
    };

    // 1. History
    fetch(`/events/recent?session_id=${encodeURIComponent(sessionId)}&limit=2000`)
      .then((res) => res.json())
      .then((body) => {
        if (!cancelled) addEvents(body.events ?? []);
      })
      .catch(() => {
        /* history is best-effort; live stream still works */
      });

    // 2. Live
    const source = new EventSource(
      `/events/stream?session_id=${encodeURIComponent(sessionId)}`,
    );
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (msg) => {
      try {
        addEvents([JSON.parse(msg.data)]);
      } catch {
        /* ignore malformed frame */
      }
    };

    return () => {
      cancelled = true;
      source.close();
      setConnected(false);
    };
  }, [sessionId]);

  return { events, connected };
}
