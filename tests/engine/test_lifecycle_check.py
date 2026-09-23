# tests/engine/test_lifecycle_check.py — the tamper check (spec §2).
import json

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import BASE, CONSOLE, FakeS3, cfg  # noqa: F401 (fixture)


def _applied(cfg, fake):
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


def test_console_rule_edits_are_not_tampering(cfg):
    fake = FakeS3()
    _applied(cfg, fake)
    fake.rules[BASE].append(CONSOLE)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert CONSOLE in fake.rules[BASE]


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
