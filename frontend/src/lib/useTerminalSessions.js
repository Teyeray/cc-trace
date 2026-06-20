import { useCallback, useEffect, useState } from 'react';

const POLL_MS = 4000;

/**
 * Manage the set of live PTY terminal sessions (distinct from *trace* sessions).
 * Polls the backend so externally-created or exited sessions stay in sync, and
 * exposes create/remove helpers.
 *
 * @returns {{
 *   sessions: Array<{id: string, cwd: string, cmd: string, alive: boolean, attached: boolean}>,
 *   create: (opts: {cwd?: string, cmd?: string}) => Promise<{id: string}>,
 *   remove: (id: string) => Promise<void>,
 *   refresh: () => void,
 * }}
 */
export function useTerminalSessions() {
  const [sessions, setSessions] = useState([]);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let active = true;
    const load = () => {
      fetch('/terminal/sessions')
        .then((res) => res.json())
        .then((body) => {
          if (active) setSessions(body.sessions ?? []);
        })
        .catch(() => {
          /* backend down: keep last-known list, stay quiet */
        });
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [tick]);

  const create = useCallback(
    async ({ cwd, cmd } = {}) => {
      const res = await fetch('/terminal/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: cwd || null, cmd: cmd || null }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `failed to create session (${res.status})`);
      }
      const session = await res.json();
      setSessions((prev) => [...prev, session]);
      return session;
    },
    [],
  );

  const remove = useCallback(async (id) => {
    await fetch(`/terminal/sessions/${id}`, { method: 'DELETE' }).catch(() => {});
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, []);

  return { sessions, create, remove, refresh };
}
