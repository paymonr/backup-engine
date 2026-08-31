# app/engine/vfiles.py — the versioned-files backup/restore runner.
# Orchestrates the per-job SQLite catalog (app.engine.catalog) and the S3
# wrappers (app.engine.s3); it performs NO subprocess/network work of its own —
# all S3 I/O goes through app.engine.s3, all confinement of the source through
# catalog.scan (safe_resolve-first).
#
# backup(): load-or-fetch the catalog -> scan the source -> diff against the
# catalog -> upload each new/changed file under a DISTINCT version-key ->
# record it -> tombstone removed files -> prune versions older than the
# retention window (deleting their S3 objects) -> upload the catalog for
# durability.
#
# restore(): load-or-fetch the catalog (same durability path as backup) ->
# either LIST every current path + its versions, or select one version of one
# path (latest, or the version current `asof` a given timestamp) and recover
# it -- thawing first when its storage_class is cold, else a direct s3.get.
# Restore reads FROM the catalog + S3 only; it never touches the job's local
# source tree.
#
# Version-key scheme (consistent across backup/restore/prune/integration):
#     media/<job>/<relpath>@<int(now)>-<uuid4 hex[:8]>
# where <relpath> is the file path relative to the job source. The trailing
# random suffix makes keys collision-resistant: two backups in the SAME second
# that both re-upload a path get DISTINCT keys, so pruning an old version can
# never s3.delete the object a newer version still points at. Restore/prune read
# the exact stored key, so the suffix is free. The current version of a path is
# the newest by uploaded_at (catalog.record_version keeps is_current in sync).
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.engine import catalog, s3

_SECONDS_PER_DAY = 86400

# Storage classes that require a thaw (restore-object) before the object can be
# read back. GLACIER_IR is deliberately excluded -- it's Glacier's
# instant-retrieval tier and reads directly, no thaw needed.
_COLD_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}


class PruneScopeError(Exception):
    """A prunable catalog row pointed at an S3 key outside the job's own
    media/<job>/ prefix. Deletion is REFUSED — the engine must never delete an
    object belonging to another job (or anything else) in the bucket."""


def _job_prefix(job_name: str) -> str:
    return f"media/{job_name}/"


