# tests/gui/test_estimate_io.py — the GUI adapter building an N-job Scenario from
# a seeded jobs.json (jobs_io.load is a raw read; no real source folders needed).
import json
import pathlib
import pytest
from app.gui import estimate_io
from app.estimator.schedule import backups_per_month

SRC = "/backup/media"  # source_root is unused by the adapter today (reserved)

VJOB = {"name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
        "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}
AJOB = {"name": "movies", "type": "archive", "source": "movies",
        "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
        "mirror": False}
VFJOB = {"name": "docs", "type": "versioned-files", "source": "docs",
         "schedule": "0 2 * * *", "enabled": True, "storage_class": "DEEP_ARCHIVE",
         "retention_days": 45}

def _cfg(tmp_path, jobs=None, env=""):
    cfg = tmp_path / "config"
    cfg.mkdir()
    if jobs is not None:
        pathlib.Path(cfg, "jobs.json").write_text(json.dumps({"jobs": jobs}))
    if env:
        (cfg / "backup.env").write_text(env)
    return str(cfg)

def _by_name(scn):
    return {j.name: j for j in scn.jobs}

# --- scenario_from_jobs ---

def test_empty_when_no_jobs_file(tmp_path):
    s = estimate_io.scenario_from_jobs(_cfg(tmp_path), SRC)
    assert s.jobs == ()

def test_builds_a_job_per_entry(tmp_path):
    s = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, AJOB]), SRC)
    by = _by_name(s)
    assert set(by) == {"appdata", "movies"}
    assert by["appdata"].engine == "versioned" and by["appdata"].storage_class == "STANDARD"
    assert by["movies"].engine == "archive" and by["movies"].storage_class == "DEEP_ARCHIVE"

def test_versioned_retention_from_keep_policy_archive_default(tmp_path):
    # An archive job with no explicit retention now migrates to its own per-job
    # {"type": "days", "days": 180} default (jobs_io's archive-default policy),
    # NOT a fallback to the scenario-level window (retention-policies Phase 1).
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, AJOB]), SRC))
    # A versioned/tiered job is modelled NATIVELY as "tiered": the keep tiers go
    # through to app.estimator.tiered (old data = gaps between sparse retained
    # snapshots), rather than being collapsed to a days-window or a snapshot count.
    a = by["appdata"]
    assert a.retention_type == "tiered"
    assert (a.keep_last, a.keep_daily, a.keep_weekly, a.keep_monthly) == (3, 7, 4, 6)
    assert a.retention_count == 0 and a.versioning_retention_days is None
    assert a.backup_interval_days > 0
    # An archive job keeps its own {"type":"days","days":180} default.
    assert by["movies"].versioning_retention_days == 180
    assert by["movies"].retention_type == "days"

def test_backups_per_month_from_schedule(tmp_path):
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, AJOB]), SRC))
    assert by["appdata"].backups_per_month == backups_per_month("0 3 * * *")  # ~daily
    assert by["movies"].backups_per_month == backups_per_month("0 4 * * 0")   # ~weekly
    assert by["appdata"].backups_per_month > by["movies"].backups_per_month

def test_size_from_cached_usage(tmp_path):
    usage = {"appdata": {"bytes": 30 * 1024 ** 3, "count": 42},
             "media/movies": {"bytes": 1000 * 1024 ** 3, "count": 7}}
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, AJOB]), SRC, usage=usage))
    assert by["appdata"].size_gb == 30 and by["appdata"].file_count == 42
    assert by["movies"].size_gb == 1000 and by["movies"].file_count == 7

def test_size_defaults_without_usage(tmp_path):
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [AJOB]), SRC))
    assert by["movies"].size_gb == estimate_io._DEFAULT_SIZE_GB
    assert by["movies"].file_count == estimate_io._DEFAULT_FILES

# --- Task 4: versioned-files -> cost profile mapping ---

def test_versioned_files_job_maps_to_versioned_cost_profile(tmp_path):
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VFJOB]), SRC))
    j = by["docs"]
    assert j.engine == "versioned-files"
    assert j.storage_class == "DEEP_ARCHIVE"
    assert j.versioning_retention_days == 45

