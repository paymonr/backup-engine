"""Just-in-time S3 bucket creation/config for opt-in per-job dedicated buckets.
aws-cli via subprocess (runner injectable), NEVER boto3 (project constraint)."""
from __future__ import annotations
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
