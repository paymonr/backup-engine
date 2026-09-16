"""One name per concept -- the single vocabulary module (spec 4.3, 4.4, 10.3).

Every later screen (templates and JSON) imports the user-facing names from
here, and the vocabulary test (10.3) enforces the forbidden-term lists below.
Storage tiers are always rendered ``plain meaning · CONSTANT``, never the
constant alone; the constant chip is the invariant that ties the two phrasings
(global and create-screen) to one concept.

The strings here are transcribed verbatim from spec 4.3 (the vocabulary table
and 4.7's state tokens) and 10.3 (the forbidden-term lists). Do not reword
them without changing the spec first.
"""

# --- Job types (4.3, ruling R1 -- global). ---------------------------------
# Internal key -> the only user-facing label for that job type.
TYPE_NAMES: dict[str, str] = {
    "versioned": "Snapshot backup",
    "archive": "Plain copy",
    "versioned-files": "File history",
}

# --- Storage tiers (4.3 storage-class row -- global Night Shift phrasing). --
# The create/edit screen uses a different blend phrasing (4.3 note 1); that
# variant belongs to that screen's task, not to this global map. The constant
# is always shown as a <code> chip alongside these phrases.
CLASS_NAMES: dict[str, str] = {
    "STANDARD": "Instant · STANDARD",
    "STANDARD_IA": "Instant, cheaper · STANDARD_IA",
    "GLACIER_IR": "Instant, archive-priced · GLACIER_IR",
    "GLACIER": "Thaw first, minutes–hours · GLACIER",
    "DEEP_ARCHIVE": "Thaw first, hours · DEEP_ARCHIVE",
}

# --- Type lines (4.3 / 5.1 -- the job page `.typeline` and the Board row hover).
# The vocabulary table's own sentences; verbatim, global (ruling R1).
TYPE_LINES: dict[str, str] = {
    "versioned": "Snapshot backup — a point in time, so you can restore any date.",
    "archive": "Plain copy — a straight copy of big, static files. No history.",
    "versioned-files": "File history — keeps every version of every file.",
}

# --- Job states (4.7 state-token text; keys are the internal constants). ----
STATE_NAMES: dict[str, str] = {
    "RUNNING": "Running",
    "PAUSED": "Paused",
    "OVERDUE": "Overdue",
    "FAILED": "Failed",
    "OK": "OK",
    "NOT_RUN_YET": "Not run yet",
}

# --- Vocabulary test lists (10.3). -----------------------------------------
# Case-sensitive, whole-word forbidden terms. Bare "thaw" is deliberately NOT
# here: the tier name "Thaw first, hours · DEEP_ARCHIVE" and its lowercase form
# "thaw-first tier" are the vocabulary (4.3), protected by ALLOWED_PHRASES.
FORBIDDEN_TERMS: set[str] = {
    "restic",
    "rclone",
    "repository",
    "snapshot_id",
    "egress",
    "ingest",
    "thawing",
    "thawed",
    "restore-request",
    "churn",
    "retention",
    "prune",
    "catalog",
    "vfiles",
    "versioned-files",
    "Bulk copy",
    "Mirror",
    "OpenTofu",
    "supercronic",
    "steady state",
    "steady-state",
}

# Asserted positively so a later cleanup cannot quietly delete the tier name.
ALLOWED_PHRASES: set[str] = {"Thaw first", "thaw-first"}

# Per-page exemptions, keyed by URL prefix -- the only ones; every other page
# is checked against the full FORBIDDEN_TERMS list.
TERM_EXEMPTIONS: dict[str, set[str]] = {
    # The glossary (5.13): exempt from every term.
    "/setup/about": set(FORBIDDEN_TERMS),
    # The preserved copy names the tool that creates the bucket (5.11).
    "/setup/destination": {"OpenTofu"},
    # The Recovery group prints RESTIC_PASSWORD / RESTIC_REPOSITORY beside the
    # plain names (5.12); the case-sensitive rule covers the env keys, this
    # makes the exemption intentional.
    "/setup/keys": {"restic", "repository"},
}
