import pytest
from app.gui import estimate_io
from app.estimator.model import effective_retention_days

def _cfg(tmp_path, body=""):
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "backup.env").write_text(body)
    return str(cfg)

def test_form_defaults_reads_env_classes_and_keep_policy(tmp_path):
    cfg = _cfg(tmp_path, "APPDATA_STORAGE_CLASS=STANDARD_IA\nMEDIA_STORAGE_CLASS=GLACIER\nKEEP_MONTHLY=12\n")
    d = estimate_io.form_defaults(cfg)
    assert d["appdata"]["storage_class"] == "STANDARD_IA"
    assert d["media"]["storage_class"] == "GLACIER"
    assert d["keep_monthly"] == 12
    assert d["keep_daily"] == 7  # default when the env key is absent

def test_form_defaults_without_backup_env_uses_model_defaults(tmp_path):
    d = estimate_io.form_defaults(str(tmp_path))  # no backup.env present
    assert d["appdata"]["storage_class"] == "STANDARD"
    assert d["media"]["storage_class"] == "DEEP_ARCHIVE"
    assert d["region"] == "us-east-1"

def test_form_defaults_sanitizes_bad_env_class(tmp_path):
    d = estimate_io.form_defaults(_cfg(tmp_path, "APPDATA_STORAGE_CLASS=NOPE\n"))
    assert d["appdata"]["storage_class"] == "STANDARD"  # bad value fell back to default

def test_scenario_uses_keep_policy_for_appdata_retention(tmp_path):
    s = estimate_io.scenario_from_params(
        {"keep_last": "3", "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "6"}, _cfg(tmp_path))
    assert s.appdata.versioning_retention_days == effective_retention_days(
        keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=6)
    assert s.media.versioning_retention_days is None  # media uses the scenario-level value

def test_scenario_overrides_from_params(tmp_path):
    s = estimate_io.scenario_from_params(
        {"appdata_size_gb": "50", "appdata_storage_class": "GLACIER_IR"}, _cfg(tmp_path))
    assert s.appdata.size_gb == 50 and s.appdata.storage_class == "GLACIER_IR"

def test_scenario_rejects_bad_number(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"appdata_size_gb": "abc"}, _cfg(tmp_path))

def test_scenario_rejects_negative(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"media_size_gb": "-5"}, _cfg(tmp_path))

def test_scenario_rejects_unknown_storage_class(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"media_storage_class": "NEBULA"}, _cfg(tmp_path))

def test_scenario_rejects_unknown_retrieval_tier(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params({"retrieval_tier": "Warp"}, _cfg(tmp_path))

def test_scenario_packing_requires_positive_member(tmp_path):
    with pytest.raises(ValueError):
        estimate_io.scenario_from_params(
            {"media_packing": "1", "media_pack_member_gb": "0"}, _cfg(tmp_path))

def test_scenario_defaults_are_valid_and_computable(tmp_path):
    # empty params -> a fully-formed default Scenario the model can price
    from app.estimator.model import estimate
    from app.estimator.prices import load_prices
    s = estimate_io.scenario_from_params({}, _cfg(tmp_path))
    est = estimate(s, load_prices("us-east-1"))
    assert est.monthly_total > 0
