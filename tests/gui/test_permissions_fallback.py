# tests/gui/test_permissions_fallback.py — the no-admin-creds path: a convergent
# bash script + runtime-key-only Verify probes (spec 2026-09-22 §2).
import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest
from app.gui import jobs_io, permissions

ACCOUNT = "123456789012"
USER_ARN = f"arn:aws:iam::{ACCOUNT}:user/backup-engine-runtime"
BUCKET = f"unraid-backup-{ACCOUNT}"
P = permissions.parse_principal(USER_ARN)


def _script():
    return permissions.script(P, bucket=BUCKET, region="us-east-1")


def test_script_is_valid_bash():
    r = subprocess.run(["bash", "-n"], input=_script(), text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_script_embeds_exactly_the_required_documents():
    s = _script()
    docs = dict(re.findall(r"cat > \"\$d/([\w-]+)\.json\" <<'EOF'\n(.*?)\nEOF", s, re.S))
    want = permissions.required_docs(P, BUCKET)
    assert set(docs) == {"trust", "bucket-admin", "runtime"}
    assert json.loads(docs["trust"]) == want["trust"]
    assert json.loads(docs["bucket-admin"]) == want["role"]
    assert json.loads(docs["runtime"]) == want["runtime"]


def test_script_only_creates_what_is_missing_and_never_touches_keys_or_data():
    s = _script()
    assert "aws iam get-role --role-name backup-engine-bucket-admin >/dev/null 2>&1 || aws iam create-role" in s
    for bad in ("access-key", "s3api", "aws s3 ", "delete-", "create-policy-version", "create-policy ",
                "attach-user-policy "):
        assert bad not in s


def test_script_extra_buckets_detach_line_is_a_comment_only():
    s = _script()
    lines = s.splitlines()
    detach_lines = [l for l in lines if "backup-engine-runtime-extra-buckets" in l]
    assert detach_lines
    assert all(l.lstrip().startswith("#") for l in detach_lines)


def test_script_runs_in_order_against_a_stub_aws(tmp_path):
    log = tmp_path / "calls"
    stub = tmp_path / "bin"; stub.mkdir()
    (stub / "aws").write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$1 $2\" >> {log}\n"
        "for a in \"$@\"; do case \"$a\" in file://*) test -s \"${a#file://}\" || exit 9;; esac; done\n"
        "case \"$1 $2\" in 'iam get-role') exit 1;; esac\n"
        "exit 0\n")
    (stub / "aws").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", _script()], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [
        "iam get-role", "iam create-role", "iam update-assume-role-policy",
        "iam put-role-policy", "iam put-user-policy"]


@pytest.mark.parametrize("principal,bucket,region", [
    (permissions.Principal(ACCOUNT, "x; rm -rf /", USER_ARN), BUCKET, "us-east-1"),
    (permissions.Principal("12345", "backup-engine-runtime", USER_ARN), BUCKET, "us-east-1"),
    (P, "Bad_Bucket;id", "us-east-1"),
    (P, BUCKET, "us-east-1; id"),
    # Shape checks (review Minor 6): match()+"$" tolerates a trailing "\n" (it
    # matches just before it) -- fullmatch closes that hole.
    (P, BUCKET, "us-east-1\n"),
    (permissions.Principal(ACCOUNT, "backup-engine-runtime\n", USER_ARN), BUCKET, "us-east-1"),
    (P, "good-name\n", "us-east-1"),
    # re.ASCII: without it \d also matches a Unicode look-alike digit (a
    # full-width "２"), which could sneak a 12-"digit"-looking account past the
    # check ("12345678901２" is 12 CHARACTERS, the last one not ASCII).
    (permissions.Principal("12345678901２", "backup-engine-runtime", USER_ARN), BUCKET, "us-east-1"),
])
def test_script_refuses_unexpected_values(principal, bucket, region):
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.script(principal, bucket=bucket, region=region)
    assert e.value.kind == "script"


# --- verify ------------------------------------------------------------------------

