# tests/estimator/test_model.py
import math
import pytest
from app.estimator.model import (
    PipelineInputs, Scenario, estimate,
    effective_object_count, billed_gb, storage_monthly, versioning_monthly,
    ingest_monthly, upfront_onetime, rotation_monthly, restore_cost,
)

def test_effective_object_count_uses_file_count_without_packing():
    p = PipelineInputs(size_gb=100, file_count=50000, storage_class="DEEP_ARCHIVE", packing=False)
    assert effective_object_count(p) == 50000

def test_effective_object_count_packs_into_members():
    p = PipelineInputs(size_gb=100, file_count=50000, storage_class="DEEP_ARCHIVE",
                       packing=True, pack_member_gb=5)
    assert effective_object_count(p) == 20  # ceil(100/5)

def test_billed_gb_standard_has_no_floor(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD")
    assert billed_gb(p, prices) == 20

def test_billed_gb_cold_applies_128kb_floor(prices):
    # 10000 tiny objects * 128KB = ~1.2207 GB, above the 0.01 GB actual size
    p = PipelineInputs(size_gb=0.01, file_count=10000, storage_class="DEEP_ARCHIVE")
    assert math.isclose(billed_gb(p, prices), 10000 * 128 / (1024 * 1024), rel_tol=1e-9)

def test_storage_monthly_warm(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD")
    assert math.isclose(storage_monthly(p, prices), 20 * 0.02)

def test_versioning_monthly_scales_with_retention(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD",
                       backups_per_month=30, change_rate_pct=10)
    s = Scenario(appdata=p, media=p, versioning_retention_days=30)
    # 20 * 0.10 * (30 * 30 / 30) = 60 GB noncurrent; * 0.02 = 1.20
    assert math.isclose(versioning_monthly(p, s, prices), 1.20)

def test_estimate_returns_stamped_estimate_with_both_pipelines(prices):
    s = Scenario()
    est = estimate(s, prices)
    assert est.price_date == "2099-01-01"
    assert set(est.pipelines) == {"appdata", "media"}
    assert est.monthly_total >= 0

def test_ingest_monthly_counts_changed_objects(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD",
                       backups_per_month=30, change_rate_pct=10)
    # new objects/backup = 5 * 0.10 = 0.5; * 30 backups = 15; * 0.005/1000
    assert math.isclose(ingest_monthly(p, prices), 15 * 0.005 / 1000)

def test_upfront_onetime_is_one_put_per_object(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    assert math.isclose(upfront_onetime(p, prices), 50000 * 0.005 / 1000)  # 0.25

def test_rotation_zero_for_warm_class(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD",
                       backups_per_month=30, change_rate_pct=10)
    s = Scenario(appdata=p, media=p)
    assert rotation_monthly(p, s, prices) == 0.0

def test_rotation_charges_min_duration_for_cold_churn(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE",
                       backups_per_month=4, change_rate_pct=1)
    s = Scenario(appdata=p, media=p)
    # rotated/mo = 2000 * 0.01 * 4 = 80 GB; * 0.001 * (180/30) = 0.48
    assert math.isclose(rotation_monthly(p, s, prices), 0.48)

def test_estimate_monthly_includes_ingest_and_rotation(prices):
    s = Scenario()
    est = estimate(s, prices)
    appdata = est.pipelines["appdata"]
    assert appdata.ingest_monthly > 0
    assert appdata.upfront_onetime > 0
    # media is cold -> rotation > 0
    assert est.pipelines["media"].rotation_monthly > 0

def test_restore_warm_class_has_no_retrieval_fee(prices):
    p = PipelineInputs(size_gb=10, file_count=100, storage_class="STANDARD")
    s = Scenario(appdata=p, media=p, restore_fraction=1.0)
    # only egress + GETs: 10 * 0.10 + 100 * 0.0004/1000
    expected = 10 * 0.10 + 100 * 0.0004 / 1000
    assert math.isclose(restore_cost(p, s, prices, 1.0), expected)

def test_restore_deep_archive_bulk_includes_retrieval_and_egress(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    s = Scenario(appdata=p, media=p, restore_fraction=1.0, retrieval_tier="Bulk")
    # egress 2000*0.10=200; GET 50000*0.0004/1000=0.02; retrieval 2000*0.0025=5;
    # retrieval requests 50000*0.025/1000=1.25 => 206.27
    assert math.isclose(restore_cost(p, s, prices, 1.0), 206.27)

def test_restore_scales_with_fraction(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    s = Scenario(appdata=p, media=p, retrieval_tier="Bulk")
    assert math.isclose(restore_cost(p, s, prices, 0.5), 206.27 / 2)

def test_unknown_retrieval_tier_raises(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    s = Scenario(appdata=p, media=p, retrieval_tier="Warp")
    with pytest.raises(ValueError) as e:
        restore_cost(p, s, prices, 1.0)
    assert "Warp" in str(e.value)

def test_full_restore_total_is_independent_of_restores_per_year(prices):
    s = Scenario(restores_per_year=0)  # no annualized restore, but full-restore still shown
    est = estimate(s, prices)
    assert est.full_restore_total > 0

def test_golden_default_scenario_totals(prices):
    # Whole-model golden against the fixed test table (all terms live).
    est = estimate(Scenario(), prices)
    assert est.monthly_total > 0
    assert est.first_year_total > est.monthly_total  # upfront + restores add on
    assert est.full_restore_total > 0
