# app/gui/readiness.py — recovery readiness (the job page rail + Get-data-back
# needs-lines) and the five setup checks (the Setup screen + the Board's needs-you
# lane). Pure reads over the caches the engine wrote; no tool ever runs here.
#
#   recovery_summary(cfg, prices, *, now=None) -> dict     (7.7.1 shape)
#   setup_checks(cfg, *, now=None) -> list[dict]           (5.10 rows)
#   warmup_summary(storage_class) -> dict | None           (7.6 WARMUP)
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config_io, jobs_io, points, vocab, estimate_io, permissions
from ..engine import runs

COLD_CLASSES = ("GLACIER", "DEEP_ARCHIVE")   # thaw-first to read

# --- WARMUP (thaw) table (7.6) --------------------------------------------
# Thaw times per cold class. Instant classes carry an empty table (no warm-up).
WARMUP: dict[str, dict[str, dict]] = {
    "DEEP_ARCHIVE": {
        "Standard": {"hours_lo": None, "hours_hi": 12},   # GUI default
        "Bulk": {"hours_lo": None, "hours_hi": 48},
    },
    "GLACIER": {
        "Expedited": {"minutes_lo": 1, "minutes_hi": 5},
        "Standard": {"hours_lo": 3, "hours_hi": 5},
        "Bulk": {"hours_lo": 5, "hours_hi": 12},
    },
    "GLACIER_IR": {},
    "STANDARD_IA": {},
    "STANDARD": {},
}
# The default warm-up tier the GUI quotes, and the cheaper alternative (5.2/7.7.1).
_WARMUP_TIERS: dict[str, tuple[str, str]] = {
    "DEEP_ARCHIVE": ("Standard", "Bulk"),
    "GLACIER": ("Standard", "Bulk"),
}


def warmup_summary(storage_class: str) -> dict | None:
    """The recovery-summary `warmup` member for a class (7.7.1), or None for an
    instant tier that needs no warm-up."""
    tiers = WARMUP.get(storage_class) or {}
    pair = _WARMUP_TIERS.get(storage_class)
    if not tiers or pair is None:
        return None
    default_tier, alt_tier = pair
    return {"tier": default_tier, "hours_hi": tiers.get(default_tier, {}).get("hours_hi"),
            "alt_tier": alt_tier, "alt_hours_hi": tiers.get(alt_tier, {}).get("hours_hi")}


# --- small helpers ---------------------------------------------------------

def _now(now) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse_ts(s):
    return points._parse_ts(s)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(at_iso, now) -> int | None:
    dt = _parse_ts(at_iso)
    if dt is None:
        return None
    return max(0, int((_now(now) - dt).total_seconds()))


def _cover_days(newest_iso, oldest_iso) -> int:
    a, b = _parse_ts(newest_iso), _parse_ts(oldest_iso)
    if a is None or b is None:
        return 0
    return max(0, (a - b).days)


def _read_json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _mtime_iso(p: Path) -> str | None:
    if not p.exists():
        return None
    return _iso(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))


def _tier_label(cls: str) -> str:
    name = vocab.CLASS_NAMES.get(cls, cls)
    return name.split(" · ")[0]


def _restore_quote(cfg, prices, name, size_gb, file_count, tier):
    """Call estimate_io.restore_quote when it exists (built by the Cost increment),
    else degrade to None. The restore cost is only as trustworthy as the size input,
    so recovery_summary sets provenance from the size — not from here."""
    fn = getattr(estimate_io, "restore_quote", None)
    if fn is None:
        return None
    try:
        return fn(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], prices, name,
                  tier=tier, size_gb=size_gb, file_count=file_count)
    except Exception:      # a restore quote must never break the readiness read
        return None


_NEEDS = {
    "versioned": ["the recovery passphrase (RESTIC_PASSWORD)", "the AWS key",
                  "this container with its /config folder (jobs.json)"],
    "archive": ["the AWS key", "this container with its /config folder"],
    "versioned-files": ["the AWS key", "this container with its /config folder (jobs.json)"],
}


