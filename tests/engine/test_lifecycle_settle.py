# tests/engine/test_lifecycle_settle.py — S3's read-after-write settle window (settle-fix-findings.md).
# S3 serves bucket configuration eventually consistently: for a while after a put, a read can still
# return the configuration from BEFORE it, even after an earlier read already showed the new one
# (smoke test run 1, S7). Within SETTLE_S of the app's own write, a live half equal to the state
# right before that write is "settling": never tampering, never alarmed or notified, never written
# back, the journal kept -- and never the reason an OLD rule is written. Anything else is judged
# exactly as before.
import json
from pathlib import Path

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import BASE, FakeS3, _live, _set_manga, cfg  # noqa: F401
from tests.engine.test_lifecycle_sync import age_writes as _age_writes
from tests.engine.test_lifecycle_versioning import (Fake2, Killed, _appd, _confirmed_undo_10_killed_after_the_put_lands,
                                                    _journal_file, _st, _ver, _vputs)

MANGA = "backup-engine:media/manga/"


@pytest.fixture
def sent(cfg, monkeypatch):
    """Every notification a check would send (the conftest autouse stub, re-patched to record)."""
    out = []
    monkeypatch.setattr(lc, "_send_notification", lambda urls, title, body: out.append(title) or True)
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text() + "APPRISE_URLS=ntfy://example/topic\n")
    return out


def _manga(rules):
    return next(r for r in rules if r["ID"] == MANGA)["NoncurrentVersionExpiration"]["NoncurrentDays"]


def _applied(cfg):
    return lc.load_applied(cfg["CACHE_DIR"], BASE)


def _read_rules(cfg):
    """The rules the last pass READ from S3 (live.json) -- proves a stale read really happened."""
    return lc.load_live(cfg["CACHE_DIR"], BASE)["rules"]


def _job_saved_to_365(cfg, fake):
    """A bucket in step at manga 180 days, then a job save that lengthens it to 365 (keeps more:
    written at once by the save's own sync)."""
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    _set_manga(cfg, {"type": "days", "days": 365})
    res = lc.sync(cfg, BASE, run=fake)
    assert res.changed and _manga(fake.rules[BASE]) == 365


def _settling(cfg):
    st = _st(cfg)
    return st["state"] == "ok" and "alarm" not in st and lc.SETTLING in st["detail"]


# --- 1. a check right after the app's own write reads the old rules ---------------------------

def test_a_check_right_after_a_job_save_that_reads_the_old_rules_is_settling(cfg, sent):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    fake.stale_next("rules", 1)                             # S3's next read: the rules before that put
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _manga(_read_rules(cfg)) == 180                  # it really did read the old rules
    assert len(fake.puts()) == puts and _vputs(fake) == []  # nothing written back
    assert _settling(cfg) and sent == []
    assert _manga(_applied(cfg)) == 365 and _manga(fake.rules[BASE]) == 365
    assert lc.check(cfg, BASE, run=fake) == "ok"            # the next read is fresh: plainly in step
    assert len(fake.puts()) == puts and lc.SETTLING not in _st(cfg)["detail"] and sent == []


def test_the_settle_note_is_owner_words():
    assert lc.SETTLING == "S3 is still applying the last change"


# --- 2. a confirmed keeps-less change, then stale reads ------------------------------------------

def test_a_confirmed_keeps_less_change_is_never_alarmed_or_undone_by_stale_reads(cfg, sent):
    fake = FakeS3()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)                                      # applied: undo 30 days
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 10}}}}}})
    assert pv.token
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10}
    fake.stale_next("rules", 2)
    puts = len(fake.puts())
    for _ in range(2):                                                # two stale reads of undo 30
        assert lc.check(cfg, BASE, run=fake) == "ok"
        assert _appd(_read_rules(cfg)) == {"NoncurrentDays": 30} and _settling(cfg)
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # a fresh read: in step
    assert len(fake.puts()) == puts and sent == []
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10} and _appd(_applied(cfg)) == {"NoncurrentDays": 10}
    assert not _journal_file(cfg).exists() and lc.outstanding(cfg, BASE) == (False, [])


# --- 3. killed after a confirmed put, then a stale read (the silent-revert case) ------------------

