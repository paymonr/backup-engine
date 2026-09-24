# tests/gui/test_permissions_levels.py — the permissions level manifest + the
# PERMISSIONS_VERSION stamp (spec 2026-09-22 §1).
import json
from pathlib import Path

import pytest
from app.gui import config_io, create_app, permissions, provision


def _env(dirs, text):
    Path(dirs["config"], "backup.env").write_text(text)


def test_manifest_level_is_the_top_of_a_contiguous_history():
    levels = [h["level"] for h in permissions.history()]
    assert levels == list(range(1, len(levels) + 1))
    assert permissions.required_level() == levels[-1]
    for h in permissions.history():
        assert h["adds"] and isinstance(h["features"], list)


def test_templates_fingerprint_matches_the_manifest():
    # The bump rule: editing any provisioning/*.tmpl must bump `level`, add a history
    # line, and update `templates_sha256`. This fails until all three are done.
    assert permissions.load_manifest()["templates_sha256"] == permissions.templates_sha256(), (
        "provisioning/*.tmpl changed: bump `level` in provisioning/permissions.json, add a "
        "history entry saying what it adds, and set templates_sha256 to "
        f"{permissions.templates_sha256()}")


def test_dedicated_buckets_is_a_level_three_feature():
    assert permissions.feature_level("dedicated-buckets") == 3
    assert permissions.feature_level("no-such-feature") is None


def test_s3_rules_is_a_level_four_feature():
    assert permissions.required_level() == 4
    assert permissions.feature_level("s3-rules") == 4


def test_tofu_outputs_the_permissions_level_from_the_manifest():
    outputs = (provision.OPENTOFU_DIR / "outputs.tf").read_text()
    assert 'output "permissions_level"' in outputs
    assert "provisioning/permissions.json" in outputs


def test_no_stamp_is_unchecked(dirs):
    _env(dirs, "S3_BUCKET=acme\n")
    st = permissions.level_status(dirs["config"])
    assert st["state"] == "unchecked" and st["level"] is None and st["missing"] == []
    assert permissions.feature_available(dirs["config"], "dedicated-buckets") is False


def test_current_stamp(dirs):
    _env(dirs, f"PERMISSIONS_VERSION={permissions.required_level()}\n"
               "PERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n")
    st = permissions.level_status(dirs["config"])
    assert st["state"] == "current" and st["checked_at"] == "2026-09-22T10:00:00Z"
    assert permissions.feature_available(dirs["config"], "dedicated-buckets") is True


def test_behind_stamp_lists_what_an_update_adds(dirs):
    _env(dirs, "PERMISSIONS_VERSION=2\n")
    st = permissions.level_status(dirs["config"])
    assert st["state"] == "behind"
    assert [h["level"] for h in st["missing"]] == list(range(3, permissions.required_level() + 1))
    assert permissions.feature_available(dirs["config"], "dedicated-buckets") is False


def test_garbage_stamp_is_unchecked(dirs):
    _env(dirs, "PERMISSIONS_VERSION=three\n")
    assert permissions.current_level(dirs["config"]) is None


def test_non_decimal_digit_stamp_is_unchecked(dirs):
    # isdigit() alone accepts "²" (superscript two) -- isdecimal() does not, even
    # though int("²") would raise if it slipped through.
    _env(dirs, "PERMISSIONS_VERSION=²\n")
    assert permissions.current_level(dirs["config"]) is None


def test_non_ascii_decimal_digit_stamp_is_unchecked(dirs):
    # isdecimal() alone accepts non-ASCII decimal digits (e.g. Arabic-Indic "١")
    # -- and int() happily parses them -- so isascii() is required too.
    _env(dirs, "PERMISSIONS_VERSION=١\n")
    assert permissions.current_level(dirs["config"]) is None


def test_template_carries_the_stamp_keys(template_path):
    keys = config_io.template_keys(template_path)
    assert "PERMISSIONS_VERSION" in keys and "PERMISSIONS_CHECKED_AT" in keys


# --- the Keys page never shows the stamp, and saving it keeps the stamp ------

@pytest.fixture
def client(dirs, template_path):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    _env(dirs, "S3_BUCKET=acme\nAWS_REGION=us-east-1\n"
               f"PERMISSIONS_VERSION={permissions.required_level()}\n"
               "PERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n")
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SECRET_KEY": "test", "TESTING": True})
    return app.test_client()


def test_keys_page_does_not_render_the_stamp(client):
    body = client.get("/setup/keys").get_data(as_text=True)
    assert 'name="PERMISSIONS_VERSION"' not in body
    assert 'name="PERMISSIONS_CHECKED_AT"' not in body


def test_saving_keys_keeps_the_stamp(client, dirs):
    client.get("/setup/keys")
    with client.session_transaction() as s:
        token = s["_csrf"]
    r = client.post("/setup/keys", data={"csrf": token, "S3_BUCKET": "acme", "AWS_REGION": "us-east-1"})
    assert r.status_code in (302, 303)
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())
    assert env["PERMISSIONS_CHECKED_AT"] == "2026-09-22T10:00:00Z"


# --- clear_stamp + config_save's stale-stamp guard (review Important 2) ------

def _post_keys(client, **extra):
    client.get("/setup/keys")
    with client.session_transaction() as s:
        token = s["_csrf"]
    data = {"csrf": token, "S3_BUCKET": "acme", "AWS_REGION": "us-east-1"}
    data.update(extra)
    return client.post("/setup/keys", data=data)


def test_clear_stamp_blanks_the_stamp_and_role_arn_only(dirs, template_path):
    _env(dirs, "S3_BUCKET=acme\nAWS_REGION=us-east-1\n"
               "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::123456789012:role/backup-engine-bucket-admin\n"
               "PERMISSIONS_VERSION=3\nPERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n")
    permissions.clear_stamp(dirs["config"], template_path)
    env = config_io.read_backup_env(dirs["config"])
    assert env.get("PERMISSIONS_VERSION", "") == ""
    assert env.get("PERMISSIONS_CHECKED_AT", "") == ""
    assert env.get("BUCKET_ADMIN_ROLE_ARN", "") == ""
    assert env["S3_BUCKET"] == "acme" and env["AWS_REGION"] == "us-east-1"


def test_keys_save_with_a_changed_bucket_clears_the_stamp(client, dirs):
    r = _post_keys(client, S3_BUCKET="new-bucket")
    assert r.status_code in (302, 303)
    env = config_io.read_backup_env(dirs["config"])
    assert env.get("PERMISSIONS_VERSION", "") == ""
    assert env.get("PERMISSIONS_CHECKED_AT", "") == ""


def test_keys_save_with_a_changed_runtime_key_clears_the_stamp(client, dirs):
    r = _post_keys(client, AWS_ACCESS_KEY_ID="AKIABRANDNEWDIFFERENTKEY")
    assert r.status_code in (302, 303)
    env = config_io.read_backup_env(dirs["config"])
    assert env.get("PERMISSIONS_VERSION", "") == ""
    assert env.get("PERMISSIONS_CHECKED_AT", "") == ""


def test_keys_save_smuggled_stamp_version_is_ignored(client, dirs):
    # Same bucket, blank key -> the stamp is kept, but at the STORED value -- a
    # posted PERMISSIONS_VERSION is not a real field on this form and must never
    # be trusted, even when the stamp isn't being cleared.
    r = _post_keys(client, PERMISSIONS_VERSION="99")
    assert r.status_code in (302, 303)
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())
