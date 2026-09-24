# tests/engine/test_lifecycle_versioning.py — versioning intent + tamper coverage (spec §1, §2; R-B7).
import json
from pathlib import Path
from types import SimpleNamespace

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


# --- fix round 1 -----------------------------------------------------------------------------

class VerPutFails(FakeS3):
    """put-bucket-versioning fails (transient) while everything else, including
    put-bucket-lifecycle-configuration, succeeds normally."""
    fail_ver = False

    def __call__(self, args, **kw):
        if self.fail_ver and args[:2] == ["s3api", "put-bucket-versioning"]:
            self.calls.append(list(args))
            return SimpleNamespace(returncode=254, stdout="", stderr="RequestTimeout")
        return super().__call__(args, **kw)


def test_a_landed_rules_write_survives_a_versioning_write_that_then_fails(cfg):
    # I1: write_rules succeeding while write_versioning then fails must not leave applied.json
    # behind S3 -- the next pass would otherwise see live rules (this pass's OWN write) differ
    # from a stale applied record and raise a false "changed outside" alarm, or even revert them.
    fake = VerPutFails()
    _started(cfg, fake)                                              # applied: abort=7, versioning=on
    tok = lc.preview(cfg, BASE, {"kind": "settings",
                                 "settings": {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}}).token
    lc.apply_confirmed(cfg, tok, BASE, run=fake)
    assert _ver(fake) == "Suspended"
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1,
                                         "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    fake.fail_ver = True
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)                                 # keeps-more: abort 7->14 + versioning on
    assert _ver(fake) == "Suspended"                                 # the versioning write failed, unchanged
    hk_live = next(r for r in fake.rules[BASE] if r["ID"] == lc.HOUSEKEEPING_ID)
    assert hk_live["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 14      # the rules write DID land
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert doc["versioning"] == "suspended"                          # the OLD value -- correct, matches S3
    hk_applied = next(r for r in doc["rules"] if r["ID"] == lc.HOUSEKEEPING_ID)
    assert hk_applied["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 14   # recorded, matches S3
    fake.fail_ver = False
    assert lc.check(cfg, BASE, run=fake) == "ok"                     # no false tamper alarm
    assert lc.load_status(cfg["CACHE_DIR"])[BASE].get("alarm") is None
    assert _ver(fake) == "Enabled"                                   # the retried versioning write lands


def test_a_confirmed_rules_change_survives_a_versioning_write_that_then_fails(cfg):
    fake = VerPutFails()
    _started(cfg, fake)                                              # applied: undo 30 days, versioning on
    edit = {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {
        "versioning": "suspended", "folders": {"appdata/": {"undo_days": 10}}}}}}
    pv = lc.preview(cfg, BASE, edit)
    assert {c.rule_id for c in pv.keeps_less} == {"backup-engine:appdata/", "versioning"}
    fake.fail_ver = True
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    appd_live = next(r for r in fake.rules[BASE] if r["ID"] == "backup-engine:appdata/")
    assert appd_live["NoncurrentVersionExpiration"] == {"NoncurrentDays": 10}          # the rules write DID land
    assert _ver(fake) == "Enabled"                                   # the versioning write failed, unchanged
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert doc["versioning"] == "on"                                 # the OLD value -- correct, matches S3
    appd_applied = next(r for r in doc["rules"] if r["ID"] == "backup-engine:appdata/")
    assert appd_applied["NoncurrentVersionExpiration"] == {"NoncurrentDays": 10}       # recorded, matches S3
    fake.fail_ver = False
    assert lc.check(cfg, BASE, run=fake) == "ok"                     # no false tamper alarm, no revert
    appd_live = next(r for r in fake.rules[BASE] if r["ID"] == "backup-engine:appdata/")
    assert appd_live["NoncurrentVersionExpiration"] == {"NoncurrentDays": 10}          # never reverted to 30


