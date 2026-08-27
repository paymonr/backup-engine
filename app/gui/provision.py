# app/gui/provision.py — renders the canonical IAM policy, runs the Test & Validate
# S3 round-trip with a runtime key, and drives a one-shot OpenTofu apply for automated
# provisioning. The ONLY module that renders the policy / calls aws / invokes tofu.
# It never writes secrets itself — it returns discovered values to the route, which
# calls config_io.write_secrets (the single secret writer).
from __future__ import annotations
import json
import os
import secrets as _secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from string import Template

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_TEMPLATE = REPO_ROOT / "provisioning" / "iam-policy.json.tmpl"
OPENTOFU_DIR = REPO_ROOT / "opentofu"
PROVISIONING_DIR = REPO_ROOT / "provisioning"


def bucket_arn(bucket: str) -> str:
    return f"arn:aws:s3:::{bucket}"


def render_policy(bucket: str, tmpl_path: str | Path = POLICY_TEMPLATE) -> str:
    tmpl = Path(tmpl_path).read_text()
    return Template(tmpl).substitute(bucket_arn=bucket_arn(bucket))


# Probe object lives under an ALLOWED prefix (appdata/*) — the least-privilege
# runtime key cannot write anywhere else, so a top-level probe would falsely fail.
CHECK_PREFIX = "appdata/__provision-check"


class ValidationError(Exception):
    def __init__(self, step: str, detail: str = ""):
        super().__init__(f"S3 {step} check failed")
        self.step = step
        self.detail = detail


def _scrub(text: str, *secret_values: str) -> str:
    out = text or ""
    for s in secret_values:
        if s:
            out = out.replace(s, "***")
    return out


def _aws_env(region: str, key: str, secret: str) -> dict:
    env = os.environ.copy()
    env.pop("AWS_PROFILE", None)
    env.update(
        AWS_ACCESS_KEY_ID=key,
        AWS_SECRET_ACCESS_KEY=secret,
        AWS_DEFAULT_REGION=region,
        AWS_EC2_METADATA_DISABLED="true",
    )
    return env


def _run_aws(args, *, region, key, secret):
    return subprocess.run(
        ["aws", *args],
        env=_aws_env(region, key, secret),
        capture_output=True, text=True,
    )


