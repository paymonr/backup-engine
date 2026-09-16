# app/gui/points.py — the restore-point READER the job page and Board consume.
# Wraps the caches scripts/lib/points.sh writes ($CACHE_DIR/state/<job>.points.json)
# and the per-job catalog ($CACHE_DIR/<job>.sqlite) and NEVER calls a tool itself
# (spec 7.5.4). Shapes per 8.5, plus the newest-six-plus-oldest visibility rule of
# 5.2 for versioned jobs and the Plain-copy "what is there now".
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..engine import runs

COLD_CLASSES = ("GLACIER", "DEEP_ARCHIVE")


# --- helpers ---------------------------------------------------------------

def _points_path(cache_dir, job) -> Path:
    return Path(cache_dir, "state", f"{job}.points.json")


def _catalog_path(cache_dir, job) -> Path:
    return Path(cache_dir, f"{job}.sqlite")


def _parse_ts(s) -> datetime | None:
    """Tolerant RFC3339 parse: accepts trailing Z and arbitrary-precision
    fractional seconds (restic emits nanoseconds) by truncating to microseconds."""
    if not isinstance(s, str) or not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    # Truncate a >6-digit fraction so fromisoformat never rejects nanoseconds.
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = head + "." + frac[:6] + rest
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(dt: datetime | None, tz) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(tz or timezone.utc).strftime("%a %d %b %H:%M")


def _read_json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _fetched_at(p: Path) -> tuple[str | None, datetime | None]:
    if not p.is_file():
        return None, None
    dt = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return _iso(dt), dt


def _last_ok_finished(cache_dir, job) -> datetime | None:
    rec = runs.last_completed_backup(cache_dir, job)
    if rec is None or rec.outcome != "ok":
        return None
    return rec.finished_at or rec.started_at


# --- the view --------------------------------------------------------------

def view(cache_dir, job, *, now=None, tz=None, keep_rule_prose=None, keep_rule_label=None) -> dict:
    """Return the restore-point view (8.5 shape) for JOB, ready for the job page.

    `job` is the job dict (name, type, storage_class, mirror). `keep_rule_prose`
    / `keep_rule_label` (4.3) are passed through when the caller has them."""
    name = job.get("name")
    typ = job.get("type")
    cls = job.get("storage_class", "STANDARD")
    cold = cls in COLD_CLASSES
    if typ == "archive":
        return _view_archive(cache_dir, name, cls, cold, job, tz)
    if typ == "versioned-files":
        return _view_versioned_files(cache_dir, name, cls, cold, tz)
    return _view_versioned(cache_dir, name, cls, cold, tz, keep_rule_prose, keep_rule_label)


def _empty(job, typ, kind) -> dict:
    return {"job": job, "type": typ, "kind": kind, "points": [], "count": 0,
            "fetched_at": None, "stale": True, "visible": [], "hidden": [],
            "hidden_count": 0, "has_details": False}


def _stale(fetched_dt: datetime | None, last_ok: datetime | None) -> bool:
    if fetched_dt is None:
        return True
    if last_ok is None:
        return False
    return fetched_dt < last_ok


def _view_versioned(cache_dir, name, cls, cold, tz, keep_rule_prose, keep_rule_label) -> dict:
    p = _points_path(cache_dir, name)
    raw = _read_json(p)
    if not isinstance(raw, list):
        out = _empty(name, "versioned", "snapshots")
        out.update({"storage_class": cls, "cold": cold})
        return out
    fetched_at, fetched_dt = _fetched_at(p)
    pts = []
    for snap in raw:
        if not isinstance(snap, dict):
            continue
        t = _parse_ts(snap.get("time"))
        summ = snap.get("summary")
        has_summary = isinstance(summ, dict)
        summ = summ if has_summary else {}
        sid = snap.get("short_id") or (snap.get("id") or "")[:8]
        pts.append({
            "id": sid,
            "time": _iso(t),
            "label": _label(t, tz),
            "size_bytes": summ.get("total_bytes_processed") if has_summary else None,
            "files_total": summ.get("total_files_processed") if has_summary else None,
            "files_new": summ.get("files_new") if has_summary else None,
            "files_changed": summ.get("files_changed") if has_summary else None,
            "bytes_added": summ.get("data_added") if has_summary else None,
            "summary": has_summary,
            "_sort": t or datetime.min.replace(tzinfo=timezone.utc),
        })
    pts.sort(key=lambda r: (r["_sort"], r["id"] or ""), reverse=True)
    for r in pts:
        del r["_sort"]
    count = len(pts)
    # Newest six restore points, then the oldest -> seven rows when count >= 8;
    # every point is a row when count <= 7 (5.2, stated once).
    if count >= 8:
        visible = pts[:6] + [pts[-1]]
        hidden = pts[6:-1]
        has_details = True
    else:
        visible = list(pts)
        hidden = []
        has_details = False
    last_ok = _last_ok_finished(cache_dir, name)
    return {
        "job": name, "type": "versioned", "kind": "snapshots",
        "storage_class": cls, "cold": cold,
        "fetched_at": fetched_at, "stale": _stale(fetched_dt, last_ok),
        "points": pts, "count": count,
        "newest": pts[0]["time"] if pts else None,
        "oldest": pts[-1]["time"] if pts else None,
        "size_provenance": "measured" if (pts and pts[0]["summary"]) else "assumed",
        "default_point": pts[0]["id"] if pts else None,
        "keep_rule_prose": keep_rule_prose, "keep_rule_label": keep_rule_label,
        "visible": visible, "hidden": hidden,
        "hidden_count": max(0, count - 7), "has_details": has_details,
    }


