import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.engine import cron

UTC = timezone.utc


# --- next_after ------------------------------------------------------------

def test_next_after_daily_strictly_after():
    after = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)
    assert cron.next_after("0 5 * * *", after, UTC) == datetime(2026, 9, 15, 5, 0, tzinfo=UTC)
    # exactly on the fire minute -> the NEXT one (strictly after)
    on = datetime(2026, 9, 15, 5, 0, tzinfo=UTC)
    assert cron.next_after("0 5 * * *", on, UTC) == datetime(2026, 9, 16, 5, 0, tzinfo=UTC)


def test_next_after_weekly_sunday():
    # 2026-09-15 is a Tuesday; next Sunday is 2026-09-20.
    after = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)
    assert cron.next_after("0 4 * * 0", after, UTC) == datetime(2026, 9, 20, 4, 0, tzinfo=UTC)


def test_next_after_every_30_minutes():
    assert cron.next_after("*/30 * * * *", datetime(2026, 9, 15, 4, 5, tzinfo=UTC), UTC) == \
        datetime(2026, 9, 15, 4, 30, tzinfo=UTC)
    assert cron.next_after("*/30 * * * *", datetime(2026, 9, 15, 4, 30, tzinfo=UTC), UTC) == \
        datetime(2026, 9, 15, 5, 0, tzinfo=UTC)


def test_next_after_accepts_day_and_month_names():
    after = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    assert cron.next_after("0 4 * * SUN", after, UTC) == datetime(2026, 9, 20, 4, 0, tzinfo=UTC)


def test_next_after_dow_7_is_sunday():
    after = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    assert cron.next_after("0 4 * * 7", after, UTC) == datetime(2026, 9, 20, 4, 0, tzinfo=UTC)


# --- DST transition vectors ------------------------------------------------

def test_spring_forward_gap_is_skipped():
    # US spring-forward 2026 = 2026-03-08, clocks jump 02:00 -> 03:00, so 02:30 does not exist.
    tz = ZoneInfo("America/New_York")
    after = datetime(2026, 3, 8, 1, 0, tzinfo=tz)
    nxt = cron.next_after("30 2 * * *", after, tz)
    assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 3, 9, 2, 30)


def test_fall_back_ambiguous_fires_once():
    # US fall-back 2026 = 2026-11-01, clocks fall 02:00 -> 01:00, so 01:30 occurs twice.
    tz = ZoneInfo("America/New_York")
    after = datetime(2026, 11, 1, 0, 0, tzinfo=tz)
    nxt = cron.next_after("30 1 * * *", after, tz)
    assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 11, 1, 1, 30)
    assert nxt > after


def test_next_after_is_monotonic_in_utc_across_spring_forward():
    tz = ZoneInfo("America/New_York")
    # hourly job; the wall hour 02:00 is skipped but UTC still advances by one hour each fire
    after = datetime(2026, 3, 8, 0, 30, tzinfo=tz)
    n1 = cron.next_after("0 * * * *", after, tz)
    n2 = cron.next_after("0 * * * *", n1, tz)
    assert n2 > n1
    assert (n2.astimezone(UTC) - n1.astimezone(UTC)) == timedelta(hours=1)


# --- describe --------------------------------------------------------------

@pytest.mark.parametrize("expr,text", [
    ("0 5 * * *", "Every day at 05:00"),
    ("0 4 * * 0", "Every Sunday at 04:00"),
    ("30 2 * * 1,4", "Mondays and Thursdays at 02:30"),
    ("15 * * * *", "Every hour at :15"),
    ("0 */6 * * *", "Every 6 hours"),
    ("0 3 1 * *", "On the 1st of every month at 03:00"),
])
def test_describe_phrases(expr, text):
    assert cron.describe(expr) == text


def test_describe_falls_back_to_mono_cron_line():
    assert cron.describe("*/30 * * * *") == "cron */30 * * * *"
    # a feature we can't compute still degrades to the mono line, never raises
    assert cron.describe("@daily") == "cron @daily"


# --- dow_word (the {dow} board template value, §7.4) -----------------------

@pytest.mark.parametrize("expr,word", [
    ("0 5 * * *", "day"),
    ("0 4 * * 0", "Sunday"),
    ("0 4 * * 7", "Sunday"),
    ("30 2 * * 1,4", "Monday and Thursday"),
])
def test_dow_word(expr, word):
    assert cron.dow_word(expr) == word


# --- Vixie day semantics ---------------------------------------------------

def test_vixie_dom_and_dow_are_ored_when_both_restricted():
    # 0 0 13 * 5 -> the 13th OR any Friday
    spec = cron.parse("0 0 13 * 5")
    assert cron.matches(spec, datetime(2026, 9, 13, 0, 0))   # the 13th (a Sunday)
    assert cron.matches(spec, datetime(2026, 9, 18, 0, 0))   # a Friday, not the 13th
    assert not cron.matches(spec, datetime(2026, 9, 14, 0, 0))  # Monday, not the 13th


def test_dom_only_restriction_is_anded_with_star_dow():
    spec = cron.parse("0 0 1 * *")
    assert cron.matches(spec, datetime(2026, 9, 1, 0, 0))
    assert not cron.matches(spec, datetime(2026, 9, 2, 0, 0))


# --- parse errors ----------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "@daily", "0 0 * * MON#2", "0 0 L * *", "0 0 * * ?", "0 0 1W * *", "0 5 * *", "0 5 * * * *",
])
def test_parse_rejects_unsupported_features(bad):
    with pytest.raises(cron.CronError):
        cron.parse(bad)


# --- local_tz --------------------------------------------------------------

def test_local_tz_known(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert cron.local_tz() == ZoneInfo("America/New_York")


def test_local_tz_default_is_utc(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert cron.local_tz().utcoffset(datetime(2026, 1, 1)) == timedelta(0)


def test_local_tz_unknown_falls_back_to_utc_with_one_warn(monkeypatch, caplog):
    monkeypatch.setenv("TZ", "Not/AZone")
    cron._warned_tz.clear()
    with caplog.at_level(logging.WARNING, logger="app.engine.cron"):
        tz = cron.local_tz()
    assert tz.utcoffset(datetime(2026, 1, 1)) == timedelta(0)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "Not/AZone" in warns[0].getMessage()