def _open_or_fetch_catalog(job_name, cache_dir, *, bucket, rclone_config, runner):
    """Ensure `<cache_dir>/<job_name>.sqlite` exists locally -- best-effort
    fetching the durable S3 copy first if it's missing -- and open it. Shared
    by backup() and restore() so both start from the same durable state."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cat_path = cache_dir / f"{job_name}.sqlite"

    # Durability: if we have no local catalog, best-effort fetch the durable
    # copy from S3 first. A fresh job (nothing uploaded yet) simply starts empty.
    if not cat_path.exists():
        try:
            s3.download_catalog(job_name, str(cat_path),
                                 bucket=bucket, rclone_config=rclone_config, runner=runner)
        except s3.S3Error:
            pass  # no remote catalog yet -> start from an empty one

    return catalog.open_catalog(str(cat_path))


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

    conn = _open_or_fetch_catalog(name, cache_dir,
                                   bucket=bucket, rclone_config=rclone_config, runner=runner)
    cat_path = Path(cache_dir) / f"{name}.sqlite"
    try:
        entries = catalog.scan(source_root, "")  # whole-tree scan; confinement enforced inside
        d = catalog.diff(conn, entries)

        uploaded = 0
        for entry in d["new"] + d["changed"]:
            rel = entry["path"]
            # media/<job>/<relpath>@<int(now)>-<uuid4 hex[:8]>; the suffix
            # guarantees a distinct object per version even within one second.
            key = f"{prefix}{rel}@{ts}-{uuid.uuid4().hex[:8]}"
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
            # tombstone rows have key=None -> nothing in S3 to delete. And never
            # s3.delete an object a live (is_current=1) row still points at --
            # belt-and-suspenders against any key ever being shared across rows.
            if key and not catalog.is_current_key(conn, key):
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


def _list_paths(conn) -> list[dict]:
    """LIST mode: every CURRENTLY-LIVE path (i.e. not tombstoned) and each of
    its non-tombstone versions, newest-first within a path. Returns and prints
    [{"path", "uploaded_at", "storage_class"}, ...].

    A path whose only history is a tombstone (fully deleted) is not listed
    here -- but any of its earlier versions remain directly recoverable via
    restore(path=..., asof=<before the deletion>), since version selection
    reads full history, not just current()."""
    rows: list[dict] = []
    for path in sorted(catalog.current(conn)):
        for v in catalog.versions(conn, path):
            if v["deleted"]:
                continue
            entry = {"path": path, "uploaded_at": v["uploaded_at"], "storage_class": v["storage_class"]}
            rows.append(entry)
            print(f"{entry['path']}\t{entry['uploaded_at']}\t{entry['storage_class']}")
    return rows


def _select_version(conn, path, asof):
    """The version of `path` to restore: the newest non-tombstone version, or
    -- with `asof` given -- the newest non-tombstone version with
    uploaded_at <= asof (the version that was current AT that time). Returns
    None if no such version exists. catalog.versions() is already ordered
    newest-first, so the first match after filtering is the one wanted."""
    candidates = [v for v in catalog.versions(conn, path) if not v["deleted"]]
    if asof is not None:
        candidates = [v for v in candidates if v["uploaded_at"] <= asof]
    return candidates[0] if candidates else None


def restore(job, *, target, path=None, asof=None, cache_dir, bucket, rclone_config,
            thaw="Bulk", runner=subprocess.run) -> list | dict:
    """Recover a versioned-files job's catalog, and optionally a file from it.

    `job` is a dict {name, ...} (only `name` is used -- restore reads from the
    catalog + S3, never the job's local source tree). The catalog is loaded
    from `<cache_dir>/<job>.sqlite`, best-effort fetching it from S3 first via
    `_open_or_fetch_catalog` if the local cache is missing (mirrors backup()'s
    durability path).

    - `path is None` -> LIST mode: print and return every current path with
      each of its versions (see `_list_paths`).
    - else -> select ONE version of `path`: the latest, or -- with `asof` --
      the version current at that timestamp (skipping tombstones either way).
      Raises LookupError if no matching version exists.
      * If the selected version's storage_class is one of `_COLD_CLASSES`
        (GLACIER, DEEP_ARCHIVE): issue `s3.thaw(key, bucket=bucket, tier=thaw,
        runner=runner)` -- an `aws s3api restore-object` call, matching
        scripts/restore.sh's archive-job thaw -- and return/report that a thaw
        was requested. This does NOT download the object (the thaw takes
        hours); no s3.get is issued in this branch.
      * Otherwise: `s3.get(key, target/path, bucket=bucket,
        rclone_config=rclone_config, runner=runner)` downloads it directly.

    Returns a dict {"status": "thaw-requested"|"restored", "path", "key", ...}
    when restoring one file, or a list of {"path","uploaded_at",
    "storage_class"} dicts in LIST mode.
    """
    name = job["name"]
    conn = _open_or_fetch_catalog(name, cache_dir,
                                   bucket=bucket, rclone_config=rclone_config, runner=runner)
    try:
        if path is None:
            return _list_paths(conn)

        row = _select_version(conn, path, asof)
        if row is None:
            when = f" as of {asof}" if asof is not None else ""
            raise LookupError(f"no version of {path!r} found in job {name!r}'s catalog{when}")

        key = row["key"]
        storage_class = row["storage_class"]
        if storage_class in _COLD_CLASSES:
            s3.thaw(key, bucket=bucket, tier=thaw, runner=runner)
            print(f"thaw requested for {path!r} ({storage_class}); re-run restore once it completes")
            return {"status": "thaw-requested", "path": path, "key": key,
                    "storage_class": storage_class}

        dest = Path(target) / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.get(key, str(dest), bucket=bucket, rclone_config=rclone_config, runner=runner)
        return {"status": "restored", "path": path, "key": key, "target": str(dest)}
    finally:
        conn.close()


def _main(argv: list[str]) -> int:
    """CLI entrypoint for ``python3 -m app.engine.vfiles``:

        python3 -m app.engine.vfiles backup <job>
        python3 -m app.engine.vfiles restore <job> list
        python3 -m app.engine.vfiles restore <job> <path> <target> \
            [--asof TS] [--tier Bulk|Standard|Expedited]

    Dispatched from scripts/backup-job.sh (the `versioned-files)` case) and
    scripts/restore.sh the same way, with `<job>` matching the `$JOB`/`$job`
    shell var those scripts already resolved via `app.gui.jobs_io`.

    This CLI does NOT re-read config/jobs.json -- it TRUSTS the JOB_* env
    vars those scripts already `eval`'d from jobs_io's (re-validated,
    shell-safe) output -- JOB_SOURCE, JOB_STORAGE_CLASS, JOB_RETENTION_DAYS --
    the same way backup-job.sh's own _run_versioned/_run_archive trust their
    JOB_* vars without re-validating them; jobs_io's own `_main` is the
    re-validation gate (name charset + source confinement) that already ran
    to produce them. It additionally reads the wiring backup-job.sh sets up:
    SOURCE_ROOT, CACHE_DIR, S3_BUCKET -- and derives the rclone config path
    scripts/lib/rclone-conf.sh always renders to: $CACHE_DIR/rclone.conf.
    """
    parser = argparse.ArgumentParser(prog="python3 -m app.engine.vfiles")
    sub = parser.add_subparsers(dest="cmd", required=True)

    backup_p = sub.add_parser("backup", help="run one incremental backup")
    backup_p.add_argument("job", help="job name (the $JOB backup-job.sh resolved)")

    restore_p = sub.add_parser("restore", help="list versions, or recover one file")
    restore_p.add_argument("job", help="job name")
    restore_p.add_argument("path", help='"list", or the relpath of a file to restore')
    restore_p.add_argument("target", nargs="?", default=None,
                            help="target dir (required unless path is 'list')")
    restore_p.add_argument("--asof", type=float, default=None,
                            help="unix timestamp: restore the version current as of then")
    restore_p.add_argument("--tier", default="Bulk", choices=["Bulk", "Standard", "Expedited"],
                            help="Glacier/Deep Archive thaw tier (cold storage classes only)")

    args = parser.parse_args(argv)

    def _require_env(name: str) -> str:
        val = os.environ.get(name)
        if not val:
            parser.error(f"missing required environment variable: {name}")
        return val

    cache_dir = _require_env("CACHE_DIR")
    bucket = _require_env("S3_BUCKET")
    rclone_config = str(Path(cache_dir) / "rclone.conf")
    retention_raw = os.environ.get("JOB_RETENTION_DAYS", "90")
    try:
        retention_days = int(retention_raw)
    except ValueError:
        parser.error(f"invalid JOB_RETENTION_DAYS: {retention_raw!r}")
    job = {
        "name": args.job,
        "source": os.environ.get("JOB_SOURCE", ""),
        "storage_class": _require_env("JOB_STORAGE_CLASS"),
        "retention_days": retention_days,
    }

    if args.cmd == "backup":
        source_root = str(Path(_require_env("SOURCE_ROOT")) / job["source"])
        stats = backup(job, source_root=source_root, cache_dir=cache_dir,
                        bucket=bucket, rclone_config=rclone_config)
        print(f'uploaded={stats["uploaded"]} deleted={stats["deleted"]} pruned={stats["pruned"]}')
        return 0

    # restore
    if args.path == "list":
        if args.target is not None:
            parser.error("'list' takes no target/--asof/--tier")
        restore(job, path=None, target="", cache_dir=cache_dir, bucket=bucket,
                rclone_config=rclone_config)
        return 0

    if args.target is None:
        parser.error("restore <job> <path> <target> [--asof TS] [--tier Bulk|Standard|Expedited]")
    try:
        result = restore(job, path=args.path, target=args.target, asof=args.asof,
                          cache_dir=cache_dir, bucket=bucket, rclone_config=rclone_config,
                          thaw=args.tier)
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f'{result["status"]}: {result["path"]} ({result["key"]})')
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
