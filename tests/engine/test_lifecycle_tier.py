# tests/engine/test_lifecycle_tier.py — the cheaper tier for old versions (spec 2026-09-23 §1, §2, §3; #9).
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.engine import lifecycle as lc, storage_summary
from tests.engine.test_lifecycle_sync import BASE, FakeS3, _live, _set_manga, cfg  # noqa: F401

M = "media/manga/"
DA30 = {"class": "DEEP_ARCHIVE", "after_days": 30}


def _tiered(cfg, tier, folder=M):
    lc.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"folders": {folder: {"tier": tier}}}}})


def test_a_tier_is_added_to_the_folder_rule():
    rule = lc.with_tier(M, lc.plain_rule(M, {"type": "days", "days": 180}), DA30)
    assert rule["NoncurrentVersionTransitions"] == [{"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE"}]
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    only = lc.with_tier(M, None, DA30)                     # keep everything + a tier
    assert only["ID"] == "backup-engine:media/manga/" and "NoncurrentVersionExpiration" not in only


def test_a_tier_that_could_not_move_anything_is_left_off():
    thirty = lc.plain_rule(M, {"type": "days", "days": 30})
    assert lc.with_tier(M, thirty, {"class": "DEEP_ARCHIVE", "after_days": 30}) == thirty
    newest = lc.plain_rule(M, {"type": "count", "count": 5})           # removed after 1 day
    assert lc.with_tier(M, newest, DA30) == newest
    assert lc.tier_error({"class": "DEEP_ARCHIVE", "after_days": 30}, thirty).startswith(
        "Old versions must move before S3 removes them")
    assert lc.tier_error({"class": "STANDARD_IA", "after_days": 30}, None) == \
        "Pick Glacier Instant Retrieval or Deep Archive."
    assert lc.tier_error({"class": "GLACIER_IR", "after_days": 0}, None) == "Move old versions after 1 day or more."
    assert lc.tier_error(DA30, lc.plain_rule(M, {"type": "days", "days": 180})) is None


# --- fix round 1, M1: exact wording (_days(...), and the impossible "before 1 day" case) -------

def test_tier_error_wording_for_one_day_and_newest_n_only():
    one_day = lc.plain_rule(M, {"type": "days", "days": 1})
    assert lc.tier_error(DA30, one_day) == \
        "Old versions must move before S3 removes them — move them before 1 day, or keep them longer."
    newest_only = lc.plain_rule(M, {"type": "count", "count": 5})     # NoncurrentDays 1, no days limit
    assert lc.tier_error(DA30, newest_only) == "Add a days limit to use a cheaper tier."


def test_desired_rules_carry_the_folders_tier(cfg):
    _tiered(cfg, DA30)
    jobs = json.loads(open(f"{cfg['CONFIG_DIR']}/jobs.json").read())["jobs"]
    rules = {r["ID"]: r for r in lc.desired_rules(BASE, BASE, jobs, lc.load_settings(cfg["CONFIG_DIR"]))}
    assert rules["backup-engine:media/manga/"]["NoncurrentVersionTransitions"] == [
        {"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE"}]
    assert "NoncurrentVersionTransitions" not in rules["backup-engine:appdata/"]


@pytest.mark.parametrize("before,after,kind", [
    (None, DA30, lc.KEEPS_LESS),                                            # tier added
    (DA30, {"class": "DEEP_ARCHIVE", "after_days": 10}, lc.KEEPS_LESS),     # moved earlier
    (DA30, {"class": "GLACIER_IR", "after_days": 30}, lc.KEEPS_LESS),       # another class
    (DA30, {"class": "DEEP_ARCHIVE", "after_days": 60}, lc.KEEPS_MORE),     # later
    (DA30, None, lc.KEEPS_MORE),                                            # removed
])
def test_classify_tier_changes(before, after, kind):
    base = lc.plain_rule(M, {"type": "days", "days": 180})
    b = lc.RuleSet({"backup-engine:media/manga/": lc.with_tier(M, base, before)}, frozenset({M}))
    a = lc.RuleSet({"backup-engine:media/manga/": lc.with_tier(M, base, after)}, frozenset({M}))
    (c,) = lc.classify(b, a)
    assert c.kind == kind