def test_versioned_files_default_retention_when_missing(tmp_path):
    job = {k: v for k, v in VFJOB.items() if k != "retention_days"}
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [job]), SRC))
    assert by["docs"].versioning_retention_days == 90

def test_versioned_files_size_from_cached_usage_media_prefix(tmp_path):
    # versioned-files jobs live under media/<job>/ in S3 (their own per-job
    # prefix, like archive) -- NOT the shared "appdata" restic repo.
    usage = {"media/docs": {"bytes": 50 * 1024 ** 3, "count": 500}}
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VFJOB]), SRC, usage=usage))
    assert by["docs"].size_gb == 50 and by["docs"].file_count == 500

def test_versioned_files_produces_a_cost_estimate(tmp_path):
    from app.estimator.model import estimate
    from app.estimator.prices import load_prices
    s = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VFJOB]), SRC)
    est = estimate(s, load_prices("us-east-1"))
    assert est.monthly_total > 0

# --- scenario_from_params (live what-if) ---

def test_params_override_per_job(tmp_path):
    s = estimate_io.scenario_from_params(
        {"appdata_size_gb": "50", "appdata_storage_class": "GLACIER_IR"},
        _cfg(tmp_path, [VJOB, AJOB]), SRC)
    by = _by_name(s)
    assert by["appdata"].size_gb == 50 and by["appdata"].storage_class == "GLACIER_IR"
    assert by["movies"].storage_class == "DEEP_ARCHIVE"  # untouched

def test_params_apply_globals(tmp_path):
    s = estimate_io.scenario_from_params(
        {"versioning_retention_days": "45", "restores_per_year": "3", "retrieval_tier": "Standard"},
        _cfg(tmp_path, [VJOB]), SRC)
    assert s.versioning_retention_days == 45 and s.restores_per_year == 3
    assert s.retrieval_tier == "Standard"

def test_params_reject_bad_number(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"appdata_size_gb": "abc"}, _cfg(tmp_path, [VJOB]), SRC)

def test_params_reject_negative(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"movies_size_gb": "-5"}, _cfg(tmp_path, [AJOB]), SRC)

def test_params_reject_unknown_storage_class(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"movies_storage_class": "NEBULA"}, _cfg(tmp_path, [AJOB]), SRC)

def test_params_reject_unknown_retrieval_tier(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"retrieval_tier": "Warp"}, _cfg(tmp_path, [VJOB]), SRC)

