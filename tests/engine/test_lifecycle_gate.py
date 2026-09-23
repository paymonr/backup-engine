# tests/engine/test_lifecycle_gate.py — keeps more / keeps less and the gate inside the pass
# (spec 2026-09-23 §2, §8; rulings R-B1, R-B2, R-B2', R-B3, R-B6, O1).
import json
from pathlib import Path

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import (BASE, CONSOLE, LEGACY, FakeS3, _events,  # noqa: F401
                                              _live, _set_manga, cfg)

M = "media/manga/"


def _rs(rules, folders):
    return lc.RuleSet({r["ID"]: r for r in rules if r}, frozenset(folders))


def _ran(cfg, *job_names):
    """R-B2': mark a job as having run -- CACHE_DIR/state/<job>.json exists (what
    backup-job.sh writes at the end of every run) -- so its folder is "known" on a
    bucket's first S3 rules apply."""
    for name in job_names:
        Path(cfg["CACHE_DIR"], "state", f"{name}.json").write_text("{}")


@pytest.mark.parametrize("before,after,kind", [
    ({"type": "days", "days": 30}, {"type": "days", "days": 180}, lc.KEEPS_MORE),           # longer
    ({"type": "days", "days": 180}, {"type": "days", "days": 30}, lc.KEEPS_LESS),           # shorter
    ({"type": "days", "days": 30}, {"type": "keep_all"}, lc.KEEPS_MORE),                    # limit removed
    ({"type": "keep_all"}, {"type": "days", "days": 365}, lc.KEEPS_LESS),                   # expiry added
    ({"type": "count", "count": 10}, {"type": "count", "count": 5}, lc.KEEPS_LESS),         # newest-N lowered
    ({"type": "count", "count": 5}, {"type": "count", "count": 10}, lc.KEEPS_MORE),         # newest-N raised
    ({"type": "days", "days": 180}, {"type": "count", "count": 10}, lc.KEEPS_LESS),         # 180 days -> 1 day + newest 10
    ({"type": "count", "count": 10, "days": 30}, {"type": "days", "days": 30}, lc.KEEPS_LESS),   # newest-N removed
    ({"type": "days", "days": 30}, {"type": "count", "count": 10, "days": 30}, lc.KEEPS_MORE),   # newest-N added, same days
    ({"type": "count", "count": 10}, {"type": "days", "days": 180}, lc.KEEPS_LESS),         # ranks 2..10 now go at 180 days
])
def test_classify_plain_copy_changes(before, after, kind):
    (c,) = lc.classify(_rs([lc.plain_rule(M, before)], [M]), _rs([lc.plain_rule(M, after)], [M]))
    assert (c.rule_id, c.folder, c.kind) == ("backup-engine:media/manga/", M, kind)


def test_undo_windows_and_housekeeping():
    hk = lc.housekeeping_rule(lc.bucket_settings({}, BASE))
    before = _rs([lc.undo_rule("appdata/", 30), hk], ["appdata/"])
    other_hk = lc.housekeeping_rule(lc.bucket_settings(
        {"buckets": {BASE: {"abort_uploads_days": 1, "delete_marker_cleanup": False}}}, BASE))
    kinds = {c.rule_id: c.kind for c in lc.classify(before, _rs([lc.undo_rule("appdata/", 60), other_hk], ["appdata/"]))}
    assert kinds == {"backup-engine:appdata/": lc.KEEPS_MORE, "backup-engine:housekeeping": lc.KEEPS_MORE}
    (c,) = lc.classify(before, _rs([lc.undo_rule("appdata/", 7), hk], ["appdata/"]))
    assert c.kind == lc.KEEPS_LESS
    assert c.words == ("appdata/: old versions removed 30 days after being replaced → "
                       "old versions removed 7 days after being replaced")


def test_keeps_words_agrees_with_describes_wording_for_the_combined_form():
    # Minor (fix round 1): _keeps_words() used ", " where describe() uses "; " for the same
    # "newest N ... older ones removed D days ..." phrase -- they must agree (one wording).
    before = lc.plain_rule(M, {"type": "count", "count": 10, "days": 30})
    after = lc.plain_rule(M, {"type": "count", "count": 5, "days": 45})
    (c,) = lc.classify(_rs([before], [M]), _rs([after], [M]))
    assert c.words == (
        "media/manga/: newest 10 old versions of each file kept; older ones removed 30 days after being replaced → "
        "newest 5 old versions of each file kept; older ones removed 45 days after being replaced")
    assert lc.describe(before).split(": ", 1)[1] in c.words


