import dataclasses
import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import runs

UTC = timezone.utc


# --- helpers ---------------------------------------------------------------

def _runs_file(cache, job):
    return Path(cache, "state", f"{job}.runs.jsonl")


def write_lines(cache, job, *records):
    p = _runs_file(cache, job)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def start_rec(id, job="appdata", kind="backup", started="2026-09-15T05:00:01Z", **extra):
    r = {"v": 1, "id": id, "job": job, "kind": kind, "event": "start", "trigger": "scheduled",
         "started_at": started, "pid": 4132, "log": f"logs/runs/{job}/{id}.log"}
    r.update(extra)
    return r


def end_rec(id, job="appdata", kind="backup", outcome="ok", finished="2026-09-15T05:04:13Z",
            duration=252, exit_code=0, error=None, **extra):
    r = {"v": 1, "id": id, "job": job, "kind": kind, "event": "end", "outcome": outcome,
         "finished_at": finished, "duration_s": duration, "exit_code": exit_code, "error": error,
         "command": "restic -r s3:... backup /backup/media/appdata --tag appdata"}
    r.update(extra)
    return r


def _rr(outcome="ok", kind="backup", dur=100, i=0, log=None):
    return runs.RunRecord(
        id=f"20260915T05000{i}Z-000{i}", job="appdata", kind=kind, trigger="scheduled",
        outcome=outcome, started_at=datetime(2026, 9, 15, 5, 0, i, tzinfo=UTC),
        finished_at=None, duration_s=dur, exit_code=0, error=None, log=log)


# --- folding: parse the Task-2 JSONL --------------------------------------

def test_fold_start_and_ok_end(tmp_path):
    c = str(tmp_path)
    rid = "20260915T050001Z-3f9a"
    write_lines(c, "appdata",
                start_rec(rid, type="versioned", storage_class="STANDARD"),
                end_rec(rid, snapshot_id="a81f3c2e", files_added=6, bytes_added=228589568,
                        files_total=533, bytes_total=56594862080))
    res = runs.read_runs(c, "appdata")
    assert res.corrupt_lines == 0 and res.truncated_head is False
    assert len(res.records) == 1
    r = res.records[0]
    assert r.outcome == "ok" and r.kind == "backup" and r.trigger == "scheduled"
    assert r.snapshot_id == "a81f3c2e" and r.files_added == 6 and r.bytes_total == 56594862080
    assert r.type == "versioned" and r.storage_class == "STANDARD"
    assert r.started_at == datetime(2026, 9, 15, 5, 0, 1, tzinfo=UTC)
    assert r.finished_at == datetime(2026, 9, 15, 5, 4, 13, tzinfo=UTC)
    assert r.duration_s == 252 and r.log.endswith(".log")


def test_fold_manga_prune_failure_record(tmp_path):
    c = str(tmp_path)
    mid = "20260913T040000Z-b21c"
    write_lines(c, "manga",
                start_rec(mid, job="manga", started="2026-09-13T04:00:00Z",
                          type="archive", storage_class="DEEP_ARCHIVE"),
                end_rec(mid, job="manga", outcome="failed", finished="2026-09-13T05:07:41Z",
                        duration=4061, exit_code=1, error="AccessDenied: s3:DeleteObjectVersion",
                        phase="prune", copied=True, files_added=1204, bytes_added=3328599040))
    r = runs.read_runs(c, "manga").records[0]
    assert r.outcome == "failed" and r.phase == "prune" and r.copied is True
    assert r.error == "AccessDenied: s3:DeleteObjectVersion"
    # cross-module carry-forward: this classifies to iam-version-perms
    from app.engine import errors
    assert errors.classify(r.error, r.exit_code, r.outcome).code == "iam-version-perms"


