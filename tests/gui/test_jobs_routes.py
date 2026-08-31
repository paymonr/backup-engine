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
                       "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                       "PRICES_LIVE": False})

@pytest.fixture
def client(app):
    return app.test_client()

def _csrf(client, path):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]

@pytest.fixture
def tmp_jobs(app):
    # Seeds a single versioned-files job directly into jobs.json.
    p = pathlib.Path(app.config["CONFIG_DIR"], "jobs.json")
    p.write_text(json.dumps({"jobs": [{"name": "m", "type": "versioned-files", "source": "media",
                                        "schedule": "0 3 * * *", "enabled": True,
                                        "storage_class": "DEEP_ARCHIVE", "retention_days": 90}]}))
    return p

def test_jobs_list_empty(client):
    # Empty state now renders the onboarding card (see
    # test_jobs_empty_state_shows_onboarding_steps) rather than a bare "No jobs yet" message.
    r = client.get("/jobs"); assert r.status_code == 200 and b"Getting started" in r.data

def test_jobs_empty_state_shows_onboarding_steps(client):
    # with no jobs configured, the page guides first-run setup
    body = client.get("/jobs").get_data(as_text=True)
    assert "Getting started" in body
    assert "/config" in body or "Provision" in body  # points at setup

def test_versioned_files_job_labeled_correctly(client, tmp_jobs):
    # tmp_jobs writes a versioned-files job into jobs.json
    body = client.get("/jobs").get_data(as_text=True)
    assert "Versioned files" in body
    assert "Archive" not in body  # the old label bug mislabeled it Archive

def test_new_form_renders_source_tree(client):
    # job_form.html renders the picker as #source-tree (renamed from the old
    # media picker's #media-tree, per task-7's single-select job-source tree).
    r = client.get("/jobs/new"); assert r.status_code == 200 and b"source-tree" in r.data

def test_wizard_has_schedule_builder_and_schedule_field(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    assert 'class="sched-builder"' in body
    assert 'name="schedule"' in body   # the posted field survives

def test_create_job_then_lists(client, app):
    t = _csrf(client, "/jobs/new")
    r = client.post("/jobs", data={"csrf": t, "name": "movies", "type": "archive",
                                    "source": "media/movies", "schedule": "0 4 * * 0",
                                    "storage_class": "DEEP_ARCHIVE", "enabled": "1"})
    assert r.status_code in (302, 303)
    jobs = json.loads(pathlib.Path(app.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert jobs[0]["name"] == "movies" and jobs[0]["type"] == "archive"
    assert b"movies" in client.get("/jobs").data

def test_create_versioned_files_job_persists_type_and_retention(client, app):
    t = _csrf(client, "/jobs/new")
    r = client.post("/jobs", data={"csrf": t, "name": "docs", "type": "versioned-files",
                                    "source": "media/movies", "schedule": "0 5 * * *",
                                    "storage_class": "DEEP_ARCHIVE", "enabled": "1",
                                    "retention_days": "120"})
    assert r.status_code in (302, 303)
    jobs = json.loads(pathlib.Path(app.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert jobs[0]["name"] == "docs" and jobs[0]["type"] == "versioned-files"
    assert jobs[0]["retention_days"] == 120
    assert jobs[0]["storage_class"] == "DEEP_ARCHIVE"


def test_jobs_page_nameless_entry_no_500(client, app):
    # Regression (FIX 2): a hand-edited nameless jobs.json entry must not 500 /jobs
    # (jobs_page does j["name"]) — jobs_io.load drops it on the fail-safe read path.
    pathlib.Path(app.config["CONFIG_DIR"], "jobs.json").write_text(
        json.dumps({"jobs": [{"type": "archive", "source": "x", "schedule": "0 4 * * 0"}]}))
    assert client.get("/jobs").status_code == 200

def test_create_rejects_bad_source(client):
    t = _csrf(client, "/jobs/new")
    assert client.post("/jobs", data={"csrf": t, "name": "x", "type": "archive",
                                       "source": "../../etc", "schedule": "0 4 * * 0",
                                       "storage_class": "STANDARD"}).status_code == 400

def test_create_requires_csrf(client):
    assert client.post("/jobs", data={"name": "x"}).status_code == 400

def test_browse_confined_404(client):
    r = client.get("/jobs/browse?path=../../etc")
    assert r.status_code == 404
    assert b"etc" not in r.data  # the 404 body must not echo the attempted path

def test_run_and_delete_require_csrf(client):
    # CSRF is verified before any lookup/side effect, so a missing token is 400
    # regardless of whether the job exists.
    assert client.post("/jobs/movies/run", data={}).status_code == 400
    assert client.post("/jobs/movies/delete", data={}).status_code == 400

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

# --- final-fix R-final-1: a corrupt jobs.json must not 500 the read/schedule path,
# and the write path must not clobber the user's bytes (surface a flash, not a 500). ---
def _corrupt(app):
    p = pathlib.Path(app.config["CONFIG_DIR"], "jobs.json")
    p.write_text("{ this is not valid json")
    return p

def test_jobs_page_200_on_corrupt_file(client, app):
    _corrupt(app)
    assert client.get("/jobs").status_code == 200   # not 500

def test_job_save_on_corrupt_file_flashes_not_500(client, app):
    p = _corrupt(app)
    t = _csrf(client, "/jobs/new")
    r = client.post("/jobs", data={"csrf": t, "name": "movies", "type": "archive",
                                   "source": "media/movies", "schedule": "0 4 * * 0",
                                   "storage_class": "STANDARD", "enabled": "1"})
    assert r.status_code in (302, 303)              # flash + redirect, not 500/bare 400
    assert p.read_text() == "{ this is not valid json"   # user's bytes untouched

def test_job_delete_on_corrupt_file_flashes_not_500(client, app):
    p = _corrupt(app)
    t = _csrf(client, "/jobs/new")
    r = client.post("/jobs/movies/delete", data={"csrf": t})
    assert r.status_code in (302, 303)
    assert p.read_text() == "{ this is not valid json"

def test_wizard_class_panel_lists_every_class_with_min_and_retrieval(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    assert "class-panel" in body
    for cls in ("STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"):
        assert cls in body
    assert "180" in body            # Deep Archive minimum days
    assert "thaw required" in body  # cold read-access surfaced

def test_wizard_has_advice_container_and_original_class(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    assert 'id="job-advice"' in body
    assert 'data-original-class' in body
    assert 'id="job-cost-restore"' in body
