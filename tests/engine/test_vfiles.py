# Tests for the `python3 -m app.engine.vfiles browse` CLI subcommand -- the
# read-only listing entry point that wraps app.engine.catalog.browse (Task 2)
# for the bash runner. See tests/engine/test_vfiles_cli.py for the existing
# backup/restore/thaw dispatch coverage this complements.
import json
import sqlite3

from app.engine import vfiles, catalog


def _set_browse_env(monkeypatch, tmp_path):
    # Only CACHE_DIR/S3_BUCKET are read before the browse dispatch (vfiles.py
    # computes cache_dir/bucket/rclone_config, THEN dispatches browse, all
    # before the JOB_STORAGE_CLASS/retention block) -- a read-only browse must
    # not require those.
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET", "b")


def test_vfiles_browse_json(tmp_path, monkeypatch, capsys):
    cat = tmp_path / "j.sqlite"
    conn = catalog.open_catalog(str(cat))
    conn.execute(
        "INSERT INTO versions(path,key,size,mtime,storage_class,uploaded_at,is_current,deleted)"
        " VALUES('docs/a.txt','k',2,0,'STANDARD','2026-09-01T00:00:00Z',1,0)"
    )
    conn.commit()
    conn.close()

    _set_browse_env(monkeypatch, tmp_path)

    def fake_open_or_fetch_catalog(job, cache_dir, *, bucket, rclone_config, runner):
        ro = sqlite3.connect(f"file:{cat}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        return ro

    monkeypatch.setattr(vfiles, "_open_or_fetch_catalog", fake_open_or_fetch_catalog)

    rc = vfiles._main(["browse", "j", "docs", "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "docs"
    assert out["entries"][0]["name"] == "a.txt"
    assert out["entries"][0]["kind"] == "file"


def test_vfiles_browse_json_root_default_relpath(tmp_path, monkeypatch, capsys):
    cat = tmp_path / "j.sqlite"
    conn = catalog.open_catalog(str(cat))
    conn.execute(
        "INSERT INTO versions(path,key,size,mtime,storage_class,uploaded_at,is_current,deleted)"
        " VALUES('top.txt','k',1,0,'DEEP_ARCHIVE','2026-09-01T00:00:00Z',1,0)"
    )
    conn.commit()
    conn.close()

    _set_browse_env(monkeypatch, tmp_path)

    def fake_open_or_fetch_catalog(job, cache_dir, *, bucket, rclone_config, runner):
        ro = sqlite3.connect(f"file:{cat}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        return ro

    monkeypatch.setattr(vfiles, "_open_or_fetch_catalog", fake_open_or_fetch_catalog)

    rc = vfiles._main(["browse", "j", "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == ""
    assert out["entries"][0] == {
        "name": "top.txt", "kind": "file", "size": 1,
        "storage_class": "DEEP_ARCHIVE", "modified": "2026-09-01T00:00:00Z",
        "versions": [
            {"version_id": "k", "uploaded_at": "2026-09-01T00:00:00Z",
             "storage_class": "DEEP_ARCHIVE", "size": 1, "deleted": False},
        ],
    }


def test_vfiles_browse_text_output(tmp_path, monkeypatch, capsys):
    cat = tmp_path / "j.sqlite"
    conn = catalog.open_catalog(str(cat))
    conn.execute(
        "INSERT INTO versions(path,key,size,mtime,storage_class,uploaded_at,is_current,deleted)"
        " VALUES('top.txt','k',1,0,'STANDARD','2026-09-01T00:00:00Z',1,0)"
    )
    conn.commit()
    conn.close()

    _set_browse_env(monkeypatch, tmp_path)

    def fake_open_or_fetch_catalog(job, cache_dir, *, bucket, rclone_config, runner):
        ro = sqlite3.connect(f"file:{cat}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        return ro

    monkeypatch.setattr(vfiles, "_open_or_fetch_catalog", fake_open_or_fetch_catalog)

    rc = vfiles._main(["browse", "j"])

    assert rc == 0
    out = capsys.readouterr().out
    assert out == "file\ttop.txt\tSTANDARD\n"


def test_vfiles_browse_rejects_invalid_job_name(tmp_path, monkeypatch):
    _set_browse_env(monkeypatch, tmp_path)

    def fail_open(*a, **k):
        raise AssertionError("_open_or_fetch_catalog must not run for an invalid job name")

    monkeypatch.setattr(vfiles, "_open_or_fetch_catalog", fail_open)

    import pytest
    with pytest.raises(SystemExit) as exc:
        vfiles._main(["browse", "../evil"])
    assert exc.value.code == 2


def test_vfiles_browse_does_not_require_job_storage_class(tmp_path, monkeypatch, capsys):
    # Defense against a regression that moves the browse dispatch below the
    # retention/JOB_STORAGE_CLASS block: browse is read-only and must not
    # require env vars that only backup/restore/thaw need.
    monkeypatch.delenv("JOB_STORAGE_CLASS", raising=False)
    monkeypatch.delenv("JOB_RETENTION_TYPE", raising=False)
    monkeypatch.delenv("JOB_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("JOB_RETENTION_COUNT", raising=False)
    _set_browse_env(monkeypatch, tmp_path)

    cat = tmp_path / "j.sqlite"
    conn = catalog.open_catalog(str(cat))
    conn.commit()
    conn.close()

    def fake_open_or_fetch_catalog(job, cache_dir, *, bucket, rclone_config, runner):
        ro = sqlite3.connect(f"file:{cat}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        return ro

    monkeypatch.setattr(vfiles, "_open_or_fetch_catalog", fake_open_or_fetch_catalog)

    rc = vfiles._main(["browse", "j", "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "" and out["entries"] == []
