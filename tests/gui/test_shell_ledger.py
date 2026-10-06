# tests/gui/test_shell_ledger.py -- the Ledger shell (spec 2026-10-04 §6), the
# stylesheet's three theme blocks, and the How it works page.
import re
from pathlib import Path
import pytest
from app.gui import create_app

ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "app" / "gui" / "static" / "style.css"
FONTS = ROOT / "app" / "gui" / "static" / "fonts"
TOKENS = ["--bg", "--surface", "--surface-2", "--side", "--ink", "--muted", "--line", "--rule",
          "--accent", "--accent-ink", "--ok", "--warn", "--warn-bg", "--danger",
          "--border", "--border-strong", "--faint", "--accent-bg", "--ok-bg", "--danger-bg",
          "--warn-border", "--danger-border", "--grid", "--ok-dim", "--warn-dim", "--danger-dim",
          "--prov-measured", "--prov-assumed", "--prov-invoiced"]


@pytest.fixture
def app(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SECRET_KEY": "test", "TESTING": True, "PRICES_LIVE": False})


def _blocks(css: str) -> dict:
    root = re.search(r"^:root\{(.*?)\n\}", css, re.S | re.M).group(1)
    media = re.search(r'@media \(prefers-color-scheme: dark\)\{\s*:root:not\(\[data-theme="light"\]\)\{(.*?)\}\s*\}', css, re.S).group(1)
    attr = re.search(r':root\[data-theme="dark"\]\{(.*?)\n\}', css, re.S).group(1)
    return {"root": root, "media": media, "attr": attr}


def test_stylesheet_defines_every_token_in_all_three_blocks():
    b = _blocks(CSS.read_text())
    for tok in TOKENS:
        for name, body in b.items():
            assert re.search(rf"{re.escape(tok)}\s*:", body), f"{tok} missing from the {name} block"
    assert "color-scheme:dark" in b["media"].replace(" ", "") and "color-scheme:dark" in b["attr"].replace(" ", "")
    assert "--accent:#8A2F3C" in b["root"].replace(" ", "") and "--accent:#D98A95" in b["attr"].replace(" ", "")


def test_stylesheet_has_no_wide_min_width_outside_scroll():
    css = CSS.read_text()
    for m in re.finditer(r"([^{}]+)\{[^{}]*min-width\s*:\s*(\d+)px", css):
        sel, px = m.group(1).strip(), int(m.group(2))
        assert px <= 360 or ".scroll" in sel or ".ledger" in sel, f"{sel} forces {px}px"


def test_fonts_are_bundled_and_declared():
    for f in ("Fraunces.ttf", "SourceSans3.ttf", "IBMPlexMono-Regular.ttf", "IBMPlexMono-Medium.ttf"):
        assert (FONTS / f).stat().st_size > 100_000, f
    css = CSS.read_text()
    assert css.count("@font-face") == 4 and "font-display:swap" in css.replace(" ", "")
    assert "fonts.googleapis" not in css


def test_shell_has_sidebar_and_one_line_footer(app):
    from flask import render_template_string
    with app.test_request_context("/"):
        html = render_template_string("{% extends 'base.html' %}{% block body %}{% endblock %}")
    assert '<nav class="screens"' in html and 'class="sidebar"' in html
    for screen in ("Board", "Cost", "Activity", "Explore", "Setup"):
        assert f">{screen}</a>" in html
    assert "How to read this screen" not in html and "States a job can be in" not in html
    assert 'href="/how-it-works"' in html and "About and licences" in html
    assert 'id="be-clock"' in html and 'id="workbar"' in html


def test_how_it_works_renders_every_anchor(app):
    body = app.test_client().get("/how-it-works").get_data(as_text=True)
    for anchor in ("jobs", "restore-points", "earlier-copies", "tiers", "numbers", "notices", "tools"):
        assert f'id="{anchor}"' in body, anchor
    assert "earlier copies" in body and "Amazon" not in body


def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    assert m, f"no rule for {selector}"
    return m.group(1).replace(" ", "")


def test_legacy_markup_keeps_working_under_the_new_core():
    # Unreworked screens still use these (review of task 2): the work strip, the Board
    # needs-row, the form footer figures, two utilities, and a danger-styled guard.
    css = CSS.read_text()
    wb = _rule(css, ".workbar, #workbar")
    assert "height:2px" in wb and "max-width:var(--maxw)" in wb
    assert "animation:slide" in _rule(css, ".workbar::after, #workbar::after")
    assert "minmax(0,1fr)170px" in _rule(css, ".needs-row")
    assert "12px1fr" in _rule(css, ".rrow")
    assert "display:inline" in _rule(css, ".formfoot .figs")
    # the Board table's 900px column swap died with the tiles (Task 4)
    assert ".compact-only" not in css and ".col-strip" not in css and ".col-took" not in css
    assert "overflow-x:auto" in _rule(css, ".tscroll")
    guard = _rule(css, ".guard")
    assert "var(--danger)" in guard and "var(--danger-bg)" in guard


def test_ofl_notice_names_every_bundled_family():
    ofl = (FONTS / "OFL.txt").read_text()
    for owner in ("Fraunces Project Authors", "Adobe", "IBM Corp"):
        assert owner in ofl, owner


def test_cost_chart_swatches_read_tokens(app):
    # app.js drawCostChart() strokeColor() reads these; without them it falls back to
    # hard-coded Night Shift colours (final fix wave C).
    css = CSS.read_text()
    assert "var(--accent)" in _rule(css, ".swatch-primary")
    assert "var(--ok)" in _rule(css, ".swatch-roll")
    assert "var(--muted)" in _rule(css, ".swatch-nover")


def test_dockerignore_drops_bak_copies_in_every_directory():
    # Docker's matcher does not cross directories for "*.bak"; "**/*.bak" does.
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert "**/*.bak" in lines


def test_job_and_cost_grids_collapse_at_mid_width():
    # At 821-1180px a kept 340px rail / 300px column squeezed the bands to ~155px.
    css = CSS.read_text().replace(" ", "")
    assert "@media(max-width:1180px){.jobgrid{grid-template-columns:1fr;}.rail{display:none;}.rail-inline{display:grid;" in css
    assert "@media(max-width:1180px){.projgrid{grid-template-columns:1fr;}}" in css
    assert "@media(max-width:820px){.jobgrid" not in css and "@media(max-width:820px){.projgrid" not in css


def test_job_form_blocked_storage_tiers_are_styled():
    # app.js toggles .struck on a blocked tier's figures; the reason shows in red.
    css = CSS.read_text()
    assert "line-through" in _rule(css, "table.classes td.struck")
    reason = _rule(css, "td .reason")
    assert "display:block" in reason and "var(--danger)" in reason
    assert "var(--faint)" in _rule(css, "table.classes tr.blocked td")
    assert "td.struck{" in css.replace(" ", "") and ".reason{" in css.replace(" ", "")


@pytest.mark.parametrize("template,title", [
    ("activity.html", "Activity"), ("run_record.html", "Record"),
    ("explore_index.html", "Your backups"), ("explore.html", "Contents"),
    ("restore.html", "Get data back")])
def test_plain_screens_use_the_band_grammar(template, title):
    # spec §6.1: the main content sits in a band with its margin title (fix wave G).
    src = (ROOT / "app" / "gui" / "templates" / template).read_text()
    assert re.search(r'<section class="band[^"]*"[^>]*>\s*<p class="slabel">' + re.escape(title) + "</p>", src), template


def test_small_ledger_rules_from_the_fix_wave():
    css = CSS.read_text()
    for sel in (".lever > legend", ".sev-tab", ".whose .colhead"):
        assert "uppercase" not in _rule(css, sel), sel
    assert "var(--accent)" in _rule(css, ".strip .cellx.running")
    phone = re.search(r"@media \(max-width:820px\)\{\s*\.shell\{[^}]*\}\s*\.sidebar\{([^}]*)\}", css)
    assert phone and "flex-wrap:wrap" in phone.group(1).replace(" ", "")


def test_job_page_and_flashes_carry_no_arrows():
    # fix wave J1: no "→" in the Job page's buttons/axis or the "watch it in Activity" flashes.
    src = re.sub(r"{#.*?#}", "", (ROOT / "app" / "gui" / "templates" / "job.html").read_text(), flags=re.S)
    assert "→" not in src
    routes = (ROOT / "app" / "gui" / "routes.py").read_text()
    assert "watch it in Activity →" not in routes and routes.count("watch it in Activity\"") == 3
    assert "border-radius:6px" not in src


def test_tables_right_align_only_numeric_cells():
    # Owner report 2026-10-06: About's tier table had right-aligned cells under left-aligned
    # headers. Right alignment is opt-in per cell (.num), never a blanket rule on a table class.
    css = CSS.read_text()
    assert not re.search(r"table\.classes th, table\.classes td\{[^}]*text-align:right", css)
    assert re.search(r"\.num\{[^}]*text-align:right", css)
