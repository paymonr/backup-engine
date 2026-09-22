"""Explore — `/explore`, `/explore/<job>`, `/explore/<job>/list.json` (Task 6).

The read-only Explore surface: a job list, a per-job browser page that
server-renders its initial directory level (the no-JS fallback), and a
list.json endpoint the client polls for in-place navigation (Task 9). Every
route here is read-only — a listing failure degrades to an inline error, never
a 500 — so these tests monkeypatch `routes._browse_level` rather than shelling
out to a real (nonexistent, in tests) `restore.sh`.

Fixture block copied from tests/gui/test_job_page_routes.py (the shared
conftest only provides `template_path` + a minimal `dirs`); `example` seeds an
`appdata` versioned job + a `manga` archive job.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.gui import config_io, create_app, jobs_io
import app.gui.routes as routes

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 7, 42, tzinfo=UTC)


# --- seeding helpers ---------------------------------------------------------

def _end(cache, name, rid, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": "scheduled", "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _seed_30_ok(cache, name):
    start = datetime(2026, 8, 17, 5, 0, 1, tzinfo=UTC)
    for i in range(30):
        d = start + timedelta(days=i)
        fin = d + timedelta(seconds=252)
        rid = f"{d:%Y%m%dT%H%M%S}Z-{i:04d}"
        _end(cache, name, rid, outcome="ok",
             started=f"{d:%Y-%m-%dT%H:%M:%S}Z", finished=f"{fin:%Y-%m-%dT%H:%M:%S}Z", duration=252)


# --- fixtures (copied from tests/gui/test_job_page_routes.py) --------------

@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    (root / "appdata").mkdir(parents=True)
    (root / "media" / "manga").mkdir(parents=True)
    return root


@pytest.fixture
def dirs(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True); (cache / "logs").mkdir()
    restore = tmp_path / "restore"; restore.mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek",
                                       "RESTIC_PASSWORD": "a-real-long-random-passphrase"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    return {"config": str(cfg), "cache": str(cache), "restore": str(restore)}


@pytest.fixture
def app(dirs, source_root, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "SOURCE_ROOT_HOST": "/mnt/user",
                       "RESTORE_ROOT": dirs["restore"], "RESTORE_ROOT_HOST": "/mnt/user/restore",
                       "SECRET_KEY": "test", "TESTING": True, "PRICES_LIVE": False})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def example(app, source_root, monkeypatch):
    cfg, cache = app.config["CONFIG_DIR"], app.config["CACHE_DIR"]
    jobs_io.upsert(cfg, {"name": "appdata", "type": "versioned", "source": "appdata",
                         "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                         "created_at": "2026-09-01T00:00:00Z",
                         "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}},
                   source_root=str(source_root))
    jobs_io.upsert(cfg, {"name": "manga", "type": "archive", "source": "media/manga",
                         "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
                         "created_at": "2026-09-01T00:00:00Z"},
                   source_root=str(source_root))
    _seed_30_ok(cache, "appdata")
    _end(cache, "manga", "20260906T040000Z-0a0a", outcome="ok",
         started="2026-09-06T04:00:00Z", finished="2026-09-06T06:09:00Z", duration=7740)
    _end(cache, "manga", "20260913T040000Z-b21c", outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion", phase="prune", copied=True,
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    (Path(cache) / "state" / "appdata.points.json").write_text(json.dumps([
        {"short_id": "a81f3c2e", "time": "2026-09-15T05:00:00Z",
         "summary": {"total_bytes_processed": 56594862080, "total_files_processed": 533,
                     "files_new": 0, "files_changed": 6, "data_added": 228589824}},
        {"short_id": "7c41e9b0", "time": "2026-09-14T05:00:00Z",
         "summary": {"total_bytes_processed": 56562000000, "total_files_processed": 533,
                     "files_changed": 4, "data_added": 100663296}},
    ]))
    (Path(cache) / "usage.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "data": {"appdata": {"bytes": 56594862080, "count": 533},
                 "media/manga": {"bytes": 1957000000000, "count": 232021}}}))
    (Path(cache) / "billing.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "months": [{"month": "2026-08", "amount": 3.98}], "forecast": None, "tag": None}))

    real_job = routes.status.job

    def _pinned(config_dir, cache_dir, scripts_dir, name, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("tz", UTC)
        return real_job(config_dir, cache_dir, scripts_dir, name, **kw)

    monkeypatch.setattr(routes.status, "job", _pinned)
    return app


def _csrf(client, path="/"):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]


# --- /explore: the job list --------------------------------------------------

def test_explore_index_lists_jobs(client, example):
    r = client.get("/explore")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "appdata" in body
    assert "manga" in body


# --- /explore/<job>: the per-job browser (server-rendered no-JS fallback) ----

def test_explore_job_renders(client, example, monkeypatch):
    monkeypatch.setattr(routes, "_browse_level",
        lambda cfg, name, jt, path, snapshot=None: {"path": path, "entries": [
            {"name": "sub", "kind": "dir", "size": None, "storage_class": None, "modified": None},
            {"name": "a.txt", "kind": "file", "size": 1, "storage_class": None, "modified": None}]})
    r = client.get("/explore/appdata")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "appdata" in body
    assert "sub/" in body
    assert "a.txt" in body


def test_explore_job_archive_renders(client, example, monkeypatch):
    # manga is an `archive` job (no snapshot machinery) — the same code path.
    monkeypatch.setattr(routes, "_browse_level",
        lambda cfg, name, jt, path, snapshot=None: {"path": path, "entries": []})
    r = client.get("/explore/manga")
    assert r.status_code == 200
    assert "manga" in r.get_data(as_text=True)


def test_explore_job_shows_inline_error_never_500(client, example, monkeypatch):
    monkeypatch.setattr(routes, "_browse_level",
        lambda cfg, name, jt, path, snapshot=None: {"error": "listing failed"})
    r = client.get("/explore/appdata")
    assert r.status_code == 200
    assert "listing failed" in r.get_data(as_text=True)


# --- unknown job -> 404 ------------------------------------------------------

def test_explore_unknown_job_404(client, example):
    r = client.get("/explore/nope")
    assert r.status_code == 404


# --- /explore/<job>/list.json -------------------------------------------------

def test_explore_list_json_archive(client, example, monkeypatch):
    monkeypatch.setattr(routes, "_browse_level",
        lambda cfg, name, jt, path, snapshot=None: {"path": path, "entries": [
            {"name": "a.txt", "kind": "file", "size": 1, "storage_class": "STANDARD", "modified": None}]})
    out = client.get("/explore/manga/list.json?path=").get_json()
    assert out["entries"][0]["name"] == "a.txt"


def test_explore_list_json_unknown_job_404(client, example):
    r = client.get("/explore/nope/list.json?path=")
    assert r.status_code == 404
    assert r.get_json()["error"]


def test_explore_list_json_needs_no_csrf(client, example, monkeypatch):
    # A plain GET, like /jobs/<name>/progress.json — never 400s for a missing token.
    monkeypatch.setattr(routes, "_browse_level",
        lambda cfg, name, jt, path, snapshot=None: {"path": path, "entries": []})
    assert client.get("/explore/appdata/list.json?path=").status_code == 200
