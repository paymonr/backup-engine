# Tests for the versioned-files restore runner (app/engine/vfiles.restore) and
# its S3 thaw wrapper (app/engine/s3.thaw). No real network/S3: a stub runner
# captures the exact argv rclone/aws would have been shelled with. Catalogs are
# seeded directly via app.engine.catalog (no backup() call needed) so each test
# controls the exact version history under test.
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
    j = {"name": "j", "source": "/src", "storage_class": "STANDARD", "retention_days": 30}
    j.update(over)
    return j


def seed_two_versions(cache, name="j"):
    """Seed <cache>/<name>.sqlite with a path 'a.txt' that has two versions:
    an old (non-current) STANDARD one at t=100 and a new (current) one at
    t=200. Returns (old_key, new_key)."""
    cache.mkdir(parents=True, exist_ok=True)
    conn = catalog.open_catalog(str(cache / f"{name}.sqlite"))
    old_key = "media/j/a.txt@100-aaaaaaaa"
    new_key = "media/j/a.txt@200-bbbbbbbb"
    catalog.record_version(conn, "a.txt", old_key, 3, 100.0, "STANDARD", 100.0)
    catalog.record_version(conn, "a.txt", new_key, 5, 200.0, "STANDARD", 200.0)
    conn.close()
    return old_key, new_key


# ---------------------------------------------------------------------------
# s3.thaw — thin aws-cli wrapper, exact argv, S3Error on non-zero
# ---------------------------------------------------------------------------

def test_s3_thaw_argv_defaults():
    r = StubRunner()
    s3.thaw("media/j/a.txt@100-aaaaaaaa", bucket="bkt", runner=r)
    assert r.calls[0] == [
        "aws", "s3api", "restore-object",
        "--bucket", "bkt", "--key", "media/j/a.txt@100-aaaaaaaa",
        "--restore-request", "Days=7,GlacierJobParameters={Tier=Bulk}",
    ]


def test_s3_thaw_argv_custom_tier_days():
    r = StubRunner()
    s3.thaw("k", bucket="bkt", tier="Expedited", days=1, runner=r)
    assert r.calls[0] == [
        "aws", "s3api", "restore-object",
        "--bucket", "bkt", "--key", "k",
        "--restore-request", "Days=1,GlacierJobParameters={Tier=Expedited}",
    ]


def test_s3_thaw_raises_on_nonzero():
    r = StubRunner(fail_on="restore-object")
    with pytest.raises(s3.S3Error):
        s3.thaw("k", bucket="bkt", runner=r)


# ---------------------------------------------------------------------------
# vfiles.restore — list / latest / asof / thaw
# ---------------------------------------------------------------------------

def test_restore_list_mode_prints_paths_and_versions(tmp_path, capsys):
    cache = tmp_path / "cache"
    seed_two_versions(cache)
    job = make_job()

    out = vfiles.restore(job, target=str(tmp_path / "out"), cache_dir=str(cache),
                          bucket="bkt", rclone_config="/cfg", runner=StubRunner())

    assert isinstance(out, list)
    assert len(out) == 2  # both versions of a.txt
    assert {r["uploaded_at"] for r in out} == {100.0, 200.0}
    assert all(r["path"] == "a.txt" for r in out)
    assert all(r["storage_class"] == "STANDARD" for r in out)
    printed = capsys.readouterr().out
    assert "a.txt" in printed and "100.0" in printed and "200.0" in printed


def test_restore_latest_downloads_newest_key(tmp_path):
    cache = tmp_path / "cache"
    old_key, new_key = seed_two_versions(cache)
    job = make_job()
    r = StubRunner()

    result = vfiles.restore(job, path="a.txt", target=str(tmp_path / "out"),
                             cache_dir=str(cache), bucket="bkt", rclone_config="/cfg", runner=r)

    assert result["status"] == "restored"
    assert result["key"] == new_key
    gets = [c for c in r.calls if "copyto" in c]
    assert any(f"s3:bkt/{new_key}" in c and str(tmp_path / "out" / "a.txt") in c for c in gets)
    assert not any(old_key in " ".join(c) for c in r.calls)  # old version never fetched


def test_restore_asof_picks_version_current_at_that_time(tmp_path):
    cache = tmp_path / "cache"
    old_key, new_key = seed_two_versions(cache)
    job = make_job()
    r = StubRunner()

    # asof=150 is between the old (t=100) and new (t=200) uploads -> old was
    # the version "current" at that time.
    result = vfiles.restore(job, path="a.txt", target=str(tmp_path / "out"),
                             cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                             asof=150, runner=r)

    assert result["status"] == "restored"
    assert result["key"] == old_key
    gets = [c for c in r.calls if "copyto" in c]
    assert any(f"s3:bkt/{old_key}" in c for c in gets)
    assert not any(new_key in " ".join(c) for c in r.calls)


