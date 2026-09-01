# Tests for the versioned-files backup runner (app/engine/vfiles.backup) and the
# thin subprocess S3 wrappers (app/engine/s3). No real network/S3: a stub runner
# captures the exact argv rclone would have been shelled with, and every case
# uses a temp source tree + temp cache_dir. The stub NEVER touches S3.
import os
import sqlite3
import types

import pytest

from app.engine import catalog, s3, vfiles


class StubRunner:
    """subprocess.run stand-in. Records each argv; returns rc=0 unless the argv
    contains `fail_on` as a substring (then rc=1, so s3.py raises S3Error)."""

    def __init__(self, fail_on=None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        rc = 1 if (self.fail_on and any(self.fail_on in a for a in argv)) else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="stub" if rc else "")


class FailFirstRunner(StubRunner):
    """Fails only its very first call (the best-effort catalog download)."""

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        rc = 1 if len(self.calls) == 1 else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="stub" if rc else "")


def make_job(**over):
    j = {"name": "j", "source": "/src", "storage_class": "DEEP_ARCHIVE", "retention_days": 30}
    j.update(over)
    return j


def joined(calls):
    return [" ".join(c) for c in calls]


# ---------------------------------------------------------------------------
# s3.py — thin wrappers, exact argv, S3Error on non-zero
# ---------------------------------------------------------------------------

def test_s3_put_delete_get_argv():
    r = StubRunner()
    s3.put("/local/f", "media/j/f@1", "GLACIER", bucket="b", rclone_config="/cfg", runner=r)
    assert r.calls[0] == [
        "rclone", "--config", "/cfg", "copyto",
        "/local/f", "s3:b/media/j/f@1", "--s3-storage-class", "GLACIER",
    ]
    s3.delete("media/j/f@1", bucket="b", rclone_config="/cfg", runner=r)
    assert r.calls[1] == ["rclone", "--config", "/cfg", "deletefile", "s3:b/media/j/f@1"]
    s3.get("media/j/f@1", "/local/out", bucket="b", rclone_config="/cfg", runner=r)
    assert r.calls[2] == ["rclone", "--config", "/cfg", "copyto", "s3:b/media/j/f@1", "/local/out"]


def test_s3_raises_on_nonzero():
    r = StubRunner(fail_on="copyto")
    with pytest.raises(s3.S3Error):
        s3.put("/l", "k", "STANDARD", bucket="b", rclone_config="/c", runner=r)


def test_s3_catalog_keys():
    r = StubRunner()
    s3.upload_catalog("myjob", "/tmp/c.sqlite", bucket="b", rclone_config="/c", runner=r)
    assert r.calls[-1][:6] == [
        "rclone", "--config", "/c", "copyto",
        "/tmp/c.sqlite", "s3:b/media/myjob/_catalog/catalog.sqlite",
    ]
    s3.download_catalog("myjob", "/tmp/c.sqlite", bucket="b", rclone_config="/c", runner=r)
    assert r.calls[-1] == [
        "rclone", "--config", "/c", "copyto",
        "s3:b/media/myjob/_catalog/catalog.sqlite", "/tmp/c.sqlite",
    ]


# ---------------------------------------------------------------------------
# vfiles.backup — incremental version-keys, tombstones, catalog durability
# ---------------------------------------------------------------------------

