# tests/gui/test_iam_policy.py — the runtime IAM policy template (spec 5.11).
#
# The least-privilege runtime key must be able to READ bucket versioning so a
# re-applied key can probe "Old versions protected" (5.10). Spec 5.11 adds
# s3:GetBucketVersioning to the bucket-scoped statement of
# provisioning/iam-policy.json.tmpl — the same template the OpenTofu module
# renders (opentofu/main.tf's templatefile), so one change covers both.
import json
from app.gui import provision


def test_template_grants_get_bucket_versioning_in_bucket_scoped_statement():
    doc = json.loads(provision.render_policy("bw-backups"))
    stmts = {s["Sid"]: s for s in doc["Statement"]}
    scoped = stmts["ListBucketScoped"]
    # the new action, in the bucket-scoped statement (Resource is the bucket arn)
    assert "s3:GetBucketVersioning" in scoped["Action"]
    assert scoped["Resource"] == "arn:aws:s3:::bw-backups"
    # it is NOT smuggled into the object-scoped statement
    assert "s3:GetBucketVersioning" not in stmts["ObjectRW"]["Action"]


def test_raw_template_text_contains_the_new_action():
    txt = provision.POLICY_TEMPLATE.read_text()
    assert "s3:GetBucketVersioning" in txt


def test_opentofu_policy_is_the_same_template():
    # opentofu/main.tf renders the very same template, so the tofu runtime policy
    # gains the action too (spec 5.11 "and the matching OpenTofu policy").
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    assert "../provisioning/iam-policy.json.tmpl" in main_tf
    assert "templatefile(" in main_tf
