# Plain words + Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every daily screen says what it needs to in labels and figures with at most three one-sentence hints, four jargon words become plain ones, the S3 mechanics live on one "How it works" page, and the whole app wears the Ledger look (Linen & wine, left sidebar, one 1440px content column, light and dark following the device), with nothing behind the screens changing.

**Architecture:** Flask + Jinja templates under `app/gui/templates/`, one stylesheet `app/gui/static/style.css`, one script `app/gui/static/app.js` whose hooks (ids and `data-*` attributes) must survive untouched. The vocabulary module `app/gui/vocab.py` is the single source of user-facing names and `tests/gui/test_vocabulary.py` is the lint that enforces it; this plan extends both before touching any screen, so every later task is driven by failing tests. Templates are reworked one screen per task; the stylesheet is rewritten once (Task 2) with the old screen-specific sections carried over verbatim so no screen loses styling while it waits its turn.

**Tech Stack:** Python 3 / Flask / Jinja2, pytest (`python3 -m pytest -q`), plain CSS with custom properties, vanilla JS, three OFL fonts bundled as static files.

**Spec:** `docs/superpowers/specs/2026-10-04-plain-words-ledger-design.md` — read §2 (decisions), §3 (vocabulary), §4 (budget), §6 (visual system) before any task.

## Global Constraints

- Daily screens = Board (`/`), Jobs list (`/jobs`), Job (`/jobs/<name>`), Activity (`/activity`), run record (`/jobs/<name>/runs/<id>`), Explore (`/explore`, `/explore/<name>`), Restore (`/jobs/<name>/restore`), Cost (`/cost`), and the job form (`/jobs/new`, `/jobs/<name>/edit`) for words only. Setup screens keep their technical names (spec §3).
- Vocabulary changes are exactly: **earlier copies** (for old versions / noncurrent versions / delete markers), **AWS** (never Amazon), **temporary folder** (never scratch folder), **estimate / what we assumed / how much changes each month** (never "The model says", "assumption(s)", "change rate"). Every other current name stays (spec §3, D6).
- Per daily template: at most **3** elements with class `hint`; safety sentences use class `safe` (don't count); provenance cells use class `when` (don't count); the collapsed tool detail and the footer don't count (spec §4).
- Tone is terse: no sentence under a section heading, no `→` appended to link text, no all-caps labels (spec §5).
- One content width: `max-width:1440px` on every screen; sidebar 210px; breakpoints 820px (sidebar → top row; rail above main) and 640px (band margin collapses) (spec §6.1, D12).
- Fonts are bundled under `app/gui/static/fonts/`; no `fonts.googleapis.com` anywhere in the app (spec §6.2).
- Tokens are the Linen & wine values in spec §6.3, light on bare `:root`, dark under `@media (prefers-color-scheme: dark)` guarded as `:root:not([data-theme="light"])` and again under `:root[data-theme="dark"]`, both with `color-scheme: dark`.
- `app.js` keeps every id and `data-*` hook it reads today (`data-progress-job`, `data-when`, `data-copy`, `data-copy-target`, `data-est*`, `data-keep*`, `data-cls-*`, `data-explore-job`, `data-suggest-cron`, `data-toggle`, `data-warn*`, `data-was`, `data-fmt`, `data-field`, `data-fix-*`, `data-job`, ids `be-clock`, `workbar`, `cost-timeline`, `est-form`, `est-error`, `explore-pane`, `job-form`, `log`, `proj-data`, `saved-cmp`, `sched-*`, `source-*`, `bucket-prefix-hint`). The `.swatch-nover`, `.swatch-roll`, `.swatch-primary` classes must keep a background colour (the chart reads them).
- The mono law (`tests/gui/test_vocabulary.py::_MONO_ANCESTOR`) stays: bare figures live under `mono`, `v`, `s`, `fig`, `n`, `num`, `stamp`, `tok`, `d`, `m`, `delta`, `chip`, `clock` or the other listed classes. Reuse those class names in new markup.
- Estimator math (`app/estimator/`), JSON endpoints, persisted files, `jobs.json` shapes: untouched. No AWS calls on GET; every POST CSRF-checked (unchanged).
- Safety copy: tag `ui-before-plain-words` before the first template change; every changed template and `style.css` keeps a `.bak` sibling; `*.bak` in `.dockerignore` (spec §7).
- Commit after every task's green run; messages without attribution lines.

## Review Focus

1. **A job on a Thaw-first tier** (`/jobs/manga` in the lint fixture): the Job page must still show the tier phrase "Thaw first, hours" (spec keeps it) and the Get data back lead must still warn that a restore from that tier takes hours. Pinned in Task 3 (`test_cold_job_keeps_tier_phrase_and_thaw_warning`).
2. **A failed run on the Board** (Needs you non-empty): the verdict rule must turn red, the Needs you row must link to the record, and the tile must sort first. Pinned in Task 4 (`test_board_failed_job_sorts_first_with_red_verdict`).
3. **Dark theme without `data-theme`** (the default for most browsers): every token must exist on bare `:root` and be redefined in both dark blocks, or a colour silently stays light. Pinned in Task 2 (`test_stylesheet_defines_every_token_in_all_three_blocks`).
4. **Phone width** (400px): the sidebar must become a top row, the Job rail must come above the main column, and nothing may force horizontal scroll (no `min-width` wider than 360px outside `.scroll`). Pinned in Task 2 (`test_stylesheet_has_no_wide_min_width_outside_scroll`) and Task 3 (`test_job_page_rail_markup_order`).
5. **The storage summary line in Activity** after this change must say "earlier copies" and never "old versions", because the Activity screen is daily (spec §3). Pinned in Task 5 (existing `test_describe_*` strings updated + `test_describe_never_says_old_versions`).

---

### Task 1: Safety copy, vocabulary strings, and the two failing lint rules

**Files:**
- Modify: `.dockerignore`
- Create: `app/gui/templates/*.html.bak`, `app/gui/static/style.css.bak` (copies)
- Modify: `app/gui/vocab.py`
- Modify: `tests/gui/test_vocabulary.py` (append)

**Interfaces:**
- Produces: `vocab.EARLIER_COPIES = "earlier copies"`, `vocab.AWS = "AWS"`, `vocab.TEMP_FOLDER = "temporary folder"`, `vocab.ESTIMATE = "Estimate"`, `vocab.ASSUMED = "What we assumed"`, `vocab.CHANGE_EACH_MONTH = "How much changes each month"`, `vocab.DAILY_FORBIDDEN_TERMS: set[str]`, `vocab.DAILY_TEMPLATES: list[str]`, `vocab.HINT_BUDGET = 3`; test helpers `daily_hits(markup)`, `hint_count(template_name)`.

- [ ] **Step 1: Tag and copy the originals**

```bash
cd /home/paymon/src/backup-engine
git tag ui-before-plain-words
for f in app/gui/templates/*.html app/gui/static/style.css; do cp "$f" "$f.bak"; done
printf '\n# temporary safety copies of the pre-Ledger templates (spec 2026-10-04 §7)\n*.bak\n' >> .dockerignore
ls app/gui/templates/*.bak | wc -l    # expected: 24
```

- [ ] **Step 2: Add the vocabulary strings and the daily lists**

Append to `app/gui/vocab.py`:

```python
# --- Plain words (spec 2026-10-04 §3, decisions D6) -------------------------
# The four changes the owner approved. Templates and Python that print these
# concepts on a DAILY screen import them from here; everything else keeps its
# current name (restore point, undo window, bucket, S3, tier phrases...).
EARLIER_COPIES = "earlier copies"          # old versions / noncurrent versions / delete markers
AWS = "AWS"                                # never "Amazon" as the actor or the biller
TEMP_FOLDER = "temporary folder"           # never "scratch folder"
ESTIMATE = "Estimate"                      # the Cost/Board figure formerly "The model says"
ASSUMED = "What we assumed"                # formerly "Assumptions"
CHANGE_EACH_MONTH = "How much changes each month"   # formerly "change rate"

# Banned on daily screens only (case-sensitive, whole word; Setup is exempt).
DAILY_FORBIDDEN_TERMS: set[str] = {
    "old version", "old versions", "delete marker", "delete markers", "noncurrent",
    "Amazon", "scratch folder", "The model says", "assumption", "assumptions", "change rate",
}

# Daily templates and their hint budget (spec §4). The budget counts elements with
# class "hint" in the template SOURCE; "safe" and "when" are separate classes.
DAILY_TEMPLATES: list[str] = [
    "board.html", "jobs.html", "job.html", "activity.html", "run_record.html",
    "explore.html", "explore_index.html", "restore.html", "cost.html", "job_form.html",
]
HINT_BUDGET = 3
```

Also add the How it works page to the exemptions (it names restic/rclone in its last section, like About):

```python
TERM_EXEMPTIONS["/how-it-works"] = set(FORBIDDEN_TERMS)
```

(put this line directly after the `TERM_EXEMPTIONS` dict).

- [ ] **Step 3: Write the two failing lint rules**

Append to `tests/gui/test_vocabulary.py`:

```python
# --- Plain words (spec 2026-10-04 §3/§4) -----------------------------------
# Two more rules, daily screens only: the plain-word banned list on rendered
# pages, and a hint budget on template source. Both fail against today's
# templates on purpose; each screen task turns its own pages green.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "gui" / "templates"

DAILY_PAGES = [
    "/", "/jobs", "/jobs/appdata", "/jobs/manga",
    f"/jobs/appdata/runs/{APPDATA_RUN}", f"/jobs/manga/runs/{MANGA_FAIL_RUN}",
    "/activity", "/explore", "/cost", "/jobs/new", "/jobs/appdata/edit",
    "/jobs/appdata/restore", "/jobs/manga/restore",
]


def daily_hits(markup: str) -> list:
    text = visible_text(markup)
    return sorted(t for t in vocab.DAILY_FORBIDDEN_TERMS if re.search(rf"\b{re.escape(t)}\b", text))


def hint_count(template_name: str) -> int:
    src = (TEMPLATES_DIR / template_name).read_text()
    src = re.sub(r"{#.*?#}", "", src, flags=re.S)          # template comments are not markup
    return len(re.findall(r'<(?:p|span|div|small|td|li)\s+class="hint(?:\s|")', src))


@pytest.mark.parametrize("url", DAILY_PAGES)
def test_daily_page_uses_plain_words(full_app, url):
    resp = full_app.test_client().get(url)
    assert resp.status_code == 200, f"{url} did not render (status {resp.status_code})"
    hits = daily_hits(resp.get_data(as_text=True))
    assert hits == [], f"jargon on daily screen {url}: {hits}"


@pytest.mark.parametrize("template", vocab.DAILY_TEMPLATES)
def test_daily_template_is_within_the_hint_budget(template):
    n = hint_count(template)
    assert n <= vocab.HINT_BUDGET, f"{template} has {n} hints; the budget is {vocab.HINT_BUDGET}"


def test_daily_pages_never_append_arrows_to_links(full_app):
    # Tone (spec §5): no "→" glued to link text on daily screens.
    for url in DAILY_PAGES:
        markup = full_app.test_client().get(url).get_data(as_text=True)
        assert not re.search(r">[^<]*→\s*</a>", markup), f"arrow on a link on {url}"


def test_no_google_fonts_anywhere():
    bad = [p for p in TEMPLATES_DIR.glob("*.html") if "fonts.googleapis.com" in p.read_text()]
    css = (TEMPLATES_DIR.parent / "static" / "style.css").read_text()
    assert bad == [] and "fonts.googleapis.com" not in css
```

- [ ] **Step 4: Run the new tests to verify they fail for the right reasons**

