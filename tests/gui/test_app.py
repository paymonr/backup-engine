import json
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
                       "SECRET_KEY": "test", "TESTING": True, "PRICES_LIVE": False})


def _csrf(client, path="/setup"):
    client.get(path)  # issues a token into the session
    with client.session_transaction() as s:
        return s["_csrf"]


def _write_jobs(dirs, jobs):
    Path(dirs["config"], "jobs.json").write_text(json.dumps({"jobs": jobs}))


def test_index_redirects_to_setup_when_unprovisioned(client):
    # fresh install: no runtime key / bucket yet -> land on Setup (spec 5.1 302 → /setup)
    r = client.get("/")
    assert r.status_code in (301, 302)
    assert "/setup" in r.headers["Location"]

def test_index_renders_board_when_provisioned(dirs, template_path):
    # 5.1 / ruling R-H: once set up, `/` renders the Board (was a redirect to /jobs).
    config_io.write_secrets(dirs["config"],
                            {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    Path(dirs["config"], "backup.env").write_text("S3_BUCKET=acme\nAWS_REGION=us-east-1\n")
    r = _make_app(dirs, template_path).test_client().get("/")
    assert r.status_code == 200
    assert b"Needs you" in r.data          # a Board band label, not a redirect

def test_no_auth_note_is_on_setup(client):
    # The "no authentication" chip explanation moved onto /setup (§5.10 sig-note).
    r = client.get("/setup")
    assert r.status_code == 200
    body = r.data.lower()
    assert b"no login" in body or b"no authentication" in body
    assert b"lan" in body

def test_csrf_roundtrip(app):
    from app.gui import security
    with app.test_request_context():
        token = security.issue_csrf()
        assert security.verify_csrf(token) is True
        assert security.verify_csrf("wrong") is False


# --- Setup readiness (spec 5.10) --------------------------------------------

def test_setup_works_unprovisioned_no_redirect(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert b"Can this machine back up" in r.data          # the h1

def test_setup_renders_the_five_checks(dirs, template_path):
    _write_jobs(dirs, [{"name": "appdata", "type": "versioned", "source": "appdata",
                        "schedule": "0 3 * * *", "enabled": True}])
    r = _make_app(dirs, template_path).test_client().get("/setup")
    body = r.get_data(as_text=True)
    for name in ("Destination reachable", "Recovery passphrase", "Old versions protected",
                 "At least one job scheduled", "Restore ever tested"):
        assert name in body

def test_setup_shipped_passphrase_is_a_blocker_first(dirs, template_path):
    _write_jobs(dirs, [{"name": "appdata", "type": "versioned", "source": "appdata",
                        "schedule": "0 3 * * *", "enabled": True}])
    config_io.write_secrets(dirs["config"],
                            {"RESTIC_PASSWORD": "CHANGEME-long-random-passphrase"})
    body = _make_app(dirs, template_path).test_client().get("/setup").get_data(as_text=True)
    assert "Not set — still the shipped example" in body
    assert 'data-blocker="1"' in body                      # rendered as a blocker
    # failing rows sort to the top: the passphrase (fail) row precedes the
    # restore-tested (warn) row.
    assert body.index('data-check="passphrase"') < body.index('data-check="restore_tested"')

def test_setup_crontab_stale_shows_informational_row(dirs, template_path, monkeypatch):
    import app.gui.routes as routes
    monkeypatch.setattr(routes.status, "crontab_stale", lambda *a, **k: True)
    body = _make_app(dirs, template_path).test_client().get("/setup").get_data(as_text=True)
    assert "The schedule file on disk does not match your jobs; restart the container." in body
    assert "Scheduler up to date" in body                 # the check name

def test_setup_no_crontab_row_when_fresh(dirs, template_path, monkeypatch):
    import app.gui.routes as routes
    monkeypatch.setattr(routes.status, "crontab_stale", lambda *a, **k: False)
    body = _make_app(dirs, template_path).test_client().get("/setup").get_data(as_text=True)
    assert "The schedule file on disk does not match your jobs" not in body

def test_setup_probe_launches_sysop_probe(client, monkeypatch):
    import app.gui.routes as routes
    calls = []
    monkeypatch.setattr(routes.ops, "launch_py",
                        lambda cfg, module, args, **kw: calls.append((module, list(args), kw)) or "rid")
    token = _csrf(client)
    r = client.post("/setup/probe", data={"csrf": token})
    assert r.status_code in (302, 303)
    assert calls and calls[0][0] == "app.engine.sysop" and calls[0][1] == ["probe"]

def test_setup_probe_requires_csrf(client):
    assert client.post("/setup/probe", data={}).status_code == 400

def test_setup_versioning_confirmed_persists(client, dirs):
    # pre-existing probe result: destination OK, versioning unknown
    state = Path(dirs["cache"], "state")
    state.mkdir(parents=True, exist_ok=True)
    (state / "_probe.json").write_text(json.dumps(
        {"destination": {"state": "ok", "probed_at": "2026-09-15T07:40:00Z"},
         "versioning": {"state": "unknown"}}))
    token = _csrf(client)
    r = client.post("/setup/versioning-confirmed", data={"csrf": token})
    assert r.status_code in (302, 303)
    probe = json.loads((state / "_probe.json").read_text())
    assert probe["versioning"]["state"] == "confirmed_by_hand"
    assert probe["destination"]["state"] == "ok"          # merge, not clobber

def test_setup_versioning_confirmed_requires_csrf(client):
    assert client.post("/setup/versioning-confirmed", data={}).status_code == 400
