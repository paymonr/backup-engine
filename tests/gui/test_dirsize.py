import subprocess
import pytest
from app.gui import dirsize, fsbrowse

def test_counts_files_and_bytes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_bytes(b"12345")
    (tmp_path / "a" / "y.txt").write_bytes(b"67")
    got = dirsize.dir_size(str(tmp_path), "a")
    assert got["count"] == 2
    assert got["bytes"] >= 7          # du -sb apparent bytes (>= file content; incl. tiny dir overhead)
    assert "capped" not in got

def test_confined(tmp_path):
    with pytest.raises(fsbrowse.PathError):
        dirsize.dir_size(str(tmp_path), "../etc")

def test_size_timeout_degrades_safely(tmp_path, monkeypatch):
    # du timing out on a giant tree must degrade to a safe result flagged capped,
    # never raise (the size is a best-effort seed, not a hard dependency).
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="du", timeout=1)
    monkeypatch.setattr(dirsize.subprocess, "run", boom)
    got = dirsize.dir_size(str(tmp_path), ".")
    assert got.get("capped") is True
    assert got["bytes"] == 0 and got["count"] >= 0

def test_size_command_failure_degrades_safely(tmp_path, monkeypatch):
    # du missing/erroring degrades to a safe zero size, flagged capped.
    def boom(*a, **k):
        raise OSError("du not found")
    monkeypatch.setattr(dirsize.subprocess, "run", boom)
    got = dirsize.dir_size(str(tmp_path), ".")
    assert got.get("capped") is True
    assert got["bytes"] == 0

def test_normal_result_has_no_capped_key(tmp_path):
    # A successful measurement carries no "capped" key.
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_bytes(b"12345")
    got = dirsize.dir_size(str(tmp_path), "a")
    assert got["count"] == 1
    assert got["bytes"] >= 5
    assert "capped" not in got
