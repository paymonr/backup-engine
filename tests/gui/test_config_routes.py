# tests/gui/test_config_routes.py — Keys & secrets (spec 5.12) at /setup/keys.
#
# Grouped, three-state, write-only. This is ALSO the ONLY surface that writes the
# Cost Explorer billing credential (§5.6 band 5 / §5.12 Billing anchor): the
# COST_EXPLORER_* write path + its coverage moved here from the deleted
# POST /costs/billing (§10.2), so the CE round-trip is pinned below.
import pytest
from pathlib import Path
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
    client.get("/setup/keys")  # issues token into session
    from app.gui import security  # noqa: F401
    with client.session_transaction() as s:
        return s["_csrf"]


# --- old /config 301s to the new /setup/keys --------------------------------

def test_old_config_path_301s_to_setup_keys(client):
    r = client.get("/config")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/setup/keys")


# --- render: groups, fields, three-state, Billing anchor --------------------

def test_keys_get_renders_known_fields(client):
    r = client.get("/setup/keys")
    assert r.status_code == 200
    assert b"AWS_REGION" in r.data and b"S3_BUCKET" in r.data


def test_keys_renders_the_four_groups(client):
    body = client.get("/setup/keys").get_data(as_text=True)
    for head in ("Destination", "Recovery", "Billing", "This machine"):
        assert head in body


def test_keys_has_billing_anchor(client):
    # /cost links to /setup/keys#billing — the id must exist (5.12).
    body = client.get("/setup/keys").get_data(as_text=True)
    assert 'id="billing"' in body


def test_keys_three_state_shows_shipped_example(client, dirs):
    # RESTIC_PASSWORD left at the shipped placeholder reads "shipped example".
    config_io.write_secrets(dirs["config"],
                            {"RESTIC_PASSWORD": "CHANGEME-long-random-passphrase"})
    body = client.get("/setup/keys").get_data(as_text=True).lower()
    assert "shipped example" in body


def test_keys_secret_status_set_but_never_echoed(client, dirs):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIAREAL"})
    r = client.get("/setup/keys")
    assert b"AKIAREAL" not in r.data                       # write-only, never echoed
    assert b"(unchanged)" in r.data                        # the set-placeholder


# --- save: backup.env + secrets, redirect, Saved ----------------------------

def test_keys_save_writes_backup_env_and_redirects(client, dirs):
    token = _csrf(client)
    r = client.post("/setup/keys", data={"csrf": token, "AWS_REGION": "eu-west-1",
                                         "S3_BUCKET": "b"})
    assert r.status_code in (302, 303)
    assert "AWS_REGION=eu-west-1" in Path(dirs["config"], "backup.env").read_text()


def test_keys_save_secrets_are_write_only(client, dirs):
    token = _csrf(client)
    client.post("/setup/keys", data={"csrf": token, "AWS_REGION": "us-east-1", "S3_BUCKET": "b",
                                     "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s",
                                     "RESTIC_PASSWORD": "p"})
    r = client.get("/setup/keys")
    assert b"AKIA" not in r.data                            # value never echoed
    assert config_io.secrets_status(dirs["config"])["AWS_ACCESS_KEY_ID"] is True


def test_keys_post_without_csrf_is_rejected(client):
    r = client.post("/setup/keys", data={"AWS_REGION": "x"})
    assert r.status_code == 400


# --- the CRITICAL carry-in: the Cost Explorer credential WRITE path ----------

def test_keys_save_writes_cost_explorer_credential_roundtrip(client, dirs):
    # Setting the CE key via Keys & secrets persists it (the only write path now).
    token = _csrf(client)
    client.post("/setup/keys", data={
        "csrf": token, "S3_BUCKET": "b",
        "COST_EXPLORER_ACCESS_KEY_ID": "CEKEY",
        "COST_EXPLORER_SECRET_ACCESS_KEY": "CESECRET",
        "COST_EXPLORER_SESSION_TOKEN": "CETOK"})
    creds = config_io.read_cost_explorer_creds(dirs["config"])
    assert creds == {"AWS_ACCESS_KEY_ID": "CEKEY", "AWS_SECRET_ACCESS_KEY": "CESECRET",
                     "AWS_SESSION_TOKEN": "CETOK"}
    # write-only: the CE value is never echoed back on re-render
    r = client.get("/setup/keys")
    assert b"CEKEY" not in r.data and b"CESECRET" not in r.data


def test_keys_save_ce_write_does_not_drop_core_secrets(client, dirs):
    # Writing the billing group must not wipe the runtime key (shared secrets.env).
    config_io.write_secrets(dirs["config"],
                            {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    token = _csrf(client)
    client.post("/setup/keys", data={
        "csrf": token, "S3_BUCKET": "b",
        "COST_EXPLORER_ACCESS_KEY_ID": "CEKEY",
        "COST_EXPLORER_SECRET_ACCESS_KEY": "CESECRET"})
    st = config_io.secrets_status(dirs["config"])
    assert st["AWS_ACCESS_KEY_ID"] is True and st["AWS_SECRET_ACCESS_KEY"] is True
    assert config_io.read_cost_explorer_creds(dirs["config"]) is not None


def test_keys_save_newly_connecting_billing_flashes_connected(client, dirs):
    token = _csrf(client)
    r = client.post("/setup/keys", data={
        "csrf": token, "S3_BUCKET": "b",
        "COST_EXPLORER_ACCESS_KEY_ID": "CEKEY",
        "COST_EXPLORER_SECRET_ACCESS_KEY": "CESECRET"},
        follow_redirects=True)
    assert r.status_code == 200
    assert b"Connected AWS billing." in r.data


def test_keys_save_flashes_saved(client, dirs):
    token = _csrf(client)
    r = client.post("/setup/keys", data={"csrf": token, "S3_BUCKET": "b"},
                    follow_redirects=True)
    assert b"Saved." in r.data
