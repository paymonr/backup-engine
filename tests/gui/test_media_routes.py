import pytest
from pathlib import Path
from app.gui import create_app

@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    (root / "Movies" / "4k").mkdir(parents=True)
    (root / "Photos").mkdir()
    return root

@pytest.fixture
def app(tmp_path, media, template_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    return create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache"),
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "MEDIA_ROOT": str(media), "SECRET_KEY": "test", "TESTING": True})

@pytest.fixture
def client(app):
    return app.test_client()

def _csrf(client):
    client.get("/media")
    with client.session_transaction() as s:
        return s["_csrf"]

def test_media_page_renders_tree(client):
    r = client.get("/media")
    assert r.status_code == 200
    assert b"media-tree" in r.data

def test_browse_returns_top_level(client):
    names = [e["name"] for e in client.get("/media/browse?path=").get_json()["entries"]]
    assert "Movies" in names and "Photos" in names

def test_browse_nested(client):
    assert client.get("/media/browse?path=Movies").get_json()["entries"] == [{"name": "4k", "path": "Movies/4k"}]

def test_browse_rejects_traversal_without_echo(client):
    r = client.get("/media/browse?path=../../etc")
    assert r.status_code == 404
    assert b"etc" not in r.data

def test_save_folders_writes_include_list(client, app):
    token = _csrf(client)
    r = client.post("/media", data={"csrf": token, "folder": ["Movies", "Photos"]})
    assert r.status_code in (302, 303)
    content = Path(app.config["CONFIG_DIR"], "media-includes.txt").read_text()
    assert "+ /Movies/**" in content and "+ /Photos/**" in content

def test_save_whole(client, app):
    token = _csrf(client)
    client.post("/media", data={"csrf": token, "whole": "1"})
    assert Path(app.config["CONFIG_DIR"], "media-includes.txt").read_text() == "+ /**\n"

def test_save_nothing_selected_backs_up_nothing(client, app):
    token = _csrf(client)
    client.post("/media", data={"csrf": token})
    assert Path(app.config["CONFIG_DIR"], "media-includes.txt").read_text() == "- **\n"

def test_save_requires_csrf(client):
    assert client.post("/media", data={"whole": "1"}).status_code == 400

def test_save_rejects_bad_folder(client):
    token = _csrf(client)
    assert client.post("/media", data={"csrf": token, "folder": "../evil"}).status_code == 400

def test_nav_has_media_link(client):
    assert b"/media" in client.get("/config").data
