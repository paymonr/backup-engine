# tests/gui/test_provision_routes.py — Destination (spec 5.11): the preserved
# three-path provisioning picker, restyled onto /setup/destination. Every string
# the tests pinned before (Guided / Scripted / Automated / Destination set /
# transient / unraid-backup / value="us-east-1" / failed at tofu apply /
# What AWS / OpenTofu reported / arn:aws:s3:::acme/appdata/* / setup.sh) still
# renders; "First-time setup" is retired in favour of "Where backups go".
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


# --- old /provision* paths 301 to the new /setup/destination* ---------------

@pytest.mark.parametrize("old,new", [
    ("/provision", "/setup/destination"),
    ("/provision/manual", "/setup/destination/manual"),
    ("/provision/scripted", "/setup/destination/scripted"),
    ("/provision/automated", "/setup/destination/automated"),
])
def test_old_provision_paths_301(client, old, new):
    r = client.get(old)
    assert r.status_code == 301
    assert r.headers["Location"].endswith(new)


# --- the picker -------------------------------------------------------------

def test_destination_renders_all_three_modes(client):
    r = client.get("/setup/destination")
    assert r.status_code == 200
    assert b"Guided" in r.data and b"Scripted" in r.data and b"Automated" in r.data


def test_destination_heading_is_where_backups_go(client):
    # "First-time setup" is retired; the h1 is now "Where backups go" (5.11).
    r = client.get("/setup/destination")
    assert r.status_code == 200
    assert b"Where backups go" in r.data
    assert b"First-time setup" not in r.data


