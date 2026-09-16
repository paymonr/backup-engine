# tests/gui/test_about_routes.py — About = the glossary (spec 5.13) at /setup/about.
import pytest
from app.gui import create_app
from app.gui.attributions import THIRD_PARTY


@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def test_old_about_path_301s_to_setup_about(client):
    r = client.get("/about")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/setup/about")


def test_about_renders(client):
    r = client.get("/setup/about")
    assert r.status_code == 200


def test_about_is_the_tool_glossary(client):
    # 5.13: each tool explained in plain language, with an anchor id.
    body = client.get("/setup/about").get_data(as_text=True)
    assert "The tools this app drives" in body
    for term in ("restic", "rclone", "the file-history catalog", "OpenTofu", "supercronic"):
        assert term in body
    # the plain-language sentences (a couple, verbatim from 5.13)
    assert "makes the snapshot backups" in body
    assert "makes the plain copies, file for file" in body
    # anchor ids the "What these tools are →" links land on
    for anchor in ("restic", "rclone", "vfiles", "tofu", "supercronic", "tiers"):
        assert f'id="{anchor}"' in body


def test_about_lists_licences_and_project(client):
    body = client.get("/setup/about").get_data(as_text=True)
    assert "Licences" in body
    for c in THIRD_PARTY:
        assert c["name"] in body
        assert c["license"] in body


def test_about_shows_version(client):
    body = client.get("/setup/about").get_data(as_text=True)
    assert "0.1.0-dev" in body or "built" in body


def test_footer_links_to_about_on_every_page(client):
    # base.html footer is shared, so any rendering page carries the About link.
    assert b"/setup/about" in client.get("/jobs/new").data
