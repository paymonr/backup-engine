# app/estimator/usage.py — real stored usage per prefix via `rclone size --json`
# (rclone is configured in the container and honors S3_ENDPOINT). Uses only the
# runtime key's existing S3 ListBucket permission. Never lists on page render;
# results are cached and refreshed on demand.
from __future__ import annotations
import json, subprocess, time
from pathlib import Path

_CACHE = "usage.json"

def _size(bucket: str, prefix: str, *, rclone_config, runner) -> dict | None:
    cmd = ["rclone", "size", f"s3:{bucket}/{prefix}", "--json"]
    if rclone_config:
        cmd += ["--config", rclone_config]
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=300)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        d = json.loads(p.stdout)
        return {"bytes": int(d["bytes"]), "count": int(d["count"])}
    except (ValueError, KeyError, TypeError):
        # ValueError: bad JSON / non-numeric field; KeyError: missing field;
        # TypeError: field present but null -> int(None). Any shape we can't read
        # as a usable size degrades this prefix to None, never crashes the refresh.
        return None

def collect_usage(bucket, archive_jobs, has_versioned, *, rclone_config=None,
                  runner=subprocess.run) -> dict:
    out: dict[str, dict | None] = {}
    if has_versioned:
        out["appdata"] = _size(bucket, "appdata", rclone_config=rclone_config, runner=runner)
    for job in archive_jobs:
        out[f"media/{job}"] = _size(bucket, f"media/{job}", rclone_config=rclone_config, runner=runner)
    return out

def save_cached(cache_dir, data) -> None:
    p = Path(cache_dir, _CACHE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched_at": time.time(), "data": data}))

def load_cached(cache_dir) -> dict | None:
    p = Path(cache_dir, _CACHE)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None
