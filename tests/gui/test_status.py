import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.gui import jobs_io, status, vocab

UTC = timezone.utc


# --- fixtures / seeding -----------------------------------------------------

def _root(tmp_path):
    r = tmp_path / "src"
    (r / "appdata").mkdir(parents=True)
    (r / "media" / "manga").mkdir(parents=True)
    return str(r)


def _cfg(tmp_path):
    c = tmp_path / "config"; c.mkdir(); return str(c)


def _cache(tmp_path):
    c = tmp_path / "cache"; (c / "state").mkdir(parents=True); return str(c)


def _mk_job(cfg, root, name="appdata", type="versioned", source="appdata",
            schedule="0 5 * * *", enabled=True, storage_class="STANDARD",
            created_at="2026-09-01T00:00:00Z", **kw):
    job = {"name": name, "type": type, "source": source, "schedule": schedule,
           "enabled": enabled, "storage_class": storage_class, "created_at": created_at}
    if type == "versioned":
        job.setdefault("keep", {"last": 3, "daily": 7, "weekly": 4, "monthly": 6})
    job.update(kw)
    jobs_io.upsert(cfg, job, source_root=root)
    return jobs_io.get(cfg, name)


def _end(cache, name, rid, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": "scheduled", "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _rid(day=15, hh=5, mm=0, tag="0000"):
    return f"202609{day:02d}T{hh:02d}{mm:02d}00Z-{tag}"


# =====================================================================
# State precedence (4.7 / 7.3): Running > Paused > Overdue > Failed > OK > Not run yet
# =====================================================================

def test_state_constants_match_vocab_keys():
    # a later vocab/mono test cross-checks these; keep them identical.
    for k in ("RUNNING", "PAUSED", "OVERDUE", "FAILED", "OK", "NOT_RUN_YET"):
        assert getattr(status, k) == k
        assert k in vocab.STATE_NAMES


def test_ok_when_last_backup_ok_and_not_overdue(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    job = _mk_job(cfg, root)
    _end(cache, "appdata", _rid(15), outcome="ok",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)   # 55 min after finish, before next fire
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["state"] == "OK" and js["label"] == "OK"


def test_failed_when_last_backup_failed(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _end(cache, "appdata", _rid(15), outcome="failed", duration=99,
         error="AccessDenied: s3:DeleteObjectVersion",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:01:40Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "FAILED"


def test_aborted_counts_as_failed(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _end(cache, "appdata", _rid(15), outcome="aborted", duration=None, exit_code=None,
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:01:40Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "FAILED"


def test_paused_beats_overdue_and_failed(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, enabled=False)
    _end(cache, "appdata", _rid(12), outcome="failed",
         started="2026-09-12T05:00:01Z", finished="2026-09-12T05:01:40Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)   # 3 days later -> would be overdue if enabled
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "PAUSED"


def test_running_lock_beats_overdue(tmp_path, monkeypatch):
    # A held lock during a missed fire is RUNNING, never Overdue (7.3): running is
    # derived from the lock, independent of next_after.
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _end(cache, "appdata", _rid(10), outcome="ok",
         started="2026-09-10T05:00:01Z", finished="2026-09-10T05:04:00Z")
    monkeypatch.setattr(runs, "is_locked", lambda cache_dir, job: job == "appdata")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)   # days after the last run -> overdue if not running
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "RUNNING"


# =====================================================================
# OVERDUE (7.3): 15-minute grace; reference is the last run's finish
# =====================================================================

def test_overdue_three_days_later(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, schedule="0 5 * * *")
    _end(cache, "appdata", _rid(12), outcome="ok",
         started="2026-09-12T05:00:01Z", finished="2026-09-12T05:04:00Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)   # next fire after finish is 13 Sep 05:00
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["state"] == "OVERDUE"
    assert js["expected_at"] == "2026-09-13T05:00:00Z"
    assert js["overdue_since"] == "2026-09-13T05:15:00Z"


def test_not_overdue_inside_grace(tmp_path):
    # 14 minutes after the missed fire -> still inside the 15-minute grace.
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, schedule="0 5 * * *")
    _end(cache, "appdata", _rid(14), outcome="ok",
         started="2026-09-14T05:00:01Z", finished="2026-09-14T05:04:00Z")
    now = datetime(2026, 9, 15, 5, 14, tzinfo=UTC)   # fire was 05:00, grace ends 05:15
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["state"] == "OK" and js["overdue_since"] is None


def test_overdue_just_past_grace(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, schedule="0 5 * * *")
    _end(cache, "appdata", _rid(14), outcome="ok",
         started="2026-09-14T05:00:01Z", finished="2026-09-14T05:04:00Z")
    now = datetime(2026, 9, 15, 5, 16, tzinfo=UTC)   # one minute past the grace
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "OVERDUE"


def test_manual_run_straddling_a_tick_is_not_overdue(tmp_path):
    # 7.3 wolf-cry vector: run started 04:50, finished 05:10 on a 0 5 * * * job;
    # at 05:30 the job is OK, not Overdue (ref is the FINISH, not the start).
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, schedule="0 5 * * *")
    _end(cache, "appdata", _rid(15, 4, 50), outcome="ok",
         started="2026-09-15T04:50:00Z", finished="2026-09-15T05:10:00Z")
    now = datetime(2026, 9, 15, 5, 30, tzinfo=UTC)
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "OK"


def test_never_ran_with_created_at_overdue_without_not_run_yet(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    # created 3 days ago, a daily job, never ran -> Overdue
    _mk_job(cfg, root, schedule="0 5 * * *", created_at="2026-09-12T00:00:00Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["state"] == "OVERDUE"


def test_legacy_job_no_created_at_never_ran_is_not_run_yet(tmp_path):
    # 7.10: an old job with no created_at is never Overdue before its first run.
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    # write a raw job with NO created_at (bypass upsert's stamp)
    Path(cfg, "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "appdata", "type": "versioned", "source": "appdata", "schedule": "0 5 * * *",
         "enabled": True, "storage_class": "STANDARD",
         "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}]}))
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["state"] == "NOT_RUN_YET"
    assert js["overdue_since"] is None


def test_unparsable_cron_next_run_none_with_note(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    Path(cfg, "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "appdata", "type": "archive", "source": "appdata", "schedule": "@daily",
         "enabled": True, "storage_class": "STANDARD"}]}))
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["next_run"] is None
    assert js["next_run_note"] == "can't compute this schedule"


def test_paused_next_run_note(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, enabled=False)
    js = status.job(cfg, cache, "/s", "appdata", now=datetime(2026, 9, 15, 6, 0, tzinfo=UTC), tz=UTC)
    assert js["next_run"] is None and js["next_run_note"] == "paused"


# =====================================================================
# Strip / counts / streak / median — kind == "backup" ONLY (6.5, 8.2)
# =====================================================================

def _seed_ok_backups(cache, name, n, base_day=1):
    # n OK daily backups, oldest first; durations 240..; ids unique
    for i in range(n):
        day = base_day + i
        _end(cache, name, _rid(day, 5, 0, f"{i:04d}"), outcome="ok",
             started=f"2026-09-{day:02d}T05:00:01Z", finished=f"2026-09-{day:02d}T05:04:00Z",
             duration=240 + i)


def test_strip_has_14_cells_and_counts(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _seed_ok_backups(cache, "appdata", 14, base_day=1)
    now = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert len(js["strip"]) == 14
    assert js["ok_14"] == 14 and js["failed_14"] == 0
    assert js["runs_total"] == 14 and js["streak"] == 14
    assert all(c["outcome"] == "ok" and c["dim"] is False for c in js["strip"])
    assert js["strip"][0]["started_at"] < js["strip"][-1]["started_at"]  # oldest first


def test_strip_pads_with_dim_cells_oldest_first(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _seed_ok_backups(cache, "appdata", 3, base_day=1)
    now = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert len(js["strip"]) == 14
    # the first 11 are dim placeholders, the last 3 are real
    assert [c["dim"] for c in js["strip"]] == [True] * 11 + [False] * 3
    assert js["strip"][0]["title"] == "no run on record yet"
    assert js["strip"][0]["run_id"] is None


def test_operation_records_are_not_strip_cells(tmp_path):
    # 8.2 vector: 14 OK backups + a restore/thaw/test-restore newer than all of them.
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _seed_ok_backups(cache, "appdata", 14, base_day=1)   # newest backup finishes 14 Sep 05:04
    # operations NEWER than the newest backup (same evening), before `now`
    _end(cache, "appdata", _rid(14, 9, 0, "aaaa"), outcome="ok", kind="restore",
         started="2026-09-14T09:00:00Z", finished="2026-09-14T09:30:00Z", duration=1800)
    _end(cache, "appdata", _rid(14, 10, 0, "bbbb"), outcome="ok", kind="thaw",
         started="2026-09-14T10:00:00Z", finished="2026-09-14T10:01:00Z", duration=60)
    _end(cache, "appdata", _rid(14, 11, 0, "cccc"), outcome="ok", kind="test-restore",
         started="2026-09-14T11:00:00Z", finished="2026-09-14T11:20:00Z", duration=1200)
    now = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)   # before the next 05:00 fire -> not overdue
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert len(js["strip"]) == 14 and all(c["outcome"] == "ok" for c in js["strip"])
    assert js["ok_14"] == 14 and js["failed_14"] == 0
    assert js["runs_total"] == 14 and js["streak"] == 14
    # median is over backups only (240..253), so <= 300; the 1800 s restore is ignored
    assert js["median_s"] is not None and js["median_s"] < 300
    assert js["state"] == "OK"  # the newest RECORD is a restore, but state uses last backup


def test_failed_restore_does_not_change_state(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _seed_ok_backups(cache, "appdata", 5, base_day=10)   # newest backup finishes 14 Sep 05:04
    _end(cache, "appdata", _rid(14, 9, 0, "dead"), outcome="failed", kind="restore",
         started="2026-09-14T09:00:00Z", finished="2026-09-14T09:05:00Z", duration=300,
         error="download failed")
    now = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)   # before the next fire -> not overdue
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["state"] == "OK" and js["failed_14"] == 0


def test_streak_breaks_on_failure(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _seed_ok_backups(cache, "appdata", 3, base_day=1)                 # days 1-3 ok
    _end(cache, "appdata", _rid(4, 5, 0, "faaa"), outcome="failed",   # day 4 fail
         started="2026-09-04T05:00:01Z", finished="2026-09-04T05:01:00Z", duration=59)
    _seed_ok_backups(cache, "appdata", 2, base_day=5)                 # days 5-6 ok
    now = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    assert js["streak"] == 2         # only the two most-recent ok runs
    assert js["ok_14"] == 5 and js["failed_14"] == 1 and js["runs_total"] == 6


def test_median_none_below_three(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    _seed_ok_backups(cache, "appdata", 2, base_day=1)
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    assert status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)["median_s"] is None


def test_strip_slow_cell_over_3x_median(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root)
    # four ~100 s runs then one 1000 s run -> median 100, the 1000 s cell is slow
    for i, dur in enumerate((100, 100, 100, 100)):
        day = 1 + i
        _end(cache, "appdata", _rid(day, 5, 0, f"{i:04d}"), outcome="ok",
             started=f"2026-09-{day:02d}T05:00:00Z", finished=f"2026-09-{day:02d}T05:01:40Z", duration=dur)
    _end(cache, "appdata", _rid(5, 5, 0, "5100"), outcome="ok",
         started="2026-09-05T05:00:00Z", finished="2026-09-05T05:16:40Z", duration=1000)
    now = datetime(2026, 9, 6, 6, 0, tzinfo=UTC)
    js = status.job(cfg, cache, "/s", "appdata", now=now, tz=UTC)
    slow = [c for c in js["strip"] if not c["dim"] and c["slow"]]
    assert len(slow) == 1 and slow[0]["duration_s"] == 1000


# =====================================================================
# crontab_stale (7.3): on-disk crontab != render_crontab(dry_run=True)
# =====================================================================

def test_crontab_stale_false_after_render(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="movies", type="archive", source="media/manga", schedule="0 4 * * 0")
    jobs_io.render_crontab(cfg, cache, "/app/scripts", source_root=root)   # writes on-disk
    assert status.crontab_stale(cfg, cache, "/app/scripts", source_root=root) is False


def test_crontab_stale_true_when_on_disk_differs(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="movies", type="archive", source="media/manga", schedule="0 4 * * 0")
    Path(cache, "crontab").write_text("0 4 * * 0 /app/scripts/backup-job.sh STALE\n")
    assert status.crontab_stale(cfg, cache, "/app/scripts", source_root=root) is True


def test_crontab_stale_true_when_missing(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="movies", type="archive", source="media/manga", schedule="0 4 * * 0")
    # no crontab on disk yet, but a job exists -> stale
    assert status.crontab_stale(cfg, cache, "/app/scripts", source_root=root) is True


# =====================================================================
# Provenance inheritance (4.6): a projection inherits the weakest input mark
# =====================================================================

def test_inherit_provenance_assumed_when_any_input_assumed():
    assert status.inherit_provenance(["measured", "assumed"]) == "assumed"
    assert status.inherit_provenance(["assumed", "invoiced"]) == "assumed"


def test_inherit_provenance_projected_when_all_measured():
    # all-measured inputs -> projected (no mark), never "measured" (4.6): a solid
    # measured line under a computed figure would claim the dollars were observed.
    assert status.inherit_provenance(["measured", "measured"]) == "projected"
    assert status.inherit_provenance(["measured", "invoiced"]) == "projected"
    assert status.inherit_provenance([]) == "projected"


# =====================================================================
# verdict (5.1) — the mixed OK + Paused case
# =====================================================================

def test_verdict_all_ok(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="appdata", schedule="0 5 * * *")
    _end(cache, "appdata", _rid(15), outcome="ok",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["verdict"]["state"] == "ok"
    assert "nothing needs you" in b["verdict"]["h2"]


def test_verdict_mixed_ok_and_paused_is_ok(tmp_path):
    # One OK job and one Paused job: nothing is WRONG, so the verdict stays OK.
    # "Every job is paused" only applies when EVERY job is paused (5.1).
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="appdata", source="appdata", schedule="0 5 * * *", enabled=True)
    _mk_job(cfg, root, name="manga", type="archive", source="media/manga",
            schedule="0 4 * * 0", enabled=False)
    _end(cache, "appdata", _rid(15), outcome="ok",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["verdict"]["state"] == "ok"
    assert "paused" not in b["verdict"]["h2"].lower()


def test_verdict_all_paused(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="appdata", source="appdata", enabled=False)
    _mk_job(cfg, root, name="manga", type="archive", source="media/manga", enabled=False)
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["verdict"]["state"] == "paused"
    assert b["verdict"]["h2"] == "Every job is paused."


def test_verdict_overdue_not_stale(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="appdata", schedule="0 5 * * *")
    _end(cache, "appdata", _rid(12), outcome="ok",
         started="2026-09-12T05:00:01Z", finished="2026-09-12T05:04:00Z")
    jobs_io.render_crontab(cfg, cache, "/s", source_root=root)   # on-disk == render -> not stale
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["verdict"]["state"] == "overdue" and b["verdict"]["job"] == "appdata"
    assert b["crontab_stale"] is False
    # second sentence stays the default when the schedule file matches (5.1)
    assert b["verdict"]["h2"] == ("appdata should have run at 05:00 and did not. "
                                  "The schedule is on, but nothing was recorded.")


def test_verdict_overdue_swaps_second_sentence_when_crontab_stale(tmp_path):
    # 5.1: an overdue verdict with a stale crontab replaces its SECOND sentence with
    # the canonical crontab-stale wording (verbatim, same as the needs-you row / 7.3).
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="appdata", schedule="0 5 * * *")
    _end(cache, "appdata", _rid(12), outcome="ok",
         started="2026-09-12T05:00:01Z", finished="2026-09-12T05:04:00Z")
    Path(cache, "crontab").write_text("0 5 * * * /s/backup-job.sh STALE\n")   # on-disk != render
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["verdict"]["state"] == "overdue" and b["crontab_stale"] is True
    # first sentence unchanged, second sentence is the stale variant
    assert b["verdict"]["h2"] == ("appdata should have run at 05:00 and did not. "
                                  "The schedule file on disk does not match your jobs; "
                                  "restart the container.")


def test_verdict_no_jobs(tmp_path):
    cfg, cache = _cfg(tmp_path), _cache(tmp_path)
    b = status.board(cfg, cache, "/s", now=datetime(2026, 9, 15, 6, 0, tzinfo=UTC), tz=UTC,
                     source_root=str(tmp_path))
    assert b["verdict"]["state"] == "none"
    assert b["verdict"]["h2"] == "Nothing is being backed up yet."
    assert b["jobs"] == []


def test_verdict_failed_uses_error_class_sentence(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="manga", type="archive", source="media/manga", schedule="0 4 * * 0")
    _end(cache, "manga", _rid(13, 4, 0), outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion",
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["verdict"]["state"] == "failed" and b["verdict"]["job"] == "manga"
    # 5.1: the second sentence is the class's `verdict` string, not its cause.
    assert "Amazon refused a delete" in b["verdict"]["h2"]
    assert b["verdict"]["button"]["label"] == "Fix the permission →"
    assert b["verdict"]["button"]["href"] == "/setup/destination"


# =====================================================================
# Board sort (4.7): worst first — Failed, Overdue, Running, Paused, OK, Not run yet
# =====================================================================

def test_board_sorts_worst_first(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    (Path(root) / "media" / "ok").mkdir()
    (Path(root) / "media" / "od").mkdir()
    _mk_job(cfg, root, name="okjob", source="appdata", schedule="0 5 * * *")
    _mk_job(cfg, root, name="failjob", type="archive", source="media/manga", schedule="0 4 * * *")
    _mk_job(cfg, root, name="odjob", type="archive", source="media/od", schedule="0 5 * * *")
    # okjob: ran an hour ago
    _end(cache, "okjob", _rid(15), outcome="ok",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z")
    # failjob: last run failed recently (so Failed, not Overdue)
    _end(cache, "failjob", _rid(15, 4, 0), outcome="failed", duration=59,
         started="2026-09-15T04:00:01Z", finished="2026-09-15T04:01:00Z", error="boom")
    # odjob: last ran 3 days ago -> Overdue
    _end(cache, "odjob", _rid(12), outcome="ok",
         started="2026-09-12T05:00:01Z", finished="2026-09-12T05:04:00Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    order = [(j["name"], j["state"]) for j in b["jobs"]]
    assert order[0][1] == "FAILED"
    assert order[1][1] == "OVERDUE"
    assert order[2][1] == "OK"


# =====================================================================
# needs_you (7.4 / 7.7): blocker rows fill {dow} via dow_word and {since}
# =====================================================================

def test_needs_you_iam_blocker_fills_dow_and_since(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="manga", type="archive", source="media/manga", schedule="0 4 * * 0")
    # a prior OK run on 6 Sep, then a failing IAM run
    _end(cache, "manga", _rid(6, 4, 0, "0a0a"), outcome="ok",
         started="2026-09-06T04:00:00Z", finished="2026-09-06T04:20:00Z", duration=1200)
    _end(cache, "manga", _rid(13, 4, 0, "b21c"), outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion",
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    rows = [r for r in b["needs_you"] if r["level"] == "blocker"]
    assert rows, "expected an IAM blocker row"
    row = rows[0]
    assert row["job"] == "manga" and row["code"] == "iam-version-perms"
    assert row["strong"] == "manga cannot finish a run."
    # {dow} filled from dow_word("0 4 * * 0") == "Sunday"; {since} from the last OK run's date
    assert "every Sunday run stops at the same point" in row["text"]
    assert "6 September" in row["text"]
    assert row["fix"]["label"] == "Fix the permission →"


def test_needs_you_since_first_run_when_no_prior_ok(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="manga", type="archive", source="media/manga", schedule="0 4 * * 0")
    _end(cache, "manga", _rid(13, 4, 0, "b21c"), outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion",
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    row = [r for r in b["needs_you"] if r["level"] == "blocker"][0]
    assert "the first run" in row["text"]


def test_needs_you_crontab_stale_warning(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="movies", type="archive", source="media/manga", schedule="0 4 * * 0")
    Path(cache, "crontab").write_text("0 4 * * 0 /app/scripts/backup-job.sh STALE\n")
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    warns = [r for r in b["needs_you"] if r["level"] == "warning" and r.get("code") == "crontab-stale"]
    assert warns and b["crontab_stale"] is True


def test_needs_you_empty_when_all_ok(tmp_path):
    cfg, root, cache = _cfg(tmp_path), _root(tmp_path), _cache(tmp_path)
    _mk_job(cfg, root, name="appdata", schedule="0 5 * * *")
    _end(cache, "appdata", _rid(15), outcome="ok",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z")
    jobs_io.render_crontab(cfg, cache, "/s", source_root=root)   # same scripts_dir board uses
    now = datetime(2026, 9, 15, 5, 10, tzinfo=UTC)   # inside grace -> OK, not overdue
    b = status.board(cfg, cache, "/s", now=now, tz=UTC, source_root=root)
    assert b["needs_you"] == []
