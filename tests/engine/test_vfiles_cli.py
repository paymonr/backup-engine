# Tests for the `python3 -m app.engine.vfiles` CLI (app.engine.vfiles._main):
# argparse dispatch + the env -> job-dict / wiring mapping that
# scripts/backup-job.sh and scripts/restore.sh rely on (see the `versioned-files)`
# case in each). No real S3/network: vfiles.backup/vfiles.restore are
# monkeypatched to capture their call args instead of touching S3.
import pytest

from app.engine import vfiles


def _set_common_env(monkeypatch, **over):
    env = {
        "SOURCE_ROOT": "/backup/media",
        "JOB_SOURCE": "appdata",
        "JOB_STORAGE_CLASS": "DEEP_ARCHIVE",
        "JOB_RETENTION_DAYS": "30",
        "CACHE_DIR": "/cache",
        "S3_BUCKET": "my-bucket",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_backup_builds_job_dict_and_wiring_from_env(monkeypatch):
    _set_common_env(monkeypatch)
    captured = {}

    def fake_backup(job, **kwargs):
        captured["job"] = job
        captured["kwargs"] = kwargs
        return {"uploaded": 1, "deleted": 0, "pruned": 2}

    monkeypatch.setattr(vfiles, "backup", fake_backup)

    rc = vfiles._main(["backup", "vf"])

    assert rc == 0
    assert captured["job"] == {
        "name": "vf", "source": "appdata",
        "storage_class": "DEEP_ARCHIVE", "retention_days": 30,
    }
    assert captured["kwargs"] == {
        "source_root": "/backup/media/appdata",
        "cache_dir": "/cache",
        "bucket": "my-bucket",
        "rclone_config": "/cache/rclone.conf",
    }


def test_backup_default_retention_days_when_env_missing(monkeypatch):
    _set_common_env(monkeypatch)
    monkeypatch.delenv("JOB_RETENTION_DAYS", raising=False)
    captured = {}

    def fake_backup(job, **kwargs):
        captured["job"] = job
        return {"uploaded": 0, "deleted": 0, "pruned": 0}

    monkeypatch.setattr(vfiles, "backup", fake_backup)

    rc = vfiles._main(["backup", "vf"])

    assert rc == 0
    assert captured["job"]["retention_days"] == 90  # matches jobs_io's default


def test_backup_missing_env_exits_2(monkeypatch):
    for name in ("CACHE_DIR", "S3_BUCKET", "SOURCE_ROOT", "JOB_STORAGE_CLASS",
                 "JOB_SOURCE", "JOB_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as exc:
        vfiles._main(["backup", "vf"])
    assert exc.value.code == 2


def test_backup_invalid_retention_days_exits_2(monkeypatch):
    _set_common_env(monkeypatch, JOB_RETENTION_DAYS="not-a-number")

    with pytest.raises(SystemExit) as exc:
        vfiles._main(["backup", "vf"])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["../evil", "..", "a/b", "foo/../bar", "with space", ""])
def test_backup_rejects_invalid_job_name(monkeypatch, bad):
    # Defense-in-depth: a hand-edited jobs.json (or a direct invoke) must not be
    # able to smuggle a name that escapes the media/<job>/ key prefix or the
    # <job>.sqlite cache path. The CLI refuses (exit 2) before backup() runs.
    _set_common_env(monkeypatch)

    def fake_backup(job, **kwargs):
        raise AssertionError("backup() must not run for an invalid job name")

    monkeypatch.setattr(vfiles, "backup", fake_backup)

    with pytest.raises(SystemExit) as exc:
        vfiles._main(["backup", bad])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["../evil", "..", "a/b"])
def test_restore_rejects_invalid_job_name(monkeypatch, bad):
    _set_common_env(monkeypatch)

    def fake_restore(job, **kwargs):
        raise AssertionError("restore() must not run for an invalid job name")

    monkeypatch.setattr(vfiles, "restore", fake_restore)

    with pytest.raises(SystemExit) as exc:
        vfiles._main(["restore", bad, "list"])
    assert exc.value.code == 2


def test_restore_list_passes_path_none(monkeypatch):
    _set_common_env(monkeypatch)
    captured = {}

    def fake_restore(job, **kwargs):
        captured["job"] = job
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(vfiles, "restore", fake_restore)

    rc = vfiles._main(["restore", "vf", "list"])

    assert rc == 0
    assert captured["job"]["name"] == "vf"
    assert captured["kwargs"]["path"] is None
    assert captured["kwargs"]["target"] == ""
    assert captured["kwargs"]["cache_dir"] == "/cache"
    assert captured["kwargs"]["bucket"] == "my-bucket"
    assert captured["kwargs"]["rclone_config"] == "/cache/rclone.conf"


def test_restore_path_target_passes_asof_and_tier(monkeypatch):
    _set_common_env(monkeypatch)
    captured = {}

    def fake_restore(job, **kwargs):
        captured["job"] = job
        captured["kwargs"] = kwargs
        return {"status": "restored", "path": "a/b.txt",
                "key": "media/vf/a/b.txt@1-xyz", "target": "/out/a/b.txt"}

    monkeypatch.setattr(vfiles, "restore", fake_restore)

    rc = vfiles._main(["restore", "vf", "a/b.txt", "/out",
                        "--asof", "1700000000", "--tier", "Expedited"])

    assert rc == 0
    assert captured["kwargs"]["path"] == "a/b.txt"
    assert captured["kwargs"]["target"] == "/out"
    assert captured["kwargs"]["asof"] == 1700000000.0
    assert captured["kwargs"]["thaw"] == "Expedited"
    assert captured["kwargs"]["cache_dir"] == "/cache"
    assert captured["kwargs"]["bucket"] == "my-bucket"
    assert captured["kwargs"]["rclone_config"] == "/cache/rclone.conf"


def test_restore_default_tier_is_bulk(monkeypatch):
    _set_common_env(monkeypatch)
    captured = {}

    def fake_restore(job, **kwargs):
        captured["kwargs"] = kwargs
        return {"status": "restored", "path": "a.txt", "key": "k", "target": "/out/a.txt"}

    monkeypatch.setattr(vfiles, "restore", fake_restore)

    vfiles._main(["restore", "vf", "a.txt", "/out"])

    assert captured["kwargs"]["thaw"] == "Bulk"
    assert captured["kwargs"]["asof"] is None


def test_restore_lookup_error_exits_1(monkeypatch):
    _set_common_env(monkeypatch)

    def fake_restore(job, **kwargs):
        raise LookupError("no version of 'x' found")

    monkeypatch.setattr(vfiles, "restore", fake_restore)

    rc = vfiles._main(["restore", "vf", "x", "/out"])
    assert rc == 1


def test_restore_missing_target_exits_2(monkeypatch):
    _set_common_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        vfiles._main(["restore", "vf", "a/b.txt"])
    assert exc.value.code == 2


def test_restore_list_with_extra_target_exits_2(monkeypatch):
    _set_common_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        vfiles._main(["restore", "vf", "list", "/out"])
    assert exc.value.code == 2
