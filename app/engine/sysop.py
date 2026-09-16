# app/engine/sysop.py — detached SYSTEM operations, recorded under `_system`.
#
#   python3 -m app.engine.sysop usage-refresh|billing-check|probe
#
# Each op: takes locks/_system.lock (fcntl — the same lock runs.reconcile probes,
# so a mid-op crash reconciles correctly), appends a start event to
# _system.runs.jsonl honouring BE_RUN_ID, tees its progress to
# logs/runs/_system/<id>.log, runs the existing engine/GUI function, appends the
# end event (ok/failed with the exception's message), and exits 0 (spec 7.7.3).
#
# These records carry `job: null` and hold ONLY the _system lock — never a per-job
# lock — so they never interfere with a job's single-flight (7.1.1, 7.7.3).
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import runs
from ..estimator import usage, billing
from ..gui import config_io, jobs_io, provision

KINDS = ("usage-refresh", "billing-check", "probe")


class SysopError(Exception):
    """A system operation could not run (records a failed end event)."""


def _now_iso(epoch=None) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg_from_env() -> dict:
    return {"CACHE_DIR": os.environ.get("CACHE_DIR", "/cache"),
            "CONFIG_DIR": os.environ.get("CONFIG_DIR", "/config")}


def _run_id() -> str:
    rid = os.environ.get("BE_RUN_ID", "")
    return rid if runs.valid_run_id(rid) else runs.new_run_id()


def _runtime_key(config_dir: str) -> tuple[str, str]:
    """The runtime AWS key for the probe. Prefer the process environment (the
    container's load_config exports it); fall back to a LOCAL parse of secrets.env
    so config_io's write-only contract is left untouched."""
    key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if key and secret:
        return key, secret
    p = Path(config_dir, "secrets.env")
    vals: dict[str, str] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals.get("AWS_ACCESS_KEY_ID", ""), vals.get("AWS_SECRET_ACCESS_KEY", "")


# --- the three operations --------------------------------------------------

def usage_refresh(cfg, *, log) -> None:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    bucket = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
    if not bucket:
        raise SysopError("no S3 bucket is configured")
    jobs = jobs_io.load(config_dir)
    # archive AND versioned-files jobs each write to media/<name> (estimate_io._size_for).
    media_jobs = [j["name"] for j in jobs if j.get("type") in ("archive", "versioned-files")]
    has_versioned = any(j.get("type") == "versioned" for j in jobs)
    rclone_config = str(Path(cache, "rclone.conf"))
    data = usage.collect_usage(bucket, media_jobs, has_versioned, rclone_config=rclone_config)
    usage.save_cached(cache, data)
    log(f"usage: measured {sum(1 for v in data.values() if v)} of {len(data)} prefix(es)")


def billing_check(cfg, *, log) -> None:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    creds = config_io.read_cost_explorer_creds(config_dir)
    tag = config_io.read_backup_env(config_dir).get("COST_EXPLORER_TAG") or None
    out = {"fetched_at": time.time(), "months": None, "forecast": None, "tag": tag, "error": None}
    if creds is None:
        out["error"] = "No billing credential is stored."
    else:
        # A BillingError is an EXPECTED cache outcome, not a run failure: it lands
        # in billing.json's `error` member so no render ever calls Cost Explorer.
        try:
            out["months"] = billing.monthly_costs(creds, tag=tag)
            out["forecast"] = billing.forecast(creds)
        except billing.BillingError as e:
            out["error"] = str(e)
    Path(cache, "billing.json").write_text(json.dumps(out))
    log(f"billing: error={out['error'] or 'none'}")


