# HANDOFF — where backup-engine stands (read this first on a new machine)

**Purpose:** let a fresh Claude session — on another computer, with none of the previous machine's
auto-memory or scratch files — pick up exactly where work stopped. Read top to bottom.

_Last updated: 2026-10-05. This repo is PUBLIC: never commit AWS account IDs, bucket names, keys or the
box's address here._

---

## Current state

- **plain-words + Ledger redesign complete on branch `plain-words-ledger`, awaiting owner merge/deploy.**
  Full rewrite of visible copy to plain words (no `prune`/`retention`/`restic`/`OpenTofu` on screen) plus
  the "Linen & wine" Ledger visual pass (bundled fonts, light+dark stylesheet, sidebar shell) across Board,
  Job, Job form, Cost, Explore, Restore, Activity/run record and Setup. Built via subagent-driven development
  (10 tasks), each task reviewed, whole-branch review done; fix wave applied (see ledger rulings in the final
  message). Tag `ui-before-plain-words` marks the commit
  immediately before this redesign started, for comparison/rollback reference.
  - **Suite:** `python3 -m pytest -q` → **2028 passed**; the `vocab.DAILY_TEMPLATES` hint-count check (every
    template's `hint_count` via `tests/gui/test_vocabulary.hint_count`) → every template ≤ 3 (board.html 0,
    job.html 2, activity.html 0, run_record.html 1, explore.html 2, explore_index.html 0, restore.html 1,
    cost.html 1, job_form.html 0); `bats tests/bats/` → 143 passed, 0 failures; `shellcheck setup.sh
    scripts/*.sh scripts/lib/*.sh tests/smoke/run.sh tools/unraid/*.sh` → unchanged from before this branch
    (no shell file touched in this redesign; the two pre-existing SC2029 info notes in
    `tools/unraid/smoke-build.sh` are untouched); `(cd opentofu && tofu fmt -check)` → clean.
  - No browser (chromium/chromium-browser/google-chrome) is available on this machine, so the planned
    screenshots at three widths were skipped; the owner reviews the look (including dark theme, which no
    headless Chromium flag renders reliably across versions) on the box after deploy.
  - Merge, push and deploy from here are the **owner's calls** — this branch is not merged or deployed yet.
- **`master` = `origin/master` = what's deployed** on the owner's Unraid box (pre-redesign state). Nothing
  else in flight beyond `plain-words-ledger` above.
- **S3 rules** (spec `specs/2026-09-23-s3-rules-design.md`, plan `plans/2026-09-23-s3-rules.md`, decision log
  `specs/2026-09-23-s3-rules-rulings.md`) is **complete, merged, deployed and live**:
  - Phases A (engine + safety), B (keeps-less gate, previews + typed confirmation, storage summaries,
    `/setup/storage` screen + side editor, versioning intent + tamper, wizard preview) and C (cheaper tier)
    all built via subagent-driven development, each task reviewed, whole-branch reviews + fix waves clean.
  - A real-AWS smoke test (`tests/smoke/`) passed on the box. Run 1 found that S3 lifecycle reads are
    eventually consistent *across reads* (a stale read after a fresh one); fixed with a 15-minute settle
    window after the app's own write (see spec §2), then run 2 passed.
  - The owner updated permissions to **level 4** and pressed Check now (2026-09-24): the old OpenTofu
    `backstop-*` 30-day rules were replaced by `backup-engine:appdata/` (30-day undo window) +
    `backup-engine:housekeeping`; versioning on; no alarm; nothing waiting. The hourly `check-all`
    (crontab `17 * * * *`) is confirmed running ("in place").
  - The live box has one job (a Snapshot job over appdata); no Plain copy or dedicated-bucket jobs.
- **Sizes display** (follow-up, deployed): every size under 1 GB shows in MB (KB/B for tiny) via the shared
  `app/gui/units.py` + Jinja filters `size` / `size_gb`; JS mirrors it (`fmtBytes`/`fmtGb` in app.js).
