# tests/estimator/test_model.py — N-job cost model.
import math
import pytest
from app.estimator.model import (
    JobInputs, Scenario, estimate,
    effective_object_count, billed_gb, storage_monthly, versioning_monthly,
    ingest_monthly, upfront_onetime, rotation_monthly, restore_cost,
    effective_retention_days,
)

def J(size_gb, file_count, storage_class, *, name="job", engine="versioned", **kw):
    return JobInputs(name=name, engine=engine, size_gb=size_gb,
                     file_count=file_count, storage_class=storage_class, **kw)

def _scn(*jobs, **kw):
    return Scenario(region="us-east-1", jobs=tuple(jobs), **kw)

# --- per-unit math (ported; the round test table makes every value hand-computable) ---

def test_effective_object_count_uses_file_count_without_packing():
    assert effective_object_count(J(100, 50000, "DEEP_ARCHIVE", packing=False)) == 50000

def test_effective_object_count_packs_into_members():
    assert effective_object_count(J(100, 50000, "DEEP_ARCHIVE", packing=True, pack_member_gb=5)) == 20  # ceil(100/5)

def test_billed_gb_standard_has_no_floor(prices):
    assert billed_gb(J(20, 5, "STANDARD"), prices) == 20

def test_billed_gb_cold_applies_128kb_floor(prices):
    # 10000 tiny objects * 128KB = ~1.2207 GB, above the 0.01 GB actual size
    assert math.isclose(billed_gb(J(0.01, 10000, "DEEP_ARCHIVE"), prices),
                        10000 * 128 / (1024 * 1024), rel_tol=1e-9)

def test_storage_monthly_warm(prices):
    assert math.isclose(storage_monthly(J(20, 5, "STANDARD"), prices), 20 * 0.02)

def test_versioning_monthly_scales_with_retention(prices):
    p = J(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10)
    s = _scn(p, versioning_retention_days=30)
    # 20 * 0.10 * (30 * 30 / 30) = 60 GB noncurrent; * 0.02 = 1.20
    assert math.isclose(versioning_monthly(p, s, prices), 1.20)

def test_ingest_monthly_counts_changed_objects(prices):
    p = J(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10)
    # new objects/backup = 5 * 0.10 = 0.5; * 30 backups = 15; * 0.005/1000
    assert math.isclose(ingest_monthly(p, prices), 15 * 0.005 / 1000)

def test_upfront_onetime_is_one_put_per_object(prices):
    assert math.isclose(upfront_onetime(J(2000, 50000, "DEEP_ARCHIVE"), prices), 50000 * 0.005 / 1000)  # 0.25

def test_rotation_zero_for_warm_class(prices):
    p = J(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10)
    assert rotation_monthly(p, _scn(p), prices) == 0.0

def test_rotation_charges_min_duration_for_cold_churn(prices):
    p = J(2000, 50000, "DEEP_ARCHIVE", backups_per_month=4, change_rate_pct=1)
    # rotated/mo = 2000 * 0.01 * 4 = 80 GB; * 0.001 * (180/30) = 0.48
    assert math.isclose(rotation_monthly(p, _scn(p), prices), 0.48)

def test_restore_warm_class_has_no_retrieval_fee(prices):
    p = J(10, 100, "STANDARD")
    s = _scn(p, restore_fraction=1.0)
    expected = 10 * 0.10 + 100 * 0.0004 / 1000  # egress + GETs only
    assert math.isclose(restore_cost(p, s, prices, 1.0), expected)

def test_restore_deep_archive_bulk_includes_retrieval_and_egress(prices):
    p = J(2000, 50000, "DEEP_ARCHIVE")
    s = _scn(p, restore_fraction=1.0, retrieval_tier="Bulk")
    # egress 200; GET 0.02; retrieval 2000*0.0025=5; retrieval req 50000*0.025/1000=1.25 => 206.27
    assert math.isclose(restore_cost(p, s, prices, 1.0), 206.27)

def test_restore_scales_with_fraction(prices):
    p = J(2000, 50000, "DEEP_ARCHIVE")
    s = _scn(p, retrieval_tier="Bulk")
    assert math.isclose(restore_cost(p, s, prices, 0.5), 206.27 / 2)

