# tests/estimator/test_cli.py
import json
import pytest
from app.estimator.cli import build_scenario, estimate_to_dict, main, _read_env_file
from app.estimator.model import estimate
from app.estimator.cli import _parser

def _args(argv):
    return _parser().parse_args(argv)

def test_read_env_file_ignores_comments_and_quotes(tmp_path):
    f = tmp_path / "backup.env"
    f.write_text('# comment\nAWS_REGION=us-west-2\nMEDIA_STORAGE_CLASS="GLACIER"\n\n')
    env = _read_env_file(f)
    assert env["AWS_REGION"] == "us-west-2"
    assert env["MEDIA_STORAGE_CLASS"] == "GLACIER"

def test_build_scenario_reads_region_and_classes_from_env():
    env = {"AWS_REGION": "us-west-2", "APPDATA_STORAGE_CLASS": "STANDARD_IA",
           "MEDIA_STORAGE_CLASS": "GLACIER"}
    s = build_scenario(_args([]), env)
    assert s.region == "us-west-2"
    assert s.appdata.storage_class == "STANDARD_IA"
    assert s.media.storage_class == "GLACIER"

def test_flags_override_env():
    env = {"MEDIA_STORAGE_CLASS": "GLACIER"}
    s = build_scenario(_args(["--media-storage-class", "DEEP_ARCHIVE", "--media-size-gb", "500"]), env)
    assert s.media.storage_class == "DEEP_ARCHIVE"
    assert s.media.size_gb == 500

def test_json_output_matches_direct_estimate(prices, capsys, monkeypatch):
    # force the CLI to use the fixed test table
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    rc = main(["--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    direct = estimate_to_dict(estimate(build_scenario(_args([]), {}), prices))
    assert out["monthly_total"] == direct["monthly_total"]
    assert out["price_date"] == "2099-01-01"

def test_table_output_shows_price_date(prices, capsys, monkeypatch):
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    assert main([]) == 0
    assert "2099-01-01" in capsys.readouterr().out

def test_unknown_storage_class_flag_errors(prices, monkeypatch, capsys):
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    rc = main(["--media-storage-class", "NEBULA"])
    assert rc != 0
