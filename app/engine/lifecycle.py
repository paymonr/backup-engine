# app/engine/lifecycle.py — S3 lifecycle rules owned by backup-engine (spec
# docs/superpowers/specs/2026-09-23-s3-rules-design.md). One owner per kind of
# history: Plain copy history IS its folder's S3 rule; Snapshot/File history get an
# undo window; housekeeping is bucket-wide. This part is the pure rules model plus
# the owner's settings (config/storage.json); I/O through the bucket-admin role is below.
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import runs
from ..gui import config_io, jobs_io, permissions, provision

APP_PREFIX = "backup-engine:"
HOUSEKEEPING_ID = APP_PREFIX + "housekeeping"
# IDs earlier versions created (OpenTofu backstops; the fixed dedicated-bucket rule).
LEGACY_IDS = frozenset({"backstop-appdata", "backstop-media", "backup-engine"})
DEFAULT_UNDO_DAYS = 30
DEFAULT_ABORT_DAYS = 7
MAX_NEWER = 100
SETTINGS_FILE = "storage.json"


@dataclass(frozen=True)
class Folder:
    folder: str                      # "" = the whole (dedicated) bucket
    kind: str                        # "plain" (Plain copy history) | "undo"
    jobs: tuple[str, ...]
    retention: dict | None = None    # plain only: the job's normalized history setting


def rule_id(folder: str) -> str:
    return APP_PREFIX + (folder or "bucket")


def folders_for(bucket: str, base: str, jobs: list[dict]) -> list[Folder]:
    """The app folders in `bucket`: on the base bucket one media/<job>/ per non-dedicated
    Plain copy / File history job and one shared appdata/ for Snapshot jobs; on a
    dedicated bucket its single job's whole bucket."""
    out: list[Folder] = []
    snapshot_jobs: list[str] = []
    for j in sorted(jobs, key=lambda j: j.get("name", "")):
        name, typ = j.get("name", ""), j.get("type")
        if j.get("dedicated") and j.get("bucket"):
            if j["bucket"] != bucket:
                continue
            folder = ""
        else:
            if bucket != base:
                continue
            folder = None
        if typ == "archive":
            out.append(Folder(f"media/{name}/" if folder is None else folder, "plain", (name,),
                              jobs_io._normalize_retention(j, typ)))
        elif typ == "versioned-files":
            out.append(Folder(f"media/{name}/" if folder is None else folder, "undo", (name,)))
        elif typ == "versioned":
            if folder is None:
                snapshot_jobs.append(name)
            else:
                out.append(Folder(folder, "undo", (name,)))
    if snapshot_jobs:
        out.append(Folder("appdata/", "undo", tuple(snapshot_jobs)))
    return sorted(out, key=lambda f: f.folder)


def buckets_for(base: str, jobs: list[dict]) -> list[str]:
    """The base bucket first, then each dedicated bucket (sorted, unique)."""
    extra = sorted({j["bucket"] for j in jobs if j.get("dedicated") and j.get("bucket")} - {base})
    return [base, *extra] if base else extra


# --- the owner's settings: config/storage.json --------------------------------

def _settings_path(config_dir: str) -> Path:
    return Path(config_dir, SETTINGS_FILE)


