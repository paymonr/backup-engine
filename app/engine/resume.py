# app/engine/resume.py — auto-resume of backups interrupted by a container restart.
#
#   python3 -m app.engine.resume
#
# On boot (after `runs boot` has reconciled any dangling RUNNING record to
# `aborted` — see runs.boot/reconcile), re-trigger every enabled job whose
# latest backup run was cut short by the restart. A `paused` run (a deliberate,
# graceful stop) is NEVER auto-resumed — only `aborted` (the runner never got a
# chance to report) qualifies. Bounded by BE_MAX_RESUMES per job so a job that
# keeps crashing on resume doesn't loop forever; the cap resets on the job's
# next clean (`ok`) run (scripts/backup-job.sh removes state/<job>.resumes on
# success).
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import runs
from ..gui import config_io, jobs_io, runner


def _resume_count(cache, job) -> int:
    p = Path(cache, "state", f"{job}.resumes")
    try:
        return int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_resume(cache, job) -> None:
    p = Path(cache, "state", f"{job}.resumes")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(_resume_count(cache, job) + 1))


def resume_interrupted(cfg, *, trigger=None, log=print) -> int:
    """Re-trigger every job whose latest backup run is `aborted`. Returns the
    count of jobs re-triggered.

    A job is re-triggered iff: auto-resume is on (config_io.auto_resume_on_boot);
    the job is enabled; its latest backup run's outcome is exactly `aborted`
    (never `running`/`ok`/`failed`/`paused`); its lock is free (no run already in
    progress); and its resume count is below BE_MAX_RESUMES. Each trigger bumps
    the resume counter (state/<job>.resumes) BEFORE launching, and sets
    BE_RESUME=1 in the launched job's environment.
    """
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not config_io.auto_resume_on_boot(config_dir):
        return 0
    trigger = trigger or (lambda name, env=None: runner.trigger_job(cfg["SCRIPTS_DIR"], name, env=env))
    max_resumes = config_io.retry_settings(config_dir)["max_resumes"]
    n = 0
    for j in jobs_io.load(config_dir):
        name = j.get("name")
        if not name or not j.get("enabled", True):
            continue
        recs = runs.read_runs(cache, name, kinds=runs.BACKUP_KINDS, reconcile=False).records
        if not recs or recs[0].outcome != "aborted":
            continue
        if runs.is_locked(cache, name) or _resume_count(cache, name) >= max_resumes:
            continue
        _bump_resume(cache, name)
        env = os.environ.copy()
        env["BE_RESUME"] = "1"
        trigger(name, env=env)
        n += 1
        log(f"resume: re-triggered interrupted job '{name}'")
    return n


def _cfg_from_env() -> dict:
    return {"CACHE_DIR": os.environ.get("CACHE_DIR", "/cache"),
            "CONFIG_DIR": os.environ.get("CONFIG_DIR", "/config"),
            "SCRIPTS_DIR": os.environ.get("SCRIPTS_DIR", "/app/scripts")}


def main() -> int:
    # Boot-time entry point (called from the entrypoint, Task 7): must never raise
    # and never block boot on a resume failure (same contract as runs._boot_cli).
    try:
        n = resume_interrupted(_cfg_from_env())
        print(f"resume: {n} interrupted job(s) re-triggered")
    except Exception as e:                    # noqa: BLE001 — boot must never fail here
        print(f"resume: failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
