# app/estimator/tiered.py — old-version ("versioning") storage for a restic repo
# under a TIERED keep policy (--keep-last/-daily/-weekly/-monthly). PURE: no I/O.
#
# THE MODEL — an exact expectation, VALIDATED against ground truth rather than guessed:
#   * a REAL restic 0.17.3 run (4,416 real backup+forget+stats cycles, 7 scenarios x
#     24 months): steady state within 0.8% on every scenario; every month within 1.2%
#     for daily schedules (weekly schedules' first 3 months sit ~0.01 of the dataset off);
#   * two independent restic-retention simulators (agree to 2.6e-8; match at steady);
#   * two independent analytical derivations under an adversarial judge.
#   Pinned in tests/estimator/test_tiered_truth.py (fixtures/tiered_truth_real.json).
#
#     old_bytes / S  =  SUM over consecutive RETAINED snapshots of ( 1 - (1-c)^gap )
#
#   S    = dataset size;  c = fraction of the DATA rewritten per backup;
#   gap  = number of BACKUPS between two consecutive retained snapshots.
#
# Why: dedup stores a file-version once, so along the retained chain a file
# contributes exactly one OLD version per gap in which it was rewritten at least
# once, and P(rewritten in a gap of g backups) = 1-(1-c)^g. That single term gives
# dedup (one term per GAP, not per snapshot), saturation (a term caps at 1.0),
# "re-editing the same file inside a gap is free", tier overlap (the retained set is
# a union, so keep_last <= keep_daily adds nothing), and the ramp (early on the
# chain is short). old/S routinely exceeds 1.0 — a file can have many distinct old
# versions alive at once, one per retained gap — and that is correct.
#
# The retained chain uses REAL CALENDAR dates: restic buckets on calendar days, ISO
# weeks and calendar months. Idealised 30-day months were the largest error source
# in every earlier version of this estimator.
#
# What this model does NOT include (it is an upper-ish bound, not a quote): sub-file
# / content-defined-chunk dedup (a big file that changes in one spot costs less),
# compression, metadata/tree/pack overhead, identical-content collisions.
from __future__ import annotations
from datetime import date, timedelta

DAYS_PER_MONTH = 30           # reporting month = 30-day block from the start date
MEAN_MONTH_DAYS = 30.44
DEFAULT_START = date(2026, 1, 1)


def _retained(dates, t, last, daily, weekly, monthly):
    """Indices of dates[0..t] kept by restic 0.17 `forget` (union of rules): walk
    newest -> oldest; each bucket rule keeps the first snapshot it meets in each
    NEW bucket until its budget is spent; --keep-last keeps the n newest."""
    keep = set()
    _S = object()
    bk = [[last,    None,                                _S],
          [daily,   lambda d: (d.year, d.month, d.day),  _S],
          [weekly,  lambda d: d.isocalendar()[:2],       _S],
          [monthly, lambda d: (d.year, d.month),         _S]]
    for nr in range(t, -1, -1):
        if all(b[0] <= 0 for b in bk):
            break
        d = dates[nr]
        for j, b in enumerate(bk):
            if b[0] <= 0:
                continue
            val = nr if j == 0 else b[1](d)
            if b[2] is _S or b[2] != val:
                b[2] = val
                b[0] -= 1
                keep.add(nr)
    # restic additionally keeps the OLDEST snapshot whenever a keep-* bucket still
    # has spare capacity after the walk (verified on the real 0.17.3 binary:
    # daily3+monthly3 over two months keeps 5 — the 5th is the oldest, tagged
    # 'monthly snapshot'; monthly 4/6 add nothing more). This pins the very first
    # snapshot while the coarse buckets fill, which is why the real early ramp runs
    # hotter than a plain bucket walk. At steady state every bucket is full, so it
    # changes nothing there.
    if any(b[0] > 0 for b in bk):
        keep.add(0)
    return sorted(keep)


def _gap_sum(chain, q):
    return sum(1.0 - q ** (chain[i + 1] - chain[i]) for i in range(len(chain) - 1))


# ------------------------------------------------ interval >= 1 day: exact per backup
def _series_daily_or_slower(c, interval_days, last, daily, weekly, monthly, months, start):
    horizon = months * DAYS_PER_MONTH
    n = int(horizon / float(interval_days) + 1e-9)
    if n * interval_days < horizon - 1e-9:
        n += 1
    offs = [i * float(interval_days) for i in range(n)]
    offs = [o for o in offs if o < horizon - 1e-9]
    dates = [start + timedelta(days=int(o)) for o in offs]
    q = 1.0 - c
    vals = [_gap_sum(_retained(dates, t, last, daily, weekly, monthly), q)
            for t in range(len(dates))]
    return offs, vals


