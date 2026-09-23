# tests/engine/test_lifecycle_check.py — the tamper check (spec §2).
import json

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import BASE, CONSOLE, FakeS3, cfg  # noqa: F401 (fixture)


def _applied(cfg, fake):
    if not fake.rules.get(BASE):
        lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)          # a bucket the app created: every folder applies
    lc.sync(cfg, BASE, run=fake)
    return list(fake.rules[BASE])


def test_matching_rules_are_ok(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["state"] == "ok"


def test_changed_app_rule_is_restored_and_alarmed(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    for r in fake.rules[BASE]:
        if r["ID"] == "backup-engine:media/manga/":
            r["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}     # someone shortened it
    assert lc.check(cfg, BASE, run=fake) == "restored"
    manga = next(r for r in fake.rules[BASE] if r["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    st = lc.load_status(cfg["CACHE_DIR"])[BASE]
    assert st["state"] == "restored" and st["alarm"]["kind"] == "restored"


def test_deleted_configuration_is_tampering(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE] = []
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert any(r["ID"] == "backup-engine:housekeeping" for r in fake.rules[BASE])


def test_console_rules_are_never_put_back_or_removed(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(CONSOLE)                 # destructive (Expiration Days) -> alarmed (I1)
    assert lc.check(cfg, BASE, run=fake) == "console_rule"
    assert CONSOLE in fake.rules[BASE] and len(fake.puts()) == 1   # only the first apply wrote
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["state"] == "ok"  # the app's own rules are fine


def test_restore_failure_is_not_restored(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE] = []
    fake.deny_put = True
    assert lc.check(cfg, BASE, run=fake) == "not_restored"
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["kind"] == "not_restored"


def test_alarm_survives_a_clean_check_until_acknowledged(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE] = []
    lc.check(cfg, BASE, run=fake)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert "alarm" in lc.load_status(cfg["CACHE_DIR"])[BASE]
    lc.acknowledge(cfg["CACHE_DIR"])
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]


def test_first_check_without_applied_state_syncs(cfg):
    fake = FakeS3()
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert lc.load_applied(cfg["CACHE_DIR"], BASE) is not None


def test_not_managed_does_nothing(cfg, tmp_path):
    from pathlib import Path
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text().replace("PERMISSIONS_VERSION=4", ""))
    fake = FakeS3()
    assert lc.check(cfg, BASE, run=fake) == "not_managed"
    assert fake.calls == []


def test_cli_check_always_exits_zero(cfg, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", cfg["CONFIG_DIR"])
    monkeypatch.setenv("CACHE_DIR", cfg["CACHE_DIR"])
    monkeypatch.setattr(lc, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lc.main(["check", "--bucket", BASE]) == 0
    assert "S3 rules check" in capsys.readouterr().out


# --- a check never raises (it runs before every backup and behind Check now) -----------

def test_a_malformed_plain_copy_job_keeps_everything_and_the_check_still_runs(cfg):
    from pathlib import Path
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"][0]["retention"] = {"type": "count", "count": "x"}          # manga
    jobs_p.write_text(json.dumps(data))
    fake = FakeS3()
    assert lc.check(cfg, BASE, run=fake) == "ok"
    ids = {r["ID"] for r in fake.rules[BASE]}
    assert "backup-engine:media/manga/" not in ids and "backup-engine:housekeeping" in ids
    assert "manga" in lc.load_status(cfg["CACHE_DIR"])[BASE]["detail"]


@pytest.mark.parametrize("with_applied,target,exc", [
    (False, "read_rules", RuntimeError("boom")),
    (False, "save_applied", OSError(28, "No space left on device")),
    (False, "desired_rules", ValueError("bad")),
    (True, "read_rules", RuntimeError("boom")),
    (True, "write_rules", OSError(28, "No space left on device")),
    (True, "_change_lines", KeyError("x")),
])
def test_check_never_raises(cfg, monkeypatch, with_applied, target, exc):
    fake = FakeS3()
    if with_applied:
        _applied(cfg, fake)
        fake.rules[BASE] = []                                   # force the restore path too

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(lc, target, boom)
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["state"] == "error"


def test_check_never_raises_even_when_the_status_file_cannot_be_written(cfg, monkeypatch):
    def boom(*a, **k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(lc, "set_status", boom)
    monkeypatch.setattr(lc, "read_rules", boom)
    assert lc.check(cfg, BASE, run=FakeS3()) == "error"


# --- I2/I3: the check applies what the jobs want; drift is judged against what was applied --

from tests.engine.test_lifecycle_sync import _live, _set_manga  # noqa: E402


def test_a_failed_job_save_sync_is_retried_by_the_next_check(cfg):
    fake = FakeS3()
    _applied(cfg, fake)                                          # manga: 180 days
    _set_manga(cfg, {"type": "days", "days": 365})
    fake.deny_put = True
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)                             # the job save's sync fails
    fake.deny_put = False
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 365}
    st = lc.load_status(cfg["CACHE_DIR"])[BASE]
    assert st["state"] == "ok" and "alarm" not in st
    assert lc.app_rules_differ(lc.load_applied(cfg["CACHE_DIR"], BASE), fake.rules[BASE]) is False


def test_tampering_is_restored_to_what_the_jobs_want_now(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    _set_manga(cfg, {"type": "days", "days": 365})               # saved while the sync failed
    _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 365}
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["kind"] == "restored"


def test_check_records_its_trigger(cfg):
    from tests.engine.test_lifecycle_sync import _events
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE] = []
    assert lc.check(cfg, BASE, run=fake, trigger="manual") == "restored"
    starts = [e for e in _events(cfg) if e["kind"] == "s3-rules" and e["event"] == "start"]
    assert starts[-1]["trigger"] == "manual"
    fake.rules[BASE] = []
    lc.check(cfg, BASE, run=fake)
    starts = [e for e in _events(cfg) if e["kind"] == "s3-rules" and e["event"] == "start"]
    assert starts[-1]["trigger"] == "scheduled"


def test_check_on_a_dedicated_bucket(cfg):
    from pathlib import Path
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"].append({"name": "photos", "type": "archive", "source": "media/photos",
                         "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
                         "retention": {"type": "count", "count": 10},
                         "dedicated": True, "bucket": f"{BASE}-photos", "bucket_versioned": True})
    jobs_p.write_text(json.dumps(data))
    ded = f"{BASE}-photos"
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE); lc.seed_new_bucket(cfg["CACHE_DIR"], ded)
    fake = FakeS3()
    lc.sync_all(cfg, run=fake)
    base_before = json.loads(json.dumps(fake.rules[BASE]))
    assert lc.check(cfg, ded, run=fake) == "ok"
    _live(fake, "backup-engine:bucket", ded)["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}
    assert lc.check(cfg, ded, run=fake) == "restored"
    assert _live(fake, "backup-engine:bucket", ded)["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": 1, "NewerNoncurrentVersions": 10}
    st = lc.load_status(cfg["CACHE_DIR"])
    assert st[ded]["alarm"]["kind"] == "restored" and "alarm" not in st[BASE]
    assert fake.rules[BASE] == base_before                       # the base bucket untouched
    assert {r["ID"] for r in fake.rules[ded]} == {"backup-engine:bucket", "backup-engine:housekeeping"}


# --- I1: a console rule that could delete backups is alarmed (never touched) ---------------

HOSTILE = {"ID": "x", "Filter": {"Prefix": ""}, "Expiration": {"Days": 1},
           "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}
HARMLESS = {"ID": "abort-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}


def _s3_rule_logs(cfg):
    from pathlib import Path
    from tests.engine.test_lifecycle_sync import _events
    ends = [e for e in _events(cfg) if e["kind"] == "s3-rules" and e["event"] == "start"]
    return [Path(cfg["CACHE_DIR"], e["log"]).read_text() for e in ends]


def test_a_new_destructive_console_rule_alarms_once_and_is_left_alone(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(json.loads(json.dumps(HOSTILE)))
    assert lc.check(cfg, BASE, run=fake) == "console_rule"
    assert HOSTILE in fake.rules[BASE] and len(fake.puts()) == 1       # never modified or deleted
    st = lc.load_status(cfg["CACHE_DIR"])[BASE]
    assert st["alarm"]["kind"] == "console_rule" and st["alarm"]["rules"] == ["x"]
    log = _s3_rule_logs(cfg)[-1]
    assert "x" in log and "expires current files 1 day after" in log
    assert "removes old versions 1 day after being replaced" in log
    # alarms once: the fingerprint moved on, the alarm stays until acknowledged
    n = len(_s3_rule_logs(cfg))
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert len(_s3_rule_logs(cfg)) == n
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["kind"] == "console_rule"
    lc.acknowledge(cfg["CACHE_DIR"])
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]


def test_a_job_save_cannot_absorb_a_hostile_console_rule(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(json.loads(json.dumps(HOSTILE)))
    _set_manga(cfg, {"type": "days", "days": 365})
    res = lc.sync(cfg, BASE, run=fake)                                 # the job save
    assert res.state == "console_rule"
    assert HOSTILE in fake.rules[BASE]                                 # kept byte-for-byte on the write
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["kind"] == "console_rule"
    assert lc.check(cfg, BASE, run=fake) == "ok"                       # still alarmed exactly once
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["rules"] == ["x"]


def test_harmless_console_rule_changes_update_the_fingerprint_silently(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(dict(HARMLESS))
    assert lc.check(cfg, BASE, run=fake) == "ok"
    disabled = dict(HOSTILE, ID="later", Status="Disabled")
    fake.rules[BASE].append(disabled)
    assert lc.check(cfg, BASE, run=fake) == "ok"                       # a disabled rule removes nothing
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]
    disabled["Status"] = "Enabled"                                     # ...until it is switched on
    assert lc.check(cfg, BASE, run=fake) == "console_rule"
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["rules"] == ["later"]


def test_a_changed_console_rule_that_becomes_destructive_alarms(cfg):
    fake = FakeS3()
    fake.rules[BASE] = [dict(HARMLESS)]
    _applied(cfg, fake)
    rule = next(r for r in fake.rules[BASE] if r["ID"] == "abort-uploads")
    rule["Transitions"] = [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}]
    assert lc.check(cfg, BASE, run=fake) == "console_rule"
    assert "moves current files to DEEP_ARCHIVE" in _s3_rule_logs(cfg)[-1]


def test_an_existing_console_rule_is_not_alarmed_on_first_apply(cfg):
    fake = FakeS3({BASE: [json.loads(json.dumps(HOSTILE))]})
    assert lc.sync(cfg, BASE, run=fake).state == "ok"
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]


def test_older_applied_state_without_a_fingerprint_records_it_silently(cfg):
    from pathlib import Path
    fake = FakeS3()
    _applied(cfg, fake)
    p = Path(cfg["CACHE_DIR"], "state", "lifecycle", f"{BASE}.applied.json")
    data = json.loads(p.read_text())
    data.pop("console", None)
    p.write_text(json.dumps(data))
    fake.rules[BASE].append(json.loads(json.dumps(HOSTILE)))
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]
    assert lc.load_console_fingerprint(cfg["CACHE_DIR"], BASE) is not None
    next(r for r in fake.rules[BASE] if r["ID"] == "x")["Expiration"] = {"Days": 2}
    assert lc.check(cfg, BASE, run=fake) == "console_rule"


def test_a_console_alarm_is_kept_when_a_tamper_alarm_follows(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(json.loads(json.dumps(HOSTILE)))
    lc.check(cfg, BASE, run=fake)
    _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}
    assert lc.check(cfg, BASE, run=fake) == "restored"
    alarm = lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]
    assert alarm["kind"] == "console_rule" and alarm["rules"] == ["x"]
    fake.rules[BASE] = [r for r in fake.rules[BASE] if r["ID"] == "x"]
    fake.deny_put = True
    assert lc.check(cfg, BASE, run=fake) == "not_restored"
    alarm = lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]
    assert alarm["kind"] == "not_restored" and alarm["rules"] == ["x"]    # the rule ID is still named


