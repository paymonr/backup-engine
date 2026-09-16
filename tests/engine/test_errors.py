from app.engine import errors


def _classify(err, exit_code=1, outcome="failed", log_tail=""):
    return errors.classify(err, exit_code, outcome, log_tail)


# --- the §7.4 table, one case per row --------------------------------------

def test_manga_access_denied_delete_version_is_iam_version_perms():
    # Carry-forward from Task 2: the manga Plain-copy prune failure. The runner
    # puts the raw AccessDenied on s3:DeleteObjectVersion in the record's error.
    c = _classify("AccessDenied: s3:DeleteObjectVersion")
    assert c.code == "iam-version-perms"
    assert c.blocker is True
    assert c.short == "AccessDenied"
    assert c.fix_route == "/setup/destination"
    # board template carries the {dow}/{since} placeholders verbatim
    assert "{dow}" in c.board and "{since}" in c.board


def test_iam_version_perms_matches_list_object_versions_variant():
    assert _classify("AccessDenied on ListBucketVersions").code == "iam-version-perms"
    assert _classify("AccessDenied (list-object-versions)").code == "iam-version-perms"


def test_plain_access_denied_without_version_verb_is_access_denied():
    c = _classify("AccessDenied: s3:PutObject")
    assert c.code == "access-denied"
    assert c.blocker is True


def test_403_is_access_denied():
    assert _classify("HTTP 403 Forbidden").code == "access-denied"


def test_repository_already_locked_is_repo_locked():
    c = _classify("Fatal: repository is already locked by PID 1 on host")
    assert c.code == "repo-locked"
    assert c.blocker is False
    assert c.fix_route == "/jobs/<name>/edit"


def test_unable_to_create_lock_is_repo_locked():
    assert _classify("unable to create lock in backend").code == "repo-locked"


def test_wrong_passphrase():
    assert _classify("wrong password or no key found").code == "wrong-passphrase"
    assert _classify("no key could be found for the repository").code == "wrong-passphrase"


def test_cold_object():
    assert _classify("InvalidObjectState: the storage class is DEEP_ARCHIVE").code == "cold-object"
    c = _classify("operation is not valid for the object's storage class")
    assert c.code == "cold-object"
    assert c.blocker is False


def test_source_missing():
    c = _classify("job 'manga' source '/backup/media/comics' missing")
    assert c.code == "source-missing"
    assert c.blocker is True
    assert _classify("source root '/backup/media' not found").code == "source-missing"


def test_no_space():
    assert _classify("write failed: no space left on device").code == "no-space"


def test_killed_by_exit_code():
    assert errors.classify(None, 143, "failed").code == "killed"
    assert errors.classify(None, 137, "failed").code == "killed"


def test_killed_by_aborted_outcome():
    assert errors.classify(None, None, "aborted").code == "killed"


def test_busy():
    assert _classify("another manga run is in progress").code == "busy"


def test_unknown_falls_back_to_cause_with_first_token_short():
    c = _classify("SomethingWeird: it broke")
    assert c.code == "unknown"
    assert c.short == "SomethingWeird"
    assert c.blocker is False


def test_healthy_run_classifies_to_none():
    assert errors.classify(None, 0, "ok") is None
    assert errors.classify("", 0, "ok") is None
    assert errors.classify(None, None, "running") is None


# --- source precedence: error first, then the last 200 log lines -----------

def test_classify_reads_the_log_tail_when_error_is_generic():
    log = "line1\nAccessDenied when calling delete-object with a version-id\nlast\n"
    c = errors.classify("job exited with status 1", 1, "failed", log)
    assert c.code == "iam-version-perms"


def test_error_field_wins_over_the_log_tail():
    # error says repo-locked; the log also has AccessDenied further up. Error is
    # the authoritative short message, so repo-locked wins.
    log = "AccessDenied on delete-object earlier\n" + "\n".join(f"n{i}" for i in range(50))
    c = errors.classify("repository is already locked", 1, "failed", log)
    assert c.code == "repo-locked"


def test_log_tail_is_limited_to_last_200_lines():
    # benign padding with no digits so no pattern (e.g. "403") sneaks a match in
    head = "AccessDenied delete-object\n" + "\n".join("padding line here" for _ in range(500))
    # the AccessDenied is now far outside the last 200 lines, so it must not match
    c = errors.classify("job exited with status 1", 1, "failed", head)
    assert c.code == "unknown"


# --- every class is fully populated (surfaces exist) -----------------------

def test_all_classes_have_the_three_surfaces():
    for code in ("iam-version-perms", "access-denied", "repo-locked", "wrong-passphrase",
                 "cold-object", "source-missing", "no-space", "killed", "busy", "unknown"):
        ec = errors.CLASSES[code]
        assert ec.verdict and ec.cause and ec.fix
        assert isinstance(ec.blocker, bool)
