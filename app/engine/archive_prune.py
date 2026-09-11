from __future__ import annotations
import argparse, subprocess, time
from app.engine import s3

_DAY = 86400

class PruneScopeError(Exception):
    """A prunable version pointed outside the job's own media/<job>/ prefix."""

def _prefix(job: str) -> str:
    return f"media/{job}/"

def select_prunable(versions, policy, now):
    t = policy.get("type")
    if t == "keep_all":
        return []
    by_key = {}
    for v in versions:
        by_key.setdefault(v["key"], []).append(v)
    out = []
    for key, vs in by_key.items():
        vs = sorted(vs, key=lambda v: v["last_modified"], reverse=True)  # newest first
        noncurrent = [v for v in vs if not v["is_latest"]]
        if t == "days":
            cutoff = now - policy["days"] * _DAY
            out += [v for v in noncurrent if v["last_modified"] < cutoff]
        elif t == "count":
            # keep the N most recent versions total (latest + newest noncurrent), drop the rest
            # operate on noncurrent to guarantee we never select is_latest
            keep = max(policy["count"] - 1, 0)
            out += noncurrent[keep:]
    return [{"key": v["key"], "version_id": v["version_id"]} for v in out]

def prune(job, policy, *, bucket, now=None, runner=subprocess.run, _versions=None, _deleter=None) -> int:
    now = now if now is not None else time.time()
    prefix = _prefix(job)
    versions = _versions if _versions is not None else s3.list_versions(bucket, prefix, runner=runner)
    targets = select_prunable(versions, policy, now)
    deleter = _deleter or (lambda k, v: s3.delete_version(bucket, k, v, runner=runner))
    # Validate all targets before deleting any (scope guard, destructive operation safety)
    for t in targets:
        if not t["key"].startswith(prefix) or ".." in t["key"].split("/"):
            raise PruneScopeError(f"refusing to delete outside {prefix!r}: {t['key']!r}")
    # All targets validated; proceed with deletion
    n = 0
    for t in targets:
        deleter(t["key"], t["version_id"]); n += 1
    return n

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="archive_prune")
    ap.add_argument("job"); ap.add_argument("--type", required=True)
    ap.add_argument("--days", type=int, default=0); ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--bucket", default=None)
    a = ap.parse_args(argv)
    import os
    bucket = a.bucket or os.environ.get("S3_BUCKET")
    policy = {"type": a.type}
    if a.type == "days": policy["days"] = a.days
    elif a.type == "count": policy["count"] = a.count
    n = prune(a.job, policy, bucket=bucket)
    print(f"archive prune '{a.job}': removed {n} old version(s)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
