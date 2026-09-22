# tests/gui/test_permissions_surfaces.py — where "permissions behind" shows up
# (spec 2026-09-22 §3). Reads backup.env only; no AWS at render.
from pathlib import Path

import pytest
from app.gui import config_io, create_app, permissions, readiness


def _provision(dirs, stamp=None):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    env = "S3_BUCKET=acme\nAWS_REGION=us-east-1\n"
    if stamp is not None:
        env += f"PERMISSIONS_VERSION={stamp}\nPERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n"
    Path(dirs["config"], "backup.env").write_text(env)


def _client(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": dirs["cache"], "SECRET_KEY": "test", "TESTING": True,
                       "PRICES_LIVE": False}).test_client()


def _rows(dirs):
    rows = readiness.setup_checks({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"]})
    return {r["code"]: r for r in rows}


def test_no_permissions_row_when_unprovisioned(dirs):
    assert "permissions" not in _rows(dirs)


def test_row_warns_when_unchecked(dirs):
    _provision(dirs)
    r = _rows(dirs)["permissions"]
    assert r["state"] == "warn"
    assert r["sentence"] == "Not checked for this version of backup-engine"
    assert r["fix_url"] == "/setup/permissions"


def test_row_ok_when_current(dirs):
    _provision(dirs, stamp=permissions.required_level())
    r = _rows(dirs)["permissions"]
    assert r["state"] == "ok" and r["verified_at"] == "2026-09-22T10:00:00Z"


def test_row_names_what_an_update_adds_when_behind(dirs):
    _provision(dirs, stamp=2)
    r = _rows(dirs)["permissions"]
    assert r["state"] == "warn" and "Dedicated per-job buckets" in r["sentence"]


def test_needs_you_row_only_when_behind_or_unchecked(dirs):
    assert permissions.needs_you_row(dirs["config"]) is None           # unprovisioned
    _provision(dirs, stamp=permissions.required_level())
    assert permissions.needs_you_row(dirs["config"]) is None           # current
    _provision(dirs)
    row = permissions.needs_you_row(dirs["config"])
    assert row["level"] == "warning" and row["code"] == "permissions-update"
    assert row["fix"] == {"label": "Update permissions", "href": "/setup/permissions"}


def test_setup_page_shows_the_row_and_link(dirs, template_path):
    _provision(dirs)
    body = _client(dirs, template_path).get("/setup").get_data(as_text=True)
    assert 'data-check="permissions"' in body
    assert "AWS permissions up to date" in body
    assert 'href="/setup/permissions"' in body


def test_board_shows_the_warning_when_unchecked(dirs, template_path):
    _provision(dirs)
    body = _client(dirs, template_path).get("/").get_data(as_text=True)
    assert "AWS permissions need an update." in body


def test_board_is_quiet_when_current(dirs, template_path):
    _provision(dirs, stamp=permissions.required_level())
    body = _client(dirs, template_path).get("/").get_data(as_text=True)
    assert "AWS permissions need an update." not in body


def test_status_json_carries_the_row(dirs, template_path):
    _provision(dirs)
    js = _client(dirs, template_path).get("/status.json").get_json()
    assert "permissions-update" in [r.get("code") for r in js["needs_you"]]


def test_destination_page_shows_the_permissions_line(dirs, template_path):
    _provision(dirs, stamp=permissions.required_level())
    body = _client(dirs, template_path).get("/setup/destination").get_data(as_text=True)
    assert 'data-perm-line="current"' in body
    assert 'href="/setup/permissions"' in body


def test_activity_labels_a_permissions_record(dirs, template_path):
    _provision(dirs)
    permissions.record(dirs["cache"], mode="update", lines=["Create the extra-buckets policy"])
    body = _client(dirs, template_path).get("/activity").get_data(as_text=True)
    assert "permissions update" in body