Run: `python3 -m pytest -q tests/gui/test_vocabulary.py -k "plain_words or hint_budget or arrows or google_fonts" 2>&1 | tail -15`
Expected: `test_daily_template_is_within_the_hint_budget[job.html]` fails (job.html has roughly 25 hints today; board, restore, explore, cost and jobs are over budget too); `test_daily_page_uses_plain_words` fails for `/`, `/jobs/appdata`, `/jobs/manga`, `/cost`, `/jobs/new`, `/jobs/appdata/edit`, `/jobs/manga/restore` naming `Amazon`, `old versions`, `The model says`, `assumption`; `test_daily_pages_never_append_arrows_to_links` fails; `test_no_google_fonts_anywhere` passes.

- [ ] **Step 5: Run the rest of the suite to confirm nothing else moved**

Run: `python3 -m pytest -q --deselect tests/gui/test_vocabulary.py 2>&1 | tail -3`
Expected: all passed (1982 before this plan; the number only grows).

- [ ] **Step 6: Commit**

```bash
git add .dockerignore app/gui/vocab.py tests/gui/test_vocabulary.py app/gui/templates/*.bak app/gui/static/style.css.bak
git commit -m "plain-words: safety copies, the four plain names, and the daily-screen lint rules (failing on purpose)"
```

---

### Task 2: Fonts, the Ledger stylesheet, the sidebar shell, and the How it works page

**Files:**
- Create: `app/gui/static/fonts/Fraunces.ttf`, `SourceSans3.ttf`, `IBMPlexMono-Regular.ttf`, `IBMPlexMono-Medium.ttf`, `app/gui/static/fonts/OFL.txt`
- Modify: `app/gui/attributions.py:6-20`
- Rewrite: `app/gui/static/style.css` (core new; sections carried over from `style.css.bak`)
- Rewrite: `app/gui/templates/base.html`
- Create: `app/gui/templates/how_it_works.html`
- Modify: `app/gui/routes.py:23-35` (add the route next to `about_page`)
- Test: `tests/gui/test_shell_ledger.py` (new), `tests/gui/test_vocabulary.py` (shell test already there), `tests/gui/test_about_routes.py`

**Interfaces:**
- Produces: route `gui.how_it_works` at `/how-it-works` rendering `how_it_works.html` with anchors `#jobs #restore-points #earlier-copies #tiers #numbers #notices #tools`; Jinja macro file-less convention for the "?" mark: `<a class="qm" href="/how-it-works#<anchor>" aria-label="How it works">?</a>`; CSS classes `.shell .sidebar .screens .content .band .band-head .slabel .statusstrip .sfig .grid4 .tiles .tile .strip .cellx .bars .bar .ledger-axis .tok .sig .btn .linklike .defgrid .rail .rail-inline .rrow .dot .qm .safe .when .hint .lead .sitefoot .verdict .menu .menu-pop`.

- [ ] **Step 1: Write the failing shell tests**

Create `tests/gui/test_shell_ledger.py`:

```python
# tests/gui/test_shell_ledger.py -- the Ledger shell (spec 2026-10-04 §6), the
# stylesheet's three theme blocks, and the How it works page.
import re
from pathlib import Path
import pytest
from app.gui import create_app, vocab

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
```

Add to `tests/gui/test_about_routes.py` (end of file):

```python
def test_about_lists_the_bundled_fonts(client):
    body = client.get("/setup/about").get_data(as_text=True)
    for name in ("Fraunces", "Source Sans 3", "IBM Plex Mono"):
        assert name in body and "OFL-1.1" in body
```

