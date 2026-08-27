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

def test_includes_round_trip(dirs):
    cio.write_includes(dirs["config"], "+ /comics/**\n- **\n")
    assert cio.read_includes(dirs["config"]) == "+ /comics/**\n- **\n"

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