def test_backup_new_changed_removed_and_catalog_upload(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    os.utime(src / "a.txt", (1000, 1000))
    cache = tmp_path / "cache"
    job = make_job()

    # --- new file ---
    r1 = StubRunner()
    now1 = 1_000_000
    s1 = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                       bucket="bkt", rclone_config="/cfg", now=now1, runner=r1)
    assert s1 == {"uploaded": 1, "deleted": 0, "pruned": 0}
    key1_prefix = f"media/j/a.txt@{now1}-"  # <ts>-<uuid8>, exact suffix is random
    puts = [c for c in r1.calls
            if "copyto" in c and any(a.startswith(f"s3:bkt/{key1_prefix}") for a in c)]
    assert puts, r1.calls
    put = puts[0]
    assert put[put.index("--s3-storage-class") + 1] == "DEEP_ARCHIVE"
    # catalog reflects the new current version at that (collision-resistant) key
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    key1 = catalog.current(conn)["a.txt"]["key"]
    assert key1.startswith(key1_prefix)
    conn.close()
    # catalog uploaded for durability at the end, forced to STANDARD (the PUT is
    # the call carrying --s3-storage-class; the earlier GET/download has none)
    cat_puts = [c for c in r1.calls
                if "s3:bkt/media/j/_catalog/catalog.sqlite" in c and "--s3-storage-class" in c]
    assert cat_puts, r1.calls
    assert cat_puts[0][cat_puts[0].index("--s3-storage-class") + 1] == "STANDARD"

    # --- changed file -> DISTINCT new version-key, old becomes non-current ---
    (src / "a.txt").write_bytes(b"hello, world!!")  # different size
    os.utime(src / "a.txt", (1050, 1050))
    r2 = StubRunner()
    now2 = 1_000_100
    s2 = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                       bucket="bkt", rclone_config="/cfg", now=now2, runner=r2)
    assert s2["uploaded"] == 1
    key2_prefix = f"media/j/a.txt@{now2}-"
    assert any(any(a.startswith(f"s3:bkt/{key2_prefix}") for a in c) for c in r2.calls)
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    key2 = catalog.current(conn)["a.txt"]["key"]
    assert key2.startswith(key2_prefix)
    assert key2 != key1  # distinct version-key from the first upload
    assert len(catalog.versions(conn, "a.txt")) == 2  # both versions retained
    conn.close()

    # --- removed file -> tombstone, drops out of current() ---
    (src / "a.txt").unlink()
    r3 = StubRunner()
    now3 = 1_000_200
    s3sum = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                          bucket="bkt", rclone_config="/cfg", now=now3, runner=r3)
    assert s3sum["deleted"] == 1
    assert s3sum["uploaded"] == 0
    # a delete-only run pushes NO version-key object (only the catalog upload,
    # whose key has no '@'): assert no copyto targets an @-versioned job key.
    assert not any(
        "copyto" in c and any(a.startswith("s3:bkt/media/j/") and "@" in a for a in c)
        for c in r3.calls
    )
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    assert "a.txt" not in catalog.current(conn)
    conn.close()


def test_backup_prune_deletes_old_versions(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    old_key = "media/j/a.txt@100"
    catalog.record_version(conn, "a.txt", old_key, 5, 100.0, "STANDARD", 100.0)          # old, non-current
    catalog.record_version(conn, "a.txt", "media/j/a.txt@100000", 5, 100.0, "STANDARD", 100000.0)  # current
    conn.close()
    # unchanged source (size 5, mtime 100) -> no new upload, current stays current
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    os.utime(src / "a.txt", (100, 100))

    job = make_job(retention_days=1)
    now = 1_000_000  # before = now - 86400 = 913600; old(100) prunable, current excluded by is_current
    r = StubRunner()
    s = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=now, runner=r)
    assert s["pruned"] == 1
    dels = [c for c in r.calls if "deletefile" in c]
    assert any(f"s3:bkt/{old_key}" in c for c in dels)
    assert not any("a.txt@100000" in j for j in joined(dels))  # current never deleted
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    assert all(v["key"] != old_key for v in catalog.versions(conn, "a.txt"))
    assert catalog.current(conn)["a.txt"]["key"] == "media/j/a.txt@100000"
    conn.close()


