# app/gui/ops.py — the operation launcher. Starts the backup runner and restore.sh
# DETACHED, with a run id pre-assigned in BE_RUN_ID so the child's very first record
# already carries the id the route redirects to (spec 7.5.2). Flask holds no handle,
# thread or state, so a page close or a GUI restart cannot lose the operation.
#
# Single-flight: no concurrent restore + backup for the SAME job. `ensure_free`
# (runs.is_locked) is an advisory probe the restore route uses to answer 409 nicely;
# the child's own `acquire_lock` (scripts/lib/common.sh) is the real guard, and it —
# not this probe — is what may ever skip a backup.
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..engine import runs
from . import fsbrowse


class OpsTimeout(Exception):
    """A sync helper exceeded its timeout."""


class OpsLocked(Exception):
    """The job is already running an operation (advisory single-flight probe)."""


def _cwd() -> str | None:
    return "/app" if os.path.isdir("/app") else None


# --- detached launch (7.5.2) ----------------------------------------------

def launch(cfg, argv: list[str], *, job: str, kind: str, trigger: str = "manual",
           env_extra: dict | None = None) -> str:
    """Spawn `bash <argv…>` detached with BE_RUN_ID pre-assigned; return the run id.

    The child writes its own start line after taking the lock, using BE_RUN_ID, so
    the record already exists under the id this returns."""
    run_id = runs.new_run_id()
    env = {**os.environ, "BE_RUN_ID": run_id, "BE_TRIGGER": trigger, **(env_extra or {})}
    Path(cfg["CACHE_DIR"], "logs", "runs", job).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["bash", *argv], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=_cwd())
    return run_id


def launch_py(cfg, module: str, args: list[str], *, kind: str, trigger: str = "manual",
              env_extra: dict | None = None) -> str:
    """python3 -m <module> … detached under job `_system`, same env contract as launch."""
    run_id = runs.new_run_id()
    env = {**os.environ, "BE_RUN_ID": run_id, "BE_TRIGGER": trigger, **(env_extra or {})}
    Path(cfg["CACHE_DIR"], "logs", "runs", runs.SYSTEM_JOB).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["python3", "-m", module, *args], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=_cwd())
    return run_id


def run_sync(cfg, argv: list[str], *, timeout) -> subprocess.CompletedProcess:
    """`bash <argv…>` run synchronously (list --json, thaw-status). Captures output;
    raises OpsTimeout on the deadline so the caller never hangs a request."""
    env = {**os.environ}
    try:
        return subprocess.run(["bash", *argv], capture_output=True, text=True,
                              timeout=timeout, cwd=_cwd(), env=env)
    except subprocess.TimeoutExpired as e:
        raise OpsTimeout(f"'{' '.join(argv)}' did not finish within {timeout}s") from e


# --- single-flight probe ---------------------------------------------------

def is_busy(cfg, job: str) -> bool:
    return runs.is_locked(cfg["CACHE_DIR"], job)


def ensure_free(cfg, job: str) -> None:
    if runs.is_locked(cfg["CACHE_DIR"], job):
        raise OpsLocked(f"another {job} operation is in progress")


# --- restore target validation (7.5.1) ------------------------------------

def _norm(p: str) -> str:
    return (p or "").strip().rstrip("/")


def _under(path: str, root: str) -> bool:
    """True when `path` equals `root` or sits under it (path-segment aware)."""
    if not root:
        return False
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def _relativize(path: str, host_root: str, container_root: str) -> str | None:
    """Map a restore target given as a host OR container path to a path relative
    to the container restore root; None when it is under neither."""
    for base in (host_root, container_root):
        base = (base or "").rstrip("/")
        if not base:
            continue
        if path == base:
            return ""
        if path.startswith(base + "/"):
            return path[len(base) + 1:]
    return None


def _blocker(code: str, message: str, status: int = 400) -> dict:
    return {"ok": False, "code": code, "message": message, "status": status}


def validate_target(cfg, job, target_host: str) -> dict:
    """Structured validation of the restore target folder (the route turns a
    non-ok result into an inline BLOCKER). A restore can never overwrite what it
    protects: the target must be a new (or empty) folder under the restore mount,
    never the job's own source.

    Returns {"ok": True, "container_path": "/restore/…"} or
            {"ok": False, "code", "message", "status"}."""
    restore_root = cfg["RESTORE_ROOT"]
    restore_root_host = cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore")
    source_root = cfg.get("SOURCE_ROOT", "")
    source_root_host = cfg.get("SOURCE_ROOT_HOST", "")
    job_source = (job or {}).get("source", "")

    t = _norm(target_host)
    if not t:
        return _blocker("empty", "Choose a folder to write the restored files to.")

    # The restore mount is checked FIRST: a path under it is structurally safe even
    # though the mount itself lives under SOURCE_ROOT_HOST (/mnt/user/restore is under
    # /mnt/user). Only a path that is NOT under the mount can hit the source refusal.
    rel = _relativize(t, restore_root_host, restore_root)
    if rel is None:
        # Not under the restore mount — is it (a prefix of) the live source?
        refuse = [source_root, source_root_host]
        if source_root_host and job_source:
            refuse.append(str(Path(source_root_host, job_source)))
        if source_root and job_source:
            refuse.append(str(Path(source_root, job_source)))
        for pre in refuse:
            if pre and _under(t, _norm(pre)):
                return _blocker("source", "Typing the live source path here is refused — "
                                          "a restore can never overwrite what it is protecting.")
        return _blocker("escape", f"The folder must be inside {restore_root_host}.")

    try:
        container = fsbrowse.safe_resolve(restore_root, rel)
    except fsbrowse.PathError:
        return _blocker("escape", f"The folder must be inside {restore_root_host}.")

    if container.exists():
        if not container.is_dir() or any(container.iterdir()):
            return _blocker("nonempty", "That folder already has files in it. "
                                        "Pick a new folder so nothing gets overwritten.")
    return {"ok": True, "container_path": str(container)}


def default_target(cfg, job, *, now=None) -> str:
    """`${RESTORE_ROOT_HOST}/<job>/<YYYY-MM-DD>/`, auto-suffixed -2, -3, … when the
    folder already exists and is non-empty (5.2). Returned as the host path."""
    restore_root = cfg["RESTORE_ROOT"]
    restore_root_host = (cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore")).rstrip("/")
    name = job["name"]
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    base = f"{restore_root_host}/{name}/{day}"

    def _is_free(host_path: str) -> bool:
        rel = _relativize(host_path, restore_root_host, restore_root)
        c = Path(restore_root, rel) if rel is not None else Path(restore_root)
        return (not c.exists()) or (c.is_dir() and not any(c.iterdir()))

    if _is_free(base):
        return base + "/"
    n = 2
    while True:
        cand = f"{base}-{n}"
        if _is_free(cand):
            return cand + "/"
        n += 1
