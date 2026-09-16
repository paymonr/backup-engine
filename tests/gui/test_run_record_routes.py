"""Run record — `/jobs/<name>/runs/<run_id>` (+ `.json`, `/log`) and the system
record detail `/activity/<run_id>` (spec 5.3, 5.5, 8.3). Task 10.

The crux is the PENDING window (5.3): `ops.launch` pre-assigns the run id and
redirects BEFORE the detached child writes its start line, so the first GET
routinely finds no record. A well-formed id inside `PENDING_WINDOW_S` (600 s)
with no record must render 200 `Starting…`; a malformed id, an unknown job, or a
well-formed id older than the window must 404. System operations (`_system`,
7.7.3) have their own detail route and NO pending state (sysop appends its start
event synchronously).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.gui import config_io, create_app, jobs_io

UTC = timezone.utc


# --- seeding helpers --------------------------------------------------------

def _end(cache, name, rid, *, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", trigger="scheduled",
         log=None, **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": trigger, "started_at": started,
                                    **({"log": log} if log else {})})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _start_only(cache, name, rid, *, kind="backup", started="2026-09-15T05:00:01Z",
                trigger="manual", log=None):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": trigger, "started_at": started,
                                    **({"log": log} if log else {})})


def _system_end(cache, rid, *, kind="usage-refresh", outcome="ok",
                started="2026-09-15T09:04:00Z", finished="2026-09-15T09:04:12Z",
                duration=12, log=None):
    runs.append_event(cache, None, {"v": 1, "id": rid, "job": None, "kind": kind,
                                    "event": "start", "trigger": "manual", "started_at": started,
                                    **({"log": log} if log else {})})
    runs.append_event(cache, None, {"v": 1, "id": rid, "job": None, "kind": kind, "event": "end",
                                    "outcome": outcome, "finished_at": finished,
                                    "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1})


def _yesterday_id(now=None):
    d = (now or datetime.now(UTC)) - timedelta(days=1)
    return d.strftime("%Y%m%dT%H%M%SZ") + "-dead"


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    (root / "appdata").mkdir(parents=True)
    (root / "media" / "manga").mkdir(parents=True)
    return root


@pytest.fixture
def dirs(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True); (cache / "logs").mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek",
                                       "RESTIC_PASSWORD": "a-real-long-random-passphrase"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    return {"config": str(cfg), "cache": str(cache)}


@pytest.fixture
def template_path():
    return str(Path(__file__).resolve().parents[2] / "config" / "backup.env.example")


@pytest.fixture
def app(dirs, source_root, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                       "PRICES_LIVE": False})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def example(app, source_root):
    cfg, cache = app.config["CONFIG_DIR"], app.config["CACHE_DIR"]
    jobs_io.upsert(cfg, {"name": "appdata", "type": "versioned", "source": "appdata",
                         "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                         "created_at": "2026-09-01T00:00:00Z",
                         "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}},
                   source_root=str(source_root))
    jobs_io.upsert(cfg, {"name": "manga", "type": "archive", "source": "media/manga",
                         "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
                         "created_at": "2026-09-01T00:00:00Z"},
                   source_root=str(source_root))
    # A few OK appdata backups so median_duration_s has enough samples.
    for i in range(4):
        d = datetime(2026, 9, 10 + i, 5, 0, 1, tzinfo=UTC)
        rid = f"{d:%Y%m%dT%H%M%S}Z-{i:04d}"
        _end(cache, "appdata", rid, outcome="ok",
             started=f"{d:%Y-%m-%dT%H:%M:%S}Z", finished=f"{d + timedelta(seconds=252):%Y-%m-%dT%H:%M:%S}Z",
             duration=252, snapshot_id="a81f3c2e", files_changed=6, bytes_added=228589568,
             files_total=533, bytes_total=56594862080)
    # manga: a prune-permission failure with the verbatim IAM error (5.2 fixture).
    _end(cache, "manga", "20260913T040000Z-b21c", outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion", phase="prune", copied=True,
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    return app


APPDATA_OK = "20260910T050001Z-0000"
MANGA_FAILED = "20260913T040000Z-b21c"


# --- pending state (5.3) ----------------------------------------------------

def test_fresh_id_renders_pending_starting(client, example):
    rid = runs.new_run_id()                       # minted just now, no record yet
    r = client.get(f"/jobs/appdata/runs/{rid}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Starting" in body                     # h1 `Starting…`
    assert "tok-running" in body
    assert "waiting for it to report in" in body


def test_pending_json_is_200_pending(client, example):
    rid = runs.new_run_id()
    r = client.get(f"/jobs/appdata/runs/{rid}.json")
    assert r.status_code == 200
    j = r.get_json()
    assert j["outcome"] == "pending"
    assert j["live"] is True
    assert j["id"] == rid
    assert j["job"] == "appdata"
    assert j["pending_since"]


def test_pending_log_is_empty_200(client, example):
    rid = runs.new_run_id()
    r = client.get(f"/jobs/appdata/runs/{rid}/log")
    assert r.status_code == 200
    assert r.get_data(as_text=True) == ""
    assert r.headers["X-Log-Offset"] == "0"
    assert r.headers["X-Log-Eof"] == "0"


def test_post_run_redirect_target_is_never_404(client, example):
    # The 5.3 acceptance: the run-record URL a launch redirects to must resolve
    # immediately even though the child has not written its start line yet.
    rid = runs.new_run_id()
    assert client.get(f"/jobs/appdata/runs/{rid}").status_code == 200


# --- live record (start appended, no end) -----------------------------------

def test_start_only_renders_live_running(client, example):
    rid = runs.new_run_id()
    _start_only(client.application.config["CACHE_DIR"], "appdata", rid,
                started=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    r = client.get(f"/jobs/appdata/runs/{rid}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Running" in body
    assert "tok-running" in body
    j = client.get(f"/jobs/appdata/runs/{rid}.json").get_json()
    assert j["outcome"] == "running"
    assert j["live"] is True


# --- completed record -------------------------------------------------------

def test_failed_record_renders_error_class(client, example):
    r = client.get(f"/jobs/manga/runs/{MANGA_FAILED}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "tok-failed" in body
    assert "AccessDenied: s3:DeleteObjectVersion" in body      # verbatim error line
    assert "Fix the permission →" in body                      # errors.classify fix
    assert "/setup/destination" in body                        # the fix route


def test_error_class_reads_log_tail_not_head(client, example):
    # `_error_class_dict` must classify off the log TAIL, not the first 64 KB
    # (routes.py:842, mirroring the progress reader at routes.py:817). Build a
    # log well over 64 KB whose decisive error line sits only at the very end
    # -- past the first 64 KB -- with no `error` field on the record, so
    # classification is forced through the log-tail path (errors.classify
    # step 3). Reading the head would see only padding and misclassify as
    # `unknown`; reading the tail finds "no space left on device".
    cache = client.application.config["CACHE_DIR"]
    rid = "20260915T060000Z-7f7f"
    log_rel = f"logs/runs/appdata/{rid}.log"
    log_abs = Path(cache, log_rel)
    log_abs.parent.mkdir(parents=True, exist_ok=True)
    padding = "INFO padding line to pad the log past sixty-five thousand bytes so the head read misses the tail\n"
    log_abs.write_text(padding * 800 + "no space left on device\n")
    assert log_abs.stat().st_size > 65536
    _end(cache, "appdata", rid, outcome="failed", duration=99, log=log_rel)
    j = client.get(f"/jobs/appdata/runs/{rid}.json").get_json()
    assert j["error_class"]["code"] == "no-space"


def test_ok_record_renders_and_json_shape(client, example):
    r = client.get(f"/jobs/appdata/runs/{APPDATA_OK}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "tok-ok" in body
    assert "a81f3c2e" in body                                   # restore point (snapshot id)
    j = client.get(f"/jobs/appdata/runs/{APPDATA_OK}.json").get_json()
    assert j["id"] == APPDATA_OK
    assert j["outcome"] == "ok"
    assert j["live"] is False
    assert "median_s" in j and "error_class" in j and "progress" in j
    assert j["started_at"].endswith("Z")                        # ISO datetime


# --- 404 cases (malformed / unknown job / stale) ----------------------------

def test_malformed_id_is_404(client, example):
    r = client.get("/jobs/appdata/runs/not-an-id")
    assert r.status_code == 404
    assert b"Back to the Board" in r.data                       # themed error page


def test_malformed_id_json_is_404_json(client, example):
    r = client.get("/jobs/appdata/runs/not-an-id.json")
    assert r.status_code == 404
    assert "error" in r.get_json()


def test_unknown_job_is_404(client, example):
    rid = runs.new_run_id()
    assert client.get(f"/jobs/ghost/runs/{rid}").status_code == 404


def test_stale_id_outside_window_is_404(client, example):
    rid = _yesterday_id()
    assert client.get(f"/jobs/appdata/runs/{rid}").status_code == 404
    assert client.get(f"/jobs/appdata/runs/{rid}.json").status_code == 404


def test_system_job_name_is_404(client, example):
    # `_system` is not a valid job name: /jobs/_system/runs/<id> must 404.
    rid = runs.new_run_id()
    assert client.get(f"/jobs/_system/runs/{rid}").status_code == 404


# --- the /log tail ----------------------------------------------------------

def test_log_endpoint_tails_the_per_run_log(client, example):
    cache = client.application.config["CACHE_DIR"]
    rid = "20260915T050001Z-1c8e"
    log_rel = f"logs/runs/appdata/{rid}.log"
    log_abs = Path(cache, log_rel)
    log_abs.parent.mkdir(parents=True, exist_ok=True)
    log_abs.write_text("line one\nline two\n")
    _end(cache, "appdata", rid, outcome="ok", log=log_rel)
    r = client.get(f"/jobs/appdata/runs/{rid}/log")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    assert "line one" in r.get_data(as_text=True)
    assert r.headers["X-Log-Eof"] == "1"
    # offset honoured: a request at EOF returns nothing more
    off = int(r.headers["X-Log-Offset"])
    r2 = client.get(f"/jobs/appdata/runs/{rid}/log?offset={off}")
    assert r2.get_data(as_text=True) == ""


def test_log_endpoint_404_when_record_has_no_log(client, example):
    r = client.get(f"/jobs/appdata/runs/{APPDATA_OK}/log")
    assert r.status_code == 404
    assert "error" in r.get_json()


# --- system record detail (/activity/<id>) ----------------------------------

def test_system_record_detail_renders(client, example):
    cache = client.application.config["CACHE_DIR"]
    rid = "20260915T090400Z-5a5a"
    log_rel = f"logs/runs/_system/{rid}.log"
    Path(cache, log_rel).parent.mkdir(parents=True, exist_ok=True)
    Path(cache, log_rel).write_text("2026-09-15T09:04:00Z usage-refresh ok\n")
    _system_end(cache, rid, kind="usage-refresh", log=log_rel)
    r = client.get(f"/activity/{rid}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "system" in body
    assert "usage refresh" in body.lower()
    assert "All activity" in body                              # back link, not `← <job>`
    j = client.get(f"/activity/{rid}.json").get_json()
    assert j["job"] is None
    assert j["outcome"] == "ok"
    # spec 8.3: a `_system` record carries NO snapshot_id and NO median_s -- absent
    # keys, not null values.
    assert "snapshot_id" not in j
    assert "median_s" not in j
    log = client.get(f"/activity/{rid}/log")
    assert log.status_code == 200
    assert "usage-refresh ok" in log.get_data(as_text=True)


def test_system_record_has_no_pending_state(client, example):
    # A fresh valid id under _system with no record → 404 (no `Starting…`).
    rid = runs.new_run_id()
    assert client.get(f"/activity/{rid}").status_code == 404
    assert client.get(f"/activity/{rid}.json").status_code == 404


def test_system_malformed_id_is_404(client, example):
    assert client.get("/activity/not-an-id").status_code == 404
