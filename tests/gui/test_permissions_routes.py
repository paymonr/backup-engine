# tests/gui/test_permissions_routes.py — /setup/permissions (spec 2026-09-22 §3).
# The engine is monkeypatched: these tests pin the routes, not AWS.
from pathlib import Path

import pytest
from app.gui import config_io, create_app, permissions, provision

ACCOUNT = "123456789012"
P = permissions.parse_principal(f"arn:aws:iam::{ACCOUNT}:user/backup-engine-runtime")
CREDS = {"ADMIN_ACCESS_KEY_ID": "ADMINKEYVALUE", "ADMIN_SECRET_ACCESS_KEY": "ADMINSECRETVALUE"}


@pytest.fixture
def app(dirs, template_path):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIARUN", "AWS_SECRET_ACCESS_KEY": "runsek"})
    Path(dirs["config"], "backup.env").write_text(f"S3_BUCKET=unraid-backup-{ACCOUNT}\nAWS_REGION=us-east-1\n")
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": dirs["cache"], "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def principal(monkeypatch):
    monkeypatch.setattr(permissions, "runtime_principal", lambda *a, **k: P)


def _csrf(client):
    client.get("/setup/permissions")
    with client.session_transaction() as s:
        return s["_csrf"]


def _steps(*statuses):
    ids = ["R1", "R2", "R3", "R4", "R5"]
    return [permissions.Step(ids[i], "attach", f"Summary {ids[i]}", ["iam", "attach-user-policy"],
                             status=st, error="boom" if st == "failed" else "")
            for i, st in enumerate(statuses)]


def test_get_unprovisioned_points_at_destination(dirs, template_path):
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SECRET_KEY": "test", "TESTING": True})
    body = app.test_client().get("/setup/permissions").get_data(as_text=True)
    assert "Set up the destination first" in body


def test_get_shows_unchecked_status_and_both_paths(client):
    body = client.get("/setup/permissions").get_data(as_text=True)
    assert 'data-perm-state="unchecked"' in body
    assert "Update permissions" in body and "Preview only" in body
    assert "Run the commands yourself" in body


def test_update_requires_csrf(client):
    assert client.post("/setup/permissions/update", data=CREDS).status_code == 400


def test_update_success_shows_steps_and_never_echoes_creds(client, monkeypatch):
    seen = {}

    def fake(principal, **kw):
        seen.update(kw, principal=principal)
        return permissions.Outcome(ok=True, applied=True, steps=_steps("done", "done", "done", "done", "done"))
    monkeypatch.setattr(permissions, "converge", fake)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert seen["principal"] == P and seen["apply_changes"] is True and seen["mode"] == "update"
    assert seen["admin"] == permissions.AdminCreds("ADMINKEYVALUE", "ADMINSECRETVALUE", None)
    assert seen["bucket"] == f"unraid-backup-{ACCOUNT}" and seen["region"] == "us-east-1"
    assert "Summary R5" in body and "AWS permissions updated." in body
    assert "delete that access key in AWS now" in body
    assert "ADMINKEYVALUE" not in body and "ADMINSECRETVALUE" not in body


def test_preview_changes_nothing(client, monkeypatch):
    seen = {}

    def fake(principal, **kw):
        seen.update(kw)
        return permissions.Outcome(ok=True, applied=False, steps=_steps("pending", "pending"))
    monkeypatch.setattr(permissions, "converge", fake)
    body = client.post("/setup/permissions/preview",
                       data={"csrf": _csrf(client), **CREDS}).get_data(as_text=True)
    assert seen["apply_changes"] is False and seen["mode"] == "check"
    assert "Preview — nothing was changed" in body and "Would do" in body
    assert "delete that access key in AWS now" not in body


def test_already_current_redirects_with_a_flash(client, monkeypatch):
    monkeypatch.setattr(permissions, "converge",
                        lambda p, **kw: permissions.Outcome(ok=True, applied=False, steps=[]))
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS},
                    follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "already in place" in body
    assert "delete that access key in AWS now" in body


