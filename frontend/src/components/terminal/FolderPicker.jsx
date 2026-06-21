import { useEffect, useState } from 'react';

/**
 * Server-side directory browser. A browser's native folder picker can't read
 * the server's filesystem (and hides absolute paths), so we navigate the
 * directories the backend exposes via GET /fs/list and report the chosen
 * absolute server path back through `onChange`.
 *
 * @param {{ value: string, onChange: (path: string) => void }} props
 */
export default function FolderPicker({ value, onChange }) {
  const [cwd, setCwd] = useState(value || null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    const qs = cwd ? `?path=${encodeURIComponent(cwd)}` : '';
    fetch(`/fs/list${qs}`)
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail ?? `list failed (${res.status})`);
        }
        return res.json();
      })
      .then((body) => {
        if (!active) return;
        setData(body);
        setCwd(body.path); // normalize to the resolved absolute path
        onChange(body.path); // selecting a folder = the current directory
      })
      .catch((e) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
    // onChange intentionally omitted: it's a stable setter from the parent and
    // including it would re-fetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cwd]);

  return (
    <div className="folder-picker">
      <div className="folder-picker__path">
        <code>{data?.path ?? '…'}</code>
      </div>
      <div className="folder-picker__list">
        {data?.parent ? (
          <button
            type="button"
            className="folder-picker__row folder-picker__row--up"
            onClick={() => setCwd(data.parent)}
          >
            ↑ ..
          </button>
        ) : null}
        {loading ? <div className="folder-picker__hint">loading…</div> : null}
        {error ? <div className="folder-picker__error">{error}</div> : null}
        {data?.entries?.length === 0 && !loading ? (
          <div className="folder-picker__hint">(no subfolders)</div>
        ) : null}
        {data?.entries?.map((entry) => (
          <button
            key={entry.path}
            type="button"
            className="folder-picker__row"
            onClick={() => setCwd(entry.path)}
            title={entry.path}
          >
            📁 {entry.name}
          </button>
        ))}
      </div>
    </div>
  );
}