def _fake(*, list_ok=True, assume_ok=True, policy_ok=True, base_denied=True,
          base_tag_success=False, rules_ok=True, flaky=0, version_delete_denied=True):
    state = {"flaky": flaky}

    def run(args, *, region, key, secret, session_token=None):
        if args[:2] == ["s3api", "list-object-versions"]:
            if state["flaky"] > 0:
                state["flaky"] -= 1
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied")
            return SimpleNamespace(returncode=0 if list_ok else 254, stdout="{}",
                                   stderr="" if list_ok else f"AccessDenied {secret}")
        if args[:2] == ["sts", "assume-role"]:
            if not assume_ok:
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied sts:AssumeRole")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Credentials": {
                "AccessKeyId": "ASIATMP", "SecretAccessKey": "tmpsek", "SessionToken": "tok"}}))
        if args[:2] == ["s3api", "list-buckets"]:
            assert session_token == "tok" and key == "ASIATMP"     # runs as the ROLE
            if not policy_ok:
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied s3:ListAllMyBuckets")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Buckets": []}))
        if args[:2] == ["s3api", "get-bucket-tagging"]:
            assert session_token == "tok" and key == "ASIATMP"     # runs as the ROLE
            # The narrowed role only grants GetBucketTagging on <base>-* -- the BASE
            # bucket must come back AccessDenied. A wide role gets through: either a
            # real TagSet (rc 0) or NoSuchTagSet when the bucket has none.
            if base_denied:
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied s3:GetBucketTagging")
            if base_tag_success:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"TagSet": []}), stderr="")
            return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (NoSuchTagSet) when calling the GetBucketTagging operation")
        if args[:2] == ["s3api", "get-bucket-lifecycle-configuration"]:
            assert session_token == "tok" and key == "ASIATMP"     # runs as the ROLE
            # BaseBucketRules grants GetBucketLifecycleConfiguration on the base
            # bucket -- a level-4 role can read it (a real config, or
            # NoSuchLifecycleConfiguration when none is set yet); a level-3 role
            # (no BaseBucketRules) gets AccessDenied.
            if rules_ok:
                return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (NoSuchLifecycleConfiguration) when calling the GetBucketLifecycleConfiguration operation")
            return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied s3:GetBucketLifecycleConfiguration")
        if args[:2] == ["s3api", "delete-object"]:
            assert session_token is None and key == "AKIARUN"     # runs as the BACKUP key
            assert args[args.index("--version-id") + 1] == "null"
            assert args[args.index("--key") + 1].startswith(permissions.VERSION_PROBE_PREFIX)
            if version_delete_denied:
                return SimpleNamespace(returncode=254, stdout="", stderr=(
                    "An error occurred (AccessDenied) when calling the DeleteObject operation: Access Denied"))
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        raise AssertionError(args)
    return run


def _verify(run, **kw):
    return permissions.verify(P, bucket=BUCKET, region="us-east-1", key="AKIARUN", secret="runsek",
                              run=run, sleep=lambda s: None, **kw)


def test_verify_all_good():
    probes = _verify(_fake())
    assert len(probes) == 6 and all(p.ok for p in probes)


def test_verify_names_the_script_step_when_the_role_is_missing():
    probes = _verify(_fake(assume_ok=False))
    assert [p.ok for p in probes] == [True, False, False, False, False, True]
    assert "step 1" in probes[1].hint
    # The third, fourth and fifth probes (all need the role, which just failed)
    # carry their OWN dependent hint rather than re-running their own checks.
    assert "Needs the role first" in probes[2].hint and "step 1" in probes[2].hint
    assert "Needs the role first" in probes[3].hint and "step 1" in probes[3].hint
    assert "Needs the role first" in probes[4].hint and "step 1" in probes[4].hint


def test_verify_detects_the_role_policy_is_missing():
    probes = _verify(_fake(policy_ok=False))
    assert [p.ok for p in probes] == [True, True, False, True, True, True]
    assert "step 2" in probes[2].hint


def test_verify_detects_an_old_unscoped_role():
    # An old, wide role policy still lets the assumed creds read the BASE
    # bucket's tags -- Verify must never stamp that as ok. Covers the exit-0
    # branch (a real TagSet comes back); test_verify_counts_no_such_tag_set_as_a_wide_role
    # covers the NoSuchTagSet branch.
    probes = _verify(_fake(base_denied=False, base_tag_success=True))
    assert [p.ok for p in probes] == [True, True, True, False, True, True]
    assert not all(p.ok for p in probes)
    assert "older, wider policy" in probes[3].hint


def test_verify_counts_no_such_tag_set_as_a_wide_role():
    probes = _verify(_fake(base_denied=False))
    assert probes[3].ok is False and "older, wider policy" in probes[3].hint


def test_verify_detects_a_level_three_role_missing_base_bucket_rules():
    # A level-3 role (no BaseBucketRules) passes the four probes above it too --
    # it can assume the role, its policy is present, and it's still correctly
    # denied the base bucket's tags. Only the 5th probe (lifecycle configuration,
    # granted solely by BaseBucketRules) catches the gap.
    probes = _verify(_fake(rules_ok=False))
    assert [p.ok for p in probes] == [True, True, True, True, False, True]
    assert not all(p.ok for p in probes)
    assert "step 2" in probes[4].hint


def test_verify_scoped_probe_runs_with_the_assumed_creds():
    seen = {}

    def run(args, *, region, key, secret, session_token=None):
        if args[:2] == ["s3api", "get-bucket-tagging"]:
            seen["key"], seen["secret"], seen["token"] = key, secret, session_token
            return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied")
        return _fake()(args, region=region, key=key, secret=secret, session_token=session_token)

    _verify(run, tries=1)
    assert seen == {"key": "ASIATMP", "secret": "tmpsek", "token": "tok"}


