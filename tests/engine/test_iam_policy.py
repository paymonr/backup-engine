import json
import re
from pathlib import Path

from app.gui import provision


def test_iam_policy_has_required_version_actions():
    """Verify the IAM policy template includes required actions for archive version management."""
    # Read the policy template
    policy_path = Path(__file__).parent.parent.parent / "provisioning" / "iam-policy.json.tmpl"
    with open(policy_path) as f:
        policy_text = f.read()

    # Substitute placeholder with dummy ARN for JSON parsing
    policy_json = policy_text.replace("${bucket_arn}", "arn:aws:s3:::test-bucket")
    policy = json.loads(policy_json)

    # Find the statements by SID
    statements_by_sid = {stmt["Sid"]: stmt for stmt in policy["Statement"]}

    # Verify ListBucketScoped statement has s3:ListBucketVersions
    list_bucket_stmt = statements_by_sid["ListBucketScoped"]
    assert "s3:ListBucketVersions" in list_bucket_stmt["Action"], \
        f"ListBucketScoped statement missing s3:ListBucketVersions. Current actions: {list_bucket_stmt['Action']}"

    # Level 4 (spec 2026-09-23 §6): the backup key may no longer PERMANENTLY delete
    # old versions — Plain copy history is S3's job now, so only soft deletes remain.
    object_rw_stmt = statements_by_sid["ObjectRW"]
    assert "s3:DeleteObjectVersion" not in object_rw_stmt["Action"]


def test_runtime_policy_never_permanently_deletes_versions():
    role_arn = "arn:aws:iam::123456789012:role/backup-engine-bucket-admin"
    doc = provision.render_policy("unraid-backup-123", role_arn)
    assert "s3:DeleteObjectVersion" not in doc
    assert "s3:DeleteObject" in doc          # soft deletes (restic / File history) still work


def test_role_manages_exactly_the_base_buckets_rules():
    stmts = {s["Sid"]: s for s in json.loads(
        provision.render_bucket_admin_policy("unraid-backup-123"))["Statement"]}
    base = stmts["BaseBucketRules"]
    assert base["Resource"] == "arn:aws:s3:::unraid-backup-123"
    assert sorted(base["Action"]) == sorted([
        "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration",
        "s3:GetBucketVersioning", "s3:PutBucketVersioning"])
    assert "s3:GetLifecycleConfiguration" in stmts["CreateAndConfig"]["Action"]


def test_manual_policy_has_no_sts_or_wildcard():
    # render_policy(bucket) with NO role ARN is the MANUAL copy-paste flow's
    # render (routes.py's Guided screen). It must stay the ORIGINAL
    # object-only, single-bucket policy: no sts:AssumeRole, no <bucket>-*
    # wildcard. Resource:"*" on AssumeRole shown to a user as "least
    # privilege" would be a privilege-escalation primitive.
    doc = provision.render_policy("unraid-backup-123")
    assert "sts:AssumeRole" not in doc
    assert "unraid-backup-123-*" not in doc


def test_automated_policy_has_scoped_sts_and_wildcard():
    # The automated/tofu path renders with a CONCRETE bucket-admin role ARN --
    # then, and only then, the multi-bucket statements appear, and
    # sts:AssumeRole is scoped to that real ARN, never "*".
    role_arn = "arn:aws:iam::123456789012:role/backup-engine-bucket-admin"
    doc = provision.render_policy("unraid-backup-123", role_arn)
    parsed = json.loads(doc)
    stmts = {s["Sid"]: s for s in parsed["Statement"]}
    assert stmts["AssumeBucketAdminRole"]["Action"] == "sts:AssumeRole"
    assert stmts["AssumeBucketAdminRole"]["Resource"] == role_arn
    assert "arn:aws:s3:::unraid-backup-123-*/*" in doc


def test_bucket_admin_policy_has_create_and_config():
    doc = provision.render_bucket_admin_policy("unraid-backup-123")
    parsed = json.loads(doc)
    stmts = {s["Sid"]: s for s in parsed["Statement"]}
    for a in ("s3:CreateBucket", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
              "s3:PutLifecycleConfiguration", "s3:PutBucketTagging", "s3:PutBucketPublicAccessBlock"):
        assert a in stmts["CreateAndConfig"]["Action"]
    # DeleteBucket lives on the separate Teardown statement, never CreateAndConfig.
    assert "s3:DeleteBucket" not in stmts["CreateAndConfig"]["Action"]


