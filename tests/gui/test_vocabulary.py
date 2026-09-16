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


# --- the mono law (spec 4.4 / 10.3) ----------------------------------------
# Every text node that is a bare machine fact -- a number with an optional unit,
# a money figure, a percentage, or an HH:MM clock -- must live under a mono
# ancestor. The class set is exactly the one this document's own markup uses for
# mono numbers (10.3): if a template prints a figure in a sans element, that is a
# bug the lint catches. Script/style subtrees are not rendered text and are
# dropped (their contents -- e.g. `setInterval(t, 30000)` -- are not language).
_MONO_NUM = re.compile(r"^\$?\d[\d,]*(\.\d+)?( ?(GB|MB|TB|KB|%|s|m|h|d|files))?$")
_MONO_CLOCK = re.compile(r"^\d{2}:\d{2}$")
_MONO_ANCESTOR = {
    "mono", "n", "v", "s", "t", "sub", "scrub", "num", "fig", "stamp",
    "price-stamp", "errline", "cmd", "clock", "chip", "tok", "delta", "d",
    "id", "m", "k-delta", "figs", "classes", "ledger-axis",
}


class _MonoLaw(html.parser.HTMLParser):
    SKIP_TAGS = {"script", "style"}
    VOID = _VisibleText.VOID

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.violations = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        cls = set((dict(attrs).get("class") or "").split())
        self.stack.append({"tag": tag, "cls": cls, "skip": tag in self.SKIP_TAGS})

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if any(f["skip"] for f in self.stack):
            return
        t = data.strip()
        if not t or not (_MONO_NUM.match(t) or _MONO_CLOCK.match(t)):
            return
        if not any(f["cls"] & _MONO_ANCESTOR for f in self.stack):
            self.violations.append(t)


def mono_violations(markup: str) -> list:
    p = _MonoLaw()
    p.feed(markup)
    return p.violations


# --- the full per-page sweep (spec 10.3) -----------------------------------
# Render every screen with the example content (appdata = Snapshot backup;
# manga = Plain copy on a thaw-first tier) and prove the vocabulary and mono
# laws hold on the real markup, not just the shell.
import json                                                            # noqa: E402
from datetime import datetime, timedelta, timezone                     # noqa: E402
from pathlib import Path                                               # noqa: E402
from app.engine import runs                                           # noqa: E402
from app.gui import config_io, jobs_io                                # noqa: E402
import app.gui.routes as routes                                       # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 15, 7, 42, tzinfo=UTC)   # matches the mockup clock


def _end(cache, name, rid, outcome="ok", started="2026-09-15T05:00:01Z",
         finished="2026-09-15T05:04:13Z", duration=252, kind="backup", **extra):
    runs.append_event(cache, name, {"v": 1, "id": rid, "job": name, "kind": kind,
                                    "event": "start", "trigger": "scheduled", "started_at": started})
    ev = {"v": 1, "id": rid, "job": name, "kind": kind, "event": "end", "outcome": outcome,
          "finished_at": finished, "duration_s": duration, "exit_code": 0 if outcome == "ok" else 1}
    ev.update(extra)
    runs.append_event(cache, name, ev)


def _seed_30_ok(cache, name):
    start = datetime(2026, 8, 17, 5, 0, 1, tzinfo=UTC)
    for i in range(30):
        d = start + timedelta(days=i)
        fin = d + timedelta(seconds=252)
        rid = f"{d:%Y%m%dT%H%M%S}Z-{i:04d}"
        dur = 532 if d.date() == datetime(2026, 8, 28).date() else 252
        _end(cache, name, rid, outcome="ok",
             started=f"{d:%Y-%m-%dT%H:%M:%S}Z", finished=f"{fin:%Y-%m-%dT%H:%M:%S}Z",
             duration=dur)


APPDATA_RUN = "20260915T050001Z-0029"     # newest OK appdata backup (from _seed_30_ok)
MANGA_FAIL_RUN = "20260913T040000Z-b21c"  # the failed prune run


