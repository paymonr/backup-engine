# tests/gui/test_estimate_routes.py — the Cost workbench routes (spec 5.6, 8.7).
# /estimate 301s to /cost; /cost renders the five bands; /cost.json and
# /estimate.json return the same cost_page JSON. PRICES_LIVE=False keeps it offline.
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


# --- /estimate 301 -> /cost (ruling R-H, spec 5.6) --------------------------

def test_estimate_redirects_permanently_to_cost(client):
    r = client.get("/estimate")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/cost")


# --- /cost page: the five bands ---------------------------------------------

def test_cost_page_renders_and_job_names(client):
    r = client.get("/cost")
    assert r.status_code == 200
    low = r.data.lower()
    assert b"cost" in low
    assert b"appdata" in r.data and b"movies" in r.data      # seeded job names rendered


def test_cost_page_has_timeline_scrubber_and_calc(client):
    body = client.get("/cost").get_data(as_text=True)
    assert 'id="cost-timeline"' in body        # svg chart mount
    assert 'id="month"' in body                # the month scrubber (replaces milestones table)
    assert 'id="proj-data"' in body            # embedded projection JSON for the JS
    assert "Refresh usage" in body             # band-1 sysop launch button
    assert "How this is calculated" in body    # band-5 details


def test_cost_page_restore_note_and_no_cost_explorer_form(client):
    body = client.get("/cost").get_data(as_text=True)
    assert "Getting it back is warm-up plus download out of Amazon." in body
    # the Cost Explorer credential is edited only at Keys & secrets now (5.6 band 5)
    assert "COST_EXPLORER_" not in body


def test_cost_page_lever_form_is_get_with_noscript_recalculate(client):
    body = client.get("/cost").get_data(as_text=True)
    assert 'id="lever-form"' in body and 'method="get"' in body
    assert "<noscript>" in body and "Recalculate" in body
    # the scenario Apply is a POST sibling by formaction (5.6)
    assert 'formaction="/costs/scenario"' in body


def test_cost_page_renders_recomputed_server_side_with_js_off(client):
    # GET /cost?<params> re-renders server-side (the <noscript> path): a name-prefixed
    # change rate moves the figures, no JS needed.
    body = client.get("/cost?appdata_change_rate_pct=30").get_data(as_text=True)
    assert body.count('id="cost-timeline"') == 1     # a fully re-rendered page, 200
    assert "The model says" in body


def test_cost_page_bad_input_shows_error_not_crash(client):
    r = client.get("/cost?movies_size_gb=-5")
    assert r.status_code == 200
    assert b"must be" in r.data.lower() or b"error" in r.data.lower()


def test_cost_page_non_us_east_1_region_no_500(dirs, template_path, tmp_path):
    pathlib.Path(dirs["config"], "backup.env").write_text("AWS_REGION=eu-west-1\n")
    app = _make_app(dirs, template_path, tmp_path, [VJOB, AJOB])
    assert app.test_client().get("/cost").status_code == 200


def test_cost_page_nameless_jobs_entry_no_500(dirs, template_path, tmp_path):
    app = _make_app(dirs, template_path, tmp_path,
                    [{"type": "archive", "source": "x", "schedule": "0 4 * * 0"}, AJOB])
    assert app.test_client().get("/cost").status_code == 200


# --- /cost.json and /estimate.json: the same cost_page JSON -----------------

def test_cost_json_keyed_by_job_names(client):
    j = client.get("/cost.json").get_json()
    assert set(j["jobs"]) == {"appdata", "movies"}
    assert j["monthly_total"] > 0
    assert j["first_year_total"] > 0
    assert "pipelines" not in j


def test_estimate_json_is_the_same_json_as_cost_json(client):
    a = client.get("/cost.json").get_json()
    b = client.get("/estimate.json").get_json()
    assert a == b


def test_cost_json_reacts_to_data_amount(client):
    small = client.get("/cost.json?movies_size_gb=100").get_json()["jobs"]["movies"]["storage"]
    big = client.get("/cost.json?movies_size_gb=100000").get_json()["jobs"]["movies"]["storage"]
    assert big > small


def test_cost_json_reacts_to_data_type(client):
    warm = client.get("/cost.json?movies_storage_class=STANDARD").get_json()["jobs"]["movies"]["storage"]
    cold = client.get("/cost.json?movies_storage_class=DEEP_ARCHIVE").get_json()["jobs"]["movies"]["storage"]
    assert warm > cold


def test_cost_json_change_rate_drives_versioning(client):
    low = client.get("/cost.json?appdata_change_rate_pct=1").get_json()["jobs"]["appdata"]["versioning"]
    high = client.get("/cost.json?appdata_change_rate_pct=50").get_json()["jobs"]["appdata"]["versioning"]
    assert high > low


def test_cost_json_archive_has_its_own_default_retention(client):
    low = client.get("/cost.json?movies_change_rate_pct=10&versioning_retention_days=10").get_json()["jobs"]["movies"]["versioning"]
    high = client.get("/cost.json?movies_change_rate_pct=10&versioning_retention_days=100").get_json()["jobs"]["movies"]["versioning"]
    assert high == low
    assert high > 0


def test_cost_json_bad_input_is_400(client):
    r = client.get("/cost.json?appdata_size_gb=abc")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_cost_json_empty_jobs_zero_totals(dirs, template_path, tmp_path):
    app = _make_app(dirs, template_path, tmp_path, [])
    j = app.test_client().get("/cost.json").get_json()
    assert j["jobs"] == {} and j["monthly_total"] == 0


def test_cost_json_includes_projection(client):
    p = client.get("/cost.json").get_json()["projection"]
    assert len(p["primary"]["months"]) == 24
    assert set(p["comparison"]) == {"no_versioning", "rolling_30"}
    assert "onetime" in p and "first_month" in p["onetime"]


def test_cost_json_has_per_job_restore_and_assumptions(client):
    j = client.get("/cost.json").get_json()
    assert {p["name"] for p in j["per_job"]} == {"appdata", "movies"}
    assert {r["name"] for r in j["restore"]} == {"appdata", "movies"}
    assert set(j["assumptions"]) == {"jobs", "scenario"}


def test_nav_has_cost_link(client):
    # R-H carry-forward: the nav's Cost link points at /cost (was /estimate).
    r = client.get("/setup/keys")
    assert b"/cost" in r.data
