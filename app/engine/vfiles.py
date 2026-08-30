# app/engine/vfiles.py — the versioned-files backup runner. Orchestrates the
# per-job SQLite catalog (app.engine.catalog) and the S3 wrappers
# (app.engine.s3); it performs NO subprocess/network work of its own — all S3
# I/O goes through app.engine.s3, all confinement of the source through
# catalog.scan (safe_resolve-first).
#
# backup(): load-or-fetch the catalog -> scan the source -> diff against the
# catalog -> upload each new/changed file under a DISTINCT version-key ->
# record it -> tombstone removed files -> prune versions older than the
# retention window (deleting their S3 objects) -> upload the catalog for
# durability.
#
# Version-key scheme (consistent across backup/restore/prune/integration):
#     media/<job>/<relpath>@<int(now)>
# where <relpath> is the file path relative to the job source. The current
# version of a path is the newest by uploaded_at (catalog.record_version keeps
# is_current in sync).
from __future__ import annotations
import subprocess
import time
from pathlib import Path

from app.engine import catalog, s3

_SECONDS_PER_DAY = 86400


class PruneScopeError(Exception):
    """A prunable catalog row pointed at an S3 key outside the job's own
    media/<job>/ prefix. Deletion is REFUSED — the engine must never delete an
    object belonging to another job (or anything else) in the bucket."""


def _job_prefix(job_name: str) -> str:
    return f"media/{job_name}/"


def backup(job, *, source_root, cache_dir, bucket, rclone_config,
           now=None, runner=subprocess.run) -> dict:
    """Run one incremental backup of `job` and return
    {"uploaded", "deleted", "pruned"} counts.

    `job` is a dict {name, source, storage_class, retention_days}.
    """
    name = job["name"]
    storage_class = job["storage_class"]
    retention_days = job["retention_days"]
    if now is None:
        now = time.time()
    ts = int(now)
    prefix = _job_prefix(name)

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cat_path = cache_dir / f"{name}.sqlite"

    # Durability: if we have no local catalog, best-effort fetch the durable
    # copy from S3 first. A fresh job (nothing uploaded yet) simply starts empty.
    if not cat_path.exists():
        try:
            s3.download_catalog(name, str(cat_path),
                                bucket=bucket, rclone_config=rclone_config, runner=runner)
        except s3.S3Error:
            pass  # no remote catalog yet -> start from an empty one

    conn = catalog.open_catalog(str(cat_path))
    try:
        entries = catalog.scan(source_root, "")  # whole-tree scan; confinement enforced inside
        d = catalog.diff(conn, entries)

        uploaded = 0
        for entry in d["new"] + d["changed"]:
            rel = entry["path"]
            key = f"{prefix}{rel}@{ts}"  # media/<job>/<relpath>@<int(now)>
            local = str(Path(source_root) / rel)
            s3.put(local, key, storage_class,
                   bucket=bucket, rclone_config=rclone_config, runner=runner)
            catalog.record_version(conn, rel, key, entry["size"], entry["mtime"],
                                   storage_class, now)
            uploaded += 1

        deleted = 0
        for path in d["deleted"]:
            catalog.mark_deleted(conn, path, now)
            deleted += 1

        # Prune versions older than the retention window. catalog.prunable never
        # returns a current row, so we only ever delete superseded versions and
        # tombstones — never the live copy of a path.
        before = now - retention_days * _SECONDS_PER_DAY
        pruned = 0
        for row in catalog.prunable(conn, before):
            key = row["key"]
            if key:  # tombstone rows have key=None -> nothing in S3 to delete
                # Load-bearing safety: never delete an object outside this job's
                # own prefix, whatever a (possibly corrupted/crafted) row claims.
                if not key.startswith(prefix):
                    raise PruneScopeError(
                        f"refusing to delete key outside {prefix!r}: {key!r}"
                    )
                s3.delete(key, bucket=bucket, rclone_config=rclone_config, runner=runner)
            catalog.delete_version(conn, row["id"])
            pruned += 1
    finally:
        conn.close()

    # Durability: push the updated catalog back to its durable key so a fresh
    # environment (or restore/prune elsewhere) can recover version history.
    s3.upload_catalog(name, str(cat_path),
                      bucket=bucket, rclone_config=rclone_config, runner=runner)

    return {"uploaded": uploaded, "deleted": deleted, "pruned": pruned}
