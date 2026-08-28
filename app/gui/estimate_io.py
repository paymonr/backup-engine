# app/gui/estimate_io.py — GUI adapter over the pure estimator model.
# Prefills form defaults from backup.env and maps form params -> a Scenario.
# Contains NO cost math itself (that lives in app.estimator.model).
from __future__ import annotations
from typing import Mapping
from . import config_io
from ..estimator.model import (
    PipelineInputs, Scenario, STORAGE_CLASSES, effective_retention_days,
)

REGION = "us-east-1"  # only bundled price table (multi-region is a later phase)
RETRIEVAL_TIERS: tuple[str, ...] = ("Bulk", "Standard", "Expedited")

_KEEP_KEYS = ("keep_last", "keep_daily", "keep_weekly", "keep_monthly")
_KEEP_ENV = {"keep_last": "KEEP_LAST", "keep_daily": "KEEP_DAILY",
             "keep_weekly": "KEEP_WEEKLY", "keep_monthly": "KEEP_MONTHLY"}
_KEEP_DEFAULT = {"keep_last": 3, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}

def _class_or_default(value: str | None, fallback: str) -> str:
    return value if value in STORAGE_CLASSES else fallback

def form_defaults(config_dir: str) -> dict:
    """Initial form values, seeded from the saved backup.env where available and
    the model's own defaults otherwise."""
    env = config_io.read_backup_env(config_dir)
    d = Scenario()
    def _env_int(name: str, fallback: int) -> int:
        try:
            return int(env.get(name, "") or fallback)
        except ValueError:
            return fallback
    return {
        "region": REGION,
        "versioning_retention_days": d.versioning_retention_days,
        "retrieval_tier": d.retrieval_tier,
        "restore_fraction": d.restore_fraction,
        "restores_per_year": d.restores_per_year,
        **{k: _env_int(_KEEP_ENV[k], _KEEP_DEFAULT[k]) for k in _KEEP_KEYS},
        "appdata": {
            "size_gb": d.appdata.size_gb, "file_count": d.appdata.file_count,
            "storage_class": _class_or_default(env.get("APPDATA_STORAGE_CLASS"), d.appdata.storage_class),
            "backups_per_month": d.appdata.backups_per_month,
            "change_rate_pct": d.appdata.change_rate_pct,
        },
        "media": {
            "size_gb": d.media.size_gb, "file_count": d.media.file_count,
            "storage_class": _class_or_default(env.get("MEDIA_STORAGE_CLASS"), d.media.storage_class),
            "backups_per_month": d.media.backups_per_month,
            "change_rate_pct": d.media.change_rate_pct,
            "packing": False, "pack_member_gb": d.media.pack_member_gb,
        },
    }

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

def _pipeline(params: Mapping, defaults: dict, name: str, *, packing_allowed: bool,
              retention: int | None) -> PipelineInputs:
    dd = defaults[name]
    cls = params.get(f"{name}_storage_class") or dd["storage_class"]
    if cls not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{cls}' for {name}")
    packing = packing_allowed and str(params.get(f"{name}_packing", "")).lower() in ("1", "true", "on")
    pack_member = _num(params, f"{name}_pack_member_gb", dd.get("pack_member_gb", 5.0), label="pack member size")
    if packing and pack_member <= 0:
        raise ValueError("pack member size must be greater than zero")
    return PipelineInputs(
        size_gb=_num(params, f"{name}_size_gb", dd["size_gb"], label=f"{name} size"),
        file_count=int(_num(params, f"{name}_file_count", dd["file_count"], label=f"{name} file count")),
        storage_class=cls,
        packing=packing,
        pack_member_gb=pack_member,
        backups_per_month=_num(params, f"{name}_backups_per_month", dd["backups_per_month"], label=f"{name} backups per month"),
        change_rate_pct=_num(params, f"{name}_change_rate_pct", dd["change_rate_pct"], label=f"{name} change rate"),
        versioning_retention_days=retention,
    )

def scenario_from_params(params: Mapping, config_dir: str) -> Scenario:
    """Build a Scenario from GUI form params, falling back to form_defaults for
    anything absent. Appdata's retention is derived from the restic keep-policy;
    media falls back to the scenario-level S3 noncurrent-retention value."""
    defaults = form_defaults(config_dir)
    keep = {k: int(_num(params, k, defaults[k], label=k.replace("_", " "))) for k in _KEEP_KEYS}
    tier = params.get("retrieval_tier") or defaults["retrieval_tier"]
    if tier not in RETRIEVAL_TIERS:
        raise ValueError(f"unknown retrieval tier '{tier}'")
    return Scenario(
        region=REGION,
        appdata=_pipeline(params, defaults, "appdata", packing_allowed=False,
                          retention=effective_retention_days(**keep)),
        media=_pipeline(params, defaults, "media", packing_allowed=True, retention=None),
        versioning_retention_days=int(_num(params, "versioning_retention_days",
                                           defaults["versioning_retention_days"],
                                           label="S3 noncurrent retention days")),
        restore_fraction=_num(params, "restore_fraction", defaults["restore_fraction"], label="restore fraction"),
        restores_per_year=_num(params, "restores_per_year", defaults["restores_per_year"], label="restores per year"),
        retrieval_tier=tier,
    )
