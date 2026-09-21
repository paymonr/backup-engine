"""Just-in-time S3 bucket creation/config for opt-in per-job dedicated buckets.
aws-cli via subprocess (runner injectable), NEVER boto3 (project constraint)."""
from __future__ import annotations
import json
import os
import re
import subprocess

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BUCKET_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,61})[a-z0-9]$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

def slugify(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")

def suggest(base: str, jobname: str) -> str:
    return f"{base}-{slugify(jobname)}"

def is_prefixed(base: str, bucket: str) -> bool:
    return bucket == base or bucket.startswith(base + "-")

def valid_bucket_name(name: str) -> bool:
    if not name or not _BUCKET_RE.match(name) or ".." in name or _IP_RE.match(name):
        return False
    return True

class BucketError(RuntimeError):
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(kind if not detail else f"{kind}: {detail}")

def _env(creds: dict) -> dict:
    e = os.environ.copy()
    e.update({k: v for k, v in (creds or {}).items() if v})
    return e

def _aws(runner, argv, creds, *, ok_substrings=()):
    cp = runner(["aws", *argv], env=_env(creds), capture_output=True, text=True)
    if cp.returncode != 0:
        err = (cp.stderr or "")
        if any(s in err for s in ok_substrings):
            return cp
        if "BucketAlreadyExists" in err:
            raise BucketError("name_taken", "that bucket name is already taken globally")
        if "TooManyBuckets" in err:
            raise BucketError("too_many", "this AWS account is at its bucket limit")
        if "AccessDenied" in err or "not authorized" in err:
            raise BucketError("access_denied", err.strip())
        raise BucketError("other", err.strip())
    return cp

_LIFECYCLE = {"Rules": [{"ID": "backup-engine", "Status": "Enabled", "Filter": {},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}

def ensure_bucket(name, *, region, versioned, creds, runner=subprocess.run) -> None:
    if not valid_bucket_name(name):
        raise BucketError("invalid_name", f"{name!r} is not a valid S3 bucket name")
    loc = [] if region == "us-east-1" else ["--create-bucket-configuration", f"LocationConstraint={region}"]
    _aws(runner, ["s3api", "create-bucket", "--bucket", name, "--region", region, *loc],
         creds, ok_substrings=("BucketAlreadyOwnedByYou",))
    _aws(runner, ["s3api", "put-public-access-block", "--bucket", name,
                  "--public-access-block-configuration",
                  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"], creds)
    _aws(runner, ["s3api", "put-bucket-ownership-controls", "--bucket", name,
                  "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"], creds)
    _aws(runner, ["s3api", "put-bucket-encryption", "--bucket", name,
                  "--server-side-encryption-configuration",
                  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'], creds)
    _aws(runner, ["s3api", "put-bucket-versioning", "--bucket", name,
                  "--versioning-configuration", f"Status={'Enabled' if versioned else 'Suspended'}"], creds)
    _aws(runner, ["s3api", "put-bucket-lifecycle-configuration", "--bucket", name,
                  "--lifecycle-configuration", json.dumps(_LIFECYCLE)], creds)
    _aws(runner, ["s3api", "put-bucket-tagging", "--bucket", name,
                  "--tagging", "TagSet=[{Key=managed-by,Value=backup-engine}]"], creds)

_GRANT_ACTIONS = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketVersions",
                  "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                  "s3:DeleteObjectVersion", "s3:AbortMultipartUpload",
                  "s3:ListMultipartUploadParts", "s3:RestoreObject"]

def _grants_bucket(document: dict, arn: str) -> bool:
    for stmt in document.get("Statement", []):
        res = stmt.get("Resource", [])
        if isinstance(res, str):
            res = [res]
        if arn in res:
            return True
    return False

def grant_object_access(policy_arn, bucket_name, *, region, creds, runner=subprocess.run) -> None:
    # Read the current default version; append this bucket's ARNs (idempotently); publish a new default.
    cur = _aws(runner, ["iam", "get-policy", "--policy-arn", policy_arn,
                        "--region", region, "--output", "json"], creds)
    ver = json.loads(cur.stdout)["Policy"]["DefaultVersionId"]
    doc = _aws(runner, ["iam", "get-policy-version", "--policy-arn", policy_arn,
                        "--version-id", ver, "--region", region, "--output", "json"], creds)
    document = json.loads(doc.stdout)["PolicyVersion"]["Document"]
    arn = f"arn:aws:s3:::{bucket_name}"
    if _grants_bucket(document, arn):
        return  # already granted; idempotent no-op

    stmts = document.setdefault("Statement", [])
    sid = "grant" + re.sub(r"[^A-Za-z0-9]", "", bucket_name)
    stmts.append({"Sid": sid, "Effect": "Allow",
                  "Action": _GRANT_ACTIONS,
                  "Resource": [arn, arn + "/*"]})

    versions = _aws(runner, ["iam", "list-policy-versions", "--policy-arn", policy_arn,
                             "--region", region, "--output", "json"], creds)
    vlist = json.loads(versions.stdout).get("Versions", [])
    if len(vlist) >= 5:
        non_default = [v for v in vlist if not v.get("IsDefaultVersion")]
        oldest = sorted(non_default, key=lambda v: v.get("CreateDate", ""))[0]
        _aws(runner, ["iam", "delete-policy-version", "--policy-arn", policy_arn,
                      "--version-id", oldest["VersionId"], "--region", region], creds)

    _aws(runner, ["iam", "create-policy-version", "--policy-arn", policy_arn,
                  "--policy-document", json.dumps(document), "--set-as-default",
                  "--region", region], creds)
