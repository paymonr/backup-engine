import json
from datetime import datetime, timezone
from pathlib import Path

from app.gui import readiness
from app.engine import runs

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 7, 42, tzinfo=UTC)


def _cfg(dirs, *, restore_ok=True):
    restore = Path(dirs["cache"], "restore")
    if restore_ok:
        restore.mkdir(exist_ok=True)
    return {"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
            "RESTORE_ROOT": str(restore), "RESTORE_ROOT_HOST": "/mnt/user/restore",
            "SOURCE_ROOT": str(Path(dirs["cache"], "src")), "SOURCE_ROOT_HOST": "/mnt/user",
            "TZ": "UTC"}


def _jobs(config, jobs):
    Path(config, "jobs.json").write_text(json.dumps({"jobs": jobs}))


def _secrets(config, text):
    Path(config, "secrets.env").write_text(text)


def _probe(cache, obj):
    Path(cache, "state", "_probe.json").write_text(json.dumps(obj))


def _points(cache, job, obj):
    Path(cache, "state", f"{job}.points.json").write_text(json.dumps(obj))


def _ok_run(cache, job, rid, finished, type_="versioned"):
    runs.append_event(cache, job, {"v": 1, "id": rid, "job": job, "kind": "backup",
                                   "event": "start", "started_at": finished, "type": type_})
    runs.append_event(cache, job, {"v": 1, "id": rid, "job": job, "kind": "backup", "event": "end",
                                   "outcome": "ok", "finished_at": finished, "duration_s": 5, "exit_code": 0})


def _snap(sid, t, tbp=56_594_862_080, tfp=533):
    return {"short_id": sid, "id": sid + "0" * 8, "time": t,
            "summary": {"total_bytes_processed": tbp, "total_files_processed": tfp,
                        "files_new": 2, "files_changed": 4, "data_added": 228_589_568}}


def _fake_quote(monkeypatch):
    table = {"appdata": {"Bulk": 4.74, "Standard": 5.00},
             "manga": {"Standard": 168.40, "Bulk": 164.10}}
    def q(config_dir, cache_dir, prices, job_name, *, fraction=1.0, tier="Standard",
          size_gb=None, file_count=None):
        return {"amount": table.get(job_name, {}).get(tier, 1.0), "size_gb": size_gb,
                "file_count": file_count, "tier": tier, "storage_class": None,
                "provenance": "measured", "warmup_hours": None,
                "price_source": "bundled", "price_date": "2026-08-27"}
    monkeypatch.setattr(readiness.estimate_io, "restore_quote", q, raising=False)


# --- WARMUP table ----------------------------------------------------------

def test_warmup_summary_deep_archive():
    w = readiness.warmup_summary("DEEP_ARCHIVE")
    assert w == {"tier": "Standard", "hours_hi": 12, "alt_tier": "Bulk", "alt_hours_hi": 48}


def test_warmup_summary_glacier():
    w = readiness.warmup_summary("GLACIER")
    assert w["tier"] == "Standard" and w["alt_tier"] == "Bulk"
    assert w["hours_hi"] == 5 and w["alt_hours_hi"] == 12


def test_warmup_summary_instant_classes_none():
    assert readiness.warmup_summary("STANDARD") is None
    assert readiness.warmup_summary("STANDARD_IA") is None
    assert readiness.warmup_summary("GLACIER_IR") is None


# --- setup_checks ----------------------------------------------------------

def test_setup_checks_five_rows_with_versioned_job(dirs):
    cfg = _cfg(dirs)
    _jobs(cfg["CONFIG_DIR"], [{"name": "appdata", "type": "versioned", "source": "a",
                              "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": True}])
    _secrets(cfg["CONFIG_DIR"], "RESTIC_PASSWORD=a-strong-passphrase\n")
    _probe(cfg["CACHE_DIR"], {"destination": {"state": "ok", "probed_at": "2026-09-14T07:40:00Z",
                                              "detail": "Write, read, delete — all OK"},
                             "versioning": {"state": "on", "checked_at": "2026-09-14T07:40:00Z"}})
    rows = readiness.setup_checks(cfg, now=NOW)
    codes = {r["code"] for r in rows}
    assert {"destination", "passphrase", "versioning", "jobs_scheduled", "restore_tested"} <= codes
    by = {r["code"]: r for r in rows}
    assert by["passphrase"]["state"] == "ok"
    assert by["destination"]["state"] == "ok"
    assert by["versioning"]["state"] == "ok"
    assert by["jobs_scheduled"]["state"] == "ok"
    assert by["restore_tested"]["state"] in ("warn", "fail")   # never tested