# --- recovery_summary (7.7.1) ---------------------------------------------

def recovery_summary(cfg, prices, *, now=None, crontab_stale=False) -> dict:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    now = _now(now)
    sec3 = config_io.secrets_status_3(config_dir)
    secrets_at = _mtime_iso(Path(config_dir, "secrets.env"))
    probe = _read_json(Path(cache, "state", "_probe.json")) or {}
    dest = probe.get("destination") or {}
    ver = probe.get("versioning") or {}

    restore_root = cfg.get("RESTORE_ROOT", "/restore")
    mount_ok = os.path.isdir(restore_root) and os.access(restore_root, os.W_OK)

    out = {
        "generated_at": _iso(now), "tz": cfg.get("TZ", "UTC"),
        "passphrase": {"state": sec3.get("RESTIC_PASSWORD", "not_set"), "checked_at": secrets_at},
        "aws_key": {"state": sec3.get("AWS_ACCESS_KEY_ID", "not_set")},
        "destination": {"state": dest.get("state", "never"),
                        "probed_at": dest.get("probed_at"),
                        "detail": dest.get("detail", "")},
        "versioning": {"state": ver.get("state", "unknown"),
                       "checked_at": ver.get("checked_at")},
        "restore_mount": {"state": "ok" if mount_ok else "missing",
                          "path_host": cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore")},
        "crontab_stale": bool(crontab_stale),
        "jobs": [],
    }

    total_dollars = 0.0
    hours_hi = None
    any_assumed = False
    for job in jobs_io.load(config_dir):
        entry = _job_summary(cfg, prices, cache, job, now)
        out["jobs"].append(entry)
        amt = (entry.get("cost_full_restore") or {}).get("amount")
        if isinstance(amt, (int, float)):
            total_dollars += amt
        if entry["size_provenance"] != "measured":
            any_assumed = True
        w = entry.get("warmup")
        if w and isinstance(w.get("hours_hi"), (int, float)):
            hours_hi = max(hours_hi or 0, w["hours_hi"])

    out["totals"] = {"hours_hi": hours_hi, "dollars": round(total_dollars, 2),
                     "dollars_provenance": "assumed" if any_assumed else "measured"}
    return out


def _job_summary(cfg, prices, cache, job, now) -> dict:
    name = job.get("name")
    typ = job.get("type")
    cls = job.get("storage_class", "STANDARD")
    cold = cls in COLD_CLASSES
    pv = points.view(cache, job, now=now)

    newest_point = None
    oldest_point = None
    points_count = 0
    cover_days = 0
    size_bytes = file_count = None
    size_measured_at = None
    size_provenance = "assumed"

    if typ == "archive":
        rec = runs.last_completed_backup(cache, name)
        at = _iso(rec.finished_at or rec.started_at) if rec else pv.get("as_of")
        if at:
            newest_point = {"id": None, "at": at, "age_s": _age_s(at, now),
                            "source": "last successful run"}
            points_count = 1
        size_bytes = pv.get("size_bytes")
        file_count = pv.get("file_count")
        size_provenance = pv.get("size_provenance", "assumed")
        size_measured_at = pv.get("size_measured_at")
    elif typ == "versioned-files":
        pts = pv.get("points") or []
        if pts:
            at = pts[0]["time"]
            newest_point = {"id": pts[0]["id"], "at": at, "age_s": _age_s(at, now),
                            "source": "last successful run"}
            oldest_point = pts[-1]["time"]
            cover_days = _cover_days(pts[0]["time"], pts[-1]["time"])
        points_count = pv.get("count", 0)
        size_bytes = pv.get("size_bytes")
        file_count = pv.get("file_count")
        size_provenance = pv.get("size_provenance", "assumed")
    else:  # versioned
        pts = pv.get("points") or []
        if pts:
            at = pts[0]["time"]
            newest_point = {"id": pts[0]["id"], "at": at, "age_s": _age_s(at, now),
                            "source": "restore-point cache"}
            oldest_point = pv.get("oldest")
            cover_days = _cover_days(pv.get("newest"), pv.get("oldest"))
            size_bytes = pts[0].get("size_bytes")
            file_count = pts[0].get("files_total")
            size_measured_at = pts[0]["time"] if pts[0].get("summary") else None
        points_count = pv.get("count", 0)
        size_provenance = pv.get("size_provenance", "assumed")

    warmup = warmup_summary(cls)
    size_gb = (size_bytes / 1_000_000_000) if isinstance(size_bytes, (int, float)) else None

    if cold and warmup:
        primary_tier, alt_tier = warmup["tier"], warmup["alt_tier"]
    else:
        primary_tier, alt_tier = "Bulk", None

    q = _restore_quote(cfg, prices, name, size_gb, file_count, primary_tier)
    cost = {"amount": q.get("amount") if q else None, "tier": primary_tier,
            "provenance": size_provenance,
            "price_source": q.get("price_source") if q else None,
            "price_date": q.get("price_date") if q else None}
    if alt_tier:
        qa = _restore_quote(cfg, prices, name, size_gb, file_count, alt_tier)
        cost["alt_amount"] = qa.get("amount") if qa else None
        cost["alt_tier"] = alt_tier

    tested = _read_json(Path(cache, "state", f"{name}.tested.json"))
    test_pending = _read_json(Path(cache, "state", f"{name}.test-thaw.json"))

    return {
        "name": name, "type": typ, "type_label": vocab.TYPE_NAMES.get(typ, typ),
        "newest_point": newest_point, "points_count": points_count,
        "oldest_point": oldest_point, "cover_days": cover_days,
        "size_bytes": size_bytes, "size_provenance": size_provenance,
        "size_measured_at": size_measured_at, "file_count": file_count,
        "storage_class": cls, "tier_label": _tier_label(cls), "cold": cold,
        "warmup": warmup, "cost_full_restore": cost,
        "needs": list(_NEEDS.get(typ, _NEEDS["archive"])),
        "tested": tested, "test_pending": test_pending,
        "kind_note": "current copy only — no version history" if typ == "archive" else None,
    }


# --- setup_checks (5.10 / 7.7.1) ------------------------------------------

_STATE_ORDER = {"fail": 0, "unknown": 1, "warn": 2, "ok": 3}


def _human(at_iso) -> str | None:
    dt = _parse_ts(at_iso)
    return dt.astimezone(timezone.utc).strftime("%d %b %H:%M") if dt else None


def setup_checks(cfg, *, now=None, crontab_stale=False) -> list[dict]:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    jobs = jobs_io.load(config_dir)
    probe = _read_json(Path(cache, "state", "_probe.json")) or {}
    sec3 = config_io.secrets_status_3(config_dir)

    rows = [
        _check_destination(probe),
        _check_passphrase(jobs, sec3, config_dir),
        _check_versioning(probe),
        _check_jobs_scheduled(jobs),
        _check_restore_tested(cache, jobs),
    ]
    perm = _check_permissions(config_dir)
    if perm:
        rows.append(perm)
    if crontab_stale:
        rows.append({"code": "scheduler", "state": "warn",
                     "sentence": "The schedule file on disk does not match your jobs; "
                                 "restart the container.",
                     "verified_at": None, "fix_label": None, "fix_url": None})
    # Failing rows sort to the top (5.10); Python's sort is stable so canonical
    # order survives within a state.
    rows.sort(key=lambda r: _STATE_ORDER.get(r["state"], 2))
    return rows


def _check_destination(probe) -> dict:
    d = probe.get("destination") or {}
    st = d.get("state")
    if st == "ok":
        state, sentence = "ok", "Write, read, delete — all OK"
    elif st in (None, "never"):
        state, sentence = "warn", "Not probed yet"
    else:
        state, sentence = "fail", "Could not write to the bucket"
    return {"code": "destination", "state": state, "sentence": sentence,
            "verified_at": d.get("probed_at"), "reason": d.get("detail") or None,
            "fix_label": "Probe now", "fix_url": "/setup/probe"}


def _check_passphrase(jobs, sec3, config_dir) -> dict:
    has_versioned = any(j.get("type") == "versioned" for j in jobs)
    row = {"code": "passphrase", "fix_label": None, "fix_url": "/setup/keys#RESTIC_PASSWORD",
           "verified_at": None}
    if not has_versioned:
        row.update(state="ok", sentence="Not needed — no Snapshot backup job")
        return row
    st = sec3.get("RESTIC_PASSWORD", "not_set")
    if st == "set":
        row.update(state="ok", sentence="Set · not the example",
                   verified_at=_mtime_iso(Path(config_dir, "secrets.env")))
    else:  # not_set or shipped_example -> BLOCKER
        row.update(state="fail", sentence="Not set — still the shipped example", blocker=True)
    return row


def _check_versioning(probe) -> dict:
    v = probe.get("versioning") or {}
    st = v.get("state")
    if st == "on":
        state, sentence = "ok", "Bucket versioning on"
    elif st == "confirmed_by_hand":
        human = _human(v.get("checked_at"))
        state, sentence = "ok", f"Confirmed by hand{(' ' + human) if human else ''}"
    elif st == "off":
        state, sentence = "fail", "Bucket versioning is off — old versions are not protected"
    else:  # unknown / never
        state, sentence = "unknown", ("Not checkable with this key — confirm in the AWS console "
                                      "(bucket → Properties → Versioning), then mark it here")
    return {"code": "versioning", "state": state, "sentence": sentence,
            "verified_at": v.get("checked_at"),
            "fix_label": "Mark as confirmed" if state == "unknown" else None,
            "fix_url": "/setup/destination"}


def _check_jobs_scheduled(jobs) -> dict:
    enabled = [j for j in jobs if j.get("enabled", True)]
    n = len(jobs)
    if not jobs:
        state, sentence = "fail", "No job is scheduled yet"
    elif not enabled:
        state, sentence = "fail", f"{n} job{'s' if n != 1 else ''}, all paused"
    else:
        e = len(enabled)
        state, sentence = "ok", f"{e} job{'s' if e != 1 else ''} scheduled"
    return {"code": "jobs_scheduled", "state": state, "sentence": sentence,
            "verified_at": None, "fix_label": None, "fix_url": "/jobs/new"}


def _check_restore_tested(cache, jobs) -> dict:
    best_at = None
    best_job = None
    for j in jobs:
        t = _read_json(Path(cache, "state", f"{j.get('name')}.tested.json"))
        at = t.get("at") if isinstance(t, dict) else None
        dt = _parse_ts(at)
        if dt and (best_at is None or dt > best_at):
            best_at, best_job = dt, j.get("name")
    if best_at is not None:
        return {"code": "restore_tested", "state": "ok",
                "sentence": f"Tested {_human(_iso(best_at))} · {best_job}",
                "verified_at": _iso(best_at), "fix_label": None,
                "fix_url": f"/jobs/{best_job}"}
    newest = jobs[-1]["name"] if jobs else None
    return {"code": "restore_tested", "state": "warn", "sentence": "Never",
            "verified_at": None, "fix_label": None,
            "fix_url": f"/jobs/{newest}" if newest else None}


def _check_permissions(config_dir) -> dict | None:
    """The permissions stamp vs this build (spec 2026-09-22 §3). Only once the
    destination is set -- before that, the setup wizard owns the story."""
    if not config_io.is_provisioned(config_dir):
        return None
    st = permissions.level_status(config_dir)
    row = {"code": "permissions", "verified_at": st["checked_at"], "fix_label": None,
           "fix_url": "/setup/permissions"}
    if st["state"] == "current":
        row.update(state="ok", sentence="Everything this version needs")
    elif st["state"] == "behind":
        row.update(state="warn", sentence="Update needed for: "
                   + "; ".join(h["adds"] for h in st["missing"]))
    else:
        row.update(state="warn", sentence="Not checked for this version of backup-engine")
    return row
