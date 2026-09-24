# HANDOFF — where backup-engine stands (read this first on a new machine)

**Purpose:** let a fresh Claude session — on another computer, with none of the previous machine's
auto-memory or scratch files — pick up exactly where work stopped. Read top to bottom.

_Last updated: 2026-09-24. This repo is PUBLIC: never commit AWS account IDs, bucket names, keys or the
box's address here._

---

## Current state

- **`master` = `origin/master` = what's deployed** on the owner's Unraid box. Nothing in flight, no open branches.
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
- **Suite:** `python3 -m pytest -q` → **1973 passed**; `bats tests/bats/` → 143, 0 failures;
  `shellcheck setup.sh scripts/*.sh scripts/lib/*.sh tests/smoke/run.sh tools/unraid/*.sh`;
  `(cd opentofu && tofu fmt -check)`.

## Next up (owner picks)

1. **After the next nightly backup (05:00 on the box):** confirm the first storage summary for the job's
   folder appears in Activity ("storage summary", setup group) — the last smoke-checklist item.
2. **Clean up the smoke-test leftovers on the box** (ask the owner first; state-changing):
   `docker image rm backup-engine:s3rules-smoke && rm -rf /root/backup-engine-smoke`
   (keep `/root/s3rules-smoke-out/` — it holds the pre-level-4 snapshot of the live bucket's rules).
3. **Saved from the permissions-converge feature:** (a) an end-to-end dedicated-bucket test — owner creates a
   job with its own `<base>-…` bucket, verify read-only, owner deletes the job, delete the empty bucket only
   after the owner confirms; (b) add the "How do I create an access key?" help to `/setup/permissions` as a
   shared include with Automated setup.
4. **Backlog:** `BACKLOG.md` (top section = S3 rules parked items).

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
- One vocabulary, enforced by `tests/gui/test_vocabulary.py` (no `prune`, `retention`, `restic`, `OpenTofu`
  in visible text; bare numbers under a mono-class ancestor).
- AWS only via the `aws` CLI subprocess (never boto3); no AWS calls on any GET; every POST CSRF-checked.
- S3 rules: console rules (non-`backup-engine:` IDs) are never modified — a new destructive one is alarmed +
  notified only (owner decision 2026-09-24); nothing that keeps less history reaches S3 without the owner's
  preview + typed bucket name; a failed or killed check never blocks a backup.
- Persisted contracts (`jobs.json`, `config/storage.json`, state files, `runs.jsonl`) only grow.
