# app/gui/estimate_io.py — GUI adapter over the pure estimator model.
# Builds a Scenario from the saved jobs.json (N user-defined jobs), maps GUI form
# params -> per-job overrides, and prefills form defaults. Contains NO cost math
# itself (that lives in app.estimator.model).
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import asdict, replace
from typing import Mapping
from . import config_io, jobs_io, storage_advice, vocab
from .storage_advice import COLD_CLASSES
from ..estimator.model import (
    JobInputs, Scenario, STORAGE_CLASSES, estimate,
    restore_cost, project, job_retention_days, cold_lockin_onetime, upfront_onetime,
    effective_object_count, cold_object_overhead_monthly, _tiered_reach_days,
)
from ..estimator.schedule import backups_per_month, backup_interval_days
from ..estimator import tiered
from ..estimator import usage, billing
from ..engine import cron

RETRIEVAL_TIERS: tuple[str, ...] = ("Bulk", "Standard", "Expedited")

# The create-screen ("blend") class phrasing (spec 5.8 §3.2) — DIFFERENT from the
# global vocab.CLASS_NAMES, which is why it lives with the wizard rather than there.
_CLASS_PLAIN = {"STANDARD": "Instant", "STANDARD_IA": "Instant, cheaper to keep",
                "GLACIER_IR": "Instant, cold price", "GLACIER": "Cold",
                "DEEP_ARCHIVE": "Deepest"}
# "Getting it back" is a static, per-class map (spec 5.8 §3.2), NOT derived from a
# tier: GLACIER prints its Standard-speed window, DEEP_ARCHIVE its Bulk ceiling.
_CLASS_READ_ACCESS = {"STANDARD": "instant", "STANDARD_IA": "instant",
                      "GLACIER_IR": "instant", "GLACIER": "3–5 h",
                      "DEEP_ARCHIVE": "≤48 h"}
_KEEP_OPTION_KEYS = ("keep_all", "tiered", "days", "count")

# Per-job fallback size/count for a job with no cached usage yet (never backed up).
# Deliberately modest so an un-measured job doesn't dominate the estimate.
_DEFAULT_SIZE_GB = 20.0
_DEFAULT_FILES = 1000

# Scenario-level globals (mirror the model's own defaults).
_GLOBAL_DEFAULTS = {
    "versioning_retention_days": 30,
    "restore_fraction": 1.0,
    "restores_per_year": 1.0,
    "retrieval_tier": "Bulk",
}

# A versioned job churns more between backups than a bulk archive job.
# versioned-files is per-file incremental versioning (like "versioned"), just
# without a shared restic repo -- same churn assumption.
# Default assumed churn per engine: 0% — a fresh estimate shows the honest floor
# (pure storage cost, no old-version/rotation churn). Users opt into a churn level
# via the wizard's "How much changes each backup?" selector / the Cost page's
# change-rate field.
_ENGINE_CHANGE = {"versioned": 0.0, "archive": 0.0, "versioned-files": 0.0}

# Sane tiered-keep defaults for a versioned job's live estimate when no keep_*
# params were posted yet -- matches job_form.html's tiered-fieldset prefill
# defaults exactly (Fix 1b) so the wizard's estimate and its form field never
# disagree about what "no input yet" means.
_KEEP_DEFAULTS = {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}


def _region(config_dir: str) -> str:
    """Region for the scenario, read per-call from the saved backup.env (only the
    bundled price table is used today; a wrong module-global captured dir would be
    a bug, so this is computed, not cached)."""
    return config_io.read_backup_env(config_dir).get("AWS_REGION") or "us-east-1"


def _num(params: Mapping, key: str, fallback, *, label: str) -> float:
    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        return float(fallback)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if v < 0:
        raise ValueError(f"{label} must be zero or positive")
    return v


def _size_for(job: dict, usage) -> tuple[float, int]:
    """bytes/count from cached usage: versioned -> the shared appdata restic
    aggregate (or, for a job with its OWN dedicated bucket, that job's own
    repo -- Task 12); archive AND versioned-files -> their own media/<name> S3
    prefix (both write to a per-job prefix, not the shared repo). Falls back to
    module defaults for an un-backed-up job."""
    key = _prefix_for(job.get("type", "versioned"), job["name"], bool(job.get("dedicated")))
    u = (usage or {}).get(key)
    if u:
        return u["bytes"] / (1024 ** 3), int(u["count"])
    return _DEFAULT_SIZE_GB, _DEFAULT_FILES


_UNDO_ENGINES = ("versioned", "versioned-files")


def _undo_days(job: dict, jobs: list[dict], config_dir) -> int | None:
    """Spec §10 (final fix wave M6): the S3 undo window -- from storage.json, per folder, via
    lifecycle's own settings reading -- that keeps what a Snapshot/File history job removed
    recoverable in S3. None for Plain copy (its history IS its S3 rule). Fail-safe: anything
    unreadable is the default window (a cost page never 500s on this)."""
    if job.get("type", "versioned") not in _UNDO_ENGINES:
        return None
    from ..engine import lifecycle                     # local: lifecycle imports gui modules
    try:
        base = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
        target = lifecycle.folder_of_job(base, jobs, job.get("name"))
        if target is None:
            return lifecycle.DEFAULT_UNDO_DAYS
        return lifecycle.undo_days(lifecycle.bucket_settings(lifecycle.load_settings(config_dir), target[0]),
                                   target[1])
    except Exception:                                  # noqa: BLE001
        return lifecycle.DEFAULT_UNDO_DAYS


def _job_inputs(job: dict, *, size_gb, file_count, scenario_retention, override,
                undo_days: int | None = None) -> JobInputs:
    engine = job.get("type", "versioned")
    # The single source of truth for a job's retention is its `retention` policy
    # object (jobs_io._normalize_retention also migrates the legacy per-type
    # `keep`/`retention_days` fields and applies jobs_io's own type defaults --
    # e.g. archive -> {"type": "days", "days": 180} -- so a raw/unvalidated job
    # dict, like a saved one, maps consistently). Reused here rather than
    # re-reading `keep`/`retention_days` directly.
    try:
        policy = jobs_io._normalize_retention(job, engine)
    except ValueError:
        # Parked P6 (T17): a hand-edited setting that can't be read (e.g. the combined form on a
        # non-Plain-copy job) never 500s the job page / Costs -- priced as keeping everything,
        # the upper bound (like folders_for, which holds the folder's current S3 rule).
        policy = {"type": "keep_all"}
    keep_tiers = {}
    if policy["type"] == "tiered":
        # A restic tiered keep policy is modelled NATIVELY (app.estimator.tiered):
        # old data is driven by the gaps between the sparse retained snapshots,
        # walked on the real calendar. Neither the old "every backup in a days
        # window" formula (~overstated) nor a "count of snapshots x churn" proxy
        # (~9x understated: a monthly snapshot kept 5 months back holds 5 months
        # of changes, not one backup's worth) is right — the tiers go through.
        keep_tiers = {k: max(0, int(policy["keep"].get(k, 0)))
                      for k in ("last", "daily", "weekly", "monthly")}
        retention_type, retention_count, retention_days = "tiered", 0, None
    elif policy["type"] == "count":
        retention_type, retention_count, retention_days = "count", policy["count"], None
    elif policy["type"] == "keep_all":
        retention_type, retention_count, retention_days = "keep_all", 0, None
    else:  # "days"
        retention_type, retention_count, retention_days = "days", 0, policy["days"]
    if undo_days is not None and engine in _UNDO_ENGINES:
        # M6 (spec §10): what a Snapshot/File history run removes stays in S3 for the folder's
        # undo window -- a "days" policy's old data lives its own window PLUS that; the other
        # policies have their own model paths (tiered/count/keep_all), where the undo window
        # replaces the scenario's fixed fallback (it feeds the comparison curves).
        retention_days = retention_days + undo_days if retention_type == "days" else undo_days
    o = override or {}
    return JobInputs(
        name=job["name"], engine=engine,
        size_gb=float(o.get("size_gb", size_gb)),
        file_count=int(o.get("file_count", file_count)),
        storage_class=job.get("storage_class", "STANDARD"),
        packing=bool(o.get("packing", False)),
        pack_member_gb=float(o.get("pack_member_gb", 5.0)),
        backups_per_month=float(o.get("backups_per_month",
                                      backups_per_month(job.get("schedule", "")))),
        change_rate_pct=float(o.get("change_rate_pct", _ENGINE_CHANGE.get(engine, 10.0))),
        versioning_retention_days=retention_days,
        retention_type=retention_type,
        retention_count=retention_count,
        keep_last=keep_tiers.get("last", 0),
        keep_daily=keep_tiers.get("daily", 0),
        keep_weekly=keep_tiers.get("weekly", 0),
        keep_monthly=keep_tiers.get("monthly", 0),
        backup_interval_days=float(o.get("backup_interval_days",
                                         backup_interval_days(job.get("schedule", "")))),
    )