def test_start_without_end_is_running(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    res = runs.read_runs(c, "appdata", reconcile=False)   # observe raw state
    assert res.records[0].outcome == "running"
    assert res.records[0].finished_at is None


def test_end_without_start_is_truncated_head(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", end_rec("20260915T050001Z-3f9a", duration=252))
    res = runs.read_runs(c, "appdata", reconcile=False)
    assert res.truncated_head is True
    r = res.records[0]
    assert r.outcome == "ok"
    assert r.started_at == datetime(2026, 9, 15, 5, 4, 13, tzinfo=UTC) - timedelta(seconds=252)


def test_last_end_wins(tmp_path):
    c = str(tmp_path)
    rid = "20260915T050001Z-3f9a"
    write_lines(c, "appdata", start_rec(rid),
                end_rec(rid, outcome="failed", error="first"),
                end_rec(rid, outcome="ok", error=None))
    assert runs.read_runs(c, "appdata").records[0].outcome == "ok"


def test_corrupt_and_v2_and_idless_lines_skipped_and_counted(tmp_path):
    c = str(tmp_path)
    p = _runs_file(c, "appdata")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"v": 2, "id": "x", "event": "start"}) + "\n")   # v > 1
        fh.write(json.dumps({"v": 1, "event": "start"}) + "\n")              # missing id
        fh.write(json.dumps(start_rec("20260915T050001Z-3f9a")) + "\n")
        fh.write(json.dumps(end_rec("20260915T050001Z-3f9a")) + "\n")
    res = runs.read_runs(c, "appdata", reconcile=False)
    assert res.corrupt_lines == 3
    assert len(res.records) == 1


def test_records_sorted_newest_first(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata",
                start_rec("20260915T050001Z-3f9a", started="2026-09-15T05:00:01Z"),
                end_rec("20260915T050001Z-3f9a"),
                start_rec("20260916T050001Z-4b2c", started="2026-09-16T05:00:01Z"),
                end_rec("20260916T050001Z-4b2c", finished="2026-09-16T05:04:00Z"))
    res = runs.read_runs(c, "appdata")
    assert res.records[0].id == "20260916T050001Z-4b2c"


# --- is_locked -------------------------------------------------------------

def test_is_locked_probe(tmp_path):
    c = str(tmp_path)
    assert runs.is_locked(c, "appdata") is False
    lp = runs.lock_path(c, "appdata")
    lp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lp, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert runs.is_locked(c, "appdata") is True
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    assert runs.is_locked(c, "appdata") is False


# --- reconcile / active_run ------------------------------------------------

