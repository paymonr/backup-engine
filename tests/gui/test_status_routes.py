import json
import pytest
from pathlib import Path
from app.gui import create_app, runner

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app): return app.test_client()

def _csrf(client):
    client.get("/status")
    with client.session_transaction() as s:
        return s["_csrf"]

def test_status_shows_last_run(client, dirs):
    Path(dirs["cache"], "state", "appdata.json").write_text(json.dumps({"outcome": "success", "snapshot_id": "abc123", "duration_s": 5}))
    r = client.get("/status")
    assert r.status_code == 200
    assert b"success" in r.data and b"abc123" in r.data

def test_run_triggers_backup(client, monkeypatch):
    called = {}
    monkeypatch.setattr(runner, "trigger_backup", lambda scripts, pipeline, env=None: called.setdefault("p", pipeline))
    token = _csrf(client)
    r = client.post("/run/media", data={"csrf": token})
    assert r.status_code in (302, 303)
    assert called["p"] == "media"

def test_run_unknown_pipeline_404(client):
    token = _csrf(client)
    assert client.post("/run/bogus", data={"csrf": token}).status_code == 404

def test_run_without_csrf_rejected(client):
    assert client.post("/run/media", data={}).status_code == 400

def test_logs_returns_tail(client, dirs):
    Path(dirs["cache"], "logs", "backup-engine.log").write_text("a\nb\nc\n")
    r = client.get("/logs?tail=2")
    assert r.status_code == 200 and r.data.decode().splitlines() == ["b", "c"]