def _view_archive(cache_dir, name, cls, cold, job, tz) -> dict:
    p = _points_path(cache_dir, name)
    raw = _read_json(p)
    fetched_at, fetched_dt = _fetched_at(p)
    last_ok = _last_ok_finished(cache_dir, name)
    folders = []
    as_of = None
    if isinstance(raw, dict):
        folders = [f for f in raw.get("folders", []) if isinstance(f, str)]
        as_of = raw.get("as_of")
    # Prefer the last successful run's finish time as "as of" (5.2).
    if last_ok is not None:
        as_of = _iso(last_ok)
    size_bytes = file_count = None
    size_measured_at = None
    usage = _read_json(Path(cache_dir, "usage.json"))
    if isinstance(usage, dict):
        data = usage.get("data") if isinstance(usage.get("data"), dict) else {}
        row = data.get(f"media/{name}")
        if isinstance(row, dict):
            size_bytes = row.get("bytes")
            file_count = row.get("count")
            fa = usage.get("fetched_at")
            if isinstance(fa, (int, float)):
                size_measured_at = _iso(datetime.fromtimestamp(fa, tz=timezone.utc))
    mirror = bool(job.get("mirror"))
    note = "current copy only — no version history"
    if mirror:
        note += " · files deleted at home are also deleted in the copy on the next run"
    out = _empty(name, "archive", "current-copy") if raw is None else {}
    out.update({
        "job": name, "type": "archive", "kind": "current-copy",
        "storage_class": cls, "cold": cold,
        "fetched_at": fetched_at, "stale": _stale(fetched_dt, last_ok),
        "folders": folders, "as_of": as_of, "as_of_source": "last successful run",
        "size_bytes": size_bytes, "file_count": file_count,
        "size_provenance": "measured" if size_bytes is not None else "assumed",
        "size_measured_at": size_measured_at,
        "mirror": mirror, "note": note, "thaw": _read_json(Path(cache_dir, "state", f"{name}.thaw.json")),
        # not applicable to a Plain copy, but keep the keys uniform for templates:
        "points": [], "count": 1 if raw is not None else 0,
        "visible": [], "hidden": [], "hidden_count": 0, "has_details": False,
    })
    return out


def _view_versioned_files(cache_dir, name, cls, cold, tz) -> dict:
    last_ok = _last_ok_finished(cache_dir, name)
    # One restore-point row per OK backup run (newest first).
    recs = [r for r in runs.read_runs(cache_dir, name, kinds=runs.BACKUP_KINDS).records
            if r.outcome == "ok"]
    pts = []
    for r in recs:
        t = r.finished_at or r.started_at
        pts.append({
            "id": r.id,
            "time": _iso(t),
            "asof": int(t.timestamp()) if t else None,
            "label": _label(t, tz),
            "files_added": r.files_added,
            "bytes_added": r.bytes_added,
        })
    file_count = size_bytes = None
    cat = _catalog_path(cache_dir, name)
    if cat.is_file():
        file_count, size_bytes = _catalog_totals(cat)
    return {
        "job": name, "type": "versioned-files", "kind": "file-history",
        "storage_class": cls, "cold": cold,
        "fetched_at": _iso(last_ok), "stale": False,
        "points": pts, "count": len(pts),
        "file_count": file_count, "size_bytes": size_bytes,
        "size_provenance": "measured" if size_bytes is not None else "assumed",
        "size_source": "catalog", "thaw": None,
        "visible": pts, "hidden": [], "hidden_count": 0, "has_details": False,
    }


def _catalog_totals(cat: Path) -> tuple[int | None, int | None]:
    """(file_count, size_bytes) over is_current=1 rows, opened read-only. Never
    raises: a missing/locked/corrupt catalog degrades both to None."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{cat}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, None
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM versions WHERE is_current = 1"
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (None, None)
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()


# --- refresh ---------------------------------------------------------------

def refresh(cfg, job: str, type: str, *, timeout: int = 120) -> bool:
    """Run the bash `points_refresh` (the sole cache writer, scripts/lib/points.sh)
    for JOB, synchronously. Returns True when the cache was (re)written. The GUI
    also refreshes on demand via `restore.sh <job> list --json` (7.5.6); this
    helper is the direct entry point that never needs the full restore script."""
    import os
    scripts = cfg["SCRIPTS_DIR"]
    script = (f'set -euo pipefail; source "{scripts}/lib/common.sh"; '
              f'source "{scripts}/lib/points.sh"; points_refresh "$1" "$2"')
    env = {**os.environ, "CACHE_DIR": str(cfg["CACHE_DIR"])}
    try:
        p = subprocess.run(["bash", "-c", script, "points_refresh", job, type],
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0
