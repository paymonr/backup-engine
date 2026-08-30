# tests/gui/test_estimate_io.py — the GUI adapter building an N-job Scenario from
# a seeded jobs.json (jobs_io.load is a raw read; no real source folders needed).
import json
import pathlib
import pytest
from app.gui import estimate_io
from app.estimator.model import effective_retention_days
from app.estimator.schedule import backups_per_month

SRC = "/backup/media"  # source_root is unused by the adapter today (reserved)

VJOB = {"name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
        "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}
AJOB = {"name": "movies", "type": "archive", "source": "movies",
        "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
        "mirror": False}

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

def test_versioned_retention_from_keep_policy_archive_none(tmp_path):
    by = _by_name(estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, AJOB]), SRC))
    assert by["appdata"].versioning_retention_days == effective_retention_days(
        keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=6)  # == 180
    assert by["movies"].versioning_retention_days is None

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
