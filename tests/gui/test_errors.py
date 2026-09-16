# tests/gui/test_errors.py -- the themed error model (spec 5.14 + 9).
#
# Every former abort() site in routes.py now renders the Night Shift error page
# (error.html) with the right status and copy, or -- for JSON endpoints -- a
# {"error": <sentence>} body with that status. A 404/500 for any route renders
# the themed page, never Flask's default. This is the controller-facing Ruling
# R-E test and is DISTINCT from tests/engine/test_errors.py (error classes).
import pytest
from flask import abort
from pathlib import Path
from app.gui import create_app, config_io, security, jobs_io


def _cfg(dirs, template_path, **over):
    c = {"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
         "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
         "SOURCE_ROOT": dirs["cache"], "SECRET_KEY": "test", "TESTING": True,
         "PRICES_LIVE": False}
    c.update(over)
    return c


@pytest.fixture
def app(dirs, template_path):
    return create_app(_cfg(dirs, template_path))


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client):
    # Issue a CSRF token into the session the same way the real forms do.
    client.get("/config")  # issues a token into the session
    with client.session_transaction() as s:
        return s["_csrf"]


# --- generic themed pages (not Flask's default) ---------------------------

def test_unknown_route_renders_themed_404(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404
    body = r.get_data(as_text=True)
    assert "There is no such page" in body
    assert "Error 404" in body                 # eyebrow
    assert "Back to the Board" in body         # the one primary action
    # themed = uses our shell, not Werkzeug's default page
    assert "Werkzeug" not in body and "Not Found</title>" not in body


def test_unknown_json_route_returns_json_404(client):
    r = client.get("/nope.json")
    assert r.status_code == 404
    assert r.is_json
    assert r.get_json()["error"]               # a sentence, not HTML


def test_method_not_allowed_is_themed(client):
    r = client.post("/about")                  # /about is GET-only
    assert r.status_code == 405
    assert "That page does not take this request" in r.get_data(as_text=True)


def test_500_renders_themed_page(dirs, template_path, monkeypatch):
    # Under TESTING Flask propagates uncaught exceptions; turn that off so the
    # 500 handler runs, then make a view blow up.
    app = create_app(_cfg(dirs, template_path, PROPAGATE_EXCEPTIONS=False))
    import app.gui.routes as routes

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(routes.config_io, "read_backup_env", boom)
    r = app.test_client().get("/config")
    assert r.status_code == 500
    body = r.get_data(as_text=True)
    assert "Something broke inside backup-engine" in body
    assert "Nothing you typed was saved." in body


# --- CSRF abort(400) sites (routes.py 44,74,94,124,247,283,294,357,379) ----

@pytest.mark.parametrize("path", [
    "/config", "/provision/manual/render", "/provision/validate",
    "/provision/automated", "/jobs", "/costs/refresh", "/costs/billing",
])
def test_csrf_failure_renders_expired_form_page(client, path):
    r = client.post(path, data={})             # no csrf token
    assert r.status_code == 400
    assert "That form had expired" in r.get_data(as_text=True)


def test_job_run_csrf_failure_is_themed_400(client):
    r = client.post("/jobs/whatever/run", data={})
    assert r.status_code == 400
    assert "That form had expired" in r.get_data(as_text=True)


def test_job_delete_csrf_failure_is_themed_400(client):
    r = client.post("/jobs/whatever/delete", data={})
    assert r.status_code == 400
    assert "That form had expired" in r.get_data(as_text=True)


# --- unknown-job 404s (routes.py 192 edit, 286 run) -----------------------

def test_edit_unknown_job_404_names_the_job(client):
    r = client.get("/jobs/ghostjob/edit")
    assert r.status_code == 404
    assert "There is no job called ghostjob" in r.get_data(as_text=True)


def test_run_unknown_job_404_names_the_job(client):
    token = _csrf(client)
    r = client.post("/jobs/ghostjob/run", data={"csrf": token})
    assert r.status_code == 404
    assert "There is no job called ghostjob" in r.get_data(as_text=True)


# --- browse / size escape -> JSON 404, no path echo (routes.py 206, 218) --

def test_browse_escape_returns_json_404_without_path_echo(client):
    r = client.get("/jobs/browse?path=../../etc")
    assert r.status_code == 404
    assert r.is_json
    assert r.get_json() == {"error": "That folder is outside the source root."}
    assert "etc" not in r.get_data(as_text=True)     # never echoes the path


def test_source_size_escape_returns_json_404_without_path_echo(client):
    r = client.get("/jobs/source-size?path=../../etc")
    assert r.status_code == 404
    assert r.is_json
    assert r.get_json() == {"error": "That folder is outside the source root."}
    assert "etc" not in r.get_data(as_text=True)


# --- the handlers registered for 403 and 409 (5.14) -----------------------
# No route raises these yet (busy 409 is a later task), so drive the handlers
# through throwaway routes to prove they render the themed copy.

def test_403_handler_is_themed(dirs, template_path):
    app = create_app(_cfg(dirs, template_path))
    app.add_url_rule("/_forbidden", "_forbidden", lambda: abort(403))
    r = app.test_client().get("/_forbidden")
    assert r.status_code == 403
    assert "That is not allowed from here" in r.get_data(as_text=True)


def test_409_handler_carries_the_busy_sentence(dirs, template_path):
    app = create_app(_cfg(dirs, template_path))
    sentence = ("appdata is busy — a backup or restore is already running. "
                "Wait for it to finish.")
    app.add_url_rule("/_busy", "_busy",
                     lambda: abort(409, description=sentence))
    r = app.test_client().get("/_busy")
    assert r.status_code == 409
    assert "appdata is busy" in r.get_data(as_text=True)


def test_409_json_caller_gets_json(dirs, template_path):
    app = create_app(_cfg(dirs, template_path))
    sentence = "appdata is busy — wait for it to finish."
    app.add_url_rule("/_busy.json", "_busyjson",
                     lambda: abort(409, description=sentence))
    r = app.test_client().get("/_busy.json")
    assert r.status_code == 409
    assert r.is_json and r.get_json() == {"error": sentence}
