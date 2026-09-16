# app/gui/status.py — the derivation layer the Board and the job page render from
# (spec §7.2, §7.3, §7.7, §8.1, §8.2). It CONSUMES the Task-3 engine (runs, cron,
# errors) and the vocabulary (vocab); it never touches the store or the network.
#
# What it derives per job (§8.2 JobStatus): the state token (§4.7 precedence
# Running > Paused > Overdue > Failed > OK > Not run yet), the 14-cell strip and
# its counts (over kind == "backup" records ONLY), median and streak, next run,
# overdue window, and the error class of the last failure. Across jobs (§8.1) it
# derives the Board verdict, the needs-you rows, and crontab_stale.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import cron, errors, runs
from . import jobs_io, vocab

# Internal state constants — MUST equal vocab.STATE_NAMES keys exactly (a later
# vocab/mono test cross-checks this; see the Task-4 brief carry-forward #4).
RUNNING = "RUNNING"
PAUSED = "PAUSED"
OVERDUE = "OVERDUE"
FAILED = "FAILED"
OK = "OK"
NOT_RUN_YET = "NOT_RUN_YET"

GRACE_S = 15 * 60                                        # ruling R9 (§7.3)
STRIP_CELLS = 14

# Board sort — worst first (§4.7): Failed, Overdue, Running, Paused, OK, Not run yet.
_SORT_RANK = {FAILED: 0, OVERDUE: 1, RUNNING: 2, PAUSED: 3, OK: 4, NOT_RUN_YET: 5}

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")

_UNKNOWN_VERDICT = "The run stopped with an error; open the record to see it."


# --- small helpers ---------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tzname(tz) -> str:
    return getattr(tz, "key", None) or getattr(tz, "tzname", lambda _: None)(None) or "UTC"


def _human_date(dt: datetime | None, tz) -> str | None:
    if dt is None:
        return None
    d = dt.astimezone(tz)
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _weekday(dt: datetime | None, tz) -> str | None:
    if dt is None:
        return None
    return _WEEKDAYS[dt.astimezone(tz).weekday()]


# --- provenance inheritance (§4.6) -----------------------------------------

def inherit_provenance(marks) -> str:
    """A projected/derived figure inherits the WEAKEST provenance of its inputs, on
    the order assumed < measured < invoiced (§4.6). This returns only the two values
    a COMPUTED figure can carry: 'assumed' when any input is assumed, else 'projected'
    — never 'measured'/'invoiced', which belong to OBSERVED figures (a size something
    walked, a byte count, the invoice). A computed figure built only from measured
    inputs is 'projected' and renders with no mark at all, because a solid measured
    line under a dollar figure would claim the dollars were observed, which they were
    not (§4.6). estimate_io.provenance_of (§7.9) is the cost adapter's home for this."""
    return "assumed" if any(m == "assumed" for m in marks) else "projected"


# --- overdue (§7.3) --------------------------------------------------------

def overdue(job: dict, last, now: datetime, tz):
    """-> (expected_at, overdue_since); (None, None) when not overdue / not computable.
    The reference instant is the last completed run's FINISH (falling back to its start
    only if a record has no finished_at), or `created_at` when the job never ran. Using
    the finish (not the start) is what stops the Board crying wolf when a manual run
    straddles a scheduled tick (§7.3)."""
    if not job.get("enabled", True):
        return None, None
    if last is not None:
        ref = last.finished_at or last.started_at
    else:
        ref = _parse_iso(job.get("created_at"))
    if ref is None:                          # legacy job, never ran: not computable
        return None, None
    try:
        expected = cron.next_after(job["schedule"], ref, tz)
    except cron.CronError:
        return None, None
    since = expected + timedelta(seconds=GRACE_S)
    if now > since:
        return expected, since
    return None, None


def _next_run(job: dict, now: datetime, tz):
    if not job.get("enabled", True):
        return None, "paused"
    try:
        return _iso(cron.next_after(job["schedule"], now, tz)), None
    except cron.CronError:
        return None, "can't compute this schedule"


# --- strip / counts (§8.2) — kind == "backup" ONLY -------------------------

def _dim_cell() -> dict:
    return {"run_id": None, "started_at": None, "outcome": None, "duration_s": None,
            "slow": False, "dim": True, "snapshot_id": None, "title": "no run on record yet"}


