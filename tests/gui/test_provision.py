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
    # s3:GetBucketVersioning added (spec 5.11) so a re-applied key can probe versioning.
    assert stmts["ListBucketScoped"]["Action"] == [
        "s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketVersions",
        "s3:GetBucketVersioning",
    ]
    assert stmts["ObjectRW"]["Action"] == [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:RestoreObject",
    ]


def test_policy_template_placeholders():
    # bucket_admin_role_arn added (multi-bucket feature) so the runtime user can
    # sts:AssumeRole the bucket-admin role; render_policy defaults it to "*" so
    # existing one-arg callers/tests are unaffected.
    txt = provision.POLICY_TEMPLATE.read_text()
    assert set(re.findall(r"\$\{(\w+)\}", txt)) == {"bucket_arn", "bucket_admin_role_arn"}


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


import json as _json
from pathlib import Path as _Path

TOFU_OUTPUT = _json.dumps({
    "runtime_access_key_id": {"value": "AKIARUNTIME", "sensitive": True},
    "runtime_secret_access_key": {"value": "runtimesecret", "sensitive": True},
    "bucket_name": {"value": "acme-backups"},
    "region": {"value": "us-east-1"},
})


def _fake_tofu(rec):
    def run(args, *, cwd, env):
        rec.setdefault("calls", []).append(args[0])
        rec["env"] = dict(env)
        rec["workdir"] = str(_Path(cwd).parent)
        rec["tfvars"] = _Path(cwd, "terraform.tfvars.json").read_text()

        class CP:
            returncode = 0
            stdout = TOFU_OUTPUT if args[0] == "output" else ""
            stderr = ""
        return CP()
    return run


def test_apply_passes_admin_creds_via_env_only_never_in_tfvars():
    rec = {}
    out = provision.run_tofu_apply("acme-backups", "us-east-1", "ADMINKEY", "ADMINSECRET",
                                   run=_fake_tofu(rec))
    assert rec["env"]["AWS_ACCESS_KEY_ID"] == "ADMINKEY"
    assert rec["env"]["AWS_SECRET_ACCESS_KEY"] == "ADMINSECRET"
    assert "ADMINKEY" not in rec["tfvars"] and "ADMINSECRET" not in rec["tfvars"]
    assert out["AWS_ACCESS_KEY_ID"] == "AKIARUNTIME"
    assert out["AWS_SECRET_ACCESS_KEY"] == "runtimesecret"
    assert out["bucket"] == "acme-backups" and out["region"] == "us-east-1"


def test_apply_order_is_init_apply_output():
    rec = {}
    provision.run_tofu_apply("b", "us-east-1", "K", "S", run=_fake_tofu(rec))
    assert rec["calls"] == ["init", "apply", "output"]


def test_apply_removes_tempdir_on_success():
    rec = {}
    provision.run_tofu_apply("b", "us-east-1", "K", "S", run=_fake_tofu(rec))
    assert not _Path(rec["workdir"]).exists()


def test_apply_failure_raises_scrubs_and_still_cleans_up():
    rec = {}

    def run(args, *, cwd, env):
        rec["workdir"] = str(_Path(cwd).parent)

        class CP:
            returncode = 1 if args[0] == "apply" else 0
            stdout = ""
            stderr = "boom ADMINSECRET leaked"
        return CP()

    with pytest.raises(provision.TofuError) as ei:
        provision.run_tofu_apply("b", "us-east-1", "ADMINKEY", "ADMINSECRET", run=run)
    assert ei.value.phase == "apply"
    assert "ADMINSECRET" not in ei.value.detail and "***" in ei.value.detail
    assert not _Path(rec["workdir"]).exists()


def test_apply_writes_tfvars_as_injection_safe_json():
    rec = {}
    evil = 'x"\nname_prefix = "evil'
    provision.run_tofu_apply(evil, "us-east-1", "K", "S", run=_fake_tofu(rec))
    parsed = _json.loads(rec["tfvars"])
    assert set(parsed.keys()) == {"bucket_name", "region"}
    assert parsed["bucket_name"] == evil   # malicious content stays a plain string value


TOFU_OUTPUT_WITH_MULTI_BUCKET = _json.dumps({
    "runtime_access_key_id": {"value": "AKIARUNTIME", "sensitive": True},
    "runtime_secret_access_key": {"value": "runtimesecret", "sensitive": True},
    "bucket_name": {"value": "acme-backups"},
    "region": {"value": "us-east-1"},
    "bucket_admin_role_arn": {"value": "arn:aws:iam::123456789012:role/backup-engine-bucket-admin"},
})


