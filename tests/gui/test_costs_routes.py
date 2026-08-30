# tests/gui/test_costs_routes.py — current-spend cost page: /costs/refresh (real
# bucket usage via usage.collect_usage, stubbed here — no real rclone) and
# /costs/billing (write-only, opt-in Cost Explorer credential, separate from the
# runtime key). Also covers estimate_io.current_costs / billing_view directly.
import json
import pathlib
import pytest
from app.gui import create_app, config_io, estimate_io
from app.estimator import usage, billing
from app.estimator.prices import load_prices

AJOB = {"name": "movies", "type": "archive", "source": "movies",
        "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
        "mirror": False}
VJOB = {"name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
        "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}


@pytest.fixture
def app(dirs, template_path, tmp_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(tmp_path / "src"), "PRICES_LIVE": False,
                       "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client):
    client.get("/estimate")  # issues the CSRF token into the session
    with client.session_transaction() as s:
        return s["_csrf"]


# --- routes: CSRF-first ------------------------------------------------------

def test_billing_connect_requires_csrf(client):
    r = client.post("/costs/billing", data={"COST_EXPLORER_ACCESS_KEY_ID": "A"})
    assert r.status_code == 400


def test_refresh_requires_csrf(client):
    assert client.post("/costs/refresh", data={}).status_code == 400


# --- /costs/billing: connect / disconnect ------------------------------------

def test_billing_connect_writes_ce_keys_and_preserves_core_secrets(client, dirs):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA",
                                             "AWS_SECRET_ACCESS_KEY": "shh",
                                             "RESTIC_PASSWORD": "pw"})
    t = _csrf(client)
    r = client.post("/costs/billing", data={"csrf": t,
        "COST_EXPLORER_ACCESS_KEY_ID": "CEKEY", "COST_EXPLORER_SECRET_ACCESS_KEY": "CESECRET"})
    assert r.status_code in (302, 303)
    raw = config_io._read_secrets_raw(pathlib.Path(dirs["config"], "secrets.env"))
    assert raw["AWS_ACCESS_KEY_ID"] == "AKIA"              # core untouched
    assert raw["RESTIC_PASSWORD"] == "pw"
    assert raw["COST_EXPLORER_ACCESS_KEY_ID"] == "CEKEY"    # CE written
    assert raw["COST_EXPLORER_SECRET_ACCESS_KEY"] == "CESECRET"
    assert config_io.secrets_mode(dirs["config"]) == "600"
    # never echoed back to the page
    page = client.get("/estimate").data
    assert b"CEKEY" not in page and b"CESECRET" not in page


def test_billing_disconnect_removes_ce_keys_keeps_core_secrets(client, dirs):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA",
                                             "AWS_SECRET_ACCESS_KEY": "shh",
                                             "RESTIC_PASSWORD": "pw"})
    t = _csrf(client)
    client.post("/costs/billing", data={"csrf": t,
        "COST_EXPLORER_ACCESS_KEY_ID": "CEKEY", "COST_EXPLORER_SECRET_ACCESS_KEY": "CESECRET"})
    r = client.post("/costs/billing", data={"csrf": t, "disconnect": "1"})
    assert r.status_code in (302, 303)
    raw = config_io._read_secrets_raw(pathlib.Path(dirs["config"], "secrets.env"))
    assert "COST_EXPLORER_ACCESS_KEY_ID" not in raw
    assert "COST_EXPLORER_SECRET_ACCESS_KEY" not in raw
    assert raw["AWS_ACCESS_KEY_ID"] == "AKIA"               # core survives disconnect
    assert config_io.read_cost_explorer_creds(dirs["config"]) is None


def test_billing_connect_writes_optional_tag_to_backup_env(client, dirs, template_path):
    t = _csrf(client)
    client.post("/costs/billing", data={"csrf": t,
        "COST_EXPLORER_ACCESS_KEY_ID": "CEKEY", "COST_EXPLORER_SECRET_ACCESS_KEY": "CESECRET",
        "COST_EXPLORER_TAG": "project=backup"})
    assert config_io.read_backup_env(dirs["config"]).get("COST_EXPLORER_TAG") == "project=backup"