def test_the_words_name_the_tier():
    # fix round 1, M9: owner words, never the raw "· DEEP_ARCHIVE" constant in running text.
    rule = lc.with_tier(M, lc.plain_rule(M, {"type": "days", "days": 180}), DA30)
    assert lc._keeps_words(rule) == ("old versions removed 180 days after being replaced; moved to "
                                     "Deep Archive 30 days after being replaced")


def test_with_tier_on_the_combined_newest_n_and_days_form():
    # fix round 1, M9.
    combined = lc.plain_rule(M, {"type": "count", "count": 5, "days": 90})
    rule = lc.with_tier(M, combined, {"class": "DEEP_ARCHIVE", "after_days": 30})
    assert rule["NoncurrentVersionTransitions"] == [{"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE"}]
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 90, "NewerNoncurrentVersions": 5}
    assert lc.with_tier(M, combined, {"class": "DEEP_ARCHIVE", "after_days": 90}) == combined


def test_adding_a_tier_waits_for_confirmation_and_needs_the_bucket_name(cfg):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake = FakeS3()
    lc.sync(cfg, BASE, run=fake)
    _tiered(cfg, DA30)
    res = lc.sync(cfg, BASE, run=fake)
    assert [c.folder for c in res.waiting] == [M]
    assert "NoncurrentVersionTransitions" not in _live(fake, "backup-engine:media/manga/")
    pv = lc.preview(cfg, BASE, {"kind": "confirm"})
    assert pv.needs_typed is True
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionTransitions"] == [
        {"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE"}]


# --- fix round 1, M2: needs_typed for a tier-only change, discriminated from a stale/missing
#     summary by seeding a FRESH summary that shows zero deletions -----------------------------

def test_a_tier_only_change_needs_typing_even_with_a_fresh_zero_impact_summary(cfg):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake = FakeS3()
    lc.sync(cfg, BASE, run=fake)                          # manga 180 days applied, no tier
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket": BASE, "folder": M, "noncurrent_by_age_days": [], "noncurrent_by_rank": [],
        "noncurrent_by_age_rank": [], "noncurrent_versions": 0, "noncurrent_bytes": 0,
        "delete_markers": 0, "current_objects": 0, "current_bytes": 0})
    _tiered(cfg, DA30)
    pv = lc.preview(cfg, BASE, {"kind": "confirm"})
    (c,) = pv.keeps_less
    assert pv.impacts[c.rule_id]["versions"] == 0            # a fresh summary, nothing to delete today
    assert pv.needs_typed is True                             # still needs typing -- it's a tier, not a deletion


# --- fix round 1, M3b: a tier that isn't colder than the folder's own upload class -------------

def test_a_tier_no_colder_than_the_jobs_own_class_is_refused_and_dropped(cfg):
    thirty = lc.plain_rule(M, {"type": "days", "days": 180})
    assert lc.with_tier(M, thirty, DA30, "DEEP_ARCHIVE") == thirty                          # same class
    assert lc.with_tier(M, thirty, {"class": "GLACIER_IR", "after_days": 30}, "GLACIER") == thirty  # warmer
    assert lc.tier_error(DA30, thirty, "DEEP_ARCHIVE") is not None
    assert lc.tier_error({"class": "GLACIER_IR", "after_days": 30}, thirty, "STANDARD") is None  # fine: colder

    jobs_path = f"{cfg['CONFIG_DIR']}/jobs.json"
    data = json.loads(open(jobs_path).read())
    data["jobs"][0]["storage_class"] = "DEEP_ARCHIVE"        # manga already uploads as Deep Archive
    open(jobs_path, "w").write(json.dumps(data))
    _tiered(cfg, DA30)
    rules = {r["ID"]: r for r in lc.desired_rules(BASE, BASE, data["jobs"], lc.load_settings(cfg["CONFIG_DIR"]))}
    assert "NoncurrentVersionTransitions" not in rules["backup-engine:media/manga/"]