def test_setup_checks_passphrase_shipped_example_is_blocker_and_sorts_first(dirs):
    cfg = _cfg(dirs)
    _jobs(cfg["CONFIG_DIR"], [{"name": "appdata", "type": "versioned", "source": "a",
                              "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": True}])
    _secrets(cfg["CONFIG_DIR"], "RESTIC_PASSWORD=CHANGEME-long-random-passphrase\n")
    rows = readiness.setup_checks(cfg, now=NOW)
    by = {r["code"]: r for r in rows}
    assert by["passphrase"]["state"] == "fail"
    assert by["passphrase"].get("blocker") is True
    # failing rows sort to the top
    assert rows[0]["state"] in ("fail", "warn", "unknown")


def test_setup_checks_passphrase_not_needed_without_versioned_job(dirs):
    cfg = _cfg(dirs)
    _jobs(cfg["CONFIG_DIR"], [{"name": "movies", "type": "archive", "source": "m",
                              "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": True}])
    rows = readiness.setup_checks(cfg, now=NOW)
    by = {r["code"]: r for r in rows}
    assert by["passphrase"]["state"] == "ok"
    assert "Not needed" in by["passphrase"]["sentence"]


def test_setup_checks_sixth_crontab_row_only_when_stale(dirs):
    cfg = _cfg(dirs)
    _jobs(cfg["CONFIG_DIR"], [])
    assert not any(r["code"] == "scheduler" for r in readiness.setup_checks(cfg, now=NOW))
    rows = readiness.setup_checks(cfg, now=NOW, crontab_stale=True)
    sched = [r for r in rows if r["code"] == "scheduler"]
    assert len(sched) == 1 and sched[0]["state"] == "warn"


def test_setup_checks_no_jobs_scheduled_fails(dirs):
    cfg = _cfg(dirs)
    _jobs(cfg["CONFIG_DIR"], [])
    by = {r["code"]: r for r in readiness.setup_checks(cfg, now=NOW)}
    assert by["jobs_scheduled"]["state"] == "fail"


# --- recovery_summary ------------------------------------------------------

def _seed_two_jobs(cfg, monkeypatch):
    config, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    _jobs(config, [
        {"name": "appdata", "type": "versioned", "source": "appdata_backups",
         "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": True},
        {"name": "manga", "type": "archive", "source": "media/manga",
         "schedule": "0 5 * * *", "storage_class": "DEEP_ARCHIVE", "enabled": True, "mirror": False},
    ])
    _secrets(config, "RESTIC_PASSWORD=a-strong-passphrase\nAWS_ACCESS_KEY_ID=AKIAREAL\n")
    _points(cache, "appdata", [_snap("a81f3c2e", "2026-09-15T05:00:01Z"),
                               _snap("4b02fa71", "2026-03-18T05:00:02Z")])
    _ok_run(cache, "appdata", "20260915T050001Z-aa11", "2026-09-15T05:00:10Z", type_="versioned")
    _points(cache, "manga", {"kind": "current-copy", "folders": ["2019"], "as_of": "2026-09-06T06:09:00Z"})
    Path(cache, "usage.json").write_text(json.dumps(
        {"fetched_at": datetime(2026, 9, 14, 7, 40, tzinfo=UTC).timestamp(),
         "data": {"media/manga": {"bytes": 1_957_000_000_000, "count": 232021}}}))
    _ok_run(cache, "manga", "20260906T060900Z-bb22", "2026-09-06T06:09:00Z", type_="archive")
    _fake_quote(monkeypatch)


def test_recovery_summary_passphrase_three_state(dirs, monkeypatch):
    cfg = _cfg(dirs)
    _seed_two_jobs(cfg, monkeypatch)
    s = readiness.recovery_summary(cfg, None, now=NOW)
    assert s["passphrase"]["state"] == "set"
    assert s["aws_key"]["state"] == "set"
    # shipped example flips it
    _secrets(cfg["CONFIG_DIR"], "RESTIC_PASSWORD=CHANGEME-long-random-passphrase\n")
    s = readiness.recovery_summary(cfg, None, now=NOW)
    assert s["passphrase"]["state"] == "shipped_example"


