# tests/engine/test_lifecycle_versioning.py — versioning intent + tamper coverage (spec §1, §2; R-B7).
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import BASE, LEGACY, FakeS3, cfg  # noqa: F401

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


def _inflight(cfg, bucket=BASE):
    return lc._load_in_flight(cfg["CACHE_DIR"], bucket)


def test_a_kill_before_any_put_behaves_normally_next_pass(cfg):
    # (b): the journal is written before the puts -- if the kill lands before either one even
    # starts, live never changed at all, so the next pass just judges normally (nothing to
    # adopt, the journal is dropped). Fix round 3, item 1: the journal is its OWN file, never
    # inside applied.json.
    fake = FailOrKill()
    _confirm_suspend(cfg, fake)
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "on", "abort_uploads_days": 14}}})
    doc_before = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    lc._write_in_flight(cfg["CACHE_DIR"], BASE, doc_before["rules"], doc_before["folders"], "on")
    assert "in_flight" not in lc.load_applied_doc(cfg["CACHE_DIR"], BASE)     # never touches applied.json
    assert _inflight(cfg)["versioning"] == "on"
    state = lc.check(cfg, BASE, run=fake)                   # nothing in flight ever landed
    assert _inflight(cfg) is None
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
    assert _inflight(cfg) is None


def test_a_corrupt_applied_record_is_never_overwritten_by_the_journal(cfg):
    # item 1: the journal must never create or rewrite the applied record on its own -- only
    # save_applied does, and only when something in the journal actually matches live. A
    # corrupt/unreadable applied.json (never valid JSON) reads as None, exactly like no
    # record at all -- and a pass whose OWN write ALSO fails (nothing lands, nothing matches
    # the journal either) must leave it exactly as unreadable as it started.
    fake = FakeS3(deny_put=True)
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    Path(lc._state_dir(cfg["CACHE_DIR"]), f"{BASE}.applied.json").write_text("{not valid json")
    lc._write_in_flight(cfg["CACHE_DIR"], BASE, [{"ID": lc.HOUSEKEEPING_ID}], ["appdata/"], "on")
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)                          # nothing in the journal matches live; write also fails
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE) is None      # still corrupt/unreadable -> None, never a stub


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
    # Fix round 4, item 4: the partial pass keeps its journal (the failed versioning put may have
    # landed), so an outside suspend to EXACTLY the confirmed target inside the journal's 6 h
    # TTL is indistinguishable from that put having landed and is adopted (the accepted TTL
    # trade-off -- a versioning-only confirmed suspend already behaved so). Once the journal is
    # past its TTL, nothing vouches for it: restored and alarmed, as before.
    fake = FailOrKill()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {
        "versioning": "suspended", "folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.mode = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    fake.mode = None
    _age_journal(cfg, 6 * 3600 + 120)
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


# --- fix round 3 (re-review of 16035ad..72a52a8) ------------------------------------------------
# item 1: the journal must never create/rewrite an applied record; N1/N1b: a failed/killed
# TRUE first apply must still read as a first apply next time, never a false tamper alarm.

class Fake2(FakeS3):
    """put_rules: None | "fail" (error, nothing landed) | "landed_err" (lands, THEN errors) |
    "kill_after" (lands, then the process is killed before the pass hears back);
    put_ver: None | "fail" | "landed_err"; ver_read: None | "unsupported"."""
    put_rules = None
    put_ver = None
    ver_read = None

    def __call__(self, args, **kw):
        if self.put_rules and args[:2] == ["s3api", "put-bucket-lifecycle-configuration"]:
            if self.put_rules in ("landed_err", "kill_after"):
                super().__call__(args, **kw)
            else:
                self.calls.append(list(args))
            if self.put_rules == "kill_after":
                raise Killed()
            return SimpleNamespace(returncode=254, stdout="", stderr="RequestTimeout")
        if self.put_ver and args[:2] == ["s3api", "put-bucket-versioning"]:
            if self.put_ver == "landed_err":
                super().__call__(args, **kw)
            else:
                self.calls.append(list(args))
            return SimpleNamespace(returncode=254, stdout="", stderr="RequestTimeout")
        if self.ver_read and args[:2] == ["s3api", "get-bucket-versioning"]:
            self.calls.append(list(args))
            return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (MethodNotAllowed)")
        return super().__call__(args, **kw)


def test_n1_true_first_apply_rules_put_failure_stays_a_first_apply(cfg):
    fake = Fake2({BASE: json.loads(json.dumps(LEGACY))})
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE) is None
    fake.put_rules = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE) is None      # never a bare {"rules": []} stub
    fake.put_rules = None
    state = lc.check(cfg, BASE, run=fake)
    _, waiting = lc.outstanding(cfg, BASE)
    assert state == "ok" and "alarm" not in _st(cfg), "false tamper alarm on the pass after a failed first apply"
    assert waiting == []
    ids = {r["ID"] for r in fake.rules[BASE]}
    assert "backstop-appdata" not in ids and "backstop-media" not in ids   # legacy backstops replaced


