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

def test_estimate_page_renders_form_and_totals(client):
    r = client.get("/estimate")
    assert r.status_code == 200
    low = r.data.lower()
    assert b"cost" in low and b"monthly" in low
    assert b"deep_archive" in low  # media default storage class is prefilled in the form

def test_estimate_json_returns_estimate(client):
    j = client.get("/estimate.json").get_json()
    assert set(j["pipelines"]) == {"appdata", "media"}
    assert j["monthly_total"] > 0
    assert j["first_year_total"] > 0
    assert "appdata_retention_days" in j

def test_estimate_json_reacts_to_data_amount(client):
    small = client.get("/estimate.json?media_size_gb=100").get_json()["pipelines"]["media"]["storage"]
    big = client.get("/estimate.json?media_size_gb=100000").get_json()["pipelines"]["media"]["storage"]
    assert big > small

def test_estimate_json_reacts_to_data_type(client):
    # STANDARD (warm) costs more per GB than DEEP_ARCHIVE (cold) for the same size
    warm = client.get("/estimate.json?media_storage_class=STANDARD").get_json()["pipelines"]["media"]["storage"]
    cold = client.get("/estimate.json?media_storage_class=DEEP_ARCHIVE").get_json()["pipelines"]["media"]["storage"]
    assert warm > cold

def test_estimate_json_keep_policy_drives_appdata_versioning(client):
    low = client.get("/estimate.json?keep_last=1&keep_daily=1&keep_weekly=0&keep_monthly=0").get_json()
    high = client.get("/estimate.json?keep_monthly=12").get_json()
    assert high["pipelines"]["appdata"]["versioning"] > low["pipelines"]["appdata"]["versioning"]
    assert high["appdata_retention_days"] > low["appdata_retention_days"]

def test_estimate_json_bad_input_is_400(client):
    r = client.get("/estimate.json?appdata_size_gb=abc")
    assert r.status_code == 400
    assert "error" in r.get_json()

def test_estimate_page_bad_input_shows_error_not_crash(client):
    r = client.get("/estimate?media_size_gb=-5")
    assert r.status_code == 200
    assert b"must be" in r.data.lower() or b"error" in r.data.lower()

def test_nav_has_estimate_link(client):
    r = client.get("/config")
    assert b"/estimate" in r.data
