import time
from app.engine import catalog


def test_schema_and_roundtrip(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "a.txt", "media/j/a.txt@1", 10, 100.0, "STANDARD", 1.0)
    cur = catalog.current(c)
    assert cur["a.txt"]["key"] == "media/j/a.txt@1" and cur["a.txt"]["size"] == 10


def test_diff_new_changed_deleted(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "keep.txt", "k1", 5, 100.0, "STANDARD", 1.0)
    catalog.record_version(c, "edit.txt", "e1", 5, 100.0, "STANDARD", 1.0)
    entries = [{"path": "keep.txt", "size": 5, "mtime": 100.0},   # unchanged
               {"path": "edit.txt", "size": 9, "mtime": 200.0},   # changed (size+mtime)
               {"path": "fresh.txt", "size": 3, "mtime": 50.0}]   # new
    d = catalog.diff(c, entries)
    assert [e["path"] for e in d["new"]] == ["fresh.txt"]
    assert [e["path"] for e in d["changed"]] == ["edit.txt"]
    assert "keep.txt" not in d["deleted"]  # still present in entries -> not deleted
    # a removed file:
    d2 = catalog.diff(c, [e for e in entries if e["path"] != "keep.txt"])
    assert "keep.txt" in d2["deleted"]


def test_prunable_keeps_current(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "f.txt", "v1", 5, 100.0, "STANDARD", 1000.0)   # old, non-current after v2
    catalog.record_version(c, "f.txt", "v2", 6, 200.0, "STANDARD", 5000.0)   # current
    p = catalog.prunable(c, before_ts=4000.0)
    assert [r["key"] for r in p] == ["v1"]           # old version prunable
    assert all(r["key"] != "v2" for r in catalog.prunable(c, before_ts=9e9))  # current never prunable


def test_prunable_beyond_count_keeps_newest_n_per_path(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "a.txt", "v1", 5, 100.0, "STANDARD", 100.0)  # oldest
    catalog.record_version(c, "a.txt", "v2", 5, 100.0, "STANDARD", 200.0)  # middle
    catalog.record_version(c, "a.txt", "v3", 5, 100.0, "STANDARD", 300.0)  # current (newest)
    got = catalog.prunable_beyond_count(c, keep_n=2)
    assert [r["key"] for r in got] == ["v1"]  # only the 1 beyond the newest 2 kept


def test_prunable_beyond_count_never_prunes_current(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "a.txt", "only", 5, 100.0, "STANDARD", 100.0)  # sole version, current
    # even with keep_n=0, the current row must never be returned
    assert catalog.prunable_beyond_count(c, keep_n=0) == []


def test_prunable_beyond_count_per_path_independent(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "a.txt", "a1", 5, 100.0, "STANDARD", 100.0)  # old
    catalog.record_version(c, "a.txt", "a2", 5, 100.0, "STANDARD", 200.0)  # current
    catalog.record_version(c, "b.txt", "b1", 5, 100.0, "STANDARD", 100.0)  # sole version, current
    got = catalog.prunable_beyond_count(c, keep_n=1)
    assert [r["key"] for r in got] == ["a1"]  # b.txt has only 1 version -> nothing beyond keep_n=1


def test_prunable_beyond_count_excludes_tombstone_rows(tmp_path):
    c = catalog.open_catalog(str(tmp_path / "cat.sqlite"))
    catalog.record_version(c, "a.txt", "a1", 5, 100.0, "STANDARD", 100.0)
    catalog.record_version(c, "a.txt", "a2", 5, 100.0, "STANDARD", 200.0)
    catalog.mark_deleted(c, "a.txt", 300.0)  # tombstone: key=None, deleted=1; a2 now non-current
    got = catalog.prunable_beyond_count(c, keep_n=0)
    keys = [r["key"] for r in got]
    assert None not in keys            # the tombstone row is never among the results
    assert set(keys) == {"a1", "a2"}   # both real versions are non-current and beyond keep_n=0
