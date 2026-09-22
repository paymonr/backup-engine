import json
from app.engine import browse

def test_fold_level_root():
    recs = [{"path": "a/b.txt", "type": "file", "size": 3},
            {"path": "a/c.txt", "type": "file", "size": 4},
            {"path": "top.txt", "type": "file", "size": 1}]
    out = browse.fold_level(recs, "")
    assert out["dirs"] == ["a"]
    assert [f["name"] for f in out["files"]] == ["top.txt"]

def test_fold_level_subdir():
    recs = [{"path": "a/b.txt", "type": "file", "size": 3},
            {"path": "a/sub/d.txt", "type": "file", "size": 9}]
    out = browse.fold_level(recs, "a")
    assert out["dirs"] == ["sub"]
    assert [f["name"] for f in out["files"]] == ["b.txt"]

def test_level_from_restic_ls(tmp_path):
    p = tmp_path / "ls.json"
    p.write_text("\n".join([
        json.dumps({"time": "2026-09-01T00:00:00Z", "struct_type": "snapshot"}),
        json.dumps({"struct_type": "node", "type": "dir",  "path": "/etc"}),
        json.dumps({"struct_type": "node", "type": "file", "path": "/etc/hosts", "size": 12}),
        json.dumps({"struct_type": "node", "type": "file", "path": "/top", "size": 1}),
    ]))
    out = browse.level_from_restic_ls(str(p), "")
    # restic paths are absolute; the leading "/" is normalized away to a job-root-relative view
    assert out["path"] == ""
    kinds = {e["name"]: e["kind"] for e in out["entries"]}
    assert kinds == {"etc": "dir", "top": "file"}
    sub = browse.level_from_restic_ls(str(p), "etc")
    assert [e["name"] for e in sub["entries"]] == ["hosts"]

def test_fold_level_empty_dir():
    """Empty type:dir records at a level should appear in dirs."""
    recs = [{"path": "empty_dir", "type": "dir"},
            {"path": "another_empty", "type": "dir"},
            {"path": "file.txt", "type": "file", "size": 5}]
    out = browse.fold_level(recs, "")
    assert sorted(out["dirs"]) == ["another_empty", "empty_dir"]
    assert [f["name"] for f in out["files"]] == ["file.txt"]

def test_level_from_restic_ls_skips_non_dict_json(tmp_path):
    """Non-dict JSON lines should be skipped without crashing."""
    p = tmp_path / "ls.json"
    p.write_text("\n".join([
        json.dumps({"time": "2026-09-01T00:00:00Z", "struct_type": "snapshot"}),
        json.dumps({"struct_type": "node", "type": "file", "path": "/file.txt", "size": 10}),
        "42",  # bare number - non-dict
        json.dumps([]),  # array - non-dict
        json.dumps({"struct_type": "node", "type": "file", "path": "/other.txt", "size": 5}),
    ]))
    out = browse.level_from_restic_ls(str(p), "")
    names = [e["name"] for e in out["entries"]]
    assert sorted(names) == ["file.txt", "other.txt"]

def test_dir_entries_precede_file_entries():
    """Dir entries must come before file entries in the ordered entries list."""
    recs = [{"path": "b_file.txt", "type": "file", "size": 1},
            {"path": "a_dir", "type": "dir"},
            {"path": "c_file.txt", "type": "file", "size": 2}]
    out = browse.fold_level(recs, "")
    entries = browse._entries(out, "")["entries"]
    # All dirs must come before all files
    dir_indices = [i for i, e in enumerate(entries) if e["kind"] == "dir"]
    file_indices = [i for i, e in enumerate(entries) if e["kind"] == "file"]
    assert all(d < f for d in dir_indices for f in file_indices), "Directories must precede files"
    assert [e["name"] for e in entries] == ["a_dir", "b_file.txt", "c_file.txt"]

def test_file_versions_preserved():
    """File records carrying versions should pass that list through _entries."""
    recs = [{"path": "versioned.txt", "type": "file", "size": 10, "versions": [{"id": "v1"}, {"id": "v2"}]}]
    out = browse.fold_level(recs, "")
    entries = browse._entries(out, "")["entries"]
    assert len(entries) == 1
    assert entries[0]["name"] == "versioned.txt"
    assert entries[0]["kind"] == "file"
    assert "versions" in entries[0]
    assert entries[0]["versions"] == [{"id": "v1"}, {"id": "v2"}]
