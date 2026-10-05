# tests/engine/test_storage_summary.py — the per-folder storage summary (spec 2026-09-23 §4, R-B8).
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.engine import storage_summary as ss

B = "unraid-backup-123456789012"
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _v(key, vid, lm, *, latest=False, size=0, marker=False):
    return {"Key": key, "VersionId": vid, "IsLatest": latest, "LastModified": lm, "Size": size, "marker": marker}


# a.cbz: current v3 (200 B); v2 (100 B) replaced 3 days ago; v1 (50 B) replaced 13 days ago
# b.cbz: deleted 1 day ago (a delete marker is current); v1 (1000 B) is its old version
# c.cbz: current only (7 B); other/x: another folder -- never counted
ENTRIES = [
    _v("media/manga/a.cbz", "a3", "2026-09-20T12:00:00.000Z", latest=True, size=200),
    _v("media/manga/a.cbz", "a2", "2026-09-10T12:00:00.000Z", size=100),
    _v("media/manga/a.cbz", "a1", "2026-09-01T12:00:00.000Z", size=50),
    _v("media/manga/b.cbz", "bm", "2026-09-22T12:00:00.000Z", latest=True, marker=True),
    _v("media/manga/b.cbz", "b1", "2026-08-24T12:00:00.000Z", size=1000),
    _v("media/manga/c.cbz", "c1", "2026-09-01T00:00:00.000Z", latest=True, size=7),
    _v("media/other/x", "x1", "2026-09-01T00:00:00.000Z", latest=True, size=5),
]


