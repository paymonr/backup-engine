# app/gui/jobs_io.py — the ONLY reader/writer of config/jobs.json. Validates job
# defs (name charset + source confined to SOURCE_ROOT) and emits shell-safe vars
# for the bash runner. Never runs a backup itself.
from __future__ import annotations
import json, os, re, sys, shlex, signal
from datetime import datetime, timezone
from pathlib import Path
from . import fsbrowse

JOBS_FILE = "jobs.json"
# \Z (end of string), NOT $: Python's `$` also matches just before a trailing
# newline, which would let "name\n" pass the charset gate and reach restic --tag,
# rclone media/<name>/, state/<name>.json, the lock, and the crontab name field.
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
TYPES = ("versioned", "archive", "versioned-files")
STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE")
_KEEP_KEYS = ("last", "daily", "weekly", "monthly")
_RETENTION_TYPES = ("keep_all", "days", "count", "tiered")

def valid_name(s: str) -> bool:
    return bool(JOB_NAME_RE.match(s or "")) and s not in (".", "..")

def _path(config_dir) -> Path:
    return Path(config_dir, JOBS_FILE)

class JobsFileError(ValueError):
    """The on-disk jobs.json exists but can't be parsed. Raised only on the WRITE
    path (upsert/delete) so a save never clobbers the user's (unparseable but
    hand-fixable) bytes. A ValueError subclass so existing `except ValueError`
    write-path handlers still catch it, while routes can catch it specifically."""

def _parse_jobs(text: str, *, drop_invalid_names: bool = False) -> list[dict]:
    # Parse jobs.json text into a list of job dicts, or raise ValueError with a
    # human reason. Shape errors (top-level not a dict, `jobs` not a list, or a
    # non-dict entry) are treated like a parse error: the file as a whole is
    # unusable, so callers degrade to "no jobs" rather than crash downstream on
    # `j.get(...)` / `j["name"]`.
    #
    # drop_invalid_names=True (the fail-SAFE read path only, via load()) additionally
    # DROPS any dict entry whose "name" fails valid_name() — a nameless/invalid-named
    # entry can't be keyed or rendered (routes do j["name"], estimate_io does
    # j['name']), so it would 500 /jobs, /estimate, /costs/refresh. The WRITE path
    # (_load_strict) leaves entries untouched so a save never silently discards the
    # user's hand-edited bytes; upsert()/validate() reject a bad new job loudly.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON ({e.msg}, line {e.lineno} column {e.colno})")
    if not isinstance(data, dict):
        raise ValueError("top-level value is not an object")
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError('"jobs" is not a list')
    if not all(isinstance(j, dict) for j in jobs):
        raise ValueError('"jobs" contains a non-object entry')
    if drop_invalid_names:
        jobs = [j for j in jobs
                if isinstance(j.get("name"), str) and valid_name(j["name"])]
    return list(jobs)

def _normalize_retention(job: dict, typ: str) -> dict:
    """One retention policy per job. Explicit `retention` wins; else migrate the
    legacy per-type fields; else default. `tiered` is versioned-only."""
    r = job.get("retention")
    if not isinstance(r, dict):   # migrate legacy shapes
        if typ == "versioned" and job.get("keep"):
            r = {"type": "tiered", "keep": job["keep"]}
        elif typ == "versioned-files":
            # Preserve old validation: explicit None is an error (key present but None),
            # missing key defaults to 90 days.
            if "retention_days" in job:
                rd = job["retention_days"]
                if rd is None:
                    raise ValueError("retention_days must be a non-negative integer")
                r = {"type": "days", "days": rd}
            else:
                r = {"type": "days", "days": 90}   # versioned-files default (backward compat)
        else:
            r = {"type": "days", "days": 180}   # archive default / anything unset
    t = r.get("type")
    if t not in _RETENTION_TYPES:
        raise ValueError(f"unknown retention type {t!r}")
    if t == "tiered":
        if typ != "versioned":
            raise ValueError("tiered retention is only valid for versioned (restic) jobs")
        keep = r.get("keep") or {}
        try:
            norm = {k: max(0, int(keep.get(k, 0))) for k in _KEEP_KEYS}
        except (TypeError, ValueError):
            raise ValueError("tiered keep values must be non-negative integers")
        if not any(norm.values()):
            # {last:0, daily:0, weekly:0, monthly:0} means "keep nothing" to restic
            # (`forget --prune --keep-last 0 --keep-daily 0 ...`) -- it would forget
            # and prune EVERY snapshot for the job's tag. Never allow it.
            raise ValueError("tiered retention must keep at least one snapshot (all keep values are zero)")
        return {"type": "tiered", "keep": norm}
    if t == "days":
        try:
            d = int(r.get("days", 0))
        except (TypeError, ValueError):
            raise ValueError("retention days must be a non-negative integer")
        if d < 0:
            raise ValueError("retention days must be >= 0")
        return {"type": "days", "days": d}
    if t == "count":
        try:
            c = int(r.get("count", 0))
        except (TypeError, ValueError):
            raise ValueError("retention count must be a positive integer")
        if c < 1:
            raise ValueError("retention count must be >= 1")
        return {"type": "count", "count": c}
    return {"type": "keep_all"}

