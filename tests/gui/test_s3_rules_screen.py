# tests/gui/test_s3_rules_screen.py — the S3 rules screen and its actions (spec 2026-09-23 §3, §5).
import html
import json
import re
from pathlib import Path

import pytest
from app.engine import lifecycle, runs, storage_summary
from app.gui import config_io, create_app, jobs_io, ops, s3_rules

BASE = "unraid-backup-123456789012"
JOBS = [
    {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
     "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": 180}},
    {"name": "appdata_backups", "type": "versioned", "source": "appdata", "schedule": "0 5 * * *",
     "enabled": True, "storage_class": "STANDARD",
     "retention": {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}},
]
# Captured before any test patches them (the integrated tests put them back).
REAL = {n: getattr(lifecycle, n) for n in ("role_creds", "read_rules", "write_rules",
                                           "read_versioning", "write_versioning")}


@pytest.fixture
def cfg(tmp_path, template_path):
    conf, cache = tmp_path / "config", tmp_path / "cache"
    conf.mkdir(); (cache / "state").mkdir(parents=True)
    src = tmp_path / "src"; (src / "media" / "manga").mkdir(parents=True); (src / "appdata").mkdir()
    config_io.write_secrets(str(conf), {"AWS_ACCESS_KEY_ID": "AKIARUN", "AWS_SECRET_ACCESS_KEY": "runsek"})
    (conf / "backup.env").write_text(f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\n"
                                     "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::1:role/r\nPERMISSIONS_VERSION=4\n")
    (conf / "jobs.json").write_text(json.dumps({"jobs": JOBS}))
    return {"CONFIG_DIR": str(conf), "CACHE_DIR": str(cache), "SOURCE_ROOT": str(src),
            "TEMPLATE_PATH": template_path}


