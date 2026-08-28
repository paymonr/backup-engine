from app.gui import media_shares as ms

def test_valid_name():
    assert ms.valid_name("comics") and ms.valid_name("tv-shows_2024.old")
    assert not ms.valid_name("..") and not ms.valid_name("a/b") and not ms.valid_name("a b")

def test_generate_whole_share():
    assert ms.generate_rules(True, []) == "+ /**\n"
    assert ms.generate_rules(False, []) == "+ /**\n"  # no folders => whole

def test_generate_single_folder_includes_ancestor_dir():
    out = ms.generate_rules(False, ["manga"])
    lines = out.splitlines()
    assert "+ /manga/**" in lines      # contents
    assert "+ /manga/" in lines        # the dir itself, so rclone descends
    assert lines[-1] == "- **"         # catch-all exclude last

def test_generate_nested_folder_emits_all_ancestors():
    lines = ms.generate_rules(False, ["manga/raw"]).splitlines()
    assert "+ /manga/raw/**" in lines
    assert "+ /manga/" in lines and "+ /manga/raw/" in lines
    assert lines[-1] == "- **"

def test_round_trip_whole():
    assert ms.parse_rules(ms.generate_rules(True, [])) == {"whole": True, "folders": [], "raw": None}
    assert ms.parse_rules("") == {"whole": True, "folders": [], "raw": None}

def test_round_trip_folders():
    got = ms.parse_rules(ms.generate_rules(False, ["manga", "manhwa"]))
    assert got["whole"] is False and got["raw"] is None
    assert sorted(got["folders"]) == ["manga", "manhwa"]

def test_parse_custom_rules_flagged_raw():
    got = ms.parse_rules("+ /a/**\n- /a/tmp/**\n- **\n")  # a `-` mid-list is non-canonical
    assert got["raw"] is not None and got["folders"] == []


from pathlib import Path
import pytest

def _tree(tmp_path):
    root = tmp_path / "media"
    (root / "comics").mkdir(parents=True)
    (root / "books").mkdir()
    shares = tmp_path / "config" / "media-shares"
    return str(root), str(shares)

def test_list_shares_marks_enabled_by_file_presence(tmp_path):
    root, shares = _tree(tmp_path)
    ms.write_selection(shares, "comics", False, ["manga"])
    got = {s["name"]: s for s in ms.list_shares(root, shares)}
    assert got["comics"]["enabled"] is True and got["comics"]["folders"] == ["manga"]
    assert got["books"]["enabled"] is False

def test_write_and_read_selection_round_trip(tmp_path):
    root, shares = _tree(tmp_path)
    ms.write_selection(shares, "comics", False, ["manga", "manhwa"])
    sel = ms.read_selection(shares, "comics")
    assert sel["whole"] is False and sorted(sel["folders"]) == ["manga", "manhwa"]

def test_write_raw_is_verbatim_and_flagged_custom(tmp_path):
    root, shares = _tree(tmp_path)
    ms.write_raw(shares, "comics", "+ /a/**\n- /a/tmp/**\n- **")
    sel = ms.read_selection(shares, "comics")
    assert sel["raw"] is not None
    assert Path(shares, "comics.txt").read_text().endswith("\n")

def test_disable_deletes_file(tmp_path):
    root, shares = _tree(tmp_path)
    ms.write_selection(shares, "comics", True, [])
    ms.disable(shares, "comics")
    assert not Path(shares, "comics.txt").exists()
    assert ms.read_selection(shares, "comics") == {"whole": False, "folders": [], "raw": None}

def test_write_rejects_invalid_name(tmp_path):
    root, shares = _tree(tmp_path)
    with pytest.raises(ValueError):
        ms.write_selection(shares, "../evil", True, [])