@pytest.fixture
def full_app(tmp_path, template_path, monkeypatch):
    src = tmp_path / "src"
    (src / "appdata").mkdir(parents=True)
    (src / "media" / "manga").mkdir(parents=True)
    cfg = tmp_path / "config"; cfg.mkdir()
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True); (cache / "logs").mkdir()
    restore = tmp_path / "restore"; restore.mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek",
                                       "RESTIC_PASSWORD": "a-real-long-random-passphrase"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    app = create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(cache), "SCRIPTS_DIR": "/app/scripts",
                      "TEMPLATE_PATH": template_path, "SOURCE_ROOT": str(src),
                      "SOURCE_ROOT_HOST": "/mnt/user", "RESTORE_ROOT": str(restore),
                      "RESTORE_ROOT_HOST": "/mnt/user/restore", "SECRET_KEY": "test",
                      "TESTING": True, "PRICES_LIVE": False})
    jobs_io.upsert(str(cfg), {"name": "appdata", "type": "versioned", "source": "appdata",
                              "schedule": "0 5 * * *", "enabled": True, "storage_class": "STANDARD",
                              "created_at": "2026-09-01T00:00:00Z",
                              "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}},
                   source_root=str(src))
    jobs_io.upsert(str(cfg), {"name": "manga", "type": "archive", "source": "media/manga",
                              "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE",
                              "created_at": "2026-09-01T00:00:00Z"},
                   source_root=str(src))
    _seed_30_ok(str(cache), "appdata")
    _end(str(cache), "manga", "20260906T040000Z-0a0a", outcome="ok",
         started="2026-09-06T04:00:00Z", finished="2026-09-06T06:09:00Z", duration=7740)
    _end(str(cache), "manga", MANGA_FAIL_RUN, outcome="failed", duration=4067,
         error="AccessDenied: s3:DeleteObjectVersion", phase="prune", copied=True,
         started="2026-09-13T04:00:00Z", finished="2026-09-13T05:07:47Z")
    (Path(cache) / "state" / "appdata.points.json").write_text(json.dumps([
        {"short_id": "a81f3c2e", "time": "2026-09-15T05:00:00Z",
         "summary": {"total_bytes_processed": 56594862080, "total_files_processed": 533,
                     "files_new": 0, "files_changed": 6, "data_added": 228589824}},
        {"short_id": "7c41e9b0", "time": "2026-09-14T05:00:00Z",
         "summary": {"total_bytes_processed": 56562000000, "total_files_processed": 533,
                     "files_changed": 4, "data_added": 100663296}},
        {"short_id": "4b02fa71", "time": "2026-03-18T05:00:00Z",
         "summary": {"total_bytes_processed": 40900000000, "total_files_processed": 500,
                     "files_changed": 0, "data_added": 40900000000}},
    ]))
    (Path(cache) / "usage.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "data": {"appdata": {"bytes": 56594862080, "count": 533},
                 "media/manga": {"bytes": 1957000000000, "count": 232021}}}))
    (Path(cache) / "billing.json").write_text(json.dumps({
        "fetched_at": 1757833200.0,
        "months": [{"month": "2026-08", "amount": 3.98}], "forecast": None, "tag": None}))

    real_job = routes.status.job

    def _pinned(config_dir, cache_dir, scripts_dir, name, **kw):
        kw.setdefault("now", NOW); kw.setdefault("tz", UTC)
        return real_job(config_dir, cache_dir, scripts_dir, name, **kw)

    monkeypatch.setattr(routes.status, "job", _pinned)
    return app


# Every screen the vocabulary law covers (spec 10.3 / the brief's step 1).
ALL_PAGES = [
    "/",                              # Board
    "/jobs/appdata",                  # job page — Snapshot backup, OK
    "/jobs/manga",                    # job page — Plain copy, failed, thaw-first tier
    f"/jobs/appdata/runs/{APPDATA_RUN}",   # run record — OK
    f"/jobs/manga/runs/{MANGA_FAIL_RUN}",  # run record — failed prune
    "/activity",                      # Activity feed
    "/cost",                          # Cost workbench
    "/setup",                         # Setup readiness
    "/setup/destination",             # Destination
    "/setup/keys",                    # Keys & secrets
    "/setup/about",                   # About / glossary
    "/jobs/new",                      # Create job
    "/jobs/appdata/edit",             # Edit job
]


def _exempt_for(url: str) -> frozenset:
    """The per-page exemption set (spec 10.3): the union of every TERM_EXEMPTIONS
    entry whose URL prefix matches `url`. Every other page gets the full list."""
    exempt = set()
    for prefix, terms in vocab.TERM_EXEMPTIONS.items():
        if url == prefix or url.startswith(prefix + "/") or url.startswith(prefix + "#"):
            exempt |= terms
    return frozenset(exempt)


@pytest.mark.parametrize("url", ALL_PAGES)
def test_page_has_no_forbidden_terms(full_app, url):
    client = full_app.test_client()
    resp = client.get(url)
    assert resp.status_code == 200, f"{url} did not render (status {resp.status_code})"
    markup = resp.get_data(as_text=True)
    hits = forbidden_hits(markup, exempt=_exempt_for(url))
    assert hits == [], f"forbidden term(s) in visible text of {url}: {sorted(hits)}"


@pytest.mark.parametrize("url", ALL_PAGES)
def test_page_obeys_the_mono_law(full_app, url):
    client = full_app.test_client()
    markup = client.get(url).get_data(as_text=True)
    bad = mono_violations(markup)
    assert bad == [], f"bare number(s) in a sans element on {url}: {sorted(set(bad))}"


def test_allowed_tier_phrase_survives_on_the_cold_job(full_app):
    # spec 10.3: at least one of ALLOWED_PHRASES renders on the manga job page,
    # so a later "cleanup" cannot quietly delete the thaw-first tier name.
    text = visible_text(full_app.test_client().get("/jobs/manga").get_data(as_text=True))
    assert any(p in text for p in vocab.ALLOWED_PHRASES), \
        "the thaw-first tier name vanished from the cold job page"