def _cell(rec, median_s, tz) -> dict:
    slow = bool(median_s and rec.duration_s and rec.duration_s > 3 * median_s)
    return {"run_id": rec.id, "started_at": _iso(rec.started_at), "outcome": rec.outcome,
            "duration_s": rec.duration_s, "slow": slow, "dim": False,
            "snapshot_id": rec.snapshot_id,
            "title": f"{_iso_short(rec.started_at, tz)} · {rec.outcome}"}


def _iso_short(dt, tz) -> str:
    if dt is None:
        return ""
    d = dt.astimezone(tz)
    return f"{_WEEKDAYS[d.weekday()][:3]} {d.day} {_MONTHS[d.month - 1][:3]} {d:%H:%M}"


def _strip(backups, median_s, tz, cells=STRIP_CELLS) -> list[dict]:
    # backups are newest-first; take the newest `cells`, render oldest-first, and
    # pad the front with dim placeholders so the strip is always `cells` wide.
    window = list(reversed(backups[:cells]))
    row = [_cell(r, median_s, tz) for r in window]
    return [_dim_cell()] * (cells - len(row)) + row


# --- error class (§7.4) → dict ---------------------------------------------

def _error_class_dict(rec) -> dict | None:
    if rec is None:
        return None
    ec = errors.classify(rec.error, rec.exit_code, rec.outcome, rec.log or "")
    if ec is None:
        return None
    return {"code": ec.code, "short": ec.short, "verdict": ec.verdict, "cause": ec.cause,
            "board": ec.board, "fix": ec.fix, "fix_label": ec.fix_label,
            "fix_route": ec.fix_route, "blocker": ec.blocker}


def _last_dict(rec) -> dict | None:
    if rec is None:
        return None
    return {"id": rec.id, "started_at": _iso(rec.started_at), "finished_at": _iso(rec.finished_at),
            "duration_s": rec.duration_s, "outcome": rec.outcome, "snapshot_id": rec.snapshot_id,
            "error": rec.error, "exit_code": rec.exit_code, "trigger": rec.trigger,
            "phase": rec.phase, "copied": rec.copied}


# --- per-job context + public JobStatus ------------------------------------

def _context(cache_dir, name, job_def, now, tz):
    """Everything both the public JobStatus and the Board's verdict/needs-you need,
    derived from ONE read of the job's records (avoids re-reconciling per consumer)."""
    records = runs.read_runs(cache_dir, name).records          # reconcile=True (real job)
    backups = [r for r in records if r.kind in runs.BACKUP_KINDS]
    completed = [r for r in backups if r.outcome != "running"]
    last = completed[0] if completed else None
    last_ok = next((r for r in completed if r.outcome == "ok"), None)

    enabled = bool(job_def.get("enabled", True))
    expected_at = overdue_since = None
    if enabled:
        expected_at, overdue_since = overdue(job_def, last, now, tz)

    if runs.is_locked(cache_dir, name):
        state = RUNNING
    elif not enabled:
        state = PAUSED
    elif overdue_since is not None:
        state = OVERDUE
    elif last is not None and last.outcome in ("failed", "aborted"):
        state = FAILED
    elif last is not None and last.outcome == "ok":
        state = OK
    else:
        state = NOT_RUN_YET

    median_s = runs.median_duration_s(backups)
    strk = runs.streak(backups)
    window = backups[:STRIP_CELLS]
    ok_14 = sum(1 for r in window if r.outcome == "ok")
    failed_14 = sum(1 for r in window if r.outcome in ("failed", "aborted"))
    next_run, next_note = _next_run(job_def, now, tz)
    err = _error_class_dict(last) if state == FAILED else None
    slow_last = bool(last and median_s and last.duration_s and last.duration_s > 3 * median_s)

    return {
        "job": job_def, "name": name, "enabled": enabled, "state": state,
        "last": last, "last_ok": last_ok, "error_class": err,
        "expected_at": expected_at, "overdue_since": overdue_since,
        "next_run": next_run, "next_run_note": next_note,
        "median_s": median_s, "slow_last": slow_last, "streak": strk,
        "runs_total": len(backups), "ok_14": ok_14, "failed_14": failed_14,
        "strip": _strip(backups, median_s, tz), "tz": tz,
    }


