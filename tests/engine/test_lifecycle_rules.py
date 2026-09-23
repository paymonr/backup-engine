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
