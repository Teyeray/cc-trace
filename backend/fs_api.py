"""Server-side directory browser for the new-session folder picker.

The terminal's working directory is a path on the *server's* filesystem, but a
browser's native folder picker can only see the user's local machine and hides
absolute paths for security. So the picker is driven by this endpoint instead:
the frontend navigates directories the backend exposes.

Browsing is confined to the user's home directory (a sensible root for a local
dev tool) — ``..`` above ``$HOME`` is rejected, so this never becomes a
read-anywhere filesystem API over loopback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(tags=["fs"])

_HOME = Path.home().resolve()

# Cap a single listing so a huge directory can't stall the event loop / balloon
# the response.
_MAX_ENTRIES = 500


def _within_home(path: Path) -> bool:
    return path == _HOME or _HOME in path.parents


@router.get("/fs/list")
def list_dir(path: str | None = Query(default=None)) -> dict[str, Any]:
    """List subdirectories of ``path`` (defaults to, and is confined to, $HOME)."""
    target = Path(path).expanduser().resolve() if path else _HOME
    if not _within_home(target):
        raise HTTPException(status_code=400, detail="path is outside the home directory")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")

    entries: list[dict[str, str]] = []
    truncated = False
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue  # hide dotfiles/dirs to reduce noise
            try:
                # Skip symlinks: a planted symlink could otherwise surface the
                # name of (or navigate toward) a directory outside HOME.
                if child.is_symlink():
                    continue
                if child.is_dir():
                    if len(entries) >= _MAX_ENTRIES:
                        truncated = True
                        break
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # unreadable entry (permissions / broken symlink)
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")

    parent = str(target.parent) if _within_home(target.parent) and target != _HOME else None
    return {
        "path": str(target),
        "parent": parent,
        "home": str(_HOME),
        "entries": entries,
        "truncated": truncated,
    }
