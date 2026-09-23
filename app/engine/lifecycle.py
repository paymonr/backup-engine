# app/engine/lifecycle.py — S3 lifecycle rules owned by backup-engine (spec
# docs/superpowers/specs/2026-09-23-s3-rules-design.md). One owner per kind of
# history: Plain copy history IS its folder's S3 rule; Snapshot/File history get an
# undo window; housekeeping is bucket-wide. This part is the pure rules model plus
# the owner's settings (config/storage.json); I/O through the bucket-admin role is below.
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
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
    note: str = ""                   # why the folder's rule isn't the job's setting (owner words)


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
            # A malformed setting (hand-edited jobs.json, a Snapshot-only choice) must not
            # break the whole bucket's rules: keep everything -- no rule, the safe direction.
            try:
                retention, note = jobs_io._normalize_retention(j, typ), ""
            except ValueError:
                retention = {"type": "keep_all"}
                note = f"{name}: its history setting couldn't be read, so S3 keeps every old version"
            out.append(Folder(f"media/{name}/" if folder is None else folder, "plain", (name,),
                              retention, note))
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


def _write_atomic(path: Path, text: str) -> None:
    """tmp + os.replace: a reader (or a crash) never sees a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@contextmanager
def _flock(path: Path):
    """An exclusive fcntl lock held for the block (released on exit or process death)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save_settings(config_dir: str, data: dict) -> None:
    _write_atomic(_settings_path(config_dir), json.dumps(data, indent=2, sort_keys=True))


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


def notes_for(bucket: str, base: str, jobs: list[dict]) -> list[str]:
    """Owner-facing notes on folders whose rule isn't the job's own setting."""
    return [f.note for f in folders_for(bucket, base, jobs) if f.note]


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


def _days(n) -> str:
    """"1 day" / "30 days" (M4)."""
    return f"{n} day" if n == 1 else f"{n} days"


