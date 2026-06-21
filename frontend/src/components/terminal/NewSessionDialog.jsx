import { useEffect, useState } from 'react';
import FolderPicker from './FolderPicker.jsx';

/**
 * Compact popover to launch a new terminal session: pick a working directory
 * (via the server-side folder browser) and what to run (a Claude Code session,
 * or a plain shell).
 *
 * @param {{
 *   defaultCwd?: string,
 *   onCreate: (opts: {cwd: string, cmd: string}) => Promise<void>,
 *   onClose: () => void,
 * }} props
 */
export default function NewSessionDialog({ defaultCwd = '', onCreate, onClose }) {
  const [cwd, setCwd] = useState(defaultCwd);
  const [cmd, setCmd] = useState('claude');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onCreate({ cwd: cwd.trim(), cmd });
      setBusy(false);  // reset before close, in case onClose only hides the dialog
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'failed to create session');
      setBusy(false);
    }
  };

  return (
    <div className="new-session" role="dialog" aria-label="New terminal session">
      <form className="new-session__form" onSubmit={submit}>
        <div className="new-session__label">
          <span>working directory</span>
          <FolderPicker value={cwd} onChange={setCwd} />
        </div>

        <fieldset className="new-session__choice">
          <label>
            <input
              type="radio"
              name="cmd"
              value="claude"
              checked={cmd === 'claude'}
              onChange={() => setCmd('claude')}
            />
            claude session
          </label>
          <label>
            <input
              type="radio"
              name="cmd"
              value=""
              checked={cmd === ''}
              onChange={() => setCmd('')}
            />
            shell
          </label>
        </fieldset>

        {error ? <p className="new-session__error">{error}</p> : null}

        <div className="new-session__actions">
          <button type="button" onClick={onClose} disabled={busy}>
            cancel
          </button>
          <button type="submit" className="new-session__create" disabled={busy}>
            {busy ? 'starting…' : 'start'}
          </button>
        </div>
      </form>
    </div>
  );
}