def test_params_packing_requires_positive_member(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params(
            {"movies_packing": "1", "movies_pack_member_gb": "0"}, _cfg(tmp_path, [AJOB]), SRC)

def test_params_thread_cached_usage_into_base_sizes(tmp_path):
    # scenario_from_params(..., usage=...) forwards to the internal scenario_from_jobs
    # call, so the modeled per-job breakdown reflects real measured sizes when a form
    # param doesn't already override that job's size.
    real_usage = {"appdata": {"bytes": 30 * 1024 ** 3, "count": 42}}
    s = estimate_io.scenario_from_params({}, _cfg(tmp_path, [VJOB]), SRC, usage=real_usage)
    assert _by_name(s)["appdata"].size_gb == 30 and _by_name(s)["appdata"].file_count == 42


def test_dotted_job_name_params_are_not_split(tmp_path):
    # A job name containing '.' must be used verbatim as the field prefix.
    dotted = {**VJOB, "name": "app.data"}
    s = estimate_io.scenario_from_params({"app.data_size_gb": "77"}, _cfg(tmp_path, [dotted]), SRC)
    assert _by_name(s)["app.data"].size_gb == 77

def test_defaults_are_valid_and_computable(tmp_path):
    from app.estimator.model import estimate
    from app.estimator.prices import load_prices
    s = estimate_io.scenario_from_params({}, _cfg(tmp_path, [VJOB, AJOB]), SRC)
    est = estimate(s, load_prices("us-east-1"))
    assert est.monthly_total > 0

# --- form_defaults ---

def test_form_defaults_globals_and_per_job_list(tmp_path):
    d = estimate_io.form_defaults(_cfg(tmp_path, [VJOB, AJOB]), SRC)
    assert d["region"] == "us-east-1"
    assert d["retrieval_tier"] == "Bulk"
    assert d["versioning_retention_days"] == 30
    names = {j["name"] for j in d["jobs"]}
    assert names == {"appdata", "movies"}
    movies = next(j for j in d["jobs"] if j["name"] == "movies")
    assert movies["storage_class"] == "DEEP_ARCHIVE" and movies["engine"] == "archive"

def test_form_defaults_reads_region_from_env(tmp_path):
    d = estimate_io.form_defaults(_cfg(tmp_path, [VJOB], env="AWS_REGION=eu-central-1\n"), SRC)
    assert d["region"] == "eu-central-1"

def test_form_defaults_no_jobs_empty_list(tmp_path):
    d = estimate_io.form_defaults(_cfg(tmp_path), SRC)
    assert d["jobs"] == []

# --- projection_bundle (cost over time) ---

from app.estimator.prices import load_prices

def _prices():
    return load_prices("us-east-1", live=False)

def test_projection_bundle_shape(tmp_path):
    scn = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    b = estimate_io.projection_bundle(scn, _prices(), months=24)
    assert set(b) == {"primary", "comparison", "onetime", "steady_state_month"}
    assert len(b["primary"]["months"]) == 24
    assert set(b["comparison"]) == {"no_versioning", "rolling_30"}
    assert b["onetime"]["first_month"] == b["primary"]["months"][0]["total"]

def test_projection_bundle_no_versioning_curve_is_flat_zero(tmp_path):
    scn = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    nv = estimate_io.projection_bundle(scn, _prices())["comparison"]["no_versioning"]
    assert all(m["versioning"] == 0.0 for m in nv["months"])

def test_projection_bundle_lockin_lists_only_cold_jobs(tmp_path):
    # VJOB=STANDARD (no lock-in), VFJOB=DEEP_ARCHIVE (has lock-in)
    scn = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    lockin = estimate_io.projection_bundle(scn, _prices())["onetime"]["lockin"]
    names = {row["job"] for row in lockin}
    assert names == {"docs"}  # VFJOB
    assert all(row["amount"] > 0 for row in lockin)

# --- wizard_estimate: churn override + projection + breakdown ---

def _wiz_params(**over):
    p = {"name": "bak-manga", "type": "versioned-files", "source": "comics/mangas",
         "schedule": "0 3 1 * *", "storage_class": "DEEP_ARCHIVE",
         "size_gb": "1000", "file_count": "5000", "retention_days": "180"}
    p.update(over)
    return p

def test_wizard_estimate_churn_lowers_versioning_and_monthly(tmp_path):
    cfg = _cfg(tmp_path, [])
    prices = _prices()
    lo = estimate_io.wizard_estimate(_wiz_params(change_rate_pct="1"), cfg, SRC, prices)
    hi = estimate_io.wizard_estimate(_wiz_params(change_rate_pct="30"), cfg, SRC, prices)
    assert hi["breakdown"]["versioning"] > lo["breakdown"]["versioning"]
    assert hi["this_job_monthly"] > lo["this_job_monthly"]
    assert lo["breakdown"]["change_rate_pct"] == 1.0

def test_wizard_estimate_returns_projection_and_breakdown(tmp_path):
    r = estimate_io.wizard_estimate(_wiz_params(change_rate_pct="1"), _cfg(tmp_path, []), SRC, _prices())
    for k in ("first_bill", "steady_monthly", "steady_month", "at_12", "at_24"):
        assert k in r["projection"]
    for k in ("billed_gb", "storage", "versioning", "rotation", "ingest",
              "upload_onetime", "lockin_onetime", "change_rate_pct", "retention_days"):
        assert k in r["breakdown"]
    assert r["breakdown"]["lockin_onetime"] > 0          # DEEP_ARCHIVE has a minimum
    assert r["breakdown"]["retention_days"] == 180

def test_wizard_estimate_static_versions_are_minority_of_bill(tmp_path):
    # ~1% churn: old-version storage should be a small fraction of base storage.
    b = estimate_io.wizard_estimate(_wiz_params(change_rate_pct="1"), _cfg(tmp_path, []), SRC, _prices())["breakdown"]
    assert b["versioning"] < b["storage"]

def test_wizard_estimate_defaults_to_no_change(tmp_path):
    # No change_rate_pct param -> 0% churn by default: pure storage, no version/
    # rotation cost. The user opts into churn via the Static/Some/A-lot selector.
    r = estimate_io.wizard_estimate(_wiz_params(), _cfg(tmp_path, []), SRC, _prices())
    assert r["breakdown"]["change_rate_pct"] == 0.0
    assert r["breakdown"]["versioning"] == 0.0
    assert r["breakdown"]["rotation"] == 0.0

def test_form_defaults_change_rate_is_zero(tmp_path):
    d = estimate_io.form_defaults(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    assert d["jobs"] and all(j["change_rate_pct"] == 0.0 for j in d["jobs"])

# --- Task 8: count + keep_all retention shapes map through to JobInputs ---

def test_count_retention_policy_maps_to_job_inputs(tmp_path):
    job = {**AJOB, "retention": {"type": "count", "count": 7}}
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [job]), SRC))
    assert by["movies"].retention_type == "count"
    assert by["movies"].retention_count == 7

