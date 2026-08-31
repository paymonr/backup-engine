from app.gui import storage_advice as sa
from app.estimator.prices import load_prices

P = load_prices("us-east-1")  # bundled table; STANDARD 0.023, DEEP_ARCHIVE min 180, dto 0.09

def _by(rows, name):
    return next(r for r in rows if r["name"] == name)

def test_info_covers_all_classes_in_model_order():
    rows = sa.storage_class_info(P)
    assert [r["name"] for r in rows] == [
        "STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"]

def test_info_standard_is_instant_no_min_no_retrieval():
    r = _by(sa.storage_class_info(P), "STANDARD")
    assert r["read_access"] == "instant"
    assert r["min_duration_days"] == 0
    assert r["cold"] is False
    assert r["retrieval"] == "none"
    assert r["rate_gb_month"] == P.storage_gb_month["STANDARD"]
    assert r["egress_per_gb"] == P.data_transfer_out_per_gb

def test_info_deep_archive_is_cold_with_min_and_retrieval():
    r = _by(sa.storage_class_info(P), "DEEP_ARCHIVE")
    assert r["cold"] is True
    assert r["min_duration_days"] == 180
    assert "thaw" in r["read_access"].lower()
    assert "$" in r["retrieval"]  # a rendered per-GB figure, not "none"

def test_advice_restic_on_cold_is_danger_and_steers_to_versioned_files():
    a = sa.class_advice("versioned", "DEEP_ARCHIVE", "0 3 * * *", None, P)
    assert any(x["level"] == "danger" and "restic" in x["text"].lower()
               and "versioned files" in x["text"].lower() for x in a)

def test_advice_restic_on_instant_has_no_danger():
    a = sa.class_advice("versioned", "GLACIER_IR", "0 3 * * *", None, P)
    assert not any(x["level"] == "danger" for x in a)

def test_advice_min_duration_note_present_for_cold_class():
    a = sa.class_advice("archive", "DEEP_ARCHIVE", "0 3 * * 0", None, P)
    assert any("180" in x["text"] and x["level"] in ("info", "warn") for x in a)

def test_advice_standard_has_no_min_duration_note():
    a = sa.class_advice("archive", "STANDARD", "0 3 * * *", None, P)
    assert not any("minimum" in x["text"].lower() for x in a)

def test_advice_high_churn_nudge_on_deep_archive_daily():
    # daily backups (freq high) + Deep Archive (180d) on an incremental type -> nudge
    a = sa.class_advice("versioned-files", "DEEP_ARCHIVE", "0 3 * * *", None, P)
    assert any(x["level"] == "warn" and "cheaper" in x["text"].lower() for x in a)

def test_advice_edit_transition_explainer_when_class_changed():
    a = sa.class_advice("archive", "GLACIER", "0 3 * * 0", "STANDARD", P)
    assert any("future" in x["text"].lower() and "STANDARD" in x["text"] for x in a)

def test_advice_edit_warmup_change_is_warn():
    # DEEP_ARCHIVE -> STANDARD is a warm-up (needs thaw+copy), stronger than info
    a = sa.class_advice("archive", "STANDARD", "0 3 * * 0", "DEEP_ARCHIVE", P)
    assert any(x["level"] == "warn" and "warm" in x["text"].lower() for x in a)

def test_advice_no_transition_note_when_class_unchanged():
    a = sa.class_advice("archive", "STANDARD", "0 3 * * *", "STANDARD", P)
    assert not any("future" in x["text"].lower() for x in a)

def test_advice_restic_on_retrieval_billed_instant_class_warns():
    # restic re-reads its repo every run; STANDARD_IA/GLACIER_IR bill retrieval on
    # each read -> per-run retrieval charge (spec §2). WARN, not a block.
    for cls in ("STANDARD_IA", "GLACIER_IR"):
        a = sa.class_advice("versioned", cls, "0 3 * * *", None, P)
        assert any(x["level"] == "warn" and "every run" in x["text"].lower()
                   and "retrieval" in x["text"].lower() for x in a), cls

def test_advice_restic_on_standard_has_no_retrieval_warning():
    # STANDARD has no retrieval fee -> no per-run retrieval warning
    a = sa.class_advice("versioned", "STANDARD", "0 3 * * *", None, P)
    assert not any("every run" in x["text"].lower() for x in a)

def test_advice_non_versioned_no_restic_retrieval_warning():
    # archive/versioned-files don't re-read a whole repo each run -> no restic note
    a = sa.class_advice("archive", "STANDARD_IA", "0 3 * * *", None, P)
    assert not any("re-reads its repository" in x["text"] for x in a)