def describe(rule: dict) -> str:
    """One app rule in plain words (Activity lines, flashes, tamper alarms -- so a rule
    edited outside the app also names what it now does to current files)."""
    where = (rule.get("Filter") or {}).get("Prefix", "") or "whole bucket"
    if rule.get("Status") == "Disabled":
        return f"{where}: switched off"
    parts = []
    nce = rule.get("NoncurrentVersionExpiration") or {}
    if "NewerNoncurrentVersions" in nce:
        parts.append(f"newest {nce['NewerNoncurrentVersions']} old versions of each file kept")
        if nce.get("NoncurrentDays", 1) > 1:
            parts.append(f"older ones removed {_days(nce['NoncurrentDays'])} after being replaced")
    elif "NoncurrentDays" in nce:
        parts.append(f"old versions removed {_days(nce['NoncurrentDays'])} after being replaced")
    if "AbortIncompleteMultipartUpload" in rule:
        parts.append("abandoned uploads cleared after "
                     + _days(rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"]))
    if (rule.get("Expiration") or {}).get("ExpiredObjectDeleteMarker"):
        parts.append("leftover delete markers cleared")
    # never created by the app -- only present when someone changed the rule outside it
    parts += [w for w in destructive_actions(rule) if not w.startswith("removes old versions")]
    return f"{where}: " + ("; ".join(parts) or "no actions")


# --- console rules: anything not made by backup-engine -------------------------------------
# Never modified or deleted (the owner may have meant them). A new or changed one that
# can delete or move backups is alarmed once, via a fingerprint kept in applied.json.

def _console_entries(rules: list[dict]) -> list[tuple[str, str, dict]]:
    out = []
    for r in rules:
        if is_app_rule(r):
            continue
        h = hashlib.sha256(json.dumps(_norm(r), sort_keys=True).encode()).hexdigest()
        rid = r.get("ID")
        out.append((rid if isinstance(rid, str) and rid else f"(no ID) {h[:12]}", h, r))
    return out


def console_fingerprint(rules: list[dict]) -> dict[str, str]:
    """{rule ID: hash of the rule} for every console rule (S3 key order/filter form ignored)."""
    return {k: h for k, h, _ in _console_entries(rules)}


def _as_list(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else ([v] if isinstance(v, dict) else [])


def destructive_actions(rule: dict) -> list[str]:
    """What an enabled rule does that can delete or move backups, in words: expiring
    current files (Days/Date), removing old versions, moving files to another class."""
    if (rule or {}).get("Status") == "Disabled":
        return []
    out = []
    exp = rule.get("Expiration") if isinstance(rule.get("Expiration"), dict) else {}
    if "Days" in exp:
        out.append(f"expires current files {_days(exp['Days'])} after they're written")
    if "Date" in exp:
        out.append(f"expires current files on {str(exp['Date'])[:10]}")
    nce = rule.get("NoncurrentVersionExpiration")
    if isinstance(nce, dict):
        words = f"removes old versions {_days(nce.get('NoncurrentDays', '?'))} after being replaced"
        if "NewerNoncurrentVersions" in nce:
            words += f" (newest {nce['NewerNoncurrentVersions']} kept)"
        out.append(words)
    for t in _as_list(rule.get("Transitions")) + _as_list(rule.get("Transition")):
        when = (f"after {_days(t['Days'])}" if "Days" in t else
                f"on {str(t['Date'])[:10]}" if "Date" in t else "")
        out.append(f"moves current files to {t.get('StorageClass', 'another class')} {when}".rstrip())
    for t in (_as_list(rule.get("NoncurrentVersionTransitions"))
              + _as_list(rule.get("NoncurrentVersionTransition"))):
        out.append(f"moves old versions to {t.get('StorageClass', 'another class')} "
                   f"{_days(t.get('NoncurrentDays', '?'))} after being replaced")
    return out


def _where(rule: dict) -> str:
    f = _norm(rule).get("Filter") or {}
    f = f if isinstance(f, dict) else {}
    both = f.get("And") if isinstance(f.get("And"), dict) else {}
    prefix = f.get("Prefix") or both.get("Prefix") or ""
    narrowed = any(k in f for k in ("Tag", "ObjectSizeGreaterThan", "ObjectSizeLessThan")) or \
        any(k != "Prefix" for k in both)
    return (prefix or "whole bucket") + (", some files" if narrowed else "")


def console_changes(stored: dict | None, live: list[dict]) -> list[tuple[str, str]]:
    """Console rules new or changed since the stored fingerprint that can delete or move
    backups: [(rule ID, "<ID> (<where>): <actions>")]. No stored fingerprint (first
    apply, older state) = nothing to compare against = no changes."""
    if stored is None:
        return []
    out = []
    for key, h, rule in _console_entries(live):
        if stored.get(key) == h:
            continue
        words = destructive_actions(rule)
        if words:
            out.append((key, f"{key} ({_where(rule)}): " + "; ".join(words)))
    return out


# --- alarms ----------------------------------------------------------------------------------

_SEVERITY = {"restored": 1, "console_rule": 2, "not_restored": 3}


def merge_alarms(alarms: list[dict | None]) -> dict | None:
    """Several open alarms (one bucket over time, or several buckets) as one: the most
    severe kind heads it (ties: the latest), every console rule ID named by any of them is
    kept, so a later alarm can never hide an earlier one's rule, and `latest` is the newest
    `at` of them all (O2: acknowledge clears only what the owner saw)."""
    alarms = [a for a in alarms if isinstance(a, dict) and a]
    if not alarms:
        return None
    head = max(alarms, key=lambda a: (_SEVERITY.get(a.get("kind"), 0), a.get("at") or ""))
    out = dict(head)
    rules = sorted({r for a in alarms for r in (a.get("rules") or [])})
    if rules:
        out["rules"] = rules
    lines = []
    for a in alarms:
        lines += [ln for ln in (a.get("lines") or []) if ln not in lines]
    out["lines"] = lines[:50]
    out["latest"] = max((a.get("latest") or a.get("at") or "") for a in alarms)
    return out


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


# A hung endpoint must not stall a job save or the pre-backup check (which also has a
# `timeout` around it in backup-job.sh). Only lifecycle's own aws calls carry these.
_AWS_TIMEOUTS = ["--cli-connect-timeout", "10", "--cli-read-timeout", "30"]


def _timed(run):
    return lambda args, **kw: run([*args, *_AWS_TIMEOUTS], **kw)


def role_creds(config_dir: str, region: str, *, run=provision._run_aws) -> dict:
    from .sysop import _runtime_key      # local import: sysop is heavy and imports gui modules
    key, secret = _runtime_key(config_dir)
    try:
        return provision.assume_role(config_io.bucket_admin_role_arn(config_dir), region=region,
                                     key=key, secret=secret, run=_timed(run))
    except provision.AssumeRoleError as e:
        raise LifecycleError("role", provision._scrub(str(e), key, secret))


def _call(run, creds, region, args):
    return _timed(run)(args, region=region, key=creds["AWS_ACCESS_KEY_ID"],
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


def bucket_lock(cache_dir: str, bucket: str):
    """Held around a bucket's whole read-compare-write pass (sync and check), so a check
    never compares new live rules with not-yet-recorded applied ones (a false alarm
    and a revert) and two writers never interleave. (Bucket names can't start with
    "_", so they never collide with _status.lock.)"""
    return _flock(Path(_state_dir(cache_dir), f"{bucket}.lock"))


def _status_lock(cache_dir: str):
    return _flock(Path(_state_dir(cache_dir), "_status.lock"))


def _applied_doc(cache_dir: str, bucket: str) -> dict | None:
    try:
        data = json.loads(Path(_state_dir(cache_dir), f"{bucket}.applied.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_applied(cache_dir: str, bucket: str) -> list[dict] | None:
    rules = (_applied_doc(cache_dir, bucket) or {}).get("rules")
    return rules if isinstance(rules, list) else None


def load_console_fingerprint(cache_dir: str, bucket: str) -> dict | None:
    """The console rules' fingerprint recorded with the last apply; None = none recorded
    (no apply yet, or state from before fingerprints)."""
    fp = (_applied_doc(cache_dir, bucket) or {}).get("console")
    return fp if isinstance(fp, dict) else None


def save_applied(cache_dir: str, bucket: str, rules: list[dict], console: dict | None = None) -> None:
    doc = {"rules": rules, "applied_at": _now_iso()}
    if console is not None:
        doc["console"] = console
    _write_atomic(Path(_state_dir(cache_dir), f"{bucket}.applied.json"), json.dumps(doc))


def _status_path(cache_dir: str) -> Path:
    return Path(cache_dir, "state", "_lifecycle.json")


def load_status(cache_dir: str) -> dict:
    try:
        data = json.loads(_status_path(cache_dir).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_status(cache_dir: str, bucket: str, state: str, detail: str = "", alarm: dict | None = None) -> None:
    """state: ok | error | unsupported | restored | not_restored. An `alarm` (restored |
    not_restored | console_rule) survives later clean checks until the owner
    acknowledges it; a new one is merged with any still open (merge_alarms). A pass that
    RESTORED the app's rules turns an open not_restored alarm of this bucket into a
    restored one (M1) -- its lines and console rule IDs stay."""
    with _status_lock(cache_dir):                # buckets share this file: read-modify-write locked
        data = load_status(cache_dir)
        prev = data.get(bucket) or {}
        prev_alarm = prev.get("alarm")
        if state == "restored" and isinstance(prev_alarm, dict) and prev_alarm.get("kind") == "not_restored":
            prev_alarm = dict(prev_alarm, kind="restored")
        entry = {"state": state, "checked_at": _now_iso(), "detail": detail}
        keep = merge_alarms([prev_alarm, alarm]) if alarm is not None else prev_alarm
        if keep:
            entry["alarm"] = keep
        data[bucket] = entry
        _write_atomic(_status_path(cache_dir), json.dumps(data, indent=2, sort_keys=True))


# --- sync / check: one read-compare-write pass ------------------------------------------

@dataclass
class SyncResult:
    bucket: str
    changed: bool
    lines: list
    state: str = "ok"          # ok | restored (app rules changed outside) | console_rule (alarmed)


def app_rules_differ(a: list[dict], b: list[dict]) -> bool:
    """THE comparison of two rule sets' app-owned rules (console rules ignored) -- used
    by the engine and by the Setup row, so "in step" means the same thing everywhere."""
    return app_rules_of(a) != app_rules_of(b)


def _change_lines(before: list[dict], after: list[dict]) -> list[str]:
    a, b = app_rules_of(before), app_rules_of(after)
    lines = [f"now: {describe(b[k])}" for k in sorted(b) if a.get(k) != b[k]]
    lines += [f"removed: {describe(a[k])}" for k in sorted(set(a) - set(b))]
    return lines


def _context(cfg) -> tuple[str, str, str]:
    env = config_io.read_backup_env(cfg["CONFIG_DIR"])
    return (env.get("S3_BUCKET", "").strip(), (env.get("AWS_REGION") or "us-east-1").strip(),
            cfg["CACHE_DIR"])


def _err_state(e: LifecycleError) -> str:
    return "unsupported" if e.kind == "unsupported" else "error"


TAMPERED = "S3 rules were changed outside backup-engine"


def _reconcile(cfg, bucket: str, *, run, trigger: str):
    with bucket_lock(cfg["CACHE_DIR"], bucket):
        return _reconcile_locked(cfg, bucket, run=run, trigger=trigger)


def _reconcile_locked(cfg, bucket: str, *, run, trigger: str):
    """The pass behind sync() and check(). Tamper detection compares the live app
    rules with what the app last APPLIED; whatever the app writes is always what the
    jobs want NOW (DESIRED), merged with the console rules:
      live == desired (no legacy IDs)  -> in step, nothing written;
      applied exists and live != applied -> changed outside: alarm + Activity, write desired;
      otherwise (job change, a failed earlier write, first apply) -> write desired.
    Live rules that already equal desired are never tampering -- nothing to put back.
    Before anything is recorded, new/changed console rules that can delete or move
    backups are alarmed ("console_rule") -- never touched -- so no pass can silently
    absorb one into the stored fingerprint.
    Returns (state, SyncResult | None, LifecycleError | None); state is what was
    recorded (ok | restored | not_restored | error | unsupported), or "console_rule"
    when this pass raised that alarm and the app's own rules are ok/restored."""
    config_dir = cfg["CONFIG_DIR"]
    base, region, cache = _context(cfg)
    jobs = jobs_io.load(config_dir)                          # under the lock: never a stale list
    want = desired_rules(bucket, base, jobs, load_settings(config_dir))
    detail = "; ".join(notes_for(bucket, base, jobs))
    applied = load_applied(cache, bucket)
    stored_fp = load_console_fingerprint(cache, bucket) if applied is not None else None
    try:
        creds = role_creds(config_dir, region, run=run)
        live = read_rules(bucket, creds, region, run=run)
    except LifecycleError as e:
        set_status(cache, bucket, _err_state(e), e.detail)
        return _err_state(e), None, e

    live_fp = console_fingerprint(live)
    changes = console_changes(stored_fp, live)
    console_alarm = None
    if changes:
        console_alarm = {"kind": "console_rule", "at": _now_iso(),
                         "lines": [line for _, line in changes], "rules": [k for k, _ in changes]}
        runs.record_system(cache, kind="s3-rules", summary=f"A new S3 rule could delete or move backups · {bucket}",
                           lines=[*console_alarm["lines"],
                                  "backup-engine left it in place — check it in the AWS console."],
                           trigger=trigger)

    def status(state, detail_="", alarm=None):
        set_status(cache, bucket, state, detail_, alarm=merge_alarms([console_alarm, alarm]))

    def headline(state):
        return "console_rule" if console_alarm and state in ("ok", "restored") else state

    in_step = not app_rules_differ(live, want) and not any(r.get("ID") in LEGACY_IDS for r in live)
    if in_step:
        status("ok", detail)                     # M2: the alarm is on disk before the fingerprint moves
        if applied is None or app_rules_differ(applied, want) or stored_fp != live_fp:
            save_applied(cache, bucket, want, console=live_fp)
        return headline("ok"), SyncResult(bucket, False, [], headline("ok")), None
    tampered = applied is not None and app_rules_differ(live, applied)
    tamper_lines = _change_lines(applied, live) if tampered else []
    try:
        write_rules(bucket, merge(live, want), creds, region, run=run)
    except LifecycleError as e:
        if not tampered:
            status(_err_state(e), e.detail)
            if applied is not None and stored_fp != live_fp:
                save_applied(cache, bucket, applied, console=live_fp)  # alarm a console rule once
            return _err_state(e), None, e
        status("not_restored", e.detail, {"kind": "not_restored", "at": _now_iso(), "lines": tamper_lines})
        if stored_fp != live_fp:
            save_applied(cache, bucket, applied, console=live_fp)
        runs.record_system(cache, kind="s3-rules", summary=f"{TAMPERED} — NOT restored · {bucket}",
                           lines=[*tamper_lines, e.detail], outcome="failed", error=e.detail,
                           trigger=trigger)
        return "not_restored", None, e
    lines = _change_lines(live, want) + ([detail] if detail else [])
    if tampered:
        status("restored", detail, {"kind": "restored", "at": _now_iso(), "lines": tamper_lines})
        save_applied(cache, bucket, want, console=live_fp)
        also = ["Your latest job settings were applied too."] if app_rules_differ(applied, want) else []
        runs.record_system(cache, kind="s3-rules", summary=f"{TAMPERED} — restored · {bucket}",
                           lines=[*tamper_lines, *also], trigger=trigger)
        return headline("restored"), SyncResult(bucket, True, lines, headline("restored")), None
    status("ok", detail)
    save_applied(cache, bucket, want, console=live_fp)
    runs.record_system(cache, kind="s3-rules", summary=f"S3 rules updated · {bucket}", lines=lines,
                       trigger=trigger)
    return headline("ok"), SyncResult(bucket, True, lines, headline("ok")), None


def sync(cfg, bucket: str, *, run=provision._run_aws, trigger: str = "manual") -> SyncResult:
    """Apply the desired app rules to `bucket` (console rules kept) -- job save/delete,
    setup. Rules changed outside backup-engine since the last apply are alarmed, not
    silently absorbed. Raises LifecycleError on failure (after recording it)."""
    if not managed(cfg["CONFIG_DIR"]):
        raise LifecycleError("not_managed", "S3 rules need the AWS permissions update")
    _, res, err = _reconcile(cfg, bucket, run=run, trigger=trigger)
    if err is not None:
        raise err
    return res


def sync_all(cfg, *, run=provision._run_aws) -> list[SyncResult]:
    base, _, _ = _context(cfg)
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    results, errors = [], []
    for bucket in buckets_for(base, jobs):
        try:
            # jobs are re-read inside each bucket's lock -- a list loaded out here could be
            # stale by then (a job save in between) and would write old settings back.
            results.append(sync(cfg, bucket, run=run))
        except LifecycleError as e:
            if e.kind == "not_managed":
                raise
            errors.append(e)
    if errors and not results:
        raise errors[0]
    return results


# --- tamper check -----------------------------------------------------------------------

def check(cfg, bucket: str, *, run=provision._run_aws, trigger: str = "scheduled") -> str:
    """Before every backup run (trigger "scheduled") and behind Check now ("manual"):
    the same pass as sync() -- drift from what was applied is restored + alarmed, and
    job settings that haven't reached S3 yet are applied. Never raises: anything
    unexpected becomes the "error" state."""
    try:
        if not managed(cfg["CONFIG_DIR"]):
            return "not_managed"
        state, _, _ = _reconcile(cfg, bucket, run=run, trigger=trigger)
        return state
    except Exception as e:                                   # noqa: BLE001 — never raise
        try:
            set_status(cfg["CACHE_DIR"], bucket, "error", f"the check stopped unexpectedly ({type(e).__name__})")
        except Exception:                                    # noqa: BLE001 — state dir unwritable
            pass
        return "error"


def acknowledge(cache_dir: str, bucket: str | None = None, seen: str | None = None) -> None:
    """Clear open alarms. With `seen` (the newest alarm time the owner's page showed), an
    alarm that arrived after that page loaded stays (O2)."""
    with _status_lock(cache_dir):
        data = load_status(cache_dir)
        for b, entry in data.items():
            if bucket not in (None, b) or not isinstance(entry, dict):
                continue
            alarm = entry.get("alarm")
            newest = (alarm.get("latest") or alarm.get("at") or "") if isinstance(alarm, dict) else ""
            if seen is None or newest <= seen:
                entry.pop("alarm", None)
        _write_atomic(_status_path(cache_dir), json.dumps(data, indent=2, sort_keys=True))


# --- CLI ------------------------------------------------------------------------------------

# What the backup log shows for each check state (owner words, never internal names).
_CHECK_WORDS = {
    "ok": "in place",
    "restored": "changed outside backup-engine — restored (see Setup)",
    "not_restored": "changed outside backup-engine — NOT restored (see Setup)",
    "console_rule": "a new S3 rule could delete or move backups (see Setup)",
    "error": "couldn't be checked (see Setup)",
    "unsupported": "this storage doesn't support S3 rules",
    "not_managed": "skipped (needs the AWS permissions update)",
}


def _cfg_from_env() -> dict:
    return {"CONFIG_DIR": os.environ.get("CONFIG_DIR", "/config"),
            "CACHE_DIR": os.environ.get("CACHE_DIR", "/cache")}


def check_all_lines(cfg) -> list[str]:
    """The hourly check (R-B9, crontab `check-all`): every bucket, as a scheduled run. Prints
    nothing when S3 rules aren't managed here (below level 4, no role, not set up). Never raises."""
    try:
        if not managed(cfg["CONFIG_DIR"]):
            return []
        base, _, _ = _context(cfg)
        buckets = buckets_for(base, jobs_io.load(cfg["CONFIG_DIR"]))
    except Exception as e:                                   # noqa: BLE001 — cron must never see a trace
        return [f"S3 rules check: couldn't start ({type(e).__name__})"]
    out = []
    for b in buckets:
        try:
            words = _CHECK_WORDS.get(check(cfg, b, trigger="scheduled"), "couldn't be checked (see Setup)")
        except Exception as e:                               # noqa: BLE001
            words = f"couldn't be checked ({type(e).__name__})"
        out.append(f"S3 rules check · {b}: {words}")
    return out


_TRIGGER = re.compile(r"[a-z][a-z-]{0,19}")


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python3 -m app.engine.lifecycle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync"); s.add_argument("--bucket")
    c = sub.add_parser("check"); c.add_argument("--bucket", required=True)
    c.add_argument("--trigger", default="scheduled")
    sub.add_parser("check-all")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = _cfg_from_env()
    if args.cmd == "check":
        # O4: backup-job.sh passes BE_TRIGGER, so a Run now records its check as manual.
        trigger = args.trigger if _TRIGGER.fullmatch(args.trigger or "") else "scheduled"
        try:
            words = _CHECK_WORDS.get(check(cfg, args.bucket, trigger=trigger), "couldn't be checked (see Setup)")
        except Exception as e:                       # noqa: BLE001 — never block a backup
            words = f"couldn't be checked ({type(e).__name__})"
        print(f"S3 rules check · {args.bucket}: {words}")
        return 0
    if args.cmd == "check-all":
        for line in check_all_lines(cfg):
            print(line)
        return 0
    try:
        results = [sync(cfg, args.bucket)] if args.bucket else sync_all(cfg)
    except LifecycleError as e:
        print(f"S3 rules: {e.kind} {e.detail}".strip(), file=sys.stderr)
        return 1
    for r in results:
        print(f"S3 rules · {r.bucket}: {'updated' if r.changed else 'already in step'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
