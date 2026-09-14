# tests/estimator/test_audit_fixes.py — locks in the correctness fixes from the
# cost-estimator audit: keep_all unbounded growth, ramped first-year, count-beyond-
# horizon, rotation early-deletion shortfall, per-class request/GET rates, restored-
# copy storage, and the projection_bundle comparison overlays for count/keep_all.
import pytest
from app.estimator.prices import load_prices
from app.estimator.model import (
    JobInputs, Scenario, estimate, project, restore_cost, rotation_monthly,
)
from app.gui import estimate_io

PT = load_prices("us-east-1")


def _job(**kw):
    base = dict(name="j", engine="versioned", size_gb=100.0, file_count=1000,
                storage_class="STANDARD", backups_per_month=30.0, change_rate_pct=10.0)
    base.update(kw)
    return JobInputs(**base)


# --- C1: keep_all never settles -------------------------------------------------
def test_keep_all_projection_is_unbounded_and_grows():
    proj = project(Scenario(jobs=(_job(retention_type="keep_all"),)), PT, months=24)
    assert proj.unbounded is True
    assert proj.months[23].total > proj.months[11].total > proj.months[0].total
    # a days job DOES settle and is not flagged unbounded
    jd = _job(retention_type="days", versioning_retention_days=90)
    assert project(Scenario(jobs=(jd,)), PT, 24).unbounded is False


def test_first_year_uses_ramped_path_not_flat_12x():
    # keep_all grows -> first year exceeds 12x the month-1 monthly
    ek = estimate(Scenario(jobs=(_job(retention_type="keep_all"),)), PT)
    assert ek.first_year_total > 12 * ek.monthly_total
    # a days job ramps UP to steady -> first year is LESS than 12x the steady monthly
    ed = estimate(Scenario(jobs=(_job(retention_type="days", versioning_retention_days=180),)), PT)
    assert ed.first_year_total < 12 * ed.monthly_total


# --- M10: a count policy that can't accrue N within the horizon ------------------
def test_count_beyond_horizon_reports_true_plateau_month():
    j = _job(retention_type="count", retention_count=1000, backups_per_month=30)
    proj = project(Scenario(jobs=(j,)), PT, months=24)
    assert proj.steady_state_month > 24            # ceil(1000/30)=34, not clamped
    assert proj.months[23].total < proj.steady_state_monthly  # not yet reached


# --- I2: rotation charges only the early-deletion shortfall ----------------------
def test_rotation_is_zero_when_retention_exceeds_minimum():
    j = _job(storage_class="DEEP_ARCHIVE", retention_type="days",
             versioning_retention_days=180, change_rate_pct=10)
    assert rotation_monthly(j, Scenario(jobs=(j,)), PT) == 0.0        # 180 == min
    j2 = _job(storage_class="DEEP_ARCHIVE", retention_type="keep_all", change_rate_pct=10)
    assert rotation_monthly(j2, Scenario(jobs=(j2,)), PT) == 0.0      # never deleted


# --- Rates: per-class retrieval-request + GET -----------------------------------
def test_deep_archive_standard_restore_request_is_10c_per_1k():
    j = _job(storage_class="DEEP_ARCHIVE", size_gb=1.0, file_count=1_000_000)
    hi = restore_cost(j, Scenario(jobs=(j,), retrieval_tier="Standard", restore_copy_days=0), PT, 1.0)
    lo = restore_cost(j, Scenario(jobs=(j,), retrieval_tier="Bulk", restore_copy_days=0), PT, 1.0)
    # Standard vs Bulk differ in BOTH the per-request fee ($0.10 vs $0.025 /1k) and
    # the per-GB retrieval rate ($0.02 vs $0.0025): 1M objects + 1 GB.
    req_diff = 1_000_000 * (0.10 - 0.025) / 1000            # $75.00
    pergb_diff = 1.0 * (0.02 - 0.0025)                       # $0.0175
    assert (hi - lo) == pytest.approx(req_diff + pergb_diff, rel=1e-6)


def test_standard_ia_restore_uses_higher_ia_get_rate():
    # STANDARD_IA GET is $0.001/1k, not the flat STANDARD $0.0004/1k.
    j = _job(storage_class="STANDARD_IA", size_gb=1.0, file_count=1_000_000)
    got = restore_cost(j, Scenario(jobs=(j,)), PT, 1.0)
    # egress(1GB, all free-tier) 0 + IA retrieval 1*0.01 + GET 1e6*0.001/1000 = 0.01 + 1.0
    assert got == pytest.approx(0.01 + 1_000_000 * 0.001 / 1000, rel=1e-6)


# --- Restored-copy staging storage during a cold thaw ---------------------------
def test_restore_cold_includes_temporary_standard_copy():
    j = _job(storage_class="DEEP_ARCHIVE", size_gb=1000.0, file_count=1000)
    with_copy = restore_cost(j, Scenario(jobs=(j,), retrieval_tier="Bulk", restore_copy_days=7), PT, 1.0)
    without = restore_cost(j, Scenario(jobs=(j,), retrieval_tier="Bulk", restore_copy_days=0), PT, 1.0)
    assert (with_copy - without) == pytest.approx(1000 * PT.storage_gb_month["STANDARD"] * 7 / 30, rel=1e-6)


# --- Egress free tier -----------------------------------------------------------
def test_restore_egress_applies_free_tier():
    j = _job(storage_class="STANDARD", size_gb=80.0, file_count=100)   # under 100GB free
    got = restore_cost(j, Scenario(jobs=(j,)), PT, 1.0)
    assert got == pytest.approx(100 * PT.get_rate("STANDARD") / 1000, rel=1e-6)  # only GETs, no egress


# --- I4: comparison overlays actually neutralize count/keep_all -----------------
def test_projection_bundle_no_versioning_zeroes_count_and_keep_all():
    for rt, extra in (("count", {"retention_count": 12}), ("keep_all", {})):
        j = _job(retention_type=rt, change_rate_pct=10, **extra)
        b = estimate_io.projection_bundle(Scenario(jobs=(j,)), PT, months=24)
        prim = b["primary"]["months"][-1]["versioning"]
        nov = b["comparison"]["no_versioning"]["months"][-1]["versioning"]
        assert prim > 0                       # the job DOES have versioning cost
        assert nov == pytest.approx(0.0)      # "no versioning" overlay truly zeroes it
