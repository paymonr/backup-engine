import pytest
from app.estimator.schedule import backups_per_month

def test_daily():
    assert backups_per_month("0 3 * * *") == pytest.approx(30.4, rel=0.02)

def test_weekly_single_dow():
    assert backups_per_month("0 4 * * 0") == pytest.approx(4.345, rel=0.02)

def test_monthly_single_dom():
    assert backups_per_month("0 3 1 * *") == pytest.approx(1.0, rel=0.02)

def test_hourly():
    assert backups_per_month("0 * * * *") == pytest.approx(730, rel=0.02)

def test_step_minutes():
    # every 15 min, every day = 96/day
    assert backups_per_month("*/15 * * * *") == pytest.approx(96 * 30.4, rel=0.02)

def test_twice_daily_list():
    assert backups_per_month("0 3,15 * * *") == pytest.approx(2 * 30.4, rel=0.02)

def test_weekdays():
    # Mon-Fri once/day ~ 5 * 4.345
    assert backups_per_month("0 3 * * 1-5") == pytest.approx(5 * 4.345, rel=0.03)

def test_unknown_or_malformed_defaults_daily():
    assert backups_per_month("not a cron") == pytest.approx(30.4, rel=0.02)
    assert backups_per_month("") == pytest.approx(30.4, rel=0.02)
