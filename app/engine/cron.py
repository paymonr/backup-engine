# app/engine/cron.py — a tiny, dependency-free Vixie-cron evaluator (spec §7.3).
#
# Pure stdlib on purpose: the container ships no croniter (the image installs only
# apprise/flask/waitress), and the spec fixes the behaviour precisely — naive
# wall-clock stepping in a chosen zone, DST-gap skipping, Vixie dom/dow OR — so a
# ~150-line evaluator we control is both the sanctioned choice and the exact one.
from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_log = logging.getLogger(__name__)

_MONTHS = {n: i for i, n in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_DOWS = {n: i for i, n in enumerate(
    ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"], start=0)}
_DAY_NAMES = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
              4: "Thursday", 5: "Friday", 6: "Saturday"}

# per-field inclusive integer bounds: minute, hour, dom, month, dow
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # dow upper 7 (== Sunday) allowed pre-normalize

_warned_tz: set[str] = set()


class CronError(ValueError):
    """The schedule uses a cron feature this app can't compute."""


@dataclasses.dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_star: bool
    dow_star: bool
    text: str


def _parse_field(raw: str, idx: int, names: dict[str, int] | None) -> tuple[frozenset[int], bool]:
    lo, hi = _BOUNDS[idx]
    star = raw == "*"
    out: set[int] = set()
    for part in raw.split(","):
        if not part:
            raise CronError("schedule uses a cron feature this app can't compute")
        base, _, step_s = part.partition("/")
        if "/" in part and step_s == "":
            raise CronError("schedule uses a cron feature this app can't compute")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError("schedule uses a cron feature this app can't compute")
            step = int(step_s)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            start, end = _num(a_s, names), _num(b_s, names)
        else:
            v = _num(base, names)
            # `A/S` means "from A to the field max, every S"; a bare `A` is just A.
            start = v
            end = hi if step_s else v
        if start < lo or end > hi or start > end:
            raise CronError("schedule uses a cron feature this app can't compute")
        out.update(range(start, end + 1, step))
    return frozenset(out), star


def _num(tok: str, names: dict[str, int] | None) -> int:
    tok = tok.strip()
    if names is not None and tok.upper() in names:
        return names[tok.upper()]
    if not tok.lstrip("-").isdigit() or tok.startswith("-"):
        raise CronError("schedule uses a cron feature this app can't compute")
    return int(tok)


def parse(expr: str) -> CronSpec:
    if not isinstance(expr, str):
        raise CronError("schedule uses a cron feature this app can't compute")
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("schedule uses a cron feature this app can't compute")
    minutes, _ = _parse_field(fields[0], 0, None)
    hours, _ = _parse_field(fields[1], 1, None)
    doms, dom_star = _parse_field(fields[2], 2, None)
    months, _ = _parse_field(fields[3], 3, _MONTHS)
    dows, dow_star = _parse_field(fields[4], 4, _DOWS)
    # 7 == Sunday == 0
    dows = frozenset(0 if d == 7 else d for d in dows)
    if not (minutes and hours and doms and months and dows):
        raise CronError("schedule uses a cron feature this app can't compute")
    return CronSpec(minutes, hours, doms, months, dows, dom_star, dow_star, expr)


def matches(spec: CronSpec, local_dt: datetime) -> bool:
    if local_dt.minute not in spec.minutes:
        return False
    if local_dt.hour not in spec.hours:
        return False
    if local_dt.month not in spec.months:
        return False
    return _day_matches(spec, local_dt)


def _day_matches(spec: CronSpec, dt: datetime) -> bool:
    dom_ok = dt.day in spec.doms
    dow = (dt.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
    dow_ok = dow in spec.dows
    # Vixie: when BOTH day-of-month and day-of-week are restricted, either matches.
    if not spec.dom_star and not spec.dow_star:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _localize(naive: datetime, tz: tzinfo) -> datetime | None:
    """Attach tz to a naive wall-clock datetime; None if that wall time does not
    exist (the spring-forward gap). Ambiguous fall-back times resolve to their
    first (fold=0) occurrence, which is a real instant, so they fire once."""
    aware = naive.replace(tzinfo=tz)
    roundtrip = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        return None
    return aware


def _first_of_next_month(t: datetime) -> datetime:
    if t.month == 12:
        return t.replace(year=t.year + 1, month=1, day=1, hour=0, minute=0)
    return t.replace(month=t.month + 1, day=1, hour=0, minute=0)


def next_after(expr: str, after: datetime, tz: tzinfo | None = None) -> datetime:
    """First scheduled fire strictly after `after`, returned tz-aware in `tz`."""
    spec = parse(expr)
    if tz is None:
        tz = local_tz()
    if after.tzinfo is not None:
        after_local = after.astimezone(tz)
    else:
        after_local = after.replace(tzinfo=tz)
    # naive wall clock, seconds zeroed, one minute past `after`
    t = after_local.replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
    for _ in range(100_000):
        if t.month not in spec.months:
            t = _first_of_next_month(t)
            continue
        if not _day_matches(spec, t):
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if t.hour not in spec.hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in spec.minutes:
            t = t + timedelta(minutes=1)
            continue
        aware = _localize(t, tz)
        if aware is None:  # nonexistent wall time (spring-forward gap) -> step past it
            t = t + timedelta(minutes=1)
            continue
        return aware
    raise CronError("schedule never fires")


def local_tz() -> tzinfo:
    name = os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name not in _warned_tz:
            _warned_tz.add(name)
            _log.warning("TZ %r is not a known timezone; falling back to UTC", name)
        return timezone.utc


# --- human-readable rendering ---------------------------------------------

def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _join_and(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _hhmm(spec: CronSpec) -> str:
    return f"{next(iter(spec.hours)):02d}:{next(iter(spec.minutes)):02d}"


def describe(expr: str) -> str:
    try:
        spec = parse(expr)
    except CronError:
        return f"cron {expr}"
    all_min = frozenset(range(60))
    all_hr = frozenset(range(24))
    all_mon = frozenset(range(1, 13))
    single_m = len(spec.minutes) == 1
    single_h = len(spec.hours) == 1
    star_min = spec.minutes == all_min
    star_hr = spec.hours == all_hr
    star_mon = spec.months == all_mon

    # Every day at HH:MM
    if single_m and single_h and spec.dom_star and star_mon and spec.dow_star:
        return f"Every day at {_hhmm(spec)}"
    # Weekly / multi-day-of-week at HH:MM
    if single_m and single_h and spec.dom_star and star_mon and not spec.dow_star:
        names = [_DAY_NAMES[d] for d in sorted(spec.dows)]
        if len(names) == 1:
            return f"Every {names[0]} at {_hhmm(spec)}"
        return f"{_join_and([n + 's' for n in names])} at {_hhmm(spec)}"
    # On the Nth of every month at HH:MM
    if single_m and single_h and not spec.dom_star and len(spec.doms) == 1 and star_mon and spec.dow_star:
        return f"On the {_ordinal(next(iter(spec.doms)))} of every month at {_hhmm(spec)}"
    # Every hour at :MM
    if single_m and star_hr and spec.dom_star and star_mon and spec.dow_star:
        return f"Every hour at :{next(iter(spec.minutes)):02d}"
    # Every N hours
    if spec.minutes == frozenset({0}) and spec.dom_star and star_mon and spec.dow_star and not star_hr:
        for step in range(2, 24):
            if spec.hours == frozenset(range(0, 24, step)):
                return f"Every {step} hours"
    return f"cron {expr}"


def dow_word(expr: str) -> str:
    """The schedule's day word for the §7.4 needs-you {dow} template:
    `day` (daily), `Sunday` (one day), `Monday and Thursday` (several)."""
    try:
        spec = parse(expr)
    except CronError:
        return "day"
    if spec.dow_star:
        return "day"
    names = [_DAY_NAMES[d] for d in sorted(spec.dows)]
    return _join_and(names)