@pytest.fixture
def client(cfg):
    app = create_app({**cfg, "SCRIPTS_DIR": "/app/scripts", "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    """Nothing here reaches AWS unless a test puts the real engine back on a FakeS3 itself."""
    def boom(*a, **k):
        raise AssertionError("S3 rules screen tests never reach AWS")
    for name in REAL:
        monkeypatch.setattr(lifecycle, name, boom)
    monkeypatch.setattr(ops, "launch_py", lambda *a, **k: pytest.fail("unexpected detached launch"))


def _csrf(client):
    client.get("/setup")
    with client.session_transaction() as s:
        return s["_csrf"]


def _applied(cfg, jobs=JOBS, settings=None, bucket=BASE):
    want = lifecycle.desired(bucket, BASE, jobs, settings or {})
    lifecycle.save_applied(cfg["CACHE_DIR"], bucket, list(want.rules.values()), folders=sorted(want.folders),
                           versioning=want.versioning)


# --- Refresh now (Task 12) ------------------------------------------------------------------

def test_refresh_now_launches_a_detached_scan_of_that_folder(client, monkeypatch):
    seen = []
    monkeypatch.setattr(ops, "launch_py", lambda c, module, args, **kw: seen.append((module, args, kw)) or "rid")
    r = client.post("/setup/storage/refresh", data={"csrf": _csrf(client), "bucket": BASE,
                                                   "folder": "media/manga/", "key": f"{BASE}|media/manga/"})
    assert r.status_code in (302, 303) and "/setup/storage" in r.headers["Location"]
    assert seen == [("app.engine.sysop", ["storage-summary", "--bucket", BASE, "--folder", "media/manga/"],
                     {"kind": "storage-summary"})]


def test_refresh_now_only_scans_an_app_folder(client):
    r = client.post("/setup/storage/refresh", data={"csrf": _csrf(client), "bucket": BASE, "folder": "logs/"})
    assert r.status_code == 404


def test_refresh_now_requires_csrf(client):
    assert client.post("/setup/storage/refresh", data={"bucket": BASE, "folder": "appdata/"}).status_code == 400


def _flashes(client):
    with client.session_transaction() as s:
        return dict(s["_flashes"]) if "_flashes" in s else {}


def test_refresh_now_warns_instead_of_launching_when_s3_rules_arent_managed(client, cfg):
    # fix round 1, Minor 2: below level 4 / no role, Refresh now must not silently do
    # nothing -- the autouse `no_aws` fixture fails this test outright if a launch is
    # attempted (ops.launch_py -> pytest.fail), so a passing test here already proves none was.
    token = _csrf(client)
    Path(cfg["CONFIG_DIR"], "backup.env").write_text(f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\n")
    r = client.post("/setup/storage/refresh", data={"csrf": token, "bucket": BASE, "folder": "media/manga/"})
    assert r.status_code in (302, 303)
    assert "AWS permissions update" in " ".join(_flashes(client).values())


def test_refresh_now_warns_instead_of_launching_for_a_custom_s3_endpoint(client, cfg):
    token = _csrf(client)
    Path(cfg["CONFIG_DIR"], "backup.env").write_text(
        f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\nBUCKET_ADMIN_ROLE_ARN=arn:aws:iam::1:role/r\n"
        "PERMISSIONS_VERSION=4\nS3_ENDPOINT=https://minio.example\n")
    r = client.post("/setup/storage/refresh", data={"csrf": token, "bucket": BASE, "folder": "media/manga/"})
    assert r.status_code in (302, 303)
    assert "doesn't support storage summaries" in " ".join(_flashes(client).values())


def test_refresh_now_scans_a_dedicated_buckets_whole_bucket_folder(client, cfg, monkeypatch):
    dedicated_bucket = "vault-dedicated-987654321098"
    jobs = [*JOBS, {"name": "vault", "type": "versioned", "source": "vault", "schedule": "0 6 * * *",
                    "enabled": True, "storage_class": "STANDARD", "dedicated": True,
                    "bucket": dedicated_bucket, "retention": {"type": "keep_all"}}]
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    seen = []
    monkeypatch.setattr(ops, "launch_py", lambda c, module, args, **kw: seen.append((module, args, kw)) or "rid")
    r = client.post("/setup/storage/refresh", data={"csrf": _csrf(client), "bucket": dedicated_bucket,
                                                   "folder": "", "key": f"{dedicated_bucket}|"})
    assert r.status_code in (302, 303)
    assert seen == [("app.engine.sysop", ["storage-summary", "--bucket", dedicated_bucket, "--folder", ""],
                     {"kind": "storage-summary"})]


def test_activity_labels_storage_summaries(client, cfg):
    runs.record_system(cfg["CACHE_DIR"], kind="storage-summary", summary="storage summary · media/manga/")
    body = client.get("/activity?kind=setup").get_data(as_text=True)
    assert "storage summary" in body


# --- the overview (Task 14) -------------------------------------------------------------------

CONSOLE_RULE = {"ID": "archive-old-logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
                "Expiration": {"Days": 14}}
OVERLAP_RULE = {"ID": "trim-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 60}}


def _full_state(cfg):
    _applied(cfg)
    applied = lifecycle.load_applied(cfg["CACHE_DIR"], BASE)
    lifecycle.save_live(cfg["CACHE_DIR"], BASE, [CONSOLE_RULE, OVERLAP_RULE, *applied])
    lifecycle.set_status(cfg["CACHE_DIR"], BASE, "ok")


def test_below_level_four_the_screen_only_asks_for_the_permissions_update(client, cfg):
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text().replace("PERMISSIONS_VERSION=4", "PERMISSIONS_VERSION=3"))
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "Needs the permissions update" in body and 'href="/setup/permissions"' in body
    assert 'data-s3-state="not-managed"' in body and "data-row=" not in body


def test_the_screen_shows_each_folder_and_what_s3_keeps(client, cfg):
    _full_state(cfg)
    body = client.get("/setup/storage").get_data(as_text=True)
    assert f'data-bucket="{BASE}"' in body and "Shared bucket" in body
    assert f'data-row="{BASE}|media/manga/"' in body and f'data-row="{BASE}|appdata/"' in body
    assert 'Old versions for <span class="mono">180</span> days' in body
    assert 'Undo window <span class="mono">30</span> days' in body
    assert "manga · Plain copy" in body and "appdata_backups · Snapshot backup" in body
    assert 'data-s3-state="ok"' in body and "Check now" in body


def test_console_rules_are_read_only_and_flagged(client, cfg):
    _full_state(cfg)
    body = client.get("/setup/storage").get_data(as_text=True)
    assert 'data-console="archive-old-logs"' in body and "expires current files 14 days after" in body
    assert "deletes or moves current backups" in body
    assert 'data-console="trim-media"' in body and "S3 applies the shorter expiry where rules overlap" in body
    assert f'href="/setup/storage?edit={BASE}%7Clogs' not in body          # never editable


def test_changes_waiting_for_confirmation_are_listed(client, cfg):
    _applied(cfg)                                                           # S3 keeps manga 180 days
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "days", "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "Waiting for your confirmation" in body
    assert "old versions removed 180 days after being replaced → old versions removed 30 days" in body


def test_a_dedicated_bucket_gets_its_own_card(client, cfg):
    jobs = JOBS + [{"name": "photos", "type": "archive", "source": "media/manga", "schedule": "0 4 * * *",
                    "enabled": True, "storage_class": "STANDARD", "retention": {"type": "count", "count": 10},
                    "dedicated": True, "bucket": f"{BASE}-photos"}]
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    _applied(cfg, jobs=jobs, bucket=f"{BASE}-photos")
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "Dedicated bucket" in body and f'data-row="{BASE}-photos|"' in body
    assert 'Newest <span class="mono">10</span> old versions per file' in body


def test_a_tamper_alarm_heads_the_screen_with_acknowledge(client, cfg):
    _full_state(cfg)
    lifecycle.set_status(cfg["CACHE_DIR"], BASE, "restored",
                         alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    body = client.get("/setup/storage").get_data(as_text=True)
    assert 'data-s3-state="blocker"' in body and "changed outside backup-engine" in body
    assert 'action="/setup/s3-rules/acknowledge"' in body and 'name="back" value="storage"' in body


def test_check_now_and_acknowledge_come_back_to_the_screen(client, monkeypatch):
    monkeypatch.setattr(lifecycle, "check", lambda c, b, **k: "ok")
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client), "back": "storage"})
    assert r.headers["Location"].endswith("/setup/storage")
    r = client.post("/setup/s3-rules/acknowledge", data={"csrf": _csrf(client), "back": "storage"})
    assert r.headers["Location"].endswith("/setup/storage")
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client), "back": "https://evil.example"})
    assert r.headers["Location"].endswith("/setup")


def test_setup_links_to_the_screen_and_the_screen_is_in_the_setup_nav(client, cfg):
    _applied(cfg)
    lifecycle.set_status(cfg["CACHE_DIR"], BASE, "ok")
    assert 'href="/setup/storage"' in client.get("/setup").get_data(as_text=True)
    import html
    body = html.unescape(client.get("/setup/storage").get_data(as_text=True))
    assert '<a href="/setup" aria-current="page">Setup</a>' in body


def test_the_screen_obeys_the_vocabulary_and_mono_laws(client, cfg):
    from tests.gui.test_vocabulary import forbidden_hits, mono_violations
    _full_state(cfg)
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "count", "count": 10, "days": 30}      # a waiting item too
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get("/setup/storage").get_data(as_text=True)
    assert forbidden_hits(body) == [] and mono_violations(body) == []


# --- the side editor, preview and apply (Task 15) ------------------------------------------------

GIB = 1024 ** 3


def _summary(cfg, folder="media/manga/"):
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": "2026-09-23T05:00:00Z", "bucket": BASE, "folder": folder,
        "noncurrent_by_age_days": [[10, 5, 500], [100, 3, 3 * GIB]], "noncurrent_by_rank": [[1, 8, 3 * GIB + 500]],
        "noncurrent_by_age_rank": [[10, 1, 5, 500], [100, 1, 3, 3 * GIB]],
        "noncurrent_versions": 8, "noncurrent_bytes": 3 * GIB + 500, "delete_markers": 0,
        "current_objects": 8, "current_bytes": 1})


def _token(body):
    return re.search(r'name="token" value="([^"]+)"', body).group(1)


def _preview(client, **fields):
    data = {"csrf": _csrf(client), "key": f"{BASE}|media/manga/"}
    data.update(fields)
    return client.post("/setup/storage/preview", data=data)


def test_change_opens_the_side_editor_for_that_row_only(client, cfg):
    _applied(cfg)
    _summary(cfg)
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert 'id="s3-editor"' in body and 'name="keep" value="days" checked' in body
    assert "This is also manga's history setting" in body
    assert 'name="undo_days"' not in body and 'name="abort_days"' not in body
    body = client.get(f"/setup/storage?edit={BASE}|appdata/").get_data(as_text=True)
    assert 'name="undo_days" value="30"' in body and 'name="keep"' not in body
    body = client.get(f"/setup/storage?edit={BASE}|*").get_data(as_text=True)
    assert 'name="abort_days" value="7"' in body and 'name="markers" value="1" checked' in body


def test_an_unknown_row_shows_no_editor(client, cfg):
    _applied(cfg)
    assert 'id="s3-editor"' not in client.get(f"/setup/storage?edit={BASE}|logs/").get_data(as_text=True)
    assert 'id="s3-editor"' not in client.get("/setup/storage?edit=other-bucket|*").get_data(as_text=True)


def test_the_live_impact_line_reads_the_summary(client, cfg):
    _applied(cfg)
    _summary(cfg)
    j = client.get(f"/setup/storage/impact.json?key={BASE}|media/manga/&keep=days&days=30").get_json()
    assert j["versions"] == 3 and "3.00 GB" in j["line"] and "permanently delete" in j["line"]
    assert client.get(f"/setup/storage/impact.json?key={BASE}|media/manga/&keep=days&days=365").get_json()["versions"] == 0
    j = client.get(f"/setup/storage/impact.json?key={BASE}|media/manga/&keep=count&count=500").get_json()
    assert "1 to 100" in j["line"]


def test_a_change_that_keeps_more_is_saved_and_applied_at_once(client, cfg, monkeypatch):
    _applied(cfg)
    applied = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: applied.append(b) or [("success", "S3 rules updated: x")])
    assert _preview(client, keep="days", days="365").status_code in (302, 303)
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "this keeps more, so S3 applies it now" in body and applied == [[BASE]]
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 365}


def test_a_bucket_wide_change_is_saved_to_storage_json(client, cfg, monkeypatch):
    _applied(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: [])
    r = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|*", "abort_days": "3"})
    assert r.status_code in (302, 303)
    b = lifecycle.bucket_settings(lifecycle.load_settings(cfg["CONFIG_DIR"]), BASE)
    assert b["abort_uploads_days"] == 3 and b["delete_marker_cleanup"] is False


def test_a_change_that_keeps_less_shows_the_preview_and_saves_nothing(client, cfg):
    _applied(cfg)
    _summary(cfg)
    r = _preview(client, keep="days", days="30")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Preview — nothing has changed yet" in body
    assert 'permanently delete about <span class="mono">3</span> old versions' in body
    assert '<span class="mono">3.00 GB</span>' in body and "oldest from" in body
    assert f'placeholder="{BASE}"' in body and 'name="token"' in body
    assert 'action="/setup/storage/refresh"' in body                        # Refresh now beside the figures
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 180}


def test_an_undo_window_preview_says_what_becomes_unrecoverable(client, cfg):
    _applied(cfg)
    body = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|appdata/",
                                                        "undo_days": "7"}).get_data(as_text=True)
    assert "Data these jobs already deleted will be unrecoverable after 7 days instead of 30 days." in body
    assert "No storage summary for this folder yet" in body


def test_an_invalid_value_is_a_form_error(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="count", count="500").get_data(as_text=True)
    assert "S3 can keep 1 to 100 old versions per file" in body and 'id="s3-editor"' in body


def test_apply_without_the_bucket_name_shows_the_preview_again(client, cfg):
    _applied(cfg)
    _summary(cfg)
    body = _preview(client, keep="days", days="30").get_data(as_text=True)
    r = client.post("/setup/storage/apply", data={"csrf": _csrf(client), "token": _token(body),
                                                   "typed": "nope", "key": f"{BASE}|media/manga/"})
    assert r.status_code == 200 and f"Type the bucket name {BASE} exactly to confirm." in r.get_data(as_text=True)
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 180}


def test_a_stale_preview_says_preview_again(client, cfg):
    _applied(cfg)
    r = client.post("/setup/storage/apply", data={"csrf": _csrf(client), "token": "x" * 24, "typed": BASE,
                                                   "key": f"{BASE}|media/manga/"}, follow_redirects=True)
    assert "That preview is out of date" in r.get_data(as_text=True)


def test_apply_end_to_end_saves_the_job_and_writes_the_rule(client, cfg, monkeypatch):
    import functools
    from tests.engine.test_lifecycle_sync import FakeS3
    for name, fn in REAL.items():
        monkeypatch.setattr(lifecycle, name, fn)
    _applied(cfg)
    _summary(cfg)
    fake = FakeS3({BASE: json.loads(json.dumps(lifecycle.load_applied(cfg["CACHE_DIR"], BASE)))})
    monkeypatch.setattr(lifecycle, "apply_confirmed", functools.partial(lifecycle.apply_confirmed, run=fake))
    body = _preview(client, keep="days", days="30").get_data(as_text=True)
    r = client.post("/setup/storage/apply", data={"csrf": _csrf(client), "token": _token(body), "typed": BASE,
                                                   "key": f"{BASE}|media/manga/"}, follow_redirects=True)
    assert "Confirmed — S3 rules updated" in r.get_data(as_text=True)
    manga = next(x for x in fake.rules[BASE] if x["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 30}


def test_waiting_changes_can_be_reviewed_and_confirmed(client, cfg):
    _applied(cfg)
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "days", "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    assert 'name="what" value="waiting"' in client.get("/setup/storage").get_data(as_text=True)
    body = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|*",
                                                        "what": "waiting"}).get_data(as_text=True)
    assert "Preview — nothing has changed yet" in body and "old versions removed 30 days" in body


def test_editor_posts_require_csrf(client):
    assert client.post("/setup/storage/preview", data={"key": f"{BASE}|*"}).status_code == 400
    assert client.post("/setup/storage/apply", data={"token": "x" * 24}).status_code == 400


def test_the_editor_and_preview_obey_the_vocabulary_and_mono_laws(client, cfg):
    from tests.gui.test_vocabulary import forbidden_hits, mono_violations
    _applied(cfg)
    _summary(cfg)
    pages = [client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True),
             client.get(f"/setup/storage?edit={BASE}|appdata/").get_data(as_text=True),
             client.get(f"/setup/storage?edit={BASE}|*").get_data(as_text=True),
             _preview(client, keep="both", count="5", days="30").get_data(as_text=True),
             _preview(client, keep="days", days="30").get_data(as_text=True)]
    for body in pages:
        assert forbidden_hits(body) == [] and mono_violations(body) == []


# --- fix round 1 --------------------------------------------------------------------------------

def _raiser(exc):
    def f(*a, **k):
        raise exc
    return f


# I1 -- the editor's first impact line is server-rendered from the same impact_line() -----------

def test_editor_opens_with_the_true_impact_line_for_a_waiting_row(client, cfg):
    _applied(cfg)                                            # baseline: manga 180 days applied
    _summary(cfg)
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "days", "days": 30}       # already waiting for confirmation
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert "permanently delete" in body and "3" in body


def test_editor_opens_with_not_checked_yet_before_the_first_check(client, cfg):
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert "Not checked yet" in body


def test_editor_opens_with_nothing_would_be_removed_when_in_step(client, cfg):
    _applied(cfg)
    _summary(cfg)
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert "Nothing S3 keeps today would be removed" in body


# I2 -- a form error on a row that no longer resolves is flashed, not dropped --------------------

def test_a_form_error_on_a_vanished_row_is_flashed_not_dropped(client, cfg):
    import html
    _applied(cfg)
    r = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|media/gone/",
                                                     "keep": "days", "days": "30"}, follow_redirects=True)
    assert "isn't one of backup-engine's" in html.unescape(r.get_data(as_text=True))


# Minors -------------------------------------------------------------------------------------

def test_a_stored_count_over_100_shows_with_no_client_side_cap(client, cfg):
    # fix round 3 superseded round 1's conditional max="100": there is no client-side max at
    # all now, for any stored count (see the fix-round-3 block below for the full "no
    # min/max/disabled" sweep) -- the server alone enforces 1-100.
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "count", "count": 500}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    count_input = re.search(r'<input[^>]*name="count"[^>]*>', body).group(0)
    assert 'value="500"' in count_input and "max=" not in count_input


def test_a_stored_days_zero_shows_as_one(client, cfg):
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "days", "days": 0}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    days_input = re.search(r'<input[^>]*name="days"[^>]*>', body).group(0)
    assert 'value="1"' in days_input


def test_a_no_op_change_says_no_change_and_skips_apply(client, cfg, monkeypatch):
    _applied(cfg)
    calls = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: calls.append(b) or [])
    r = _preview(client, keep="days", days="180")              # already what's stored and applied
    assert r.status_code in (302, 303)
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "No change." in body and calls == []


def test_resaving_an_already_waiting_value_does_not_claim_applied(client, cfg, monkeypatch):
    _applied(cfg)                                              # baseline: manga 180 days applied
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "days", "days": 30}        # already saved, already waiting
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    calls = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: calls.append(b) or [("warning", "still waits")])
    r = _preview(client, keep="days", days="30")               # resaves the SAME value
    assert r.status_code in (302, 303)
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "this keeps more, so S3 applies it now" not in body
    assert calls == [[BASE]]


def test_refresh_now_button_is_hidden_for_a_custom_s3_endpoint(client, cfg):
    _applied(cfg)
    _summary(cfg)
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text() + "S3_ENDPOINT=https://minio.example\n")
    body = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert 'action="/setup/storage/refresh"' not in body
    body = _preview(client, keep="days", days="30").get_data(as_text=True)
    assert 'action="/setup/storage/refresh"' not in body


def test_refresh_now_wording_follows_refresh_ok_when_theres_no_summary(client, cfg):
    _applied(cfg)                                              # no _summary(cfg): the no-summary branch
    body_on = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert "Refresh now for exact figures" in body_on
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text() + "S3_ENDPOINT=https://minio.example\n")
    body_off = client.get(f"/setup/storage?edit={BASE}|media/manga/").get_data(as_text=True)
    assert "Refresh now for exact figures" not in body_off


def test_days_zero_is_a_form_error(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="days", days="0").get_data(as_text=True)
    assert "Enter a whole number of days" in body and 'id="s3-editor"' in body


def test_days_above_the_upper_bound_is_a_form_error(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="days", days="36501").get_data(as_text=True)
    assert "Enter a whole number of days" in body


def test_a_huge_day_count_is_a_form_error_not_a_crash(client, cfg):
    _applied(cfg)
    r = _preview(client, keep="days", days="9" * 20)
    assert r.status_code == 200 and "Enter a whole number of days" in r.get_data(as_text=True)


def test_a_form_error_keeps_the_owners_entries(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="count", count="500", days="45").get_data(as_text=True)
    assert 'name="keep" value="count" checked' in body
    count_input = re.search(r'<input[^>]*name="count"[^>]*>', body).group(0)
    days_input = re.search(r'<input[^>]*name="days"[^>]*>', body).group(0)
    assert 'value="500"' in count_input and 'value="45"' in days_input


def test_both_saves_count_and_days_to_the_job(client, cfg, monkeypatch):
    _applied(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: [])
    r = _preview(client, keep="both", count="50", days="200")  # keeps more than the baseline (180, 0)
    assert r.status_code in (302, 303)
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "count", "count": 50, "days": 200}


def test_a_settings_edit_keeps_other_buckets(client, cfg, monkeypatch):
    _applied(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: [])
    other = "vault-dedicated-987654321098"
    Path(cfg["CONFIG_DIR"], "storage.json").write_text(json.dumps(
        {"version": 1, "buckets": {other: {"abort_uploads_days": 99, "delete_marker_cleanup": False}}}))
    r = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|*", "abort_days": "3"})
    assert r.status_code in (302, 303)
    settings = lifecycle.load_settings(cfg["CONFIG_DIR"])
    assert settings["buckets"][other]["abort_uploads_days"] == 99
    assert lifecycle.bucket_settings(settings, BASE)["abort_uploads_days"] == 3


def test_apply_flashes_a_lifecycle_error_and_keeps_waiting(client, cfg, monkeypatch):
    import html
    monkeypatch.setattr(lifecycle, "apply_confirmed", _raiser(lifecycle.LifecycleError("aws", "boom")))
    r = client.post("/setup/storage/apply", data={"csrf": _csrf(client), "token": "x" * 24, "typed": BASE,
                                                   "key": f"{BASE}|media/manga/"}, follow_redirects=True)
    body = html.unescape(r.get_data(as_text=True))
    assert "AWS refused the change" in body and "still waits for your confirmation" in body


def test_apply_flashes_a_stale_after_save_race(client, cfg, monkeypatch):
    import html
    monkeypatch.setattr(lifecycle, "apply_confirmed",
                        _raiser(lifecycle.PreviewError("stale", lifecycle._STALE_RACE)))
    r = client.post("/setup/storage/apply", data={"csrf": _csrf(client), "token": "x" * 24, "typed": BASE,
                                                   "key": f"{BASE}|media/manga/"}, follow_redirects=True)
    assert "Confirm what's waiting" in html.unescape(r.get_data(as_text=True))


# --- fix round 3 ---------------------------------------------------------------------------------
# Controller ruling on round 2's disabling approach: it works with JS, but with JS OFF a
# server-rendered `disabled` field never re-enables (a no-JS owner who hits a form error and
# then switches options is stuck -- worse than round 1). This codebase's own no-JS pattern
# (_s3_preview.html's typed-confirm, restore.html's) is the server is the gate: a native
# min/max/step constraint never blocks "Preview change" here, for any field, in any mode --
# round 2's `disabled` wiring and round 1's conditional `max="100"` are both gone. Replaces
# round 2's disabled-markup/sync-script tests (no longer applicable) with the reviewer's exact
# scenario going through in one post, plus the "no client-side constraints anywhere" sweep.

def test_the_editors_number_inputs_carry_no_client_side_constraints(client, cfg):
    _applied(cfg)
    for url in (f"/setup/storage?edit={BASE}|media/manga/", f"/setup/storage?edit={BASE}|appdata/",
                f"/setup/storage?edit={BASE}|*"):
        body = client.get(url).get_data(as_text=True)
        tags = re.findall(r'<input type="number"[^>]*>', body)
        assert tags, f"no number input found on {url}"
        for tag in tags:
            assert "min=" not in tag and "max=" not in tag and "disabled" not in tag
            assert 'step="any"' in tag


def test_the_reviewers_scenario_previews_in_one_post(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="days", days="0").get_data(as_text=True)
    assert "Enter a whole number of days" in body                    # the first post's form error
    r = _preview(client, keep="count", count="20")                   # the owner's very next post
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Preview — nothing has changed yet" in body
    assert "Enter a whole number of days" not in body


def test_a_non_whole_day_count_is_a_form_error(client, cfg):
    _applied(cfg)
    body = _preview(client, keep="days", days="1.5").get_data(as_text=True)
    assert "Enter a whole number of days" in body and 'id="s3-editor"' in body


# --- versioning on the screen (Task 16) ------------------------------------------------------------

def test_the_bucket_card_shows_versioning_and_offers_to_suspend_it(client, cfg):
    _applied(cfg)
    lifecycle.save_live(cfg["CACHE_DIR"], BASE, lifecycle.load_applied(cfg["CACHE_DIR"], BASE), versioning="on")
    body = client.get("/setup/storage").get_data(as_text=True)
    assert '<span class="tok tok-ok">versioning on</span>' in body and "Suspend versioning…" in body
    body = client.get(f"/setup/storage?edit={BASE}|*").get_data(as_text=True)
    assert 'name="versioning" value="on" checked' in body


def test_suspending_versioning_is_previewed_behind_the_bucket_name(client, cfg):
    _applied(cfg)
    body = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|*", "abort_days": "7",
                                                        "markers": "1", "versioning": "suspended"}).get_data(as_text=True)
    assert "versioning: on → suspended" in body and "gone immediately from now on" in body
    assert f'placeholder="{BASE}"' in body
    assert lifecycle.bucket_settings(lifecycle.load_settings(cfg["CONFIG_DIR"]), BASE)["versioning"] == "on"


def test_the_editor_keeps_the_versioning_choice_on_a_form_error(client, cfg):
    _applied(cfg)
    body = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|*",
                                                        "abort_days": "0", "versioning": "suspended"}
                       ).get_data(as_text=True)
    assert "Clear abandoned uploads after 1 to" in body and 'id="s3-editor"' in body
    # fix round 2, (e): the submitted radio itself stays checked, not the stored ("on") one.
    assert 'name="versioning" value="suspended" checked' in body
    assert 'name="versioning" value="on" checked' not in body


def test_a_suspended_chip_is_neutral_when_it_matches_the_owners_own_intent(client, cfg):
    # fix round 1, Minor: suspended-by-choice is not a warning -- only a mismatch (not yet
    # reconciled, or tampered) or a pending confirmation earns the warning style.
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}
    lifecycle.save_settings(cfg["CONFIG_DIR"], settings)
    _applied(cfg, settings=settings)
    lifecycle.save_live(cfg["CACHE_DIR"], BASE, lifecycle.load_applied(cfg["CACHE_DIR"], BASE), versioning="suspended")
    body = client.get("/setup/storage").get_data(as_text=True)
    assert '<span class="tok tok-ok">versioning suspended</span>' in body


def test_the_suspend_link_hides_while_a_suspend_is_already_waiting(client, cfg):
    _applied(cfg)                                                  # applied: versioning "on"
    lifecycle.save_live(cfg["CACHE_DIR"], BASE, lifecycle.load_applied(cfg["CACHE_DIR"], BASE), versioning="on")
    lifecycle.save_settings(cfg["CONFIG_DIR"], {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}})
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "Suspend versioning…" not in body
    assert '<span class="tok tok-overdue">versioning on</span>' in body   # warning: something's pending


# --- fix round 1 -------------------------------------------------------------------------------

def test_a_versioning_only_keeps_more_edit_is_saved_and_applied_at_once(client, cfg, monkeypatch):
    # I2: a bucket-wide edit that changes ONLY versioning (housekeeping unchanged) used to be
    # matched by rid == HOUSEKEEPING_ID alone, so it fell through to "No change." and never
    # called apply_for -- the intent was saved but never actually reached S3 this pass.
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}
    lifecycle.save_settings(cfg["CONFIG_DIR"], settings)
    _applied(cfg, settings=settings)
    assert lifecycle.load_applied_doc(cfg["CACHE_DIR"], BASE)["versioning"] == "suspended"
    applied = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: applied.append(b) or [("success", "S3 rules updated: x")])
    r = client.post("/setup/storage/preview", data={"csrf": _csrf(client), "key": f"{BASE}|*", "abort_days": "7",
                                                     "markers": "1", "versioning": "on"})
    assert r.status_code in (302, 303) and applied == [[BASE]]
    body = client.get("/setup/storage").get_data(as_text=True)
    assert "this keeps more, so S3 applies it now" in body
    assert lifecycle.bucket_settings(lifecycle.load_settings(cfg["CONFIG_DIR"]), BASE)["versioning"] == "on"


# --- fix round 2 -------------------------------------------------------------------------------

def test_the_chip_reads_never_turned_on_and_is_neutral_when_that_matches_intent(client, cfg):
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}
    lifecycle.save_settings(cfg["CONFIG_DIR"], settings)
    _applied(cfg, settings=settings)
    lifecycle.save_live(cfg["CACHE_DIR"], BASE, lifecycle.load_applied(cfg["CACHE_DIR"], BASE), versioning="never")
    body = client.get("/setup/storage").get_data(as_text=True)
    assert '<span class="tok tok-ok">versioning never turned on</span>' in body


def test_the_chip_warns_when_live_has_drifted_from_intent(client, cfg):
    _applied(cfg)                                       # intent "on"
    lifecycle.save_live(cfg["CACHE_DIR"], BASE, lifecycle.load_applied(cfg["CACHE_DIR"], BASE), versioning="suspended")
    body = client.get("/setup/storage").get_data(as_text=True)
    assert '<span class="tok tok-overdue">versioning suspended</span>' in body


# --- the wizard (Task 17, R-B5) and the job page line ------------------------------------------

def _save_manga(client, **fields):
    data = {"csrf": _csrf(client), "name": "manga", "type": "archive", "source": "media/manga",
            "schedule": "0 3 * * *", "storage_class": "STANDARD", "enabled": "1",
            "retention_type": "days", "retention_days": "180"}
    data.update(fields)
    return client.post("/jobs", data=data)


def test_shortening_plain_copy_history_in_the_wizard_previews_instead_of_saving(client, cfg, monkeypatch):
    _applied(cfg)
    _summary(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: pytest.fail("nothing is saved or applied yet"))
    r = _save_manga(client, retention_days="30")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="s3-history-preview"' in body
    assert 'action="/jobs/history/confirm"' in body and 'name="name" value="manga"' in body
    assert 'permanently delete about <span class="mono">3</span> old versions' in body
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 180}


def test_lengthening_plain_copy_history_saves_as_usual(client, cfg, monkeypatch):
    _applied(cfg)
    seen = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: seen.append(b) or [])
    r = _save_manga(client, retention_days="365")
    assert r.status_code in (302, 303) and seen == [[BASE]]
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 365}


def test_before_the_first_check_the_wizard_saves_and_the_pass_holds_it(client, cfg, monkeypatch):
    # fix round 1: prove the pass ACTUALLY holds it, not just that apply_for was called --
    # wire the real engine onto a FakeS3 that already has the live (pre-app) 180-day rule, the
    # way a bucket looks the first time backup-engine ever checks it (probe P1b).
    import functools
    from tests.engine.test_lifecycle_sync import FakeS3
    for name, fn in REAL.items():
        monkeypatch.setattr(lifecycle, name, fn)
    want = lifecycle.desired(BASE, BASE, JOBS, {})
    fake = FakeS3({BASE: list(want.rules.values())})
    Path(cfg["CACHE_DIR"], "state", "manga.json").write_text("{}")            # manga has run -> known folder
    Path(cfg["CACHE_DIR"], "state", "appdata_backups.json").write_text("{}")
    monkeypatch.setattr(lifecycle, "sync", functools.partial(lifecycle.sync, run=fake))
    r = _save_manga(client, retention_days="30")
    assert r.status_code in (302, 303)
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 30}
    manga = next(x for x in fake.rules[BASE] if x["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}    # the pass HOLDS it
    _, waiting = lifecycle.outstanding({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}, BASE)
    assert any(c.folder == "media/manga/" for c in waiting)


def test_confirming_the_wizard_preview_saves_the_job_and_applies(client, cfg, monkeypatch):
    import functools
    from tests.engine.test_lifecycle_sync import FakeS3
    for name, fn in REAL.items():
        monkeypatch.setattr(lifecycle, name, fn)
    _applied(cfg)
    _summary(cfg)
    fake = FakeS3({BASE: json.loads(json.dumps(lifecycle.load_applied(cfg["CACHE_DIR"], BASE)))})
    monkeypatch.setattr(lifecycle, "apply_confirmed", functools.partial(lifecycle.apply_confirmed, run=fake))
    body = _save_manga(client, retention_days="30").get_data(as_text=True)
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": _token(body),
                                                    "typed": BASE, "name": "manga"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/jobs/manga")
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 30}
    manga = next(x for x in fake.rules[BASE] if x["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert Path(cfg["CACHE_DIR"], "crontab").exists()                       # a save re-renders the schedule


def test_a_stale_wizard_preview_goes_back_to_the_form(client, cfg):
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": "x" * 24,
                                                    "typed": BASE, "name": "manga"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/jobs/manga/edit")
    assert client.post("/jobs/history/confirm", data={"token": "x" * 24}).status_code == 400   # CSRF


def test_the_wizard_offers_newest_n_plus_days_for_plain_copy(client, cfg, monkeypatch):
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: [])
    body = client.get("/jobs/new").get_data(as_text=True)
    assert 'value="count_days"' in body and 'name="retention_nd_days"' in body
    assert "Kept by S3 inside AWS" in body and "undo window" in body
    r = _save_manga(client, name="comics", retention_type="count_days",
                    retention_nd_count="10", retention_nd_days="30")
    assert r.status_code in (302, 303)
    assert jobs_io.get(cfg["CONFIG_DIR"], "comics")["retention"] == {"type": "count", "count": 10, "days": 30}
    body = client.get("/jobs/comics/edit").get_data(as_text=True)
    assert 'value="count_days" checked' in body and 'name="retention_nd_days" value="30"' in body


def test_the_combined_option_is_capped_at_100(client, cfg):
    body = _save_manga(client, name="comics", retention_type="count_days",
                       retention_nd_count="500", retention_nd_days="30").get_data(as_text=True)
    assert "S3 can keep at most 100 old versions per file" in html.unescape(body)
    assert jobs_io.get(cfg["CONFIG_DIR"], "comics") is None


def test_the_job_page_says_what_s3_keeps(client, cfg):
    from tests.gui.test_vocabulary import forbidden_hits, mono_violations
    _applied(cfg)
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert "History in S3" in body and 'S3 keeps old versions <span class="mono">180</span> days' in body
    line = re.search(r"<dd data-s3-history>.*?</dd>", body, re.S).group(0)
    assert forbidden_hits(line) == [] and mono_violations(line) == []
    body = client.get("/jobs/appdata_backups").get_data(as_text=True)
    assert 'S3 keeps what this job removed for <span class="mono">30</span> more days' in body


def test_the_job_page_mentions_a_change_waiting_for_confirmation(client, cfg):
    _applied(cfg)
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "days", "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    assert "a change is waiting for your confirmation" in client.get("/jobs/manga").get_data(as_text=True)


# --- fix round 1 (task-17-fix1.md) --------------------------------------------------------------

# --- I1: a race-stale confirm (apply_confirmed raises AFTER already saving the job) ------------

def test_a_race_stale_confirm_after_save_shows_as_waiting_not_a_lost_change(client, cfg, monkeypatch):
    _applied(cfg)
    _summary(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: pytest.fail("job_save never reaches this"))
    body = _save_manga(client, retention_days="30").get_data(as_text=True)
    tok = _token(body)

    def racy(c, token, typed, **kw):
        # Simulates apply_confirmed's own race outcome: the job save landed (its own
        # _save_edit_unlocked), then the final reconcile found the S3 rules had moved and
        # refused to write past the gate -- PreviewError("stale", _STALE_RACE) AFTER the save.
        t = lifecycle.load_preview(c["CACHE_DIR"], token)
        lifecycle._save_edit_unlocked(c, t["edit"])
        raise lifecycle.PreviewError("stale", lifecycle._STALE_RACE)
    monkeypatch.setattr(lifecycle, "apply_confirmed", racy)
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": tok,
                                                    "typed": BASE, "name": "manga"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/jobs/manga")
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 30}
    warn = _flashes(client).get("warning", "")
    assert "S3 rules changed since your preview" in warn
    assert "Save the job again" not in warn                 # that advice can't work -- it's saved


def test_an_unsaved_stale_preview_keeps_the_save_again_advice_and_the_edit_form(client, cfg):
    # The ordinary (before-save, single-use-token-reused) stale case is unaffected by I1's fix:
    # nothing was saved, so "save the job again" is still the right advice, and the edit form
    # (not the job page) is still where that resave happens.
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": "x" * 24,
                                                    "typed": BASE, "name": "manga"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/jobs/manga/edit")
    warn = _flashes(client).get("warning", "")
    assert "Save the job again" in warn


def test_a_lifecycle_error_on_the_wizard_path_leaves_the_change_waiting(client, cfg, monkeypatch):
    import functools
    from tests.engine.test_lifecycle_sync import FakeS3
    for name, fn in REAL.items():
        monkeypatch.setattr(lifecycle, name, fn)
    _applied(cfg)
    _summary(cfg)
    fake = FakeS3({BASE: json.loads(json.dumps(lifecycle.load_applied(cfg["CACHE_DIR"], BASE)))}, deny_put=True)
    monkeypatch.setattr(lifecycle, "apply_confirmed", functools.partial(lifecycle.apply_confirmed, run=fake))
    body = _save_manga(client, retention_days="30").get_data(as_text=True)
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": _token(body),
                                                    "typed": BASE, "name": "manga"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/jobs/manga")
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 30}
    manga = next(x for x in fake.rules[BASE] if x["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}     # S3 unchanged
    _, waiting = lifecycle.outstanding({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}, BASE)
    assert any(c.folder == "media/manga/" for c in waiting)


def test_a_wrong_typed_name_on_confirm_re_previews_with_the_error_not_a_bounce(client, cfg):
    _applied(cfg)
    _summary(cfg)
    body = _save_manga(client, retention_days="30").get_data(as_text=True)
    tok = _token(body)
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": tok,
                                                    "typed": "nope", "name": "manga"})
    body2 = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="s3-history-preview"' in body2
    assert "Type the bucket name" in body2
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 180}  # still unsaved
    assert 'name="retention_days" value="30"' in body2          # the wizard form still carries the entry
    new_tok = _token(body2)
    assert new_tok and new_tok != tok                           # a fresh token; the old one is spent
    assert client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": tok,
                                                       "typed": BASE, "name": "manga"}
                       ).headers["Location"].endswith("/jobs/manga/edit")  # the old token really is gone


def test_run_now_is_carried_through_the_preview_and_honoured_after_confirm(client, cfg, monkeypatch):
    import functools
    from tests.engine.test_lifecycle_sync import FakeS3
    from app.gui import runner as runner_mod
    for name, fn in REAL.items():
        monkeypatch.setattr(lifecycle, name, fn)
    _applied(cfg)
    _summary(cfg)
    fake = FakeS3({BASE: json.loads(json.dumps(lifecycle.load_applied(cfg["CACHE_DIR"], BASE)))})
    monkeypatch.setattr(lifecycle, "apply_confirmed", functools.partial(lifecycle.apply_confirmed, run=fake))
    triggered = []
    monkeypatch.setattr(runner_mod, "trigger_job", lambda scripts_dir, name: triggered.append(name))
    body = _save_manga(client, retention_days="30", run_now="1").get_data(as_text=True)
    assert 'name="run_now" value="1"' in body
    r = client.post("/jobs/history/confirm", data={"csrf": _csrf(client), "token": _token(body),
                                                    "typed": BASE, "name": "manga", "run_now": "1"})
    assert r.status_code in (302, 303)
    assert triggered == ["manga"]


# --- I2: "History in S3" reflects versioning being off ------------------------------------------

def test_the_job_page_says_versioning_is_off_for_a_suspended_base_bucket(client, cfg):
    from tests.gui.test_vocabulary import forbidden_hits, mono_violations
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended"}}}
    Path(cfg["CONFIG_DIR"], "storage.json").write_text(json.dumps(settings))
    body = client.get("/jobs/manga").get_data(as_text=True)          # Plain copy
    assert "Versioning is off for this bucket" in body
    line = re.search(r"<dd data-s3-history>.*?</dd>", body, re.S).group(0)
    assert forbidden_hits(line) == [] and mono_violations(line) == []
    body = client.get("/jobs/appdata_backups").get_data(as_text=True)  # Snapshot backup (undo)
    assert "Versioning is off for this bucket" in body


def test_the_job_page_says_versioning_is_off_for_a_dedicated_unversioned_bucket(client, cfg):
    ded = f"{BASE}-vault"
    Path(cfg["SOURCE_ROOT"], "media", "vault").mkdir(parents=True)
    jobs = [*JOBS, {"name": "vault", "type": "archive", "source": "media/vault", "schedule": "0 6 * * *",
                    "enabled": True, "storage_class": "STANDARD", "dedicated": True, "bucket": ded,
                    "bucket_versioned": False, "retention": {"type": "days", "days": 90}}]
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get("/jobs/vault").get_data(as_text=True)
    assert "Versioning is off for this bucket" in body


def test_the_job_page_unsupported_message_differs_by_job_type(client, cfg):
    _applied(cfg)
    lifecycle.set_status(cfg["CACHE_DIR"], BASE, "unsupported", "no lifecycle")
    body = client.get("/jobs/manga").get_data(as_text=True)               # Plain copy
    assert "Plain copy keeps all old versions" in body
    body = client.get("/jobs/appdata_backups").get_data(as_text=True)     # Snapshot backup (undo)
    assert "an undo window can't be set there" in body


# --- I3: the combined option's number inputs carry no client-side constraints -------------------

def test_the_combined_options_number_inputs_carry_no_client_side_constraints(client, cfg):
    body = client.get("/jobs/new").get_data(as_text=True)
    for name in ("retention_nd_count", "retention_nd_days"):
        tag = re.search(rf'<input type="number"[^>]*name="{name}"[^>]*>', body).group(0)
        assert "min=" not in tag and "max=" not in tag and "disabled" not in tag
        assert 'step="any"' in tag


# --- Minors -----------------------------------------------------------------------------------

def test_the_combined_options_wizard_copy_is_accurate(client, cfg):
    body = client.get("/jobs/new").get_data(as_text=True)
    assert "indefinitely" in body and "even after the file is deleted" in body


def test_the_job_page_combined_option_reuses_old_version_words(client, cfg):
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "count", "count": 10, "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    _applied(cfg, jobs=jobs)
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert 'older ones removed <span class="mono">30</span> days after being replaced' in body


def test_the_wizard_undo_note_shows_the_real_undo_days(client, cfg):
    settings = {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 14}}}}}
    Path(cfg["CONFIG_DIR"], "storage.json").write_text(json.dumps(settings))
    body = client.get("/jobs/appdata_backups/edit").get_data(as_text=True)
    assert 'undo window — <span class="mono">14</span> days' in body
    # a brand new (unsaved) job has no folder yet -- the default window, not a stale override
    body = client.get("/jobs/new").get_data(as_text=True)
    assert 'undo window — <span class="mono">30</span> days' in body


def test_the_wizard_s3_copy_note_is_honest_when_not_managed(client, cfg):
    Path(cfg["CONFIG_DIR"], "backup.env").write_text(f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\n")
    body = client.get("/jobs/new").get_data(as_text=True)
    assert "Kept by S3 inside AWS" not in body
    assert "AWS permissions update" in body


def test_the_wizard_s3_copy_note_is_honest_when_unsupported(client, cfg):
    lifecycle.set_status(cfg["CACHE_DIR"], BASE, "unsupported", "no lifecycle")
    body = client.get("/jobs/new").get_data(as_text=True)
    assert "Kept by S3 inside AWS" not in body
    assert "doesn't support S3 rules" in body


def test_the_keep_rule_label_includes_days_for_the_combined_form(client, cfg):
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "count", "count": 10, "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    body = client.get("/jobs/manga").get_data(as_text=True)
    assert "Keep the newest 10, older ones 30 days" in body


def test_count_days_is_rejected_for_a_snapshot_backup(client, cfg, monkeypatch):
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: pytest.fail("never reached: refused"))
    r = client.post("/jobs", data={"csrf": _csrf(client), "name": "appdata_backups", "type": "versioned",
                                   "source": "appdata", "schedule": "0 5 * * *", "storage_class": "STANDARD",
                                   "enabled": "1", "retention_type": "count_days",
                                   "retention_nd_count": "10", "retention_nd_days": "30"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "only valid for Plain copy" in body
    assert jobs_io.get(cfg["CONFIG_DIR"], "appdata_backups")["retention"]["type"] == "tiered"  # unchanged


def test_a_new_job_on_an_applied_baseline_never_previews(client, cfg, monkeypatch):
    _applied(cfg)
    _summary(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: [])
    Path(cfg["SOURCE_ROOT"], "media", "comics").mkdir(parents=True)
    r = _save_manga(client, name="comics", source="media/comics", retention_days="1")
    assert r.status_code in (302, 303) and 's3-history-preview' not in r.get_data(as_text=True)


def test_combined_to_combined_edits_gate_only_when_shortening(client, cfg, monkeypatch):
    jobs = json.loads(json.dumps(JOBS))
    jobs[0]["retention"] = {"type": "count", "count": 10, "days": 30}
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    _applied(cfg, jobs=jobs)
    _summary(cfg)
    monkeypatch.setattr(s3_rules, "apply_for", lambda c, b: [])

    def pv(**fields):
        r = _save_manga(client, retention_type="count_days", **fields)
        return r.status_code, ('s3-history-preview' in r.get_data(as_text=True))
    assert pv(retention_nd_count="10", retention_nd_days="60") == (302, False)    # more days: lengthens
    assert pv(retention_nd_count="5", retention_nd_days="30") == (200, True)      # fewer kept: shortens
    assert pv(retention_nd_count="20", retention_nd_days="10") == (200, True)     # far fewer days: shortens


def test_when_preview_itself_blows_up_the_save_goes_through_and_the_pass_holds_it(client, cfg, monkeypatch):
    import functools
    from tests.engine.test_lifecycle_sync import FakeS3
    for name, fn in REAL.items():
        monkeypatch.setattr(lifecycle, name, fn)
    _applied(cfg)
    fake = FakeS3({BASE: json.loads(json.dumps(lifecycle.load_applied(cfg["CACHE_DIR"], BASE)))})
    monkeypatch.setattr(lifecycle, "sync", functools.partial(lifecycle.sync, run=fake))
    monkeypatch.setattr(lifecycle, "preview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _save_manga(client, retention_days="30")
    assert r.status_code in (302, 303)
    assert jobs_io.get(cfg["CONFIG_DIR"], "manga")["retention"] == {"type": "days", "days": 30}
    manga = next(x for x in fake.rules[BASE] if x["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}     # S3 untouched
    _, waiting = lifecycle.outstanding({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}, BASE)
    assert any(c.folder == "media/manga/" for c in waiting)
