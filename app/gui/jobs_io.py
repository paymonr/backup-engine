# app/gui/jobs_io.py — the ONLY reader/writer of config/jobs.json. Validates job
# defs (name charset + source confined to SOURCE_ROOT) and emits shell-safe vars
# for the bash runner. Never runs a backup itself.
from __future__ import annotations
import json, os, re, sys, shlex
from pathlib import Path
from . import fsbrowse

JOBS_FILE = "jobs.json"
# \Z (end of string), NOT $: Python's `$` also matches just before a trailing
# newline, which would let "name\n" pass the charset gate and reach restic --tag,
# rclone media/<name>/, state/<name>.json, the lock, and the crontab name field.
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
TYPES = ("versioned", "archive")
STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE")
_KEEP_KEYS = ("last", "daily", "weekly", "monthly")

def valid_name(s: str) -> bool:
    return bool(JOB_NAME_RE.match(s or "")) and s not in (".", "..")

def _path(config_dir) -> Path:
    return Path(config_dir, JOBS_FILE)

class JobsFileError(ValueError):
    """The on-disk jobs.json exists but can't be parsed. Raised only on the WRITE
    path (upsert/delete) so a save never clobbers the user's (unparseable but
    hand-fixable) bytes. A ValueError subclass so existing `except ValueError`
    write-path handlers still catch it, while routes can catch it specifically."""

def _parse_jobs(text: str) -> list[dict]:
    # Parse jobs.json text into a list of job dicts, or raise ValueError with a
    # human reason. Shape errors (top-level not a dict, `jobs` not a list, or a
    # non-dict entry) are treated like a parse error: the file as a whole is
    # unusable, so callers degrade to "no jobs" rather than crash downstream on
    # `j.get(...)` / `j["name"]`.
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
    return list(jobs)

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
        return _parse_jobs(p.read_text())
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
    if typ == "versioned":
        keep = job.get("keep") or {}
        out["keep"] = {k: max(0, int(keep.get(k, 0))) for k in _KEEP_KEYS}
    else:
        out["mirror"] = bool(job.get("mirror", False))
    return out

def upsert(config_dir, job: dict, *, source_root) -> None:
    job = validate(job, source_root)
    jobs = [j for j in _load_strict(config_dir) if j.get("name") != job["name"]]
    jobs.append(job)
    _path(config_dir).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")

def delete(config_dir, name) -> None:
    jobs = [j for j in _load_strict(config_dir) if j.get("name") != name]
    _path(config_dir).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")

def emit_shell(job: dict) -> str:
    q = shlex.quote
    lines = [f"JOB_NAME={q(job['name'])}", f"JOB_TYPE={q(job['type'])}",
             f"JOB_SOURCE={q(job['source'])}", f"JOB_STORAGE_CLASS={q(job.get('storage_class','STANDARD'))}"]
    if job["type"] == "versioned":
        keep = job.get("keep", {})
        lines += [f"JOB_KEEP_{k.upper()}={int(keep.get(k, 0))}" for k in _KEEP_KEYS]
    else:
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
