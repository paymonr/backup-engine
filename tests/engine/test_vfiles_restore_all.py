# Tests for the versioned-files whole-scope restore (vfiles.restore_all), the new
# `vfiles thaw` subcommand, `restore ... list --json`, backup()'s totals return, and
# the two catalog helpers (paths / current_totals). No real S3/network: a stub runner
# captures the exact argv rclone/aws would have been shelled with. Catalogs are seeded
# directly via app.engine.catalog so each test controls the exact version history.
import json
import types

import pytest

from app.engine import catalog, vfiles


class StubRunner:
    """subprocess.run stand-in. Records each argv; rc=0 unless argv contains `fail_on`."""

    def __init__(self, fail_on=None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        rc = 1 if (self.fail_on and any(self.fail_on in a for a in argv)) else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="stub" if rc else "")


def make_job(**over):
    j = {"name": "j", "source": "appdata", "storage_class": "STANDARD", "policy": {"type": "keep_all"}}
    j.update(over)
    return j


def _open(cache, name="j"):
    cache.mkdir(parents=True, exist_ok=True)
    return catalog.open_catalog(str(cache / f"{name}.sqlite"))


def _gets(r):
    return [c for c in r.calls if "copyto" in c and "s3:bkt/media/j/" in " ".join(c)]


def _thaws(r):
    return [c for c in r.calls if "restore-object" in c]


# ---------------------------------------------------------------------------
# catalog.paths / catalog.current_totals
# ---------------------------------------------------------------------------

def test_catalog_paths_distinct_and_current_totals(tmp_path):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "a.txt", "media/j/a.txt@1-x", 10, 1.0, "STANDARD", 1.0)
    catalog.record_version(conn, "a.txt", "media/j/a.txt@2-y", 20, 2.0, "STANDARD", 2.0)  # supersedes
    catalog.record_version(conn, "b/c.txt", "media/j/b/c.txt@1-z", 5, 1.0, "STANDARD", 1.0)
    try:
        assert catalog.paths(conn) == ["a.txt", "b/c.txt"]          # DISTINCT, sorted
        count, total = catalog.current_totals(conn)
        assert count == 2 and total == 25                            # 20 (a.txt current) + 5 (b/c.txt)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# backup() now returns bytes + catalog totals (contract only grows)
# ---------------------------------------------------------------------------

def test_backup_returns_bytes_and_totals(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "a.txt").write_bytes(b"hello")          # 5 bytes
    (src / "b.bin").write_bytes(b"\x00" * 12)      # 12 bytes
    job = make_job()
    stats = vfiles.backup(job, source_root=str(src), cache_dir=str(tmp_path / "cache"),
                          bucket="bkt", rclone_config="/cfg", runner=StubRunner())
    assert stats["uploaded"] == 2 and stats["deleted"] == 0 and stats["pruned"] == 0
    assert stats["bytes"] == 17
    assert stats["files_total"] == 2 and stats["bytes_total"] == 17
    # every old key stays present (contract only grows)
    assert set(stats) >= {"uploaded", "deleted", "pruned", "bytes", "files_total", "bytes_total"}


def test_backup_main_prints_totals_line(tmp_path, capsys, monkeypatch):
    # _main's print line is the unit under test (backup() itself is covered above);
    # monkeypatch backup() so the CLI's formatting is what we assert (spec 7.5.5).
    def fake_backup(job, **kwargs):
        return {"uploaded": 1, "deleted": 0, "pruned": 0,
                "bytes": 2, "files_total": 1, "bytes_total": 2}

    monkeypatch.setattr(vfiles, "backup", fake_backup)
    env = {"SOURCE_ROOT": str(tmp_path), "JOB_SOURCE": "src", "JOB_STORAGE_CLASS": "STANDARD",
           "JOB_RETENTION_TYPE": "keep_all", "CACHE_DIR": str(tmp_path / "cache"), "S3_BUCKET": "bkt"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rc = vfiles._main(["backup", "j"])
    assert rc == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    # old three keys first, then the new ones (spec 7.5.5)
    assert line.startswith("uploaded=1 deleted=0 pruned=0 ")
    assert "bytes=2" in line and "files_total=1" in line and "bytes_total=2" in line


# ---------------------------------------------------------------------------
# vfiles.restore_all — whole-scope "everything as of point"
# ---------------------------------------------------------------------------

def test_restore_all_writes_each_current_path_into_target(tmp_path, capsys):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "a.txt", "media/j/a.txt@2-a", 5, 2.0, "STANDARD", 2.0)
    catalog.record_version(conn, "dir/b.txt", "media/j/dir/b.txt@2-b", 7, 2.0, "STANDARD", 2.0)
    conn.close()
    target = tmp_path / "out" / "2026-09-16"
    r = StubRunner()
    res = vfiles.restore_all(make_job(), target=str(target), cache_dir=str(tmp_path / "cache"),
                             bucket="bkt", rclone_config="/cfg", runner=r)
    assert res == {"restored": 2, "thaw_requested": 0, "skipped": 0, "bytes": 12}
    gets = _gets(r)
    assert any(str(target / "a.txt") in c for c in gets)
    assert any(str(target / "dir" / "b.txt") in c for c in gets)
    out = capsys.readouterr().out
    assert "restored: a.txt" in out
    assert "restored=2 thaw_requested=0 skipped=0 bytes=12" in out