def test_keep_all_retention_policy_maps_to_job_inputs(tmp_path):
    job = {**AJOB, "retention": {"type": "keep_all"}}
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [job]), SRC))
    assert by["movies"].retention_type == "keep_all"
    assert by["movies"].retention_count == 0

def test_days_retention_policy_maps_to_job_inputs(tmp_path):
    job = {**AJOB, "retention": {"type": "days", "days": 42}}
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [job]), SRC))
    assert by["movies"].retention_type == "days"
    assert by["movies"].retention_count == 0
    assert by["movies"].versioning_retention_days == 42

def test_tiered_retention_policy_is_modelled_natively(tmp_path):
    # A tiered restic keep-policy is carried through as retention_type="tiered" with
    # its keep tiers intact (not collapsed to a days-window or a snapshot count).
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB]), SRC))
    a = by["appdata"]
    assert a.retention_type == "tiered"
    assert (a.keep_last, a.keep_daily, a.keep_weekly, a.keep_monthly) == (3, 7, 4, 6)
    assert a.retention_count == 0 and a.versioning_retention_days is None


# ===========================================================================
# Task 12: the Cost workbench adapters (all CALL the frozen model; the Task-1
# guard in tests/estimator/test_untouched.py stays green).
# ===========================================================================
from app.estimator import model as _model
from app.estimator import usage as _usage


def _cache(tmp_path, data=None, billing=None):
    cache = tmp_path / "cache"
    (cache / "state").mkdir(parents=True, exist_ok=True)
    if data is not None:
        _usage.save_cached(str(cache), data)
    if billing is not None:
        pathlib.Path(cache, "billing.json").write_text(json.dumps(billing))
    return str(cache)


# --- delta_verdict (4.6): the 15/30 bands ----------------------------------

def test_delta_verdict_close_enough_expensive_side():
    # +14.1% (the Board example) is within 15% -> "close enough to trust".
    v = estimate_io.delta_verdict(114.1, 100.0)
    assert v == "close enough to trust, and it errs on the expensive side"


def test_delta_verdict_close_enough_cheap_side():
    v = estimate_io.delta_verdict(85.9, 100.0)
    assert v == "close enough to trust, and it errs on the cheap side"


def test_delta_verdict_model_runs_high_band():
    assert estimate_io.delta_verdict(120.0, 100.0) == "model runs high — worth a look at the assumptions"


def test_delta_verdict_model_runs_low_band():
    assert estimate_io.delta_verdict(80.0, 100.0) == "model runs low — worth a look at the assumptions"


def test_delta_verdict_far_apart_band():
    v = estimate_io.delta_verdict(140.0, 100.0)
    assert v == "far apart — check the assumptions and whether the invoice covers more than these backups"


# --- provenance_of (4.6/7.9): only "assumed" or "projected" ----------------

def test_provenance_of_all_measured_is_projected():
    assert estimate_io.provenance_of(["measured", "measured"]) == "projected"


def test_provenance_of_any_assumed_is_assumed():
    assert estimate_io.provenance_of(["measured", "assumed"]) == "assumed"


