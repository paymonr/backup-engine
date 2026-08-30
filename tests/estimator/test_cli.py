# tests/estimator/test_cli.py — the N-job CLI (reads config/jobs.json).
import json
import pathlib
from app.estimator.cli import _parser, build_scenario, estimate_to_dict, main, render_table
from app.estimator.model import estimate

def _args(argv):
    return _parser().parse_args(argv)

def _seed(config_dir, jobs):
    pathlib.Path(config_dir, "jobs.json").write_text(json.dumps({"jobs": jobs}))

VJOB = {"name": "appdata", "type": "versioned", "source": "appdata",
        "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
        "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}
AJOB = {"name": "movies", "type": "archive", "source": "movies",
        "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
        "mirror": False}

def _cfg(tmp_path, jobs=None, env=None):
    cfg = tmp_path / "config"
    cfg.mkdir()
    if jobs is not None:
        _seed(str(cfg), jobs)
    if env:
        (cfg / "backup.env").write_text(env)
    return str(cfg)

def test_build_scenario_reads_jobs_from_jobs_json(tmp_path):
    cfg = _cfg(tmp_path, [VJOB, AJOB])
    s = build_scenario(_args(["--config-dir", cfg]))
    by_name = {j.name: j for j in s.jobs}
    assert set(by_name) == {"appdata", "movies"}
    assert by_name["appdata"].storage_class == "STANDARD"
    assert by_name["movies"].storage_class == "DEEP_ARCHIVE"
    assert by_name["movies"].engine == "archive"

def test_build_scenario_region_from_env_and_flag_override(tmp_path):
    cfg = _cfg(tmp_path, [VJOB], env="AWS_REGION=us-west-2\n")
    assert build_scenario(_args(["--config-dir", cfg])).region == "us-west-2"
    assert build_scenario(_args(["--config-dir", cfg, "--region", "eu-west-1"])).region == "eu-west-1"

def test_no_jobs_file_gives_empty_scenario(tmp_path):
    cfg = _cfg(tmp_path)  # no jobs.json
    s = build_scenario(_args(["--config-dir", cfg]))
    assert s.jobs == ()

def test_json_output_matches_direct_estimate(prices, capsys, monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, [VJOB, AJOB])
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    rc = main(["--config-dir", cfg, "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    direct = estimate_to_dict(estimate(build_scenario(_args(["--config-dir", cfg])), prices))
    assert out["monthly_total"] == direct["monthly_total"]
    assert set(out["jobs"]) == {"appdata", "movies"}
    assert out["price_date"] == "2099-01-01"

def test_table_output_shows_price_date_and_job_names(prices, capsys, monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, [VJOB, AJOB])
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    assert main(["--config-dir", cfg]) == 0
    out = capsys.readouterr().out
    assert "2099-01-01" in out
    assert "[appdata]" in out and "[movies]" in out

def test_unknown_storage_class_in_jobs_errors(prices, monkeypatch, tmp_path, capsys):
    bad = {**VJOB, "storage_class": "NEBULA"}
    cfg = _cfg(tmp_path, [bad])
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    assert main(["--config-dir", cfg]) != 0

def test_unknown_retrieval_tier_errors_even_for_all_warm_jobs(prices, monkeypatch, tmp_path, capsys):
    # A warm-only scenario still must reject a bad --retrieval-tier (checked
    # unconditionally, not only when a cold job triggers the retrieval path).
    cfg = _cfg(tmp_path, [VJOB])  # STANDARD (warm)
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    assert main(["--config-dir", cfg, "--retrieval-tier", "Warp"]) != 0

def test_explicit_zero_versioning_retention_days_is_honored(tmp_path):
    cfg = _cfg(tmp_path, [VJOB])
    s = build_scenario(_args(["--config-dir", cfg, "--versioning-retention-days", "0"]))
    assert s.versioning_retention_days == 0

def test_render_table_handles_no_jobs():
    from app.estimator.model import Scenario
    from app.estimator.prices import load_prices
    est = estimate(Scenario(), load_prices("us-east-1"))
    assert "no jobs configured" in render_table(est)
