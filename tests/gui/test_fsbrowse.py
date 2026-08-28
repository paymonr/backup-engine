import os
import pytest
from pathlib import Path
from app.gui import fsbrowse
from app.gui.fsbrowse import PathError

def _root(tmp_path):
    root = tmp_path / "media"
    (root / "comics" / "manga").mkdir(parents=True)
    (root / "books").mkdir()
    (root / "comics" / "cover.jpg").write_text("x")  # a file, must not be listed
    return root

def test_safe_resolve_allows_in_root(tmp_path):
    root = _root(tmp_path)
    assert fsbrowse.safe_resolve(root, "comics") == (root / "comics").resolve()
    assert fsbrowse.safe_resolve(root, "") == root.resolve()

def test_safe_resolve_rejects_parent_traversal(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(PathError):
        fsbrowse.safe_resolve(root, "../secret")

def test_safe_resolve_rejects_absolute(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(PathError):
        fsbrowse.safe_resolve(root, "/etc")

def test_safe_resolve_rejects_escaping_symlink(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(PathError):
        fsbrowse.safe_resolve(root, "link")

def test_list_dirs_returns_immediate_subdirs_only(tmp_path):
    root = _root(tmp_path)
    assert fsbrowse.list_dirs(root, "") == ["books", "comics"]
    assert fsbrowse.list_dirs(root, "comics") == ["manga"]  # not cover.jpg

def test_list_dirs_skips_escaping_symlink_child(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()
    os.symlink(outside, root / "books" / "escape")
    assert fsbrowse.list_dirs(root, "books") == []  # escaping symlink skipped

def test_safe_resolve_rejects_symlink_loop(tmp_path):
    root = _root(tmp_path)
    loop = root / "loop"
    os.symlink(loop, loop)  # self-referential symlink -> ELOOP
    with pytest.raises(PathError):
        fsbrowse.safe_resolve(root, "loop")

def test_list_dirs_skips_symlink_loop_child(tmp_path):
    root = _root(tmp_path)
    loop = root / "books" / "loop"
    os.symlink(loop, loop)  # self-referential symlink -> ELOOP
    assert fsbrowse.list_dirs(root, "books") == []  # looping symlink skipped, no crash