def test_n1b_true_first_apply_killed_before_any_put(cfg, monkeypatch):
    fake = Fake2({BASE: json.loads(json.dumps(LEGACY))})

    def boom(*a, **k):
        raise Killed()
    monkeypatch.setattr(lc, "write_rules", boom)
    with pytest.raises(Killed):
        lc.sync(cfg, BASE, run=fake)
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE) is None      # never a bare {"rules": []} stub
    monkeypatch.undo()
    state = lc.check(cfg, BASE, run=fake)
    assert state == "ok" and "alarm" not in _st(cfg)
    ids = {r["ID"] for r in fake.rules[BASE]}
    assert "backstop-appdata" not in ids and "backstop-media" not in ids


@pytest.mark.parametrize("console_change", [False, True])
def test_n2_confirmed_landed_but_errored_put_is_adopted(cfg, console_change):
    # a confirmed undo 30->10 whose rules put LANDED but reported an error (timeout); in the
    # True case, a console rule ALSO changed (the fingerprint moved) in the same pass.
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 10}}}}}})
    if console_change:
        fake.rules[BASE].append({"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": "x/"},
                                 "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]})
    fake.put_rules = "landed_err"
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}      # DID land despite the reported error
    fake.put_rules = None
    state = lc.check(cfg, BASE, run=fake)
    alarm = _st(cfg).get("alarm") or {}
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}, "confirmed, landed write reverted"
    assert alarm.get("kind") in (None, "console_rule")
    assert state in ("ok", "console_rule")


def test_n3_ok_with_versioning_unmanaged_does_not_heal_a_versioning_alarm(cfg):
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    fake.versioning[BASE] = "Suspended"                         # outside suspend
    fake.put_ver = "fail"
    assert lc.check(cfg, BASE, run=fake) == "not_restored"
    assert _st(cfg)["alarm"]["kind"] == "not_restored"
    fake.put_ver, fake.ver_read = None, "unsupported"
    lc.check(cfg, BASE, run=fake)
    assert _st(cfg)["alarm"]["kind"] == "not_restored", "alarm healed although versioning still suspended"


def test_n7_seeded_rules_put_failure_keeps_o1_note(cfg):
    fake = Fake2(versioning={BASE: "Suspended"})
    fake.rules[BASE] = [{"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": ""},
                         "Expiration": {"Days": 5}}]
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake.put_rules = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)
    fake.put_rules = None
    lc.check(cfg, BASE, run=fake)
    blob = "".join(x.read_text() for x in sorted(Path(cfg["CACHE_DIR"], "logs").rglob("*.log")))
    assert "Kept as it is" in blob


# --- fix round 4 (re-review of 72a52a8..88823ca) ------------------------------------------------

def _journal_file(cfg, bucket=BASE):
    return lc._inflight_path(cfg["CACHE_DIR"], bucket)


def _age_journal(cfg, seconds):
    p = _journal_file(cfg)
    doc = json.loads(p.read_text())
    doc["written_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(doc))


def _confirmed_undo_10_killed_after_the_put_lands(cfg):
    """A confirmed undo 30->10 whose rules put LANDS, then the process is killed before the
    pass records it: live now matches the journal exactly, applied.json still says 30."""
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)                                     # applied: undo 30 days
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.put_rules = "kill_after"
    with pytest.raises(Killed):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    fake.put_rules = None
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}                           # landed...
    assert _appd(lc.load_applied(cfg["CACHE_DIR"], BASE)) == {"NoncurrentDays": 30}    # ...never recorded
    assert _appd(_inflight(cfg)["rules"]) == {"NoncurrentDays": 10}                    # live DOES match the journal
    return fake


