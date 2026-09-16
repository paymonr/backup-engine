# tests/gui/test_vocabulary.py -- the one-name-per-concept lint (spec 4.3, 10.3).
#
# SHELL SCOPE (task 7b): the shared chrome in base.html -- topbar, nav, work
# bar, notices, footer -- must carry no forbidden term in its visible text.
# The full per-page sweep (every screen with the example fixture, plus the mono
# law and ALLOWED_PHRASES) is Task 15; this module ships the reusable extractor
# and pins the shell so the redesign starts clean.
import re
import html.parser
import pytest
from app.gui import create_app, vocab


def _cfg(dirs, template_path):
    return {"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
            "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
            "SECRET_KEY": "test", "TESTING": True, "PRICES_LIVE": False}


@pytest.fixture
def app(dirs, template_path):
    return create_app(_cfg(dirs, template_path))


# --- the extractor (spec 10.3 "What is searched") --------------------------
# Visible text only: drop the whole subtree of <code>, <pre>, <script>,
# <style>, .term, .cmd, .errline, details.tooldetail, and any span.mono whose
# text is an env-key name (^[A-Z0-9_]+$). Attributes are never searched.
class _VisibleText(html.parser.HTMLParser):
    SKIP_TAGS = {"code", "pre", "script", "style"}
    SKIP_CLASSES = {"term", "cmd", "errline", "tooldetail"}
    VOID = {"br", "img", "input", "meta", "link", "hr", "area", "base",
            "col", "embed", "source", "track", "wbr"}
    ENV_KEY = re.compile(r"^[A-Z0-9_]+$")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []  # one frame per open, non-void element

    def _skipping(self):
        return any(f["skip"] for f in self.stack)

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        cls = set((dict(attrs).get("class") or "").split())
        self.stack.append({
            "tag": tag,
            "skip": tag in self.SKIP_TAGS or bool(cls & self.SKIP_CLASSES),
            "mono": tag == "span" and "mono" in cls,
            "buf": [],
        })

    def handle_startendtag(self, tag, attrs):
        pass  # self-closing (svg paths etc.): no text, push nothing

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                frame = self.stack[i]
                del self.stack[i:]  # also discard any deeper unclosed frames
                if frame["mono"] and not frame["skip"] and not self._skipping():
                    text = "".join(frame["buf"]).strip()
                    if not self.ENV_KEY.match(text):
                        self.parts.append(" " + text + " ")
                return

    def handle_data(self, data):
        if self._skipping():
            return
        for f in reversed(self.stack):
            if f["mono"]:
                f["buf"].append(data)
                return
        self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def visible_text(markup: str) -> str:
    p = _VisibleText()
    p.feed(markup)
    return p.text()


def forbidden_hits(markup: str, exempt=frozenset()) -> list:
    text = visible_text(markup)
    return [t for t in (vocab.FORBIDDEN_TERMS - set(exempt))
            if re.search(rf"\b{re.escape(t)}\b", text)]


def render_shell(app) -> str:
    # base.html with an empty body block == the pure shared shell.
    from flask import render_template_string
    with app.test_request_context("/"):
        return render_template_string(
            "{% extends 'base.html' %}{% block body %}{% endblock %}")


# --- the shell is clean ----------------------------------------------------

def test_shell_text_has_no_forbidden_terms(app):
    hits = forbidden_hits(render_shell(app))
    assert hits == [], f"forbidden term(s) in base.html shell text: {hits}"


def test_vocab_is_a_jinja_global(app):
    # Templates reach plain names through the `vocab` global (spec 4.3/4.4).
    assert app.jinja_env.globals.get("vocab") is vocab


# --- the extractor obeys the 10.3 rules (guards the lint itself) -----------

def test_matcher_is_case_sensitive_whole_word():
    # bare lowercase word banned...
    assert "restic" in forbidden_hits("<p>runs restic nightly</p>")
    # ...but the uppercase env key is not a hit (case-sensitive),
    assert "restic" not in forbidden_hits("<p>RESTIC_PASSWORD</p>")
    # ...and it must be a whole word.
    assert "prune" not in forbidden_hits("<p>pruned branches everywhere</p>")


def test_code_and_term_subtrees_are_dropped():
    assert forbidden_hits("<code>restic snapshots</code>") == []
    assert forbidden_hits('<span class="term">rclone</span>') == []
    assert forbidden_hits('<span class="mono">RESTIC_REPOSITORY</span>') == []


def test_per_page_exemptions_are_honoured():
    markup = "<p>the restic repository</p>"
    # checked against the full list -> both fire
    assert set(forbidden_hits(markup)) == {"restic", "repository"}
    # the Keys screen exempts exactly those two (env-key readouts, 5.12)
    assert forbidden_hits(markup, exempt=vocab.TERM_EXEMPTIONS["/setup/keys"]) == []
