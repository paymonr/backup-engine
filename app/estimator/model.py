# app/estimator/model.py — PURE cost model. No I/O, no env, no print, no AWS.
#
# Simplifying assumptions (decision-support, not billing-accurate):
#   * The 128 KB minimum billable object size is applied as
#     billed_gb = max(size_gb, object_count * 128KB) rather than per-object.
#   * Data-transfer-out uses a single flat first-tier $/GB rate.
#   * Rotation/early-deletion (Task 3) charges churned bytes for the full
#     minimum-storage-duration as a conservative early-deletion proxy; this
#     intentionally overlaps with the versioning term, giving a deliberate
#     conservative upper bound rather than a precise churn accounting.
from __future__ import annotations
from dataclasses import dataclass, field
from math import ceil
from .prices import PriceTable

STORAGE_CLASSES: tuple[str, ...] = (
    "STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
)
_GB_PER_KB = 1 / (1024 * 1024)

@dataclass(frozen=True)
class PipelineInputs:
    size_gb: float
    file_count: int
    storage_class: str
    packing: bool = False
    pack_member_gb: float = 5.0
    backups_per_month: float = 30.0
    change_rate_pct: float = 10.0
    # Optional per-pipeline override for how long noncurrent versions are retained.
    # None -> fall back to the scenario-level versioning_retention_days. This lets
    # appdata carry a restic-keep-policy-derived window (effective_retention_days)
    # while media keeps the scenario's S3 noncurrent-expiry value.
    versioning_retention_days: int | None = None

@dataclass(frozen=True)
class Scenario:
    region: str = "us-east-1"
    appdata: PipelineInputs = field(
        default_factory=lambda: PipelineInputs(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10))
    media: PipelineInputs = field(
        default_factory=lambda: PipelineInputs(2000, 50000, "DEEP_ARCHIVE", backups_per_month=4, change_rate_pct=1))
    versioning_retention_days: int = 30
    restore_fraction: float = 1.0
    restores_per_year: float = 1.0
    retrieval_tier: str = "Bulk"

@dataclass
class LineItems:
    storage: float
    versioning: float
    ingest_monthly: float
    upfront_onetime: float
    rotation_monthly: float
    restore_per_event: float
    effective_object_count: int
    billed_gb: float

@dataclass
class Estimate:
    price_date: str
    price_source: str
    region: str
    pipelines: dict[str, LineItems]
    monthly_total: float
    first_year_total: float
    full_restore_total: float

def _rate(prices: PriceTable, storage_class: str) -> float:
    if storage_class not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{storage_class}'")
    return prices.storage_gb_month[storage_class]

def effective_object_count(p: PipelineInputs) -> int:
    if p.packing:
        return max(1, ceil(p.size_gb / p.pack_member_gb))
    return p.file_count

def effective_retention_days(keep_last: int = 0, keep_daily: int = 0,
                             keep_weekly: int = 0, keep_monthly: int = 0) -> int:
    """Proxy (in days) for the span of history a restic keep-policy retains — the
    furthest-back reach across the daily/weekly/monthly tiers, with keep_last (a
    snapshot count) treated as a lower-bound floor. Decision-support only, not a
    precise restic prune simulation; feeds a pipeline's versioning_retention_days."""
    return max(keep_last, keep_daily, keep_weekly * 7, keep_monthly * 30, 1)

def billed_gb(p: PipelineInputs, prices: PriceTable) -> float:
    if p.storage_class == "STANDARD":
        return p.size_gb
    floor = effective_object_count(p) * prices.min_billable_object_kb * _GB_PER_KB
    return max(p.size_gb, floor)

def storage_monthly(p: PipelineInputs, prices: PriceTable) -> float:
    return billed_gb(p, prices) * _rate(prices, p.storage_class)

def versioning_monthly(p: PipelineInputs, scenario: Scenario, prices: PriceTable) -> float:
    retention = (p.versioning_retention_days if p.versioning_retention_days is not None
                 else scenario.versioning_retention_days)
    noncurrent_gb = p.size_gb * (p.change_rate_pct / 100) * (
        p.backups_per_month * retention / 30)
    return noncurrent_gb * _rate(prices, p.storage_class)

def ingest_monthly(p: PipelineInputs, prices: PriceTable) -> float:
    new_objects_per_backup = effective_object_count(p) * (p.change_rate_pct / 100)
    return new_objects_per_backup * p.backups_per_month * prices.put_per_1k / 1000

def upfront_onetime(p: PipelineInputs, prices: PriceTable) -> float:
    return effective_object_count(p) * prices.put_per_1k / 1000

def rotation_monthly(p: PipelineInputs, scenario: Scenario, prices: PriceTable) -> float:
    min_days = prices.min_storage_duration_days.get(p.storage_class, 0)
    if not min_days:
        return 0.0
    rotated_gb_per_month = p.size_gb * (p.change_rate_pct / 100) * p.backups_per_month
    return rotated_gb_per_month * _rate(prices, p.storage_class) * (min_days / 30)

def _retrieval_per_gb(storage_class: str, tier: str, prices: PriceTable) -> float:
    table = prices.retrieval_per_gb.get(storage_class)
    if table is None:
        return 0.0  # warm class (e.g. STANDARD): no retrieval fee
    if tier in table:
        return table[tier]
    if storage_class in ("GLACIER", "DEEP_ARCHIVE"):
        raise ValueError(f"retrieval tier '{tier}' not available for {storage_class}")
    return next(iter(table.values()))  # non-tiered cold (IA/GLACIER_IR): single rate

def restore_cost(p: PipelineInputs, scenario: Scenario, prices: PriceTable, fraction: float) -> float:
    restored_gb = p.size_gb * fraction
    restored_objects = effective_object_count(p) * fraction
    cost = restored_gb * prices.data_transfer_out_per_gb + restored_objects * prices.get_per_1k / 1000
    per_gb = _retrieval_per_gb(p.storage_class, scenario.retrieval_tier, prices)
    if per_gb:
        cost += restored_gb * per_gb
        if p.storage_class in ("GLACIER", "DEEP_ARCHIVE"):
            cost += restored_objects * prices.retrieval_request_per_1k[scenario.retrieval_tier] / 1000
    return cost

def _line_items(p: PipelineInputs, scenario: Scenario, prices: PriceTable) -> LineItems:
    return LineItems(
        storage=storage_monthly(p, prices),
        versioning=versioning_monthly(p, scenario, prices),
        ingest_monthly=ingest_monthly(p, prices),
        upfront_onetime=upfront_onetime(p, prices),
        rotation_monthly=rotation_monthly(p, scenario, prices),
        restore_per_event=restore_cost(p, scenario, prices, scenario.restore_fraction),
        effective_object_count=effective_object_count(p),
        billed_gb=billed_gb(p, prices),
    )

def estimate(scenario: Scenario, prices: PriceTable) -> Estimate:
    pipelines = {name: _line_items(getattr(scenario, name), scenario, prices)
                 for name in ("appdata", "media")}
    monthly = sum(li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly
                  for li in pipelines.values())
    upfront = sum(li.upfront_onetime for li in pipelines.values())
    annual_restore = sum(li.restore_per_event for li in pipelines.values()) * scenario.restores_per_year
    first_year = 12 * monthly + upfront + annual_restore
    full_restore = sum(restore_cost(getattr(scenario, name), scenario, prices, 1.0)
                       for name in ("appdata", "media"))
    return Estimate(prices.date, prices.source, prices.region, pipelines,
                    monthly, first_year, full_restore)