def scenario_from_jobs(config_dir, source_root, *, usage=None, overrides=None) -> Scenario:
    """Build a Scenario straight from the saved jobs.json. Sizes come from cached
    usage where available, else per-job defaults. `overrides` is a {name: {...}}
    map of programmatic per-job overrides (unused on the plain page)."""
    overrides = overrides or {}
    jobs = jobs_io.load(config_dir)
    inputs = []
    for j in jobs:
        size_gb, files = _size_for(j, usage)
        inputs.append(_job_inputs(j, size_gb=size_gb, file_count=files,
                                  scenario_retention=None, override=overrides.get(j["name"]),
                                  undo_days=_undo_days(j, jobs, config_dir)))
    return Scenario(region=_region(config_dir), jobs=tuple(inputs), **_GLOBAL_DEFAULTS)


def _apply_job_params(j: JobInputs, params: Mapping) -> JobInputs:
    """Overlay a single job's live what-if params (keyed by the job NAME) onto its
    JobInputs. Job names can contain '.'/'-'/'_' — used verbatim as the field prefix."""
    name = j.name
    cls = params.get(f"{name}_storage_class") or j.storage_class
    if cls not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{cls}' for {name}")
    if f"{name}_packing" in params:
        packing = str(params.get(f"{name}_packing", "")).lower() in ("1", "true", "on")
    else:
        packing = j.packing
    pack_member = _num(params, f"{name}_pack_member_gb", j.pack_member_gb, label="pack member size")
    if packing and pack_member <= 0:
        raise ValueError("pack member size must be greater than zero")
    return replace(
        j,
        size_gb=_num(params, f"{name}_size_gb", j.size_gb, label=f"{name} size"),
        file_count=int(_num(params, f"{name}_file_count", j.file_count, label=f"{name} file count")),
        storage_class=cls,
        packing=packing,
        pack_member_gb=pack_member,
        backups_per_month=_num(params, f"{name}_backups_per_month", j.backups_per_month,
                               label=f"{name} backups per month"),
        change_rate_pct=_num(params, f"{name}_change_rate_pct", j.change_rate_pct,
                             label=f"{name} change rate"),
    )


def scenario_from_params(params: Mapping, config_dir, source_root, *, usage=None) -> Scenario:
    """Live what-if: the saved jobs.json Scenario with GUI form params overlaid per
    job (name-prefixed) plus the scenario-level globals. `usage` (cached usage's
    `data` dict, keyed like scenario_from_jobs expects) threads real measured sizes
    into the per-job breakdown when available; back-compat default None falls back
    to the per-job defaults exactly as before."""
    base = scenario_from_jobs(config_dir, source_root, usage=usage)
    tier = params.get("retrieval_tier") or base.retrieval_tier
    if tier not in RETRIEVAL_TIERS:
        raise ValueError(f"unknown retrieval tier '{tier}'")
    return replace(
        base,
        jobs=tuple(_apply_job_params(j, params) for j in base.jobs),
        versioning_retention_days=int(_num(params, "versioning_retention_days",
                                           base.versioning_retention_days,
                                           label="S3 noncurrent retention days")),
        restore_fraction=_num(params, "restore_fraction", base.restore_fraction,
                              label="restore fraction"),
        restores_per_year=_num(params, "restores_per_year", base.restores_per_year,
                               label="restores per year"),
        retrieval_tier=tier,
    )


def form_defaults(config_dir, source_root) -> dict:
    """Initial form values: the scenario-level globals plus a per-job list the
    template loops to render each job's inputs and breakdown row."""
    base = scenario_from_jobs(config_dir, source_root)
    return {
        "region": base.region,
        "versioning_retention_days": base.versioning_retention_days,
        "retrieval_tier": base.retrieval_tier,
        "restore_fraction": base.restore_fraction,
        "restores_per_year": base.restores_per_year,
        "jobs": [
            {
                "name": j.name, "engine": j.engine,
                "size_gb": j.size_gb, "file_count": j.file_count,
                "storage_class": j.storage_class,
                "backups_per_month": j.backups_per_month,
                "change_rate_pct": j.change_rate_pct,
                "packing": j.packing, "pack_member_gb": j.pack_member_gb,
                "versioning_retention_days": j.versioning_retention_days,
            }
            for j in base.jobs
        ],
    }


def retention_from_form(params: Mapping, *, default_type: str = "days") -> dict:
    """Map wizard form/query params (the job_form.html retention-policy selector:
    retention_type + the matching field) to a raw job['retention'] dict. Values are
    passed through as posted (str) -- jobs_io.validate()/_normalize_retention does
    the actual type-coercion and validation, this only shapes the {type: ...} object
    it expects. Shared by the job-save route and the live wizard estimate below so
    the two can't diverge on what a submitted policy means."""
    t = params.get("retention_type", default_type)
    if t == "keep_all":
        return {"type": "keep_all"}
    if t == "count":
        return {"type": "count", "count": params.get("retention_count", "5")}
    if t == "count_days":
        # Plain copy's combined S3 form (spec 2026-09-23 §1): newest N kept, older ones D days.
        return {"type": "count", "count": params.get("retention_nd_count", "10"),
                "days": params.get("retention_nd_days", "30")}
    if t == "tiered":
        return {"type": "tiered", "keep": {k: params.get(f"keep_{k}", "0")
                                            for k in ("last", "daily", "weekly", "monthly")}}
    if t == "days":
        return {"type": "days", "days": params.get("retention_days", "90")}
    raise ValueError(f"unknown retention type {t!r}")


def _explain(cand: JobInputs, prices, proj, steady_versioning: float) -> dict | None:
    """Plain-language facts behind the versioning number — the wizard's "why these
    numbers" block. Every figure is derived from the validated model, never
    restated by hand, so the explanation can't drift from the estimate."""
    if cand.change_rate_pct <= 0:
        # Retention only costs money when files are REPLACED. With 0% change there
        # are no old versions, so keep-policy edits correctly change nothing.
        return {"zero_churn": True}
    if cand.retention_type != "tiered":
        return None
    args = (cand.change_rate_pct / 100, cand.backup_interval_days,
            cand.keep_last, cand.keep_daily, cand.keep_weekly, cand.keep_monthly)
    scale = cand.size_gb * prices.storage_gb_month.get(cand.storage_class, 0.0)
    plateau = tiered.plateau_month(cand.keep_last, cand.keep_daily, cand.keep_weekly,
                                   cand.keep_monthly, cand.backup_interval_days)
    steady_frac = (steady_versioning / scale) if scale > 0 else 0.0
    m1 = proj.months[0].versioning if proj.months else 0.0
    # keep_last is subsumed by keep_daily at <= 1 backup/day: --keep-daily n already
    # keeps the n newest snapshots, so any keep_last <= n changes nothing.
    redundant_last = cand.backup_interval_days >= 1.0 and 0 < cand.keep_last <= cand.keep_daily
    tiers = {t["tier"]: t["adds_fraction"] * scale for t in tiered.tier_ladder(*args)}
    if redundant_last:
        tiers["daily"] = tiers.get("daily", 0.0) + tiers.pop("last", 0.0)
    longest = ("monthly" if cand.keep_monthly >= 2 else "weekly" if cand.keep_weekly >= 2
               else "daily" if cand.keep_daily >= 2 else "last")
    return {
        "zero_churn": False,
        "plateau_month": plateau,
        "longest_tier": longest,
        "longest_keep": getattr(cand, f"keep_{longest}"),
        "month1_pct_of_steady": (m1 / steady_versioning) if steady_versioning > 0 else None,
        "steady_versioning": steady_versioning,
        "old_multiplier": steady_frac,
        "old_gb": steady_frac * cand.size_gb,
        "size_gb": cand.size_gb,
        "keep_last_redundant": redundant_last,
        "keep_last": cand.keep_last,
        "keep_daily": cand.keep_daily,
        "ladder": [{"tier": k, "keep": getattr(cand, f"keep_{k}"), "adds_per_month": v}
                   for k, v in tiers.items() if getattr(cand, f"keep_{k}") > 0],
    }


