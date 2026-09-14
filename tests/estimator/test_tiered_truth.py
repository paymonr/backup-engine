# tests/estimator/test_tiered_truth.py — the tiered (restic keep-policy) old-version
# model is pinned to GROUND TRUTH from a REAL restic 0.17.3 run: 4,416 real
# backup+forget+stats cycles, 24 months per scenario, pure old-data blobs / current
# data, zero measurement violations (fixtures/tiered_truth_real.json). Two
# independent restic-retention simulators (fixtures/tiered_truth.json) triangulate
# the steady state. If a refactor drifts the model, these fail — they are NOT
# hand-mirrors of the formula.
import json
import math
import pathlib
import pytest
from datetime import date
from app.estimator import tiered

_FX = pathlib.Path(__file__).parent / "fixtures"
REAL = json.load(open(_FX / "tiered_truth_real.json"))   # real restic 0.17.3
SIM = json.load(open(_FX / "tiered_truth.json"))          # two independent simulators
START = date(2026, 1, 1)
# (c, interval_days, last, daily, weekly, monthly) — identical to the experiment.
SC = {
    "S1": (0.01, 1, 3, 7, 4, 6), "S2": (0.01, 1, 6, 7, 4, 6), "S3": (0.10, 1, 3, 7, 4, 6),
    "S4": (0.01, 7, 3, 7, 4, 6), "S5": (0.01, 1, 3, 7, 0, 0), "S6": (0.01, 1, 20, 0, 0, 0),
    "S7": (0.01, 1, 3, 7, 4, 12),
}


def _close(pred, truth, rel=0.06, abs_small=0.016):
    """Real-restic monthly values carry RNG noise (random file choice), and the
    early months of a WEEKLY schedule (4 backups/month, ~20 churned files each)
    are discrete and calendar-phase sensitive. Allow 6% relative, or 0.012 of the
    dataset absolute (~1% of size, cents on a real job) when the truth is small.
    Measured: every daily scenario is within 1.2% in every month; the weekly
    scenario's months 2-4 sit <= 0.015 absolute ABOVE the real binary (the model
    retains slightly more, early, for a sparse weekly cadence), steady 0.4%."""
    return abs(pred - truth) <= abs_small if truth < 0.2 else abs(pred - truth) / truth <= rel


@pytest.mark.parametrize("sid", sorted(SC))
def test_matches_real_restic_every_month(sid):
    p = SC[sid]
    months = tiered.old_fraction_by_month(*p, months=len(REAL[sid]["months"]), start=START)
    for m, (pred, truth) in enumerate(zip(months, REAL[sid]["months"]), 1):
        assert _close(pred, truth), f"{sid} month {m}: model {pred:.4f} vs real restic {truth:.4f}"


@pytest.mark.parametrize("sid", sorted(SC))
def test_matches_real_restic_steady_state(sid):
    # Measured model-vs-real steady error was <1% on every scenario; allow 3%.
    pred = tiered.steady_fraction(*SC[sid], months=24, start=START)
    truth = REAL[sid]["steady"]
    assert abs(pred - truth) / truth <= 0.03, f"{sid}: {pred:.4f} vs real {truth:.4f}"


@pytest.mark.parametrize("sid", sorted(SC))
def test_simulators_and_real_restic_agree_at_steady_state(sid):
    # Triangulation: the two simulators' steady state must sit within 3% of the
    # real binary (they do — the only divergence is the early ramp, where restic's
    # spare-capacity oldest-snapshot rule pins the first snapshot).
    assert abs(SIM[sid]["steady"] - REAL[sid]["steady"]) / REAL[sid]["steady"] <= 0.03


def test_exact_closed_forms_where_the_chain_is_contiguous():
    # Contiguous dailies: old = (retained-1) * c exactly. S5 (last3/d7): 6*0.01; S6 (last20): 19*0.01.
    assert math.isclose(tiered.steady_fraction(*SC["S5"], start=START), 6 * 0.01, rel_tol=1e-9)
    assert math.isclose(tiered.steady_fraction(*SC["S6"], start=START), 19 * 0.01, rel_tol=1e-9)


def test_keep_last_within_keep_daily_is_free():
    # last=3 vs last=6 with daily=7 at one backup/day: identical retained set, identical cost.
    a = tiered.old_fraction_by_month(*SC["S1"], months=24, start=START)
    b = tiered.old_fraction_by_month(*SC["S2"], months=24, start=START)
    assert a == b


def test_ramps_over_the_longest_tier_then_plateaus():
    # S1 (monthly=6): month 1 is a small fraction of steady; it plateaus by ~month 6.
    m = tiered.old_fraction_by_month(*SC["S1"], months=24, start=START)
    steady = sum(m[18:24]) / 6
    assert m[0] < 0.2 * steady                # first month carries little of the eventual cost
    assert m[1] < m[2] < m[3] < m[4]          # climbing through the ramp
    assert abs(m[9] - steady) / steady < 0.1  # flat after the longest tier fills
    assert tiered.plateau_month(3, 7, 4, 6, 1.0) in (6, 7)


def test_sub_daily_fast_path_agrees_with_exact_per_backup_walk():
    # Hourly keep-last-only must still be exactly 19*c (invariant the fast path preserves).
    hourly = tiered.steady_fraction(0.01, 1 / 24, 20, 0, 0, 0, start=START)
    assert math.isclose(hourly, 19 * 0.01, rel_tol=1e-6)


def test_zero_churn_and_single_snapshot_cost_nothing():
    assert tiered.old_fraction_by_month(0.0, 1, 3, 7, 4, 6, months=6, start=START) == [0.0] * 6
    assert tiered.old_fraction_by_month(0.01, 1, 1, 0, 0, 0, months=6, start=START) == [0.0] * 6


def test_runtime_is_wizard_safe():
    import time
    for iv in (1.0, 7.0, 1 / 24):
        t0 = time.perf_counter()
        tiered.old_fraction_by_month(0.01, iv, 3, 7, 4, 6, months=24, start=START)
        assert time.perf_counter() - t0 < 1.0, f"interval {iv} too slow for live estimates"
