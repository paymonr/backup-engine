import pytest
from pathlib import Path
from app.gui import create_app


@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client, path):
    client.get(path)  # issues token into session
    with client.session_transaction() as s:
        return s["_csrf"]


def test_provision_home_renders_all_three_modes(client):
    r = client.get("/provision")
    assert r.status_code == 200
    assert b"Guided" in r.data and b"Scripted" in r.data and b"Automated" in r.data


def test_manual_render_returns_scoped_policy_json(client):
    token = _csrf(client, "/provision/manual")
    r = client.post("/provision/manual/render",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1"})
    assert r.status_code == 200
    assert b"arn:aws:s3:::acme/appdata/*" in r.data


def test_manual_render_requires_csrf(client):
    r = client.post("/provision/manual/render", data={"bucket": "b", "region": "r"})
    assert r.status_code == 400


def test_manual_render_rejects_missing_fields(client):
    token = _csrf(client, "/provision/manual")
    r = client.post("/provision/manual/render", data={"csrf": token, "bucket": "", "region": ""})
    assert r.status_code == 400


def test_scripted_panel_shows_setup_command(client):
    r = client.get("/provision/scripted")
    assert r.status_code == 200
    assert b"setup.sh" in r.data