def test_prune_skips_tombstone_s3_delete(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    catalog.record_version(conn, "gone.txt", "media/j/gone.txt@100", 5, 100.0, "STANDARD", 100.0)
    catalog.mark_deleted(conn, "gone.txt", 200.0)  # tombstone: key=None; real version now non-current
    conn.close()
    src = tmp_path / "src"
    src.mkdir()  # empty source

    job = make_job(retention_days=1)
    now = 1_000_000  # before = 913600; both rows old -> both prunable
    r = StubRunner()
    s = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=now, runner=r)
    dels = [c for c in r.calls if "deletefile" in c]
    # real version deleted from S3 ...
    assert any("s3:bkt/media/j/gone.txt@100" in c for c in dels)
    # ... but the tombstone (key=None) is NEVER handed to s3.delete
    assert not any("None" in j for j in joined(r.calls))
    assert s["pruned"] == 2  # both catalog rows pruned
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    assert catalog.versions(conn, "gone.txt") == []
    conn.close()


def test_prune_refuses_key_outside_job_prefix(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    # crafted attack: an old non-current row whose key points OUTSIDE media/j/
    catalog.record_version(conn, "p", "media/OTHERJOB/secret@1", 1, 1.0, "STANDARD", 1.0)
    catalog.record_version(conn, "p", "media/j/p@2", 1, 1.0, "STANDARD", 2.0)  # current, in-prefix
    conn.close()
    src = tmp_path / "src"
    src.mkdir()
    (src / "p").write_bytes(b"x")
    os.utime(src / "p", (1, 1))  # unchanged vs current -> no upload

    job = make_job(retention_days=1)
    now = 1_000_000  # bad row (uploaded_at 1.0) is prunable
    r = StubRunner()
    with pytest.raises(vfiles.PruneScopeError):
        vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=now, runner=r)
    # the out-of-prefix key was NEVER deleted from S3
    assert not any("media/OTHERJOB/secret@1" in j for j in joined(r.calls))


def test_prune_refuses_dotdot_traversal_key(tmp_path):
    # A crafted key that STARTS WITH media/<job>/ but contains a '..' segment
    # would traverse out of the job prefix ("media/j/../otherjob/x"). startswith
    # alone accepts it, so the guard must reject '..' segments explicitly and
    # never s3.delete it.
    cache = tmp_path / "cache"
    cache.mkdir()
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    catalog.record_version(conn, "p", "media/j/../otherjob/secret@1", 1, 1.0, "STANDARD", 1.0)
    catalog.record_version(conn, "p", "media/j/p@2", 1, 1.0, "STANDARD", 2.0)  # current, in-prefix
    conn.close()
    src = tmp_path / "src"
    src.mkdir()
    (src / "p").write_bytes(b"x")
    os.utime(src / "p", (1, 1))  # unchanged vs current -> no upload

    job = make_job(retention_days=1)
    now = 1_000_000  # bad row (uploaded_at 1.0) is prunable
    r = StubRunner()
    with pytest.raises(vfiles.PruneScopeError):
        vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=now, runner=r)
    # the traversal key was NEVER handed to s3.delete
    assert not any("otherjob/secret" in j for j in joined(r.calls))


def test_prune_refuses_out_of_prefix_key_even_when_marked_current(tmp_path):
    # Hardening: the scope guard runs BEFORE the is_current check, so an
    # out-of-prefix key is refused even if a crafted is_current=1 row ALSO
    # points at it (which would otherwise short-circuit the old guard and let
    # the row be pruned silently). Must raise PruneScopeError.
    cache = tmp_path / "cache"
    cache.mkdir()
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    evil = "media/OTHERJOB/secret@1"
    catalog.record_version(conn, "p", evil, 1, 1.0, "STANDARD", 1.0)   # old, non-current -> prunable
    catalog.record_version(conn, "p", evil, 1, 1.0, "STANDARD", 2.0)   # current, SAME out-of-prefix key
    conn.close()
    src = tmp_path / "src"
    src.mkdir()
    (src / "p").write_bytes(b"x")
    os.utime(src / "p", (1, 1))  # unchanged vs current -> no upload

    job = make_job(retention_days=1)
    now = 1_000_000  # old row (uploaded_at 1.0) is prunable
    r = StubRunner()
    with pytest.raises(vfiles.PruneScopeError):
        vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=now, runner=r)
    assert not any("media/OTHERJOB/secret@1" in j for j in joined(r.calls))


