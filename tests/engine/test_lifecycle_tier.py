# tests/engine/test_lifecycle_tier.py — the cheaper tier for old versions (spec 2026-09-23 §1, §2, §3; #9).
import json
from types import SimpleNamespace

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import BASE, FakeS3, _live, cfg  # noqa: F401

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
    rule = lc.with_tier(M, lc.plain_rule(M, {"type": "days", "days": 180}), DA30)
    assert lc._keeps_words(rule) == ("old versions removed 180 days after being replaced; moved to "
                                     "Thaw first, hours · DEEP_ARCHIVE 30 days after being replaced")


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


class _SizedS3(FakeS3):
    """FakeS3 whose lifecycle configuration also reports the bucket's minimum-object-size setting."""
    def __init__(self, *a, min_size=None, **k):
        super().__init__(*a, **k)
        self.min_size = min_size

    def __call__(self, args, **kw):
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
