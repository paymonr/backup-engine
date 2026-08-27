# app/gui/provision.py — renders the canonical IAM policy, runs the Test & Validate
# S3 round-trip with a runtime key, and drives a one-shot OpenTofu apply for automated
# provisioning. The ONLY module that renders the policy / calls aws / invokes tofu.
# It never writes secrets itself — it returns discovered values to the route, which
# calls config_io.write_secrets (the single secret writer).
from __future__ import annotations
import os
import secrets as _secrets
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