- **2026-10-03 follow-ups (on master, NOT yet deployed — owner deploys):**
  - Storage summary is verbose: the Activity log says current files + size, old versions + size + how old the
    oldest is, delete markers, what the folder's rule removes in the next 7 days, and the listing time
    (`storage_summary.describe`/`stats`); the end record carries `stats` and the record page shows a
    "What it did" line (`RunRecord.stats`, only grows).
  - `/setup/permissions` has the "How do I create an access key?" help, as `_access_key_help.html` shared with
    Automated setup (`ak_after` / `ak_verb` wording knobs).
  - **Dedicated-bucket end-to-end test done on the box (2026-10-03):** a disabled Plain copy job with its own
    `<base>-e2e-dedicated` bucket was created through the live GUI → bucket created, versioning on,
    `backup-engine:housekeeping` applied by the S3 rules update, runtime key can list + read versioning but is
    refused every bucket-config read and DeleteBucket (as designed) → job deleted through the GUI (bucket stays,
    as designed) → the empty bucket removed via the bucket-admin role. Smoke leftovers (image + dir) removed
    from the box. Two small observations went to `BACKLOG.md`.
- **Suite:** `python3 -m pytest -q` → **1973 passed**; `bats tests/bats/` → 143, 0 failures;
  `shellcheck setup.sh scripts/*.sh scripts/lib/*.sh tests/smoke/run.sh tools/unraid/*.sh`;
  `(cd opentofu && tofu fmt -check)`.

## Next up (owner picks)

1. **Merge, push and deploy `plain-words-ledger`** — the plain-words + Ledger redesign above is complete and
   reviewed; merging to master, pushing, and running the deploy are the owner's calls.
2. Once deployed: review the dark theme and the three widths on the box's own browser (no headless Chromium
   on the dev machine to pre-screenshot it); delete the `.bak` safety copies once the owner is satisfied
   (see "Safety copies" below).
3. **Backlog:** `BACKLOG.md` (top section = S3 rules parked items; plain-words + Ledger follow-ups near the top).

Done since the last handoff: the first nightly storage summary landed 2026-09-25 and has run daily since;
smoke-test leftovers cleaned; both permissions-converge leftovers (dedicated-bucket E2E, access-key help).

## How to resume on a new machine

1. `git clone git@github.com:paymonr/backup-engine.git` (or `git pull` on an existing checkout) — `master`.
2. Run the suite (commands above). Needs python3 + pytest, bats, shellcheck, OpenTofu for `tofu fmt`.
3. For feature work use the superpowers skills (brainstorming → writing-plans → subagent-driven-development).
   SDD ledgers live in `.superpowers/` (gitignored, machine-local) — the S3 rules ledger did NOT travel;
   its rulings are exported to `specs/2026-09-23-s3-rules-rulings.md`. Trust `git log` + docs over memory.
4. Claude's auto-memory from the old machine does not exist here — the working agreements below replace it.

## Deploy and smoke test (the owner runs these; state-changing SSH/push are theirs)

- **Deploy master:** `BE_BOX=root@<box-ip> bash tools/unraid/deploy.sh` — rsyncs the checkout to the box
  (never tfstate/.terraform/.git), builds `backup-engine:cost` ON the box, recreates the `backup-engine`
  container from its own live spec, health-gates it and auto-rolls back (`backup-engine-prev` /
  `backup-engine:prev` kept). Requires branch `master` and passwordless SSH to the box.
- **Real-AWS smoke test (before shipping S3-rules engine changes):**
  `BE_BOX=root@<box-ip> bash tools/unraid/smoke-build.sh` (separate image `backup-engine:s3rules-smoke`;
  the live container is untouched), then the owner runs **in their own terminal on the box**
  `bash /root/backup-engine-smoke/tests/smoke/run.sh` — it prompts for transient admin keys (never echoed,
  stored or logged), uses scratch buckets `<base>-s3smoke-*` (a code guard refuses any write to another
  bucket), always cleans up, and writes a report under `/root/s3rules-smoke-out/`.
