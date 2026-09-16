# app/gui/estimate_io.py — GUI adapter over the pure estimator model.
# Builds a Scenario from the saved jobs.json (N user-defined jobs), maps GUI form
# params -> per-job overrides, and prefills form defaults. Contains NO cost math
# itself (that lives in app.estimator.model).
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import asdict, replace
from typing import Mapping
from . import config_io, jobs_io, storage_advice, vocab
from ..estimator.model import (
    JobInputs, Scenario, STORAGE_CLASSES, estimate,
    restore_cost, project, job_retention_days, cold_lockin_onetime, upfront_onetime,
    effective_object_count,
)
from ..estimator.schedule import backups_per_month, backup_interval_days
from ..estimator import tiered
from ..estimator import usage, billing

RETRIEVAL_TIERS: tuple[str, ...] = ("Bulk", "Standard", "Expedited")

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
    aggregate; archive AND versioned-files -> their own media/<name> S3 prefix
    (both write to a per-job prefix, not the shared repo). Falls back to
    module defaults for an un-backed-up job."""
    key = "appdata" if job.get("type") == "versioned" else f"media/{job['name']}"
    u = (usage or {}).get(key)
    if u:
        return u["bytes"] / (1024 ** 3), int(u["count"])
    return _DEFAULT_SIZE_GB, _DEFAULT_FILES


def _job_inputs(job: dict, *, size_gb, file_count, scenario_retention, override) -> JobInputs:
    engine = job.get("type", "versioned")
    # The single source of truth for a job's retention is its `retention` policy
    # object (jobs_io._normalize_retention also migrates the legacy per-type
    # `keep`/`retention_days` fields and applies jobs_io's own type defaults --
    # e.g. archive -> {"type": "days", "days": 180} -- so a raw/unvalidated job
    # dict, like a saved one, maps consistently). Reused here rather than
    # re-reading `keep`/`retention_days` directly.
    policy = jobs_io._normalize_retention(job, engine)
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
                                  scenario_retention=None, override=overrides.get(j["name"])))
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


def wizard_estimate(params: Mapping, config_dir, source_root, prices, *, saved_class=None) -> dict:
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
    # The wizard's retention-policy selector posts retention_type + the matching
    # field; older/direct callers (no retention_type) fall back to the pre-selector
    # per-type params so this stays backward compatible.
    if "retention_type" in params:
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

    candidate = _job_inputs(job, size_gb=size_gb, file_count=file_count,
                            scenario_retention=None, override=override)

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
    return {
        "this_job_monthly": this_est.monthly_total,
        "new_total_monthly": estimate(total_scn, prices).monthly_total,
        "this_job_restore": this_restore,
        "advice": advice,
        "guidance": storage_advice.type_advice(engine, cls),
        "explain": _explain(candidate, prices, proj, li.versioning),
        "projection": {
            "first_bill": ms[0].total,
            "steady_monthly": proj.steady_state_monthly,
            "steady_month": proj.steady_state_month,
            "at_6": ms[min(5, len(ms) - 1)].total,
            "at_12": ms[min(11, len(ms) - 1)].total,
            "at_24": ms[-1].total,
            # What you'll actually pay across the first six months (cumulative),
            # so the card can answer "what does the next half-year cost me?".
            "total_6mo": sum(m.total for m in ms[:6]),
            # keep_all never plateaus: the card must show growth, not a fake "settles at".
            "unbounded": proj.unbounded,
        },
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
    jobs_by_name = {j["name"]: j for j in jobs_io.load(config_dir)}
    prefixes = []
    for prefix, u in data.items():
        if not u:
            continue
        gb = u["bytes"] / (1024 ** 3)
        if prefix == "appdata":
            cls, label = "STANDARD", _APPDATA_LABEL
        else:
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


def _prefix_for(engine, name) -> str:
    return "appdata" if engine == "versioned" else f"media/{name}"


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
    measured = (size_gb is not None) or (data.get(_prefix_for(ji.engine, job_name)) is not None)
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
    per_job = []
    for j in scenario.jobs:
        li = est.jobs[j.name]
        u = data.get(_prefix_for(j.engine, j.name))
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
    data, fetched_str = _usage_data(cache_dir)
    u = data.get(_prefix_for(engine, name))
    size_prov = "measured" if u else "assumed"
    shared_by = sum(1 for j in jobs_io.load(config_dir) if j.get("type") == "versioned")
    shared_store = engine == "versioned" and shared_by > 1
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

    versioned_count = sum(1 for j in scenario.jobs if j.engine == "versioned")
    jobs_raw = {jj["name"]: jj for jj in jobs_io.load(config_dir)}
    per_job, restore_rows = [], []
    for j in scenario.jobs:
        li = est.jobs[j.name]
        key = _prefix_for(j.engine, j.name)
        u = data.get(key)
        size_bytes = u["bytes"] if u else None
        size_prov = "measured" if u else "assumed"
        monthly = li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly
        in_bucket_monthly = (by_prefix.get(key) or {}).get("monthly")
        shared = j.engine == "versioned" and versioned_count > 1
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
