# app/engine/s3.py — thin subprocess wrappers around rclone for the
# versioned-files backup/restore/prune runners. This is the ONLY module in the
# engine that shells out to S3 tooling; app.engine.vfiles orchestrates the
# catalog and calls into here so the network boundary stays in one place.
#
# Every operation targets the rclone remote named [s3] (rendered by
# scripts/lib/rclone-conf.sh), addressed as "s3:<bucket>/<key>". `runner` is
# injectable (defaults to subprocess.run) so tests drive these with a stub that
# captures argv instead of touching a real bucket. A non-zero return raises
# S3Error carrying the tool's stderr.
from __future__ import annotations
import subprocess


class S3Error(Exception):
    """An rclone/aws invocation returned non-zero."""


# The per-job catalog lives under the job's own prefix so a job is fully
# self-describing in the bucket and restore/prune can recover it.
def catalog_key(job: str) -> str:
    return f"media/{job}/_catalog/catalog.sqlite"


def _remote(bucket: str, key: str) -> str:
    return f"s3:{bucket}/{key}"


def _run(runner, argv: list[str]) -> None:
    proc = runner(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()
        raise S3Error(f"{argv[0]} failed (exit {proc.returncode}): {stderr}")


def put(local, key, storage_class, *, bucket, rclone_config, runner=subprocess.run) -> None:
    """Upload a single local file to the exact object `key` at `storage_class`.
    Uses `copyto` (single-file, exact-name) so each distinct version-key maps to
    exactly one object."""
    _run(runner, [
        "rclone", "--config", str(rclone_config), "copyto",
        str(local), _remote(bucket, key),
        "--s3-storage-class", storage_class,
    ])


def delete(key, *, bucket, rclone_config, runner=subprocess.run) -> None:
    """Delete a single object by exact `key`."""
    _run(runner, [
        "rclone", "--config", str(rclone_config), "deletefile",
        _remote(bucket, key),
    ])


def get(key, local, *, bucket, rclone_config, runner=subprocess.run) -> None:
    """Download a single object `key` to the exact local path `local`."""
    _run(runner, [
        "rclone", "--config", str(rclone_config), "copyto",
        _remote(bucket, key), str(local),
    ])


def upload_catalog(job, local, *, bucket, rclone_config, runner=subprocess.run) -> None:
    """Upload the per-job catalog DB to its durable key (STANDARD so it stays
    immediately readable and overwritable — never archived)."""
    put(local, catalog_key(job), "STANDARD",
        bucket=bucket, rclone_config=rclone_config, runner=runner)


def download_catalog(job, local, *, bucket, rclone_config, runner=subprocess.run) -> None:
    """Fetch the per-job catalog DB from its durable key to `local`. Callers that
    tolerate a fresh (never-uploaded) job should catch S3Error."""
    get(catalog_key(job), local,
        bucket=bucket, rclone_config=rclone_config, runner=runner)


def thaw(key, *, bucket, tier="Bulk", days=7, runner=subprocess.run) -> None:
    """Issue a Glacier/Deep Archive restore-object request for `key`, mirroring
    scripts/restore.sh's archive-job thaw (`aws s3api restore-object --bucket
    ... --key ... --restore-request Days=<days>,GlacierJobParameters={Tier=<tier>}`).
    Uses the `aws` CLI directly, not rclone -- rclone has no restore-object
    equivalent. Does not block: the object only becomes downloadable once AWS
    finishes the thaw (hours for Glacier Bulk, up to ~48h for Deep Archive)."""
    _run(runner, [
        "aws", "s3api", "restore-object",
        "--bucket", bucket, "--key", key,
        "--restore-request", f"Days={days},GlacierJobParameters={{Tier={tier}}}",
    ])
