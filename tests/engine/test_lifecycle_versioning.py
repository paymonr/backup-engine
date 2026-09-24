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


# --- fix round 2 (re-review of cbe2f7e..16035ad) ----------------------------------------------
# (a) the per-half tamper rule; (b) the write journal (in_flight, for a kill/timeout the
# per-half rule alone can't fix -- a CONFIRMED keeps-less apply); (c) an Activity entry + the
# O1 note on the partial path; (d) the alarm kind reflecting whichever half was tampered.

class Killed(BaseException):
    """Simulates SIGTERM (backup-job.sh's `timeout`) landing between the two puts -- no
    Python cleanup code runs at all, unlike a caught LifecycleError."""


class FailOrKill(FakeS3):
    mode = None          # None | "fail" | "kill"

    def __call__(self, args, **kw):
        if self.mode and args[:2] == ["s3api", "put-bucket-versioning"]:
            self.calls.append(list(args))
            if self.mode == "kill":
                raise Killed()
            return SimpleNamespace(returncode=254, stdout="", stderr="RequestTimeout")
        return super().__call__(args, **kw)


def _hk(rules):
    return next(r for r in rules if r["ID"] == lc.HOUSEKEEPING_ID)["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"]


def _appd(rules):
    return next(r for r in rules if r["ID"] == "backup-engine:appdata/")["NoncurrentVersionExpiration"]


def _st(cfg):
    return lc.load_status(cfg["CACHE_DIR"])[BASE]


def _confirm_suspend(cfg, fake):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}})
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert fake.versioning[BASE] == "Suspended"


def _events(cfg):
    p = Path(cfg["CACHE_DIR"], "state", "_system.runs.jsonl")
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_a_kill_between_the_puts_on_a_keeps_more_change_is_not_a_false_alarm(cfg):
    # (a): a kill (not a caught LifecycleError) between write_rules landing and write_versioning
    # -- the NEXT pass must see live already matching the (unaffected) rules target and never
    # treat that as tampering, purely from the per-half comparison (no journal needed here,
    # since the rules half's target never changed across the two passes).
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    fake.mode = "kill"
    with pytest.raises(Killed):
        lc.sync(cfg, BASE, run=fake)
    assert _hk(fake.rules[BASE]) == 14 and fake.versioning[BASE] == "Suspended"
    fake.mode = None
    state = lc.check(cfg, BASE, run=fake)
    assert state == "ok" and "alarm" not in _st(cfg)


def test_versioning_moved_outside_to_exactly_the_pending_target_is_not_alarmed(cfg):
    # (a): live versioning changed OUTSIDE the app to exactly the new (not-yet-applied)
    # target while a rules change is also pending -- live already equals target, so per the
    # per-half rule that's never tampering, even though it differs from what was applied.
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    fake.versioning[BASE] = "Enabled"                       # outside actor: exactly the new target
    state = lc.check(cfg, BASE, run=fake)
    assert state == "ok" and "alarm" not in _st(cfg)
    assert _hk(fake.rules[BASE]) == 14


def test_a_kill_after_a_confirmed_rules_write_adopts_it_next_pass_instead_of_reverting(cfg):
    # (b): the write journal -- a CONFIRMED (gated=False) undo 30->10 + suspend, killed right
    # after write_rules lands. The per-half rule alone can't save this: the NEXT pass is an
    # ORDINARY gated check(), which would judge live (now 10) against the old GATED baseline
    # (still 30, since save_applied never ran) and revert it. The journal lets that pass adopt
    # the landed rules half instead.
    fake = FailOrKill()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {
        "versioning": "suspended", "folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.mode = "kill"
    with pytest.raises(Killed):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10} and _ver(fake) == "Enabled"
    fake.mode = None
    state = lc.check(cfg, BASE, run=fake)                   # an ordinary GATED check, not a re-confirm
    assert state == "ok" and "alarm" not in _st(cfg)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}      # never reverted to 30
    _, waiting = lc.outstanding(cfg, BASE)
    assert [c.rule_id for c in waiting] == ["versioning"]         # the suspend still needs fresh confirmation


