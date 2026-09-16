"""Get data back — restore / thaw / download / test-restore from the GUI
(spec 5.2 band, 5.4 confirmation page, 8.9/8.10 contracts, ruling R11). Task 11.

The safety spine under test:
  * the job page's band is ONE `<form method="get">` that only NAVIGATES to the
    confirmation page — it starts nothing and carries no `confirm` field;
  * the confirmation page (`GET /jobs/<name>/restore`) renders the consequence and
    the typed-name confirm, and launches nothing;
  * work starts ONLY on the confirmation page's single POST, which requires the
    user to have TYPED the job name (`confirm`), verified server-side;
  * the target is confined to RESTORE_ROOT and may never be the source it protects;
  * the small POST siblings (`restore-points/refresh`, `thaw/check`, `test-restore`)
    refresh a cache or poll a warm-up — they never start a restore.

`ops.launch` / `ops.run_sync` are monkeypatched so nothing shells out; RESTORE_ROOT
is a real tmp dir so the mount check passes.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.engine import runs
from app.gui import config_io, create_app, jobs_io, ops

UTC = timezone.utc
RESTORE_HOST = "/mnt/user/restore"


# --- seeding helpers --------------------------------------------------------

def _end(cache, name, rid, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": "scheduled", "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    (root / "appdata").mkdir(parents=True)
    (root / "media" / "manga").mkdir(parents=True)
    return root


@pytest.fixture
def restore_root(tmp_path):
    r = tmp_path / "restore"
    r.mkdir()
    return r


@pytest.fixture
def dirs(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True); (cache / "logs").mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek",
                                       "RESTIC_PASSWORD": "a-real-long-random-passphrase"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    return {"config": str(cfg), "cache": str(cache)}


@pytest.fixture
def template_path():
    return str(Path(__file__).resolve().parents[2] / "config" / "backup.env.example")


@pytest.fixture
def app(dirs, source_root, restore_root, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": str(source_root), "SOURCE_ROOT_HOST": "/mnt/user",
                       "RESTORE_ROOT": str(restore_root), "RESTORE_ROOT_HOST": RESTORE_HOST,
                       "SECRET_KEY": "test", "TESTING": True, "PRICES_LIVE": False})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def example(app, source_root):
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
    _end(cache, "appdata", "20260915T050001Z-0000", outcome="ok",
         started="2026-09-15T05:00:01Z", finished="2026-09-15T05:04:13Z", duration=252)
    _end(cache, "manga", "20260906T040000Z-0a0a", outcome="ok",
         started="2026-09-06T04:00:00Z", finished="2026-09-06T06:09:00Z", duration=7740)
    # restore-point cache for appdata (versioned snapshots)
    (Path(cache) / "state" / "appdata.points.json").write_text(json.dumps([
        {"short_id": "a81f3c2e", "time": "2026-09-15T05:00:00Z",
         "summary": {"total_bytes_processed": 56594862080, "total_files_processed": 533,
                     "files_new": 0, "files_changed": 6, "data_added": 228589824}},
        {"short_id": "4b02fa71", "time": "2026-03-18T05:00:00Z",
         "summary": {"total_bytes_processed": 40900000000, "total_files_processed": 500,
                     "files_changed": 0, "data_added": 40900000000}},
    ]))
    # measured sizes so the confirm page can quote a size + a stubbed restore price
    (Path(cache) / "usage.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "data": {"appdata": {"bytes": 56594862080, "count": 533},
                 "media/manga": {"bytes": 1957000000000, "count": 232021}}}))
    return app


@pytest.fixture
def launched(monkeypatch):
    """Capture ops.launch calls without spawning anything."""
    calls = []

    def fake(cfg, argv, *, job, kind, trigger="manual", env_extra=None):
        rid = runs.new_run_id()
        calls.append({"argv": list(argv), "job": job, "kind": kind, "id": rid})
        return rid

    monkeypatch.setattr(ops, "launch", fake)
    return calls


def _csrf(client, path="/jobs/appdata"):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]


def _container(app, job_name, host_target):
    job = jobs_io.get(app.config["CONFIG_DIR"], job_name)
    r = ops.validate_target(app.config, job, host_target)
    assert r["ok"], r
    return r["container_path"]


APPDATA_TARGET = f"{RESTORE_HOST}/appdata/2026-09-16"
MANGA_TARGET = f"{RESTORE_HOST}/manga/2026-09-16"


# --- config wiring (Task 6 carry-forward) -----------------------------------

def test_create_app_exposes_restore_root_config(app):
    assert app.config["RESTORE_ROOT"]
    assert app.config["RESTORE_ROOT_HOST"] == RESTORE_HOST


# --- the band never starts anything (5.2) -----------------------------------

def test_band_is_a_get_form_that_only_navigates(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert 'action="/jobs/appdata/restore"' in body
    assert 'method="get"' in body
    # no typed-name confirm on the job page (5.2): that lives on the confirm page
    assert 'name="confirm"' not in body
    assert 'id="confirm-name"' not in body
    # the chooser and target the GET form carries
    assert 'id="restore-target"' in body
    assert 'name="target"' in body


def test_band_carries_restore_point_radios(client, example):
    body = client.get("/jobs/appdata").get_data(as_text=True)
    assert 'name="point"' in body
    assert "a81f3c2e" in body                       # a real restore point in the list


def test_cold_band_offers_warm_up_first(client, example):
    body = client.get("/jobs/manga").get_data(as_text=True)
    # Plain copy on a thaw-first tier → warm-up first, a GET nav (intent=thaw)
    assert "Warm up first" in body
    assert 'value="thaw"' in body


def test_cold_band_shows_check_now_while_warming(client, example):
    # A pending warm-up (thaw.json, not yet ready) shows the waiting state and the
    # real POST `Check now` (thaw/check) — which starts and costs nothing.
    cache = client.application.config["CACHE_DIR"]
    Path(cache, "state", "manga.thaw.json").write_text(json.dumps(
        {"tier": "Standard", "scope": ".", "requested_at": "2026-09-16T09:00:00Z",
         "expected_ready_by": "2026-09-16T21:00:00Z", "objects_requested": 20,
         "last_check": {"sampled": 20, "ready": 3, "pending": 17}}))
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert "warming up" in body.lower()
    assert 'action="/jobs/manga/thaw/check"' in body


def test_cold_band_flips_to_download_when_ready(client, example):
    # Warm-up ready (last_check ready == sampled) → the same band offers Download now.
    cache = client.application.config["CACHE_DIR"]
    Path(cache, "state", "manga.thaw.json").write_text(json.dumps(
        {"tier": "Standard", "scope": ".", "requested_at": "2026-09-16T09:00:00Z",
         "expected_ready_by": "2026-09-16T21:00:00Z", "objects_requested": 20,
         "last_check": {"sampled": 20, "ready": 20, "pending": 0}}))
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert "Download now" in body
    assert 'value="download"' in body


# --- the confirmation page (GET) launches NOTHING (5.4) ---------------------

def test_get_confirm_renders_typed_name_gate_and_launches_nothing(client, example, launched):
    r = client.get("/jobs/appdata/restore?intent=restore&point=a81f3c2e"
                   f"&target={APPDATA_TARGET}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="confirm-name"' in body             # the ONLY screen with the typed confirm
    assert 'id="start-restore"' in body
    assert "Type" in body and "appdata" in body
    assert launched == []                          # a GET starts nothing


def test_get_confirm_junk_values_fall_back_to_defaults(client, example):
    # ill-formed tier / scope render 200 with defaults in place, never an error page
    r = client.get("/jobs/appdata/restore?intent=restore&point=a81f3c2e"
                   "&tier=Nonsense&scope=../etc")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "get data back" in body.lower()


def test_get_confirm_unknown_job_is_404(client, example):
    assert client.get("/jobs/ghost/restore").status_code == 404


def test_get_confirm_default_target_is_under_restore_root(client, example):
    body = client.get("/jobs/appdata/restore?intent=restore&point=a81f3c2e").get_data(as_text=True)
    assert f"{RESTORE_HOST}/appdata/" in body      # the dated default target


def test_get_confirm_cold_shows_both_speeds(client, example):
    body = client.get("/jobs/manga/restore?intent=thaw").get_data(as_text=True)
    assert "Standard" in body and "Bulk" in body   # both retrieval speeds priced
    assert 'name="tier"' in body


# --- impossible intents render a BLOCKER, no primary button (5.4) ------------

def test_thaw_on_warm_tier_renders_blocker_on_get(client, example):
    r = client.get("/jobs/appdata/restore?intent=thaw")   # appdata is STANDARD (instant)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="start-restore"' not in body                # no primary button
    assert "instant tier" in body.lower() or "nothing to warm up" in body.lower()


# --- the POST is the only thing that starts work (5.4 / 8.10) ---------------

def test_post_restore_needs_typed_name(client, example, launched):
    t = _csrf(client)
    r = client.post("/jobs/appdata/restore",
                    data={"csrf": t, "intent": "restore", "point": "a81f3c2e",
                          "target": APPDATA_TARGET, "confirm": "wrong"})
    assert r.status_code == 400
    body = r.get_data(as_text=True)
    assert "Type the job name exactly as shown to start." in body
    assert "a81f3c2e" in body                       # values intact on re-render
    assert launched == []                           # nothing started


def test_post_restore_happy_path_launches_and_302s_to_run_record(client, example, launched):
    t = _csrf(client)
    r = client.post("/jobs/appdata/restore",
                    data={"csrf": t, "intent": "restore", "point": "a81f3c2e",
                          "target": APPDATA_TARGET, "confirm": "appdata"})
    assert r.status_code == 302
    assert len(launched) == 1
    call = launched[0]
    assert call["kind"] == "restore"
    assert call["job"] == "appdata"
    cont = _container(client.application, "appdata", APPDATA_TARGET)
    assert call["argv"][0].endswith("restore.sh")
    assert call["argv"][1:4] == ["appdata", "restore", "a81f3c2e"]
    assert call["argv"][4] == cont
    assert r.headers["Location"].endswith(f"/jobs/appdata/runs/{call['id']}")


def test_post_download_argv_for_plain_copy(client, example, launched):
    t = _csrf(client)
    r = client.post("/jobs/manga/restore",
                    data={"csrf": t, "intent": "download", "scope": ".",
                          "target": MANGA_TARGET, "confirm": "manga"})
    assert r.status_code == 302
    call = launched[0]
    assert call["kind"] == "download"
    cont = _container(client.application, "manga", MANGA_TARGET)
    assert call["argv"][1:] == ["manga", "download", ".", cont]


def test_post_thaw_argv_for_cold_plain_copy(client, example, launched):
    t = _csrf(client)
    r = client.post("/jobs/manga/thaw",
                    data={"csrf": t, "scope": ".", "tier": "Standard", "confirm": "manga"})
    assert r.status_code == 302
    call = launched[0]
    assert call["kind"] == "thaw"
    assert call["argv"][1:] == ["manga", "thaw", ".", "--tier", "Standard"]


def test_post_thaw_on_warm_tier_is_400(client, example, launched):
    t = _csrf(client)
    r = client.post("/jobs/appdata/thaw",
                    data={"csrf": t, "scope": ".", "tier": "Standard", "confirm": "appdata"})
    assert r.status_code == 400
    assert launched == []


# --- target confinement: never the source, always under RESTORE_ROOT --------

def test_post_target_under_source_is_refused(client, example, launched):
    t = _csrf(client)
    under_source = f"/mnt/user/{jobs_io.get(client.application.config['CONFIG_DIR'], 'appdata')['source']}"
    r = client.post("/jobs/appdata/restore",
                    data={"csrf": t, "intent": "restore", "point": "a81f3c2e",
                          "target": under_source, "confirm": "appdata"})
    assert r.status_code == 400
    assert "a restore can never overwrite what it is protecting" in r.get_data(as_text=True)
    assert launched == []


def test_post_target_outside_restore_root_is_refused(client, example, launched):
    t = _csrf(client)
    r = client.post("/jobs/appdata/restore",
                    data={"csrf": t, "intent": "restore", "point": "a81f3c2e",
                          "target": "/some/other/place", "confirm": "appdata"})
    assert r.status_code == 400
    assert f"must be inside {RESTORE_HOST}" in r.get_data(as_text=True)
    assert launched == []


def test_post_missing_mount_is_400_with_mount_copy(client, example, launched, tmp_path):
    # A restore mount that is not present → the whole thing is blocked.
    client.application.config["RESTORE_ROOT"] = str(tmp_path / "nope")
    t = _csrf(client)
    r = client.post("/jobs/appdata/restore",
                    data={"csrf": t, "intent": "restore", "point": "a81f3c2e",
                          "target": APPDATA_TARGET, "confirm": "appdata"})
    assert r.status_code == 400
    assert "Nowhere to put restored files yet" in r.get_data(as_text=True)
    assert launched == []


def test_post_lock_held_is_409(client, example, launched, monkeypatch):
    monkeypatch.setattr(ops, "ensure_free", lambda cfg, job: (_ for _ in ()).throw(ops.OpsLocked("busy")))
    t = _csrf(client)
    r = client.post("/jobs/appdata/restore",
                    data={"csrf": t, "intent": "restore", "point": "a81f3c2e",
                          "target": APPDATA_TARGET, "confirm": "appdata"})
    assert r.status_code == 409
    assert launched == []


def test_post_restore_requires_csrf(client, example, launched):
    r = client.post("/jobs/appdata/restore",
                    data={"intent": "restore", "point": "a81f3c2e",
                          "target": APPDATA_TARGET, "confirm": "appdata"})
    assert r.status_code == 400
    assert launched == []


# --- the small POST siblings start / cost nothing ---------------------------

def test_restore_points_refresh_is_post_and_starts_no_restore(client, example, launched, monkeypatch):
    seen = {}
    monkeypatch.setattr("app.gui.points.refresh",
                        lambda cfg, job, type, **kw: seen.update(job=job, type=type) or True)
    t = _csrf(client)
    r = client.post("/jobs/appdata/restore-points/refresh", data={"csrf": t})
    assert r.status_code in (302, 303)
    assert seen == {"job": "appdata", "type": "versioned"}
    assert launched == []                            # a cache refresh, not a restore


def test_thaw_check_reads_thaw_json_and_merges_last_check(client, example, launched, monkeypatch):
    cache = client.application.config["CACHE_DIR"]
    thaw = Path(cache, "state", "manga.thaw.json")
    thaw.write_text(json.dumps({"requested_at": "2026-09-16T09:00:00Z", "tier": "Standard",
                                "scope": ".", "expected_ready_by": "2026-09-16T21:00:00Z",
                                "objects_requested": 20}))
    calls = {}

    def fake_run_sync(cfg, argv, *, timeout):
        calls["argv"] = list(argv)

        class CP:
            returncode = 0
            stdout = '{"sampled":20,"ready":20,"pending":0,"not_requested":0}'
            stderr = ""
        return CP()

    monkeypatch.setattr(ops, "run_sync", fake_run_sync)
    t = _csrf(client, "/jobs/manga")
    r = client.post("/jobs/manga/thaw/check", data={"csrf": t})
    assert r.status_code in (302, 303)
    assert calls["argv"][1:3] == ["manga", "thaw-status"]
    assert launched == []


def test_thaw_check_without_a_pending_warmup_is_400(client, example, monkeypatch):
    # only a cold-test warm-up present — the two files are NOT interchangeable (dec. 54)
    cache = client.application.config["CACHE_DIR"]
    Path(cache, "state", "manga.test-thaw.json").write_text(
        json.dumps({"key": "media/manga/x", "expected_ready_by": "2026-09-18T04:00:00Z"}))
    called = {"n": 0}
    monkeypatch.setattr(ops, "run_sync", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    t = _csrf(client, "/jobs/manga")
    r = client.post("/jobs/manga/thaw/check", data={"csrf": t})
    assert r.status_code == 400
    assert called["n"] == 0


# --- test restore (R11) -----------------------------------------------------

def test_test_restore_first_press_launches_and_302s(client, example, launched):
    t = _csrf(client, "/jobs/manga")
    r = client.post("/jobs/manga/test-restore", data={"csrf": t})
    assert r.status_code == 302
    call = launched[0]
    assert call["kind"] == "test-restore"
    assert call["argv"][1:] == ["manga", "test"]
    assert r.headers["Location"].endswith(f"/jobs/manga/runs/{call['id']}")


def test_cold_test_restore_check_now_still_warming(client, example, launched, monkeypatch):
    cache = client.application.config["CACHE_DIR"]
    Path(cache, "state", "manga.test-thaw.json").write_text(json.dumps(
        {"key": "media/manga/x.bin", "requested_at": "2026-09-16T09:00:00Z",
         "expected_ready_by": "2026-09-18T04:00:00Z"}))

    def fake_run_sync(cfg, argv, *, timeout):        # the stub script: still cold
        class CP:
            returncode = 0
            stdout = ""
            stderr = ""
        return CP()                                  # leaves test-thaw.json in place

    monkeypatch.setattr(ops, "run_sync", fake_run_sync)
    t = _csrf(client, "/jobs/manga")
    r = client.post("/jobs/manga/test-restore", data={"csrf": t}, follow_redirects=True)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Still warming up" in body                # the note flash
    assert "Warming up" in body                      # the rail row unchanged
    assert launched == []                            # a re-check launches nothing


def test_cold_test_restore_check_now_ready_writes_tested(client, example, launched, monkeypatch):
    cache = client.application.config["CACHE_DIR"]
    tt = Path(cache, "state", "manga.test-thaw.json")
    tt.write_text(json.dumps({"key": "media/manga/x.bin",
                              "expected_ready_by": "2026-09-16T04:00:00Z"}))

    def fake_run_sync(cfg, argv, *, timeout):        # the stub script: ready → downloaded
        Path(cache, "state", "manga.tested.json").write_text(json.dumps(
            {"at": "2026-09-16T09:12:00Z", "path": "media/manga/x.bin", "bytes": 4096}))
        tt.unlink()

        class CP:
            returncode = 0
            stdout = ""
            stderr = ""
        return CP()

    monkeypatch.setattr(ops, "run_sync", fake_run_sync)
    t = _csrf(client, "/jobs/manga")
    r = client.post("/jobs/manga/test-restore", data={"csrf": t}, follow_redirects=True)
    assert r.status_code == 200
    assert Path(cache, "state", "manga.tested.json").exists()
    body = r.get_data(as_text=True)
    assert "Tested" in body                          # rail flipped to Tested
    assert "x.bin" in body                           # the kept path/basename
    assert launched == []