def test_bucket_admin_policy_has_teardown_statements():
    # Task 11: enumerate-by-tag + empty + delete for just-in-time buckets (created
    # outside tofu, so `tofu destroy` can't remove them). Stays on this role only.
    # ListAllMyBuckets can't be resource-scoped (TeardownList, on "*"); the rest
    # of teardown is scoped to <bucket>-* (Teardown + TeardownObjects).
    doc = provision.render_bucket_admin_policy("unraid-backup-123")
    parsed = json.loads(doc)
    stmts = {s["Sid"]: s for s in parsed["Statement"]}
    assert stmts["TeardownList"]["Action"] == ["s3:ListAllMyBuckets"]
    assert stmts["TeardownList"]["Resource"] == "*"
    for a in ("s3:GetBucketTagging", "s3:ListBucketVersions", "s3:DeleteBucket"):
        assert a in stmts["Teardown"]["Action"]
    for a in ("s3:DeleteObject", "s3:DeleteObjectVersion"):
        assert a in stmts["TeardownObjects"]["Action"]


def test_bucket_admin_policy_is_scoped_to_the_base_prefix():
    # Addendum 2026-09-22 (prefix-only dedicated buckets), narrowed further by
    # level 4 (spec 2026-09-23 §6): every S3 statement except the un-scopable
    # TeardownList and the new BaseBucketRules (the role's one deliberate,
    # narrowly-scoped window onto the base bucket's lifecycle + versioning) is
    # confined to <base>-*, so create/config/teardown can never touch the base
    # bucket itself.
    bucket = "unraid-backup-123"
    doc = provision.render_bucket_admin_policy(bucket)
    parsed = json.loads(doc)
    stmts = {s["Sid"]: s for s in parsed["Statement"]}
    assert set(stmts) == {"CreateAndConfig", "TeardownList", "Teardown", "TeardownObjects",
                          "BaseBucketRules"}
    for sid, stmt in stmts.items():
        if sid in ("TeardownList", "BaseBucketRules"):
            continue
        resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
        for r in resources:
            assert r.startswith(f"arn:aws:s3:::{bucket}-"), (sid, r)
    # The base bucket itself, exact, appears nowhere EXCEPT BaseBucketRules.
    for sid, stmt in stmts.items():
        if sid == "BaseBucketRules":
            continue
        resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
        assert f"arn:aws:s3:::{bucket}" not in resources, (sid, resources)


def test_bucket_admin_policy_has_no_iam_actions():
    # The privilege-escalation fix: the role can no longer touch IAM at all.
    doc = provision.render_bucket_admin_policy("unraid-backup-123")
    assert '"iam:' not in doc
    for bad in ("iam:PutUserPolicy", "iam:AttachUserPolicy", "iam:CreatePolicy\"",
                "iam:*", "iam:PassRole", "iam:CreatePolicyVersion"):
        assert bad not in doc


def test_main_tf_has_no_extra_buckets_resources():
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    assert "runtime_extra_buckets" not in main_tf
    assert "extra_buckets_policy_arn" not in main_tf


def test_outputs_tf_has_no_extra_buckets_policy_arn():
    outputs = (provision.OPENTOFU_DIR / "outputs.tf").read_text()
    assert "runtime_extra_buckets_policy_arn" not in outputs


def test_tofu_outputs_the_runtime_user_arn():
    outputs = (provision.OPENTOFU_DIR / "outputs.tf").read_text()
    assert 'output "runtime_user_arn"' in outputs
    assert "aws_iam_user.runtime.arn" in outputs


def test_tofu_no_longer_owns_lifecycle_rules():
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    variables = (provision.OPENTOFU_DIR / "variables.tf").read_text()
    versions = (provision.OPENTOFU_DIR / "versions.tf").read_text()
    assert 'resource "aws_s3_bucket_lifecycle_configuration"' not in main_tf
    assert "noncurrent_version_expiration_days" not in variables
    assert "abort_incomplete_multipart_days" not in variables
    # M3: old state that still tracks the backstop configuration FORGETS it (never destroys
    # it -- a destroy would delete the app's rules and the owner's console rules with it).
    assert re.search(r"removed\s*\{\s*from\s*=\s*aws_s3_bucket_lifecycle_configuration\.backup\s*\}", main_tf)
    assert "destroy = true" not in main_tf
    assert 'required_version = ">= 1.7.0"' in versions
