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
    candidate = (root / rel).resolve()  # absolute `rel` replaces root; `..`/symlinks are followed
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
        except OSError:
            continue
        if resolved.is_dir() and _within(root, resolved):
            out.append(entry.name)
    return out