def test_failed_step_explains_rerun(client, monkeypatch):
    monkeypatch.setattr(permissions, "converge",
                        lambda p, **kw: permissions.Outcome(ok=False, applied=True,
                                                            steps=_steps("done", "failed", "not-run")))
    body = client.post("/setup/permissions/update",
                       data={"csrf": _csrf(client), **CREDS}).get_data(as_text=True)
    assert "Stopped at the first failure" in body and "Not run" in body


def test_admin_token_error_is_explained(client, monkeypatch):
    def boom(p, **kw):
        raise provision.AdminCapabilityError("token", "InvalidClientTokenId ADMINSECRETVALUE")
    monkeypatch.setattr(permissions, "converge", boom)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and "can't manage AWS IAM" in body
    assert "ADMINSECRETVALUE" not in body


def test_account_mismatch_is_explained(client, monkeypatch):
    def boom(p, **kw):
        raise permissions.PermissionsError("account_mismatch", "The admin credentials are for AWS account 999999999999, but the backup key belongs to account 123456789012.")
    monkeypatch.setattr(permissions, "converge", boom)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    assert r.status_code == 400 and "belongs to account" in r.get_data(as_text=True)


def test_runtime_key_problem_is_explained(client, monkeypatch):
    def boom(*a, **k):
        raise permissions.PermissionsError("runtime_key", "InvalidClientTokenId")
    monkeypatch.setattr(permissions, "runtime_principal", boom)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    assert r.status_code == 400 and "saved backup key" in r.get_data(as_text=True)


# --- the commands path -------------------------------------------------------------

def test_show_commands_renders_the_script(client):
    body = client.post("/setup/permissions/script", data={"csrf": _csrf(client)}).get_data(as_text=True)
    assert 'id="perm-script"' in body
    assert "put-user-policy" in body and 'data-copy-target="perm-script"' in body


def test_show_commands_requires_csrf(client):
    assert client.post("/setup/permissions/script").status_code == 400


def test_show_commands_runtime_key_failure(client, monkeypatch):
    def boom(*a, **k):
        raise permissions.PermissionsError("runtime_key", "InvalidClientTokenId")
    monkeypatch.setattr(permissions, "runtime_principal", boom)
    r = client.post("/setup/permissions/script", data={"csrf": _csrf(client)})
    assert r.status_code == 400 and "saved backup key" in r.get_data(as_text=True)


def test_verify_all_good_stamps_and_records(client, dirs, monkeypatch):
    monkeypatch.setattr(permissions, "verify",
                        lambda p, **kw: [permissions.Probe("A", True), permissions.Probe("B", True)])
    r = client.post("/setup/permissions/verify", data={"csrf": _csrf(client)}, follow_redirects=True)
    assert "Verified" in r.get_data(as_text=True)
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())
    assert env["BUCKET_ADMIN_ROLE_ARN"] == permissions.role_arn(ACCOUNT)
    assert "permissions" in Path(dirs["cache"], "state", "_system.runs.jsonl").read_text()


def test_verify_failure_lists_probes_and_does_not_stamp(client, dirs, monkeypatch):
    monkeypatch.setattr(permissions, "verify", lambda p, **kw: [
        permissions.Probe("Can list old versions in the backup bucket", True),
        permissions.Probe("Can assume the role backup-engine-bucket-admin", False,
                          "Steps 3 and 5 of the script set this up — did step 3 run?")])
    body = client.post("/setup/permissions/verify", data={"csrf": _csrf(client)}).get_data(as_text=True)
    assert "✗" in body and "did step 3 run?" in body
    assert "PERMISSIONS_VERSION" not in config_io.read_backup_env(dirs["config"])


def test_commands_mode_explains_it_is_the_last_setup_step(client):
    body = client.get("/setup/permissions?mode=commands").get_data(as_text=True)
    assert "Last step of setup" in body
