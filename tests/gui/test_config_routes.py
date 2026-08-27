import pytest
from pathlib import Path
from app.gui import create_app

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app): return app.test_client()

def _csrf(client):
    client.get("/config")  # issues token into session
    from app.gui import security
    with client.session_transaction() as s:
        return s["_csrf"]

def test_config_get_renders_known_fields(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert b"AWS_REGION" in r.data and b"S3_BUCKET" in r.data

def test_config_save_writes_backup_env_and_redirects(client, dirs):
    token = _csrf(client)
    r = client.post("/config", data={"csrf": token, "AWS_REGION": "eu-west-1", "S3_BUCKET": "b"})
    assert r.status_code in (302, 303)
    assert "AWS_REGION=eu-west-1" in Path(dirs["config"], "backup.env").read_text()

def test_config_save_secrets_are_write_only(client, dirs):
    token = _csrf(client)
    client.post("/config", data={"csrf": token, "AWS_REGION": "us-east-1", "S3_BUCKET": "b",
                                 "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s", "RESTIC_PASSWORD": "p"})
    r = client.get("/config")
    # status shows "set" but never the value
    assert b"AKIA" not in r.data and b"RESTIC_PASSWORD" in r.data

def test_config_post_without_csrf_is_rejected(client):
    r = client.post("/config", data={"AWS_REGION": "x"})
    assert r.status_code == 400