def test_a_kill_right_after_write_rules_lands_is_never_mistaken_for_tampering(cfg):
    # save_applied never ran at all (e.g. the process died right after the AWS call
    # returned, before Python could write the state file) -- the next pass must see live
    # already matching what it wants NOW and self-heal, never alarm over a stale record.
    fake = FakeS3()
    _started(cfg, fake)                                              # applied: abort=7
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"abort_uploads_days": 14}}})
    hk = next(r for r in fake.rules[BASE] if r["ID"] == lc.HOUSEKEEPING_ID)
    hk["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] = 14   # simulate: the write landed...
    # ...but applied.json was never updated -- still says abort=7
    assert lc.load_applied(cfg["CACHE_DIR"], BASE) != fake.rules[BASE]
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]
    hk_applied = next(r for r in lc.load_applied(cfg["CACHE_DIR"], BASE) if r["ID"] == lc.HOUSEKEEPING_ID)
    assert hk_applied["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 14    # self-healed


def test_write_versioning_rejects_a_state_other_than_on_or_suspended():
    fake = FakeS3()
    with pytest.raises(ValueError):
        lc.write_versioning(BASE, "never", ROLE_CREDS, "us-east-1", run=fake)
    assert _vputs(fake) == []                                        # never sent Suspended by accident


class VerUnsupported(FakeS3):
    """get-bucket-versioning isn't implemented on this storage; lifecycle rules work fine."""
    def __call__(self, args, **kw):
        if args[:2] == ["s3api", "get-bucket-versioning"]:
            self.calls.append(list(args))
            return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (NotImplemented)")
        return super().__call__(args, **kw)


def test_a_storage_that_cant_report_versioning_still_gets_its_lifecycle_rules(cfg):
    fake = VerUnsupported()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    state = lc.check(cfg, BASE, run=fake)
    assert state == "ok"                                             # not "unsupported" -- rules still applied
    ids = {r["ID"] for r in fake.rules[BASE]}
    assert "backup-engine:media/manga/" in ids and lc.HOUSEKEEPING_ID in ids
    assert _vputs(fake) == []                                        # never attempted a versioning write
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert "versioning" not in doc                                   # nothing claimed as applied
    assert lc.load_live(cfg["CACHE_DIR"], BASE).get("versioning") is None
    assert "doesn't report versioning" in lc.load_status(cfg["CACHE_DIR"])[BASE]["detail"]


def test_outstanding_flags_an_unapplied_keeps_more_versioning_change(cfg):
    fake = FakeS3()
    _started(cfg, fake)                                              # applied doc: versioning "on"
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    lc.save_applied(cfg["CACHE_DIR"], BASE, doc["rules"], folders=doc["folders"], versioning="suspended")
    not_reached, waiting = lc.outstanding(cfg, BASE)
    assert not_reached is True and waiting == []                     # keeps more, not a waiting confirmation


def test_inputs_hash_goes_stale_when_only_versioning_changed(cfg):
    # ABA (fix round 1): a preview token must go stale if the applied record's versioning
    # changes underneath it, even when rules/folders bytes are untouched.
    fake = FakeS3()
    _started(cfg, fake)
    h1 = lc._inputs_hash(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], BASE)
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    lc.save_applied(cfg["CACHE_DIR"], BASE, doc["rules"], folders=doc["folders"], versioning="suspended")
    h2 = lc._inputs_hash(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], BASE)
    assert h1 != h2


def test_no_alarm_on_the_first_check_after_an_upgrade_without_a_versioning_record(cfg):
    # An applied.json from before Task 16 has no "versioning" key at all -- even when live
    # versioning differs from the current intent, that must read as "not yet applied"
    # (like any other pending job/settings change), never as tampering.
    fake = FakeS3(versioning={BASE: "Suspended"})
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)                                     # first apply; records rules + versioning
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    doc.pop("versioning", None)                                      # simulate: pre-Task-16 applied record
    lc._write_atomic(Path(lc._state_dir(cfg["CACHE_DIR"]), f"{BASE}.applied.json"), json.dumps(doc))
    fake.versioning[BASE] = "Suspended"                               # live still whatever it was before
    state = lc.check(cfg, BASE, run=fake)
    assert state not in ("restored", "not_restored")
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]