def test_restore_all_asof_picks_older_version(tmp_path):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "a.txt", "media/j/a.txt@100-old", 3, 100.0, "STANDARD", 100.0)
    catalog.record_version(conn, "a.txt", "media/j/a.txt@200-new", 9, 200.0, "STANDARD", 200.0)
    conn.close()
    r = StubRunner()
    res = vfiles.restore_all(make_job(), target=str(tmp_path / "out"), asof=150,
                             cache_dir=str(tmp_path / "cache"), bucket="bkt",
                             rclone_config="/cfg", runner=r)
    assert res["restored"] == 1 and res["bytes"] == 3
    joined = [" ".join(c) for c in r.calls]
    assert any("media/j/a.txt@100-old" in j for j in joined)
    assert not any("media/j/a.txt@200-new" in j for j in joined)


def test_restore_all_skips_a_path_deleted_as_of_now(tmp_path):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "keep.txt", "media/j/keep.txt@100-k", 4, 100.0, "STANDARD", 100.0)
    catalog.record_version(conn, "gone.txt", "media/j/gone.txt@100-g", 4, 100.0, "STANDARD", 100.0)
    catalog.mark_deleted(conn, "gone.txt", 200.0)   # tombstone is newest -> not live now
    conn.close()
    r = StubRunner()
    res = vfiles.restore_all(make_job(), target=str(tmp_path / "out"),
                             cache_dir=str(tmp_path / "cache"), bucket="bkt",
                             rclone_config="/cfg", runner=r)
    assert res["restored"] == 1 and res["skipped"] == 1
    assert not any("gone.txt" in " ".join(c) for c in r.calls)


def test_restore_all_cold_version_thaws_not_gets(tmp_path, capsys):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "cold.txt", "media/j/cold.txt@100-c", 8, 100.0, "DEEP_ARCHIVE", 100.0)
    conn.close()
    r = StubRunner()
    res = vfiles.restore_all(make_job(), target=str(tmp_path / "out"),
                             cache_dir=str(tmp_path / "cache"), bucket="bkt",
                             rclone_config="/cfg", thaw="Standard", runner=r)
    assert res == {"restored": 0, "thaw_requested": 1, "skipped": 0, "bytes": 0}
    assert not _gets(r)                                   # cold -> no direct download
    thaws = _thaws(r)
    assert thaws and thaws[0] == [
        "aws", "s3api", "restore-object",
        "--bucket", "bkt", "--key", "media/j/cold.txt@100-c",
        "--restore-request", "Days=7,GlacierJobParameters={Tier=Standard}",
    ]
    assert "thaw requested: cold.txt" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# vfiles.thaw — catalog-selected warm-up (NOT an rclone-prefix sweep)
# ---------------------------------------------------------------------------

def test_thaw_two_cold_versions_issues_exactly_one(tmp_path, capsys):
    # Two versions of ONE path, both cold. The rclone-prefix approach would warm
    # (and bill for) both; the catalog approach warms only the CURRENT one.
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "a.txt", "media/j/a.txt@100-old", 3, 100.0, "DEEP_ARCHIVE", 100.0)
    catalog.record_version(conn, "a.txt", "media/j/a.txt@200-new", 3, 200.0, "DEEP_ARCHIVE", 200.0)
    conn.close()
    r = StubRunner()
    res = vfiles.thaw(make_job(), scope=".", cache_dir=str(tmp_path / "cache"),
                      bucket="bkt", rclone_config="/cfg", tier="Bulk", runner=r)
    thaws = _thaws(r)
    assert len(thaws) == 1
    assert "media/j/a.txt@200-new" in " ".join(thaws[0])       # the current version
    assert "media/j/a.txt@100-old" not in " ".join(thaws[0])
    assert res["thaw_requested"] == 1
    assert "thaw_requested=1" in capsys.readouterr().out


def test_thaw_warm_version_counted_in_skipped(tmp_path, capsys):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "warm.txt", "media/j/warm.txt@100-w", 3, 100.0, "STANDARD", 100.0)
    conn.close()
    r = StubRunner()
    res = vfiles.thaw(make_job(), scope=".", cache_dir=str(tmp_path / "cache"),
                      bucket="bkt", rclone_config="/cfg", runner=r)
    assert res == {"thaw_requested": 0, "skipped": 1}
    assert not _thaws(r)
    assert "thaw_requested=0 skipped=1" in capsys.readouterr().out