def probe(cfg, *, log) -> None:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    env = config_io.read_backup_env(config_dir)
    bucket = env.get("S3_BUCKET", "").strip()
    region = (env.get("AWS_REGION", "") or "us-east-1").strip()
    key, secret = _runtime_key(config_dir)

    try:
        provision.validate_runtime_key(bucket, region, key, secret)
        dest = {"state": "ok", "probed_at": _now_iso(), "detail": "Write, read, delete — all OK"}
    except provision.ValidationError as e:
        # An unreachable destination is a RECORDED probe result, not a run failure.
        dest = {"state": "failed", "probed_at": _now_iso(),
                "detail": provision._scrub(getattr(e, "detail", "") or str(e), key, secret)}
    log(f"probe: destination {dest['state']}")

    ver = {"state": "unknown", "checked_at": _now_iso()}
    cp = provision._run_aws(["s3api", "get-bucket-versioning", "--bucket", bucket],
                           region=region, key=key, secret=secret)
    if cp.returncode == 0:
        try:
            status = (json.loads(cp.stdout or "{}") or {}).get("Status")
        except (ValueError, TypeError):
            status = None
        ver = {"state": "on" if status == "Enabled" else "off", "checked_at": _now_iso()}
    # AccessDenied (or any error) on get-bucket-versioning -> unknown (7.7.2).
    log(f"probe: versioning {ver['state']}")

    Path(cache, "state").mkdir(parents=True, exist_ok=True)
    Path(cache, "state", "_probe.json").write_text(
        json.dumps({"destination": dest, "versioning": ver}))


_OPS = {"usage-refresh": usage_refresh, "billing-check": billing_check, "probe": probe}


# --- orchestration ---------------------------------------------------------

def run(kind: str) -> int:
    """Run one system operation end to end. Always returns 0 (7.7.3)."""
    if kind not in _OPS:
        print(f"sysop: unknown operation {kind!r}", file=sys.stderr)
        return 2
    cfg = _cfg_from_env()
    cache = cfg["CACHE_DIR"]
    run_id = _run_id()
    Path(cache, "logs", "runs", runs.SYSTEM_JOB).mkdir(parents=True, exist_ok=True)
    Path(cache, "state").mkdir(parents=True, exist_ok=True)
    Path(cache, "locks").mkdir(parents=True, exist_ok=True)
    log_rel = f"logs/runs/{runs.SYSTEM_JOB}/{run_id}.log"

    # The _system lock is the SAME path runs.reconcile probes; hold it for the whole
    # op so a mid-op crash reconciles the dangling record, and a completed op leaves
    # it free with both events already written.
    lockfh = open(runs.lock_path(cache, runs.SYSTEM_JOB), "a")
    try:
        fcntl.flock(lockfh.fileno(), fcntl.LOCK_EX)
    except OSError:
        pass

    started = time.time()
    runs.append_event(cache, None, {
        "v": 1, "id": run_id, "job": None, "kind": kind, "event": "start",
        "trigger": os.environ.get("BE_TRIGGER", "manual"),
        "started_at": _now_iso(started), "pid": os.getpid(), "log": log_rel})

    outcome, error = "ok", None
    with open(Path(cache, log_rel), "a") as lf:
        def log(msg: str) -> None:
            lf.write(f"{_now_iso()} {msg}\n"); lf.flush()
        try:
            log(f"{kind} start")
            _OPS[kind](cfg, log=log)
            log(f"{kind} ok")
        except Exception as e:                      # noqa: BLE001 — any op failure -> failed end
            outcome = "failed"
            error = str(e) or e.__class__.__name__
            log(f"{kind} failed: {error}")

    runs.append_event(cache, None, {
        "v": 1, "id": run_id, "job": None, "kind": kind, "event": "end",
        "outcome": outcome, "finished_at": _now_iso(),
        "duration_s": int(time.time() - started),
        "exit_code": 0 if outcome == "ok" else 1, "error": error})

    try:
        fcntl.flock(lockfh.fileno(), fcntl.LOCK_UN)
    finally:
        lockfh.close()
    return 0


def main(argv) -> int:
    if not argv or argv[0] not in _OPS:
        print("usage: python3 -m app.engine.sysop usage-refresh|billing-check|probe", file=sys.stderr)
        return 2
    return run(argv[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
