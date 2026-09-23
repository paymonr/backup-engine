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


def test_check_now_syncs_then_checks(client, monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, "sync_all", lambda cfg, **k: calls.append("sync") or [])
    monkeypatch.setattr(lifecycle, "check", lambda cfg, b, **k: calls.append(("check", b)) or "ok")
    r = client.post("/setup/s3-rules/check", data={"csrf": _csrf(client)})
    assert r.status_code in (302, 303) and calls == ["sync", ("check", BASE)]


def test_acknowledge_clears_the_alarm(client, cfg):
    _status(cfg, state="ok", checked_at="2026-09-23T05:00:00Z", detail="",
            alarm={"kind": "restored", "at": "2026-09-23T04:59:00Z", "lines": []})
    client.post("/setup/s3-rules/acknowledge", data={"csrf": _csrf(client)})
    assert "alarm" not in lifecycle.load_status(cfg["CACHE_DIR"])[BASE]


def test_activity_labels_s3_rules_records(client, cfg):
    from app.engine import runs
    runs.record_system(cfg["CACHE_DIR"], kind="s3-rules", summary="S3 rules updated · b")
    assert "S3 rules update" in client.get("/activity").get_data(as_text=True)
