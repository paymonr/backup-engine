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
    assert j["bytes"] >= 1000 and j["count"] == 1  # du -sb: file bytes (+ tiny dir overhead on some FS)


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


def test_jobs_estimate_versioned_files_returns_a_number(client):
    r = client.get("/jobs/estimate.json", query_string={
        "name": "docs", "type": "versioned-files", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 5 * * *",
        "retention_days": "90", "size_gb": "50"})
    assert r.status_code == 200
    j = r.get_json()
    assert isinstance(j["this_job_monthly"], (int, float))
    assert j["new_total_monthly"] >= j["this_job_monthly"]


def test_jobs_estimate_projection_includes_six_month_total(client):
    r = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 4 * * 0", "size_gb": "500"})
    assert r.status_code == 200
    p = r.get_json()["projection"]
    # The cumulative next-6-months figure the card needs is present, positive, and
    # at least the first month (it is a sum of >=1 non-negative months).
    assert isinstance(p["total_6mo"], (int, float))
    assert p["total_6mo"] >= p["first_bill"] > 0
    assert p["total_6mo"] >= p["at_6"]


def test_jobs_estimate_guidance_recommends_class_per_type(client):
    # Versioned steers to STANDARD; selecting it flags on_recommended True.
    v = client.get("/jobs/estimate.json", query_string={
        "name": "cfg", "type": "versioned", "source": "movies",
        "storage_class": "STANDARD", "schedule": "0 5 * * *", "size_gb": "5"}).get_json()
    assert v["guidance"]["recommend_class"] == "STANDARD"
    assert v["guidance"]["on_recommended"] is True
    assert v["guidance"]["type_label"] and v["guidance"]["type_when"]
    # A cold class for a versioned job is NOT the recommendation.
    v2 = client.get("/jobs/estimate.json", query_string={
        "name": "cfg", "type": "versioned", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 5 * * *", "size_gb": "5"}).get_json()
    assert v2["guidance"]["on_recommended"] is False
    # Archive steers to DEEP_ARCHIVE.
    a = client.get("/jobs/estimate.json", query_string={
        "name": "media", "type": "archive", "source": "movies",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 4 * * 0", "size_gb": "500"}).get_json()
    assert a["guidance"]["recommend_class"] == "DEEP_ARCHIVE"
    assert a["guidance"]["on_recommended"] is True


def test_jobs_estimate_warns_and_bundling_fixes_many_small_on_cold(client):
    # 5M objects / 1000GB ~ 0.2MB avg on DEEP_ARCHIVE -> the bundling warning fires.
    loose = client.get("/jobs/estimate.json", query_string={
        "name": "manga", "type": "archive", "source": "movies", "storage_class": "DEEP_ARCHIVE",
        "schedule": "0 4 * * 0", "size_gb": "1000", "file_count": "5000000"}).get_json()
    assert "bundle" in " ".join(a["text"] for a in loose["advice"]).lower()
    # Turning on packing collapses the effective object count: warning clears AND the
    # one-time upload drops sharply.
    packed = client.get("/jobs/estimate.json", query_string={
        "name": "manga", "type": "archive", "source": "movies", "storage_class": "DEEP_ARCHIVE",
        "schedule": "0 4 * * 0", "size_gb": "1000", "file_count": "5000000",
        "packing": "1", "pack_member_gb": "1"}).get_json()
    assert "bundle" not in " ".join(a["text"] for a in packed["advice"]).lower()
    assert packed["breakdown"]["upload_onetime"] < loose["breakdown"]["upload_onetime"] / 100


def test_jobs_estimate_bundle_warning_at_manga_shape(client):
    # Appendix B #43 raised the bundling threshold 1 MB -> 10 MB so the owner's real
    # manga example (232,021 files in 1824 GB = 8.05 MB avg) on DEEP_ARCHIVE now DOES
    # fire the per-object bundling advice (it is the case the warning exists for).
    j = client.get("/jobs/estimate.json", query_string={
        "name": "manga", "type": "archive", "source": "movies", "storage_class": "DEEP_ARCHIVE",
        "schedule": "0 4 * * 0", "size_gb": "1824", "file_count": "232021"}).get_json()
    assert "bundle" in " ".join(a["text"] for a in j["advice"]).lower()


def test_jobs_estimate_no_bundle_warning_above_ten_mb_average(client):
    # Above 10 MB average (1824 GB / 100,000 files = 18.7 MB) the warning stays silent.
    j = client.get("/jobs/estimate.json", query_string={
        "name": "big", "type": "archive", "source": "movies", "storage_class": "DEEP_ARCHIVE",
        "schedule": "0 4 * * 0", "size_gb": "1824", "file_count": "100000"}).get_json()
    assert "bundle" not in " ".join(a["text"] for a in j["advice"]).lower()


def test_jobs_estimate_deep_archive_put_is_10x_standard(client):
    def onetime(cls):
        return client.get("/jobs/estimate.json", query_string={
            "name": "m", "type": "archive", "source": "movies", "storage_class": cls,
            "schedule": "0 4 * * 0", "size_gb": "1000", "file_count": "1000000",
        }).get_json()["breakdown"]["upload_onetime"]
    assert onetime("DEEP_ARCHIVE") == pytest.approx(10 * onetime("STANDARD"), rel=0.01)


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


def test_jobs_estimate_bad_retention_type_is_400(client):
    r = client.get("/jobs/estimate.json", query_string={
        "name": "photos", "type": "archive", "source": "movies",
        "storage_class": "STANDARD", "schedule": "0 4 * * 0", "retention_type": "bogus"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_jobs_estimate_count_retention_differs_from_days(client):
    # Task 9 fix: the wizard's live-cost path (wizard_estimate ->
    # estimate_io.retention_from_form) must reflect the CHOSEN retention policy,
    # not just ignore it -- a versioned/archive/versioned-files job can now pick
    # count/keep_all, not only the one legacy per-type field. A non-zero change
    # rate is required to exercise the old-version ("versioning") cost term at all.
    q = {"name": "archv-count", "type": "archive", "source": "movies",
         "storage_class": "STANDARD", "schedule": "0 4 * * 0", "size_gb": "500",
         "change_rate_pct": "10"}
    days = client.get("/jobs/estimate.json", query_string={
        **q, "retention_type": "days", "retention_days": "90"}).get_json()
    count = client.get("/jobs/estimate.json", query_string={
        **q, "retention_type": "count", "retention_count": "3"}).get_json()
    assert count["breakdown"]["versioning"] != pytest.approx(days["breakdown"]["versioning"])
    assert count["this_job_monthly"] != pytest.approx(days["this_job_monthly"])


def test_jobs_estimate_keep_all_retention_differs_from_days(client):
    q = {"name": "archv-keepall", "type": "archive", "source": "movies",
         "storage_class": "STANDARD", "schedule": "0 4 * * 0", "size_gb": "500",
         "change_rate_pct": "10"}
    days = client.get("/jobs/estimate.json", query_string={
        **q, "retention_type": "days", "retention_days": "90"}).get_json()
    keep_all = client.get("/jobs/estimate.json", query_string={
        **q, "retention_type": "keep_all"}).get_json()
    assert keep_all["breakdown"]["versioning"] != pytest.approx(days["breakdown"]["versioning"])
    assert keep_all["this_job_monthly"] != pytest.approx(days["this_job_monthly"])


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


def test_estimate_json_includes_advice_and_restore(client):
    r = client.get("/jobs/estimate.json", query_string={
        "type": "versioned", "storage_class": "DEEP_ARCHIVE",
        "size_gb": "50", "schedule": "0 3 * * *", "name": "x", "source": "media"})
    assert r.status_code == 200
    j = r.get_json()
    assert "this_job_restore" in j and j["this_job_restore"] > 0
    assert any(a["level"] == "danger" for a in j["advice"])  # restic + cold


def test_estimate_json_advice_empty_for_plain_standard(client):
    r = client.get("/jobs/estimate.json", query_string={
        "type": "archive", "storage_class": "STANDARD",
        "size_gb": "10", "schedule": "0 3 * * *", "name": "y", "source": "media"})
    j = r.get_json()
    assert j["advice"] == []


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


def test_jobs_estimate_includes_projection_and_breakdown(client):
    j = client.get("/jobs/estimate.json", query_string={
        "name": "bak-manga", "type": "versioned-files", "source": "comics/mangas",
        "storage_class": "DEEP_ARCHIVE", "schedule": "0 3 1 * *",
        "size_gb": "1000", "retention_days": "180", "change_rate_pct": "1"}).get_json()
    assert {"first_bill", "steady_monthly", "steady_month", "at_12"} <= set(j["projection"])
    assert {"storage", "versioning", "lockin_onetime", "change_rate_pct"} <= set(j["breakdown"])
    assert j["breakdown"]["change_rate_pct"] == 1.0


def test_jobs_estimate_churn_param_changes_monthly(client):
    q = {"name": "bak-manga", "type": "versioned-files", "source": "comics/mangas",
         "storage_class": "DEEP_ARCHIVE", "schedule": "0 3 1 * *", "size_gb": "1000",
         "retention_days": "180"}
    lo = client.get("/jobs/estimate.json", query_string={**q, "change_rate_pct": "1"}).get_json()
    hi = client.get("/jobs/estimate.json", query_string={**q, "change_rate_pct": "30"}).get_json()
    assert hi["this_job_monthly"] > lo["this_job_monthly"]


def test_wizard_page_has_change_rate_radios_and_when_matrix(client):
    body = client.get("/jobs/new").get_data(as_text=True)
    assert 'name="change_rate_pct"' in body          # change-rate radios (5.8 §3.4)
    assert "How much of it changes each backup?" in body
    assert 'id="fig-job"' in body                    # WHEN x WHOSE this-job cell (5.8 §3.6)
    assert 'id="new-working"' in body                # Show working disclosure


def test_wizard_change_rate_defaults_to_a_little(client):
    import re
    body = client.get("/jobs/new").get_data(as_text=True)
    # The fresh form starts at ~1% (Appendix B #17): the ch-1 radio is checked.
    assert re.search(r'id="ch-1"[^>]*\bvalue="1"[^>]*\bchecked', body)