def test_a_kill_before_any_put_behaves_normally_next_pass(cfg):
    # (b): the journal is written before the puts -- if the kill lands before either one even
    # starts, live never changed at all, so the next pass just judges normally (nothing to
    # adopt, the journal is dropped).
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    doc_before = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    lc._write_in_flight(cfg["CACHE_DIR"], BASE, doc_before["rules"], doc_before["folders"], "on")
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["in_flight"]["versioning"] == "on"
    state = lc.check(cfg, BASE, run=fake)                   # nothing in flight ever landed
    assert "in_flight" not in lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert state in ("ok", "restored")
    assert _hk(fake.rules[BASE]) == 14 and fake.versioning[BASE] == "Enabled"


def test_in_flight_present_but_live_tampered_to_something_else_still_alarms(cfg):
    # (b): a journal exists, but live doesn't match it (someone else changed things in the
    # meantime) -- dropped, judged normally, still alarms like any other real tamper.
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    lc._write_in_flight(cfg["CACHE_DIR"], BASE, doc["rules"], doc["folders"], "on")
    next(r for r in fake.rules[BASE] if r["ID"] == lc.HOUSEKEEPING_ID)["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] = 1
    state = lc.check(cfg, BASE, run=fake)
    assert state == "restored" and _st(cfg)["alarm"]["kind"] == "restored"
    assert "in_flight" not in lc.load_applied_doc(cfg["CACHE_DIR"], BASE)


def test_partial_record_then_an_outside_rules_change_keeps_alarming(cfg):
    # a partial-failure record (untampered) followed by a REAL outside rules change, with the
    # versioning put STILL failing: stays "not_restored" for as long as that failure persists
    # -- restoring the rules half doesn't retroactively call the whole pass "restored" while
    # something else the app wanted is still not in S3.
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    next(r for r in fake.rules[BASE] if r["ID"] == lc.HOUSEKEEPING_ID)["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] = 3
    state = lc.check(cfg, BASE, run=fake)                   # mode still "fail"
    assert state == "not_restored" and _st(cfg)["alarm"]["kind"] == "not_restored"
    fake.mode = None
    lc.check(cfg, BASE, run=fake)
    assert _hk(fake.rules[BASE]) == 14 and fake.versioning[BASE] == "Enabled"


def test_confirmed_partial_then_an_outside_suspend_is_restored_and_alarmed(cfg):
    fake = FailOrKill()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {
        "versioning": "suspended", "folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    fake.mode = None
    fake.versioning[BASE] = "Suspended"                     # someone outside suspends
    state = lc.check(cfg, BASE, run=fake)
    assert state == "restored" and fake.versioning[BASE] == "Enabled"
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}
    _, waiting = lc.outstanding(cfg, BASE)
    assert [c.rule_id for c in waiting] == ["versioning"]


def test_gate_intact_on_the_partial_path(cfg):
    # Task 11's gate must still hold a keeps-less folder change back even when the SAME pass
    # also has an (unrelated) versioning write that fails.
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {
        "versioning": "on", "abort_uploads_days": 14, "folders": {"appdata/": {"undo_days": 10}}}}})
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    assert _hk(fake.rules[BASE]) == 14 and _appd(fake.rules[BASE]) == {"NoncurrentDays": 30}     # held
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert _appd(doc["rules"]) == {"NoncurrentDays": 30} and _hk(doc["rules"]) == 14 and doc["versioning"] == "suspended"
    fake.mode = None
    state = lc.check(cfg, BASE, run=fake)
    _, waiting = lc.outstanding(cfg, BASE)
    assert state == "ok" and "alarm" not in _st(cfg)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 30} and fake.versioning[BASE] == "Enabled"
    assert [c.rule_id for c in waiting] == ["backup-engine:appdata/"]


def test_expect_stale_intact_on_the_partial_path(cfg):
    # Task 13's expect re-check must still force gated=True on the partial path -- nothing
    # unconfirmed ever written ungated, even with a versioning write failing alongside it.
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {
        "versioning": "on", "abort_uploads_days": 14, "folders": {"appdata/": {"undo_days": 10}}}}})
    fake.mode = "fail"
    with lc.bucket_lock(cfg["CACHE_DIR"], BASE):
        state, res, err, stale = lc._reconcile_locked(cfg, BASE, run=fake, trigger="manual", gated=False, expect="bogus")
    assert stale is True and err is not None
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 30}
    assert _appd(lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["rules"]) == {"NoncurrentDays": 30}


