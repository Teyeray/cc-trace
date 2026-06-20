import { useEffect, useState } from 'react';

const POLL_MS = 6000;

/**
 * Fetch a session's conversation turns (user/assistant text) parsed from the
 * Claude Code transcript. Polls so new turns appear as the session continues.
 *
 * @param {string|null} sessionId
 * @returns {Array<{uuid:string, role:'user'|'assistant', text:string, timestamp:string, model:string|null, blocks:Object}>}
 */
export function useTranscript(sessionId) {
  const [turns, setTurns] = useState([]);

  useEffect(() => {
    if (!sessionId) {
      setTurns([]);
      return undefined;
    }
    let active = true;
    const load = () => {
      fetch(`/transcript?session_id=${encodeURIComponent(sessionId)}&limit=2000`)
        .then((res) => res.json())
        .then((body) => {
          if (active) setTurns(body.turns ?? []);
        })
        .catch(() => {});
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [sessionId]);

  return turns;
}
