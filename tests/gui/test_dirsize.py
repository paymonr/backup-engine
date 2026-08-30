import os, pytest
from app.gui import dirsize, fsbrowse

def test_counts_files_and_bytes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_bytes(b"12345")
    (tmp_path / "a" / "y.txt").write_bytes(b"67")
    got = dirsize.dir_size(str(tmp_path), "a")
    assert got == {"bytes": 7, "count": 2}

def test_confined(tmp_path):
    with pytest.raises(fsbrowse.PathError):
        dirsize.dir_size(str(tmp_path), "../etc")

def test_budget_caps_without_raising(tmp_path):
    # A zero (elapsed>=budget) wall-clock budget trips the ceiling immediately, so a
    # huge share can't hang /jobs/source-size: it returns what it has, flagged capped.
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_bytes(b"12345")
    got = dirsize.dir_size(str(tmp_path), "a", budget_s=0.0)
    assert got.get("capped") is True
    assert got["bytes"] >= 0 and got["count"] >= 0  # safe shape, no raise

def test_walk_failure_degrades_safely(tmp_path, monkeypatch):
    # A failure enumerating the tree must degrade to a safe zero result, never raise
    # (the size is only a best-effort seed for the wizard, not a hard dependency).
    (tmp_path / "a").mkdir()
    def boom(*a, **k):
        raise OSError("walk blew up")
    monkeypatch.setattr(dirsize.os, "walk", boom)
    got = dirsize.dir_size(str(tmp_path), "a")
    assert got["bytes"] == 0 and got["count"] == 0
    assert got.get("capped") is True

def test_normal_result_has_no_capped_key(tmp_path):
    # An uncapped, successful walk keeps the exact {"bytes","count"} contract (no
    # stray "capped" key) so /jobs/source-size's existing equality tests still hold.
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_bytes(b"12345")
    assert dirsize.dir_size(str(tmp_path), "a") == {"bytes": 5, "count": 1}
