# tests/gui/test_settings_routes.py -- the Settings page (/setup/settings), the sidebar theme
# switch (/setup/theme), and the split from Keys & secrets (2026-10-06).
from pathlib import Path

import pytest
from app.gui import create_app, config_io


@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client):
    client.get("/setup/settings")
    with client.session_transaction() as s:
        return s["_csrf"]


def _env(dirs):
    return config_io.read_backup_env(dirs["config"])


# --- the page -------------------------------------------------------------------

def test_settings_page_renders_theme_and_the_moved_settings(client):
    body = client.get("/setup/settings").get_data(as_text=True)
    for word in ("Light", "Dark", "Auto"):
        assert f'value="{word.lower()}"' in body, word
    for key in ("TZ", "LOG_LEVEL", "APPRISE_URLS", "NOTIFY_ON_SUCCESS", "HEALTHCHECK_URL",
                "BE_MAX_ATTEMPTS", "BE_RETRY_BASE_SECONDS", "BE_MAX_RESUMES",
                "RCLONE_TRANSFERS", "RCLONE_BWLIMIT", "AUTO_RESUME_ON_BOOT"):
        assert f'name="{key}"' in body, key
    assert 'name="AWS_ACCESS_KEY_ID"' not in body and 'name="S3_BUCKET"' not in body


def test_keys_page_no_longer_shows_the_moved_settings(client):
    body = client.get("/setup/keys").get_data(as_text=True)
    for key in ("TZ", "APPRISE_URLS", "RCLONE_TRANSFERS", "AUTO_RESUME_ON_BOOT", "GUI_THEME"):
        assert f'name="{key}"' not in body, key
    assert 'name="S3_BUCKET"' in body and 'name="SOURCE_ROOT"' in body
    # the deployment-only GUI keys show read-only under Mounts
    assert 'name="GUI_PORT"' not in body and "GUI_PORT" in body


# --- saving ---------------------------------------------------------------------

def test_settings_save_writes_its_keys_and_keeps_the_destination(client, dirs):
    config_io.write_backup_env(client.application.config["TEMPLATE_PATH"], dirs["config"],
                               {"S3_BUCKET": "keep-me", "AWS_REGION": "eu-west-1"})
    token = _csrf(client)
    r = client.post("/setup/settings", data={"csrf": token, "GUI_THEME": "dark", "TZ": "Europe/Oslo",
                                             "BE_MAX_ATTEMPTS": "5", "RCLONE_TRANSFERS": "4",
                                             "NOTIFY_ON_SUCCESS": "true"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/setup/settings")
    env = _env(dirs)
    assert env["GUI_THEME"] == "dark" and env["TZ"] == "Europe/Oslo"
    assert env["BE_MAX_ATTEMPTS"] == "5" and env["RCLONE_TRANSFERS"] == "4"
    assert env["AUTO_RESUME_ON_BOOT"] == "false"            # unchecked checkbox
    assert env["S3_BUCKET"] == "keep-me" and env["AWS_REGION"] == "eu-west-1"


def test_settings_save_rejects_a_bad_theme_and_csrf(client, dirs):
    token = _csrf(client)
    assert client.post("/setup/settings", data={"csrf": token, "GUI_THEME": "purple"}).status_code == 400
    assert client.post("/setup/settings", data={"GUI_THEME": "dark"}).status_code == 400


def test_keys_save_carries_the_settings_through(client, dirs):
    tpl = client.application.config["TEMPLATE_PATH"]
    config_io.write_backup_env(tpl, dirs["config"], {"S3_BUCKET": "b", "GUI_THEME": "dark",
                                                     "TZ": "Europe/Oslo", "RCLONE_TRANSFERS": "4"})
    token = _csrf(client)
    r = client.post("/setup/keys", data={"csrf": token, "S3_BUCKET": "b", "AWS_REGION": "us-east-1"})
    assert r.status_code in (302, 303)
    env = _env(dirs)
    assert env["GUI_THEME"] == "dark" and env["TZ"] == "Europe/Oslo" and env["RCLONE_TRANSFERS"] == "4"


# --- the sidebar theme switch ---------------------------------------------------

def test_theme_switch_writes_the_theme_and_returns_to_the_page(client, dirs):
    token = _csrf(client)
    r = client.post("/setup/theme", data={"csrf": token, "theme": "light", "next": "/activity"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/activity")
    assert _env(dirs)["GUI_THEME"] == "light"
    r = client.post("/setup/theme", data={"csrf": token, "theme": "auto", "next": "https://evil.example/"})
    assert r.headers["Location"].endswith("/")             # off-site next is ignored
    assert _env(dirs)["GUI_THEME"] == "auto"


def test_theme_switch_rejects_bad_values_and_missing_csrf(client, dirs):
    token = _csrf(client)
    assert client.post("/setup/theme", data={"csrf": token, "theme": "sepia"}).status_code == 400
    assert client.post("/setup/theme", data={"theme": "dark"}).status_code == 400
    assert "GUI_THEME" not in _env(dirs)


# --- the shell applies it -------------------------------------------------------

def test_shell_sets_data_theme_from_the_saved_setting(client, dirs):
    tpl = client.application.config["TEMPLATE_PATH"]
    body = client.get("/setup/settings").get_data(as_text=True)
    assert "data-theme=" not in body.split("<body")[0]         # auto: the device decides
    config_io.write_backup_env(tpl, dirs["config"], {"GUI_THEME": "dark"})
    head = client.get("/setup/settings").get_data(as_text=True).split("<body")[0]
    assert 'data-theme="dark"' in head


def test_sidebar_has_a_settings_link_and_the_theme_switch(client):
    body = client.get("/setup/settings").get_data(as_text=True)
    assert 'href="/setup/settings"' in body and ">Settings</a>" in body
    assert 'action="/setup/theme"' in body
    assert body.count('name="theme"') == 3
