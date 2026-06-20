import { useCallback, useEffect, useState } from 'react';

/**
 * Drives the workflow side of the pipeline: list saved workflows, build one
 * from a trace session, and fetch/distill/run a selected workflow. Mirrors the
 * fetch + optimistic-update style of useTerminalSessions.
 *
 * @returns {{
 *   workflows: Array<{id: string, name: string, node_count: number}>,
 *   refresh: () => void,
 *   build: (sessionId: string) => Promise<{id: string}>,
 *   get: (id: string) => Promise<object>,
 *   distill: (id: string, persist?: boolean) => Promise<object>,
 *   run: (id: string, opts?: object) => Promise<object>,
 * }}
 */
export function useWorkflows() {
  const [workflows, setWorkflows] = useState([]);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let active = true;
    fetch('/workflows')
      .then((res) => res.json())
      .then((body) => {
        if (active) setWorkflows(body.workflows ?? []);
      })
      .catch(() => {
        /* backend down: keep last-known list, stay quiet */
      });
    return () => {
      active = false;
    };
  }, [tick]);

  const build = useCallback(
    async (sessionId) => {
      const res = await fetch(
        `/workflows/build?session_id=${encodeURIComponent(sessionId)}`,
        { method: 'POST' },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `build failed (${res.status})`);
      }
      const wf = await res.json();
      refresh();
      return wf;
    },
    [refresh],
  );

  const get = useCallback(async (id) => {
    const res = await fetch(`/workflows/${id}`);
    if (!res.ok) throw new Error(`workflow ${id} not found`);
    return res.json();
  }, []);

  const distill = useCallback(async (id, persist = false) => {
    const res = await fetch(
      `/workflows/${id}/distill?persist=${persist ? 'true' : 'false'}`,
      { method: 'POST' },
    );
    if (!res.ok) throw new Error(`distill failed (${res.status})`);
    return res.json();
  }, []);

  const run = useCallback(async (id, opts = {}) => {
    const res = await fetch(`/workflows/${id}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? `run failed (${res.status})`);
    }
    return res.json();
  }, []);

  return { workflows, refresh, build, get, distill, run };
}