def test_provenance_of_empty_is_projected():
    assert estimate_io.provenance_of([]) == "projected"


def test_provenance_of_never_returns_measured_or_invoiced():
    # It answers for a COMPUTED figure; observed marks are set directly (4.6).
    assert estimate_io.provenance_of(["measured"]) == "projected"


# --- restore_quote (7.6): no new math; == model.restore_cost ---------------

def test_restore_quote_amount_equals_model_restore_cost(tmp_path):
    cfg = _cfg(tmp_path, [AJOB])
    data = {"media/movies": {"bytes": 1000 * 1024 ** 3, "count": 7}}
    cache = _cache(tmp_path, data=data)
    prices = _prices()
    q = estimate_io.restore_quote(cfg, cache, prices, "movies", tier="Standard")
    # Rebuild the same scenario/job the adapter uses and price it with the frozen model.
    scn = estimate_io.scenario_from_jobs(cfg, SRC, usage=data)
    from dataclasses import replace
    scn = replace(scn, retrieval_tier="Standard")
    j = _by_name(scn)["movies"]
    assert q["amount"] == pytest.approx(_model.restore_cost(j, scn, prices, 1.0))
    assert q["tier"] == "Standard" and q["storage_class"] == "DEEP_ARCHIVE"
    assert q["provenance"] == "measured"                    # size came from the usage cache


def test_restore_quote_warmup_hours_for_cold_class(tmp_path):
    cfg = _cfg(tmp_path, [AJOB])
    cache = _cache(tmp_path, data={"media/movies": {"bytes": 5 * 1024 ** 3, "count": 2}})
    q = estimate_io.restore_quote(cfg, cache, _prices(), "movies", tier="Standard")
    assert q["warmup_hours"] == (None, 12)                  # DEEP_ARCHIVE Standard: up to 12 h


def test_restore_quote_override_size_is_measured(tmp_path):
    cfg = _cfg(tmp_path, [AJOB])
    cache = _cache(tmp_path)                                # no usage cache
    q = estimate_io.restore_quote(cfg, cache, _prices(), "movies",
                                  tier="Bulk", size_gb=200.0, file_count=5)
    assert q["size_gb"] == 200.0 and q["provenance"] == "measured"


# --- board_cost (8.1): the Board's cost object, caches only ----------------

def test_board_cost_with_caches(tmp_path):
    cfg = _cfg(tmp_path, [VJOB, AJOB])
    data = {"appdata": {"bytes": 56594862080, "count": 533},
            "media/movies": {"bytes": 1957000000000, "count": 232021}}
    cache = _cache(tmp_path, data=data,
                   billing={"fetched_at": 1757833200.0,
                            "months": [{"month": "2026-08", "amount": 3.98}],
                            "forecast": None, "tag": None})
    bc = estimate_io.board_cost(cfg, cache, _prices())
    assert bc["in_bucket_bytes"] == 56594862080 + 1957000000000
    assert bc["invoice"]["month"] == "2026-08" and bc["invoice"]["amount"] == 3.98
    assert bc["model_monthly"] is not None and bc["model_monthly"] > 0
    assert bc["model_monthly_provenance"] in ("assumed", "projected")
    assert {p["name"] for p in bc["per_job"]} == {"appdata", "movies"}
    mv = next(p for p in bc["per_job"] if p["name"] == "movies")
    # tier_label is the PLAIN phrase; the CONSTANT lives in storage_class (8.1 minor).
    assert mv["tier_label"] == "Thaw first, hours"
    assert mv["storage_class"] == "DEEP_ARCHIVE"
    assert "·" not in mv["tier_label"]


def test_board_cost_without_caches(tmp_path):
    cfg = _cfg(tmp_path, [VJOB, AJOB])
    cache = _cache(tmp_path)                                # empty: no usage, no billing
    bc = estimate_io.board_cost(cfg, cache, _prices())
    assert bc["in_bucket_bytes"] is None
    assert bc["invoice"] is None
    assert bc["delta"] is None


def test_board_cost_no_jobs(tmp_path):
    cfg = _cfg(tmp_path, [])
    bc = estimate_io.board_cost(cfg, _cache(tmp_path), _prices())
    assert bc["per_job"] == [] and bc["model_monthly"] is None


