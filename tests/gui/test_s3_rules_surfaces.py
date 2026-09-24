# tests/gui/test_s3_rules_surfaces.py — Setup row, Board blocker, check/acknowledge (spec §5).
import json
from pathlib import Path

import pytest
from app.engine import lifecycle
from app.gui import config_io, create_app, readiness, s3_rules

BASE = "unraid-backup-123456789012"


def _env(cfg, level=4):
    Path(cfg["CONFIG_DIR"], "backup.env").write_text(
        f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\nBUCKET_ADMIN_ROLE_ARN=arn:aws:iam::1:role/r\n"
        f"PERMISSIONS_VERSION={level}\n")


@pytest.fixture
def cfg(dirs):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    c = {"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"]}
    _env(c)
    return c


def _status(cfg, **entry):
    Path(cfg["CACHE_DIR"], "state", "_lifecycle.json").write_text(json.dumps({BASE: entry}))


def test_row_absent_when_unprovisioned(dirs):
    assert s3_rules.setup_row({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"]}) is None


def test_row_asks_for_the_permissions_update_below_level_four(cfg):
    _env(cfg, level=3)
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "warn" and row["fix_url"] == "/setup/permissions"


def test_row_ok(cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="")
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "ok" and row["verified_at"] == "2026-09-23T05:00:00Z"


def test_row_not_checked_yet(cfg):
    assert s3_rules.setup_row(cfg)["sentence"] == "Not checked yet"


@pytest.mark.parametrize("kind,word", [("restored", "restored"), ("not_restored", "NOT restored")])
def test_tamper_alarm_is_a_blocker_on_setup_and_board(cfg, kind, word):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": kind, "at": "2026-09-23T04:59:00Z", "lines": []})
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "fail" and row.get("blocker") and word in row["sentence"]
    board = s3_rules.needs_you_row(cfg)
    assert board["level"] == "blocker" and board["code"] == "s3-rules-tampered" and word in board["text"]


def test_console_rule_alarm_is_a_blocker_naming_the_rule(cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "console_rule", "at": "2026-09-23T04:59:00Z", "lines": [], "rules": ["x", "y"]})
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "fail" and row.get("blocker")
    assert row["sentence"] == "A new S3 rule could delete or move backups: x, y"
    board = s3_rules.needs_you_row(cfg)
    assert board["level"] == "blocker" and board["code"] == "s3-rules-console-rule"
    assert "x, y" in board["text"]


def test_not_restored_alarm_still_names_a_console_rule(cfg):
    _status(cfg, state="not_restored", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "not_restored", "at": "2026-09-23T04:59:00Z", "lines": [], "rules": ["x"]})
    row = s3_rules.setup_row(cfg)
    assert "NOT restored" in row["sentence"] and "x" in row["sentence"]


def test_acknowledged_not_restored_stays_a_warning_not_ok(cfg):
    # lifecycle.acknowledge() only pops the alarm -- the bucket's own `state` stays
    # "not_restored" until the next Check now/backup run. That must not read as ok.
    _status(cfg, state="not_restored", checked_at="2026-09-23T05:00:00Z", detail="AccessDenied",
            alarm={"kind": "not_restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "fail" and row.get("blocker")

    lifecycle.acknowledge(cfg["CACHE_DIR"])

    row = s3_rules.setup_row(cfg)
    assert row["state"] == "warn"
    assert row["sentence"] == "S3 rules still differ from what your jobs need — try Check now"


def test_multi_bucket_not_restored_alarm_wins_over_restored(cfg):
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "movies", "type": "archive", "source": "media/movies", "schedule": "0 5 * * *",
         "enabled": True, "storage_class": "STANDARD", "dedicated": True, "bucket": f"{BASE}-movies"},
        {"name": "shows", "type": "archive", "source": "media/shows", "schedule": "0 6 * * *",
         "enabled": True, "storage_class": "STANDARD", "dedicated": True, "bucket": f"{BASE}-shows"},
    ]}))
    Path(cfg["CACHE_DIR"], "state", "_lifecycle.json").write_text(json.dumps({
        BASE: {"state": "ok", "checked_at": "2026-09-23T05:00:00Z", "detail": ""},
        f"{BASE}-movies": {"state": "not_restored", "checked_at": "2026-09-23T05:00:00Z", "detail": "",
                           "alarm": {"kind": "not_restored", "at": "2026-09-23T04:59:00Z", "lines": []}},
        f"{BASE}-shows": {"state": "restored", "checked_at": "2026-09-23T05:00:00Z", "detail": "",
                          "alarm": {"kind": "restored", "at": "2026-09-23T04:58:00Z", "lines": []}},
    }))
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "fail" and row.get("blocker") and "NOT restored" in row["sentence"]
    board = s3_rules.needs_you_row(cfg)
    assert board["level"] == "blocker" and "NOT restored" in board["text"]


def _jobs(cfg, days):
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": days}}]}))