def test_reconcile_aborts_running_when_lock_free(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    assert runs.reconcile(c, "appdata") == 1
    res = runs.read_runs(c, "appdata", reconcile=False)
    assert res.records[0].outcome == "aborted"
    assert res.records[0].error and "stopped without reporting" in res.records[0].error
    assert runs.reconcile(c, "appdata") == 0   # idempotent


def test_reconcile_leaves_running_and_active_run_when_lock_held(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    lp = runs.lock_path(c, "appdata")
    lp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lp, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert runs.reconcile(c, "appdata") == 0
        assert runs.read_runs(c, "appdata", reconcile=False).records[0].outcome == "running"
        ar = runs.active_run(c, "appdata")
        assert ar is not None and ar.outcome == "running"
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    assert runs.active_run(c, "appdata") is None   # lock free now


def test_reconcile_force_aborts_even_when_locked(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    lp = runs.lock_path(c, "appdata")
    lp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lp, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert runs.reconcile(c, "appdata", force=True) == 1
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    assert runs.read_runs(c, "appdata", reconcile=False).records[0].outcome == "aborted"


def test_read_runs_reconcile_true_aborts_dangling(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    res = runs.read_runs(c, "appdata")   # reconcile=True default
    assert res.records[0].outcome == "aborted"


# --- backfill --------------------------------------------------------------

def _legacy(cache, job, **kw):
    st = Path(cache, "state")
    st.mkdir(parents=True, exist_ok=True)
    data = {"last_run": "2026-09-13T05:07:41Z", "outcome": "success", "type": "archive",
            "snapshot_id": "a81f3c2e11223344", "duration_s": 4061, "error": "", "exit_code": 0}
    data.update(kw)
    (st / f"{job}.json").write_text(json.dumps(data))


def test_backfill_record_from_legacy_state(tmp_path):
    c = str(tmp_path)
    _legacy(c, "movies")
    rec = runs.backfill_record(c, "movies")
    assert rec.id == "20260913T050741Z-0000"
    assert rec.backfilled is True and rec.outcome == "ok" and rec.type == "archive"
    assert rec.snapshot_id == "a81f3c2e"    # first 8 chars
    assert rec.finished_at == datetime(2026, 9, 13, 5, 7, 41, tzinfo=UTC)
    assert rec.started_at == datetime(2026, 9, 13, 5, 7, 41, tzinfo=UTC) - timedelta(seconds=4061)


def test_backfill_failure_outcome(tmp_path):
    c = str(tmp_path)
    _legacy(c, "movies", outcome="failure", snapshot_id="", duration_s=10, error="boom", exit_code=1)
    rec = runs.backfill_record(c, "movies")
    assert rec.outcome == "failed" and rec.error == "boom" and rec.exit_code == 1


def test_read_runs_returns_backfill_when_no_runs_file(tmp_path):
    c = str(tmp_path)
    _legacy(c, "movies")
    res = runs.read_runs(c, "movies")
    assert len(res.records) == 1 and res.records[0].backfilled is True


def test_materialize_backfill_writes_one_line_once(tmp_path):
    c = str(tmp_path)
    _legacy(c, "movies")
    assert runs.materialize_backfill(c, "movies") is True
    p = runs.runs_path(c, "movies")
    assert p.exists() and len(p.read_text().strip().splitlines()) == 1
    res = runs.read_runs(c, "movies", reconcile=False)
    assert res.truncated_head is False   # the line carries started_at
    assert len(res.records) == 1 and res.records[0].backfilled and res.records[0].outcome == "ok"
    # second call is a no-op (runs file now present)
    assert runs.materialize_backfill(c, "movies") is False
    assert len(runs.read_runs(c, "movies", reconcile=False).records) == 1


def test_missing_everything_is_empty(tmp_path):
    res = runs.read_runs(str(tmp_path), "ghost")
    assert res.records == [] and res.corrupt_lines == 0


# --- boot ------------------------------------------------------------------

def test_boot_materializes_and_reconciles(tmp_path):
    c = str(tmp_path)
    _legacy(c, "movies")
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    summary = runs.boot(c, ["movies", "appdata"])
    assert runs.runs_path(c, "movies").exists()
    assert runs.read_runs(c, "appdata", reconcile=False).records[0].outcome == "aborted"
    assert isinstance(summary, dict)


def test_boot_never_raises_on_corrupt_file(tmp_path):
    c = str(tmp_path)
    st = Path(c, "state")
    st.mkdir(parents=True)
    (st / "appdata.runs.jsonl").write_text("garbage not json\n")
    summary = runs.boot(c, ["appdata"])
    assert isinstance(summary, dict)


def test_all_jobs_with_runs(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"))
    write_lines(c, "manga", start_rec("20260913T040000Z-b21c", job="manga"))
    runs.append_event(c, None, {"v": 1, "id": "20260915T060000Z-aaaa", "job": None,
                                "kind": "usage-refresh", "event": "start",
                                "started_at": "2026-09-15T06:00:00Z"})
    jobs = runs.all_jobs_with_runs(c)
    assert set(jobs) == {"appdata", "manga"}   # _system excluded


# --- read_all --------------------------------------------------------------

def test_read_all_merges_system_and_filters(tmp_path):
    c = str(tmp_path)
    write_lines(c, "appdata", start_rec("20260915T050001Z-3f9a"), end_rec("20260915T050001Z-3f9a"))
    runs.append_event(c, None, {"v": 1, "id": "20260915T060000Z-aaaa", "job": None,
                                "kind": "usage-refresh", "event": "start",
                                "started_at": "2026-09-15T06:00:00Z"})
    runs.append_event(c, None, {"v": 1, "id": "20260915T060000Z-aaaa", "job": None,
                                "kind": "usage-refresh", "event": "end", "outcome": "ok",
                                "finished_at": "2026-09-15T06:00:05Z", "duration_s": 5,
                                "exit_code": 0, "error": None})
    allrecs = runs.read_all(c, ["appdata"])
    assert {"usage-refresh", "backup"} <= {r.kind for r in allrecs}
    only = runs.read_all(c, ["appdata"], kind="backup")
    assert only and all(r.kind == "backup" for r in only)


# --- median / streak -------------------------------------------------------

def test_median_none_below_three():
    assert runs.median_duration_s([_rr(dur=100, i=0), _rr(dur=200, i=1)]) is None


def test_median_of_ok_backups():
    assert runs.median_duration_s([_rr(dur=100, i=0), _rr(dur=200, i=1), _rr(dur=300, i=2)]) == 200.0


def test_median_ignores_operations():
    recs = [_rr(dur=100, i=0), _rr(dur=200, i=1), _rr(dur=300, i=2),
            _rr(kind="restore", dur=99999, i=3)]
    assert runs.median_duration_s(recs) == 200.0


def test_streak_counts_consecutive_ok_backups_skipping_operations():
    recs = [_rr(outcome="ok", i=0), _rr(outcome="ok", i=1),
            _rr(kind="restore", outcome="failed", i=2),   # an operation, skipped
            _rr(outcome="ok", i=3), _rr(outcome="failed", i=4)]
    assert runs.streak(recs) == 3


def test_streak_zero_when_newest_backup_failed():
    assert runs.streak([_rr(outcome="failed", i=0), _rr(outcome="ok", i=1)]) == 0


# --- ids / pending ---------------------------------------------------------

def test_valid_and_new_run_id():
    assert runs.valid_run_id("20260915T050001Z-3f9a")
    assert runs.valid_run_id("20260913T050741Z-0000")   # reserved backfill suffix is a valid format
    assert not runs.valid_run_id("nope")
    assert runs.valid_run_id(runs.new_run_id())


def test_run_id_started_at():
    assert runs.run_id_started_at("20260915T050001Z-3f9a") == datetime(2026, 9, 15, 5, 0, 1, tzinfo=UTC)
    assert runs.run_id_started_at("bad") is None


def test_is_pending_window():
    now = datetime(2026, 9, 15, 5, 5, 0, tzinfo=UTC)
    assert runs.is_pending("20260915T050001Z-3f9a", now=now) is True    # 4 min old
    assert runs.is_pending("20260915T044001Z-3f9a", now=now) is False   # >10 min old
    assert runs.is_pending("bad", now=now) is False


# --- read_log path confinement ---------------------------------------------

def test_read_log_reads_within_logs_runs(tmp_path):
    c = str(tmp_path)
    d = Path(c, "logs", "runs", "appdata")
    d.mkdir(parents=True)
    (d / "20260915T050001Z-3f9a.log").write_text("hello\nworld\n")
    rec = dataclasses.replace(_rr(), log="logs/runs/appdata/20260915T050001Z-3f9a.log")
    text, off, eof = runs.read_log(c, rec)
    assert "hello" in text and eof is True and off > 0


def test_read_log_refuses_paths_outside_logs_runs(tmp_path):
    c = str(tmp_path)
    rec = dataclasses.replace(_rr(), log="../../etc/passwd")
    text, off, eof = runs.read_log(c, rec)
    assert text == "" and eof is True


# --- paused outcome + attempts ---------------------------------------------

def test_paused_outcome_and_attempts_fold(tmp_path):
    from app.engine import runs
    c = str(tmp_path)
    runs.append_event(c, "cfg", {"v":1,"id":"20260921T050000Z-aaaa","job":"cfg","kind":"backup","event":"start","started_at":"2026-09-21T05:00:00Z"})
    runs.append_event(c, "cfg", {"v":1,"id":"20260921T050000Z-aaaa","job":"cfg","kind":"backup","event":"end","outcome":"paused","finished_at":"2026-09-21T05:01:00Z","attempts":2})
    rec = runs.read_runs(c, "cfg", reconcile=False).records[0]
    assert rec.outcome == "paused" and rec.attempts == 2


# --- record_system -----------------------------------------------------------

def test_record_system_writes_a_start_end_pair_and_a_log(tmp_path):
    rid = runs.record_system(str(tmp_path), kind="s3-rules", summary="S3 rules updated · b",
                             lines=["media/m/: old versions removed 180 days after being replaced"])
    events = [json.loads(l) for l in (tmp_path / "state" / "_system.runs.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["event"]) for e in events] == [("s3-rules", "start"), ("s3-rules", "end")]
    assert events[1]["outcome"] == "ok" and events[0]["id"] == rid
    log = (tmp_path / events[0]["log"]).read_text()
    assert "S3 rules updated · b" in log and "180 days" in log