# --- job_cost_band (8.7) ----------------------------------------------------

def test_job_cost_band_figures_and_change_rate(tmp_path):
    cfg = _cfg(tmp_path, [VJOB])
    data = {"appdata": {"bytes": 56594862080, "count": 533}}
    cache = _cache(tmp_path, data=data)
    job = {**VJOB}
    band = estimate_io.job_cost_band(job, cfg, cache, _prices())
    assert band["first_bill"] is not None
    assert band["in_bucket_bytes"] == 56594862080
    assert band["size_provenance"] == "measured"
    assert band["file_count"] == 533
    assert "change_rate_pct" in band            # surfaces the §7.8 assumption (5.2 row)
    assert band["steady_month"] >= 1


def test_job_cost_band_shared_store_flag(tmp_path):
    v2 = {**VJOB, "name": "appdata2"}
    cfg = _cfg(tmp_path, [VJOB, v2])
    band = estimate_io.job_cost_band(VJOB, cfg, _cache(tmp_path), _prices())
    assert band["shared_store"] is True and band["shared_by"] == 2


# --- cost_page (8.7) --------------------------------------------------------

def test_cost_page_shape(tmp_path):
    cfg = _cfg(tmp_path, [VJOB, AJOB])
    data = {"appdata": {"bytes": 30 * 1024 ** 3, "count": 42},
            "media/movies": {"bytes": 1000 * 1024 ** 3, "count": 7}}
    cache = _cache(tmp_path, data=data)
    page = estimate_io.cost_page({}, cfg, cache, _prices(), SRC)
    for k in ("jobs", "monthly_total", "projection", "current", "billing",
              "per_job", "restore", "assumptions", "price"):
        assert k in page, k
    assert {p["name"] for p in page["per_job"]} == {"appdata", "movies"}
    assert {r["name"] for r in page["restore"]} == {"appdata", "movies"}
    assert set(page["assumptions"]) == {"jobs", "scenario"}


def test_cost_page_scrubber_matches_projection_months(tmp_path):
    cfg = _cfg(tmp_path, [VJOB])
    page = estimate_io.cost_page({}, cfg, _cache(tmp_path), _prices(), SRC)
    months = page["projection"]["primary"]["months"]
    assert len(months) == 24
    # the scrubber readout is exactly projection.primary.months[m-1] (5.6)
    assert months[5]["total"] == page["projection"]["primary"]["months"][5]["total"]


# --- keep_all at 0% change is bounded, and the ADAPTER says so (7.9) --------

def test_cost_page_keep_all_at_zero_change_is_bounded(tmp_path):
    job = {**AJOB, "retention": {"type": "keep_all"}}
    cfg = _cfg(tmp_path, [job])
    # default change rate is 0% -> keep_all cannot grow -> the adapter overrides
    # the model's unbounded=True to False.
    page = estimate_io.cost_page({}, cfg, _cache(tmp_path), _prices(), SRC)
    assert page["projection"]["primary"]["unbounded"] is False


def test_cost_page_keep_all_with_change_stays_unbounded(tmp_path):
    job = {**AJOB, "retention": {"type": "keep_all"}}
    cfg = _cfg(tmp_path, [job])
    page = estimate_io.cost_page({"movies_change_rate_pct": "10"}, cfg,
                                 _cache(tmp_path), _prices(), SRC)
    assert page["projection"]["primary"]["unbounded"] is True


def test_cost_page_applies_saved_scenario_defaults(tmp_path):
    cfg = _cfg(tmp_path, [VJOB])
    # a saved cost.json scenario supplies the retrieval tier when no param overrides it
    pathlib.Path(cfg, "cost.json").write_text(json.dumps(
        {"restore_fraction": 0.5, "restores_per_year": 3, "retrieval_tier": "Standard",
         "set_at": "2026-09-12T00:00:00Z"}))
    page = estimate_io.cost_page({}, cfg, _cache(tmp_path), _prices(), SRC)
    assert page["assumptions"]["scenario"]["retrieval_tier"] == "Standard"
    assert page["assumptions"]["scenario"]["restore_fraction"] == 0.5
