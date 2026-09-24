# app/engine/lifecycle.py — S3 lifecycle rules owned by backup-engine (spec
# docs/superpowers/specs/2026-09-23-s3-rules-design.md). One owner per kind of
# history: Plain copy history IS its folder's S3 rule; Snapshot/File history get an
# undo window; housekeeping is bucket-wide. This part is the pure rules model plus
# the owner's settings (config/storage.json); I/O through the bucket-admin role is below.
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
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

# Cheaper tier for old versions (spec §1, Phase C).
TIER_CLASSES = ("GLACIER_IR", "DEEP_ARCHIVE")
TIER_MIN_STORAGE_DAYS = {"GLACIER_IR": 90, "DEEP_ARCHIVE": 180}   # S3's minimum storage duration
MIN_TIER_DAYS = 1
MIN_SIZE_DEFAULT = "all_storage_classes_128K"                     # S3's default for small objects

# Owner words for a storage class in running text -- never the raw AWS constant (fix round 1,
# M4/M9): shared by tier_error (a tier no colder than the folder's own upload class, M3b),
# _keeps_words and destructive_actions/describe (the tier's move, in Activity/tamper lines).
_STORAGE_WORDS = {"STANDARD": "Standard", "STANDARD_IA": "Standard-IA",
                  "GLACIER_IR": "Glacier Instant Retrieval", "GLACIER": "Glacier",
                  "DEEP_ARCHIVE": "Deep Archive"}


def _storage_words(cls: str) -> str:
    return _STORAGE_WORDS.get(cls, cls)


def tier_rank(cls: str) -> int:
    """Coldness rank of a storage class (higher = colder); -1 for an unrecognized value. S3
    never transitions an object to a WARMER class than it's already in -- shared by
    storage_summary.moved (M3a: a class switch never claims a version the old, colder-or-equal
    tier already moved) and with_tier/tier_error (M3b: a tier no colder than the folder's own
    upload class can't move anything)."""
    return jobs_io.STORAGE_CLASSES.index(cls) if cls in jobs_io.STORAGE_CLASSES else -1


@dataclass(frozen=True)
class Folder:
    folder: str                      # "" = the whole (dedicated) bucket
    kind: str                        # "plain" (Plain copy history) | "undo"
    jobs: tuple[str, ...]
    retention: dict | None = None    # plain only: the job's normalized history setting
    note: str = ""                   # why the folder's rule isn't the job's setting (owner words)
    hold: bool = False               # the job's setting can't be read: S3 keeps the rule it has (I1)


def rule_id(folder: str) -> str:
    return APP_PREFIX + (folder or "bucket")


