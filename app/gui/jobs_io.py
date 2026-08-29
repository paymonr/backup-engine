# app/gui/jobs_io.py — the ONLY reader/writer of config/jobs.json. Validates job
# defs (name charset + source confined to SOURCE_ROOT) and emits shell-safe vars
# for the bash runner. Never runs a backup itself.
from __future__ import annotations
import json, os, re, sys, shlex
from pathlib import Path
from . import fsbrowse

JOBS_FILE = "jobs.json"
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TYPES = ("versioned", "archive")
STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE")
_KEEP_KEYS = ("last", "daily", "weekly", "monthly")

def valid_name(s: str) -> bool:
    return bool(JOB_NAME_RE.match(s or "")) and s not in (".", "..")

def _path(config_dir) -> Path:
    return Path(config_dir, JOBS_FILE)

def load(config_dir) -> list[dict]:
    p = _path(config_dir)
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return list(data.get("jobs", []))

def get(config_dir, name) -> dict | None:
    return next((j for j in load(config_dir) if j.get("name") == name), None)

def validate(job: dict, source_root) -> dict:
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
    if not resolved.is_dir():
        raise ValueError(f"source folder does not exist: {source}")
    sched = str(job.get("schedule", "")).strip()
    if len(sched.split()) != 5:
        raise ValueError("schedule must be a 5-field cron expression")
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
    jobs = [j for j in load(config_dir) if j.get("name") != job["name"]]
    jobs.append(job)
    _path(config_dir).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")

def delete(config_dir, name) -> None:
    jobs = [j for j in load(config_dir) if j.get("name") != name]
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
    if len(argv) != 1:
        print("usage: python3 -m app.gui.jobs_io <job-name>", file=sys.stderr); return 2
    job = get(os.environ.get("CONFIG_DIR", "/config"), argv[0])
    if job is None:
        print(f"no such job: {argv[0]}", file=sys.stderr); return 3
    sys.stdout.write(emit_shell(job)); return 0

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
