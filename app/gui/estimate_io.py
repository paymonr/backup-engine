# app/gui/estimate_io.py — GUI adapter over the pure estimator model.
# Builds a Scenario from the saved jobs.json (N user-defined jobs), maps GUI form
# params -> per-job overrides, and prefills form defaults. Contains NO cost math
# itself (that lives in app.estimator.model).
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import asdict, replace
from typing import Mapping
from . import config_io, jobs_io, storage_advice
from ..estimator.model import (
    JobInputs, Scenario, STORAGE_CLASSES, effective_retention_days, estimate,
    restore_cost, project, job_retention_days, cold_lockin_onetime, upfront_onetime,
    effective_object_count,
)
from ..estimator.schedule import backups_per_month
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
    if policy["type"] == "tiered":
        # A restic tiered keep policy retains a SPARSE set of snapshots (~the sum of
        # the keep tiers), not one per backup — so model it as a "count" of that many
        # noncurrent snapshots. The old days-window x backups_per_month formula
        # overstated tiered versioning ~10x for daily backups (e.g. last3/daily7/
        # weekly4/monthly6 -> ~20 snapshots, not 180 backups over a 180-day window).
        keep = policy["keep"]
        snapshots = sum(max(0, int(keep.get(k, 0))) for k in ("last", "daily", "weekly", "monthly"))
        retention_type, retention_count, retention_days = "count", max(1, snapshots), None
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