def test_verify_scoped_probe_scrubs_a_non_access_denied_error():
    def run(args, *, region, key, secret, session_token=None):
        if args[:2] == ["s3api", "list-object-versions"]:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if args[:2] == ["sts", "assume-role"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Credentials": {
                "AccessKeyId": "ASIATMP", "SecretAccessKey": "tmpsek",
                "SessionToken": "tmpsessiontoken"}}))
        if args[:2] == ["s3api", "list-buckets"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Buckets": []}))
        if args[:2] == ["s3api", "get-bucket-tagging"]:
            return SimpleNamespace(
                returncode=254, stdout="",
                stderr="SlowDown for key=ASIATMP secret=tmpsek session=tmpsessiontoken")
        if args[:2] == ["s3api", "get-bucket-lifecycle-configuration"]:
            return SimpleNamespace(
                returncode=254, stdout="",
                stderr="An error occurred (NoSuchLifecycleConfiguration) when calling the "
                       "GetBucketLifecycleConfiguration operation")
        if args[:2] == ["s3api", "delete-object"]:
            return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied")
        raise AssertionError(args)
    probes = _verify(run, tries=1)
    assert probes[3].ok is False
    assert "try Verify again" in probes[3].hint
    assert "ASIATMP" not in probes[3].detail
    assert "tmpsek" not in probes[3].detail
    assert "tmpsessiontoken" not in probes[3].detail


def test_verify_retries_for_iam_propagation():
    slept = []
    probes = permissions.verify(P, bucket=BUCKET, region="us-east-1", key="AKIARUN", secret="runsek",
                                run=_fake(flaky=1), sleep=slept.append)
    assert all(p.ok for p in probes) and slept == [5.0]


def test_verify_scrubs_the_runtime_secret():
    probes = _verify(_fake(list_ok=False), tries=1)
    assert "runsek" not in probes[0].detail
    assert "step 3" in probes[0].hint


def test_verify_scrubs_the_assumed_role_creds_too():
    # Probe 3 runs with the TEMPORARY assumed-role creds, not the runtime key --
    # a failing s3api list-buckets whose stderr echoes them must still come back
    # scrubbed.
    def run(args, *, region, key, secret, session_token=None):
        if args[:2] == ["s3api", "list-object-versions"]:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if args[:2] == ["sts", "assume-role"]:
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Credentials": {
                "AccessKeyId": "ASIATMP", "SecretAccessKey": "tmpsek",
                "SessionToken": "tmpsessiontoken"}}))
        if args[:2] == ["s3api", "list-buckets"]:
            return SimpleNamespace(
                returncode=254, stdout="",
                stderr="AccessDenied for key=ASIATMP secret=tmpsek token=tmpsessiontoken")
        if args[:2] == ["s3api", "get-bucket-tagging"]:
            return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied")
        if args[:2] == ["s3api", "get-bucket-lifecycle-configuration"]:
            return SimpleNamespace(
                returncode=254, stdout="",
                stderr="An error occurred (NoSuchLifecycleConfiguration) when calling the "
                       "GetBucketLifecycleConfiguration operation")
        if args[:2] == ["s3api", "delete-object"]:
            return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied")
        raise AssertionError(args)
    probes = _verify(run, tries=1)
    assert probes[2].ok is False
    assert "ASIATMP" not in probes[2].detail
    assert "tmpsek" not in probes[2].detail
    assert "tmpsessiontoken" not in probes[2].detail


def test_verify_proves_the_backup_key_cannot_permanently_delete_old_versions():
    probes = _verify(_fake())
    assert probes[5].name == "The backup key can't permanently delete old versions" and probes[5].ok


def test_verify_flags_a_backup_key_that_still_deletes_old_versions():
    probes = _verify(_fake(version_delete_denied=False), tries=1)
    assert probes[5].ok is False and "step 3" in probes[5].hint


def test_the_version_probe_runs_even_when_the_role_is_missing():
    probes = _verify(_fake(assume_ok=False), tries=1)
    assert probes[5].ok is True


def test_the_version_probe_names_a_key_no_job_can_own():
    # "~" is outside the job-name charset, so the probe can never touch a real job's folder.
    assert "~" in permissions.VERSION_PROBE_PREFIX
    assert not jobs_io.valid_name(permissions.VERSION_PROBE_PREFIX.split("/")[1])


# Parked P3 (T10): the Verify version-delete probe's other-error branch -- an aws failure that
# isn't AccessDenied proves nothing either way: not ok, "try Verify again", secret scrubbed.
def test_the_version_delete_probe_on_another_error_asks_to_verify_again():
    from types import SimpleNamespace
    from app.gui import permissions

    def run(args, *, region, key, secret):
        assert args[:2] == ["s3api", "delete-object"] and "--version-id" in args
        return SimpleNamespace(returncode=255, stdout="",
                               stderr=f"Could not connect to the endpoint URL (key {key} secret {secret})")
    p = permissions._version_delete_probe(bucket="b", region="us-east-1", key="AKIAPROBE",
                                          secret="s3cr3tvalue", run=run)
    assert p.ok is False and "try Verify again" in p.hint
    assert "s3cr3tvalue" not in p.detail and "Could not connect" in p.detail