def load_settings(config_dir: str) -> dict:
    try:
        data = json.loads(_settings_path(config_dir).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    buckets = data.get("buckets")
    return {"version": 1, "buckets": buckets if isinstance(buckets, dict) else {}}


def save_settings(config_dir: str, data: dict) -> None:
    p = _settings_path(config_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)


def _pos_int(v, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


def bucket_settings(settings: dict, bucket: str) -> dict:
    b = ((settings or {}).get("buckets") or {}).get(bucket) or {}
    folders = b.get("folders")
    return {"versioning": "suspended" if b.get("versioning") == "suspended" else "on",
            "abort_uploads_days": _pos_int(b.get("abort_uploads_days"), DEFAULT_ABORT_DAYS),
            "delete_marker_cleanup": b.get("delete_marker_cleanup", True) is not False,
            "folders": folders if isinstance(folders, dict) else {}}


def undo_days(bset: dict, folder: str) -> int:
    return _pos_int((bset["folders"].get(folder) or {}).get("undo_days"), DEFAULT_UNDO_DAYS)


# --- rules ----------------------------------------------------------------------

def _rule(folder: str, **actions) -> dict:
    return {"ID": rule_id(folder), "Status": "Enabled", "Filter": {"Prefix": folder}, **actions}


def plain_rule(folder: str, retention: dict) -> dict | None:
    """Plain copy's history setting as its S3 rule; None = keep everything."""
    t = (retention or {}).get("type")
    if t == "days":
        nce = {"NoncurrentDays": max(1, int(retention.get("days") or 0))}
    elif t == "count":
        nce = {"NoncurrentDays": max(1, int(retention.get("days") or 1)),
               "NewerNoncurrentVersions": min(MAX_NEWER, max(1, int(retention["count"])))}
    else:   # keep_all (tiered is Snapshot-only and never reaches a Plain copy folder)
        return None
    return _rule(folder, NoncurrentVersionExpiration=nce)


def undo_rule(folder: str, days: int) -> dict:
    return _rule(folder, NoncurrentVersionExpiration={"NoncurrentDays": days})


def housekeeping_rule(bset: dict) -> dict:
    r = {"ID": HOUSEKEEPING_ID, "Status": "Enabled", "Filter": {"Prefix": ""},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": bset["abort_uploads_days"]}}
    if bset["delete_marker_cleanup"]:
        r["Expiration"] = {"ExpiredObjectDeleteMarker": True}
    return r


def desired_rules(bucket: str, base: str, jobs: list[dict], settings: dict) -> list[dict]:
    bset = bucket_settings(settings, bucket)
    rules = []
    for f in folders_for(bucket, base, jobs):
        r = plain_rule(f.folder, f.retention) if f.kind == "plain" else undo_rule(f.folder, undo_days(bset, f.folder))
        if r:
            rules.append(r)
    rules.append(housekeeping_rule(bset))
    return rules


def is_app_rule(rule: dict) -> bool:
    rid = (rule or {}).get("ID", "")
    return rid.startswith(APP_PREFIX) or rid in LEGACY_IDS


def merge(live_rules: list[dict], app_rules: list[dict]) -> list[dict]:
    """S3 replaces a bucket's whole configuration: keep every console rule as it is."""
    return [r for r in live_rules if not is_app_rule(r)] + list(app_rules)


def _norm(rule: dict) -> dict:
    r = json.loads(json.dumps(rule))
    if "Filter" not in r and "Prefix" in r:          # S3's older top-level Prefix form
        r["Filter"] = {"Prefix": r.pop("Prefix")}
    if isinstance(r.get("Filter"), dict) and not r["Filter"]:
        r["Filter"] = {"Prefix": ""}
    return r


def app_rules_of(rules: list[dict]) -> dict[str, dict]:
    return {r["ID"]: _norm(r) for r in rules if is_app_rule(r)}


def describe(rule: dict) -> str:
    """One app rule in plain words (Activity lines, flashes)."""
    where = (rule.get("Filter") or {}).get("Prefix", "") or "whole bucket"
    parts = []
    nce = rule.get("NoncurrentVersionExpiration") or {}
    if "NewerNoncurrentVersions" in nce:
        parts.append(f"newest {nce['NewerNoncurrentVersions']} old versions of each file kept")
        if nce.get("NoncurrentDays", 1) > 1:
            parts.append(f"older ones removed {nce['NoncurrentDays']} days after being replaced")
    elif "NoncurrentDays" in nce:
        parts.append(f"old versions removed {nce['NoncurrentDays']} days after being replaced")
    if "AbortIncompleteMultipartUpload" in rule:
        parts.append(f"abandoned uploads cleared after {rule['AbortIncompleteMultipartUpload']['DaysAfterInitiation']} days")
    if (rule.get("Expiration") or {}).get("ExpiredObjectDeleteMarker"):
        parts.append("leftover delete markers cleared")
    return f"{where}: " + "; ".join(parts)


# --- I/O through the bucket-admin role ---------------------------------------------

class LifecycleError(Exception):
    """kind: not_managed (below level 4 / no role) | unsupported (storage has no
    lifecycle API) | role (couldn't assume the role) | aws (a lifecycle call failed).
    `detail` is secret-scrubbed."""
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"s3 rules: {kind}")
        self.kind, self.detail = kind, detail


_UNSUPPORTED = ("NotImplemented", "MethodNotAllowed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def managed(config_dir: str) -> bool:
    return (permissions.feature_available(config_dir, "s3-rules")
            and bool(config_io.bucket_admin_role_arn(config_dir)))


def role_creds(config_dir: str, region: str, *, run=provision._run_aws) -> dict:
    from .sysop import _runtime_key      # local import: sysop is heavy and imports gui modules
    key, secret = _runtime_key(config_dir)
    try:
        return provision.assume_role(config_io.bucket_admin_role_arn(config_dir), region=region,
                                     key=key, secret=secret, run=run)
    except provision.AssumeRoleError as e:
        raise LifecycleError("role", provision._scrub(str(e), key, secret))


def _call(run, creds, region, args):
    return run(args, region=region, key=creds["AWS_ACCESS_KEY_ID"],
               secret=creds["AWS_SECRET_ACCESS_KEY"], session_token=creds.get("AWS_SESSION_TOKEN"))


def _fail(creds, stderr: str) -> LifecycleError:
    detail = provision._scrub(stderr or "", creds.get("AWS_ACCESS_KEY_ID", ""),
                              creds.get("AWS_SECRET_ACCESS_KEY", ""), creds.get("AWS_SESSION_TOKEN", "")).strip()
    kind = "unsupported" if any(s in (stderr or "") for s in _UNSUPPORTED) else "aws"
    return LifecycleError(kind, detail)


def read_rules(bucket: str, creds: dict, region: str, *, run=provision._run_aws) -> list[dict]:
    cp = _call(run, creds, region, ["s3api", "get-bucket-lifecycle-configuration",
                                    "--bucket", bucket, "--output", "json"])
    if cp.returncode == 0:
        try:
            data = json.loads(cp.stdout or "{}")
        except ValueError:
            raise LifecycleError("aws", "unreadable lifecycle response")
        rules = data.get("Rules") if isinstance(data, dict) else None
        return list(rules) if isinstance(rules, list) else []
    if "NoSuchLifecycleConfiguration" in (cp.stderr or ""):
        return []
    raise _fail(creds, cp.stderr)


def write_rules(bucket: str, rules: list[dict], creds: dict, region: str, *, run=provision._run_aws) -> None:
    cp = _call(run, creds, region, ["s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket,
                                    "--lifecycle-configuration", json.dumps({"Rules": rules})])
    if cp.returncode != 0:
        raise _fail(creds, cp.stderr)


# --- state files ---------------------------------------------------------------------

def _state_dir(cache_dir: str) -> Path:
    return Path(cache_dir, "state", "lifecycle")


def load_applied(cache_dir: str, bucket: str) -> list[dict] | None:
    try:
        data = json.loads(Path(_state_dir(cache_dir), f"{bucket}.applied.json").read_text())
    except (OSError, ValueError):
        return None
    rules = data.get("rules") if isinstance(data, dict) else None
    return rules if isinstance(rules, list) else None


def save_applied(cache_dir: str, bucket: str, rules: list[dict]) -> None:
    d = _state_dir(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    Path(d, f"{bucket}.applied.json").write_text(json.dumps({"rules": rules, "applied_at": _now_iso()}))


def _status_path(cache_dir: str) -> Path:
    return Path(cache_dir, "state", "_lifecycle.json")


def load_status(cache_dir: str) -> dict:
    try:
        data = json.loads(_status_path(cache_dir).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_status(cache_dir: str, bucket: str, state: str, detail: str = "", alarm: dict | None = None) -> None:
    """state: ok | error | unsupported | restored | not_restored. An `alarm` (a tamper
    event) survives later clean checks until the owner acknowledges it."""
    data = load_status(cache_dir)
    prev = data.get(bucket) or {}
    entry = {"state": state, "checked_at": _now_iso(), "detail": detail}
    keep = alarm if alarm is not None else prev.get("alarm")
    if keep:
        entry["alarm"] = keep
    data[bucket] = entry
    _status_path(cache_dir).parent.mkdir(parents=True, exist_ok=True)
    _status_path(cache_dir).write_text(json.dumps(data, indent=2, sort_keys=True))


# --- sync: bring a bucket's app rules to the desired set ------------------------------

@dataclass
class SyncResult:
    bucket: str
    changed: bool
    lines: list


def _change_lines(before: list[dict], after: list[dict]) -> list[str]:
    a, b = app_rules_of(before), app_rules_of(after)
    lines = [f"now: {describe(b[k])}" for k in sorted(b) if a.get(k) != b[k]]
    lines += [f"removed: {describe(a[k])}" for k in sorted(set(a) - set(b))]
    return lines


def _context(cfg) -> tuple[str, str, str]:
    env = config_io.read_backup_env(cfg["CONFIG_DIR"])
    return (env.get("S3_BUCKET", "").strip(), (env.get("AWS_REGION") or "us-east-1").strip(),
            cfg["CACHE_DIR"])


def sync(cfg, bucket: str, *, run=provision._run_aws, jobs=None) -> SyncResult:
    """Apply the desired app rules to `bucket` (console rules kept). Records the
    result in state and, when anything changed, an Activity entry."""
    config_dir = cfg["CONFIG_DIR"]
    base, region, cache = _context(cfg)
    if not managed(config_dir):
        raise LifecycleError("not_managed", "S3 rules need the AWS permissions update")
    want = desired_rules(bucket, base, jobs_io.load(config_dir) if jobs is None else jobs,
                         load_settings(config_dir))
    try:
        creds = role_creds(config_dir, region, run=run)
        live = read_rules(bucket, creds, region, run=run)
        if app_rules_of(live) == app_rules_of(want) and not any(
                r.get("ID") in LEGACY_IDS for r in live):
            save_applied(cache, bucket, want)
            set_status(cache, bucket, "ok")
            return SyncResult(bucket, False, [])
        write_rules(bucket, merge(live, want), creds, region, run=run)
    except LifecycleError as e:
        set_status(cache, bucket, "unsupported" if e.kind == "unsupported" else "error", e.detail)
        raise
    save_applied(cache, bucket, want)
    set_status(cache, bucket, "ok")
    lines = _change_lines(live, want)
    runs.record_system(cache, kind="s3-rules", summary=f"S3 rules updated · {bucket}", lines=lines)
    return SyncResult(bucket, True, lines)


def sync_all(cfg, *, run=provision._run_aws) -> list[SyncResult]:
    base, _, _ = _context(cfg)
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    results, errors = [], []
    for bucket in buckets_for(base, jobs):
        try:
            results.append(sync(cfg, bucket, run=run, jobs=jobs))
        except LifecycleError as e:
            if e.kind == "not_managed":
                raise
            errors.append(e)
    if errors and not results:
        raise errors[0]
    return results
