# app/gui/estimate_io.py — GUI adapter over the pure estimator model.
# Builds a Scenario from the saved jobs.json (N user-defined jobs), maps GUI form
# params -> per-job overrides, and prefills form defaults. Contains NO cost math
# itself (that lives in app.estimator.model).
from __future__ import annotations
from dataclasses import replace
from typing import Mapping
from . import config_io, jobs_io
from ..estimator.model import (
    JobInputs, Scenario, STORAGE_CLASSES, effective_retention_days,
)
from ..estimator.schedule import backups_per_month

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
_ENGINE_CHANGE = {"versioned": 10.0, "archive": 1.0}


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
    """bytes/count from cached usage: archive -> media/<name>; versioned -> the
    appdata aggregate. Falls back to module defaults for an un-backed-up job."""
    key = f"media/{job['name']}" if job.get("type") == "archive" else "appdata"
    u = (usage or {}).get(key)
    if u:
        return u["bytes"] / (1024 ** 3), int(u["count"])
    return _DEFAULT_SIZE_GB, _DEFAULT_FILES


def _job_inputs(job: dict, *, size_gb, file_count, scenario_retention, override) -> JobInputs:
    engine = job.get("type", "versioned")
    if engine == "versioned":
        keep = job.get("keep") or {}
        retention = effective_retention_days(**{f"keep_{k}": int(keep.get(k, 0))
                                                for k in ("last", "daily", "weekly", "monthly")})
    else:
        retention = None  # falls back to the scenario noncurrent-retention window
    o = override or {}
    return JobInputs(
        name=job["name"], engine=engine,
        size_gb=float(o.get("size_gb", size_gb)),
        file_count=int(o.get("file_count", file_count)),
        storage_class=job.get("storage_class", "STANDARD"),
        packing=bool(o.get("packing", False)),
        backups_per_month=float(o.get("backups_per_month",
                                      backups_per_month(job.get("schedule", "")))),
        change_rate_pct=float(o.get("change_rate_pct", _ENGINE_CHANGE.get(engine, 10.0))),
        versioning_retention_days=retention,
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


def scenario_from_params(params: Mapping, config_dir, source_root) -> Scenario:
    """Live what-if: the saved jobs.json Scenario with GUI form params overlaid per
    job (name-prefixed) plus the scenario-level globals."""
    base = scenario_from_jobs(config_dir, source_root)
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