def _job_monthly(li) -> float:
    """A job's recurring monthly total from its LineItems (the four recurring
    terms), matching estimate().monthly_total's per-job contribution."""
    return li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly


def _job_typical(scn_job: JobInputs, base: Scenario, prices) -> tuple[float, bool]:
    """(typical monthly, is-unbounded) for one job, with the keep_all-at-0% override
    of 7.9 applied: keep_all is unbounded ONLY when something actually changes."""
    proj = project(replace(base, jobs=(scn_job,)), prices)
    unbounded = proj.unbounded and scn_job.change_rate_pct > 0
    typical = proj.months[0].total if unbounded else proj.steady_state_monthly
    return typical, unbounded


def _wizard_classes(candidate: JobInputs, base: Scenario, prices, engine: str) -> list[dict]:
    """One priced row per model.STORAGE_CLASSES (spec 8.6 `classes`): the candidate
    re-priced on that class with the current type/change/keep rule, its full restore,
    the static read-access map, the minimum stay and per-run retrieval, and whether a
    Snapshot backup is BLOCKED on it. Calls the frozen model only."""
    unbounded = candidate.retention_type == "keep_all" and candidate.change_rate_pct > 0
    rows = []
    for cls in STORAGE_CLASSES:
        cand = replace(candidate, storage_class=cls)
        li = estimate(replace(base, jobs=(cand,)), prices).jobs[cand.name]
        blocked = engine == "versioned" and cls in COLD_CLASSES
        retr = prices.retrieval_per_gb.get(cls) or {}
        rows.append({
            "class": cls, "plain": _CLASS_PLAIN[cls],
            "monthly": None if unbounded else _job_monthly(li),
            "restore_once": restore_cost(cand, base, prices, 1.0),
            "read_access": _CLASS_READ_ACCESS[cls],
            "min_days": int(prices.min_storage_duration_days.get(cls, 0)),
            "blocked": blocked,
            "reason": "can't be read by a Snapshot backup" if blocked else None,
            "retrieval_per_run": candidate.size_gb * (retr.get("Standard") or 0.0),
        })
    return rows


def _wizard_keep_options(candidate: JobInputs, base: Scenario, prices, engine: str, *,
                         days_val: int, count_val: int, keep: dict) -> list[dict]:
    """One priced row per keep preset (spec 8.6 `keep_options`): delta_monthly is the
    old-version + rotation cost of that policy at the current class/change/size; the
    keep_all-at-0% override makes it BOUNDED (delta 0.0, not null) at 0% change."""
    def _variant(key: str) -> JobInputs:
        base_kw = dict(retention_count=0, keep_last=0, keep_daily=0, keep_weekly=0,
                       keep_monthly=0, versioning_retention_days=None)
        if key == "keep_all":
            return replace(candidate, retention_type="keep_all", **base_kw)
        if key == "days":
            return replace(candidate, retention_type="days",
                           **{**base_kw, "versioning_retention_days": days_val})
        if key == "count":
            return replace(candidate, retention_type="count",
                           **{**base_kw, "retention_count": count_val})
        return replace(candidate, retention_type="tiered",
                       retention_count=0, versioning_retention_days=None,
                       keep_last=keep["last"], keep_daily=keep["daily"],
                       keep_weekly=keep["weekly"], keep_monthly=keep["monthly"])

    out = []
    for key in _KEEP_OPTION_KEYS:
        kc = _variant(key)
        li = estimate(replace(base, jobs=(kc,)), prices).jobs[kc.name]
        delta = li.versioning + li.rotation_monthly
        unbounded = key == "keep_all" and candidate.change_rate_pct > 0
        row = {"key": key, "unbounded": unbounded,
               "allowed": (engine == "versioned") if key == "tiered" else True}
        if key == "keep_all":
            row.update(delta_monthly=None if unbounded else delta, points=None, reach_days=None)
        elif key == "tiered":
            row.update(delta_monthly=delta,
                       points=sum(keep[k] for k in ("last", "daily", "weekly", "monthly")),
                       reach_days=round(_tiered_reach_days(kc)), keep=dict(keep))
        elif key == "days":
            iv = kc.backup_interval_days or 1.0
            row.update(delta_monthly=delta,
                       points=int(round(days_val / iv)) if iv else days_val,
                       reach_days=days_val, days=days_val)
        else:  # count
            row.update(delta_monthly=delta, points=count_val,
                       reach_days=round(count_val * (kc.backup_interval_days or 1.0)),
                       count=count_val)
        out.append(row)
    return out


def _wizard_all_jobs(candidate: JobInputs, base: Scenario, prices, name: str) -> dict:
    """The whole-account row (spec 8.6 `all_jobs`): this candidate ADDED to every
    other saved job (replacing any same-named one). typical_floor is the sum of each
    job's typical-if-bounded-else-first-bill, for the `at least $X` cell."""
    others = tuple(j for j in base.jobs if j.name != name)
    total_scn = replace(base, jobs=others + (candidate,))
    proj = project(total_scn, prices)
    unbounded = proj.unbounded and any(j.change_rate_pct > 0 for j in total_scn.jobs)
    floor = sum(_job_typical(j, base, prices)[0] for j in total_scn.jobs)
    return {"first_bill": proj.months[0].total, "typical": proj.steady_state_monthly,
            "typical_floor": floor, "total_6mo": sum(m.total for m in proj.months[:6]),
            "unbounded": unbounded, "others": [j.name for j in others]}


def _wizard_blockers(engine: str, cls: str, *, retention_type=None, keep=None) -> list[dict]:
    """Server-side blocker list (spec 5.8 §3.3 / 8.6 `blockers`). A Snapshot backup on
    a cold class is the OVERRIDABLE blocker (logged acknowledgement). An all-zero
    tiered keep is a hard WON'T-RUN surfaced in the wizard beside the Advanced inputs
    (jobs_io.validate rejects it too, on POST) — passed `retention_type`/`keep` so this
    can catch it live; the enforcement gate is unchanged (source stays with validate)."""
    out = []
    if engine == "versioned" and cls in COLD_CLASSES:
        plain = _CLASS_PLAIN[cls]
        out.append({
            "code": "snapshots_on_cold_class", "class": cls, "plain": plain,
            "text": (f"A Snapshot backup can't read from {plain} · {cls}. It re-reads "
                     f"its whole store every run, so every scheduled run would fail on a "
                     f"data read."),
            "fixes": [{"label": "Use File history instead", "set": {"type": "versioned-files"}},
                      {"label": "Use Instant, cheaper", "set": {"storage_class": "STANDARD_IA"}}],
            "overridable": True})
    if (engine == "versioned" and retention_type == "tiered" and keep is not None
            and not any(int(keep.get(k, 0) or 0) for k in ("last", "daily", "weekly", "monthly"))):
        out.append({
            "code": "all_zero_tiered", "class": None, "plain": None,
            "text": "Keeping 0 of everything would remove every restore point. Keep at least one.",
            "fixes": [{"label": "Use last 3 · daily 7 · weekly 4 · monthly 6",
                       "set": {"keep_last": "3", "keep_daily": "7",
                               "keep_weekly": "4", "keep_monthly": "6"}}],
            "overridable": False})
    return out