def validate_runtime_key(bucket, region, key, secret, *, run=_run_aws) -> None:
    """Real list -> put -> get -> delete against the bucket with the runtime key.
    Raises ValidationError (with .step) on the first failure. Writes nothing."""
    check_key = f"{CHECK_PREFIX}-{_secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory() as td:
        body = Path(td, "probe")
        body.write_text("backup-engine provision check\n")
        got = Path(td, "got")
        ops = [
            ("list", ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", "appdata/", "--max-items", "1"]),
            ("put", ["s3api", "put-object", "--bucket", bucket, "--key", check_key, "--body", str(body)]),
            ("get", ["s3api", "get-object", "--bucket", bucket, "--key", check_key, str(got)]),
            ("delete", ["s3api", "delete-object", "--bucket", bucket, "--key", check_key]),
        ]
        for step, args in ops:
            cp = run(args, region=region, key=key, secret=secret)
            if cp.returncode != 0:
                raise ValidationError(step, _scrub(cp.stderr, key, secret))


class TofuError(Exception):
    def __init__(self, phase: str, detail: str = ""):
        super().__init__(f"tofu {phase} failed")
        self.phase = phase
        self.detail = detail


def _tofu_env(admin_key: str, admin_secret: str, session_token: str | None) -> dict:
    env = os.environ.copy()
    env.pop("AWS_PROFILE", None)
    env.update(AWS_ACCESS_KEY_ID=admin_key, AWS_SECRET_ACCESS_KEY=admin_secret)
    if session_token:
        env["AWS_SESSION_TOKEN"] = session_token
    else:
        env.pop("AWS_SESSION_TOKEN", None)
    return env


def _run_tofu(args, *, cwd, env):
    return subprocess.run(["tofu", *args], cwd=cwd, env=env, capture_output=True, text=True)


def render_console_steps(bucket: str, region: str) -> dict:
    loc = "" if region == "us-east-1" else \
        f" --create-bucket-configuration LocationConstraint={region}"
    cli = [
        f"aws s3api create-bucket --bucket {bucket} --region {region}{loc}",
        f"aws s3api put-bucket-versioning --bucket {bucket} "
        f"--versioning-configuration Status=Enabled",
        f"aws s3api put-bucket-encryption --bucket {bucket} "
        f"--server-side-encryption-configuration "
        f"'{{\"Rules\":[{{\"ApplyServerSideEncryptionByDefault\":"
        f"{{\"SSEAlgorithm\":\"AES256\"}}}}]}}'",
        f"aws s3api put-public-access-block --bucket {bucket} "
        f"--public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,"
        f"BlockPublicPolicy=true,RestrictPublicBuckets=true",
        "aws iam create-policy --policy-name backup-engine-runtime-object-only "
        "--policy-document file://iam-policy.json",
        "aws iam create-user --user-name backup-engine-runtime",
        "aws iam attach-user-policy --user-name backup-engine-runtime "
        "--policy-arn <policy-arn-from-create-policy>",
        "aws iam create-access-key --user-name backup-engine-runtime",
    ]
    steps = [
        "Create the bucket in your region with versioning + default SSE + all public access blocked.",
        "Add lifecycle rules: expire noncurrent versions and abort incomplete multipart uploads.",
        "Save the policy JSON above as iam-policy.json, then create an IAM policy from it.",
        "Create an IAM user and attach that policy.",
        "Create an access key for the user — that is your runtime key/secret.",
        "Paste the runtime key/secret below and click Test & Validate.",
    ]
    return {"cli": cli, "steps": steps}


def run_tofu_apply(bucket, region, admin_key, admin_secret, session_token=None,
                   *, run=_run_tofu, module_src=OPENTOFU_DIR, provisioning_src=PROVISIONING_DIR) -> dict:
    """One-shot `tofu init && apply` in a throwaway temp dir. Admin creds go ONLY via the
    subprocess env. Returns the runtime key/secret + bucket/region from `tofu output`.
    Always removes the temp dir (which holds the ephemeral state)."""
    workdir = tempfile.mkdtemp(prefix="be-provision-")
    scrub_vals = (admin_key, admin_secret, session_token or "")
    try:
        tf_dir = Path(workdir, "opentofu")
        shutil.copytree(module_src, tf_dir)
        shutil.copytree(provisioning_src, Path(workdir, "provisioning"))
        # tfvars as JSON so bucket/region can't break out of an HCL string
        # literal (injection-safe). tofu auto-loads terraform.tfvars.json.
        # Holds ONLY non-secret bucket/region — never any credential.
        Path(tf_dir, "terraform.tfvars.json").write_text(
            json.dumps({"bucket_name": bucket, "region": region})
        )
        env = _tofu_env(admin_key, admin_secret, session_token)
        for phase, args in (("init", ["init", "-backend=false", "-input=false"]),
                            ("apply", ["apply", "-auto-approve", "-input=false"])):
            cp = run(args, cwd=str(tf_dir), env=env)
            if cp.returncode != 0:
                raise TofuError(phase, _scrub(cp.stderr, *scrub_vals))
        cp = run(["output", "-json"], cwd=str(tf_dir), env=env)
        if cp.returncode != 0:
            raise TofuError("output", _scrub(cp.stderr, *scrub_vals))
        data = json.loads(cp.stdout)
        return {
            "AWS_ACCESS_KEY_ID": data["runtime_access_key_id"]["value"],
            "AWS_SECRET_ACCESS_KEY": data["runtime_secret_access_key"]["value"],
            "bucket": data["bucket_name"]["value"],
            "region": data["region"]["value"],
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
