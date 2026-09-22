# app/engine/runs.py — the READER over the append-only per-job run records the
# bash writer (scripts/lib/runs.sh, Task 2) produces, plus RUNNING detection,
# boot reconcile, and legacy-state backfill (spec §7.1–7.2).
#
# The writer appends two JSONL lines per run (a `start` and an `end` with the same
# `id`) to $CACHE_DIR/state/<job>.runs.jsonl. This module folds those into
# RunRecords, never raising on a corrupt line, and is the only Python writer — and
# it appends only from reconcile(), which first proves the job lock free (§7.1.9).
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import secrets
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$")
BACKUP_KINDS = ("backup",)
OP_KINDS = ("restore", "download", "thaw", "test-restore")
SYSTEM_JOB = "_system"
PENDING_WINDOW_S = 600            # a launched run may take this long to write its start line (5.3)

_ABORTED_ERROR = ("The run stopped without reporting — the container was probably restarted "
                  "or the process was killed.")
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


@dataclasses.dataclass
class RunRecord:
    id: str
    job: str | None
    kind: str
    trigger: str
    outcome: str                      # running | ok | failed | aborted | paused
    started_at: datetime              # tz-aware UTC
    finished_at: datetime | None
    duration_s: int | None
    exit_code: int | None
    error: str | None
    phase: str | None = None
    copied: bool | None = None
    snapshot_id: str | None = None
    files_new: int | None = None
    files_changed: int | None = None
    files_added: int | None = None
    bytes_added: int | None = None
    files_total: int | None = None
    bytes_total: int | None = None
    rclone_errors: int | None = None
    files_restored: int | None = None
    bytes_restored: int | None = None
    objects_requested: int | None = None
    thaw_requested: int | None = None
    log: str | None = None
    command: str | None = None
    type: str | None = None
    storage_class: str | None = None
    params: dict = dataclasses.field(default_factory=dict)
    pid: int | None = None
    attempts: int | None = None
    backfilled: bool = False


@dataclasses.dataclass
class ReadResult:
    records: list[RunRecord]
    corrupt_lines: int
    truncated_head: bool


# --- ids -------------------------------------------------------------------

def valid_run_id(s: str) -> bool:
    return bool(isinstance(s, str) and RUN_ID_RE.match(s))


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


