# tests/gui/test_costs_routes.py — the Cost workbench's mutating routes (spec 5.6):
# /costs/refresh + /costs/billing/refresh now launch a DETACHED sysop op (they no
# longer call usage.collect_usage / Cost Explorer synchronously — those assertions
# live in tests/engine/test_sysop.py); /costs/scenario persists $CONFIG_DIR/cost.json;
# /costs/billing 301s to Keys & secrets. Also covers current_costs / billing_view.
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
VFJOB = {"name": "docs", "type": "versioned-files", "source": "docs",
         "schedule": "0 5 * * *", "enabled": True, "storage_class": "DEEP_ARCHIVE",
         "retention_days": 90}


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
    client.get("/cost")  # issues the CSRF token into the session
    with client.session_transaction() as s:
        return s["_csrf"]


@pytest.fixture
def launched(monkeypatch):
    """Capture ops.launch_py without spawning a detached process."""
    from app.gui import routes
    calls = []
    monkeypatch.setattr(routes.ops, "launch_py",
                        lambda cfg, module, args, **kw: calls.append((module, list(args), kw)) or "rid")
    return calls


# --- CSRF-first --------------------------------------------------------------

def test_refresh_requires_csrf(client):
    assert client.post("/costs/refresh", data={}).status_code == 400


def test_billing_refresh_requires_csrf(client):
    assert client.post("/costs/billing/refresh", data={}).status_code == 400


def test_scenario_requires_csrf(client):
    assert client.post("/costs/scenario", data={"restore_fraction": "1"}).status_code == 400


# --- /costs/refresh: detached sysop launch, NOT synchronous collect_usage ----

def test_refresh_launches_usage_refresh_sysop(client, dirs, launched, monkeypatch):
    from app.gui import routes
    pathlib.Path(dirs["config"], "backup.env").write_text("S3_BUCKET=mybucket\nAWS_REGION=us-east-1\n")
    called = {"n": 0}
    monkeypatch.setattr(routes.usage, "collect_usage",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    t = _csrf(client)
    r = client.post("/costs/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)
    assert launched and launched[0][0] == "app.engine.sysop"
    assert launched[0][1] == ["usage-refresh"]
    assert called["n"] == 0                    # collect_usage NOT called in the request


def test_refresh_without_bucket_flashes_and_launches_nothing(client, launched):
    t = _csrf(client)
    r = client.post("/costs/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)         # no crash
    assert launched == []                       # launched nothing


def test_billing_refresh_launches_billing_check_sysop(client, launched):
    t = _csrf(client)
    r = client.post("/costs/billing/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)
    assert launched and launched[0][1] == ["billing-check"]


# --- /costs/scenario: persists $CONFIG_DIR/cost.json (spec 5.6 band 3) --------

def test_scenario_writes_cost_json_and_redirects_saved(client, dirs):
    t = _csrf(client)
    r = client.post("/costs/scenario", data={"csrf": t, "restore_fraction": "0.5",
                                             "restores_per_year": "3", "retrieval_tier": "Standard"})
    assert r.status_code in (302, 303)
    saved = json.loads(pathlib.Path(dirs["config"], "cost.json").read_text())
    assert saved["restore_fraction"] == 0.5
    assert saved["restores_per_year"] == 3
    assert saved["retrieval_tier"] == "Standard"
    assert "set_at" in saved
    # the success flash carries "Saved."
    with client.session_transaction() as s:
        flashes = dict(s["_flashes"]) if "_flashes" in s else {}
    assert "Saved." in flashes.values()


def test_scenario_bad_restore_fraction_is_400_and_no_file(client, dirs):
    t = _csrf(client)
    r = client.post("/costs/scenario", data={"csrf": t, "restore_fraction": "0.7",
                                             "restores_per_year": "1", "retrieval_tier": "Bulk"})
    assert r.status_code == 400
    assert not pathlib.Path(dirs["config"], "cost.json").exists()


def test_scenario_missing_file_is_read_as_defaults(dirs, template_path, tmp_path):
    # A missing cost.json means the model's own defaults and is never an error (5.6).
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": str(tmp_path / "src"), "PRICES_LIVE": False,
                      "SECRET_KEY": "test", "TESTING": True})
    assert estimate_io.read_cost_scenario(dirs["config"]) == {}


# --- /costs/billing is gone: 301 to Keys & secrets (spec 5.6 band 5) ---------

def test_costs_billing_301s_to_setup_keys(client):
    r = client.post("/costs/billing", data={})
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/setup/keys#billing")


# --- /cost page: bands from caches, no Cost Explorer form --------------------

def test_cost_page_renders_in_the_bucket_now_no_ce_form(client):
    r = client.get("/cost")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "In the bucket now" in body
    assert "COST_EXPLORER_" not in body        # the credential is edited at Keys & secrets


def test_small_measured_job_shows_mb_on_the_per_job_row(client, dirs):
    # Owner request: sizes scale their unit -- a small measured job's per-job row
    # reads "358.4 MB", not a flat two-decimal "0.35 GB".
    small = {"name": "docs", "type": "versioned-files", "source": "docs",
             "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
             "retention_days": 7}
    pathlib.Path(dirs["config"], "jobs.json").write_text(json.dumps({"jobs": [small]}))
    small_bytes = int(0.35 * 1024 ** 3)          # the owner's own example figure
    usage.save_cached(dirs["cache"], {"media/docs": {"bytes": small_bytes, "count": 12}})
    r = client.get("/cost", query_string={"docs_change_rate_pct": "10"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "358.4 MB" in body
    assert "0.35 GB" not in body
    # `old_versions_gb` (from li.versioning / rate) used to print a flat
    # "%.1f GB" regardless of magnitude -- this job's is small enough (~250 MB)
    # that the bug would have shown "0.2 GB" or "0.3 GB" here.
    assert "of old versions" in body
    assert "250.9 MB of old versions" in body
    assert " GB of old versions" not in body


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
    assert by_prefix["media/movies"]["class"] == "DEEP_ARCHIVE"
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


# --- estimate_io.read_billing_cache (cache-only; no Cost Explorer at render) --

def test_read_billing_cache_not_connected_without_file(tmp_path):
    cache = tmp_path / "cache"; cache.mkdir()
    assert estimate_io.read_billing_cache(str(cache)) == {"connected": False}


def test_read_billing_cache_parses_the_cache(tmp_path):
    cache = tmp_path / "cache"; cache.mkdir()
    import time
    pathlib.Path(cache, "billing.json").write_text(json.dumps({
        "fetched_at": time.time(), "months": [{"month": "2026-07", "amount": 1.0}],
        "forecast": {"month": "2026-08", "amount": 2.0}, "tag": None, "error": None}))
    v = estimate_io.read_billing_cache(str(cache))
    assert v["connected"] is True
    assert v["months"] == [{"month": "2026-07", "amount": 1.0}]
    assert v["stale"] is False


# --- estimate_io.billing_view (unchanged live view; kept for the sysop path) --

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