def folders_for(bucket: str, base: str, jobs: list[dict]) -> list[Folder]:
    """The app folders in `bucket`: on the base bucket one media/<job>/ per non-dedicated
    Plain copy / File history job and one shared appdata/ for Snapshot jobs; on a
    dedicated bucket its single job's whole bucket."""
    out: list[Folder] = []
    snapshot_jobs: list[str] = []
    for j in sorted(jobs, key=lambda j: str(j.get("name", ""))):
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
            # break the whole bucket's rules -- nor silently drop the folder's rule (final fix
            # wave I1): the folder HOLDS whatever rule S3 has for it now (resolve_held), until
            # the setting can be read again.
            try:
                retention, note, hold = jobs_io._normalize_retention(j, typ), "", False
            except ValueError:
                retention, hold = None, True
                note = f"{name}: its history setting couldn't be read, so S3 keeps the rule it already has"
            out.append(Folder(f"media/{name}/" if folder is None else folder, "plain", (name,),
                              retention, note, hold))
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
    """Fail-safe read (pages, previews): an unreadable file reads as the defaults. Anything
    that WRITES S3 or storage.json from the settings uses load_settings_strict instead."""
    try:
        data = json.loads(_settings_path(config_dir).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    buckets = data.get("buckets")
    return {"version": 1, "buckets": buckets if isinstance(buckets, dict) else {}}


class SettingsFileError(ValueError):
    """storage.json exists but can't be read (bad JSON, or not the shape the app writes)."""


def load_settings_strict(config_dir: str) -> dict:
    """The owner's settings for a pass that writes (final fix wave I1): a missing file is the
    defaults, but a present-but-unreadable one raises SettingsFileError -- acting on the
    defaults instead would silently undo what the owner confirmed (a suspend, a tier, a longer
    undo window). Checks the shape as far as the app reads it: buckets, each bucket, its
    folders and each folder entry are objects."""
    p = _settings_path(config_dir)
    if not p.exists():
        return {"version": 1, "buckets": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise SettingsFileError(f"storage.json can't be read ({type(e).__name__})")
    buckets = data.get("buckets", {}) if isinstance(data, dict) else None
    ok = isinstance(buckets, dict) and all(
        isinstance(b, dict)
        and isinstance(b.get("folders", {}), dict)
        and all(isinstance(f, dict) for f in b.get("folders", {}).values())
        for b in buckets.values())
    if not ok:
        raise SettingsFileError("storage.json isn't in the shape backup-engine writes")
    return {"version": 1, "buckets": buckets}


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


def settings_lock(config_dir: str):
    """Held around a read-modify-write of storage.json (fcntl, next to it, like the other
    locks) -- so two writers (e.g. seed_undo_days from a sync/check pass, and a later owner
    edit) never interleave. Later tasks' storage.json writers use this same lock."""
    return _flock(Path(config_dir, f".{SETTINGS_FILE}.lock"))


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


def folder_tier(bset: dict, folder: str) -> dict | None:
    """A folder's cheaper tier for old versions (storage.json folders[<folder>].tier), or None."""
    t = (bset["folders"].get(folder) or {}).get("tier")
    if not isinstance(t, dict) or t.get("class") not in TIER_CLASSES:
        return None
    try:
        days = int(t.get("after_days"))
    except (TypeError, ValueError):
        return None
    return {"class": t["class"], "after_days": days} if days >= MIN_TIER_DAYS else None


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


def with_tier(folder: str, rule: dict | None, tier: dict | None, storage_class: str | None = None) -> dict | None:
    """A folder rule plus its cheaper tier for old versions (or a tier-only rule when the folder
    keeps everything). A tier that can't move anything before S3 removes it (after_days >= the
    expiry days) is left off: S3 rejects that rule, and leaving the move off keeps more. A tier
    that isn't colder than the folder's own upload class (fix round 1, M3b -- e.g. a Deep
    Archive Plain copy job) can't move anything either, and is dropped the same way."""
    if not tier or (storage_class and tier_rank(tier["class"]) <= tier_rank(storage_class)):
        return rule
    d, _n = expiry(rule)
    if tier["after_days"] >= d:
        return rule
    t = [{"NoncurrentDays": tier["after_days"], "StorageClass": tier["class"]}]
    return {**rule, "NoncurrentVersionTransitions": t} if rule else _rule(folder, NoncurrentVersionTransitions=t)


def tier_error(tier: dict | None, rule: dict | None, storage_class: str | None = None) -> str | None:
    """Owner words for a tier the editor must refuse (spec error table), else None."""
    if not tier:
        return None
    if tier.get("class") not in TIER_CLASSES:
        return "Pick Glacier Instant Retrieval or Deep Archive."
    days = tier.get("after_days")
    if not isinstance(days, int) or days < MIN_TIER_DAYS:
        return f"Move old versions after {_days(MIN_TIER_DAYS)} or more."
    if storage_class and tier_rank(tier["class"]) <= tier_rank(storage_class):
        return (f"Files here already upload as {_storage_words(storage_class)} — "
                f"{_storage_words(tier['class'])} wouldn't move them anywhere cheaper.")
    d, n = expiry(rule)
    if days >= d:
        if d == 1 and n:                     # newest-N-only: no days limit to move earlier than
            return "Add a days limit to use a cheaper tier."
        return (f"Old versions must move before S3 removes them — move them before {_days(int(d))}, "
                "or keep them longer.")
    return None


def notes_for(bucket: str, base: str, jobs: list[dict]) -> list[str]:
    """Owner-facing notes on folders whose rule isn't the job's own setting."""
    return [f.note for f in folders_for(bucket, base, jobs) if f.note]


def _folder_storage_class(jobs: list[dict], names: tuple[str, ...]) -> str:
    """The warmest storage class among a folder's jobs (default STANDARD) -- the easiest for a
    tier to beat; if a tier isn't even colder than this, it can't help anything the folder holds
    (fix round 1, M3b)."""
    classes = [j.get("storage_class") for j in jobs if j.get("name") in names]
    classes = [c for c in classes if c in jobs_io.STORAGE_CLASSES]
    return min(classes, key=jobs_io.STORAGE_CLASSES.index) if classes else "STANDARD"


def desired_rules(bucket: str, base: str, jobs: list[dict], settings: dict) -> list[dict]:
    """The app rules the jobs and settings want. A HELD folder (its job's setting can't be
    read, I1) has none here -- desired() marks it, and resolve_held() gives it whatever rule
    the baseline (what S3 has) holds for it."""
    bset = bucket_settings(settings, bucket)
    rules = []
    for f in folders_for(bucket, base, jobs):
        if f.hold:
            continue
        r = plain_rule(f.folder, f.retention) if f.kind == "plain" else undo_rule(f.folder, undo_days(bset, f.folder))
        r = with_tier(f.folder, r, folder_tier(bset, f.folder), _folder_storage_class(jobs, f.jobs))
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


def _old_version_words(days, newest) -> str:
    """The "what happens to old versions" phrase for one (days, newest-kept) pair -- shared by
    describe() (a live S3 rule) and _keeps_words() (a change's before/after) so their wording
    never drifts (fix round 1, Minor)."""
    if newest:
        return (f"newest {newest} old versions of each file kept"
                + (f"; older ones removed {_days(days)} after being replaced" if days > 1 else ""))
    return f"old versions removed {_days(days)} after being replaced"


def describe(rule: dict) -> str:
    """One app rule in plain words (Activity lines, flashes, tamper alarms -- so a rule
    edited outside the app also names what it now does to current files)."""
    where = (rule.get("Filter") or {}).get("Prefix", "") or "whole bucket"
    if rule.get("Status") == "Disabled":
        return f"{where}: switched off"
    parts = []
    nce = rule.get("NoncurrentVersionExpiration") or {}
    if "NewerNoncurrentVersions" in nce:
        parts.append(_old_version_words(nce.get("NoncurrentDays", 1), nce["NewerNoncurrentVersions"]))
    elif "NoncurrentDays" in nce:
        parts.append(_old_version_words(nce["NoncurrentDays"], 0))
    if "AbortIncompleteMultipartUpload" in rule:
        parts.append("abandoned uploads cleared after "
                     + _days(rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"]))
    if (rule.get("Expiration") or {}).get("ExpiredObjectDeleteMarker"):
        parts.append("leftover delete markers cleared")
    # current-file actions here are never created by the app -- only present when someone
    # changed the rule outside it; old-version moves ARE the app's own tier (Phase C) too, so
    # both need naming (fix round 1, M4: the old "never created by the app" comment was stale).
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
        cls = t.get("StorageClass") or "another class"           # owner words (fix round 1, M4)
        out.append(f"moves old versions to {_storage_words(cls)} "
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


def console_rules(rules: list[dict]) -> list[tuple[str, dict]]:
    """[(key, rule)] for every rule not made by backup-engine (key = its ID, or "(no ID) …")."""
    return [(k, r) for k, _h, r in _console_entries(rules)]


def rule_prefix(rule: dict) -> str:
    f = _norm(rule).get("Filter") or {}
    if not isinstance(f, dict):
        return ""
    both = f.get("And") if isinstance(f.get("And"), dict) else {}
    return f.get("Prefix") or both.get("Prefix") or ""


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


# --- keeps more / keeps less (spec §2 classify; rulings R-B1, R-B2, R-B3, R-B6) ------------

KEEPS_MORE, KEEPS_LESS = "keeps_more", "keeps_less"
_NONCURRENT = ("NoncurrentVersionExpiration", "NoncurrentVersionTransitions")


@dataclass(frozen=True)
class RuleSet:
    """A bucket's app rules as one comparable whole: {rule ID: rule} in write order, the app
    folders it covers ("" = a dedicated bucket's whole bucket) and the versioning intent
    (Task 16; None = not known / not managed). `held` (final fix wave I1): rule IDs whose
    folder keeps whatever rule the baseline has (its job's setting can't be read) --
    resolve_held() fills them in once the baseline is known."""
    rules: dict
    folders: frozenset
    versioning: str | None = None
    held: frozenset = frozenset()


@dataclass(frozen=True)
class Change:
    rule_id: str               # the app rule's ID ("versioning" for versioning, Task 16)
    folder: str | None         # the app folder; None = bucket-wide (housekeeping, versioning)
    kind: str                  # KEEPS_MORE | KEEPS_LESS
    words: str                 # owner words: "<where>: <before> → <after>"
    before: dict | None        # the rule S3 was last given (None = none)
    after: dict | None         # the rule the jobs and settings want now (None = none)


def _folder_of(rid: str) -> str:
    rest = rid[len(APP_PREFIX):] if rid.startswith(APP_PREFIX) else rid
    return "" if rest == "bucket" else rest


def _nce_pair(nce, default_days: int) -> tuple[int, int]:
    """(NoncurrentDays, NewerNoncurrentVersions) from a NoncurrentVersionExpiration dict, or
    (default_days, 0) when there isn't one. Shared by expiry() (and so _stricter(), which
    reuses it) and seed_undo_days() -- each call site keeps its own meaning for a MISSING
    NoncurrentDays: expiry() defaults to 1 day; seed_undo_days() defaults to 0, so a legacy
    rule with no explicit days is simply skipped there (fix round 1, Minor)."""
    if not isinstance(nce, dict):
        return default_days, 0
    return (int(nce.get("NoncurrentDays", default_days) or default_days),
            int(nce.get("NewerNoncurrentVersions", 0) or 0))


def expiry(rule) -> tuple[float, int]:
    """(days, newest kept) of a folder rule's old-version expiry: an old version goes once it
    was replaced `days` ago AND at least `newest` newer old versions exist. (inf, 0) = old
    versions are never removed."""
    r = rule or {}
    nce = r.get("NoncurrentVersionExpiration")
    if r.get("Status") == "Disabled" or not isinstance(nce, dict):
        return math.inf, 0
    return _nce_pair(nce, 1)


def tier_of(rule) -> tuple[str, int, int] | None:
    """(storage class, days after being replaced, newest kept out of the move) of a rule's
    earliest old-version move that can actually fire, or None. A transition at or after the
    rule's own UNCONDITIONAL expiry (no newest-N exemption on the expiry) never fires -- S3
    removes the version first -- and doesn't count (fix round 1, M5: a first-apply baseline
    merged from an overlapping legacy rule can carry exactly this kind of dead transition)."""
    r = rule or {}
    if r.get("Status") == "Disabled":
        return None
    ts = _as_list(r.get("NoncurrentVersionTransitions")) + _as_list(r.get("NoncurrentVersionTransition"))
    if not ts:
        return None
    d, n = expiry(r)
    live = [t for t in ts if not (n == 0 and int(t.get("NoncurrentDays", 0) or 0) >= d)]
    if not live:
        return None
    t = min(live, key=lambda x: int(x.get("NoncurrentDays", 0) or 0))
    return (str(t.get("StorageClass", "")), int(t.get("NoncurrentDays", 0) or 0),
            int(t.get("NewerNoncurrentVersions", 0) or 0))


def tier_keeps_less(before, after) -> bool:
    """A tier added, moved earlier, switched to another class, or newly moving versions its own
    newest-N used to protect, keeps less (spec §2; fix round 1, M5)."""
    bt, at = tier_of(before), tier_of(after)
    if at is None:
        return False
    return bt is None or at[0] != bt[0] or at[1] < bt[1] or at[2] < bt[2]


def keeps_less(before, after) -> bool:
    """True when `after` can remove an old version `before` keeps (it removes sooner, or
    protects fewer newest versions), or moves old versions to a cheaper tier sooner."""
    bd, bn = expiry(before)
    ad, an = expiry(after)
    return (ad != math.inf and (ad < bd or an < bn)) or tier_keeps_less(before, after)


def _keeps_words(rule) -> str:
    """The "what happens to old versions" phrase, including any cheaper-tier move -- shares
    _old_version_words with describe() (fix round 1, Minor) so the non-tier wording never
    drifts between the two; only the tier suffix is new here."""
    d, n = expiry(rule)
    words = "every old version kept" if d == math.inf else _old_version_words(d, n)
    t = tier_of(rule)
    if t:
        words += f"; moved to {_storage_words(t[0])} {_days(t[1])} after being replaced"
    return words


def _change_words(rid: str, before, after) -> str:
    if rid == HOUSEKEEPING_ID:
        def body(r):
            return describe(r).split(": ", 1)[-1] if r else "no clean-up rule"
        return f"whole bucket: {body(before)} → {body(after)}"
    return f"{_folder_of(rid) or 'whole bucket'}: {_keeps_words(before)} → {_keeps_words(after)}"


def desired(bucket: str, base: str, jobs: list[dict], settings: dict, *, base_versioned: bool = True) -> RuleSet:
    """desired_rules() as a RuleSet, with the folders the jobs have now, the versioning
    intent (Task 16) and the held folders' rule IDs (I1)."""
    folders = folders_for(bucket, base, jobs)
    return RuleSet({r["ID"]: r for r in desired_rules(bucket, base, jobs, settings)},
                   frozenset(f.folder for f in folders),
                   versioning_intent(bucket, base, jobs, settings, base_versioned=base_versioned),
                   frozenset(rule_id(f.folder) for f in folders if f.hold))


def resolve_held(before: RuleSet | None, want: RuleSet) -> RuleSet:
    """`want` with every held folder (I1) given exactly the rule `before` has for it -- or
    none, when before has none. Applied wherever want meets a baseline (the pass, previews,
    outstanding, the screens), so a folder whose job's setting can't be read is never a
    change: S3 keeps what it has there, gated or confirmed."""
    if not want.held:
        return want
    rules = {rid: r for rid, r in want.rules.items() if rid != HOUSEKEEPING_ID}
    for rid in want.held:
        if before is not None and rid in before.rules:
            rules[rid] = before.rules[rid]
    ordered = {rid: rules[rid] for rid in sorted(rules)}
    if HOUSEKEEPING_ID in want.rules:
        ordered[HOUSEKEEPING_ID] = want.rules[HOUSEKEEPING_ID]
    return RuleSet(ordered, want.folders, want.versioning, want.held)


def want_for(cfg, bucket: str, jobs=None, settings=None) -> RuleSet:
    """What jobs.json + storage.json (+ BASE_BUCKET_VERSIONED) want for `bucket` now -- files
    only, never AWS."""
    base, _, _ = _context(cfg)
    config_dir = cfg["CONFIG_DIR"]
    return desired(bucket, base, jobs_io.load(config_dir) if jobs is None else jobs,
                   load_settings(config_dir) if settings is None else settings,
                   base_versioned=config_io.base_bucket_versioned(config_dir))


def applied_view(cfg, bucket: str, jobs=None, settings=None) -> tuple[RuleSet | None, RuleSet]:
    """(baseline, want) from files only -- never AWS: what the app last applied (None before
    the first apply), its known folders widened to every folder whose job has run (I2), and
    what jobs.json + storage.json want now, held folders resolved against that baseline (I1).
    The one pairing previews, outstanding() and the screens compare, so they always agree
    with what the pass itself holds."""
    base, _, cache = _context(cfg)
    jobs = jobs_io.load(cfg["CONFIG_DIR"]) if jobs is None else jobs
    want = want_for(cfg, bucket, jobs, settings)
    before = known_baseline(baseline_from_applied(_applied_doc(cache, bucket), want), cache, bucket, base, jobs)
    return before, resolve_held(before, want)


def baseline_from_applied(doc, want: RuleSet, live_versioning: str | None = None) -> RuleSet | None:
    """The baseline once the app has applied (R-B1): exactly what it last wrote. A record
    from before `folders` existed counts every current folder as known (the careful side:
    nothing is mistaken for a new job); one from before `versioning` existed takes the
    versioning S3 reports (when the caller read it, Task 16)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return None
    known = doc.get("folders")
    v = doc.get("versioning")
    return RuleSet(app_rules_of(doc["rules"]),
                   frozenset(known) if isinstance(known, list) else want.folders,
                   v if v in VERSIONING_STATES else live_versioning)


def _legacy_targets(rid: str, folders) -> list[str]:
    if rid == "backstop-appdata":
        return ["appdata/"]
    if rid == "backstop-media":
        return sorted(f for f in folders if f.startswith("media/"))
    if rid == "backup-engine":
        return [""]
    return [_folder_of(rid)]


def _stricter(a: dict | None, b: dict) -> dict:
    """Two rules on one folder as S3 actually applies them: an old version goes if EITHER
    rule would remove it. When one rule's removals are a superset of the other's (its days
    and newest-kept are both <=), that rule alone is exact for the pair. Otherwise the two
    are incomparable, and no per-dimension combination (e.g. the min of each day/newest) is
    safe -- it can UNDER-state what's really being removed today and so HIDE a real
    keeps-less change (fix round 1, Minor). Keep one of the two rules unchanged instead:
    that always UNDER-approximates the true combined deletion, so it never hides a shrink --
    deterministically `b` (the later, more specific rule, in practice the app's own)."""
    if a is None:
        return b
    da, na = expiry(a)
    db, nb = expiry(b)
    chosen = a if da <= db and na <= nb else b
    out = dict(chosen)
    if "NoncurrentVersionTransitions" in b and "NoncurrentVersionTransitions" not in out:
        out["NoncurrentVersionTransitions"] = b["NoncurrentVersionTransitions"]
    return out


def baseline_from_live(live_rules: list[dict], want: RuleSet, known: frozenset | None = None,
                       versioning: str | None = None) -> RuleSet:
    """The baseline of a FIRST apply (R-B1): the live app and legacy rules mapped to folders --
    backstop-appdata -> appdata/, backstop-media -> every media/<job>/ folder, a dedicated
    bucket's `backup-engine` -> the whole bucket, backup-engine:<folder> -> that folder. Only
    their old-version actions count; switched-off rules count as none. `known` is which
    folders are treated as already having data in S3 (R-B2'; default -- careful side -- is
    every current folder, `want.folders`); anything else is a new job's folder, never a
    keeps-less change no matter what the live/legacy rules say. `versioning` (Task 16) is
    what was just read live -- the baseline's own versioning."""
    by_folder: dict[str, dict] = {}
    housekeeping = None
    for r in live_rules:
        if not is_app_rule(r) or r.get("Status") == "Disabled":
            continue
        n = _norm(r)
        if n.get("ID") == HOUSEKEEPING_ID:
            housekeeping = n
            continue
        actions = {k: n[k] for k in _NONCURRENT if k in n}
        if not actions:
            continue
        for folder in _legacy_targets(n["ID"], want.folders):
            by_folder[folder] = _stricter(by_folder.get(folder), _rule(folder, **actions))
    rules = {rule_id(f): by_folder[f] for f in sorted(by_folder)}
    if housekeeping is not None:
        rules[HOUSEKEEPING_ID] = housekeeping
    return RuleSet(rules, want.folders if known is None else known, versioning)


def _has_run(cache_dir: str, job: str, exclude_run: str | None = None) -> bool:
    """R-B2': CACHE_DIR/state/<job>.json (written by backup-job.sh at the end of every run,
    success or failure -- app/gui/runner.py::read_state) OR a record in
    CACHE_DIR/state/<job>.runs.jsonl (app/engine/runs.py, appended at the START of a run) -- so
    a first run still in progress, or one that was only ever paused, counts too (fix round 1,
    Minor). `exclude_run` (final fix wave I2): the run whose pre-backup check this is --
    backup-job.sh appends its start record BEFORE the check, yet it hasn't uploaded anything
    yet, so it never makes its own job's folder known. A line that can't be read counts as a
    run (the careful side)."""
    if Path(cache_dir, "state", f"{job}.json").exists():
        return True
    try:
        lines = Path(cache_dir, "state", f"{job}.runs.jsonl").read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return True
        if not isinstance(rec, dict) or exclude_run is None or rec.get("id") != exclude_run:
            return True
    return False


def _known_folders(cache_dir: str, bucket: str, base: str, jobs: list[dict],
                   exclude_run: str | None = None) -> frozenset:
    """R-B2': a folder is known when at least one of its jobs has run before this one --
    because only then can it have data in S3. A folder whose jobs never ran is a new job's
    folder: always keeps-more. Final fix wave I2: this holds on EVERY pass (not only a bucket's
    first apply), unioned with the folders the applied record already knows (R-B2's "never
    forget") -- see known_baseline()."""
    return frozenset(f.folder for f in folders_for(bucket, base, jobs)
                     if any(_has_run(cache_dir, j, exclude_run) for j in f.jobs))


def known_baseline(before: RuleSet | None, cache_dir: str, bucket: str, base: str, jobs: list[dict],
                   exclude_run: str | None = None) -> RuleSet | None:
    """An applied baseline whose known folders also include every folder whose job has run
    before this run (final fix wave I2) -- a job that ran while its own rule never reached S3
    (a failed save-time sync) has data there, so a first or shortened rule for it keeps less
    and waits, exactly as on a bucket's first apply."""
    if before is None:
        return None
    known = _known_folders(cache_dir, bucket, base, jobs, exclude_run)
    return before if known <= before.folders else RuleSet(before.rules, before.folders | known,
                                                          before.versioning, before.held)


def classify(before: RuleSet, after: RuleSet) -> list[Change]:
    """Every rule that differs, marked keeps-more or keeps-less (spec §2). Keeps-less: a known
    folder whose old versions would go sooner or with fewer newest ones protected (incl. an
    expiry added where there was none). Keeps-more: everything else -- longer, removed
    limits, a new job's folder (R-B2), a deleted job's rule going away, housekeeping."""
    out = []
    for rid in dict.fromkeys([*before.rules, *after.rules]):
        b, a = before.rules.get(rid), after.rules.get(rid)
        if (_norm(b) if b else None) == (_norm(a) if a else None):
            continue
        if rid == HOUSEKEEPING_ID:
            folder, kind = None, KEEPS_MORE                   # never removes a backup
        else:
            folder = _folder_of(rid)
            known = folder in before.folders
            kind = KEEPS_LESS if known and keeps_less(b, a) else KEEPS_MORE
        out.append(Change(rid, folder, kind, _change_words(rid, b, a), b, a))
    bv, av = before.versioning, after.versioning
    if bv is not None and av is not None and not versioning_matches(bv, av):
        out.append(Change("versioning", None, KEEPS_LESS if av == "suspended" else KEEPS_MORE,
                          f"versioning: {_VER_WORDS.get(bv, bv)} → {_VER_WORDS.get(av, av)}", None, None))
    return out


def gate(before: RuleSet, after: RuleSet) -> RuleSet:
    """R-B1: what may be written without the owner's confirmation -- `after`, except that a
    folder whose change keeps less keeps `before`'s rule (or its absence), and a suspend
    keeps `before`'s versioning."""
    held = {c.rule_id for c in classify(before, after) if c.kind == KEEPS_LESS}
    rules = {}
    for rid, r in after.rules.items():
        if rid not in held:
            rules[rid] = r
        elif rid in before.rules:
            rules[rid] = before.rules[rid]
    versioning = before.versioning if "versioning" in held else after.versioning
    return RuleSet(rules, after.folders, versioning)


def outstanding(cfg, bucket: str) -> tuple[bool, list[Change]]:
    """From files only (no AWS, R-B3): (S3 was last given less than the gate allows -- a
    keeps-more change not applied yet, e.g. a failed job-save write; the keeps-less changes
    waiting for the owner's confirmation). Nothing applied yet -> (False, [])."""
    before, want = applied_view(cfg, bucket)
    if before is None:
        return False, []
    waiting = [c for c in classify(before, want) if c.kind == KEEPS_LESS]
    target = gate(before, want)
    not_reached = (app_rules_differ(list(before.rules.values()), list(target.rules.values()))
                   or (before.versioning is not None and not versioning_matches(before.versioning, target.versioning)))
    return not_reached, waiting


def seed_undo_days(config_dir: str, bucket: str, live_rules: list[dict], folders: list[Folder]) -> bool:
    """R-B6 (#7): a first apply over a legacy backstop LONGER than the default undo window
    keeps that window -- its days become storage.json undo_days for the undo folders it
    covered, written once and only where the folder has no undo_days yet (the owner's own
    choice wins). Returns True when storage.json changed."""
    undo = {f.folder for f in folders if f.kind == "undo"}
    found: dict[str, int] = {}
    for r in live_rules:
        rid = r.get("ID")
        nce = r.get("NoncurrentVersionExpiration")
        if rid not in LEGACY_IDS or r.get("Status") == "Disabled" or not isinstance(nce, dict) \
                or "NewerNoncurrentVersions" in nce:
            continue
        days, _ = _nce_pair(nce, 0)
        if days <= DEFAULT_UNDO_DAYS:
            continue
        for f in _legacy_targets(rid, undo):
            if f in undo:
                found[f] = min(found.get(f, days), days)
    if not found:
        return False
    with settings_lock(config_dir):                      # read-modify-write, never interleaved
        settings = load_settings_strict(config_dir)       # I1: never write over an unreadable file
        b = settings["buckets"].get(bucket)
        b = dict(b) if isinstance(b, dict) else {}
        fs = dict(b["folders"]) if isinstance(b.get("folders"), dict) else {}
        changed = False
        for folder, days in sorted(found.items()):
            entry = dict(fs[folder]) if isinstance(fs.get(folder), dict) else {}
            if "undo_days" in entry:
                continue
            entry["undo_days"] = days
            fs[folder] = entry
            changed = True
        if changed:
            b["folders"] = fs
            settings["buckets"][bucket] = b
            save_settings(config_dir, settings)
    return changed


def folder_of_job(base: str, jobs: list[dict], name: str) -> tuple[str, str] | None:
    """(bucket, app folder) a job's data lives in, or None for an unknown job."""
    job = next((j for j in jobs if j.get("name") == name), None)
    if job is None:
        return None
    bucket = job["bucket"] if job.get("dedicated") and job.get("bucket") else base
    f = next((f for f in folders_for(bucket, base, jobs) if name in f.jobs), None)
    return (bucket, f.folder) if f else None


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


def read_lifecycle(bucket: str, creds: dict, region: str, *, run=provision._run_aws) -> tuple[list[dict], str | None]:
    """The bucket's lifecycle rules and its TransitionDefaultMinimumObjectSize (None when S3 or
    this aws CLI doesn't report one). NoSuchLifecycleConfiguration -> no rules."""
    cp = _call(run, creds, region, ["s3api", "get-bucket-lifecycle-configuration",
                                    "--bucket", bucket, "--output", "json"])
    if cp.returncode == 0:
        try:
            data = json.loads(cp.stdout or "{}")
        except ValueError:
            raise LifecycleError("aws", "unreadable lifecycle response")
        data = data if isinstance(data, dict) else {}
        rules, size = data.get("Rules"), data.get("TransitionDefaultMinimumObjectSize")
        return (list(rules) if isinstance(rules, list) else []), (size if isinstance(size, str) else None)
    if "NoSuchLifecycleConfiguration" in (cp.stderr or ""):
        return [], None
    raise _fail(creds, cp.stderr)


MIN_SIZE_FLAG = "--transition-default-minimum-object-size"
# Probed once per `run` (production has exactly one: provision._run_aws) -- [(run, supported)],
# a list rather than a dict keyed by id(run) so a garbage-collected run object's id can never be
# reused and silently hand back a stale answer (fix round 1, #9 controller ruling).
_MIN_SIZE_PROBED: list = []


def _min_size_supported(run, creds, region) -> bool:
    """Whether this process's aws CLI understands --transition-default-minimum-object-size
    (added to botocore ~September 2024; Alpine 3.20's pinned aws-cli, 2.15.57, predates it).
    Probed with a local `help` call -- no AWS reached, no bucket needed -- and cached; a failed
    probe counts as unsupported (the safe direction: never send the flag, S3's own default
    already applies)."""
    for cached_run, supported in _MIN_SIZE_PROBED:
        if cached_run is run:
            return supported
    try:
        cp = _call(run, creds, region, ["s3api", "put-bucket-lifecycle-configuration", "help"])
        supported = cp.returncode == 0 and MIN_SIZE_FLAG in (cp.stdout or "")
    except Exception:                        # noqa: BLE001 -- a probe must never crash a sync
        supported = False
    _MIN_SIZE_PROBED.append((run, supported))
    return supported


def write_rules(bucket: str, rules: list[dict], creds: dict, region: str, *, run=provision._run_aws,
                min_size: str | None = None) -> None:
    """Put the whole configuration (S3 replaces it). A small-object setting the owner chose
    (anything but S3's default) is passed back so a put never resets it (#9) -- but only when
    this process's aws CLI actually understands the flag; when it doesn't, the flag is simply
    never sent (S3 then applies its own default, the benign direction -- `_reconcile_locked`
    notes it via its "small files" detail, and records the reading for Task 18b's copy). The
    app never SETS a min-size itself."""
    args = ["s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket,
            "--lifecycle-configuration", json.dumps({"Rules": rules})]
    if min_size and min_size != MIN_SIZE_DEFAULT and _min_size_supported(run, creds, region):
        args += [MIN_SIZE_FLAG, min_size]
    cp = _call(run, creds, region, args)
    if cp.returncode != 0:
        raise _fail(creds, cp.stderr)


# --- versioning (Task 16, R-B7): owner intent, read/write through the role -----------------

_VERSIONING = {"Enabled": "on", "Suspended": "suspended"}
_VER_WORDS = {"on": "on", "suspended": "suspended", "never": "never turned on"}
VERSIONING_STATES = ("on", "suspended")            # the only states the app itself ever stores/writes


def read_versioning(bucket: str, creds: dict, region: str, *, run=provision._run_aws) -> str:
    """"on" | "suspended" | "never" -- a bucket that was never versioned reports no Status."""
    cp = _call(run, creds, region, ["s3api", "get-bucket-versioning", "--bucket", bucket, "--output", "json"])
    if cp.returncode != 0:
        raise _fail(creds, cp.stderr)
    try:
        status = (json.loads(cp.stdout or "{}") or {}).get("Status")
    except (ValueError, AttributeError):
        raise LifecycleError("aws", "unreadable versioning response")
    return _VERSIONING.get(status, "never")


def write_versioning(bucket: str, state: str, creds: dict, region: str, *, run=provision._run_aws) -> None:
    # fix round 1, Minor: never silently send Suspended for a bad `state` -- a caller bug
    # must surface as a bug, not as an unintended suspend.
    if state not in VERSIONING_STATES:
        raise ValueError(f"write_versioning: state must be 'on' or 'suspended', not {state!r}")
    cp = _call(run, creds, region, ["s3api", "put-bucket-versioning", "--bucket", bucket,
                                    "--versioning-configuration",
                                    f"Status={'Enabled' if state == 'on' else 'Suspended'}"])
    if cp.returncode != 0:
        raise _fail(creds, cp.stderr)


def versioning_intent(bucket: str, base: str, jobs: list[dict], settings: dict, *,
                      base_versioned: bool = True) -> str | None:
    """storage.json's `versioning`, else what the install already chose: a dedicated bucket's
    job `bucket_versioned`, the base bucket's BASE_BUCKET_VERSIONED (default on) -- so no
    install is flipped against a choice it already made. None (final fix wave I3): a dedicated
    bucket no job uses any more -- its versioning is left exactly as it is (S3 can never turn it
    back off, so the app never writes versioning for a bucket nothing backs up to)."""
    job = None
    if bucket != base:
        job = next((j for j in jobs if j.get("dedicated") and j.get("bucket") == bucket), None)
        if job is None:
            return None
    raw = ((settings or {}).get("buckets") or {}).get(bucket)
    v = raw.get("versioning") if isinstance(raw, dict) else None
    if v in VERSIONING_STATES:
        return v
    if job is not None:
        return "on" if job.get("bucket_versioned", True) else "suspended"
    return "on" if base_versioned else "suspended"


def versioning_matches(live: str | None, want: str | None) -> bool:
    """S3 can't return to never-versioned, so "never" is in step with an intent of suspended."""
    return want is None or live == want or (live == "never" and want == "suspended")


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


def save_live(cache_dir: str, bucket: str, rules: list[dict], versioning: str | None = None,
             min_size: str | None = None) -> None:
    """What a pass last READ from S3 -- console rules included -- so the S3 rules screen shows
    them without an AWS call on GET. `min_size` (#9, fix round 1 M8) is the bucket's
    TransitionDefaultMinimumObjectSize as read (None when S3 or this aws CLI didn't report one),
    so Task 18b's copy can say the right thing without another AWS call."""
    doc = {"rules": rules, "read_at": _now_iso()}
    if versioning is not None:
        doc["versioning"] = versioning
    if min_size is not None:
        doc["min_size"] = min_size
    _write_atomic(Path(_state_dir(cache_dir), f"{bucket}.live.json"), json.dumps(doc))


def load_live(cache_dir: str, bucket: str) -> dict | None:
    try:
        data = json.loads(Path(_state_dir(cache_dir), f"{bucket}.live.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("rules"), list) else None


def load_applied_doc(cache_dir: str, bucket: str) -> dict | None:
    """The whole applied record: {"rules", "applied_at", "console"?, "folders"?} (GUI, previews)."""
    return _applied_doc(cache_dir, bucket)


def save_applied(cache_dir: str, bucket: str, rules: list[dict], console: dict | None = None,
                 folders: list[str] | None = None, versioning: str | None = None) -> None:
    doc = {"rules": rules, "applied_at": _now_iso()}
    if console is not None:
        doc["console"] = console
    if folders is not None:
        doc["folders"] = sorted(folders)             # the app folders known at this apply (R-B2)
    if versioning is not None:
        doc["versioning"] = versioning               # the versioning the app applied (R-B7)
    _write_atomic(Path(_state_dir(cache_dir), f"{bucket}.applied.json"), json.dumps(doc))
    if console is not None:                          # a recorded fingerprint supersedes the O1 marker
        try:
            _o1_noted_path(cache_dir, bucket).unlink()
        except OSError:
            pass


def seed_new_bucket(cache_dir: str, bucket: str) -> None:
    """A bucket backup-engine just created has nothing applied and knows no folders, so its
    first S3 rules apply treats every folder as a new job's (keeps more -> applied, R-B2).
    Never overwrites an existing record."""
    if _applied_doc(cache_dir, bucket) is None:
        save_applied(cache_dir, bucket, [], folders=[])


_INFLIGHT_TTL_S = 6 * 3600          # fix round 3, item 1: an unresolved journal older than
                                     # this is dropped without adoption, judged normally


def _inflight_path(cache_dir: str, bucket: str) -> Path:
    return Path(_state_dir(cache_dir), f"{bucket}.inflight.json")


def _write_in_flight(cache_dir: str, bucket: str, rules: list[dict], folders: list[str],
                     versioning: str | None) -> None:
    """A write journal, in its OWN file (fix round 3, item 1 -- never applied.json, which
    only save_applied ever writes): the TARGET this pass is about to write to S3, recorded
    immediately before the puts. Covers a kill/timeout landing between a put actually
    reaching S3 and save_applied recording it -- including a CONFIRMED keeps-less apply,
    which the per-half tamper rule alone can't fix: once live has moved past the ordinary
    GATED baseline, the very next (ungated-unaware, journal-unaware) pass would judge it
    against that stale baseline and revert it. `_resolve_in_flight` reads this back, and
    always deletes it, at the start of the next pass."""
    doc = {"rules": rules, "folders": sorted(folders), "versioning": versioning, "written_at": _now_iso()}
    _write_atomic(_inflight_path(cache_dir, bucket), json.dumps(doc))


def _load_in_flight(cache_dir: str, bucket: str) -> dict | None:
    try:
        data = json.loads(_inflight_path(cache_dir, bucket).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    written = _parse_iso(data.get("written_at"))
    if written is None or (datetime.now(timezone.utc) - written).total_seconds() > _INFLIGHT_TTL_S:
        return None                   # fix round 3, item 1: stale -- never adopted
    return data


def _delete_in_flight(cache_dir: str, bucket: str) -> None:
    try:
        _inflight_path(cache_dir, bucket).unlink()
    except OSError:
        pass


def _resolve_in_flight(cache_dir: str, bucket: str, doc, live: list[dict], live_ver: str | None,
                       ver_managed: bool) -> dict | None:
    """The other half of the write journal (fix round 3, item 1), read at the very start of a
    pass (after live is known): a journal from a pass that never got to record what it wrote
    is adopted, PER HALF, wherever live now matches it EXACTLY -- as if save_applied had
    already run for that half; a half that doesn't match is simply dropped (no adoption, no
    alarm) and this pass judges it normally, the same as any other pending change. The
    journal file is always deleted once resolved, whether anything adopted or not, so a stale
    one never lingers -- but only AFTER an adoption is saved (fix round 4, item 3): a kill
    between the two then leaves the journal for the next pass to adopt again (idempotent),
    never a lost adoption that the next check would revert with a false alarm. Never invents
    or rewrites an applied record on its own: applied.json is
    only ever written by `save_applied`, called here only when something actually adopts AND
    there's a valid rules list to record (a bucket with no applied record and no rules match
    stays exactly as it was -- still `None`, still judged as a first apply next time, never a
    bare `{"rules": []}` stub that would make the next pass see an empty, folder-less
    baseline). The OLD console fingerprint is always carried through unchanged (never the
    fresh one from this pass's own `live` read) -- a console rule added between the killed
    pass and this one must still be compared and alarmed, never silently absorbed into the
    fingerprint just because an unrelated app-rule half also happened to adopt. Returns the
    applied doc as it now stands (`doc` itself, unchanged, when there was nothing to resolve
    or nothing adopted)."""
    inf = _load_in_flight(cache_dir, bucket)
    if inf is None:                                  # none, unreadable or past its TTL: drop it
        _delete_in_flight(cache_dir, bucket)
        return doc
    applied = doc.get("rules") if isinstance(doc, dict) and isinstance(doc.get("rules"), list) else None
    applied_ver = doc.get("versioning") if isinstance(doc, dict) and doc.get("versioning") in VERSIONING_STATES \
        else None
    folders = doc.get("folders") if isinstance(doc, dict) and isinstance(doc.get("folders"), list) else None
    console = doc.get("console") if isinstance(doc, dict) and isinstance(doc.get("console"), dict) else None
    adopted = False
    if not app_rules_differ(live, inf.get("rules") or []):
        applied, folders, adopted = inf.get("rules") or [], sorted(inf.get("folders") or []), True
    if ver_managed and versioning_matches(live_ver, inf.get("versioning")):
        applied_ver, adopted = inf.get("versioning"), True
    if not adopted or applied is None:              # nothing changed, or no rules baseline yet
        _delete_in_flight(cache_dir, bucket)
        return doc
    save_applied(cache_dir, bucket, applied, console=console, folders=folders, versioning=applied_ver)
    _delete_in_flight(cache_dir, bucket)            # item 3: only once the adoption is on disk
    return _applied_doc(cache_dir, bucket)


def _o1_noted_path(cache_dir: str, bucket: str) -> Path:
    return Path(_state_dir(cache_dir), f"{bucket}.o1-noted.json")


def _o1_noted(cache_dir: str, bucket: str) -> dict | None:
    """Fix round 4, item 5: the console fingerprint whose O1 note ("Kept as it is -- a rule
    added in the AWS console") already reached Activity while no fingerprint could be recorded
    in applied.json -- a TRUE first apply whose rules put keeps failing, which must never write
    an applied record (the next pass has to stay a first apply). None = nothing noted yet."""
    try:
        data = json.loads(_o1_noted_path(cache_dir, bucket).read_text())
    except (OSError, ValueError):
        return None
    fp = data.get("console") if isinstance(data, dict) else None
    return fp if isinstance(fp, dict) else None


def _mark_o1_noted(cache_dir: str, bucket: str, fp: dict) -> None:
    _write_atomic(_o1_noted_path(cache_dir, bucket), json.dumps({"console": fp, "noted_at": _now_iso()}))


def _status_path(cache_dir: str) -> Path:
    return Path(cache_dir, "state", "_lifecycle.json")


def load_status(cache_dir: str) -> dict:
    try:
        data = json.loads(_status_path(cache_dir).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alarm_mentions_versioning(alarm: dict) -> bool:
    return any(isinstance(line, str) and line.startswith("versioning:") for line in (alarm.get("lines") or []))


_FROM_DISK = object()          # set_status's default: read the prior alarm from the status file


def set_status(cache_dir: str, bucket: str, state: str, detail: str = "", alarm: dict | None = None, *,
               ver_managed: bool = True, prior_alarm=_FROM_DISK) -> None:
    """state: ok | error | unsupported | restored | not_restored. An `alarm` (restored |
    not_restored | console_rule) survives later clean checks until the owner
    acknowledges it; a new one is merged with any still open (merge_alarms). A pass that
    RESTORED the app's rules turns an open not_restored alarm of this bucket into a
    restored one (M1) -- its lines and console rule IDs stay. Fix round 2: a later pass that
    reaches a plain "ok" (fully settled -- nothing tampered, nothing pending, not merely one
    half of a still-failing multi-half write) does the same -- S3 matching everything the app
    currently wants means whatever the alarm was about no longer applies, so it's never left
    stuck at "not_restored" forever just because the pass that actually fixed it happened to
    report "ok" rather than "restored". Fix round 3, item 3: an "ok" pass never heals an open
    alarm that's about VERSIONING when this pass couldn't even check versioning
    (`ver_managed=False`, an unsupported storage) -- "ok" here only means the rules half is
    settled, not that the still-suspended-outside versioning is back the way it was. Fix round
    4, item 6: the same holds for a "restored" pass -- restoring the rules half says nothing
    about versioning it never read. An alarm about rules only still heals on either.

    `prior_alarm` (fix round 3, item 6): by default this call's own "before" state is read
    straight off disk, as always -- but a caller that ALREADY wrote its own provisional alarm
    earlier in the SAME pass (the "status first" pre-write, so a kill right after a
    restoring put can't lose it) must pass the alarm that was truly open BEFORE that
    provisional write, not the provisional write itself: merging against your own just-written
    guess would let a merely-provisional "not_restored" (severity 3) permanently outrank and
    erase a genuinely open, lower-severity alarm (e.g. "console_rule", severity 2) that was
    there first, even once the true outcome turns out milder than the guess."""
    with _status_lock(cache_dir):                # buckets share this file: read-modify-write locked
        data = load_status(cache_dir)
        prev = data.get(bucket) or {}
        prev_alarm = prev.get("alarm") if prior_alarm is _FROM_DISK else prior_alarm
        heals = (isinstance(prev_alarm, dict) and prev_alarm.get("kind") == "not_restored"
                and state in ("ok", "restored")
                and (ver_managed or not _alarm_mentions_versioning(prev_alarm)))
        if heals:
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
    waiting: list = field(default_factory=list)   # Changes held for the owner's confirmation (R-B1)


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


# final fix wave I1: the owner-words detail of a pass that stopped on an unreadable config file.
CONFIG_JOBS_UNREADABLE = "the jobs file couldn't be read — S3 rules left as they are"
CONFIG_SETTINGS_UNREADABLE = "the S3 rules settings couldn't be read (storage.json) — S3 rules left as they are"
CONFIG_ERRORS = (CONFIG_JOBS_UNREADABLE, CONFIG_SETTINGS_UNREADABLE)


def _config_error(cache: str, bucket: str, detail: str, stale: bool):
    """The pass stops before any AWS call and writes nothing (I1); an open alarm stays."""
    set_status(cache, bucket, "error", detail)
    return "error", None, LifecycleError("config", detail), stale


TAMPERED = "S3 rules were changed outside backup-engine"


def _reconcile(cfg, bucket: str, *, run, trigger: str, run_id: str | None = None):
    with bucket_lock(cfg["CACHE_DIR"], bucket):
        return _reconcile_locked(cfg, bucket, run=run, trigger=trigger, run_id=run_id)


def _reconcile_locked(cfg, bucket: str, *, run, trigger: str, gated: bool = True, expect: str | None = None,
                      run_id: str | None = None):
    """The pass behind sync(), check() and -- with gated=False -- apply_confirmed().

    What it writes is TARGET: what jobs.json + storage.json want (DESIRED: app rules and the
    versioning intent, Task 16) gated against the BASELINE -- the rules and versioning the app
    last applied, or on a first apply the live app/legacy rules mapped to folders plus the live
    versioning (R-B1). A folder whose change keeps less keeps the baseline's rule (or its
    absence), and a suspend keeps the baseline's versioning, until the owner confirms it
    through a preview; a new job's folder always applies (R-B2). Tamper detection compares
    live with what was APPLIED -- app rules and versioning (R-B7):
      live == target (no legacy IDs)       -> in step, nothing written;
      applied exists and live != applied  -> changed outside: alarm + Activity, write target;
      otherwise (job change, a failed earlier write, first apply) -> write target.
    Live state that already equals target is never tampering -- nothing to put back. Before
    anything is recorded, new/changed console rules that can delete or move backups are
    alarmed ("console_rule") and never touched; the status/alarm is always written before
    the fingerprint moves (M2). A first apply seeds longer legacy undo windows (R-B6) and
    names the console rules that can already delete or move backups (O1).

    `expect` (apply_confirmed only, fix round 1 I1): the exact target the owner previewed and
    confirmed (_set_hash, which covers versioning too). Checked right here, against WANT as
    this pass just (re-)read it off disk -- the one authoritative re-check, since jobs.json/
    storage.json can change between apply_confirmed's own pre-check and this read (job writers
    don't take the bucket lock). A mismatch forces `gated` on for this pass -- the write
    becomes the ordinary GATED one, so nothing unconfirmed is EVER written ungated -- and is
    reported back via the 4th return value so apply_confirmed can tell the owner nothing that
    keeps less applied.

    Versioning is read/written as its own half (fix round 1): a storage that can't report
    versioning at all (NotImplemented/MethodNotAllowed, e.g. a non-AWS S3-compatible endpoint
    while lifecycle rules work fine) leaves that half unmanaged for this pass -- no versioning
    write, no versioning tamper check, `record_ver` (what's recorded to applied.json) stays
    whatever was last confirmed applied -- and the rules half still applies normally.

    Per-half tamper rule and write journal (fix round 2, (a)/(b)): a half is tampered only
    when live differs from BOTH what was applied AND what this pass's target wants (live
    already equal to target is never tampering, even mid-way through a multi-half write --
    that's the docstring's own rule, applied per half now). `_write_in_flight`/
    `_resolve_in_flight` cover the gap a kill/timeout leaves between a put landing and
    save_applied recording it (which the per-half rule alone can't fix for a CONFIRMED
    keeps-less apply, since live then sits past the ordinary GATED baseline the very next,
    journal-unaware pass would otherwise judge it against and revert).

    Returns (state, SyncResult | None, LifecycleError | None, expect_stale: bool); state is what
    was recorded (ok | restored | not_restored | error | unsupported), or "console_rule" when
    this pass raised that alarm and the app's own rules are ok/restored."""
    config_dir = cfg["CONFIG_DIR"]
    base, region, cache = _context(cfg)
    stale = False
    # final fix wave I1: read jobs.json and storage.json STRICTLY, under the lock (never a stale
    # list), before any AWS call -- a present-but-unreadable file stops the pass with state
    # "error" and nothing written, instead of acting on "no jobs" / the defaults (which would
    # remove every folder rule, or undo a confirmed suspend or tier).
    try:
        jobs = jobs_io.load_strict(config_dir)
    except (ValueError, OSError):
        return _config_error(cache, bucket, CONFIG_JOBS_UNREADABLE, stale)
    try:
        settings = load_settings_strict(config_dir)
    except SettingsFileError:
        return _config_error(cache, bucket, CONFIG_SETTINGS_UNREADABLE, stale)
    notes = notes_for(bucket, base, jobs)
    doc = _applied_doc(cache, bucket)
    try:
        creds = role_creds(config_dir, region, run=run)
        live, min_size = read_lifecycle(bucket, creds, region, run=run)
    except LifecycleError as e:
        set_status(cache, bucket, _err_state(e), e.detail)
        return _err_state(e), None, e, stale
    try:
        live_ver = read_versioning(bucket, creds, region, run=run)
    except LifecycleError as e:
        if e.kind != "unsupported":
            set_status(cache, bucket, _err_state(e), e.detail)
            return _err_state(e), None, e, stale
        # fix round 1, Minor: unsupported here means only that THIS storage can't report
        # versioning -- the lifecycle rules half still works, so don't fail the whole pass.
        live_ver, notes = None, [*notes, "this storage doesn't report versioning"]
    ver_managed = live_ver is not None
    detail = "; ".join(notes)
    save_live(cache, bucket, live, versioning=live_ver, min_size=min_size)

    doc = _resolve_in_flight(cache, bucket, doc, live, live_ver, ver_managed)   # fix round 2, (b)
    applied = doc.get("rules") if isinstance(doc, dict) and isinstance(doc.get("rules"), list) else None
    applied_ver = doc.get("versioning") if isinstance(doc, dict) and doc.get("versioning") in VERSIONING_STATES \
        else None
    stored_fp = (doc.get("console") if isinstance(doc, dict) and isinstance(doc.get("console"), dict) else None) \
        if applied is not None else None

    first = applied is None
    if first:
        try:
            if seed_undo_days(config_dir, bucket, live, folders_for(bucket, base, jobs)):   # R-B6 (#7)
                settings = load_settings_strict(config_dir)
        except SettingsFileError:
            return _config_error(cache, bucket, CONFIG_SETTINGS_UNREADABLE, stale)
    want = want_for(cfg, bucket, jobs, settings)
    # R-B2': on a first apply, a folder is only "known" (able to gate a keeps-less change)
    # once one of its jobs has actually run -- otherwise it is a new job's folder, always
    # keeps-more, no matter what a legacy/console rule already does to that prefix.
    # Final fix wave I2: the same definition on EVERY pass -- known = the applied record's folders
    # (never forgotten, R-B2) plus every folder whose job ran before THIS run (run_id: the pre-
    # backup check's own run, which hasn't uploaded yet, never counts).
    known = _known_folders(cache, bucket, base, jobs, run_id) if first else None
    before = (baseline_from_live(live, want, known, versioning=live_ver) if first
              else known_baseline(baseline_from_applied(doc, want, live_versioning=live_ver),
                                  cache, bucket, base, jobs, run_id))
    want = resolve_held(before, want)          # I1: a folder whose setting can't be read keeps its rule
    if expect is not None and _set_hash(want) != expect:
        stale, gated = True, True             # never write anything unconfirmed (I1)
    waiting = [c for c in classify(before, want) if c.kind == KEEPS_LESS] if gated else []
    target_set = gate(before, want) if gated else want
    target, target_ver = list(target_set.rules.values()), target_set.versioning
    # fix round 1, #9 controller ruling: whenever a tier is (still) wanted here and this aws
    # CLI can't control the small-object threshold on a put, the owner should know why small
    # files never move (S3's own default -- 128 KB -- silently governs every put we make).
    if any(r.get("NoncurrentVersionTransitions") for r in target) and not _min_size_supported(run, creds, region):
        notes = [*notes, "small files (under 128 KB) stay where they are"]
        detail = "; ".join(notes)
    # I1: never forget a known folder -- a folder that once had a job (so may already have
    # data in S3) stays recorded even after that job is deleted, so if it's later reused
    # (the only way to change a job's type is delete + recreate under the same name) its new
    # rule is judged against what S3 actually has there, not treated as a brand-new folder.
    # `before.folders` already IS the previous applied record's folders (baseline_from_applied
    # reused, not re-parsed) on every pass but the first, where it's <= want.folders anyway.
    folders = sorted(before.folders | want.folders)
    old_folders = doc.get("folders") if isinstance(doc, dict) and isinstance(doc.get("folders"), list) else None
    waiting_lines = [f"Waiting for your confirmation (S3 keeps the current rule): {c.words}" for c in waiting]
    # O1; fix round 2: gated on "no fingerprint recorded yet" (stored_fp is None), not on
    # `first` -- a bucket seed_new_bucket pre-seeded (applied=[], first=False) has no console
    # fingerprint yet either, and deserves the same "note, don't alarm" first look.
    live_fp = console_fingerprint(live)
    console_note = ([f"Kept as it is — a rule added in the AWS console: {line}"
                     for _, line in console_changes({}, live)] if stored_fp is None else [])
    if console_note and _o1_noted(cache, bucket) == live_fp:
        console_note = []                        # fix round 4, item 5: these exact rules were noted
    changes = console_changes(stored_fp, live)
    console_alarm = None
    if changes:
        console_alarm = {"kind": "console_rule", "at": _now_iso(),
                         "lines": [line for _, line in changes], "rules": [k for k, _ in changes]}
        runs.record_system(cache, kind="s3-rules", summary=f"A new S3 rule could delete or move backups · {bucket}",
                           lines=[*console_alarm["lines"],
                                  "backup-engine left it in place — check it in the AWS console."],
                           trigger=trigger)

    def status(state, detail_="", alarm=None, prior_alarm=_FROM_DISK):
        set_status(cache, bucket, state, detail_, alarm=merge_alarms([console_alarm, alarm]),
                  ver_managed=ver_managed, prior_alarm=prior_alarm)

    def headline(state):
        return "console_rule" if console_alarm and state in ("ok", "restored") else state

    rules_in_step = not app_rules_differ(live, target) and not any(r.get("ID") in LEGACY_IDS for r in live)
    # fix round 1, Minor: when this pass can't manage versioning at all (unsupported storage),
    # it's never out of step and never tampered -- `record_ver` (what applied.json gets) stays
    # whatever was last actually confirmed applied, never a target we never verified.
    ver_in_step = True if not ver_managed else versioning_matches(live_ver, target_ver)
    record_ver = target_ver if ver_managed else applied_ver
    if rules_in_step and ver_in_step:
        status("ok", detail)                     # M2: the alarm is on disk before the fingerprint moves
        _delete_in_flight(cache, bucket)          # defensive: _resolve_in_flight already did this
        if (first or app_rules_differ(applied, target) or stored_fp != live_fp or old_folders != folders
                or applied_ver != record_ver):
            save_applied(cache, bucket, target, console=live_fp, folders=folders, versioning=record_ver)
        if console_note:
            runs.record_system(cache, kind="s3-rules", summary=f"S3 rules checked · {bucket}",
                               lines=console_note, trigger=trigger)
        return headline("ok"), SyncResult(bucket, False, [], headline("ok"), waiting), None, stale
    # fix round 2, (a): a half is tampered only when live differs from BOTH what was applied
    # AND what this pass's target wants -- live already equal to target (rules_in_step /
    # ver_in_step) is never tampering, even mid-way through a multi-half write.
    rules_tampered = applied is not None and app_rules_differ(live, applied) and not rules_in_step
    ver_tampered = (ver_managed and applied_ver is not None
                    and not versioning_matches(live_ver, applied_ver) and not ver_in_step)
    tampered = rules_tampered or ver_tampered
    tamper_lines = ((_change_lines(applied, live) if rules_tampered else [])
                    + ([f"versioning: was {_VER_WORDS[applied_ver]}, now {_VER_WORDS.get(live_ver, live_ver)}"]
                       if ver_tampered else []))
    if tampered:
        # fix round 3, item 6: the alarm goes on disk BEFORE the puts, as soon as tampering is
        # known -- a kill right after a restoring put lands (before this pass ever reaches its
        # own post-write status() call) must not lose the alarm. `true_prior_alarm` is what
        # was genuinely open before THIS provisional write (e.g. a console_rule alarm) -- the
        # three post-write calls below pass it back in explicitly (set_status's `prior_alarm`)
        # so a merely-provisional "not_restored" here can never permanently outrank and erase
        # it once the true outcome is known, even though it's what's actually on disk in the
        # meantime for a kill to find.
        true_prior_alarm = (load_status(cache).get(bucket) or {}).get("alarm")
        status("not_restored", detail, {"kind": "not_restored", "at": _now_iso(), "lines": tamper_lines})
    rules_written = False
    _write_in_flight(cache, bucket, target, folders, record_ver)          # fix round 2, (b)
    try:
        if not rules_in_step:
            write_rules(bucket, merge(live, target), creds, region, run=run, min_size=min_size)
            rules_written = True
        if not ver_in_step:
            write_versioning(bucket, target_ver, creds, region, run=run)
    except LifecycleError as e:
        if rules_written:
            # fix round 1, I1: the rules half reached S3 even though this pass overall failed
            # (the versioning write) -- record it (folders union, the OLD applied versioning:
            # that half never changed) so applied.json matches S3, and the NEXT pass never
            # mistakes our own successful half-write for tampering (or reverts a confirmed
            # change). Status/alarm before save_applied on every path (M2); an Activity entry
            # always names the rules that landed and the versioning failure, plus any O1
            # console note (fix round 2, (c)). `tampered` here is unchanged from fix round
            # 1 -- a pass whose tampered half WAS restored but leaves the OTHER (untampered)
            # write still failing is "not_restored" for as long as that failure persists
            # (probe #3: an outside rules change while the versioning put keeps failing must
            # keep alarming); once a LATER pass reaches a clean "ok" with nothing left
            # outstanding, set_status's own not_restored -> restored upgrade (fix round 2,
            # M1 broadened) fires without this pass needing to guess ahead of that (probe
            # #10). Fix round 4, item 4: the journal is KEPT on both branches here -- the
            # versioning put may have landed although it reported an error (a timeout after
            # S3 applied it), and this pass records the OLD versioning because it can't know;
            # the next pass adopts the versioning half if live matches the journal, else
            # judges it normally (the rules half it re-adopts is what was just recorded).
            if tampered:
                # fix round 3, item 5: say what WAS and WASN'T restored, not a bare error
                # detail -- rules_written is True here, so the rules half always landed.
                rules_word = "rules restored" if rules_tampered else "rules applied"
                status("not_restored", e.detail, {"kind": "not_restored", "at": _now_iso(), "lines": tamper_lines},
                      prior_alarm=true_prior_alarm)
                save_applied(cache, bucket, target, console=live_fp, folders=folders, versioning=applied_ver)
                runs.record_system(cache, kind="s3-rules", summary=f"{TAMPERED} — NOT restored · {bucket}",
                                   lines=[*tamper_lines, f"{rules_word}; versioning couldn't be changed: {e.detail}",
                                          *console_note], outcome="failed", error=e.detail, trigger=trigger)
                return "not_restored", None, e, stale
            status(_err_state(e), e.detail)
            save_applied(cache, bucket, target, console=live_fp, folders=folders, versioning=applied_ver)
            runs.record_system(cache, kind="s3-rules", summary=f"S3 rules updated · {bucket}",
                               lines=[*_change_lines(live, target), f"versioning couldn't be changed: {e.detail}",
                                      *console_note], trigger=trigger)
            return _err_state(e), None, e, stale
        if not tampered:
            status(_err_state(e), e.detail)
            if applied is not None and stored_fp != live_fp:
                save_applied(cache, bucket, applied, console=live_fp, folders=old_folders,
                             versioning=applied_ver)                                    # alarm once
            # fix round 3, item 4: the O1 console note must still reach Activity even when
            # NOTHING landed this pass (the rules put itself failed/was never attempted) --
            # previously this branch never logged to Activity at all.
            if console_note:
                runs.record_system(cache, kind="s3-rules", summary=f"S3 rules checked · {bucket}",
                                   lines=console_note, trigger=trigger)
                # fix round 4, item 5: a TRUE first apply records no fingerprint here (it must
                # stay a first apply), so remember the note went out -- once, not every pass.
                if applied is None:
                    _mark_o1_noted(cache, bucket, live_fp)
            return _err_state(e), None, e, stale
        status("not_restored", e.detail, {"kind": "not_restored", "at": _now_iso(), "lines": tamper_lines},
              prior_alarm=true_prior_alarm)
        if stored_fp != live_fp:
            save_applied(cache, bucket, applied, console=live_fp, folders=old_folders, versioning=applied_ver)
        runs.record_system(cache, kind="s3-rules", summary=f"{TAMPERED} — NOT restored · {bucket}",
                           lines=[*tamper_lines, e.detail, *console_note], outcome="failed", error=e.detail,
                           trigger=trigger)
        return "not_restored", None, e, stale
    lines = (_change_lines(live, target) + ([] if ver_in_step else [f"now: versioning {_VER_WORDS[target_ver]}"])
             + ([detail] if detail else []) + waiting_lines + console_note)
    if tampered:
        status("restored", detail, {"kind": "restored", "at": _now_iso(), "lines": tamper_lines},
              prior_alarm=true_prior_alarm)
        save_applied(cache, bucket, target, console=live_fp, folders=folders, versioning=record_ver)
        _delete_in_flight(cache, bucket)              # definitively recorded -- the journal's job is done
        also = (["Your latest job settings were applied too."]
                if app_rules_differ(applied, target) or applied_ver != record_ver else [])
        runs.record_system(cache, kind="s3-rules", summary=f"{TAMPERED} — restored · {bucket}",
                           lines=[*tamper_lines, *also, *waiting_lines], trigger=trigger)
        return headline("restored"), SyncResult(bucket, True, lines, headline("restored"), waiting), None, stale
    status("ok", detail)
    save_applied(cache, bucket, target, console=live_fp, folders=folders, versioning=record_ver)
    _delete_in_flight(cache, bucket)                  # definitively recorded -- the journal's job is done
    runs.record_system(cache, kind="s3-rules", summary=f"S3 rules updated · {bucket}", lines=lines,
                       trigger=trigger)
    return headline("ok"), SyncResult(bucket, True, lines, headline("ok"), waiting), None, stale


def sync(cfg, bucket: str, *, run=provision._run_aws, trigger: str = "manual") -> SyncResult:
    """Apply the desired app rules to `bucket` (console rules kept) -- job save/delete,
    setup. Rules changed outside backup-engine since the last apply are alarmed, not
    silently absorbed. Raises LifecycleError on failure (after recording it)."""
    if not managed(cfg["CONFIG_DIR"]):
        raise LifecycleError("not_managed", "S3 rules need the AWS permissions update")
    _, res, err, _ = _reconcile(cfg, bucket, run=run, trigger=trigger)
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

def check(cfg, bucket: str, *, run=provision._run_aws, trigger: str = "scheduled",
          run_id: str | None = None) -> str:
    """Before every backup run (trigger "scheduled") and behind Check now ("manual"):
    the same pass as sync() -- drift from what was applied is restored + alarmed, and
    job settings that haven't reached S3 yet are applied. Never raises: anything
    unexpected becomes the "error" state. `run_id` (I2): the backup run this check runs
    before (BE_RUN_ID) -- its own start record never makes its job's folder known."""
    try:
        if not managed(cfg["CONFIG_DIR"]):
            return "not_managed"
        state, _, _, _ = _reconcile(cfg, bucket, run=run, trigger=trigger, run_id=run_id)
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


# --- previews and typed confirmation (spec §3, ruling R-B4) ------------------------------------

PREVIEW_TTL_S = 3600
SUMMARY_STALE_DAYS = 7             # fix round 1 I2: a summary this old counts as missing
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
_STALE = "That preview is out of date — preview the change again."
_STALE_RACE = ("Saved — but the S3 rules changed since your preview, so nothing that keeps "
               "less was applied. Confirm what's waiting.")


class PreviewError(Exception):
    """kind: not_managed | not_checked (nothing applied yet) | stale (token missing, older
    than an hour, jobs/settings/the applied rules changed since, or a race landed between the
    re-check and the write) | typed (bucket name not typed, or not a string) | invalid (the
    edit can't be saved -- the token is KEPT so the owner can fix the cause and retry).
    `message` is owner words."""
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class Preview:
    bucket: str
    token: str | None                 # None: nothing keeps less -- save + sync directly
    changes: list                     # every Change the write would make vs what S3 was given
    keeps_less: list                  # the Changes that need confirmation: this edit's own + already waiting
    own: list                         # the keeps-less changes caused by THIS edit alone (fix round 1)
    impacts: dict                     # rule_id -> {versions, bytes, oldest_age_days, scanned_at} | None
    needs_typed: bool                 # the bucket name must be typed to apply
    edit: dict


def _parse_iso(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _pending_path(cache_dir: str, token: str) -> Path:
    return Path(_state_dir(cache_dir), "pending", f"{token}.json")


def load_preview(cache_dir: str, token: str) -> dict | None:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    try:
        data = json.loads(_pending_path(cache_dir, token).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def discard_preview(cache_dir: str, token: str) -> None:
    if isinstance(token, str) and _TOKEN.fullmatch(token):
        try:
            _pending_path(cache_dir, token).unlink()
        except OSError:
            pass


def _prune_previews(cache_dir: str) -> None:
    d = Path(_state_dir(cache_dir), "pending")
    now = datetime.now(timezone.utc).timestamp()
    for p in d.glob("*.json") if d.is_dir() else []:
        try:
            if now - p.stat().st_mtime > PREVIEW_TTL_S:
                p.unlink()
        except OSError:
            pass


def _source_root(cfg) -> str:
    return cfg.get("SOURCE_ROOT") or os.environ.get("SOURCE_ROOT", "/backup/media")


def _scripts_dir(cfg) -> str:
    return cfg.get("SCRIPTS_DIR") or os.environ.get("SCRIPTS_DIR", "/app/scripts")


def _inputs_hash(config_dir: str, cache_dir: str, bucket: str) -> str:
    """jobs.json + storage.json + this bucket's applied record (rules, folders) exactly as
    they are on disk -- any save, or apply, in between goes stale. The applied record guards
    ABA: jobs.json/storage.json can change and change back to the exact same bytes while an
    apply moved the baseline in between (fix round 1, Minor)."""
    h = hashlib.sha256()
    for name in (jobs_io.JOBS_FILE, SETTINGS_FILE):
        try:
            h.update(Path(config_dir, name).read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    doc = _applied_doc(cache_dir, bucket) or {}
    h.update(json.dumps({"rules": doc.get("rules"), "folders": doc.get("folders"),
                         "versioning": doc.get("versioning")}, sort_keys=True).encode())
    return h.hexdigest()


def _set_hash(rs: RuleSet) -> str:
    payload = {"rules": [_norm(r) for r in rs.rules.values()], "versioning": rs.versioning}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _validated_job(job: dict, existing: list[dict], source_root: str) -> dict:
    """Normalize a job edit exactly as jobs_io.upsert's validate() will -- name trimmed,
    retention/defaults normalized, its created_at preserved from any prior job of the same
    (normalized) name -- so the preview's target is computed from the job as it will really
    be saved (fix round 1, I1 root fix). Raises ValueError, same as validate()."""
    name = str(job.get("name", "")).strip()
    prior = next((j for j in existing if j.get("name") == name), None)
    job = dict(job)
    if prior is not None and jobs_io._parse_iso(prior.get("created_at")) is not None:
        job["created_at"] = prior["created_at"]
    elif jobs_io._parse_iso(job.get("created_at")) is None:
        job["created_at"] = jobs_io._now_iso()
    return jobs_io.validate(job, source_root)


_BAD_EDIT = "That change couldn't be read — try again."


def _valid_edit_shape(edit) -> bool:
    """kind<->payload: "job" needs `job`, "settings" needs `settings` and no `job`, "confirm"
    carries neither. Shared by edited() (raises PreviewError) and save_edit()/
    _save_edit_unlocked() (raises ValueError) -- fix round 2, Minor 2: save_edit is a public,
    standalone entry point (a keeps-more edit's caller uses it directly, without going through
    edited()/preview() first) and must enforce the same shape."""
    if not isinstance(edit, dict):
        return False
    kind, job, new = edit.get("kind"), edit.get("job"), edit.get("settings")
    return ((kind == "job" and isinstance(job, dict))
            or (kind == "settings" and isinstance(new, dict) and job is None)
            or (kind == "confirm" and job is None and new is None))


def edited(jobs: list[dict], settings: dict, edit: dict, *, source_root: str | None = None) \
        -> tuple[list[dict], dict]:
    """jobs + settings as they will be once `edit` is saved -- the job normalized exactly as
    jobs_io.upsert will (fix round 1, I1). Enforces kind<->payload (_valid_edit_shape) --
    otherwise PreviewError("invalid") (fix round 1, Minor)."""
    if not _valid_edit_shape(edit):
        raise PreviewError("invalid", _BAD_EDIT)
    job, new = edit.get("job"), edit.get("settings")
    if isinstance(job, dict):
        try:
            job = _validated_job(job, jobs, source_root or os.environ.get("SOURCE_ROOT", "/backup/media"))
        except ValueError as e:
            raise PreviewError("invalid", str(e))
        jobs = [j for j in jobs if j.get("name") != job.get("name")] + [job]
    if isinstance(new, dict):
        buckets = new.get("buckets")
        settings = {"version": 1, "buckets": buckets if isinstance(buckets, dict) else {}}
    return jobs, settings


def _save_edit_unlocked(cfg, edit: dict) -> None:
    """The writes behind save_edit -- job via jobs_io (validated -- raises ValueError -- and
    with the crontab re-rendered so it stays in step, exactly like routes.py's job_save), then
    storage.json -- WITHOUT taking settings_lock. For apply_confirmed, which already holds it
    for the whole check-then-save window: flock locks are per open file description, so a
    second open()+flock of the same lock file in this process would block forever (fix round
    1, Minor). Enforces kind<->payload (_valid_edit_shape) -- raises ValueError otherwise (fix
    round 2, Minor 2): apply_confirmed's edit already passed through edited()'s check, but
    save_edit's OTHER caller (a standalone keeps-more save) may not have."""
    if not _valid_edit_shape(edit):
        raise ValueError(_BAD_EDIT)
    config_dir = cfg["CONFIG_DIR"]
    job = edit.get("job")
    if isinstance(job, dict):
        source_root = _source_root(cfg)
        jobs_io.upsert(config_dir, job, source_root=source_root)
        jobs_io.render_crontab(config_dir, cfg["CACHE_DIR"], _scripts_dir(cfg), source_root=source_root)
    new = edit.get("settings")
    if isinstance(new, dict):
        # final fix wave I1: the edit was built on the GUI's fail-safe reading (the defaults,
        # when the file can't be read) -- never let it silently replace the owner's own file.
        try:
            load_settings_strict(config_dir)
        except SettingsFileError:
            raise ValueError("The S3 rules settings file (storage.json) can't be read — fix or remove it "
                             "before changing S3 rules.")
        buckets = new.get("buckets")
        save_settings(config_dir, {"version": 1, "buckets": buckets if isinstance(buckets, dict) else {}})


def save_edit(cfg, edit: dict) -> None:
    """Save an edit standalone (a keeps-more edit: the caller saves it and syncs, no preview
    needed) -- storage.json under settings_lock. apply_confirmed uses _save_edit_unlocked
    instead, taking the lock itself around the whole check-then-save window. Enforces
    kind<->payload up front (fails fast, before touching the lock) -- raises ValueError."""
    if not _valid_edit_shape(edit):
        raise ValueError(_BAD_EDIT)
    if isinstance(edit.get("settings"), dict):
        with settings_lock(cfg["CONFIG_DIR"]):
            _save_edit_unlocked(cfg, edit)
    else:
        _save_edit_unlocked(cfg, edit)


def _summary_fresh(summary: dict | None) -> bool:
    from . import storage_summary                 # local: storage_summary imports lifecycle
    scanned = storage_summary.scanned_at(summary) if summary else None
    return scanned is not None and (datetime.now(timezone.utc) - scanned).total_seconds() \
        <= SUMMARY_STALE_DAYS * 86400


def _needs_typed(change: Change, imp: dict | None, fresh: bool) -> bool:
    """Typed confirmation whenever the change deletes anything -- or might: no summary yet, or
    one older than SUMMARY_STALE_DAYS (spec error table: "Summary missing or old", fix round 1
    I2) -- suspends versioning, or moves history to a cheaper tier (spec §3's tier damage
    warning: minimum storage charge, small objects untouched, hours-and-money restores)."""
    if change.folder is None:
        return change.rule_id == "versioning"
    return imp is None or not fresh or imp["versions"] > 0 or tier_keeps_less(change.before, change.after)


def preview(cfg, bucket: str, edit: dict) -> Preview:
    """What saving `edit` would change in `bucket`'s rules, vs what S3 was last given. Files
    only -- never AWS. Writes a token whenever the edit itself causes a keeps-less change, or
    (a "confirm" edit) whenever anything is already waiting; `keeps_less` always lists every
    keeps-less change the write would make (this edit's own, `own`, plus anything already
    waiting) -- since the confirmed write is ungated, the owner sees exactly what's written."""
    from . import storage_summary                 # local: storage_summary imports lifecycle
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not managed(config_dir):
        raise PreviewError("not_managed", "S3 rules need the AWS permissions update first.")
    source_root = _source_root(cfg)
    jobs, settings = edited(jobs_io.load(config_dir), load_settings(config_dir), edit, source_root=source_root)
    base = _context(cfg)[0]
    if bucket not in buckets_for(base, jobs):
        raise PreviewError("invalid", f"{bucket!r} isn't one of this install's buckets.")
    before, want = applied_view(cfg, bucket, jobs, settings)
    if before is None:
        raise PreviewError("not_checked", "S3 rules for this bucket haven't been checked yet — "
                                          "press Check now first.")
    changes = classify(before, want)
    less = [c for c in changes if c.kind == KEEPS_LESS]
    if edit.get("kind") == "confirm":
        own = less                                 # confirming IS resolving whatever's outstanding
    else:
        # A change is the EDIT's own when the edit itself alters that rule -- its value differs
        # from what's on disk right now (unedited) -- and the result keeps less than the
        # baseline (already true: `less`). This also counts an edit that further TIGHTENS a
        # rule that's already waiting (e.g. manga waiting 180->30, this edit sets it to 10):
        # the rule the edit produces differs from the current (30-day) one, so it's own, even
        # though manga was already keeps-less without the edit (fix round 2, Minor 6). A rule
        # the edit leaves untouched (identical before and after the edit) is never own, no
        # matter its keeps-less status -- purely informational via `keeps_less`.
        _, current_want = applied_view(cfg, bucket)    # jobs.json + storage.json exactly as they are now
        def _n(rs, rid):
            if rid == "versioning":                # Task 16: not a folder rule -- RuleSet.versioning
                return rs.versioning
            r = rs.rules.get(rid)
            return _norm(r) if r else None
        own = [c for c in less if _n(current_want, c.rule_id) != _n(want, c.rule_id)]
    impacts: dict[str, dict | None] = {}
    fresh: dict[str, bool] = {}
    for c in less:
        if c.folder is None:
            continue
        summary = storage_summary.load(cache, bucket, c.folder)
        fresh[c.rule_id] = _summary_fresh(summary)
        impacts[c.rule_id] = (dict(storage_summary.impact(summary, c.before, c.after),
                                   scanned_at=summary.get("scanned_at"),
                                   moved=storage_summary.moved(summary, c.before, c.after))
                              if summary else None)
    needs_typed = any(_needs_typed(c, impacts.get(c.rule_id), fresh.get(c.rule_id, False)) for c in less)
    token = None
    if own:
        _prune_previews(cache)
        token = secrets.token_urlsafe(18)
        _write_atomic(_pending_path(cache, token), json.dumps({
            "token": token, "bucket": bucket, "edit": edit, "target_hash": _set_hash(want),
            "inputs_hash": _inputs_hash(config_dir, cache, bucket), "created_at": _now_iso(),
            "needs_typed": needs_typed}))
    return Preview(bucket, token, changes, less, own, impacts, needs_typed, edit)


def apply_confirmed(cfg, token: str, typed: str, *, run=provision._run_aws) -> SyncResult:
    """The owner confirmed a preview: re-check it, save its edit, and write the proposed rules
    WITHOUT the gate, under the bucket lock (R-B4) -- the token is read once inside that lock
    (strict single use: an already-consumed token is stale), and the final write is re-checked
    against the previewed target from inside the same reconcile pass that reads jobs.json/
    storage.json, so a race that lands between this function's own pre-check and that read can
    never ride along ungated (fix round 1, I1)."""
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not managed(config_dir):
        raise PreviewError("not_managed", "S3 rules need the AWS permissions update first.")
    if not isinstance(typed, str):
        raise PreviewError("typed", "Type the bucket name to confirm.")
    pre = load_preview(cache, token)
    if pre is None or not isinstance(pre.get("bucket"), str):
        raise PreviewError("stale", _STALE)
    bucket = pre["bucket"]
    with bucket_lock(cache, bucket):
        t = load_preview(cache, token)             # re-read inside the lock: strict single use
        if t is None or t.get("bucket") != bucket:
            raise PreviewError("stale", _STALE)
        created = _parse_iso(t.get("created_at"))
        if created is None or (datetime.now(timezone.utc) - created).total_seconds() > PREVIEW_TTL_S:
            discard_preview(cache, token)
            raise PreviewError("stale", _STALE)
        if t.get("needs_typed") and typed.strip() != bucket:
            raise PreviewError("typed", f"Type the bucket name {bucket} exactly to confirm.")
        edit = t.get("edit")
        # order: bucket lock (outer, already held) -> settings lock -> status lock (taken
        # transiently inside _reconcile_locked, after this block) (fix round 1, Minor).
        lock = settings_lock(config_dir) if isinstance(edit, dict) and isinstance(edit.get("settings"), dict) \
            else nullcontext()
        with lock:
            jobs, settings = edited(jobs_io.load(config_dir), load_settings(config_dir), edit,
                                    source_root=_source_root(cfg))
            if (_inputs_hash(config_dir, cache, bucket) != t.get("inputs_hash")
                    or _set_hash(applied_view(cfg, bucket, jobs, settings)[1]) != t.get("target_hash")):
                discard_preview(cache, token)
                raise PreviewError("stale", _STALE)
            try:
                _save_edit_unlocked(cfg, edit)
            except ValueError as e:                  # jobs_io.JobsFileError is a ValueError too
                raise PreviewError("invalid", str(e))   # token kept: fix the cause and retry
            discard_preview(cache, token)
        _, res, err, race_stale = _reconcile_locked(cfg, bucket, run=run, trigger="manual", gated=False,
                                                    expect=t.get("target_hash"))
    if err is not None:
        raise err
    if race_stale:
        raise PreviewError("stale", _STALE_RACE)
    return res


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
            words = _CHECK_WORDS.get(check(cfg, args.bucket, trigger=trigger,
                                           run_id=os.environ.get("BE_RUN_ID") or None),
                                     "couldn't be checked (see Setup)")
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