# --- #5: state files are written atomically and read-modify-written under locks ------------

import fcntl  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402


def _is_locked(path) -> bool:
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


@pytest.mark.parametrize("write", [
    lambda cache: lc.set_status(cache, BASE, "error", "later"),
    lambda cache: lc.save_applied(cache, BASE, []),
    lambda cache: lc.acknowledge(cache),
])
def test_state_files_are_replaced_atomically(cfg, monkeypatch, write):
    cache = cfg["CACHE_DIR"]
    lc.set_status(cache, BASE, "ok", alarm={"kind": "restored", "at": "t", "lines": []})
    lc.save_applied(cache, BASE, [{"ID": "backup-engine:housekeeping"}])
    before = {p.name: p.read_text() for p in Path(cache, "state").rglob("*.json")}

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(lc.os, "replace", boom)
    with pytest.raises(OSError):
        write(cache)
    after = {p.name: p.read_text() for p in Path(cache, "state").rglob("*.json")}
    assert after == before                                   # never half-written
    assert not [p for p in Path(cache, "state").rglob("*.tmp")]


def test_concurrent_status_updates_on_different_buckets_keep_both_alarms(cfg, monkeypatch):
    cache = cfg["CACHE_DIR"]
    real = lc.load_status

    def slow_load(c):
        data = real(c)
        time.sleep(0.2)                                      # widen the read-modify-write window
        return data
    monkeypatch.setattr(lc, "load_status", slow_load)
    ts = [threading.Thread(target=lc.set_status, args=(cache, b, "restored"),
                           kwargs={"alarm": {"kind": "restored", "at": "t", "lines": []}})
          for b in (BASE, f"{BASE}-photos")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    st = real(cache)
    assert st[BASE]["alarm"] and st[f"{BASE}-photos"]["alarm"]


def test_the_whole_pass_holds_the_bucket_lock(cfg, monkeypatch):
    lock = Path(cfg["CACHE_DIR"], "state", "lifecycle", f"{BASE}.lock")
    seen = []
    real_load = lc.jobs_io.load

    def spy_load(config_dir):
        seen.append(_is_locked(lock))
        return real_load(config_dir)
    monkeypatch.setattr(lc.jobs_io, "load", spy_load)
    fake = FakeS3()
    real_run = fake.__call__

    def spy_run(args, **kw):
        seen.append(_is_locked(lock))
        return real_run(args, **kw)
    lc.sync(cfg, BASE, run=spy_run)
    lc.check(cfg, BASE, run=spy_run)
    assert seen and all(seen)
    assert not _is_locked(lock)                              # released afterwards


def test_a_check_waits_for_a_job_save_sync_on_the_same_bucket(cfg):
    fake = FakeS3()
    _applied(cfg, fake)                                      # manga 180 applied
    entered, release, order = threading.Event(), threading.Event(), []

    def slow(args, **kw):
        if args[:2] == ["s3api", "put-bucket-lifecycle-configuration"]:
            order.append("put")
            r = fake(args, **kw)                             # the new rules are live...
            entered.set()
            release.wait(5)                                  # ...but not yet recorded as applied
            order.append("put-done")
            return r
        if args[:2] == ["s3api", "get-bucket-lifecycle-configuration"]:
            order.append("get")
        return fake(args, **kw)

    _set_manga(cfg, {"type": "days", "days": 365})
    results = {}
    s = threading.Thread(target=lambda: results.setdefault("sync", lc.sync(cfg, BASE, run=slow)))
    s.start()
    assert entered.wait(5)
    c = threading.Thread(target=lambda: results.setdefault("check", lc.check(cfg, BASE, run=slow)))
    c.start()
    time.sleep(0.3)
    assert order == ["get", "put"]                           # the check has not read S3 yet
    release.set()
    s.join(5); c.join(5)
    assert order == ["get", "put", "put-done", "get"]
    assert results["check"] == "ok" and "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]


# --- #11: the backup log gets words, not internal states; #10: short aws timeouts ----------

@pytest.mark.parametrize("state,words", [
    ("not_managed", "skipped (needs the AWS permissions update)"),
    ("ok", "in place"),
    ("restored", "changed outside backup-engine — restored (see Setup)"),
    ("not_restored", "changed outside backup-engine — NOT restored (see Setup)"),
    ("console_rule", "a new S3 rule could delete or move backups (see Setup)"),
    ("error", "couldn't be checked (see Setup)"),
    ("unsupported", "this storage doesn't support S3 rules"),
])
def test_cli_check_prints_words_not_internal_states(cfg, monkeypatch, capsys, state, words):
    monkeypatch.setenv("CONFIG_DIR", cfg["CONFIG_DIR"])
    monkeypatch.setenv("CACHE_DIR", cfg["CACHE_DIR"])
    seen = {}
    monkeypatch.setattr(lc, "check", lambda c, b, **k: seen.update(k) or state)
    assert lc.main(["check", "--bucket", BASE]) == 0
    assert capsys.readouterr().out.strip() == f"S3 rules check · {BASE}: {words}"
    assert seen.get("trigger", "scheduled") == "scheduled"


def test_lifecycle_aws_calls_carry_short_timeouts(cfg):
    fake = FakeS3()
    lc.sync(cfg, BASE, run=fake)
    fake.rules[BASE] = []
    lc.check(cfg, BASE, run=fake)
    assert fake.calls
    for c in fake.calls:
        assert c[-4:] == ["--cli-connect-timeout", "10", "--cli-read-timeout", "30"], c


# --- Phase A carry-overs: M1, M2, O2, O4, R-B9 --------------------------------------------

def test_a_restored_pass_turns_an_open_not_restored_alarm_into_restored(cfg):
    cache = cfg["CACHE_DIR"]
    lc.set_status(cache, BASE, "not_restored", "AccessDenied",
                  alarm={"kind": "not_restored", "at": "2026-09-23T01:00:00Z", "lines": ["a"], "rules": ["x"]})
    lc.set_status(cache, BASE, "restored",
                  alarm={"kind": "restored", "at": "2026-09-23T02:00:00Z", "lines": ["b"]})
    alarm = lc.load_status(cache)[BASE]["alarm"]
    assert alarm["kind"] == "restored" and alarm["rules"] == ["x"] and alarm["lines"] == ["a", "b"]
    # any other state keeps the most severe: a later failed restore is NOT restored again
    lc.set_status(cache, BASE, "not_restored", "AccessDenied",
                  alarm={"kind": "not_restored", "at": "2026-09-23T03:00:00Z", "lines": []})
    assert lc.load_status(cache)[BASE]["alarm"]["kind"] == "not_restored"


def test_a_later_restore_on_the_same_bucket_replaces_not_restored_end_to_end(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(json.loads(json.dumps(HOSTILE)))
    assert lc.check(cfg, BASE, run=fake) == "console_rule"
    fake.rules[BASE] = [r for r in fake.rules[BASE] if r["ID"] == "x"]      # app rules deleted...
    fake.deny_put = True
    assert lc.check(cfg, BASE, run=fake) == "not_restored"                   # ...and not put back
    fake.deny_put = False
    assert lc.check(cfg, BASE, run=fake) == "restored"                       # ...now they are
    alarm = lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]
    assert alarm["kind"] == "restored" and alarm["rules"] == ["x"]           # the console rule stays named


def test_a_failed_status_write_never_loses_a_console_alarm(cfg, monkeypatch):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(json.loads(json.dumps(HOSTILE)))
    real = lc.set_status

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(lc, "set_status", boom)
    assert lc.check(cfg, BASE, run=fake) == "error"
    monkeypatch.setattr(lc, "set_status", real)
    assert lc.check(cfg, BASE, run=fake) == "console_rule"                   # alarmed, never absorbed
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["rules"] == ["x"]


def test_acknowledge_keeps_an_alarm_newer_than_what_the_owner_saw(cfg):
    cache = cfg["CACHE_DIR"]
    lc.set_status(cache, BASE, "restored",
                  alarm={"kind": "restored", "at": "2026-09-23T01:00:00Z", "lines": []})
    lc.set_status(cache, f"{BASE}-photos", "restored",
                  alarm={"kind": "restored", "at": "2026-09-23T05:00:00Z", "lines": []})
    lc.acknowledge(cache, seen="2026-09-23T02:00:00Z")
    st = lc.load_status(cache)
    assert "alarm" not in st[BASE] and "alarm" in st[f"{BASE}-photos"]
    lc.acknowledge(cache, seen="2026-09-23T05:00:00Z")
    assert "alarm" not in lc.load_status(cache)[f"{BASE}-photos"]


def test_acknowledge_judges_a_merged_alarm_by_its_newest_part(cfg):
    cache = cfg["CACHE_DIR"]
    lc.set_status(cache, BASE, "not_restored",
                  alarm={"kind": "not_restored", "at": "2026-09-23T01:00:00Z", "lines": []})
    lc.set_status(cache, BASE, "not_restored",
                  alarm={"kind": "console_rule", "at": "2026-09-23T04:00:00Z", "lines": [], "rules": ["x"]})
    lc.acknowledge(cache, seen="2026-09-23T01:00:00Z")          # the page only showed the first
    assert lc.load_status(cache)[BASE]["alarm"]["rules"] == ["x"]


def test_cli_check_passes_the_trigger_through(cfg, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", cfg["CONFIG_DIR"])
    monkeypatch.setenv("CACHE_DIR", cfg["CACHE_DIR"])
    seen = {}
    monkeypatch.setattr(lc, "check", lambda c, b, **k: seen.update(k) or "ok")
    assert lc.main(["check", "--bucket", BASE, "--trigger", "manual"]) == 0
    assert seen["trigger"] == "manual"
    assert lc.main(["check", "--bucket", BASE, "--trigger", "$(rm -rf /)"]) == 0
    assert seen["trigger"] == "scheduled"                       # anything odd falls back


def test_check_all_checks_every_bucket_as_a_scheduled_run(cfg, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", cfg["CONFIG_DIR"])
    monkeypatch.setenv("CACHE_DIR", cfg["CACHE_DIR"])
    seen = []
    monkeypatch.setattr(lc, "check", lambda c, b, **k: seen.append((b, k.get("trigger"))) or "ok")
    assert lc.main(["check-all"]) == 0
    assert seen == [(BASE, "scheduled")]
    assert capsys.readouterr().out.strip() == f"S3 rules check · {BASE}: in place"


def test_check_all_is_silent_when_s3_rules_are_not_managed(cfg, monkeypatch, capsys):
    from pathlib import Path
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text().replace("PERMISSIONS_VERSION=4", "PERMISSIONS_VERSION=3"))
    monkeypatch.setenv("CONFIG_DIR", cfg["CONFIG_DIR"])
    monkeypatch.setenv("CACHE_DIR", cfg["CACHE_DIR"])
    monkeypatch.setattr(lc, "check", lambda *a, **k: pytest.fail("no check below level 4"))
    assert lc.main(["check-all"]) == 0
    assert capsys.readouterr().out == ""
