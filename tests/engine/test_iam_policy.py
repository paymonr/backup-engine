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


def test_runtime_policy_has_sts_and_wildcard(tmp_path):
    doc = provision.render_policy("unraid-backup-123")
    assert "sts:AssumeRole" in doc
    assert "arn:aws:s3:::unraid-backup-123-*/*" in doc


def test_bucket_admin_policy_has_create_and_config():
    doc = provision.render_bucket_admin_policy("unraid-backup-123")
    for a in ("s3:CreateBucket", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
              "s3:PutLifecycleConfiguration", "s3:PutBucketTagging", "s3:PutBucketPublicAccessBlock"):
        assert a in doc
    assert "s3:DeleteBucket" not in doc     # delete lives on a separate teardown grant