def _wizard_warnings(engine: str, cls: str, candidate: JobInputs, prices, saved_class) -> list[dict]:
    """The Heads-up blocks (spec 5.8 §3.7 / 8.6 `warnings`) — the same findings
    class_advice fires, RE-VOICED in the blend vocabulary. Additive to `advice`."""
    out = []
    plain = _CLASS_PLAIN[cls]
    eoc = effective_object_count(candidate)
    min_days = int(prices.min_storage_duration_days.get(cls, 0))
    if (cls in COLD_CLASSES and eoc >= 50_000 and candidate.size_gb
            and (candidate.size_gb * 1024 / eoc) < 10.0):
        upload_once = upfront_onetime(candidate, prices)
        overhead = cold_object_overhead_monthly(candidate, prices)
        out.append({"code": "per_object", "text": (
            f"{eoc:,} objects × 40 KB of per-object overhead on {plain} · {cls}, and "
            f"uploads cost ~10× more per request there — ${upload_once:,.2f}, once, and "
            f"${overhead:,.2f} a month on top of the data. Bundling them first (one .cbz per "
            f"chapter, or tar) collapses the count."),
            "fix": {"label": "My files are already bundled", "set": {"packing": "1"}}})
    if min_days >= 90:
        lockin = cold_lockin_onetime(candidate, prices)
        out.append({"code": "min_stay", "text": (
            f"{min_days}-day minimum stay on {plain} · {cls}. Delete it tomorrow and you "
            f"still pay through day {min_days} — ${lockin:,.2f}."), "fix": None})
    if engine == "versioned" and cls not in COLD_CLASSES and prices.retrieval_per_gb.get(cls):
        per_run = candidate.size_gb * (prices.retrieval_per_gb[cls].get("Standard") or 0.0)
        out.append({"code": "snapshots_on_ia", "text": (
            f"A Snapshot backup re-reads its store every run, and {plain} · {cls} charges "
            f"$0.01 a GB for every read — on {candidate.size_gb:,.2f} GB that is about "
            f"${per_run:,.2f} a run, often more than the cheaper storage saves."),
            "fix": {"label": "Use Instant · STANDARD", "set": {"storage_class": "STANDARD"}}})
    if (engine in ("archive", "versioned-files") and min_days >= 180
            and candidate.backups_per_month >= 4 and candidate.change_rate_pct > 0):
        out.append({"code": "frequent_on_long_min", "text": (
            f"You back up {int(round(candidate.backups_per_month))} times a month onto a "
            f"{min_days}-day-minimum tier. Each replaced file re-incurs that minimum, so a "
            f"shorter-minimum tier can be cheaper despite a higher rate."),
            "fix": {"label": "Use Instant, cold price · GLACIER_IR",
                    "set": {"storage_class": "GLACIER_IR"}}})
    if saved_class and saved_class != cls:
        sp = _CLASS_PLAIN.get(saved_class, saved_class)
        out.append({"code": "class_change", "text": (
            f"Changing the tier affects future uploads only — files already stored stay in "
            f"{sp} · {saved_class}. Moving existing data is a separate admin action."),
            "fix": None})
        if STORAGE_CLASSES.index(cls) < STORAGE_CLASSES.index(saved_class):
            out.append({"code": "class_change_warmer", "text": (
                f"This is a warm-up change ({sp} · {saved_class} → {plain} · {cls}): "
                f"existing objects can't move to a warmer tier on their own — they need a "
                f"warm-up and a copy, which costs retrieval + requests."), "fix": None})
    return out


def _wizard_schedule(sched: str, other_jobs, engine: str) -> dict:
    """The `schedule` block (spec 8.6): human phrase, cron, backups/month, and a
    collision when another enabled Snapshot backup fires the same minute+hour."""
    try:
        human = cron.describe(sched) if sched else ""
    except Exception:
        human = sched
    return {"human": human, "cron": sched, "backups_per_month": backups_per_month(sched),
            "collision": _schedule_collision(sched, other_jobs, engine)}


def _schedule_collision(sched: str, other_jobs, engine: str) -> dict | None:
    if engine != "versioned" or not other_jobs:
        return None
    f = sched.split()
    if len(f) != 5 or not (f[0].isdigit() and f[1].isdigit()):
        return None
    minute, hour = int(f[0]), int(f[1])
    versioned = [o for o in other_jobs if o.get("type") == "versioned"
                 and o.get("enabled", True)]
    taken = set()
    hit = None
    for o in versioned:
        of = str(o.get("schedule", "")).split()
        if len(of) == 5 and of[1] == str(hour) and of[0].isdigit():
            taken.add(int(of[0]))
            if int(of[0]) == minute and hit is None:
                hit = o
    if hit is None:
        return None
    suggest = (minute + 20) % 60
    while suggest in taken:
        suggest = (suggest + 1) % 60
    return {"job": hit.get("name"), "at": f"{hour:02d}:{minute:02d}",
            "suggest": f"{hour:02d}:{suggest:02d}",
            "suggest_cron": f"{suggest} {hour} {f[2]} {f[3]} {f[4]}"}


def _first_bill_reason(first: float, typical: float, versioning: float,
                       steady_month: int, upload_onetime: float, eoc: int, put_1k: float) -> tuple:
    """(reason, text) for the create screen's first-bill clause (spec 5.8 §3.6)."""
    ramp = versioning > 0 and steady_month > 1
    upload = upload_onetime >= 0.05
    if ramp and upload:
        return "both", (f"because no old versions exist yet, and uploading {eoc:,} objects "
                        f"costs ${upload_onetime:,.2f}, once.")
    if ramp:
        return "ramp", "because no old versions exist yet"
    if upload:
        return "upload", (f"because uploading {eoc:,} objects to a cold tier costs "
                          f"${put_1k:,.2f} per 1,000 requests — ${upload_onetime:,.2f}, once. "
                          f"Charged per request, not per GB.")
    return "flat", "Your first bill is the same — nothing builds up."