- `git push origin master` is run by the owner (`! git push origin master`).

## Working agreements (carried over from the old machine's memory)

- **Commit often** — small per-step commits so a reboot can't lose work; no attribution/Co-Authored-By lines.
- **Recommend and lead** on low-stakes, reversible choices; ask only for genuine owner decisions (merges,
  pushes, deploys, anything touching AWS or the box, security trade-offs).
- **Visual/taste choices:** build the real contenders as mockups and let the owner choose.
- **Usage budget:** the owner hits session caps — pass the model explicitly to every subagent; Sonnet for
  mechanical work and small reviews, Opus for delicate logic and final reviews.
- **Published app:** others install it (Unraid Community Apps) — prefer real upgrade paths over hand-fixing
  the owner's box.
- **Secrets:** keep AWS keys out of chat; never print `~/.aws/credentials`; admin keys are transient.
- **Form gotcha:** a button that "does nothing" → check `form.checkValidity()` and hidden invalid inputs.
  Number inputs follow "the server is the gate" (no client min/max/disabled; `step="any"`).
- **Stale restic lock** blocks prune (backup OK, "prune failed:") → `restic unlock` (the runner self-heals).

## Global constraints (do not violate)

- Estimator math is **frozen** (`app/estimator/`); extend cost behaviour only via `app/gui/estimate_io.py`.
- One vocabulary on the daily screens, enforced by `tests/gui/test_vocabulary.py` (no `prune`, `retention`,
  `restic`, `OpenTofu` in visible text; bare numbers under a mono-class ancestor). Engine words still appear
  in Tool detail and under Setup.
- AWS only via the `aws` CLI subprocess (never boto3); no AWS calls on any GET; every POST CSRF-checked.
- S3 rules: console rules (non-`backup-engine:` IDs) are never modified — a new destructive one is alarmed +
  notified only (owner decision 2026-09-24); nothing that keeps less history reaches S3 without the owner's
  preview + typed bucket name; a failed or killed check never blocks a backup.
- Persisted contracts (`jobs.json`, `config/storage.json`, state files, `runs.jsonl`) only grow.

## Safety copies (delete when the owner says so)

The plain-words + Ledger redesign kept a pre-redesign `.bak` copy beside every template/stylesheet it touched,
so any screen can be diffed back to its exact pre-redesign text. Tag `ui-before-plain-words` marks the commit
before the redesign started, as the other way to recover the old copies. 26 files:

- `app/gui/static/style.css.bak`
- `app/gui/templates/_access_key_help.html.bak`
- `app/gui/templates/_console_cap.html.bak`
- `app/gui/templates/_s3_editor.html.bak`
- `app/gui/templates/_s3_preview.html.bak`
- `app/gui/templates/about.html.bak`
- `app/gui/templates/activity.html.bak`
- `app/gui/templates/base.html.bak`
- `app/gui/templates/board.html.bak`
- `app/gui/templates/config.html.bak`
- `app/gui/templates/cost.html.bak`
- `app/gui/templates/error.html.bak`
- `app/gui/templates/explore.html.bak`
- `app/gui/templates/explore_index.html.bak`
- `app/gui/templates/job.html.bak`
- `app/gui/templates/job_form.html.bak`
- `app/gui/templates/jobs.html.bak`
- `app/gui/templates/permissions.html.bak`
- `app/gui/templates/provision_automated.html.bak`
- `app/gui/templates/provision_home.html.bak`
- `app/gui/templates/provision_manual.html.bak`
- `app/gui/templates/provision_scripted.html.bak`
- `app/gui/templates/restore.html.bak`
- `app/gui/templates/run_record.html.bak`
- `app/gui/templates/s3_rules.html.bak`
- `app/gui/templates/setup.html.bak`

Remove them (owner's call, once satisfied with the deployed look) with:

```
git rm app/gui/templates/*.bak app/gui/static/style.css.bak && sed -i '/^\*\*\/\*\.bak$/d;/safety copies of the pre-Ledger/d' .dockerignore
```
