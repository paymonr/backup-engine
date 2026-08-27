import json
import re
import pytest
from app.gui import provision


def test_render_policy_is_valid_json_scoped_to_bucket():
    doc = json.loads(provision.render_policy("acme-backups"))
    assert doc["Version"] == "2012-10-17"
    stmts = {s["Sid"]: s for s in doc["Statement"]}
    assert stmts["ListBucketScoped"]["Resource"] == "arn:aws:s3:::acme-backups"
    assert stmts["ObjectRW"]["Resource"] == [
        "arn:aws:s3:::acme-backups/appdata/*",
        "arn:aws:s3:::acme-backups/media/*",
    ]


def test_render_policy_action_set_matches_least_privilege():
    stmts = {s["Sid"]: s for s in json.loads(provision.render_policy("b"))["Statement"]}
    assert stmts["ListBucketScoped"]["Action"] == ["s3:ListBucket", "s3:GetBucketLocation"]
    assert stmts["ObjectRW"]["Action"] == [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:RestoreObject",
    ]


def test_policy_template_has_exactly_one_placeholder():
    txt = provision.POLICY_TEMPLATE.read_text()
    assert set(re.findall(r"\$\{(\w+)\}", txt)) == {"bucket_arn"}


def test_tofu_module_consumes_canonical_policy_template():
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    assert "templatefile(" in main_tf
    assert "../provisioning/iam-policy.json.tmpl" in main_tf
    # the old inline policy-document block is gone (single source of truth)
    assert 'data "aws_iam_policy_document" "runtime"' not in main_tf


def _ok(**_):
    class CP:
        returncode = 0
        stdout = ""
        stderr = ""
    return CP()


def test_validate_runs_list_put_get_delete_in_order():
    seen = []

    def fake(args, *, region, key, secret):
        seen.append(args[1])  # the s3api subcommand
        return _ok()

    provision.validate_runtime_key("b", "us-east-1", "AKIA", "sekret", run=fake)
    assert seen == ["list-objects-v2", "put-object", "get-object", "delete-object"]


def test_validate_probe_object_is_under_allowed_prefix():
    captured = {}

    def fake(args, *, region, key, secret):
        if args[1] == "put-object":
            captured["key"] = args[args.index("--key") + 1]
        return _ok()

    provision.validate_runtime_key("b", "us-east-1", "AKIA", "sekret", run=fake)
    assert captured["key"].startswith("appdata/")


def test_validate_raises_at_failing_step_and_scrubs_secret():
    def fake(args, *, region, key, secret):
        class CP:
            returncode = 0 if args[1] != "get-object" else 1
            stdout = ""
            stderr = "AccessDenied for sekret"
        return CP()

    with pytest.raises(provision.ValidationError) as ei:
        provision.validate_runtime_key("b", "us-east-1", "AKIA", "sekret", run=fake)
    assert ei.value.step == "get"
    assert "sekret" not in ei.value.detail and "***" in ei.value.detail
