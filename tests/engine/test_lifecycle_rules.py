# tests/engine/test_lifecycle_rules.py — the pure S3 rules model (spec 2026-09-23 §1).
import json

import pytest
from app.engine import lifecycle as lc

BASE = "unraid-backup-123456789012"


def _job(name, typ, retention=None, **kw):
    j = {"name": name, "type": typ, "source": name, "schedule": "0 3 * * *", "enabled": True}
    if retention is not None:
        j["retention"] = retention
    j.update(kw)
    return j


JOBS = [
    _job("manga", "archive", {"type": "days", "days": 180}),
    _job("documents", "versioned-files", {"type": "days", "days": 90}),
    _job("appdata_backups", "versioned", {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}),
    _job("nas", "versioned", {"type": "tiered", "keep": {"last": 1, "daily": 0, "weekly": 0, "monthly": 0}}),
    _job("photos", "archive", {"type": "count", "count": 10}, dedicated=True, bucket=f"{BASE}-photos"),
]


def _by_id(rules):
    return {r["ID"]: r for r in rules}


def test_base_bucket_folders():
    fs = lc.folders_for(BASE, BASE, JOBS)
    assert [(f.folder, f.kind, f.jobs) for f in fs] == [
        ("appdata/", "undo", ("appdata_backups", "nas")),
        ("media/documents/", "undo", ("documents",)),
        ("media/manga/", "plain", ("manga",)),
    ]


def test_dedicated_bucket_is_one_whole_bucket_folder():
    fs = lc.folders_for(f"{BASE}-photos", BASE, JOBS)
    assert [(f.folder, f.kind, f.jobs) for f in fs] == [("", "plain", ("photos",))]
    assert fs[0].retention == {"type": "count", "count": 10}


def test_buckets_for_lists_base_then_dedicated():
    assert lc.buckets_for(BASE, JOBS) == [BASE, f"{BASE}-photos"]


