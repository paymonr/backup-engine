"""Unit tests for the single vocabulary module (spec 4.3 + 10.3).

Every later screen imports app.gui.vocab for its user-facing names and for the
forbidden-term lists the vocabulary test (10.3) enforces. These tests pin the
exact strings from the spec so a later "cleanup" cannot quietly reword them.
"""
from app.gui import vocab


def test_type_names_are_the_night_shift_labels():
    # Spec 4.3, ruling R1 (global).
    assert vocab.TYPE_NAMES == {
        "versioned": "Snapshot backup",
        "archive": "Plain copy",
        "versioned-files": "File history",
    }


def test_class_names_are_plain_meaning_dot_constant():
    # Spec 4.3 storage-class row (global Night Shift phrasing).
    assert vocab.CLASS_NAMES == {
        "STANDARD": "Instant · STANDARD",
        "STANDARD_IA": "Instant, cheaper · STANDARD_IA",
        "GLACIER_IR": "Instant, archive-priced · GLACIER_IR",
        "GLACIER": "Thaw first, minutes–hours · GLACIER",
        "DEEP_ARCHIVE": "Thaw first, hours · DEEP_ARCHIVE",
    }


def test_class_names_cover_every_storage_class():
    from app.estimator.model import STORAGE_CLASSES
    assert set(vocab.CLASS_NAMES) == set(STORAGE_CLASSES)


def test_state_names_are_the_state_token_words():
    # Spec 4.7 token text; keys are the internal state constants.
    assert vocab.STATE_NAMES == {
        "RUNNING": "Running",
        "PAUSED": "Paused",
        "OVERDUE": "Overdue",
        "FAILED": "Failed",
        "OK": "OK",
        "NOT_RUN_YET": "Not run yet",
    }


def test_forbidden_terms_is_exactly_the_spec_list():
    # Spec 10.3. Bare "thaw" is deliberately NOT here (it is the tier name).
    assert vocab.FORBIDDEN_TERMS == {
        "restic", "rclone", "repository", "snapshot_id", "egress", "ingest",
        "thawing", "thawed", "restore-request", "churn", "retention", "prune",
        "catalog", "vfiles", "versioned-files", "Bulk copy", "Mirror",
        "OpenTofu", "supercronic", "steady state", "steady-state",
    }
    assert isinstance(vocab.FORBIDDEN_TERMS, set)
    assert "thaw" not in vocab.FORBIDDEN_TERMS


def test_allowed_phrases_protect_the_tier_name():
    # Spec 10.3: at least these must be assertable as present.
    assert vocab.ALLOWED_PHRASES == {"Thaw first", "thaw-first"}
    assert isinstance(vocab.ALLOWED_PHRASES, set)


def test_term_exemptions_are_keyed_by_url_prefix():
    # Spec 10.3 per-page exemption table -- the only exemptions.
    assert set(vocab.TERM_EXEMPTIONS) == {
        "/setup/about", "/setup/destination", "/setup/keys",
    }
    # About is the glossary: exempt from every term.
    assert vocab.TERM_EXEMPTIONS["/setup/about"] == vocab.FORBIDDEN_TERMS
    # Destination names the tool that creates the bucket.
    assert vocab.TERM_EXEMPTIONS["/setup/destination"] == {"OpenTofu"}
    # Keys prints the RESTIC_* env key names beside their plain names.
    assert vocab.TERM_EXEMPTIONS["/setup/keys"] == {"restic", "repository"}
    for terms in vocab.TERM_EXEMPTIONS.values():
        assert isinstance(terms, set)