def test_a_new_jobs_folder_always_keeps_more():
    (c,) = lc.classify(_rs([], []), _rs([lc.plain_rule(M, {"type": "days", "days": 1})], [M]))
    assert c.kind == lc.KEEPS_MORE


def test_a_deleted_jobs_rule_going_away_keeps_more():
    (c,) = lc.classify(_rs([lc.plain_rule(M, {"type": "days", "days": 30})], [M]), _rs([], []))
    assert c.kind == lc.KEEPS_MORE and c.after is None


def test_the_same_rule_in_s3s_formatting_is_no_change():
    r = lc.plain_rule(M, {"type": "days", "days": 30})
    s3_form = json.loads(json.dumps({"Status": "Enabled", "Filter": {"Prefix": M}, "ID": r["ID"],
                                     "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}))
    assert lc.classify(_rs([r], [M]), _rs([s3_form], [M])) == []


def test_gate_holds_only_the_folders_that_keep_less():
    hk = lc.housekeeping_rule(lc.bucket_settings({}, BASE))
    before = _rs([lc.plain_rule(M, {"type": "days", "days": 180}), lc.undo_rule("appdata/", 30), hk],
                 [M, "appdata/"])
    after = _rs([lc.plain_rule(M, {"type": "days", "days": 30}), lc.undo_rule("appdata/", 60),
                 lc.plain_rule("media/new/", {"type": "days", "days": 7}), hk], [M, "appdata/", "media/new/"])
    t = lc.gate(before, after)
    assert t.rules["backup-engine:media/manga/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert t.rules["backup-engine:appdata/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 60}
    assert t.rules["backup-engine:media/new/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}
    assert list(t.rules)[-1] == "backup-engine:housekeeping" and t.folders == after.folders


def test_gate_keeps_a_known_folder_without_a_rule_until_confirmed():
    assert lc.gate(_rs([], [M]), _rs([lc.plain_rule(M, {"type": "days", "days": 30})], [M])).rules == {}


def test_first_apply_baseline_maps_the_legacy_backstops_to_folders():
    want = lc.RuleSet({}, frozenset({"appdata/", M, "media/documents/"}))
    app_rule = {"ID": "backup-engine:media/manga/", "Status": "Enabled", "Filter": {"Prefix": M},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 7}}
    b = lc.baseline_from_live(LEGACY + [CONSOLE, app_rule], want)
    assert b.rules["backup-engine:appdata/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert b.rules["backup-engine:media/documents/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert b.rules["backup-engine:media/manga/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}  # the stricter
    assert "AbortIncompleteMultipartUpload" not in b.rules["backup-engine:appdata/"]
    assert not any(r.get("ID") == "archive-old-logs" for r in b.rules.values())
    assert b.folders == want.folders


def test_first_apply_baseline_on_a_dedicated_bucket_ignores_switched_off_rules():
    want = lc.RuleSet({}, frozenset({""}))
    legacy = {"ID": "backup-engine", "Status": "Enabled", "Filter": {"Prefix": ""},
              "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}
    assert lc.baseline_from_live([legacy], want).rules["backup-engine:bucket"]["NoncurrentVersionExpiration"] == \
        {"NoncurrentDays": 30}
    assert lc.baseline_from_live([dict(legacy, Status="Disabled")], want).rules == {}


def test_older_applied_state_counts_every_current_folder_as_known():
    want = lc.RuleSet({}, frozenset({M}))
    assert lc.baseline_from_applied({"rules": []}, want).folders == frozenset({M})
    assert lc.baseline_from_applied({"rules": [], "folders": []}, want).folders == frozenset()
    assert lc.baseline_from_applied(None, want) is None


# --- R-B2': on a FIRST apply a folder is known only once one of its jobs has run -----------

def test_baseline_from_live_known_none_defaults_to_every_current_folder():
    want = lc.RuleSet({}, frozenset({M}))
    assert lc.baseline_from_live([], want).folders == frozenset({M})


def test_baseline_from_live_known_restricts_which_folders_gate():
    want = lc.RuleSet({}, frozenset({M, "appdata/"}))
    assert lc.baseline_from_live([], want, frozenset({M})).folders == frozenset({M})


def test_a_job_that_never_ran_is_not_known_on_a_first_apply(cfg):
    # A Plain copy job with a `days` retention whose job NEVER ran: no rule waits for
    # confirmation -- its folder is new, so the rule is written at once.
    fake = FakeS3()
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert res.waiting == []


def test_a_job_that_has_run_is_known_on_a_first_apply(cfg):
    # Same job, but it HAS run (state/manga.json exists): a shorter setting than what a
    # live legacy rule already protects waits for confirmation.
    _ran(cfg, "manga")
    _set_manga(cfg, {"type": "days", "days": 7})
    fake = FakeS3({BASE: LEGACY})                             # backstop-media: 30 days
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert [c.folder for c in res.waiting] == [M]


def test_the_owners_migration_case_only_holds_the_job_that_has_run(cfg):
    # legacy backstop-media 30 days live; a Plain copy job with days:7 that HAS run waits
    # (the old 30-day rule stays for that folder); one with days:180 that never ran applies.
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"][0]["retention"] = {"type": "days", "days": 7}          # manga: has run
    data["jobs"].append({"name": "comics", "type": "archive", "source": "media/comics",
                         "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
                         "retention": {"type": "days", "days": 180}})    # comics: never ran
    jobs_p.write_text(json.dumps(data))
    _ran(cfg, "manga")
    fake = FakeS3({BASE: LEGACY})
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert _live(fake, "backup-engine:media/comics/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert [c.folder for c in res.waiting] == [M]


def test_a_job_that_has_run_and_is_genuinely_longer_applies_without_waiting(cfg):
    # I3 (fix round 1): the controller-required case -- legacy backstop-media 30d live, a
    # Plain copy job that HAS run with days:180 (longer than the legacy backstop): applied
    # at once, not waiting -- proves the gate on a KNOWN folder, not just the "unknown
    # folder always applies" shortcut.
    _ran(cfg, "manga")
    fake = FakeS3({BASE: LEGACY})
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert res.waiting == []


def test_has_run_also_counts_a_runs_jsonl_with_no_json_yet(cfg):
    # Minor (fix round 1): a first run still in progress (or one that was only ever paused)
    # writes state/<job>.runs.jsonl before state/<job>.json exists -- that must count too.
    Path(cfg["CACHE_DIR"], "state", "manga.runs.jsonl").write_text('{"event": "start"}\n')
    _set_manga(cfg, {"type": "days", "days": 7})
    fake = FakeS3({BASE: LEGACY})
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert [c.folder for c in res.waiting] == [M]


def test_stricter_never_hides_a_shrink_behind_an_incomparable_pair(cfg):
    # Minor (fix round 1): legacy 30d (no newest limit) + a live app rule (1 day, newest 10)
    # on the same folder -- neither contains the other, so combining them by per-dimension
    # min (the old bug) would claim (1 day, no newest limit) is already live, which HIDES a
    # real shrink to a plain 7-day rule. Picking one rule (the app's) instead never hides it.
    _ran(cfg, "manga")
    _set_manga(cfg, {"type": "days", "days": 7})
    app_rule = {"ID": "backup-engine:media/manga/", "Status": "Enabled", "Filter": {"Prefix": M},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 10}}
    fake = FakeS3({BASE: LEGACY + [app_rule]})
    res = lc.sync(cfg, BASE, run=fake)
    assert [c.folder for c in res.waiting] == [M]
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": 1, "NewerNoncurrentVersions": 10}                # held: S3 keeps the live rule


# --- I1: a deleted job's folder is never forgotten (fix round 1) ---------------------------

def test_a_deleted_jobs_folder_stays_recorded_though_its_rule_is_gone(cfg):
    _ran(cfg, "manga")
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)                               # manga: 180 days applied
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"].pop(0)                                        # delete manga
    jobs_p.write_text(json.dumps(data))
    res = lc.sync(cfg, BASE, run=fake)
    assert res.waiting == []                                   # a deleted job's rule going away keeps more
    assert "backup-engine:media/manga/" not in {r["ID"] for r in fake.rules[BASE]}
    assert M in lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["folders"]     # not forgotten


def test_recreating_a_deleted_jobs_folder_under_a_new_type_waits_for_confirmation(cfg):
    # I1 repro: manga Plain copy 180d applied -> job deleted -> re-created as File history
    # (the only way to change a job's type) -- its 30-day undo rule must wait: S3's folder
    # was left with NO rule (keep everything) when the old job was deleted, so a fresh
    # 30-day undo rule is a real shrink, not a new job's folder.
    _ran(cfg, "manga")
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)                               # manga: 180 days applied
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    manga = data["jobs"].pop(0)
    jobs_p.write_text(json.dumps(data))
    lc.sync(cfg, BASE, run=fake)                               # job deleted: rule gone, folder remembered
    data = json.loads(jobs_p.read_text())
    manga = dict(manga, type="versioned-files", retention_days="90")
    manga.pop("retention", None)
    data["jobs"].insert(0, manga)                              # same name, new type (type is locked in the UI)
    jobs_p.write_text(json.dumps(data))
    res = lc.sync(cfg, BASE, run=fake)
    assert [c.folder for c in res.waiting] == [M]
    assert "backup-engine:media/manga/" not in {r["ID"] for r in fake.rules[BASE]}    # S3 still has no rule there


# --- the pass ------------------------------------------------------------------------------

def test_the_spec_migration_applies_longer_plain_copy_history_automatically(cfg):
    _ran(cfg, "manga", "appdata_backups")                    # I3: exercise the gate on KNOWN folders
    fake = FakeS3({BASE: LEGACY})                            # manga 180 > the 30-day backstop
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert res.waiting == []
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["folders"] == ["appdata/", M]


def test_a_first_apply_holds_what_keeps_less_and_applies_the_rest(cfg):
    _ran(cfg, "manga")                                        # R-B2': manga's folder is known
    _set_manga(cfg, {"type": "days", "days": 7})
    fake = FakeS3({BASE: LEGACY})
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert [c.folder for c in res.waiting] == [M]
    assert any("Waiting for your confirmation" in line for line in res.lines)
    not_reached, waiting = lc.outstanding(cfg, BASE)
    assert not_reached is False and [(c.folder, c.kind) for c in waiting] == [(M, lc.KEEPS_LESS)]


def test_a_held_change_is_never_applied_by_a_later_pass(cfg):
    _ran(cfg, "manga")                                        # R-B2': manga's folder is known
    _set_manga(cfg, {"type": "days", "days": 7})
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert len(fake.puts()) == 1


def test_a_bucket_without_rules_holds_every_known_folder(cfg):
    _ran(cfg, "manga", "appdata_backups")                     # R-B2': both folders are known
    fake = FakeS3()                                          # nothing applied, nothing live
    res = lc.sync(cfg, BASE, run=fake)
    assert [r["ID"] for r in fake.rules[BASE]] == ["backup-engine:housekeeping"]
    assert sorted(c.folder for c in res.waiting) == ["appdata/", M]


def test_a_bucket_the_app_created_applies_everything(cfg):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake = FakeS3()
    res = lc.sync(cfg, BASE, run=fake)
    assert {r["ID"] for r in fake.rules[BASE]} == {"backup-engine:media/manga/", "backup-engine:appdata/",
                                                  "backup-engine:housekeeping"}
    assert res.waiting == [] and res.state == "ok"
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)               # never overwrites a real record
    assert lc.load_applied_doc(cfg["CACHE_DIR"], BASE)["rules"] != []


def test_a_new_job_after_the_first_apply_applies_without_confirmation(cfg):
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"].append({"name": "comics", "type": "archive", "source": "media/comics", "schedule": "0 3 * * *",
                         "enabled": True, "storage_class": "STANDARD",
                         "retention": {"type": "days", "days": 1}})
    jobs_p.write_text(json.dumps(data))
    res = lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/comics/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 1}
    assert res.waiting == []