def test_a_stale_read_after_a_killed_confirmed_put_keeps_the_journal_and_writes_nothing(cfg, sent):
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)        # S3: 10, applied: 30, journal: 10
    fake.stale_next("rules", 1)                                       # ...but S3's next read still says 30
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # an ordinary GATED check
    assert _appd(_read_rules(cfg)) == {"NoncurrentDays": 30}
    assert len(fake.puts()) == puts, "the gated pass wrote the old rule back"
    assert _journal_file(cfg).exists()                                # kept: not dropped, not adopted
    assert _appd(_applied(cfg)) == {"NoncurrentDays": 30} and _settling(cfg)
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # the fresh read adopts it
    assert len(fake.puts()) == puts and not _journal_file(cfg).exists()
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10} and _appd(_applied(cfg)) == {"NoncurrentDays": 10}
    assert "alarm" not in _st(cfg) and sent == []
    assert lc.outstanding(cfg, BASE) == (False, [])


def test_a_stale_read_after_a_killed_confirmed_put_never_writes_the_old_rule_with_a_new_change(cfg, sent):
    # The same kill, and the owner lengthens manga (keeps more) before the next check, which reads
    # the pre-kill rules: the pass writes the new change on top of the CONFIRMED undo 10 -- never 30.
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)
    fake.stale_next("rules", 1)
    _set_manga(cfg, {"type": "days", "days": 365})
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10} and _manga(fake.rules[BASE]) == 365
    assert _appd(_applied(cfg)) == {"NoncurrentDays": 10} and _manga(_applied(cfg)) == 365
    assert "alarm" not in _st(cfg) and sent == [] and not _journal_file(cfg).exists()
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok" and len(fake.puts()) == puts


def test_a_killed_pass_whose_put_never_landed_waits_out_the_window_then_is_judged_as_before(cfg, sent):
    # The journal's pre-write state can also be S3's REAL state (killed before the put reached S3):
    # settling for the window, then exactly as before -- the unconfirmed-in-S3 change waits again.
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)
    fake.rules[BASE] = fake.before_put[(BASE, "rules")]               # the put never reached S3
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg) and _journal_file(cfg).exists()
    _age_writes(cfg, lc.SETTLE_S + 60)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert len(fake.puts()) == puts and not _journal_file(cfg).exists() and "alarm" not in _st(cfg)
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 30}
    assert [c.rule_id for c in lc.outstanding(cfg, BASE)[1]] == ["backup-engine:appdata/"]


# --- 4 / 5. real tampering inside the window ---------------------------------------------------

def test_an_outside_change_to_neither_state_inside_the_window_is_restored_and_alarmed(cfg, sent):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    _live(fake, MANGA)["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}      # neither 180 nor 365
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert _manga(fake.rules[BASE]) == 365 and _st(cfg)["alarm"]["kind"] == "restored"
    assert len(sent) == 1


def test_an_outside_change_back_to_the_exact_pre_write_rules_waits_for_the_window_then_is_restored(cfg, sent):
    # The accepted trade-off (ruling: detection delayed <= 15 min + one check).
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    _live(fake, MANGA)["NoncurrentVersionExpiration"] = {"NoncurrentDays": 180}    # exactly the pre-write rules
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg)
    assert len(fake.puts()) == puts and sent == []
    _age_writes(cfg, lc.SETTLE_S + 60)
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert _manga(fake.rules[BASE]) == 365 and _st(cfg)["alarm"]["kind"] == "restored" and len(sent) == 1


# The window, pinned at the ruling's number (15 minutes -- not derived from SETTLE_S, so a changed
# constant fails here): the same stale read just inside and just outside it.
@pytest.mark.parametrize("age_s,settles", [(15 * 60 - 60, True), (15 * 60 + 60, False)])
def test_the_settle_window_is_fifteen_minutes(cfg, sent, age_s, settles):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    _age_writes(cfg, age_s)
    fake.stale_next("rules", 1)
    state = lc.check(cfg, BASE, run=fake)
    if settles:
        assert state == "ok" and _settling(cfg) and sent == []
    else:
        assert state == "restored" and _st(cfg)["alarm"]["kind"] == "restored" and len(sent) == 1


# --- 6. versioning --------------------------------------------------------------------------------

def _confirmed_suspend(cfg, fake):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}})
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _ver(fake) == "Suspended"


def test_stale_enabled_reads_after_a_confirmed_suspend_are_settling_not_a_re_suspend_loop(cfg, sent):
    fake = FakeS3()
    _confirmed_suspend(cfg, fake)
    fake.stale_next("versioning", 3)
    vputs = len(_vputs(fake))
    for _ in range(3):
        assert lc.check(cfg, BASE, run=fake) == "ok"
        assert lc.load_live(cfg["CACHE_DIR"], BASE)["versioning"] == "on" and _settling(cfg)
    assert lc.check(cfg, BASE, run=fake) == "ok" and lc.SETTLING not in _st(cfg)["detail"]
    assert len(_vputs(fake)) == vputs and _ver(fake) == "Suspended" and sent == []
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "suspended"


