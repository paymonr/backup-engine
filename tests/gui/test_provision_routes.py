import pytest
from pathlib import Path
from app.gui import create_app


@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client, path):
    client.get(path)  # issues token into session
    with client.session_transaction() as s:
        return s["_csrf"]


def test_provision_home_renders_all_three_modes(client):
    r = client.get("/provision")
    assert r.status_code == 200
    assert b"Guided" in r.data and b"Scripted" in r.data and b"Automated" in r.data


def test_manual_render_returns_scoped_policy_json(client):
    token = _csrf(client, "/provision/manual")
    r = client.post("/provision/manual/render",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1"})
    assert r.status_code == 200
    assert b"arn:aws:s3:::acme/appdata/*" in r.data


def test_manual_render_requires_csrf(client):
    r = client.post("/provision/manual/render", data={"bucket": "b", "region": "r"})
    assert r.status_code == 400


def test_manual_render_rejects_missing_fields(client):
    token = _csrf(client, "/provision/manual")
    r = client.post("/provision/manual/render", data={"csrf": token, "bucket": "", "region": ""})
    assert r.status_code == 400


def test_scripted_panel_shows_setup_command(client):
    r = client.get("/provision/scripted")
    assert r.status_code == 200
    assert b"setup.sh" in r.data


def test_validate_success_writes_secrets_and_backup_env(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "validate_runtime_key", lambda *a, **k: None)
    token = _csrf(client, "/provision/manual")
    r = client.post("/provision/validate",
                    data={"csrf": token, "bucket": "acme", "region": "eu-west-1",
                          "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    assert r.status_code in (302, 303)
    sec = Path(dirs["config"], "secrets.env").read_text()
    assert "AWS_ACCESS_KEY_ID=AKIA" in sec and "AWS_SECRET_ACCESS_KEY=sek" in sec
    be = Path(dirs["config"], "backup.env").read_text()
    assert "AWS_REGION=eu-west-1" in be and "S3_BUCKET=acme" in be


def test_validate_failure_saves_nothing_and_hides_secret(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.ValidationError("put", "denied")

    monkeypatch.setattr(provision, "validate_runtime_key", boom)
    token = _csrf(client, "/provision/manual")
    r = client.post("/provision/validate",
                    data={"csrf": token, "bucket": "acme", "region": "eu-west-1",
                          "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert b"AKIA" not in r.data and b"sek" not in r.data


def test_validate_requires_csrf(client):
    r = client.post("/provision/validate", data={"bucket": "b"})
    assert r.status_code == 400


def test_automated_form_renders(client):
    r = client.get("/provision/automated")
    assert r.status_code == 200
    assert b"transient" in r.data.lower()


def test_automated_success_writes_runtime_key_and_never_shows_secrets(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                         "AWS_SECRET_ACCESS_KEY": "runsek",
                                         "bucket": "acme", "region": "us-east-1"})
    token = _csrf(client, "/provision/automated")
    r = client.post("/provision/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"ADMINK" not in r.data and b"ADMINS" not in r.data and b"runsek" not in r.data
    sec = Path(dirs["config"], "secrets.env").read_text()
    assert "AWS_ACCESS_KEY_ID=AKIARUN" in sec
    be = Path(dirs["config"], "backup.env").read_text()
    assert "S3_BUCKET=acme" in be and "AWS_REGION=us-east-1" in be


def test_automated_failure_saves_nothing(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.TofuError("apply", "boom")

    monkeypatch.setattr(provision, "run_tofu_apply", boom)
    token = _csrf(client, "/provision/automated")
    r = client.post("/provision/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert b"ADMINK" not in r.data and b"ADMINS" not in r.data


def test_automated_requires_csrf(client):
    r = client.post("/provision/automated", data={"bucket": "b"})
    assert r.status_code == 400


def test_manual_form_defaults_region_us_east_1(client):
    r = client.get("/provision/manual")
    assert b'value="us-east-1"' in r.data


def test_automated_form_shows_auto_naming_and_region_default(client):
    r = client.get("/provision/automated")
    assert b"unraid-backup" in r.data
    assert b'value="us-east-1"' in r.data


def test_automated_auto_names_bucket_from_account(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "aws_account_id", lambda *a, **k: "123456789012")
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda bucket, *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                                 "AWS_SECRET_ACCESS_KEY": "runsek",
                                                 "bucket": bucket, "region": "us-east-1"})
    token = _csrf(client, "/provision/automated")
    r = client.post("/provision/automated",
                    data={"csrf": token, "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                    follow_redirects=True)
    assert r.status_code == 200
    be = Path(dirs["config"], "backup.env").read_text()
    assert "S3_BUCKET=unraid-backup-123456789012" in be


def test_automated_override_bucket_skips_account_lookup(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise AssertionError("account lookup must be skipped when an override is provided")

    monkeypatch.setattr(provision, "aws_account_id", boom)
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda bucket, *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                                 "AWS_SECRET_ACCESS_KEY": "runsek",
                                                 "bucket": bucket, "region": "us-east-1"})
    token = _csrf(client, "/provision/automated")
    client.post("/provision/automated",
                data={"csrf": token, "bucket": "my-own-bucket", "region": "us-east-1",
                      "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                follow_redirects=True)
    be = Path(dirs["config"], "backup.env").read_text()
    assert "S3_BUCKET=my-own-bucket" in be


def test_provision_home_shows_first_run_setup_when_unprovisioned(client):
    r = client.get("/provision")
    assert r.status_code == 200
    assert b"First-time setup" in r.data


def test_provision_home_shows_ready_when_provisioned(dirs, template_path):
    from app.gui import config_io
    config_io.write_secrets(dirs["config"],
                            {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    Path(dirs["config"], "backup.env").write_text("S3_BUCKET=acme\nAWS_REGION=us-east-1\n")
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SECRET_KEY": "test", "TESTING": True})
    r = app.test_client().get("/provision")
    assert r.status_code == 200
    assert b"Destination set" in r.data and b"acme" in r.data
    assert b"First-time setup" not in r.data


def test_automated_account_lookup_failure_saves_nothing(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.AccountLookupError("nope")

    monkeypatch.setattr(provision, "aws_account_id", boom)
    token = _csrf(client, "/provision/automated")
    r = client.post("/provision/automated",
                    data={"csrf": token, "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
