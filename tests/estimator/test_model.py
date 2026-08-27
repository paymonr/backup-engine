# tests/estimator/test_model.py
import math
import pytest
from app.estimator.model import (
    PipelineInputs, Scenario, estimate,
    effective_object_count, billed_gb, storage_monthly, versioning_monthly,
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
