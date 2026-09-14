# tests/gui/test_storage_advice.py — the positive "which type/class & why"
# recommendation (type_advice). class_advice's caveats are exercised elsewhere.
from app.gui import storage_advice


def test_type_advice_versioned_recommends_standard():
    g = storage_advice.type_advice("versioned", "STANDARD")
    assert g["recommend_class"] == "STANDARD"
    assert g["on_recommended"] is True
    assert "restic" in g["class_reason"]
    assert g["type_label"] and g["type_when"]


def test_type_advice_flags_non_recommended_class():
    g = storage_advice.type_advice("versioned", "GLACIER")
    assert g["recommend_class"] == "STANDARD"
    assert g["on_recommended"] is False


def test_type_advice_archive_and_versioned_files_recommend_deep_archive():
    assert storage_advice.type_advice("archive", "DEEP_ARCHIVE")["recommend_class"] == "DEEP_ARCHIVE"
    assert storage_advice.type_advice("archive", "DEEP_ARCHIVE")["on_recommended"] is True
    assert storage_advice.type_advice("versioned-files")["recommend_class"] == "DEEP_ARCHIVE"


def test_type_advice_no_class_leaves_on_recommended_none():
    g = storage_advice.type_advice("versioned")
    assert g["on_recommended"] is None


def test_type_advice_unknown_type_is_none():
    assert storage_advice.type_advice("bogus") is None
    assert storage_advice.type_advice("") is None


def test_every_type_guidance_recommends_a_real_storage_class():
    from app.estimator.model import STORAGE_CLASSES
    for t, g in storage_advice.TYPE_GUIDANCE.items():
        assert g["recommend_class"] in STORAGE_CLASSES, t
