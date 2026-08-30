# Tests for the versioned-files backup runner (app/engine/vfiles.backup) and the
# thin subprocess S3 wrappers (app/engine/s3). No real network/S3: a stub runner
# captures the exact argv rclone would have been shelled with, and every case
# uses a temp source tree + temp cache_dir. The stub NEVER touches S3.
import os
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
    key1 = f"media/j/a.txt@{now1}"
    puts = [c for c in r1.calls if "copyto" in c and f"s3:bkt/{key1}" in c]
    assert puts, r1.calls
    put = puts[0]
    assert put[put.index("--s3-storage-class") + 1] == "DEEP_ARCHIVE"
    # catalog reflects the new current version at that exact key
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    assert catalog.current(conn)["a.txt"]["key"] == key1
    conn.close()
    # catalog uploaded for durability at the end
    assert any("s3:bkt/media/j/_catalog/catalog.sqlite" in c for c in r1.calls)

    # --- changed file -> DISTINCT new version-key, old becomes non-current ---
    (src / "a.txt").write_bytes(b"hello, world!!")  # different size
    os.utime(src / "a.txt", (1050, 1050))
    r2 = StubRunner()
    now2 = 1_000_100
    s2 = vfiles.backup(job, source_root=str(src), cache_dir=str(cache),
                       bucket="bkt", rclone_config="/cfg", now=now2, runner=r2)
    assert s2["uploaded"] == 1
    key2 = f"media/j/a.txt@{now2}"
    assert any(f"s3:bkt/{key2}" in c for c in r2.calls)
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    assert catalog.current(conn)["a.txt"]["key"] == key2
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
    # nothing uploaded to a version-key on a pure-delete run (catalog upload aside)
    assert not any("copyto" in c and any("@1_000" in a for a in c) for c in r3.calls)
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