def test_apply_captures_bucket_admin_role_arn():
    def run(args, *, cwd, env):
        class CP:
            returncode = 0
            stdout = TOFU_OUTPUT_WITH_MULTI_BUCKET if args[0] == "output" else ""
            stderr = ""
        return CP()

    out = provision.run_tofu_apply("acme-backups", "us-east-1", "K", "S", run=run)
    assert out["bucket_admin_role_arn"] == "arn:aws:iam::123456789012:role/backup-engine-bucket-admin"
    assert "runtime_extra_buckets_policy_arn" not in out


def test_apply_tolerates_tofu_output_missing_multi_bucket_keys():
    # An older/stubbed `tofu output` without the multi-bucket outputs must not
    # raise -- run_tofu_apply reads them defensively.
    out = provision.run_tofu_apply("b", "us-east-1", "K", "S", run=_fake_tofu({}))
    assert out["bucket_admin_role_arn"] == ""


class _CP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_derive_bucket_name():
    assert provision.derive_bucket_name("123456789012") == "unraid-backup-123456789012"


def test_aws_account_id_reads_account_from_sts():
    def fake(args, *, region, key, secret, session_token=None):
        assert args[:2] == ["sts", "get-caller-identity"]
        return _CP(stdout="123456789012\n")
    assert provision.aws_account_id("us-east-1", "AKIA", "sek", run=fake) == "123456789012"


def test_aws_account_id_forwards_session_token():
    seen = {}

    def fake(args, *, region, key, secret, session_token=None):
        seen["token"] = session_token
        return _CP(stdout="123456789012")
    provision.aws_account_id("us-east-1", "AKIA", "sek", "TOKEN", run=fake)
    assert seen["token"] == "TOKEN"


def test_aws_account_id_raises_and_scrubs_secret_on_failure():
    def fake(args, *, region, key, secret, session_token=None):
        return _CP(returncode=1, stderr="AccessDenied for sek")
    with pytest.raises(provision.AccountLookupError) as ei:
        provision.aws_account_id("us-east-1", "AKIA", "sek", run=fake)
    assert "sek" not in ei.value.detail and "***" in ei.value.detail


def test_aws_account_id_rejects_non_account_output():
    def fake(args, *, region, key, secret, session_token=None):
        return _CP(stdout="not-an-account")
    with pytest.raises(provision.AccountLookupError):
        provision.aws_account_id("us-east-1", "AKIA", "sek", run=fake)


def test_console_steps_walk_through_bucket_creation():
    steps = " ".join(provision.render_console_steps("acme", "us-east-1")["steps"]).lower()
    assert "create bucket" in steps
    assert "versioning" in steps and "encryption" in steps
    assert "block public access" in steps and "lifecycle" in steps
    assert "unraid-backup" in steps  # suggested naming convention
    # spec 5.11: step 5 reads the retention default (180 days), matching the model.
    assert "expire old versions after 180 days" in steps


# --- verify_admin_can_provision (capability preflight) ----------------------
#
# `sts get-caller-identity` (aws_account_id above) succeeds for creds that
# CANNOT touch IAM -- e.g. a plain `aws sts get-session-token` session with no
# MFA. Those creds pass the account lookup, then blow up mid-`tofu apply` at
# the IAM user with InvalidClientTokenId, leaving an orphaned bucket. This
# probe catches that BEFORE tofu ever runs, using an ACCOUNT-LEVEL IAM read
# that works for both an IAM user and a role/SSO principal -- never
# `iam get-user`, which requires --user-name for a role/SSO caller and would
# falsely reject valid credentials that CAN provision.

def test_verify_admin_can_provision_success_probes_account_level_iam_read():
    seen = {}

    def fake(args, *, region, key, secret, session_token=None):
        seen["args"] = args
        return _CP(returncode=0, stdout="", stderr="")

    provision.verify_admin_can_provision("us-east-1", "AKIA", "sek", run=fake)
    assert seen["args"][0] == "iam"
    assert seen["args"][1] != "get-user"
    assert seen["args"][1] in ("get-account-summary", "list-account-aliases")


def test_verify_admin_can_provision_forwards_session_token():
    seen = {}

    def fake(args, *, region, key, secret, session_token=None):
        seen["token"] = session_token
        return _CP(returncode=0)

    provision.verify_admin_can_provision("us-east-1", "AKIA", "sek", "TOKEN", run=fake)
    assert seen["token"] == "TOKEN"


