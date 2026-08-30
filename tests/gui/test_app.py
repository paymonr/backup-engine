import pytest
from app.gui import create_app

@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app):
    return app.test_client()

def test_index_redirects_to_jobs(client):
    r = client.get("/")
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