(if that file's client fixture is named differently, use its name; it renders `/setup/about` already.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest -q tests/gui/test_shell_ledger.py tests/gui/test_about_routes.py 2>&1 | tail -8`
Expected: FAIL — tokens missing (`--side`), fonts missing, sidebar missing, `/how-it-works` 404, fonts not on About.

- [ ] **Step 3: Bundle the fonts and credit them**

```bash
cd /home/paymon/src/backup-engine && mkdir -p app/gui/static/fonts
R=https://raw.githubusercontent.com/google/fonts/main/ofl
curl -sSL -o app/gui/static/fonts/Fraunces.ttf "$R/fraunces/Fraunces%5BSOFT%2CWONK%2Copsz%2Cwght%5D.ttf"
curl -sSL -o app/gui/static/fonts/SourceSans3.ttf "$R/sourcesans3/SourceSans3%5Bwght%5D.ttf"
curl -sSL -o app/gui/static/fonts/IBMPlexMono-Regular.ttf "$R/ibmplexmono/IBMPlexMono-Regular.ttf"
curl -sSL -o app/gui/static/fonts/IBMPlexMono-Medium.ttf "$R/ibmplexmono/IBMPlexMono-Medium.ttf"
curl -sSL -o app/gui/static/fonts/OFL.txt "$R/fraunces/OFL.txt"
ls -la app/gui/static/fonts/     # Fraunces ~360 KB, SourceSans3 ~646 KB, Plex ~136 KB each
```

Add to `THIRD_PARTY` in `app/gui/attributions.py` (after the Flask entry):

```python
    {"name": "Fraunces", "license": "OFL-1.1", "url": "https://github.com/undercasetype/Fraunces",
     "role": "GUI typeface (titles and figures)"},
    {"name": "Source Sans 3", "license": "OFL-1.1", "url": "https://github.com/adobe-fonts/source-sans",
     "role": "GUI typeface (text)"},
    {"name": "IBM Plex Mono", "license": "OFL-1.1", "url": "https://github.com/IBM/plex",
     "role": "GUI typeface (paths, ids, commands)"},
```

- [ ] **Step 4: Write the new stylesheet core**

Replace `app/gui/static/style.css` with the following, then append the carried-over sections in Step 5.

```css
/* app/gui/static/style.css — backup-engine, the Ledger design (spec 2026-10-04 §6).
   Linen & wine. Light on bare :root; dark under prefers-color-scheme (guarded so an
   explicit data-theme="light" wins) and again under data-theme="dark". Legacy token
   names (--border, --faint, --ok-bg ...) are kept so the screen-specific sections
   carried over from style.css.bak keep working. The previous stylesheet is
   style.css.bak (temporary, spec §7). */

/* ============================================================
   1. Tokens
   ============================================================ */
:root{
  color-scheme:light;
  --bg:#F2EFE8; --surface:#FAF8F3; --surface-2:#E8E3D9; --side:#E9E5DC;
  --ink:#2B2522; --ink-strong:#1A1512; --muted:#6A615B; --faint:#8C827B;
  --line:#D6D0C6; --rule:#B8B0A4; --border:#D6D0C6; --border-strong:#B8B0A4; --grid:#E3DDD2;
  --accent:#8A2F3C; --accent-hover:#A3414F; --accent-ink:#FBF4F2; --accent-bg:#F4E4E6;
  --ok:#2E7D5B; --ok-bg:#E4F0E9; --ok-dim:#8FBFA8;
  --warn:#B07A1B; --warn-bg:#F3E8D2; --warn-border:#D9C08A; --warn-dim:#D4B77A;
  --danger:#A8392F; --danger-bg:#F5E1DE; --danger-border:#DDA9A2; --danger-dim:#D39A93;
  --prov-measured:rgba(46,125,91,.55); --prov-assumed:rgba(176,122,27,.55); --prov-invoiced:rgba(138,47,60,.55);
  --radius:3px; --radius-sm:2px;
  --font-display:"Fraunces",Georgia,"Times New Roman",serif;
  --font-sans:"Source Sans 3",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --font-mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sp-1:.25rem; --sp-2:.5rem; --sp-3:.75rem; --sp-4:1rem; --sp-5:1.25rem; --sp-6:1.5rem; --sp-8:2rem;
  --gutter:clamp(16px,3vw,40px); --maxw:1440px; --sidebar:210px; --margin:170px;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --bg:#1B1715; --surface:#221D1A; --surface-2:#2B2520; --side:#130F0E;
    --ink:#ECE6E0; --ink-strong:#F7F3EE; --muted:#A79E96; --faint:#8E857D;
    --line:#352E29; --rule:#463D37; --border:#352E29; --border-strong:#463D37; --grid:#2F2924;
    --accent:#D98A95; --accent-hover:#E5A3AC; --accent-ink:#1F1012; --accent-bg:#3A2226;
    --ok:#5BBE8E; --ok-bg:#1B2B23; --ok-dim:#3E7A5F;
    --warn:#D9A64A; --warn-bg:#2A2416; --warn-border:#5A4A24; --warn-dim:#8A6F35;
    --danger:#E27B70; --danger-bg:#301C1A; --danger-border:#6A3430; --danger-dim:#8E4A44;
    --prov-measured:rgba(91,190,142,.55); --prov-assumed:rgba(217,166,74,.55); --prov-invoiced:rgba(217,138,149,.55);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#1B1715; --surface:#221D1A; --surface-2:#2B2520; --side:#130F0E;
  --ink:#ECE6E0; --ink-strong:#F7F3EE; --muted:#A79E96; --faint:#8E857D;
  --line:#352E29; --rule:#463D37; --border:#352E29; --border-strong:#463D37; --grid:#2F2924;
  --accent:#D98A95; --accent-hover:#E5A3AC; --accent-ink:#1F1012; --accent-bg:#3A2226;
  --ok:#5BBE8E; --ok-bg:#1B2B23; --ok-dim:#3E7A5F;
  --warn:#D9A64A; --warn-bg:#2A2416; --warn-border:#5A4A24; --warn-dim:#8A6F35;
  --danger:#E27B70; --danger-bg:#301C1A; --danger-border:#6A3430; --danger-dim:#8E4A44;
  --prov-measured:rgba(91,190,142,.55); --prov-assumed:rgba(217,166,74,.55); --prov-invoiced:rgba(217,138,149,.55);
}

/* ============================================================
   2. Fonts (bundled, SIL OFL — see About and licences)
   ============================================================ */
@font-face{ font-family:"Fraunces"; src:url(fonts/Fraunces.ttf) format("truetype"); font-weight:100 900; font-display:swap; }
@font-face{ font-family:"Source Sans 3"; src:url(fonts/SourceSans3.ttf) format("truetype"); font-weight:200 900; font-display:swap; }
@font-face{ font-family:"IBM Plex Mono"; src:url(fonts/IBMPlexMono-Regular.ttf) format("truetype"); font-weight:400; font-display:swap; }
@font-face{ font-family:"IBM Plex Mono"; src:url(fonts/IBMPlexMono-Medium.ttf) format("truetype"); font-weight:500; font-display:swap; }

/* ============================================================
   3. Base
   ============================================================ */
*,*::before,*::after{ box-sizing:border-box; }
html{ -webkit-text-size-adjust:100%; }
body{ margin:0; background:var(--bg); color:var(--ink); font:15.5px/1.5 var(--font-sans); -webkit-font-smoothing:antialiased; }
h1,h2,h3,p,dl,dd,ul,ol,figure{ margin:0; }
h1{ font-family:var(--font-display); font-size:clamp(1.8rem,3.4vw,2.4rem); font-weight:500; letter-spacing:-.015em; line-height:1.1; text-wrap:balance; }
h2{ font-family:var(--font-display); font-size:1.05rem; font-weight:600; line-height:1.3; }
h3{ font-family:var(--font-display); font-size:1rem; font-weight:600; }
a{ color:var(--accent); text-decoration:none; }
a:hover{ color:var(--accent-hover); text-decoration:underline; }
code{ font-family:var(--font-mono); font-size:.88em; background:var(--surface-2); padding:.05rem .3rem; border-radius:var(--radius-sm); }
pre{ font-family:var(--font-mono); font-size:.8rem; background:var(--surface-2); color:var(--ink); padding:.75rem; border-radius:var(--radius-sm); overflow-x:auto; margin:0; }
button,input,select,textarea{ font:inherit; color:inherit; }
[hidden]{ display:none !important; }
:focus-visible{ outline:2px solid var(--accent); outline-offset:2px; }
.mono{ font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.muted{ color:var(--muted); } .faint{ color:var(--faint); } .sm{ font-size:.86rem; }
.measure{ max-width:65ch; }
.lead{ max-width:62ch; }
.hint{ font-size:.86rem; color:var(--muted); }
.safe{ font-size:.92rem; color:var(--ink); }
.when{ font-size:.84rem; color:var(--muted); font-variant-numeric:tabular-nums; }
.linklike{ background:none; border:0; padding:0; color:var(--accent); cursor:pointer; font:inherit; font-weight:500; }
.linklike:hover{ text-decoration:underline; }
.scroll,.table-scroll{ overflow-x:auto; }

/* ============================================================
   4. Shell — sidebar + content column (spec §6.1)
   ============================================================ */
.shell{ display:grid; grid-template-columns:var(--sidebar) minmax(0,1fr); min-height:100vh; }
.sidebar{ background:var(--side); border-right:1px solid var(--rule); display:flex; flex-direction:column; gap:.15rem; padding:1.4rem .9rem; position:sticky; top:0; align-self:start; height:100vh; }
.brand{ font-family:var(--font-display); font-size:1.2rem; font-weight:600; letter-spacing:-.01em; color:var(--ink); padding:.2rem .7rem 1.1rem; }
.brand:hover{ text-decoration:none; color:var(--ink); }
.screens{ display:flex; flex-direction:column; gap:.1rem; }
.screens a{ color:var(--muted); font-weight:500; padding:.42rem .7rem; border-left:2px solid transparent; border-radius:0 var(--radius) var(--radius) 0; }
.screens a:hover{ color:var(--ink); text-decoration:none; }
.screens a[aria-current]{ color:var(--ink); border-left-color:var(--accent); background:var(--surface); }
.sidebar-foot{ margin-top:auto; display:grid; gap:.4rem; padding:0 .7rem; }
.chip{ font-family:var(--font-mono); font-size:.68rem; color:var(--faint); border:1px solid var(--rule); border-radius:999px; padding:.1rem .5rem; justify-self:start; }
.clock{ font-family:var(--font-mono); font-size:.72rem; color:var(--faint); }
.content{ min-width:0; display:flex; flex-direction:column; }
main{ width:100%; max-width:var(--maxw); margin-inline:auto; padding:2rem var(--gutter) 3rem; flex:1; }
.notices{ max-width:var(--maxw); margin:1rem auto 0; padding:0 var(--gutter); display:grid; gap:.5rem; width:100%; }
.workbar{ max-width:var(--maxw); margin-inline:auto; padding:0 var(--gutter); width:100%; }
.sitefoot{ width:100%; max-width:var(--maxw); margin-inline:auto; padding:1.25rem var(--gutter) 2rem; border-top:1px solid var(--rule); font-size:.8rem; color:var(--muted); display:flex; gap:1.25rem; flex-wrap:wrap; }
@media (max-width:820px){
  .shell{ grid-template-columns:1fr; }
  .sidebar{ position:static; height:auto; flex-direction:row; align-items:center; gap:1rem; padding:.75rem 16px; border-right:0; border-bottom:1px solid var(--rule); }
  .brand{ padding:0; }
  .screens{ flex-direction:row; gap:.5rem; flex-wrap:wrap; }
  .screens a{ border-left:0; padding:.3rem .5rem; border-radius:0; }
  .screens a[aria-current]{ background:transparent; border-bottom:2px solid var(--accent); }
  .sidebar-foot{ display:none; }
}

/* ============================================================
   5. Screens, bands and the margin title (spec §6.1)
   ============================================================ */
.screen{ display:grid; gap:0; }
.screen > h1{ margin-bottom:.4rem; }
.eyebrow, .screen > .slabel:first-child{ font-family:var(--font-sans); font-size:.86rem; color:var(--muted); font-weight:500; }
.slabel{ font-family:var(--font-display); font-size:1.05rem; font-weight:600; color:var(--ink); line-height:1.3; }
.band{ display:grid; grid-template-columns:var(--margin) minmax(0,1fr); column-gap:2rem; row-gap:0; padding:1rem 0 1.5rem; border-top:1px solid var(--rule); }
.band > *{ grid-column:2; margin-bottom:.6rem; min-width:0; }
.band > *:last-child{ margin-bottom:0; }
.band > .band-head, .band > .slabel:first-child{ grid-column:1; grid-row:1 / span 60; margin-bottom:0; display:block; }
.band-head .slabel + .slabel, .band-head .more, .band-head .band-head-actions{ display:block; font-family:var(--font-sans); font-size:.86rem; font-weight:400; color:var(--muted); margin-top:.3rem; }
.band-head .more a, .band-head .band-head-actions a{ font-weight:500; }
@media (max-width:640px){
  .band{ grid-template-columns:1fr; }
  .band > *{ grid-column:1; }
  .band > .band-head, .band > .slabel:first-child{ grid-row:auto; margin-bottom:.6rem; }
}
.grid2{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:1.5rem; }

/* ============================================================
   6. Figures (4.6) — serif value, ruled cells
   ============================================================ */
.statusstrip, .grid4, .figs{ display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); border-top:1px solid var(--rule); border-bottom:1px solid var(--rule); }
.sfig{ display:grid; gap:.15rem; padding:.9rem 1rem .9rem 1rem; border-right:1px solid var(--line); }
.sfig:first-child{ padding-left:0; } .sfig:last-child{ border-right:0; }
.sfig .k{ font-size:.8rem; color:var(--muted); }
.sfig .v{ font-family:var(--font-display); font-size:1.6rem; font-weight:500; letter-spacing:-.01em; color:var(--ink); font-variant-numeric:tabular-nums; }
.sfig .s{ font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums; }
.fig{ font-family:var(--font-display); font-variant-numeric:tabular-nums; color:var(--ink); }
.p-measured{ border-bottom:1px solid var(--prov-measured); }
.p-assumed{ border-bottom:1px dotted var(--prov-assumed); color:var(--muted); }
.p-invoiced{ border-bottom:3px double var(--prov-invoiced); }
.stamp, .price-stamp{ font-family:var(--font-mono); font-size:.72rem; color:var(--faint); }
.legend{ display:flex; gap:1rem; flex-wrap:wrap; font-size:.78rem; color:var(--muted); align-items:center; }
.key-cell{ display:inline-block; width:10px; height:10px; border-radius:2px; vertical-align:-1px; margin-right:.3rem; background:var(--grid); }
.delta{ font-family:var(--font-mono); font-size:.82rem; color:var(--muted); font-variant-numeric:tabular-nums; }
.n, .num{ font-variant-numeric:tabular-nums; }
.num{ text-align:right; white-space:nowrap; }

/* ============================================================
   7. State tokens (4.7) — a coloured word, no pill
   ============================================================ */
.tok{ display:inline-flex; align-items:center; gap:.3rem; font-size:.78rem; font-weight:600; letter-spacing:.01em; color:var(--muted); }
.tok-ok{ color:var(--ok); } .tok-failed{ color:var(--danger); } .tok-overdue{ color:var(--warn); }
.tok-running{ color:var(--accent); } .tok-paused, .tok-notrun{ color:var(--faint); } .tok-warming{ color:var(--warn); }
.dot{ width:10px; height:10px; border-radius:50%; margin-top:.4rem; background:var(--ok); flex:none; }
.dot-ok{ background:var(--ok); } .dot-warn{ background:var(--warn); } .dot-danger{ background:var(--danger); }

/* ============================================================
   8. Verdict (Board) and signals (4.5)
   ============================================================ */
.verdict{ border-left:3px solid var(--ok); padding:.3rem 0 .3rem 1.2rem; display:flex; justify-content:space-between; gap:1rem 2rem; flex-wrap:wrap; align-items:center; }
.verdict h2{ font-size:clamp(1.3rem,2.3vw,1.75rem); font-weight:500; letter-spacing:-.01em; line-height:1.2; text-wrap:balance; max-width:40ch; }
.verdict .sub{ color:var(--muted); margin-top:.35rem; font-variant-numeric:tabular-nums; }
.sig{ display:flex; gap:.6rem; align-items:flex-start; border-left:3px solid var(--rule); background:var(--surface-2); padding:.7rem .9rem; border-radius:0 var(--radius) var(--radius) 0; font-size:.92rem; }
.sig .glyph{ display:none; }
.sig .body{ flex:1; min-width:0; }
.sig .label{ font-weight:600; margin-right:.4rem; }
.sig-note{ border-left-color:var(--rule); background:transparent; padding-inline:.9rem 0; color:var(--muted); }
.sig-advice{ border-left-color:var(--accent); background:var(--accent-bg); }
.sig-warning{ border-left-color:var(--warn); background:var(--warn-bg); }
.sig-blocker, .sig-failure{ border-left-color:var(--danger); background:var(--danger-bg); }
.sig-success{ border-left-color:var(--ok); background:var(--ok-bg); }
.flash-x{ margin-left:auto; }
.needs-row, .rrow{ display:grid; grid-template-columns:12px 1fr; gap:.7rem; align-items:start; }
.rrow .k{ font-size:.86rem; color:var(--muted); }
.rrow .v{ font-size:.95rem; color:var(--ink); }
.rrow .t{ font-size:.78rem; color:var(--muted); margin-top:.1rem; overflow-wrap:anywhere; }

/* ============================================================
   9. Buttons and controls
   ============================================================ */
.btn{ display:inline-flex; align-items:center; gap:.4rem; border:1px solid var(--rule); background:transparent; color:var(--ink); border-radius:var(--radius); padding:.5rem .9rem; font-weight:500; cursor:pointer; text-decoration:none; line-height:1.2; }
.btn:hover{ background:var(--surface-2); text-decoration:none; color:var(--ink); }
.btn-primary{ background:var(--accent); border-color:var(--accent); color:var(--accent-ink); font-weight:600; }
.btn-primary:hover{ background:var(--accent-hover); border-color:var(--accent-hover); color:var(--accent-ink); }
.btn-ghost{ border-color:transparent; }
.btn-danger{ color:var(--danger); border-color:var(--danger-border); }
.btn-sm{ padding:.3rem .6rem; font-size:.86rem; }
.btn-xs{ padding:.1rem .4rem; font-size:.74rem; }
.btn[disabled], .btn.blocked{ opacity:.5; cursor:not-allowed; }
.actions{ display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
.menu{ display:inline-block; position:relative; }
.menu > summary{ list-style:none; cursor:pointer; }
.menu > summary::-webkit-details-marker{ display:none; }
.menu-pop{ position:absolute; right:0; z-index:5; background:var(--surface); border:1px solid var(--rule); border-radius:var(--radius); padding:.4rem; min-width:170px; display:grid; gap:.2rem; }
.qm{ display:inline-flex; align-items:center; justify-content:center; width:1.05rem; height:1.05rem; border-radius:50%; border:1px solid var(--muted); color:var(--muted); font-size:.68rem; font-weight:600; vertical-align:middle; margin-left:.2rem; text-decoration:none; }
.qm:hover{ color:var(--ink); border-color:var(--ink); text-decoration:none; }
.copy{ font-size:.78rem; }

/* ============================================================
   10. Tables and definition lists
   ============================================================ */
table{ border-collapse:collapse; width:100%; font-size:.92rem; }
th{ text-align:left; font-weight:500; color:var(--muted); font-size:.8rem; padding:.45rem .6rem .45rem 0; border-bottom:1px solid var(--rule); }
td{ padding:.55rem .6rem .55rem 0; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td{ border-bottom:0; }
.jobname{ font-family:var(--font-display); font-size:1.05rem; font-weight:500; color:var(--ink); }
.jobpath{ font-family:var(--font-mono); font-size:.76rem; color:var(--faint); word-break:break-all; }
.cell2{ display:grid; gap:.1rem; }
.cell2 .v{ font-family:var(--font-display); font-size:1rem; color:var(--ink); font-variant-numeric:tabular-nums; }
.cell2 .s{ font-size:.78rem; color:var(--muted); font-variant-numeric:tabular-nums; }
.defgrid{ display:grid; grid-template-columns:max-content minmax(0,1fr); gap:.55rem 1.5rem; }
.defgrid dt{ color:var(--muted); }
.defgrid dd{ min-width:0; overflow-wrap:anywhere; }
@media (max-width:520px){ .defgrid{ grid-template-columns:1fr; gap:.15rem; } .defgrid dd{ margin-bottom:.6rem; } }

/* ============================================================
   11. Run strip and duration bars (the one bold element)
   ============================================================ */
.ledger-wrap{ overflow-x:auto; }
.ledger{ display:grid; gap:4px; }
.strip{ display:grid; grid-template-columns:repeat(30,minmax(6px,1fr)); gap:3px; }
.strip .cellx{ display:block; height:14px; background:var(--grid); border-radius:var(--radius-sm); }
.strip .cellx.ok{ background:var(--ok); } .strip .cellx.fail{ background:var(--danger); } .strip .cellx.slow{ background:var(--warn); }
.strip .cellx.dim{ opacity:.55; }
.strip .cellx.current{ outline:2px solid var(--accent); outline-offset:1px; }
.bars{ display:grid; grid-template-columns:repeat(30,minmax(6px,1fr)); gap:3px; align-items:end; height:22px; }
.bars .bar{ background:var(--muted); opacity:.45; border-radius:1px; }
.bars .bar.tall{ background:var(--warn); opacity:.9; }
.ledger-axis{ display:flex; justify-content:space-between; font-family:var(--font-mono); font-size:.72rem; color:var(--muted); }
.board-strip .strip{ grid-template-columns:repeat(14,minmax(6px,1fr)); }

/* ============================================================
   12. Job page (5.2) — head, grid, rail, tiles
   ============================================================ */
.jobhead{ display:flex; justify-content:space-between; gap:1rem; flex-wrap:wrap; align-items:flex-start; margin-bottom:1.5rem; }
.titleline{ display:flex; gap:.6rem; align-items:baseline; flex-wrap:wrap; }
.typeline{ margin-top:.4rem; color:var(--muted); }
.pathline{ margin-top:.35rem; font-family:var(--font-mono); font-size:.84rem; color:var(--muted); overflow-wrap:anywhere; }
.jobgrid{ display:grid; grid-template-columns:minmax(0,1fr) 340px; column-gap:3.5rem; align-items:start; }
@media (min-width:1500px){ .jobgrid{ grid-template-columns:minmax(0,1fr) 380px; column-gap:4.5rem; } }
.rail{ display:grid; gap:.9rem; min-width:0; padding-top:1rem; border-top:1px solid var(--rule); }
.rail-inline{ display:none; }
.rail h3, .rail-inline h3{ margin-bottom:.2rem; }
@media (max-width:820px){ .jobgrid{ grid-template-columns:1fr; } .rail{ display:none; } .rail-inline{ display:grid; gap:.9rem; padding-top:1rem; border-top:1px solid var(--rule); margin:0 0 1rem; } }
.tiles{ display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:1.25rem; }
@media (max-width:420px){ .tiles{ grid-template-columns:1fr; } }
.tile{ border:1px solid var(--rule); border-radius:var(--radius); background:var(--surface); padding:1rem 1.1rem 1.1rem; display:grid; gap:.75rem; min-width:0; }
.tile .name{ font-family:var(--font-display); font-size:1.3rem; font-weight:500; letter-spacing:-.01em; color:var(--ink); }
.tile .kindl{ color:var(--muted); font-size:.9rem; margin-top:.1rem; }
.tile .figs{ border-bottom:0; }
.tile .sfig{ padding:.6rem .8rem .2rem 0; padding-left:.8rem; }
.tile .sfig:first-child{ padding-left:0; }
.tile .sfig .v{ font-size:1.2rem; }
.tile .strip .cellx{ height:10px; }
.jobprog{ margin-top:6px; }
.jobprog-top{ margin:12px 0 4px; }
.jobprog-bar{ position:relative; height:6px; border-radius:999px; background:var(--surface-2); overflow:hidden; }
.jobprog-fill{ position:absolute; inset:0 auto 0 0; width:0; background:var(--accent); transition:width .4s ease; }
.jobprog-bar.indet .jobprog-fill{ width:38%; transition:none; animation:slide 1.2s linear infinite; }
.jobprog-text{ display:block; margin-top:3px; color:var(--faint); font-size:.8rem; }
@keyframes slide{ from{ left:-40%; } to{ left:100%; } }
.tooldetail > summary{ cursor:pointer; color:var(--muted); font-size:.9rem; }
.cmd{ display:grid; gap:.4rem; margin:.6rem 0; }
.errline{ font-family:var(--font-mono); font-size:.82rem; color:var(--ink); background:var(--surface-2); padding:.4rem .6rem; border-radius:var(--radius-sm); display:inline-block; }
.guard{ border-left:3px solid var(--warn); background:var(--warn-bg); padding:.7rem .9rem; border-radius:0 var(--radius) var(--radius) 0; font-size:.92rem; }
.guard strong{ display:block; margin-bottom:.15rem; }
.guard input[type=text]{ max-width:16rem; }

/* ============================================================
   13. Activity table, run record
   ============================================================ */
.filterrow, .filters{ display:flex; gap:1rem; flex-wrap:wrap; align-items:end; margin-bottom:1rem; }
.fld{ display:grid; gap:.2rem; font-size:.86rem; color:var(--muted); }
table.activity td.mono{ font-size:.86rem; }
.actrow.has-log{ cursor:pointer; }
.actlog pre{ margin-top:.3rem; }
#runlog, #log{ max-height:60vh; }
```

- [ ] **Step 5: Carry over the screen-specific sections, then the responsive tail**

Append, verbatim, these line ranges of `app/gui/static/style.css.bak` to the new file, in this order (they only use token names that still exist):
- lines 428–453 (`Forms — inputs, guard, confirm`) — but delete the `.guard{...}` and `.guard input[type=text]{...}` lines from the copy (the new core defines them);
- lines 454–506 (`blocker / warning blocks on the form screen`);
- lines 507–535 (`cards, tree, shell block, step, levers`);
- lines 536–560 (`cost workbench`);
- lines 561–567 (`motion — change flash`);
- lines 602–616 (`prefers-reduced-motion`).

Then run `grep -n "font-mono\|font-sans" app/gui/static/style.css | wc -l` and `grep -c "fonts.googleapis" app/gui/static/style.css` (expected 0). Inside the carried-over form section, change `input, select, textarea{ ... background:var(--bg) ...}` to `background:var(--surface)` and `border:1px solid var(--border)` to `border:1px solid var(--rule)` if those literal rules are present (spec §6.4: ruled inputs on `--surface`).

- [ ] **Step 6: Rewrite the shell**

Replace `app/gui/templates/base.html` with:

```html
<!-- app/gui/templates/base.html — the shared Ledger shell (spec 2026-10-04 §6.1).
     Every screen extends this and fills the body block. The chrome — sidebar with the
     five screens, notices, the one-line footer — is the same on every page. -->
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}backup-engine{% endblock %}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head><body>
{% set ep = request.endpoint or '' %}
{%- set nav_active =
     'help' if ep == 'gui.how_it_works'
     else 'setup' if (ep.startswith('gui.provision') or ep.startswith('gui.setup') or ep in ['gui.config_page', 'gui.config_save', 'gui.about_page'])
     else 'cost' if (ep in ['gui.cost_page_view', 'gui.cost_json', 'gui.estimate_page', 'gui.jobs_estimate_json'] or ep.startswith('gui.costs'))
     else 'activity' if (ep.startswith('gui.activity') or ep == 'gui.logs')
     else 'explore' if ep.startswith('gui.explore')
     else 'board' -%}
<div class="shell">
<header class="sidebar">
  <a class="brand" href="{{ url_for('gui.index') }}" aria-label="backup-engine home">backup-engine</a>
  <nav class="screens" aria-label="Screens">
    <a href="{{ url_for('gui.index') }}" {{ 'aria-current="page"' if nav_active == 'board' }}>Board</a>
    <a href="{{ url_for('gui.cost_page_view') }}" {{ 'aria-current="page"' if nav_active == 'cost' }}>Cost</a>
    <a href="/activity" {{ 'aria-current="page"' if nav_active == 'activity' }}>Activity</a>
    <a href="/explore" {{ 'aria-current="page"' if nav_active == 'explore' }}>Explore</a>
    <a href="/setup" {{ 'aria-current="page"' if nav_active == 'setup' }}>Setup</a>
  </nav>
  <div class="sidebar-foot">
    <a class="chip" href="/setup" title="This GUI has no authentication. Put it behind a reverse proxy or SSO and never expose it directly to the internet.">LAN only, no auth</a>
    <time class="clock mono" id="be-clock" data-tz="{{ tz }}"{% block clock_attrs %}{% endblock %}>{% block clock %}{% endblock %}</time>
  </div>
</header>
<div class="content">
<div class="workbar" id="workbar" hidden></div>
{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}
<div class="notices" role="status" aria-live="polite">
  {% for category, msg in messages %}
  {%- set cat = category if category in ['success', 'failure', 'warning', 'blocker', 'note'] else 'note' -%}
  <div class="sig sig-{{ cat }}" data-flash="{{ cat }}">
    <div class="body">
      {% if cat == 'success' %}<span class="label">Done</span>
      {% elif cat == 'failure' %}<span class="label">Failed</span>
      {% elif cat == 'warning' %}<span class="label">Warning</span>
      {% elif cat == 'blocker' %}<span class="label">Blocker</span>{% endif %}
      {{ msg }}
    </div>
    <button type="button" class="flash-x btn-ghost btn-xs" aria-label="Dismiss">&times;</button>
  </div>
  {% endfor %}
</div>
{% endif %}{% endwith %}
<main>
{% block body %}{% endblock %}
</main>
<footer class="sitefoot">
  <span>Times in {{ tz }}.</span>
  <a href="/how-it-works">How it works</a>
  <a href="/setup/about">About and licences</a>
</footer>
</div>
</div>
<script>
// Shell chrome only: the clock, and flash dismiss/auto-dismiss. Every read/write is
// guarded; nothing here is load-bearing for the page.
(function () {
  "use strict";
  try {
    var c = document.getElementById("be-clock");
    if (c && !c.textContent.trim()) {
      var tz = c.getAttribute("data-tz") || "";
      var fmt = function () {
        var opts = {weekday: "short", day: "2-digit", month: "short", year: "numeric",
                    hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short"};
        if (tz) { opts.timeZone = tz; }
        var parts = {};
        try {
          new Intl.DateTimeFormat("en-GB", opts).formatToParts(new Date())
            .forEach(function (p) { parts[p.type] = p.value; });
        } catch (e) {
          delete opts.timeZone;
          new Intl.DateTimeFormat("en-GB", opts).formatToParts(new Date())
            .forEach(function (p) { parts[p.type] = p.value; });
        }
        return parts.weekday + " " + parts.day + " " + parts.month + " " + parts.year +
               " " + parts.hour + ":" + parts.minute + " " + (parts.timeZoneName || "");
      };
      var tick = function () { try { c.textContent = fmt(); } catch (e) {} };
      tick();
      setInterval(tick, 30000);
    }
  } catch (e) {}
  try {
    var notices = document.querySelector(".notices");
    if (notices) {
      notices.addEventListener("click", function (ev) {
        var x = ev.target.closest ? ev.target.closest(".flash-x") : null;
        if (x && x.parentNode) { x.parentNode.remove(); }
      });
      Array.prototype.forEach.call(notices.querySelectorAll('[data-flash="success"]'), function (el) {
        setTimeout(function () { if (el.parentNode) { el.remove(); } }, 7000);
      });
    }
  } catch (e) {}
})();
</script>
<script src="{{ url_for('static', filename='app.js') }}"></script>
</body></html>
```

- [ ] **Step 7: Add the How it works page and route**

Create `app/gui/templates/how_it_works.html`:

```html
{% extends "base.html" %}
{% block title %}How it works · backup-engine{% endblock %}
{% block body %}
<section class="screen read measure" aria-label="How it works">
  <p class="slabel">How it works</p>
  <h1>What this app does with your files</h1>
  <p class="lead" style="margin-top:.6rem">Plain answers to the questions the “?” marks lead here from. Nothing on this page is a control.</p>

  <div class="band" id="jobs">
    <p class="slabel">Three kinds of job</p>
    <p><strong>Snapshot backup</strong> takes a point in time, so you can restore any date. Only what changed since the last run is sent, so daily runs stay small.</p>
    <p><strong>Plain copy</strong> is a straight copy of big, static files, such as a media library. There is no history, just what is there now.</p>
    <p><strong>File history</strong> keeps every version of every file, one file at a time. Pick it for documents you edit.</p>
  </div>

  <div class="band" id="restore-points">
    <p class="slabel">Restore points and the keep rule</p>
    <p>A restore point is one completed Snapshot backup: the folder exactly as it was at that moment. The Job page counts them and shows how far back they reach.</p>
    <p>The keep rule thins them out over time. “Last 3, one a day for 7 days, one a week for 4 weeks, one a month for 6 months” keeps every recent run and fewer older ones, so the count on the Job page settles instead of growing forever. Restore points that no rule keeps are removed after the next run.</p>
  </div>

  <div class="band" id="earlier-copies">
    <p class="slabel">Earlier copies and the undo window</p>
    <p>When a run replaces or deletes a file in S3, S3 keeps the earlier copy for a while instead of destroying it at once. That is the undo window, set under Setup, S3 rules. Inside the window a bad run can be undone; after it, S3 removes the earlier copies for good.</p>
    <p>The storage summary in Activity counts those earlier copies for each job folder, how much space they take, and what the window will remove in the coming week.</p>
  </div>

  <div class="band" id="tiers">
    <p class="slabel">Storage tiers</p>
    <p><strong>Instant</strong> tiers read the moment you ask and cost the most per month. <strong>Thaw first</strong> tiers cost a fraction to keep, but a file has to be fetched before it can be read, which takes minutes to hours and is charged per request.</p>
    <p>Snapshot backups must stay on an Instant tier because every run reads the previous one. Plain copies of files you rarely need are the natural fit for a Thaw-first tier.</p>
  </div>

  <div class="band" id="numbers">
    <p class="slabel">Where a number came from</p>
    <p>Every figure carries a mark. <span class="fig p-measured">measured</span>: something walked the folder or read the bucket, on a date it tells you. <span class="fig p-assumed">assumed</span>: dotted and dimmer, because it rests on a guess you made, such as how much changes each month. <span class="fig p-invoiced">invoiced</span>: the bill AWS actually sent. No line at all means projected: an estimate built from the others, which takes the weakest mark of everything that fed it.</p>
  </div>

  <div class="band" id="notices">
    <p class="slabel">What the notices mean</p>
    <p><strong>Needs you</strong> on the Board lists anything that will not fix itself: a failed run, a key that cannot write, a restore with nowhere to go.</p>
    <p>A <strong>Warning</strong> will cost money or surprise you later; nothing is stopped. A <strong>Blocker</strong> stops the button and names the ways out. <strong>Done</strong> appears for a moment after you press something. <strong>Failed</strong> is a run that ended badly, with a time, the exact error and a record you can open.</p>
  </div>

  <div class="band" id="tools">
    <p class="slabel">What actually runs</p>
    <p>Snapshot backups are made by restic, which keeps one encrypted store per machine; two Snapshot jobs must not start in the same minute. Plain copies and File history are made by rclone, one folder per job. The exact command for each job is under “Tool detail” on its Job page. <a href="/setup/about">About and licences</a> names every tool and its licence.</p>
  </div>
</section>
{% endblock %}
```

Add to `app/gui/routes.py` directly after `about_page`:

```python
@bp.get("/how-it-works")
def how_it_works():
    # The one explanation page (spec 2026-10-04 §4): the "?" marks link to its anchors.
    return render_template("how_it_works.html")
```

- [ ] **Step 8: Run the shell tests, then the full suite**

Run: `python3 -m pytest -q tests/gui/test_shell_ledger.py tests/gui/test_about_routes.py tests/gui/test_vocabulary.py 2>&1 | tail -12`
Expected: shell tests PASS; the Task-1 daily rules still fail (screens not yet done); the pre-existing shell test `test_shell_text_has_no_forbidden_terms` passes.

Run: `python3 -m pytest -q 2>&1 | tail -5`
Expected: only the Task-1 daily rules fail. If any other test fails because it asserted on the old footer or topbar text (`grep -rn "How to read this screen\|switcher\|topbar" tests/gui`), update that assertion to the new shell (sidebar `class="screens"`, footer "How it works").

- [ ] **Step 9: Commit**

```bash
git add app/gui/static/fonts app/gui/static/style.css app/gui/templates/base.html app/gui/templates/how_it_works.html app/gui/routes.py app/gui/attributions.py tests/gui/test_shell_ledger.py tests/gui/test_about_routes.py
git commit -m "ledger: bundled fonts, the Linen & wine stylesheet with light+dark, the sidebar shell, and /how-it-works"
```

---

### Task 3: The Job page

**Files:**
- Modify: `app/gui/templates/job.html` (whole file; the mockup target is `docs/superpowers/mockups/2026-10-04-ledger-directions.html`, section `#d1`)
- Modify: `app/gui/templates/_console_cap.html`
- Modify: `app/gui/routes.py:1163-1200` (`_what_it_did`), `routes.py:1935-1938` (`PLAIN_COUNT_CAP`)
- Test: `tests/gui/test_job_page_routes.py`, `tests/gui/test_vocabulary.py` (daily rules for `/jobs/appdata`, `/jobs/manga`)

**Interfaces:**
- Consumes: CSS classes from Task 2; `vocab.EARLIER_COPIES`, `vocab.AWS`, `vocab.TEMP_FOLDER`; the "?" convention.
- Produces: the pattern every later screen copies (band + margin title, figures row, rail).

- [ ] **Step 1: Write the failing page tests**

Append to `tests/gui/test_job_page_routes.py` (use the file's existing client/app fixtures; `full_app` from `test_vocabulary` can be imported with `from tests.gui.test_vocabulary import full_app  # noqa: F401` if a seeded job is needed):

```python
def test_job_page_is_terse_and_marks_the_three_questions(full_app):
    body = full_app.test_client().get("/jobs/appdata").get_data(as_text=True)
    # the three "?" marks, each a link to the explanation page
    assert body.count('class="qm"') == 3
    assert body.count('href="/how-it-works#restore-points"') == 2      # Restore points figure + Keep rule
    assert body.count('href="/how-it-works#tiers"') == 1               # Storage tier
    # teaching paragraphs are gone
    for gone in ("Could you actually get this back right now?", "Set at creation",
                 "Pulls a single file into a", "The bars under the strip are how long",
                 "Snapshot backups are made by", "an assumption you set"):
        assert gone not in body, gone
    # the safety sentence stays, as a safe line, not a hint
    assert 'class="safe"' in body and "never touched or overwritten" in body
    # no arrows glued to links
    assert "cost view →" not in body and "Edit →" not in body


def test_job_page_rail_markup_order(full_app):
    # Phone width: the inline rail (shown ≤820px) precedes the main column; the
    # sticky rail follows it. Both render the same readiness rows.
    body = full_app.test_client().get("/jobs/appdata").get_data(as_text=True)
    assert body.index('class="rail-inline"') < body.index('class="band"') < body.index('class="rail"')
    assert body.count("Recovery readiness") == 2


def test_cold_job_keeps_tier_phrase_and_thaw_warning(full_app):
    body = full_app.test_client().get("/jobs/manga").get_data(as_text=True)
    assert "Thaw first, hours" in body
    assert "hours" in body and "Get data back" in body
    assert "Amazon" not in body and "old versions" not in body
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest -q tests/gui/test_job_page_routes.py -k "terse or rail_markup or cold_job" 2>&1 | tail -6`
Expected: FAIL (no `qm`, hints present).

- [ ] **Step 3: Rework `job.html`**

Work through the file top to bottom. Keep every `id`, `data-*`, form `action`, `csrf` input and macro call exactly as they are. Changes:

1. **Head** (`.jobhead`, lines 93–158): keep. Change `{{ vocab.TYPE_LINES[...] }}`-style type line as is. Replace the `→` in the `&nbsp;→&nbsp;` path line with `<span class="muted">to</span>`. Buttons: "Run now", "Get data back", the `···` menu; menu items lose their `→` ("Edit"). The delete confirm `data-confirm` text keeps its sentence but says "nothing in the bucket is deleted" (already true) — no change.
2. **Needs-attention block** ("Why this happens" / "What to do", lines ~195–215): keep the two `slabel`s as a single `sig sig-failure` with the label "Failed" and the two paragraphs; drop any `hint` class inside it.
3. **Status strip** (lines 163–194): keep the five `.sfig` cells. In the Restore points cell: `<span class="k">Restore points <a class="qm" href="/how-it-works#restore-points" aria-label="How it works">?</a></span>`. Streak `.s`: when broken, "since <date of first ok after the last failure>" if the view provides it, else keep the existing text.
4. **Rail macro** (`rail()`): delete `<p class="hint" ...>Could you actually get this back right now?</p>`; delete the hint under the Test restore button; rename the "Old versions protected" row's `.k` to `Earlier copies protected` (value "Bucket versioning on" stays); the button text becomes "Test restore, one file, about $0.01" (no em dash); the two thaw-related hints ("Checking costs nothing…", "Asks Amazon to warm up…") go; the Thaw check button keeps its label.
5. **Last 30 runs band**: keep `band-head` with the two `slabel`s ("Last 30 runs" and the `mono` summary, change `·` separator to `, ` inside the summary: `{{ ledger.ok }} OK, {{ ledger.failed }} failed`). Delete the long hint paragraph after the ledger entirely (the strip's `title` attributes already carry each run's date and outcome).
6. **Get data back band**: the lead becomes `<p class="safe">Pick the date you want back. Files are written into a new folder. The live <span class="mono">{{ ... }}</span> is never touched or overwritten.</p>`; "Browse contents →" → "Browse contents"; the restore-points list keeps its table but: the "Restore points have not been listed yet. List them now" hint becomes a plain `<p>` with the button; line 312's "listed earlier · Refresh" becomes `<p class="when">listed earlier <button type="submit" form="rp-refresh" class="btn btn-sm btn-ghost">Refresh</button></p>`; line 346's `Data out of Amazon:` becomes `Data out of {{ vocab.AWS }}:` (the "Warm-up + data out:" label stays, warm-up is kept vocabulary); the restore-target hint "A new, dated folder. Typing the live source path here is refused…" becomes a `safe` sentence: "A new, dated folder. The live source path is refused, so a restore can never overwrite what it protects."; price notes "not priced yet" / "one time, for a full restore" keep class `hint` (these are two of the three allowed) — **or** fold them into the figure's `.s` span (preferred: `<span class="s">one time, for a full restore</span>`), which makes them not hints at all.
7. **What this job costs band**: the `more` link loses the arrow; the four `.sfig` cells stay; "old versions have built up" → `{{ vocab.EARLIER_COPIES }} have built up`; the table's third column cells use `class="when"` (not `hint`); "Getting data out of Amazon" → `Getting data out of {{ vocab.AWS }}`; the `sig sig-note` "Per-job is not available: Amazon's cost report…" becomes one line `<p class="when">The bill is one figure for the whole account, so it is not split per job.</p>`; keep `price-stamp`.
8. **How it is set up band**: delete the three "Set at creation" `hint` spans and the "(an assumption you set…)" span; `<dt>Storage tier <a class="qm" href="/how-it-works#tiers" aria-label="How it works">?</a></dt>`; `<dt>Keep rule <a class="qm" href="/how-it-works#restore-points" aria-label="How it works">?</a></dt>`; the History in S3 row gets no "?" but its text becomes `S3 keeps {{ vocab.EARLIER_COPIES }} of what this job removed for <span class="mono">{{ s3h.days }}</span> more days (the undo window)` and the "S3 rules →" link loses its arrow. The three "?" marks on the page are Restore points, Keep rule and Storage tier (spec §4). "Edit →" → "Edit".
9. **Tool detail**: keep `<details class="tooldetail">` and the `cmd` blocks; delete the three engine explanation hints ("Snapshot backups are made by restic…", "Plain copies are made by rclone…", "File history is kept by this app's own catalog…"); the id/tag line becomes `<p class="stamp">Last snapshot id <span class="mono">…</span>, tag <span class="mono">…</span>. Schedule <span class="mono">{{ cron }}</span>.</p>` (the "repository password comes from the recovery passphrase" clause and the "What these tools are →" link go; the footer's How it works covers it).
10. **`_console_cap.html`** (included on this page and the form): replace "old versions" with `{{ vocab.EARLIER_COPIES }}` (three places).
11. **`routes._what_it_did`** storage-summary branch (lines 1193–1199): `"no old versions"` → `f"no {vocab.EARLIER_COPIES}"`, `f"{n:,} old versions (…)"` → `f"{n:,} {vocab.EARLIER_COPIES} (…)"` (import `vocab` in routes if not already). `PLAIN_COUNT_CAP` (line 1937) → `f"S3 can keep at most {lifecycle.MAX_NEWER} {vocab.EARLIER_COPIES} per file — pick …"` (the form screen shows it).

After the edit: `python3 -c "from tests.gui.test_vocabulary import hint_count; print(hint_count('job.html'))"` must print ≤ 3.

- [ ] **Step 4: Update the tests that pinned removed text**

Run: `python3 -m pytest -q tests/gui/test_job_page_routes.py tests/gui/test_run_record_routes.py tests/gui/test_costs_routes.py tests/gui/test_status.py 2>&1 | grep -E "FAILED|passed|failed"`
For each failure that asserts the old wording ("Set at creation", "Amazon", "old versions", "Per-job is not available", "Open the full cost view →"), change the expected string to the new one from Step 3 (e.g. `"Getting data out of AWS"`, `"earlier copies"`, `"Open the full cost view"`). Do not delete a test; change its expectation.

- [ ] **Step 5: Run the page tests and the daily rules for the job pages**

Run: `python3 -m pytest -q tests/gui/test_job_page_routes.py "tests/gui/test_vocabulary.py::test_daily_page_uses_plain_words[/jobs/appdata]" "tests/gui/test_vocabulary.py::test_daily_page_uses_plain_words[/jobs/manga]" "tests/gui/test_vocabulary.py::test_daily_template_is_within_the_hint_budget[job.html]" 2>&1 | tail -4`
Expected: PASS.

- [ ] **Step 6: Full suite, then commit**

Run: `python3 -m pytest -q 2>&1 | tail -3` — only other screens' daily rules may still fail.

```bash
git add app/gui/templates/job.html app/gui/templates/_console_cap.html app/gui/routes.py tests/gui/test_job_page_routes.py tests/gui/test_run_record_routes.py tests/gui/test_costs_routes.py tests/gui/test_status.py
git commit -m "ledger: the Job page — terse, three ? marks, earlier copies / AWS, ruled bands and rail"
```

---

### Task 4: The Board and the Jobs list (tiles)

**Files:**
- Modify: `app/gui/templates/board.html` (lines 33–315), `app/gui/templates/jobs.html`
- Modify: `app/gui/status.py` only if the verdict/h2 strings contain "Amazon" or "The model says" (`grep -n "Amazon\|model says\|old versions" app/gui/status.py app/gui/routes.py`)
- Test: `tests/gui/test_board_routes.py`, `tests/gui/test_jobs_routes.py`

**Interfaces:**
- Consumes: `.verdict`, `.tiles`, `.tile`, `.sfig`, `.strip.board-strip`, `.needs-row`, `vocab.ESTIMATE`, `vocab.EARLIER_COPIES`, `vocab.AWS`.
- Produces: the tile markup reused by `jobs.html`:

```html
{# one tile per job, in the same worst-first order as the old table row (board.html:98-160) #}
{% set pj = (status.cost.per_job | selectattr('name', 'equalto', j.name) | list | first) if status.cost else none %}
<article class="tile" data-tile="{{ j.name }}">
  <div>
    <span class="tok tok-{{ 'notrun' if j.state == 'NOT_RUN_YET' else j.state|lower }}" id="board-tok-{{ j.name }}">{{ j.label }}</span>
    <div class="name"><a href="/jobs/{{ j.name }}">{{ j.name }}</a></div>
    <p class="kindl">{{ vocab.TYPE_LINES.get(j.type, j.type_label) }}</p>
    {% if j.state == 'RUNNING' %}<div class="jobprog" data-progress-job="{{ j.name }}"><div class="jobprog-bar"><div class="jobprog-fill" style="width:0"></div></div><span class="jobprog-text sm mono">working…</span></div>{% endif %}
  </div>
  <div class="figs">
    <div class="sfig"><span class="k">Last run</span>
      {% if j.last %}<span class="v">{{ hhmm(j.last.started_at) }}</span><span class="s" data-when="{{ j.last.finished_at }}">{{ datelabel(j.last.finished_at) }}{% if j.last.duration_s is not none %}, {{ dur(j.last.duration_s) }}{% endif %}</span>
      {% else %}<span class="v">—</span><span class="s">never</span>{% endif %}</div>
    <div class="sfig"><span class="k">Next run</span>
      {% if j.next_run %}<span class="v">{{ hhmm(j.next_run) }}</span><span class="s" data-when="{{ j.next_run }}">{{ datelabel(j.next_run) }}</span>
      {% else %}<span class="v">—</span><span class="s">{{ j.next_run_note or 'paused' }}</span>{% endif %}</div>
    <div class="sfig"><span class="k">Size</span>
      {% if pj %}<span class="v">{{ prov(pj.size_bytes | size, pj.size_provenance) }}</span><span class="s">{% if pj.monthly is not none %}{{ prov(money(pj.monthly), pj.monthly_provenance) }} a month{% else %}no price yet{% endif %}</span>
      {% else %}<span class="v">—</span><span class="s">not measured yet</span>{% endif %}</div>
  </div>
  <div class="board-strip">
    <div class="strip" role="img" aria-label="Last {{ j.strip|length }} runs for {{ j.name }}.">
      {% for cell in j.strip -%}
      {%- set color = 'fail' if cell.outcome in ['failed','aborted'] else ('running' if cell.outcome == 'running' else ('slow' if cell.slow else ('ok' if cell.outcome == 'ok' else ''))) -%}
      {%- set dimc = ' dim' if (color and loop.index0 < (j.strip|length - 7)) else '' -%}
      {%- if cell.run_id -%}<a class="cellx{{ (' ' + color) if color }}{{ dimc }}" href="/jobs/{{ j.name }}/runs/{{ cell.run_id }}" title="{{ cell.title }}"></a>
      {%- else -%}<span class="cellx" title="{{ cell.title }}"></span>{%- endif -%}
      {% endfor %}
    </div>
    <div class="ledger-axis"><span>{{ j.strip|length }} runs</span><span>latest</span></div>
  </div>
</article>
```

(`hhmm`, `datelabel`, `dur`, `prov`, `money` are the macros `board.html` defines at its top; `jobs.html` must define the same five macros or `{% import %}` them from a new `_macros.html` that both files use. `data-job` is NOT used on tiles: `app.js` line 222 updates every `[data-job]` element from `/status.json`, so leave those attributes exactly where they are today.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/test_board_routes.py`:

```python
def test_board_tiles_jobs_and_keeps_the_estimate_word(full_app):
    body = full_app.test_client().get("/").get_data(as_text=True)
    assert body.count('class="tile"') == 2            # appdata + manga
    assert 'class="verdict"' in body and "Needs you" in body
    assert "Estimate" in body and "The model says" not in body
    assert "Amazon" not in body and "old versions" not in body
    assert 'href="/how-it-works#notices"' in body and 'href="/how-it-works#numbers"' in body
    assert "+ New job" not in body and "New job" in body


def test_board_failed_job_sorts_first_with_red_verdict(full_app):
    body = full_app.test_client().get("/").get_data(as_text=True)
    # manga's last run failed (prune refused): it tiles first and the verdict rule is red
    assert body.index('data-tile="manga"') < body.index('data-tile="appdata"')
    assert 'class="verdict" style="border-left-color:var(--danger)"' in body
    assert 'href="/jobs/manga/runs/' in body        # the Needs you row links to the record
```

(`full_app` comes from `from tests.gui.test_vocabulary import full_app  # noqa: F401`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest -q tests/gui/test_board_routes.py -k "tiles or sorts_first" 2>&1 | tail -4`
Expected: FAIL (no tiles).

- [ ] **Step 3: Rework `board.html` and `jobs.html`**

`board.html`:
1. Verdict band (lines 36–47): keep `.verdict` with its `style="border-left-color:var(...)"`; the button loses its arrow ("Open appdata_backups").
2. Needs you band (48–81): `<p class="slabel">Needs you <a class="qm" href="/how-it-works#notices" aria-label="How it works">?</a></p>`; rows become `needs-row` (dot + text + link to the record, link text "Open the record"); the empty state "Nothing needs you." stays as a plain `<p class="muted">` (not `hint`); delete the `hint` "… · open the run record →" by making the link part of the row text.
3. Jobs band (82–164): header `Jobs, worst first` + summary `N jobs` + a `btn btn-sm` "New job"; replace the `<table>` with `<div class="tiles">` of the tile markup above, one per job in the existing worst-first order; delete the `compact-only hint mono` line (its content is in the tile); keep the legend as `.legend` with `key-cell` swatches, text "OK · failed · slow, three times its usual · no run yet · older than the last 7" but with commas instead of `·`.
4. What it costs band (165–end): "Open the full cost view →" → "Open the full cost view"; `.sfig` labels: "The model says" → `{{ vocab.ESTIMATE }}`; the `hint` "+ … of old versions" span → `+ … of {{ vocab.EARLIER_COPIES }}` with class `s`; the `/mo` and `—` hint spans → class `s`; the `sig sig-note` "The bill is not split per job: Amazon's cost report…" → `<p class="when">The bill is one figure for the whole account, so it is not split per job.</p>`; "No cost to show until a job exists." stays as `<p class="muted">`; keep the provenance legend and `price-stamp`; add the "?" next to the Estimate label: `<span class="k">{{ vocab.ESTIMATE }} <a class="qm" href="/how-it-works#numbers" aria-label="How it works">?</a></span>`.

`jobs.html`: replace the table with the same `tiles` block (import nothing; copy the tile markup), keep the "Getting started" `h2` block for the empty state, delete the four `hint` spans ("(restic)", "(catalog)", "(rclone)" and the size one — the engine names were already forbidden terms inside `hint`? they sit in `span.hint` and pass the lint only because they are inside `code`; drop them).

Also `grep -n "Amazon\|model says" app/gui/status.py app/gui/routes.py app/gui/estimate_io.py` and replace any string that reaches the Board (`vocab.AWS`, `vocab.ESTIMATE`).

- [ ] **Step 4: Fix tests that pinned the table or old words**

Run: `python3 -m pytest -q tests/gui/test_board_routes.py tests/gui/test_jobs_routes.py tests/gui/test_status.py tests/gui/test_estimate_routes.py 2>&1 | grep -E "FAILED|passed"`
Update expectations that looked for `<table>` rows, "The model says", "Amazon", "+ New job", "Jobs · worst first" to the new markup/words. Keep every test.

- [ ] **Step 5: Run the Board rules and the suite**

Run: `python3 -m pytest -q tests/gui/test_board_routes.py tests/gui/test_jobs_routes.py "tests/gui/test_vocabulary.py::test_daily_page_uses_plain_words[/]" "tests/gui/test_vocabulary.py::test_daily_page_uses_plain_words[/jobs]" "tests/gui/test_vocabulary.py::test_daily_template_is_within_the_hint_budget[board.html]" "tests/gui/test_vocabulary.py::test_daily_template_is_within_the_hint_budget[jobs.html]" 2>&1 | tail -3`
Expected: PASS. Then `python3 -m pytest -q 2>&1 | tail -3`.

- [ ] **Step 6: Commit**

```bash
git add app/gui/templates/board.html app/gui/templates/jobs.html app/gui/status.py app/gui/routes.py tests/gui/test_board_routes.py tests/gui/test_jobs_routes.py tests/gui/test_status.py tests/gui/test_estimate_routes.py
git commit -m "ledger: Board and Jobs list as tiles, verdict rule, Needs you rows, Estimate wording"
```

---

### Task 5: Activity, the run record, and the storage summary words

**Files:**
- Modify: `app/gui/templates/activity.html`, `app/gui/templates/run_record.html`
- Modify: `app/engine/storage_summary.py` (`describe`), `app/gui/routes.py` (`_what_it_did`, done in Task 3 — verify)
- Test: `tests/engine/test_storage_summary.py`, `tests/engine/test_sysop.py`, `tests/gui/test_activity_routes.py`, `tests/gui/test_run_record_routes.py`

- [ ] **Step 1: Write the failing tests**

In `tests/engine/test_storage_summary.py`, change the three `describe` expectations from `"old versions: 3 (1 KB) — the oldest was replaced 13 days ago"` to `"earlier copies: 3 (1 KB), the oldest was replaced 13 days ago"`, `"old versions: none"` → `"earlier copies: none"`, `"rule: S3 removes old versions 30 days after they were replaced — nothing goes in the next 7 days"` → `"rule: S3 removes earlier copies 30 days after they were replaced; nothing goes in the next 7 days"`, and the `about 1 (50 B) goes` / `go` lines likewise with `;` instead of ` — `; the delete-markers line becomes `"delete markers: 1 (files that were removed; their earlier copies are counted above)"`. Add:

```python
def test_describe_never_says_old_versions():
    s = _scan()
    text = "\n".join(ss.describe(s, rule={"NoncurrentVersionExpiration": {"NoncurrentDays": 30}}, took_s=1))
    assert "old version" not in text and "earlier copies" in text
```

In `tests/engine/test_sysop.py::test_storage_summary_logs_the_verbose_lines_and_records_its_figures` change `"old versions: 3 (1 B)\n"` to `"earlier copies: 3 (1 B)\n"`. In `tests/gui/test_run_record_routes.py::test_what_it_did_for_a_storage_summary_record` change `"2,633 old versions (42.92 GB), the oldest replaced 13 days ago"` to `"2,633 earlier copies (42.92 GB), the oldest replaced 13 days ago"` and `"no old versions"` to `"no earlier copies"`.

Append to `tests/gui/test_activity_routes.py`:

```python
def test_activity_and_record_are_terse(full_app):
    client = full_app.test_client()
    body = client.get("/activity").get_data(as_text=True)
    assert "What this machine has done" in body and "Raw shared log →" not in body and "Raw shared log" in body
    rec = client.get(f"/jobs/manga/runs/{MANGA_FAIL_RUN}").get_data(as_text=True)
    assert 'class="sig sig-failure"' in rec and "old versions" not in rec and "earlier copies" in rec
    assert "Open the shared log →" not in rec
```

(`from tests.gui.test_vocabulary import full_app, MANGA_FAIL_RUN  # noqa: F401`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest -q tests/engine/test_storage_summary.py tests/engine/test_sysop.py tests/gui/test_run_record_routes.py tests/gui/test_activity_routes.py 2>&1 | tail -6`
Expected: FAIL on the changed strings.

- [ ] **Step 3: Change the words and the templates**

`app/engine/storage_summary.py::describe`: import `from ..gui import vocab` next to `units`; build the lines with `vocab.EARLIER_COPIES` and replace every ` — ` joiner with `; ` or `, ` exactly as the tests spell them:

```python
    if st["noncurrent_versions"]:
        old = f"{vocab.EARLIER_COPIES}: {st['noncurrent_versions']:,} ({units.fmt_bytes(st['noncurrent_bytes'])})"
        if st["oldest_age_days"] is not None:
            old += f", the oldest was replaced {lifecycle._days(st['oldest_age_days'])} ago"
        lines.append(old)
    else:
        lines.append(f"{vocab.EARLIER_COPIES}: none")
    markers = f"delete markers: {st['delete_markers']:,}"
    if st["delete_markers"]:
        markers += f" (files that were removed; their {vocab.EARLIER_COPIES} are counted above)"
    ...
            head = f"rule: S3 removes {vocab.EARLIER_COPIES} {lifecycle._days(int(rd))} after they were replaced"
            ...
            lines.append(f"{head}; {tail}")
```

`activity.html`: the `h1` stays; the filters keep their `fld` labels; the one `hint` ("Raw shared log →") becomes `<a class="linklike" href="/logs">Raw shared log</a>` inside the band head; the `sig sig-note` empty state becomes `<p class="muted">Nothing has run yet.</p>` with the existing form/button; the table keeps `class="activity"` and its `data-log` rows.

`run_record.html`: the figures (`Started`, `Finished`, `Took`, `Started by`, `Restore point`, `What it did`, `Error`) move from the `defgrid` into a `statusstrip` of `.sfig` cells where each has a short value (Started, Finished, Took, Started by) and a `defgrid` for the longer ones (Restore point, What it did, Error); the "Why this happens / What to do" pair becomes one `sig sig-failure` with the `error_class.cause` and `.fix` paragraphs and the fix button; "Open the shared log →" → "Open the shared log"; the predates-history sentence keeps class `hint` (one of three); `Copy` button and `#runlog` stay.

- [ ] **Step 4: Run the four test files, the daily rules for activity and the records, then the suite**

Run: `python3 -m pytest -q tests/engine/test_storage_summary.py tests/engine/test_sysop.py tests/gui/test_run_record_routes.py tests/gui/test_activity_routes.py tests/gui/test_vocabulary.py -k "storage or sysop or record or activity or hint_budget" 2>&1 | tail -3`
Expected: PASS for activity.html / run_record.html budgets and the `/activity` and run-record daily pages.

- [ ] **Step 5: Commit**

```bash
git add app/engine/storage_summary.py app/gui/templates/activity.html app/gui/templates/run_record.html tests/engine/test_storage_summary.py tests/engine/test_sysop.py tests/gui/test_run_record_routes.py tests/gui/test_activity_routes.py
git commit -m "ledger: Activity and run record; the storage summary says earlier copies"
```

---

### Task 6: Explore and Restore

**Files:**
- Modify: `app/gui/templates/explore.html`, `explore_index.html`, `restore.html`
- Modify: `app/gui/routes.py` where the restore page's quote strings say "Amazon" or "warm-up + data out" (`grep -n "Amazon\|warm-up" app/gui/routes.py app/gui/templates/restore.html`)
- Test: `tests/gui/test_explore_routes.py`, `tests/gui/test_restore_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/test_restore_routes.py`:

```python
def test_restore_page_is_terse_and_uses_aws(full_app):
    body = full_app.test_client().get("/jobs/manga/restore").get_data(as_text=True)
    assert "Amazon" not in body and "AWS" in body
    assert 'class="safe"' in body                      # the never-overwrites sentence
    assert "Disabled until the name matches." in body  # the confirm guard keeps its one line
    assert body.count('class="hint') <= 3
```

Append to `tests/gui/test_explore_routes.py` (use that file's own client fixture that stubs `_browse_level`):

```python
def test_explore_pages_are_terse(client):
    idx = client.get("/explore").get_data(as_text=True)
    assert "Browse your backups" in idx and "→" not in idx
    page = client.get("/explore/appdata").get_data(as_text=True)
    assert "Restore point" in page and "→" not in page
```

(`client` and `example` are that file's own fixtures, lines 81 and 86; replace `appdata` with the job name `example` seeds, read it from the fixture before writing the test.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/gui/test_restore_routes.py tests/gui/test_explore_routes.py -k "terse or uses_aws" 2>&1 | tail -4`
Expected: FAIL.

- [ ] **Step 3: Rework the three templates**

`restore.html`: the eyebrow `slabel` "name · get data back" → "Get data back"; the `h1` stays; hints: "Inside it the files keep their original path." → class `safe`; "(warm-up + data out)" → `.s` text "fetch and data out"; the two "not priced yet" / "one time" spans → `.s`; the thaw/Amazon sentence → `{{ vocab.AWS }}`; "Disabled until the name matches." stays as the one `hint`; the confirm `guard` keeps its input and `data-confirm-name`.

`explore.html`: the breadcrumb `slabel` link loses nothing; the restore-point `<select id="snap-select">` keeps its id and the `cold_classes` logic; "No restore points have been listed for this job yet." stays (`hint` 1 of 3); the `td.hint` cells ("deleted", "Cold…", sizes) → class `when`; the `details` listing keeps its markup; any `→` in link text removed.

`explore_index.html`: the per-job `span.hint` → `.kindl` (the type line), rendered as tiles (reuse the Task 4 tile markup minus the strip and figures: state, name, kind line, "Browse" link).

- [ ] **Step 4: Run, fix pinned text, run the suite**

Run: `python3 -m pytest -q tests/gui/test_restore_routes.py tests/gui/test_explore_routes.py tests/gui/test_vocabulary.py -k "restore or explore or hint_budget" 2>&1 | tail -3` → PASS, then `python3 -m pytest -q 2>&1 | tail -3`.

- [ ] **Step 5: Commit**

```bash
git add app/gui/templates/explore.html app/gui/templates/explore_index.html app/gui/templates/restore.html app/gui/routes.py tests/gui/test_restore_routes.py tests/gui/test_explore_routes.py
git commit -m "ledger: Explore and Restore — terse, AWS, safety sentences"
```

---

### Task 7: Cost

**Files:**
- Modify: `app/gui/templates/cost.html` (267 lines), `app/gui/estimate_io.py` only for user-facing strings (`grep -n "Amazon\|old versions\|assumption\|change rate\|model says" app/gui/estimate_io.py app/gui/routes.py | grep -v "^.*#"`)
- Test: `tests/gui/test_costs_routes.py`, `tests/gui/test_estimate_routes.py`, `tests/gui/test_estimate_io.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gui/test_costs_routes.py`:

```python
def test_cost_page_uses_the_plain_words(full_app):
    body = full_app.test_client().get("/cost").get_data(as_text=True)
    for gone in ("The model says", "Assumptions and billing", "change rate", "Amazon", "old versions"):
        assert gone not in body, gone
    for kept in ("Estimate", "What we assumed", "How much changes each month", "earlier copies", "AWS"):
        assert kept in body, kept
    assert 'href="/how-it-works#numbers"' in body
    assert 'id="cost-timeline"' in body and 'id="est-form"' in body and 'id="proj-data"' in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest -q tests/gui/test_costs_routes.py -k plain_words 2>&1 | tail -3` → FAIL.

- [ ] **Step 3: Rework `cost.html` (words and bands only; the levers, canvas and JSON islands stay)**

- `h1` "What will this cost, and is the model telling the truth?" → "What this will cost".
- Band titles: "Proof" stays; "Per job" stays; "Projection" → "Estimate" with the "?" (`<p class="slabel">Estimate <a class="qm" href="/how-it-works#numbers" aria-label="How it works">?</a></p>`); "What a restore costs" stays; "Assumptions and billing" → "What we assumed, and billing".
- Every "The model says" → `{{ vocab.ESTIMATE }}`; every "assumption"/"assumptions" in visible text → "what we assumed" (or the label `{{ vocab.ASSUMED }}`); the change-rate lever's legend → `{{ vocab.CHANGE_EACH_MONTH }}`; "old versions" (8) → `{{ vocab.EARLIER_COPIES }}`; "Amazon" (3) → `{{ vocab.AWS }}`.
- Hints: "not comparable — one shared store" → `.s` text "not comparable, one shared store"; "Still climbing at month … — Keep everything never plateaus." → keep as `hint` 1 ("Still climbing at month N; keep everything never settles."); "Settles month …." → `.s`; the "set … …" span → `.when`.
- Remove `→` from link text; the `sig sig-failure` (a bad lever value) stays.
- In `estimate_io.py`, only strings rendered on screen change (e.g. `"because no old versions exist yet"` → `f"because no {vocab.EARLIER_COPIES} exist yet"`); the **math and keys are frozen**.

- [ ] **Step 4: Update pinned tests, run, suite, commit**

Run: `python3 -m pytest -q tests/gui/test_costs_routes.py tests/gui/test_estimate_routes.py tests/gui/test_estimate_io.py tests/gui/test_vocabulary.py -k "cost or estimate or hint_budget" 2>&1 | grep -E "FAILED|passed"`; change expectations of the old words; then `python3 -m pytest -q 2>&1 | tail -3`.

```bash
git add app/gui/templates/cost.html app/gui/estimate_io.py tests/gui/test_costs_routes.py tests/gui/test_estimate_routes.py tests/gui/test_estimate_io.py
git commit -m "ledger: Cost — estimate / what we assumed / how much changes each month; earlier copies; AWS"
```

---

### Task 8: The job form (create / edit)

**Files:**
- Modify: `app/gui/templates/job_form.html` (words only + Ledger band grammar; field ids, names, order unchanged)
- Modify: `app/gui/routes.py` `_RETENTION_WAS`/`_CHANGE_WAS` strings only if they contain banned words (`grep -n "assumption\|old versions" app/gui/routes.py | sed -n 1,20p`)
- Test: `tests/gui/test_job_form_routes.py`, `tests/gui/test_static_app_js.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gui/test_job_form_routes.py`:

```python
def test_job_form_keeps_its_fields_and_uses_plain_words(full_app):
    body = full_app.test_client().get("/jobs/new").get_data(as_text=True)
    for fid in ("job-form", "sched-builder", "sched-input", "source-input", "source-tree", "est-form"):
        assert f'id="{fid}"' in body, fid
    for name in ("name", "type", "source", "schedule", "storage_class", "retention_type", "dedicated", "bucket"):
        assert f'name="{name}"' in body, name
    assert "assumption" not in body and "old versions" not in body
    assert "How much changes each month" in body and "earlier copies" in body
```

- [ ] **Step 2: Run to verify it fails** — `python3 -m pytest -q tests/gui/test_job_form_routes.py -k plain_words 2>&1 | tail -3`.

- [ ] **Step 3: Rework `job_form.html`**

Keep every `<input>`, `<select>`, `<fieldset>`, id, name, `data-*` and the `<noscript>` recalc button exactly. Change: the change-rate `legend` → `{{ vocab.CHANGE_EACH_MONTH }}`; "assumption" (2) → "what we assumed"; "old versions" (8, Plain copy keep-rule copy and the S3 preview include) → `{{ vocab.EARLIER_COPIES }}`; wrap each step of the form in a `band` with its `slabel` in the margin (Name and kind / Source / Schedule / Storage tier / What it keeps / Cost); remove `→` from link text. `_s3_preview.html` and `_s3_editor.html` are Setup partials included here: they are exempt from the words rule only on Setup pages, so on this page the preview's "old versions" (2) must also read `{{ vocab.EARLIER_COPIES }}` — change them in the partials (Setup keeps its *names* but "earlier copies" is a permitted plain word there too).

- [ ] **Step 4: Run, fix pinned text, suite, commit**

Run: `python3 -m pytest -q tests/gui/test_job_form_routes.py tests/gui/test_static_app_js.py tests/gui/test_s3_rules_screen.py tests/gui/test_s3_rules_final_wave.py tests/gui/test_vocabulary.py -k "form or app_js or s3_rules or plain_words" 2>&1 | grep -E "FAILED|passed"`; update expectations; `python3 -m pytest -q 2>&1 | tail -3`.

```bash
git add app/gui/templates/job_form.html app/gui/templates/_s3_preview.html app/gui/templates/_s3_editor.html app/gui/routes.py tests/gui/test_job_form_routes.py tests/gui/test_s3_rules_screen.py tests/gui/test_s3_rules_final_wave.py
git commit -m "ledger: the job form — same fields, plain words, ruled bands"
```

---

### Task 9: Setup screens — trim, keep the names

**Files:**
- Modify: `setup.html`, `config.html`, `permissions.html`, `s3_rules.html`, `provision_home.html`, `provision_automated.html`, `provision_manual.html`, `provision_scripted.html`, `about.html`, `_access_key_help.html`, `error.html`
- Test: `tests/gui/test_readiness.py`, `test_config_routes.py`, `test_permissions_routes.py`, `test_permissions_surfaces.py`, `test_s3_rules_screen.py`, `test_provision_routes.py`, `test_about_routes.py`, `test_errors.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/gui/test_readiness.py`:

```python
def test_setup_screens_drop_arrows_and_say_aws(full_app):
    client = full_app.test_client()
    for url in ("/setup", "/setup/destination", "/setup/keys", "/setup/permissions", "/setup/storage", "/setup/about"):
        body = client.get(url).get_data(as_text=True)
        assert not re.search(r">[^<]*→\s*</a>", body), f"arrow on a link on {url}"
        assert "Amazon" not in body, url
```

(add `import re` at the top if missing; `full_app` from `test_vocabulary`.)

- [ ] **Step 2: Run to verify it fails** — `python3 -m pytest -q tests/gui/test_readiness.py -k arrows 2>&1 | tail -3`.

- [ ] **Step 3: Trim each Setup template**

Rules for every file: keep technical names (bucket, versioning, IAM, S3 rules, OpenTofu where exempt); remove `→` from link text; "Amazon" → `{{ vocab.AWS }}` (`config.html`: "not by Amazon" → "not by AWS"); delete hints that only restate a label (e.g. `setup.html`'s "Set up the destination → · Keys & secrets → …" line becomes a plain list of links; `provision_home.html`'s three mode descriptions stay since they decide a choice); keep every `details` block collapsed as today; wrap each screen's sections in `band` + margin `slabel`. `error.html`: `h1` + the message in a `sig sig-failure`. `about.html`: the licence table now lists the fonts (Task 2) — check it renders them in the `defgrid`.

- [ ] **Step 4: Run the Setup tests, fix pinned arrows/words, suite, commit**

Run: `python3 -m pytest -q tests/gui/test_readiness.py tests/gui/test_config_routes.py tests/gui/test_permissions_routes.py tests/gui/test_permissions_surfaces.py tests/gui/test_s3_rules_screen.py tests/gui/test_provision_routes.py tests/gui/test_about_routes.py tests/gui/test_errors.py 2>&1 | grep -E "FAILED|passed"`; update expectations; `python3 -m pytest -q 2>&1 | tail -3` → all green including every Task-1 rule.

```bash
git add app/gui/templates/*.html tests/gui/
git commit -m "ledger: Setup screens trimmed, names kept, no arrows, AWS"
```

---

### Task 10: Final pass — budgets, screenshots, bats/shellcheck, handoff

**Files:**
- Modify: `docs/superpowers/HANDOFF.md`, `docs/superpowers/BACKLOG.md`
- Create: `docs/superpowers/mockups/screenshots/` (if a browser is available)

- [ ] **Step 1: Prove every rule holds**

Run: `python3 -m pytest -q 2>&1 | tail -3` → all passed, count ≥ 2000.
Run: `python3 - <<'EOF'
from tests.gui.test_vocabulary import hint_count
from app.gui import vocab
for t in vocab.DAILY_TEMPLATES: print(f"{hint_count(t):2d}  {t}")
EOF` → every line ≤ 3.
Run: `bats tests/bats/ | tail -2` and `shellcheck setup.sh scripts/*.sh scripts/lib/*.sh tests/smoke/run.sh tools/unraid/*.sh` → unchanged (no shell was touched; confirm).

- [ ] **Step 2: Screenshots at three widths in both themes (when a browser exists)**

```bash
B=$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)
if [ -n "$B" ]; then
  mkdir -p docs/superpowers/mockups/screenshots
  (cd /home/paymon/src/backup-engine && CONFIG_DIR=$PWD/.tmp-shot/config CACHE_DIR=$PWD/.tmp-shot/cache python3 -m flask --app app.gui run -p 8098 >/dev/null 2>&1 &) ; sleep 2
  for w in 400 1024 1920; do for p in "" jobs activity cost explore setup how-it-works; do
    "$B" --headless --disable-gpu --window-size=${w},1400 --screenshot=docs/superpowers/mockups/screenshots/${p:-board}-${w}.png "http://127.0.0.1:8098/${p}" 2>/dev/null
  done; done
  pkill -f "flask --app app.gui run -p 8098"; rm -rf .tmp-shot
else
  echo "no browser on this machine: screenshots skipped; the owner reviews on the box after deploy" | tee -a docs/superpowers/HANDOFF.md
fi
```
(Chromium has no dark-mode flag that is reliable across versions; the dark theme is reviewed in the owner's browser. Note that in the handoff.)

- [ ] **Step 3: Inventory the safety copies and update the handoff**

Run: `ls app/gui/templates/*.bak app/gui/static/*.bak | wc -l` and paste the list into `docs/superpowers/HANDOFF.md` under a new heading "Safety copies (delete when the owner says so)" with the one-line command to remove them: `git rm app/gui/templates/*.bak app/gui/static/style.css.bak && sed -i '/^\*\.bak$/d;/safety copies of the pre-Ledger/d' .dockerignore`. Record the tag `ui-before-plain-words`. Move "Current state" to: plain-words + Ledger complete on branch, awaiting owner merge/deploy. In `BACKLOG.md` add: explicit theme toggle; fourth Board column above 1920px.

- [ ] **Step 4: Commit**

```bash
git add -f docs/superpowers/HANDOFF.md docs/superpowers/BACKLOG.md docs/superpowers/mockups/screenshots 2>/dev/null
git commit -m "docs: handoff + backlog after the plain-words + Ledger redesign; safety-copy inventory"
```

Then hand the branch to `superpowers:finishing-a-development-branch` (merge, push and deploy are the owner's calls).
