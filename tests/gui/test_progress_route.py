# tests/gui/test_progress_route.py — GET /jobs/<name>/progress.json (spec 5.5).
# Live progress for a running job; {"running": false} when idle; 404 for unknown.
import json
import pathlib

import pytest

from app.engine import runs
from app.gui import config_io, create_app


@pytest.fixture
def app(tmp_path, template_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    (cfg / "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "appdata_backup", "type": "versioned", "source": "appdata",
         "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
         "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}]}))
    return create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache"),
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(tmp_path / "src"), "SECRET_KEY": "t", "TESTING": True,
                       "PRICES_LIVE": False})


@pytest.fixture
def client(app):
    return app.test_client()


def test_idle_job_reports_not_running(client):
    r = client.get("/jobs/appdata_backup/progress.json")
    assert r.status_code == 200
    assert r.get_json() == {"running": False}


def test_unknown_job_404s(client):
    r = client.get("/jobs/nope/progress.json")
    assert r.status_code == 404
    assert r.get_json() == {"running": False}


def test_running_restic_job_reports_percent(client, app, monkeypatch):
    cache = app.config["CACHE_DIR"]
    state = pathlib.Path(cache, "state"); state.mkdir(parents=True)
    (state / "appdata_backup-last.jsonl").write_text(
        '{"message_type":"status","seconds_elapsed":100,"percent_done":0.5,'
        '"total_files":533,"files_done":260,"total_bytes":10000000000,"bytes_done":5000000000}\n')
    monkeypatch.setattr(runs, "active_run", lambda c, j: runs.RunRecord(
        id="20260919T155129Z-9f96", job="appdata_backup", kind="backup", trigger="manual",
        outcome="running", started_at=None, finished_at=None, duration_s=None,
        exit_code=None, error=None))
    r = client.get("/jobs/appdata_backup/progress.json")
    assert r.status_code == 200
    d = r.get_json()
    assert d["running"] is True and d["engine"] == "restic"
    assert d["percent"] == 50.0 and d["bytes_total"] == 10_000_000_000