def test_thaw_and_restore_all_agree_on_count(tmp_path):
    # Same catalog selection backs both, so a warm-up then a restore report the same
    # thaw_requested and never warm a version the restore will not read.
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "a.txt", "media/j/a.txt@100-old", 3, 100.0, "DEEP_ARCHIVE", 100.0)
    catalog.record_version(conn, "a.txt", "media/j/a.txt@200-new", 3, 200.0, "DEEP_ARCHIVE", 200.0)
    catalog.record_version(conn, "b.txt", "media/j/b.txt@100-b", 3, 100.0, "STANDARD", 100.0)
    conn.close()
    t = vfiles.thaw(make_job(), scope=".", cache_dir=str(tmp_path / "cache"),
                    bucket="bkt", rclone_config="/cfg", runner=StubRunner())
    ra = vfiles.restore_all(make_job(), target=str(tmp_path / "out"),
                            cache_dir=str(tmp_path / "cache"), bucket="bkt",
                            rclone_config="/cfg", runner=StubRunner())
    assert t["thaw_requested"] == ra["thaw_requested"] == 1


# ---------------------------------------------------------------------------
# restore ... list --json  (the refresh-restore-points shape)
# ---------------------------------------------------------------------------

def test_restore_list_json_shape(tmp_path, monkeypatch, capsys):
    conn = _open(tmp_path / "cache")
    catalog.record_version(conn, "a.txt", "media/j/a.txt@1-x", 10, 1.0, "STANDARD", 1.0)
    catalog.record_version(conn, "a.txt", "media/j/a.txt@2-y", 20, 2.0, "STANDARD", 2.0)
    catalog.record_version(conn, "b.txt", "media/j/b.txt@1-z", 5, 1.0, "DEEP_ARCHIVE", 1.0)
    conn.close()
    for k, v in {"CACHE_DIR": str(tmp_path / "cache"), "S3_BUCKET": "bkt",
                 "JOB_STORAGE_CLASS": "STANDARD", "JOB_SOURCE": "appdata",
                 "JOB_RETENTION_TYPE": "keep_all"}.items():
        monkeypatch.setenv(k, v)
    rc = vfiles._main(["restore", "j", "list", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["job"] == "j" and data["kind"] == "file-history"
    assert data["file_count"] == 2 and data["size_bytes"] == 25
    paths = {p["path"]: p for p in data["paths"]}
    assert set(paths) == {"a.txt", "b.txt"}
    assert paths["a.txt"]["storage_class"] == "STANDARD"
    assert paths["b.txt"]["storage_class"] == "DEEP_ARCHIVE"


def test_cli_restore_dot_dispatches_restore_all(tmp_path, monkeypatch):
    captured = {}

    def fake_restore_all(job, **kwargs):
        captured["job"] = job
        captured["kwargs"] = kwargs
        return {"restored": 0, "thaw_requested": 0, "skipped": 0, "bytes": 0}

    monkeypatch.setattr(vfiles, "restore_all", fake_restore_all)
    for k, v in {"CACHE_DIR": str(tmp_path / "cache"), "S3_BUCKET": "bkt",
                 "JOB_STORAGE_CLASS": "STANDARD", "JOB_SOURCE": "appdata",
                 "JOB_RETENTION_TYPE": "keep_all"}.items():
        monkeypatch.setenv(k, v)
    rc = vfiles._main(["restore", "j", ".", "/out", "--asof", "1700000000", "--tier", "Standard"])
    assert rc == 0
    assert captured["job"]["name"] == "j"
    assert captured["kwargs"]["target"] == "/out"
    assert captured["kwargs"]["asof"] == 1700000000.0
    assert captured["kwargs"]["thaw"] == "Standard"


def test_cli_thaw_subcommand_dispatches(tmp_path, monkeypatch):
    captured = {}

    def fake_thaw(job, **kwargs):
        captured["job"] = job
        captured["kwargs"] = kwargs
        return {"thaw_requested": 0, "skipped": 0}

    monkeypatch.setattr(vfiles, "thaw", fake_thaw)
    for k, v in {"CACHE_DIR": str(tmp_path / "cache"), "S3_BUCKET": "bkt",
                 "JOB_STORAGE_CLASS": "DEEP_ARCHIVE", "JOB_SOURCE": "appdata",
                 "JOB_RETENTION_TYPE": "keep_all"}.items():
        monkeypatch.setenv(k, v)
    rc = vfiles._main(["thaw", "j", ".", "--tier", "Standard"])
    assert rc == 0
    assert captured["job"]["name"] == "j"
    assert captured["kwargs"]["scope"] == "."
    assert captured["kwargs"]["tier"] == "Standard"
