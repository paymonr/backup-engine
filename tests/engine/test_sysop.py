import json
from pathlib import Path

import pytest

from app.engine import sysop
from app.engine import runs


def _setup(tmp_path, monkeypatch, *, jobs=None, backup_env=None, secrets=None):
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True)
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "jobs.json").write_text(json.dumps({"jobs": jobs if jobs is not None else [
        {"name": "appdata", "type": "versioned", "source": "appdata_backups",
         "schedule": "0 5 * * *", "storage_class": "STANDARD"},
        {"name": "movies", "type": "archive", "source": "media/movies",
         "schedule": "0 5 * * *", "storage_class": "DEEP_ARCHIVE"},
    ]}))
    be = backup_env if backup_env is not None else "S3_BUCKET=my-bucket\nAWS_REGION=us-east-1\nCOST_EXPLORER_TAG=proj\n"
    (cfg / "backup.env").write_text(be)
    if secrets is not None:
        (cfg / "secrets.env").write_text(secrets)
    monkeypatch.setenv("CACHE_DIR", str(cache))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.delenv("BE_RUN_ID", raising=False)
    monkeypatch.delenv("BE_TRIGGER", raising=False)
    return str(cache), str(cfg)


def _system_records(cache):
    p = Path(cache, "state", "_system.runs.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# --- usage-refresh ---------------------------------------------------------

def test_usage_refresh_calls_collect_usage_and_saves_cache(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch)
    seen = {}
    def fake_collect(bucket, archive_jobs, has_versioned, *, rclone_config=None, runner=None):
        seen.update(bucket=bucket, archive_jobs=list(archive_jobs), has_versioned=has_versioned)
        return {"media/movies": {"bytes": 5, "count": 2}, "appdata": {"bytes": 9, "count": 3}}
    monkeypatch.setattr(sysop.usage, "collect_usage", fake_collect)
    rc = sysop.run("usage-refresh")
    assert rc == 0
    assert seen["bucket"] == "my-bucket"
    assert seen["archive_jobs"] == ["movies"]   # archive + versioned-files prefixes
    assert seen["has_versioned"] is True
    cached = json.loads(Path(cache, "usage.json").read_text())
    assert cached["data"]["media/movies"]["bytes"] == 5
    recs = _system_records(cache)
    assert {r["event"] for r in recs} == {"start", "end"}
    assert all(r["job"] is None and r["kind"] == "usage-refresh" for r in recs)
    end = [r for r in recs if r["event"] == "end"][0]
    assert end["outcome"] == "ok"


def test_usage_refresh_failure_records_failed_and_still_exits_0(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("rclone exploded")
    monkeypatch.setattr(sysop.usage, "collect_usage", boom)
    rc = sysop.run("usage-refresh")
    assert rc == 0
    end = [r for r in _system_records(cache) if r["event"] == "end"][0]
    assert end["outcome"] == "failed"
    assert "rclone exploded" in end["error"]


# --- billing-check ---------------------------------------------------------

def test_billing_check_writes_billing_json(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch,
                           secrets="COST_EXPLORER_ACCESS_KEY_ID=k\nCOST_EXPLORER_SECRET_ACCESS_KEY=s\n")
    monkeypatch.setattr(sysop.billing, "monthly_costs", lambda creds, **k: [{"month": "2026-09", "amount": 1.23}])
    monkeypatch.setattr(sysop.billing, "forecast", lambda creds, **k: {"month": "2026-10", "amount": 2.0})
    rc = sysop.run("billing-check")
    assert rc == 0
    bill = json.loads(Path(cache, "billing.json").read_text())
    assert bill["months"] == [{"month": "2026-09", "amount": 1.23}]
    assert bill["forecast"] == {"month": "2026-10", "amount": 2.0}
    assert bill["tag"] == "proj"
    assert bill["error"] is None
    assert "fetched_at" in bill
    end = [r for r in _system_records(cache) if r["event"] == "end"][0]
    assert end["outcome"] == "ok"


def test_billing_check_billing_error_lands_in_error_member_not_a_render(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch,
                           secrets="COST_EXPLORER_ACCESS_KEY_ID=k\nCOST_EXPLORER_SECRET_ACCESS_KEY=s\n")
    def boom(creds, **k):
        raise sysop.billing.BillingError("access denied on cost explorer")
    monkeypatch.setattr(sysop.billing, "monthly_costs", boom)
    rc = sysop.run("billing-check")
    assert rc == 0
    bill = json.loads(Path(cache, "billing.json").read_text())
    assert bill["error"] == "access denied on cost explorer"
    assert bill["months"] is None
    end = [r for r in _system_records(cache) if r["event"] == "end"][0]
    assert end["outcome"] == "ok"   # the op recorded the error successfully


# --- probe -----------------------------------------------------------------

class _CP:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc; self.stdout = out; self.stderr = err


def test_probe_writes_probe_json_ok(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch, secrets="AWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n")
    monkeypatch.setattr(sysop.provision, "validate_runtime_key", lambda *a, **k: None)
    monkeypatch.setattr(sysop.provision, "_run_aws",
                        lambda args, **k: _CP(0, json.dumps({"Status": "Enabled"})))
    rc = sysop.run("probe")
    assert rc == 0
    probe = json.loads(Path(cache, "state", "_probe.json").read_text())
    assert probe["destination"]["state"] == "ok"
    assert probe["versioning"]["state"] == "on"
    end = [r for r in _system_records(cache) if r["event"] == "end"][0]
    assert end["outcome"] == "ok"


def test_probe_destination_failure_records_failed_destination_but_run_ok(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch, secrets="AWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n")
    def bad(*a, **k):
        raise sysop.provision.ValidationError("put", "AccessDenied: s3:PutObject")
    monkeypatch.setattr(sysop.provision, "validate_runtime_key", bad)
    monkeypatch.setattr(sysop.provision, "_run_aws", lambda args, **k: _CP(0, json.dumps({"Status": "Suspended"})))
    rc = sysop.run("probe")
    probe = json.loads(Path(cache, "state", "_probe.json").read_text())
    assert probe["destination"]["state"] == "failed"
    assert probe["versioning"]["state"] == "off"
    end = [r for r in _system_records(cache) if r["event"] == "end"][0]
    assert end["outcome"] == "ok"


def test_probe_versioning_access_denied_is_unknown(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch, secrets="AWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n")
    monkeypatch.setattr(sysop.provision, "validate_runtime_key", lambda *a, **k: None)
    monkeypatch.setattr(sysop.provision, "_run_aws",
                        lambda args, **k: _CP(255, "", "An error occurred (AccessDenied) ..."))
    sysop.run("probe")
    probe = json.loads(Path(cache, "state", "_probe.json").read_text())
    assert probe["versioning"]["state"] == "unknown"


# --- _runtime_key: secrets.env is the source of truth, not the (stale) env ----

def test_runtime_key_prefers_secrets_env_over_stale_process_env(tmp_path, monkeypatch):
    # The container exported the OLD key into the process env at startup; after a
    # re-provision, secrets.env holds the NEW key and must win.
    _cache, cfgdir = _setup(tmp_path, monkeypatch,
                            secrets="AWS_ACCESS_KEY_ID=NEWKEY\nAWS_SECRET_ACCESS_KEY=NEWSEC\n")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "OLDKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "OLDSEC")
    assert sysop._runtime_key(cfgdir) == ("NEWKEY", "NEWSEC")


def test_runtime_key_falls_back_to_env_when_secrets_absent(tmp_path, monkeypatch):
    _cache, cfgdir = _setup(tmp_path, monkeypatch)  # no secrets.env written
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENVSEC")
    assert sysop._runtime_key(cfgdir) == ("ENVKEY", "ENVSEC")


def test_runtime_key_falls_back_to_env_when_secrets_has_no_aws_key(tmp_path, monkeypatch):
    # secrets.env exists but carries no runtime AWS pair (e.g. only a CE credential).
    _cache, cfgdir = _setup(tmp_path, monkeypatch,
                            secrets="COST_EXPLORER_ACCESS_KEY_ID=c\nCOST_EXPLORER_SECRET_ACCESS_KEY=d\n")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENVSEC")
    assert sysop._runtime_key(cfgdir) == ("ENVKEY", "ENVSEC")


# --- probe rides out a not-yet-propagated key --------------------------------

def test_probe_retries_on_invalid_access_key_then_succeeds(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch, secrets="AWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n")
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sysop.provision.ValidationError(
                "list", "An error occurred (InvalidAccessKeyId) ... key does not exist")
        return None

    monkeypatch.setattr(sysop.provision, "validate_runtime_key", flaky)
    monkeypatch.setattr(sysop.provision, "_run_aws",
                        lambda args, **k: _CP(0, json.dumps({"Status": "Enabled"})))
    slept = []
    monkeypatch.setattr(sysop.time, "sleep", lambda s: slept.append(s))
    rc = sysop.run("probe")
    assert rc == 0
    probe = json.loads(Path(cache, "state", "_probe.json").read_text())
    assert probe["destination"]["state"] == "ok"
    assert calls["n"] == 3            # two transient failures, then success
    assert len(slept) == 2            # one backoff before each retry


def test_probe_gives_up_after_propagation_bound(tmp_path, monkeypatch):
    # Realistic creds so probe's secret-scrub doesn't mangle the recorded detail.
    cache, cfgdir = _setup(tmp_path, monkeypatch,
                           secrets="AWS_ACCESS_KEY_ID=AKIAEXAMPLE\nAWS_SECRET_ACCESS_KEY=examplesecret\n")
    calls = {"n": 0}

    def always_bad(*a, **k):
        calls["n"] += 1
        raise sysop.provision.ValidationError(
            "list", "An error occurred (InvalidAccessKeyId) ... key does not exist")

    monkeypatch.setattr(sysop.provision, "validate_runtime_key", always_bad)
    monkeypatch.setattr(sysop.provision, "_run_aws", lambda args, **k: _CP(255, "", "denied"))
    slept = []
    monkeypatch.setattr(sysop.time, "sleep", lambda s: slept.append(s))
    rc = sysop.run("probe")
    assert rc == 0
    probe = json.loads(Path(cache, "state", "_probe.json").read_text())
    assert probe["destination"]["state"] == "failed"
    assert "InvalidAccessKeyId" in probe["destination"]["detail"]
    # bounded: one initial attempt + the fixed backoff schedule, ~<=20s total.
    assert calls["n"] == len(sysop._PROBE_RETRY_DELAYS) + 1
    assert len(slept) == len(sysop._PROBE_RETRY_DELAYS)
    assert sum(slept) <= 20


def test_probe_does_not_retry_a_non_transient_failure(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch, secrets="AWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n")
    calls = {"n": 0}

    def denied(*a, **k):
        calls["n"] += 1
        raise sysop.provision.ValidationError("put", "AccessDenied: s3:PutObject")

    monkeypatch.setattr(sysop.provision, "validate_runtime_key", denied)
    monkeypatch.setattr(sysop.provision, "_run_aws", lambda args, **k: _CP(255, "", "denied"))
    slept = []
    monkeypatch.setattr(sysop.time, "sleep", lambda s: slept.append(s))
    sysop.run("probe")
    probe = json.loads(Path(cache, "state", "_probe.json").read_text())
    assert probe["destination"]["state"] == "failed"
    assert calls["n"] == 1            # no retry on a non-transient error
    assert slept == []


# --- run id + records ------------------------------------------------------

def test_honours_preassigned_be_run_id(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("BE_RUN_ID", "20260916T050001Z-abcd")
    monkeypatch.setattr(sysop.usage, "collect_usage", lambda *a, **k: {})
    sysop.run("usage-refresh")
    recs = _system_records(cache)
    assert all(r["id"] == "20260916T050001Z-abcd" for r in recs)
    # the log path the record points at exists and is named by the id
    log = [r for r in recs if r["event"] == "start"][0]["log"]
    assert log == "logs/runs/_system/20260916T050001Z-abcd.log"
    assert Path(cache, log).is_file()


def test_system_record_not_marked_running_after_completion(tmp_path, monkeypatch):
    cache, cfgdir = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sysop.usage, "collect_usage", lambda *a, **k: {})
    sysop.run("usage-refresh")
    # reader folds the two lines into one completed record (not "running"),
    # and reconcile leaves it alone (the op wrote its own end).
    recs = runs.read_runs(cache, None).records
    assert len(recs) == 1
    assert recs[0].outcome == "ok"
    assert recs[0].job is None


# --- the cached billing.json read path (7.7.3, estimate_io) ----------------

def test_billing_cache_round_trips_through_read_billing_cache(tmp_path, monkeypatch):
    from app.gui import estimate_io
    cache, cfgdir = _setup(tmp_path, monkeypatch,
                           secrets="COST_EXPLORER_ACCESS_KEY_ID=k\nCOST_EXPLORER_SECRET_ACCESS_KEY=s\n")
    monkeypatch.setattr(sysop.billing, "monthly_costs", lambda creds, **k: [{"month": "2026-09", "amount": 1.23}])
    monkeypatch.setattr(sysop.billing, "forecast", lambda creds, **k: None)
    sysop.run("billing-check")
    view = estimate_io.read_billing_cache(cache)
    assert view["connected"] is True
    assert view["months"] == [{"month": "2026-09", "amount": 1.23}]
    assert view["error"] is None
    assert view["stale"] is False


def test_read_billing_cache_absent_is_not_connected(tmp_path):
    from app.gui import estimate_io
    assert estimate_io.read_billing_cache(str(tmp_path)) == {"connected": False}