def test_restore_asof_exact_boundary_is_inclusive(tmp_path):
    # asof == the older version's own uploaded_at -> that version is "current"
    # at that instant (>=, not >).
    cache = tmp_path / "cache"
    old_key, new_key = seed_two_versions(cache)
    job = make_job()
    r = StubRunner()

    result = vfiles.restore(job, path="a.txt", target=str(tmp_path / "out"),
                             cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                             asof=100, runner=r)
    assert result["key"] == old_key


def test_restore_asof_before_any_version_raises(tmp_path):
    cache = tmp_path / "cache"
    seed_two_versions(cache)
    job = make_job()

    with pytest.raises(LookupError):
        vfiles.restore(job, path="a.txt", target=str(tmp_path / "out"),
                        cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                        asof=50, runner=StubRunner())


def test_restore_unknown_path_raises(tmp_path):
    cache = tmp_path / "cache"
    seed_two_versions(cache)
    job = make_job()

    with pytest.raises(LookupError):
        vfiles.restore(job, path="nope.txt", target=str(tmp_path / "out"),
                        cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                        runner=StubRunner())


def test_restore_skips_tombstone_version(tmp_path):
    # A path that was deleted after its only real version must not be
    # "restorable" past the deletion -- asof after the tombstone finds nothing.
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    catalog.record_version(conn, "gone.txt", "media/j/gone.txt@100-x", 3, 100.0, "STANDARD", 100.0)
    catalog.mark_deleted(conn, "gone.txt", 200.0)
    conn.close()
    job = make_job()

    # asof after the tombstone: newest non-tombstone version is still the one
    # at t=100 (asof only bounds by uploaded_at, and the tombstone itself,
    # having deleted=1, is never a restore candidate) -- so it IS found.
    result = vfiles.restore(job, path="gone.txt", target=str(tmp_path / "out"),
                             cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                             asof=250, runner=StubRunner())
    assert result["key"] == "media/j/gone.txt@100-x"

    # a path with ONLY a tombstone (never a real version) has no candidates
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    catalog.mark_deleted(conn, "never-real.txt", 50.0)
    conn.close()
    with pytest.raises(LookupError):
        vfiles.restore(job, path="never-real.txt", target=str(tmp_path / "out"),
                        cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                        runner=StubRunner())


def test_restore_cold_class_triggers_thaw_not_get(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    key = "media/j/cold.txt@100-x"
    catalog.record_version(conn, "cold.txt", key, 3, 100.0, "DEEP_ARCHIVE", 100.0)
    conn.close()
    job = make_job()
    r = StubRunner()

    result = vfiles.restore(job, path="cold.txt", target=str(tmp_path / "out"),
                             cache_dir=str(cache), bucket="bkt", rclone_config="/cfg", runner=r)

    assert result["status"] == "thaw-requested"
    assert result["key"] == key
    assert not any("copyto" in c for c in r.calls)  # no direct download
    thaws = [c for c in r.calls if "restore-object" in c]
    assert thaws and thaws[0] == [
        "aws", "s3api", "restore-object",
        "--bucket", "bkt", "--key", key,
        "--restore-request", "Days=7,GlacierJobParameters={Tier=Bulk}",
    ]


def test_restore_cold_class_custom_thaw_tier(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    conn = catalog.open_catalog(str(cache / "j.sqlite"))
    key = "media/j/cold.txt@100-x"
    catalog.record_version(conn, "cold.txt", key, 3, 100.0, "GLACIER", 100.0)
    conn.close()
    job = make_job()
    r = StubRunner()

    vfiles.restore(job, path="cold.txt", target=str(tmp_path / "out"),
                    cache_dir=str(cache), bucket="bkt", rclone_config="/cfg",
                    thaw="Expedited", runner=r)

    thaws = [c for c in r.calls if "restore-object" in c]
    assert thaws[0][thaws[0].index("--restore-request") + 1] == "Days=7,GlacierJobParameters={Tier=Expedited}"


def test_restore_attempts_catalog_download_and_survives_failure(tmp_path):
    cache = tmp_path / "cache"  # no local catalog -> download attempted
    job = make_job()
    r = FailFirstRunner()  # the download attempt fails; restore must still run (empty catalog)

    out = vfiles.restore(job, target=str(tmp_path / "out"), cache_dir=str(cache),
                          bucket="bkt", rclone_config="/cfg", runner=r)

    assert r.calls[0] == [
        "rclone", "--config", "/cfg", "copyto",
        "s3:bkt/media/j/_catalog/catalog.sqlite", str(cache / "j.sqlite"),
    ]
    assert out == []  # empty catalog -> empty list


def test_restore_reuses_local_catalog_without_download(tmp_path):
    cache = tmp_path / "cache"
    seed_two_versions(cache)
    job = make_job()
    r = StubRunner()

    vfiles.restore(job, target=str(tmp_path / "out"), cache_dir=str(cache),
                    bucket="bkt", rclone_config="/cfg", runner=r)

    assert not any("_catalog" in " ".join(c) for c in r.calls)  # cache already present
