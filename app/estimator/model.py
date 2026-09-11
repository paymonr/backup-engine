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
from dataclasses import dataclass
from math import ceil
from .prices import PriceTable

STORAGE_CLASSES: tuple[str, ...] = (
    "STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
)
_GB_PER_KB = 1 / (1024 * 1024)
_DAYS_PER_MONTH = 30.4  # matches schedule.py

@dataclass(frozen=True)
class JobInputs:
    name: str
    engine: str  # "versioned" | "archive" — the job type (informational to the model)
    size_gb: float
    file_count: int
    storage_class: str
    packing: bool = False
    pack_member_gb: float = 5.0
    backups_per_month: float = 30.0
    change_rate_pct: float = 10.0
    # Optional per-job override for how long noncurrent versions are retained.
    # None -> fall back to the scenario-level versioning_retention_days. This lets
    # a versioned job carry a restic-keep-policy-derived window
    # (effective_retention_days) while an archive job keeps the scenario's S3
    # noncurrent-expiry value.
    versioning_retention_days: int | None = None
    # The job's retention POLICY shape (from jobs_io's `retention` object):
    # "days" -> the day-window formula above (versioning_retention_days); "count"
    # -> bounded by retention_count versions, independent of any day window;
    # "keep_all" -> unbounded, grows for the full projection horizon. A restic
    # "tiered" policy is mapped to "days" upstream (via effective_retention_days),
    # so this field never carries "tiered" itself.
    retention_type: str = "days"
    retention_count: int = 0

@dataclass(frozen=True)
class Scenario:
    region: str = "us-east-1"
    jobs: tuple[JobInputs, ...] = ()
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
    jobs: dict[str, LineItems]  # job name -> LineItems
    monthly_total: float
    first_year_total: float
    full_restore_total: float

@dataclass
class MonthPoint:
    month: int
    storage: float
    versioning: float
    ingest: float
    rotation: float
    onetime: float
    total: float

@dataclass
class Projection:
    months: list[MonthPoint]
    steady_state_month: int
    steady_state_monthly: float

def _rate(prices: PriceTable, storage_class: str) -> float:
    if storage_class not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{storage_class}'")
    return prices.storage_gb_month[storage_class]

def effective_object_count(p: JobInputs) -> int:
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

def billed_gb(p: JobInputs, prices: PriceTable) -> float:
    if p.storage_class == "STANDARD":
        return p.size_gb
    floor = effective_object_count(p) * prices.min_billable_object_kb * _GB_PER_KB
    return max(p.size_gb, floor)

def storage_monthly(p: JobInputs, prices: PriceTable) -> float:
    return billed_gb(p, prices) * _rate(prices, p.storage_class)

def job_retention_days(p: JobInputs, scenario: Scenario) -> int:
    """The effective noncurrent-version retention (days) for a job: its own
    override when set, else the scenario-level window. Shared by the steady-state
    versioning term and the over-time ramp so the two never diverge."""
    return (p.versioning_retention_days if p.versioning_retention_days is not None
            else scenario.versioning_retention_days)

def versioning_monthly(p: JobInputs, scenario: Scenario, prices: PriceTable) -> float:
    """Steady-state old-version storage, shaped by the job's retention_type:
    "days" (and "tiered", mapped to "days" upstream) uses today's age-window
    formula; "count" is bounded by N versions, independent of any day window;
    "keep_all" has no steady state, so this returns one month's accrual rate
    (see project()'s unbounded growth for the timeline shape)."""
    if p.retention_type == "count":
        noncurrent_gb = p.size_gb * (p.change_rate_pct / 100) * p.retention_count
    elif p.retention_type == "keep_all":
        noncurrent_gb = p.size_gb * (p.change_rate_pct / 100) * p.backups_per_month
    else:
        retention = job_retention_days(p, scenario)
        noncurrent_gb = p.size_gb * (p.change_rate_pct / 100) * (
            p.backups_per_month * retention / 30)
    return noncurrent_gb * _rate(prices, p.storage_class)

def ingest_monthly(p: JobInputs, prices: PriceTable) -> float:
    new_objects_per_backup = effective_object_count(p) * (p.change_rate_pct / 100)
    return new_objects_per_backup * p.backups_per_month * prices.put_per_1k / 1000

def upfront_onetime(p: JobInputs, prices: PriceTable) -> float:
    return effective_object_count(p) * prices.put_per_1k / 1000

def cold_lockin_onetime(p: JobInputs, prices: PriceTable) -> float:
    """The minimum you pay for the INITIAL dataset in a cold class even if you
    deleted it the day after upload: billed_gb * $/GB * (min_days / 30). Zero for
    classes with no minimum-storage-duration (STANDARD)."""
    min_days = prices.min_storage_duration_days.get(p.storage_class, 0)
    if not min_days:
        return 0.0
    return billed_gb(p, prices) * _rate(prices, p.storage_class) * (min_days / 30)

def rotation_monthly(p: JobInputs, scenario: Scenario, prices: PriceTable) -> float:
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