def test_keep_everything_to_days_on_a_known_folder_waits(cfg):
    _set_manga(cfg, {"type": "keep_all"})
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)                             # keep everything: the rule goes (keeps more)
    assert "backup-engine:media/manga/" not in {r["ID"] for r in fake.rules[BASE]}
    _set_manga(cfg, {"type": "days", "days": 365})
    res = lc.sync(cfg, BASE, run=fake)
    assert [c.folder for c in res.waiting] == [M]
    assert "backup-engine:media/manga/" not in {r["ID"] for r in fake.rules[BASE]}


def test_a_legacy_backstop_longer_than_30_days_seeds_the_undo_window(cfg):
    _ran(cfg, "manga", "appdata_backups")                    # I3: exercise the gate on KNOWN folders
    long = json.loads(json.dumps(LEGACY))
    for r in long:
        r["NoncurrentVersionExpiration"] = {"NoncurrentDays": 90}
    fake = FakeS3({BASE: long})
    res = lc.sync(cfg, BASE, run=fake)
    assert lc.load_settings(cfg["CONFIG_DIR"])["buckets"][BASE]["folders"]["appdata/"]["undo_days"] == 90
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 90}
    assert res.waiting == []                                 # appdata 90 -> 90; manga 90 -> 180


def test_seed_undo_days_holds_the_settings_lock_during_its_read_modify_write(cfg, monkeypatch):
    # Minor (fix round 1): storage.json's read-modify-write in seed_undo_days must be
    # guarded by settings_lock() like the other state files' locks.
    import fcntl

    def _is_locked(path):
        with open(path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False

    lock_path = Path(cfg["CONFIG_DIR"], ".storage.json.lock")
    seen = []
    real_save = lc.save_settings

    def spy_save(config_dir, data):
        seen.append(_is_locked(lock_path))
        return real_save(config_dir, data)
    monkeypatch.setattr(lc, "save_settings", spy_save)

    long = json.loads(json.dumps(LEGACY))
    for r in long:
        r["NoncurrentVersionExpiration"] = {"NoncurrentDays": 90}
    lc.sync(cfg, BASE, run=FakeS3({BASE: long}))
    assert seen == [True]                                    # locked while save_settings ran
    assert not _is_locked(lock_path)                          # released afterwards


def test_the_undo_seed_never_overrides_the_owners_own_setting(cfg):
    _ran(cfg, "appdata_backups")                              # R-B2': appdata/ is known
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 14}}}}})
    long = json.loads(json.dumps(LEGACY))
    for r in long:
        r["NoncurrentVersionExpiration"] = {"NoncurrentDays": 90}
    res = lc.sync(cfg, BASE, run=FakeS3({BASE: long}))
    assert lc.load_settings(cfg["CONFIG_DIR"])["buckets"][BASE]["folders"]["appdata/"]["undo_days"] == 14
    assert [c.folder for c in res.waiting] == ["appdata/"]  # 90 -> 14 keeps less: held


def test_a_first_apply_names_existing_console_rules_that_can_delete_backups(cfg):
    fake = FakeS3({BASE: LEGACY + [CONSOLE]})
    lc.sync(cfg, BASE, run=fake)
    start = [e for e in _events(cfg) if e["event"] == "start"][-1]
    log = Path(cfg["CACHE_DIR"], start["log"]).read_text()
    assert ("Kept as it is — a rule added in the AWS console: archive-old-logs (logs/): "
            "expires current files 14 days after they're written") in log
    assert "alarm" not in lc.load_status(cfg["CACHE_DIR"])[BASE]


def test_folder_of_job():
    jobs = [{"name": "manga", "type": "archive"},
            {"name": "photos", "type": "archive", "dedicated": True, "bucket": f"{BASE}-photos"},
            {"name": "appdata_backups", "type": "versioned"}]
    assert lc.folder_of_job(BASE, jobs, "manga") == (BASE, M)
    assert lc.folder_of_job(BASE, jobs, "photos") == (f"{BASE}-photos", "")
    assert lc.folder_of_job(BASE, jobs, "appdata_backups") == (BASE, "appdata/")
    assert lc.folder_of_job(BASE, jobs, "nope") is None
