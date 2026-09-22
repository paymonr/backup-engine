# app/engine/vfiles.py — the versioned-files backup/restore runner.
# Orchestrates the per-job SQLite catalog (app.engine.catalog) and the S3
# wrappers (app.engine.s3); it performs NO subprocess/network work of its own —
# all S3 I/O goes through app.engine.s3, all confinement of the source through
# catalog.scan (safe_resolve-first).
#
# backup(): load-or-fetch the catalog -> scan the source -> diff against the
# catalog -> upload each new/changed file under a DISTINCT version-key ->
# record it -> tombstone removed files -> prune versions per the job's
# retention policy (days window, keep-newest-N count, or keep_all -- deleting
# the pruned versions' S3 objects) -> upload the catalog for durability.
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
from app.gui.jobs_io import valid_name

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


def _has_dotdot(key: str) -> bool:
    """True if any '/'-separated segment of `key` is exactly '..'. Such a key
    could traverse OUT of the job prefix even while textually startswith()-ing
    it (e.g. 'media/<job>/../otherjob/x' starts with 'media/<job>/' yet points
    elsewhere), so the scope guard must reject it, not just the startswith miss."""
    return ".." in key.split("/")


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
    {"uploaded", "deleted", "pruned", "bytes", "files_total", "bytes_total"}.

    The first three are this run's counts; `bytes` is the bytes uploaded this run;
    `files_total`/`bytes_total` are the live footprint after the run (the catalog's
    current versions). The contract only grows -- the old three keys are unchanged.

    `job` is a dict {name, source, storage_class, policy}, where `policy` is a
    retention policy dict: {"type": "days", "days": N} | {"type": "count",
    "count": N} | {"type": "keep_all"}.
    """
    name = job["name"]
    storage_class = job["storage_class"]
    policy = job["policy"]
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
        bytes_uploaded = 0
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
            bytes_uploaded += int(entry["size"])

        deleted = 0
        for path in d["deleted"]:
            catalog.mark_deleted(conn, path, now)
            deleted += 1

        # Prune per the job's retention policy. "keep_all" skips prune entirely;
        # "days" prunes the age window (today's behavior); "count" keeps only the
        # newest N non-tombstone versions per path. Both catalog.prunable and
        # catalog.prunable_beyond_count never return a current row, so we only
        # ever delete superseded versions and tombstones — never the live copy
        # of a path.
        ptype = policy["type"]
        if ptype == "keep_all":
            prunable_rows = []
        elif ptype == "count":
            prunable_rows = catalog.prunable_beyond_count(conn, policy["count"])
        else:  # "days"
            before = now - policy["days"] * _SECONDS_PER_DAY
            prunable_rows = catalog.prunable(conn, before)

        pruned = 0
        for row in prunable_rows:
            key = row["key"]
            # tombstone rows have key=None -> nothing in S3 to delete.
            if key:
                # Load-bearing scope guard, checked FIRST -- before the
                # is_current check -- so an out-of-scope key is REFUSED whatever
                # a (possibly corrupted/crafted) row claims, even one a crafted
                # is_current row also points at. The engine must NEVER delete an
                # object outside this job's own media/<job>/ prefix. Reject both
                # a prefix miss AND any '..' segment (which could traverse out of
                # the prefix while still textually starting with it).
                if not key.startswith(prefix) or _has_dotdot(key):
                    raise PruneScopeError(
                        f"refusing to delete key outside {prefix!r}: {key!r}"
                    )
                # Never s3.delete an object a live (is_current=1) row still points
                # at -- belt-and-suspenders against any key ever being shared
                # across rows.
                if not catalog.is_current_key(conn, key):
                    s3.delete(key, bucket=bucket, rclone_config=rclone_config, runner=runner)
            catalog.delete_version(conn, row["id"])
            pruned += 1

        # Live footprint AFTER this run (current versions only), for the record's
        # files_total/bytes_total -- read before the catalog is closed.
        files_total, bytes_total = catalog.current_totals(conn)
    finally:
        conn.close()

    # Durability: push the updated catalog back to its durable key so a fresh
    # environment (or restore/prune elsewhere) can recover version history.
    s3.upload_catalog(name, str(cat_path),
                      bucket=bucket, rclone_config=rclone_config, runner=runner)

    return {"uploaded": uploaded, "deleted": deleted, "pruned": pruned,
            "bytes": bytes_uploaded, "files_total": files_total, "bytes_total": bytes_total}


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


def _current_at(conn, path, asof):
    """The version of `path` that was CURRENT at `asof` (or now, when asof is None),
    tombstone-aware: the newest version with uploaded_at <= asof; None when that
    newest version is a tombstone (the path was deleted as of then) or none exists.

    This differs from `_select_version` deliberately. Single-file restore() uses
    `_select_version` so you can recover a file that was later deleted; whole-scope
    restore_all()/thaw() use `_current_at` so a point-in-time restore reproduces the
    tree as it WAS -- a path deleted before `asof` is simply absent."""
    versions = catalog.versions(conn, path)  # newest-first, tombstones included
    if asof is not None:
        versions = [v for v in versions if v["uploaded_at"] <= asof]
    if not versions:
        return None
    top = versions[0]
    return None if top["deleted"] else top


def _select_scope(conn, asof, scope):
    """[(path, row)] for every path whose version current at `asof` is live, plus the
    count of paths skipped (deleted/absent as of then). `scope` filters to a folder
    prefix; "." (the only File-history scope, 5.2) or "" means the whole tree. Shared
    by restore_all() and thaw() so a warm-up and the restore it precedes pick the exact
    same versions."""
    pairs, skipped = [], 0
    base = "" if scope in (None, ".", "") else scope.rstrip("/") + "/"
    for path in catalog.paths(conn):
        if base and not (path == scope or path.startswith(base)):
            continue
        row = _current_at(conn, path, asof)
        if row is None:
            skipped += 1
        else:
            pairs.append((path, row))
    return pairs, skipped


def restore_all(job, *, target, asof=None, cache_dir, bucket, rclone_config,
                thaw="Bulk", runner=subprocess.run) -> dict:
    """Restore EVERY file that was live at `asof` (or now) into `target/<path>` --
    File history's "restore everything as of this point". Cold versions are warmed
    via s3.thaw() instead of downloaded (the copy takes hours); the run still exits 0
    so the GUI can report how many were warmed vs written and offer "Download again
    later" (5.4). Returns {"restored", "thaw_requested", "skipped", "bytes"}."""
    name = job["name"]
    conn = _open_or_fetch_catalog(name, cache_dir,
                                   bucket=bucket, rclone_config=rclone_config, runner=runner)
    try:
        pairs, skipped = _select_scope(conn, asof, ".")
        restored = thaw_requested = total_bytes = 0
        for path, row in pairs:
            key = row["key"]
            if row["storage_class"] in _COLD_CLASSES:
                s3.thaw(key, bucket=bucket, tier=thaw, runner=runner)
                print(f"thaw requested: {path}")
                thaw_requested += 1
            else:
                dest = Path(target) / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                s3.get(key, str(dest), bucket=bucket, rclone_config=rclone_config, runner=runner)
                print(f"restored: {path}")
                restored += 1
                total_bytes += int(row["size"] or 0)
    finally:
        conn.close()
    print(f"restored={restored} thaw_requested={thaw_requested} skipped={skipped} bytes={total_bytes}")
    return {"restored": restored, "thaw_requested": thaw_requested,
            "skipped": skipped, "bytes": total_bytes}


