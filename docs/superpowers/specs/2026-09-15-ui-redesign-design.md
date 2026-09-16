# backup-engine UI redesign — implementation spec

Date: 2026-09-15. Status: decided; ready to implement. Audience: an implementer with no other context.

backup-engine is a Dockerized Unraid → AWS S3 backup tool: a Python/Flask/Jinja GUI in `app/gui`
(port 8099), a bash runner `scripts/backup-job.sh`, engines restic (versioned snapshots), rclone
(plain copy, incl. Deep Archive) and the app's own file-history catalog, and a validated cost
estimator in `app/estimator`. One container, one user (the owner, at home). This document specifies
the complete redesign of the GUI and the engine work it needs, in one increment.

## 1. How to use this document

### 1.1 What is decided and what is not

Everything in sections 2–12 is decided. The owner made three shaping decisions and one direction
choice (section 2), a design panel and audit produced the principles (section 3), and every open
question the source extracts raised has been ruled on — the rulings are in Appendix B. Do not re-open
them; if you find a genuine contradiction that Appendix B does not cover, pick the option that keeps
the app honest (section 3.1, principle 5 and 7) and note it in your commit message.

Nothing in this document is a placeholder. Copy strings are exact and must ship verbatim unless a
live value is interpolated (marked `<like this>`). Dollar figures, dates and sizes in examples are the
owner's example content (section 1.5) — the live app computes its own.

### 1.2 Where the visual truth lives

Two mockups were built on identical real content; the owner looked at both and chose. They are the
visual truth for sizes, weights, spacing and colour. When this document and a mockup disagree on a
CSS value, the mockup wins (ruling R6); when they disagree on copy or behaviour, this document wins.

| Source | File | Published |
|---|---|---|
| Night Shift (the skeleton, every screen except create/edit job) | `docs/superpowers/specs/2026-09-15-ui-redesign-sources/mockup-night-shift.html` | https://claude.ai/artifact/VhZDuSCap8hDmQEnK3B436 |
| Night Shift direction (behaviour for screens the mockup did not build) | `docs/superpowers/specs/2026-09-15-ui-redesign-sources/direction-night-shift.json` | — |
| Ledger Runbook blend (ONLY its create-job screen, `<main id="screen-new">`) | `docs/superpowers/specs/2026-09-15-ui-redesign-sources/mockup-ledger-runbook.html` | https://claude.ai/artifact/RsPnaRKN7L7jQxJrzGx6Vr (third tab, "New job") |
| Blend direction (create-job behaviour) | `docs/superpowers/specs/2026-09-15-ui-redesign-sources/direction-blend.json` | — |
| UI audit (the 12 problems, the preserve list, the 7 principles) | `docs/superpowers/specs/2026-09-15-ui-redesign-sources/ui-audit.json` | — |

The Night Shift mockup contains a "New job" screen (lines 872–1055): it is superseded by the blend's
create-job screen and must not be built. The blend mockup contains Status, Job and Cost screens: they
were not chosen and must not be built. Appendix A maps every component of the two chosen surfaces to
the section that specifies it.

### 1.3 Branch, tests, deploy

- Start from branch `impeccable-versioned-files` (HEAD `7d1a181`). `master` (`ffc9a6b`) is a strict
  ancestor of it; nothing on master is missing. Create a new branch off it (suggested name
  `ui-redesign`). Work in commits per task (section 11); the owner wants frequent commits.
- The branch deliberately removed the old two-pipeline (appdata/media) model and its media-shares
  screen: there is no `/shares` route and no `app/gui/media_shares.py`. "Media shares" is not a screen
  in this redesign (ruling R5). The job form's folder browser (`app/gui/static/app.js:14-64`,
  `app/gui/fsbrowse.py`, `app/gui/dirsize.py`) replaced it and is preserved inside the create/edit
  screen.
- `docs/` is gitignored (`.gitignore:39`): add this spec and any plan with `git add -f`.
- Test commands (all must be green before every commit that touches the area):
  - Python: `python3 -m pytest -q --deselect tests/estimator/test_billing.py::test_forecast_parses`
    (today: `495 passed, 1 deselected`; the deselected test is date-sensitive and unrelated).
  - Shell: `bats tests/bats/` and `shellcheck scripts/*.sh scripts/lib/*.sh`.
  - Integration (needs restic/rclone/aws/minio on the dev box; docker is absent there): `bats tests/integration/`.
- Deploy is gated and the OWNER runs it; you prepare and ask. The procedure: `git push`, then
  `rsync` the tree to `root@192.168.1.227:/root/backup-engine-build/`, then run
  `/root/deploy-backup-engine.sh` on the box, which retags the old image, runs
  `docker build -t backup-engine:cost`, recreates the container from its own `docker inspect` spec,
  health-gates `http://localhost:8099/` (expects 2xx/3xx immediately after recreate, with whatever
  `/cache` and `/config` contain) and auto-rolls back on failure. One manual step precedes the first
  deploy of this work: the owner adds the new read/write path mapping `/mnt/user/restore` → `/restore`
  to the container template once (section 7.5.1), because the deploy script recreates the container
  from the existing spec.
- The GUI has no login and is LAN-only; nothing in this redesign adds authentication.

### 1.4 Reading order

Section 4 (vocabulary, states, severity, provenance) is the dictionary for everything after it.
Section 7 (engine) is what the screens in section 5 read. Section 8 lists every JSON contract.
Section 11 gives the build order.

### 1.5 The example content

All examples use the owner's two real jobs. Use them in tests and fixtures.

| | `appdata` | `manga` |
|---|---|---|
| Type | Snapshot backup (`versioned`, restic) | Plain copy (`archive`, rclone) |
| Source | `/mnt/user/appdata_backups` | `/mnt/user/data/media/comics/mangas` |
| Storage tier | Instant · `STANDARD` | Thaw first, hours · `DEEP_ARCHIVE` |
| Measured | 52.71 GB · 533 files (walked 14 Sep 07:40) | 1.78 TB · 232,021 files |
| Schedule | every day at 05:00 | every Sunday at 04:00 |
| Keep rule | last 3 · daily 7 · weekly 4 · monthly 6 (14 restore points now, back to 18 Mar 2026) | keep for 180 days |
| Change rate | ~1% (assumed) | 0% |
| Last run | Tue 15 Sep 05:00, OK, 4 m 12 s (typical ~4 m 08 s), restore point `a81f3c2e` | Sun 13 Sep 04:00, FAILED after 1 h 07 m, exit 1, `AccessDenied: s3:DeleteObjectVersion` (the prune step; typical ~2 h 12 m) |
| Cost | $2.69/mo once settled (month 6); first bill $1.39 | $1.85/mo |
| Account | model $4.54/mo · last invoice $3.98 (August 2026) · +14.1% | |

The manga failure is the example configuration blocker: the runtime key was created before the IAM
policy gained `s3:ListBucketVersions` and `s3:DeleteObjectVersion`, so the run's prune step is refused.
Section 7.1.6 makes a prune failure a recorded failure (today it is a warning nobody sees).

## 2. Owner decisions and non-goals

### 2.1 Verbatim decisions

Request: "Use some design sense to redesign the UI of this app and the way information is displayed."

Three shaping decisions, fixed:

1. Restore is a GUI operation. Each job's page lists its points in time and can restore a point in
   time, warm up (thaw) and download via the existing `scripts/restore.sh`, with honest cold-storage
   warm-up warnings. This is new feature work on top of the presentation redesign.
2. Home is split: a health/status summary on top (did last night run, when, how long, next run,
   failures with a path to the cause) and a cost summary below, each linking to its full view.
3. Self-explanatory for the owner's future self on a fresh machine: plain language throughout;
   restic/rclone/S3 terms demoted to expandable detail; the recovery passphrase, the shared store /
   tag model and warm-up times explained where they matter.

Direction choice (2026-09-15, verbatim): "I want the skeleton of Night Shift. The new job creation
from Ledger Runbook. And the run strip from Night Shift. I understand that the run strip from Night
Shift will require a lot more backend engine work to store that data. Please write it up so I can pass
it over to another model to complete."

Binding interpretation:

- SKELETON = Night Shift: sitemap, navigation, URL scheme, every screen's layout and information order
  except create/edit job, the state vocabulary, the severity split (prospective configuration blockers
  vs retrospective run failures), the provenance rule ("a projection inherits the weakest provenance
  of its inputs"), the type/colour system and the copy voice.
- CREATE/EDIT JOB = the blend mockup's create-job screen, exactly: RECOMMENDED block that prints the
  rule that fired; storage-class table priced for the measured folder; change rate asked directly
  above the keep rule it moves; keep rule kept at 0% change with its non-cost justification; WHEN
  (first bill / typical month / first 6 months) × WHOSE (this job / all jobs) never mixed in one row;
  sticky footer; bundled-vs-live price stamp toggle; blockers with a logged override.
- RUN STRIP = Night Shift's 14-cell strip per job, with the engine work to record run history.
- Where Night Shift and the blend vocabulary conflict, Night Shift wins everywhere except on the
  create/edit-job screen (section 4.3 lists the two places that differ).

### 2.2 Not chosen — do not import

The Runbook direction's margin column; Quiet Ledger's ledger rows as the skeleton; the blend mockup's
Status, Job and Cost screens; the Night Shift mockup's New job screen; the mockups' screen-switcher
(a demo device — the real nav is section 4.1).

### 2.3 Must not change (from the audit's preserve list)

- The estimator's math: `app/estimator/model.py`, `tiered.py`, `prices.py`, `usage.py`, `billing.py`,
  `schedule.py`. `app/gui/estimate_io.py` may be extended (new adapter functions, more calls into the
  unchanged model). `tests/estimator/` stays untouched and green; section 10.3 adds a guard.
- Live, debounced (250 ms) recompute that degrades to `—` rather than to a wrong number, never blocks on
  the folder walk, and keeps a `<noscript>` fallback.
- The provisioning safety model (`app/gui/provision.py`): probe list→put→get→delete before anything is
  saved; transient admin credentials only in the form and the subprocess env, zeroed in `finally`,
  never logged or templated; scrubbed tool stderr shown in an open `<details>`; failures re-render the
  same page with HTTP 400 and save nothing; CSRF on every POST; `is_provisioned()` treats a
  `changeme` bucket as unset.
- Write-only secrets with per-key status, the `'••••• (unchanged)'` placeholder, the separate read-only
  Cost Explorer credential, the 600-mode check.
- The schedule builder (`app.js:66-128`): builder ↔ cron round-trip with range checks, raw field
  fallback for exotic expressions.
- Conditional visibility `data-when-*` (`app.js:502-556`), including snapping an orphaned `tiered`
  selection to `days`; `[hidden]{display:none !important}` stays.
- The folder browser (`app.js:14-64`, `fsbrowse.py`, `dirsize.py`): confined to `SOURCE_ROOT`,
  directories only, `..`/absolute rejected.
- The dark token set (section 6.1), the eyebrow → h1 → lead opening on reading-width pages, tabular
  numerals on numeric columns, one global `:focus-visible` ring, `prefers-reduced-motion` respected.
- The persisted job schema's existing fields and `jobs_io.validate`'s rules (new fields are added,
  none removed); the per-job state file `$CACHE_DIR/state/<job>.json` keeps being written with the
  same keys (`last_run`, `outcome` `success|failure`, `type`, `snapshot_id`, `duration_s`, `error`,
  `exit_code`); `restore.sh`'s per-type dispatch is surfaced, not reimplemented.
- The honest copy where it exists: "Assumed, not measured", "cost estimate only — the tool copies files
  as they are and won't bundle for you", "this container never sees them", "never the runtime backup
  key", "measurement may be incomplete", "Couldn't finish measuring this folder".

## 3. Principles and the problems they fix

### 3.1 The seven principles (from the audit, applied)

1. The job is the object, and restore is one of its verbs. One page per job owns identity, health,
   schedule, settings, projected cost, measured spend, run history and restore; the Board is a roll-up.
2. One surface answers one question; the two axes of cost (WHEN × WHOSE) are never mixed in one row.
3. A control and the number it moves are on screen together; a panel that cannot recompute does not
   belong on a live page.
4. The model's causality is expressed in the structure, not in prose: the change-rate assumption sits
   directly above the keep rule it gives meaning to, and the keep rule shows its consequence in place.
5. Every number states where it came from and how sure it is (measured / assumed / invoiced /
   projected), and wherever a projection and an actual coexist the delta is shown.
6. Settings, measured facts and assumptions are three idioms with three persistence rules; an
   assumption never evaporates (it is stored on the job).
7. One severity scale with real consequences (a blocker blocks; success and failure never share a
   shape; long actions show progress; failures land in-app with input intact) and one name per
   concept, in the owner's vocabulary, with implementation names demoted to expandable detail.

### 3.2 Traceability: audit problem → the sections that resolve it

| # | Problem (short) | Resolved by |
|---|---|---|
| 1 | A job is not an object; identity scattered under four identifiers | 4.1 (sitemap, `/jobs/<name>`), 4.2 (single identifier rule), 5.2 (Job page), 5.3 (Run record) |
| 2 | Restore exists in the engine, absent from the GUI | 5.4 (Get data back), 7.5 (restore execution), 7.6 (warm-up), 5.2 rail |
| 3 | Cost tiles mix WHEN × WHOSE; two cost screens, two vocabularies | 5.8 §3.6 (WHEN × WHOSE), 5.6 (Cost workbench), 4.3 (vocabulary), 5.1 band 4 |
| 4 | Retention presented as a live lever but inert at 0% change | 5.8 §3.4–3.5 (change rate above keep rule; inert rendering with justification) |
| 5 | Cause and effect never visible together; sticky output buries the form | 5.8 §7 (sticky footer that cannot grow), 5.6 band 3 (levers docked in the result) |
| 6 | Settings, measured facts and assumptions share one idiom; assumptions not persisted | 5.8 §1 (the three idioms stated once), 7.8 (`assumptions`, `measured` on the job), 5.9 (edit diff) |
| 7 | No provenance; placeholder looks like a measurement; model never compared with the invoice | 4.6 (provenance scale), 5.1 band 4 and 5.6 band 1 (delta line), 5.8 §8 (price stamp) |
| 8 | One `.advice-item` for guidance, warnings and blockers; a known-failing config saves | 4.5 (severity model), 5.8 §3.3 (WON'T RUN blocks the save; logged override), 9 (flash categories) |
| 9 | Health is one word with no time; no in-progress state; failure is a dead end | 4.7 (job states), 5.1 (Board verdict, table), 5.2 (status strip, failure record), 5.3 (Run record), 7.1–7.3 (run history, RUNNING, OVERDUE) |
| 10 | Setup announced as a sequence then abandoned; "set" cannot tell a placeholder from a secret | 5.10 (Setup readiness), 5.12 (three-state secrets), 7.7 (readiness data) |
| 11 | Vocabulary drifts per screen; implementation names are the labels | 4.3 (vocabulary table), 4.4 (mono law), 5.13 (About as the glossary), 10.3 (vocabulary test) |
| 12 | No severity/progress/failure vocabulary in the shell; 14 `abort()`s; banner devalues amber | 4.5, 6.4 (work bar, change flash), 9 (error pages, flash categories), 5.15 |

## 4. Information architecture

### 4.1 Sitemap, navigation, URL scheme

Primary navigation, in this order, on every page: **Board · Cost · Activity · Setup**. "+ New job" is a
button on the Board's Jobs band head (ruling R4), not a nav item. The brand `backup-engine` links to `/`.

| Route | Name | Container | Question it answers | Replaces |
|---|---|---|---|---|
| `GET /` | Board | dense 1240px | Did my backups run last night, and is anything wrong right now? | `index()` redirect + `/jobs` table (`routes.py:20-26,159-164`) |
| `GET /jobs` | — | — | 301 → `/` | |
| `GET /jobs/<name>` | Job page | dense + 304px rail | Everything about this one job | NEW |
| `GET /jobs/<name>/runs/<run_id>` | Run record | read 780px | What happened in this run, and why did it fail? | NEW |
| `GET /jobs/<name>/restore` | Get data back (the confirmation; `POST` to the same path starts the work and redirects to the run record, which is the live operation) | read 780px | What exactly is about to happen, what will it cost, how long will it take? | NEW |
| `GET /jobs/new`, `GET /jobs/<name>/edit`, `POST /jobs` | Job setup | form 960px | (create/edit — section 5.8/5.9) | existing (`routes.py:178-197,244-278`) |
| `GET /cost` | Cost workbench | dense | Is this model telling me the truth, and what will this cost over time? | `/estimate` (`routes.py:312-342`); `GET /estimate` 301 → `/cost` |
| `GET /activity` | Activity | dense | What has this machine actually done, across all jobs? | the orphaned `/logs` (kept as a raw view) |
| `GET /activity/<run_id>` | System run record | read 780px | What happened in this system operation (usage refresh, billing check, probe, destination setup)? | NEW — the same template as `/jobs/<name>/runs/<id>`, for the records whose `job` is null |
| `GET /setup` | Setup readiness | read | Is this install able to back up — and able to restore? | `/provision` picker's "what next" role |
| `GET /setup/destination` (+ the three paths) | Destination | read | Where do backups go, and can this container write there? | `/provision`, `/provision/manual`, `/provision/manual/render`, `/provision/scripted`, `/provision/validate`, `/provision/automated` (all 301 to the new path; POST targets move) |
| `GET/POST /setup/keys` | Keys & secrets | read | What credentials does this hold, and which are real? | `/config` (301) |
| `GET /setup/about` | About | read | What version is this and what is it built on? | `/about` (301) |

Mutating and JSON endpoints (all listed with their contracts in section 8): `POST /jobs/<name>/run`,
`/pause`, `/resume`, `/delete`, `/restore`, `/thaw`, `/thaw/check`, `/test-restore`,
`/restore-points/refresh`; `GET /status.json`, `/jobs/<name>/status.json`,
`/jobs/<name>/runs/<run_id>.json`, `/jobs/<name>/runs/<run_id>/log`, `/jobs/<name>/restore-points.json`,
`/activity.json`, `/activity/<run_id>.json`, `/activity/<run_id>/log`, `/cost.json`,
`/jobs/estimate.json`, `/jobs/browse`, `/jobs/source-size`;
`POST /costs/refresh`, `/costs/scenario`, `/costs/billing/refresh`, `/setup/probe`,
`/setup/versioning-confirmed`; `GET /logs` (raw tail, unchanged). `GET /estimate.json` stays as an
alias of `/cost.json`. `POST /costs/billing` is **removed**: the Cost Explorer credential is edited in
exactly one place, Keys & secrets (5.12), and two write-only forms over one secret with two save routes
is how a value ends up half-saved. `GET /costs/billing` 301s to `/setup/keys#billing` so any old link
lands somewhere true.

First-run gate: when `config_io.is_provisioned()` is false, `/`, `/cost`, `/activity`, `/jobs/new` and
every job route redirect (302) to `/setup`. `/setup/*` never redirects. The deploy health gate hits `/`
and gets a 302 either way.

Navigation graph (who links to whom):
- Board → job page (row / job name), run record (strip cell, "open the run record →"), `/cost`, `/setup`
  (needs-you rows, the chip), a job page's restore band or rail ("Test a restore →"), `/setup/destination`
  ("Fix the permission →"), `/jobs/new`.
- Job page → run record (ledger cells, failure record), `/jobs/<name>/restore` (the Get data back band's
  GET form: `Start restore →` / `Warm up first — up to 12 h →` / `Download now →`, and the cold-store
  blocker's warm-up link),
  `/cost`, `/jobs/<name>/edit`, `/setup/about` ("What these tools are →"), a sibling job's restore band.
- Run record → its job page, Activity, the fix destination named in its cause/fix block.
- Activity → run record (each row), job page (job tag).
- Cost → job page (every job name), `/jobs/<name>#restore-band` ("Get data back →" per row).
- Setup → `/setup/destination`, `/setup/keys`, `/setup/about`, `/jobs/new`, a job page's rail.

### 4.2 The single identifier rule

The job name is the one identifier of a job: verbatim on the Board, the job page `<h1>`, the cost
tables, Activity, log tags, the S3 URI, the restic tag and the crontab. Everywhere it appears except the
job page's own `<h1>` it is a link to `/jobs/<name>` styled `.jobname` (weight 560, `--ink`, .98rem).
Never split a name on `.` in URLs or JS keys (names may contain dots: `jobs_io.JOB_NAME_RE`).

### 4.3 One name per concept — the vocabulary

Rules: the user-facing name is the only label; the internal term appears only in the "Tool detail"
`<details>` of the job page, inside `<code>` chips, on the About page, in the raw command block and in
the run record's Command row. Storage tiers are always `plain meaning · CONSTANT`, never the constant
alone. "Amazon" in prose, `AWS` only inside constants and chips.

| Concept | User-facing name (Night Shift, global) | Internal term(s) it replaces | Where the internal term may still appear |
|---|---|---|---|
| job type `versioned` | **Snapshot backup** — "a point in time, so you can restore any date" | versioned, restic, "Versioned & encrypted" | Tool detail; create screen `.term` chip `versioned` |
| job type `archive` | **Plain copy** — "a straight copy of big, static files. No history." (ruling R1; never "Bulk copy", never "Mirror") | archive, rclone, sync, "Bulk archive" | Tool detail; `.term` chip `archive` |
| job type `versioned-files` | **File history** — "keeps every version of every file" | versioned-files, catalog, vfiles, manifest | Tool detail; `.term` chip `versioned-files` |
| storage class | **Storage tier**: `Instant · STANDARD`, `Instant, cheaper · STANDARD_IA`, `Instant, archive-priced · GLACIER_IR`, `Thaw first, minutes–hours · GLACIER`, `Thaw first, hours · DEEP_ARCHIVE` | storage class, class | the constant is always shown as a `<code>` chip |
| a restic snapshot | **Restore point** — `Tue 15 Sep 05:00 · a81f3c2e` | snapshot, snapshot_id, latest | Tool detail "Last snapshot id"; run record |
| the shared restic repository | **The snapshot store** — "all snapshot backups share one store, which is why two jobs must not run in the same minute" | restic repo, RESTIC_REPOSITORY, repository lock | Tool detail; Keys & secrets read-only readout |
| `RESTIC_PASSWORD` | **Recovery passphrase** — "without it, no snapshot backup can be read: not by you, not by Amazon, not by anyone" | RESTIC_PASSWORD, encryption key | Keys & secrets shows the env key name beside the plain name |
| retention | **Keep rule** — "how long we keep old versions" | retention, retention policy, keep-policy, prune, rotation | Tool detail; run record Command |
| rotation cost line | **Replacing old versions** | rotation, rotation_monthly | Cost workbench "How this is calculated" |
| ingest | **Uploading** — "charged per request, not per gigabyte, so usually pennies"; one-time → **First upload (once)** | ingest, upfront_onetime | same |
| egress | **Data out of Amazon** | egress, data-transfer-out | same |
| thaw | **Warm up** (verb) / **Warm-up** (noun), always with its duration | thaw, restore-request | Tool detail; run record Command |
| retrieval tier | **Retrieval speed**: `Bulk — cheapest, up to 48 h`, `Standard — up to 12 h` (DEEP_ARCHIVE); `Bulk — cheapest, 5–12 h`, `Standard — 3–5 h`, `Expedited — 1–5 min, costs most` (GLACIER) | tier, Bulk/Standard/Expedited as bare words | inside the warm-up step only |
| S3 prefix | **Where it goes** — literal `s3://<bucket>/media/<job>/` | prefix, sub-prefix | the URI itself is mono |
| churn | **How much changes between runs**; noun **Change rate**; options `Nothing — files only get added (0%)`, `A little — rare replacements (~1%)`, `Some — regular edits (~10%)`, `A lot — churny (~30%)` | churn, change_rate_pct | never |
| bundling | **My files are already bundled (.cbz, tar)** | packing, pack_member_gb | never |
| `keep_all` | **Keep everything** → figures read `Keeps growing — there is no plateau for this job` | unbounded, "steady state" (forbidden for these jobs) | never |
| plateau month | `Settles at $X/mo from month N` | steady_state_monthly, plateau | never |
| job states | `OK · Failed · Running · Paused · Overdue · Not run yet` | success/failure badges, `enabled` | never |
| provisioning | **Set up the destination** (inside Setup) — "a bucket this container can write to and nothing else" | Provision, "Step 1 of 2", tofu | About (OpenTofu) |
| the log | **Activity** — "a feed of operations, each tagged with its job" | logs, tail | `/logs` raw view link from Activity |
| `mirror` flag | **When you delete a file locally**: `delete it from the copy too` / `keep it in the copy` | mirror, sync | Tool detail (the `rclone sync` command) |
| a run of `backup-job.sh` | **Run** (a scheduled run, a manual run) | job execution, cron fire | never |

The two places the create/edit-job screen differs (blend vocabulary wins there, by the owner's rule):

1. Storage tier plain phrases in the class table: `Instant · STANDARD`, `Instant, cheaper to keep ·
   STANDARD_IA`, `Instant, cold price · GLACIER_IR`, `Cold · GLACIER`, `Deepest · DEEP_ARCHIVE`. The
   constant chip is the invariant that ties the two phrasings to one concept.
2. Severity labels on that screen: `Recommended for this folder` (advice level), `Heads up` (warning
   level), `Won't run` (blocker level) — same four channels as section 4.5, different label words.

Job type names do NOT differ: the create screen's radios read `Snapshot backup · versioned`,
`File history · versioned-files`, `Plain copy · archive` (ruling R1 is global).

Type line on the job page header (`.typeline`) and in the Board row hover — the Night Shift sentences,
which are the vocabulary table's own: Snapshot backup — `Snapshot backup — a point in time, so you can
restore any date.`; Plain copy — `Plain copy — a straight copy of big, static files. No history.`;
File history — `File history — keeps every version of every file.` The create/edit screen's radio
`.why` sentences (5.8 §3.1) are the blend's and appear only there; they are never used on a skeleton
screen.

Where the internal term may still appear (the pages the vocabulary test exempts, 10.3): `/setup/about`
(the glossary, exempt from every term); `/setup/destination` and its sub-paths (exempt from `OpenTofu`,
which names the tool that creates the bucket); `/setup/keys` (exempt from `restic` and `repository`,
which appear only inside the env key names `RESTIC_PASSWORD` and `RESTIC_REPOSITORY`). Everywhere else
the internal term is confined to `<code>`, `.term`, `.cmd`, `.errline` and `details.tooldetail`.

Keep-rule sentences by `retention.type`, in two named forms — one concept, two grammars, and every
screen names which one it uses (`vocab.keep_rule_label(retention)` / `vocab.keep_rule_prose(retention)`):

| `retention.type` | `keep_rule_label` — the `·` form, for `.defgrid` rows, table cells and Board hover | `keep_rule_prose` — the comma/"then" form, for inline sentences |
|---|---|---|
| `tiered` | `Last <l> · one a day for <d> days · one a week for <w> weeks · one a month for <m> months` | `last <l>, then one a day for <d> days, one a week for <w> weeks, one a month for <m> months` |
| `days` | `Everything for <N> days` | `everything for <N> days` |
| `count` | `Just the last <N>` | `just the last <N>` |
| `keep_all` | `Keep everything` | `Keep everything` |

`keep_rule_label` is used by 5.2 "How it is set up", the Board row hover and 5.9's `was:` readouts;
`keep_rule_prose` by 5.2's restore-point hint and 8.5's `keep_rule_prose` member. One third form exists
and only on the create/edit screen, where the blend's vocabulary wins: `keep_rule_compact` —
`last 3 · daily 7 · weekly 4 · monthly 6` — inside the tiered option's `.why` line (5.8 §3.5). Those
three are the whole set; nothing invents a fourth.

### 4.4 The mono law

Two families, split by duty. Sans (`--font-sans`) carries language: headings, labels, prose, buttons.
Mono (`--font-mono`) carries every fact the eye scans: clock times, relative times, durations, byte
sizes, money, percentages, restore-point ids, exit codes, cron expressions, paths, S3 URIs, env key
names, tool commands, error strings — all with `font-variant-numeric: tabular-nums`. The state token,
topbar chip, clock, stamps, legends and section counts are mono. A number in a sans element is a bug
(section 10.3 tests it). Nothing on any page is larger than the h1.

### 4.5 The severity scale — the Signal component

Six levels on two axes. PROSPECTIVE levels describe a configuration: they render inline beside the
control that causes them and never carry a timestamp. RETROSPECTIVE levels describe an event: they
always carry a time and a link to the record, and never a fix button **inside the signal box** — when
the failure's error class is recognised, the `WHY THIS HAPPENS` / `WHAT TO DO` columns render directly
beneath the signal (outside `.sig`'s body, in their own `.grid2`), and the fix button lives in the
`WHAT TO DO` column. The signal itself stays a statement of what happened. Every signal encodes four
channels at once — left rule, ground, label word, and a 12×12 stroked SVG glyph in `currentColor`
(never an emoji) — so colour is never the only carrier.

| # | Axis | Class | Left rule | Ground | Label | Glyph | Consequence |
|---|---|---|---|---|---|---|---|
| 1 | — | `sig-note` | none (`border-left:0; padding-left:0`) | none; text `--faint` .88rem | none | none | Ambient explanation; the lowest-cost thing on screen. |
| 2 | prospective | `sig-advice` | 2px `--accent` | `--surface` | `Advice` in `--accent` | hollow circle-dot `<circle cx=6 cy=6 r=5 fill=none stroke-width=1.3/><circle cx=6 cy=6 r=1.5 fill=currentColor/>` | Usually carries a one-click accept. Take it or leave it. |
| 3 | prospective | `sig-warning` | 2px `--warn` | `--warn-bg` | `Warning` in `--warn` | outline triangle `<path d="M6 1.4 11.1 10.6H0.9Z"/><path d="M6 4.6v2.6"/><path d="M6 8.9v.2"/>` stroke 1.3 | Will cost money or surprise you later. Nothing is stopped. |
| 4 | prospective | `sig-blocker` | 3px `--danger` | `--danger-bg` | `Blocker` in `--danger` | filled octagon `<polygon points="4,1 8,1 11,4 11,8 8,11 4,11 1,8 1,4" fill=currentColor/>` | Disables the primary action, names the two ways out, jumps to the control. |
| 5 | retrospective | `sig-success` | 2px `--ok` | `--ok-bg` | `Done` in `--ok` | circle-check `<circle cx=6 cy=6 r=5/><path d="M3.6 6.2 5.3 7.9 8.5 4.4"/>` stroke 1.4 round | Only ever a moment after you press something; auto-dismisses after 7 s. |
| 6 | retrospective | `sig-failure` | 3px `--danger` | `--danger-bg` | `Failed` in `--danger` | stroked octagon with X `<polygon points="4,1 8,1 11,4 11,8 8,11 4,11 1,8 1,4" fill=none/><path d="M4.4 4.4 7.6 7.6M7.6 4.4 4.4 7.6"/>` stroke 1.3 | A run that ended badly: time, verbatim error in mono, exit code, link to the record — inside the box. Cause and fix render under the box as two `.grid2` columns, and the fix button sits in `WHAT TO DO`. Never shares a shape with Done. |

Base `.sig`: `display:grid; grid-template-columns:16px minmax(0,1fr); gap:.75rem; align-items:start;
padding:.75rem 1rem; border-left:2px solid transparent`. `.glyph{margin-top:2px}`; `.label` mono .68rem/600
letter-spacing .1em, block, `margin-bottom:.25rem`; `.body{max-width:74ch}`; `p + p{margin-top:.35rem}`.
Success is otherwise a `tok-ok` token plus a timestamped line; only transient confirmations are boxed.
On the create/edit screen the same component is styled as the blend's `.sev.rec / .sev.heads /
.sev.wont / .sev.note` (section 5.8) with the label words from 4.3.

Blockers "name the two ways out"; warnings say what it will cost or when it will surprise you; notes
explain without asking for anything; empty states are one sentence and one button.

### 4.6 The provenance scale

Every figure states where it came from with a 1px underline style that marks the input class it
inherits, plus a text stamp where the detail matters.

| Mark | Class | Rendering | Meaning | Stamp text forms |
|---|---|---|---|---|
| MEASURED | `.p-measured` | `border-bottom:1px solid var(--prov-measured); padding-bottom:1px`; ink `--ink` | Something walked the folder or read the bucket, on a date it will tell you. | `measured 14 Sep 07:40`, `Walked the folder on 14 Sep 07:40.`, `probed 14 Sep 07:40` |
| ASSUMED | `.p-assumed` | `border-bottom:1px dotted var(--prov-assumed); padding-bottom:1px; color:var(--muted)` — the number itself is dimmer | Rests on a guess you made (change rate, bundling) or on the 20 GB / 1,000-file placeholder. | `assumed — default 20 GB placeholder, not measured`, `Assumed, not measured: …` |
| INVOICED | `.p-invoiced` | `border-bottom:3px double var(--prov-invoiced); padding-bottom:0` | The bill Amazon actually sent (Cost Explorer). | `August 2026 · the real bill`; add `whole account, not just these backups` when not tag-scoped |
| PROJECTED | no class | no underline, `--ink` | Computed by the model from the inputs above. | — |

The inheritance rule: a projected figure carries the weakest provenance of its inputs, on the order
assumed < measured < invoiced. `estimate_io.provenance_of(...)` (section 7.9) therefore returns exactly
two values for a COMPUTED figure — `assumed` when any input is assumed, `projected` otherwise.
`measured` and `invoiced` are reserved for OBSERVED figures: a size something walked, a byte count read
out of the bucket, a number Amazon billed. A projection built only from measured inputs loses its dotted
line and returns to plain `--ink` with **no underline at all** — a solid measured line under a computed
dollar figure would claim that the dollars were observed, which they were not.

Concretely: the size input is `measured` when a `measured` record exists on the job (or the usage cache
holds the prefix), else `assumed`; the change-rate input is `assumed` whenever it is > 0 and the figure
includes any old-versions cost (versioning + rotation > 0); bundling is `assumed` when `bundled` is
true. So for the example: `First bill $1.39`, `By month 6 $2.67` and `Every month after $2.69` are
`assumed` (their version store rests on the 1% guess); `52.71 GB`, `1.78 TB` and `In the bucket now
1.86 TB` are `measured`; `$3.98` is `invoiced`; manga's `$1.85/mo` at 0% change is `projected` and
prints with no mark at all, exactly as the mockups draw it. Measure the folder and set 0% change and
the money figure simply loses its dotted line — that, not a new line, is what "the assumption is gone"
looks like.

A total over several jobs is a computed figure like any other and takes the weakest mark among the jobs
it sums: `$4.54` includes appdata's assumed `$2.69`, so on every skeleton screen it renders
`<span class="p-assumed">$4.54</span>` — the Board's `The model says` figure, the Board's per-job totals
row and the cost view's totals row. The mockups left totals unmarked; the rule is applied uniformly
instead (Appendix B, decision 46). The create/edit screen is the one exception and marks by size alone
(5.8 §3.6).

`.price-stamp`: inline-flex, mono .72rem `--muted`, `1px solid var(--border)` with
`border-bottom-style:dotted`, radius 2px, padding .15rem .4rem; text `prices: bundled table 2026-08-27`.
`.is-live`: `border-bottom-style:solid; border-color:var(--prov-measured); color:var(--ink)`; text
`prices: live AWS 2026-09-15`. Rendered on every band that contains money.

`.legend` (one per band with marked figures): `— measured · ⋯ assumed · ═ invoiced · no line =
projected` (swatches are `<i>` elements carrying the real border styles). `.delta` (mono .82rem
`--muted`, `<b>` in `--ink` 500) is mandatory wherever a projection and an actual coexist:
`model <b>$4.54</b> · invoice <b>$3.98</b> · <b>+14.1%</b> — <verdict>`.

Delta verdict words (`estimate_io.delta_verdict(model, invoice)`): difference = model − invoice, pct =
difference / invoice. |pct| ≤ 15% → `close enough to trust, and it errs on the expensive side` (model
higher) / `close enough to trust, and it errs on the cheap side` (model lower); 15% < |pct| ≤ 30% →
`model runs high — worth a look at the assumptions` / `model runs low — worth a look at the
assumptions`; |pct| > 30% → `far apart — check the assumptions and whether the invoice covers more
than these backups`. The bands are 15/30, not 10/25, so that the example's +14.1% reads `close enough
to trust` exactly as the mockup prints it (Appendix B, decision 41).

The short form printed in the `Difference` figure's sub-line is independent of those bands: it is
always `<signed pct> · <direction>` where direction is `model runs high` (model > invoice), `model runs
low` (model < invoice) or `matches` (|pct| < 0.05%) — `+14.1% · model runs high`, `−3.2% · model runs
low`, `±0.0% · matches` — exactly as the mockup shows it. The colour of the `Difference` value keeps its
own, tighter threshold (`--ok` when |pct| ≤ 10%, `--warn` above it) because the mockup prints the
example's `+$0.56` in `--warn` while the sentence beside it already reads `close enough to trust` (R6).

### 4.7 Job states — the state token

`.tok`: inline-flex, gap .3rem, mono .68rem/600, letter-spacing .06em, padding .16rem .38rem,
`border-radius:0`, `border:1px solid`, nowrap; `::before` is a 6×6px `currentColor` square.

| Token text | Class | Colour / border / ground | Derivation (`app/gui/status.py`, section 7.2–7.3) |
|---|---|---|---|
| `Running` | `tok-running` (`::before` pulses opacity 1→.25→1 over 1.6 s; static under reduced motion) | `--accent` / `rgba(76,141,255,.4)` / `--accent-bg` | the job's lock `$CACHE_DIR/locks/<job>.lock` is held (non-blocking `flock` probe from the GUI process) |
| `Paused` | `tok-paused` | `--muted` / `--border-strong` / `--surface-2` | `jobs.json` `enabled == false` |
| `Overdue` | `tok-overdue` | `--warn` / `--warn-border` / `--warn-bg` | enabled; the first scheduled fire after the last run's start (or after `created_at` if never run) is more than 15 minutes in the past and no run started since it (ruling R9) |
| `Failed` | `tok-failed` | `--danger` / `--danger-border` / `--danger-bg` | enabled; newest completed backup run has outcome `failed` or `aborted` |
| `OK` | `tok-ok` | `--ok` / `rgba(70,192,138,.35)` / `--ok-bg` | enabled; newest completed backup run has outcome `ok` |
| `Not run yet` | `tok-notrun` | `--faint` / `--border` / `--surface-2` | enabled; no completed backup run and not overdue |

Precedence when several apply: **Running > Paused > Overdue > Failed > OK > Not run yet** (ruling R9).
Board sort (worst first): **Failed, Overdue, Running, Paused, OK, Not run yet**; ties by next run
ascending, then name. `overdue_since` is computed for every enabled job regardless of its token so a
Failed job can also say "and the scheduled runs since have not happened".

Verdict colour by worst state: Failed/blocker → `--danger`; Overdue → `--warn`; Running → `--accent`;
all OK → `--ok`; Not run yet / no jobs → `--faint`.

Time cells are two stacked mono lines, absolute over relative, used identically for last run and next
run: absolute `HH:MM` when today or tomorrow, else `Dow HH:MM`; relative forms `today, 2 h 42 m ago`,
`Sun, 2 d ago`, `tomorrow, in 21 h 18 m`, `in 4 d 20 h`. Duration cells are actual over typical:
`4 m 12 s` / `~4 m 08 s typical`; a failed run's duration is tinted `--danger` with sub-line `stopped
early · ~2 h 12 m`; a run over 3× its typical is tinted `--warn`. Durations print as `4 m 12 s`,
`1 h 07 m`, `12 s`, `2 d 03 h`. The display zone is the container's `TZ`; every page prints it once
(section 5.15 footer).

## 5. Screens

Shared shell for every screen (section 6.3): sticky topbar (brand · nav · chip · clock), the 2px work
bar under it, the page in one of three containers (`.dense` 1240px, `.read` 780px, `.form` 960px), and
the footer with the legend `<details>`. Bands, not cards: sections are separated by a 1px `--border`
rule and 32px; cards exist only for bounded objects (a form step, a run record, the rail, the lever
panel, the folder tree, the class table). Nothing has a shadow.

Each subsection gives: purpose, layout and information order, every component with exact copy, states,
interactions, data contract (with source `file:line` or NEW), acceptance criteria.

### 5.1 Board — `/`

Purpose: "Did my backups run last night, and is anything wrong right now?" Polls `GET /status.json`
every 30 s; a changed figure or token gets the 400 ms change flash.

Layout: dense container, four bands in this order. `.screen{padding-block:2rem 4rem}`;
`.band{border-top:1px solid var(--border); margin-top:2rem; padding-top:2rem}`; the first band has no rule.

#### Band 1 — Verdict (`.verdict`)

`border-left:3px solid <worst-state colour>; background:var(--surface); padding:1rem 1.5rem;
display:flex; gap:1.5rem; align-items:center; justify-content:space-between; flex-wrap:wrap`. `<h2>`
1.05rem/640 `--ink-strong`, `max-width:62ch`; `.sub` mono .82rem `--muted` `margin-top:.4rem`
tabular; one right-aligned `btn btn-primary` that always points at the worst thing.

Copy per worst state (`status.verdict()` picks the worst job by the sort order in 4.7):

| Worst state | h2 | sub | button |
|---|---|---|---|
| Failed (example) | `manga has not backed up since Sunday. Amazon refused a delete — one permission is missing from the key this machine uses.` (the second sentence is the error class's `verdict` string, section 7.4 — **not** its `cause`, which is longer and is used on the job page; unknown class → `The run stopped with an error; open the record to see it.`) | `Sun 13 Sep 04:00 · failed after 1 h 07 m · 1 of 2 jobs failing · next run tomorrow 05:00, in 21 h 18 m` | `Fix the permission →` (the error class's fix label; unknown class → `Open the run record →`) |
| Overdue | `<job> should have run at <HH:MM> and did not. The schedule is on, but nothing was recorded.` — when `crontab_stale` is true the second sentence is `The schedule file on disk does not match your jobs; restart the container.` | `expected <Dow D Mon HH:MM> · last run <relative> · <N of M> jobs overdue` | `Open <job> →` |
| Running | `<job> is running now.` | `started <HH:MM> · <elapsed> so far · usually ~<median>` (omit the last clause with no median) | `Watch it in Activity →` |
| All OK | `Everything ran. <Both jobs / All N jobs> backed up on schedule and nothing needs you.` (one job: `appdata backed up on schedule and nothing needs you.`) | `appdata 05:00 · 4 m 12 s · manga Sun 04:00 · 2 h 09 m · next run tomorrow 05:00, in 21 h 18 m` (one segment per job, newest first, then the next run) | `Open <most recently run job> →` |
| Paused only / Not run yet only | `Nothing has run yet.` (not run yet) / `Every job is paused.` | `<N> jobs · next run —` | `Run <job> now` (POST) / `Resume <job>` |
| No jobs | `Nothing is being backed up yet.` | (none) | `Create the first job →` |

Sub-line grammar: `<when> · <what> · <N of M jobs failing> · next run <day> <HH:MM>, in <countdown>`;
the countdown ticks every second client-side from `next_scheduled` in the JSON.

#### Band 2 — Needs you (`.needs`)

`.slabel` `Needs you` + `<span class="cnt">· <N></span>`. `.needs{display:grid; gap:.75rem}`. Each
`.needs-row`: `grid-template-columns:minmax(0,1fr) 170px; gap:.75rem; align-items:center;
border:1px solid var(--border); border-left:0`; `.is-blocker{border-color:var(--danger-border)}`;
`.is-warning{border-color:var(--warn-border)}`; `.is-advice{border-color:var(--border)}`;
`.act{padding-right:1rem; display:flex;
justify-content:flex-end}`. Under 620px one column, `.act` left-aligned with `padding:0 1rem .75rem 1rem`.
Order: blockers, then warnings, then advice, then setup gaps (a setup-gap row keeps its own level but
sorts after the job rows of that level). Empty → the band collapses to one `--faint` line
`Nothing needs you.`

Row sources (`status.needs_you()`, section 7.7): (a) each job whose last failed run classifies as a
blocker error class (section 7.4) → BLOCKER; (b) `crontab_stale` → WARNING; (c) each failing Setup
readiness check (section 5.10) → BLOCKER for the passphrase check, WARNING for the others; (d) a
restore mount that is missing while at least one job exists → WARNING; (e) a warm-up that is ready to
download → ADVICE, not note: it carries a button, and a note by definition has "no rule, no ground, no
label, nothing to do" (4.5). It renders as the Advice signal (`sig-advice`, label `Advice`, the hollow
circle-dot glyph, `.needs-row.is-advice`) with the copy `<strong>manga is warmed up.</strong> The files
are readable until Tue 22 Sep, then they go cold again.` and `btn btn-sm` `Download now →` →
`/jobs/manga#restore-band`. An acknowledged create-screen blocker (section 5.8 §3.3) never appears here.

Blocker row (the IAM example):
```
[octagon]  BLOCKER
manga cannot finish a run. Amazon refused a delete, so every Sunday run stops at the same point.
The files are copied, but old versions have not been cleaned up since 6 September.
┌ .errline ──────────────────────────┐
│ AccessDenied: s3:DeleteObjectVersion │
└────────────────────────────────────┘
(hint) Two ways out: run ./setup.sh on this machine to re-apply the key policy, or add the permission
by hand in the AWS console (IAM → the backup key → this bucket). Either one takes a minute; the next
scheduled run then succeeds.
(hint) Sun 13 Sep 04:00 · open the run record →
                                                     [ Fix the permission → ]  (btn-primary btn-sm → /setup/destination)
```
`<strong>manga cannot finish a run.</strong>`; `./setup.sh` in `<code>`. `.errline`: mono .82rem `--ink`,
`background:var(--bg); border:1px solid var(--danger-border); padding:.4rem .5rem; display:block;
overflow-x:auto; white-space:pre; margin:.45rem 0`. Generic blocker row: `<strong><job> cannot finish a
run.</strong> <board>` · errline · hint `<fix>` · hint `<time> · open the run record →` · button
`<fix label>`, where `<board>` is the error class's `board` template with `{dow}`/`{since}` filled by
`status.needs_you()`. That substitution and its fallback to `cause` are defined once, in 7.4 — this row
just renders the result. In practice only `iam-version-perms` carries a templated `board`; every other
class has `board = None` (the `—` column in 7.4's table) and the row prints `cause`, which always reads
as a standalone sentence.

Warning row (restore never tested):
```
[triangle]  WARNING
No restore has ever been tested. The recovery passphrase is set and is not the shipped example — but
nothing has yet proved it can actually read your snapshots back.
(hint) A test pulls one file into a scratch folder and costs about a cent.
                                                     [ Test a restore → ]  (→ /jobs/<newest snapshot job>#rail)
```
Other warning rows: `The schedule file on disk does not match your jobs. Restart the container so your
latest job settings take effect.` [`How →` → `/setup`]; `Nowhere to put restored files yet. Add the path
mapping /mnt/user/restore → /restore to this container (read/write) and restart it.` [`How →` →
`/setup`]; `Bucket versioning could not be checked with the backup key. Confirm it is on in the AWS
console, then mark it here.` [`Open Setup →`]; readiness rows reuse their failing sentence (5.10).

#### Band 3 — Jobs

Head: `.slabel` `Jobs · worst first`; right: `.slabel` `<span class="mono"><N> jobs</span>` and
`btn btn-sm` `+ New job` → `/jobs/new`. Table in `.scroll` (`overflow-x:auto`): `border-collapse:collapse;
width:100%`; `thead th` .72rem/650 uppercase .1em `--faint`, left, `padding:0 .75rem .5rem 0`,
`border-bottom:1px solid var(--border)`, nowrap; `tbody td` `padding:.75rem .75rem .75rem 0;
border-bottom:1px solid var(--border); vertical-align:top`; `.num` right mono tabular nowrap;
`tr.jobrow:hover td{background:var(--surface)}`; radius 0 everywhere.

| # | `<th>` | Cell | Notes |
|---|---|---|---|
| 1 | `State` | state token | |
| 2 | `Job` | `.jobname` link over `.jobpath` (mono .76rem `--faint`, `word-break:break-all`, the host path `/mnt/user/…`) over `.compact-only.hint.mono` (visible < 900px only): `took 1 h 07 m · 1 failed, 13 OK in 14 runs` / `took 4 m 12 s · 14 runs, no failures` / `never run` | |
| 3 | `Last run` | `.cell2`: `.v` mono .92rem `--ink` `04:00` over `.s` mono .78rem `--faint` `Sun, 2 d ago`; never ran → `—` / `never` | |
| 4 | `Took` | `.v` `1 h 07 m` (`--danger` if failed, `--warn` if slow) over `.s` `stopped early · ~2 h 12 m` / `~4 m 08 s typical` / `no typical yet` | `col-took`, hidden < 900px |
| 5 | `Next run` | `Sun 04:00` / `in 4 d 20 h`; paused → `—` / `paused`; not computable → `—` / `can't compute this schedule` | |
| 6 | `Last 14 runs` | the run strip (section 6.5) | `col-strip` `width:168px`, hidden < 900px. "Runs" means backup runs: the strip and the counts beside it are over `kind == "backup"` records only, so a Sunday restore is never a green cell and never changes `13 OK in 14 runs` |
| 7 | `Size` (`.num`) | `.v` `<span class="p-measured">1.78 TB</span>` over `.s` `232,021 files`; unmeasured → `.p-assumed` `20 GB` / `assumed` | |
| 8 | `Per month` (`.num`) | `.v` `$1.85` over `.s` `projected`; appdata `.v` `<span class="p-assumed">$2.69</span>` / `settles month 6`; keep_all → `still climbing`; unavailable → `—` / `no price yet` | |

Legend under the table (`.legend`, mono .72rem `--faint`; `.key-cell` 9×12px swatch): `OK` (`--ok`) ·
`failed` (`--danger`) · `slow — 3× its own typical run` (`--warn`) · `no run on record yet` (`--grid`) ·
`older than the last 7 runs` (`--ok-dim`).

Row click opens the job page (whole row is clickable via JS; the name is a real link for no-JS).
Hovering a strip cell shows its `title`; clicking opens that run record. A state change on poll
flashes the changed cell.

#### Band 4 — What it costs

Head: `.slabel` `What it costs`; right `.more.linklike` `Open the full cost view →` → `/cost`.
`.grid4` (`repeat(auto-fit, minmax(180px,1fr))`, gap 1rem 1.5rem, `margin-bottom:1.5rem`) of four
`.sfig` (`.k` .68rem/650 uppercase .1em `--faint`; `.v` mono 1.15rem `--ink`; `.s` mono .76rem `--faint`):

| `.k` | `.v` | `.s` |
|---|---|---|
| `In the bucket now` | `<span class="p-measured">1.86 TB</span>` | `measured 14 Sep 07:40 · 2 folders` |
| `Last invoice` | `<span class="p-invoiced">$3.98</span>` | `August 2026 · the real bill` |
| `The model says` | `<span class="p-assumed">$4.54</span><span class="s"> /mo</span>` (the total inherits the weakest mark among the jobs it sums, 4.6 — `.p-assumed` here because appdata's `$2.69` is assumed; unmarked when every job's figure is `projected`) | `both jobs, once settled` (`all jobs, once settled` for N ≠ 2; `at least` prefix when any job keeps everything) |
| `Difference` | `+$0.56` in `--warn` (`--ok` when |pct| ≤ 10%) | `+14.1% · model runs high` |

Delta line `.delta`: `model <b>$4.54</b> · invoice <b>$3.98</b> · <b>+14.1%</b> — close enough to trust,
and it errs on the expensive side.` Note (`.sig.sig-note`): `Why the model runs high: it prices appdata
at the point where old versions stop piling up (month 6, $2.69/mo). In August they were still piling
up, so August was cheaper than the steady month you are being quoted.` (rendered only when at least one
job's `steady_month` is later than the invoice month's age; otherwise omitted).

Per-job table — `<th>`: `Job` · `What it keeps for you` · `Storage tier` · `Projected` (num) · `Share of
the bill` (num). Rows: `manga` · `<span class="mono p-measured">1.78 TB</span> <span class="hint">· 232,021
.cbz files</span>` (the extension = most common extension from the last walk when known, else
`files`) · `Thaw first, hours <code>DEEP_ARCHIVE</code>` · `$1.85 <span class="hint">/mo</span>` · `not
split` (hint); `appdata` · `<span class="mono p-measured">52.71 GB</span> <span class="hint">+ <span
class="p-assumed">~11.6 GB</span> of old versions</span>` · `Instant <code>STANDARD</code>` · `<span
class="p-assumed">$2.69</span> <span class="hint">/mo</span>` · `not split`; totals row (`colspan=3`,
hint) `Both jobs, every month once settled` · `<strong class="mono p-assumed">$4.54</strong>` · `<span
class="mono p-invoiced">$3.98</span>`.

Note: `The bill is not split per job: Amazon's cost report comes back as one number for the whole
account (or for the whole bucket when it is tagged), so “share of the bill” cannot be split between
jobs that share a bucket.` Legend + `.price-stamp`.

States: usage never refreshed → `In the bucket now` `—` / `not measured yet · Refresh usage on the cost
view`; billing not connected → `Last invoice` `—` / `billing not connected`, `Difference` `—` /
`connect billing on the cost view`, delta line omitted; billing error → `Last invoice` `—` / `billing
check failed · see Activity`; no jobs → band body is the single line `No cost to show until a job exists.`

Data (`GET /status.json`, section 8.1; cost figures from `estimate_io.board_cost()` NEW reading only
caches: `usage.json`, `billing.json`, jobs + model).

Acceptance:
- [ ] `/` renders 200 with an empty `/cache` and a provisioned `/config` (no jobs → the empty verdict); 302 → `/setup` when unprovisioned.
- [ ] Example fixture renders the Failed verdict (the error class's `verdict` sentence), the IAM blocker row with its `board` sentence and `Fix the permission →` pointing at `/setup/destination`, manga above appdata, manga's strip with one red cell, appdata's `$2.69` and the `$4.54` total both dotted, manga's `$1.85` unmarked.
- [ ] A held lock on `locks/appdata.lock` renders `Running` and disables nothing on the Board (buttons live on the job page).
- [ ] `status.json` polled by the page; a changed token flashes; the countdown ticks.
- [ ] No `<th>` or label contains a forbidden term (10.3).

### 5.2 Job page — `/jobs/<name>`

Purpose: everything about one job: is it healthy, what did it do, how do I get the data back, what
does it cost, how is it set up. Polls `GET /jobs/<name>/status.json` every 30 s (every 2 s while Running).

Layout: `.jobgrid{display:grid; grid-template-columns:minmax(0,1fr) 304px; gap:2rem; align-items:start}`;
`.rail{position:sticky; top:76px; border:1px solid var(--border); border-radius:6px;
background:var(--surface); padding:1rem}`. Under 1000px one column and the rail renders directly
under the status strip (ruling R7: the template emits the rail twice — a `.rail-inline` copy after
the strip shown ≤1000px, the sticky one shown >1000px).

Information order: header → transient Done → status strip → failure record (if failed) → ledger →
Get data back → What this job costs → How it is set up → Tool detail; rail: Recovery readiness.

#### Header (`.jobhead`)
`.titleline` = state token + `<h1><name></h1>`; `.typeline` (`--muted` .95rem) = type line (4.3), e.g.
`Snapshot backup — a point in time, so you can restore any date.`; `.pathline` (mono .8rem `--faint`)
= `/mnt/user/appdata_backups  →  s3://bw-backups/appdata/ · tag appdata` (Snapshot backup) or
`/mnt/user/data/media/comics/mangas  →  s3://bw-backups/media/manga/` (others).
`.actions`: `btn#run-now` `Run now` (disabled with label `Running…` while Running; POST
`/jobs/<name>/run`), `btn btn-primary` `Get data back` (scrolls to `#restore-band`), `btn btn-ghost`
`···` (`aria-label="More actions"`) opening a small menu: `Pause` / `Resume` (POST), `Edit →`,
`Delete…` (opens the confirm-to-act dialog: `Type <span class="mono"><name></span> to delete this job.
Its run history and restore-point cache on this machine go with it; nothing in the bucket is
deleted.` → POST `/jobs/<name>/delete` with `confirm`).

A running job shows, under the actions, the live line `Running since 05:00:01 · 2 m 14 s · open the
run record →` (mono, `--muted`); when the lock is held but no start record exists: `Running · started
before this app was watching`.

#### Transient Done (`#run-done`, `sig sig-success`, hidden by default)
Trigger, exactly: shown when a poll returns `active: null` **and** `last.id` differs from the `last.id`
this page session saw on its previous poll — whatever started that run (Run now, the schedule, the
command line). Never on first paint: the first poll only records the baseline `last.id`. The body is
built from `last`, not from anything the page remembers about a button press, so a scheduled run that
finishes while the page is open announces itself the same way. Label `Done`; body `Run finished in 4 m 09 s.
New restore point <span class="mono">c37b0d5e</span> — 6 files changed, 214 MB new.` (Plain copy: `Run
finished in 2 h 09 m. 1,204 files copied, 3.1 GB.`; File history: `Run finished in 8 m 12 s. 12 files
uploaded, 40 MB.`). Auto-hides after 7 s.

#### Status strip (`.statusstrip`, `repeat(auto-fit, minmax(140px,1fr))`, gap 1rem 1.5rem)

| `.k` | `.v` | `.s` |
|---|---|---|
| `Last run` | `05:00` | `today · 2 h 42 m ago` |
| `Took` | `4 m 12 s` | `~4 m 08 s typical` |
| `Next run` | `05:00` | `tomorrow · in 21 h 18 m` |
| `Streak` | `30 runs` | `no failures on record` |
| `Restore points` (Snapshot backup, File history) / `Copies` (Plain copy) | `14` / `1` | `back to 18 Mar 2026` / `what is there now` |

States: never ran → `—`/`never`, `—`/`no typical yet`, `0`/`no runs yet`, `0`/`none yet`; paused →
`Next run` `—`/`paused`; failed → `Streak` `0`/`broken Sun 13 Sep`; overdue → `Next run` `05:00` /
`overdue — expected Mon 14 Sep 05:00, 1 d 6 h ago`; running → `Took` `2 m 14 s` / `so far · usually
~4 m 08 s` and, past 3× the median, `taking longer than usual`.

#### Failure record — only when the last run failed (`sig sig-failure`, between strip and ledger)
```
[octagon-x]  FAILED
Sun 13 Sep 04:00 · stopped after 1 h 07 m · exit code 1
┌ .errline ────────────────────────────┐
│ AccessDenied: s3:DeleteObjectVersion │
└──────────────────────────────────────┘
WHY THIS HAPPENS                       │ WHAT TO DO
<cause from the error class>           │ <fix from the error class>
                                       │ [ Fix the permission → ]   open the run record →
```
The `.sig.sig-failure` box ends at the `.errline`: it carries the time, the duration, the exit code, the
verbatim error and the link to the record, and no button — a retrospective signal states what happened
(4.5). The two `.slabel` columns (`.grid2` `WHY THIS HAPPENS` / `WHAT TO DO`) are siblings of the signal,
rendered directly under it and outside `.sig`'s body, and the fix button lives at the foot of `WHAT TO
DO`; it appears only when the class has a `fix_route`. The
prune case adds a first line to the cause: `The files themselves were copied; the clean-up of old
versions was refused, so the keep rule is not being applied.` An `aborted` run reads `stopped without
reporting` in place of `stopped after …` and the killed class's cause/fix. Unknown class: the two
columns are replaced by `The tool reported an error this app does not recognise. Read the log in the
run record; the last lines usually name the reason.`

#### Ledger — `Last 30 runs`
The same rule as the Board strip: **backup runs only** (`kind == "backup"`). Head right `.slabel.mono`
`30 OK · 0 failed` (counts over the cells shown). `.ledger-wrap{overflow-x:auto;
padding-bottom:4px}` → `.ledger{min-width:358px}` → `.strip[role=img][aria-label]` with 30 `.cellx` at
10×20px (section 6.5), then `.bars[aria-hidden="true"]` (flex, gap 2px, `align-items:flex-end;
height:20px; margin-top:3px`; each `.bar` 10px wide `background:var(--border-strong)`; the tallest gets
`.tall{background:var(--muted)}`), one bar per cell at the same pitch, height on a **squared** scale:
`height_px = max(2, round(20 × (duration_s / max_duration_in_window) ** 2))` (padding cells: 2px
`--grid`). Squared, not linear, because the bars exist to make one slow run legible: linearly the
example's typical 4 m 08 s beside a 8 m 52 s peak draws every normal bar at 9–10 px against 20 px and
the row reads as noise, while squared it reproduces the mockup exactly — (252/532)² × 20 ≈ 4.5 px for a
typical run, 20 px for the tall one (mockup lines 691–702 draw 4–6 px bars and one 20 px `.tall`; R6).
The bars are decorative: `aria-hidden` keeps them out of the strip's own `aria-label`, which already
carries every duration. Then `.ledger-axis` (flex space-between, mono .72rem `--faint`,
`max-width:358px`): `<date of the oldest run shown>` · `last 7 runs →` · `latest`.
Hint: `Thirty runs, none failed. The bars under the strip are how long each run took — the tall one is
Fri 28 Aug at <span class="mono">8 m 52 s</span>, twice the usual and still well inside normal. Hover any
run for its date, outcome and restore point.` Composition: sentence 1 = `Thirty runs, none failed.` /
`Thirty runs, <N> failed.` / `<N> runs on record, none failed.` (fewer than 30); sentence 2 names the
tallest bar: `… — the tall one is <Dow D Mon> at <duration>, <ratio phrase> and still well inside normal.`
where the ratio phrase is `about the usual` (< 1.5×), `twice the usual` (1.5–2.5×), `<n>× the usual`
(rounded) and the tail becomes `— slower than it should be` when it is a `slow` cell; sentence 3 is fixed.
`aria-label`: `Last 30 runs for appdata: every run succeeded.` / `Last 30 runs for manga: 13 OK, 1 failed
(Sun 13 Sep).` / `No runs on record for <job>.`

#### Get data back (`#restore-band`)
`.slabel` `Get data back`; `.lead`: `Pick the date you want back. Files are written into a new folder —
the live <span class="mono">/mnt/user/appdata_backups</span> is never touched or overwritten.`

Snapshot backup — restore-point list. `.rphead` (grid `22px 130px 92px minmax(0,1fr)`, .68rem uppercase
`--faint`): `` · `Date & time` · `Restore point` · `What it gives you back`. Rows `<label class="rp">`
(same grid, `padding:.5rem .75rem; border-bottom:1px solid var(--border); cursor:pointer`, hover
`--surface`) with `input[type=radio] name="point"`, `.d` mono .88rem `--ink`, `.id` mono .82rem
`--muted`, `.m` mono .78rem `--faint`:

Which rows are visible, stated once and used everywhere: **the newest six restore points, then the
oldest — seven rows when the count is 8 or more; when the count is 7 or fewer every point is a row and
there is no `<details>`.** `N` in the summary is `count − 7`. (The mockup's seventh row was labelled
"kept as the weekly one"; Appendix B #27 drops that claim because restic does not record why a snapshot
was kept, and the newest-six rule replaces it while keeping the mockup's seven rows and its
`Show the other 7 restore points`.)

| `.d` | `.id` | `.m` |
|---|---|---|
| `Tue 15 Sep 05:00` (checked) | `a81f3c2e` | `52.71 GB · 6 files changed, 218 MB new · today` |
| `Mon 14 Sep 05:00` | `7c41e9b0` | `52.68 GB · 4 files changed, 96 MB new` |
| `Sun 13 Sep 05:00` | `2b90f4d5` | `52.66 GB · 9 files changed, 402 MB new` |
| `Sat 12 Sep 05:00` | `e5d1a06c` | `52.61 GB · 3 files changed, 71 MB new` |
| `Fri 11 Sep 05:00` | `91f7bc3d` | `52.60 GB · 7 files changed, 188 MB new` |
| `Thu 10 Sep 05:00` | `4a2e8d1a` | `52.55 GB · 2 files changed, 40 MB new` |
| `Wed 18 Mar 05:00` (the oldest) | `4b02fa71` | `38.09 GB · the first backup` |

Then `<details>` `Show the other <N> restore points` (`Show the other 7 restore points` for the example's
14) → a plain table (`td.mono` date, `td.mono.hint` id, `td.hint` size). Under it the hint `Fourteen
points are kept, because your keep rule is: last 3, then one a day for 7 days, one a week for 4 weeks,
one a month for 6 months.` (the `keep_rule_prose` form from 4.3; `keep_all` → `Every point is kept —
your keep rule is Keep everything.`). Points without a restic
summary (made before restic 0.17) show `.m` `size not recorded`. Under 620px the grid collapses to
`22px minmax(0,1fr)` and `.id`/`.m` hide. Empty cache: `Restore points have not been listed yet.` +
`btn btn-sm` `List them now` (POST `/jobs/<name>/restore-points/refresh`); stale cache (older than the
last OK run): hint `listed <relative> · Refresh` link.

File history — the same list, one row per OK run (`.d` the run's finish time, `.id` the run id's first
8 characters, `.m` `12 files uploaded, 40 MB`), plus a `Path` field: `label.fld` `Restore` with a
`select#scope` of exactly **two** options — `Everything as of this point` (value `.`) and `One file, by
path` (value `file`) — and, when `file` is chosen, a text input `#path` `or one file, by path`
(relative, mono). There are no per-folder options here: the engine's two File history invocations are
`.` (every path live at that moment) and one relpath (`vfiles.py:296`), so a folder choice would have no
argv to produce (7.5.6) and would silently restore everything or nothing. Per-folder File history
restores are out of scope for this increment (Appendix B, decision 47).

Plain copy — no points (ruling R8). A `.defgrid` titled by `.slabel` `What is there now`: `Copy as of`
`Sun 6 Sep 06:09 · last completed run`; `Files` `232,021 <hint>measured 14 Sep 07:40</hint>`; `Size`
`<span class="p-measured">1.78 TB</span>`; `Storage tier` `Thaw first, hours <code>DEEP_ARCHIVE</code>`;
`History` `current copy only — no version history` (+ ` · files deleted at home are also deleted in the
copy on the next run` when `mirror` is true); `Folders` `<select#scope>` `Everything` / one per top-level
folder from the cache. Size unknown: `not measured yet — <a>Refresh usage</a>`.

Target: `label.fld` `Write the files to` → `input#restore-target` value `/mnt/user/restore/appdata/2026-09-15/`
(default `<RESTORE_ROOT_HOST>/<job>/<YYYY-MM-DD>/`, auto-suffixed `-2`, `-3` when that folder exists and
is non-empty); hint `A new, dated folder. Typing the live source path here is refused — a restore can
never overwrite what it is protecting.` Typing a path under the source → inline BLOCKER `This is the
folder the job protects. Pick a folder under /mnt/user/restore.` and the button disables. Restore mount
missing → the whole form is replaced by a BLOCKER: `Nowhere to put restored files yet. Add a path mapping
to this container — host /mnt/user/restore → container /restore, read/write — then restart it. Until
then, restores run from the command line into /cache/restore/<job> (see the README).`

Needs-line (`.needsline`, three rows of `✓` in `--ok` or `!` in `--warn`/`--danger` + sentence):
- `<strong>Recovery passphrase:</strong> set, and not the shipped example. Without it nothing here can be
  read — not by you, not by Amazon.` (Snapshot backup only; failing → `--danger` `<strong>Recovery
  passphrase:</strong> not set — still the shipped example. Nothing here can be read back until it is.`
  + `Fix →` `/setup/keys#RESTIC_PASSWORD`)
- `<strong>Ready now:</strong> this job is on the instant tier <code>STANDARD</code>, so there is no
  waiting — the first file starts copying immediately.` / cold: `--warn` `<strong>Not ready yet:</strong>
  this job is on the thaw-first tier <code>DEEP_ARCHIVE</code>. Amazon has to warm it up before a single
  file can be read — up to 12 hours at Standard speed, up to 48 hours at Bulk — and the warm-up is charged
  separately.`
- `<strong>Data out of Amazon:</strong> 52.71 GB ≈ <span class="mono">$4.74</span> <span class="hint">one
  time, for a full restore</span>` / cold: `<strong>Warm-up + data out:</strong> 1.78 TB ≈ <span
  class="mono">$168.40</span> at Standard speed · about <span class="mono">$164.10</span> at Bulk` (both
  from `estimate_io.restore_quote`; the numbers are examples).

Sibling warning (`sig sig-warning`, when another job is on a cold tier and this one is not): `Your other
job is not like this one. <strong>manga</strong> sits on the thaw-first tier <code>DEEP_ARCHIVE</code>:
before a single file of it can be read, Amazon has to warm it up, and that takes <strong>up to 12
hours</strong> and is charged separately. Worth knowing before the night you need it.` + hint linklike
`See what a manga restore takes →`.

**The band never starts anything.** The whole section from the restore-point list down to the button is
one `<form method="get" action="/jobs/<name>/restore">` whose fields are `point` (versioned) or
`scope` + `path` (others), `target`, `tier` and the hidden `intent` (`restore` | `thaw` | `download`).
Submitting it is a plain navigation to the confirmation page (5.4), which owns the typed-name confirm
and the one POST that starts work. This is the single restore flow: the job page collects the choice,
`/jobs/<name>/restore` states the consequence and takes the confirmation, the run record is the live
operation. There is no typed-name field on the job page.

The small POST controls that sit visually inside this band — `List them now`
(`/jobs/<name>/restore-points/refresh`), `Refresh usage`, and `Check now` (`/jobs/<name>/thaw/check`) —
are **not** nested `<form>` elements (HTML forbids a form inside a form). Each is its own
`<form method="post">` rendered as a sibling of the GET form and associated to its button by the HTML5
`form="<id>"` attribute, so it posts only its own hidden fields and none of the restore selection.
They start no restore and cost nothing; they only refresh a cache or poll a warm-up.

Guard (`.guard`: `border:1px solid var(--danger-border); background:var(--danger-bg); padding:1rem;
radius 4px`) — text only here, no input: `<strong>This writes 52.71 GB to your array and starts a charged
download.</strong> The next screen shows the full cost and asks you to type the job name.` Under it the
`.confirm` row holds just the submit: `btn btn-primary#go-restore` `Start restore →` (the id
`#start-restore` belongs to the confirmation page's real button, 5.4) and
`span.hint#go-hint` `Pick a restore point and a folder first.` JS enables the button as soon as a
point (or scope/path) and a non-empty, valid `target` are present, and swaps the hint to `Next: confirm
what this will do.`; with no JS the button is enabled and the confirmation page does the validating.

Cold tier: the button is `Warm up first — up to 12 h →`, `intent=thaw`, and a `select#tier` `Retrieval
speed` precedes it (options from 4.3; default Standard — the confirmation page repeats the select so the
choice can still be changed there); the guard reads `<strong>This asks Amazon to warm up 232,021 files,
one request each — the request itself takes hours to send — and starts a charged warm-up of
1.78 TB.</strong> The next screen shows both speeds priced and asks you to type the job name.` With a
warm-up in progress the band shows the waiting state (5.4) and `[Check now]` (that one is a real
`POST /jobs/<name>/thaw/check` — it starts nothing and costs nothing); when it is ready the button
becomes `Download now →` with `intent=download`, again a GET to the confirmation page. Snapshot backup
on a cold tier (a hand-edited or acknowledged config): BLOCKER `This snapshot store is on a thaw-first
tier. Every read — even listing dates — needs the whole store warmed up first.` with `[Warm up the store
— one request per object, hours to days]` (a link to `/jobs/<name>/restore?intent=thaw&scope=.`) and
`move the job to an instant tier for future backups →` (edit).

#### What this job costs
Head `.slabel` `What this job costs`; `.more` `Open the full cost view →`. `.grid4`: `First bill`
`<span class="p-assumed">$1.39</span>` / `month 1 · projected`; `By month 6` `<span class="p-assumed">$2.67</span>`
/ `old versions have built up`; `Every month after` `<span class="p-assumed">$2.69</span>` / `settles from
month 6 — it stops rising`; `In the bucket now` `<span class="p-measured">64 GB</span>` / `measured 14 Sep
07:40`.

`In the bucket now` on a **Snapshot backup** job is the whole snapshot store, not this job's share of
it: every versioned job writes into the one fixed `appdata/` prefix (`backup-job.sh:36`) and `usage.json`
measures prefixes, so no honest per-job split exists. With more than one Snapshot backup job the `.s`
sub-line becomes `measured 14 Sep 07:40 · whole snapshot store · shared by <N> jobs`, `in_bucket_monthly`
prices that whole prefix at `STANDARD`, and the cost view's totals row counts the store **once** (5.6
band 2). With exactly one Snapshot backup job the figure is that job's, and the sub-line stays
`measured <date>`.

`keep_all` → `Every month after` `Keeps growing` / `there is no plateau for this job`. Plain copy
at 0% change → all three unmarked, `By month 6` `$1.85` / `same as every month`.

Table `<th>` `Where the money goes` · `How much of it` · `Where that number came from`:
`Storing your files` · `<span class="p-measured">52.71 GB</span> · 533 files` · `Walked the folder on 14 Sep
07:40.`; `Storing old versions` · `<span class="p-assumed">~11.6 GB</span>` · `Assumed, not measured: about
1% of the folder is replaced each night, so old copies settle at 1.22× your data.`; `Uploading` ·
`pennies` (or the figure when ≥ $0.05) · `Charged per request, not per gigabyte — 533 small files is
nothing.`; `Getting data out of Amazon` · `$0.00` · `Only charged when you restore. A full one would be
$4.74.`; Plain copy adds `First upload (once)` · `$11.60` · `232,021 requests at $0.05 per 1,000 —
charged once, on the first run.` and, on a tier with a minimum stay, `Minimum stay` · `$10.95` · `Delete
it tomorrow and you still pay through day 180.`

Delta `Projection against the real bill, whole account: model <b>$4.54</b> · invoice <b>$3.98</b> ·
<b>+14.1%</b>`; note `Per-job is not available: Amazon's cost report returns one account-wide figure, so
this job's own share of the $3.98 cannot be shown honestly yet.`; legend with `.price-stamp#price-stamp`
and checkbox `#live-prices` `use live AWS prices` (re-fetches `/jobs/<name>/status.json?prices=live`
and repaints the band's figures with the flash; the stamp flips to `.is-live`).

#### How it is set up (`.defgrid` `200px minmax(0,1fr)`; `dt` .84rem `--faint`; hairline row rules)
Head `.slabel` `How it is set up`; `.more.linklike` `Edit →`. Rows: `What it protects` `.mono
/mnt/user/appdata_backups`; `What happens to it` `Snapshot backup — a point in time, restore any date`
(Plain copy → `Plain copy — a straight copy, no history`; File history → `File history — every version
of every file`);
`Storage tier` `Instant <code>STANDARD</code> — readable the second you ask` (cold: `Thaw first, hours
<code>DEEP_ARCHIVE</code> — up to 12 hours before a file can be read`; an acknowledged blocker adds the
sub-line `acknowledged: Snapshot backup on a thaw-first tier, 15 Sep`); `How much changes between runs`
`A little — about 1% a night <span class="hint">(an assumption you set, used for every cost figure
above)</span>`; `Keep rule` (`keep_rule_label`, 4.3); `When it runs` `Every day at 05:00` (`cron.describe`;
paused → `Every day at 05:00 — paused`); `Where it goes` `.mono s3://bw-backups/appdata/`; `When you
delete a file locally` `keep it in the copy` / `delete it from the copy too` (Plain copy only); `Already
bundled` `no` / `yes, about 0.05 GB each` (Plain copy, File history); `State` `Scheduled` / `Paused`;
`Created` `12 Sep 2026` / `before run history existed`.

#### Tool detail (`<details class="tooldetail">`)
`summary` `Tool detail — what actually runs`. Snapshot backup: hint `Snapshot backups are made by
<strong>restic</strong>. All snapshot jobs share one encrypted store, which is why two of them must never
start in the same minute — the second would find the store locked.`; `.cmd` block `restic -r
s3:s3.amazonaws.com/bw-backups/appdata \ backup /mnt/user/appdata_backups --tag appdata` with `btn btn-sm
copy` `Copy` (→ `Copied` for 1.4 s); hint `Last snapshot id <span class="mono">a81f3c2e</span> · tag <span
class="mono">appdata</span> · repository password comes from the recovery passphrase. <a>What these tools
are →</a>`. Plain copy: `Plain copies are made by <strong>rclone</strong>. Each Plain copy job has its own
folder in the bucket; nothing is shared.` + `rclone copy /mnt/user/data/media/comics/mangas
s3:bw-backups/media/manga --s3-storage-class DEEP_ARCHIVE` (`sync` when mirror). File history: `File
history is kept by this app's own catalog (one small database per job) and copied by
<strong>rclone</strong>, one file at a time.` + `python3 -m app.engine.vfiles backup manga`. Then the raw
schedule `cron <span class="mono">0 5 * * *</span>`, and `<N> unreadable lines in the run history` when
the reader counted any.

#### Rail — `Recovery readiness`
`<h3>Recovery readiness</h3>`; hint `Could you actually get this back right now?`; `.rrow` (grid `14px
minmax(0,1fr)`, `padding:.55rem 0`, hairline top; `.dot` 8×8 square; `.k` .84rem `--muted`; `.v` mono
.84rem `--ink`; `.t` mono .7rem `--faint`):

| dot | `.k` | `.v` | `.t` |
|---|---|---|---|
| ok | `Recovery passphrase` (Snapshot backup only) | `Set · not the example` | `checked 15 Sep 07:40` |
| ok | `Destination reachable` | `Write, read, delete — all OK` | `probed 14 Sep 07:40` |
| ok | `Old versions protected` | `Bucket versioning on` | `checked 14 Sep 07:40` |
| ok | `Dates you can go back to` / `What is there` (Plain copy) | `14 · back to 18 Mar` / `1 copy · as of Sun 6 Sep` | `181 days of cover` / `no version history` |
| warn | `Restore ever tested` | `Never` (in `--warn`) | `nothing has proved it reads back` |

Failing rows: `dot-danger`, `.v` in `--danger` (`Not set — still the shipped example`, `Could not write
to the bucket`, `Versioning off — old versions unprotected`); unknown: `dot-warn` `Not checked yet` /
`Not checkable with this key`. Button (`btn`, full width): `Test restore — one file, ~$0.01` → POST
`/jobs/<name>/test-restore`; hint `Pulls a single file into a scratch folder, checks it, deletes it, and
writes today's date into this row.` Cold tier (ruling R11): button `Test restore — warm up one file, ~$0.02,
up to 48 h` and hint `Asks Amazon to warm up the most recently copied file at Bulk speed, then downloads
it to <span class="mono">/mnt/user/restore/manga/test/</span> when it is ready.` (price = `restore_quote`
for that one object at Bulk, rounded up to the cent, minimum `$0.01`).

The pending cold test, end to end (ruling R11 — the only two-step operation in the app):
- After the first press, `restore.sh <job> test` has written `state/<job>.test-thaw.json` and ended `ok`
  with `"tested_pending":true` (7.5.3 §9). The `Restore ever tested` row becomes `.v` `Warming up` in
  `--warn` with `.t` `ready by ~Wed 17 Sep 04:00` (from `test_pending.expected_ready_by`), and the
  button becomes `Check now` — no price on its face, because checking costs nothing.
- `Check now` **re-POSTs `/jobs/<name>/test-restore`**. There is no separate endpoint: the script sees
  `test-thaw.json`, runs `aws s3api head-object` on the one key, and either (a) it is still warming —
  it rewrites `test-thaw.json`, ends `ok` with `"tested_pending":true`, the row is unchanged and the
  redirect carries the `note` flash `Still warming up — ready by ~Wed 17 Sep 04:00.`; or (b) it is
  ready — it `rclone copyto`s the key to `/mnt/user/restore/<job>/test/<basename>`, verifies it is
  non-empty, writes `state/<job>.tested.json`, deletes `test-thaw.json` and ends `ok` with
  `tested_path`/`tested_bytes`. The row then reads `.v` `Tested 17 Sep 09:12` (`--ok`) with `.t`
  `<basename>, kept under /mnt/user/restore/<job>/test/` — kept, not deleted, because on a cold tier it
  cost hours and money to get; the warm test still deletes its scratch copy (Appendix B, decision 30).
- `POST /jobs/<name>/thaw/check` is NOT this button: it reads `<job>.thaw.json` (a scoped warm-up for a
  real restore), never `<job>.test-thaw.json`.

Data: `status.json` (8.2), `restore-points.json` (8.5), `readiness.recovery_summary()` (7.7),
`estimate_io.job_cost_band()` NEW (8.7), `jobs_io.get`.

Acceptance:
- [ ] appdata fixture: OK token, five status figures, 30-cell ledger with bars, seven visible restore points + details, needs-line all green; the band's form is `method="get" action="/jobs/appdata/restore"` and carries no `confirm` field.
- [ ] manga fixture: Failed token, failure record with the prune line and `Fix the permission →`, `Copies` figure, "What is there now", the band's button `Warm up first — up to 12 h →` (a GET to the confirmation page), cold rail button copy.
- [ ] Held lock: `Run now` disabled and labelled `Running…`, live line present.
- [ ] `···` menu: Pause flips to Paused (token, `Next run —`), Resume restores; Delete requires the typed name.
- [ ] No forbidden term outside `.tooldetail`, `<code>`, `.cmd`.

### 5.3 Run record — `/jobs/<name>/runs/<run_id>`

Purpose: one run as a record — outcome, start, duration, restore point, exit code, the error verbatim,
the run's own log, the command line that ran, and "Why this happens / What to do" for recognised errors.
Reached from any strip cell, any failure signal, Activity, and the redirect after Run now / Start restore.

Layout: reading container; eyebrow `.slabel` `<job> · run record`; h1 `Sun 13 Sep 04:00 — Failed`
(`— OK`, `— Running`, `— Stopped` for aborted); lead `Started by the schedule. Stopped after 1 h 07 m
with exit code 1.` (`Started by you (Run now). Finished in 4 m 12 s.` / `Started by you. Running for
2 m 14 s.` / `Started by the schedule. Stopped without reporting — the container was probably restarted
or the process was killed.`). Restore kinds: eyebrow `<job> · restore` / `· warm-up` / `· download` /
`· test restore`; h1 `Restore Tue 15 Sep 05:00 — OK`, `Warm-up — waiting`, `Test restore — OK`.

One `.step` card (bounded object) holding:
1. Token + `.defgrid`: `Outcome` (token) · `Started` `Sun 13 Sep 2026 04:00:02` · `Finished` `05:07:14`
   (running: `—`) · `Took` `1 h 07 m 12 s` + hint `~2 h 12 m typical` · `Started by` `schedule` /
   `you (Run now)` / `once` / `command line` · `Restore point` `a81f3c2e` (Snapshot backup, OK only) ·
   `What it did` `6 files changed, 218 MB new · 533 files, 52.71 GB in the folder` (from the record's
   stats; Plain copy `1,204 files copied, 3.1 GB · 3 errors`; restore `533 files, 52.71 GB written to
   /mnt/user/restore/appdata/2026-09-15/`; warm-up `232,021 files requested at Standard speed · ready by
   ~Tue 15 Sep 21:12 · download before Tue 22 Sep`) · `Exit code` `1` · `Error` (`.errline`, verbatim;
   omitted when null).
2. For a recognised error class: `.grid2` `WHY THIS HAPPENS` / `WHAT TO DO` with the fix button
   (section 7.4 copy). Restore records that ended with warm-up requests: `sig-note` `<M> files are
   still cold and were asked to warm up; <N> were written. Come back after the stated hours and press
   Download again.`
3. `Command` — `.cmd` block with `Copy`: the recorded command line (no secrets).
4. `Log` — `<pre id="runlog">` of the per-run log, tailed while live (`GET …/log?offset=`), with a
   `btn btn-sm` `Copy` and, when the log is longer than 64 KB, `Show the whole log` (loads the rest).
   Backfilled records (no per-run log) show `This run predates run history; only the shared log exists.
   <a href="/logs">Open the shared log →</a>`.
5. Links: `← <job>`, `All activity →`.

Live state: while `outcome == running` the page polls `…/runs/<id>.json` every 2 s, shows the work bar,
a `.progress` line (`1.2 GB of 52.7 GB · 6 m 40 s left` when parseable from rclone `Transferred:` lines
or restic `percent_done`; else `working… 2 m 14 s`). When it ends the page re-renders in place.

Pending state — the landing page of every `Run now`, `Start restore`, warm-up, download and test
restore. Those POSTs redirect the moment `ops.launch` returns; the child writes its start line only
after it has loaded the config, read `jobs.json` and taken the lock (hundreds of milliseconds), so the
first GET routinely finds no record. It must not 404. `GET /jobs/<name>/runs/<run_id>` returns **200 in
a `pending` state** when the id matches `RUN_ID_RE`, the job exists, the id's own timestamp is within
the last 10 minutes and no record exists yet: token `Running` (`tok-running`), h1 `Starting…`, lead
`Starting the run — waiting for it to report in.`, the work bar on, no defgrid, polling
`…/runs/<id>.json` every 2 s. `…/runs/<id>.json` answers `200 {"outcome":"pending","live":true,"id":…}`
in the same window. After 30 s of pending the page swaps in place to `This run never reported starting.
The job was probably busy (another run held its lock) — check Activity.` with `[Back to <job>]` and
stops polling; the work bar goes off.

404 (the error page of 5.14, JSON for `.json` / `log`) is then reserved for exactly three cases: an id
that does not match `RUN_ID_RE`, an unknown job, and a well-formed id older than 10 minutes with no
record. Ten minutes is the pending window (`runs.PENDING_WINDOW_S = 600`), parsed from the id's own
`YYYYMMDDTHHMMSSZ` prefix, so a stale bookmark still 404s.

Data: `runs.get_run` (7.1.7), `errors.classify` (7.4), `runs.read_log`, `status.median`. Contract 8.3.

Acceptance:
- [ ] `GET /jobs/manga/runs/<id>` for the fixture: Failed token, error line verbatim, cause/fix with `Fix the permission →`, command, log.
- [ ] `GET /jobs/appdata/runs/not-an-id` → 404 error page; a well-formed id minted just now with no record → 200 with the pending copy (`Starting…`); the same id with a start line appended → the live record; a well-formed id stamped yesterday with no record → 404.
- [ ] `POST /jobs/appdata/run` followed immediately by a GET of its redirect target → 200, never 404.
- [ ] Live polling stops when the record closes; the log endpoint's `X-Log-Eof` honoured.

### 5.4 Get data back — `/jobs/<name>/restore`

Purpose: the one confirmation step, and the only place a restore can be started. The job page's band
collects the choice and navigates here (5.2); this page states exactly what is about to happen, takes
the typed-name confirmation, and POSTs. `GET` with query parameters renders the confirmation; `POST`
starts the work and redirects to the run record, which is the live operation page (5.3).

Query contract (all optional except as noted; contract 8.10): `intent` ∈ `restore` (default) | `thaw` |
`download`; `point` (a restore-point id or `latest`, required for a versioned `restore`); `scope` (a
top-level folder or `.`, default `.`); `path` (one file, relative); `target` (host path, defaults to the
band's default when absent); `tier` ∈ `Bulk|Standard|Expedited` (warm-up kinds only, default `Standard`).
Unknown or ill-formed values are not an error page: the field falls back to its default and the page
renders the fallback in place, so a hand-typed URL can always be corrected on screen. An `intent` the
job's type cannot do (a `thaw` on a warm tier, a `point` on a Plain copy) renders the page with a
BLOCKER and no primary button.

Layout: reading container; eyebrow `.slabel` `<job> · get data back`; h1 `Restore Tue 15 Sep 05:00`
(`Warm up manga`, `Download manga`, `Restore everything as of Tue 15 Sep 05:03`, `Restore one file as of
…`); lead `Here is exactly what is about to happen, what it costs, and how long it takes. Nothing has
started yet.` One `.step` card with a `.defgrid`:

| `dt` | `dd` |
|---|---|
| `Restore point` | `Tue 15 Sep 05:00 · a81f3c2e` (Plain copy: `Copy as of Sun 6 Sep 06:09`) |
| `What` | `Everything in this point` / `Only <folder>` / `One file: <path>` |
| `Writes to` | `.mono /mnt/user/restore/appdata/2026-09-15/` + hint `Inside it the files land under <span class="mono">backup/media/appdata_backups/</span> — the snapshot keeps the original path.` (Snapshot backup only) |
| `Size` | `52.71 GB · 533 files` (provenance mark) |
| `Needs` | the needs-line rows from 5.2 |
| `Costs` | `≈ $4.74 one time` / cold: `≈ $168.40 (warm-up at Standard speed + data out)` |
| `Takes` | `about <N> min at <speed>` (speed = median bytes/s of this job's OK runs when known, else `starts immediately; the copy runs as fast as your line allows`) / cold: `up to 12 h warm-up, then the copy` |

For a warm-up: `Retrieval speed` `select#tier` with the options from 4.3, the honest hours per option,
and the two priced quotes.

Then the guard and the typed-name confirm — they live here and nowhere else. `.guard` (same CSS as
5.2): `<strong>This writes 52.71 GB to your array and starts a charged download.</strong> Type the job
name to confirm.` (warm-up: `<strong>This asks Amazon to warm up 232,021 files, one request each — the
request itself takes hours to send — and starts a charged warm-up of 1.78 TB.</strong> Type the job name
to confirm.`) → `.confirm` row: `label.fld` `Type <span class="mono">appdata</span>`,
`input#confirm-name` (`placeholder`, `autocomplete=off`, `spellcheck=false`, max-width 16rem), `btn
btn-primary#start-restore` (disabled) labelled `Start restore` / `Warm up first — up to 12 h` /
`Download now` by intent, and `span.hint#confirm-hint` `Disabled until the name matches.` JS: on input,
enabled iff `value.trim() === name`; the hint becomes `Ready. This starts a charged download of
52.71 GB.` in `--ok`. With no JS the button is enabled and the server rejects a wrong name.

The whole card is one `<form method="post">` posting to `/jobs/<name>/restore` (`intent` `restore` or
`download`) or `/jobs/<name>/thaw` (`intent` `thaw`) with `point|scope|path`, `target`, `tier`,
`confirm` and `csrf`. Server validation failures re-render this page at 400 with the field error and
every value intact (`Type the job name exactly as shown to start.`, `That folder already has files in
it. Pick a new folder so nothing gets overwritten.`, `The folder must be inside /mnt/user/restore.`);
success → 302 to the run record, which is the live operation page (5.3). The work bar shows on every
page while any operation is live.

Warm-up waiting state (rendered on the job page band and on the run record while `thaw.json` is
pending): token `Warming up` (`tok-overdue` colours, class `tok-warming`), `Amazon is warming up 232,021
files at Standard speed. Requested Tue 15 Sep 09:12 · ready by ~21:12 · download before Tue 22 Sep, then
it goes cold again.` + `btn` `Check now` (POST `/jobs/<name>/thaw/check`, synchronous, ≤ 90 s, flashes
`Checked 20 files: 20 ready.` / `Checked 20 files: 3 ready, 17 still warming.`) + note `Amazon says the
sampled files are ready — download may still skip stragglers; run it again if it reports errors.` when
ready. Expired: `The warm-up has expired — request it again.` with `Warm up again`.

Data: `points.py`, `readiness.py`, `estimate_io.restore_quote`, `ops.validate_target`, `thaw.json`.
Contracts 8.5 and the POST table in 8.10.

Acceptance:
- [ ] Confirmation renders for each type with the correct verb, target and quote.
- [ ] The guard enables the primary button only on the exact job name (this is the only screen with `#confirm-name`).
- [ ] `GET /jobs/appdata/restore` with no query renders with the band's defaults (scope `.`, the default dated target, `intent=restore`).
- [ ] POST with wrong `confirm` → 400 re-render with the message and values intact.
- [ ] Target outside the root / non-empty / under the source → 400 with the exact sentence.
- [ ] Lock held → 409 with the busy blocker flash.
- [ ] Happy path per type → the argv table in 7.5.6 and a 302 to the run record.

### 5.5 Activity — `/activity`

Purpose: reverse-chronological feed of every operation across all jobs — scheduled and manual runs,
restores, warm-ups, downloads, test restores, usage refreshes, billing checks, destination probes,
provisioning — each tagged with its job and expandable to its log.

Layout: dense container; `.slabel` `Activity` + h1 `What this machine has done`; filter row of three
`select`s (`Job: all / appdata / manga`, `What: all / runs / restores / setup`, `Outcome: all / OK /
failed / running`) that submit as query parameters (`job`, `kind`, `outcome`) and `limit` (default 100)
with `Show 100 more`. One table, `<th>`: `When` (time cell) · `Job` (name link; system rows `—`) · `What`
(`scheduled run` / `manual run` / `restore` / `warm-up` / `download` / `test restore` / `usage refresh` /
`billing check` / `destination probe` / `destination setup`) · `Outcome` (token: `OK`, `Failed`,
`Running`, `Stopped` (aborted, `tok-failed`), `Waiting` (warm-up pending, `tok-warming`)) · `Took`
(duration cell) · `Record` (`open →`). Clicking a row toggles a `<pre>` with the log's last 200 lines
(120 ms height animation; `GET …/log`). A running operation is the top row with an elapsed counter;
the work bar is on. Empty: `Nothing has run yet.` + `Run appdata now` (POST, the first enabled job) or
`Create the first job →`. Footer link: `Raw shared log →` → `/logs`.

System records have their own record page. `_system` is not a valid job name, so
`/jobs/_system/runs/<id>` cannot exist; instead **`GET /activity/<run_id>`** (plus `.json` and `/log`,
same contracts as 8.3) renders the run-record template of 5.3 for a record whose `job` is null: eyebrow
`system · <what>` (`usage refresh`, `billing check`, `destination probe`, `destination setup`), h1
`<What> Tue 15 Sep 09:04 — OK`, no job link, no restore-point row, and `← All activity` in place of
`← <job>`. Every system row in the table and in `activity.json` carries `record: "/activity/<id>"`;
job rows keep `/jobs/<name>/runs/<id>`. A malformed or unknown id → the 404 error page (there is no
pending state for system operations: `sysop` appends its start event synchronously before it works).

Kind `provision` is written by the provisioning success paths: `provision_validate` and
`provision_automated` call `runs.append_event(cache_dir, None, {...,"kind":"provision","event":"start"})`
immediately before the tool runs and the matching `end` event after it, synchronously in the request
(they already block on the tool and already re-render on failure), so "destination setup" appears in
Activity with its outcome and its scrubbed output as the log.

Data: `runs.read_all(cache_dir, jobs)` merging `state/*.runs.jsonl` and `state/_system.runs.jsonl`
(7.1.7); contract 8.4.

Acceptance:
- [ ] Merges job and `_system` records newest first; filters work as query parameters; `limit` honoured.
- [ ] Each row links to the correct run record; system rows have no job link and their `Record · open →` resolves to `/activity/<id>` with a 200.
- [ ] A successful `POST /setup/destination/validate` leaves one `provision` record in `_system.runs.jsonl` and one Activity row.

### 5.6 Cost workbench — `/cost`

Purpose: "Is this model telling me the truth, and what will this cost me over time?" Dense container,
five bands. Every figure is mono tabular with its provenance mark; one legend per band; one
`.price-stamp` at the end with the live-prices checkbox (as on the job page).

Band 1 — Proof. `.grid4` as the Board's band 4 (`In the bucket now` · `Last invoice` · `The model says` ·
`Difference`), each `.s` naming source and date, plus the delta line and its verdict. Right of the head:
`btn btn-sm` `Refresh usage` (POST `/costs/refresh`, runs as an operation record; the button disables
and the work bar shows) and `btn btn-sm` `Check the bill` (POST `/costs/billing/refresh`). When Cost
Explorer is connected but not tag-scoped, a `sig-note` inside the band: `This invoice is the whole AWS
account, not just these backups. Until a cost-allocation tag scopes it (Keys & secrets → Billing), the
difference above compares a projection for two folders against everything you pay Amazon.` Not
connected: `Last invoice` `—` / `billing not connected` and a `linklike` `Connect AWS billing →` →
`/setup/keys#billing` (the credential is edited there and nowhere else, band 5).

Band 2 — Per job. `<th>` `Job` · `What it keeps for you` · `Storage tier` · `Projected` · `In the
bucket` · `Δ`: `manga` · `<p-measured>1.78 TB</p-measured> · 232,021 files` · `Thaw first, hours
<code>DEEP_ARCHIVE</code>` · `$1.85 /mo` · `<p-measured>1.78 TB</p-measured> · $1.85 /mo priced now` ·
`±$0.00`; `appdata` · `52.71 GB + ~11.6 GB of old versions` · `Instant <code>STANDARD</code>` ·
`<p-assumed>$2.69</p-assumed> /mo` · `64 GB · $1.47 /mo priced now` · `−$1.22 <hint>versions still
piling up</hint>`; totals rule `All jobs` · `<span class="p-assumed">$4.54</span>` · `$3.32` ·
`−$1.22` (the `Projected` total takes the weakest mark among its jobs, 4.6). `In the bucket` prices the
measured prefix at its class now (`estimate_io.current_costs`, shared store priced at STANDARD).

Snapshot backup rows show the **whole** snapshot store in `In the bucket`, because it is one shared
`appdata/` prefix (5.2): with more than one such job the cell gains the `.hint` sub-line `whole snapshot
store · shared by <N> jobs`, every one of those rows shows the same bytes and the same
`in_bucket_monthly`, and the `All jobs` totals row counts the store **once** — summing the rows would
bill it N times. `Δ` on those rows is then `—` with the `.hint` `not comparable — one shared store`,
because a per-job difference against a shared measurement would be a number nobody can act on. With a
single Snapshot backup job (the example) none of this is visible: the store is that job's.

Band 3 — Projection. `.projgrid{grid-template-columns:minmax(0,1fr) 300px}` (one column ≤ 900px). Left:
the SVG chart (`#cost-timeline`, viewBox 720×260, the existing `drawCostChart` reworked): three curves
— `your keep rules` (`--accent`), `no old versions kept` (`--muted`), `rolling 30 days` (`--ok`) — with
end-of-line labels carrying the 24-month totals (`$102 over 24 months`), a dashed vertical at the
settle month labelled `settles month 6`, and for a `keep_all` job the readout `still climbing at month
24` with no plateau line. Under the chart the month scrubber: `input[type=range]#month` 1–24 whose
readout (`.scrub`, mono) is the breakdown at that month: `Month 6 · storing your files $1.21 · old
versions $1.46 · uploading $0.01 · replacing old versions $0.00 · total $2.67` (from
`projection.primary.months[m-1]`; this replaces the static milestones table). Right: the lever panel
(`.levers`, a card): per job `Change rate` (the four radios), `Already bundled` (checkbox + size), and
scenario-wide `Restore` (`restore_fraction` as `How much you'd get back: all of it / half / a tenth`),
`Restores a year` (number), `Retrieval speed` (select). Levers are what-if: editing recomputes live
(250 ms debounce, `GET /cost.json?<params>`, the changed figures flash, the curve morphs over 180 ms);
a lever differing from the saved job gets a 2px `--accent` left rule and the readout `was: 1%`; the
panel head shows the chip `scratch — not saved` with `[Apply to appdata]` (POST `/jobs/<name>/assumptions`,
writes `assumptions` on that job) and `[Reset]`.

The whole lever panel is a real `<form method="get" action="/cost">` whose inputs are exactly the query
parameters `/cost.json` accepts, and it ends with
`<noscript><button class="btn">Recalculate</button></noscript>` — the preserved no-JS fallback
(`estimate.html:215`, 2.3). Without JS the form submits, `/cost` re-renders server-side with the same
figures, and nothing on the page is a dead control. The JS path just intercepts the submit and calls
`cost.json` instead.

The panel's POST buttons — `[Apply to appdata]` (`/jobs/<name>/assumptions`), the scenario group's
`[Apply]` (`/costs/scenario`), and `[Reset]` — are **not** nested forms. `[Apply to appdata]` is a
submit button carrying `formmethod="post" formaction="/jobs/<name>/assumptions"`, so it posts this same
GET form's lever fields (which are a superset of the assumptions) to that route; the scenario `[Apply]`
is its own sibling `<form method="post" action="/costs/scenario">` associated by `form="<id>"` so it
posts only the three scenario fields; `[Reset]` is a link back to `/cost`. No `<form>` is ever nested
inside another.

The scenario-wide levers (`restore_fraction`, `restores_per_year`, `retrieval_tier`) persist, because
they are not per-job: `[Apply]` on that group posts **`POST /costs/scenario`** (`csrf`,
`restore_fraction` ∈ `1 | 0.5 | 0.1`, `restores_per_year` int ≥ 0, `retrieval_tier` ∈
`Bulk | Standard | Expedited`) → 302 `/cost` with the `success` flash `Saved.` It writes
`$CONFIG_DIR/cost.json` NEW, whose entire shape is
`{"restore_fraction": 1.0, "restores_per_year": 1, "retrieval_tier": "Standard", "set_at": "<iso>"}`
(temp + `os.replace`; a missing or unparseable file means the model's own defaults and is never an
error). Out-of-range values → 400 with the field message; the file is written only on a clean parse.

Band 4 — What a restore costs. `<th>` `Job` · `Data out` · `Warm-up` · `Speed` · `Total, once` · ``:
`appdata` · `52.71 GB` · `none — instant tier` · `—` · `$4.74` · `[Get data back →]`; `manga` ·
`1.78 TB` · `up to 12 h` · `Standard` · `$168.40` (+ a second line `Bulk · up to 48 h · $164.10`) ·
`[Get data back →]`. Note under it: `Getting it back is warm-up plus download out of Amazon. It does not
get cheaper when the storage does.`

Band 5 — Assumptions and billing. The persisted assumptions listed as `.defgrid` rows with `.p-assumed`
marks and `set 12 Sep` stamps. Then billing — a **read-out, not a form**. There is exactly one editing
surface for the Cost Explorer credential and it is Keys & secrets (5.12), because two write-only forms
over one secret, with two save routes, is how a value silently ends up half-saved. Connected:
`.defgrid`-style line `Billing: connected · read-only Cost Explorer credential · tag <COST_EXPLORER_TAG>`
(`tag not scoped` when it is empty) plus `linklike` `Change under Keys & secrets →` →
`/setup/keys#billing`. (`Check the bill` stays where it already is, on band 1 beside the figure it
refreshes; it is not repeated here.) Not
connected: `Billing: not connected — the invoice column stays empty.` plus `linklike`
`Connect AWS billing →` → the same anchor. Lead, unchanged in voice: `Optional and read-only: a separate
Cost Explorer credential, never the runtime backup key.` `POST /costs/billing` is deleted (the route
301s to `/setup/keys`), and the `Connected AWS billing.` / `Disconnected AWS billing.` flashes move to
the Keys & secrets save path, which raises them when the `COST_EXPLORER_*` group goes from empty to set
or set to empty. Last, the collapsed `<details>` `How this is calculated` with the five existing
bullets re-voiced: `Amazon bills every object as at least 128 KB on the cold tiers.` · `Data out is
priced at the first-tier rate.` · `Replacing a file on a tier with a minimum stay is charged for the
whole minimum.` · `Snapshot backups keep old versions according to the keep rule; Plain copies keep
replaced files for the days you chose.` · `One bundled price table (us-east-1) stamped with its capture
date, or the live AWS list when you switch it on.`

Data: `estimate_io.cost_page()` NEW composing `scenario_from_jobs`/`scenario_from_params`, `estimate`,
`projection_bundle`, `current_costs`, cached billing, `restore_quote` per job, `delta_verdict`;
contract 8.7. `POST /costs/refresh` and `/costs/billing/refresh` launch `python3 -m app.engine.sysop
usage-refresh|billing-check` detached (7.7.3) and redirect to `/cost` with the note flash `Refreshing
usage — watch it in Activity →`.

Acceptance:
- [ ] `/estimate` → 301 `/cost`; `/estimate.json` and `/cost.json` return the same JSON.
- [ ] Band 1 figures come from caches only (no Cost Explorer call during render).
- [ ] Scrubber readout matches `projection.primary.months[m-1]` to the cent; `keep_all` shows `still climbing` and no plateau line.
- [ ] Levers: edit → flash + morph; Apply writes `assumptions`; Reset returns to saved.
- [ ] The lever panel is a `GET /cost` form carrying the `cost.json` parameters and ending in the `<noscript>` `Recalculate` button; `GET /cost?change_rate_pct=10` renders the recomputed figures server-side with JS off.
- [ ] `POST /costs/scenario` writes `$CONFIG_DIR/cost.json` and redirects with `Saved.`; an out-of-range `restore_fraction` → 400 and no file written.
- [ ] `/cost` contains no `COST_EXPLORER_*` input; `POST /costs/billing` is gone and `/costs/billing` 301s to `/setup/keys#billing`.

### 5.7 Raw log — `/logs`

Unchanged behaviour (`runner.tail_log`, `text/plain`, `?tail=N`), linked only from Activity's footer.
No template.

### 5.8 Create job — `/jobs/new` (from the blend)

Purpose: create one job with every cost consequence visible beside the control that moves it. Route
`GET /jobs/new`; live figures `GET /jobs/estimate.json?<form>&prices=<kind>`; measurement `GET
/jobs/source-size?path=`; tree `GET /jobs/browse?path=`; save `POST /jobs`. Container `.form` 960px
(`padding-inline:24px; padding-block:24px 72px`; 16px ≤ 640px). Everything numbered below is one
`<section class="sec">` (`margin-top:32px; border-top:1px solid var(--border); padding-top:24px`)
opening with `<h2 class="lbl">N · Title</h2>` (11px mono uppercase .12em `--faint`).

The product computes every figure with `estimate_io.wizard_estimate` (extended, 7.9); the mockup's JS
arithmetic is a placeholder and must not be ported.

#### §1 Page header and the three idioms
`<p class="lbl">New job</p>`; `<h1 class="h">What do you want to protect?</h1>` (20px/1.3/620,
`text-wrap:balance`); lead `.measure.muted`: `Three kinds of thing on this page: <b>settings</b> change
what gets backed up, <b>measured facts</b> come off disk, and <b>assumptions</b> are your estimate and only
move the forecast.` Settings post as form controls saved by `jobs_io`; measured facts are read-only
figures with a timestamp and a Re-measure action; assumptions are marked and persisted on the job as
`assumptions` (7.8). Until a folder is picked and measured, sections 2–4 are dimmed (`color:var(--faint)`,
`pointer-events:none` on the controls, never `hidden`) with the reason printed under each `h2`:
`Pick a folder first — everything below is priced for it.`; every figure renders `—`.

#### §2 Section 1 · Which folder
`<h2 class="lbl">1 · Which folder</h2>`; `<p class="mono sm faint">/mnt/user</p>` (the owner-facing root
= `SOURCE_ROOT_HOST`, NEW env, default `/mnt/user`); the folder tree in `.card.tree` (`max-width:34rem`;
`padding:10px 12px; max-height:320px; overflow:auto; display:grid; gap:2px`; rows `label` flex gap 8px,
14px, hover `--surface-2`; checkbox `accent-color:var(--accent)`; names mono) — the preserved lazy tree
from `app.js` (expander per node, children from `/jobs/browse`, single-select writing the relative path
to hidden `name="source"`), restyled with the expander as a mono glyph. `Selected <span
class="mono">/mnt/user/appdata_backups</span>` (nothing picked: `Selected <span class="faint">nothing
yet</span>`). Measurement line `.sm.faint`: `52.71 GB · 533 files · measured just now · <button
class="linkish">Re-measure</button>` — GB to 2 decimals with thousands separators, MB to 1 decimal
below 1 GB, KB below 1 MB (the shipped `fmtBytes`); the stamp is `just now` in-session, on edit the
saved `measured.at` as `Mon 14 Sep`. While walking: `measuring…` with `—`. Walk failed (`capped: true`,
`bytes: 0`): `Couldn't finish measuring this folder — it may be extremely large or unreadable.` and
`20 GB is a placeholder, not a measurement. Run this job once to measure it.`; every downstream figure
switches to `.n.assumed`. Partial (`capped` with bytes): append ` (measurement may be incomplete)` and
render as assumed. The wizard posts `size_gb`, `file_count`, `measured_at` and the two hidden fields
`measured_bytes` (the exact byte count, straight from `/jobs/source-size`) and `measured_capped` — the
first because `size_gb` is a rounded GB float and `job["measured"]["bytes"]` must be exact, the second
because a capped walk has to stay visibly capped after a save (7.8, 8.6). Today `file_count` never
reaches the model at all — fix that too.

#### §3 Section 2 · The plan
`<h2 class="lbl">2 · The plan</h2>`; sub-headings `<h3 class="subh">` (15px/1.4/620).

**§3.1 What kind of backup.** Three radios `name="type"` (`versioned`, `versioned-files`, `archive`),
comparison open, never behind a disclosure. The recommended one is wrapped in `<div class="sev rec">`
(`.sev`: grid gap 6px, `padding:12px 14px; border-left:2px solid var(--faint); margin-block:12px`;
`.rec`: rule `--accent`, no fill) with tab `<span class="sev-tab">` (glyph hollow circle-dot, text
`Recommended for this folder`), the radio, and the rule line `.sm.faint.measure`: `Why: 533 files in
52.71 GB that change a little between runs is the shape this is for. Rule: under 10,000 files and some
change → Snapshot backup.` The other two radios sit below, bare. Radio markup:
```
<label class="radio" for="t-snap"><input type="radio" id="t-snap" name="type" value="versioned">
  <span><b>Snapshot backup</b> &nbsp;<a class="term" href="/setup/about#restic" title="jobs.json: type = versioned (restic)">versioned</a></span>
  <span class="why">Roll the whole folder back to any night. Encrypted, de-duplicated, and it needs a tier it can read every run.</span></label>
```
`File history` · `versioned-files` — `Keeps every version of each file. Restores one file at a time, and
works on cold storage.`; `Plain copy` · `archive` — `Cheapest. The current state of the folder, no
history.` `.term`: mono .92em `--faint`, dotted underline (`text-decoration-color:var(--border-strong)`,
offset 3px), `cursor:help`.

Recommendation rules (`storage_advice.recommend_type(*, size_gb, file_count, change_rate_pct, measured,
change_rate_set=True)` NEW, pure, keyword-only — the numeric arguments are easy to transpose, so the
signature forbids it; returns `None` on an unmeasured folder or when no rule fires), evaluated in order:
1. `file_count < 10,000 and change_rate_pct > 0` → Snapshot backup. Why: `<count> files in <size> that
   change <churn phrase> between runs is the shape this is for.` Rule: `under 10,000 files and some
   change → Snapshot backup`.
2. `file_count ≥ 50,000 and size_gb / file_count < 0.01 and change_rate_pct == 0` → Plain copy. Why:
   `<count> files in <size> that only ever get added is the shape this is for.` Rule: `more than 50,000
   files and nothing changes → Plain copy`. The size clause is an average under 10 MB per file, not 1 MB:
   the owner's own example (manga, 232,021 files in 1.78 TB) averages 7.9 MB, and a rule that cannot fire
   for the job it was written for is not a rule. What actually drives the advice at this shape is the
   object COUNT — 232,021 upload requests, one per file — which is why 10 MB is still narrow enough to
   exclude a folder of a few large files.
3. `size_gb ≥ 500 and change_rate_pct == 0` → Plain copy. Why: `<size> that only ever gets added is the
   shape this is for.` Rule: `large and nothing changes → Plain copy`.
**The change rate is not an answer until the owner gives one.** The fresh form starts at ~1% (Appendix B
#17), so a literal reading of rules 2 and 3 (`change_rate_pct == 0`) would mean the manga-shaped folder
— 232,021 files, 1.78 TB, the very job these rules were written for — gets no RECOMMENDED block at all
until the owner happens to click the `Nothing` radio. That is the owner's chosen block silently
disabled on the example job, so the predicate is evaluated against a tri-state, not a number:

- `recommend_type` takes one more keyword, `change_rate_set: bool` (default `True`).
- While the change-rate radios are **untouched in this session** (`change_rate_set=False`), rules are
  evaluated with the change rate treated as unset: rule 1 requires only `file_count < 10,000`; rules 2
  and 3 drop their `change_rate_pct == 0` clause and fire on shape alone. Rule order is unchanged, so
  a small folder still lands on rule 1.
- The moment the owner clicks any change-rate radio, `change_rate_set` is `True` and the predicates read
  exactly as written above. The edit screen always passes `True` (a saved `assumptions.change_rate_pct`
  is a real answer, 7.8).
- The form carries this as `change_rate_touched=1` on `/jobs/estimate.json` once a radio is clicked;
  absent means untouched (8.6).
- Only the PREDICATE ignores the change rate. The printed `Why:` sentence always uses the form's current
  value, so a fresh appdata form prints "…that change a little between runs…" exactly as the mockup does.

No rule → no radio checked and, in the block's place, `.sev.note`: `No recommendation for this folder —
none of the rules fit its shape. Pick the kind yourself; the table below is priced for all three.`
Churn phrases: 0% `never`, 1% `a little`, 10% `some`, 30% `a lot`. Re-evaluated when the folder,
measurement or change rate changes; it pre-selects the recommended type only if the owner has not
touched the type radios in this session; otherwise the block moves and the selection stays.

**§3.2 Where it's stored — priced for your 52.71 GB.** `<h3 class="subh">Where it's stored — priced for
your <span class="n">52.71 GB</span></h3>` (unmeasured: `priced for an assumed <span class="n
assumed">20 GB</span>`). `<div class="tscroll card">` → `<table class="classes">` (`min-width:640px`,
14px; `th` 11px mono caps right-aligned except the first; `td` mono tabular right-aligned, first cell
sans left; hairlines; last row no border; `tr.blocked td{color:var(--faint)}`, `td.struck` line-through;
`.reason` block, sans 13px `--danger`, left). Columns: `Class` · `Per month` · `Getting it back` ·
`Minimum stay` · `All of it, once`. Five rows in `model.STORAGE_CLASSES` order, first cell `<label
class="cls"><input type="radio" name="storage_class" value="STANDARD"><span>Instant<br><a class="term"
href="/setup/about#tiers" title="AWS S3 storage class">STANDARD</a></span></label>`:

| Plain / constant | Per month | Getting it back | Minimum stay | All of it, once |
|---|---|---|---|---|
| Instant / `STANDARD` | `$2.69` | `instant` | `—` | `$4.74` |
| Instant, cheaper to keep / `STANDARD_IA` | `$1.46` | `instant` | `30 d` | `$4.74` |
| Instant, cold price / `GLACIER_IR` | (model) | `instant` | `90 d` | (model) |
| Cold / `GLACIER` | struck | `3–5 h` | `90 d` | struck + `can't be read by a Snapshot backup` |
| Deepest / `DEEP_ARCHIVE` | struck | `≤48 h` | `180 d` | struck + `can't be read by a Snapshot backup` |

Per month = `classes[i].monthly` (the candidate re-priced on that class with the current type, change
rate and keep rule; `—` with `title="still growing — Keep everything never plateaus"` when unbounded);
Getting it back = a literal static map, served as `classes[i].read_access` and never derived from a
tier: `STANDARD → instant`, `STANDARD_IA → instant`, `GLACIER_IR → instant`, `GLACIER → 3–5 h`,
`DEEP_ARCHIVE → ≤48 h` (verbatim from mockup-ledger-runbook.html lines 835 and 841). The two cold values
are deliberately not one tier's numbers — GLACIER prints its Standard-speed window and DEEP_ARCHIVE its
Bulk ceiling, which is what each one's default retrieval actually costs the owner in waiting; "the
Bulk-tier ceiling" would print `5–12 h` for GLACIER and contradict both the table and the mockup. The
per-speed detail lives in the warm-up step's `Retrieval speed` options (4.3), not here.
Minimum stay = `prices.min_storage_duration_days`;
All of it, once = `classes[i].restore_once` (the model's `restore_cost` at fraction 1.0, scenario tier).
Blocked rows follow the selected type live: Snapshot backup → `GLACIER`, `DEEP_ARCHIVE`; the other two
types → none. Selecting a blocked row is allowed and raises the WON'T RUN block. Sentence under the
table `.sm.faint.measure`: `Getting it back is warm-up plus download out of AWS. It does not get cheaper
when the storage does.` Heads-up blocks (§3.7) render directly under this sentence.

**§3.3 The WON'T RUN blocker.** `<div class="sev wont" id="class-blocker" hidden>` (rule 3px `--danger`,
`--danger-bg`; tab glyph filled octagon, text `Won't run`). Body: `A Snapshot backup can't read from
<b>Deepest</b> · <span class="mono">DEEP_ARCHIVE</span>. <a class="term">restic</a> re-reads its whole
store every run, so every scheduled run would fail on a data read.` Actions `.acts`: `btn btn-ghost
btn-xs` `Use File history instead` (sets `versioned-files`), `btn btn-ghost btn-xs` `Use Instant, cheaper`
(sets `STANDARD_IA`), `linkish` `Save it anyway ▸`, `span.sm.faint` `records an acknowledgement on the
job`. Predicate `type == versioned and storage_class in {GLACIER, DEEP_ARCHIVE}`, evaluated on every
change and on load, client- and server-side. While true: both footer buttons disabled and `<p class="sm"
id="create-why" style="color:var(--danger)">Fix the blocker above first.</p>` under the footer. `Save it
anyway ▸`: re-enables the buttons, keeps the block visible, relabels itself `Acknowledged — will save
anyway`, replaces `#create-why` with `.sm.faint` `Saving with the blocker acknowledged.`, and the save
posts `acknowledge_blocker=snapshots_on_cold_class`; `job_save` refuses a POST whose server-side blocker
list is non-empty unless every code is acknowledged (re-renders with the block open). Persisted as
`acknowledged` (7.8); the Board never nags about an acknowledged blocker. Other blockers: no folder →
buttons disabled and `#create-why` `Pick a folder first.`; all-zero tiered keeps → WON'T RUN beside the
Advanced inputs: `Keeping 0 of everything would remove every restore point. Keep at least one.` with
`Use last 3 · daily 7 · weekly 4 · monthly 6`.

**§3.4 How much of it changes each backup?** Directly above the keep rule. Four radios
`name="change_rate_pct"`: `ch-0` `0` `Nothing — files only get added <span class="mono faint">(0%)</span>`;
`ch-1` `1` `A little — rare replacements (~1%)` (fresh-form default); `ch-10` `10` `Some — regular edits
(~10%)`; `ch-30` `30` `A lot — churny (~30%)`. Then `.sm.faint` `An assumption. It never changes what
gets backed up.` Shown for all three types. It moves: the Per-month column, every keep-rule delta, the
consequence sentence, WHEN/WHOSE, the footer, the working text and the recommendation.

**§3.5 How long to keep old versions.** `<h3 class="subh" id="keep-head">How long to keep old
versions</h3>`; `<div id="keep-block">` with radios `name="retention_type"`:

| id | value | label | `.why` |
|---|---|---|---|
| `k-all` | `keep_all` | `Keep everything` | `<span class="k-delta">still growing</span> · never plateaus` |
| `k-thin` | `tiered` | `Thin them out over time` | `<span class="k-delta">+$1.48/mo</span> · <span class="mono">last 3 · daily 7 · weekly 4 · monthly 6</span> — 20 restore points over about 190 days · <a>Advanced ▸</a>` |
| `k-days` | `days` | `Keep for <input name="retention_days" value="180"> days` | `<span class="k-delta">+$13.32/mo</span> · 180 restore points` |
| `k-n` | `count` | `Keep the last <input name="retention_count" value="30"> versions` | `<span class="k-delta">+$2.22/mo</span> · 30 restore points` |

The mono run in the tiered `.why` is `keep_rule_compact` (4.3) — the blend's compact grammar, used on
this screen and nowhere else; `keep_rule_label` and `keep_rule_prose` are the skeleton's two forms.
Inline number inputs are `width:5ch`, mono. `Advanced ▸` on the tiered option reveals four inputs
`keep_last / keep_daily / keep_weekly / keep_monthly` labelled `last`, `daily`, `weekly`, `monthly`
(defaults 3/7/4/6). Deltas = `keep_options[k].delta_monthly` (the old-versions cost of that policy at
the selected tier); restore-point counts from `keep_options[k].points` / `reach_days` (tiered: the tier
sum over `_tiered_reach_days`; days: `days × backups per day`; count: N). Tiered is Snapshot-backup-only:
on the other types the row stays visible, struck, radio disabled, with `.reason` `only a Snapshot backup
can thin out`; a type change away from Snapshot backup snaps a checked tiered to days (preserved
`applyVisibility`). Defaults per type: Snapshot backup → tiered; File history → days 180; Plain copy →
days 180. Consequence line `#keep-consequence`:
1. change > 0, bounded: `At ~1% change, old versions settle at about 1.22× your data — <span
   class="n">$1.48</span> of the <span class="n">$2.69</span>. Keeping less also limits how far back you
   can restore, which is worth something at 0% too.`
2. change > 0, Keep everything: `Keep everything never plateaus, so no typical month is printed for it —
   only "still growing". At ~1% change the store grows without bound.`
3. change = 0%: `Nothing gets replaced, so there are no old versions to store — every option above adds
   <span class="n">$0.00</span>. It is still a real choice: it bounds how far back you can restore, and
   how much one bad night can cost you.`
At 0%: the heading gains `<span style="font-weight:400;font-size:13px;color:var(--warn)">— no cost
effect at 0% change · still bounds how far back you can restore</span>` (the only amber on this
screen); `#keep-block` gets `.inert` (`color:var(--muted)`, radios stay enabled); every delta reads
`+$0.00/mo`, Keep everything's tail becomes `· nothing to grow`. Leaving 0% restores all of it.

**§3.6 What it costs — WHEN × WHOSE.** `<hr class="hair">`; `<p class="lbl">What it costs · pick a
<b style="color:var(--ink)">when</b>, read the <b style="color:var(--ink)">whose</b> row</p>`. Segmented
control `<div class="seg" role="tablist" aria-label="When">` with three tabs (`data-when`): `first` `Your
first bill`; `typical` `A typical month` (default); `six` `The first 6 months`. `.whose` grid
(`minmax(0,1fr) 12ch`; 10ch ≤ 640px): column head `#when-head` names the held axis in full — `your first
bill` / `a typical month, per month` / `the first 6 months, a total not a rate`; row 1 `this job —
appdata` (the live name; `this job` before one is typed) `$2.69`; row 2 (top hairline) `all jobs —
appdata + manga` `$4.54`. Dead cells: this job's typical when unbounded → `still growing`; the all-jobs
typical when any job is unbounded → `at least $X` (`all_jobs.typical_floor`); the six-month cells always
print the real totals. Assumed size → both `.n.assumed`. Any figure the server cannot compute → `—`.

**What drives `.n.assumed` on this screen — and only on this screen:** `provenance.size == "assumed"`,
nothing else. That means the folder walk failed, or no folder has been measured yet and the 20 GB /
1,000-file placeholder is standing in. Every other `provenance.*` key in the 8.6 response is ignored
here. The change-rate assumption is already declared twice — by the radios the owner just set and by the
`An assumption. It never changes what gets backed up.` line under them — so marking it a third time
would dot and dim every figure on a normal, fully measured form, the opposite of what the blend mockup
draws (at the default ~1% every figure there is plain; Appendix B #49). The skeleton screens keep the
full 4.6 rule, because their reader did not set the assumption and has to be told it exists.
Closing sentences `#when-note`: `About <span class="n">$2.69</span> a month once history has built up.
Your first bill is <span class="n">$1.39</span>, because no old versions exist yet. <button
class="linkish" data-toggle="new-working">Show working ▸</button>` — unbounded: `No typical month —
Keep everything never settles. Your first bill is $1.39, because no old versions exist yet.`; reason
clause from `first_bill_reason`: `because no old versions exist yet` (ramp) / `because uploading 232,021
objects to a cold tier costs $0.05 per 1,000 requests — $11.60, once. Charged per request, not per GB.`
(upload) / `because no old versions exist yet, and uploading <N> objects costs $<Y>, once.` (both) /
`Your first bill is the same — nothing builds up.` (flat). `Show working ▸` toggles `#new-working`
(`.sev.note`, 120 ms height, `aria-expanded`): head `52.71 GB measured × <span class="mono">$0.023</span>/GB·mo
= <span class="mono">$1.21</span> to store the files.` then bounded: `Old versions at ~1% change =
1.22 × 52.71 GB = <span class="mono">64.31 GB</span> = <span class="mono">$1.48</span>. Together <span
class="mono">$2.69</span>. Month 1 holds about 12% of that version store, so the first bill is <span
class="mono">$1.39</span>; it reaches <span class="mono">$2.67</span> by month 6 and plateaus. Six months
add up to <span class="mono">$12.19</span>.` (`and plateaus` only when `steady_month ≤ 6`, else `and keeps
climbing until month <N>`; the percentage is `explain.month1_pct_of_steady`); unbounded: `Keep everything
retains every copy ever made, so the version store has no settling point at ~1% change and no typical
month can be printed for it — only "still growing". Your first bill is <span class="mono">$1.39</span>,
because no old versions exist yet.`

**§3.7 Heads-up blocks** (`.sev.heads`: rule 2px `--warn`, `--warn-bg`; tab glyph outline triangle
`<path fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" d="M6 1 11.2 10.6H.8z"/>`,
text `Heads up`) under the class table, from `warnings[]`:
1. per-object overhead (cold tier, ≥ 50,000 effective objects averaging under 10 MB — raise
   `storage_advice.class_advice`'s current `(size_gb * 1024 / object_count) < 1.0` to `< 10.0`, for the
   same reason as recommendation rule 2: at 7.9 MB average the manga example is exactly the case this
   warning describes, and the cost it warns about is per-request, not per-byte): `232,021 objects × 40
   KB of per-object overhead on <b>Deepest</b> · <span class="mono">DEEP_ARCHIVE</span>, and uploads cost
   ~10× more per request there — $11.60, once, and $0.05 a month on top of the data. Bundling them first
   (one .cbz per chapter, or tar) collapses the count.` + `btn btn-ghost btn-xs` `My files are already
   bundled` (checks the bundled box).
2. minimum stay (≥ 90 days): `180-day minimum stay on <b>Deepest</b> · <span class="mono">DEEP_ARCHIVE</span>.
   Delete it tomorrow and you still pay through day 180 — <span class="n">$10.95</span>.`
3. Snapshot backup on a per-GB-retrieval tier: `A Snapshot backup re-reads its store every run, and
   <b>Instant, cheaper to keep</b> · <span class="mono">STANDARD_IA</span> charges $0.01 a GB for every
   read — on 52.71 GB that is about <span class="n">$0.53</span> a run, often more than the cheaper storage
   saves.` + `Use Instant · STANDARD`.
4. frequent runs on a long-minimum tier (Plain copy / File history, ≥ 4 runs a month, minimum ≥ 180 d,
   change > 0): `You back up 30 times a month onto a 180-day-minimum tier. Each replaced file re-incurs
   that minimum, so a shorter-minimum tier can be cheaper despite a higher rate.` + `Use Instant, cold
   price · GLACIER_IR`.
5. the edit-only class-change pair (5.9).

**§3.8 Type-specific settings** (after the keep rule): Plain copy → `<h3 class="subh">When you delete a
file locally</h3>` radios `name="mirror"` `0` `keep it in the copy` (default) / `1` `delete it from the
copy too`. Plain copy and File history → under the class-table sentence and above the change rate:
checkbox `name="packing"` `My files are already bundled (.cbz, tar)` and, when checked, `Typical archive
size <input name="pack_member_gb" value="0.05"> GB each`; hint `cost estimate only — the tool copies
files as they are and won't bundle for you` and `An assumption. It never changes what gets backed up.`
Both use the preserved `data-when-type` / `data-when-packing` mechanism.

#### §4 Section 3 · How often
`<h2 class="lbl">3 · How often</h2>`; row: `Frequency` `select#freq` (`Hourly`, `Daily` (default),
`Weekly`, `Monthly`), `At` `input[type=time]#attime` (default `05:00`), `On` day-of-week select (Weekly),
`Day` 1–28 (Monthly) — the preserved builder, conditional visibility kept. Human sentence `.sm`: `Runs
every day at 05:00 — <span class="mono faint">0 5 * * *</span> · <a>Advanced ▸</a>` (`Runs every hour at
:15`, `Runs every Sunday at 04:00`, `Runs on day 1 of every month at 03:00`). `Advanced ▸` reveals the raw
`name="schedule"` field with `e.g. <code>0 3 * * *</code> = daily 3am, <code>0 4 * * 0</code> = weekly Sun
4am` and `Use the builder`; an unparseable saved cron opens it on load. Checkbox `name="enabled"
value="1"` `Run on this schedule` (checked). Collision line (Snapshot backup only): `No other Snapshot
backup runs at 05:00. Snapshot backups share one store and cannot overlap.` / `<b>appdata</b> also runs
at 05:00. Snapshot backups share one store and cannot overlap — use 05:20. <button class="btn btn-ghost
btn-xs">Use 05:20</button>` (the first free minute ≥ +20 min; predicate: another enabled `versioned` job
with the same minute and hour; other jobs embedded as `<script type="application/json" id="other-jobs">`).

#### §5 Section 4 · Name it
`<h2 class="lbl">4 · Name it</h2>`; `Job name` `input.mono name="name"` (`max-width:24rem`); hint by type:
Snapshot backup `Letters, digits, dot, dash, underscore. Used as the tag on each snapshot. All Snapshot
backups share one store — <span class="mono">s3://bw-backups/appdata/</span>.`; others `Letters, digits,
dot, dash, underscore. Used as the folder name in the bucket — <span class="mono">s3://bw-backups/media/<name>/</span>.`
Validation `jobs_io.JOB_NAME_RE`; create refuses an existing name: `A job called appdata already exists.`

#### §6 The sticky footer
`<div class="formfoot">` (`position:sticky; bottom:0; z-index:20; height:44px; display:flex;
align-items:center; gap:12px; padding-inline:14px; background:var(--surface); border-top:1px solid
var(--border); margin-top:24px` — fixed height, cannot grow; ≤ 640px it wraps to two rows). Contents:
`btn btn-primary#create-btn` `Create job`; `btn btn-ghost#create-run-btn` `Create and run it now`
(saves, launches the run, lands on the job page Running); `linkish` `Cancel` (→ `/`); `.figs#foot-figs`
(`margin-left:auto`, 13px `--faint`, `.n` in `--ink`): `a typical month — this job <span class="n">$2.69</span>
· all jobs <span class="n">$4.54</span> &nbsp; <a href="/cost">over time →</a>` (`your first bill — …`,
`the first 6 months — …`; follows the WHEN tab; dead cells identical to §3.6). Submitting relabels the
pressed button `Creating…` and disables both. `#create-why` sits after the footer in the DOM.

#### §7 The price stamp and the live toggle
`<p class="lbl" id="pricestamp">` at the bottom: `Prices: bundled table, us-east-1, captured 27 Aug 2026.
<button class="linkish">Use live AWS prices ▸</button>` / `Prices: live AWS price list, us-east-1,
fetched 15 Sep 09:04. <button class="linkish">Back to the bundled table ▸</button>`; fetching: `Prices:
fetching the live AWS price list…` (toggle disabled); failed: `Prices: bundled table, us-east-1, captured
27 Aug 2026 — the live list could not be fetched.`; region without a table: `Prices: bundled us-east-1
table used for eu-west-1 (no table for it), captured 27 Aug 2026.` Toggling re-requests with
`prices=live|bundled`; every figure re-renders and flashes. A page-level preference; nothing persisted.

#### §8 Validation and failure containment
Live estimate errors (`{"error"}` 400) → a `.sev.note` line under section 2 and every figure `—`. Per-field
validation on blur (name charset; cron shape; tiered keeps not all zero; days ≥ 0; count ≥ 1; source
present); field errors `.sm` in `--danger` under the control. On submit the server re-renders this page
with every value intact and errors anchored: `job name must be…` → name; `source …` → section 1;
`schedule …` → section 3; retention messages → keep block; `unknown storage class` → class table.

Corrupt `jobs.json` on save (`JobsFileError`) → **a 200 re-render of this form**, every typed value
intact, with a top-of-page `sig-failure` carrying `jobs.json is not valid JSON; fix or remove it before
editing jobs.` Not the error page and not a flash-and-redirect: the owner has just filled in four
sections, and both of the other two behaviours throw that away for a fault that has nothing to do with
what was typed. The error page and the `failure` flash are removed from 8.10 and 5.15 for this route;
the flash survives only on `POST /jobs/<name>/delete`, which has no form to re-render.

Live recompute keeps its no-JS fallback (2.3): the form ends with
`<noscript><button type="submit" name="recalc" value="1">Recalculate</button></noscript>`. A POST
carrying `recalc=1` re-renders this page with every figure computed server-side from the submitted
values and **saves nothing** — no `jobs.json` write, no redirect, no flash — so the page is honest with
JS off instead of showing stale or empty numbers.

CSRF kept. Save → 302 `/jobs/<name>` with the success flash `Saved appdata.` + `[Run it now]`.

Data: `wizard_estimate` (8.6), `jobs_io.validate` (7.8). JS: 250 ms debounce on any form change;
render order class table → keep deltas → keep heading/inert/consequence → WHEN/WHOSE → sentences →
footer → working; only changed cells flash (`.flash`, 200 ms, `--accent-bg`).

Acceptance:
- [ ] Fresh page: sections 2–4 dimmed with the reason; picking a folder measures, un-dims, prices, recommends (rule 1 for 533 files / 52.71 GB / 1%).
- [ ] Picking `DEEP_ARCHIVE` on Snapshot backup: struck row, WON'T RUN, footer disabled; fix buttons work; `Save it anyway ▸` enables and the POST carries `acknowledge_blocker`.
- [ ] 0% change: heading clause, `.inert`, `+$0.00/mo` on every option, consequence state 3.
- [ ] WHEN tabs re-render both rows and the footer from the last response without a request.
- [ ] Every figure `—` on a 400 from the estimate; the POST never reaches an error page.

### 5.9 Edit job — `/jobs/<name>/edit`

Same template and four sections, pre-filled from `jobs_io.get`; `lbl` `Edit job`; `h1` = the job name.
Locked (rendered as read-only `.defgrid` rows, not disabled inputs): `Name` — `The name is the tag on
every snapshot and the folder name in the bucket. To rename, create a new job and delete this one.`;
`Kind` — a read-only `dd` holding the type's user-facing label followed by its `.term` chip, exactly as
the create screen's radio prints it minus the input: `Snapshot backup <span class="term"
title="jobs.json: type = versioned (restic)">versioned</span>` (`Plain copy · archive`, `File history ·
versioned-files`) — plus the sentence `Set at creation. Changing the kind would leave what is already
stored under a different recovery model. To change it, create a new job.`; `Where it goes` — the mono
identity line. The RECOMMENDED block is not shown.

A blocker the owner already acknowledged does not have to be argued again. When the saved job carries an
`acknowledged` entry (7.8) whose `code` **and** `class` match a blocker the server raises for the
submitted form, that blocker counts as acknowledged: the block renders already in its Acknowledged state
(visible, `Save it anyway ▸` replaced by `Acknowledged — will save anyway`, footer enabled,
`#create-why` reading `Saving with the blocker acknowledged.`), and the form emits the hidden
`acknowledge_blocker=<code>` for it so an untouched save round-trips. Changing the class to a different
blocked class is a NEW blocker — the codes match but the classes do not — and must be acknowledged
again; changing it to an allowed class clears the block and leaves the stale `acknowledged` entry in
place, harmless and dated. Everything else is editable. Diff-aware: the saved value of every
control is embedded (`<script type="application/json" id="saved-job">`); a changed control's row gets a
2px `--accent` left rule and `was: <saved value in the same idiom>` (`was: Instant · STANDARD`, `was: A
little — rare replacements (~1%)`, `was: every day at 03:00`); the footer adds `(was $2.41)` after the
this-job figure when the typical month changed. Assumptions and the measurement load from the saved
job. A class change raises the preserved pair as Heads-up blocks: `Changing the tier affects future
uploads only — files already stored stay in <b>Instant</b> · <span class="mono">STANDARD</span>. Moving
existing data is a separate admin action.` and, when warmer, `This is a warm-up change (<b>Deepest</b> ·
DEEP_ARCHIVE → <b>Instant</b> · STANDARD): existing objects can't move to a warmer tier on their own — they
need a warm-up and a copy, which costs retrieval + requests.` Changing the source on a Plain copy with
`delete it from the copy too` → Heads-up `The copy will be brought in line with the new folder at the
next run; files that only existed in the old folder will be deleted from the copy.` Keeping fewer points
than exist → Heads-up `14 restore points exist now; this keeps at most 7. The extra ones are removed at
the next run and cannot be recovered.` Footer: `Save changes` / `Save and run it now` / `Cancel` (→
`/jobs/<name>`). Server-side, `job_save` takes `type` and `name` from the saved job for an existing name.
On save: 302 to the job page with `Saved appdata.`

Acceptance:
- [ ] Name and kind render locked; posting a different `type` for an existing job is ignored.
- [ ] Changing the class shows `was:` and the heads-up pair; reverting removes them.
- [ ] Assumptions round-trip: edit → save → edit shows the same change rate and bundling.

### 5.10 Setup readiness — `/setup`

Purpose: "Is this install able to back up — and able to restore?" Reading container; eyebrow `Setup`;
h1 `Can this machine back up — and get it back?`; lead `Five real checks. The list always shows what
remains; there is no step counter to lose your place in.` No cards: one checklist table, `<th>` `` ·
`Check` · `What it means` · `Verified` · ``; failing rows sort to the top and mirror into the Board's
needs-you lane. Rows (`readiness.setup_checks()`):

| Check | OK sentence | Failing sentence | Verified | Fix → |
|---|---|---|---|---|
| `Destination reachable` | `Write, read, delete — all OK` | `Could not write to the bucket` + the scrubbed reason in a `.errline`; never probed → `Not probed yet` (`dot-warn`) | `probed 14 Sep 07:40` | `Probe now` (POST `/setup/probe`, an operation record) · `Set up the destination →` |
| `Recovery passphrase` | `Set · not the example` | `Not set — still the shipped example` (BLOCKER) — compared verbatim against `CHANGEME-long-random-passphrase`; `Not needed — no Snapshot backup job` (`dot-ok`, hint) when no versioned job exists | `checked 15 Sep 07:40` | `/setup/keys#RESTIC_PASSWORD` |
| `Old versions protected` | `Bucket versioning on` | `Bucket versioning is off — old versions are not protected`; `unknown` → `Not checkable with this key — confirm in the AWS console (bucket → Properties → Versioning), then mark it here` + `btn btn-sm` `Mark as confirmed` (POST `/setup/versioning-confirmed`) → `Confirmed by hand 15 Sep` | `checked 14 Sep 07:40` | `/setup/destination` |
| `At least one job scheduled` | `2 jobs scheduled` | `No job is scheduled yet` (all paused → `2 jobs, all paused`) | — | `/jobs/new` |
| `Restore ever tested` | `Tested 15 Sep 07:52 · appdata` | `Never` | — | the newest job page's rail |

A sixth informational row when `crontab_stale` (7.3) is true — worded exactly as everywhere else the
flag surfaces (Board band 2 line, 7.3): `Scheduler up to date` / `The schedule file on disk does not
match your jobs; restart the container.` It is informational, not a blocker: `crontab_stale` can only
mean the on-disk crontab differs from `render_crontab(dry_run=True)` (7.3), never that a job failed.
Below the table, three quiet links: `Set up the
destination →`, `Keys & secrets →`, `About this app →`; and the expanded chip sentence in a
`sig-note`: `This GUI has no login. It is meant to be reached only from your LAN; do not expose port
8099 to the internet.` Completing the last item removes the setup block from the Board.

Data: `readiness.setup_checks()` reading `state/_probe.json`, secrets, jobs, `state/<job>.tested.json`,
`status.crontab_stale` (7.7).

Acceptance: [ ] fixture with the shipped passphrase renders the BLOCKER row first and mirrors it on the
Board; [ ] `Mark as confirmed` persists and re-renders; [ ] `/setup` works unprovisioned (no redirect).

### 5.11 Destination — `/setup/destination`

The preserved three-path provisioning picker, restyled onto the reading container. Eyebrow `Setup ·
destination`; h1 `Where backups go`; lead `A bucket this container can write to and nothing else.`
Provisioned: a readiness row `Destination set — bucket <span class="mono">bw-backups</span> in <span
class="mono">us-east-1</span>.` then `Re-provision below to change it.` The three paths as `.step` cards
(the only cards): eyebrow `No admin creds` h3 `Guided-manual` — `We render the exact least-privilege IAM
policy and walk you through creating the bucket in the AWS console. You validate the key you make.`
`Walk me through it →`; eyebrow `Your shell` h3 `Scripted` — `Run setup.sh from a shell that already has
your admin credentials. They never touch this container.` `Show the command →`; eyebrow `Transient
creds` h3 `Automated` — `Paste short-lived admin credentials. We create the bucket with OpenTofu, save
the runtime key, and discard the admin creds.` `Provision for me →`. Sub-routes keep their existing
templates' honest copy and flow (`/setup/destination/manual`, `/manual/render`, `/scripted`,
`/validate`, `/automated`) with: `.cmd` Copy buttons on every block the user is told to copy; scrubbed
stderr in an open `<details>` `What AWS reported` / `What AWS / OpenTofu reported`; validation failures
re-render with both credential fields intact; `Test & Validate` and `Provision now` use the progress
pattern (work bar + disabled button with `Validating…` / `Provisioning…`); every exit lands on `/setup`
with the success flash `Destination set: <bucket> in <region>. Next: the recovery passphrase, then the
first job.` The IAM policy template gains `s3:GetBucketVersioning` in the bucket-scoped statement
(`provisioning/iam-policy.json.tmpl`, and the matching OpenTofu policy) so re-applied keys can probe
versioning; step 5 of the console walkthrough (`provision.py:144-177`) reads `Turn on versioning, and
expire old versions after 180 days` (matching the retention default). All existing strings the tests
assert (`Guided`, `Scripted`, `Automated`, `Destination set`, `transient`, `unraid-backup`,
`value="us-east-1"`, `failed at tofu apply`, `What AWS / OpenTofu reported`, `arn:aws:s3:::acme/appdata/*`,
`setup.sh`) stay; `First-time setup` is retired (the test is updated to assert `Where backups go`).

### 5.12 Keys & secrets — `/setup/keys`

Reading container; eyebrow `Setup · keys & secrets`; h1 `What this machine holds`; lead `Grouped by
what each value is for. Secrets are write-only: leave a field blank to keep what is there.` Four groups
(`.slabel` heads, `.defgrid`-style rows label / input / status):
- **Destination**: `S3_BUCKET`, `AWS_REGION`, `S3_ENDPOINT`, `AWS_ACCESS_KEY_ID` (secret), `AWS_SECRET_ACCESS_KEY` (secret), `RCLONE_TRANSFERS`, `RCLONE_BWLIMIT`.
Every env key name on this screen renders inside `<code class="hint">` — not a bare `span.mono` — so
the one screen that must print `RESTIC_PASSWORD`, `RESTIC_REPOSITORY`, `RCLONE_TRANSFERS` and
`RCLONE_BWLIMIT` puts them where 4.3 allows an implementation name to appear and where the vocabulary
test drops them by construction (10.3).

- **Recovery**: `RESTIC_PASSWORD` shown as `Recovery passphrase <code class="hint">RESTIC_PASSWORD</code>` with the sentence `Without it, no snapshot backup can be read: not by you, not by Amazon, not by anyone. Write it down somewhere that survives this machine.`; `RESTIC_REPOSITORY` as a read-only readout `derived: s3:s3.us-east-1.amazonaws.com/bw-backups/appdata`.
- **Billing** (`<h2 class="slabel" id="billing">Billing</h2>` — the anchor `/cost` links to): `COST_EXPLORER_ACCESS_KEY_ID`, `COST_EXPLORER_SECRET_ACCESS_KEY`, `COST_EXPLORER_SESSION_TOKEN` (secrets; `the read-only billing credential, never the runtime backup key`), `COST_EXPLORER_TAG`. **This is the only place those four are edited** (5.6 band 5 is a read-out). On save, a group that went from all-empty to set flashes `Connected AWS billing.` in addition to `Saved.`; set to all-empty flashes `Disconnected AWS billing.`
- **This machine**: `TZ`, `LOG_LEVEL`, `SOURCE_ROOT`, `APPRISE_URLS`, `NOTIFY_ON_SUCCESS`, `HEALTHCHECK_URL`, `GUI_PORT`, `GUI_ENABLED`, and the new `RESTORE_ROOT`, `RESTORE_ROOT_HOST`, `SOURCE_ROOT_HOST`.
Group membership is `config_io.KEY_GROUPS` NEW; keys in the template but in no group fall into **This
machine**. Secret status is three-state (`config_io.secrets_status_3`): `set` / `not set` / `still the
shipped example` (compared against `CHANGEME` and `CHANGEME-long-random-passphrase`), rendered as a
state token beside the field (`tok-ok` `set`, `tok-failed` `not set`, `tok-overdue` `shipped example`);
placeholder `'••••• (unchanged)'` when set. `File mode: 600` / `not 600 — fix with chmod 600
/config/secrets.env` beside the secrets. Save re-renders with values intact and field-level messages on
error; success flash `Saved.`; when `TZ` changed the flash adds `TZ changes take effect after a
restart.` `/config` → 301.

### 5.13 About — `/setup/about`

Reading container; eyebrow `Setup · about`; h1 `backup-engine`; `.fig.big` `1.4.0` (from
`current_app.config["VERSION"]`) with `.stamp` `built <date>` (NEW env `BUILD_DATE`, default `unknown`).
Then `.slabel` `The tools this app drives`, one row each with an anchor id: `#restic` `restic — makes the
snapshot backups and keeps their history in one encrypted store.`; `#rclone` `rclone — makes the plain
copies, file for file.`; `#vfiles` `the file-history catalog — this app's own index of every version of
every file, one small database per job.`; `#tofu` `OpenTofu — creates the bucket and the key, only if
you chose the automated setup.`; `#supercronic` `supercronic — runs the schedule inside the container.`;
`#tiers` a table of the five tiers (plain phrase · constant · getting it back · minimum stay). Then
`.slabel` `Licences` with the existing `attributions.THIRD_PARTY` rows (name link, role, licence code)
and the project link. Every `.term` and `What these tools are →` link lands on its anchor. `/about` → 301.

### 5.14 Error pages

One template `error.html` on the reading container, registered with `@app.errorhandler` for 400, 403,
404, 405, 409 and 500 in `app/gui/__init__.py` (NEW code): eyebrow `Error <code>`; h1 per code — 404
`There is no such page` (or `There is no job called <name>` / `There is no run <id> for <name>` when
the handler is given a description); 400 `That request could not be understood` (CSRF description →
`That form had expired`, lead `Go back, reload the page, and try again — nothing was changed.`); 403
`That is not allowed from here`; 405 `That page does not take this request`; 409 `<job> is busy`
(description carries the sentence); 500 `Something broke inside backup-engine` with lead `The details
are in the container log. Nothing you typed was saved.`; one `btn btn-primary` `Back to the Board`.
JSON endpoints (`/jobs/browse`, `/jobs/source-size`, every `*.json`, `/log`) answer JSON
`{"error": "<sentence>"}` with the status instead of HTML (the handler checks `request.path.endswith(".json")`
or the endpoint's `json` marker). Section 9 lists every `abort()` site and its replacement.

### 5.15 Flash notices and the shared shell

Flash categories map to signals: `success` → `sig-success` (`Done`, auto-dismiss 7 s), `failure` →
`sig-failure` (persistent until dismissed, carries the verbatim reason), `warning` → `sig-warning`,
`blocker` → `sig-blocker`, `note` → `sig-note`. `base.html` renders `get_flashed_messages(with_categories=true)`
into `.notices` under the topbar; an uncategorised flash renders as `note`. Strings:

| Site | Category | Text |
|---|---|---|
| Run now | `success` | `Run started. Watch it in Activity →` (the redirect lands on the run record, which shows it) |
| Run now while locked (409) | `blocker` | `appdata is busy — a backup or restore is already running. Wait for it to finish.` |
| Job saved | `success` | `Saved appdata.` + `[Run it now]` |
| Job deleted | `success` | `Deleted appdata and its run history on this machine.` |
| Pause / resume | `success` | `Paused appdata — it will not run until you resume it.` / `Resumed appdata — next run Tue 16 Sep 05:00.` |
| Keys saved | `success` | `Saved.` |
| Usage refresh started | `note` | `Refreshing usage — watch it in Activity →` |
| Billing check started | `note` | `Checking the bill — watch it in Activity →` |
| Keys saved, billing group newly set / cleared (5.12 — the only billing write path) | `success` | `Connected AWS billing.` / `Disconnected AWS billing.` |
| No bucket on refresh | `blocker` | `Set the bucket under Setup → Destination before refreshing usage.` |
| Destination validated / provisioned | `success` | `Destination set: <bucket> in <region>. Next: the recovery passphrase, then the first job.` |
| Restore points listed | `success` / `failure` | `Listed 14 restore points.` / `Could not list restore points: <reason>` |
| Warm-up check | `note` | `Checked 20 files: 3 ready, 17 still warming.` |
| Test restore still warming (5.2 rail, `Check now`) | `note` | `Still warming up — ready by ~Wed 17 Sep 04:00.` |
| Scenario levers saved | `success` | `Saved.` |
| Corrupt jobs.json on delete | `failure` | `jobs.json is not valid JSON; fix or remove it before editing jobs.` (on `POST /jobs` the same sentence is a `sig-failure` at the top of the re-rendered form instead, 5.8 §8 — a flash would lose the form) |

Shell (every page): topbar `.topbar` sticky, `--surface`, bottom hairline, full-bleed; `.topbar-in`
`max-width:1240px; min-height:56px; display:flex; gap:1rem; flex-wrap:wrap; padding-block:8px`: brand
`backup<b>-</b>engine` (weight 640, the hyphen in `--accent`) → `/`; `nav.switcher` (`aria-label="Screens"`,
`--bg` ground, 1px `--border`, radius 4px, padding 2px; items .86rem/550 `--muted` `.34rem .7rem`;
current = `--ink` on `--surface-2` + `inset 0 -2px 0 var(--accent)`; `aria-current="page"`) with
Board · Cost · Activity · Setup (Setup is current on every `/setup/*` and `/jobs/new`, `/jobs/*/edit`
count as Board); right cluster `.chip` `LAN only · no auth` (mono .7rem, links to `/setup`) and `.clock`
(mono .74rem `--faint`, tabular): `Tue 15 Sep 2026 · 07:42 UTC · refreshed 12 s ago` on polling pages,
without the last segment elsewhere; the zone is the container's `TZ`. The retired amber "No
authentication" banner is not rendered anywhere. Work bar `#workbar` (section 6.4). Footer `.sitefoot`
(`max-width:1240px; border-top:1px solid var(--border); padding-block:1.5rem 2rem`): the collapsed
`<details>` `How to read this screen — the marks and the six levels` with two `.grid2` columns —
`Where a number came from`: `52.71 GB` (p-measured) `— measured. Something walked the folder or read the
bucket, on a date it will tell you.` · `~11.6 GB` (p-assumed) `— assumed. Dotted, and printed dimmer,
because it rests on a guess you made.` · `$3.98` (p-invoiced) `— invoiced. This is the bill Amazon
actually sent.` · `$1.85` `— projected, no line at all. A projection takes the weakest mark of
everything that fed it, and this one was fed only by measurements.`; `How loudly it is telling you`: `Note — background explanation. No box, no colour, nothing to
do.` · `Advice — take it or leave it. Usually comes with a one-click accept.` · `Warning — will cost you
money or surprise you later. Nothing is stopped.` · `Blocker — stops the button. Names the two ways out
and jumps you to the control.` · `Done — only ever a moment after you press something. Then it clears
itself.` · `Failed — a run that ended badly. Carries a time, the exact error, and a record you can open.
Never shares a shape with Done.`; then `States a job can be in:` followed by the six tokens; then the
hint `Times follow the container's clock (TZ=UTC).` and `<a href="/setup/about">About & licences</a>`.

## 6. Design system

### 6.1 Tokens (`app/gui/static/style.css` `:root`, rewritten)

Keep the preserved set and add what the mockups add:
```
color-scheme: dark;
--bg:#0E1116; --surface:#161A21; --surface-2:#1E232B;
--border:#2A313B; --border-strong:#3A434F;
--ink:#E6EAF0; --muted:#AAB2BF; --faint:#8A93A0;
--accent:#4C8DFF; --accent-hover:#6AA1FF; --accent-ink:#0B0E13; --accent-bg:#16233B;
--ok:#46C08A; --ok-bg:#10241B;
--warn:#E0B15A; --warn-bg:#2A2412; --warn-border:#4A3F22;
--danger:#F0616B; --danger-bg:#2A1417; --danger-border:#5A2A2E;
--ink-strong:#F5F8FC;                     /* verdict line and flashing figures only */
--grid:#20262F;                           /* empty strip cells */
--ok-dim:#2C6B52; --warn-dim:#6B5326; --danger-dim:#7A3239;   /* strip: every cell 8+ from the right */
--prov-measured:rgba(70,192,138,.55); --prov-assumed:rgba(224,177,90,.55); --prov-invoiced:rgba(76,141,255,.55);
--radius:6px; --radius-sm:4px;
--font-sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
--font-mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
--sp-1:.25rem; --sp-2:.5rem; --sp-3:.75rem; --sp-4:1rem; --sp-6:1.5rem; --sp-8:2rem;
--gutter:clamp(16px,2.6vw,24px);
--w-dense:1240px; --w-read:780px; --w-form:960px;
```
No web fonts are loaded (the GUI must work on a LAN without internet); the mono stack falls back to
`ui-monospace`. No new hues, no gradients, no tints on large areas: the only saturated fills are the
strip cells and the primary button. `body{background:var(--bg); color:var(--ink); font:15px/1.55
var(--font-sans); padding-inline:var(--gutter); margin:0}`. Dark-committed: no light theme.

### 6.2 Type scale and weights

`h1 1.6rem/680/−.01em` (`text-wrap:balance`; on the form screen `.h` 20px/1.3/620); `h2 1.05rem/640/−.005em`;
`h3 .92rem/620`; `.slabel .72rem/650 uppercase .1em --faint` (the form screen's `.lbl` is 11px mono
uppercase .12em); `.lead --muted max-width 66ch`; `.hint --faint .84rem`; `.sm 13px/1.5`; mono figures
`.92rem/−.01em` (`.cell2 .v`), sub-values `.78rem --faint`; `.sfig .v 1.15rem`; `.fig.big 1.35rem/500`;
`label.fld .9rem/520 --ink` (`display:block; margin:0 0 var(--sp-2)`; `.confirm label.fld{margin-bottom:.35rem}` — the
restore band's and the confirmation page's field labels, mockup-night-shift.html line 256);
buttons `.86rem/560`, `.btn-sm .78rem`, `.btn-xs` 11px mono uppercase .12em; `code` `.88em` on
`--surface-2` with 1px `--border`, radius 2px, padding `.05em .3em`; `pre` `.8rem` on `--bg`, 1px
`--border`, padding .75rem, radius 4px, `overflow-x:auto`. Weights used app-wide: 400, 500, 520, 550,
560, 600, 620, 640, 650, 680 — exactly as the mockups use them (R6); do not introduce others. (520 is
`label.fld`; 600 is `.tok` (4.7) and `.sig .label` (4.5), mockup lines 118 and 140 — both were in the
spec's own component definitions before they were in this list.)

### 6.3 Spacing, containers, rules, radius

Vertical rhythm on the 4/8/12/16/24/32 px scale only. Containers: `.dense` 1240px (Board, Job page,
Cost, Activity), `.read` 780px (run record, restore confirm, setup pages, about, error), `.form` 960px
(create/edit job); all centred with the body gutter, so no page scrolls horizontally; tables, strips
and the class table sit in their own `overflow-x:auto` wrapper. Bands `.band{border-top:1px solid
var(--border); margin-top:2rem; padding-top:2rem}`; the first has no rule. Cards (`1px var(--border)`,
radius 6px, `--surface`) only for bounded objects. Radius 6px/4px on objects, 0 on all tabular and
strip surfaces and on the state token. No shadows, no hover lifts, no entrance fades. Buttons: `.btn`
1px `--border-strong` on `--surface`, radius 4px, padding `.45rem .8rem`, hover `--surface-2`;
`.btn-primary` `--accent` bg + border, ink `--accent-ink`, hover `--accent-hover`; `.btn-ghost`
transparent `--muted`; `[disabled]{opacity:.45; cursor:not-allowed}`; `.linklike`/`.linkish` accent
text buttons; global `:focus-visible{outline:2px solid var(--accent); outline-offset:2px}`.

On `.form` pages the buttons follow the blend, which is the visual truth for that screen (R6;
mockup-ledger-runbook.html lines 136–150): `.form .btn{border-radius:0; background:transparent;
font-size:14px; padding:.44rem .85rem; border:1px solid var(--border-strong)}`,
`.form .btn:hover{background:var(--surface-2)}`, `.form .btn-ghost{border-color:var(--border)}` — a
visible border, not the shell's borderless ghost, because the blocker's `Use File history instead` /
`Use Instant, cheaper` and the footer's `Create and run it now` are ghosts that must read as buttons —
`.form .btn-primary` as elsewhere but hovering `#6AA1FF` (= `--accent-hover`),
`.form .btn-danger{color:var(--danger); border-color:var(--border)}`, and
`.btn-xs{font-size:11px; font-family:var(--font-mono); text-transform:uppercase; letter-spacing:.12em;
padding:.2rem .5rem}` (`.btn-xs` exists only on this screen, so it needs no `.form` scope).
`.form .btn[disabled]:hover{background:transparent}`. Inputs and
selects mono .88rem on `--bg`, 1px `--border-strong`, radius 4px, `max-width:26rem` (the form screen:
`--surface-2` ground, radius 0, `color-scheme:dark`). `details` 1px `--border`, radius 4px,
`--surface-2`, padding `.5rem .8rem`; `summary` .88rem/550 `--muted`, hover `--ink`.

### 6.4 Motion

1. Change flash `.flash`: `animation:flash .4s ease-out` from `background:var(--accent-bg);
   color:var(--ink-strong)` to transparent, applied to the figure that changed (remove, force reflow
   with `void el.offsetWidth`, add). The form screen uses 200 ms. The old `ul.flash` notice list is
   gone (notices are `.sig`), so the class name is free.
2. Work bar `#workbar`: `position:sticky; top:56px; z-index:29; height:2px; background:var(--surface-2);
   overflow:hidden`, full-bleed; `::after` a 38%-wide `--accent` block animating `translateX(-100%) →
   translateX(320%)` over 1.2 s linear infinite. Shown while any operation is live (the page's poll
   sets it). Under reduced motion: static stripes `repeating-linear-gradient(90deg, var(--accent) 0 8px,
   var(--surface-2) 8px 16px)` and a counting label `working… 12s`.
3. Progress pattern for every long action (Run now, restore, warm-up, download, test restore, Refresh
   usage, Check the bill, Probe, Test & Validate, Provision now): the button disables and relabels
   (`Running…`, `Validating…`, `Provisioning…`, `Refreshing…`), the work bar shows, a live row appears in
   Activity, and the button re-enables only when the record closes.
4. Micro: 180 ms path morph on the projection curve; 120 ms height on expand/collapse; 1.6 s opacity
   pulse on `tok-running::before`.
5. `prefers-reduced-motion`: every animation duration → `.001ms`; the flash becomes `box-shadow:inset
   2px 0 0 var(--accent)` held 2 s; pulse off; curves snap.

### 6.5 Component catalogue

- **Job row + rail**: section 5.1 band 3 (`tr.jobrow`, `.cell2`, `.jobname`, `.jobpath`,
  `.compact-only`), section 5.2 rail (`.rail`, `.rrow`, `.dot`, `.dot-warn`, `.dot-danger`).
- **Run strip** (`.strip[role=img]` of `.cellx`): the job's last 14 **backup** run records (30 on the
  job page) — `kind == "backup"` with outcome `ok`, `failed` or `aborted`, plus a running backup as the
  rightmost cell — oldest → newest left to right, newest rightmost, left-padded with empty cells
  (ruling R2). Restore, download, warm-up and test-restore records are operations, not runs: they are
  never cells and never counted, so a Sunday restore cannot paint a green square or turn
  `1 failed, 13 OK in 14 runs` into `1 failed, 14 OK in 15 runs`. They live in Activity (5.5) and on
  their own record pages. Cell
  9×18px (`.ledger .cellx` 10×20px), gap 2px, radius 0; `.col-strip{width:168px}`. Classes by outcome:
  `ok` `--ok`, `slow` `--warn` (OK but > 3× typical), `fail` `--danger` (failed or aborted), `running`
  `--accent` with the pulse (rightmost only); **every cell at position 8 or more from the right adds
  `dim`** (`--ok-dim`, `--warn-dim`, `--danger-dim`) — positions 8–14 on the Board's 14-cell strip and
  8–30 on the job page's 30-cell ledger, so only the newest 7 are ever saturated and "last week reads
  loudest" holds at both lengths (the mockup dims ledger cells 1–23 and saturates the last 7);
  padding cells `--grid` with `title="no run on record yet"` and no link.
  Each real cell is `<a href="/jobs/<name>/runs/<id>">` with `title` = `Tue 15 Sep 05:00 · OK · 4 m 12 s ·
  restore point a81f3c2e` / `Sun 13 Sep 04:00 · FAILED after 1 h 07 m · AccessDenied` (error class's
  short name: the first token of the error before `:`) / `Fri 28 Aug 05:00 · OK · 8 m 52 s — slowest run on
  record` / `Mon 14 Sep 05:00 · STOPPED after 12 m 03 s · the container restarted` / `Tue 15 Sep 05:00 ·
  RUNNING · 2 m 14 s so far`. `aria-label` per 5.1/5.2. On touch, focusing a cell shows its title in a
  `.hint.mono` line under the strip. Fewer than 14 runs → padding; never ran → 14 padding cells, label
  `No runs on record for <job>.`
- **Signal** (`.sig` + the six classes) — 4.5; **needs-row** — 5.1 band 2.
- **Blocker block / warning block on the form screen** (`.sev.wont`, `.sev.heads`, `.sev.rec`,
  `.sev.note`, `.sev-tab`, `.acts`) — 5.8 §3.3, §3.7.
- **Provenance marks** `.p-measured`, `.p-assumed`, `.p-invoiced`; form-screen `.n`, `.n.assumed`; `.term`;
  `.price-stamp` (+ `.is-live`); `.legend`; `.stamp`; `.fig`, `.fig.big` — 4.6.
- **Verdict** `.verdict` — 5.1 band 1. **Delta line** `.delta` — 4.6. **Figures** `.sfig`, `.grid4`.
- **State token** `.tok` + six classes, plus `.tok-warming` (warm-up waiting; `tok-overdue` colours) — 4.7.
- **Tables**: `.scroll > table` (Board, Cost, Activity), `.defgrid` (`200px minmax(0,1fr)`; one column
  ≤ 620px), `table.classes` (form screen), `.rphead`/`.rp` restore-point rows.
- **Sticky footer** `.formfoot` — 5.8 §6. **Segmented control** `.seg`, **WHOSE grid** `.whose` — 5.8 §3.6.
- **Log viewer** `pre#runlog` (mono .8rem, `--bg`, `max-height:60vh; overflow:auto`, tail-follow while
  live, `Copy` button; Activity's inline `<pre>` is the same style at `max-height:40vh`).
- **Command block** `.cmd` (`pre` + `btn btn-sm copy` `Copy` → `Copied` 1.4 s; `data-copy` holds the
  one-line form). **Error line** `.errline`.
- **Guard + confirm-to-act** `.guard`, `.confirm` — the typed-name confirm exists in exactly two places:
  the restore confirmation page (5.4) and the job page's Delete dialog (5.2). The job page's own
  `.guard` in the Get data back band is the same box with no input — a statement of consequence above a
  button that only navigates (5.2).
- **Card** `.step` (form step, run record, restore confirm, the three destination paths) and `.card`
  (tree, class table), `.levers`.
- **Buttons/inputs** — 6.3; **chip** `.chip`, **clock** `.clock`, **switcher** `nav.switcher` — 5.15.

### 6.6 Responsive rules

- ≤ 1000px: `.jobgrid` → one column; the sticky rail hides and `.rail-inline` (after the status strip) shows.
- ≤ 900px: `.col-strip`, `.col-took` hidden; `.compact-only` shown; `.projgrid` → one column.
- ≤ 640px: form-screen gutters 16px; `.formfoot{height:auto; flex-wrap:wrap; padding-block:8px}` with
  `.figs` on its own row; `.whose` second column 10ch.
- ≤ 620px: `.needs-row` → one column; `.rp`/`.rphead` → `22px minmax(0,1fr)`; `.defgrid` → one column;
  `.verdict{padding:1rem}`; `.step{padding:1rem}`.
- ≤ 560px: the topbar wraps (nav on its own row).
- Phone width (~400px) works: gutter ≥ 16px; no `min-width` wider than the screen outside an
  `overflow-x:auto` wrapper (the 358px ledger and the 640px class table scroll inside theirs).

## 7. Engine work

Facts this rests on (verified on `7d1a181`): `CACHE_DIR=/cache`, `CONFIG_DIR=/config`; `$CACHE_DIR/state`,
`locks`, `logs` are created at boot (`scripts/entrypoint.sh:15`); `acquire_lock NAME` opens fd 9 on
`$CACHE_DIR/locks/<name>.lock` and `flock -n 9`, released by the kernel when every holder exits
(`scripts/lib/common.sh:48-57` — this spec changes that one flag to `-w 5`, 7.1.5); the only run state today is `$CACHE_DIR/state/<job>.json`
(`scripts/backup-job.sh:45-46,105-108`); restic backup runs with `--json` tee'd to
`state/<job>-last.jsonl` (`backup-job.sh:56-58`); rclone runs `copy|sync … --stats-one-line --stats 30s -v`
(`backup-job.sh:82-87`); the crontab is rendered once at boot (`entrypoint.sh:24-35`) and supercronic
(0.2.33) never overlaps the same job; `TZ` from `backup.env` is exported before supercronic and the GUI
start (`scripts/lib/config.sh:15-39`); no `croniter`, no `boto3` (`Dockerfile:32`); `runner.trigger_job`
Popens `bash backup-job.sh <name>` detached (`app/gui/runner.py:23-29`); `/mnt/user` is mounted read-only
at `/backup/media`; bats tests stub `restic`/`rclone`/`python3` and one asserts the python3 stub log is
empty (`tests/bats/backup-job.bats:74-84`) — so the bash runner must not call python for bookkeeping.

File map:

| Path | New/Changed | Purpose |
|---|---|---|
| `scripts/lib/runs.sh` | NEW | Run-record writer (append-only JSONL), per-run log path, rotation, tool-stat parsers. Pure bash + grep/awk/tail/find. |
| `scripts/lib/points.sh` | NEW | `points_refresh JOB TYPE` → `$CACHE_DIR/state/<job>.points.json` after a successful backup and on demand. |
| `scripts/lib/common.sh` | CHANGED | `die` records its message in `_BE_LAST_ERR`; `acquire_lock` waits 5 s (`flock -w 5`) instead of failing instantly. |
| `scripts/backup-job.sh` | CHANGED | Lock first, `runs_start`, per-run log via tee, stats per engine, prune failures recorded, `runs_end` on both paths, points refresh, legacy state file kept. |
| `scripts/restore.sh` | CHANGED | Lock for mutating actions; run records (`restore` / `download` / `thaw` / `test-restore`); `list --json`, `thaw-status`, `test`, `.` scope, `--include`; honours `BE_RUN_ID`; persists warm-up state. |
| `scripts/entrypoint.sh` | CHANGED | `mkdir -p logs/runs restore-test`; `python3 -m app.engine.runs boot`; `supercronic -inotify`; pid file. |
| `app/engine/runs.py` | NEW | Reader/fold/reconcile/backfill API + `boot` CLI. |
| `app/engine/cron.py` | NEW | 5-field cron evaluator (`next_after`, `describe`), TZ helper. |
| `app/engine/errors.py` | NEW | Error-class table. |
| `app/engine/sysop.py` | NEW | Detached system operations (usage refresh, billing check, destination probe) recorded under `_system`. |
| `app/engine/vfiles.py` | CHANGED | `backup()` returns bytes/totals; `restore_all()`; `list --json`; a new `thaw` subcommand (7.5.3 §7). |
| `app/engine/catalog.py` | CHANGED | `paths(conn)`, `current_totals(conn)`. |
| `app/gui/status.py` | NEW | State derivation, next run, overdue, strip, median, streak, verdict, needs-you, `crontab_stale`. |
| `app/gui/points.py` | NEW | Restore-point views per type from caches. |
| `app/gui/ops.py` | NEW | Launches scripts detached with a pre-assigned run id; sync helpers with timeouts; `validate_target`. |
| `app/gui/readiness.py` | NEW | Recovery readiness + setup checks; `WARMUP` table; passphrase three-state. |
| `app/gui/vocab.py` | NEW | The vocabulary dicts (4.3) used by templates and JSON; `FORBIDDEN_TERMS`, `ALLOWED_PHRASES` and `TERM_EXEMPTIONS` for the vocabulary test (10.3). |
| `app/gui/jobs_io.py` | CHANGED | `created_at`, `assumptions`, `measured`, `acknowledged`; `set_enabled()`; `render_crontab()`; delete removes the job's cache files. |
| `app/gui/config_io.py` | CHANGED | `secrets_status_3()`, `KEY_GROUPS`. |
| `app/gui/estimate_io.py` | CHANGED (extended only) | `wizard_estimate` extension, `restore_quote`, `board_cost`, `job_cost_band`, `cost_page`, `delta_verdict`, `provenance_of`. |
| `app/gui/storage_advice.py` | CHANGED | `recommend_type()`; `class_advice` re-voiced into `warnings[]`. |
| `app/gui/routes.py`, `app/gui/__init__.py` | CHANGED | Routes of 4.1; error handlers; flash categories. |
| `app/gui/templates/*`, `static/style.css`, `static/app.js` | REWRITTEN | Section 5–6. |
| `docker-compose.yml`, `backup-engine.xml`, `config/backup.env.example` | CHANGED | `/restore` mount; `RESTORE_ROOT`, `RESTORE_ROOT_HOST`, `SOURCE_ROOT_HOST`, `RUNS_*` knobs. |
| `provisioning/iam-policy.json.tmpl`, `opentofu/*` | CHANGED | `s3:GetBucketVersioning`; 180-day noncurrent expiry. |

### 7.1 Run history

#### 7.1.1 Storage
`$CACHE_DIR/state/<job>.runs.jsonl` — append-only, one JSON object per line, two lines per run (a
`start` and an `end` event with the same `id`), each line ≤ 4096 bytes (error ≤ 1000 chars, command
≤ 500) written by one `printf … >>` so every append is one atomic write. Per-run log
`$CACHE_DIR/logs/runs/<job>/<run_id>.log` (complete stdout+stderr of the run). The global
`$CACHE_DIR/logs/backup-engine.log` keeps receiving `log()` lines unchanged. System operations go to
`$CACHE_DIR/state/_system.runs.jsonl` with `"job": null`, written from Python (`_system` is not a valid
job name).

#### 7.1.2 Run id
`<UTC start compact>-<4 hex>`, regex `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$`, e.g. `20260915T050001Z-3f9a`
(suffix = 2 random bytes from `/dev/urandom`; suffix `0000` is reserved for the backfilled legacy
record). Used verbatim as the URL segment and the log file name. The GUI pre-assigns one via
`BE_RUN_ID`; the writer accepts it only when it matches the regex.

#### 7.1.3 Event schema (v1)
Start (written the moment the lock is held, before any work):
```json
{"v":1,"id":"20260915T050001Z-3f9a","job":"appdata","kind":"backup","event":"start",
 "trigger":"scheduled","started_at":"2026-09-15T05:00:01Z","pid":4132,
 "log":"logs/runs/appdata/20260915T050001Z-3f9a.log",
 "type":"versioned","storage_class":"STANDARD"}
```
End (exactly one, from the success or the failure path):
```json
{"v":1,"id":"20260915T050001Z-3f9a","job":"appdata","kind":"backup","event":"end",
 "outcome":"ok","finished_at":"2026-09-15T05:04:13Z","duration_s":252,"exit_code":0,"error":null,
 "command":"restic -r s3:s3.us-east-1.amazonaws.com/bw-backups/appdata backup /backup/media/appdata_backups --tag appdata",
 "snapshot_id":"a81f3c2e","files_new":2,"files_changed":4,"files_added":6,"bytes_added":228589568,
 "files_total":533,"bytes_total":56594862080}
```
The manga example's end line:
```json
{"v":1,"id":"20260913T040000Z-b21c","job":"manga","kind":"backup","event":"end",
 "outcome":"failed","finished_at":"2026-09-13T05:07:41Z","duration_s":4061,"exit_code":1,
 "error":"AccessDenied: s3:DeleteObjectVersion","phase":"prune","copied":true,
 "command":"rclone copy /backup/media/data/media/comics/mangas s3:bw-backups/media/manga --s3-storage-class DEEP_ARCHIVE",
 "files_added":1204,"bytes_added":3328599040,"files_total":null,"bytes_total":null,"rclone_errors":0}
```
A restore start carries `"kind":"restore"` and `"params":{"point":"a81f3c2e","scope":".","target":"/restore/appdata/2026-09-15","tier":null}`.

Field dictionary (folded record): `v` 1; `id`; `job` (null for system); `kind` ∈ `backup | restore |
download | thaw | test-restore | usage-refresh | billing-check | probe | provision`; `trigger` ∈
`scheduled` (default) | `manual` (GUI) | `once` (`RUN_ONCE`) | `cli` (`BE_TRIGGER=cli`); `started_at`
ISO UTC; `pid` (informational only); `log` (relative to `CACHE_DIR`); `command` (≤ 500 chars, never a
secret); `type`, `storage_class` (backup kind); `params` (restore kinds); `outcome` ∈ `running`
(implied while only a start exists) | `ok` | `failed` | `aborted`; `finished_at`, `duration_s`,
`exit_code`, `error`; `phase` ∈ `copy | prune` (failed backups; which step failed) and `copied`
(true when the copy/snapshot step finished before the prune failed); `snapshot_id` (restic short id,
8 hex); `files_new`, `files_changed` (restic); `files_added`, `bytes_added` (restic `files_new+files_changed`
and `data_added`; rclone final `Transferred:` files and bytes; vfiles `uploaded=`/`bytes=`);
`files_total`, `bytes_total` (restic `total_files_processed`/`total_bytes_processed`; vfiles catalog
totals; rclone null); `rclone_errors`; restore kinds add `files_restored`, `bytes_restored`, `target`,
`objects_requested`, `tier`, `thaw_requested`, `tested_path`, `tested_bytes`; `backfilled` true only on
the legacy-seeded record.

#### 7.1.4 Writer — `scripts/lib/runs.sh`
```bash
#!/usr/bin/env bash
# scripts/lib/runs.sh — per-job run records (append-only JSONL) + per-run logs. Source, don't execute.
RUNS_KEEP_LINES="${RUNS_KEEP_LINES:-400}"    # 2 lines/run -> the newest 200 runs survive a rotation
RUNS_ROTATE_AT="${RUNS_ROTATE_AT:-500}"
RUNS_LOG_KEEP_DAYS="${RUNS_LOG_KEEP_DAYS:-120}"
_RUNS_ID_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
_runs_esc() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
  printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037'; }
_runs_now()   { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
_runs_num()   { case "${1:-}" in ''|*[!0-9]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }
# _runs_str VALUE -> a JSON string when non-empty, the literal null when empty. Use it for EVERY
# optional member; `${V:+"$V"}${V:-null}` is wrong bash (`:-` yields the VALUE when V is set, so a set
# V prints the string twice) and produces an unparseable line.
_runs_str()   { if [ -n "${1:-}" ]; then printf '"%s"' "$(_runs_esc "$1")"; else printf 'null'; fi; }
runs_new_id() { printf '%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"; }
runs_file()   { printf '%s/state/%s.runs.jsonl\n' "$CACHE_DIR" "$1"; }
runs_set_command() { BE_RUN_COMMAND="${1:0:500}"; }
# runs_start JOB KIND [EXTRA_MEMBERS]  — EXTRA_MEMBERS = already-escaped JSON members
runs_start() {
  local job="$1" kind="$2" extra="${3:-}"
  mkdir -p "$CACHE_DIR/state" "$CACHE_DIR/logs/runs/$job"
  # shellcheck disable=SC2254
  case "${BE_RUN_ID:-}" in $_RUNS_ID_GLOB) ;; *) BE_RUN_ID="$(runs_new_id)" ;; esac
  export BE_RUN_ID
  BE_RUN_JOB="$job"; BE_RUN_KIND="$kind"; BE_RUN_START_EPOCH="$(date +%s)"; BE_RUN_COMMAND=""; BE_RUN_ENDED=0
  BE_RUN_STARTED=1          # the ONLY flag that says a start line was written (BE_RUN_ID is pre-set by the GUI)
  BE_RUN_LOG="logs/runs/$job/$BE_RUN_ID.log"; export BE_RUN_LOG
  printf '{"v":1,"id":"%s","job":"%s","kind":"%s","event":"start","trigger":"%s","started_at":"%s","pid":%d,"log":"%s"%s}\n' \
    "$BE_RUN_ID" "$(_runs_esc "$job")" "$kind" "$(_runs_esc "${BE_TRIGGER:-scheduled}")" "$(_runs_now)" "$$" \
    "$BE_RUN_LOG" "${extra:+,$extra}" >>"$(runs_file "$job")"
}
# runs_end OUTCOME EXIT_CODE [ERROR] [EXTRA_MEMBERS]  — idempotent; no-op if runs_start never ran.
# The guard MUST key on BE_RUN_STARTED, never on BE_RUN_ID: `ops.launch` pre-assigns BE_RUN_ID in the
# child's environment, so a run that dies before runs_start (lock collision, job not found, missing
# source) still has an id — keying on the id would append an end line with BE_RUN_JOB/KIND/START_EPOCH
# unset and abort inside the EXIT trap under `set -u`.
runs_end() {
  [ "${BE_RUN_STARTED:-0}" -eq 1 ] && [ "${BE_RUN_ENDED:-0}" -eq 0 ] || return 0
  local outcome="$1" rc="${2:-0}" err="${3:-}" extra="${4:-}" f errj="null"
  f="$(runs_file "$BE_RUN_JOB")"
  [ -n "$err" ] && errj="\"$(_runs_esc "${err:0:1000}")\""
  printf '{"v":1,"id":"%s","job":"%s","kind":"%s","event":"end","outcome":"%s","finished_at":"%s","duration_s":%d,"exit_code":%d,"error":%s,"command":"%s"%s}\n' \
    "$BE_RUN_ID" "$(_runs_esc "$BE_RUN_JOB")" "$BE_RUN_KIND" "$outcome" "$(_runs_now)" \
    "$(( $(date +%s) - BE_RUN_START_EPOCH ))" "$rc" "$errj" "$(_runs_esc "$BE_RUN_COMMAND")" "${extra:+,$extra}" >>"$f"
  BE_RUN_ENDED=1
  _runs_rotate "$f" "$BE_RUN_JOB"
}
_runs_rotate() { local f="$1" job="$2" n; n="$(wc -l <"$f" 2>/dev/null || echo 0)"
  if [ "$n" -gt "$RUNS_ROTATE_AT" ]; then tail -n "$RUNS_KEEP_LINES" "$f" >"$f.tmp.$$" && mv -f "$f.tmp.$$" "$f"; fi
  find "$CACHE_DIR/logs/runs/$job" -name '*.log' -type f -mtime +"$RUNS_LOG_KEEP_DAYS" -delete 2>/dev/null || true; }
# --- generic failure path, shared by backup-job.sh and restore.sh -------------
# runs_fail MSG [RC] [EXTRA] — record-only: no state file, no notify, no healthcheck.
runs_fail() { runs_end failed "${2:-1}" "$1" "${3:-}" || true; }
# runs_exit_trap RC — install as: trap 'runs_exit_trap "$?"' EXIT
runs_exit_trap() { local rc="$1"
  if [ "$rc" -ne 0 ] && [ "${_BE_FAIL_HANDLED:-0}" -eq 0 ]; then runs_fail "${_BE_LAST_ERR:-exited with status $rc}" "$rc"; fi
  [ -n "${_BE_TEE_PID:-}" ] && { exec 1>&- 2>&-; wait "$_BE_TEE_PID" 2>/dev/null || true; }; }
# tool-stat parsers (end-of-run, from files the run wrote)
_restic_summary_field() { grep '"message_type":"summary"' "$1" 2>/dev/null | tail -n1 | grep -o "\"$2\":[0-9]*" | head -n1 | cut -d: -f2; }
_restic_snapshot_id()   { grep '"message_type":"summary"' "$1" 2>/dev/null | tail -n1 | grep -o '"snapshot_id":"[a-f0-9]*"' | head -n1 | cut -d'"' -f4 | cut -c1-8; }
# rclone prints TWO lines that start with "Transferred:" in every stats block — bytes first
# ("Transferred:   3.100 GiB / 3.100 GiB, 100%, 8.912 MiB/s, ETA 0s") then files
# ("Transferred:            1204 / 1204, 100%"). The bytes grep therefore REQUIRES a unit token, and the
# files grep requires the "N / M," shape; without that, `tail -n1` returns the files line for both and
# bytes_added silently becomes the file count.
_rclone_stat_bytes()  { grep -E '^Transferred:[[:space:]]+[0-9.]+ [KMGTPE]?i?B / ' "$1" 2>/dev/null | tail -n1 |
  awk '{n=$2; u=$3; m=1; if(u~/^Ki?B?/)m=1024; else if(u~/^Mi?B?/)m=1024^2; else if(u~/^Gi?B?/)m=1024^3; else if(u~/^Ti?B?/)m=1024^4; else if(u~/^Pi?B?/)m=1024^5; printf "%d", n*m}'; }
_rclone_stat_files()  { grep -E '^Transferred:[[:space:]]+[0-9]+ / [0-9]+,' "$1" 2>/dev/null | tail -n1 | awk '{print $2}'; }
_rclone_stat_errors() { grep -E '^Errors:[[:space:]]+[0-9]+' "$1" 2>/dev/null | tail -n1 | awk '{print $2}'; }
_vfiles_stat() { grep -o "$2=[0-9]*" "$1" 2>/dev/null | tail -n1 | cut -d= -f2; }
_first_error_line() { grep -m1 -E 'AccessDenied|Error|error|denied|failed' "$1" 2>/dev/null | cut -c1-300; }
```

#### 7.1.5 `scripts/backup-job.sh` — insertion points
Source `lib/runs.sh` and `lib/points.sh` after line 10. Rewrite `main()` (lines 14–49) so the lock is
taken BEFORE anything that can fail (a lock collision is the only exit that writes no record):
```bash
main() {
  trap '_usb_exit_trap "$?"' EXIT; trap 'exit 143' TERM; trap 'exit 130' INT
  [ -f "${CONFIG_DIR:-/config}/backup.env" ] && load_config "${CONFIG_DIR:-/config}"
  local jobsio="${JOBS_IO_CMD:-python3 -m app.gui.jobs_io}"
  local def; if ! def="$(CONFIG_DIR="${CONFIG_DIR:-/config}" $jobsio "$JOB")"; then _fail "job '$JOB' not found"; fi
  set -a; eval "$def"; set +a
  acquire_lock "$JOB"                                                                          # (1)
  runs_start "$JOB" backup "\"type\":\"$JOB_TYPE\",\"storage_class\":\"$JOB_STORAGE_CLASS\""   # (2)
  exec > >(tee -a "$CACHE_DIR/$BE_RUN_LOG") 2>&1; _BE_TEE_PID=$!                              # (3)
  version_banner
  validate_source                                                                              # (4)
  local src="$SOURCE_ROOT/$JOB_SOURCE"
  [ -d "$src" ] || _fail "job '$JOB' source '$src' missing"
  : "${RESTIC_CACHE_DIR:=$CACHE_DIR/restic}"
  : "${RESTIC_REPOSITORY:=s3:${S3_ENDPOINT:-s3.${AWS_REGION:-}.amazonaws.com}/${S3_BUCKET:-}/appdata}"
  RUN_STATS=""; COPIED=0
  case "$JOB_TYPE" in
    versioned) _run_versioned "$src" ;; archive) _run_archive "$src" ;; versioned-files) _run_vfiles "$JOB" ;;
    *) _fail "job '$JOB' has unknown type '$JOB_TYPE'" ;;
  esac
  local dur=$(( $(date +%s) - BE_RUN_START_EPOCH ))
  runs_end ok 0 "" "\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS}" || log_warn "could not record run"   # (5)
  _write_state success "" 0                                                                    # (6)
  points_refresh "$JOB" "$JOB_TYPE" || log_warn "restore-point cache refresh failed for '$JOB' (non-fatal)"   # (7)
  log_info "job '$JOB' complete ($JOB_TYPE, ${dur}s)"
  notify success "backup '$JOB' OK" "$JOB_TYPE finished in ${dur}s"; healthcheck success
}
```
Engine hooks. **`backup-job.sh` runs under `set -euo pipefail` (line 3), so every tool call whose exit
status the script wants to inspect must be written `rc=0; <tool> … | tee … || rc=$?` — never
`<tool> … | tee …; rc=${PIPESTATUS[0]}`.** With `pipefail` a failing tool makes the whole pipeline
non-zero and `errexit` leaves the script on that line: the `rc=` assignment never runs, `RUN_STATS` is
never computed, and the EXIT trap records the generic `job exited with status 1` with phase `copy` —
which is exactly the manga example (`phase":"prune"`, `copied":true`) failing to be producible.
`|| rc=$?` both suppresses `errexit` and receives the pipeline's status (verified:
`bash -c 'set -euo pipefail; false | tee /dev/null; echo reached'` prints nothing). The same rule
applies to every hook below and to `restore.sh` (7.5.3), which is also `set -euo pipefail` (line 4).

- `_run_versioned` (51–75): before `restic … backup` add `runs_set_command "restic -r $RESTIC_REPOSITORY
  backup $src --tag $JOB"`; after the `SNAP_ID=` line (58) set `SNAP_ID="$(_restic_snapshot_id "$f")"`,
  `COPIED=1` and build `RUN_STATS` from `_restic_summary_field` (`files_new`, `files_changed`,
  `data_added`, `total_files_processed`, `total_bytes_processed`; `files_added` = new+changed, null only
  when both are absent). The prune step (71–72) becomes
  ```bash
  local plog="$CACHE_DIR/state/$JOB-prune.log" rc=0; : >"$plog"
  restic … forget --prune --tag "$JOB" "${forget_args[@]}" 2>&1 | tee -a "$plog" >/dev/null || rc=$?
  [ "$rc" -eq 0 ] || _fail_phase prune "prune failed: $(_first_error_line "$plog")"
  ```
  **`: >"$plog"` is not optional.** `_first_error_line` is `grep -m1`, so an appended-forever log makes
  every later failure record repeat the FIRST error the file ever saw: the owner fixes the IAM
  permission, the next failure is something else entirely, and the Board still says `AccessDenied:
  s3:DeleteObjectVersion` and still offers `Fix the permission →`. Truncate at the start of every run,
  exactly as `$rlog` already does. The same line is required for `$JOB-prune.log` in `_run_archive` and
  for `$JOB-vfiles.log` in `_run_vfiles`.
  (the cold-tier deferral at line 70 stays a warning).
- `_run_archive` (77–93): drop `--stats-one-line` from `args` (83); replace line 86 with
  ```bash
  runs_set_command "rclone $verb $src s3:$S3_BUCKET/media/$JOB --s3-storage-class $JOB_STORAGE_CLASS"
  local rlog="$CACHE_DIR/state/$JOB-rclone.log" rc=0; : >"$rlog"
  rclone "${args[@]}" 2>&1 | tee -a "$rlog" || rc=$?
  ```
  then compute `RUN_STATS` (`files_added`, `bytes_added`, `files_total:null`, `bytes_total:null`,
  `rclone_errors`) from `$rlog` — the stats block is written even on a partial failure, so the record
  still says how far it got — and only then `[ "$rc" -eq 0 ] || _fail "rclone $verb failed for '$JOB'"`;
  on success `COPIED=1`. The prune call (89–91) becomes
  ```bash
  local plog="$CACHE_DIR/state/$JOB-prune.log" rc=0; : >"$plog"
  python3 -m app.engine.archive_prune "$JOB" --type … --days … --count … 2>&1 | tee -a "$plog" >/dev/null || rc=$?
  [ "$rc" -eq 0 ] || _fail_phase prune "$(_first_error_line "$plog")"
  ```
  `app/engine/archive_prune.py` must print the S3 error's stderr on failure and exit 1. Today it does
  neither: `prune()` lets `S3Error("<tool> failed (exit N): <stderr>")` (`app/engine/s3.py:31-35`)
  propagate out of `main` as a Python traceback, and `_first_error_line`'s
  `grep -m1 -E 'AccessDenied|Error|error|denied|failed'` would match the traceback's own echoed source
  line (`raise S3Error(f"{argv[0]} failed …")`) before it ever reached the message — the failure record
  would quote the app's source code at the owner. So `main` wraps the `prune()` call in
  `except s3.S3Error as e:`, prints **one** line to stderr and `return 1`, with no traceback:
  `AccessDenied: s3:DeleteObjectVersion` when the stderr contains `AccessDenied` and the failing call
  was `delete-object` with `--version-id`; `AccessDenied: s3:ListBucketVersions` when it was
  `list-object-versions`; otherwise the first non-empty line of the tool's stderr, truncated to 300
  characters. (The mapping is call → IAM action name, which is what the owner has to paste into the
  policy.) `tests/engine/test_archive_prune.py` gains a case per branch, asserting the exit code is 1,
  stdout+stderr is a single line, and `Traceback` appears nowhere.
- `_run_vfiles` (95–103): `runs_set_command "python3 -m app.engine.vfiles backup $1"`; run as
  ```bash
  local vlog="$CACHE_DIR/state/$JOB-vfiles.log" rc=0; : >"$vlog"
  python3 -m app.engine.vfiles backup "$1" 2>&1 | tee -a "$vlog" || rc=$?
  ```
  parse `uploaded=`, `bytes=`, `files_total=`, `bytes_total=` from `$vlog` with `_vfiles_stat`, then
  `[ "$rc" -eq 0 ] || _fail "file-history backup failed for '$JOB'"`; `COPIED=1` on success.
- Replace `_record_failure` / `_fail` / `_usb_exit_trap` (105–110):
```bash
_write_state() { local outcome="$1" msg="$2" rc="$3"; mkdir -p "$CACHE_DIR/state"
  printf '{"last_run":"%s","outcome":"%s","type":"%s","snapshot_id":"%s","duration_s":%d,"error":"%s","exit_code":%d,"run_id":"%s","started_at":"%s","finished_at":"%s"}\n' \
    "$(_runs_now)" "$outcome" "${JOB_TYPE:-}" "${SNAP_ID:-}" "$(( $(date +%s) - ${BE_RUN_START_EPOCH:-$(date +%s)} ))" \
    "$(_runs_esc "${msg:0:1000}")" "$rc" "${BE_RUN_ID:-}" "$(date -u -d "@${BE_RUN_START_EPOCH:-$(date +%s)}" '+%Y-%m-%dT%H:%M:%SZ')" "$(_runs_now)" \
    >"$CACHE_DIR/state/$JOB.json"; }
_record_failure() { local msg="$1" rc="${2:-1}" phase="${3:-copy}"; _BE_FAIL_HANDLED=1
  runs_end failed "$rc" "$msg" "\"phase\":\"$phase\",\"copied\":$([ "${COPIED:-0}" -eq 1 ] && echo true || echo false),\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS}" || true
  # Same guard as runs_end, and for the same reason: only a run that actually started owns the legacy
  # state file. A lock collision (BE_RUN_ID pre-set by the GUI, runs_start never reached) must leave
  # state/<job>.json exactly as the last real run wrote it. The `-z JOB_TYPE` arm keeps a pre-lock
  # config failure ("job not found") visible in the legacy file, which is all that exists for it.
  if [ "${BE_RUN_STARTED:-0}" -eq 1 ] || [ -z "${JOB_TYPE:-}" ]; then _write_state failure "$msg" "$rc"; fi
  notify failure "backup '$JOB' FAILED" "$msg"; healthcheck failure; }
_fail()       { _record_failure "$1" 1 copy; die "$1"; }
_fail_phase() { _record_failure "$2" 1 "$1"; die "$2"; }
_usb_exit_trap() { local rc="$1"
  if [ "$rc" -ne 0 ] && [ "$_BE_FAIL_HANDLED" -eq 0 ]; then _record_failure "${_BE_LAST_ERR:-job exited with status $rc}" "$rc"; fi
  [ -n "${_BE_TEE_PID:-}" ] && { exec 1>&- 2>&-; wait "$_BE_TEE_PID" 2>/dev/null || true; }; }
```
`common.sh:die` becomes `die() { _BE_LAST_ERR="$*"; log_error "$*"; exit 1; }`, and
`common.sh:acquire_lock` changes its one flag — `flock -n 9` → **`flock -w 5 9`**. The GUI now probes
that same lock file on every Board and job-page poll (7.2) by taking `LOCK_EX|LOCK_NB` for microseconds;
with `-n`, a scheduled fire that lands inside one of those microseconds loses the race, dies before
`runs_start`, and the backup is skipped with nothing but a WARN in the global log — no record, no Board
signal, exactly the "board that cries wolf" (in reverse) the direction warns about. Five seconds is far
longer than any probe and far shorter than any real run, so only a genuine concurrent holder (a second
backup of the same job, or a run fired while a restore holds the lock) still loses the race. The change
is one flag and one bats case (`acquire_lock` against a lock held for 1 s succeeds; against one held
for 10 s fails). When it does lose, `acquire_lock` calls `die` with `another <job> run is in progress`;
that dies before `runs_start`, so `BE_RUN_STARTED` is 0. `_record_failure`'s `_write_state` guard is
therefore false, `runs_end` is a no-op, and `state/<job>.json` is left exactly as the last real run
wrote it even though the GUI had already pre-assigned `BE_RUN_ID` in the environment. That `die`
message is the WARN — it is the whole of what the collision reports: one line in the global shared log,
no per-job run record, no Board signal, and **no failure notification or healthcheck** (those fire only
from the success path, or from `_record_failure` once a start line exists). There is nothing to record
because the holder owns the run, and the scheduler simply retries at the next tick. (This is a
different lock from restic's own repository lock; a `repository is already locked` error surfaces
*during* a run, after `runs_start`, and is the `repo-locked` failure record in 7.4.) `docker stop` → the TERM trap →
a `failed` record with `exit_code` 143; SIGKILL → a dangling start → `aborted` by reconcile (7.2).

#### 7.1.6 Prune failures are failures
Today `restic forget --prune` and `archive_prune` errors are `|| log_warn` (`backup-job.sh:72,91`), so
the manga example would be `success` with an invisible warning. From now on a prune failure ends the
run as `failed` with `phase:"prune"` and `copied:true` (and `snapshot_id` when one was made): a job
whose keep rule cannot be applied is not doing what it was told, and hiding that is dishonest. The
failure record says so (5.2). The cold-tier prune deferral and `rclone check` differences stay
warnings.

#### 7.1.7 Reader — `app/engine/runs.py`
```python
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$")
BACKUP_KINDS = ("backup",); OP_KINDS = ("restore", "download", "thaw", "test-restore")
SYSTEM_JOB = "_system"
PENDING_WINDOW_S = 600            # a launched run may take this long to write its start line (5.3)

@dataclasses.dataclass
class RunRecord:
    id: str; job: str | None; kind: str; trigger: str
    outcome: str                      # running | ok | failed | aborted
    started_at: datetime              # tz-aware UTC
    finished_at: datetime | None; duration_s: int | None
    exit_code: int | None; error: str | None
    phase: str | None = None; copied: bool | None = None
    snapshot_id: str | None = None
    files_new: int | None = None; files_changed: int | None = None
    files_added: int | None = None; bytes_added: int | None = None
    files_total: int | None = None; bytes_total: int | None = None
    rclone_errors: int | None = None
    files_restored: int | None = None; bytes_restored: int | None = None
    objects_requested: int | None = None; thaw_requested: int | None = None
    log: str | None = None; command: str | None = None
    type: str | None = None; storage_class: str | None = None
    params: dict = dataclasses.field(default_factory=dict)
    pid: int | None = None; backfilled: bool = False

@dataclasses.dataclass
class ReadResult:
    records: list[RunRecord]; corrupt_lines: int; truncated_head: bool

def valid_run_id(s: str) -> bool
def new_run_id() -> str                                   # utcnow compact + secrets.token_hex(2)
def run_id_started_at(run_id: str) -> datetime | None     # the id's own UTC stamp, None if malformed
def is_pending(run_id: str, *, now=None) -> bool          # valid id, no record, stamp within PENDING_WINDOW_S
def runs_path(cache_dir, job) -> Path                     # <cache>/state/<job>.runs.jsonl
def lock_path(cache_dir, job) -> Path                     # <cache>/locks/<job>.lock
def is_locked(cache_dir, job) -> bool
def read_runs(cache_dir, job, *, kinds=None, limit=None, reconcile=True, now=None) -> ReadResult
def read_all(cache_dir, jobs: list[str], *, limit=100, job=None, kind=None, outcome=None) -> list[RunRecord]
def get_run(cache_dir, job, run_id) -> RunRecord | None
def last_completed_backup(cache_dir, job) -> RunRecord | None
def active_run(cache_dir, job) -> RunRecord | None        # outcome running AND is_locked
def reconcile(cache_dir, job, *, now=None, force=False) -> int
def backfill_record(cache_dir, job) -> RunRecord | None
def materialize_backfill(cache_dir, job) -> bool
def append_event(cache_dir, job: str | None, event: dict) -> None
def log_file(cache_dir, rec) -> Path | None
def read_log(cache_dir, rec, *, offset=0, max_bytes=65536) -> tuple[str, int, bool]   # refuses paths outside logs/runs
def median_duration_s(records, *, window=30) -> float | None   # ok backups among the newest `window` backup records; None if < 3
def streak(records) -> int
def all_jobs_with_runs(cache_dir) -> list[str]
def boot(cache_dir, jobs) -> dict
```
Folding: parse every line, skip and count non-JSON / missing `id`/`event` / `v` > 1, never raise;
group by `id`; `start` fills identity, `end` fills outcome (last `end` wins); an `end` without a
`start` (rotation cut) yields `started_at = finished_at − duration_s`, `truncated_head=True`; a `start`
without an `end` is `running`; with `reconcile=True` and any running record, call `reconcile()` then
re-read; sort newest first by (`started_at`, `id`); filter, limit; a missing file returns the backfilled
record when `<job>.json` exists, else `[]`. Timestamps parsed with
`datetime.fromisoformat(s.replace("Z", "+00:00"))`; every datetime in the API is tz-aware UTC.
`python3 -m app.engine.runs boot` reads `CACHE_DIR`, the job list from `jobs_io.load(CONFIG_DIR)` plus
`all_jobs_with_runs`, materializes backfills, reconciles with `force=True`, prints one summary line,
exits 0 always.

#### 7.1.8 Backfill
From `state/<job>.json`: `id = last_run.replace("-","").replace(":","") + "-0000"`; `finished_at =
last_run`; `started_at = finished_at − duration_s` (or `= finished_at`); `outcome` `success → ok`,
anything else → `failed`; `error`, `exit_code`, `snapshot_id` (first 8 chars), `type` copied; `log =
None`; `trigger = "scheduled"`; `backfilled = True`. `materialize_backfill` writes ONE `end` line that
also carries the start fields (the fold accepts it) and only when `<job>.runs.jsonl` is absent.

#### 7.1.9 Rotation and failure modes
Rotation keeps the newest 400 lines (200 runs) once the file passes 500 lines; per-run logs older than
120 days are deleted; all three are env knobs (`RUNS_KEEP_LINES`, `RUNS_ROTATE_AT`, `RUNS_LOG_KEEP_DAYS`,
commented in `backup.env.example`). Disk full → bookkeeping calls are `|| log_warn`, never failing a
backup that succeeded. Two writers are impossible (backups and restore operations both hold the job
lock; Python appends only from `reconcile`, which first proves the lock free). Corrupt lines are
skipped, counted and shown in Tool detail. Deleting a job removes `state/<job>.runs.jsonl`,
`<job>.json`, `<job>-last.jsonl`, `<job>-rclone.log`, `<job>-prune.log`, `<job>-vfiles.log`,
`<job>.points.json`, `<job>.thaw.json`, `<job>.tested.json` and `logs/runs/<job>/` (the confirm dialog says so).

### 7.2 RUNNING detection

A job is RUNNING iff its lock is held, probed from the GUI process:
```python
def is_locked(cache_dir, job) -> bool:
    p = lock_path(cache_dir, job); p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        try: fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN); return False
```
No heartbeat, no PID checks: the kernel releases the lock on any process death. The probe takes an
exclusive lock, so it is itself a (very short) holder — which is why `acquire_lock` must wait rather
than fail instantly (`flock -w 5`, 7.1.5); a 30-second poll that happens to coincide with the 05:00
fire must not be what skips tonight's backup. The probe holds the lock for microseconds and releases it
in the same call; it never blocks a real run for measurable time, and the runner's own `acquire_lock`
remains the single source of truth about who is running. The `running` record
tells WHAT is running (`kind`, `id`, `started_at`, `command`, log); lock held with no running record →
still RUNNING, `active=None`, sub-line `running · started before this app was watching`.
`reconcile()`: for every folded `running` record, if `force` (boot) or `not is_locked()` → append
`{"event":"end","outcome":"aborted","finished_at":<now>,"duration_s":null,"exit_code":null,"error":"The run stopped without reporting — the container was probably restarted or the process was killed."}`.
No age threshold (a 1.78 TB Plain copy legitimately runs for hours); past 3× the median a RUNNING job
only gets the sub-line `taking longer than usual (typical 2 h 12 m)`. Reconcile runs at boot (from
`entrypoint.sh:prepare()`, `|| log_warn`) and lazily from every `read_runs(reconcile=True)`.
Single-flight: `POST /jobs/<name>/run` → 409 with the busy blocker flash when locked; otherwise
`ops.launch` and 302 to the run record; the runner's own `acquire_lock` closes the probe/launch race.
That redirect lands before the child has written anything, so the run record renders its `pending`
state (5.3) until the start line appears — and, if the child lost the lock race and died, tells the
owner so after 30 s instead of 404ing.

### 7.3 OVERDUE · NOT RUN YET · PAUSED · NEXT RUN

State precedence and sort: section 4.7 (ruling R9). Algorithm (`status.overdue`):
```python
GRACE_S = 15 * 60                                              # ruling R9
def overdue(job, last, now, tz) -> tuple[datetime | None, datetime | None]:
    """-> (expected_at, overdue_since); None when not overdue / not computable."""
    if not job.get("enabled", True): return None, None
    ref = (last.finished_at or last.started_at) if last else parse(job.get("created_at"))
    if ref is None: return None, None                          # legacy job, never ran: not computable
    try: expected = cron.next_after(job["schedule"], ref, tz)   # first fire strictly after ref
    except cron.CronError: return None, None
    if now > expected + timedelta(seconds=GRACE_S): return expected, expected + timedelta(seconds=GRACE_S)
    return None, None
```
The reference instant is the last completed run's **finish**, falling back to its start only if a
record somehow has no `finished_at`. A run that was still going when a scheduled tick landed counts as
that tick's run. Using `started_at` instead produces a false Overdue on the most ordinary sequence
there is: the owner presses Run now at 04:50, the 05:00 tick fires into `acquire_lock` and dies with no
record (7.1.5), the manual run finishes at 05:10, `next_after(schedule, 04:50)` is the 05:00 tick that
was skipped — and at 05:15 the Board calls a job Overdue that backed up five minutes ago. That is the
board crying wolf, and it is the one thing this screen cannot afford. `next_after` alone is still all
that is needed: it is immune to schedule edits and is reset correctly by a manual run; `last` includes
failed runs on purpose (a job that failed today is Failed, not also Overdue). Running takes precedence,
so a long run in flight is never Overdue. Sub-lines:
`Overdue — expected Mon 14 Sep 03:00, 1 d 6 h ago · last ran Sun 13 Sep 03:00`; never ran: `Overdue —
never ran · expected Mon 14 Sep 03:00, 1 d 6 h ago · created 12 Sep`. NOT RUN YET = no completed
backup run and not overdue. PAUSED = `enabled == false`.

`app/engine/cron.py` (pure, no dependency):
```python
class CronError(ValueError): ...
@dataclasses.dataclass(frozen=True)
class CronSpec: minutes: frozenset[int]; hours: frozenset[int]; doms: frozenset[int]; months: frozenset[int]; dows: frozenset[int]; dom_star: bool; dow_star: bool; text: str
def parse(expr: str) -> CronSpec
def matches(spec: CronSpec, local_dt: datetime) -> bool
def next_after(expr: str, after: datetime, tz=None) -> datetime      # tz-aware in `tz`, strictly > after
def describe(expr: str) -> str
def local_tz() -> tzinfo                                             # zoneinfo.ZoneInfo(os.environ.get("TZ") or "UTC"), UTC + one WARN if unknown
```
Grammar per field: `*`, `N`, `A-B`, `*/S`, `A-B/S`, `A/S`, comma lists, `JAN..DEC`, `SUN..SAT`, `7` =
Sunday; ranges minute 0–59, hour 0–23, dom 1–31, month 1–12, dow 0–6; anything else (`L`, `W`, `#`,
`?`, `@daily`) → `CronError("schedule uses a cron feature this app can't compute")`. Day semantics =
Vixie: both dom and dow restricted → either matches. `next_after`: naive local wall clock, `t = after
in tz, seconds zeroed, + 1 min`; loop ≤ 100 000: month mismatch → 1st of next month 00:00; day → next
day 00:00; hour → next hour :00; minute → next minute; else found; attach `tz`; skip nonexistent wall
times (spring-forward gap); exhaustion → `CronError` ("never"). `describe`: `Every day at 05:00`,
`Every Sunday at 04:00`, `Mondays and Thursdays at 02:30`, `Every hour at :15`, `Every 6 hours`, `On
the 1st of every month at 03:00`, otherwise `cron 0 4 * * 0` (mono).

Timezone: supercronic and the GUI share the container `TZ`; all stored timestamps are UTC `Z`;
rendering converts to `local_tz()`; the zone is printed once per page. `TZ` edits need a restart
(the Keys & secrets flash says so).

Scheduler agreement: `entrypoint.sh` starts `supercronic -inotify "$CACHE_DIR/crontab" &` and writes
`$!` to `$CACHE_DIR/supercronic.pid`; `jobs_io.render_crontab(config_dir, cache_dir, scripts_dir,
dry_run=False)` writes the crontab in exactly the entrypoint's format (`<schedule> <scripts_dir>/backup-job.sh <name>`
for enabled valid jobs; temp + `os.replace`) after every `upsert`, `delete`, `set_enabled`. **After
every crontab write the GUI sends `SIGUSR2` to the pid in `supercronic.pid`** — unconditionally, not
after a delay and not on a condition: inotify may not fire on Unraid's FUSE share, the signal is
supercronic's own reload trigger, and re-reading an unchanged file costs nothing. The call is wrapped so
a missing pid file, an unparseable pid or `ProcessLookupError` (ESRCH — the container is running without
the scheduler, which is legal: `GUI_ENABLED` and the scheduler are independent) is ignored silently.

`status.crontab_stale` = on-disk crontab ≠ `render_crontab(dry_run=True)`, and that is its **whole**
definition: it detects a failed write or a hand-edited `jobs.json`, nothing else. It cannot mean "the
scheduler has not reloaded", because there is nothing to compare against — supercronic exposes no
reload state, and the GUI has just written the file, so on-disk always equals the render the instant
after a successful save. Any copy that promises otherwise is a promise the app cannot keep, which is why
the Board's warning reads `The schedule file on disk does not match your jobs. Restart the container so
your latest job settings take effect.` (5.1) and the verdict's second sentence reads `The schedule file
on disk does not match your jobs; restart the container.` — both true statements about the file.
`next_run` per job = `cron.next_after(schedule, now)` (paused → `—`; `CronError` → `—` with the note
`can't compute this schedule`); Board `next_scheduled` = min over enabled jobs.

### 7.4 Error classes — `app/engine/errors.py`
```python
@dataclasses.dataclass(frozen=True)
class ErrorClass:
    code: str; short: str
    verdict: str                  # ONE line, Board band 1's second sentence (5.1)
    cause: str                    # the full "WHY THIS HAPPENS" paragraph (job page, run record)
    board: str | None             # needs-you row body; may contain {dow} and {since}
    fix: str; fix_label: str | None; fix_route: str | None; blocker: bool
def classify(error: str | None, exit_code: int | None, outcome: str | None, log_tail: str = "") -> ErrorClass | None
```
Match on `error` first, then the last 200 lines of the run log. `fix_route` may contain `<name>`.

Three fields, three surfaces, because the same failure has to be said at three lengths and the sources
each wrote it differently: `verdict` is the Board verdict's second sentence (one line, beside a
button); `cause` is the `WHY THIS HAPPENS` column on the job page and the run record (a paragraph, with
room to explain); `board` is the needs-you row's body, which is the only one that knows the job's
schedule. `status.needs_you()` renders `<strong>{job} cannot finish a run.</strong> {board}` with
`{dow}` = the schedule's day word from `cron.describe` (`Sunday`, `day`, `Monday and Thursday`) and
`{since}` = the date of the last OK run (`6 September`), or `the first run` when there has never been
one; when `board` is `None` it falls back to `{cause}`, which always stands alone as a sentence.

| code | matches | short | cause | fix | fix label → route | blocker |
|---|---|---|---|---|---|---|
| `iam-version-perms` | `AccessDenied` and (`DeleteObjectVersion`|`ListBucketVersions`|`list-object-versions`|`delete-object`) | `AccessDenied` | `Amazon refused a delete. The key this machine uses is missing the permission that lets a Plain copy clean up old versions, so every run stops at the same point.` | `Two ways out: run ./setup.sh on this machine to re-apply the key policy, or add the permission by hand in the AWS console (IAM → the backup key → this bucket). Either one takes a minute; the next scheduled run then succeeds.` | `Fix the permission →` `/setup/destination` | yes |
| `access-denied` | `AccessDenied`|`403` | `AccessDenied` | `Amazon refused a request — the key this machine uses lacks a permission for it.` | `Re-apply the key policy with ./setup.sh, or compare the key's policy with the one Setup shows.` | `Check the key →` `/setup/destination` | yes |
| `repo-locked` | `repository is already locked`|`unable to create lock` | `store locked` | `Two snapshot backups tried to use the store at the same minute, and the second found it locked.` | `Give the two jobs different minutes. Nothing is damaged; this job runs normally next time.` | `Edit the schedule →` `/jobs/<name>/edit` | no |
| `wrong-passphrase` | `wrong password`|`no key could be found` | `wrong passphrase` | `The recovery passphrase this machine has does not open the snapshot store.` | `Enter the passphrase that was used when the store was created, under Keys & secrets. Without it no snapshot can be read.` | `Set it →` `/setup/keys#RESTIC_PASSWORD` | yes |
| `cold-object` | `InvalidObjectState`|`not valid for the object's storage class` | `not warmed up` | `These files are on a thaw-first tier and have not been warmed up.` | `Warm up first, wait the stated hours, then download again.` | `Warm up →` `/jobs/<name>#restore-band` | no |
| `source-missing` | `source .* missing`|`source root .* not found` | `source missing` | `The folder this job protects was not there when the run started — usually the share was not mounted yet.` | `Check the path mapping for the container and that the share exists; then Run now.` | `Edit the job →` `/jobs/<name>/edit` | yes |
| `no-space` | `no space left on device` | `disk full` | `The cache or restore disk is full.` | `Free space under /cache (restic cache) or the restore folder, then Run now.` | — | no |
| `killed` | exit_code in (137, 143) or outcome `aborted` | `stopped` | `The run was stopped from outside — the container restarted or was stopped.` | `Nothing to fix if you restarted it on purpose; otherwise check the container's log.` | — | no |
| `busy` | `another .* run is in progress` | `busy` | `Started while another operation on this job was still running.` | `Wait for the running operation; this one will run at its next scheduled time.` | — | no |
| `unknown` | anything else | first token before `:` (≤ 24 chars) | `The tool reported an error this app does not recognise.` | `Read the log below; the last lines usually name the reason.` | — | no |

`verdict` and `board` per class (verbatim; `board` is `None` wherever the row would have to guess which
step failed, and the row then prints `cause`):

| code | `verdict` | `board` |
|---|---|---|
| `iam-version-perms` | `Amazon refused a delete — one permission is missing from the key this machine uses.` | `Amazon refused a delete, so every {dow} run stops at the same point. The files are copied, but old versions have not been cleaned up since {since}.` |
| `access-denied` | `Amazon refused a request — one permission is missing from the key this machine uses.` | — |
| `repo-locked` | `Two snapshot backups tried to use the store in the same minute.` | — |
| `wrong-passphrase` | `The recovery passphrase on this machine does not open the snapshot store.` | — |
| `cold-object` | `The files are still cold — Amazon has to warm them up before anything can read them.` | — |
| `source-missing` | `The folder this job protects was not there when the run started.` | — |
| `no-space` | `The disk this job writes to is full.` | — |
| `killed` | `The run was stopped from outside — the container restarted or was stopped.` | — |
| `busy` | `Another operation on this job was still running.` | — |
| `unknown` | `The run stopped with an error; open the record to see it.` | — |

The `unknown` verdict is the same sentence 5.1 prints when `classify` returns `None`, so the Board has
one code path either way. Note that the example's verdict says *refused a delete*: the Night Shift
mockup wrote "refused the upload", which is wrong for this error — `DeleteObjectVersion` is refused
during the clean-up step, after the files are copied (Appendix B, decision 48).

Blocker classes mirror into the Board's needs-you lane; the others are retrospective failure records only.

### 7.5 Restore execution

#### 7.5.1 Where restored files go
Host `/mnt/user/restore` → container `/restore` (rw). `docker-compose.yml`: `- /mnt/user/restore:/restore`.
`backup-engine.xml`: `<Config Name="Restore folder (rw)" Target="/restore" Default="/mnt/user/restore" Mode="rw" Type="Path" Display="always" Required="false">Where restores are written — a new dated folder per restore, never the live share.</Config>`.
`backup.env.example`: `RESTORE_ROOT=/restore`, `RESTORE_ROOT_HOST=/mnt/user/restore`,
`SOURCE_ROOT_HOST=/mnt/user`; Flask config reads all three with those defaults. Mount check:
`os.path.isdir(RESTORE_ROOT) and os.access(RESTORE_ROOT, os.W_OK)`; missing → the band blocker (5.2)
and any POST → 400 with the same text. Default target `${RESTORE_ROOT}/<job>/<YYYY-MM-DD>/` shown as
the host path; non-empty → `-2`, `-3`, …

`ops.validate_target(cfg, job, target_host) -> str` (container path): map the host prefix to the
container prefix; `fsbrowse.safe_resolve(RESTORE_ROOT, rel)` (escape → 400 `The folder must be inside
/mnt/user/restore.`); must not exist or be an empty directory (else 400 `That folder already has files
in it. Pick a new folder so nothing gets overwritten.`); any value starting with `SOURCE_ROOT`,
`SOURCE_ROOT_HOST` or the job's source → 400 `Typing the live source path here is refused — a restore
can never overwrite what it is protecting.`; `confirm` must equal the job name (400 `Type the job name
exactly as shown to start.`); locked → 409.

#### 7.5.2 Execution model — `app/gui/ops.py`
```python
class OpsTimeout(Exception): ...
def launch(cfg, argv: list[str], *, job: str, kind: str, trigger: str = "manual", env_extra: dict | None = None) -> str:
    """Spawn `bash <script> …` detached with BE_RUN_ID pre-assigned; return the run id."""
    run_id = runs.new_run_id()
    env = {**os.environ, "BE_RUN_ID": run_id, "BE_TRIGGER": trigger, **(env_extra or {})}
    Path(cfg["CACHE_DIR"], "logs", "runs", job).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["bash", *argv], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd="/app" if os.path.isdir("/app") else None)
    return run_id
def launch_py(cfg, module: str, args: list[str], *, kind: str) -> str        # python3 -m <module> …, job "_system", same env contract
def run_sync(cfg, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess   # capture_output, text; raises OpsTimeout
```
The child writes its own start line after taking the lock, using `BE_RUN_ID`, and tees its output to
the per-run log; Flask holds no handle, thread or state, so a page close or a GUI restart cannot lose
the operation (a container restart marks it `aborted`; warm-up state survives as a file). `runner.trigger_job`
stays (tests import it); `job_run` uses `ops.launch(cfg, [f"{SCRIPTS_DIR}/backup-job.sh", name], job=name, kind="backup")`.
`run_sync` is used only for `list --json` (≤ 120 s) and `thaw-status` (≤ 90 s).

#### 7.5.3 `scripts/restore.sh` changes
1. Source `lib/runs.sh` and `lib/points.sh` after line 11. Usage (lines 13–22) becomes:
```
restore.sh <job> list [--json]
restore.sh <job> restore <snapshot-id|latest> <target-dir> [--include <path>]   (versioned only)
restore.sh <job> thaw <prefix|.> [--tier Bulk|Standard|Expedited] [--dry-run]   (archive: any prefix; versioned-files: "." only, via vfiles thaw; versioned: whole store)
restore.sh <job> thaw-status <prefix|.>                                            (archive, versioned-files)
restore.sh <job> download <prefix|.> <target-dir>                                  (archive only)
restore.sh <job> <path|.> <target> [--asof TS] [--tier Bulk|Standard|Expedited]   (versioned-files; "." = every file)
restore.sh <job> test                                                              (any type; one file)
```
2. In `main()` after the `set -a; eval; set +a` (line 114): kind from `$1` (`list`/`thaw-status` → no
   lock, no record; `restore`/`download`/`.`/`<path>` → `restore` (`download` for archive `download`);
   `thaw` → `thaw`; `test` → `test-restore`). For record kinds: `acquire_lock "$job"` (die → "another
   <job> run is in progress"), `runs_start "$job" "$kind" "\"params\":{…}"` (values `_runs_esc`'d:
   `point`, `scope`, `target`, `tier`, `asof`, `include`), `exec > >(tee -a "$CACHE_DIR/$BE_RUN_LOG") 2>&1`,
   then the failure path below; every success branch ends `runs_end ok 0 "" "<extra>"`. The
   versioned-files branch no longer `exec`s python (line 123): `python3 -m app.engine.vfiles restore
   "$job" "$@" || _fail "file-history restore failed for '$job'"` so `runs_end` runs.

   **The failure path, spelled out** — "the same traps as backup-job.sh" is not implementable and would
   be wrong if it were: `_fail`, `_record_failure`, `_write_state`, `_fail_phase` and `_usb_exit_trap`
   are defined *inside* `scripts/backup-job.sh`, not in a lib, and they are backup-specific — they
   overwrite `state/<job>.json` (the job's last-BACKUP state, which the Board and the legacy
   `runner.read_state` both read), notify `backup '<job>' FAILED`, and ping the backup healthcheck. A
   failed restore must do none of those three: it must not make the Board say the backup failed, must
   not tell Apprise a backup failed, and must not fail a healthcheck that watches backups. So the
   generic half moves into `scripts/lib/runs.sh` as `runs_fail` and `runs_exit_trap` (7.1.4), and:
   - `backup-job.sh` keeps its own `_record_failure` — `runs_fail` (with the `phase`/`copied`/stats
     extras) **plus** `_write_state`, notify and healthcheck — and keeps installing
     `trap '_usb_exit_trap "$?"' EXIT`, which is now a two-line wrapper that adds nothing but
     `_record_failure`'s richer extras.
   - `restore.sh` installs `trap 'runs_exit_trap "$?"' EXIT` and defines
     `_fail() { _BE_FAIL_HANDLED=1; runs_fail "$1"; notify failure "restore '$job' FAILED" "$1"; die "$1"; }`
     — a run record and a notification, no state file, no healthcheck. `list` and `thaw-status` install
     no trap at all (they take no lock and write no record, so there is nothing to close).
3. `.` = whole scope: `[ "$prefix" = "." ] && prefix=""` so the remote is `s3:$S3_BUCKET/media/$job/`;
   in vfiles `.` dispatches to `restore_all`.
4. `list --json`: versioned → `restic -r … snapshots --tag "$job" --json`; archive → `points_render
   "$job" archive` (`{"kind":"current-copy","folders":[…],"as_of":"…"}` from `rclone lsf --dirs-only`);
   versioned-files → `python3 -m app.engine.vfiles restore "$job" list --json`.
5. `restore` (versioned) gains `--include <path>`; on success counts `find "$target" -type f | wc -l`
   and `du -sb` → extra members `"files_restored":N,"bytes_restored":B,"target":"…"`.
6. `download`: errexit-safe, exactly as the backup hooks (7.1.5) —
   ```bash
   rlog="$CACHE_DIR/state/$job-rclone.log"; rc=0
   rclone copy … -v 2>&1 | tee -a "$rlog" || rc=$?
   ```
   then parse the stats (`files_restored`, `bytes_restored`, `rclone_errors`) from `$rlog`, then
   `[ "$rc" -eq 0 ] || _fail "download reported errors — N files were not ready yet (still warming up)
   or failed"`. Written the other way round (`; rc=${PIPESTATUS[0]}`), `set -euo pipefail` at line 4
   would exit the script on the failing pipeline and the record would lose both the stats and the
   sentence.
7. `thaw`: after issuing, count requests `n` and write `$CACHE_DIR/state/$job.thaw.json` (7.6); extra
   `"objects_requested":n,"tier":"…"`; a warm tier → exit 2 `job is not on a thaw-first tier`; a live
   count line every 500 objects `warmed 12 500 of ~232 021`. Three branches, one per type:
   - **archive**: `rclone lsf -R --files-only s3:$S3_BUCKET/media/$job/$prefix` → `aws s3api
     restore-object` per key. Every key under the prefix is a current file, so warming them all is
     exactly what was asked for.
   - **versioned** (a hand-edited or acknowledged cold store): restore-object for every key under
     `appdata/`, with the log line `Warming up the whole snapshot store (every snapshot job shares it).
     This issues one request per object and can take a long time for a large store.`
   - **versioned-files**: dispatches to a NEW subcommand,
     `python3 -m app.engine.vfiles thaw <job> <scope|.> [--asof TS] [--tier T]`. It must NOT use the
     archive branch: a File history prefix holds `media/<job>/<rel>@<ts>-<uuid>` for **every version
     ever stored** plus `_catalog/catalog.sqlite` (`app/engine/vfiles.py:115`), so `rclone lsf` over it
     would warm — and charge for — the entire history, while the confirm page quoted the current files
     and 7.5.5 promises the catalog decides what gets warmed. The subcommand instead selects, through
     the catalog, the version current at `--asof` (or now) for every path under `<scope>`, calls
     `s3.thaw()` only for rows whose `storage_class` is in `vfiles._COLD_CLASSES`, prints
     `thaw requested: <path>` per file and a final `thaw_requested=N skipped=K`, and exits 0.
     `restore_all()` reuses the same selection, so a warm-up followed by a restore reports the same
     count and never warms a version the restore will not read.
8. `thaw-status <prefix|.>`: sample up to 20 keys (`rclone lsf -R --files-only`), `aws s3api head-object
   … --query Restore --output text` each → `None` / `ongoing-request="true"` / `ongoing-request="false"`;
   print `{"sampled":n,"ready":r,"pending":p,"not_requested":c}`; merged into `thaw.json.last_check`.
9. `test`: versioned → `restic ls --json latest --tag "$job"` first `"type":"file"` → `restic restore
   latest --tag "$job" --include "$path" --target "$CACHE_DIR/restore-test/$job/$BE_RUN_ID"`; archive on
   a warm tier → first key from `rclone lsf -R --files-only … | head -n1` → `rclone copyto` into the
   same scratch dir; archive on a cold tier (ruling R11) → `aws s3api restore-object` for the most
   recently modified key (`rclone lsf -R --files-only --format tp` sorted by time, last) with `Tier=Bulk`,
   write `$CACHE_DIR/state/$job.test-thaw.json` `{"key","requested_at","expected_ready_by","copy_expires_at","run_id"}`,
   exit 0 with `"tested_pending":true`; a later `test` with that file present and `head-object` reporting
   ready → `rclone copyto` the key to `$RESTORE_ROOT/$job/test/<basename>` (kept), then record; a later
   `test` with `head-object` reporting **not** ready → rewrite `test-thaw.json` (same key, same
   `expected_ready_by`) and end `ok` with `"tested_pending":true` again, so pressing `Check now` twice
   costs nothing and changes nothing (5.2 rail);
   versioned-files → first path from `vfiles restore list` → `vfiles restore <job> <path> <dir>`
   (`thaw-requested` → the same pending state). On a completed test: verify the file exists and is
   non-empty, write `$CACHE_DIR/state/$job.tested.json` `{"at","path","bytes","run_id"}`, delete the
   scratch dir (warm case), `runs_end ok` with `"tested_path","tested_bytes"`.

#### 7.5.4 Restore-point cache — `scripts/lib/points.sh` and `app/gui/points.py`
`points_refresh JOB TYPE` runs after every successful backup and on `POST …/restore-points/refresh`,
writing `$CACHE_DIR/state/<job>.points.json` via temp + `mv`: versioned → raw `restic snapshots --tag
<job> --json`; archive → `{"kind":"current-copy","folders":[…],"as_of":"<now>"}`; versioned-files →
nothing (the catalog `$CACHE_DIR/<job>.sqlite` is read directly, `?mode=ro`). `points.py` wraps and
never calls a tool; shapes in 8.5. "Kept as daily/weekly/monthly" is not derivable from restic and is
not shown.

#### 7.5.5 Per-type meaning of "download" (copy for the confirm page's `What` row and the band lead)
Plain copy: `Download copies the current copy of the files under <scope> from Amazon into the folder
you chose. There is no version history here — you get the files as they were on <as_of>.` Snapshot
backup: `Restore writes every file in that restore point into the folder. Then unpack your app archives
from there as you normally would.` File history: `Restore writes every file that existed at that point
in time into the folder — the latest version of each, or the version current at the point you chose.`
Cold File history versions are warmed instead of downloaded; the record reports how many were warmed
vs written and the band offers `Download again later`.

`vfiles.restore_all(job, *, target, asof=None, cache_dir, bucket, rclone_config, thaw="Bulk", runner=subprocess.run) -> dict`:
every path with a live version at `asof` (or now) → `target/<path>`; cold versions get `s3.thaw()`;
prints `restored: <path>` / `thaw requested: <path>` per file and a final `restored=N thaw_requested=M
skipped=K bytes=B`; exit 0 even when `thaw_requested > 0`. `catalog.paths(conn)` (`SELECT DISTINCT path`)
and `catalog.current_totals(conn)` (`SELECT COUNT(*), COALESCE(SUM(size),0) FROM versions WHERE is_current=1`)
are added; `backup()` returns `{"uploaded","deleted","pruned","bytes","files_total","bytes_total"}` and
`_main` prints them on one line with the old three keys first.

#### 7.5.6 Exact invocations issued by the GUI (`bash /app/scripts/restore.sh …` unless noted)

| User action | Type | argv |
|---|---|---|
| Start restore, point P | versioned | `<job> restore <P> <container-target>` |
| Start restore, one folder | versioned | `<job> restore <P> <target> --include <path>` |
| Download, scope S | archive (warm, or cold + ready) | `<job> download <S or .> <target>` |
| Warm up, scope S, speed T | archive | `<job> thaw <S or .> --tier <Bulk|Standard>` (rclone lists the prefix, one restore-object per key) |
| Warm up, speed T | versioned-files | `<job> thaw . --tier <Bulk|Standard>` → `python3 -m app.engine.vfiles thaw <job> . --tier <T>` (catalog-selected current versions only, 7.5.3 §7). `.` is the only scope: File history has no folder scope (5.2) |
| Warm up the store | versioned (cold) | `<job> thaw .` |
| Check warm-up | archive / versioned-files | `<job> thaw-status <S or .>` (sync) |
| Restore everything as of point | versioned-files | `<job> . <target> --asof <epoch> --tier <T>` |
| Restore one file as of point | versioned-files | `<job> <path> <target> --asof <epoch> --tier <T>` |
| Test restore | any | `<job> test` |
| Refresh restore points | any | `<job> list --json` (sync, stdout → cache file) |
| Run now | any | `bash /app/scripts/backup-job.sh <job>` with `BE_TRIGGER=manual` |

### 7.6 Warm-up (thaw) semantics
`readiness.WARMUP`: `DEEP_ARCHIVE` Standard up to 12 h (GUI default), Bulk up to 48 h; `GLACIER`
Expedited 1–5 min, Standard 3–5 h, Bulk 5–12 h; `GLACIER_IR`, `STANDARD_IA`, `STANDARD` none.
`restore.sh thaw` issues `aws s3api restore-object … --restore-request "Days=7,GlacierJobParameters={Tier=<tier>}"`
per object; the warmed copy stays readable 7 days. Persisted `$CACHE_DIR/state/<job>.thaw.json`:
```json
{"requested_at":"2026-09-15T09:12:00Z","run_id":"20260915T091200Z-77ab","tier":"Standard","scope":".",
 "days":7,"expected_ready_by":"2026-09-15T21:12:00Z","copy_expires_at":"2026-09-22T09:12:00Z",
 "objects_requested":232021,"last_check":{"at":"2026-09-16T07:30:00Z","sampled":20,"ready":20,"pending":0,"not_requested":0}}
```
GUI states: none → `Warm up first`; requested and `now < expected_ready_by` → waiting (5.4) with `Check
now`; `last_check.ready == sampled > 0` or `now ≥ expected_ready_by` → `Download now` enabled with the
stragglers note; `now > copy_expires_at` → expired. Cost: `estimate_io.restore_quote(config_dir,
cache_dir, prices, job_name, *, fraction=1.0, tier="Standard", size_gb=None, file_count=None) -> dict`
builds the job's `JobInputs` like `scenario_from_jobs`, overrides size/count with measured values when
given, `replace(scenario, retrieval_tier=tier)`, returns `{"amount": model.restore_cost(j, scn, prices,
fraction), "size_gb", "file_count", "tier", "storage_class", "provenance", "warmup_hours": (lo, hi)|None,
"price_source", "price_date"}` — no new math; the 7-day staging copy is already inside `restore_cost`.

### 7.7 Readiness, setup checks and system operations

#### 7.7.1 `readiness.recovery_summary(cfg, prices, *, now=None) -> dict`
```json
{"generated_at":"2026-09-15T07:42:00Z","tz":"UTC",
 "passphrase":{"state":"set|not_set|shipped_example","checked_at":"…"},
 "aws_key":{"state":"set|not_set|shipped_example"},
 "destination":{"state":"ok|failed|never","probed_at":"…","detail":"Write, read, delete — all OK"},
 "versioning":{"state":"on|off|unknown|confirmed_by_hand","checked_at":"…"},
 "restore_mount":{"state":"ok|missing","path_host":"/mnt/user/restore"},
 "crontab_stale": false,
 "jobs":[{"name":"appdata","type":"versioned","type_label":"Snapshot backup",
   "newest_point":{"id":"a81f3c2e","at":"2026-09-15T05:00:01Z","age_s":9719,"source":"restore-point cache"},
   "points_count":14,"oldest_point":"2026-03-18T05:00:02Z","cover_days":181,
   "size_bytes":56594862080,"size_provenance":"measured","size_measured_at":"2026-09-15T05:00:01Z","file_count":533,
   "storage_class":"STANDARD","tier_label":"Instant","cold":false,"warmup":null,
   "cost_full_restore":{"amount":4.74,"tier":"Bulk","provenance":"measured","price_source":"bundled","price_date":"2026-08-27"},
   "needs":["the recovery passphrase (RESTIC_PASSWORD)","the AWS key","this container with its /config folder (jobs.json)"],
   "tested":{"at":"…","path":"…"},"test_pending":null,"kind_note":null},
  {"name":"manga","type":"archive","type_label":"Plain copy",
   "newest_point":{"id":null,"at":"2026-09-06T06:09:00Z","age_s":0,"source":"last successful run"},
   "points_count":1,"oldest_point":null,"cover_days":0,
   "size_bytes":1957000000000,"size_provenance":"measured","size_measured_at":"2026-09-14T07:40:00Z","file_count":232021,
   "storage_class":"DEEP_ARCHIVE","tier_label":"Thaw first, hours","cold":true,
   "warmup":{"tier":"Standard","hours_hi":12,"alt_tier":"Bulk","alt_hours_hi":48},
   "cost_full_restore":{"amount":168.40,"tier":"Standard","alt_amount":164.10,"alt_tier":"Bulk","provenance":"measured","price_source":"bundled","price_date":"2026-08-27"},
   "needs":["the AWS key","this container with its /config folder"],"tested":null,"test_pending":null,
   "kind_note":"current copy only — no version history"}],
 "totals":{"hours_hi":12,"dollars":173.14,"dollars_provenance":"measured"}}
```
Sources: `passphrase`/`aws_key` from `config_io.secrets_status_3` (empty → `not_set`; equal to the
shipped example → `shipped_example`; else `set`); `destination`, `versioning` from `state/_probe.json`
written by the probe operation; `newest_point`/counts from `points.json` (versioned), OK runs
(versioned-files) or the last OK run (archive); sizes from the newest snapshot summary, `usage.json`,
or the catalog, else the placeholder (`assumed`); `cost_full_restore` from `restore_quote`; `tested`
from `state/<job>.tested.json`, `test_pending` from `state/<job>.test-thaw.json`.

`readiness.setup_checks(cfg) -> list[dict]` returns the five (six) rows of 5.10 with `{code, state:
ok|warn|fail|unknown, sentence, verified_at, fix_label, fix_url}`; failing rows also feed
`status.needs_you()`.

#### 7.7.2 The probe — `POST /setup/probe`
Launches `python3 -m app.engine.sysop probe` (detached, kind `probe`, job `_system`): runs
`provision.validate_runtime_key(bucket, region, key, secret)` with the runtime key from secrets
(list→put→get→delete), then `aws s3api get-bucket-versioning --bucket <bucket>`; writes
`state/_probe.json` `{"destination":{"state","probed_at","detail"},"versioning":{"state","checked_at"}}`
where an `AccessDenied` on `get-bucket-versioning` gives `unknown`. `POST /setup/versioning-confirmed`
sets `versioning.state = "confirmed_by_hand"` with the time. The provisioning validate/automated
success paths write the same `_probe.json` (they just ran the probe) and are also the only writers of
run kind `provision`: each calls `runs.append_event(cache_dir, None, {...,"kind":"provision",
"event":"start"})` immediately before the tool runs and the matching `end` event after it, synchronously
in the request — those routes already block on the tool — so "destination setup" shows up in Activity
with its outcome and its scrubbed output as the record's log (5.5). No other code writes that kind.

#### 7.7.3 System operations — `app/engine/sysop.py`
`python3 -m app.engine.sysop usage-refresh|billing-check|probe`: takes `locks/_system.lock`
(`fcntl`), appends a start event to `_system.runs.jsonl` (via `runs.append_event`, honouring
`BE_RUN_ID`), tees its output to `logs/runs/_system/<id>.log`, runs the existing function
(`usage.collect_usage` + `usage.save_cached`; `billing.monthly_costs` + `billing.forecast` → writes
`$CACHE_DIR/billing.json` `{"fetched_at","months","forecast","tag","error"}`; the probe above),
appends the end event (`ok`/`failed` with the exception's message), exits 0. `estimate_io.billing_view`
reads `billing.json` only (no Cost Explorer call during any render); a cache older than 7 days is
still shown, stamped with its date.

### 7.8 `jobs_io` changes
`validate()` (145–194) passes through and defaults these new keys (everything else unchanged):
- `created_at` (ISO UTC): kept when the input carries a parseable string; `upsert()` sets it to now
  for a new name and copies the existing job's value otherwise; `emit_shell` ignores it.
- `assumptions`: `{"change_rate_pct": float (0|1|10|30), "bundled": bool, "pack_member_gb": float,
  "set_at": iso}` — defaults `0.0`, `false`, `0.05`; a migration on read backfills defaults so edit
  never resets them.
- `measured`: `{"bytes": int, "count": int, "at": iso, "capped": bool}` or absent.
- `acknowledged`: `[{"code": "snapshots_on_cold_class", "class": "DEEP_ARCHIVE", "at": iso}]` or absent.

**How `job_save` builds those two from the POST** — stated because today's `job_save`
(`routes.py:250-268`) builds a fixed dict and `validate` drops unknown keys, so an implementer would
otherwise have to invent it, and because `size_gb` (a 2-decimal GB float) cannot reconstruct a byte
count. The form gains two hidden fields, listed in 8.6: `measured_bytes` (int, copied verbatim from
`/jobs/source-size`'s `bytes`) and `measured_capped` (`1`, or the field absent). Then:

```python
if f.get("measured_bytes"):
    job["measured"] = {"bytes": int(f["measured_bytes"]), "count": int(f.get("file_count") or 0),
                       "at": f.get("measured_at") or now_iso(), "capped": bool(f.get("measured_capped"))}
job["assumptions"] = {"change_rate_pct": float(f.get("change_rate_pct") or 0),
                      "bundled": bool(f.get("packing")), "pack_member_gb": float(f.get("pack_member_gb") or 0.05),
                      "set_at": now_iso()}
```
`measured` is written only when `measured_bytes` is present, so an edit that never re-measures keeps the
saved measurement and its date; `assumptions` is always written (the radios always have a value) and
`set_at` is refreshed on every save, which is what the "set 12 Sep" stamps on the cost view read.
`jobs_io.validate` passes both through after type-checking each member (wrong types → the key is
dropped, never a 500), and `emit_shell` ignores both.
- `set_enabled(config_dir, name, enabled) -> dict` (strict load, flip, write; `JobsFileError` like delete).
- `render_crontab(config_dir, cache_dir, scripts_dir, *, dry_run=False) -> str` (7.3), called after every write.
- `delete()` also removes the job's cache files (7.1.9) when given `cache_dir`.
`emit_shell` and the CLI `--list` are unchanged. `estimate_io._job_inputs` reads `job["assumptions"]`
instead of `_ENGINE_CHANGE`; `scenario_from_jobs` reads `job["measured"]` before the usage cache and
the placeholder, and reports `size_provenance` per job (`measured | observed | assumed`).

### 7.9 `estimate_io` extensions (adapter only, no model change)
- `wizard_estimate(params, config_dir, source_root, prices, *, saved_class=None, other_jobs=None)`
  returns the extended response of 8.6: the existing keys plus `price_kind`, `price_region`, `live_failed`,
  `all_jobs` (via `project(total_scn)`: `months[0].total`, `sum(months[:6])`, `steady_state_monthly`,
  `unbounded`, `typical_floor` = Σ over jobs of (typical if bounded else first bill), `others`),
  `breakdown` additions (`rate_gb_month`, `old_gb`, `old_multiplier`, `effective_object_count`,
  `put_rate_per_1k`, `cold_overhead_monthly`), `classes` (one `estimate()` + `restore_cost()` per
  `model.STORAGE_CLASSES` with the candidate's class swapped; `blocked = engine == "versioned" and cls in
  COLD_CLASSES`; `retrieval_per_run = size_gb × prices.retrieval_per_gb[cls]["Standard"]`), `keep_options`
  (one `estimate()` per preset with the current form values; `delta_monthly = versioning + rotation_monthly`;
  `points`, `reach_days` from `model._tiered_reach_days`/days/count; `allowed`), `recommendation`
  (`storage_advice.recommend_type`), `blockers`, `warnings` (the re-voiced `class_advice`), `schedule`
  (`cron.describe`, `backups_per_month`, `collision`), `first_bill_reason` (`ramp` if versioning > 0 and
  `steady_month > 1`; `upload` if `upload_onetime ≥ 0.05`; `both`; `flat` if |first − typical| < 0.005),
  `provenance` per figure (`provenance_of`).
- `?prices=bundled|live` on `/jobs/estimate.json`, `/cost.json` and `/jobs/<name>/status.json` overrides
  `PRICES_LIVE` for that request: `load_prices(region, cache_dir=…, live=(kind == "live"))`; `price_kind
  = "live"` iff `prices.source.startswith("aws-price-list")`; a live failure returns bundled with
  `live_failed: true`.
- `restore_quote` (7.6); `delta_verdict` (4.6); `board_cost(config_dir, cache_dir, prices)` (the
  Board's band 4 figures from caches); `job_cost_band(job, …)` (5.2's four figures and four rows);
  `cost_page(params, …)` (5.6).
- `provenance_of(inputs) -> str` returns **only** `"assumed"` or `"projected"` (4.6). `"measured"` and
  `"invoiced"` are set directly on observed figures (`size_provenance`, `in_bucket_*`, the invoice) and
  never come out of this function. A figure whose inputs are all measured is `"projected"` and renders
  with no mark at all.
- **`keep_all` at 0% change is bounded, and the adapter is what says so.** `model.project()`
  (`app/estimator/model.py:388`) sets `unbounded = any(retention_type == "keep_all")` regardless of
  churn, which is correct arithmetic in general and wrong at exactly one point: at 0% nothing is ever
  replaced, so there are no old versions to accumulate and the curve is flat. The model is NOT touched
  (2.3). Instead the adapter overrides, after calling it:
  `unbounded = proj.unbounded and change_rate_pct > 0` — applied to `projection`, to `all_jobs`, and to
  each `keep_options[*]`. At 0% the flat curve means `steady_state_monthly` is the typical figure and
  `steady_month` is 1, so the create screen prints a real typical month instead of "still growing" and
  `keep_options["keep_all"].delta_monthly` is `0.0` rather than `null` (8.6). Test:
  `keep_all` + 0% → `unbounded is False`, `typical == first_bill`, `delta_monthly == 0.0`.
- **The legacy response keys do not change shape.** `advice` (the `class_advice` list, each
  `{level, text, …}`) and `guidance` (`type_advice`) are returned byte-identical to today; `warnings[]`
  is **additive** — the same findings re-voiced in the blend's vocabulary for the Heads-up blocks
  (5.8 §3.7) — and `blockers[]` likewise. Nothing reads `advice` in the new UI, but keeping it lets
  `tests/gui/test_jobs_estimate_routes.py` keep asserting on it unchanged (10.2), which is the cheapest
  possible proof that the re-voicing did not change which findings fire.

### 7.10 Migration and compatibility
- `state/<job>.json` keeps its keys plus `run_id`, `started_at`, `finished_at`; `runner.read_state`
  and its tests are unchanged; bats greps for `"outcome":"success"|"failure"` still hold.
- `<job>.runs.jsonl` absent → the reader returns the backfilled record or `[]`; `boot` materializes it.
- Old jobs.json (no `created_at`, no `assumptions`) → valid; never Overdue before its first run;
  assumptions default to 0% / not bundled.
- No `/restore` mount → everything except starting a restore works; the band shows the blocker.
- `RUN_ONCE` and `docker exec …/backup-job.sh <job>` keep working and now produce records.
- Removing `--stats-one-line` changes the global log's rclone stats from one line to five per 30 s.
- `restore.sh` now refuses to run alongside a backup of the same job (before: it ran concurrently).
- Rotation keeps 200 runs; raise the env knobs for more.
- The old routes 301 to the new ones so bookmarks keep working; `/logs` is unchanged.

## 8. Data contracts

Every JSON response carries `generated_at` (ISO UTC) and `tz` (the display zone name). Datetimes are
ISO UTC strings; the client renders absolute/relative forms with the zone. Money is a float in dollars
(two decimals when rendered); sizes are bytes (the client formats). Every money or size figure that
can be marked carries a sibling `<key>_provenance` ∈ `measured | assumed | invoiced | projected`.

### 8.1 `GET /status.json` (Board)
```jsonc
{"generated_at":"…","tz":"UTC","next_scheduled":{"job":"appdata","at":"2026-09-16T05:00:00Z"}|null,
 "verdict":{"state":"failed","job":"manga","h2":"…","sub":"…","button":{"label":"Fix the permission →","href":"/setup/destination"}},
 // level ∈ blocker | warning | advice | note; sort order blockers, warnings, advice, setup gaps (5.1).
 // `advice` is the level of an actionable-but-not-urgent row (a warm-up ready to download); `note`
 // stays reserved for rows with nothing to do, which therefore never carry `fix`.
 "needs_you":[{"level":"blocker","code":"iam-version-perms","job":"manga","text":"…","strong":"manga cannot finish a run.",
               "errline":"AccessDenied: s3:DeleteObjectVersion","hint":"Two ways out: …","when":"2026-09-13T04:00:00Z",
               "record":"/jobs/manga/runs/20260913T040000Z-b21c","fix":{"label":"Fix the permission →","href":"/setup/destination"}}],
 "jobs":[JobStatus…],                       // sorted worst first (4.7)
 "cost":{"in_bucket_bytes":…,"in_bucket_at":"…","prefix_count":2,"invoice":{"month":"2026-08","amount":3.98,"tag_scoped":false}|null,
         "model_monthly":4.54,"model_monthly_provenance":"assumed","model_floor":null,"delta":{"amount":0.56,"pct":14.1,"verdict":"…","short":"+14.1% · model runs high"}|null,
         "why_high_note":"…"|null,
         "per_job":[{"name":"manga","size_bytes":…,"size_provenance":"measured","file_count":232021,"ext":".cbz","old_versions_gb":null,
                     "tier_label":"Thaw first, hours","storage_class":"DEEP_ARCHIVE","monthly":1.85,"monthly_provenance":"projected","settles":null}],
         // `monthly_provenance` ∈ assumed | projected only (4.6): it is a computed figure. `measured`
         // belongs to `size_bytes`/`in_bucket_bytes`, `invoiced` to `invoice.amount`.
         "price":{"kind":"bundled","region":"us-east-1","date":"2026-08-27"}}}
```

### 8.2 `JobStatus` — `GET /jobs/<name>/status.json` and each element of `status.json.jobs`
```jsonc
{"name":"appdata","type":"versioned","type_label":"Snapshot backup","type_line":"…","source":"appdata_backups",
 "source_host":"/mnt/user/appdata_backups","destination_uri":"s3://bw-backups/appdata/","tag":"appdata",
 "state":"ok","label":"OK","enabled":true,"created_at":"…"|null,
 "last":{"id":"…","started_at":"…","finished_at":"…","duration_s":252,"outcome":"ok","snapshot_id":"a81f3c2e","error":null,"exit_code":0,
         "phase":null,"copied":true,"files_changed":6,"bytes_added":228589568,"trigger":"scheduled"}|null,
 "active":{"id":"…","kind":"backup","started_at":"…","elapsed_s":134,"longer_than_usual":false}|null,
 "next_run":"2026-09-16T05:00:00Z"|null,"next_run_note":null|"paused"|"can't compute this schedule",
 "expected_at":null,"overdue_since":null,
 "median_s":248.0|null,"slow_last":false,
 // Every count below, and every strip cell, is over `kind == "backup"` records ONLY (6.5): restores,
 // downloads, warm-ups and test restores are operations, not runs, and must not colour the strip or
 // move the counts. `last` is likewise the last backup run — the job page's "Last run" figure.
 "streak":30,"runs_total":112,"ok_14":14,"failed_14":0,
 "strip":[{"run_id":null,"started_at":null,"outcome":null,"duration_s":null,"slow":false,"dim":true,"snapshot_id":null,"title":"no run on record yet"},
          …14 (or 30 with "ledger":true) cells, oldest first…],
 "bars":[2,4,…],                              // job page only: pixel heights per ledger cell
 "error_class":{"code":"iam-version-perms","short":"AccessDenied","cause":"…","fix":"…","fix_label":"…","fix_route":"/setup/destination","blocker":true}|null,
 "points":{"count":14,"oldest":"…","newest":"…","kind":"snapshots|current-copy|file-history","stale":false}|null,
 "thaw":{…7.6…}|null,
 // test_pending mirrors state/<job>.test-thaw.json verbatim (7.5.3 §9), null when no cold test is
 // waiting; the rail reads expected_ready_by for its `ready by ~…` line (5.2).
 "test_pending":{"key":"media/manga/2026/ch-0412.cbz","requested_at":"2026-09-15T04:02:11Z",
                 "expected_ready_by":"2026-09-17T04:02:11Z","copy_expires_at":"2026-09-24T04:02:11Z",
                 "run_id":"20260915T040211Z-1c8e"}|null,
 "crontab_stale":false,
 "cost":{…job_cost_band (8.7)…}}               // job page only (`?band=cost`)
```

### 8.3 Run record — `GET /jobs/<name>/runs/<run_id>.json` and `…/log`

`GET /activity/<run_id>`, `/activity/<run_id>.json` and `/activity/<run_id>/log` are the same three
contracts for a system record (`job: null`, `_system.runs.jsonl`, 5.5): identical JSON with `"job":
null`, no `snapshot_id`, no `median_s`, and no pending state (a system operation appends its start
event synchronously, so a missing record is a 404, never "starting"). `_system` is not a valid job name,
so `/jobs/_system/runs/<id>` does not exist and must 404 like any unknown job.

`.json`: the `RunRecord` fields of 7.1.7 as JSON (datetimes ISO), plus `live: bool`, `median_s`,
`error_class` (8.2 shape) and `progress: {"done_bytes","total_bytes","eta_s"}|null` parsed from the
log tail (rclone `Transferred:` lines; restic `percent_done` from the `-last.jsonl`). `…/log?offset=N`
→ `text/plain` chunk (≤ 64 KB) with headers `X-Log-Offset: <new offset>` and `X-Log-Eof: 0|1`; 404 JSON
when the record has no log.

Pending (5.3): a well-formed id for an existing job with no record yet and an id timestamp inside
`PENDING_WINDOW_S` (600 s) → the record route renders 200 in the pending state and `.json` returns
`{"generated_at":…,"tz":…,"id":"<run_id>","job":"<name>","outcome":"pending","live":true,"pending_since":"<id timestamp>"}`
with status 200; `…/log` returns an empty body with `X-Log-Offset: 0` and `X-Log-Eof: 0`. 404 (HTML page
for the record route, JSON for `.json`/`log`) only for a malformed id, an unknown job, or a well-formed
id older than the window with no record.

### 8.4 `GET /activity.json`
`{"generated_at","tz","filters":{"job","kind","outcome","limit"},"items":[{"id","job":"appdata"|null,"kind","what":"scheduled run",
"trigger","outcome","label":"OK|Failed|Running|Stopped|Waiting","started_at","finished_at","duration_s","error","record":"/jobs/appdata/runs/…","log":true}]}`
— newest first, merged across jobs and `_system`, `kind` filter accepts `runs` (backup), `restores`
(restore/download/thaw/test-restore), `setup` (usage-refresh/billing-check/probe/provision). `record`
is `/jobs/<job>/runs/<id>` when `job` is a name and **`/activity/<id>` when `job` is null** (8.3): there
is no job route for a system record, and every row in this feed must resolve.

### 8.5 `GET /jobs/<name>/restore-points.json`
Snapshot backup:
```json
{"job":"appdata","type":"versioned","kind":"snapshots","storage_class":"STANDARD","cold":false,
 "fetched_at":"2026-09-15T05:04:20Z","stale":false,
 "points":[{"id":"a81f3c2e","time":"2026-09-15T05:00:01Z","label":"Tue 15 Sep 05:00","size_bytes":56594862080,
            "files_total":533,"files_new":2,"files_changed":4,"bytes_added":228589568,"summary":true}],
 "count":14,"newest":"…","oldest":"2026-03-18T05:00:02Z","size_provenance":"measured","default_point":"a81f3c2e",
 "keep_rule_prose":"last 3, then one a day for 7 days, one a week for 4 weeks, one a month for 6 months",
 "keep_rule_label":"Last 3 · one a day for 7 days · one a week for 4 weeks · one a month for 6 months"}
```
Plain copy: `{"job":"manga","type":"archive","kind":"current-copy","storage_class":"DEEP_ARCHIVE","cold":true,
"as_of":"2026-09-06T06:09:00Z","as_of_source":"last successful run","folders":["2019","2020","2021"],
"size_bytes":1957000000000,"file_count":232021,"size_provenance":"measured","size_measured_at":"…",
"mirror":false,"note":"current copy only — no version history","thaw":{…}|null}`.
File history: `{"job":"photos","type":"versioned-files","kind":"file-history",…,"points":[{"id":"20260915T050001Z-91aa",
"time":"…","asof":1757912590,"label":"Tue 15 Sep 05:03","files_added":12,"bytes_added":40120033}],"count":37,
"file_count":18234,"size_bytes":…,"size_provenance":"measured","size_source":"catalog","thaw":null}` —
no `folders` member: File history's scope is `.` or one file by path (5.2), so a folder list would be a
control with no argv behind it.
Empty cache → `{"job","type","kind","points":[],"count":0,"fetched_at":null,"stale":true}`.

### 8.6 `GET /jobs/estimate.json?<form fields>&prices=<kind>` (create/edit job)
Request = every form field (`csrf` ignored): `type`, `source`, `size_gb`, `file_count`, `measured_at`,
`measured_bytes`, `measured_capped`, `schedule`, `enabled`, `storage_class`, `retention_type`,
`retention_days`, `retention_count`, `keep_last`, `keep_daily`, `keep_weekly`, `keep_monthly`, `mirror`,
`packing`, `pack_member_gb`, `change_rate_pct`, `change_rate_touched`, `name`, `prices`. The same list
is what `POST /jobs` carries. Three of them are new and all three are hidden or implicit:
`measured_bytes` (int, verbatim from `/jobs/source-size`) and `measured_capped` (`1` or absent) exist
because `size_gb` is a rounded GB float and `job["measured"]["bytes"]` must be exact (7.8);
`change_rate_touched` (`1` or absent) is what makes the recommendation rules read the change rate as
unset on a fresh form (5.8 §3.1) — the edit screen always sends `1`. Response (existing keys kept;
`wizard_estimate` 7.9):
```jsonc
{"this_job_monthly":2.69,"new_total_monthly":4.54,"this_job_restore":4.74,
 "price_source":"…","price_date":"2026-08-27","price_kind":"bundled","price_region":"us-east-1","live_failed":false,
 "projection":{"first_bill":1.39,"steady_monthly":2.69,"steady_month":7,"at_6":2.67,"at_12":2.69,"at_24":2.69,"total_6mo":12.19,"unbounded":false},
 "all_jobs":{"first_bill":3.24,"typical":4.54,"typical_floor":4.54,"total_6mo":23.29,"unbounded":false,"others":["manga"]},
 "breakdown":{"billed_gb":52.71,"storage":1.21,"versioning":1.48,"rotation":0.0,"ingest":0.0009,"upload_onetime":0.0027,"lockin_onetime":0.0,
              "change_rate_pct":1.0,"retention_days":null,"rate_gb_month":0.023,"old_gb":64.31,"old_multiplier":1.22,
              "effective_object_count":533,"put_rate_per_1k":0.005,"cold_overhead_monthly":0.0},
 "explain":{…existing…},
 "classes":[{"class":"STANDARD","plain":"Instant","monthly":2.69,"restore_once":4.74,"read_access":"instant","min_days":0,"blocked":false,"reason":null,"retrieval_per_run":0.0},
            {"class":"STANDARD_IA","plain":"Instant, cheaper to keep",…,"min_days":30,"retrieval_per_run":0.53},
            {"class":"GLACIER_IR","plain":"Instant, cold price",…,"min_days":90},
            {"class":"GLACIER","plain":"Cold","read_access":"3–5 h","min_days":90,"blocked":true,"reason":"can't be read by a Snapshot backup",…},
            {"class":"DEEP_ARCHIVE","plain":"Deepest","read_access":"≤48 h","min_days":180,"blocked":true,"reason":"can't be read by a Snapshot backup",…}],
 "keep_options":[{"key":"keep_all","delta_monthly":null,"unbounded":true,"points":null,"reach_days":null,"allowed":true},
                 {"key":"tiered","delta_monthly":1.48,"unbounded":false,"points":20,"reach_days":190,"keep":{"last":3,"daily":7,"weekly":4,"monthly":6},"allowed":true},
                 {"key":"days","delta_monthly":13.31,"unbounded":false,"points":180,"days":180,"allowed":true},
                 {"key":"count","delta_monthly":2.22,"unbounded":false,"points":30,"count":30,"allowed":true}],
 "recommendation":{"type":"versioned","label":"Snapshot backup","rule":"under 10,000 files and some change → Snapshot backup","why":"533 files in 52.71 GB that change a little between runs is the shape this is for."}|null,
 "blockers":[{"code":"snapshots_on_cold_class","class":"DEEP_ARCHIVE","text":"…","fixes":[{"label":"Use File history instead","set":{"type":"versioned-files"}},{"label":"Use Instant, cheaper","set":{"storage_class":"STANDARD_IA"}}],"overridable":true}],
 "warnings":[{"code":"min_stay|per_object|snapshots_on_ia|frequent_on_long_min|class_change|class_change_warmer","text":"…","fix":{"label":"…","set":{…}}|null}],
 "schedule":{"human":"every day at 05:00","cron":"0 5 * * *","backups_per_month":30,"collision":null|{"job":"appdata","at":"05:00","suggest":"05:20","suggest_cron":"20 5 * * *"}},
 "first_bill_reason":"ramp|upload|both|flat","first_bill_reason_text":"because no old versions exist yet",
 "provenance":{"this_job_monthly":"assumed","first_bill":"assumed","total_6mo":"assumed","classes":"assumed","size":"measured"}}
```
Errors: `{"error":"<message>"}` with 400 (the existing `ValueError` path). At 0% change every
`keep_options[*].delta_monthly` is `0.0` (including `keep_all`, whose `unbounded` is `false` and whose
`points`/`reach_days` stay `null`), and `projection.unbounded` / `all_jobs.unbounded` are `false` too —
the adapter's override, not a model change (7.9).

### 8.7 `GET /cost.json?<what-if params>` and `job_cost_band`
`cost.json` = `{**asdict(Estimate), "projection": projection_bundle, "current": current_costs, "billing":
cached billing_view, "delta": {…}, "per_job": [{"name","size_bytes","size_provenance","old_versions_gb",
"storage_class","tier_label","monthly","monthly_provenance","in_bucket_bytes","in_bucket_monthly","delta"}],
"restore": [{"name","size_bytes","warmup":{…}|null,"tier","amount","alt_tier","alt_amount","provenance"}],
"assumptions": {"jobs": {"appdata": {"change_rate_pct","bundled","pack_member_gb","set_at"}}, "scenario":
{"restore_fraction","restores_per_year","retrieval_tier","set_at"}}, "price": {...}}`. `job_cost_band` =
`{"first_bill","first_bill_provenance","at_6","at_6_provenance","steady","steady_provenance","steady_month",
"unbounded","in_bucket_bytes","in_bucket_at","rows":[{"label":"Storing your files","amount_text":"52.71 GB · 533 files",
"amount_provenance":"measured","source":"Walked the folder on 14 Sep 07:40."},…],"delta":{…}|null,"price":{…}}`.

### 8.8 Measurement and browsing (unchanged)
`GET /jobs/browse?path=` → `{"entries":[{"name","path"}]}` (404 JSON on escape);
`GET /jobs/source-size?path=` → `{"bytes","count","capped"?}` (404 JSON on escape).

### 8.9 Template contexts (server-rendered pages)
Board: `status` (8.1 as a dict), `csrf`. Job page: `job`, `status` (8.2 with `ledger:true` and `cost`),
`points` (8.5), `readiness` (the job's entry of 7.7.1 plus the global checks), `restore` (default target,
mount state, quote(s), tier options), `sibling_cold` (name or None), `csrf`. Run record: `job`, `rec`,
`error_class`, `median_s`, `live`, `pending` (5.3). Get data back: `job`, `intent`, `choice`
(point/scope/path/target/tier, each already defaulted), `quote`, `needs`, `blocker` (None or the
impossible-intent signal), `errors` (field → message), `csrf`. Activity: `items`, `filters`, `jobs`. Cost:
`cost` (8.7), `form` (levers), `csrf`. Create/edit: `job` (None or the saved job), `saved_json` (for
diffing), `source_root_host`, `storage_classes`, `other_jobs` (`[{name, schedule, type, enabled}]`),
`defaults` (fresh-form defaults), `errors`, `csrf`. Setup: `checks`, `lan_sentence`. Destination /
keys / about: as today plus `groups`, `secret_status` (three-state), `tools`.

### 8.10 Form GET and POST contracts

One GET form: `GET /jobs/<name>/restore?intent=&point=&scope=&path=&target=&tier=` — the Get data back
band's navigation to the confirmation page (5.2, 5.4). It mutates nothing, needs no CSRF, always renders
200 for an existing job (ill-formed values fall back to defaults in place; an impossible `intent`
renders a BLOCKER instead of the primary button), and 404s only for an unknown job. Every mutating
restore/warm-up/download POST below originates from that confirmation page; the only restore-family POST
that does not is `test-restore` (one file, its price on the button face) and `thaw/check` (starts nothing).

| Route | Fields | Success | Failure |
|---|---|---|---|
| `POST /jobs` | 8.6's fields + `acknowledge_blocker` (repeatable) + `run_now` (`1` from "Create and run it now") + `recalc` (`1` from the `<noscript>` button: re-render with server-computed figures, save nothing) | 302 `/jobs/<name>` (+ launch when `run_now`) | 200 re-render with `errors`; corrupt jobs.json → **200 re-render** with the `sig-failure` sentence and every value intact (5.8 §8), never the error page |
| `POST /jobs/<name>/run` | `csrf` | 302 `/jobs/<name>/runs/<id>` | 409 flash `blocker` |
| `POST /jobs/<name>/pause` · `/resume` | `csrf` | 302 back to the job page with the flash | — |
| `POST /jobs/<name>/delete` | `csrf`, `confirm` | 302 `/` | 400 re-render of the job page with `Type the job name exactly as shown to delete.` |
| `POST /jobs/<name>/restore` (from 5.4 only) | `csrf`, `intent` (`restore`\|`download`), `point` (versioned) / `scope` + `path` (others), `target`, `tier`, `confirm` | 302 run record | 400 re-render of 5.4 / 409 |
| `POST /jobs/<name>/thaw` (from 5.4 only) | `csrf`, `scope`, `tier`, `confirm` | 302 run record | 400 re-render of 5.4 / 409 |
| `POST /jobs/<name>/thaw/check` | `csrf` | 302 job page + `note` flash | 400 when no warm-up is pending |
| `POST /jobs/<name>/test-restore` | `csrf` | 302 run record | 409 |
| `POST /jobs/<name>/test-restore` again, while `test-thaw.json` exists (the rail's `Check now`, 5.2) | `csrf` | 302 run record; still cold → `note` flash `Still warming up — ready by ~<time>.`; ready → the file is downloaded and `tested.json` written | 409 |
| `POST /jobs/<name>/restore-points/refresh` | `csrf` | 302 job page + flash | flash `failure` |
| `POST /jobs/<name>/assumptions` | `csrf`, `change_rate_pct`, `packing`, `pack_member_gb` | 302 `/cost` | 400 |
| `POST /costs/refresh` · `/costs/billing/refresh` · `/setup/probe` | `csrf` | 302 with the `note` flash | `blocker` flash when no bucket |
| `POST /costs/scenario` | `csrf`, `restore_fraction` ∈ `1|0.5|0.1`, `restores_per_year` int ≥ 0, `retrieval_tier` ∈ `Bulk|Standard|Expedited` | 302 `/cost` + `Saved.`; writes `$CONFIG_DIR/cost.json` (5.6 band 3) | 400 with the field message; nothing written |
| `POST /setup/keys` | as today's `/config` | 302 `/setup/keys` + `Saved.` | 200 re-render with field messages |
| `POST /setup/versioning-confirmed` | `csrf` | 302 `/setup` | — |
| `POST /setup/destination/manual/render` · `/validate` · `/automated` | as today | 302 `/setup` with the success flash | 400 re-render (unchanged) |

## 9. Error handling

Every `abort()` in `routes.py` today, and what replaces it:

| Line | Today | Replacement |
|---|---|---|
| 44, 74, 94, 124, 247, 283, 294, 357, 379 | CSRF `abort(400)` | `abort(400, description="csrf")` → the error page `That form had expired` (JSON endpoints: `{"error":"That form had expired — reload and try again."}`) |
| 192 | `abort(404)` on edit of an unknown job | `abort(404, description=f"There is no job called {name}")` → error page |
| 206, 218 | `abort(404)` on browse/size escape | JSON `{"error":"That folder is outside the source root."}` 404 (never echoes the path) |
| 276 | `abort(400)` on any `ValueError` in `job_save` | re-render `job_form.html` with `errors` mapped to fields (5.8 §8), status 200 |
| 286 | `abort(404)` on run of an unknown job | error page `There is no job called <name>` |

New error sites: run record 404s (`There is no run <id> for <name>` — only outside the pending window,
5.3), restore validation 400s
(re-render), busy 409s (`abort(409, description=f"{name} is busy — a backup or restore is already
running. Wait for it to finish.")` for JSON callers, a `blocker` flash + redirect for form callers),
`JobsFileError` → the error page with the plain cause **except on `POST /jobs`**, which re-renders the
form at 200 with the sentence as a `sig-failure` and every value intact (5.8 §8), and on
`POST /jobs/<name>/delete`, which keeps the `failure` flash (5.15). The handlers live in `app/gui/__init__.py`
(`register_error_handlers(app)`): for each of 400/403/404/405/409/500 render `error.html` (5.14)
unless the request path ends in `.json` or hits `/jobs/browse`, `/jobs/source-size`, `/log`, in
which case return `{"error": <sentence>}`. 500 also logs the traceback (`app.logger.exception`).
Provisioning failures keep their existing 400 re-render with `error`/`error_detail`.

Flash categories and visuals: 5.15. The `.notices` region renders under the topbar on every page;
`success` auto-dismisses after 7 s (JS) and has a `×` dismiss; `failure`/`blocker` persist until
dismissed; the dismiss is client-side only.

## 10. Testing

### 10.1 What exists (keep green)
`tests/estimator/` (untouched), `tests/gui/` (`test_app.py`, `test_about_routes.py`, `test_config_io.py`,
`test_config_routes.py`, `test_costs_routes.py`, `test_dirsize.py`, `test_estimate_io.py`,
`test_estimate_routes.py`, `test_fsbrowse.py`, `test_jobs_estimate_routes.py`, `test_jobs_io.py`,
`test_jobs_routes.py`, `test_provision.py`, `test_provision_routes.py`, `test_runner.py`,
`test_storage_advice.py`), `tests/engine/` (`test_archive_prune.py`, `test_catalog.py`,
`test_iam_policy.py`, `test_s3.py`, `test_vfiles_*.py`), `tests/bats/*.bats`, `tests/integration/*.bats`.
Conventions: `tests/gui/conftest.py` provides `template_path` and `dirs` (`{"config","cache"}` with
`cache/state` and `cache/logs`); each module builds `create_app({...,"PRICES_LIVE": False,"SECRET_KEY":"test","TESTING": True})`;
CSRF via `client.session_transaction()`; seed `jobs.json` / `backup.env` / secrets directly; nothing
shells out (monkeypatch `runner`, `provision`, `usage`, `billing`, and now `ops.launch`, `ops.run_sync`).

### 10.2 Existing tests that must be updated (not deleted)
- `test_jobs_routes.py`: `Getting started` → the Board empty verdict `Nothing is being backed up yet.`;
  `Versioned files` / `"Archive" not in body` → `File history` / `Plain copy`; `id="job-advice"`,
  `id="job-cost-restore"`, `id="job-cost-sizing"`, `Calculating Directory Size and Info`,
  `id="source-info"` → the new ids (`#class-blocker`, `#keep-consequence`, `#foot-figs`, `#pricestamp`,
  the measurement line); `data-when-*` assertions stay; `class="sched-builder"` stays; tiered radio and
  keep prefill assertions stay.
- `test_jobs_estimate_routes.py:325-338`: `name="change_rate_pct"` stays (now radios); `How much
  changes each backup` → `How much of it changes each backup?`; `id="job-cost-first"`,
  `id="job-cost-breakdown"` → `#fig-job`, `#new-working`; `<option value="0" … selected` → `id="ch-1"
  … checked` (the fresh default is ~1%).
- `test_estimate_routes.py`: `/estimate` now 301 → `/cost`; `class-panel` → `table.classes` is on the
  form only, so this test asserts `id="cost-timeline"`, `id="month"` (scrubber), `Refresh usage`,
  `How this is calculated`; `Restoring is retrieval + egress` → `Getting it back is warm-up plus download
  out of Amazon.`; `/estimate` in nav → `/cost`.
- `test_costs_routes.py` — four groups, and the first two are semantic changes, not renames:
  - **100–185, the `/costs/refresh` tests** (`test_refresh_calls_collect_usage_and_saves_cache`,
    `test_refresh_includes_versioned_files_job_prefix`, the two not-500 guards,
    `test_refresh_without_bucket_flashes_and_does_not_call_collect_usage`) assert that the route calls
    `usage.collect_usage` **synchronously** and that `usage.load_cached` then holds the result. 5.6
    makes it a detached `ops.launch_py(… "usage-refresh")` + `note` flash, so they are rewritten:
    monkeypatch `ops.launch_py`, assert it was called with `usage-refresh`, assert the response is 302
    with the flash, and assert `usage.collect_usage` was **not** called in the request. The
    `collect_usage` / `save_cached` / media-prefix assertions move verbatim to `test_sysop.py`, where
    they belong now; the no-bucket case keeps its `blocker` flash and its "launched nothing" assertion.
  - **226–258, the `billing_view` tests** assert it calls `billing.monthly_costs` / `billing.forecast`
    live and returns `{"connected": True, "error": …}`. 7.7.3 makes it cache-only, so they seed
    `$CACHE_DIR/billing.json` and assert the parsed view; the `BillingError` case moves to
    `test_sysop.py` and asserts `billing.json`'s `error` member instead.
  - **261–267**: `current spend` → `In the bucket now`.
  - Anything asserting the Cost Explorer form on `/cost` (`connect aws billing`, the `COST_EXPLORER_*`
    inputs, `POST /costs/billing`) moves to `test_config_routes.py` against `/setup/keys#billing`,
    which is now the only editing surface (5.6 band 5, 5.12); `/cost` is asserted to contain no
    `COST_EXPLORER_` string at all.
- `test_jobs_estimate_routes.py`, the advice tests — they stay green **unchanged** and are the guard
  that the re-voicing did not change which findings fire: 7.9 requires `advice[]` and `guidance` to be
  returned byte-identical, with `warnings[]` additive, so 125–140 (`"bundle" in advice`), 271
  (`level == "danger"`) and 279 (`advice == []`) need no edit. One test in that file must be **inverted**,
  for a different reason: `test_jobs_estimate_no_bundle_warning_for_large_objects` (≈141–147) asserts
  that 1824 GB over 232,021 files — 8.05 MB average — raises no bundling advice. Appendix B #43 raises
  that threshold from 1 MB to 10 MB precisely so the manga example does fire, so the test becomes
  `test_jobs_estimate_bundle_warning_at_manga_shape` asserting `"bundle" in advice`, and a new
  companion asserts silence above 10 MB average (e.g. `size_gb=1824, file_count=100000`, 18.7 MB).
- `test_app.py`: `/` → 302 `/setup` when unprovisioned and 200 when provisioned; the `no authentication`
  assertion moves to `/setup` (`This GUI has no login`).
- `test_about_routes.py`: `/about` → 301 `/setup/about`; the footer link asserts `/setup/about` on `/`.
- `test_provision_routes.py`: paths under `/setup/destination`; `First-time setup` → `Where backups go`;
  every other asserted string stays.
- `test_config_routes.py`: `/config` → 301; the form is at `/setup/keys`; three-state status tokens.
- `tests/bats/backup-job.bats`: rclone argv no longer contains `--stats-one-line`; the missing-source
  case asserts both the legacy `"outcome":"failure"` and a `runs.jsonl` `"outcome":"failed"` line.

### 10.3 New tests
- `tests/gui/test_vocabulary.py`: render every page with the example fixture (jobs.json + state
  files + `runs.jsonl` + `points.json` + `usage.json` + `billing.json`) and assert no forbidden term
  survives. The rules are exact, because the test must be able to go green against the copy this
  document mandates:
  - **What is searched**: the visible text only. Parse the HTML, drop the whole subtree of `<code>`,
    `<pre>`, `<script>`, `<style>`, `.term`, `.cmd`, `.errline`, `details.tooldetail`, and any
    `span.mono` whose text is an env key name (`^[A-Z0-9_]+$`), then concatenate the remaining text
    nodes. Attributes are never searched — `name="retention_type"`, `value="versioned-files"`,
    `href="/setup/about#restic"` and `data-when-type` are markup, not language.
  - **How it matches**: case-sensitive, whole word — `re.search(rf"\b{re.escape(term)}\b", text)`.
    Case sensitivity is what lets `RESTIC_PASSWORD` stand as an env key while `restic` stays banned as
    a word.
  - **`vocab.FORBIDDEN_TERMS`**: `restic`, `rclone`, `repository`, `snapshot_id`, `egress`, `ingest`,
    `thawing`, `thawed`, `restore-request`, `churn`, `retention`, `prune`, `catalog`, `vfiles`,
    `versioned-files`, `Bulk copy`, `Mirror`, `OpenTofu`, `supercronic`, `steady state`, `steady-state`.
    Bare `thaw` is NOT on the list and must not be added: the tier name `Thaw first, hours ·
    DEEP_ARCHIVE` and its lowercase form `thaw-first tier` are the vocabulary (4.3) and appear on the
    Board, the job page, the restore pages, the cost page and Setup. `vocab.ALLOWED_PHRASES =
    ("Thaw first", "thaw-first")` is asserted positively — at least one of them renders on the manga
    job page — so a later "cleanup" cannot quietly delete the tier name.
  - **Per-page exemptions** (`vocab.TERM_EXEMPTIONS`, keyed by URL prefix — the only ones; every other
    page is checked against the full list):

    | Page | Exempt terms | Why |
    |---|---|---|
    | `/setup/about` | all of them | it is the glossary (5.13) |
    | `/setup/destination`, `/setup/destination/*` | `OpenTofu` | the preserved copy names the tool that creates the bucket: "We create the bucket with OpenTofu", "What AWS / OpenTofu reported" (5.11), asserted by existing tests |
    | `/setup/keys` | `restic`, `repository` | the Recovery group prints the env key names `RESTIC_PASSWORD` and `RESTIC_REPOSITORY` beside the plain names (5.12); the case-sensitive rule already covers them, the exemption makes it intentional |

  Mono law: parse each page with
  `html.parser`; every text node matching `^\$?\d[\d,]*(\.\d+)?( ?(GB|MB|TB|KB|%|s|m|h|d|files))?$` or
  `^\d{2}:\d{2}$` must have **an ancestor at any depth** with a class in `{mono, n, v, s, t, sub, scrub,
  num, fig, stamp, price-stamp, errline, cmd, clock, chip, tok, delta, d, id, m, k-delta, figs, classes,
  ledger-axis}`; the state token, clock and chip are mono. The list is the set of classes this document's
  own markup actually uses for mono numbers, and it must stay in step with it: `.s` carries sub-values
  (`232,021 files`, `5 files`, `/mo`), `.t` the rail stamps, `.sub` the verdict line, `.scrub` the month
  readout, `.num` the Board's numeric columns, and `classes` is on `table.classes` itself — its cells
  (`$2.69`, `30 d`, `180 d`) are mono by CSS with no class of their own, which the any-depth ancestor
  rule covers. Written without those, the test fails on the Board, the job page and the create screen,
  i.e. on correct markup, which is the one thing a lint must never do.
- `tests/engine/test_runs.py`: fold rules; `is_locked`; `reconcile` (lock free → one aborted line,
  idempotent; lock held → untouched; `force`); `materialize_backfill` once; `read_log` offsets and
  path refusal; `median_duration_s` (window 30, < 3 → None), `streak`; `boot` never raises on a
  corrupt file; `read_all` merges `_system`.
- `tests/engine/test_cron.py`: the vectors — `0 3 * * *` after `02:59Z` → `03:00`, after `03:00:00` →
  next day; `0 4 * * 0` after Tuesday → Sunday 04:00; `*/15 * * * *`; `30 2 1 * *`; `0 0 * * 1,4`;
  `0 0 15 * 1` (Vixie OR); names `0 0 * JAN MON`; `7` = Sunday; invalid (`L`, 6 fields, `61`) →
  `CronError`; `0 0 31 2 *` → `CronError`; DST `30 2 * * *` in `America/New_York` after 2026-03-08 01:00
  → 03:30 local; `describe()` phrases.
- `tests/engine/test_errors.py`: one case per class; `AccessDenied: s3:DeleteObjectVersion` →
  `iam-version-perms`, `blocker=True`.
- `tests/engine/test_vfiles_restore_all.py`: two paths + a tombstone; `asof` picks the older version;
  cold → `thaw` argv, `thaw_requested == 1`; the totals line; `list --json`. Plus the new `vfiles thaw`
  subcommand (7.5.3 §7): a job holding **two versions of one path**, both cold, issues **exactly one**
  `restore-object` — the current one — and prints `thaw_requested=1`; a warm version is counted in
  `skipped=` and issues nothing; `thaw .` and a following `restore_all` report the same
  `thaw_requested` count, because both select through the catalog. (The point of the test is that the
  rclone-prefix approach would have issued two and billed for the history.)
- `tests/gui/test_status.py` (fake clock; runs seeded with `runs.append_event`): the precedence matrix
  (lock held > paused > overdue > failed > ok > not run yet — note Overdue outranks Failed only in
  precedence, not in sort; assert both orders of 4.7); daily job last run 3 days ago → `expected_at` the
  next fire, overdue 15 min later; 14 minutes after a missed fire → not overdue; a running lock during
  a missed fire → Running, not Overdue; paused never overdue; never-ran with `created_at` 3 days ago →
  Overdue; without → Not run yet; unparsable cron → `next_run None` with the note; sort order;
  `crontab_stale`; strip padding, dim by position, slow rule, aborted as failed, titles. Two vectors
  this spec's review added, both of which a naive implementation gets wrong:
  - **a manual run that straddles a tick is not overdue.** Seed one `backup` run started 04:50 and
    finished 05:10 on a `0 5 * * *` job; at 05:30 the job is OK, not Overdue. (With `ref =
    started_at` the 05:00 tick is "missed" and the Board cries wolf five minutes after a successful
    backup, 7.3.)
  - **an operation is not a run.** Seed 14 OK backups and one `restore` record (and one `thaw`, and one
    `test-restore`) newer than all of them; the strip still has 14 cells, all `ok`, the newest of them
    the newest *backup*; `ok_14 == 14`, `failed_14 == 0`, `runs_total == 14`, `streak == 14`, and
    `median_s` is unchanged by the restore's duration. Then seed a failed restore: the job's state is
    still `OK` and the Board shows no failure (6.5, 8.2).
- `tests/gui/test_points.py`: versioned from a `points.json` fixture (with and without `summary`,
  RFC3339 with nanoseconds and offset); archive from `points.json` + `usage.json`; versioned-files from
  a seeded sqlite catalog + runs; `stale`.
- `tests/gui/test_ops.py`: `launch` passes `BE_RUN_ID`/`BE_TRIGGER`, `start_new_session`, DEVNULL; id
  format; `run_sync` timeout → `OpsTimeout`; `validate_target` rules (root escape, non-empty, source
  path, auto-suffix).
- `tests/gui/test_restore_routes.py` (monkeypatch `ops.launch` to capture argv; tmp `RESTORE_ROOT`):
  the job page's band is a GET form (`method="get"`, `action="/jobs/appdata/restore"`, no `confirm`
  input, no `#confirm-name`); `GET /jobs/appdata/restore` with the band's query renders the confirm page
  with `#confirm-name` and launches nothing; `GET` with a junk `tier`/`scope` still renders 200 with the
  defaults; `GET` for an unknown job → 404; then every row of 7.5.6 posted from that page → exact argv
  and 302 to `/jobs/<name>/runs/<id>`; confirm mismatch → 400 re-render with the sentence and values
  intact; lock held → 409; cold versioned → the store-warm-up path; thaw on a warm tier → the BLOCKER
  render on GET and 400 on POST; `/thaw/check` merges `last_check`; test-restore on cold → the Bulk
  warm-up argv; missing mount → 400 with the mount copy; bad run id → 404; `/log?offset` headers. Also
  the cold test restore's two steps (5.2 rail, R11): the first POST launches `restore.sh <job> test`;
  with `state/<job>.test-thaw.json` seeded and the stubbed script reporting "still cold", the second
  POST of the **same** route redirects with the `note` flash `Still warming up — ready by ~…` and the
  rail still reads `Warming up`; with the stub reporting ready, it writes `tested.json` and the rail
  reads `Tested <date>` with the kept path. `POST /jobs/<name>/thaw/check` with only `test-thaw.json`
  present (no `thaw.json`) → 400: the two files are not interchangeable.
- `tests/gui/test_readiness.py`: passphrase three-state; size provenance propagates to
  `cost_full_restore.provenance`; totals; cold job gets both tiers; `setup_checks` rows and sort.
- `tests/gui/test_activity_routes.py`: merge, filters, `limit`, `/activity.json` shape.
- `tests/gui/test_board_routes.py`: 8.1 shape; each verdict state's h2/button; needs-you ordering;
  acknowledged blocker absent; the cost strip from caches only (monkeypatch `billing.monthly_costs` to
  raise — the page must still render).
- `tests/gui/test_job_page_routes.py`: 5.2 acceptance items; `Copies` for archive; failure record for
  the prune case; pause/resume/delete confirm.
- `tests/gui/test_run_record_routes.py`: 5.3 acceptance items, including the pending window — a fresh
  well-formed id → 200 with `Starting…` and `.json` `{"outcome":"pending"}`; the same id after a start
  line is appended → the live record; an id stamped 20 minutes ago with no record → 404; a malformed id
  → 404; `POST …/run` (with `ops.launch` monkeypatched to do nothing) then a GET of the redirect target
  → 200, proving the primary flow never lands on 404.
- `tests/gui/test_jobs_io.py` additions: `created_at` set/preserved/passed; `assumptions`, `measured`,
  `acknowledged` round-trip; `set_enabled`; `render_crontab` byte-identical to the entrypoint format;
  `delete` removes the cache files.
- `tests/gui/test_estimate_io.py` additions: `classes` has 5 rows in `STORAGE_CLASSES` order, blocked
  exactly for versioned × {GLACIER, DEEP_ARCHIVE}; `keep_options` deltas ≥ 0, all zero at 0% change,
  `null` only for `keep_all` with change > 0; `all_jobs.first_bill ≥ projection.first_bill`;
  `typical_floor`; `keep_all` at 0% change → `unbounded is False`, `typical == first_bill`,
  `delta_monthly == 0.0` and `steady_month == 1` (the adapter override of 7.9 — without it the create
  screen prints "still growing" for a job that cannot grow, and the model stays untouched);
  `price_kind` flips with `?prices=`; `recommend_type` — always called with keywords,
  never positionally — fires rule 1 on `(size_gb=52.71, file_count=533, change_rate_pct=1)`, rule 2 on
  the manga shape `(size_gb=1780, file_count=232021, change_rate_pct=0)` (this is the case the 10 MB
  average threshold exists for; assert `rule` contains `more than 50,000 files`), rule 3 on
  `(size_gb=600, file_count=10, change_rate_pct=0)`, and returns `None` for `measured=False`; plus the
  fresh-form tri-state of 5.8 §3.1 — the manga shape with `change_rate_pct=1, change_rate_set=False`
  still fires rule 2, the same shape with `change_rate_set=True` fires nothing (`None`), and
  `(size_gb=52.71, file_count=533, change_rate_pct=1, change_rate_set=False)` still fires rule 1, so a
  fresh form recommends for both example jobs and a deliberate answer is honoured literally;
  `class_advice` raises heads-up 1 on that same manga shape on `DEEP_ARCHIVE` and not on
  `(size_gb=1780, file_count=40)`; `first_bill_reason` cases;
  `restore_quote.amount == model.restore_cost(...)` for the same inputs; `delta_verdict` bands at the
  15/30 boundaries (±14.1% → `close enough to trust`, matching the Board example; ±20% → `model runs
  high/low`; ±40% → `far apart`);
  `provenance_of` weakest-input rule; `board_cost` with and without caches.
- `tests/gui/test_job_form_routes.py` additions: POST with a blocker and no ack → 200 re-render with
  `#class-blocker` visible; with ack → 302 and `acknowledged` persisted; POST on an existing name with a
  different `type` → the saved type wins; create with an existing name → field error.
- `tests/gui/test_errors.py`: each handler renders `error.html` with the right h1; JSON paths return
  JSON; CSRF 400 says `That form had expired`.
- `tests/gui/test_sysop.py`: each sysop kind writes start/end events under `_system`, `billing.json`
  written, failure → `failed` with the message (functions monkeypatched); plus the assertions that moved
  here from `test_costs_routes.py` (10.2): `usage-refresh` calls `collect_usage` with the right bucket
  and media-prefix list and saves the cache, `billing-check` writes `billing.json`, and a `BillingError`
  lands in that file's `error` member rather than anywhere near a render.
- `tests/engine/test_archive_prune.py` additions (7.1.5): an `S3Error` from a versioned delete prints
  exactly `AccessDenied: s3:DeleteObjectVersion` and returns 1; from `list-object-versions`,
  `AccessDenied: s3:ListBucketVersions`; any other stderr, its first line truncated to 300 chars. In
  every case stdout+stderr is one line and contains no `Traceback` — the run record quotes this text
  verbatim to the owner.
- `tests/gui/test_costs_scenario.py` (or an addition to `test_costs_routes.py`): `POST /costs/scenario`
  writes `$CONFIG_DIR/cost.json` with the four members and redirects with `Saved.`; a bad
  `restore_fraction` → 400 and no file; a missing file is read as defaults and never raises; `/cost`
  renders with the saved scenario applied.
- Estimator guard `tests/estimator/test_untouched.py`: hashes of `app/estimator/model.py`, `tiered.py`,
  `prices.py`, `usage.py`, `billing.py`, `schedule.py` equal the values recorded at the start of the
  branch (compute them in the first commit; the test fails if any file changes, forcing an explicit,
  reviewed update).
- bats: `tests/bats/runs.bats` (writer functions, escaping validated with a real `python3 -c
  json.loads`, rotation, stat parsers against fixtures `tests/bats/fixtures/restic-backup-summary.jsonl`,
  `rclone-final-stats.txt` (captured on the box from rclone 1.68.2 before merging), `restic-snapshots.json`,
  `restic-ls.jsonl`, `aws-head-object-*.txt`), plus three cases that pin the bugs this spec's review
  found: (a) the `rclone-final-stats.txt` fixture contains BOTH `Transferred:` lines
  (`Transferred:   3.100 GiB / 3.100 GiB, 100%, 8.912 MiB/s, ETA 0s` and
  `Transferred:            1204 / 1204, 100%`) and the test asserts `_rclone_stat_bytes` → `3328599654`
  and `_rclone_stat_files` → `1204`, i.e. the two parsers never return the same number; (b) a versioned
  `runs_end ok` whose `SNAP_ID=a81f3c2e` produces a line that `python3 -c 'import json,sys;
  [json.loads(l) for l in sys.stdin]'` parses and whose `snapshot_id` is exactly `a81f3c2e` (and, with
  `SNAP_ID` empty, `null`); (c) `runs_end` with `BE_RUN_ID` exported but `runs_start` never called
  appends nothing and returns 0 under `set -u`; `backup-job.bats` additions (start+end lines, stats from
  stubs, `BE_TRIGGER=manual`, points cache written; an rclone stub that prints a full stats block and
  then exits 1 → the end line has `"outcome":"failed"`, `"phase":"copy"` AND `"files_added":1204`,
  proving the errexit-safe hook ran the parser; a restic stub that succeeds on `backup` and exits 1 on
  `forget` printing `AccessDenied: s3:DeleteObjectVersion` → the end line has `"outcome":"failed"`,
  `"phase":"prune"`, `"copied":true` and that error string; a lock held by a background `flock` with
  `BE_RUN_ID=20260915T050001Z-3f9a` exported → exit ≠ 0, no `state/<job>.runs.jsonl` created and the
  pre-seeded legacy `state/<job>.json` byte-identical afterwards; and the `-w 5` pair (7.1.5):
  `acquire_lock` against a lock a background process releases after ~1 s **succeeds**, against one held
  for the whole test **fails** with `another <job> run is in progress` — the first case is the GUI's
  poll probe, which must never be what skips a backup); `restore.bats` additions (records +
  lock, `list --json` no lock, `download .` argv, a download whose rclone stub exits 1 after a stats
  block still records `bytes_restored`, `thaw .` writes `thaw.json`, `thaw-status` counts, `test` per
  type, vfiles `.` in-process); `entrypoint.bats` (dirs created, `runs boot` invoked, `-inotify` argv via a
  supercronic shim with `GUI_ENABLED=false`).
- Integration: `restore_appdata.bats` (ok end line with a real `snapshot_id`, `points.json`, `list
  --json`, `test` writes `tested.json`, `restore latest` record with `files_restored ≥ 1`);
  `archive_roundtrip.bats` (`files_added ≥ 1`, `bytes_added > 0` — the real verification of the rclone
  parse); `vfiles_roundtrip.bats` (`restore.sh photos . <dir>` restores every file).

### 10.4 Fixtures
`tests/gui/fixtures/example/`: `jobs.json` (appdata, manga per 1.5, with `created_at`, `assumptions`,
`measured`), `state/appdata.json`, `state/manga.json`, `state/appdata.runs.jsonl` (30 ok runs, one slow),
`state/manga.runs.jsonl` (13 ok, the failed prune run last), `state/appdata.points.json` (14 snapshots
with summaries), `state/manga.points.json`, `usage.json`, `billing.json`, `_probe.json`,
`logs/runs/manga/<id>.log`. A `conftest.py` helper `seed_example(dirs)` copies it.

### 10.5 The exact green commands
`python3 -m pytest -q --deselect tests/estimator/test_billing.py::test_forecast_parses` ·
`bats tests/bats/` · `shellcheck scripts/*.sh scripts/lib/*.sh` · (dev box) `bats tests/integration/`.

## 11. Suggested implementation order and risks

Each increment is self-contained, leaves every test green, and ends in a commit. Engine first (the
screens read it), then the shell, then screens, then the create/edit screen, then polish.

1. **Estimator guard + vocabulary module.** `tests/estimator/test_untouched.py` with the hashes;
   `app/gui/vocab.py`. Commit.
2. **Run-record writer.** `scripts/lib/runs.sh`, `common.sh:die`, `backup-job.sh` rewrite (7.1.5–7.1.6),
   `tests/bats/runs.bats`, `backup-job.bats` additions, fixtures. Commit.
3. **Reader, cron, errors, boot.** `app/engine/runs.py`, `cron.py`, `errors.py`; `entrypoint.sh`
   (`runs boot`, `-inotify`, pid file, dirs); `tests/engine/test_runs.py`, `test_cron.py`,
   `test_errors.py`, `entrypoint.bats`. Commit.
4. **jobs_io and status.** `created_at`, `assumptions`, `measured`, `acknowledged`, `set_enabled`,
   `render_crontab`, cache-file delete; `app/gui/status.py`; `tests/gui/test_jobs_io.py`, `test_status.py`.
   Commit.
5. **Points, readiness, ops, sysop.** `scripts/lib/points.sh`, `app/gui/points.py`, `readiness.py`,
   `ops.py`, `app/engine/sysop.py`, `config_io.secrets_status_3` + `KEY_GROUPS`, cached `billing.json`;
   their tests. Commit.
6. **restore.sh + vfiles.** 7.5.3, 7.5.5; `restore.bats`, `test_vfiles_restore_all.py`; the `/restore`
   mount in compose/xml/env. Commit.
7. **Shell and design system.** `style.css` rewrite (6), `base.html` (topbar, nav, work bar, notices,
   footer), `error.html` + handlers (9), flash categories, route renames with 301s, `vocab` in Jinja
   globals; `test_errors.py`, `test_vocabulary.py` (initially over the shell only). Commit.
8. **Board.** `/` + `status.json`; `test_board_routes.py`. Commit.
9. **Job page.** `/jobs/<name>` + `status.json` + pause/resume/delete + Tool detail + rail;
   `test_job_page_routes.py`. Commit.
10. **Run record + Activity.** `/jobs/<name>/runs/<id>` (+ `.json`, `/log`), `/activity` (+ `.json`),
    `/activity/<run_id>` (+ `.json`, `/log`) for system records, the `provision` events on the
    provisioning success paths; tests. Commit.
11. **Get data back.** The band on the job page, `/jobs/<name>/restore`, thaw/check/test-restore/points
    refresh routes; `test_restore_routes.py`. Commit.
12. **Cost workbench.** `/cost` + `cost.json`, `POST /costs/scenario` + `$CONFIG_DIR/cost.json`, the
    `<noscript>` recalculate path, `estimate_io.cost_page`/`board_cost`/`job_cost_band`/
    `delta_verdict`/`provenance_of`/`restore_quote`, the `keep_all`-at-0% override, sysop launches for
    refresh/billing, the removal of `POST /costs/billing`; update `test_estimate_routes.py`,
    `test_costs_routes.py` (per 10.2), `test_estimate_io.py`, add `test_sysop.py` moves. Commit.
13. **Setup, Destination, Keys, About.** 5.10–5.13; IAM template + tofu policy change; update
    `test_provision_routes.py`, `test_config_routes.py`, `test_about_routes.py`, `test_app.py`. Commit.
14. **Create/edit job.** `wizard_estimate` extension + `recommend_type` + `warnings` (7.9), the
    template and JS (5.8–5.9), `job_save` re-render + blocker enforcement; update `test_jobs_routes.py`,
    `test_jobs_estimate_routes.py`, add `test_job_form_routes.py`. Commit.
15. **Polish.** Full `test_vocabulary.py` over every page; responsive pass at 400px; reduced-motion
    pass; README "Restore runbook" update (`.` scope, `list --json`, `thaw-status`, `test`, the lock,
    the mount, the GUI as the primary path); `backup.env.example` knobs; deploy notes. Commit.

Risks and mitigations:

| Risk | Mitigation |
|---|---|
| rclone's final stats block differs from the regex on rclone 1.68.2 | Capture the real block on the box before step 2 merges (`rclone copy /tmp/x s3:<bucket>/tmp-verify -v --stats 1s 2>&1 | tail -6`) into the fixture; the integration test asserts `bytes_added > 0`; fallback `--use-json-log --stats-log-level NOTICE` and parse the last line's `stats` object. |
| `restic snapshots --json` summary member names on 0.17.3 | Confirm with `restic snapshots --json | head -c 2000` on the box; the parser reads the names in 7.1.3 (`total_bytes_processed`, `files_new`, …). |
| `supercronic -inotify` not firing on Unraid's FUSE `/cache` | An unconditional `SIGUSR2` to `supercronic.pid` after every crontab write (7.3) — belt and braces, ignored if the scheduler is not running — plus the `crontab_stale` warning for a write that failed outright; verify on the box (12.7). |
| A timezone/DST mismatch turns a healthy install into a red Board | Cron and `now` are evaluated in the same zone; `TZ=UTC` ships; DST vectors in `test_cron.py`; the 15-minute grace absorbs skew; Running takes precedence over Overdue. |
| A `running` record left by a killed process | Reconcile at boot and lazily on read, gated on the kernel lock; never by age. |
| Thousands of `restore-object` calls for a big Plain copy | The confirm page states it (`the request itself takes hours to send`); the run record shows the count line every 500 objects; the operation is detached. |
| Restore into the wrong place | Three server-side rules (root, empty, never the source) + typed-name confirm; `/mnt/user` stays read-only. |
| Cost Explorer calls blocking a render | All billing reads come from `billing.json`; refresh is a detached sysop. |
| Prune-failure change surprises an existing install (the next run of a job whose key lacks the permission shows Failed) | That is the intended honesty; the failure record names the fix; the README's changelog says it. |
| The health gate after deploy hits `/` with an unprovisioned `/config` or an empty `/cache` | `/` returns 302 → `/setup` unprovisioned and 200 with the empty verdict otherwise; `test_app.py` covers both. |
| Tests that stub `python3` break if the runner calls Python | The runner's bookkeeping is bash-only (7.1.4); `points_refresh` shells to `restic`/`rclone` only. |
| Old `/cache/state/*.json` on the box predate this work | The backfill (7.1.8) seeds one record per job at first boot; `read_state` unchanged. |

## 12. Deploy and verification checklist

Before deploy (owner): create `/mnt/user/restore` on the array; edit the container template once to add
`/mnt/user/restore` → `/restore` (read/write); optionally set `RESTORE_ROOT_HOST` if a different host
path is used. Then `git push`, rsync, `/root/deploy-backup-engine.sh` (1.3).

On the box, in this order:
1. `docker exec backup-engine sh -c 'rclone version; restic version; supercronic -version'` — versions as expected.
2. `docker logs backup-engine | tail -20` — the `runs boot` summary line and `supercronic … -inotify`.
3. `cat /cache/state/appdata.runs.jsonl` — the backfilled `…-0000` line; `cat /cache/crontab` — one line per enabled job.
4. Open `/` — Board: verdict, needs-you (expect the IAM blocker if the key predates the policy), both
   jobs with a one-cell strip, the cost strip from caches; the clock says `refreshed N s ago` and ticks.
5. Open `/jobs/appdata` — OK token, status strip, one-cell ledger, restore points (press `List them
   now` if empty; expect 14), needs-line green; the Get-data-back guard is a text warning only (no
   typed-name field here — that lives on the confirmation page, step 9) and `Start restore →` enables
   once a point and a valid target are chosen; `···` → Pause → token Paused, `Next run —`; Resume.
6. Press `Run now` — redirect to the run record, which shows `Starting…` for a moment (never a 404) and
   then the live record; the log tails live; `Running…` on the job page; when
   done, the Done signal and a second strip cell. `cat /cache/logs/runs/appdata/<id>.log` fills.
7. Edit appdata's schedule (change the minute) — `cat /cache/crontab` already shows the new minute when
   the save returns (the GUI writes it in the request, not on a timer); `docker logs` shows
   supercronic's reload line, triggered by inotify or by the `SIGUSR2` the GUI always sends (7.3); the
   Board shows no `The schedule file on disk does not match your jobs` warning.
8. Rail → `Test restore — one file, ~$0.01` — the run record shows the restored path; the rail row
   reads `Tested <today>`; `/setup` shows `Tested … · appdata`.
9. Get data back → pick the newest point, keep the default target, press `Start restore →` (the band
   only navigates) → on the confirmation page check the size, cost and target, type `appdata`, press
   `Start restore` — the run record shows progress and ends `Done`;
   `ls /mnt/user/restore/appdata/<date>/backup/media/appdata_backups/` lists files.
10. Open `/jobs/manga` — Failed (if the permission is still missing) with the failure record and `Fix
    the permission →`; `Copies 1`; `What is there now`; `Warm up first — up to 12 h` with the two quotes.
    Do NOT start the warm-up unless intended (it issues 232,021 requests).
11. `/setup` — five checks; `Probe now` writes `_probe.json` and the Board's needs-you updates;
    versioning row shows `on` or `Not checkable with this key`.
12. `/setup/keys` — grouped form, three-state tokens, `Saved.` on save; `/setup/about` — version, tools, licences.
13. `/cost` — proof band from caches; `Refresh usage` → Activity shows `usage refresh` live → the
    figures flash on completion; scrubber readout matches the chart.
14. `/jobs/new` — pick a small folder: measurement lands, sections un-dim, a recommendation prints its
    rule; pick `DEEP_ARCHIVE` on Snapshot backup: WON'T RUN, footer disabled; `Use Instant, cheaper`
    clears it; Cancel.
15. `/activity` — every operation above listed newest first; filters work; `Raw shared log →` opens `/logs`.
16. Phone width (Chrome device toolbar, 400px): Board, job page, create job — no horizontal page scroll;
    the class table and ledger scroll inside their wrappers; the sticky footer wraps to two rows.
17. Old links: `/jobs`, `/estimate`, `/config`, `/about`, `/provision` all land on the new pages.

## Appendix A — Mockup-to-spec traceability

Night Shift mockup (`mockup-night-shift.html`):

| Component (mockup) | Spec section |
|---|---|
| `:root` tokens (lines 12–40) | 6.1 |
| `.topbar`, `.brand`, `nav.switcher`, `.chip`, `.clock` (63–91, 365–379) | 5.15 (switcher content replaced by the real nav, R4) |
| `#workbar` (87–91, 379) | 6.4 |
| `.tok*` state tokens (118–128) | 4.7 |
| `.sig*` signals + glyphs (136–162) | 4.5 |
| `.p-measured/.p-assumed/.p-invoiced`, `.price-stamp`, `.legend`, `.fig`, `.stamp`, `.delta` (102–116) | 4.6 |
| Board `.verdict` (386–400) | 5.1 band 1 |
| Board `.needs`, `.needs-row`, `.errline` (404–428) | 5.1 band 2 |
| Board jobs table, `.cell2`, `.jobname`, `.jobpath`, `.compact-only`, `.col-strip`, `.col-took` | 5.1 band 3 |
| `.strip`/`.cellx` run strip + legend | 6.5 (run strip), 5.1 legend, R2 |
| Board band 4 `.grid4`, `.sfig`, delta, note, per-job table, legend, stamp | 5.1 band 4 |
| Job page `.jobgrid`, `.rail`, `.jobhead`, `.actions` (614–640) | 5.2 header, rail |
| `#run-done` transient | 5.2 Transient Done |
| `.statusstrip` | 5.2 status strip |
| Ledger `.ledger`, `.bars`, `.ledger-axis`, hint | 5.2 ledger |
| `#restore-band`: `.rphead`, `.rp`, details, target, `.needsline`, sibling warning | 5.2 Get data back (a GET form) |
| `.guard`, `.confirm`, `#confirm-name`, `#start-restore` and their JS (1190–1198) | 5.4 — the mockup drew them in the band; they move to the confirmation page so the name is typed once (Appendix B, decision 42). The band keeps `.guard` as text only. |
| "What this job costs" `.grid4`, table, delta, legend, `#price-stamp`, `#live-prices` | 5.2 cost band |
| "How it is set up" `.defgrid`, `Edit →` | 5.2 settings |
| Tool detail `<details>`, `.cmd`, Copy | 5.2 Tool detail, 6.5 |
| Rail `.rrow`, `.dot`, Test restore button | 5.2 rail, R11 |
| Footer `.sitefoot` legend `<details>` (1059–1083) | 5.15 |
| Motion `.flash`, `pulse`, reduced motion (321–323, 356–362) | 6.4 |
| Media queries (900/1000/620) | 6.6 |
| New job screen (872–1055) | superseded — not built (5.8 comes from the blend) |
| Mockup-only footer line ("design direction mockup…") | not shipped |

Ledger Runbook blend mockup (`mockup-ledger-runbook.html`, `<main id="screen-new">` 766–984, CSS 9–267, JS 1067–1229):

| Component (mockup) | Spec section |
|---|---|
| `.wrap` 960px, `.lbl`, `.h`, lead with the three idioms | 5.8 §1, 6.3 |
| Section 1: root line, `.card.tree`, Selected line, measurement line, Re-measure | 5.8 §2 |
| `.sev.rec` RECOMMENDED block, `.radio`, `.term`, Why/Rule line | 5.8 §3.1 |
| `h3.subh` "Where it's stored — priced for your …", `.tscroll.card`, `table.classes`, `tr.blocked`, `.reason` | 5.8 §3.2 |
| `.sev.wont` `#class-blocker`, `.acts`, fix buttons, `Save it anyway ▸`, `#create-why` | 5.8 §3.3 |
| Churn radios + "An assumption…" line | 5.8 §3.4 |
| `#keep-head`, `#keep-block`, `.k-delta`, Advanced ▸, `#keep-consequence`, `.inert` | 5.8 §3.5 |
| `hr.hair`, WHEN `.seg`, WHOSE `.whose`, `#when-head`, `#fig-job`, `#fig-all`, `#when-note`, `#new-working` | 5.8 §3.6 |
| `.sev.heads` (CSS only; none at rest) | 5.8 §3.7 |
| Section 3 frequency/time, human sentence, Advanced ▸, enabled checkbox, collision line | 5.8 §4 |
| Section 4 name field + hint | 5.8 §5 |
| `.formfoot`, `#create-btn`, `#create-run-btn`, Cancel, `#foot-figs` | 5.8 §6 |
| `#pricestamp` + toggle | 5.8 §7 |
| `.flash` recompute animation (200 ms) | 5.8 §8, 6.4 |
| `.altstate` "Alternate state — blocker" box | mockup chrome — not built |
| Mockup JS cost arithmetic (`DATA_GB`, `RATE`, `F1`, `oldGB`, `model`) | not ported — the estimator computes everything (7.9, 8.6) |
| Blend Status / Job / Cost screens | not chosen — not built |

## Appendix B — Decisions where the sources were silent or disagreed

The owner's rulings (settled before this document):

- **R1** The user-facing name of type `archive` is "Plain copy" everywhere (never "Bulk copy", never
  "Mirror" — it collides with the persisted `mirror` boolean); the versioned type is "Snapshot backup"
  (Night Shift's vocabulary), also on the create screen.
- **R2** The run strip shows the last 14 runs of the job (newest on the right), one cell per run record;
  hover/tap gives date, outcome, duration, link to the record; fewer than 14 runs → empty cells on the
  left ("no run on record yet"). Missed schedules are not cells — the Overdue state and "next run" carry
  that. Copy: "Last 14 runs", legend "no run on record yet" / "older than the last 7 runs", axis "last 7
  runs →" / "latest". The Night Shift mockup drew 14 calendar nights; switching back is a one-line change
  in `status.strip()` (group records by local date instead of taking the newest N).
- **R3** Screens the mockup did not build are specified from the direction JSON plus the mockup's
  components and presented as specified. Screens with no mockup: Run record (5.3), Get data back
  confirm/operation page (5.4), Activity (5.5), Cost workbench (5.6; the Board's band 4 and the job
  page's cost band are the built fragments), Setup readiness (5.10), Destination (5.11), Keys & secrets
  (5.12), About (5.13), error pages (5.14), the Edit job variant (5.9), and a FAILED job page (5.2's
  failure record is composed from the Board's blocker row and the direction's two-column description).
- **R4** Primary nav: Board · Cost · Activity · Setup; "+ New job" on the Board's band head; the
  mockups' screen switcher was a demo device.
- **R5** Media shares is not a screen; the job form's folder browser is preserved inside the folder step.
- **R6** Where the direction JSON and the mockup CSS disagree on a size or weight, the mockup wins
  (e.g. the needs-row action column is 170px, not 140px; the rail sticks at `top:76px`).
- **R7** Under 1000px the rail moves under the status strip.
- **R8** Plain copy jobs have no snapshots: their Get data back section shows "what is there now"
  (object count, size, tier, last completed run) plus warm-up and download; only Snapshot backup and
  File history jobs list points in time.
- **R9** Overdue grace = 15 minutes after the scheduled start; precedence Running > Paused > Overdue >
  Failed > OK > Not run yet; the Board sorts Failed, Overdue, Running, Paused, OK, Not run yet.
- **R10** Dollar figures in the mockups are example content, never fixture truth; the live app computes them.
- **R11** "Test restore" on a DEEP_ARCHIVE/GLACIER job is a warm-up request: thaw the single most
  recently copied object at the Bulk tier, record it as a pending operation with its ETA, then download
  it to `/mnt/user/restore/<job>/test/` when ready; the button carries the price on its face.

Decisions made in this document (chosen default and why):

1. **Prune failures are recorded failures** (`outcome: failed`, `phase: prune`, `copied: true`). Today
   they are `|| log_warn`, so the owner's example (manga, AccessDenied on DeleteObjectVersion) could not
   exist. A job whose keep rule cannot run is not doing what it was told; the record says the files were
   copied. The Board blocker's third sentence changed from the mockup's "Nothing has been copied since 6
   September." to "The files are copied, but old versions have not been cleaned up since 6 September."
   because the former would be false.
2. **Restore root is `/mnt/user/restore` (singular)**: the owner context (R11) uses it; the mockup's
   `/mnt/user/restores/` is replaced in all copy and in `RESTORE_ROOT_HOST`.
3. **Strip saturation is by position** — the 7 rightmost cells saturated, every cell 8 or more from the
   right dim, at any strip length (8–14 on the Board, 8–30 on the job-page ledger) — not by age. It
   follows from R2 ("older than the last 7 runs"), matches the mockup's ledger (cells 1–23 dim, last 7
   saturated) and keeps a weekly job's strip readable.
4. **Typical duration** = median of OK backup runs among the newest 30 backup records, undefined below
   three OK runs (`no typical yet`); "slow" = duration > 3× typical AND typical ≥ 60 s (so a 5 s job
   never reads slow at 15 s).
5. **Run id** = `YYYYMMDDTHHMMSSZ-xxxx` (the engine extract's format), not a bare timestamp: two runs
   in one second stay distinct and the id is URL-safe.
6. **Rotation** keeps 200 runs (400 lines), logs 120 days; env knobs.
7. **Outcome vocabulary** in run records: `running | ok | failed | aborted`; the legacy state file keeps
   `success | failure`. An `aborted` run renders as failed (`Stopped`).
8. **Storage-tier plain phrases differ on the create/edit screen** (the blend's "Instant, cheaper to
   keep" / "Instant, cold price" / "Cold" / "Deepest") and use Night Shift's phrases everywhere else,
   per the owner's precedence rule; the constant chip is always adjacent, so the concept is unambiguous.
   Severity label words on that screen are the blend's (`Recommended for this folder`, `Heads up`,
   `Won't run`).
9. **Job type names are global** (R1): the create screen's radios read "Snapshot backup", and the WON'T
   RUN copy reads "A Snapshot backup can't read from …" instead of the mockup's "Snapshots can't read".
10. **Create/edit container is 960px** (`--w-form`), the blend mockup's `.wrap`; the class table needs
    640px of width and the reading container would force a scroll.
11. **No web fonts**: the mono stack is `ui-monospace` first; the Night Shift mockup's Google Fonts link
    is dropped so the GUI works on a LAN without internet.
12. **`GLACIER_IR` row included** in the class table (five rows in `model.STORAGE_CLASSES` order); the app
    supports it and the vocabulary names it.
13. **Blocked class rows follow the selected type live**; the mockup's static `tr.blocked` was a rendering shortcut.
14. **Three recommendation rules** (5.8 §3.1): the mockup's one verbatim plus two derived from the
    manga example; every printed rule is a truthful one-liner; anything else → "No recommendation".
    Re-evaluation pre-selects only while the type radios are untouched.
15. **Keep everything at 0% change** reads `+$0.00/mo · nothing to grow` (the truth at 0%), not the
    mockup's "still growing".
16. **"The first 6 months" for Keep everything** prints the model's real total (the model computes it);
    `still growing` is reserved for the typical-month cell and `at least $X` for the all-jobs typical
    cell, with `typical_floor` = Σ (typical if bounded else first bill) computed server-side — this also
    covers an unbounded OTHER job (the mockup did not handle it).
17. **Fresh-form defaults**: change rate ~1% (the state in which every control has a visible
    consequence; the owner can drop it to 0% and the page explains what that does), time 05:00, days
    preset 180, count preset 30, tier STANDARD, keep rule tiered for Snapshot backup and days 180 for
    the other two. The ~1% default is a *display* default, not an answer: until the owner touches a
    change-rate radio the recommendation rules evaluate the change rate as **unset**
    (`change_rate_set=False`, 5.8 §3.1), because rules 2 and 3 require 0% and would otherwise never fire
    for the manga-shaped folder they were written for — the RECOMMENDED block would be silently absent
    on exactly the example job. The printed churn wording still follows the radios.
18. **Tiered on non-Snapshot types** is visible, struck, with the reason and the radio disabled (the
    class-table principle), and still snaps to days on a type change.
19. **Bundled and mirror controls' placement**: bundled under the class-table sentence (it moves the
    per-object figures shown there); the delete-locally choice after the keep rule (a setting about
    deletions, which the keep rule then governs).
20. **Save-it-anyway mechanics**: relabel to `Acknowledged — will save anyway`, persist `acknowledged`
    entries, server enforcement, no Board nagging about an acknowledged blocker.
21. **Heads-up copy** for the four class warnings is re-voiced from `storage_advice` strings in the
    blend's vocabulary; the minimum-stay sentence is the blend's own shape.
22. **`.flash` class**: the old `ul.flash` notice list is retired (notices are signals), so the change
    flash keeps the name.
23. **Font weights**: exactly those the mockups use (6.2).
24. **Edit variant**: name and kind locked, drawn from two sentences in the sources plus the owner's
    instruction that type and destination lock.
25. **`file_count` is posted by the wizard** (today only `size_gb` is), fixing the silent bug that kept
    232,021 objects out of the model.
26. **The IAM blocker is not raised on the create screen**; it is derived from failed runs and shown on
    the Board, the job page and Setup.
27. **"Kept as daily/weekly/monthly"** sub-copy on restore points is dropped: restic does not store the
    reason a snapshot was kept, so it cannot be promised; the rows show size and change stats and the
    keep-rule sentence sits under the list. That removed the mockup's sixth row ("kept as the weekly
    one") and with it the rule that produced seven visible rows, so the rule is restated in its place:
    **the newest six points plus the oldest** (5.2). Seven rows and `Show the other 7 restore points`
    for the example's 14, exactly as the mockup renders, with no claim restic cannot back.
28. **Per-folder restore pricing** for Plain copy scopes is quoted as a full restore with the note
    `priced as a full restore` (no per-folder sizes are cached).
29. **Warm-up default speed** in the GUI is Standard (the headline "up to 12 h" everywhere in Night
    Shift), with Bulk offered and priced beside it; the CLI default stays Bulk.
30. **Warm test restore** writes to a scratch folder under `/cache/restore-test/<job>/<run id>/`, is
    verified and deleted (the mockup said `/tmp`; the copy now says "a scratch folder"); the cold case
    follows R11.
31. **Bucket versioning check**: probed with `get-bucket-versioning`; `AccessDenied` → `unknown` with a
    `Mark as confirmed` control; `s3:GetBucketVersioning` is added to the IAM template so re-applied
    keys can probe it (the policy today lacks it).
32. **Cost Explorer is never called during a render**: results live in `billing.json`, refreshed by a
    detached system operation; usage refresh and the destination probe use the same mechanism
    (`app/engine/sysop.py`), so they appear in Activity with progress.
33. **Provenance of the first bill** is `assumed` whenever the month-1 figure includes any old-versions
    cost at an assumed change rate (the mockup left `$1.39` unmarked as a simplification); the rule is
    applied uniformly (4.6).
34. **"Share of the bill"** stays `not split` with an honest note; the mockup's "How to tag them →" link
    is dropped because S3 cost-allocation tags are per bucket, not per prefix, so tagging cannot split
    two jobs in one bucket.
35. **Clock shows the zone** (`07:42 UTC`) and the footer states `Times follow the container's clock`;
    the mockup's clock had no zone, but every time on the page depends on it.
36. **Flash categories** are five (`success`, `failure`, `warning`, `blocker`, `note`); the busy 409
    uses `blocker` because it refuses an action rather than reporting a past event.
37. **JSON endpoints return JSON errors**, never the HTML error page (their callers are `fetch`).
38. **Deleting a job deletes its run history and caches on this machine** (never bucket data); the
    confirm dialog says so.
39. **`SOURCE_ROOT_HOST`** (default `/mnt/user`) is introduced so the owner-facing path can be printed
    without hard-coding it.
40. **A `Mark as confirmed` by-hand state** for versioning is accepted as readiness (`confirmed_by_hand`)
    because the runtime key may never be able to probe it.
41. **The delta verdict bands are 15/30, not 10/25** (4.6). The mockup pairs the example's `+14.1%` with
    "close enough to trust, and it errs on the expensive side", which a 10% band would contradict; the
    copy ships verbatim, so the band moved to fit it. The `Difference` figure's own colour keeps the
    tighter 10% threshold, because the mockup also prints that `+$0.56` in `--warn` (R6), and its
    sub-line (`+14.1% · model runs high`) is a direction label, not a verdict.
42. **One restore flow: the band collects, the confirmation page starts** (5.2, 5.4). The job page's Get
    data back band is a GET form that navigates to `/jobs/<name>/restore`; that page owns the `.guard`,
    the typed-name confirm and the single POST. The mockup drew the guard and `Start restore` inside the
    band (its button is an inert `type="button"`), and the direction describes both "type-the-job-name
    confirm" and a `/jobs/<name>/restore` operation — specified both ways, the owner would have typed the
    name twice or the confirmation page would have been unreachable. The confirmation page wins because
    it is where the honest cold-storage numbers live (cost, warm-up hours, what gets written where),
    which is shaping decision 1's whole point; the band keeps the mockup's guard box as a text-only
    statement of consequence.
43. **Recommendation rule 2 and heads-up 1 trigger at an average under 10 MB**, not 1 MB (5.8 §3.1,
    §3.7; `storage_advice.py:69` changes from `< 1.0` to `< 10.0` MB). At 1 MB neither could fire for
    manga — 232,021 files in 1.78 TB averages 7.9 MB — yet both texts are written about manga, and the
    cost they describe is per-request, driven by the object count.
44. **A launched run's record page has a `pending` state** (5.3). `Run now` and every restore POST
    redirect to `/jobs/<name>/runs/<id>` before the child has taken the lock and written its start line,
    so a well-formed id with no record yet renders 200 and polls for ten minutes
    (`runs.PENDING_WINDOW_S`), turning into "This run never reported starting" after 30 s. 404 is
    reserved for a malformed id, an unknown job, or a stale id — otherwise the app's most-used button
    would land on an error page.
45. **A retrospective signal still has a fix button — outside the box** (4.5, 5.2, 5.3). The direction's
    "never a fix button" keeps the failure signal a statement of what happened; the recognised error
    class's `WHY THIS HAPPENS` / `WHAT TO DO` columns render beneath it, and `WHAT TO DO` is where
    `Fix the permission →` lives. Without this, the app's most important failure would be a dead end,
    which is audit problem 9.
46. **A total carries the weakest mark among the jobs it sums** (4.6). `$4.54` includes appdata's
    assumed `$2.69`, so it renders `.p-assumed` on the Board's `The model says` figure and on both
    totals rows. The mockups drew totals unmarked, which contradicts the rule the same page's legend
    explains; decision 33 already applied the rule uniformly to the first bill, and a total that hides
    an assumption is the one number most likely to be quoted back as fact.
47. **No per-folder File history restore in this increment** (5.2, 7.5.6). The engine has exactly two
    File history invocations — `.` (every path live at that moment) and one relpath — so a folder
    picker would be a control with no argv behind it. The scope select offers `Everything as of this
    point` and `One file, by path`. Adding `restore_all(prefix=…)` later is a contained change: one
    argument, one trailing-slash argv form, one test.
48. **The IAM verdict says "refused a delete"** (5.1, 7.4). The Night Shift mockup's verdict reads
    "refused the upload", which is wrong for this error: `DeleteObjectVersion` is refused during the
    clean-up step, *after* the files have been copied — which is also why the needs-row's third
    sentence had to change (decision 1). Copy that describes the wrong step sends the owner to the
    wrong fix.
49. **The create/edit screen marks by size alone** (5.8 §3.6): `.n.assumed` is driven by
    `provenance.size == "assumed"` and nothing else. On this screen the owner *is* the source of the
    change-rate assumption — the radios are two inches above the figure and the line under them says so
    — and marking it again would dot and dim every number on a fully measured form, which is not what
    the blend draws. The skeleton screens keep the full 4.6 rule, because their reader did not set it.
50. **Duration bars use a squared scale**, `max(2, round(20 × (d / d_max)²))` (5.2). The mockup draws
    typical runs at 4–6 px against a 20 px peak; linear would draw them at 9–10 px and the row would
    read as noise rather than as "one run took twice as long". R6 makes the mockup the truth on size.
51. **Buttons follow the blend on `.form` pages and Night Shift everywhere else** (6.3). The two
    mockups genuinely differ (radius 0 vs 4, transparent vs `--surface`, a bordered ghost vs a
    borderless one), and the create screen's blocker fixes and footer rely on the bordered ghost reading
    as a button. Scoping by container is the only rule that keeps both mockups' screens looking like
    themselves.
52. **The Cost Explorer credential has exactly one editing surface**, Keys & secrets (5.6 band 5,
    5.12). `/cost` shows a status line and a link. Two write-only forms over one secret, with two save
    routes and a `'••••• (unchanged)'` placeholder in both, is how a value ends up half-saved with
    nobody able to say which form last won.
53. **A corrupt `jobs.json` on save re-renders the form**, it does not take over the page (5.8 §8). The
    owner has just filled in four sections; an error page or a flash-and-redirect throws all of it away
    for a fault that has nothing to do with what was typed. The flash survives only on delete, which has
    no form to re-render.
54. **`Check now` on a pending cold test restore re-POSTs `/jobs/<name>/test-restore`** (5.2, R11).
    One endpoint, one script, one state file: the script decides from `test-thaw.json` whether this is
    "still warming" or "download it now". `/thaw/check` is a different operation over a different file
    (`thaw.json`, a scoped warm-up for a real restore) and the two must not be crossed.
55. **The run strip counts backup runs only** (6.5, 8.2). Run records now cover restores, downloads,
    warm-ups and test restores, and R2 defines a run as an invocation of `backup-job.sh`. A restore
    painting a green square — or a failed restore painting a red one — would make the one row the owner
    reads every morning mean something other than "did my backups run".

<!-- end of spec -->
