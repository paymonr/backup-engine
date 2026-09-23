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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import runs
from . import storage_summary
from ..estimator import usage, billing
from ..gui import config_io, jobs_io, provision

KINDS = ("usage-refresh", "billing-check", "probe", "storage-summary")


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
    """The runtime AWS key for the probe. Prefer the CURRENT secrets.env over the
    process environment.

    Ordering matters: the container's entrypoint (lib/config.sh load_config) exports
    the runtime key into this long-running Flask process AT STARTUP, so `os.environ`
    holds whatever key was in secrets.env when the container started. After a
    re-provision writes a NEW key to secrets.env, that env is STALE (it still carries
    the old — possibly already-deleted — key) while secrets.env is the source of
    truth. Reading env first would make the probe (a child of Flask that inherits
    os.environ) fail with InvalidAccessKeyId against a live, correctly-provisioned
    destination. So parse secrets.env FIRST — with a LOCAL reader that leaves
    config_io's write-only contract untouched — and fall back to the process env only
    when secrets.env carries no runtime key/secret (e.g. a fresh container before any
    provision)."""
    p = Path(config_dir, "secrets.env")
    vals: dict[str, str] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
    key = vals.get("AWS_ACCESS_KEY_ID", "")
    secret = vals.get("AWS_SECRET_ACCESS_KEY", "")
    if key and secret:
        return key, secret
    return (os.environ.get("AWS_ACCESS_KEY_ID", ""),
            os.environ.get("AWS_SECRET_ACCESS_KEY", ""))


# A freshly-created IAM access key can transiently 401 (InvalidAccessKeyId, or an
# invalid-security-token error) for a few seconds until it propagates across AWS. In
# the probe — which may fire the instant provisioning writes a new key — ride that
# out with a bounded retry rather than record a false failure. Scoped to the probe
# ONLY: provision.validate_runtime_key's behaviour for the admin/apply flow is
# unchanged.
_PROBE_TRANSIENT_RE = re.compile(
    r"InvalidAccessKeyId|InvalidClientTokenId|security token .* invalid|RequestExpired",
    re.I,
)
# Backoff between retries, in seconds. One initial attempt + these retries; the sum
# bounds the extra wait to ~18s so an immediate post-provision probe rides out key
# propagation without hanging the operation.
_PROBE_RETRY_DELAYS = (1, 2, 3, 3, 4, 5)


def _validate_with_propagation_retry(bucket, region, key, secret, *, log) -> None:
    """provision.validate_runtime_key, but tolerant of a not-yet-propagated key:
    on a transient InvalidAccessKeyId/token error, retry with backoff (bounded by
    _PROBE_RETRY_DELAYS) before letting the ValidationError propagate. A
    non-transient failure (AccessDenied, NoSuchBucket, …) is re-raised immediately."""
    for attempt in range(len(_PROBE_RETRY_DELAYS) + 1):
        try:
            provision.validate_runtime_key(bucket, region, key, secret)
            return
        except provision.ValidationError as e:
            detail = getattr(e, "detail", "") or str(e)
            if attempt >= len(_PROBE_RETRY_DELAYS) or not _PROBE_TRANSIENT_RE.search(detail):
                raise
            delay = _PROBE_RETRY_DELAYS[attempt]
            log(f"probe: runtime key not propagated yet ({e.step}); retry in {delay}s")
            time.sleep(delay)


# --- the three operations --------------------------------------------------

def usage_refresh(cfg, *, log) -> None:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    bucket = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
    if not bucket:
        raise SysopError("no S3 bucket is configured")
    jobs = jobs_io.load(config_dir)
    rclone_config = str(Path(cache, "rclone.conf"))

    # A job with ITS OWN dedicated bucket (multi-bucket, Task 12) never lives in
    # the base bucket, so it must be measured against its own bucket separately
    # (below) instead of folding into -- or being silently dropped from -- the
    # base collect.
    dedicated = [j for j in jobs if j.get("dedicated") and j.get("bucket")]
    dedicated_names = {j["name"] for j in dedicated}
    non_dedicated = [j for j in jobs if j["name"] not in dedicated_names]

    # archive AND versioned-files jobs each write to media/<name> (estimate_io._size_for).
    base_media = [j["name"] for j in non_dedicated if j.get("type") in ("archive", "versioned-files")]
    base_has_versioned = any(j.get("type") == "versioned" for j in non_dedicated)
    data = usage.collect_usage(bucket, base_media, base_has_versioned, rclone_config=rclone_config)

    for j in dedicated:
        job_bucket = j["bucket"]
        if j.get("type") == "versioned":
            # estimate_io keys a dedicated versioned job's usage as "appdata:<name>"
            # (never the shared "appdata") so it isn't folded into every other
            # versioned job's aggregate.
            d = usage.collect_usage(job_bucket, [], True, rclone_config=rclone_config)
            data[f"appdata:{j['name']}"] = d.get("appdata")
        elif j.get("type") in ("archive", "versioned-files"):
            # The media/<name> key is bucket-agnostic -- only the source bucket
            # differs -- so merge straight in under the same key.
            d = usage.collect_usage(job_bucket, [j["name"]], False, rclone_config=rclone_config)
            data[f"media/{j['name']}"] = d[f"media/{j['name']}"]

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
        _validate_with_propagation_retry(bucket, region, key, secret, log=log)
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