def test_corrupt_catalog_fails_safe_without_deleting(tmp_path):
    # A corrupt/malformed catalog.sqlite must degrade safely: backup raises on
    # open (before the prune loop) and NEVER issues an s3.delete or a catalog
    # upload that would overwrite the durable copy with the corrupt bytes.
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "j.sqlite").write_bytes(b"not a sqlite database, just garbage")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")

    job = make_job(retention_days=1)
    r = StubRunner()
    with pytest.raises(sqlite3.DatabaseError):
        vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=1_000_000, runner=r)
    # no deletion and no upload happened -- the run aborted before touching S3
    assert not any("deletefile" in c for c in r.calls)
    assert not any("copyto" in c for c in r.calls)


def test_same_second_change_yields_distinct_keys_no_aliasing(tmp_path):
    # Two backups in the SAME wall-clock second, with a size change between them,
    # must NOT share a version-key (that would let pruning the old row delete the
    # object the current row still points at).
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"one")
    os.utime(src / "a.txt", (10, 10))
    cache = tmp_path / "cache"
    job = make_job(retention_days=1)
    now = 1_000_000  # identical for both runs

    vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                  bucket="bkt", rclone_config="/c", now=now, runner=StubRunner())
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    k1 = catalog.current(conn)["a.txt"]["key"]
    conn.close()

    (src / "a.txt").write_bytes(b"two-different-length")  # size change
    os.utime(src / "a.txt", (11, 11))
    vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                  bucket="bkt", rclone_config="/c", now=now, runner=StubRunner())
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    keys = [v["key"] for v in catalog.versions(conn, "a.txt")]
    k2 = catalog.current(conn)["a.txt"]["key"]
    conn.close()

    assert k1.startswith(f"media/j/a.txt@{now}-") and k2.startswith(f"media/j/a.txt@{now}-")
    assert k1 != k2               # distinct despite the identical second
    assert len(set(keys)) == 2    # no two rows share an object


def test_prune_never_deletes_object_a_current_row_shares(tmp_path):
    # Belt-and-suspenders: even if two rows somehow shared a key, pruning the old
    # one must NOT s3.delete the object the current row still references.
    cache = tmp_path / "cache"
    cache.mkdir()
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    shared = "media/j/a.txt@1000-deadbeef"
    catalog.record_version(conn, "a.txt", shared, 3, 10.0, "STANDARD", 100.0)   # old, non-current
    catalog.record_version(conn, "a.txt", shared, 5, 11.0, "STANDARD", 100.0)   # current, SAME crafted key
    conn.close()
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    os.utime(src / "a.txt", (11, 11))  # size 5, mtime 11 -> matches current -> unchanged

    job = make_job(retention_days=1)
    now = 1_000_000  # old row (uploaded_at 100) < before(913600) -> prunable
    r = StubRunner()
    s = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=now, runner=r)
    assert s["pruned"] == 1                              # old row removed from catalog
    assert not any(shared in j for j in joined(r.calls))  # object NEVER deleted from S3
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    assert catalog.current(conn)["a.txt"]["key"] == shared  # current version intact
    conn.close()


def test_backup_attempts_catalog_download_and_survives_failure(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hi")
    os.utime(src / "a.txt", (500, 500))
    cache = tmp_path / "cache"  # no local catalog -> download attempted
    job = make_job()
    r = FailFirstRunner()  # the download attempt fails; backup must still succeed empty
    s = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                      bucket="bkt", rclone_config="/c", now=1000, runner=r)
    # first shell call was the best-effort catalog download from the exact key
    assert r.calls[0] == [
        "rclone", "--config", "/c", "copyto",
        "s3:bkt/media/j/_catalog/catalog.sqlite", str(cache / "j.sqlite"),
    ]
    assert s["uploaded"] == 1  # started from an empty catalog despite the failed download
