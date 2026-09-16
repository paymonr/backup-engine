# Backup-Engine UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the backup-engine GUI as the "Night Shift" operations shell — jobs as first-class objects, restore in the GUI, a per-run history strip, split status/cost home, one vocabulary — with the create/edit-job screen from the "Ledger Runbook" blend, on the app's real content.

**Architecture:** Engine first (a run-record store, cron/overdue/next-run derivation, an error-class table, a detached-operation launcher, restore execution), then the shell and design system, then each screen reads that engine, then the create/edit-job wizard, then a polish pass. Flask/Jinja GUI, bash runner, restic/rclone/aws-cli engines. The validated cost estimator is frozen and only read through `app/gui/estimate_io.py`.

**Tech Stack:** Python 3 / Flask / Jinja2, vanilla JS + CSS (no framework), bash (`set -euo pipefail`) with `flock` locking, supercronic scheduler, pytest + bats, Docker on Unraid.

**Spec:** `docs/superpowers/specs/2026-09-15-ui-redesign-design.md` (committed at `10c5744`). The plan argues from the spec; executors read both. Every task cites the spec subsections that hold its verbatim copy strings, data contracts, and code — those are authoritative and must be used exactly.

## Global Constraints

Every task's requirements implicitly include this section.

- **Branch:** all work on `ui-redesign-nightshift` (already created off `impeccable-versioned-files`). Never commit to `master` or `impeccable-versioned-files`.
- **Green command (must pass after every task's commit):** `python3 -m pytest -q --deselect tests/estimator/test_billing.py::test_forecast_parses` — today `495 passed, 1 deselected`; the deselected test is date-sensitive and unrelated. Bats where a task adds them: `bats tests/bats/<file>.bats` and `bats tests/integration/<file>.bats`.
- **The estimator's math is frozen.** Do not edit `app/estimator/model.py`, `tiered.py`, `prices.py`, `usage.py`, `billing.py`, `schedule.py`, or anything under `tests/estimator/`. Extend behaviour only by adding adapter functions in `app/gui/estimate_io.py` that call the unchanged model. Task 1 installs a hash guard that fails if any frozen file changes.
- **One vocabulary, enforced by a test.** User-facing copy uses the plain names in spec §4.3; tool/implementation terms (`restic`, `rclone`, `repository`, `snapshot_id`, `egress`, …) are demoted to expandable detail only. The mono law (§4.4): monospace is only ever a machine's own string (a path, a key name, a bucket, a command), always paired with its plain-language half. `tests/gui/test_vocabulary.py` (Task 7, completed Task 26) enforces both against `vocab.FORBIDDEN_TERMS` / `ALLOWED_PHRASES` / `TERM_EXEMPTIONS`.
- **The persisted contracts only grow.** `jobs.json` fields and `jobs_io.validate` rules gain fields, never lose them. The per-job state file `$CACHE_DIR/state/<job>.json` keeps its existing keys (`last_run`, `outcome`, `type`, `snapshot_id`, `duration_s`, `error`, `exit_code`). Old state files and a missing `runs.jsonl` must not break anything (backfill, not migration).
- **Preserve (spec §2.3), do not reimplement:** the provisioning safety model (`app/gui/provision.py`), write-only secrets with per-key status, the schedule builder (`app.js:66-128`), conditional visibility `data-when-*` (`app.js:502-556`), the folder browser (`app.js:14-64`, `fsbrowse.py`, `dirsize.py`), the dark token set, the eyebrow→h1→lead reading-page opening, the honest copy strings listed in §2.3, and restore.sh's per-type dispatch (surfaced, not rewritten).
- **Do not import the unchosen directions (spec §2.2):** no Runbook margin column, no Quiet Ledger ledger-rows-as-skeleton. Where Night Shift and the blend vocabulary conflict, Night Shift wins except on the create/edit-job screen.
- **TDD, DRY, YAGNI, frequent commits.** Every task is test-first and ends in one commit. Bash bookkeeping is pure bash + `grep/awk/tail/find` — the runner must never call Python for bookkeeping (`tests/bats/backup-job.bats:74-84`).
- **Visual truth:** the two mockups under `docs/superpowers/specs/2026-09-15-ui-redesign-sources/` (`mockup-night-shift.html`, `mockup-ledger-runbook.html`) and the artifacts they were published as. When copy or layout is ambiguous, the mockup is authoritative; a ruling in spec Appendix B overrides where noted.

## File Structure

New files (spec §7 file map):

- `scripts/lib/runs.sh` — append-only JSONL run-record writer, per-run log path, rotation, tool-stat parsers (pure bash).
- `scripts/lib/points.sh` — `points_refresh JOB TYPE` → `$CACHE_DIR/state/<job>.points.json`.
- `app/engine/runs.py` — run-record reader / fold / reconcile / backfill + `boot` CLI.
- `app/engine/cron.py` — 5-field cron evaluator (`next_after`, `describe`) + TZ helper.
- `app/engine/errors.py` — the error-class table and `classify()`.
- `app/engine/sysop.py` — detached system operations (usage refresh, billing check, destination probe) recorded under `_system`.
- `app/gui/status.py` — state derivation, next run, overdue, strip, median, streak, verdict, needs-you, `crontab_stale`.
- `app/gui/points.py` — restore-point views per job type from caches.
- `app/gui/ops.py` — launch scripts detached with a pre-assigned run id; sync helpers with timeouts; `validate_target`.
- `app/gui/readiness.py` — recovery readiness + setup checks; `WARMUP` table; passphrase three-state.
- `app/gui/vocab.py` — vocabulary dicts + the test's `FORBIDDEN_TERMS` / `ALLOWED_PHRASES` / `TERM_EXEMPTIONS`.
- New templates: `error.html`, `board.html` (or `/` in the existing index), `job.html`, `run.html`, `activity.html`, `cost.html`, plus the Setup-family restyles.
- New tests named per task below.

Changed files: `scripts/backup-job.sh`, `scripts/restore.sh`, `scripts/lib/common.sh`, `scripts/entrypoint.sh`, `app/engine/vfiles.py`, `app/gui/jobs_io.py`, `app/gui/routes.py` (and any blueprint split the spec names), `app/gui/estimate_io.py`, `app/gui/config_io.py`, `app/gui/static/style.css`, `app/gui/static/app.js`, `app/gui/templates/base.html` and the screen templates, `provisioning/iam-policy.json.tmpl`, the OpenTofu policy, `docker-compose*.yml` / `backup-engine.xml` / `backup.env.example`, and the test files each task names.

---

## Task Ordering

The 15 spec §11 increments map to the tasks below. Engine tasks (1–6) are headless and fully unit/bats-testable; do them first. Shell + design system (7) unblocks every screen. Screens (8–12) each read the engine. Setup family (13), create/edit job (14), polish (15). Each task leaves the full suite green and ends in a commit. Larger increments are split where a reviewer could reject one half while approving the other; those carry an `a`/`b` suffix and share the increment's spec sections.

---

### Task 1: Estimator freeze guard + vocabulary module

**Spec:** §2.3, §4.3, §4.4, §10.3 (the `FORBIDDEN_TERMS` / `ALLOWED_PHRASES` / `TERM_EXEMPTIONS` lists), §11 step 1.

**Files:**
- Create: `app/gui/vocab.py`
- Create: `tests/estimator/test_untouched.py`
- Test: `tests/gui/test_vocab.py`

**Interfaces:**
- Produces: `vocab.TYPE_NAMES`, `vocab.CLASS_NAMES`, `vocab.STATE_NAMES` and any other dicts §4.3 defines (exact keys/values from the spec table); `vocab.FORBIDDEN_TERMS: set[str]`, `vocab.ALLOWED_PHRASES: set[str]`, `vocab.TERM_EXEMPTIONS: dict[str, set[str]]` (per-page allow-lists from §10.3). All later template and JSON tasks consume these.

- [ ] **Step 1: Write the freeze-guard test.** `tests/estimator/test_untouched.py`: compute SHA-256 of each frozen file (`app/estimator/model.py`, `tiered.py`, `prices.py`, `usage.py`, `billing.py`, `schedule.py`) and assert it equals a pinned hex constant. Generate the pins from the current files.
- [ ] **Step 2: Run it — expect PASS now** (the pins match the current files); this test exists to FAIL later if anyone edits the frozen math.
- [ ] **Step 3: Write `tests/gui/test_vocab.py`** asserting the dicts expose the exact names from §4.3 (e.g. archive → `Plain copy`, versioned → the §4.3 name; storage-class plain names; state names OVERDUE/PAUSED/RUNNING/etc.) and that `FORBIDDEN_TERMS` contains the §10.3 terms and `ALLOWED_PHRASES` the §10.3 exceptions.
- [ ] **Step 4: Run — expect FAIL** (`vocab` not created).
- [ ] **Step 5: Write `app/gui/vocab.py`** with the dicts and lists verbatim from §4.3 and §10.3.
- [ ] **Step 6: Run both new tests + full suite — expect green.**
- [ ] **Step 7: Commit** `feat(vocab): freeze estimator math with a hash guard; add the vocabulary module`.

---

### Task 2: Run-record writer (`runs.sh`) + `backup-job.sh` rewrite

**Spec:** §7.1.1–§7.1.7 (run-record schema, `runs.sh` functions, the `backup-job.sh` `main()` insertion points, the `set -euo pipefail` `rc=0; … | tee … || rc=$?` rule, the `BE_RUN_STARTED` guard, `_runs_str`, prune-failures-are-failures), §7.1.4 (rclone/restic stat parsers). **This is the single trickiest task; the spec's bash is verbatim — transcribe it, do not paraphrase.**

**Files:**
- Create: `scripts/lib/runs.sh`
- Modify: `scripts/backup-job.sh` (the `main()` and the three `_run_*` hooks), `scripts/lib/common.sh` (`die` records `_BE_LAST_ERR`; `acquire_lock` → `flock -w 5`)
- Test: `tests/bats/runs.bats` (new), `tests/bats/backup-job.bats` (additions)

**Interfaces:**
- Produces: the JSONL record shape at `$CACHE_DIR/state/<job>.runs.jsonl` (fields per §7.1.1), per-run log at `$CACHE_DIR/logs/runs/<job>/<id>.log`, and the functions `runs_start`, `runs_end`, `points`-agnostic helpers, `_runs_str`. Consumed by `app/engine/runs.py` (Task 3) and `restore.sh` (Task 6).

- [ ] **Step 1: Write `tests/bats/runs.bats`** — cases from §10 test plan: a start line then an ok end line json-parses (validate with `python3 -c` in the test only, never in the runner) and carries `snapshot_id == a81f3c2e` for a versioned run; `_runs_str` empties to `null` and quotes a value; escaping of a message with quotes/newlines; rotation keeps the last N; appending under `set -u` returns 0.
- [ ] **Step 2: Run — expect FAIL** (`runs.sh` absent).
- [ ] **Step 3: Write `scripts/lib/runs.sh`** transcribing §7.1.2–§7.1.4 verbatim (writer, log path, rotation, `_rclone_stat_bytes` with the unit-required grep, restic summary parse).
- [ ] **Step 4: Rewrite `backup-job.sh main()` and hooks** per §7.1.5 verbatim: lock→`runs_start`→`tee`→validate→dispatch→`runs_end ok … $(_runs_str …)`→`_write_state`→points→notify; every tool call as `rc=0; <tool> … | tee … || rc=$?`; prune failure ends the run `failed` with `phase:"prune"`, `copied:true` (§7.1.6). Update `common.sh` `die`/`acquire_lock`.
- [ ] **Step 5: Add `backup-job.bats` cases** — start+end lines present, stats parsed from a faked rclone/restic, rclone argv no longer has `--stats-one-line`, prune AccessDenied yields the `failed`/`phase:"prune"` record (the manga example).
- [ ] **Step 6: Run `bats tests/bats/runs.bats tests/bats/backup-job.bats` + full pytest — green.**
- [ ] **Step 7: Commit** `feat(runner): append-only run records; honest prune failures; 5s lock wait`.

---

### Task 3: Run reader + cron + error classes + boot

**Spec:** §7.1 (reader/fold/reconcile/backfill), §7.2 (RUNNING/`is_locked`/`reconcile`/`active_run`), §7.3 (`crontab_stale`, SIGUSR2, `next_after`, `describe`, TZ, the 15-min OVERDUE grace, `overdue()` reference instant), §7.4 (error classes + `classify`), the `runs boot` CLI and `entrypoint.sh` changes.

**Files:**
- Create: `app/engine/runs.py`, `app/engine/cron.py`, `app/engine/errors.py`
- Modify: `scripts/entrypoint.sh` (`runs boot`, `supercronic … -inotify`, `supercronic.pid`, dir creation)
- Test: `tests/engine/test_runs.py`, `tests/engine/test_cron.py`, `tests/engine/test_errors.py`, `tests/bats/entrypoint.bats` (additions)

**Interfaces:**
- Produces: `runs.read_runs(cache_dir, job, reconcile=False)`, `runs.RunRecord`, `runs.is_locked`, `runs.active_run`, `runs.reconcile`, `runs.backfill`, `runs.boot` (exact signatures §7.1/§7.2); `cron.next_after(expr, now, tz)`, `cron.describe(expr)`, `cron.local_tz()`; `errors.ErrorClass`, `errors.classify(error, exit_code, outcome, log_tail)`. Consumed by `status.py` (Task 4) and every screen.

- [ ] **Step 1: Write `test_cron.py`** — `next_after` for `0 5 * * *`, `0 4 * * 0`, `*/30 * * * *`; `describe` → `Sunday`/`day`/`Monday and Thursday`; DST-transition vectors; unknown-TZ falls back to UTC with one WARN (§7.3 `local_tz`).
- [ ] **Step 2: Run — FAIL. Step 3: Write `app/engine/cron.py`** per §7.3.
- [ ] **Step 4: Write `test_errors.py`** — each row of the §7.4 table: `AccessDenied … DeleteObjectVersion` → `iam-version-perms` (blocker, `board` with `{dow}`/`{since}`); `repository is already locked` → `repo-locked`; passphrase; cold-object; unknown falls back to `cause`. **Step 5: Write `app/engine/errors.py`** verbatim from §7.4.
- [ ] **Step 6: Write `test_runs.py`** — parse the Task-2 JSONL; fold rules; `is_locked`; `reconcile` (a `running` record with a free lock → one `aborted` line, `force` at boot folds all); `backfill` of a lone legacy state file into a `…-0000` record; `read_runs(reconcile=True)`.
- [ ] **Step 7: Write `app/engine/runs.py`** per §7.1/§7.2; add `runs boot` CLI. **Step 8: `entrypoint.sh`** — `runs boot`, `-inotify`, pid file, `locks`/`logs` dirs; add `entrypoint.bats` cases.
- [ ] **Step 9: Run all new tests + full suite — green. Step 10: Commit** `feat(engine): run reader, cron/overdue, error classes, boot reconcile`.

---

### Task 4: `jobs_io` growth + `status.py`

**Spec:** §7.7 (`status` derivation, verdict, needs-you, strip, median, streak, `crontab_stale`), §7.8 (`jobs_io` new fields/functions), §4.5 (severity), §4.6 (provenance/delta), §4.7 (state vocabulary + precedence Running>Paused>Overdue>Failed>OK>Not-run-yet), §8.1/§8.2 (`/status.json` + `JobStatus` shapes).

**Files:**
- Modify: `app/gui/jobs_io.py` (`created_at`, `assumptions`, `measured`, `acknowledged`, `set_enabled`, `render_crontab`, cache-file delete on job delete)
- Create: `app/gui/status.py`
- Test: `tests/gui/test_jobs_io.py`, `tests/gui/test_status.py`

**Interfaces:**
- Consumes: `runs`, `cron`, `errors` (Task 3). Produces: `status.board()` / `status.job(name)` / `JobStatus` with `strip`, `ok_14`, `failed_14`, `runs_total`, `streak`, `median_s`, `state`, `verdict`, `needs_you`, `crontab_stale` (§8.1/§8.2); `jobs_io` new accessors. Consumed by every screen route.

- [ ] **Step 1: Write `test_jobs_io.py`** additions — new fields round-trip; `validate` still rejects the old invalids and the new ones (all-zero tiered keep); `set_enabled` toggles `enabled`; `render_crontab(dry_run=True)`; deleting a job removes its `state/*` and `runs.jsonl`.
- [ ] **Step 2: Run — FAIL. Step 3: Extend `jobs_io.py`** (fields added, none removed).
- [ ] **Step 4: Write `test_status.py`** — state precedence (a running lock beats overdue); OVERDUE only past the 15-min grace; `strip`/counts restricted to `kind=="backup"` (a restore record is not a strip cell); `streak`; `median_s`; verdict copy for the mixed OK+Paused case; provenance inheritance (a projection takes the weakest input mark, §4.6); `crontab_stale` true when on-disk ≠ `render_crontab(dry_run=True)`.
- [ ] **Step 5: Write `app/gui/status.py`** per §7.7/§8.1/§8.2.
- [ ] **Step 6: Run + full suite — green. Step 7: Commit** `feat(gui): job status derivation and jobs_io growth`.

---

### Task 5: Points, readiness, ops, sysop

**Spec:** §7.5.x (points), §7.6 (readiness, `WARMUP`, passphrase three-state, setup checks), §7.5.2 (`ops.launch`, sync helpers, `validate_target`), §7.7.3 + §7 sysop (`app/engine/sysop.py`), §5.12/§7.8 (`config_io.secrets_status_3` + `KEY_GROUPS`), cached `billing.json`.

**Files:**
- Create: `scripts/lib/points.sh`, `app/gui/points.py`, `app/gui/readiness.py`, `app/gui/ops.py`, `app/engine/sysop.py`
- Modify: `app/gui/config_io.py` (`secrets_status_3`, `KEY_GROUPS`)
- Test: `tests/bats/points.bats` (or an addition), `tests/gui/test_points.py`, `test_readiness.py`, `test_ops.py`, `tests/engine/test_sysop.py`, `tests/gui/test_config_io.py`

**Interfaces:**
- Produces: `points.refresh`/`points.view(job,type)`; `readiness.recovery_summary`/`setup_checks`; `ops.launch(job, kind, argv, run_id=…)` (detached, pre-assigns run id), `ops.run_sync(…, timeout)`, `ops.validate_target(path, job)`; `sysop` CLI `usage-refresh|billing-check|probe` recording under `_system`; `config_io.secrets_status_3`, `KEY_GROUPS`. Consumed by Get-data-back (Task 11), Cost (Task 12), Setup (Task 13).

- [ ] **Step 1–2:** `points.sh` + `test_points.py` — `points_refresh` writes `state/<job>.points.json` for each type; `points.view` shapes the newest-six-plus-oldest rule (§5.2) and the Plain-copy "what is there now".
- [ ] **Step 3–4:** `readiness.py` + `test_readiness.py` — five setup checks (§5.10), passphrase three-state vs the shipped example, `WARMUP` table, `recovery_summary` (§7.6).
- [ ] **Step 5–6:** `ops.py` + `test_ops.py` — `launch` pre-assigns `BE_RUN_ID` and returns immediately; `validate_target` refuses the source path and anything outside `/mnt/user/restore`; sync helpers time out; single-flight against `is_locked`.
- [ ] **Step 7–8:** `sysop.py` + `test_sysop.py` — the three operations write `_system.runs.jsonl` records; `config_io` three-state secrets + `KEY_GROUPS` + `test_config_io.py`; cached `billing.json` read path.
- [ ] **Step 9: Full suite — green. Step 10: Commit** `feat(engine/gui): restore points, readiness, op launcher, system operations`.

---

### Task 6: `restore.sh` + `vfiles` restore/thaw + the restore mount

**Spec:** §7.5.3 (`restore.sh` under `set -euo pipefail`, the same traps, record kinds, `acquire_lock` die message), §7.5.5/§7.5.6 (exact invocations, `.` scope, `list --json`, `thaw`, `download`, `test`), §7.1.8 (`vfiles.py` changes), §5.4 (thaw semantics/timing), the `/restore` mount in compose/xml/env.

**Files:**
- Modify: `scripts/restore.sh`, `app/engine/vfiles.py` (`backup()` returns totals; `restore_all()`; `list --json`; `thaw` subcommand)
- Modify: `docker-compose.yml`, `docker-compose.test.yml`, `backup-engine.xml`, `backup.env.example` (add `/mnt/user/restore` → `/restore` rw, `RESTORE_ROOT_HOST`)
- Test: `tests/bats/restore.bats` (additions), `tests/gui/test_vfiles_restore_all.py`

**Interfaces:**
- Consumes: `runs.sh` record kinds (Task 2), `ops.launch` (Task 5). Produces: `restore.sh` subcommands `list|restore|thaw|download|test` per job type each writing a run record; `vfiles.restore_all`, `vfiles.list --json`, `vfiles.thaw`. Consumed by Get-data-back (Task 11).

- [ ] **Step 1: `restore.bats` additions** — each subcommand writes a record (kind `restore`/`thaw`/`download`/`test-restore`); a poll-probe lock never skips a restore; `test` restores one file; `.` scope restores everything-as-of; cold `thaw` issues per-object warm-up.
- [ ] **Step 2: Run — FAIL. Step 3: Edit `restore.sh`** per §7.5.3/§7.5.6 (traps, kinds, die message, invocations).
- [ ] **Step 4: `test_vfiles_restore_all.py`** — `restore_all` writes into a dated target; `list --json` shape; `thaw` on a cold prefix. **Step 5: Edit `vfiles.py`** per §7.1.8.
- [ ] **Step 6: Add the `/restore` mount** to compose/xml/env with the honest comment.
- [ ] **Step 7: `bats tests/bats/restore.bats` + full pytest — green. Step 8: Commit** `feat(restore): GUI-driven restore/thaw/download/test with run records`.

---

### Task 7a: Design system — `style.css` rewrite

**Spec:** §6 (tokens §6.1, type scale §6.2, buttons §6.3, spacing, the component catalogue §6.5: job row + rail, run strip with the dim-by-position rule, blocker/warning blocks, provenance marks, verdict line, tables, sticky footer, price stamp, log viewer, state tokens), §6.6 (responsive), §2.3 (preserve the dark token set, `:focus-visible`, reduced-motion). Mockup CSS is authoritative on exact values.

**Files:**
- Modify: `app/gui/static/style.css`
- Test: none (visual); a smoke assertion may live in Task 7b.

- [ ] **Step 1: Rewrite `style.css`** to the §6 system: keep the preserved tokens (§6.1), add the mockups' additions (`--ok-bg`/`--warn-bg`/`--danger-bg`, state-token colours, run-strip cell, rail, `.sig`/`.sig-*`, `.guard`, `.price-stamp`, `.errline`, sticky footer at 44px, provenance `.p-measured`/`.p-assumed`/`.p-invoiced`/`.p-projected`), the run-strip dim rule (position ≥ 8 from the right adds `dim`; 8–14 on the Board, 8–30 on the ledger), the type weights list, `prefers-reduced-motion`, one `:focus-visible` ring, and the §6.6 responsive rules (400px gutter, ≤620px column collapses, ≤1000px rail placement per ruling R7). Pull exact hex/px from the mockup `<style>` blocks.
- [ ] **Step 2: Commit** `feat(gui): Night Shift design system in style.css` (paired with 7b if the reviewer prefers one commit; otherwise commit here).

### Task 7b: Shell — `base.html`, nav, error pages, flash categories, route renames

**Spec:** §4.1 (URL scheme + nav Board·Cost·Activity·Setup + "+ New job"), §5.14/§5.15 (topbar, work bar, notices, footer, nav current-item rule, flash table), §9 (replace every `abort()` with the error-page/flash model; flash categories + visuals), §4.4 (mono law in Jinja globals), the 301 redirects for renamed routes.

**Files:**
- Modify: `app/gui/templates/base.html`, `app/gui/routes.py` (nav, error handlers, `vocab` Jinja global, route renames + 301s)
- Create: `app/gui/templates/error.html`
- Test: `tests/gui/test_errors.py` (new), `tests/gui/test_vocabulary.py` (new, initially asserting over the shell only)

**Interfaces:**
- Consumes: `vocab` (Task 1). Produces: `base.html` blocks every screen extends; the error handler; flash categories. Consumed by Tasks 8–14.

- [ ] **Step 1: Write `test_errors.py`** — each former `abort()` site (list from §9) now renders `error.html` with the right status and copy; a 404/500 renders the themed page, not Flask's default.
- [ ] **Step 2: Write `test_vocabulary.py`** (shell scope) — `base.html` text nodes contain no `FORBIDDEN_TERMS` (case-sensitive whole-word, text nodes only, `.mono` env-keys stripped, per-page `TERM_EXEMPTIONS`).
- [ ] **Step 3: Run — FAIL. Step 4: Write `error.html`, rewrite `base.html`** (topbar, nav with the current-item rule, work bar, notices/flash, footer), register error handlers replacing `abort()`, add `vocab` to Jinja globals, add the 301 route renames.
- [ ] **Step 5: Run both + full suite — green. Step 6: Commit** `feat(gui): Night Shift shell, error pages, flash categories, one nav`.

---

### Task 8: Board — `/` + `/status.json`

**Spec:** §5.1 (the split home: status band on top, cost strip below; verdict, needs-you lane, per-job rows with the one-cell-and-strip, the cost strip from caches, the ticking clock), §8.1 (`/status.json`), §4.6 (`board_cost` provenance). "If this machine died today" block uses cached values only.

**Files:**
- Modify: `app/gui/routes.py` (the `/` handler + `/status.json`), `app/gui/templates/` (board template)
- Create: board template (or restyle the existing index)
- Test: `tests/gui/test_board_routes.py`

**Interfaces:** Consumes `status.board()` (Task 4), `estimate_io.board_cost` (added in Task 12 — until then the strip reads a cached value; **Ruling:** Task 8 renders the cost strip from `current_costs` cache and a placeholder projected figure, and Task 12 wires `board_cost`; note this in the ledger so the reviewer expects it).

- [ ] **Step 1: Write `test_board_routes.py`** — `/` renders verdict + needs-you + both example jobs (appdata OK, manga failed with the IAM blocker row) + a one-cell strip + the cost strip; `/status.json` matches §8.1; the clock element carries `refreshed N s ago`; no Cost Explorer call during render.
- [ ] **Step 2: FAIL → Step 3: Implement** `/` + `/status.json` + template per §5.1/§8.1.
- [ ] **Step 4: Run + suite — green. Step 5: Commit** `feat(gui): Board — split status/cost home`.

---

### Task 9: Job page — `/jobs/<name>`

**Spec:** §5.2 (job page: identity/health, the ledger strip, Tool detail expander, the rail, pause/resume/delete, the "What this job costs" band, the Snapshot-store-shared note) and the OK vs FAILED variants; §5.9's locked fields for the edit link; §8.2. **Get-data-back is Task 11** — here, stub its band as a link/placeholder the reviewer knows is filled in Task 11.

**Files:**
- Modify: `app/gui/routes.py` (`/jobs/<name>`, pause/resume/delete POSTs), job template
- Test: `tests/gui/test_job_page_routes.py`

**Interfaces:** Consumes `status.job()` (Task 4), `points.view` (Task 5). Produces the job page shell that Task 11 extends with the live restore band.

- [ ] **Step 1: Write `test_job_page_routes.py`** — OK job renders token/strip/ledger/needs-line/cost band; FAILED job (manga) renders the failure record + `Fix the permission →`; pause → token Paused + `Next run —`; resume; delete removes the job and its caches; unknown job → themed 404.
- [ ] **Step 2: FAIL → Step 3: Implement** per §5.2/§8.2 with the Get-data-back band stubbed.
- [ ] **Step 4: Run + suite — green. Step 5: Commit** `feat(gui): job page with ledger strip, cost band, pause/resume/delete`.

---

### Task 10: Run record + Activity

**Spec:** §5.3 (run record: live pending state — 200 `Starting…`, 2 s poll inside `PENDING_WINDOW_S`, 404 only for malformed/unknown/stale; the log tail; the Done signal), §5.5 (Activity: runs + system operations newest-first, filters, `Raw shared log →`), §8.3/§8.4, §7.7.3 (system records), the `provision` events on provisioning success paths.

**Files:**
- Modify: `app/gui/routes.py` (`/jobs/<name>/runs/<id>` + `.json` + `/log`; `/activity` + `.json`; `/activity/<run_id>` + `.json` + `/log`), templates
- Modify: `app/gui/provision.py` (emit `provision` records on success)
- Test: `tests/gui/test_run_record_routes.py`, `tests/gui/test_activity_routes.py`

- [ ] **Step 1: Write both test files** — a fresh well-formed run id → 200 pending `Starting…`, then the live record; malformed/unknown/stale → 404; `/log` tails; Activity lists a backup, a restore and a system op newest-first, filters work, provisioning writes a `provision` record.
- [ ] **Step 2: FAIL → Step 3: Implement** per §5.3/§5.5/§8.3/§8.4.
- [ ] **Step 4: Run + suite — green. Step 5: Commit** `feat(gui): run records and Activity feed`.

---

### Task 11: Get data back (restore in the GUI)

**Spec:** §5.2 "Get data back" band (the GET form that only navigates; the small POST siblings via `form=`/`formaction`; restore-point list rule; Plain-copy "what is there now"; target validation blocker; the needs-line; the sibling cold warning; the cold-tier warm-up flow; `test restore`), §5.4 (the confirmation page `/jobs/<name>/restore` — the typed-name confirm, both-speeds pricing, the single POST that starts work, the waiting state), §8.9/§8.10, ruling R11 (cold test restore).

**Files:**
- Modify: `app/gui/routes.py` (`/jobs/<name>/restore` GET confirm + POST start; `/jobs/<name>/thaw/check`; `/jobs/<name>/test-restore`; `/jobs/<name>/restore-points/refresh`), the job template's band + the confirmation template
- Test: `tests/gui/test_restore_routes.py`

**Interfaces:** Consumes `ops.launch`/`ops.validate_target` (Task 5), `restore.sh` (Task 6), `points.view` (Task 5), `estimate_io.restore_quote` (Task 12 — **Ruling:** stub the quote figure from a cached value here; Task 12 wires the real `restore_quote`; ledger it).

- [ ] **Step 1: Write `test_restore_routes.py`** — the band GET navigates to the confirm page (starts nothing); a target under the source → blocker + disabled; confirm page requires the typed job name for the POST; the POST launches an `ops` restore and 302s to the run record; cold job → warm-up flow with both speeds; `thaw/check` and `restore-points/refresh` are POST and start/cost nothing; `test-restore` on a cold job follows R11.
- [ ] **Step 2: FAIL → Step 3: Implement** per §5.2/§5.4/§8.9/§8.10.
- [ ] **Step 4: Run + suite — green. Step 5: Commit** `feat(gui): restore, thaw, download and test-restore from the job page`.

---

### Task 12: Cost workbench — `/cost`

**Spec:** §5.6 (five bands; the lever panel GET form + `formaction`/`form=` POST siblings + `<noscript>` recalculate; the scenario `cost.json` persistence; the removal of `POST /costs/billing`), §8.7 (`cost_page` contract), §4.6 (`delta_verdict` bands 15%/30%, `provenance_of`), the `keep_all`-at-0% override, the `estimate_io` adapters, sysop launches for refresh/billing.

**Files:**
- Modify: `app/gui/estimate_io.py` (add `cost_page`, `board_cost`, `job_cost_band`, `delta_verdict`, `provenance_of`, `restore_quote`, the `keep_all`-at-0% override — all calling the frozen model), `app/gui/routes.py` (`/cost` + `/cost.json`, `POST /costs/scenario`, `/estimate`→`/cost` 301, remove `POST /costs/billing`), cost template
- Test: update `tests/gui/test_estimate_routes.py`, `tests/gui/test_costs_routes.py` (per §10.2), `tests/gui/test_estimate_io.py`; add `tests/engine/test_sysop.py` moves; wire `board_cost`/`restore_quote` into Tasks 8/11.

- [ ] **Step 1: Update/write the estimate_io tests** for the new adapters (no change to frozen math; the guard from Task 1 must stay green); `delta_verdict` band edges; `keep_all` at 0% change.
- [ ] **Step 2: FAIL → Step 3: Add the adapters to `estimate_io.py`.**
- [ ] **Step 4: Write/adjust `test_costs_routes.py`** — `/cost` bands from caches; scrubber matches `projection.primary.months[m-1]`; lever GET form + `<noscript>` recalculate renders server-side with JS off; `POST /costs/scenario` writes `$CONFIG_DIR/cost.json` and 302s `Saved.`; `POST /costs/billing` gone (301 to `/setup/keys#billing`).
- [ ] **Step 5: Implement `/cost`** + wire `board_cost` into Task 8's strip and `restore_quote` into Task 11's figures (remove the earlier stubs; update those two tests).
- [ ] **Step 6: Run + suite — green. Step 7: Commit** `feat(gui): cost workbench with what-if levers and honest provenance`.

---

### Task 13: Setup, Destination, Keys, About

**Spec:** §5.10 (Setup readiness — five checks + the informational `crontab_stale` row), §5.11 (Destination — the preserved three-path picker restyled; the IAM template + tofu policy gain `s3:GetBucketVersioning`; step-5 console copy), §5.12 (Keys & secrets — grouped three-state, the Billing anchor), §5.13 (About = the glossary). Preserve the provisioning safety model.

**Files:**
- Modify: `app/gui/routes.py` (setup/destination/keys/about handlers), the four templates, `provisioning/iam-policy.json.tmpl`, the OpenTofu policy
- Test: update `tests/gui/test_provision_routes.py`, `test_config_routes.py`, `test_about_routes.py`, `tests/gui/test_app.py`; add `tests/*/test_iam_policy.py` assertion for the new action

- [ ] **Step 1: Update the four route tests** — `/setup` five checks + the `crontab_stale` info row wording; Destination keeps every asserted string (`Guided`/`Scripted`/`Automated`/`Destination set`/…) and `Where backups go` replaces `First-time setup`; Keys groups + three-state + Billing anchor; About renders the glossary terms; the IAM template contains `s3:GetBucketVersioning`.
- [ ] **Step 2: FAIL → Step 3: Implement** the restyles + policy change (keep provisioning safety intact).
- [ ] **Step 4: Run + suite — green. Step 5: Commit** `feat(gui): setup readiness, destination, keys, glossary — restyled and honest`.

---

### Task 14: Create/edit job — `/jobs/new`, `/jobs/<name>/edit`

**Spec:** §5.8 (create job — the numbered sections; the RECOMMENDED block that prints the fired rule; the storage-class table priced for the measured folder with cold rows struck through; churn asked above retention; retention kept at 0% churn with its justification; WHEN × WHOSE never mixed; the sticky footer; the bundled-vs-live price stamp; blockers with logged override), §5.9 (edit — locked fields after creation), §7.9 (`wizard_estimate` extension, `recommend_type`, `warnings`), §8.6/§8.10 (contracts), `job_save` re-render + blocker enforcement.

**Files:**
- Modify: `app/gui/estimate_io.py` (`wizard_estimate` extension, `recommend_type`, `warnings` — still only reading the frozen model), `app/gui/routes.py` (`/jobs/new`, `/jobs/<name>/edit`, `POST /jobs`, `/jobs/estimate.json`, `/jobs/source-size`, `/jobs/browse`), the create/edit template, `app/gui/static/app.js` (the wizard JS: live recompute, radios, WHEN/WHOSE, sticky footer, price stamp, blockers)
- Test: update `tests/gui/test_jobs_routes.py`, `test_jobs_estimate_routes.py`; add `tests/gui/test_job_form_routes.py`

**Interfaces:** Consumes `vocab` (Task 1), `estimate_io` (Task 12), the folder browser + schedule builder + `data-when-*` (preserved). Produces the wizard.

- [ ] **Step 1: Update/write the estimate tests** — `recommend_type` fires rule 2/3 on the manga shape (avg < 10 MB, §5.8 §3.1 with the R-adjusted threshold) on a fresh untouched form; `wizard_estimate` returns the storage-class table rows, the WHEN×WHOSE matrix, milestones, explain, the recommended rule, the price stamp; `warnings` for cold-class-on-many-small-files.
- [ ] **Step 2: FAIL → Step 3: Extend `estimate_io.py`** (frozen guard stays green).
- [ ] **Step 4: Write `test_job_form_routes.py`** — `/jobs/new` renders the numbered sections; a measured folder un-dims and prints a recommendation; picking `DEEP_ARCHIVE` on a Snapshot backup trips a WON'T RUN blocker and disables the footer; `Use Instant, cheaper` clears it; `POST /jobs` re-renders with the blocker enforced and saves only on a clean form; `/jobs/<name>/edit` locks the kind/prefix fields.
- [ ] **Step 5: Implement** the routes, template and wizard JS per §5.8/§5.9/§7.9/§8.6.
- [ ] **Step 6: Run + suite — green. Step 7: Commit** `feat(gui): create/edit job wizard with cost consequences beside each control`.

---

### Task 15: Polish — full vocabulary test, responsive, reduced-motion, docs

**Spec:** §11 step 15, §10.3 (full vocabulary/mono-law coverage), §6.6 (400px), §2.3 (reduced-motion), the README "Restore runbook" and `backup.env.example` notes.

**Files:**
- Modify: `tests/gui/test_vocabulary.py` (extend to every page), `app/gui/templates/*` + `style.css` (responsive/reduced-motion fixes surfaced by the pass), `README.md`, `backup.env.example`
- Test: `tests/gui/test_vocabulary.py` (full)

- [ ] **Step 1: Extend `test_vocabulary.py`** to render every page (Board, job, run, activity, cost, setup family, create/edit) and assert the mono law + no forbidden terms with the per-page exemptions. Run — fix any real violations in the templates.
- [ ] **Step 2: Responsive pass at 400px** and a `prefers-reduced-motion` pass; fix issues in `style.css`/templates. **Step 3: Update README** ("Restore runbook": the GUI as primary path, `.` scope, `list --json`, `thaw`, `test`, the lock, the mount) and `backup.env.example` knobs.
- [ ] **Step 4: Full suite (pytest + all bats) — green. Step 5: Commit** `feat(gui): full vocabulary coverage, responsive + reduced-motion polish, restore runbook docs`.

---

## Self-Review

Run after writing (checklist, not a dispatch):

1. **Spec coverage:** §4–§10 each map to a task above (4→7b/8/9; 5→8–14; 6→7a/7b; 7→2–6; 8→4/8–14; 9→7b; 10→every task's tests + 15; Appendix B rulings ride inside the cited sections). §11's 15 increments map 1:1 (7 split into 7a/7b). No section is unclaimed.
2. **Placeholder scan:** the plan defers verbatim copy/contracts/code to the committed spec sections it cites (the spec is the authority, per the header). Two deliberate forward-references — `board_cost` (Task 8→12) and `restore_quote` (Task 11→12) — are called out with a **Ruling** to stub-then-wire; the reviewer is told to expect them.
3. **Type consistency:** producer/consumer names match across tasks (`status.board`/`status.job`, `runs.read_runs`, `cron.next_after`, `errors.classify`, `ops.launch`, `estimate_io.cost_page`/`board_cost`/`restore_quote`).

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-15-ui-redesign.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration. Mechanical implementers on Opus (per the usage budget), reviewers scaled to each diff.

**2. Inline Execution** — execute tasks in this session with checkpoints.

**Which approach?**