def test_restore_standard_ia_uses_single_rate_and_no_request_fee(prices):
    # STANDARD_IA: retrieval_per_gb table has only {"Standard": 0.01}, so any tier
    # falls back to next(iter(...)); no per-1k retrieval *request* fee applies.
    p = J(10, 100, "STANDARD_IA")
    s = _scn(p, restore_fraction=1.0)
    expected = 10 * 0.10 + 100 * 0.0004 / 1000 + 10 * 0.01
    assert math.isclose(restore_cost(p, s, prices, 1.0), expected)

def test_unknown_retrieval_tier_raises(prices):
    p = J(2000, 50000, "DEEP_ARCHIVE")
    with pytest.raises(ValueError) as e:
        restore_cost(p, _scn(p, retrieval_tier="Warp"), prices, 1.0)
    assert "Warp" in str(e.value)

def test_storage_monthly_raises_for_unknown_storage_class(prices):
    with pytest.raises(ValueError):
        storage_monthly(J(10, 5, "NEBULA"), prices)

def test_versioning_monthly_uses_per_job_retention_override(prices):
    p = J(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10, versioning_retention_days=60)
    s = _scn(p, versioning_retention_days=30)
    # uses 60, not 30: 20 * 0.10 * (30 * 60 / 30) = 120 GB; * 0.02 = 2.40
    assert math.isclose(versioning_monthly(p, s, prices), 2.40)

def test_versioning_monthly_falls_back_to_scenario_retention(prices):
    p = J(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10)
    s = _scn(p, versioning_retention_days=30)
    assert math.isclose(versioning_monthly(p, s, prices), 1.20)

def test_effective_retention_days_reaches_furthest_tier():
    assert effective_retention_days(keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=6) == 180
    assert effective_retention_days(keep_last=1, keep_daily=7, keep_weekly=4, keep_monthly=0) == 28
    assert effective_retention_days(keep_last=5) == 5
    assert effective_retention_days() == 1

# --- N-job scenario / estimate ---

def test_empty_scenario_zero_totals(prices):
    e = estimate(_scn(), prices)
    assert e.jobs == {} and e.monthly_total == 0
    assert e.first_year_total == 0 and e.full_restore_total == 0

def test_estimate_is_stamped_and_keyed_by_job_names(prices):
    a = J(20, 5, "STANDARD", name="appdata", engine="versioned")
    m = J(2000, 50000, "DEEP_ARCHIVE", name="movies", engine="archive")
    e = estimate(_scn(a, m), prices)
    assert e.price_date == "2099-01-01"
    assert set(e.jobs) == {"appdata", "movies"}
    assert e.monthly_total >= 0

def test_two_jobs_sum(prices):
    a = J(20, 5, "STANDARD", name="appdata", engine="versioned", backups_per_month=30, change_rate_pct=10)
    m = J(2000, 50000, "DEEP_ARCHIVE", name="movies", engine="archive", backups_per_month=4, change_rate_pct=1)
    e = estimate(_scn(a, m), prices)
    assert set(e.jobs) == {"appdata", "movies"}
    per = e.jobs
    assert math.isclose(
        e.monthly_total,
        sum(li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly for li in per.values()))
    # the archive job is cold -> rotation > 0; the versioned job ingests
    assert per["movies"].rotation_monthly > 0
    assert per["appdata"].ingest_monthly > 0 and per["appdata"].upfront_onetime > 0

def test_per_job_retention_override_used(prices):
    a = J(10, 5, "STANDARD", name="a", engine="versioned", versioning_retention_days=90, change_rate_pct=10)
    e = estimate(_scn(a), prices)
    assert e.jobs["a"].versioning > 0

def test_first_year_adds_upfront_and_restores(prices):
    a = J(20, 5, "STANDARD", name="appdata")
    m = J(2000, 50000, "DEEP_ARCHIVE", name="movies", engine="archive")
    e = estimate(_scn(a, m), prices)
    assert e.first_year_total > e.monthly_total
    assert e.full_restore_total > 0

def test_full_restore_total_is_independent_of_restores_per_year(prices):
    m = J(2000, 50000, "DEEP_ARCHIVE", name="movies", engine="archive")
    e = estimate(_scn(m, restores_per_year=0), prices)
    assert e.full_restore_total > 0

def test_public_api_importable():
    from app.estimator import estimate, Scenario, JobInputs, Estimate, load_prices  # noqa: F401
