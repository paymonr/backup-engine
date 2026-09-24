# tests/engine/test_lifecycle_final_wave.py — the Phase B/C final fix wave (findings I1–I5,
# M1, M2, M8, M9, M12, P1, P4, P7): the unattended pass never writes from an unreadable
# config, one definition of a "known" folder, no versioning write for a bucket no job uses,
# guided-manual console rules in the first-apply baseline, and alarm notifications.
import json
from pathlib import Path

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import (BASE, LEGACY, FakeS3, _events, _live,  # noqa: F401
                                              _set_manga, cfg)

M = "media/manga/"


def _ran(cfg, *job_names):
    for name in job_names:
        Path(cfg["CACHE_DIR"], "state", f"{name}.json").write_text("{}")


def _jobs(cfg):
    return json.loads(Path(cfg["CONFIG_DIR"], "jobs.json").read_text())["jobs"]


def _write_jobs(cfg, jobs):
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))


def _status(cfg, bucket=BASE):
    return lc.load_status(cfg["CACHE_DIR"]).get(bucket) or {}


# --- I1: the unattended pass never writes S3 from an unreadable config ------------------------

def test_a_corrupt_jobs_file_writes_nothing_and_says_so(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3({BASE: LEGACY})
    assert lc.check(cfg, BASE, run=fake) == "ok"                     # the owner's migration
    before = json.loads(json.dumps(fake.rules[BASE]))
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text("{not json")
    fake.calls.clear()
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.calls == []                                          # not even a read
    assert fake.rules[BASE] == before                               # every folder rule still there
    st = _status(cfg)
    assert st["state"] == "error" and "jobs file couldn't be read" in st["detail"]
    assert "S3 rules left as they are" in st["detail"]


def test_probe1_fixing_the_jobs_file_leaves_nothing_waiting(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    good = Path(cfg["CONFIG_DIR"], "jobs.json").read_text()
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text("{not json")
    lc.check(cfg, BASE, run=fake)
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(good)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert lc.outstanding(cfg, BASE) == (False, [])
    assert len(fake.puts()) == 1                                     # only the migration ever wrote


def test_a_job_entry_with_an_unusable_name_counts_as_an_unreadable_jobs_file(cfg):
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    jobs = _jobs(cfg)
    jobs.append(dict(jobs[0], name="bad name"))
    _write_jobs(cfg, jobs)
    fake.calls.clear()
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.calls == [] and "jobs file couldn't be read" in _status(cfg)["detail"]


@pytest.mark.parametrize("text", ["{oops", "[]", '{"buckets": []}', '{"buckets": {"b": 7}}',
                                  '{"buckets": {"b": {"folders": []}}}',
                                  '{"buckets": {"b": {"folders": {"appdata/": 30}}}}'])
def test_an_unreadable_settings_file_writes_nothing(cfg, text):
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    Path(cfg["CONFIG_DIR"], "storage.json").write_text(text)
    fake.calls.clear()
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.calls == []
    assert "S3 rules settings couldn't be read" in _status(cfg)["detail"]
    assert Path(cfg["CONFIG_DIR"], "storage.json").read_text() == text        # never overwritten


def test_a_missing_settings_file_is_the_defaults(cfg):
    assert lc.load_settings_strict(cfg["CONFIG_DIR"]) == {"version": 1, "buckets": {}}


def test_probe7_a_confirmed_suspend_and_tier_survive_an_unreadable_settings_file(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake)
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended", "folders": {
        M: {"tier": {"class": "DEEP_ARCHIVE", "after_days": 30}}}}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert fake.versioning[BASE] == "Suspended"
    rules = json.loads(json.dumps(fake.rules[BASE]))
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.versioning[BASE] == "Suspended" and fake.rules[BASE] == rules


def test_a_job_save_sync_on_an_unreadable_settings_file_raises_a_config_error(cfg):
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    fake = FakeS3()
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=fake)
    assert e.value.kind == "config" and fake.calls == []


def test_check_all_on_a_corrupt_jobs_file_writes_nothing(cfg, monkeypatch):
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text("{not json")
    fake.calls.clear()
    real = lc.check
    monkeypatch.setattr(lc, "check", lambda c, b, **kw: real(c, b, run=fake, **kw))
    lines = lc.check_all_lines(cfg)
    assert lines == [f"S3 rules check · {BASE}: couldn't be checked (see Setup)"]
    assert fake.calls == []


def test_a_first_apply_seed_never_overwrites_an_unreadable_settings_file(cfg, tmp_path):
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    long = [dict(r, NoncurrentVersionExpiration={"NoncurrentDays": 90}) for r in LEGACY]
    with pytest.raises(lc.SettingsFileError):
        lc.seed_undo_days(cfg["CONFIG_DIR"], BASE, long, lc.folders_for(BASE, BASE, _jobs(cfg)))
    assert Path(cfg["CONFIG_DIR"], "storage.json").read_text() == "{oops"


# --- I1: a folder whose job's history setting can't be read holds its baseline rule ---------

def test_an_unreadable_history_setting_holds_the_rule_s3_already_has(cfg):
    _ran(cfg, "manga")
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)                                     # manga 180 days applied
    _set_manga(cfg, {"type": "count", "count": "x"})                  # hand-edited, unreadable
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert len(fake.puts()) == 1
    assert lc.outstanding(cfg, BASE) == (False, [])
    assert "manga" in _status(cfg)["detail"]


def test_an_unreadable_history_setting_on_a_first_apply_keeps_what_the_legacy_rule_does(cfg):
    _ran(cfg, "manga")
    _set_manga(cfg, {"type": "nope"})
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


def test_an_unreadable_history_setting_is_held_through_a_confirmed_apply(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    _set_manga(cfg, {"type": "count", "count": "x"})
    settings = {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 7}}}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert [c.folder for c in pv.keeps_less] == ["appdata/"]           # manga isn't a change
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}
