# app/gui/provision.py — renders the canonical IAM policy, runs the Test & Validate
# S3 round-trip with a runtime key, and drives a one-shot OpenTofu apply for automated
# provisioning. The ONLY module that renders the policy / calls aws / invokes tofu.
# It never writes secrets itself — it returns discovered values to the route, which
# calls config_io.write_secrets (the single secret writer).
from __future__ import annotations
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