def test_row_warns_when_the_latest_job_settings_have_not_reached_s3(cfg):
    # I3: a job save whose sync failed must not read "in place" once a later check is ok.
    _jobs(cfg, 180)
    lifecycle.save_applied(cfg["CACHE_DIR"], BASE, lifecycle.desired_rules(BASE, BASE, json.loads(
        Path(cfg["CONFIG_DIR"], "jobs.json").read_text())["jobs"], {}))
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="")
    assert s3_rules.setup_row(cfg)["state"] == "ok"
    _jobs(cfg, 365)                                  # saved; S3 still enforces 180
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "warn"
    assert row["sentence"] == "Your latest job settings haven't reached S3 yet — try Check now"
    _status(cfg, state="error", checked_at="2026-09-23T05:00:00Z", detail="AccessDenied")
    assert s3_rules.setup_row(cfg)["sentence"] == "Your latest job settings haven't reached S3 yet — try Check now"


def test_row_pending_check_never_calls_aws(cfg, monkeypatch):
    def no_aws(*a, **k):
        raise AssertionError("no AWS on a GET")
    monkeypatch.setattr(lifecycle, "read_rules", no_aws)
    monkeypatch.setattr(lifecycle, "role_creds", no_aws)
    _jobs(cfg, 365)
    lifecycle.save_applied(cfg["CACHE_DIR"], BASE, [])
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="")
    assert s3_rules.setup_row(cfg)["state"] == "warn"


def test_unsupported_storage_warns(cfg):
    _status(cfg, state="unsupported", checked_at="2026-09-23T05:00:00Z", detail="NotImplemented")
    assert "doesn't support S3 rules" in s3_rules.setup_row(cfg)["sentence"]


def test_no_board_row_without_an_alarm(cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="")
    assert s3_rules.needs_you_row(cfg) is None


def test_setup_checks_include_the_row(cfg):
    codes = {r["code"] for r in readiness.setup_checks(cfg)}
    assert "s3_rules" in codes


# --- routes -------------------------------------------------------------------------------

@pytest.fixture
def client(cfg, template_path):
    app = create_app({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": cfg["CACHE_DIR"], "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


def _csrf(client):
    client.get("/setup")
    with client.session_transaction() as s:
        return s["_csrf"]


def test_setup_page_shows_the_row_and_its_actions(client, cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    body = client.get("/setup").get_data(as_text=True)
    assert 'data-check="s3_rules"' in body and "S3 rules match your jobs" in body
    assert 'action="/setup/s3-rules/acknowledge"' in body


def test_board_shows_the_blocker(client, cfg):
    _status(cfg, state="restored", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    assert "S3 rules were changed outside backup-engine" in client.get("/").get_data(as_text=True)


def test_check_now_requires_csrf(client):
    assert client.post("/setup/s3-rules/check").status_code == 400


def test_check_now_checks_every_bucket_as_a_manual_run(client, monkeypatch):
    # The check itself applies what the jobs want (I3) and judges drift against what
    # was applied (I2) -- a sync first would overwrite tampering before the check saw it.
    calls = []
    monkeypatch.setattr(lifecycle, "sync_all", lambda cfg, **k: calls.append("sync") or [])
    monkeypatch.setattr(lifecycle, "check", lambda cfg, b, **k: calls.append(("check", b, k.get("trigger"))) or "ok")
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)})
    assert r.status_code in (302, 303) and calls == [("check", BASE, "manual")]


def _real_check_with(monkeypatch, fake):
    import functools
    monkeypatch.setattr(lifecycle, "check", functools.partial(lifecycle.check, run=fake))


def test_check_now_alarms_on_a_tampered_rule(client, cfg, monkeypatch):
    from tests.engine.test_lifecycle_sync import FakeS3
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": 180}}]}))
    lifecycle.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake = FakeS3()
    lifecycle.sync(cfg, BASE, run=fake)
    manga = next(r for r in fake.rules[BASE] if r["ID"] == "backup-engine:media/manga/")
    manga["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}          # shortened in the console
    _real_check_with(monkeypatch, fake)
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "all in place" not in body and "see Setup for what needs attention" in body
    assert lifecycle.load_status(cfg["CACHE_DIR"])[BASE]["alarm"]["kind"] == "restored"
    manga = next(r for r in fake.rules[BASE] if r["ID"] == "backup-engine:media/manga/")
    assert manga["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}


def test_check_now_says_all_in_place_only_without_an_open_alarm(client, cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "check", lambda cfg, b, **k: "ok")
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    body = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)},
                       follow_redirects=True).get_data(as_text=True)
    assert "all in place" not in body
    lifecycle.acknowledge(cfg["CACHE_DIR"])
    body = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)},
                       follow_redirects=True).get_data(as_text=True)
    assert "all in place" in body


def test_check_now_below_level_four_asks_for_the_permissions_update(client, cfg):
    _env(cfg, level=3)
    body = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)},
                       follow_redirects=True).get_data(as_text=True)
    assert "S3 rules need the AWS permissions update first." in body


