# app/gui/fsbrowse.py — path-traversal-confined directory listing under a fixed root.
# The ONLY place user-supplied media paths are resolved. Read-only: it lists, never writes.
from __future__ import annotations
from pathlib import Path

class PathError(Exception):
    """Raised when a requested path resolves outside its allowed root."""

def _within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents

def safe_resolve(root, rel: str = "") -> Path:
    root = Path(root).resolve()
    try:
        candidate = (root / rel).resolve()  # absolute `rel` replaces root; `..`/symlinks are followed
    except (OSError, RuntimeError) as exc:  # e.g. ELOOP: a looping symlink (RuntimeError on 3.12+)
        raise PathError("path could not be resolved") from exc
    if not _within(root, candidate):
        raise PathError("path escapes root")
    return candidate

def list_dirs(root, rel: str = "") -> list[str]:
    root = Path(root).resolve()
    base = safe_resolve(root, rel)
    if not base.is_dir():
        raise PathError("not a directory")
    out: list[str] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        try:
            resolved = entry.resolve()
        except (OSError, RuntimeError):  # e.g. ELOOP: a looping symlink (RuntimeError on 3.12+)
            continue
        if resolved.is_dir() and _within(root, resolved):
            out.append(entry.name)
    return out
