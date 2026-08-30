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
