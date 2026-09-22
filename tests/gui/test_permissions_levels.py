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


def test_template_carries_the_stamp_keys(template_path):
    keys = config_io.template_keys(template_path)
    assert "PERMISSIONS_VERSION" in keys and "PERMISSIONS_CHECKED_AT" in keys


# --- the Keys page never shows the stamp, and saving it keeps the stamp ------

@pytest.fixture
def client(dirs, template_path):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    _env(dirs, "S3_BUCKET=acme\nAWS_REGION=us-east-1\n"
               "PERMISSIONS_VERSION=3\nPERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n")
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
    assert env["PERMISSIONS_VERSION"] == "3"
    assert env["PERMISSIONS_CHECKED_AT"] == "2026-09-22T10:00:00Z"
