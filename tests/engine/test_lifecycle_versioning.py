# tests/engine/test_lifecycle_versioning.py — versioning intent + tamper coverage (spec §1, §2; R-B7).
import json
from pathlib import Path

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import BASE, FakeS3, cfg  # noqa: F401

ROLE_CREDS = {"AWS_ACCESS_KEY_ID": "ASIAROLE", "AWS_SECRET_ACCESS_KEY": "rolesecret", "AWS_SESSION_TOKEN": "roletok"}


def _ver(fake, bucket=BASE):
    return fake.versioning.get(bucket, "Enabled")


def _vputs(fake):
    return [c for c in fake.calls if c[:2] == ["s3api", "put-bucket-versioning"]]


def _settings(cfg, **bucket):
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: bucket}})


def _started(cfg, fake):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)


@pytest.mark.parametrize("status,state", [("Enabled", "on"), ("Suspended", "suspended"), (None, "never")])
def test_read_versioning(status, state):
    assert lc.read_versioning(BASE, ROLE_CREDS, "us-east-1", run=FakeS3(versioning={BASE: status})) == state


@pytest.mark.parametrize("settings,jobs,base_versioned,want", [
    ({}, [], True, "on"),
    ({}, [], False, "suspended"),
    ({"buckets": {BASE: {"versioning": "suspended"}}}, [], True, "suspended"),
    ({}, [{"name": "p", "dedicated": True, "bucket": f"{BASE}-p", "bucket_versioned": False}], True, "suspended"),
])
def test_versioning_intent(settings, jobs, base_versioned, want):
    bucket = f"{BASE}-p" if jobs else BASE
    assert lc.versioning_intent(bucket, BASE, jobs, settings, base_versioned=base_versioned) == want


def test_an_enabled_bucket_is_left_alone_and_the_intent_is_recorded(cfg):
    fake = FakeS3()
    _started(cfg, fake)
    assert _vputs(fake) == []
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "on"
    assert lc.load_live(cfg["CACHE_DIR"], BASE)["versioning"] == "on"


def test_a_never_versioned_bucket_is_turned_on_automatically(cfg):
    fake = FakeS3(versioning={BASE: None})
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    res = lc.sync(cfg, BASE, run=fake)
    assert _ver(fake) == "Enabled" and res.waiting == [] and "now: versioning on" in res.lines


def test_suspending_versioning_waits_for_confirmation(cfg):
    fake = FakeS3()
    _started(cfg, fake)
    _settings(cfg, versioning="suspended")
    res = lc.sync(cfg, BASE, run=fake)
    assert _ver(fake) == "Enabled" and _vputs(fake) == []
    assert [(c.rule_id, c.kind, c.words) for c in res.waiting] == [
        ("versioning", lc.KEEPS_LESS, "versioning: on → suspended")]
    assert lc.outstanding(cfg, BASE)[1][0].rule_id == "versioning"


def test_a_confirmed_suspend_needs_the_bucket_name_and_applies(cfg):
    fake = FakeS3()
    _started(cfg, fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}})
    assert pv.needs_typed is True and [c.rule_id for c in pv.keeps_less] == ["versioning"]
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _ver(fake) == "Suspended"
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "suspended"


def test_versioning_changed_outside_is_restored_and_alarmed(cfg):
    fake = FakeS3()
    _started(cfg, fake)
    fake.versioning[BASE] = "Suspended"                               # someone suspended it
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert _ver(fake) == "Enabled"
    alarm = lc.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]
    assert alarm["kind"] == "restored" and "versioning: was on, now suspended" in alarm["lines"]


def test_a_versioning_restore_that_fails_is_not_restored(cfg):
    fake = FakeS3()
    _started(cfg, fake)
    fake.versioning[BASE] = "Suspended"
    fake.deny_put = True
    assert lc.check(cfg, BASE, run=fake) == "not_restored"


def test_a_dedicated_bucket_made_without_versioning_stays_that_way(cfg):
    ded = f"{BASE}-photos"
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"].append({"name": "photos", "type": "archive", "source": "media/photos", "schedule": "0 3 * * *",
                         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "keep_all"},
                         "dedicated": True, "bucket": ded, "bucket_versioned": False})
    jobs_p.write_text(json.dumps(data))
    lc.seed_new_bucket(cfg["CACHE_DIR"], ded)
    fake = FakeS3(versioning={ded: "Suspended"})
    res = lc.sync(cfg, ded, run=fake)
    assert _vputs(fake) == [] and res.waiting == []


def test_an_unversioned_base_bucket_setting_is_never_forced_either_way(cfg):
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text() + "BASE_BUCKET_VERSIONED=false\n")
    fake = FakeS3()                                                   # yet it is Enabled in S3
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    res = lc.sync(cfg, BASE, run=fake)
    assert _ver(fake) == "Enabled" and [c.rule_id for c in res.waiting] == ["versioning"]