def thaw(job, *, scope=".", asof=None, cache_dir, bucket, rclone_config,
         tier="Bulk", runner=subprocess.run) -> dict:
    """Warm up (restore-object) exactly the versions a following restore_all() would
    read: the version current at `asof` for every path under `scope`, and only those
    whose storage_class is cold. A warm version issues nothing and is counted in
    `skipped`; a path with two cold versions issues ONE request (the current one), not
    two -- an rclone-prefix sweep over media/<job>/ would warm the entire history and
    bill for it. Returns {"thaw_requested", "skipped"} and always exits 0."""
    name = job["name"]
    conn = _open_or_fetch_catalog(name, cache_dir,
                                   bucket=bucket, rclone_config=rclone_config, runner=runner)
    try:
        pairs, skipped = _select_scope(conn, asof, scope)
        thaw_requested = 0
        for path, row in pairs:
            if row["storage_class"] in _COLD_CLASSES:
                s3.thaw(row["key"], bucket=bucket, tier=tier, runner=runner)
                print(f"thaw requested: {path}")
                thaw_requested += 1
            else:
                skipped += 1  # already warm -> nothing to do
    finally:
        conn.close()
    print(f"thaw_requested={thaw_requested} skipped={skipped}")
    return {"thaw_requested": thaw_requested, "skipped": skipped}


def _list_json(conn, name) -> dict:
    """The `list --json` shape (refresh restore points, 7.5.6): the current file set
    with per-file storage class, plus the live totals. File history has no folders
    (its scope is "." or one path), so there is no `folders` member (8.5)."""
    rows = []
    for path, row in sorted(catalog.current(conn).items()):
        rows.append({"path": path, "uploaded_at": row["uploaded_at"],
                     "storage_class": row["storage_class"]})
    count, total = catalog.current_totals(conn)
    return {"job": name, "kind": "file-history", "file_count": count,
            "size_bytes": total, "paths": rows}


