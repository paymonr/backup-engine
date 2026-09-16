"""Activity — `/activity` (+ `.json`) (spec 5.5, 8.4). Task 10.

Reverse-chronological feed of every operation across all jobs — scheduled and
manual runs, restores/warm-ups/downloads/test restores, and system operations
(`_system`: usage refresh, billing check, destination probe, and the `provision`
records the provisioning success paths write). Filters (`job`, `kind`, `outcome`,
`limit`) submit as query parameters; every row resolves to its record
(`/jobs/<name>/runs/<id>` for a job, `/activity/<id>` for a system op); footer
links to the raw shared log.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.gui import config_io, create_app, jobs_io
import app.gui.routes as routes

UTC = timezone.utc


# --- seeding helpers --------------------------------------------------------

def _end(cache, name, rid, *, outcome="ok", kind="backup", trigger="scheduled",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z", duration=252, **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": trigger, "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _system_end(cache, rid, *, kind="usage-refresh", outcome="ok",
                started="2026-09-16T09:04:00Z", finished="2026-09-16T09:04:12Z", duration=12):
    runs.append_event(cache, None, {"v": 1, "id": rid, "job": None, "kind": kind,
                                    "event": "start", "trigger": "manual", "started_at": started})
    runs.append_event(cache, None, {"v": 1, "id": rid, "job": None, "kind": kind, "event": "end",
                                    "outcome": outcome, "finished_at": finished,
                                    "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1})


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
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
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


# ids in ascending time order so newest-first ordering is checkable by position
BACKUP_ID = "20260915T050001Z-0001"       # oldest
RESTORE_ID = "20260916T050001Z-0002"      # middle
SYSTEM_ID = "20260916T090400Z-0003"       # newest


@pytest.fixture
def seeded(app, source_root):
    cfg, cache = app.config["CONFIG_DIR"], app.config["CACHE_DIR"]
    jobs_io.upsert(cfg, {"name": "appdata", "type": "versioned", "source": "appdata",
                         "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                         "created_at": "2026-09-01T00:00:00Z"}, source_root=str(source_root))
    # a scheduled backup, then a manual restore, then a system usage-refresh
    _end(cache, "appdata", BACKUP_ID, outcome="ok", kind="backup", trigger="scheduled",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z")
    _end(cache, "appdata", RESTORE_ID, outcome="ok", kind="restore", trigger="manual",
         started="2026-09-16T05:00:01Z", finished="2026-09-16T05:20:00Z", duration=1199)
    _system_end(cache, SYSTEM_ID, kind="usage-refresh", outcome="ok")
    return app


# --- listing + ordering -----------------------------------------------------

def test_lists_backup_restore_and_system_newest_first(client, seeded):
    body = client.get("/activity").get_data(as_text=True)
    assert "What this machine has done" in body
    # all three present
    for frag in ("scheduled run", "restore", "usage refresh"):
        assert frag in body
    # newest first: the system op (16 Sep 09:04) before the restore (16 Sep 05:00)
    # before the backup (15 Sep). Compare the record-link positions.
    p_sys = body.index(f"/activity/{SYSTEM_ID}")
    p_res = body.index(f"/jobs/appdata/runs/{RESTORE_ID}")
    p_bak = body.index(f"/jobs/appdata/runs/{BACKUP_ID}")
    assert p_sys < p_res < p_bak


def test_json_shape_and_record_links(client, seeded):
    j = client.get("/activity.json").get_json()
    assert "generated_at" in j and "tz" in j and "filters" in j
    items = {it["id"]: it for it in j["items"]}
    assert items[BACKUP_ID]["record"] == f"/jobs/appdata/runs/{BACKUP_ID}"
    assert items[BACKUP_ID]["what"] == "scheduled run"
    assert items[RESTORE_ID]["what"] == "restore"
    # system record: job null, resolves via /activity/<id>
    assert items[SYSTEM_ID]["job"] is None
    assert items[SYSTEM_ID]["record"] == f"/activity/{SYSTEM_ID}"
    assert items[SYSTEM_ID]["what"] == "usage refresh"


def test_system_row_record_resolves(client, seeded):
    j = client.get("/activity.json").get_json()
    rec = next(it for it in j["items"] if it["id"] == SYSTEM_ID)
    assert client.get(rec["record"]).status_code == 200


# --- filters ----------------------------------------------------------------

def test_filter_kind_runs(client, seeded):
    ids = [it["id"] for it in client.get("/activity.json?kind=runs").get_json()["items"]]
    assert ids == [BACKUP_ID]


def test_filter_kind_restores(client, seeded):
    ids = [it["id"] for it in client.get("/activity.json?kind=restores").get_json()["items"]]
    assert ids == [RESTORE_ID]


def test_filter_kind_setup(client, seeded):
    ids = [it["id"] for it in client.get("/activity.json?kind=setup").get_json()["items"]]
    assert ids == [SYSTEM_ID]


def test_filter_job_excludes_system(client, seeded):
    ids = [it["id"] for it in client.get("/activity.json?job=appdata").get_json()["items"]]
    assert set(ids) == {BACKUP_ID, RESTORE_ID}
    assert SYSTEM_ID not in ids


def test_filter_outcome_failed(client, seeded):
    cache = client.application.config["CACHE_DIR"]
    _end(cache, "appdata", "20260917T050001Z-0009", outcome="failed",
         started="2026-09-17T05:00:01Z", finished="2026-09-17T05:01:00Z", duration=59,
         error="boom")
    ids = [it["id"] for it in client.get("/activity.json?outcome=failed").get_json()["items"]]
    assert ids == ["20260917T050001Z-0009"]


def test_limit_is_honoured(client, seeded):
    j = client.get("/activity.json?limit=1").get_json()
    assert len(j["items"]) == 1
    assert j["items"][0]["id"] == SYSTEM_ID                     # newest
    assert j["filters"]["limit"] == 1


# --- footer + empty ---------------------------------------------------------

def test_raw_shared_log_link(client, seeded):
    body = client.get("/activity").get_data(as_text=True)
    assert "Raw shared log" in body
    assert 'href="/logs"' in body


def test_empty_activity(client, app, source_root):
    body = client.get("/activity").get_data(as_text=True)
    assert "Nothing has run yet" in body


# --- provisioning writes a `provision` record (5.5) -------------------------

def _csrf(client, path="/setup/destination"):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]


def test_provision_validate_success_writes_provision_record(client, app, monkeypatch):
    cache = app.config["CACHE_DIR"]
    monkeypatch.setattr(routes.provision, "validate_runtime_key", lambda *a, **k: None)
    t = _csrf(client)
    r = client.post("/setup/destination/validate", data={
        "csrf": t, "bucket": "bw-backups", "region": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIAXX", "AWS_SECRET_ACCESS_KEY": "secretzz"})
    assert r.status_code in (302, 303)
    # exactly one provision record in _system.runs.jsonl
    recs = [rec for rec in runs.read_runs(cache, "_system", reconcile=False).records
            if rec.kind == "provision"]
    assert len(recs) == 1
    assert recs[0].outcome == "ok"
    # and one Activity row for it, labelled "destination setup"
    j = client.get("/activity.json?kind=setup").get_json()
    setups = [it for it in j["items"] if it["kind"] == "provision"]
    assert len(setups) == 1
    assert setups[0]["what"] == "destination setup"
    assert client.get(setups[0]["record"]).status_code == 200