def test_recovery_summary_cold_job_gets_both_tiers_and_totals(dirs, monkeypatch):
    cfg = _cfg(dirs)
    _seed_two_jobs(cfg, monkeypatch)
    s = readiness.recovery_summary(cfg, None, now=NOW)
    jobs = {j["name"]: j for j in s["jobs"]}
    manga = jobs["manga"]
    assert manga["cold"] is True
    assert manga["warmup"] == {"tier": "Standard", "hours_hi": 12, "alt_tier": "Bulk", "alt_hours_hi": 48}
    assert manga["cost_full_restore"]["tier"] == "Standard"
    assert manga["cost_full_restore"]["amount"] == 168.40
    assert manga["cost_full_restore"]["alt_tier"] == "Bulk"
    assert manga["cost_full_restore"]["alt_amount"] == 164.10
    assert manga["kind_note"] == "current copy only — no version history"
    appdata = jobs["appdata"]
    assert appdata["cold"] is False
    assert appdata["warmup"] is None
    assert appdata["cost_full_restore"]["tier"] == "Bulk"
    assert appdata["cost_full_restore"]["amount"] == 4.74
    # totals dollars = sum of the two full-restore amounts (7.7.1 example: 173.14)
    assert round(s["totals"]["dollars"], 2) == 173.14
    assert s["totals"]["hours_hi"] == 12


def test_recovery_summary_size_provenance_propagates(dirs, monkeypatch):
    cfg = _cfg(dirs)
    _seed_two_jobs(cfg, monkeypatch)
    s = readiness.recovery_summary(cfg, None, now=NOW)
    jobs = {j["name"]: j for j in s["jobs"]}
    # both jobs have measured sizes -> cost provenance measured
    assert jobs["appdata"]["size_provenance"] == "measured"
    assert jobs["appdata"]["cost_full_restore"]["provenance"] == "measured"
    assert jobs["manga"]["cost_full_restore"]["provenance"] == "measured"


def test_recovery_summary_assumed_size_propagates_to_cost(dirs, monkeypatch):
    cfg = _cfg(dirs)
    _jobs(cfg["CONFIG_DIR"], [{"name": "appdata", "type": "versioned", "source": "a",
                              "schedule": "0 5 * * *", "storage_class": "STANDARD", "enabled": True}])
    _secrets(cfg["CONFIG_DIR"], "RESTIC_PASSWORD=pw\n")
    # a snapshot WITHOUT a summary -> size not recorded -> assumed
    _points(cfg["CACHE_DIR"], "appdata", [{"short_id": "nosum", "id": "nosum" + "0" * 8,
                                           "time": "2026-09-15T05:00:01Z"}])
    _fake_quote(monkeypatch)
    s = readiness.recovery_summary(cfg, None, now=NOW)
    job = s["jobs"][0]
    assert job["size_provenance"] == "assumed"
    assert job["cost_full_restore"]["provenance"] == "assumed"


def test_recovery_summary_restore_mount_state(dirs, monkeypatch):
    cfg_ok = _cfg(dirs, restore_ok=True)
    _seed_two_jobs(cfg_ok, monkeypatch)
    s = readiness.recovery_summary(cfg_ok, None, now=NOW)
    assert s["restore_mount"]["state"] == "ok"
    assert s["restore_mount"]["path_host"] == "/mnt/user/restore"
    # missing mount
    cfg_bad = dict(cfg_ok, RESTORE_ROOT=str(Path(dirs["cache"], "nope")))
    s2 = readiness.recovery_summary(cfg_bad, None, now=NOW)
    assert s2["restore_mount"]["state"] == "missing"


def test_recovery_summary_tested_and_test_pending(dirs, monkeypatch):
    cfg = _cfg(dirs)
    _seed_two_jobs(cfg, monkeypatch)
    Path(cfg["CACHE_DIR"], "state", "appdata.tested.json").write_text(
        json.dumps({"at": "2026-09-15T07:52:00Z", "path": "/mnt/user/restore/appdata/test/x", "bytes": 10}))
    Path(cfg["CACHE_DIR"], "state", "manga.test-thaw.json").write_text(
        json.dumps({"key": "media/manga/x.cbz", "requested_at": "2026-09-15T04:02:11Z",
                    "expected_ready_by": "2026-09-17T04:02:11Z", "run_id": "20260915T040211Z-1c8e"}))
    s = readiness.recovery_summary(cfg, None, now=NOW)
    jobs = {j["name"]: j for j in s["jobs"]}
    assert jobs["appdata"]["tested"]["at"] == "2026-09-15T07:52:00Z"
    assert jobs["manga"]["test_pending"]["expected_ready_by"] == "2026-09-17T04:02:11Z"


def test_recovery_summary_destination_never_probed(dirs, monkeypatch):
    cfg = _cfg(dirs)
    _seed_two_jobs(cfg, monkeypatch)
    s = readiness.recovery_summary(cfg, None, now=NOW)
    assert s["destination"]["state"] == "never"
    assert s["versioning"]["state"] in ("unknown", "never", "off", "on")