def restore_cost(p: JobInputs, scenario: Scenario, prices: PriceTable, fraction: float) -> float:
    restored_gb = p.size_gb * fraction
    restored_objects = effective_object_count(p) * fraction
    cost = restored_gb * prices.data_transfer_out_per_gb + restored_objects * prices.get_per_1k / 1000
    per_gb = _retrieval_per_gb(p.storage_class, scenario.retrieval_tier, prices)
    if per_gb:
        cost += restored_gb * per_gb
        if p.storage_class in ("GLACIER", "DEEP_ARCHIVE"):
            cost += restored_objects * prices.retrieval_request_per_1k[scenario.retrieval_tier] / 1000
    return cost

def _line_items(p: JobInputs, scenario: Scenario, prices: PriceTable) -> LineItems:
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
    jobs = {j.name: _line_items(j, scenario, prices) for j in scenario.jobs}
    monthly = sum(li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly
                  for li in jobs.values())
    upfront = sum(li.upfront_onetime for li in jobs.values())
    annual_restore = sum(li.restore_per_event for li in jobs.values()) * scenario.restores_per_year
    first_year = 12 * monthly + upfront + annual_restore
    full_restore = sum(restore_cost(j, scenario, prices, 1.0) for j in scenario.jobs)
    return Estimate(prices.date, prices.source, prices.region, jobs,
                    monthly, first_year, full_restore)

def _versioning_fill(retention_days: int, month: int) -> float:
    """Fraction of the steady-state noncurrent history accumulated by `month`:
    linear ramp to 1.0 at the retention horizon, flat after. Zero retention
    (versioning off) -> 0.0, never a divide-by-zero. ("days"/"tiered" jobs.)"""
    if retention_days <= 0:
        return 0.0
    return min(1.0, (month * _DAYS_PER_MONTH) / retention_days)

def _count_fill(count: int, backups_per_month: float, month: int) -> float:
    """Fraction of the steady-state N-version noncurrent pool accumulated by
    `month`: ramps as backups accrue toward the count, then plateaus -- the
    count-policy analogue of _versioning_fill's day-window ramp. Zero count ->
    0.0, never a divide-by-zero. ("count" jobs.)"""
    if count <= 0:
        return 0.0
    return min(1.0, (backups_per_month * month) / count)

def _days_plateau_month(retention_days: int) -> int:
    """The first month _versioning_fill(retention_days, month) reaches 1.0, or 0
    if it never does (zero/negative retention -> always flat at 0)."""
    if not retention_days or retention_days <= 0:
        return 0
    return max(1, ceil(retention_days / _DAYS_PER_MONTH))

def _count_plateau_month(count: int, backups_per_month: float) -> int:
    """The first month _count_fill(count, backups_per_month, month) reaches 1.0,
    or 0 if it never does (zero count, or zero cadence -> always flat at 0 --
    consistent with _count_fill, which stays 0 forever in that case)."""
    if count <= 0 or backups_per_month <= 0:
        return 0
    return max(1, ceil(count / backups_per_month))

def project(scenario: Scenario, prices: PriceTable, months: int = 24) -> Projection:
    """Per-month cost trajectory: current-data storage, ingest and rotation are
    flat from month 1; the versioning term ramps per the job's retention_type
    ("days"/"tiered" ramp-then-plateau at the retention horizon; "count"
    ramp-then-plateau once N versions have accrued; "keep_all" grows linearly for
    the full horizon, no plateau); the one-time upload charge lands in month 1.
    The plateau (any month at/after steady state, excluding one-time) equals
    estimate(...).monthly_total for "days"/"tiered"/"count" jobs -- "keep_all"
    jobs never plateau, so a scenario with any keep_all job keeps growing past
    that point too."""
    if months < 1:
        raise ValueError("months must be >= 1")
    per_job = []  # (storage, steady_versioning, ingest, rotation, onetime, retention_type, retention_days, retention_count, backups_per_month)
    max_plateau_month = 0  # the LATEST month any job's own ramp reaches its steady state
    for j in scenario.jobs:
        ret = job_retention_days(j, scenario)
        if j.retention_type == "count":
            plateau = _count_plateau_month(j.retention_count, j.backups_per_month)
        elif j.retention_type == "keep_all":
            plateau = 0  # never plateaus -- doesn't bound steady_state_month
        else:
            plateau = _days_plateau_month(ret)
        max_plateau_month = max(max_plateau_month, plateau)
        per_job.append((
            storage_monthly(j, prices), versioning_monthly(j, scenario, prices),
            ingest_monthly(j, prices), rotation_monthly(j, scenario, prices),
            upfront_onetime(j, prices), j.retention_type, ret, j.retention_count,
            j.backups_per_month,
        ))
    pts: list[MonthPoint] = []
    for t in range(1, months + 1):
        s = v = ing = rot = one = 0.0
        for store, steady_ver, jing, jrot, jup, rtype, jret, rcount, bpm in per_job:
            s += store
            if rtype == "count":
                v += steady_ver * _count_fill(rcount, bpm, t)
            elif rtype == "keep_all":
                v += steady_ver * t  # unbounded: accrues every month, never plateaus
            else:
                v += steady_ver * _versioning_fill(jret, t)
            ing += jing
            rot += jrot
            if t == 1:
                one += jup
        pts.append(MonthPoint(t, s, v, ing, rot, one, s + v + ing + rot + one))
    steady_month = min(months, max_plateau_month) if max_plateau_month else 1
    steady_monthly = sum(store + sv + ing + rot
                         for store, sv, ing, rot, _up, _rt, _ret, _rc, _bpm in per_job)
    return Projection(pts, steady_month, steady_monthly)
