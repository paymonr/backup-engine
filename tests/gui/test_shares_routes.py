import os
import shutil
import pytest
from pathlib import Path
from app.gui import create_app

@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    (root / "comics" / "manga").mkdir(parents=True)
    (root / "books").mkdir()
    return root

@pytest.fixture
def app(tmp_path, media, template_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    return create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache"),
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "MEDIA_ROOT": str(media), "MEDIA_SHARES_DIR": str(cfg / "media-shares"),
                       "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app):
    return app.test_client()

def _csrf(client):
    client.get("/shares")
    with client.session_transaction() as s:
        return s["_csrf"]

def test_shares_page_lists_shares(client):
    r = client.get("/shares")
    assert r.status_code == 200
    assert b"comics" in r.data and b"books" in r.data

def test_browse_returns_json_subdirs(client):
    r = client.get("/shares/browse?share=comics&path=")
    assert r.status_code == 200
    assert r.get_json() == {"entries": [{"name": "manga", "path": "manga"}]}

def test_browse_rejects_traversal_without_echo(client):
    r = client.get("/shares/browse?share=comics&path=../../etc")
    assert r.status_code == 404
    assert b"etc" not in r.data

def test_browse_rejects_bad_share_name(client):
    assert client.get("/shares/browse?share=../evil&path=").status_code == 404

def test_browse_rejects_escaping_symlink_share(client, app):
    # A symlink DIRECTLY under MEDIA_ROOT whose name passes valid_name must not
    # let the browse root escape MEDIA_ROOT (confinement, spec §7 invariant 1).
    media = Path(app.config["MEDIA_ROOT"])
    outside = media.parent / "outside"
    (outside / "topsecret").mkdir(parents=True)
    os.symlink(outside, media / "evil")
    r = client.get("/shares/browse?share=evil&path=")
    assert r.status_code == 404
    assert b"topsecret" not in r.data  # no path echo either

def test_save_rejects_escaping_symlink_share(client, app):
    # Enabling a symlink-share would make backup-media.sh rclone outside-root data
    # to S3; the escaping symlink must be rejected before any file is written.
    media = Path(app.config["MEDIA_ROOT"])
    outside = media.parent / "outside2"
    outside.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, media / "evil2")
    token = _csrf(client)
    r = client.post("/shares/evil2", data={"csrf": token, "enabled": "1", "whole": "1"})
    assert r.status_code == 404
    assert not Path(app.config["MEDIA_SHARES_DIR"], "evil2.txt").exists()

def test_save_enable_writes_file(client, app):
    token = _csrf(client)
    r = client.post("/shares/comics", data={"csrf": token, "enabled": "1", "whole": "1"})
    assert r.status_code in (302, 303)
    assert Path(app.config["MEDIA_SHARES_DIR"], "comics.txt").read_text() == "+ /**\n"

def test_save_with_folders(client, app):
    token = _csrf(client)
    client.post("/shares/comics", data={"csrf": token, "enabled": "1", "folder": ["manga"]})
    assert "+ /manga/**" in Path(app.config["MEDIA_SHARES_DIR"], "comics.txt").read_text()

def test_save_disable_deletes_file(client, app):
    token = _csrf(client)
    client.post("/shares/comics", data={"csrf": token, "enabled": "1", "whole": "1"})
    client.post("/shares/comics", data={"csrf": token})  # enabled absent => disable
    assert not Path(app.config["MEDIA_SHARES_DIR"], "comics.txt").exists()

def test_save_requires_csrf(client):
    assert client.post("/shares/comics", data={"enabled": "1"}).status_code == 400

def test_save_unknown_share_is_404(client):
    token = _csrf(client)
    assert client.post("/shares/nope", data={"csrf": token, "enabled": "1"}).status_code == 404

def test_save_rejects_newline_in_folder(client, app):
    # media_shares.write_selection must reject a folder value containing a
    # newline before it is ever written into the rclone filter file.
    token = _csrf(client)
    r = client.post("/shares/comics", data={"csrf": token, "enabled": "1", "folder": ["manga\nevil"]})
    assert r.status_code == 400
    assert not Path(app.config["MEDIA_SHARES_DIR"], "comics.txt").exists()

def test_save_enable_requires_dir_but_disable_survives_missing_source(client, app):
    # Enabling still requires an existing source dir under MEDIA_ROOT. But once
    # a share is enabled, its source dir may later vanish (unmounted/removed);
    # disabling it (deleting the orphaned filter file) must still succeed so the
    # GUI stays the recovery path even when backup-media.sh would now fail.
    token = _csrf(client)
    client.post("/shares/comics", data={"csrf": token, "enabled": "1", "whole": "1"})
    assert Path(app.config["MEDIA_SHARES_DIR"], "comics.txt").exists()
    shutil.rmtree(Path(app.config["MEDIA_ROOT"]) / "comics")
    r_enable = client.post("/shares/comics", data={"csrf": token, "enabled": "1", "whole": "1"})
    assert r_enable.status_code == 404
    r_disable = client.post("/shares/comics", data={"csrf": token})  # enabled absent => disable
    assert r_disable.status_code in (302, 303)
    assert not Path(app.config["MEDIA_SHARES_DIR"], "comics.txt").exists()