def wizard_estimate(params: Mapping, config_dir, source_root, prices, *, saved_class=None,
                    other_jobs=None, live_failed=False) -> dict:
    """Live cost for the job create/edit WIZARD: prices a CANDIDATE job built from
    the in-progress form params (not yet saved), plus what the total across every
    saved job becomes with this candidate added in — replacing any existing job of
    the same name so editing a job doesn't double-count it. `prices` is loaded by
    the caller (route) so this stays pure/testable (no pricing I/O in here)."""
    name = str(params.get("name", "")).strip()
    engine = params.get("type") or "versioned"
    source = str(params.get("source", "")).strip()
    cls = params.get("storage_class") or "STANDARD"
    if cls not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{cls}'")
    job = {"name": name, "type": engine, "source": source,
           "schedule": params.get("schedule", ""), "storage_class": cls}
    # All-zero tiered keep is a WON'T-RUN (5.8 §3.3): jobs_io._normalize_retention
    # rejects it, so building the candidate with it would 400 the live estimate. When
    # it is posted, substitute the sane defaults for the candidate (so the rest of the
    # estimate still prices) and let _wizard_blockers raise the inline block instead.
    keep_posted = {k: int(_num(params, f"keep_{k}", 0, label=k)) for k in ("last", "daily", "weekly", "monthly")}
    all_zero_tiered = (engine == "versioned" and params.get("retention_type") == "tiered"
                       and not any(keep_posted.values()))
    # The wizard's retention-policy selector posts retention_type + the matching
    # field; older/direct callers (no retention_type) fall back to the pre-selector
    # per-type params so this stays backward compatible.
    if all_zero_tiered:
        job["retention"] = {"type": "tiered", "keep": dict(_KEEP_DEFAULTS)}
    elif "retention_type" in params:
        job["retention"] = retention_from_form(params)
    elif engine == "versioned":
        # No keep_* params posted (pre-selector caller / first live estimate before
        # the wizard's tiered fields have a value) -> the sane defaults, not zero.
        # jobs_io._normalize_retention (Fix 1) now rejects an all-zero tiered keep
        # policy outright, so defaulting to "0" here would 400 the live estimate.
        job["keep"] = {k: params.get(f"keep_{k}", str(d)) for k, d in _KEEP_DEFAULTS.items()}
    elif engine == "versioned-files":
        job["retention_days"] = params.get("retention_days", 90)
    if engine == "archive":
        job["mirror"] = bool(params.get("mirror"))

    # Size/count precedence: explicit size_gb/file_count params -> module defaults.
    # The estimate NEVER walks the filesystem here — it has to be instant on every
    # keystroke. A picked source folder's real size is fetched separately (async) by
    # /jobs/source-size and threaded back in via the size_gb field, so it still flows
    # through this same param, just without blocking the live recompute.
    size_gb = _num(params, "size_gb", _DEFAULT_SIZE_GB, label="size")
    file_count = int(_num(params, "file_count", _DEFAULT_FILES, label="file count"))

    # "How much changes each backup?" — the wizard's Static/Some/A-lot selector
    # sends a %; absent -> the per-engine default (unchanged behavior). This is the
    # dominant driver of the old-version + rotation cost, so surfacing it is what
    # makes the wizard estimate trustworthy for static media.
    override: dict = {}
    if str(params.get("change_rate_pct", "")).strip() != "":
        override["change_rate_pct"] = _num(params, "change_rate_pct",
                                            _ENGINE_CHANGE.get(engine, 10.0), label="change rate")
    # Bundling ("my source files are packed into ~N GB archives, e.g. .cbz") — a
    # modeling input for archive / versioned-files jobs: it collapses the effective
    # object count, which is what drives the one-time upload and the cold per-object
    # overhead. The tool doesn't bundle for you; this reflects source files already
    # bundled. Restic (versioned) auto-packs, so the wizard hides it there.
    if "packing" in params:
        override["packing"] = str(params.get("packing", "")).lower() in ("1", "true", "on")
        override["pack_member_gb"] = _num(params, "pack_member_gb", 5.0, label="bundle size")
        if override["packing"] and override["pack_member_gb"] <= 0:
            raise ValueError("bundle size must be greater than zero")
    override = override or None

    others_saved = [j for j in jobs_io.load(config_dir) if j.get("name") != name]
    candidate = _job_inputs(job, size_gb=size_gb, file_count=file_count,
                            scenario_retention=None, override=override,
                            undo_days=_undo_days(job, others_saved + [job], config_dir))

    base = scenario_from_jobs(config_dir, source_root)
    this_scn = replace(base, jobs=(candidate,))
    others = tuple(j for j in base.jobs if j.name != name)
    total_scn = replace(base, jobs=others + (candidate,))

    this_est = estimate(this_scn, prices)
    li = this_est.jobs[candidate.name]
    proj = project(this_scn, prices)
    ms = proj.months
    # Candidate full-restore (retrieval + egress) at fraction 1.0, using the
    # scenario's retrieval tier. Reuses the model; adds no math here.
    this_restore = restore_cost(candidate, base, prices, 1.0)
    advice = storage_advice.class_advice(engine, cls, str(params.get("schedule", "")),
                                         saved_class, prices,
                                         object_count=effective_object_count(candidate),
                                         size_gb=candidate.size_gb)

    # --- Task 14 wizard extensions (spec 5.8 / 7.9 / 8.6) ---------------------
    # `measured` == a real folder walk stands behind size_gb (the exact byte count
    # threaded via measured_bytes); a capped/failed walk or the 20 GB placeholder is
    # `assumed` (5.8 §2/§3.6). Only this drives .n.assumed on the create screen.
    mb = params.get("measured_bytes")
    capped = str(params.get("measured_capped", "")).strip().lower() in ("1", "true", "on")
    measured = False
    if mb is not None and str(mb).strip() != "" and not capped:
        try:
            measured = int(float(mb)) > 0
        except (TypeError, ValueError):
            measured = False
    # Tri-state for the recommendation (5.8 §3.1): the rate is "set" once a radio is
    # clicked (change_rate_touched=1); the edit screen always sends it.
    change_rate_set = str(params.get("change_rate_touched", "")).strip().lower() in ("1", "true", "on")

    days_val = int(_num(params, "retention_days", 180, label="days")) or 180
    count_val = int(_num(params, "retention_count", 30, label="count")) or 30
    keep = {k: int(_num(params, f"keep_{k}", d, label=k))
            for k, d in _KEEP_DEFAULTS.items()}

    classes = _wizard_classes(candidate, base, prices, engine)
    keep_options = _wizard_keep_options(candidate, base, prices, engine,
                                        days_val=days_val, count_val=count_val, keep=keep)
    all_jobs = _wizard_all_jobs(candidate, base, prices, name)
    blockers = _wizard_blockers(engine, cls,
                                retention_type=params.get("retention_type"), keep=keep_posted)
    warnings = _wizard_warnings(engine, cls, candidate, prices, saved_class)
    schedule = _wizard_schedule(str(params.get("schedule", "")), other_jobs, engine)
    recommendation = storage_advice.recommend_type(
        size_gb=candidate.size_gb, file_count=candidate.file_count,
        change_rate_pct=candidate.change_rate_pct, measured=measured,
        change_rate_set=change_rate_set)

    unbounded = proj.unbounded and candidate.change_rate_pct > 0       # 7.9 override
    first_bill, typical = ms[0].total, proj.steady_state_monthly
    reason, reason_text = _first_bill_reason(
        first_bill, typical, li.versioning, proj.steady_state_month,
        li.upfront_onetime, effective_object_count(candidate), prices.put_rate(cls))
    size_prov = "measured" if measured else "assumed"
    money_prov = "projected" if measured else "assumed"
    rate = prices.storage_gb_month.get(cls, 0.0)
    old_gb = (li.versioning / rate) if rate else 0.0
    price_kind = "live" if str(prices.source or "").startswith("aws-price-list") else "bundled"

    return {
        "this_job_monthly": this_est.monthly_total,
        "new_total_monthly": estimate(total_scn, prices).monthly_total,
        "this_job_restore": this_restore,
        "advice": advice,
        "guidance": storage_advice.type_advice(engine, cls),
        "explain": _explain(candidate, prices, proj, li.versioning),
        "price_kind": price_kind, "price_region": prices.region, "live_failed": bool(live_failed),
        "projection": {
            "first_bill": first_bill,
            "steady_monthly": typical,
            "steady_month": proj.steady_state_month,
            "at_6": ms[min(5, len(ms) - 1)].total,
            "at_12": ms[min(11, len(ms) - 1)].total,
            "at_24": ms[-1].total,
            # What you'll actually pay across the first six months (cumulative),
            # so the card can answer "what does the next half-year cost me?".
            "total_6mo": sum(m.total for m in ms[:6]),
            # keep_all never plateaus UNLESS nothing changes (7.9 override).
            "unbounded": unbounded,
        },
        "all_jobs": all_jobs,
        "classes": classes,
        "keep_options": keep_options,
        "recommendation": recommendation,
        "blockers": blockers,
        "warnings": warnings,
        "schedule": schedule,
        "first_bill_reason": reason,
        "first_bill_reason_text": reason_text,
        "provenance": {"this_job_monthly": money_prov, "first_bill": money_prov,
                       "total_6mo": money_prov, "classes": money_prov, "size": size_prov},
        "breakdown": {
            "billed_gb": li.billed_gb,
            "storage": li.storage,
            "versioning": li.versioning,
            "rotation": li.rotation_monthly,
            "ingest": li.ingest_monthly,
            "upload_onetime": li.upfront_onetime,
            "lockin_onetime": cold_lockin_onetime(candidate, prices),
            "change_rate_pct": candidate.change_rate_pct,
            "retention_days": candidate.versioning_retention_days,
            "rate_gb_month": rate,
            "old_gb": old_gb,
            "old_multiplier": (old_gb / candidate.size_gb) if candidate.size_gb else 0.0,
            "effective_object_count": effective_object_count(candidate),
            "put_rate_per_1k": prices.put_rate(cls),
            "cold_overhead_monthly": cold_object_overhead_monthly(candidate, prices),
        },
    }


def projection_bundle(scenario: Scenario, prices, months: int = 24) -> dict:
    """Primary trajectory + comparison variants + the one-time/first-month
    breakdown, all as plain dicts for the template and /estimate.json. Pure over
    its inputs (prices are passed in, like wizard_estimate)."""
    primary = project(scenario, prices, months)

    def _retagged(scn, cap):
        # Neutralize by POLICY SHAPE, not just the day-window: count/keep_all jobs
        # ignore versioning_retention_days, so ALSO force them onto a "days" policy
        # with the capped window — otherwise the no_versioning / rolling_30 overlay
        # curves are silent no-ops for count/keep_all jobs (they'd match primary).
        return replace(
            scn,
            jobs=tuple(replace(j, retention_type="days", retention_count=0,
                               versioning_retention_days=cap(job_retention_days(j, scn)))
                       for j in scn.jobs),
            versioning_retention_days=cap(scn.versioning_retention_days),
        )
    no_versioning = _retagged(scenario, lambda _r: 0)
    rolling_30 = _retagged(scenario, lambda r: min(r, 30))

    onetime = {
        "upload": sum(upfront_onetime(j, prices) for j in scenario.jobs),
        "lockin": [{"job": j.name, "storage_class": j.storage_class,
                    "amount": cold_lockin_onetime(j, prices)}
                   for j in scenario.jobs if cold_lockin_onetime(j, prices) > 0],
        "first_month": primary.months[0].total,
    }
    return {
        "primary": asdict(primary),
        "comparison": {
            "no_versioning": asdict(project(no_versioning, prices, months)),
            "rolling_30": asdict(project(rolling_30, prices, months)),
        },
        "onetime": onetime,
        "steady_state_month": primary.steady_state_month,
    }


# --- Current spend: real bucket usage priced now + optional Cost Explorer -------

# The versioned aggregate ("appdata") is one shared restic repo across every
# versioned job — cold storage classes aren't usable for restic yet, so it is
# always priced at STANDARD regardless of any individual job's chosen class.
_APPDATA_LABEL = "all versioned jobs (shared repo)"


