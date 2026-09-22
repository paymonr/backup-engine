# tests/gui/test_permissions_fallback.py — the no-admin-creds path: a convergent
# bash script + runtime-key-only Verify probes (spec 2026-09-22 §2).
import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest
from app.gui import permissions

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
])
def test_script_refuses_unexpected_values(principal, bucket, region):
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.script(principal, bucket=bucket, region=region)
    assert e.value.kind == "script"


# --- verify ------------------------------------------------------------------------

def _fake(*, list_ok=True, assume_ok=True, policy_ok=True, flaky=0):
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
        raise AssertionError(args)
    return run


def _verify(run, **kw):
    return permissions.verify(P, bucket=BUCKET, region="us-east-1", key="AKIARUN", secret="runsek",
                              run=run, sleep=lambda s: None, **kw)


def test_verify_all_good():
    probes = _verify(_fake())
    assert len(probes) == 3 and all(p.ok for p in probes)


def test_verify_names_the_script_step_when_the_role_is_missing():
    probes = _verify(_fake(assume_ok=False))
    assert [p.ok for p in probes] == [True, False, False]
    assert "step 1" in probes[1].hint


def test_verify_detects_the_role_policy_is_missing():
    probes = _verify(_fake(policy_ok=False))
    assert [p.ok for p in probes] == [True, True, False]
    assert "step 2" in probes[2].hint


def test_verify_retries_for_iam_propagation():
    slept = []
    probes = permissions.verify(P, bucket=BUCKET, region="us-east-1", key="AKIARUN", secret="runsek",
                                run=_fake(flaky=1), sleep=slept.append)
    assert all(p.ok for p in probes) and slept == [5.0]


def test_verify_scrubs_the_runtime_secret():
    probes = _verify(_fake(list_ok=False), tries=1)
    assert "runsek" not in probes[0].detail