def test_desired_rules_for_the_base_bucket():
    rules = _by_id(lc.desired_rules(BASE, BASE, JOBS, {}))
    assert rules["backup-engine:media/manga/"] == {
        "ID": "backup-engine:media/manga/", "Status": "Enabled",
        "Filter": {"Prefix": "media/manga/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 180}}
    assert rules["backup-engine:media/documents/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert rules["backup-engine:appdata/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert rules["backup-engine:housekeeping"] == {
        "ID": "backup-engine:housekeeping", "Status": "Enabled", "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        "Expiration": {"ExpiredObjectDeleteMarker": True}}
    assert set(rules) == {"backup-engine:media/manga/", "backup-engine:media/documents/",
                          "backup-engine:appdata/", "backup-engine:housekeeping"}


def test_desired_rules_for_a_dedicated_bucket():
    rules = _by_id(lc.desired_rules(f"{BASE}-photos", BASE, JOBS, {}))
    assert rules["backup-engine:bucket"]["Filter"] == {"Prefix": ""}
    assert rules["backup-engine:bucket"]["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": 1, "NewerNoncurrentVersions": 10}


@pytest.mark.parametrize("retention,expected", [
    ({"type": "days", "days": 180}, {"NoncurrentDays": 180}),
    ({"type": "days", "days": 0}, {"NoncurrentDays": 1}),
    ({"type": "count", "count": 10}, {"NoncurrentDays": 1, "NewerNoncurrentVersions": 10}),
    ({"type": "count", "count": 10, "days": 30}, {"NoncurrentDays": 30, "NewerNoncurrentVersions": 10}),
    ({"type": "count", "count": 500}, {"NoncurrentDays": 1, "NewerNoncurrentVersions": 100}),
])
def test_plain_rule_shapes(retention, expected):
    assert lc.plain_rule("media/x/", retention)["NoncurrentVersionExpiration"] == expected


def test_keep_everything_has_no_rule():
    assert lc.plain_rule("media/x/", {"type": "keep_all"}) is None
    jobs = [_job("x", "archive", {"type": "keep_all"})]
    assert "backup-engine:media/x/" not in _by_id(lc.desired_rules(BASE, BASE, jobs, {}))


def test_settings_change_undo_window_and_housekeeping(tmp_path):
    settings = {"version": 1, "buckets": {BASE: {
        "abort_uploads_days": 3, "delete_marker_cleanup": False,
        "folders": {"appdata/": {"undo_days": 60}}}}}
    lc.save_settings(str(tmp_path), settings)
    rules = _by_id(lc.desired_rules(BASE, BASE, JOBS, lc.load_settings(str(tmp_path))))
    assert rules["backup-engine:appdata/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 60}
    assert rules["backup-engine:media/documents/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert rules["backup-engine:housekeeping"] == {
        "ID": "backup-engine:housekeeping", "Status": "Enabled", "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}


def test_missing_or_garbage_settings_fall_back_to_defaults(tmp_path):
    assert lc.load_settings(str(tmp_path)) == {"version": 1, "buckets": {}}
    (tmp_path / "storage.json").write_text("not json")
    assert lc.load_settings(str(tmp_path)) == {"version": 1, "buckets": {}}
    b = lc.bucket_settings({"buckets": {BASE: {"abort_uploads_days": "x"}}}, BASE)
    assert b["abort_uploads_days"] == 7 and b["delete_marker_cleanup"] is True and b["versioning"] == "on"


def test_app_rules_are_recognised_including_legacy_ids():
    for rid in ("backup-engine:media/x/", "backup-engine:housekeeping", "backstop-appdata",
                "backstop-media", "backup-engine"):
        assert lc.is_app_rule({"ID": rid})
    assert not lc.is_app_rule({"ID": "archive-old-logs"})
    assert not lc.is_app_rule({})


def test_merge_keeps_console_rules_verbatim_and_replaces_app_rules():
    console = {"ID": "archive-old-logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
               "Expiration": {"Days": 14}}
    legacy = {"ID": "backstop-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
              "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}
    app = lc.desired_rules(BASE, BASE, JOBS, {})
    merged = lc.merge([console, legacy], app)
    assert merged[0] == console
    assert "backstop-media" not in _by_id(merged)
    assert [r for r in merged if lc.is_app_rule(r)] == app


def test_app_rules_of_normalises_what_s3_returns():
    ours = lc.desired_rules(BASE, BASE, JOBS, {})
    # S3 may return an empty bucket-wide filter as {} and a different key order.
    returned = json.loads(json.dumps(ours))
    for r in returned:
        if r["Filter"] == {"Prefix": ""}:
            r["Filter"] = {}
    assert lc.app_rules_of(returned) == lc.app_rules_of(ours)


def test_describe_in_words():
    assert lc.describe(lc.plain_rule("media/m/", {"type": "days", "days": 180})) == \
        "media/m/: old versions removed 180 days after being replaced"
    assert lc.describe(lc.plain_rule("", {"type": "count", "count": 10})) == \
        "whole bucket: newest 10 old versions of each file kept"
    assert lc.describe(lc.plain_rule("media/m/", {"type": "count", "count": 10, "days": 30})) == \
        "media/m/: newest 10 old versions of each file kept; older ones removed 30 days after being replaced"
    assert lc.describe(lc.housekeeping_rule(lc.bucket_settings({}, BASE))) == \
        "whole bucket: abandoned uploads cleared after 7 days; leftover delete markers cleared"


@pytest.mark.parametrize("bad", [
    {"type": "count", "count": "x"},
    {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}},   # Snapshot-only
    {"type": "nope"},
])
def test_a_malformed_plain_copy_setting_holds_its_folders_rule(bad):
    # Never raise: one bad job must not break the bucket's rules -- and never silently drop
    # the folder's rule either (final fix wave I1): the folder HOLDS whatever rule S3 has for
    # it (resolve_held against the baseline), and the folder says why.
    jobs = [_job("manga", "archive", bad), _job("documents", "versioned-files", {"type": "days", "days": 90})]
    fs = {f.folder: f for f in lc.folders_for(BASE, BASE, jobs)}
    assert fs["media/manga/"].hold is True and fs["media/manga/"].retention is None
    assert "manga" in fs["media/manga/"].note and fs["media/documents/"].note == ""
    assert fs["media/documents/"].hold is False
    rules = _by_id(lc.desired_rules(BASE, BASE, jobs, {}))
    assert "backup-engine:media/manga/" not in rules and "backup-engine:media/documents/" in rules
    want = lc.desired(BASE, BASE, jobs, {})
    assert want.held == frozenset({"backup-engine:media/manga/"})
    kept = lc.plain_rule("media/manga/", {"type": "days", "days": 180})
    before = lc.RuleSet({kept["ID"]: kept}, frozenset({"media/manga/"}))
    resolved = lc.resolve_held(before, want)
    assert resolved.rules["backup-engine:media/manga/"] == kept
    assert list(resolved.rules)[-1] == lc.HOUSEKEEPING_ID
    assert lc.classify(before, resolved) == [c for c in lc.classify(before, resolved)
                                             if c.rule_id != "backup-engine:media/manga/"]
    # no baseline rule there -> none (S3 keeps everything, as it does now)
    assert "backup-engine:media/manga/" not in lc.resolve_held(lc.RuleSet({}, frozenset()), want).rules


# --- console rules: fingerprint + destructive actions in words (I1) --------------------------

def test_console_fingerprint_ignores_app_rules_and_s3_formatting():
    app = lc.desired_rules(BASE, BASE, JOBS, {})
    console = {"ID": "logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 14}}
    fp = lc.console_fingerprint(app + [console])
    assert set(fp) == {"logs"}
    reordered = json.loads(json.dumps({"Expiration": {"Days": 14}, "Filter": {"Prefix": "logs/"},
                                       "Status": "Enabled", "ID": "logs"}))
    assert lc.console_fingerprint([reordered]) == fp
    assert lc.console_fingerprint([dict(console, Expiration={"Days": 15})]) != fp
    no_id = {"Status": "Enabled", "Filter": {}, "Expiration": {"Days": 1}}
    assert len(lc.console_fingerprint([no_id, dict(no_id, Expiration={"Days": 2})])) == 2


@pytest.mark.parametrize("rule,words", [
    ({"Expiration": {"Days": 1}}, ["expires current files 1 day after they're written"]),
    ({"Expiration": {"Date": "2027-01-01T00:00:00Z"}}, ["expires current files on 2027-01-01"]),
    ({"NoncurrentVersionExpiration": {"NoncurrentDays": 3}}, ["removes old versions 3 days after being replaced"]),
    ({"NoncurrentVersionExpiration": {"NoncurrentDays": 3, "NewerNoncurrentVersions": 2}},
     ["removes old versions 3 days after being replaced (newest 2 kept)"]),
    ({"Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]}, ["moves current files to GLACIER after 30 days"]),
    ({"NoncurrentVersionTransitions": [{"NoncurrentDays": 5, "StorageClass": "DEEP_ARCHIVE"}]},
     ["moves old versions to Deep Archive 5 days after being replaced"]),   # fix round 1, M4: owner words
    ({"Expiration": {"ExpiredObjectDeleteMarker": True}}, []),
    ({"AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}, []),
    ({"Status": "Disabled", "Expiration": {"Days": 1}}, []),
])
def test_destructive_actions_in_words(rule, words):
    assert lc.destructive_actions(dict({"ID": "r", "Filter": {"Prefix": ""}}, **rule)) == words


def test_merge_alarms_keeps_the_most_severe_and_every_rule_id():
    restored = {"kind": "restored", "at": "2026-09-23T01:00:00Z", "lines": ["a"]}
    console = {"kind": "console_rule", "at": "2026-09-23T02:00:00Z", "lines": ["b"], "rules": ["x"]}
    not_restored = {"kind": "not_restored", "at": "2026-09-23T00:00:00Z", "lines": ["c"]}
    assert lc.merge_alarms([]) is None and lc.merge_alarms([None]) is None
    m = lc.merge_alarms([restored, console])
    assert m["kind"] == "console_rule" and m["rules"] == ["x"]
    m = lc.merge_alarms([console, not_restored, dict(console, rules=["y"])])
    assert m["kind"] == "not_restored" and m["rules"] == ["x", "y"]


def test_describe_names_what_a_tampered_app_rule_now_does():
    # Tamper lines are built with describe(): a hostile edit must read as what it does.
    r = lc.plain_rule("media/m/", {"type": "days", "days": 180})
    r["Expiration"] = {"Days": 1}
    r["Transitions"] = [{"Days": 0, "StorageClass": "GLACIER"}]
    words = lc.describe(r)
    assert "expires current files 1 day after they're written" in words
    assert "moves current files to GLACIER after 0 days" in words
    assert lc.describe(dict(lc.plain_rule("media/m/", {"type": "days", "days": 180}), Status="Disabled")) == \
        "media/m/: switched off"
    assert lc.describe({"ID": "backup-engine:x", "Status": "Enabled", "Filter": {"Prefix": "x/"}}) == "x/: no actions"


# --- Phase A carry-overs (M4, O2) --------------------------------------------------------

def test_one_day_reads_as_a_day():
    assert lc.describe(lc.plain_rule("media/m/", {"type": "days", "days": 1})) == \
        "media/m/: old versions removed 1 day after being replaced"
    bset = lc.bucket_settings({"buckets": {BASE: {"abort_uploads_days": 1,
                                                  "delete_marker_cleanup": False}}}, BASE)
    assert lc.describe(lc.housekeeping_rule(bset)) == "whole bucket: abandoned uploads cleared after 1 day"
    rule = {"ID": "r", "Filter": {"Prefix": ""},
            "NoncurrentVersionTransitions": [{"NoncurrentDays": 1, "StorageClass": "GLACIER"}],
            "Transitions": [{"Days": 1, "StorageClass": "GLACIER"}]}
    assert lc.destructive_actions(rule) == ["moves current files to GLACIER after 1 day",
                                            "moves old versions to Glacier 1 day after being replaced"]


def test_merge_alarms_remembers_the_newest_alarm_time():
    a = {"kind": "not_restored", "at": "2026-09-23T01:00:00Z", "lines": []}
    b = {"kind": "console_rule", "at": "2026-09-23T03:00:00Z", "lines": [], "rules": ["x"]}
    m = lc.merge_alarms([a, b])
    assert m["kind"] == "not_restored" and m["at"] == "2026-09-23T01:00:00Z"
    assert m["latest"] == "2026-09-23T03:00:00Z"
    older = {"kind": "restored", "at": "2026-09-23T02:00:00Z", "lines": []}
    assert lc.merge_alarms([m, older])["latest"] == "2026-09-23T03:00:00Z"
