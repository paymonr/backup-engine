from pathlib import Path
import pytest
from app.gui import media_shares as ms

# ---- rule grammar ----

def test_generate_whole():
    assert ms.generate_rules(True, []) == "+ /**\n"

def test_generate_empty_selection_is_nothing():
    assert ms.generate_rules(False, []) == "- **\n"

def test_generate_single_folder_includes_ancestor_dir():
    lines = ms.generate_rules(False, ["Movies"]).splitlines()
    assert "+ /Movies/**" in lines and "+ /Movies/" in lines
    assert lines[-1] == "- **"

def test_generate_nested_folder_emits_all_ancestors():
    lines = ms.generate_rules(False, ["media/movies"]).splitlines()
    assert "+ /media/movies/**" in lines
    assert "+ /media/" in lines and "+ /media/movies/" in lines
    assert lines[-1] == "- **"

def test_roundtrip_whole():
    assert ms.parse_rules(ms.generate_rules(True, [])) == {"whole": True, "folders": [], "raw": None}

def test_roundtrip_nothing():
    assert ms.parse_rules(ms.generate_rules(False, [])) == {"whole": False, "folders": [], "raw": None}

def test_roundtrip_folders():
    got = ms.parse_rules(ms.generate_rules(False, ["Movies", "Photos/2024"]))
    assert got["whole"] is False and got["raw"] is None
    assert sorted(got["folders"]) == ["Movies", "Photos/2024"]

def test_parse_custom_rules_flagged_raw():
    got = ms.parse_rules("+ /a/**\n- /a/tmp/**\n- **\n")  # a mid-list exclude is non-canonical
    assert got["raw"] is not None and got["folders"] == []

# ---- single include-list I/O ----

def _cfg(tmp_path):
    c = tmp_path / "config"; c.mkdir(); return str(c)

def test_write_read_selection_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    ms.write_selection(cfg, False, ["Movies", "Photos"])
    sel = ms.read_selection(cfg)
    assert sel["whole"] is False and sorted(sel["folders"]) == ["Movies", "Photos"]
    assert Path(cfg, "media-includes.txt").exists()

def test_read_absent_is_nothing(tmp_path):
    assert ms.read_selection(_cfg(tmp_path)) == {"whole": False, "folders": [], "raw": None}

def test_write_whole(tmp_path):
    cfg = _cfg(tmp_path)
    ms.write_selection(cfg, True, ["ignored"])
    assert Path(cfg, "media-includes.txt").read_text() == "+ /**\n"

def test_write_raw_is_verbatim_and_flagged_custom(tmp_path):
    cfg = _cfg(tmp_path)
    ms.write_raw(cfg, "+ /x/**\n- /x/tmp/**\n- **")
    assert Path(cfg, "media-includes.txt").read_text().endswith("\n")
    assert ms.read_selection(cfg)["raw"] is not None

def test_write_rejects_traversal_and_newline(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        ms.write_selection(cfg, False, ["../evil"])
    with pytest.raises(ValueError):
        ms.write_selection(cfg, False, ["x\ny"])
    with pytest.raises(ValueError):
        ms.write_selection(cfg, False, ["/absolute"])