# --- storage summary (spec 2026-09-23 §4, R-B8) ------------------------------------------------

SUMMARY_FRESH_S = 600      # an after-run scan skips a folder scanned this recently (several
                           # Snapshot jobs share appdata/); Refresh now always scans


def _summary_target(cfg, params: dict | None) -> tuple[str, str] | None:
    """(bucket, folder) to scan, or None when there is nothing to do here: S3 rules aren't
    managed (below level 4 / no role), a custom S3 endpoint, or an unknown job."""
    from . import lifecycle                   # local: lifecycle imports sysop lazily too
    config_dir = cfg["CONFIG_DIR"]
    env = config_io.read_backup_env(config_dir)
    if env.get("S3_ENDPOINT", "").strip() or not lifecycle.managed(config_dir):
        return None
    params = params or {}
    if params.get("job"):
        return lifecycle.folder_of_job(env.get("S3_BUCKET", "").strip(), jobs_io.load(config_dir), params["job"])
    if params.get("bucket") and params.get("folder") is not None:
        return params["bucket"], params["folder"]
    return None


def _summary_skip(cfg, params: dict | None) -> bool:
    """Silent skips (no Activity record at all): nothing to scan here, or -- for the after-run
    scan only -- a summary of the same folder taken in the last SUMMARY_FRESH_S seconds."""
    target = _summary_target(cfg, params)
    if target is None:
        return True
    if os.environ.get("BE_TRIGGER", "manual") == "scheduled":
        s = storage_summary.load(cfg["CACHE_DIR"], *target)
        at = storage_summary._parse((s or {}).get("scanned_at"))
        if at is not None and time.time() - at.timestamp() < SUMMARY_FRESH_S:
            return True
    return False


def storage_summary_op(cfg, *, log, job=None, bucket=None, folder=None, run=provision._run_aws) -> None:
    target = _summary_target(cfg, {"job": job, "bucket": bucket, "folder": folder})
    if target is None:
        raise SysopError("nothing to scan: S3 rules aren't managed here, or the job has no folder")
    bucket, folder = target
    region = (config_io.read_backup_env(cfg["CONFIG_DIR"]).get("AWS_REGION") or "us-east-1").strip()
    key, secret = _runtime_key(cfg["CONFIG_DIR"])
    summary = storage_summary.scan(bucket, folder, region=region, key=key, secret=secret, run=run, log=log)
    storage_summary.save(cfg["CACHE_DIR"], summary)
    log(f"storage summary: {folder or 'whole bucket'} in {bucket} — {summary['noncurrent_versions']:,} old "
        f"versions, {summary['current_objects']:,} current files, {summary['delete_markers']:,} delete markers")


_OPS = {"usage-refresh": usage_refresh, "billing-check": billing_check, "probe": probe,
        "storage-summary": storage_summary_op}


# --- orchestration ---------------------------------------------------------

def run(kind: str, params: dict | None = None) -> int:
    """Run one system operation end to end. Always returns 0 (7.7.3)."""
    if kind not in _OPS:
        print(f"sysop: unknown operation {kind!r}", file=sys.stderr)
        return 2
    cfg = _cfg_from_env()
    if kind == "storage-summary" and _summary_skip(cfg, params):
        return 0
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
            _OPS[kind](cfg, log=log, **(params or {}))
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
        print("usage: python3 -m app.engine.sysop usage-refresh|billing-check|probe|"
              "storage-summary [--job NAME | --bucket B --folder F]", file=sys.stderr)
        return 2
    params = None
    if argv[0] == "storage-summary":
        import argparse
        ap = argparse.ArgumentParser(prog="python3 -m app.engine.sysop storage-summary")
        ap.add_argument("--job")
        ap.add_argument("--bucket")
        ap.add_argument("--folder")
        a = ap.parse_args(argv[1:])
        params = {"job": a.job, "bucket": a.bucket, "folder": a.folder}
    return run(argv[0], params)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
