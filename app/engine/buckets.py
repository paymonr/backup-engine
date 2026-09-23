"""Just-in-time S3 bucket creation/config for opt-in per-job dedicated buckets.
aws-cli via subprocess (runner injectable), NEVER boto3 (project constraint)."""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# fullmatch (not match()+"$") below: match()+"$" tolerates a trailing "\n" -- it
# matches just before it -- fullmatch requires the whole string to be consumed.
# re.ASCII keeps \d in _IP_RE from also matching Unicode look-alike digits.
_BUCKET_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,61})[a-z0-9]$", re.ASCII)
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$", re.ASCII)

def slugify(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")

def suggest(base: str, jobname: str) -> str:
    return f"{base}-{slugify(jobname)}"

def is_prefixed(base: str, bucket: str) -> bool:
    return bucket == base or bucket.startswith(base + "-")

def valid_bucket_name(name: str) -> bool:
    if not name or not _BUCKET_RE.fullmatch(name) or ".." in name or _IP_RE.fullmatch(name):
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

def oldest_non_default_version(versions: list[dict], limit: int = 5) -> str | None:
    """IAM keeps at most `limit` versions of a managed policy. At the limit, the
    oldest non-default version is the one to delete before publishing another."""
    if len(versions) < limit:
        return None
    non_default = [v for v in versions if not v.get("IsDefaultVersion")]
    if not non_default:
        return None
    return sorted(non_default, key=lambda v: v.get("CreateDate", ""))[0]["VersionId"]

# --- Teardown: enumerate + empty + delete just-in-time dedicated buckets. These
# buckets are created OUTSIDE OpenTofu (see ensure_bucket above), so `tofu destroy`
# never removes them -- this is that cleanup path. Delete perms for this live ONLY
# on the bucket-admin role (provisioning/bucket-admin-policy.json.tmpl's Teardown
# statement), NEVER on the everyday runtime key.

_DELETE_BATCH = 1000  # aws s3api delete-objects hard limit per request

def list_managed(region, creds, runner=subprocess.run) -> list[str]:
    """All buckets tagged managed-by=backup-engine. Untagged buckets and buckets
    get-bucket-tagging errors on (e.g. access denied) are treated as 'not managed'
    and skipped -- never raised."""
    cp = _aws(runner, ["s3api", "list-buckets", "--region", region, "--output", "json"], creds)
    try:
        names = [b.get("Name") for b in json.loads(cp.stdout or "{}").get("Buckets", []) if b.get("Name")]
    except ValueError:
        return []
    managed = []
    for name in names:
        tag_cp = runner(["aws", "s3api", "get-bucket-tagging", "--bucket", name,
                         "--region", region, "--output", "json"],
                        env=_env(creds), capture_output=True, text=True)
        if tag_cp.returncode != 0:
            continue                      # untagged / access-denied -> not managed
        try:
            tags = json.loads(tag_cp.stdout or "{}").get("TagSet", [])
        except ValueError:
            continue
        if any(t.get("Key") == "managed-by" and t.get("Value") == "backup-engine" for t in tags):
            managed.append(name)
    return managed

def empty_and_delete(name, *, region, creds, runner=subprocess.run) -> None:
    """Delete every object version + delete-marker (batched), THEN the bucket
    itself. Order matters -- a bucket with any object left in it can't be deleted."""
    cp = _aws(runner, ["s3api", "list-object-versions", "--bucket", name,
                       "--region", region, "--output", "json"], creds)
    try:
        data = json.loads(cp.stdout or "{}")
    except ValueError:
        data = {}
    objects = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in (data.get("Versions") or [])]
    objects += [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in (data.get("DeleteMarkers") or [])]
    for i in range(0, len(objects), _DELETE_BATCH):
        batch = objects[i:i + _DELETE_BATCH]
        _aws(runner, ["s3api", "delete-objects", "--bucket", name, "--region", region,
                     "--delete", json.dumps({"Objects": batch, "Quiet": True})], creds)
    _aws(runner, ["s3api", "delete-bucket", "--bucket", name, "--region", region], creds)

def _assume_role(role_arn, *, region, creds, runner=subprocess.run) -> dict:
    cp = _aws(runner, ["sts", "assume-role", "--role-arn", role_arn,
                       "--role-session-name", "backup-engine-teardown",
                       "--region", region, "--output", "json"], creds)
    c = json.loads(cp.stdout)["Credentials"]
    return {"AWS_ACCESS_KEY_ID": c["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
            "AWS_SESSION_TOKEN": c["SessionToken"]}

def _teardown_cli(runner=subprocess.run) -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    role_arn = os.environ.get("BUCKET_ADMIN_ROLE_ARN", "")
    if not role_arn:
        print("buckets teardown: BUCKET_ADMIN_ROLE_ARN is not set", file=sys.stderr)
        return 2
    base_creds = {"AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
                  "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "")}
    try:
        creds = _assume_role(role_arn, region=region, creds=base_creds, runner=runner)
        names = list_managed(region, creds, runner=runner)
        for name in names:
            print(f"teardown: emptying + deleting {name}")
            empty_and_delete(name, region=region, creds=creds, runner=runner)
    except BucketError as e:
        print(f"buckets teardown: {e}", file=sys.stderr)
        return 1
    print(f"teardown: removed {len(names)} managed bucket(s)")
    return 0

def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "teardown":
        return _teardown_cli()
    print("usage: python3 -m app.engine.buckets teardown", file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(_main())
