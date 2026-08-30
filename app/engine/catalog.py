# app/engine/catalog.py — per-job SQLite catalog of file version history for the
# versioned-files backup engine. Tracks, per local file path, every uploaded
# version (storage key, size, mtime, storage class, upload time) plus tombstone
# rows for local deletions, so backup/restore/prune runners (later tasks) can
# diff a local scan against what's already been uploaded.
#
# One table, `versions`, holds every row a path has ever had. Exactly one row
# per path may have is_current=1 at a time -- that's the "live" version a scan
# is diffed against. record_version() and mark_deleted() both enforce this by
# clearing any prior current row for the path before inserting the new one.
#
# stdlib sqlite3 + app.gui.fsbrowse only. No network, no subprocess.
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from app.gui import fsbrowse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL,
    key           TEXT,
    size          INTEGER,
    mtime         REAL,
    storage_class TEXT,
    uploaded_at   REAL,
    is_current    INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_versions_path ON versions(path);
"""


def open_catalog(path) -> sqlite3.Connection:
    """Open (creating if needed) the per-job catalog DB and ensure its schema."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def scan(source_root, rel) -> list[dict]:
    """Walk SOURCE_ROOT/rel and return [{"path", "size", "mtime"}, ...], `path`
    relative to the job source root. Confinement is checked FIRST via
    fsbrowse.safe_resolve -- raises fsbrowse.PathError on any escape attempt
    (absolute rel, "..", symlink) before anything is walked.
    """
    base = fsbrowse.safe_resolve(source_root, rel)  # confinement FIRST, before any walk
    root = Path(source_root).resolve()

    entries: list[dict] = []
    # Unlike dirsize.dir_size (an async web-request helper), scan() feeds the
    # backup run itself and needs a complete listing, so it is not wall-clock
    # bounded. followlinks stays False (os.walk default), matching fsbrowse's
    # own refusal to cross symlinks out of the root.
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            full = Path(dirpath, name)
            try:
                st = full.stat()
            except OSError:
                continue  # unreadable/vanished file -- skip, don't abort the walk
            try:
                rel_path = full.relative_to(root)
            except ValueError:
                continue  # shouldn't happen since full is under base, which is under root
            entries.append({
                "path": rel_path.as_posix(),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    return entries


def current(conn: sqlite3.Connection) -> dict:
    """{path: row} for every path's current (is_current=1) version."""
    rows = conn.execute(
        "SELECT * FROM versions WHERE is_current = 1 ORDER BY path"
    ).fetchall()
    return {row["path"]: row for row in rows}


def diff(conn: sqlite3.Connection, entries: list[dict]) -> dict:
    """Compare scanned `entries` against the catalog's current versions by
    path + size + mtime (mtime rounded to int seconds).

    Returns {"new": [entry, ...], "changed": [entry, ...], "deleted": [path, ...]}.
    """
    cur = current(conn)
    seen = set()
    new: list[dict] = []
    changed: list[dict] = []
    for entry in entries:
        path = entry["path"]
        seen.add(path)
        row = cur.get(path)
        if row is None:
            new.append(entry)
        elif row["size"] != entry["size"] or int(row["mtime"]) != int(entry["mtime"]):
            changed.append(entry)
    deleted = [path for path in cur if path not in seen]
    return {"new": new, "changed": changed, "deleted": deleted}


def record_version(conn: sqlite3.Connection, path, key, size, mtime, storage_class, uploaded_at) -> None:
    """Insert a new uploaded version for `path` as the current one, clearing
    is_current on whatever version was previously current for that path."""
    conn.execute("UPDATE versions SET is_current = 0 WHERE path = ? AND is_current = 1", (path,))
    conn.execute(
        "INSERT INTO versions (path, key, size, mtime, storage_class, uploaded_at, is_current, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 0)",
        (path, key, size, mtime, storage_class, uploaded_at),
    )
    conn.commit()


def mark_deleted(conn: sqlite3.Connection, path, at) -> None:
    """Record a tombstone version for `path` (local file removed), clearing
    is_current on the prior current row. The tombstone itself is never current
    -- it drops the path out of current(), so a file recreated later shows up
    as "new" again on the next diff()."""
    conn.execute("UPDATE versions SET is_current = 0 WHERE path = ? AND is_current = 1", (path,))
    conn.execute(
        "INSERT INTO versions (path, key, size, mtime, storage_class, uploaded_at, is_current, deleted) "
        "VALUES (?, NULL, NULL, NULL, NULL, ?, 0, 1)",
        (path, at),
    )
    conn.commit()


def versions(conn: sqlite3.Connection, path) -> list[sqlite3.Row]:
    """All versions of `path`, newest first."""
    return conn.execute(
        "SELECT * FROM versions WHERE path = ? ORDER BY uploaded_at DESC, id DESC",
        (path,),
    ).fetchall()


def prunable(conn: sqlite3.Connection, before_ts) -> list[sqlite3.Row]:
    """Non-current versions (including tombstones) uploaded before `before_ts`.
    Never includes an is_current=1 row."""
    return conn.execute(
        "SELECT * FROM versions WHERE is_current = 0 AND uploaded_at < ? ORDER BY uploaded_at ASC",
        (before_ts,),
    ).fetchall()


def delete_version(conn: sqlite3.Connection, id) -> None:
    """Remove a version row from the catalog (after its storage object, if
    any, has been deleted by the caller)."""
    conn.execute("DELETE FROM versions WHERE id = ?", (id,))
    conn.commit()