# --- /costs/refresh: repopulates the usage cache (stub collect_usage) --------

def test_refresh_calls_collect_usage_and_saves_cache(client, dirs, monkeypatch):
    from app.gui import routes
    pathlib.Path(dirs["config"], "backup.env").write_text("S3_BUCKET=mybucket\nAWS_REGION=us-east-1\n")
    pathlib.Path(dirs["config"], "jobs.json").write_text(json.dumps({"jobs": [AJOB, VJOB]}))
    called = {}

    def fake_collect(bucket, archive_jobs, has_versioned, **kw):
        called["bucket"] = bucket
        called["archive_jobs"] = list(archive_jobs)
        called["has_versioned"] = has_versioned
        called["rclone_config"] = kw.get("rclone_config")
        return {"media/movies": {"bytes": 5, "count": 1}, "appdata": {"bytes": 9, "count": 2}}

    monkeypatch.setattr(routes.usage, "collect_usage", fake_collect)
    t = _csrf(client)
    r = client.post("/costs/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)
    assert called["bucket"] == "mybucket"
    assert called["archive_jobs"] == ["movies"]
    assert called["has_versioned"] is True
    assert called["rclone_config"]  # rclone.conf path passed, no env creds needed
    cached = usage.load_cached(dirs["cache"])
    assert cached["data"]["media/movies"] == {"bytes": 5, "count": 1}
    assert cached["data"]["appdata"] == {"bytes": 9, "count": 2}


def test_refresh_with_malformed_jobs_json_is_not_500(client, dirs, monkeypatch):
    # A hand-broken config/jobs.json must not 500 the refresh: jobs_io.load is the
    # fail-safe read path (returns [] on a whole-file parse error), so costs_refresh
    # degrades to "no jobs" and still redirects. Regression guard for Task 7's
    # parked corrupt-jobs.json concern.
    from app.gui import routes
    pathlib.Path(dirs["config"], "backup.env").write_text("S3_BUCKET=mybucket\nAWS_REGION=us-east-1\n")
    pathlib.Path(dirs["config"], "jobs.json").write_text("{ not valid json ")
    monkeypatch.setattr(routes.usage, "collect_usage", lambda *a, **k: {})
    t = _csrf(client)
    r = client.post("/costs/refresh", data={"csrf": t})
    assert r.status_code != 500
    assert r.status_code in (302, 303)


def test_refresh_with_nameless_jobs_entry_is_not_500(client, dirs, monkeypatch):
    # Regression (FIX 2): costs_refresh iterates jobs (j["name"], j.get("type")). A
    # hand-edited nameless entry must not 500 — jobs_io.load drops it (fail-safe).
    from app.gui import routes
    pathlib.Path(dirs["config"], "backup.env").write_text("S3_BUCKET=mybucket\nAWS_REGION=us-east-1\n")
    pathlib.Path(dirs["config"], "jobs.json").write_text(
        json.dumps({"jobs": [{"type": "archive", "source": "x", "schedule": "0 4 * * 0"}, AJOB]}))
    monkeypatch.setattr(routes.usage, "collect_usage", lambda *a, **k: {})
    t = _csrf(client)
    r = client.post("/costs/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)


def test_refresh_without_bucket_flashes_and_does_not_call_collect_usage(client, dirs, monkeypatch):
    from app.gui import routes
    called = {"n": 0}
    monkeypatch.setattr(routes.usage, "collect_usage",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    t = _csrf(client)
    r = client.post("/costs/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)   # no crash
    assert called["n"] == 0


# --- estimate_io.current_costs -----------------------------------------------

def test_current_costs_prices_cached_usage_archive_at_class_appdata_at_standard(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; cache.mkdir()
    pathlib.Path(cfg, "jobs.json").write_text(json.dumps({"jobs": [AJOB]}))
    usage.save_cached(str(cache), {
        "appdata": {"bytes": 10 * 1024 ** 3, "count": 5},
        "media/movies": {"bytes": 100 * 1024 ** 3, "count": 2},
    })
    prices = load_prices("us-east-1")
    result = estimate_io.current_costs(str(cfg), str(cache), prices)
    by_prefix = {p["prefix"]: p for p in result["prefixes"]}
    assert by_prefix["appdata"]["class"] == "STANDARD"
    assert by_prefix["appdata"]["label"] == "all versioned jobs (shared repo)"
    assert by_prefix["media/movies"]["class"] == "DEEP_ARCHIVE"   # the archive job's own class
    assert by_prefix["appdata"]["monthly"] == pytest.approx(10 * prices.storage_gb_month["STANDARD"])
    assert by_prefix["media/movies"]["monthly"] == pytest.approx(100 * prices.storage_gb_month["DEEP_ARCHIVE"])
    assert result["total_monthly"] == pytest.approx(
        by_prefix["appdata"]["monthly"] + by_prefix["media/movies"]["monthly"])


def test_current_costs_unavailable_without_cached_usage(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; cache.mkdir()
    result = estimate_io.current_costs(str(cfg), str(cache), load_prices("us-east-1"))
    assert result == {"available": False}


def test_current_costs_skips_failed_prefix_but_keeps_the_rest(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; cache.mkdir()
    pathlib.Path(cfg, "jobs.json").write_text(json.dumps({"jobs": [AJOB]}))
    usage.save_cached(str(cache), {"appdata": None, "media/movies": {"bytes": 1024 ** 3, "count": 1}})
    result = estimate_io.current_costs(str(cfg), str(cache), load_prices("us-east-1"))
    assert [p["prefix"] for p in result["prefixes"]] == ["media/movies"]


# --- estimate_io.billing_view -------------------------------------------------

def test_billing_view_not_connected_without_creds(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    assert estimate_io.billing_view(str(cfg)) == {"connected": False}


def test_billing_view_parses_stubbed_data_when_connected(tmp_path, monkeypatch):
    cfg = tmp_path / "config"; cfg.mkdir()
    config_io.write_secrets(str(cfg), {"COST_EXPLORER_ACCESS_KEY_ID": "A",
                                       "COST_EXPLORER_SECRET_ACCESS_KEY": "B"})
    monkeypatch.setattr(billing, "monthly_costs",
                        lambda creds, **kw: [{"month": "2026-07", "amount": 1.0}])
    monkeypatch.setattr(billing, "forecast",
                        lambda creds, **kw: {"month": "2026-08", "amount": 2.0})
    result = estimate_io.billing_view(str(cfg))
    assert result == {"connected": True,
                      "months": [{"month": "2026-07", "amount": 1.0}],
                      "forecast": {"month": "2026-08", "amount": 2.0},
                      "tag": None}


def test_billing_view_captures_billingerror(tmp_path, monkeypatch):
    cfg = tmp_path / "config"; cfg.mkdir()
    config_io.write_secrets(str(cfg), {"COST_EXPLORER_ACCESS_KEY_ID": "A",
                                       "COST_EXPLORER_SECRET_ACCESS_KEY": "B"})

    def boom(creds, **kw):
        raise billing.BillingError("access denied")

    monkeypatch.setattr(billing, "monthly_costs", boom)
    result = estimate_io.billing_view(str(cfg))
    assert result == {"connected": True, "error": "access denied"}


# --- /estimate page renders the new sections without crashing ----------------

def test_estimate_page_renders_current_spend_and_billing_sections(client):
    r = client.get("/estimate")
    assert r.status_code == 200
    low = r.data.lower()
    assert b"current spend" in low
    assert b"connect aws billing" in low