def test_verify_admin_can_provision_raises_token_kind_on_invalid_client_token_id():
    def fake(args, *, region, key, secret, session_token=None):
        return _CP(returncode=255, stderr="An error occurred (InvalidClientTokenId) when "
                                          "calling the GetAccountSummary operation: The "
                                          "security token included in the request is invalid.")

    with pytest.raises(provision.AdminCapabilityError) as ei:
        provision.verify_admin_can_provision("us-east-1", "ASIAFAKE", "sek", "tok", run=fake)
    assert ei.value.kind == "token"


def test_verify_admin_can_provision_raises_permission_kind_on_access_denied():
    def fake(args, *, region, key, secret, session_token=None):
        return _CP(returncode=254,
                   stderr="An error occurred (AccessDenied) when calling the "
                          "GetAccountSummary operation: User: arn:aws:iam::123:user/x is "
                          "not authorized to perform: iam:GetAccountSummary")

    with pytest.raises(provision.AdminCapabilityError) as ei:
        provision.verify_admin_can_provision("us-east-1", "AKIA", "sek", run=fake)
    assert ei.value.kind == "permission"


def test_verify_admin_can_provision_raises_unknown_kind_for_other_failures():
    def fake(args, *, region, key, secret, session_token=None):
        return _CP(returncode=1, stderr="some unrelated network error")

    with pytest.raises(provision.AdminCapabilityError) as ei:
        provision.verify_admin_can_provision("us-east-1", "AKIA", "sek", run=fake)
    assert ei.value.kind == "unknown"


def test_verify_admin_can_provision_scrubs_key_secret_and_token_from_detail():
    def fake(args, *, region, key, secret, session_token=None):
        return _CP(returncode=1, stderr="AccessDenied for AKIA and sek and tok123")

    with pytest.raises(provision.AdminCapabilityError) as ei:
        provision.verify_admin_can_provision("us-east-1", "AKIA", "sek", "tok123", run=fake)
    assert "AKIA" not in ei.value.detail
    assert "sek" not in ei.value.detail
    assert "tok123" not in ei.value.detail
    assert "***" in ei.value.detail


def test_run_tofu_apply_returns_runtime_user_arn():
    import json as _json
    from types import SimpleNamespace
    out = {"runtime_access_key_id": {"value": "AKIARUN"},
           "runtime_secret_access_key": {"value": "runsek"},
           "bucket_name": {"value": "acme"}, "region": {"value": "us-east-1"},
           "runtime_user_arn": {"value": "arn:aws:iam::123456789012:user/backup-engine-runtime"}}

    def fake(args, *, cwd, env):
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=_json.dumps(out) if args[0] == "output" else "")

    r = provision.run_tofu_apply("acme", "us-east-1", "AK", "SK", run=fake)
    assert r["runtime_user_arn"] == "arn:aws:iam::123456789012:user/backup-engine-runtime"


def test_run_tofu_apply_tolerates_missing_runtime_user_arn():
    import json as _json
    from types import SimpleNamespace
    out = {"runtime_access_key_id": {"value": "A"}, "runtime_secret_access_key": {"value": "S"},
           "bucket_name": {"value": "b"}, "region": {"value": "us-east-1"}}

    def fake(args, *, cwd, env):
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=_json.dumps(out) if args[0] == "output" else "")

    assert provision.run_tofu_apply("b", "us-east-1", "AK", "SK", run=fake)["runtime_user_arn"] == ""


def test_guided_cli_uses_an_inline_user_policy():
    # Spec 2026-09-22 §4: the full policy later lands on the same inline name via
    # put-user-policy, so guided setup no longer creates a managed policy.
    cli = "\n".join(provision.render_console_steps("acme", "us-east-1")["cli"])
    assert ("aws iam put-user-policy --user-name backup-engine-runtime "
            "--policy-name backup-engine-runtime-object-only --policy-document file://iam-policy.json") in cli
    assert "create-policy" not in cli and "attach-user-policy" not in cli
    assert cli.index("create-user") < cli.index("put-user-policy") < cli.index("create-access-key")


def test_guided_console_steps_create_an_inline_policy():
    steps = " ".join(provision.render_console_steps("acme", "us-east-1")["steps"]).lower()
    assert "create inline policy" in steps
    assert "backup-engine-runtime-object-only" in steps