def _main(argv: list[str]) -> int:
    """CLI entrypoint for ``python3 -m app.engine.vfiles``:

        python3 -m app.engine.vfiles backup <job>
        python3 -m app.engine.vfiles restore <job> list [--json]
        python3 -m app.engine.vfiles restore <job> . <target> \
            [--asof TS] [--tier Bulk|Standard|Expedited]   (-> restore_all)
        python3 -m app.engine.vfiles restore <job> <path> <target> \
            [--asof TS] [--tier Bulk|Standard|Expedited]
        python3 -m app.engine.vfiles thaw <job> <scope|.> \
            [--asof TS] [--tier Bulk|Standard|Expedited]

    Dispatched from scripts/backup-job.sh (the `versioned-files)` case) and
    scripts/restore.sh the same way, with `<job>` matching the `$JOB`/`$job`
    shell var those scripts already resolved via `app.gui.jobs_io`.

    This CLI does NOT re-read config/jobs.json -- it TRUSTS the JOB_* env
    vars those scripts already `eval`'d from jobs_io's (re-validated,
    shell-safe) output -- JOB_SOURCE, JOB_STORAGE_CLASS, JOB_RETENTION_TYPE
    and (depending on its value) JOB_RETENTION_DAYS or JOB_RETENTION_COUNT --
    the same way backup-job.sh's own _run_versioned/_run_archive trust their
    JOB_* vars without re-validating them; jobs_io's own `_main` is the
    re-validation gate (name charset + source confinement) that already ran
    to produce them. It additionally reads the wiring backup-job.sh sets up:
    SOURCE_ROOT, CACHE_DIR, S3_BUCKET (and, for dedicated-bucket jobs,
    JOB_BUCKET, which wins over S3_BUCKET -- mirrors the bash runner's
    ${JOB_BUCKET:-$S3_BUCKET}) -- and derives the rclone config path
    scripts/lib/rclone-conf.sh always renders to: $CACHE_DIR/rclone.conf.
    """
    parser = argparse.ArgumentParser(prog="python3 -m app.engine.vfiles")
    sub = parser.add_subparsers(dest="cmd", required=True)

    backup_p = sub.add_parser("backup", help="run one incremental backup")
    backup_p.add_argument("job", help="job name (the $JOB backup-job.sh resolved)")

    restore_p = sub.add_parser("restore", help="list versions, or recover one file / everything")
    restore_p.add_argument("job", help="job name")
    restore_p.add_argument("path", help='"list", ".", or the relpath of a file to restore')
    restore_p.add_argument("target", nargs="?", default=None,
                            help="target dir (required unless path is 'list')")
    restore_p.add_argument("--asof", type=float, default=None,
                            help="unix timestamp: restore the version current as of then")
    restore_p.add_argument("--tier", default="Bulk", choices=["Bulk", "Standard", "Expedited"],
                            help="Glacier/Deep Archive thaw tier (cold storage classes only)")
    restore_p.add_argument("--json", action="store_true", help="'list' only: emit the restore-points JSON")

    thaw_p = sub.add_parser("thaw", help="warm up (restore-object) the current versions under a scope")
    thaw_p.add_argument("job", help="job name")
    thaw_p.add_argument("scope", nargs="?", default=".", help='"." (whole tree) or a folder prefix')
    thaw_p.add_argument("--asof", type=float, default=None,
                         help="unix timestamp: warm the versions current as of then")
    thaw_p.add_argument("--tier", default="Bulk", choices=["Bulk", "Standard", "Expedited"],
                         help="Glacier/Deep Archive thaw tier")

    browse_p = sub.add_parser("browse", help="list one directory level of the catalog")
    browse_p.add_argument("job")
    browse_p.add_argument("relpath", nargs="?", default="")
    browse_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    # Defense-in-depth: the job name feeds the S3 key prefix (media/<job>/), the
    # prune scope guard, AND the local cache filename (<job>.sqlite). jobs_io's
    # _main re-validates it upstream at run time, but a direct/hand-edited invoke
    # must not be able to smuggle a '/' or '..' through the name and make keys or
    # cache paths escape. Refuse loudly here before any of that is derived.
    if not valid_name(args.job):
        parser.error(f"invalid job name: {args.job!r}")

    def _require_env(name: str) -> str:
        val = os.environ.get(name)
        if not val:
            parser.error(f"missing required environment variable: {name}")
        return val

    cache_dir = _require_env("CACHE_DIR")
    # JOB_BUCKET (dedicated-bucket jobs) wins over S3_BUCKET (the base bucket,
    # still required as a fallback) -- mirrors the bash runner's
    # ${JOB_BUCKET:-$S3_BUCKET} so a dedicated-bucket job's catalog/versions
    # never land in (or get pruned from) the base bucket.
    bucket = os.environ.get("JOB_BUCKET") or _require_env("S3_BUCKET")
    rclone_config = str(Path(cache_dir) / "rclone.conf")

    # browse is read-only and dispatches HERE -- before the retention/
    # JOB_STORAGE_CLASS block below -- so listing a job's catalog never
    # requires the JOB_* env vars that only backup/restore/thaw need.
    if args.cmd == "browse":
        from . import catalog
        import json as _json
        conn = _open_or_fetch_catalog(args.job, cache_dir, bucket=bucket,
                                      rclone_config=rclone_config, runner=subprocess.run)
        try:
            level = catalog.browse(conn, args.relpath or "")
        finally:
            conn.close()
        if args.json:
            print(_json.dumps(level))
        else:
            for e in level["entries"]:
                print(f"{e['kind']}\t{e['name']}\t{e.get('storage_class') or ''}")
        return 0

    # JOB_RETENTION_TYPE selects the policy shape; JOB_RETENTION_DAYS defaults to
    # 90 when unset (backward compat with jobs.json's own versioned-files
    # default -- see jobs_io._normalize_retention), matching today's behavior
    # for any invocation that predates JOB_RETENTION_TYPE. count/keep_all are
    # new: count has no sensible default, so JOB_RETENTION_COUNT is required.
    retention_type = os.environ.get("JOB_RETENTION_TYPE", "days")
    if retention_type == "days":
        days_raw = os.environ.get("JOB_RETENTION_DAYS", "90")
        try:
            policy = {"type": "days", "days": int(days_raw)}
        except ValueError:
            parser.error(f"invalid JOB_RETENTION_DAYS: {days_raw!r}")
    elif retention_type == "count":
        count_raw = _require_env("JOB_RETENTION_COUNT")
        try:
            policy = {"type": "count", "count": int(count_raw)}
        except ValueError:
            parser.error(f"invalid JOB_RETENTION_COUNT: {count_raw!r}")
    elif retention_type == "keep_all":
        policy = {"type": "keep_all"}
    else:
        parser.error(f"invalid JOB_RETENTION_TYPE: {retention_type!r}")

    job = {
        "name": args.job,
        "source": os.environ.get("JOB_SOURCE", ""),
        "storage_class": _require_env("JOB_STORAGE_CLASS"),
        "policy": policy,
    }

    if args.cmd == "backup":
        source_root = str(Path(_require_env("SOURCE_ROOT")) / job["source"])
        stats = backup(job, source_root=source_root, cache_dir=cache_dir,
                        bucket=bucket, rclone_config=rclone_config)
        # Old three keys first (spec 7.5.5); backup-job.sh's _vfiles_stat parses the rest.
        print(f'uploaded={stats["uploaded"]} deleted={stats["deleted"]} pruned={stats["pruned"]}'
              f' bytes={stats.get("bytes", 0)} files_total={stats.get("files_total", 0)}'
              f' bytes_total={stats.get("bytes_total", 0)}')
        return 0

    if args.cmd == "thaw":
        thaw(job, scope=args.scope, asof=args.asof, cache_dir=cache_dir, bucket=bucket,
             rclone_config=rclone_config, tier=args.tier)
        return 0

    # restore
    if args.path == "list":
        if args.target is not None:
            parser.error("'list' takes no target/--asof/--tier")
        if args.json:
            conn = _open_or_fetch_catalog(job["name"], cache_dir,
                                          bucket=bucket, rclone_config=rclone_config,
                                          runner=subprocess.run)
            try:
                import json as _json
                print(_json.dumps(_list_json(conn, job["name"])))
            finally:
                conn.close()
            return 0
        restore(job, path=None, target="", cache_dir=cache_dir, bucket=bucket,
                rclone_config=rclone_config)
        return 0

    if args.target is None:
        parser.error("restore <job> <path|.> <target> [--asof TS] [--tier Bulk|Standard|Expedited]")

    # "." = restore everything as of the point (dispatches to restore_all, spec 7.5.3 §3)
    if args.path == ".":
        restore_all(job, target=args.target, asof=args.asof, cache_dir=cache_dir,
                    bucket=bucket, rclone_config=rclone_config, thaw=args.tier)
        return 0

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