# item 1: the 6 h TTL, pinned at the spec's number (not derived from _INFLIGHT_TTL_S, so a
# changed/disabled constant fails here). Live matches the journal in both cases -- only its age differs.
@pytest.mark.parametrize("age_s,adopted", [(6 * 3600 - 120, True), (6 * 3600 + 120, False)])
def test_a_journal_live_matches_is_adopted_only_inside_its_6h_ttl(cfg, age_s, adopted):
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)
    _age_journal(cfg, age_s)
    state = lc.check(cfg, BASE, run=fake)                           # an ordinary GATED check
    assert not _journal_file(cfg).exists()                          # consumed either way
    if adopted:
        assert state == "ok" and "alarm" not in _st(cfg)
        assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}
        assert _appd(lc.load_applied(cfg["CACHE_DIR"], BASE)) == {"NoncurrentDays": 10}
    else:                                                           # judged normally: live != applied
        assert state == "restored" and _st(cfg)["alarm"]["kind"] == "restored"
        assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 30}
        assert _appd(lc.load_applied(cfg["CACHE_DIR"], BASE)) == {"NoncurrentDays": 30}


# item 2: "status first" for tamper -- a tampered pass killed right after the restoring rules put
# lands (before it ever reaches its own post-write status call) still leaves the alarm on disk.
def test_a_tampered_pass_killed_right_after_the_restoring_put_lands_leaves_the_alarm(cfg):
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    fake.rules[BASE] = [r for r in fake.rules[BASE] if r["ID"] != "backup-engine:appdata/"]   # changed outside
    fake.put_rules = "kill_after"
    with pytest.raises(Killed):
        lc.check(cfg, BASE, run=fake)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 30}          # the restoring put DID land
    alarm = _st(cfg).get("alarm")
    assert alarm and alarm["kind"] == "not_restored", "the kill lost the tamper alarm"
    assert any("appdata/" in ln for ln in alarm["lines"])
    fake.put_rules = None
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # the landed restore is adopted...
    alarm = _st(cfg)["alarm"]
    assert alarm["kind"] == "restored" and any("appdata/" in ln for ln in alarm["lines"])   # ...the alarm stays


# item 3: the journal is deleted only AFTER its adoption is saved -- a kill in between (here: the
# save itself, or the delete right after it) must never lose the adoption, or the next check
# reverts a confirmed, landed write with a false alarm.
@pytest.mark.parametrize("killed_in", ["save_applied", "_delete_in_flight"])
def test_a_kill_while_a_journal_is_being_adopted_never_loses_the_adoption(cfg, monkeypatch, killed_in):
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)

    def boom(*a, **k):
        raise Killed()
    monkeypatch.setattr(lc, killed_in, boom)
    with pytest.raises(Killed):
        lc.check(cfg, BASE, run=fake)                               # killed while adopting the journal
    monkeypatch.undo()
    assert _journal_file(cfg).exists()                              # not consumed: the adoption isn't done
    state = lc.check(cfg, BASE, run=fake)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}, "a confirmed, landed write was reverted"
    assert state == "ok" and "alarm" not in _st(cfg)
    assert _appd(lc.load_applied(cfg["CACHE_DIR"], BASE)) == {"NoncurrentDays": 10}
    assert not _journal_file(cfg).exists()


# item 4: a rules write lands, then the versioning put of a CONFIRMED suspend lands too but reports
# an error (e.g. a timeout after S3 applied it). That pass records the OLD versioning -- it can't
# know -- so it must keep the journal: the next check adopts the landed suspend instead of
# reverting it with a false tamper alarm.
@pytest.mark.parametrize("rules_edit", [{"abort_uploads_days": 14},                        # keeps more
                                        {"folders": {"appdata/": {"undo_days": 10}}}])      # keeps less