# ------------------------------------------------ interval < 1 day: exact per DAY
def _series_sub_daily(c, interval_days, last, daily, weekly, monthly, months, start):
    """bpd backups per day. The daily/weekly/monthly rules can only ever keep the
    NEWEST backup of a calendar day, so the bucket chain is walked over day-
    representatives (<= horizon days) instead of every backup. keep-last and the
    always-growing gap from the newest older anchor to "now" are averaged over
    the day's intra-day positions analytically. Returns one time-averaged value
    per day (offs = day offsets)."""
    bpd = int(round(1.0 / float(interval_days)))
    horizon_days = months * DAYS_PER_MONTH
    q = 1.0 - c
    dates = [start + timedelta(days=d) for d in range(horizon_days)]
    # day-rep backup index = the day's LAST backup: s_{d+1}-1 with s_d = d*bpd
    rep_idx = [(d + 1) * bpd - 1 for d in range(horizon_days)]
    vals = []
    for D in range(horizon_days):
        chain_days = _retained(dates, D, 0, daily, weekly, monthly)  # bucket rules only
        older = [rep_idx[d] for d in chain_days if d < D]
        s_D = D * bpd
        fixed = _gap_sum(older, q) if len(older) > 1 else 0.0
        a = older[-1] if older else None            # newest older anchor (backup idx)
        acc = 0.0
        for h in range(1, bpd + 1):                 # intra-day position of "now"
            now = s_D + h - 1
            lo = now - last + 1                      # oldest keep-last index
            if a is None:
                # no older anchor: only the keep-last prefix exists (bounded by history)
                first = 0
                span = min(last - 1, now - first)
                acc += max(0, span) * (1.0 - q)
            elif lo > a:
                acc += (1.0 - q ** (lo - a)) + (last - 1) * (1.0 - q)
            else:
                acc += (now - a) * (1.0 - q)         # keep-last window overlaps anchor: unit gaps
        vals.append(fixed + acc / bpd)
    return [float(d) for d in range(horizon_days)], vals


def old_series(c, interval_days, last, daily, weekly, monthly, months=24, start=DEFAULT_START):
    """(offset_days, old_fraction) samples — per backup for interval >= 1 day, per
    day (time-averaged within the day) for sub-daily intervals."""
    if interval_days >= 1.0:
        return _series_daily_or_slower(c, interval_days, last, daily, weekly, monthly, months, start)
    return _series_sub_daily(c, interval_days, last, daily, weekly, monthly, months, start)


def old_fraction_by_month(c, interval_days, last, daily, weekly, monthly,
                          months=24, start=DEFAULT_START):
    """Time-averaged old-version data (fraction of dataset size) per month 1..months."""
    if c <= 0 or (last + daily + weekly + monthly) <= 1:
        return [0.0] * months
    offs, vals = old_series(c, interval_days, last, daily, weekly, monthly, months, start)
    acc = [[] for _ in range(months)]
    for o, v in zip(offs, vals):
        acc[int(o // DAYS_PER_MONTH)].append(v)
    return [sum(a) / len(a) if a else 0.0 for a in acc]


def steady_fraction(c, interval_days, last, daily, weekly, monthly, months=24, start=DEFAULT_START):
    """Long-run old-version fraction: mean of the last 6 reporting months of a
    horizon long enough to have plateaued (>= longest tier + 6 months)."""
    m = old_fraction_by_month(c, interval_days, last, daily, weekly, monthly, months, start)
    return sum(m[-6:]) / 6.0


def plateau_month(last, daily, weekly, monthly, interval_days):
    """The month by which the retained chain has fully populated (the reach of the
    longest tier) — old versions ramp until roughly here, then go flat."""
    iv = float(interval_days)
    reach_days = max((last - 1) * iv, (daily - 1) * max(iv, 1.0),
                     7.0 * (weekly - 1), MEAN_MONTH_DAYS * (monthly - 1), 0.0)
    return max(1, int(reach_days // DAYS_PER_MONTH) + 1)


def tier_ladder(c, interval_days, last, daily, weekly, monthly):
    """Interpretable per-tier MARGINAL contribution to the steady old-version
    fraction (the 'gap ladder' for the UI): each tier extends the retained chain
    further back and plants anchors one spacing apart over the stretch it alone
    covers, adding (extra_reach / spacing) gaps of cost 1-(1-c)^(backups per gap).
    A tier whose reach is already covered by a denser one adds nothing (that is
    exactly why keep_last <= keep_daily is free at <= 1 backup/day).
    Back-of-envelope only (within ~5% of the exact routine); use for explanation."""
    iv = float(interval_days)
    day_step = max(iv, 1.0)
    shells = [
        ("last",    (last - 1) * iv,                                   iv),
        ("daily",   (daily - 1) * day_step,                            day_step),
        ("weekly",  7.0 * (weekly - 2) + 4.0 if weekly >= 2 else 0.0,  7.0),
        ("monthly", MEAN_MONTH_DAYS * (monthly - 2) + 16.2 if monthly >= 2 else 0.0, MEAN_MONTH_DAYS),
    ]
    out, reach = [], 0.0
    for name, r, step in shells:
        add = 0.0
        if r > reach:
            add = ((r - reach) / step) * (1.0 - (1.0 - c) ** max(1.0, step / iv))
            reach = r
        out.append({"tier": name, "adds_fraction": add, "covered": r > 0 and add == 0.0})
    return out
