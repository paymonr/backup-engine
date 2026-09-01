import pytest
from pathlib import Path
from app.gui import create_app, config_io

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app):
    return app.test_client()

def _make_app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

def test_index_redirects_to_provision_when_unprovisioned(client):
    # fresh install: no runtime key / bucket yet -> land on the setup wizard
    r = client.get("/")
    assert r.status_code in (301, 302)
    assert "/provision" in r.headers["Location"]

def test_index_redirects_to_jobs_when_provisioned(dirs, template_path):
    config_io.write_secrets(dirs["config"],
                            {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    Path(dirs["config"], "backup.env").write_text("S3_BUCKET=acme\nAWS_REGION=us-east-1\n")
    r = _make_app(dirs, template_path).test_client().get("/")
    assert r.status_code in (301, 302)
    assert "/jobs" in r.headers["Location"]

def test_no_auth_banner_present(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert b"no authentication" in r.data.lower()

def test_csrf_roundtrip(app):
    from app.gui import security
    with app.test_request_context():
        from flask import session
        token = security.issue_csrf()
        assert security.verify_csrf(token) is True
        assert security.verify_csrf("wrong") is False
