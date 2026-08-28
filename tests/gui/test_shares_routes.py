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