def test_versioning_back_on_outside_the_window_is_restored_and_alarmed(cfg, sent):
    fake = FakeS3()
    _confirmed_suspend(cfg, fake)
    _age_writes(cfg, lc.SETTLE_S + 60)
    fake.versioning[BASE] = "Enabled"
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert _ver(fake) == "Suspended" and "versioning: was suspended, now on" in _st(cfg)["alarm"]["lines"]
    assert len(sent) == 1


def test_a_stale_read_after_a_killed_confirmed_suspend_keeps_the_journal(cfg, sent, monkeypatch):
    fake = Fake2()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}})

    def killed(*a, **k):
        raise Killed()
    with monkeypatch.context() as m:
        m.setattr(lc, "save_applied", killed)                         # killed between the put and the record
        with pytest.raises(Killed):
            lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _ver(fake) == "Suspended" and lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "on"
    fake.stale_next("versioning", 1)
    vputs = len(_vputs(fake))
    assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg)
    assert len(_vputs(fake)) == vputs and _journal_file(cfg).exists()
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "on"      # counted as landed, never recorded
    assert lc.check(cfg, BASE, run=fake) == "ok"                     # fresh: the journal is adopted
    assert len(_vputs(fake)) == vputs and _ver(fake) == "Suspended" and not _journal_file(cfg).exists()
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "suspended"
    assert "alarm" not in _st(cfg) and sent == []


# --- more than one write inside the window; the other half; new changes ------------------------

def test_a_read_of_the_state_before_an_earlier_write_inside_the_window_is_settling_too(cfg, sent):
    fake = FakeS3()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)                                      # manga 180
    at_180 = json.loads(json.dumps(fake.rules[BASE]))
    for days in (365, 400):                                           # two job saves, one after the other
        _set_manga(cfg, {"type": "days", "days": days})
        lc.sync(cfg, BASE, run=fake)
    fake.stale_next("rules", 1, old=at_180)                           # a front end still two writes behind
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg)
    assert len(fake.puts()) == puts and sent == []


def test_a_settling_half_never_hides_real_tampering_of_the_other_half(cfg, sent):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    fake.stale_next("rules", 1)                                       # rules: a stale read (settling)
    fake.versioning[BASE] = "Suspended"                               # versioning: suspended outside
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert len(fake.puts()) == puts and _ver(fake) == "Enabled"       # only the tampered half is written
    alarm = _st(cfg)["alarm"]
    assert alarm["kind"] == "restored" and alarm["lines"] == ["versioning: was on, now suspended"]
    assert lc.SETTLING in _st(cfg)["detail"] and len(sent) == 1


def test_a_new_change_made_inside_the_window_is_still_written(cfg, sent):
    # A second job save whose own sync reads the rules from before the first one.
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    fake.stale_next("rules", 1)
    _set_manga(cfg, {"type": "days", "days": 400})
    res = lc.sync(cfg, BASE, run=fake)
    assert res.changed and res.state == "ok"
    assert _manga(fake.rules[BASE]) == 400 and _manga(_applied(cfg)) == 400
    assert "alarm" not in _st(cfg) and sent == []
    for before in (180, 365):                                         # either earlier state, read stale
        fake.stale_next("rules", 1, old=[dict(r, NoncurrentVersionExpiration={"NoncurrentDays": before})
                                          if r["ID"] == MANGA else r for r in fake.rules[BASE]])
        assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg)


def test_a_confirmed_apply_that_reads_stale_still_writes_what_the_owner_confirmed(cfg, sent):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.stale_next("rules", 1)                                       # the confirm's own read is stale
    res = lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert res.changed
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10} and _manga(fake.rules[BASE]) == 365
    assert _appd(_applied(cfg)) == {"NoncurrentDays": 10}
    assert lc.outstanding(cfg, BASE) == (False, []) and "alarm" not in _st(cfg) and sent == []


def test_a_settling_pass_does_not_heal_an_open_not_restored_alarm(cfg, sent):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    lc.set_status(cfg["CACHE_DIR"], BASE, "not_restored", "x",
                  {"kind": "not_restored", "at": lc._now_iso(), "lines": ["media/manga/: was 180"]})
    fake.stale_next("rules", 1)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _st(cfg)["alarm"]["kind"] == "not_restored"                # S3 hasn't settled: nothing is proven yet
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _st(cfg)["alarm"]["kind"] == "restored"                    # a settled "ok" heals it, as before