def test_a_confirmed_suspend_that_lands_but_errors_after_a_rules_write_is_kept(cfg, rules_edit):
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"versioning": "suspended", **rules_edit}}}})
    assert "versioning" in {c.rule_id for c in pv.keeps_less}
    fake.put_ver = "landed_err"
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    live_rules = json.loads(json.dumps(fake.rules[BASE]))
    assert _ver(fake) == "Suspended"                                 # it DID land, despite the error
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "on"      # the pass couldn't know
    fake.put_ver = None
    state = lc.check(cfg, BASE, run=fake)
    assert _ver(fake) == "Suspended", "a confirmed, landed suspend was reverted"
    assert state == "ok" and "alarm" not in _st(cfg)
    assert fake.rules[BASE] == live_rules                            # the landed rules untouched too
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "suspended"
    assert lc.outstanding(cfg, BASE) == (False, [])
    assert not _journal_file(cfg).exists()


# item 5: a TRUE first apply (no applied record) whose rules put keeps failing notes the console
# rules that can already delete or move backups (O1) ONCE -- not on every failing pass -- and
# never by writing an applied record (the next pass must stay a first apply).
def _o1_notes(cfg):
    blob = "".join(x.read_text() for x in sorted(Path(cfg["CACHE_DIR"], "logs").rglob("*.log")))
    return blob.count("Kept as it is — a rule added in the AWS console: my-console-rule")


def test_a_true_first_apply_whose_rules_put_keeps_failing_notes_the_console_rule_once(cfg):
    console = {"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 5}}
    fake = Fake2({BASE: json.loads(json.dumps(LEGACY)) + [dict(console)]})
    fake.put_rules = "fail"
    for _ in range(3):
        assert lc.check(cfg, BASE, run=fake) == "error"
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE) is None      # still a first apply
    assert _o1_notes(cfg) == 1
    fake.put_rules = None
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # the first apply finally lands
    assert "alarm" not in _st(cfg)
    assert _o1_notes(cfg) == 1                                          # still noted just once
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["console"] == lc.console_fingerprint(fake.rules[BASE])
    assert not lc._o1_noted_path(cfg["CACHE_DIR"], BASE).exists()      # the recorded fingerprint took over


def test_a_console_rule_that_changes_between_failing_first_apply_passes_is_noted_again(cfg):
    console = {"ID": "my-console-rule", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 5}}
    fake = Fake2({BASE: json.loads(json.dumps(LEGACY)) + [dict(console)]})
    fake.put_rules = "fail"
    lc.check(cfg, BASE, run=fake)
    lc.check(cfg, BASE, run=fake)
    next(r for r in fake.rules[BASE] if r["ID"] == "my-console-rule")["Expiration"]["Days"] = 2   # changed
    lc.check(cfg, BASE, run=fake)
    lc.check(cfg, BASE, run=fake)
    assert _o1_notes(cfg) == 2                                          # once per distinct set of console rules


# item 6 (N3's sibling): a "restored" pass on storage that doesn't report versioning (that half
# unmanaged) restores the rules but never checked versioning -- it must not heal an open
# not_restored alarm about versioning, exactly like an "ok" pass (N3).
def test_n3_restored_with_versioning_unmanaged_does_not_heal_a_versioning_alarm(cfg):
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    fake.versioning[BASE] = "Suspended"                             # outside suspend...
    fake.put_ver = "fail"
    assert lc.check(cfg, BASE, run=fake) == "not_restored"          # ...not put back
    fake.put_ver, fake.ver_read = None, "unsupported"               # versioning can't even be read now
    fake.rules[BASE] = [r for r in fake.rules[BASE] if r["ID"] != "backup-engine:appdata/"]   # rules changed too
    assert lc.check(cfg, BASE, run=fake) == "restored"              # the rules half is put back
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 30} and _ver(fake) == "Suspended"
    alarm = _st(cfg)["alarm"]
    assert alarm["kind"] == "not_restored", "a restored rules half healed an alarm about unchecked versioning"
    assert "versioning: was on, now suspended" in alarm["lines"] and any("appdata/" in ln for ln in alarm["lines"])
    fake.ver_read = None                                            # versioning readable again
    assert lc.check(cfg, BASE, run=fake) == "restored" and _ver(fake) == "Enabled"
    assert _st(cfg)["alarm"]["kind"] == "restored"                  # now it really is back