def _to_status(ctx, stale) -> dict:
    j = ctx["job"]
    typ = j.get("type")
    return {
        "name": ctx["name"], "type": typ, "type_label": vocab.TYPE_NAMES.get(typ, typ),
        "source": j.get("source"), "enabled": ctx["enabled"],
        "created_at": j.get("created_at"),
        "state": ctx["state"], "label": vocab.STATE_NAMES.get(ctx["state"], ctx["state"]),
        "last": _last_dict(ctx["last"]),
        "next_run": ctx["next_run"], "next_run_note": ctx["next_run_note"],
        "expected_at": _iso(ctx["expected_at"]), "overdue_since": _iso(ctx["overdue_since"]),
        "median_s": ctx["median_s"], "slow_last": ctx["slow_last"],
        "streak": ctx["streak"], "runs_total": ctx["runs_total"],
        "ok_14": ctx["ok_14"], "failed_14": ctx["failed_14"],
        "strip": ctx["strip"], "error_class": ctx["error_class"],
        "crontab_stale": stale,
    }


def job(config_dir, cache_dir, scripts_dir, name, *, now=None, tz=None,
        source_root=None, job_def=None, stale=None) -> dict | None:
    """The JobStatus for one job (§8.2), or None when the job does not exist."""
    now = now or datetime.now(timezone.utc)
    tz = tz or cron.local_tz()
    if job_def is None:
        job_def = jobs_io.get(config_dir, name)
    if job_def is None:
        return None
    if stale is None:
        stale = crontab_stale(config_dir, cache_dir, scripts_dir, source_root=source_root)
    ctx = _context(cache_dir, name, job_def, now, tz)
    return _to_status(ctx, stale)


# --- crontab_stale (§7.3) --------------------------------------------------

def crontab_stale(config_dir, cache_dir, scripts_dir, *, source_root=None) -> bool:
    """True iff the on-disk crontab differs from render_crontab(dry_run=True). That is
    its WHOLE definition (§7.3): it detects a failed write or a hand-edited jobs.json,
    never 'the scheduler has not reloaded'."""
    want = jobs_io.render_crontab(config_dir, cache_dir, scripts_dir, dry_run=True,
                                  source_root=source_root)
    try:
        have = (Path(cache_dir) / "crontab").read_text()
    except (FileNotFoundError, OSError):
        have = ""
    return have != want


# --- Board (§8.1) ----------------------------------------------------------