def load(config_dir) -> list[dict]:
    # Fail-SAFE READ path (crontab render, Jobs page, get/run/restore): a missing
    # file is empty (silent); a present-but-corrupt file emits ONE stderr diagnostic
    # and returns [] instead of raising — a whole-file parse error must degrade to
    # "no jobs" (drop everything), never brick container boot or 500 the GUI. Pure
    # (no writes). The write path uses _load_strict() so it never clobbers.
    p = _path(config_dir)
    if not p.exists():
        return []
    try:
        return _parse_jobs(p.read_text(), drop_invalid_names=True)
    except ValueError as e:
        print(f"jobs.json: {e} — ignoring (no jobs loaded)", file=sys.stderr)
        return []

def _load_strict(config_dir) -> list[dict]:
    # WRITE path reader (upsert/delete): a missing file is empty, but a present
    # file that fails to parse RAISES so the caller aborts instead of overwriting
    # the user's bytes with a save built on the swallowed-empty load().
    p = _path(config_dir)
    if not p.exists():
        return []
    try:
        return _parse_jobs(p.read_text())
    except ValueError:
        raise JobsFileError("jobs.json is not valid JSON; fix or remove it before "
                            "editing jobs")

def get(config_dir, name) -> dict | None:
    return next((j for j in load(config_dir) if j.get("name") == name), None)

def _now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None

def _normalize_assumptions(job: dict) -> dict:
    """7.8: `{change_rate_pct: float, bundled: bool, pack_member_gb: float, set_at: iso}`.
    Backfilled with defaults on every read so an edit never resets them; a member of
    the wrong type falls back to its default rather than 500."""
    a = job.get("assumptions")
    a = a if isinstance(a, dict) else {}
    def _f(key, default):
        try:
            return float(a[key])
        except (KeyError, TypeError, ValueError):
            return default
    out = {"change_rate_pct": _f("change_rate_pct", 0.0),
           "bundled": bool(a.get("bundled", False)),
           "pack_member_gb": _f("pack_member_gb", 0.05)}
    if isinstance(a.get("set_at"), str):
        out["set_at"] = a["set_at"]
    return out