def test_every_write_on_record_keeps_the_state_before_it(cfg):
    fake = FakeS3()
    _job_saved_to_365(cfg, fake)
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert doc["written_at"] == doc["previous"][-1]["written_at"]
    assert _manga(doc["previous"][-1]["rules"]) == 180                # the rules S3 had before the 365 put
    assert "versioning" not in doc["previous"][-1]                    # versioning wasn't written
    checked = dict(doc)
    lc.check(cfg, BASE, run=fake)                                     # a pass that writes nothing keeps it
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["previous"] == checked["previous"]
    _age_writes(cfg, lc.SETTLE_S + 60)
    _set_manga(cfg, {"type": "days", "days": 400})
    lc.sync(cfg, BASE, run=fake)
    doc = lc.load_applied_doc(cfg["CACHE_DIR"], BASE)
    assert [_manga(e["rules"]) for e in doc["previous"]] == [365]     # writes past the window are dropped


def test_the_journal_keeps_the_state_before_its_write(cfg):
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)
    inf = json.loads(_journal_file(cfg).read_text())
    assert _appd(inf["previous"][-1]["rules"]) == {"NoncurrentDays": 30}
    assert inf["previous"][-1]["written_at"] == inf["written_at"]


# --- a put that reported failure is not a write S3 may still be applying -----------------------

def test_a_rules_put_that_failed_is_retried_by_the_next_check_not_taken_for_settling(cfg, sent):
    fake = FakeS3()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)
    _set_manga(cfg, {"type": "days", "days": 365})
    fake.deny_put = True
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)                                  # the job save's own sync fails
    assert _journal_file(cfg).exists()                                # kept: an errored put may still land
    inf = json.loads(_journal_file(cfg).read_text())                  # ...but what it replaced is no settle
    assert not [e for e in inf.get("previous", []) if any(r["ID"] == MANGA for r in e.get("rules") or [])]
    fake.deny_put = False
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _manga(fake.rules[BASE]) == 365 and lc.SETTLING not in _st(cfg)["detail"]


def test_a_versioning_put_that_failed_is_retried_by_the_next_check_not_taken_for_settling(cfg, sent):
    fake = Fake2(versioning={BASE: None})                             # never versioned: the app turns it on
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake.put_ver = "fail"
    with pytest.raises(lc.LifecycleError):
        lc.sync(cfg, BASE, run=fake)                                  # rules land, the versioning put fails
    inf = json.loads(_journal_file(cfg).read_text())
    assert all("versioning" not in e for e in inf["previous"])        # only what landed stays a candidate
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["previous"][-1].get("rules") is not None
    fake.put_ver = None
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _ver(fake) == "Enabled" and lc.SETTLING not in _st(cfg)["detail"]


# --- the settle entries travel with the write they belong to -------------------------------------

def test_a_stale_read_from_before_an_earlier_write_keeps_a_killed_passs_journal(cfg, sent):
    # A job save (180 -> 365, recorded), then a confirmed undo 30 -> 10 killed after its put lands.
    # A front end still serving the rules from before the job save must keep that journal too --
    # else the gated pass drops it, and the next fresh read looks like tampering and reverts 10.
    fake = Fake2()
    _job_saved_to_365(cfg, fake)
    at_180 = fake.before_put[(BASE, "rules")]
    pv = lc.preview(cfg, BASE, {"kind": "settings",
                               "settings": {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 10}}}}}})
    fake.put_rules = "kill_after"
    with pytest.raises(Killed):
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    fake.put_rules = None
    fake.stale_next("rules", 1, old=at_180)
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg)
    assert _journal_file(cfg).exists() and len(fake.puts()) == puts
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # fresh: adopted
    assert _appd(fake.rules[BASE]) == {"NoncurrentDays": 10} and _appd(_applied(cfg)) == {"NoncurrentDays": 10}
    assert len(fake.puts()) == puts and "alarm" not in _st(cfg) and sent == [] and not _journal_file(cfg).exists()


def test_an_adopted_journals_pre_write_state_is_still_settling_afterwards(cfg, sent):
    fake = _confirmed_undo_10_killed_after_the_put_lands(cfg)
    assert lc.check(cfg, BASE, run=fake) == "ok"                      # fresh: the journal is adopted
    assert not _journal_file(cfg).exists() and _appd(_applied(cfg)) == {"NoncurrentDays": 10}
    fake.stale_next("rules", 1)                                       # ...then S3 serves undo 30 once more
    puts = len(fake.puts())
    assert lc.check(cfg, BASE, run=fake) == "ok" and _settling(cfg)
    assert len(fake.puts()) == puts and sent == []
