"""Board — `/` + `/status.json` (spec 5.1, 8.1, 4.6).

The Board is the home screen: a status band on top (verdict, the needs-you lane,
per-job rows with the run strip) and a cost strip below, rendered from
`status.board()` (Task 4) plus a CACHE-ONLY cost object (Ruling R-A: Task 8 stubs
the projected figure from `current_costs`/`billing.json`; Task 12 wires the real
`estimate_io.board_cost`). Rendering must NEVER hit Cost Explorer.

The example content is the spec's: `appdata` (Snapshot backup) is OK, `manga`
(Plain copy) failed on Sunday 13 Sep with the IAM permission blocker. `now` is
pinned so the derived states are deterministic regardless of the wall clock.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.estimator import billing
from app.gui import config_io, create_app, jobs_io
import app.gui.routes as routes

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 7, 42, tzinfo=UTC)   # matches the mockup clock


# --- seeding helpers (adapted from tests/gui/test_status.py) ----------------

def _end(cache, name, rid, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": "scheduled", "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _rid(day=15, hh=5, mm=0, tag="0000"):
    return f"202609{day:02d}T{hh:02d}{mm:02d}00Z-{tag}"


def _seed_ok_backups(cache, name, n, base_day=1):
    for i in range(n):
        day = base_day + i
        _end(cache, name, _rid(day, 5, 0, f"{i:04d}"), outcome="ok",
             started=f"2026-09-{day:02d}T05:00:01Z", finished=f"2026-09-{day:02d}T05:04:00Z",
             duration=240 + i)


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
    # Provisioned: a runtime key + a real bucket, so `/` renders the Board (5.1).
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    return {"config": str(cfg), "cache": str(cache)}


@pytest.fixture
def app(dirs, source_root, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                       "PRICES_LIVE": False})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def example(app, source_root, monkeypatch):
    """Seed the spec example: appdata OK, manga FAILED (IAM blocker), usage +
    billing caches for the cost strip. Pins `now` so states are deterministic."""
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
    # appdata: 14 nightly OK runs, newest 15 Sep 05:04 -> OK, not overdue at NOW.
    _seed_ok_backups(cache, "appdata", 14, base_day=2)
    # manga: an OK Sunday (6 Sep) then a failed Sunday (13 Sep) with the IAM error.
    _end(cache, "manga", _rid(6, 4, 0, "0a0a"), outcome="ok",
         started="2026-09-06T04:00:00Z", finished="2026-09-06T06:09:00Z", duration=7740)
    _end(cache, "manga", _rid(13, 4, 0, "b21c"), outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion",
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    # usage cache -> the cost strip's "In the bucket now" + per-job size (cache-only).
    (Path(cache) / "usage.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "data": {"appdata": {"bytes": 56594862080, "count": 533},
                 "media/manga": {"bytes": 1957000000000, "count": 232021}}}))
    # billing cache -> the "Last invoice" figure (written by the billing-check sysop,
    # 7.7.3); read_billing_cache NEVER calls Cost Explorer.
    (Path(cache) / "billing.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "months": [{"month": "2026-08", "amount": 3.98}], "forecast": None, "tag": None}))

    # Pin `now` so the route's real status.board() derivation is deterministic.
    real_board = routes.status.board

    def _pinned(config_dir, cache_dir, scripts_dir, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("tz", UTC)
        return real_board(config_dir, cache_dir, scripts_dir, **kw)

    monkeypatch.setattr(routes.status, "board", _pinned)
    return app


@pytest.fixture
def no_cost_explorer(monkeypatch):
    """Spy: the Board must render from caches only — no live Cost Explorer call."""
    called = {"ce": False}

    def _boom(*a, **k):
        called["ce"] = True
        raise AssertionError("Cost Explorer must not be called during a Board render")

    monkeypatch.setattr(billing, "monthly_costs", _boom)
    monkeypatch.setattr(billing, "forecast", _boom)
    return called


# --- routing / home ---------------------------------------------------------

def test_root_renders_board_when_provisioned(client, example):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Needs you" in r.data          # band 2 label
    assert b"worst first" in r.data        # band 3 label


def test_root_redirects_to_provision_when_unprovisioned(dirs, source_root, template_path):
    # Wipe the runtime key so the app is no longer provisioned.
    Path(dirs["config"], "secrets.env").write_text("")
    Path(dirs["config"], "backup.env").write_text("")
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    r = app.test_client().get("/")
    assert r.status_code in (301, 302)
    assert "/provision" in r.headers["Location"] or "/setup" in r.headers["Location"]


def test_jobs_redirects_permanently_to_board(client, example):
    r = client.get("/jobs")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/")   # -> the Board at `/`


# --- band 1: verdict --------------------------------------------------------

def test_verdict_is_the_manga_failure(client, example):
    body = client.get("/").get_data(as_text=True)
    assert "manga has not backed up since Sunday" in body        # verdict h2 (5.1)
    assert "Amazon refused a delete" in body                     # error class `verdict`
    assert "Fix the permission" in body                          # the fix button label


# --- band 2: the needs-you lane (the IAM blocker) ---------------------------

def test_needs_you_shows_iam_blocker_row(client, example):
    body = client.get("/").get_data(as_text=True)
    assert "manga cannot finish a run." in body                  # blocker `strong`
    assert "AccessDenied: s3:DeleteObjectVersion" in body        # the errline
    assert "every Sunday run stops at the same point" in body    # `board` template, {dow} filled
    assert "6 September" in body                                  # {since} filled
    assert "/setup/destination" in body                          # fix href


# --- band 3: both jobs, worst first, with the run strip ---------------------

def test_both_jobs_render_worst_first(client, example):
    body = client.get("/").get_data(as_text=True)
    # manga (Failed) sorts above appdata (OK) in the jobs table
    assert body.index("board-tok-manga") < body.index("board-tok-appdata")
    # state tokens
    assert 'class="tok tok-failed"' in body
    assert 'class="tok tok-ok"' in body
    # the run strip: manga's one red cell + appdata's dimmed old OK cells (6.5)
    assert "cellx fail" in body                                  # manga's single failure
    assert "cellx ok dim" in body                                # positional dim, older than 7


def test_versioned_files_and_archive_use_night_shift_names(client, example):
    body = client.get("/").get_data(as_text=True)
    # manga is a Plain copy; appdata a Snapshot backup (4.3 type lines, row hover)
    assert "Plain copy" in body
    assert "Snapshot backup" in body


# --- band 4: the cost strip (cache-only stub, R-A) --------------------------

def test_cost_strip_renders_from_caches(client, example, no_cost_explorer):
    body = client.get("/").get_data(as_text=True)
    assert "In the bucket now" in body
    assert "Last invoice" in body
    assert "The model says" in body
    assert no_cost_explorer["ce"] is False                       # no CE during render


# --- the ticking clock (5.15): the Board is a polling page ------------------

def test_clock_carries_refreshed_segment(client, example):
    body = client.get("/").get_data(as_text=True)
    assert re.search(r"refreshed\s+\d+\s+s\s+ago", body)


# --- /status.json (8.1) -----------------------------------------------------

def test_status_json_matches_contract(client, example, no_cost_explorer):
    r = client.get("/status.json")
    assert r.status_code == 200 and r.mimetype == "application/json"
    js = r.get_json()
    # top-level shape
    for k in ("generated_at", "tz", "next_scheduled", "verdict", "needs_you", "jobs",
              "crontab_stale", "cost"):
        assert k in js, k
    # verdict
    assert js["verdict"]["state"] == "failed" and js["verdict"]["job"] == "manga"
    assert js["verdict"]["button"]["href"] == "/setup/destination"
    # needs-you: the IAM blocker
    blockers = [n for n in js["needs_you"] if n["level"] == "blocker"]
    assert blockers and blockers[0]["code"] == "iam-version-perms"
    assert blockers[0]["errline"] == "AccessDenied: s3:DeleteObjectVersion"
    assert blockers[0]["fix"]["href"] == "/setup/destination"
    # jobs: worst first, each a JobStatus (8.2) with a 14-cell strip
    assert [j["name"] for j in js["jobs"]] == ["manga", "appdata"]
    assert js["jobs"][0]["state"] == "FAILED" and js["jobs"][1]["state"] == "OK"
    assert len(js["jobs"][1]["strip"]) == 14
    assert js["jobs"][1]["ok_14"] == 14 and js["jobs"][0]["failed_14"] == 1
    # cost object (8.1): cache-only figures + provenance
    cost = js["cost"]
    for k in ("in_bucket_bytes", "invoice", "model_monthly", "model_monthly_provenance",
              "per_job", "price"):
        assert k in cost, k
    assert cost["in_bucket_bytes"] == 56594862080 + 1957000000000
    assert cost["invoice"]["month"] == "2026-08" and cost["invoice"]["amount"] == 3.98
    assert cost["model_monthly_provenance"] in ("assumed", "projected")
    assert {p["name"] for p in cost["per_job"]} == {"appdata", "manga"}
    assert no_cost_explorer["ce"] is False


def test_status_json_no_cost_explorer_even_when_billing_cache_absent(client, example,
                                                                     no_cost_explorer):
    # Remove the billing cache: the invoice degrades to null, still no CE call.
    Path(client.application.config["CACHE_DIR"], "billing.json").unlink()
    js = client.get("/status.json").get_json()
    assert js["cost"]["invoice"] is None
    assert no_cost_explorer["ce"] is False
