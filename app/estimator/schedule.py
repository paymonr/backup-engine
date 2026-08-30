# app/estimator/schedule.py — PURE: 5-field cron -> approximate backups/month.
# Decision-support only (not a real cron simulation). Unknown/malformed -> daily.
from __future__ import annotations

_DAYS_PER_MONTH = 30.4
_WEEKS_PER_MONTH = 30.4 / 7  # ~4.345

def _count(field: str, lo: int, hi: int) -> int:
    """How many discrete values a single cron field matches within [lo, hi]."""
    span = hi - lo + 1
    total = 0
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, _, s = part.partition("/")
            step = int(s) if s.isdigit() and int(s) > 0 else 1
            part = base
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            if not (x.isdigit() and y.isdigit()):
                return 0
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
        else:
            return 0
        a, b = max(a, lo), min(b, hi)
        if b < a:
            continue
        total += len(range(a, b + 1, step))
    return total

def backups_per_month(cron: str) -> float:
    fields = (cron or "").split()
    if len(fields) != 5:
        return _DAYS_PER_MONTH
    minute, hour, dom, mon, dow = fields
    per_day = _count(minute, 0, 59) * _count(hour, 0, 23)
    if per_day == 0:
        return _DAYS_PER_MONTH
    dom_all = dom.strip() == "*"
    dow_all = dow.strip() == "*"
    if dom_all and dow_all:
        days = _DAYS_PER_MONTH
    elif not dom_all and dow_all:
        days = min(float(_count(dom, 1, 31)), _DAYS_PER_MONTH)
    elif dom_all and not dow_all:
        days = min(_count(dow, 0, 6) * _WEEKS_PER_MONTH, _DAYS_PER_MONTH)
    else:  # Vixie cron: dom OR dow
        days = min(_count(dom, 1, 31) + _count(dow, 0, 6) * _WEEKS_PER_MONTH, _DAYS_PER_MONTH)
    mon_factor = _count(mon, 1, 12) / 12.0 if mon.strip() != "*" else 1.0
    return per_day * days * (mon_factor if mon_factor > 0 else 1.0)