def current_costs(config_dir, cache_dir, prices) -> dict:
    """Price the last refreshed `usage.collect_usage` snapshot at today's rates —
    "what you're spending already", independent of the live what-if form. Prefixes
    with no successful measurement (never refreshed, or that one `rclone size`
    call failed) are left out rather than zeroing the whole result; the whole
    thing reports unavailable only when there is nothing usable at all."""
    cached = usage.load_cached(cache_dir)
    data = (cached or {}).get("data") or {}
    jobs = jobs_io.load(config_dir)
    jobs_by_name = {j["name"]: j for j in jobs}
    # Task 12: map each job's OWN usage-cache key straight back to it, so a
    # dedicated-bucket versioned job (key "appdata:<name>", not the shared
    # "appdata") is never mislabeled or folded into the shared aggregate below.
    key_to_job = {_prefix_for(j.get("type", "versioned"), j["name"], bool(j.get("dedicated"))): j
                  for j in jobs}
    prefixes = []
    for prefix, u in data.items():
        if not u:
            continue
        gb = u["bytes"] / (1024 ** 3)
        job = key_to_job.get(prefix)
        if prefix == "appdata":
            cls, label = "STANDARD", _APPDATA_LABEL
        elif job is not None:
            cls, label = job.get("storage_class", "STANDARD"), job["name"]
        else:
            # Orphaned prefix (renamed/deleted job) -- best-effort label, as before.
            name = prefix.split("/", 1)[1] if "/" in prefix else prefix
            job = jobs_by_name.get(name)
            cls = (job or {}).get("storage_class", "STANDARD")
            label = name
        rate = prices.storage_gb_month.get(cls, 0.0)
        prefixes.append({
            "prefix": prefix, "label": label, "bytes": u["bytes"],
            "gb": gb, "class": cls, "monthly": gb * rate,
        })
    if not prefixes:
        return {"available": False}
    fetched_at = (cached or {}).get("fetched_at")
    fetched_str = (datetime.fromtimestamp(fetched_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                  if fetched_at else None)
    return {
        "fetched_at": fetched_str,
        "prefixes": prefixes,
        "total_monthly": sum(p["monthly"] for p in prefixes),
    }


def billing_view(config_dir) -> dict:
    """Optional Cost Explorer invoice + forecast via the SEPARATE read-only CE
    credential. Never returns the creds themselves. `{"connected": False}` when no
    (complete) CE credential is stored; `{"connected": True, "error": ...}` when
    stored but the CE call itself fails (bad creds, no CE permission, etc)."""
    creds = config_io.read_cost_explorer_creds(config_dir)
    if creds is None:
        return {"connected": False}
    tag = config_io.read_backup_env(config_dir).get("COST_EXPLORER_TAG") or None
    try:
        months = billing.monthly_costs(creds, tag=tag)
        fc = billing.forecast(creds)
    except billing.BillingError as e:
        return {"connected": True, "error": str(e)}
    return {"connected": True, "months": months, "forecast": fc, "tag": tag}


def read_billing_cache(cache_dir) -> dict:
    """Cache-only billing view (7.7.3): reads $CACHE_DIR/billing.json — written by
    the `billing-check` sysop — and NEVER calls Cost Explorer during a render. A
    cache older than 7 days is still shown, stamped `stale`. Returns
    `{"connected": False}` when the cache is absent/unreadable; otherwise the
    parsed months/forecast/tag with the cache's own `error` member surfaced."""
    import json
    import time
    from pathlib import Path
    p = Path(cache_dir, "billing.json")
    if not p.is_file():
        return {"connected": False}
    try:
        raw = json.loads(p.read_text())
    except (ValueError, OSError):
        return {"connected": False}
    if not isinstance(raw, dict):
        return {"connected": False}
    fetched = raw.get("fetched_at")
    age_days = stale = None
    if isinstance(fetched, (int, float)):
        age_days = (time.time() - fetched) / 86400.0
        stale = age_days > 7
    return {"connected": True, "months": raw.get("months"), "forecast": raw.get("forecast"),
            "tag": raw.get("tag"), "error": raw.get("error"),
            "fetched_at": fetched, "age_days": age_days, "stale": bool(stale)}


# ===========================================================================
# Task 12 — the Cost workbench adapters (spec 5.6 / 8.7). Every one of these is
# an ADAPTER: it shapes the frozen model's output for the GUI and NEVER does cost
# math itself (the arithmetic lives in app.estimator.model, hash-guarded by
# tests/estimator/test_untouched.py). No live network at render — cache-only
# reads (usage.load_cached, read_billing_cache); live refresh is a sysop launch.
# ===========================================================================

_SCENARIO_KEYS = ("restore_fraction", "restores_per_year", "retrieval_tier")

# The user-facing "how much changes between runs" phrase for a change rate (5.2).
_CHANGE_PHRASES = {0: "Nothing — files only get added", 1: "A little — about 1% a night",
                   10: "Some — about 10% a night", 30: "A lot — about 30% a night"}


def delta_verdict(model_value: float, invoice_value: float) -> str:
    """The verdict WORDS for a model-vs-invoice gap (spec 4.6): bands at 15/30%.
    difference = model − invoice; pct = difference / invoice."""
    difference = model_value - invoice_value
    pct = (difference / invoice_value * 100.0) if invoice_value else 0.0
    ap, high = abs(pct), difference >= 0
    if ap <= 15:
        return ("close enough to trust, and it errs on the expensive side" if high
                else "close enough to trust, and it errs on the cheap side")
    if ap <= 30:
        return ("model runs high — worth a look at the assumptions" if high
                else "model runs low — worth a look at the assumptions")
    return "far apart — check the assumptions and whether the invoice covers more than these backups"


def provenance_of(inputs) -> str:
    """The provenance of a COMPUTED figure (spec 4.6/7.9): weakest input on the
    order assumed < measured < invoiced, collapsed to exactly two answers — it
    returns ``assumed`` when any input is assumed, else ``projected``. ``measured``
    and ``invoiced`` are set directly on OBSERVED figures and never come out here."""
    return "assumed" if any(p == "assumed" for p in (inputs or [])) else "projected"


def _money_provenance(size_prov, versioning, rotation, change_rate, bundled=False) -> str:
    """The provenance mark for a per-job money figure: the size input, plus the
    change-rate guess when it actually moves the figure (old-versions cost > 0), and
    the bundling guess when bundled (4.6)."""
    marks = [size_prov]
    if change_rate > 0 and (versioning + rotation) > 0:
        marks.append("assumed")
    if bundled:
        marks.append("assumed")
    return provenance_of(marks)


def _invoice_from_cache(billing) -> dict | None:
    """The most recent month in the cached billing view, or None (8.1 `invoice`)."""
    months = billing.get("months") if billing.get("connected") else None
    if not months:
        return None
    last = months[-1]
    return {"month": last.get("month"), "amount": last.get("amount"),
            "tag_scoped": bool(billing.get("tag"))}


def _cost_delta(model_value, invoice) -> dict | None:
    """model − invoice with the short form and the 15/30 verdict (4.6). None unless
    both a model figure and a non-zero invoice amount exist."""
    amount_inv = invoice.get("amount") if isinstance(invoice, dict) else invoice
    if model_value is None or amount_inv in (None, 0):
        return None
    amount = model_value - amount_inv
    pct = amount / amount_inv * 100.0
    if abs(pct) < 0.05:
        short = "±0.0% · matches"
    else:
        direction = "model runs high" if amount > 0 else "model runs low"
        short = f"{pct:+.1f}%".replace("-", "−") + f" · {direction}"
    return {"amount": round(amount, 2), "pct": round(pct, 1),
            "verdict": delta_verdict(model_value, amount_inv), "short": short}


def _change_phrase(pct) -> str:
    key = int(round(pct))
    if key in _CHANGE_PHRASES:
        return _CHANGE_PHRASES[key]
    return f"About {pct:g}% a night" if pct > 0 else _CHANGE_PHRASES[0]


def _usage_data(cache_dir) -> tuple[dict, str | None]:
    """The cached usage `data` dict and a formatted `fetched_at` (cache-only)."""
    cached = usage.load_cached(cache_dir) or {}
    fetched = cached.get("fetched_at")
    fetched_str = (datetime.fromtimestamp(fetched, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                   if fetched else None)
    return (cached.get("data") or {}), fetched_str


def _prefix_for(engine, name, dedicated=False) -> str:
    """The usage-cache key for a job. A versioned job normally shares the ONE
    "appdata" restic aggregate in the base bucket -- but a job with its OWN
    dedicated bucket (multi-bucket, Task 12) gets its own repo there, so it must
    key separately or its usage would be folded into (or steal from) every other
    versioned job's shared number. Archive/versioned-files jobs already have
    their own media/<name> prefix regardless of which bucket that prefix lives
    in, so they need no such split."""
    if engine == "versioned":
        return f"appdata:{name}" if dedicated else "appdata"
    return f"media/{name}"


def restore_quote(config_dir, cache_dir, prices, job_name, *, fraction=1.0,
                  tier="Standard", size_gb=None, file_count=None) -> dict:
    """A full-restore price for one job (spec 7.6): builds the job's JobInputs like
    scenario_from_jobs, overrides size/count with measured values when given, sets
    the retrieval tier, and returns model.restore_cost — no new math here (the
    7-day staging copy is already inside restore_cost)."""
    data, _ = _usage_data(cache_dir)
    base = scenario_from_jobs(config_dir, "", usage=data)
    ji = next((j for j in base.jobs if j.name == job_name), None)
    if ji is None:
        raise ValueError(f"no job called {job_name}")
    if tier not in RETRIEVAL_TIERS:
        raise ValueError(f"unknown retrieval tier '{tier}'")
    dedicated = bool((jobs_io.get(config_dir, job_name) or {}).get("dedicated"))
    measured = (size_gb is not None) or (
        data.get(_prefix_for(ji.engine, job_name, dedicated)) is not None)
    if size_gb is not None:
        ji = replace(ji, size_gb=float(size_gb))
    if file_count is not None:
        ji = replace(ji, file_count=int(file_count))
    scn = replace(base, retrieval_tier=tier,
                  jobs=tuple(ji if j.name == job_name else j for j in base.jobs))
    from . import readiness  # local import: readiness imports estimate_io (avoid cycle)
    w = readiness.WARMUP.get(ji.storage_class, {}).get(tier)
    warmup_hours = ((w.get("hours_lo"), w.get("hours_hi"))
                    if w and ("hours_hi" in w or "hours_lo" in w) else None)
    return {"amount": restore_cost(ji, scn, prices, fraction),
            "size_gb": ji.size_gb, "file_count": ji.file_count, "tier": tier,
            "storage_class": ji.storage_class,
            "provenance": "measured" if measured else "assumed",
            "warmup_hours": warmup_hours,
            "price_source": prices.source, "price_date": prices.date}


def board_cost(config_dir, cache_dir, prices) -> dict:
    """The Board's cost object (spec 8.1 `cost`), from CACHES ONLY — the priced
    usage cache for the measured "in the bucket now" size, the REAL model for the
    projected monthly, and cache-only billing for the invoice. Never Cost Explorer,
    never a live pricing call at render."""
    region = _region(config_dir)
    price = ({"kind": prices.source, "region": region, "date": prices.date}
             if prices is not None else None)
    billing = read_billing_cache(cache_dir)
    invoice = _invoice_from_cache(billing)
    cur = (current_costs(config_dir, cache_dir, prices)
           if prices is not None else {"available": False})
    prefixes = cur.get("prefixes") or []
    in_bucket = sum(p["bytes"] for p in prefixes) or None
    data, _ = _usage_data(cache_dir)

    scenario = scenario_from_jobs(config_dir, "", usage=data)
    empty = {"in_bucket_bytes": in_bucket, "in_bucket_at": cur.get("fetched_at"),
             "prefix_count": len(prefixes), "invoice": invoice, "model_monthly": None,
             "model_monthly_provenance": "projected", "model_floor": None,
             "delta": None, "why_high_note": None, "per_job": [], "price": price}
    if not scenario.jobs or prices is None:
        return empty

    est = estimate(scenario, prices)
    jobs_raw = {jj["name"]: jj for jj in jobs_io.load(config_dir)}
    per_job = []
    for j in scenario.jobs:
        li = est.jobs[j.name]
        dedicated = bool(jobs_raw.get(j.name, {}).get("dedicated"))
        u = data.get(_prefix_for(j.engine, j.name, dedicated))
        size_prov = "measured" if u else "assumed"
        monthly = li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly
        per_job.append({
            "name": j.name, "size_bytes": (u["bytes"] if u else None),
            "size_provenance": size_prov, "file_count": (u.get("count") if u else None),
            "ext": None, "old_versions_gb": None,
            "tier_label": vocab.tier_label(j.storage_class), "storage_class": j.storage_class,
            "monthly": monthly, "settles": None,
            "monthly_provenance": _money_provenance(size_prov, li.versioning,
                                                    li.rotation_monthly, j.change_rate_pct, j.packing),
        })
    model_monthly = est.monthly_total
    return {
        "in_bucket_bytes": in_bucket, "in_bucket_at": cur.get("fetched_at"),
        "prefix_count": len(prefixes), "invoice": invoice,
        "model_monthly": model_monthly,
        "model_monthly_provenance": provenance_of([p["monthly_provenance"] for p in per_job]),
        "model_floor": None, "delta": _cost_delta(model_monthly, invoice),
        "why_high_note": None, "per_job": per_job, "price": price,
    }


def _cost_rows(ji, u, fetched_str) -> list[dict]:
    """The `Where the money goes` rows for the job page cost band (8.7 `rows`)."""
    if u:
        amount_text = f"{u['bytes'] / (1024 ** 3):,.2f} GB · {u['count']:,} files"
        prov, source = "measured", (f"Walked the folder on {fetched_str}."
                                    if fetched_str else "Measured from the bucket.")
    else:
        amount_text, prov, source = f"{ji.size_gb:,.2f} GB", "assumed", "Not measured yet."
    return [{"label": "Storing your files", "amount_text": amount_text,
             "amount_provenance": prov, "source": source}]


def job_cost_band(job, config_dir, cache_dir, prices) -> dict:
    """The job page's `What this job costs` band (spec 5.2 / 8.7): the four headline
    figures (First bill / By month 6 / Every month after / In the bucket now), the
    change-rate assumption row's value, and the whole-account delta. Cache-only."""
    name = job.get("name")
    engine = job.get("type", "versioned")
    dedicated = bool(job.get("dedicated"))
    data, fetched_str = _usage_data(cache_dir)
    u = data.get(_prefix_for(engine, name, dedicated))
    size_prov = "measured" if u else "assumed"
    # A job in its OWN dedicated bucket has its OWN repo -- it neither shares
    # nor counts toward another versioned job's shared "appdata" store (Task 12).
    shared_by = sum(1 for j in jobs_io.load(config_dir)
                    if j.get("type") == "versioned" and not j.get("dedicated"))
    shared_store = engine == "versioned" and not dedicated and shared_by > 1
    price = ({"kind": prices.source, "region": _region(config_dir), "date": prices.date}
             if prices is not None else None)
    invoice = _invoice_from_cache(read_billing_cache(cache_dir))

    base = scenario_from_jobs(config_dir, "", usage=data)
    ji = next((j for j in base.jobs if j.name == name), None)
    common = {
        "in_bucket_bytes": (u["bytes"] if u else None), "in_bucket_at": fetched_str,
        "size_provenance": size_prov, "file_count": (u.get("count") if u else None),
        "tier_label": vocab.tier_label(job.get("storage_class", "STANDARD")),
        "storage_class": job.get("storage_class", "STANDARD"),
        "invoice": invoice, "price": price,
        "shared_store": shared_store, "shared_by": shared_by,
    }
    if ji is None or prices is None:
        return {**common, "first_bill": None, "first_bill_provenance": "projected",
                "at_6": None, "by_month_6": None, "at_6_provenance": "projected",
                "steady": None, "settled": None, "steady_provenance": "projected",
                "steady_month": 1, "unbounded": False, "monthly": None,
                "monthly_provenance": "projected", "model_monthly": None, "delta": None,
                "change_rate_pct": None, "change_phrase": None, "rows": _cost_rows_none(u, fetched_str)}

    this_scn = replace(base, jobs=(ji,))
    proj = project(this_scn, prices, 24)
    est_this = estimate(this_scn, prices)
    li = est_this.jobs[ji.name]
    ms = proj.months
    change = ji.change_rate_pct
    unbounded = proj.unbounded and change > 0            # keep_all-at-0% override (7.9)
    steady = None if unbounded else proj.steady_state_monthly
    prov = _money_provenance(size_prov, li.versioning, li.rotation_monthly, change, ji.packing)
    all_model = estimate(base, prices).monthly_total
    return {
        **common,
        "first_bill": ms[0].total, "first_bill_provenance": prov,
        "at_6": ms[5].total, "by_month_6": ms[5].total, "at_6_provenance": prov,
        "steady": steady, "settled": steady, "steady_provenance": prov,
        "steady_month": proj.steady_state_month, "unbounded": unbounded,
        "monthly": li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly,
        "monthly_provenance": prov,
        "model_monthly": all_model, "delta": _cost_delta(all_model, invoice),
        "change_rate_pct": change, "change_phrase": _change_phrase(change),
        "rows": _cost_rows(ji, u, fetched_str),
    }


def _cost_rows_none(u, fetched_str) -> list[dict]:
    if u:
        return [{"label": "Storing your files",
                 "amount_text": f"{u['bytes'] / (1024 ** 3):,.2f} GB · {u['count']:,} files",
                 "amount_provenance": "measured",
                 "source": (f"Walked the folder on {fetched_str}." if fetched_str else "Measured.")}]
    return [{"label": "Storing your files", "amount_text": "—",
             "amount_provenance": "assumed", "source": "Not measured yet."}]


def read_cost_scenario(config_dir) -> dict:
    """$CONFIG_DIR/cost.json (5.6 band 3): the persisted scenario-wide levers. A
    missing or unparseable file means the model's own defaults — never an error."""
    import json
    from pathlib import Path
    p = Path(config_dir, "cost.json")
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _apply_keep_all_override(projection, scenario) -> None:
    """keep_all at 0% change is BOUNDED and the adapter says so (spec 7.9): the model
    sets unbounded = any(keep_all) regardless of churn, correct in general and wrong
    at 0% (nothing is ever replaced, so the curve is flat). Override, don't touch the
    model. Applied to the primary projection."""
    unbounded = any(j.retention_type == "keep_all" and j.change_rate_pct > 0
                    for j in scenario.jobs)
    projection["primary"]["unbounded"] = unbounded


def _tier_options_for(storage_class) -> list[str]:
    from . import readiness
    return list(readiness.WARMUP.get(storage_class, {}).keys())


def _restore_row(config_dir, cache_dir, prices, ji, size_bytes, file_count, scenario) -> dict:
    """One `restore` row for the cost page (8.7): the primary quote at the scenario
    tier, plus the alternate speed for a cold class."""
    size_gb = size_bytes / (1024 ** 3) if size_bytes else None
    q = restore_quote(config_dir, cache_dir, prices, ji.name, fraction=1.0,
                      tier=scenario.retrieval_tier, size_gb=size_gb, file_count=file_count)
    row = {"name": ji.name, "size_bytes": size_bytes, "warmup": None,
           "tier": q["tier"], "amount": q["amount"], "alt_tier": None,
           "alt_amount": None, "provenance": q["provenance"]}
    if ji.storage_class in ("GLACIER", "DEEP_ARCHIVE"):
        row["warmup"] = {"tier": q["tier"], "hours": q["warmup_hours"]}
        alt = next((t for t in _tier_options_for(ji.storage_class) if t != q["tier"]), None)
        if alt:
            aq = restore_quote(config_dir, cache_dir, prices, ji.name, fraction=1.0,
                               tier=alt, size_gb=size_gb, file_count=file_count)
            row["alt_tier"], row["alt_amount"] = aq["tier"], aq["amount"]
    return row


def cost_page(params: Mapping, config_dir, cache_dir, prices, source_root) -> dict:
    """The whole Cost workbench payload (spec 5.6 / 8.7): composes scenario_from_params
    (levers over the saved jobs + the persisted cost.json scenario), estimate,
    projection_bundle (with the keep_all-at-0% override), current_costs, cached
    billing, a restore_quote per job and the delta. Pure over its inputs (prices are
    passed in). Raises ValueError on bad lever input, like scenario_from_params."""
    data, _ = _usage_data(cache_dir)
    saved = read_cost_scenario(config_dir)
    merged = dict(params)
    for k in _SCENARIO_KEYS:
        if k not in merged and saved.get(k) is not None:
            merged[k] = saved[k]
    scenario = scenario_from_params(merged, config_dir, source_root, usage=data)
    est = estimate(scenario, prices)
    projection = projection_bundle(scenario, prices, 24)
    _apply_keep_all_override(projection, scenario)

    cur = current_costs(config_dir, cache_dir, prices)
    by_prefix = {p["prefix"]: p for p in cur.get("prefixes") or []}
    billing = read_billing_cache(cache_dir)
    invoice = _invoice_from_cache(billing)

    jobs_raw = {jj["name"]: jj for jj in jobs_io.load(config_dir)}
    # A dedicated-bucket versioned job has its own repo -- exclude it from the
    # "how many jobs share appdata" count (Task 12).
    versioned_count = sum(1 for j in scenario.jobs
                          if j.engine == "versioned" and not jobs_raw.get(j.name, {}).get("dedicated"))
    per_job, restore_rows = [], []
    for j in scenario.jobs:
        li = est.jobs[j.name]
        dedicated = bool(jobs_raw.get(j.name, {}).get("dedicated"))
        key = _prefix_for(j.engine, j.name, dedicated)
        u = data.get(key)
        size_bytes = u["bytes"] if u else None
        size_prov = "measured" if u else "assumed"
        monthly = li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly
        in_bucket_monthly = (by_prefix.get(key) or {}).get("monthly")
        shared = j.engine == "versioned" and not dedicated and versioned_count > 1
        pj_delta = None
        if not shared and in_bucket_monthly is not None:
            pj_delta = round(in_bucket_monthly - monthly, 2)
        rate = prices.storage_gb_month.get(j.storage_class, 0.0)
        per_job.append({
            "name": j.name, "size_bytes": size_bytes, "size_provenance": size_prov,
            "old_versions_gb": (li.versioning / rate if (li.versioning and rate) else None),
            "storage_class": j.storage_class, "tier_label": vocab.tier_label(j.storage_class),
            "monthly": monthly,
            "monthly_provenance": _money_provenance(size_prov, li.versioning,
                                                    li.rotation_monthly, j.change_rate_pct, j.packing),
            "in_bucket_bytes": size_bytes, "in_bucket_monthly": in_bucket_monthly,
            "delta": pj_delta, "shared_store": shared,
        })
        restore_rows.append(_restore_row(config_dir, cache_dir, prices, j, size_bytes,
                                         (u.get("count") if u else None), scenario))

    assumptions = {
        "jobs": {j.name: {"change_rate_pct": j.change_rate_pct, "bundled": j.packing,
                          "pack_member_gb": j.pack_member_gb,
                          "set_at": (jobs_raw.get(j.name, {}).get("assumptions") or {}).get("set_at")}
                 for j in scenario.jobs},
        "scenario": {"restore_fraction": scenario.restore_fraction,
                     "restores_per_year": scenario.restores_per_year,
                     "retrieval_tier": scenario.retrieval_tier,
                     "set_at": saved.get("set_at")},
    }
    return {
        **asdict(est), "projection": projection, "current": cur, "billing": billing,
        "invoice": invoice, "delta": _cost_delta(est.monthly_total, invoice),
        "model_monthly_provenance": provenance_of([p["monthly_provenance"] for p in per_job]),
        "per_job": per_job, "restore": restore_rows, "assumptions": assumptions,
        "price": {"kind": prices.source, "region": scenario.region, "date": prices.date},
    }


def combined_in_use(config_dir, job: dict | None = None) -> bool:
    """Final fix wave M6: the frozen model prices "keep the newest N old versions; older ones go
    after D days" (Plain copy's combined form) as newest N only -- cost screens say so when any
    job (or this job) uses it. Never raises."""
    def combined(j) -> bool:
        if not isinstance(j, dict) or j.get("type") != "archive":
            return False
        try:
            r = jobs_io._normalize_retention(j, "archive")
        except ValueError:
            return False
        return r["type"] == "count" and r.get("days", 1) > 1
    try:
        return combined(job) if job is not None else any(combined(j) for j in jobs_io.load(config_dir))
    except Exception:                                  # noqa: BLE001 — a cost page never 500s on this
        return False


def tier_in_use(config_dir, job: dict | None = None) -> bool:
    """Spec §10: the frozen model doesn't price moving old versions to a cheaper tier; cost
    screens say so when any app folder (or this job's folder) has one. Never raises."""
    from ..engine import lifecycle                     # local: lifecycle imports gui modules
    try:
        settings = lifecycle.load_settings(config_dir)
        jobs = jobs_io.load(config_dir)
        base = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
        if job is not None:
            targets = [lifecycle.folder_of_job(base, jobs, job.get("name"))]
        else:
            targets = [(b, f.folder) for b in lifecycle.buckets_for(base, jobs)
                       for f in lifecycle.folders_for(b, base, jobs)]
        return any(t is not None and lifecycle.folder_tier(lifecycle.bucket_settings(settings, t[0]), t[1])
                   for t in targets)
    except Exception:                                  # noqa: BLE001 — a cost page never 500s on this
        return False