def _normalize_measured(job: dict) -> dict | None:
    """7.8: `{bytes: int, count: int, at: iso, capped: bool}` or absent. Present only
    when `bytes` is a usable int (size_gb is rounded, so the exact byte count is the
    thing worth keeping); an unusable value drops the whole key, never a 500."""
    m = job.get("measured")
    if not isinstance(m, dict) or isinstance(m.get("bytes"), bool):
        return None
    try:
        b = int(m["bytes"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        c = int(m.get("count", 0))
    except (TypeError, ValueError):
        c = 0
    out = {"bytes": b, "count": c, "capped": bool(m.get("capped", False))}
    if isinstance(m.get("at"), str):
        out["at"] = m["at"]
    return out

def _normalize_acknowledged(job: dict) -> list | None:
    """7.8: `[{code, class, at}]` or absent. Keep only well-formed entries."""
    ack = job.get("acknowledged")
    if not isinstance(ack, list):
        return None
    out = []
    for e in ack:
        if isinstance(e, dict) and isinstance(e.get("code"), str) and isinstance(e.get("class"), str):
            entry = {"code": e["code"], "class": e["class"]}
            if isinstance(e.get("at"), str):
                entry["at"] = e["at"]
            out.append(entry)
    return out or None

def validate(job: dict, source_root, *, require_exists: bool = True) -> dict:
    # require_exists=True (the GUI WRITE path, upsert) rejects a typo'd/non-existent
    # source at creation time. require_exists=False (the CLI READ path — run,
    # schedule, restore) enforces confinement/name/schedule/type/class but NOT
    # existence: restore runs on a fresh box with no local source, and a transient
    # mount blip must not drop a job from the schedule. Confinement (safe_resolve)
    # is checked in BOTH modes — it is the security gate, independent of existence.
    name = str(job.get("name", "")).strip()
    if not valid_name(name):
        raise ValueError("job name must be letters, digits, dot, dash, underscore")
    typ = job.get("type")
    if typ not in TYPES:
        raise ValueError(f"unknown job type {typ!r}")
    source = str(job.get("source", "")).strip().strip("/")
    if not source:
        raise ValueError("source is required")
    try:
        resolved = fsbrowse.safe_resolve(source_root, source)
    except fsbrowse.PathError:
        raise ValueError("source escapes the mount")
    if require_exists and not resolved.is_dir():
        raise ValueError(f"source folder does not exist: {source}")
    sched = str(job.get("schedule", "")).strip()
    fields = sched.split()
    # Exactly 5 fields separated by single ASCII spaces. `sched == " ".join(fields)`
    # rejects interior tabs/newlines/CRs and multi-spaces that .split() would tolerate
    # but that corrupt the TAB-delimited --list output the entrypoint parses.
    if len(fields) != 5 or sched != " ".join(fields):
        raise ValueError("schedule must be a 5-field cron expression (single-space separated)")
    cls = job.get("storage_class", "STANDARD")
    if cls not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class {cls!r}")
    out = {"name": name, "type": typ, "source": source, "schedule": sched,
           "enabled": bool(job.get("enabled", True)), "storage_class": cls}
    if typ == "archive":
        out["mirror"] = bool(job.get("mirror", False))
    # Compute retention first, then derive legacy fields from it (single source of truth)
    out["retention"] = _normalize_retention(job, typ)
    if typ == "versioned":
        # Mirror tiered keep policy if present, else zero dict
        ret = out["retention"]
        if ret["type"] == "tiered":
            out["keep"] = dict(ret["keep"])
        else:
            out["keep"] = {k: 0 for k in _KEEP_KEYS}
    elif typ == "versioned-files":
        # Mirror days value if present, else 0
        ret = out["retention"]
        out["retention_days"] = ret["days"] if ret["type"] == "days" else 0
    # --- Task 4 new fields (7.8): added, never removed; emit_shell ignores them. ---
    ca = _parse_iso(job.get("created_at"))
    if ca is not None:
        out["created_at"] = job["created_at"]
    out["assumptions"] = _normalize_assumptions(job)
    measured = _normalize_measured(job)
    if measured is not None:
        out["measured"] = measured
    ack = _normalize_acknowledged(job)
    if ack is not None:
        out["acknowledged"] = ack
    return out

def upsert(config_dir, job: dict, *, source_root, now=None) -> None:
    existing = _load_strict(config_dir)
    name = str(job.get("name", "")).strip()
    # created_at is the job's fixed birthday: keep the prior value on an edit,
    # stamp `now` for a new name. Do it before validate() so it passes through.
    prior = next((j for j in existing if j.get("name") == name), None)
    job = dict(job)
    if prior is not None and _parse_iso(prior.get("created_at")) is not None:
        job["created_at"] = prior["created_at"]
    elif _parse_iso(job.get("created_at")) is None:
        job["created_at"] = _now_iso(now)
    job = validate(job, source_root)
    jobs = [j for j in existing if j.get("name") != job["name"]]
    jobs.append(job)
    _path(config_dir).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")

# The full set of per-job caches removed when a job is deleted (7.1.9). Everything
# but the log dir lives under state/; the log dir is logs/runs/<job>/.
_CACHE_STATE_SUFFIXES = (".runs.jsonl", ".json", "-last.jsonl", "-rclone.log", "-prune.log",
                         "-vfiles.log", ".points.json", ".thaw.json", ".tested.json",
                         ".test-thaw.json")

def _remove_job_caches(cache_dir, name) -> None:
    import shutil
    state = Path(cache_dir, "state")
    for suf in _CACHE_STATE_SUFFIXES:
        try:
            (state / f"{name}{suf}").unlink()
        except (FileNotFoundError, OSError):
            pass
    shutil.rmtree(Path(cache_dir, "logs", "runs", name), ignore_errors=True)

def delete(config_dir, name, cache_dir=None) -> None:
    jobs = [j for j in _load_strict(config_dir) if j.get("name") != name]
    _path(config_dir).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")
    # Only after the config write commits (which raises on a corrupt file, leaving
    # the caches intact) do we drop the job's caches (7.1.9).
    if cache_dir is not None and valid_name(name):
        _remove_job_caches(cache_dir, name)

def set_enabled(config_dir, name, enabled: bool) -> dict | None:
    """Flip one job's `enabled` flag (Pause/Resume). Strict load so a corrupt
    jobs.json raises JobsFileError like delete() rather than clobbering it."""
    jobs = _load_strict(config_dir)
    out = None
    for j in jobs:
        if j.get("name") == name:
            j["enabled"] = bool(enabled)
            out = j
    if out is not None:
        _path(config_dir).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")
    return out

def _crontab_path(cache_dir) -> Path:
    return Path(cache_dir, "crontab")

def _signal_supercronic(cache_dir) -> None:
    # After every crontab write, SIGUSR2 the scheduler so it reloads even where
    # inotify does not fire (Unraid FUSE). A missing/unparseable pid or a dead
    # process (running without the scheduler is legal) is ignored silently (7.3).
    try:
        pid = int(Path(cache_dir, "supercronic.pid").read_text().strip())
        os.kill(pid, signal.SIGUSR2)
    except (FileNotFoundError, ProcessLookupError, ValueError, OSError):
        pass

def render_crontab(config_dir, cache_dir, scripts_dir, *, dry_run=False, source_root=None) -> str:
    """Render the crontab in exactly entrypoint.sh:emit_crontab's format —
    `<schedule> <scripts_dir>/backup-job.sh <name>` for each enabled, valid job.
    Invalid jobs (confinement/name/schedule) are dropped just as `--list` drops
    them, so on-disk == this render whenever nothing was hand-edited (crontab_stale).
    dry_run=True returns the text without writing or signalling (7.3, 7.8)."""
    source_root = source_root if source_root is not None else os.environ.get("SOURCE_ROOT", "/backup/media")
    lines = []
    for job in load(config_dir):
        try:
            v = validate(job, source_root, require_exists=False)
        except ValueError:
            continue
        if not v.get("enabled"):
            continue
        lines.append(f"{v['schedule']} {scripts_dir}/backup-job.sh {v['name']}")
    text = "".join(line + "\n" for line in lines)
    if not dry_run:
        p = _crontab_path(cache_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, p)
        _signal_supercronic(cache_dir)
    return text

def emit_shell(job: dict) -> str:
    q = shlex.quote
    lines = [f"JOB_NAME={q(job['name'])}", f"JOB_TYPE={q(job['type'])}",
             f"JOB_SOURCE={q(job['source'])}", f"JOB_STORAGE_CLASS={q(job.get('storage_class','STANDARD'))}"]
    r = job.get("retention") or {"type": "days", "days": 180}
    lines.append(f"JOB_RETENTION_TYPE={q(r['type'])}")
    if r["type"] == "days":
        lines.append(f"JOB_RETENTION_DAYS={int(r['days'])}")
    elif r["type"] == "count":
        lines.append(f"JOB_RETENTION_COUNT={int(r['count'])}")
    elif r["type"] == "tiered":
        keep = r.get("keep", {})
        lines += [f"JOB_KEEP_{k.upper()}={int(keep.get(k, 0))}" for k in _KEEP_KEYS]
    if job["type"] == "archive":
        lines.append(f"JOB_MIRROR={'true' if job.get('mirror') else 'false'}")
    return "\n".join(lines) + "\n"

def _main(argv: list[str]) -> int:
    # config/jobs.json lives on the writable /config mount and is UNTRUSTED at read
    # time: a hand-edited (non-GUI-written) file bypasses upsert()'s write-time
    # validate(). Re-validate here — the one gate the runner/scheduler/restore all
    # pass through — so source confinement + name charset + schedule shape hold at
    # RUN and SCHEDULE time, not just at GUI-write time. SOURCE_ROOT matches the same
    # var backup-job.sh interpolates as "$SOURCE_ROOT/$JOB_SOURCE" (config.sh default).
    config_dir = os.environ.get("CONFIG_DIR", "/config")
    source_root = os.environ.get("SOURCE_ROOT", "/backup/media")
    if argv == ["--list"]:
        for job in load(config_dir):
            try:  # confinement/name/schedule only — a transient mount blip must not drop it
                v = validate(job, source_root, require_exists=False)
            except ValueError as e:
                print(f"skipping invalid job {job.get('name', '?')!r}: {e}", file=sys.stderr)
                continue
            enabled = "1" if v.get("enabled") else "0"
            print(f"{enabled}\t{v['schedule']}\t{v['name']}")
        return 0
    if len(argv) != 1:
        print("usage: python3 -m app.gui.jobs_io <job-name> | --list", file=sys.stderr); return 2
    job = get(config_dir, argv[0])
    if job is None:
        print(f"no such job: {argv[0]}", file=sys.stderr); return 3
    try:  # RUN path: confinement/name/schedule/type/class. NOT existence — this
          # emit path is shared by restore.sh, which runs on a fresh/rebuilt box
          # where the local source is legitimately absent (it restores FROM S3).
          # backup-job.sh's own `[ -d "$src" ]` is the existence gate for backups.
        job = validate(job, source_root, require_exists=False)
    except ValueError as e:
        print(f"invalid job {argv[0]!r}: {e}", file=sys.stderr); return 4
    sys.stdout.write(emit_shell(job)); return 0

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