# --- fix round 1, M5: first-apply baseline edge cases -------------------------------------------

def test_a_transition_preempted_by_its_own_expiry_counts_as_no_tier():
    dead = {"NoncurrentVersionExpiration": {"NoncurrentDays": 30},
            "NoncurrentVersionTransitions": [{"NoncurrentDays": 60, "StorageClass": "DEEP_ARCHIVE"}]}
    assert lc.tier_of(dead) is None                            # removed at 30 days; never reaches 60
    alive = {"NoncurrentVersionExpiration": {"NoncurrentDays": 90},
             "NoncurrentVersionTransitions": [{"NoncurrentDays": 60, "StorageClass": "DEEP_ARCHIVE"}]}
    assert lc.tier_of(alive) == ("DEEP_ARCHIVE", 60, 0)
    protected = {"NoncurrentVersionExpiration": {"NoncurrentDays": 30, "NewerNoncurrentVersions": 5},
                 "NoncurrentVersionTransitions": [{"NoncurrentDays": 60, "StorageClass": "DEEP_ARCHIVE"}]}
    assert lc.tier_of(protected) == ("DEEP_ARCHIVE", 60, 0)    # newest-N on the EXPIRY doesn't kill the tier


def test_a_tier_losing_its_own_newest_n_protection_keeps_less():
    protected_tier = {"NoncurrentVersionTransitions": [
        {"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE", "NewerNoncurrentVersions": 5}]}
    unprotected_tier = {"NoncurrentVersionTransitions": [
        {"NoncurrentDays": 30, "StorageClass": "DEEP_ARCHIVE"}]}
    assert lc.tier_of(protected_tier) == ("DEEP_ARCHIVE", 30, 5)
    assert lc.tier_keeps_less(protected_tier, unprotected_tier) is True    # loses its protection
    assert lc.tier_keeps_less(unprotected_tier, protected_tier) is False   # gains protection: keeps more


def test_an_overlapping_legacy_expiry_that_kills_a_live_tier_does_not_hide_a_real_one(cfg):
    # Reviewer's probe: backstop-media (30d, whole media/) and backup-engine:media/manga/ (180d +
    # DA 60) both live at once. The 60-day move never actually fires -- backstop-media removes
    # the version at 30 days first -- so the TRUE baseline has no working tier. Wanting the same
    # 180d + DA60 must be judged against that reality (a newly-WORKING tier), not the merged
    # rule's literal (dead) transition text, and so must wait for confirmation.
    (Path(cfg["CACHE_DIR"], "state")).mkdir(parents=True, exist_ok=True)
    (Path(cfg["CACHE_DIR"], "state") / "manga.json").write_text("{}")     # R-B2': manga has run
    live = [
        {"ID": "backstop-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
        {"ID": "backup-engine:media/manga/", "Status": "Enabled", "Filter": {"Prefix": "media/manga/"},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 180},
         "NoncurrentVersionTransitions": [{"NoncurrentDays": 60, "StorageClass": "DEEP_ARCHIVE"}]},
    ]
    fake = FakeS3({BASE: live})
    _set_manga(cfg, {"type": "days", "days": 180})
    _tiered(cfg, {"class": "DEEP_ARCHIVE", "after_days": 60})
    res = lc.sync(cfg, BASE, run=fake)
    assert [c.folder for c in res.waiting] == [M]
    # gated: whatever S3 was already enforcing (removal at 30 days; the 60-day move never
    # actually fires) stays exactly as effective as before -- nothing keeps-less applied
    # without confirmation, even though the merged rule's own fields look identical to what
    # was wanted.
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


