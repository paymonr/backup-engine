from pathlib import Path
from app.gui import config_io as cio

def test_template_keys_include_optional_commented_keys(template_path):
    keys = cio.template_keys(template_path)
    assert "AWS_REGION" in keys and "S3_BUCKET" in keys
    assert "S3_ENDPOINT" in keys          # commented-out optional key in the example
    assert "APPDATA_STORAGE_CLASS" in keys

def test_write_backup_env_regenerates_from_template_preserving_comments(template_path, dirs):
    cio.write_backup_env(template_path, dirs["config"], {"AWS_REGION": "us-west-2", "S3_BUCKET": "mybucket"})
    out = Path(dirs["config"], "backup.env").read_text()
    assert "AWS_REGION=us-west-2" in out
    assert "S3_BUCKET=mybucket" in out
    # a comment line from the example survives
    assert any(line.startswith("#") for line in out.splitlines())
    # round-trips through the reader
    assert cio.read_backup_env(dirs["config"])["AWS_REGION"] == "us-west-2"

def test_write_backup_env_ignores_blank_values(template_path, dirs):
    cio.write_backup_env(template_path, dirs["config"], {"S3_ENDPOINT": ""})
    out = Path(dirs["config"], "backup.env").read_text()
    # blank S3_ENDPOINT stays as the template's commented line, not an empty assignment
    assert "\nS3_ENDPOINT=\n" not in out

def test_secrets_are_write_only(dirs):
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "pw"})
    status = cio.secrets_status(dirs["config"])
    assert status == {"AWS_ACCESS_KEY_ID": True, "AWS_SECRET_ACCESS_KEY": True, "RESTIC_PASSWORD": True}
    # blank leaves existing unchanged; non-blank overwrites
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "new", "RESTIC_PASSWORD": ""})
    from app.gui.config_io import _parse_env
    vals = _parse_env(Path(dirs["config"], "secrets.env").read_text())
    assert vals["AWS_ACCESS_KEY_ID"] == "AKIA"       # unchanged
    assert vals["AWS_SECRET_ACCESS_KEY"] == "new"    # overwritten
    assert cio.secrets_mode(dirs["config"]) == "600"

def test_secrets_status_absent_file(dirs):
    assert cio.secrets_status(dirs["config"]) == {k: False for k in cio.SECRET_KEYS}
    assert cio.secrets_mode(dirs["config"]) is None

def test_secret_value_containing_hash_survives_blank_keeps_existing(dirs):
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "p#ssw0rd"})
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "x", "RESTIC_PASSWORD": ""})
    from app.gui.config_io import _parse_env
    vals = _parse_env(Path(dirs["config"], "secrets.env").read_text())
    assert vals["RESTIC_PASSWORD"] == "p#ssw0rd"

def test_secret_value_with_space_and_hash_survives_blank_keeps_existing_verbatim(dirs):
    # A secret containing " #" would have its "comment" stripped by _parse_env's
    # inline-comment logic. write_secrets must preserve it verbatim on a
    # blank-keeps-existing round trip.
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "pa ss #word"})
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "x", "RESTIC_PASSWORD": ""})
    vals = cio._read_secrets_raw(Path(dirs["config"], "secrets.env"))
    assert vals["RESTIC_PASSWORD"] == "pa ss #word"

def test_secret_value_with_wrapping_quotes_survives_verbatim(dirs):
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": '"quoted#val"'})
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "x", "RESTIC_PASSWORD": ""})
    vals = cio._read_secrets_raw(Path(dirs["config"], "secrets.env"))
    assert vals["RESTIC_PASSWORD"] == '"quoted#val"'

def test_secret_value_with_leading_trailing_spaces_survives_verbatim(dirs):
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "  spaced  "})
    cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "x", "RESTIC_PASSWORD": ""})
    vals = cio._read_secrets_raw(Path(dirs["config"], "secrets.env"))
    assert vals["RESTIC_PASSWORD"] == "  spaced  "

def test_write_secrets_rejects_newline_in_value(dirs):
    import pytest
    with pytest.raises(ValueError):
        cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA\nEVIL=1", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "pw"})
    with pytest.raises(ValueError):
        cio.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA\rEVIL=1", "AWS_SECRET_ACCESS_KEY": "shh", "RESTIC_PASSWORD": "pw"})

def test_write_backup_env_neutralizes_newline_in_value(template_path, dirs):
    cio.write_backup_env(template_path, dirs["config"], {"AWS_REGION": "us-west-2\nEVIL=1", "S3_BUCKET": "mybucket"})
    out = Path(dirs["config"], "backup.env").read_text()
    assert "EVIL=1" not in out.splitlines()
    assert not any(line.startswith("EVIL=") for line in out.splitlines())
    assert "AWS_REGION=us-west-2 EVIL=1" in out

def test_parse_env_strips_real_inline_comment_keeping_embedded_spaces(dirs):
    from app.gui.config_io import _parse_env
    vals = _parse_env("APPDATA_SCHEDULE=0 3 * * *      # nightly\n")
    assert vals["APPDATA_SCHEDULE"] == "0 3 * * *"
