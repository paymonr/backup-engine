# tests/gui/test_jobs_estimate_routes.py — the job-wizard live-cost GET routes:
# /jobs/source-size (confined dir_size) and /jobs/estimate.json (this-job + new
# total). Both are GET + side-effect-free, so no CSRF is exercised here.
# PRICES_LIVE=False keeps them offline (bundled table, no network).
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


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    (root / "movies").mkdir(parents=True)
    (root / "movies" / "a.mkv").write_bytes(b"x" * 1000)
    (root / "appdata").mkdir()
    return root


@pytest.fixture
def app(dirs, template_path, source_root):
    _seed_jobs(dirs["config"], [VJOB, AJOB])
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "PRICES_LIVE": False,
                       "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


# --- /jobs/source-size ---

def test_source_size_confined_404(client):
    r = client.get("/jobs/source-size?path=../etc")
    assert r.status_code == 404
    assert b"etc" not in r.data  # no path echo, mirrors /jobs/browse


def test_source_size_ok(client):
    r = client.get("/jobs/source-size?path=movies")
    assert r.status_code == 200
    j = r.get_json()
    assert j["bytes"] == 1000 and j["count"] == 1


def test_source_size_missing_folder_ok_empty(client):
    # A nonexistent-but-non-escaping path still resolves fine under dir_size (it
    # just yields an empty tree) -- confinement is what /jobs/source-size guards.
    r = client.get("/jobs/source-size?path=nope")
    assert r.status_code == 200
    assert r.get_json() == {"bytes": 0, "count": 0}


# --- /jobs/estimate.json ---

def test_jobs_estimate_returns_this_and_total(client):
    r = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 4 * * 0", "size_gb": "500"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["this_job_monthly"] >= 0
    assert j["new_total_monthly"] >= j["this_job_monthly"]
    assert "price_source" in j and "price_date" in j


def test_jobs_estimate_new_job_adds_to_existing_total(client):
    base = client.get("/estimate.json").get_json()["monthly_total"]
    r = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 4 * * 0", "size_gb": "500"})
    j = r.get_json()
    assert j["new_total_monthly"] == pytest.approx(base + j["this_job_monthly"])


def test_jobs_estimate_editing_existing_job_replaces_not_doubles(client):
    # "movies" already exists in jobs.json. Estimating a change to IT (same name)
    # must REPLACE it in the total, not add a second copy.
    base = client.get("/estimate.json").get_json()
    movies_li = base["jobs"]["movies"]
    movies_original_monthly = (movies_li["storage"] + movies_li["versioning"]
                               + movies_li["ingest_monthly"] + movies_li["rotation_monthly"])
    others_monthly = base["monthly_total"] - movies_original_monthly

    edited = client.get("/jobs/estimate.json", query_string={
        "name": "movies", "type": "archive", "source": "movies",
        "storage_class": "STANDARD", "schedule": "0 4 * * 0", "size_gb": "500"}).get_json()

    assert edited["new_total_monthly"] == pytest.approx(others_monthly + edited["this_job_monthly"])
    # Sanity: a double-counting bug would still include the OLD movies cost too.
    assert edited["new_total_monthly"] != pytest.approx(base["monthly_total"] + edited["this_job_monthly"])


def test_jobs_estimate_bad_size_gb_is_400(client):
    r = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 4 * * 0", "size_gb": "abc"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_jobs_estimate_bad_storage_class_is_400(client):
    r = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "NEBULA", "schedule": "0 4 * * 0"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_jobs_estimate_no_source_size_gb_uses_default(client):
    # No source, no size_gb -> falls back to the module default rather than 500ing.
    r = client.get("/jobs/estimate.json", query_string={
        "name": "brand-new", "type": "versioned", "schedule": "0 5 * * *"})
    assert r.status_code == 200
    assert r.get_json()["this_job_monthly"] >= 0


def test_jobs_estimate_non_us_east_1_region_no_500(dirs, template_path, source_root):
    # Regression (FIX 1): /jobs/estimate.json called load_prices unguarded, so a
    # non-us-east-1 AWS_REGION 500'd the wizard. It must return 200 now (us-east-1
    # fallback for the un-bundled region; PRICES_LIVE=False keeps it offline).
    pathlib.Path(dirs["config"], "backup.env").write_text("AWS_REGION=eu-west-1\n")
    _seed_jobs(dirs["config"], [VJOB, AJOB])
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": str(source_root), "PRICES_LIVE": False,
                      "SECRET_KEY": "test", "TESTING": True})
    r = app.test_client().get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 4 * * 0", "size_gb": "500"})
    assert r.status_code == 200
    assert r.get_json()["this_job_monthly"] >= 0


def test_jobs_estimate_never_walks_source_uses_default(client):
    # FIX: the wizard estimate must NEVER touch the filesystem (it has to be instant
    # on every keystroke). With a source= but NO size_gb it uses _DEFAULT_SIZE_GB,
    # NOT the picked folder's real on-disk bytes -- the real size is fetched
    # separately, async, by /jobs/source-size and threaded back via size_gb.
    from app.gui.estimate_io import _DEFAULT_SIZE_GB
    walked = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "STANDARD", "schedule": "0 4 * * 0"}).get_json()
    default_sized = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "STANDARD", "schedule": "0 4 * * 0",
        "size_gb": str(_DEFAULT_SIZE_GB)}).get_json()
    # Uses the DEFAULT size, matching an explicit size_gb=_DEFAULT_SIZE_GB request...
    assert walked["this_job_monthly"] == pytest.approx(default_sized["this_job_monthly"])
    # ...and NOT the near-zero cost the folder's ~1000-byte real size would produce
    # (which is what a filesystem walk would have used).
    tiny = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "STANDARD", "schedule": "0 4 * * 0",
        "size_gb": str(1000 / 1024 ** 3)}).get_json()
    assert walked["this_job_monthly"] != pytest.approx(tiny["this_job_monthly"])