class _SizedS3(FakeS3):
    """FakeS3 whose lifecycle configuration also reports the bucket's minimum-object-size
    setting, and whose `... help` probe reports whether this fake aws CLI understands
    --transition-default-minimum-object-size (fix round 1, #9)."""
    def __init__(self, *a, min_size=None, flag_supported=True, help_fails=False, **k):
        super().__init__(*a, **k)
        self.min_size, self.flag_supported, self.help_fails = min_size, flag_supported, help_fails
        self.help_calls = 0

    def __call__(self, args, **kw):
        if args[:3] == ["s3api", "put-bucket-lifecycle-configuration", "help"]:
            self.help_calls += 1
            if self.help_fails:
                return SimpleNamespace(returncode=255, stdout="", stderr="unknown command")
            text = ("--transition-default-minimum-object-size (string)\n" if self.flag_supported
                    else "some other help text\n")
            return SimpleNamespace(returncode=0, stdout=text, stderr="")
        cp = super().__call__(args, **kw)
        if args[:2] == ["s3api", "get-bucket-lifecycle-configuration"] and cp.returncode == 0 and self.min_size:
            data = json.loads(cp.stdout)
            data["TransitionDefaultMinimumObjectSize"] = self.min_size
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(data))
        return cp


def test_a_bucket_setting_for_small_objects_survives_every_put(cfg):
    fake = _SizedS3({BASE: [{"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}]},
                    min_size="varies_by_storage_class")
    lc.sync(cfg, BASE, run=fake)
    (put,) = fake.puts()
    assert put[put.index("--transition-default-minimum-object-size") + 1] == "varies_by_storage_class"


def test_the_default_small_object_setting_is_never_sent(cfg):
    fake = _SizedS3({BASE: [{"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}]},
                    min_size="all_storage_classes_128K")
    lc.sync(cfg, BASE, run=fake)
    assert all("--transition-default-minimum-object-size" not in c for c in fake.puts())


# --- fix round 1, controller ruling on #9: CLI support detection -------------------------------

def test_the_flag_is_never_sent_when_this_aws_cli_doesnt_support_it(cfg):
    fake = _SizedS3({BASE: [{"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}]},
                    min_size="varies_by_storage_class", flag_supported=False)
    lc.sync(cfg, BASE, run=fake)
    assert fake.help_calls == 1
    assert all("--transition-default-minimum-object-size" not in c for c in fake.puts())


def test_a_failed_probe_counts_as_unsupported(cfg):
    fake = _SizedS3({BASE: [{"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}]},
                    min_size="varies_by_storage_class", help_fails=True)
    lc.sync(cfg, BASE, run=fake)
    assert all("--transition-default-minimum-object-size" not in c for c in fake.puts())


def test_the_probe_runs_at_most_once_per_run(cfg):
    fake = _SizedS3({}, min_size="varies_by_storage_class")
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    _tiered(cfg, DA30)
    lc.sync(cfg, BASE, run=fake)                              # first write with a tier -- probes once
    assert fake.help_calls == 1
    _tiered(cfg, {"class": "DEEP_ARCHIVE", "after_days": 60})  # a further tier change -- another write
    lc.sync(cfg, BASE, run=fake)
    assert fake.help_calls == 1                                # still just the one probe for this run


def test_an_owner_words_note_when_the_cli_cant_control_small_objects(cfg):
    fake = _SizedS3({}, min_size="varies_by_storage_class", flag_supported=False)
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    _tiered(cfg, DA30)
    res = lc.sync(cfg, BASE, run=fake)
    assert any("small files" in l and "128 KB" in l for l in res.lines)
    assert "small files" in lc.load_status(cfg["CACHE_DIR"])[BASE]["detail"]


def test_no_small_object_note_without_a_tier(cfg):
    fake = _SizedS3({}, min_size="varies_by_storage_class", flag_supported=False)
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    res = lc.sync(cfg, BASE, run=fake)
    assert not any("small files" in l for l in res.lines)


# --- fix round 1, M8: the #9 reading is recorded in live.json (Task 18b's copy needs no AWS) ---

def test_the_bucket_min_size_reading_is_recorded_in_live_json(cfg):
    fake = _SizedS3({BASE: [{"ID": "x", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}]},
                    min_size="varies_by_storage_class")
    lc.sync(cfg, BASE, run=fake)
    assert lc.load_live(cfg["CACHE_DIR"], BASE)["min_size"] == "varies_by_storage_class"
