# tests/gui/test_estimate_routes.py — /estimate + /estimate.json over N seeded jobs.
# PRICES_LIVE=False keeps the routes offline (bundled table, no network).
import json
import pathlib
import pytest
from app.gui import create_app

VJOB = {"name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
        "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}
AJOB = {"name": "movies", "type": "archive", "source": "movies",
        "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
        "mirror": False}

def _seed_jobs(config_dir, jobs):
    pathlib.Path(config_dir, "jobs.json").write_text(json.dumps({"jobs": jobs}))

def _make_app(dirs, template_path, tmp_path, jobs):
    _seed_jobs(dirs["config"], jobs)
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(tmp_path / "src"), "PRICES_LIVE": False,
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def app(dirs, template_path, tmp_path):
    return _make_app(dirs, template_path, tmp_path, [VJOB, AJOB])

@pytest.fixture
def client(app):
    return app.test_client()

def test_estimate_page_renders_form_and_job_names(client):
    r = client.get("/estimate")
    assert r.status_code == 200
    low = r.data.lower()
    assert b"cost" in low and b"monthly" in low
    assert b"appdata" in r.data and b"movies" in r.data  # seeded job names rendered

def test_estimate_page_has_class_reference(client):
    body = client.get("/estimate").get_data(as_text=True)
    assert "class-panel" in body
    assert "Restoring is retrieval + egress" in body  # the retrieval/egress note

def test_estimate_json_keyed_by_job_names(client):
    j = client.get("/estimate.json").get_json()
    assert set(j["jobs"]) == {"appdata", "movies"}
    assert j["monthly_total"] > 0
    assert j["first_year_total"] > 0
    assert "pipelines" not in j and "appdata_retention_days" not in j

def test_estimate_json_reacts_to_data_amount(client):
    small = client.get("/estimate.json?movies_size_gb=100").get_json()["jobs"]["movies"]["storage"]
    big = client.get("/estimate.json?movies_size_gb=100000").get_json()["jobs"]["movies"]["storage"]
    assert big > small

def test_estimate_json_reacts_to_data_type(client):
    warm = client.get("/estimate.json?movies_storage_class=STANDARD").get_json()["jobs"]["movies"]["storage"]
    cold = client.get("/estimate.json?movies_storage_class=DEEP_ARCHIVE").get_json()["jobs"]["movies"]["storage"]
    assert warm > cold  # STANDARD costlier per GB than DEEP_ARCHIVE for the same job

def test_estimate_json_change_rate_drives_versioning(client):
    low = client.get("/estimate.json?appdata_change_rate_pct=1").get_json()["jobs"]["appdata"]["versioning"]
    high = client.get("/estimate.json?appdata_change_rate_pct=50").get_json()["jobs"]["appdata"]["versioning"]
    assert high > low

def test_estimate_json_global_retention_drives_archive_versioning(client):
    # An archive job has no per-job retention, so it uses the scenario-level window.
    low = client.get("/estimate.json?versioning_retention_days=10").get_json()["jobs"]["movies"]["versioning"]
    high = client.get("/estimate.json?versioning_retention_days=100").get_json()["jobs"]["movies"]["versioning"]
    assert high > low

def test_estimate_json_bad_input_is_400(client):
    r = client.get("/estimate.json?appdata_size_gb=abc")
    assert r.status_code == 400
    assert "error" in r.get_json()

def test_estimate_page_bad_input_shows_error_not_crash(client):
    r = client.get("/estimate?movies_size_gb=-5")
    assert r.status_code == 200
    assert b"must be" in r.data.lower() or b"error" in r.data.lower()

def test_estimate_json_empty_jobs_zero_totals(dirs, template_path, tmp_path):
    app = _make_app(dirs, template_path, tmp_path, [])
    j = app.test_client().get("/estimate.json").get_json()
    assert j["jobs"] == {} and j["monthly_total"] == 0

def test_nav_has_estimate_link(client):
    r = client.get("/config")
    assert b"/estimate" in r.data

def test_estimate_page_non_us_east_1_region_no_500(dirs, template_path, tmp_path):
    # Regression (FIX 1): only us-east-1.json is bundled. A normal, GUI-editable
    # non-us-east-1 AWS_REGION must not 500 /estimate — load_prices falls back to
    # the us-east-1 table for the un-bundled region (offline, PRICES_LIVE=False).
    pathlib.Path(dirs["config"], "backup.env").write_text("AWS_REGION=eu-west-1\n")
    app = _make_app(dirs, template_path, tmp_path, [VJOB, AJOB])
    assert app.test_client().get("/estimate").status_code == 200

def test_estimate_page_nameless_jobs_entry_no_500(dirs, template_path, tmp_path):
    # Regression (FIX 2): a hand-edited nameless jobs.json entry must not 500 the
    # page — jobs_io.load drops it (fail-safe read path).
    app = _make_app(dirs, template_path, tmp_path,
                    [{"type": "archive", "source": "x", "schedule": "0 4 * * 0"}, AJOB])
    assert app.test_client().get("/estimate").status_code == 200
