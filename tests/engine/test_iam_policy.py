import json
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

    # Verify ObjectRW statement has s3:DeleteObjectVersion
    object_rw_stmt = statements_by_sid["ObjectRW"]
    assert "s3:DeleteObjectVersion" in object_rw_stmt["Action"], \
        f"ObjectRW statement missing s3:DeleteObjectVersion. Current actions: {object_rw_stmt['Action']}"


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
    for a in ("s3:CreateBucket", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
              "s3:PutLifecycleConfiguration", "s3:PutBucketTagging", "s3:PutBucketPublicAccessBlock"):
        assert a in doc
    assert "s3:DeleteBucket" not in doc     # delete lives on a separate teardown grant


def test_bucket_admin_policy_config_actions_are_not_bucket_prefix_scoped():
    # The role is reachable only after AssumeRole and holds no object
    # read/delete and no DeleteBucket, so Resource:"*" here is contained --
    # and required, since the feature allows OFF-PREFIX bucket names (a
    # dedicated bucket need not be named "<base>-*"), so scoping create/config
    # actions to "<bucket>-*" would deny configuring an off-prefix bucket.
    doc = provision.render_bucket_admin_policy("unraid-backup-123")
    assert "unraid-backup-123-*" not in doc
    parsed = json.loads(doc)
    assert len(parsed["Statement"]) == 1
    assert parsed["Statement"][0]["Resource"] == "*"