def test_acknowledge_clears_the_alarm(client, cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    client.post("/setup/s3-rules/acknowledge", data={"csrf": _csrf(client)})
    assert "alarm" not in lifecycle.load_status(cfg["CACHE_DIR"])[BASE]


def test_activity_labels_s3_rules_records(client, cfg):
    from app.engine import runs
    runs.record_system(cfg["CACHE_DIR"], kind="s3-rules", summary="S3 rules updated · b")
    assert "S3 rules update" in client.get("/activity").get_data(as_text=True)


@pytest.fixture
def unprovisioned_client(dirs, template_path):
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": dirs["cache"], "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


def test_check_now_on_an_unprovisioned_install_is_a_no_op(unprovisioned_client, monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "sync_all", lambda cfg, **k: calls.append("sync") or [])
    monkeypatch.setattr(lifecycle, "check", lambda cfg, b, **k: calls.append(("check", b)) or "ok")
    r = unprovisioned_client.post("/setup/s3-rules/check", data={"csrf": _csrf(unprovisioned_client)})
    assert r.status_code in (302, 303) and calls == []


# --- Check now never fails (500) -----------------------------------------------------------

def test_check_all_never_raises(cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(lifecycle, "sync_all", boom)
    monkeypatch.setattr(lifecycle, "check", boom)
    (level, text), = s3_rules.check_all(cfg)
    assert level == "warning" and "Setup" in text


def test_check_all_never_raises_on_a_broken_jobs_file(cfg, monkeypatch):
    from app.gui import jobs_io

    def boom(*a, **k):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(jobs_io, "load", boom)
    (level, _), = s3_rules.check_all(cfg)
    assert level == "warning"


def test_check_now_route_redirects_even_when_the_check_blows_up(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(lifecycle, "sync_all", boom)
    monkeypatch.setattr(lifecycle, "check", boom)
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)})
    assert r.status_code in (302, 303)


def test_check_now_with_a_malformed_job_redirects_not_500(client, cfg, monkeypatch):
    # The real engine end to end (fake AWS): a Plain copy job with a broken history
    # setting and no applied state used to raise out of check() and 500 the route.
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "count", "count": "x"}}]}))
    from tests.engine.test_lifecycle_sync import FakeS3
    from app.gui import provision
    fake = FakeS3()
    monkeypatch.setattr(provision, "_run_aws", fake)
    monkeypatch.setattr(lifecycle, "read_lifecycle", lambda b, c, r, **k: (fake.rules.get(b, []), None))
    monkeypatch.setattr(lifecycle, "write_rules", lambda b, rules, c, r, **k: fake.rules.__setitem__(b, rules))
    monkeypatch.setattr(lifecycle, "role_creds", lambda *a, **k: {"AWS_ACCESS_KEY_ID": "A", "AWS_SECRET_ACCESS_KEY": "S"})
    monkeypatch.setattr(lifecycle, "read_versioning", lambda b, c, r, **k: "on")
    monkeypatch.setattr(lifecycle, "write_versioning", lambda b, s, c, r, **k: None)
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)})
    assert r.status_code in (302, 303)
    assert "backup-engine:media/manga/" not in {x["ID"] for x in fake.rules[BASE]}


def test_setup_page_offers_acknowledge_for_a_console_rule_alarm(client, cfg):
    import html
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "console_rule", "at": "2026-09-23T04:59:00Z", "lines": [], "rules": ["x"]})
    body = html.unescape(client.get("/setup").get_data(as_text=True))
    assert "A new S3 rule could delete or move backups: x" in body
    assert 'action="/setup/s3-rules/acknowledge"' in body
    assert "A new S3 rule could delete or move backups" in html.unescape(client.get("/").get_data(as_text=True))


# --- O2: acknowledge what the page showed, never a 500 ------------------------------------

def test_setup_page_carries_the_alarm_time_it_showed(client, cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    body = client.get("/setup").get_data(as_text=True)
    assert 'name="seen" value="2026-09-23T04:59:00Z"' in body


def test_acknowledge_only_clears_what_the_page_showed(client, cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T06:00:00Z", "lines": []})
    body = client.post("/setup/s3-rules/acknowledge",
                       data={"csrf": _csrf(client), "seen": "2026-09-23T04:59:00Z"},
                       follow_redirects=True).get_data(as_text=True)
    assert "alarm" in lifecycle.load_status(cfg["CACHE_DIR"])[BASE]
    assert "A newer S3 rules alarm arrived after this page loaded" in body


def test_acknowledge_never_500s(client, cfg, monkeypatch):
    def boom(*a, **k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(lifecycle, "acknowledge", boom)
    import html
    r = client.post("/setup/s3-rules/acknowledge", data={"csrf": _csrf(client)}, follow_redirects=True)
    assert r.status_code == 200 and "Couldn't clear the S3 rules alarm" in html.unescape(r.get_data(as_text=True))


def test_row_warns_about_changes_waiting_for_confirmation(cfg):
    jobs180 = [{"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
                "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": 180}}]
    lifecycle.save_applied(cfg["CACHE_DIR"], BASE, lifecycle.desired_rules(BASE, BASE, jobs180, {}),
                           folders=["media/manga/"])
    _jobs(cfg, 30)                                    # saved since: keeps less than S3 has
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="")
    row = s3_rules.setup_row(cfg)
    assert row["state"] == "warn" and row["fix_url"] == "/setup/storage"
    assert row["sentence"] == "1 change waiting for your confirmation"