def test_console_alarm_survives_the_partial_path(cfg):
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    fake.rules[BASE].append({"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": ""},
                             "Expiration": {"Days": 5}})
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    assert _st(cfg)["alarm"]["kind"] == "console_rule"
    assert any(r["ID"] == "my-console-rule" for r in fake.rules[BASE])            # never touched
    fake.mode = None
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _st(cfg)["alarm"]["kind"] == "console_rule"                              # kept until acked


def test_first_apply_partial_keeps_the_o1_note(cfg):
    # (c): a seeded bucket (applied=[] pre-seeded, so `first` is False) still deserves the O1
    # "kept as it is" note -- it's gated on no console fingerprint recorded yet, not on `first`.
    fake = FailOrKill(versioning={BASE: "Suspended"})
    fake.rules[BASE] = [{"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": ""},
                         "Expiration": {"Days": 5}}]
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    fake.mode = None
    lc.check(cfg, BASE, run=fake)
    logs = sorted(Path(cfg["CACHE_DIR"], "logs").rglob("*.log"))
    blob = "".join(x.read_text() for x in logs)
    assert "Kept as it is" in blob


def test_true_first_apply_partial_keeps_the_o1_note(cfg):
    # (c): a TRUE first apply (no seed at all, an upgrading install) with a partial failure --
    # the O1 note must reach Activity from the exception path, not just the "nothing to write"
    # fast path.
    fake = FailOrKill(versioning={BASE: "Suspended"})
    fake.rules[BASE] = [{"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": ""},
                         "Expiration": {"Days": 5}}]
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE) is None
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    fake.mode = None
    lc.check(cfg, BASE, run=fake)
    logs = sorted(Path(cfg["CACHE_DIR"], "logs").rglob("*.log"))
    blob = "".join(x.read_text() for x in logs)
    assert "Kept as it is" in blob


def test_tampered_rules_restored_while_versioning_keeps_failing_eventually_reads_restored(cfg):
    # (d): S3 abort tampered (7->3) while versioning is also mid-change and keeps failing --
    # the tampered rules half gets restored every pass; once a later pass is fully clean
    # ("ok", nothing left outstanding), the stuck alarm reads "restored", never stuck forever
    # at "not_restored" just because the pass that actually fixed it reported "ok".
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on"}}})
    next(r for r in fake.rules[BASE] if r["ID"] == lc.HOUSEKEEPING_ID)["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] = 3
    fake.mode = "fail"
    lc.check(cfg, BASE, run=fake)
    fake.mode = None
    lc.check(cfg, BASE, run=fake)
    assert _st(cfg)["alarm"]["kind"] == "restored"


def test_unsupported_versioning_never_perpetually_outstanding(cfg):
    class NI(FakeS3):
        def __call__(self, args, **kw):
            if args[:2] == ["s3api", "get-bucket-versioning"]:
                self.calls.append(list(args))
                return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (MethodNotAllowed)")
            return super().__call__(args, **kw)
    f = NI()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    assert lc.check(cfg, BASE, run=f) == "ok"
    assert lc.check(cfg, BASE, run=f) == "ok"
    nr, waiting = lc.outstanding(cfg, BASE)
    assert nr is False and waiting == []


def test_confirmed_partial_landed_rules_reach_activity(cfg):
    # (c): the confirmed undo 30->10 that DID reach S3, even though the pass overall failed
    # (the versioning half), must be visible in Activity -- not silently dropped because the
    # pass as a whole raised.
    fake = FailOrKill()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {
        "versioning": "suspended", "folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    fake.mode = None
    lc.check(cfg, BASE, run=fake)
    logs = sorted(Path(cfg["CACHE_DIR"], "logs").rglob("*.log"))
    blob = "".join(x.read_text() for x in logs)
    assert "10 days" in blob
