import json, pathlib
import pytest
from app.gui import create_app

@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    (root / "media" / "movies").mkdir(parents=True)
    (root / "appdata").mkdir()
    return root

@pytest.fixture
def app(tmp_path, source_root, template_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    return create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache"),
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app):
    return app.test_client()

def _csrf(client, path):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]

def test_jobs_list_empty(client):
    r = client.get("/jobs"); assert r.status_code == 200 and b"No jobs" in r.data

def test_new_form_renders_source_tree(client):
    # job_form.html renders the picker as #source-tree (renamed from the old
    # media picker's #media-tree, per task-7's single-select job-source tree).
    r = client.get("/jobs/new"); assert r.status_code == 200 and b"source-tree" in r.data

def test_create_job_then_lists(client, app):
    t = _csrf(client, "/jobs/new")
    r = client.post("/jobs", data={"csrf": t, "name": "movies", "type": "archive",
                                    "source": "media/movies", "schedule": "0 4 * * 0",
                                    "storage_class": "DEEP_ARCHIVE", "enabled": "1"})
    assert r.status_code in (302, 303)
    jobs = json.loads(pathlib.Path(app.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert jobs[0]["name"] == "movies" and jobs[0]["type"] == "archive"
    assert b"movies" in client.get("/jobs").data

def test_create_rejects_bad_source(client):
    t = _csrf(client, "/jobs/new")
    assert client.post("/jobs", data={"csrf": t, "name": "x", "type": "archive",
                                       "source": "../../etc", "schedule": "0 4 * * 0",
                                       "storage_class": "STANDARD"}).status_code == 400

def test_create_requires_csrf(client):
    assert client.post("/jobs", data={"name": "x"}).status_code == 400

def test_browse_confined_404(client):
    assert client.get("/jobs/browse?path=../../etc").status_code == 404

def test_run_and_delete(client, app, monkeypatch):
    from app.gui import runner
    called = {}
    monkeypatch.setattr(runner, "trigger_job", lambda s, n, env=None: called.setdefault("n", n))
    t = _csrf(client, "/jobs/new")
    client.post("/jobs", data={"csrf": t, "name": "movies", "type": "archive",
                                "source": "media/movies", "schedule": "0 4 * * 0",
                                "storage_class": "STANDARD"})
    assert client.post("/jobs/movies/run", data={"csrf": t}).status_code in (302, 303) and called["n"] == "movies"
    assert client.post("/jobs/nope/run", data={"csrf": t}).status_code == 404
    client.post("/jobs/movies/delete", data={"csrf": t})
    jobs = json.loads(pathlib.Path(app.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert jobs == []

def test_nav_has_jobs(client):
    assert b"/jobs" in client.get("/jobs").data