def board(config_dir, cache_dir, scripts_dir, *, now=None, tz=None, source_root=None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = tz or cron.local_tz()
    stale = crontab_stale(config_dir, cache_dir, scripts_dir, source_root=source_root)
    defs = [j for j in jobs_io.load(config_dir) if j.get("name")]
    ctxs = [_context(cache_dir, j["name"], j, now, tz) for j in defs]
    ctxs.sort(key=lambda c: (_SORT_RANK.get(c["state"], 9), c["next_run"] or "~", c["name"]))
    statuses = [_to_status(c, stale) for c in ctxs]
    return {
        "generated_at": _iso(now), "tz": _tzname(tz),
        "next_scheduled": _next_scheduled(ctxs),
        "verdict": verdict(ctxs, now, tz, stale),
        "needs_you": needs_you(ctxs, stale, tz),
        "jobs": statuses, "crontab_stale": stale,
    }


def _next_scheduled(ctxs):
    cand = [(c["next_run"], c["name"]) for c in ctxs if c["next_run"]]
    if not cand:
        return None
    at, name = min(cand)
    return {"job": name, "at": at}


# --- verdict (§5.1) --------------------------------------------------------

def _open_button(name):
    return {"label": f"Open {name} →", "href": f"/jobs/{name}"}


def verdict(ctxs, now, tz, stale=False) -> dict:
    if not ctxs:
        return {"state": "none", "job": None, "h2": "Nothing is being backed up yet.",
                "sub": None, "button": {"label": "Create the first job →", "href": "/jobs/new"}}

    failed = [c for c in ctxs if c["state"] == FAILED]
    overdue_c = [c for c in ctxs if c["state"] == OVERDUE]
    running = [c for c in ctxs if c["state"] == RUNNING]

    if failed:
        c = failed[0]
        ec = c["error_class"] or {}
        since = _weekday(c["last_ok"].finished_at, tz) if c["last_ok"] else "its last successful run"
        second = ec.get("verdict") or _UNKNOWN_VERDICT
        h2 = f"{c['name']} has not backed up since {since}. {second}"
        label = ec.get("fix_label") or "Open the run record →"
        route = ec.get("fix_route")
        href = route.replace("<name>", c["name"]) if route else _record_href(c)
        return {"state": "failed", "job": c["name"], "h2": h2, "sub": None,
                "button": {"label": label, "href": href}}

    if overdue_c:
        c = overdue_c[0]
        hhmm = _hhmm(c["expected_at"], tz)
        # §5.1: when the on-disk crontab no longer matches the jobs, the overdue
        # verdict's second sentence becomes the canonical crontab-stale wording
        # (verbatim as on the needs-you row and §7.3) instead of the default.
        second = ("The schedule file on disk does not match your jobs; restart the container."
                  if stale else "The schedule is on, but nothing was recorded.")
        h2 = f"{c['name']} should have run at {hhmm} and did not. {second}"
        return {"state": "overdue", "job": c["name"], "h2": h2, "sub": None,
                "button": _open_button(c["name"])}

    if running:
        c = running[0]
        return {"state": "running", "job": c["name"], "h2": f"{c['name']} is running now.",
                "sub": None, "button": {"label": "Watch it in Activity →", "href": "/activity"}}

    # Only OK / Paused / Not-run-yet remain — nothing is WRONG.
    oks = [c for c in ctxs if c["state"] == OK]
    if oks:
        newest = max(oks, key=lambda c: (c["last"].finished_at or c["last"].started_at)
                     if c["last"] else datetime.min.replace(tzinfo=timezone.utc))
        if len(oks) == 1:
            h2 = f"{oks[0]['name']} backed up on schedule and nothing needs you."
        else:
            count = "Both jobs" if len(oks) == 2 else f"All {len(oks)} jobs"
            h2 = f"Everything ran. {count} backed up on schedule and nothing needs you."
        return {"state": "ok", "job": newest["name"], "h2": h2, "sub": None,
                "button": _open_button(newest["name"])}

    if all(c["state"] == PAUSED for c in ctxs):
        first = ctxs[0]
        return {"state": "paused", "job": first["name"], "h2": "Every job is paused.",
                "sub": None, "button": {"label": f"Resume {first['name']}",
                                        "href": f"/jobs/{first['name']}"}}

    # everything left is Not-run-yet (possibly mixed with Paused)
    first = ctxs[0]
    return {"state": "notrun", "job": first["name"], "h2": "Nothing has run yet.",
            "sub": None, "button": {"label": f"Run {first['name']} now",
                                    "href": f"/jobs/{first['name']}/run"}}


def _record_href(ctx):
    rec = ctx["last"]
    if rec is None:
        return f"/jobs/{ctx['name']}"
    return f"/jobs/{ctx['name']}/runs/{rec.id}"


def _hhmm(when, tz) -> str:
    dt = when if isinstance(when, datetime) else _parse_iso(when)
    if dt is None:
        return "—"
    return f"{dt.astimezone(tz):%H:%M}"


# --- needs-you (§7.7 / §7.4) -----------------------------------------------

def needs_you(ctxs, stale, tz) -> list[dict]:
    """The Board's needs-you rows (§8.1). Order: blockers, then warnings, then advice,
    then setup gaps. This task derives (a) job failures whose class is a BLOCKER and
    (b) the crontab_stale WARNING; readiness checks, the missing restore mount and
    warm-up advice (§7.7.1) are added by the readiness task."""
    blockers, warnings = [], []
    for c in ctxs:
        ec = c["error_class"]
        if c["state"] == FAILED and ec and ec["blocker"]:
            blockers.append(_blocker_row(c, tz))
    if stale:
        warnings.append({
            "level": "warning", "code": "crontab-stale", "job": None,
            "text": ("The schedule file on disk does not match your jobs. "
                     "Restart the container so your latest job settings take effect."),
            "fix": None,
        })
    return blockers + warnings


def _blocker_row(ctx, tz) -> dict:
    c = ctx
    ec = c["error_class"]
    rec = c["last"]
    dow = cron.dow_word(c["job"].get("schedule", ""))
    since = _human_date(c["last_ok"].finished_at, tz) if c["last_ok"] else "the first run"
    body = ec["board"] if ec["board"] is not None else ec["cause"]
    text = body.replace("{dow}", dow).replace("{since}", since)
    route = ec.get("fix_route")
    fix = None
    if ec.get("fix_label"):
        fix = {"label": ec["fix_label"],
               "href": route.replace("<name>", c["name"]) if route else _record_href(c)}
    return {
        "level": "blocker", "code": ec["code"], "job": c["name"],
        "strong": f"{c['name']} cannot finish a run.", "text": text,
        "errline": rec.error if rec else None, "hint": ec["fix"],
        "when": _iso(rec.finished_at or rec.started_at) if rec else None,
        "record": _record_href(c), "fix": fix,
    }
