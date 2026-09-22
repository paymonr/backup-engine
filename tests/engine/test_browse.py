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
