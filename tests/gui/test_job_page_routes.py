"""Job page — `/jobs/<name>` (spec 5.2, 8.2) + pause/resume/delete (Task 9).

One page per job: identity/health header, the status strip, the `Last 30 runs`
ledger, the Get-data-back needs-line (the restore FORM itself is Task 11 — stubbed
here), the cache-only `What this job costs` band (Ruling R-I; the real
`job_cost_band` is Task 12), the recovery rail, and the `···` pause/resume/delete
controls. The spec example: `appdata` (Snapshot backup) is OK; `manga` (Plain
copy, DEEP_ARCHIVE) failed on Sunday 13 Sep with the IAM prune-permission blocker.
`now` is pinned so the derived states are deterministic regardless of the clock.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.estimator import billing
from app.gui import config_io, create_app, jobs_io
import app.gui.routes as routes

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 7, 42, tzinfo=UTC)   # matches the mockup clock


# --- seeding helpers --------------------------------------------------------

def _end(cache, name, rid, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": "scheduled", "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _seed_30_ok(cache, name):
    # Thirty nightly OK backups ending 15 Sep 05:04 (17 Aug → 15 Sep, mockup range).
    start = datetime(2026, 8, 17, 5, 0, 1, tzinfo=UTC)
    for i in range(30):
        d = start + timedelta(days=i)
        fin = d + timedelta(seconds=252)
        rid = f"{d:%Y%m%dT%H%M%S}Z-{i:04d}"
        # one clearly-tall run (Fri 28 Aug) so the squared bar scale has a peak
        dur = 532 if d.date() == datetime(2026, 8, 28).date() else 252
        _end(cache, name, rid, outcome="ok",
             started=f"{d:%Y-%m-%dT%H:%M:%S}Z", finished=f"{fin:%Y-%m-%dT%H:%M:%S}Z",
             duration=dur)


# --- fixtures ---------------------------------------------------------------

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
    restore = tmp_path / "restore"; restore.mkdir()   # a present restore mount (Task 11)
    # Provisioned: runtime key + bucket, and a REAL recovery passphrase (not the
    # shipped example) so the passphrase needs-line / rail row read green.
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
    # manga: an OK Sunday (6 Sep) then a FAILED Sunday (13 Sep). A prune failure
    # (7.1.6): copied:true, phase:"prune", the IAM DeleteObjectVersion error.
    _end(cache, "manga", "20260906T040000Z-0a0a", outcome="ok",
         started="2026-09-06T04:00:00Z", finished="2026-09-06T06:09:00Z", duration=7740)
    _end(cache, "manga", "20260913T040000Z-b21c", outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion", phase="prune", copied=True,
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    # restore-point cache for appdata (a few snapshots -> rail cover + count).
    (Path(cache) / "state" / "appdata.points.json").write_text(json.dumps([
        {"short_id": "a81f3c2e", "time": "2026-09-15T05:00:00Z",
         "summary": {"total_bytes_processed": 56594862080, "total_files_processed": 533,
                     "files_new": 0, "files_changed": 6, "data_added": 228589824}},
        {"short_id": "7c41e9b0", "time": "2026-09-14T05:00:00Z",
         "summary": {"total_bytes_processed": 56562000000, "total_files_processed": 533,
                     "files_changed": 4, "data_added": 100663296}},
        {"short_id": "4b02fa71", "time": "2026-03-18T05:00:00Z",
         "summary": {"total_bytes_processed": 40900000000, "total_files_processed": 500,
                     "files_changed": 0, "data_added": 40900000000}},
    ]))
    # usage + billing caches -> the cost band's measured size + invoice/delta.
    (Path(cache) / "usage.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "data": {"appdata": {"bytes": 56594862080, "count": 533},
                 "media/manga": {"bytes": 1957000000000, "count": 232021}}}))
    (Path(cache) / "billing.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "months": [{"month": "2026-08", "amount": 3.98}], "forecast": None, "tag": None}))

    # Pin `now` so status.job()'s state derivation is deterministic.
    real_job = routes.status.job

    def _pinned(config_dir, cache_dir, scripts_dir, name, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("tz", UTC)
        return real_job(config_dir, cache_dir, scripts_dir, name, **kw)

    monkeypatch.setattr(routes.status, "job", _pinned)
    return app


@pytest.fixture
def no_cost_explorer(monkeypatch):
    """Spy: the cost band must render from caches only — no live Cost Explorer."""
    called = {"ce": False}

    def _boom(*a, **k):
        called["ce"] = True
        raise AssertionError("Cost Explorer must not be called during a job-page render")

    monkeypatch.setattr(billing, "monthly_costs", _boom)
    monkeypatch.setattr(billing, "forecast", _boom)
    return called


def _csrf(client, path="/"):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]


# --- OK job (appdata): the whole page renders -------------------------------

def test_ok_job_renders_identity_and_ok_token(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert client.get("/jobs/appdata").status_code == 200
    # state token + name + type line (4.3) + the mono identity line
    assert 'class="tok tok-ok"' in body
    assert "<h1>appdata</h1>" in body
    assert "Snapshot backup — a point in time, so you can restore any date." in body
    assert "s3://bw-backups/appdata/" in body


def test_ok_job_renders_status_strip(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    for k in ("Last run", "Took", "Next run", "Streak", "Restore points"):
        assert k in body


def test_ok_job_renders_30_run_ledger_with_bars(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert "Last 30 runs" in body
    assert "30 OK · 0 failed" in body
    assert body.count('class="cellx') == 30           # exactly 30 ledger cells
    assert 'class="bar' in body                        # decorative duration bars


def test_ok_job_renders_needs_line(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert "Get data back" in body
    assert "Recovery passphrase:" in body              # versioned needs-line row 1
    assert "Ready now:" in body                        # instant STANDARD tier row 2
    # restore COST is not priced until estimate_io.restore_quote (Task 12)
    assert "not priced yet" in body


def test_ok_job_restore_band_is_a_get_form_without_confirm(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    # The band never starts anything: a GET to the confirmation page, no `confirm`.
    assert 'action="/jobs/appdata/restore"' in body
    assert 'method="get"' in body
    assert 'name="confirm"' not in body


def test_ok_job_renders_cost_band_cache_only(client, example, no_cost_explorer):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert "What this job costs" in body
    assert "In the bucket now" in body
    assert "52.71 GB" in body                          # measured size from usage cache
    assert no_cost_explorer["ce"] is False             # no CE during render


def test_ok_job_has_edit_link_with_locked_fields(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert "How it is set up" in body
    assert '/jobs/appdata/edit' in body                # the edit link (Task 14)
    assert "Set at creation" in body                   # locked-field indication (5.9)


def test_ok_job_tool_detail_is_where_tool_terms_live(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert "Tool detail" in body
    # tool vocabulary is demoted to the expander (mono law, 4.4)
    assert "restic" in body


def test_ok_job_recovery_rail(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert "Recovery readiness" in body
    assert "Destination reachable" in body
    assert "Restore ever tested" in body


# --- FAILED job (manga): the failure record --------------------------------

def test_failed_job_renders_failure_record_and_fix(client, example):
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert 'class="tok tok-failed"' in body
    assert "sig-failure" in body
    assert "AccessDenied: s3:DeleteObjectVersion" in body       # the verbatim errline
    assert "Fix the permission →" in body                       # from errors.classify
    assert "/setup/destination" in body                         # the fix route
    # the prune case adds its first line to the cause (7.1.6 / 5.2)
    assert "the clean-up of old versions was refused" in body


def test_failed_job_is_a_plain_copy_with_what_is_there_now(client, example):
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert "Plain copy — a straight copy" in body
    assert "What is there now" in body                          # archive band (5.2)
    assert "Copies" in body                                     # strip figure label


# --- pause / resume ---------------------------------------------------------

def test_pause_flips_token_and_clears_next_run(client, example):
    t = _csrf(client, "/jobs/appdata")
    r = client.post("/jobs/appdata/pause", data={"csrf": t})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/jobs/appdata")
    jobs = json.loads(Path(client.application.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert next(j for j in jobs if j["name"] == "appdata")["enabled"] is False
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert 'class="tok tok-paused"' in body
    assert "paused" in body                                     # Next run — · paused


def test_pause_rerenders_crontab_so_not_stale(client, example):
    t = _csrf(client, "/jobs/appdata")
    client.post("/jobs/appdata/pause", data={"csrf": t})
    # After the toggle the on-disk crontab matches the jobs -> not stale (carry-in).
    assert routes.status.crontab_stale(
        client.application.config["CONFIG_DIR"], client.application.config["CACHE_DIR"],
        client.application.config["SCRIPTS_DIR"],
        source_root=client.application.config["SOURCE_ROOT"]) is False


def test_resume_restores_the_job(client, example):
    t = _csrf(client, "/jobs/appdata")
    client.post("/jobs/appdata/pause", data={"csrf": t})
    client.post("/jobs/appdata/resume", data={"csrf": t})
    jobs = json.loads(Path(client.application.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert next(j for j in jobs if j["name"] == "appdata")["enabled"] is True
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert 'class="tok tok-ok"' in body


def test_pause_resume_require_csrf(client, example):
    assert client.post("/jobs/appdata/pause", data={}).status_code == 400
    assert client.post("/jobs/appdata/resume", data={}).status_code == 400


def test_pause_unknown_job_is_404(client, example):
    t = _csrf(client, "/jobs/appdata")
    assert client.post("/jobs/nope/pause", data={"csrf": t}).status_code == 404


# --- delete: removes the job AND its caches ---------------------------------

def test_delete_removes_job_and_its_caches(client, example):
    cache = client.application.config["CACHE_DIR"]
    runs_file = Path(cache, "state", "appdata.runs.jsonl")
    points_file = Path(cache, "state", "appdata.points.json")
    assert runs_file.exists() and points_file.exists()          # seeded
    t = _csrf(client, "/jobs/appdata")
    r = client.post("/jobs/appdata/delete", data={"csrf": t})
    assert r.status_code in (301, 302, 303)
    jobs = json.loads(Path(client.application.config["CONFIG_DIR"], "jobs.json").read_text())["jobs"]
    assert "appdata" not in [j["name"] for j in jobs]
    # the carry-in: deleting a job removes its state/<job>.* caches
    assert not runs_file.exists()
    assert not points_file.exists()


# --- unknown job -> themed 404 ----------------------------------------------

def test_unknown_job_is_themed_404(client, example):
    r = client.get("/jobs/ghost")
    assert r.status_code == 404
    assert b"There is no job called ghost" in r.data           # error.html (5.14)
    assert b"ghost" in r.data
