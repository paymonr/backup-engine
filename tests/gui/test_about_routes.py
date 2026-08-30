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


def test_about_lists_bundled_tools_and_licenses(client):
    r = client.get("/about")
    assert r.status_code == 200
    body = r.data.lower()
    # the two the licensing question was about, plus a couple of licenses
    assert b"restic" in body and b"rclone" in body
    assert b"bsd-2-clause" in body and b"mit" in body


def test_about_renders_every_attribution(client):
    r = client.get("/about")
    for c in THIRD_PARTY:
        assert c["name"].encode() in r.data
        assert c["license"].encode() in r.data


def test_footer_links_to_about_on_every_page(client):
    # base.html footer is shared, so any page carries the About link
    assert b"/about" in client.get("/jobs").data