def test_destination_shows_ready_when_provisioned(dirs, template_path):
    from app.gui import config_io
    config_io.write_secrets(dirs["config"],
                            {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    Path(dirs["config"], "backup.env").write_text("S3_BUCKET=acme\nAWS_REGION=us-east-1\n")
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SECRET_KEY": "test", "TESTING": True})
    r = app.test_client().get("/setup/destination")
    assert r.status_code == 200
    assert b"Destination set" in r.data and b"acme" in r.data


# --- guided-manual ----------------------------------------------------------

def test_manual_render_returns_scoped_policy_json(client):
    token = _csrf(client, "/setup/destination/manual")
    r = client.post("/setup/destination/manual/render",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1"})
    assert r.status_code == 200
    assert b"arn:aws:s3:::acme/appdata/*" in r.data


def test_manual_render_requires_csrf(client):
    r = client.post("/setup/destination/manual/render", data={"bucket": "b", "region": "r"})
    assert r.status_code == 400


def test_manual_render_rejects_missing_fields(client):
    token = _csrf(client, "/setup/destination/manual")
    r = client.post("/setup/destination/manual/render",
                    data={"csrf": token, "bucket": "", "region": ""})
    assert r.status_code == 400


def test_manual_form_defaults_region_us_east_1(client):
    r = client.get("/setup/destination/manual")
    assert b'value="us-east-1"' in r.data


# --- scripted ---------------------------------------------------------------

def test_scripted_panel_shows_setup_command(client):
    r = client.get("/setup/destination/scripted")
    assert r.status_code == 200
    assert b"setup.sh" in r.data


# --- validate (guided-manual key) -------------------------------------------

def test_validate_success_writes_secrets_and_lands_on_setup(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "validate_runtime_key", lambda *a, **k: None)
    token = _csrf(client, "/setup/destination/manual")
    r = client.post("/setup/destination/validate",
                    data={"csrf": token, "bucket": "acme", "region": "eu-west-1",
                          "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    assert r.status_code in (302, 303)
    assert r.headers["Location"].endswith("/setup")
    sec = Path(dirs["config"], "secrets.env").read_text()
    assert "AWS_ACCESS_KEY_ID=AKIA" in sec and "AWS_SECRET_ACCESS_KEY=sek" in sec
    be = Path(dirs["config"], "backup.env").read_text()
    assert "AWS_REGION=eu-west-1" in be and "S3_BUCKET=acme" in be


def test_validate_success_flashes_destination_set(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "validate_runtime_key", lambda *a, **k: None)
    token = _csrf(client, "/setup/destination/manual")
    r = client.post("/setup/destination/validate",
                    data={"csrf": token, "bucket": "acme", "region": "eu-west-1",
                          "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"Destination set: acme in eu-west-1" in r.data
    assert b"the recovery passphrase, then the first job" in r.data


def test_validate_failure_saves_nothing_and_hides_secret(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.ValidationError("put", "denied")

    monkeypatch.setattr(provision, "validate_runtime_key", boom)
    token = _csrf(client, "/setup/destination/manual")
    r = client.post("/setup/destination/validate",
                    data={"csrf": token, "bucket": "acme", "region": "eu-west-1",
                          "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert b"AKIA" not in r.data and b"sek" not in r.data


def test_validate_requires_csrf(client):
    r = client.post("/setup/destination/validate", data={"bucket": "b"})
    assert r.status_code == 400


# --- automated --------------------------------------------------------------

def test_automated_form_renders(client):
    r = client.get("/setup/destination/automated")
    assert r.status_code == 200
    assert b"transient" in r.data.lower()


def test_automated_success_writes_runtime_key_and_never_shows_secrets(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                         "AWS_SECRET_ACCESS_KEY": "runsek",
                                         "bucket": "acme", "region": "us-east-1"})
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"ADMINK" not in r.data and b"ADMINS" not in r.data and b"runsek" not in r.data
    assert b"Destination set: acme in us-east-1" in r.data
    sec = Path(dirs["config"], "secrets.env").read_text()
    assert "AWS_ACCESS_KEY_ID=AKIARUN" in sec
    be = Path(dirs["config"], "backup.env").read_text()
    assert "S3_BUCKET=acme" in be and "AWS_REGION=us-east-1" in be


def test_automated_failure_saves_nothing(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.TofuError("apply", "boom")

    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply", boom)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert b"ADMINK" not in r.data and b"ADMINS" not in r.data


def test_automated_requires_csrf(client):
    r = client.post("/setup/destination/automated", data={"bucket": "b"})
    assert r.status_code == 400


def test_automated_form_shows_auto_naming_and_region_default(client):
    r = client.get("/setup/destination/automated")
    assert b"unraid-backup" in r.data
    assert b'value="us-east-1"' in r.data


def test_automated_auto_names_bucket_from_account(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "aws_account_id", lambda *a, **k: "123456789012")
    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda bucket, *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                                 "AWS_SECRET_ACCESS_KEY": "runsek",
                                                 "bucket": bucket, "region": "us-east-1"})
    token = _csrf(client, "/setup/destination/automated")
    client.post("/setup/destination/automated",
                data={"csrf": token, "region": "us-east-1",
                      "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                follow_redirects=True)
    be = Path(dirs["config"], "backup.env").read_text()
    assert "S3_BUCKET=unraid-backup-123456789012" in be


def test_automated_override_bucket_skips_account_lookup(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise AssertionError("account lookup must be skipped when an override is provided")

    monkeypatch.setattr(provision, "aws_account_id", boom)
    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda bucket, *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                                 "AWS_SECRET_ACCESS_KEY": "runsek",
                                                 "bucket": bucket, "region": "us-east-1"})
    token = _csrf(client, "/setup/destination/automated")
    client.post("/setup/destination/automated",
                data={"csrf": token, "bucket": "my-own-bucket", "region": "us-east-1",
                      "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                follow_redirects=True)
    be = Path(dirs["config"], "backup.env").read_text()
    assert "S3_BUCKET=my-own-bucket" in be


def test_automated_failure_surfaces_the_real_tofu_error(client, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.TofuError("apply", "Error: creating S3 Bucket: BucketAlreadyOwnedByYou")

    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply", boom)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert b"failed at tofu apply" in r.data              # the generic summary
    assert b"BucketAlreadyOwnedByYou" in r.data           # the ACTUAL reason, now shown
    assert b"What AWS / OpenTofu reported" in r.data


def test_automated_account_lookup_failure_saves_nothing(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.AccountLookupError("nope")

    monkeypatch.setattr(provision, "aws_account_id", boom)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()


# --- capability preflight (reject IAM-incapable admin creds before tofu) ----
#
# `aws_account_id` (sts get-caller-identity) succeeds for a plain
# `sts get-session-token` session with no MFA -- those creds still cannot
# touch IAM and blow up mid-`tofu apply`, leaving an orphaned bucket. The
# route must call `provision.verify_admin_can_provision` after the account
# lookup and BEFORE `run_tofu_apply` ever runs.

def test_automated_token_barred_creds_save_nothing_and_guide_temp_creds(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.AdminCapabilityError("token", "InvalidClientTokenId")

    monkeypatch.setattr(provision, "verify_admin_can_provision", boom)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ASIAFAKETEMPKEY",
                          "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert not Path(dirs["config"], "backup.env").exists()
    assert b"ASIA" in r.data
    assert b"session token" in r.data.lower()
    assert b"Nothing was saved" in r.data
    assert b"ASIAFAKETEMPKEY" not in r.data and b"ADMINS" not in r.data


def test_automated_permission_barred_creds_save_nothing_and_name_the_fix(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.AdminCapabilityError("permission", "AccessDenied")

    monkeypatch.setattr(provision, "verify_admin_can_provision", boom)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "AKIAADMIN", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert b"allowed to create IAM users" in r.data
    assert b"provisioning permissions" in r.data
    assert b"Nothing was saved" in r.data


def test_automated_unknown_capability_failure_shows_generic_message_and_detail(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.AdminCapabilityError("unknown", "some odd network error")

    monkeypatch.setattr(provision, "verify_admin_can_provision", boom)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "AKIAADMIN", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400
    assert not Path(dirs["config"], "secrets.env").exists()
    assert b"couldn" in r.data.lower() and b"provision" in r.data.lower()
    assert b"some odd network error" in r.data


def test_automated_capability_check_runs_before_tofu_apply(client, dirs, monkeypatch):
    from app.gui import provision

    def boom(*a, **k):
        raise provision.AdminCapabilityError("token", "InvalidClientTokenId")

    def tofu_must_not_run(*a, **k):
        raise AssertionError("run_tofu_apply must not run on IAM-incapable admin creds")

    monkeypatch.setattr(provision, "verify_admin_can_provision", boom)
    monkeypatch.setattr(provision, "run_tofu_apply", tofu_must_not_run)
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ASIAFAKE", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"})
    assert r.status_code == 400   # tofu_must_not_run would have raised AssertionError -> 500


def test_automated_success_flashes_delete_admin_key_nudge(client, dirs, monkeypatch):
    from app.gui import provision
    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply",
                        lambda *a, **k: {"AWS_ACCESS_KEY_ID": "AKIARUN",
                                         "AWS_SECRET_ACCESS_KEY": "runsek",
                                         "bucket": "acme", "region": "us-east-1"})
    token = _csrf(client, "/setup/destination/automated")
    r = client.post("/setup/destination/automated",
                    data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                          "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                    follow_redirects=True)
    assert r.status_code == 200
    sec = Path(dirs["config"], "secrets.env").read_text()
    assert "AWS_ACCESS_KEY_ID=AKIARUN" in sec
    assert b"never stored your admin key" in r.data
    assert b"delete that access key" in r.data
