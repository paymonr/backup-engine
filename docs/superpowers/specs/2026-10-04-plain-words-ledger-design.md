# Plain words + Ledger: the backup-engine redesign — design spec

_2026-10-04. Owner decisions are recorded as made; this spec is the brief for the implementation plan._

## 1. Why

The owner's words (2026-10-03): "as the app stands, it's a bit too complicated looking." Pressed on what
that means, the answer was **the words, not the layout**: too much explaining on every screen, and too
much jargon. Then, separately: run a design check and make it look better, as a **new visual direction**
rather than a polish of Night Shift.

**Who the screens are for:** someone who has never used AWS (knows "backup", "copy", "restore", and
that it costs money) and the typical Unraid tinkerer (comfortable with Docker and shares, has an AWS
account, does not want to think about S3 day to day). Not the owner alone.

**Success:** a daily screen says what it needs to in labels and figures, with at most three one-sentence
hints; the S3 mechanics are one click away and named correctly when shown; the whole app has one look,
one content width, and works in light and dark following the device. Nothing behind the screens changes.

## 2. Decisions (owner, with dates)

| # | Decision | Date |
|---|---|---|
| D1 | The complaint is jargon + explanation, in equal measure (not screens, density or look). | 2026-10-03 |
| D2 | Audience: never-used-AWS **and** Unraid tinkerer. | 2026-10-03 |
| D3 | Mechanics hidden by **plain daily screens, technical Setup** (option C), plus a few "?" marks. | 2026-10-03 |
| D4 | Keep the originals: changed templates get a `.bak` sibling, temporarily; plus a git tag. | 2026-10-03 |
| D5 | Approach: **glossary first, then rewrite**, enforced by tests. | 2026-10-03 |
| D6 | Vocabulary: change only rows 2, 8, 13, 14 of the table (§3); keep every other current name. | 2026-10-03 |
| D7 | Explanation budget: three hints per daily screen; "How it works" page; five "?" places. | 2026-10-03 |
| D8 | Tone **A, terse**: labels and figures, no sentence under section headings. | 2026-10-03 |
| D9 | Visual: **new direction** (B), not a Night Shift polish. | 2026-10-03 |
| D10 | Theme: **follows the system**, light and dark both designed. | 2026-10-03 |
| D11 | Direction: **Ledger**, with a **left sidebar** for the screens. | 2026-10-04 |
| D12 | Wide screens: one centred content column, **1440px cap on every screen**; the Board tiles jobs. | 2026-10-04 |
| D13 | Palette: **Linen & wine**. | 2026-10-04 |

Mockups the decisions were made on (private artifacts; copies in `docs/superpowers/mockups/`):
`2026-10-03-job-tones.html` (today / terse / one quiet sentence) and
`2026-10-04-ledger-directions.html` (Ledger with sidebar, Signal, Workbench, Ledger Board, four palettes).

## 3. Vocabulary

Daily screens: Board, Jobs list, Job, Activity, run record, Explore, Restore, Cost. Setup and its
sub-screens (permissions, S3 rules, storage, provisioning, config, about) keep their technical names.

Changes (everything else keeps its current name, including restore point, undo window, the tier phrases with their constants, bucket, S3, versioning, keep rule, streak, needs you):

| # | Today | From now on | Where |
|---|---|---|---|
| 2 | old versions, noncurrent versions, delete markers | **earlier copies** ("earlier copies of replaced or deleted files") | daily screens and the storage summary lines in Activity |
| 8 | Amazon | **AWS** | everywhere Amazon appears as the actor or the biller |
| 13 | scratch folder | **temporary folder** | Job page, restore |
| 14 | "The model says", "assumptions", "change rate" | **estimate**, **what we assumed**, **how much changes each month** | Board, Job, Cost |

Rules:
- `app/gui/vocab.py` gains these as named strings; templates and JSON labels import them (one name
  per concept, as today).
- `tests/gui/test_vocabulary.py` gains a **daily-screen banned list**: `old version`, `old versions`,
  `delete marker`, `noncurrent`, `Amazon`, `scratch folder`, `The model says`, `assumption`,
  `change rate`, applied to the daily templates and the Activity/sysop log lines. Setup templates are
  exempt. The existing engine-word list stays.
- The storage summary's log lines (`storage_summary.describe`) and the Job page "What it did" line
  switch to "earlier copies". JSON endpoints and persisted files do not change.