def run_id_started_at(run_id: str) -> datetime | None:
    if not valid_run_id(run_id):
        return None
    try:
        return datetime.strptime(run_id.split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_pending(run_id: str, *, now=None) -> bool:
    st = run_id_started_at(run_id)
    if st is None:
        return False
    now = now or _utcnow()
    return abs((now - st).total_seconds()) <= PENDING_WINDOW_S


# --- paths -----------------------------------------------------------------

def runs_path(cache_dir, job) -> Path:
    return Path(cache_dir, "state", f"{job or SYSTEM_JOB}.runs.jsonl")


def lock_path(cache_dir, job) -> Path:
    return Path(cache_dir, "locks", f"{job}.lock")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- RUNNING detection (§7.2) ---------------------------------------------

def is_locked(cache_dir, job) -> bool:
    p = lock_path(cache_dir, job)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


# --- folding ---------------------------------------------------------------

_NUM_FIELDS = ("duration_s", "exit_code", "files_new", "files_changed", "files_added",
               "bytes_added", "files_total", "bytes_total", "rclone_errors",
               "files_restored", "bytes_restored", "objects_requested", "thaw_requested", "pid", "attempts")
_STR_FIELDS = ("kind", "trigger", "error", "phase", "snapshot_id", "log", "command",
               "type", "storage_class")


def _fold_group(grp: dict) -> tuple[RunRecord, bool]:
    start = grp.get("start")
    end = grp.get("end")
    merged: dict = {}
    if start:
        merged.update(start)
    if end:
        merged.update(end)

    started = _parse_ts(merged.get("started_at"))
    finished = _parse_ts(merged.get("finished_at"))
    duration = merged.get("duration_s")
    truncated = end is not None and start is None and "started_at" not in merged
    if started is None and finished is not None and isinstance(duration, (int, float)):
        started = finished - timedelta(seconds=int(duration))

    if end is not None:
        outcome = merged.get("outcome") or "ok"
    else:
        outcome = "running"

    def num(k):
        v = merged.get(k)
        return v if isinstance(v, (int, float)) else None

    def s(k):
        v = merged.get(k)
        return v if isinstance(v, str) and v != "" else None

    rec = RunRecord(
        id=merged.get("id"),
        job=merged.get("job"),
        kind=s("kind") or "backup",
        trigger=s("trigger") or "scheduled",
        outcome=outcome,
        started_at=started,
        finished_at=finished,
        duration_s=num("duration_s"),
        exit_code=num("exit_code"),
        error=s("error"),
        phase=s("phase"),
        copied=merged.get("copied") if isinstance(merged.get("copied"), bool) else None,
        snapshot_id=s("snapshot_id"),
        files_new=num("files_new"),
        files_changed=num("files_changed"),
        files_added=num("files_added"),
        bytes_added=num("bytes_added"),
        files_total=num("files_total"),
        bytes_total=num("bytes_total"),
        rclone_errors=num("rclone_errors"),
        files_restored=num("files_restored"),
        bytes_restored=num("bytes_restored"),
        objects_requested=num("objects_requested"),
        thaw_requested=num("thaw_requested"),
        log=s("log"),
        command=s("command"),
        type=s("type"),
        storage_class=s("storage_class"),
        params=merged.get("params") if isinstance(merged.get("params"), dict) else {},
        pid=num("pid"),
        attempts=num("attempts"),
        backfilled=bool(merged.get("backfilled")),
    )
    return rec, truncated


def _read_folded(cache_dir, job) -> ReadResult:
    p = runs_path(cache_dir, job)
    if not p.exists():
        ev = _backfill_dict(cache_dir, job)
        if ev is None:
            return ReadResult([], 0, False)
        rec, _ = _fold_group({"end": ev})
        return ReadResult([rec], 0, False)

    corrupt = 0
    groups: dict[str, dict] = {}
    text = p.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            corrupt += 1
            continue
        if not isinstance(obj, dict) or "id" not in obj or "event" not in obj:
            corrupt += 1
            continue
        v = obj.get("v", 1)
        if not isinstance(v, int) or v > 1:
            corrupt += 1
            continue
        ev = obj["event"]
        if ev not in ("start", "end"):
            corrupt += 1
            continue
        groups.setdefault(obj["id"], {})[ev] = obj   # last `end` wins

    records: list[RunRecord] = []
    truncated_head = False
    for grp in groups.values():
        rec, trunc = _fold_group(grp)
        truncated_head = truncated_head or trunc
        records.append(rec)
    records.sort(key=lambda r: (r.started_at or _MIN_DT, r.id or ""), reverse=True)
    return ReadResult(records, corrupt, truncated_head)


def read_runs(cache_dir, job, *, kinds=None, limit=None, reconcile=True, now=None) -> ReadResult:
    res = _read_folded(cache_dir, job)
    if reconcile and any(r.outcome == "running" for r in res.records):
        if globals()["reconcile"](cache_dir, job, now=now) > 0:
            res = _read_folded(cache_dir, job)
    recs = res.records
    if kinds is not None:
        recs = [r for r in recs if r.kind in kinds]
    if limit is not None:
        recs = recs[:limit]
    return ReadResult(recs, res.corrupt_lines, res.truncated_head)


# --- queries over the folded records --------------------------------------

def read_all(cache_dir, jobs, *, limit=100, job=None, kind=None, outcome=None) -> list[RunRecord]:
    names = list(dict.fromkeys(list(jobs) + [SYSTEM_JOB]))
    recs: list[RunRecord] = []
    for j in names:
        if job is not None and j != job:
            continue
        recs.extend(_read_folded(cache_dir, j).records)
    if kind is not None:
        recs = [r for r in recs if r.kind == kind]
    if outcome is not None:
        recs = [r for r in recs if r.outcome == outcome]
    recs.sort(key=lambda r: (r.started_at or _MIN_DT, r.id or ""), reverse=True)
    return recs[:limit]


def get_run(cache_dir, job, run_id) -> RunRecord | None:
    for r in _read_folded(cache_dir, job).records:
        if r.id == run_id:
            return r
    return None


def last_completed_backup(cache_dir, job) -> RunRecord | None:
    for r in read_runs(cache_dir, job, kinds=BACKUP_KINDS).records:
        if r.outcome != "running":
            return r
    return None


def active_run(cache_dir, job) -> RunRecord | None:
    if not is_locked(cache_dir, job):
        return None
    for r in _read_folded(cache_dir, job).records:
        if r.outcome == "running":
            return r
    return None


def median_duration_s(records, *, window=30) -> float | None:
    backups = [r for r in records if r.kind in BACKUP_KINDS][:window]
    durs = [r.duration_s for r in backups if r.outcome == "ok" and r.duration_s is not None]
    if len(durs) < 3:
        return None
    return float(statistics.median(durs))


def streak(records) -> int:
    n = 0
    for r in records:
        if r.kind not in BACKUP_KINDS:
            continue
        if r.outcome == "ok":
            n += 1
        else:
            break
    return n


# --- reconcile (§7.2) ------------------------------------------------------

def append_event(cache_dir, job, event: dict) -> None:
    p = runs_path(cache_dir, job)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators to match the bash writer's line shape (one JSON object,
    # no interior spaces) so the JSONL stays uniform for readers and greps.
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")


def reconcile(cache_dir, job, *, now=None, force=False) -> int:
    running = [r for r in _read_folded(cache_dir, job).records if r.outcome == "running"]
    if not running:
        return 0
    if not force and is_locked(cache_dir, job):
        return 0
    ts = (now or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for r in running:
        append_event(cache_dir, job, {
            "v": 1, "id": r.id, "job": r.job, "kind": r.kind, "event": "end",
            "outcome": "aborted", "finished_at": ts, "duration_s": None,
            "exit_code": None, "error": _ABORTED_ERROR,
        })
        n += 1
    return n


# --- backfill (§7.1.8) -----------------------------------------------------

def _backfill_dict(cache_dir, job) -> dict | None:
    p = Path(cache_dir, "state", f"{job}.json")
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    last_run = data.get("last_run")
    if not last_run or not isinstance(last_run, str):
        return None
    rid = last_run.replace("-", "").replace(":", "") + "-0000"
    finished = _parse_ts(last_run)
    dur = data.get("duration_s")
    if finished is not None and isinstance(dur, (int, float)):
        started_s = (finished - timedelta(seconds=int(dur))).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        started_s = last_run
    snap = data.get("snapshot_id") or ""
    snap = snap[:8] if snap else None
    return {
        "v": 1, "id": rid, "job": job, "kind": "backup", "event": "end",
        "outcome": "ok" if data.get("outcome") == "success" else "failed",
        "trigger": "scheduled",
        "started_at": started_s, "finished_at": last_run,
        "duration_s": int(dur) if isinstance(dur, (int, float)) else None,
        "exit_code": data.get("exit_code") if isinstance(data.get("exit_code"), int) else None,
        "error": (data.get("error") or None),
        "snapshot_id": snap,
        "type": (data.get("type") or None),
        "log": None, "backfilled": True,
    }


def backfill_record(cache_dir, job) -> RunRecord | None:
    ev = _backfill_dict(cache_dir, job)
    if ev is None:
        return None
    rec, _ = _fold_group({"end": ev})
    return rec


def materialize_backfill(cache_dir, job) -> bool:
    if runs_path(cache_dir, job).exists():
        return False
    ev = _backfill_dict(cache_dir, job)
    if ev is None:
        return False
    append_event(cache_dir, job, ev)
    return True


# Alias so the required `runs.backfill` name resolves (brief interface list).
backfill = materialize_backfill


# --- logs ------------------------------------------------------------------

def log_file(cache_dir, rec) -> Path | None:
    if not getattr(rec, "log", None):
        return None
    return Path(cache_dir) / rec.log


def read_log(cache_dir, rec, *, offset=0, max_bytes=65536) -> tuple[str, int, bool]:
    p = log_file(cache_dir, rec)
    if p is None:
        return ("", offset, True)
    base = (Path(cache_dir) / "logs" / "runs").resolve()
    try:
        rp = p.resolve()
    except OSError:
        return ("", offset, True)
    if rp != base and base not in rp.parents:   # refuse paths outside logs/runs
        return ("", offset, True)
    if not rp.is_file():
        return ("", offset, True)
    size = rp.stat().st_size
    with open(rp, "rb") as fh:
        fh.seek(max(0, offset))
        data = fh.read(max_bytes)
    new_offset = max(0, offset) + len(data)
    return (data.decode("utf-8", "replace"), new_offset, new_offset >= size)


# --- discovery + boot ------------------------------------------------------

def all_jobs_with_runs(cache_dir) -> list[str]:
    d = Path(cache_dir, "state")
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.runs.jsonl")):
        name = p.name[: -len(".runs.jsonl")]
        if name != SYSTEM_JOB:
            out.append(name)
    return out


def boot(cache_dir, jobs) -> dict:
    names = list(dict.fromkeys(list(jobs) + all_jobs_with_runs(cache_dir)))
    backfilled = 0
    aborted = 0
    for job in names:
        try:
            if materialize_backfill(cache_dir, job):
                backfilled += 1
            aborted += reconcile(cache_dir, job, force=True)
        except Exception as e:            # boot must never raise (§7.1.7)
            print(f"runs boot: {job}: {e}", file=sys.stderr)
    return {"jobs": len(names), "backfilled": backfilled, "aborted": aborted}


def _boot_cli() -> int:
    cache_dir = os.environ.get("CACHE_DIR", "/cache")
    config_dir = os.environ.get("CONFIG_DIR", "/config")
    job_names: list[str] = []
    try:
        from app.gui import jobs_io
        job_names = [j.get("name") for j in jobs_io.load(config_dir) if j.get("name")]
    except Exception as e:
        print(f"runs boot: could not load jobs.json: {e}", file=sys.stderr)
    try:
        summary = boot(cache_dir, job_names)
        print(f"runs boot: {summary['jobs']} job(s), {summary['backfilled']} backfilled, "
              f"{summary['aborted']} dangling run(s) marked aborted")
    except Exception as e:                # exits 0 always
        print(f"runs boot: reconcile failed: {e}", file=sys.stderr)
    return 0


def _main(argv) -> int:
    if argv and argv[0] == "boot":
        return _boot_cli()
    print("usage: python3 -m app.engine.runs boot", file=sys.stderr)
    return 0                              # never a nonzero exit from this module


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
