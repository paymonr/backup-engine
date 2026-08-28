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