## 4. Explanation budget and the "How it works" page

- **Per daily template, at most three elements with class `hint`** (`p`, `span` or `div`), each one
  sentence. A hint survives only if it says something a label cannot. The footer line does not count;
  the collapsed tool detail does not count; table cells carrying provenance ("Walked the folder on
  19 Sep") are data, not hints, and use a different class (`when`).
- **Safety sentences stay and do not count**, one sentence each, class `safe`: a restore never
  overwrites the folder it protects; deleting a job does not delete its bucket; a Snapshot job on a
  slow tier will not run; the "Nowhere to put restored files yet" guard.
- **Notices show state, not teaching.** A notice (`sig`) appears only when something needs the owner
  or failed. Informational notes (e.g. "Per-job is not available…") become one plain line or go.
- **One page, `/how-it-works`**, linked from every footer and from the "?" marks (anchors). Sections,
  in order: three kinds of job; restore points and the keep rule; earlier copies and the undo window
  (and what the storage summary counts); storage tiers; where numbers come from (the measured /
  assumed / invoiced / projected marks, moved from the footer legend); the notices (the six levels,
  moved from the footer legend); what actually runs (restic and rclone named, pointer to About).
- **"?" marks**, five places only: Restore points (Job page figure), Keep rule and Storage tier (Job
  page setup list), Needs you (Board), the Estimate figure (Cost and Board). A "?" is a link to the
  matching anchor; nothing opens in place.
- **Footer** on every screen becomes one line: "Times are UTC. · How it works · About and licences".
  The "How to read this screen" legend is removed from every footer.
- **Setup** keeps its names and gets the same trim: hints that restate a label go; long procedures stay
  collapsed (`details`), like the access-key help today.
- **Test:** `test_vocabulary.py` gains a per-template hint count for the daily templates (≤ 3) and
  asserts `/how-it-works` renders every anchor the "?" marks point at.

## 5. Tone

Terse (D8). Section headings carry a label, optionally a short summary figure or a link ("13 OK, 2
failed"; "Edit"). No sentence under a heading. No arrows appended to link text. No all-caps labels.
Figures first, words second.

## 6. Visual system: Ledger, Linen & wine

The backup as a logbook you trust. Ruled rows, serif figures, a margin for section titles.

### 6.1 Layout
- **Sidebar** (left, 210px, a slightly darker linen) holds the brand and the five screens: Board,
  Cost, Activity, Explore, Setup; the active screen is marked by a wine rule on its left edge. Under
  820px the sidebar becomes a top row with the screens inline and the active one underlined.
- **Content column**: centred to the right of the sidebar, **max-width 1440px** on every screen
  (D12), side padding `clamp(16px, 3vw, 40px)`. Reading text ≤ 65ch.
- **Bands**: each section is a two-column grid, a 170px margin for the serif title (and its summary
  or link, stacked under it) and the content to the right, separated from the previous band by a
  1px rule. Under 640px the margin collapses above the content. Band rows use margins, not grid
  row-gap (the mockup bug of 2026-10-04).
- **Job page**: head (state, serif name, kind line, path line, actions right); figures row (Last run,
  Took, Next run, Streak, Restore points) as ruled cells; then main column (Last 30 runs; Get data
  back; What this job costs; How it is set up; Tool detail collapsed) with the Recovery readiness
  rail at 340px (380px above 1500px). Under 820px the rail moves above the main column.
- **Board**: verdict (serif sentence with a left rule in the state colour, next-run line, one
  button); Needs you; **job tiles** worst first, `repeat(auto-fill, minmax(360px, 1fr))`, each tile
  = state, serif name, kind line, three ruled figures (Last run, Next run, Size with monthly cost),
  the 14-run strip; a key line; What it costs (four figures, table). Three tiles across at 1440px.
- **Other screens** follow the same band grammar: Activity (filters in the margin's content row, the
  table in a band), Explore (restore-point picker as a figure row, the listing in a band), Cost (the
  levers in bands, charts untouched in markup, restyled through tokens), Jobs list (tiles as on the
  Board), run record (figures row + log band), Restore (a form in bands), Setup screens (bands;
  forms follow §6.4). The job form keeps its field order and ids.

### 6.2 Type
- **Fraunces** (serif, optical size axis) for the job name, section titles, and figures; weight 500,
  letter-spacing −0.01em; figures `font-variant-numeric: tabular-nums`.
- **Source Sans 3** for everything else, 15.5px base, line-height 1.5.
- **IBM Plex Mono** for paths, ids, cron, the price stamp and the tool command only.
- Fonts are **bundled** in `app/gui/static/fonts/` (all three are SIL OFL; add them to About and
  licences) with `font-display: swap` and real fallback stacks (Georgia / system-ui / ui-monospace).
  No Google Fonts link in the shipped app: the GUI must render on a LAN with no internet.

### 6.3 Tokens (Linen & wine)

Light, on bare `:root` (the default): `--bg #F2EFE8`, `--surface #FAF8F3`, `--surface-2 #E8E3D9`,
`--side #E9E5DC`, `--ink #2B2522`, `--muted #6A615B`, `--line #D6D0C6`, `--rule #B8B0A4`,
`--accent #8A2F3C`, `--accent-ink #FBF4F2`, `--ok #2E7D5B`, `--warn #B07A1B`, `--warn-bg #F3E8D2`,
`--danger #A8392F`.

Dark, under `@media (prefers-color-scheme: dark)` guarded as `:root:not([data-theme="light"])` and
again under `:root[data-theme="dark"]` (so a future explicit toggle wins both ways), with
`color-scheme: dark`: `--bg #1B1715`, `--surface #221D1A`, `--surface-2 #2B2520`, `--side #130F0E`,
`--ink #ECE6E0`, `--muted #A79E96`, `--line #352E29`, `--rule #463D37`, `--accent #D98A95`,
`--accent-ink #1F1012`, `--ok #5BBE8E`, `--warn #D9A64A`, `--warn-bg #2A2416`, `--danger #E27B70`.

Status colours (ok / warn / danger) are the same hues in every screen; the accent is never used for
state. Provenance marks (measured / assumed / invoiced) keep their current underline grammar in the
status hues at 55% alpha. Radius 3px (2px on small elements). No shadows.

### 6.4 Components
Buttons (1px rule, transparent; primary = wine fill, light ink); figures (`.fig` k/v/s, serif v);
run strip (30 cells, 3px gap, state colours, older cells at 55%) and duration bars; notice (warn
background, 3px left rule in the state colour, no icon); tables (ruled rows, muted headers, no
zebra); definition list (`dl.setup`, muted dt, two columns collapsing to one under 520px); readiness
rail (dot + k/v/t rows, one full-width button); job tile; the "?" mark (1.05rem circle, muted); tool
detail (`details`, mono `pre` on `--surface-2`); footer line; form controls (ruled inputs on
`--surface`, wine focus ring, labels above fields, the number-input rules of the form gotcha memory
unchanged).

### 6.5 What stays
`app.js` behaviours (progress polling, countdowns, copy buttons, confirm-by-name, the form
recalculation) keep their hooks and data attributes. The estimator math, every JSON endpoint, every
persisted file, and the Setup screens' technical names do not change. The vocabulary module remains
the one source of user-facing names.

## 7. Safety copy (D4)
Before the first template change: `git tag ui-before-plain-words` on master. Every template or static
file that changes gets a `<name>.bak` sibling (the pre-change content) kept until the owner says the
backups can go; `*.bak` is added to `.dockerignore` so no image ships them. The old `style.css`
becomes `style.css.bak`; the new stylesheet replaces it under the same name so no template references
change.

## 8. Order of work
1. Foundations: tag + `.bak` rule; `vocab.py` additions; the two new test rules (daily banned list,
   hint budget) written **failing** against today's templates; fonts bundled; `style.css` rewritten
   to the Ledger tokens and components with `base.html` carrying the sidebar and the one-line footer;
   `/how-it-works` page and route.
2. Job page (sets the pattern): wording trim to budget, "?" marks, Ledger layout.
3. Board and Jobs list (tiles).
4. Activity and run record.
5. Explore and Restore.
6. Cost.
7. Job form (create/edit), in Ledger form styling, no field changes.
8. Setup screens: trim + restyle, names kept.
9. Final pass: every template under budget, vocabulary test green, screenshots of each screen in
   light and dark at 400px, 1024px and 1920px, suite green, `.bak` inventory listed for the owner.

Each step is one reviewed task; the suite is green between steps.

## 9. Out of scope (backlog)
An explicit light/dark toggle in Setup; a fourth Board column above 1920px; per-job share of the
bill (needs billing data the app does not have); any change to what the screens *do*.