class FakeLister:
    """ListObjectVersions (--no-paginate + --cli-input-json) over ENTRIES, paged by MaxKeys."""
    def __init__(self, entries=ENTRIES, fail=None):
        self.entries, self.fail, self.calls = list(entries), fail, []

    def __call__(self, args, *, region, key, secret, session_token=None):
        self.calls.append(list(args))
        assert args[:2] == ["s3api", "list-object-versions"] and "--no-paginate" in args
        assert session_token is None and key == "AKIARUN"          # the RUNTIME key, never the role
        if self.fail:
            return SimpleNamespace(returncode=254, stdout="", stderr=self.fail)
        inp = json.loads(args[args.index("--cli-input-json") + 1])
        rows = [e for e in self.entries if e["Key"].startswith(inp.get("Prefix", ""))]
        start = 0
        if "KeyMarker" in inp:
            start = next(i for i, e in enumerate(rows)
                         if (e["Key"], e["VersionId"]) == (inp["KeyMarker"], inp["VersionIdMarker"])) + 1
        page = rows[start:start + inp["MaxKeys"]]
        truncated = start + inp["MaxKeys"] < len(rows)
        out = {"IsTruncated": truncated,
               "Versions": [{k: e[k] for k in ("Key", "VersionId", "IsLatest", "LastModified", "Size")}
                            for e in page if not e["marker"]],
               "DeleteMarkers": [{k: e[k] for k in ("Key", "VersionId", "IsLatest", "LastModified")}
                                 for e in page if e["marker"]]}
        if truncated:
            out["NextKeyMarker"], out["NextVersionIdMarker"] = page[-1]["Key"], page[-1]["VersionId"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")


def _scan(page_keys=1000, lister=None):
    return ss.scan(B, "media/manga/", region="us-east-1", key="AKIARUN", secret="runsek",
                   run=lister or FakeLister(), now=NOW, page_keys=page_keys)


def test_scan_buckets_old_versions_by_age_and_rank():
    s = _scan()
    assert s["bucket"] == B and s["folder"] == "media/manga/" and s["scanned_at"] == "2026-09-23T12:00:00Z"
    assert s["noncurrent_by_age_days"] == [[1, 1, 1000], [3, 1, 100], [13, 1, 50]]
    assert s["noncurrent_by_rank"] == [[1, 2, 1100], [2, 1, 50]]
    assert s["noncurrent_by_age_rank"] == [[1, 1, 1, 1000], [3, 1, 1, 100], [13, 2, 1, 50]]
    assert (s["noncurrent_versions"], s["noncurrent_bytes"]) == (3, 1150)
    assert (s["delete_markers"], s["current_objects"], s["current_bytes"]) == (1, 2, 207)


@pytest.mark.parametrize("page_keys", [1, 2, 3])
def test_paging_never_splits_a_file_in_two(page_keys):
    lister = FakeLister()
    assert _scan(page_keys, lister) == _scan(1000)
    assert len(lister.calls) > 1
    second = json.loads(lister.calls[1][lister.calls[1].index("--cli-input-json") + 1])
    assert "KeyMarker" in second and "VersionIdMarker" in second


def test_ranks_beyond_100_fold_into_101():
    from datetime import timedelta
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stamps = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z") for i in range(106)]
    # S3's order: newest first -- v105 is current, v104..v0 are 105 old versions
    many = [_v("media/manga/z", f"v{i}", stamps[i], latest=(i == 105), size=1) for i in reversed(range(106))]
    ranks = {r: n for r, n, _b in _scan(lister=FakeLister(many))["noncurrent_by_rank"]}
    assert max(ranks) == ss.RANK_CAP and ranks[ss.RANK_CAP] == 5 and ranks[1] == ranks[100] == 1


def test_a_failed_listing_is_scrubbed():
    with pytest.raises(ss.SummaryError) as e:
        _scan(lister=FakeLister(fail="AccessDenied for AKIARUN runsek"))
    assert "runsek" not in str(e.value) and "AKIARUN" not in str(e.value)


class _StuckLister:
    """A page that claims IsTruncated but hands back the SAME (NextKeyMarker,
    NextVersionIdMarker) it was called with -- a listing that never advances
    (fix round 1, Minor 1). Fails loudly after a few calls instead of letting an
    unguarded scan() spin forever."""
    def __init__(self, cap=5):
        self.calls, self.cap = 0, cap

    def __call__(self, args, *, region, key, secret, session_token=None):
        self.calls += 1
        assert self.calls <= self.cap, "scan() looped without making progress"
        inp = json.loads(args[args.index("--cli-input-json") + 1])
        next_key = inp.get("KeyMarker", "media/manga/a.cbz")
        next_vid = inp.get("VersionIdMarker", "a1")
        out = {"IsTruncated": True,
               "Versions": [{"Key": "media/manga/a.cbz", "VersionId": "a1", "IsLatest": True,
                             "LastModified": "2026-09-20T12:00:00.000Z", "Size": 1}],
               "DeleteMarkers": [], "NextKeyMarker": next_key, "NextVersionIdMarker": next_vid}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")


def test_a_page_marker_that_never_advances_raises_instead_of_looping_forever():
    lister = _StuckLister()
    with pytest.raises(ss.SummaryError):
        ss.scan(B, "media/manga/", region="us-east-1", key="AKIARUN", secret="runsek",
               run=lister, now=NOW, page_keys=1)
    assert lister.calls <= 2


def test_the_whole_bucket_is_listed_without_a_prefix():
    lister = FakeLister()
    ss.scan(B, "", region="us-east-1", key="AKIARUN", secret="runsek", run=lister, now=NOW)
    assert "Prefix" not in json.loads(lister.calls[0][lister.calls[0].index("--cli-input-json") + 1])


def test_scanned_at_reads_the_summarys_timestamp():
    # A public reader for `scanned_at` (fix round 1, Minor 4) -- so callers (sysop's
    # freshness check) never reach into the private `_parse`.
    assert ss.scanned_at({"scanned_at": "2026-09-23T12:00:00Z"}) == NOW
    assert ss.scanned_at({}) is None
    assert ss.scanned_at(None) is None


def test_save_and_load_round_trip(tmp_path):
    s = _scan()
    ss.save(str(tmp_path), s)
    assert ss.summary_path(str(tmp_path), B, "media/manga/").name == f"{B}__media-manga.json"
    assert ss.load(str(tmp_path), B, "media/manga/") == s
    assert ss.load(str(tmp_path), B, "appdata/") is None
    assert ss.slug("") == "bucket" and ss.slug("appdata/") == "appdata"


@pytest.mark.parametrize("bad", ["x", [[1, 2]], [["a", 1, 2, 3]]])
def test_a_malformed_summary_counts_as_no_summary(tmp_path, bad):
    # fix round 1, Minor: a hand-edited or corrupted summary file must never crash impact() --
    # it reads as "no summary yet" instead, same as a missing file.
    s = _scan()
    s["noncurrent_by_age_rank"] = bad
    ss.save(str(tmp_path), s)
    assert ss.load(str(tmp_path), B, "media/manga/") is None


def test_a_non_string_scanned_at_counts_as_no_summary(tmp_path):
    s = _scan()
    s["scanned_at"] = 12345
    ss.save(str(tmp_path), s)
    assert ss.load(str(tmp_path), B, "media/manga/") is None


def test_a_summary_missing_the_histograms_entirely_still_loads(tmp_path):
    # sysop's after-run freshness check only reads scanned_at (fix round 1: don't require the
    # full scan shape for that, only reject a field that IS present but malformed).
    ss.save(str(tmp_path), {"bucket": B, "folder": "media/manga/", "scanned_at": "2026-09-23T12:00:00Z"})
    assert ss.load(str(tmp_path), B, "media/manga/") is not None


def _days(d):
    return {"NoncurrentVersionExpiration": {"NoncurrentDays": d}}


def test_a_delete_marker_between_two_versions_still_ages_the_one_beneath_it():
    # fix round 1, Minor 6: a key deleted then recreated -- the delete marker sits BETWEEN
    # the current (recreated) version and the older one, and it's the marker's own
    # LastModified (its immediate successor), not the current version's, that ages the
    # version beneath it.
    entries = [
        _v("media/manga/d.cbz", "d2", "2026-09-20T12:00:00.000Z", latest=True, size=300),
        _v("media/manga/d.cbz", "dm", "2026-09-15T12:00:00.000Z", marker=True),
        _v("media/manga/d.cbz", "d1", "2026-09-01T12:00:00.000Z", size=150),
    ]
    s = _scan(lister=FakeLister(entries))
    assert s["noncurrent_by_age_days"] == [[8, 1, 150]]
    assert s["noncurrent_by_rank"] == [[1, 1, 150]]
    assert (s["noncurrent_versions"], s["noncurrent_bytes"]) == (1, 150)
    assert (s["delete_markers"], s["current_objects"], s["current_bytes"]) == (1, 1, 300)


def test_impact_is_exact_for_days_newest_and_both():
    s = _scan()
    assert ss.impact(s, None, _days(3)) == {"versions": 2, "bytes": 150, "oldest_age_days": 13}
    assert ss.impact(s, _days(30), _days(3)) == {"versions": 2, "bytes": 150, "oldest_age_days": 13}
    assert ss.impact(s, _days(10), _days(3)) == {"versions": 1, "bytes": 100, "oldest_age_days": 3}
    newest1 = {"NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 1}}
    assert ss.impact(s, None, newest1) == {"versions": 1, "bytes": 50, "oldest_age_days": 13}
    both = {"NoncurrentVersionExpiration": {"NoncurrentDays": 20, "NewerNoncurrentVersions": 1}}
    assert ss.impact(s, None, both) == {"versions": 0, "bytes": 0, "oldest_age_days": None}
    assert ss.impact(s, _days(3), None) == {"versions": 0, "bytes": 0, "oldest_age_days": None}


def test_moved_counts_what_a_tier_newly_moves():
    s = _scan()                                              # old versions aged 1, 3 and 13 days
    tier = {"NoncurrentVersionTransitions": [{"NoncurrentDays": 3, "StorageClass": "DEEP_ARCHIVE"}]}
    assert ss.moved(s, None, tier) == {"versions": 2, "bytes": 150}
    earlier = {"NoncurrentVersionTransitions": [{"NoncurrentDays": 1, "StorageClass": "DEEP_ARCHIVE"}]}
    assert ss.moved(s, tier, earlier) == {"versions": 1, "bytes": 1000}          # only the newly reached one
    removed_first = dict(tier, NoncurrentVersionExpiration={"NoncurrentDays": 10})
    assert ss.moved(s, None, removed_first) == {"versions": 1, "bytes": 100}      # age 13 is removed instead
    assert ss.moved(s, tier, None) == {"versions": 0, "bytes": 0}


def test_moved_never_pulls_a_version_back_to_a_warmer_class():
    # fix round 1, M3a: S3 never moves an object back up to a warmer class. Every version here
    # is already past day 1, so a prior Deep Archive tier already parked all of them there --
    # switching the FOLDER RULE to a (warmer) Glacier Instant Retrieval tier can't claim any of
    # them as newly moved.
    s = _scan()
    da = {"NoncurrentVersionTransitions": [{"NoncurrentDays": 1, "StorageClass": "DEEP_ARCHIVE"}]}
    gir = {"NoncurrentVersionTransitions": [{"NoncurrentDays": 1, "StorageClass": "GLACIER_IR"}]}
    assert ss.moved(s, da, gir) == {"versions": 0, "bytes": 0}
    # the reverse (GIR -> DA) is a real further move once a version reaches the new, colder day.
    assert ss.moved(s, gir, da) == {"versions": 3, "bytes": 1150}


# --- describe(): the verbose Activity lines (owner request 2026-10-03) ------------------------

def test_describe_spells_out_the_summary_in_several_lines():
    s = _scan()
    lines = ss.describe(s, rule={"NoncurrentVersionExpiration": {"NoncurrentDays": 30}}, took_s=3.4)
    assert lines[0] == f"storage summary: media/manga/ in {B}"
    assert lines[1] == "current files: 2 (207 B)"
    assert lines[2] == "earlier copies: 3 (1 KB), the oldest was replaced 13 days ago"
    assert lines[3] == "delete markers: 1 (files that were removed; their earlier copies are counted above)"
    assert lines[4] == "rule: S3 removes earlier copies 30 days after they were replaced; nothing goes in the next 7 days"
    assert lines[5] == "listed in 3 s"


def test_describe_counts_what_goes_in_the_next_week():
    s = _scan()
    # ages 1, 3 and 13 days: a 15-day rule reaches the 13-day-old one within a week (50 B)
    l15 = ss.describe(s, rule={"NoncurrentVersionExpiration": {"NoncurrentDays": 15}})
    assert l15[4] == "rule: S3 removes earlier copies 15 days after they were replaced; about 1 (50 B) goes in the next 7 days"
    # a 5-day rule: all three are at or past due (S3 removes them within about a day)
    l5 = ss.describe(s, rule={"NoncurrentVersionExpiration": {"NoncurrentDays": 5}})
    assert l5[4] == "rule: S3 removes earlier copies 5 days after they were replaced; about 3 (1 KB) go in the next 7 days"
    # newest-N exemptions are honoured (rank 2 is the only one beyond the newest 1)
    ln = ss.describe(s, rule={"NoncurrentVersionExpiration": {"NoncurrentDays": 5, "NewerNoncurrentVersions": 1}})
    assert ln[4] == ("rule: S3 removes earlier copies 5 days after they were replaced, keeping the newest 1 per file"
                     "; about 1 (50 B) goes in the next 7 days")


def test_describe_without_a_rule_or_a_timing():
    s = _scan()
    # rule known to be absent / disabled: earlier copies stay for good
    assert ss.describe(s, rule=None)[4] == "rule: earlier copies are kept for good"
    assert ss.describe(s, rule={"Status": "Disabled"})[4] == "rule: earlier copies are kept for good"
    # the rules were never checked: no rule line at all, and no timing line without took_s
    lines = ss.describe(s, rule_known=False)
    assert len(lines) == 4 and not any(l.startswith("rule:") for l in lines)


def test_describe_handles_an_empty_folder_and_whole_bucket():
    empty = {"bucket": B, "folder": "", "noncurrent_by_age_days": [], "noncurrent_by_rank": [],
             "noncurrent_by_age_rank": [], "noncurrent_versions": 0, "noncurrent_bytes": 0,
             "delete_markers": 0, "current_objects": 0, "current_bytes": 0}
    lines = ss.describe(empty, rule=None)
    assert lines[0] == f"storage summary: whole bucket in {B}"
    assert lines[1] == "current files: 0 (0 B)"
    assert lines[2] == "earlier copies: none"
    assert lines[3] == "delete markers: 0"


def test_describe_never_says_old_versions():
    s = _scan()
    text = "\n".join(ss.describe(s, rule={"NoncurrentVersionExpiration": {"NoncurrentDays": 30}}, took_s=1))
    assert "old version" not in text and "earlier copies" in text


def test_stats_picks_the_record_figures():
    s = _scan()
    assert ss.stats(s) == {"current_objects": 2, "current_bytes": 207, "noncurrent_versions": 3,
                           "noncurrent_bytes": 1150, "delete_markers": 1, "oldest_age_days": 13}
    assert ss.stats(dict(s, noncurrent_by_age_days=[]))["oldest_age_days"] is None
