# backup-engine — backlog

Open build/design items, most recent first. Each has a status so work can be
resumed after a break. Design items follow the brainstorming → spec → plan flow.

---

## 1. In-app restic data explorer  — TODO (not started)

**What:** an in-app way to *browse/inspect* what's actually stored in S3 for a
job, using restic (and the equivalent for the other engines) — snapshots → file
tree, find-by-name, and preview/stream a single file. Surfaces what today is only
reachable via the `restic` CLI inside the container.

**Why:** after a backup you can't see your files in the S3 console — restic stores
encrypted, deduplicated pack files under `appdata/data/`, so the bucket is opaque.
Right now exploring means `docker exec … restic ls/find/dump`.

**Notes / starting points:**
- restic already provides it: `restic snapshots`, `restic ls [-l] <snap> [path]`,
  `restic find <pat>`, `restic dump <snap> <path>`. `app/gui/points.py` already
  wraps `restic snapshots --tag <job>` for restore points.
- Distinct from the existing **restore** flow ("Get data back") — that's about
  *retrieving*; this is about *browsing/verifying* without restoring.
- Likely: a read-only browse endpoint (like `/jobs/<job>/browse` but over the
  repo, not the local disk) + a lazy tree UI; cache listings (restic ls can be
  slow on big snapshots). Per-engine: restic (versioned) via `ls`; versioned-files
  via its SQLite catalog; archive via S3/rclone listing (already ~browsable).

---

## 2. Backup resilience / state monitor  — DESIGN IN PROGRESS (awaiting ✅ on defaults)

**What:** survive interruptions of large (2 TB) backups — pause/resume, retry a
failed run with backoff, auto-resume after a container restart/reboot/deploy, and
clear "aborted → resuming (N already done)" visibility.

**Reassuring baseline (already true):** restic/rclone/vfiles are incremental, so an
interrupted run never loses uploaded data; a re-run resumes via dedup. The app
already marks a dead run "aborted" on boot. Gaps are orchestration + visibility,
not data safety.

**Recommended architecture (no new daemon):** extend the pieces that exist —
- `backup-job.sh`: bounded retry-with-backoff on *transient* errors; fail fast on permanent.
- Control flag `state/<job>.control` + SIGINT → clean partial → new `paused` outcome; resume = clear + re-trigger (dedup continues).
- Entrypoint `runs boot`: auto-resume runs interrupted by a restart (gated by schedule-enabled + lock-free + retry cap).
- New `paused`/`resuming` states surfaced in Activity/board/job page + the progress bar.
- Naming: keep "Pause schedule" (existing = disable schedule) vs new "Stop / Resume" (a running run).

**Status:** architecture proposed; user asked for all four capabilities. Awaiting
confirmation of the 4 defaults (pause = graceful stop+resume; auto-resume-on-boot;
retry 3×/backoff/transient-only; naming), then → spec (`docs/superpowers/specs/`)
→ writing-plans → build. **Do not build until spec approved.**

---

## 3. Optional per-job dedicated S3 buckets (just-in-time)  — BUILT (awaiting merge/deploy)

Reframed from "one-bucket-per-job-type" to **opt-in, per-job** dedicated buckets,
JIT-created at job save via STS AssumeRole (runtime key stays object-only). Object
Lock deferred; drivers = isolation/lifecycle/cost + per-bucket versioning.

**Status:** spec `docs/superpowers/specs/2026-09-19-multi-bucket-design.md`, plan
`docs/superpowers/plans/2026-09-19-multi-bucket.md`, built via SDD on branch
`ui-redesign-nightshift` (commits eb4970f..89bf72b). Whole-branch review = SHIP;
1064 tests pass (only the pre-existing unrelated billing date-test fails). NOT yet
merged to master or deployed.

**Follow-ons (deferred, not blocking):**
- **F1 — cost collection for dedicated buckets:** `app/engine/sysop.py` `usage_refresh`
  still measures only the base bucket, so the app's own cost workbench shows empty
  storage/cost for a dedicated job (AWS-console per-bucket cost still works). Make
  usage_refresh query each dedicated job's bucket and write the `appdata:<name>` /
  `media/<name>` usage-cache key that `estimate_io` already reads.
- **Object Lock / WORM:** the deferred v2 (needs the restic-prune-vs-immutability design).
- **Minor cleanups** (all triaged non-blocking at final review): uncommented
  backup.env.example keys; dead `..`/IP branches in `valid_bucket_name`; scrub
  access-key-id/session-token in AssumeRoleError too; friendly message when
  BUCKET_ADMIN_ROLE_ARN is unconfigured; first-run "repointed" flash; teardown CLI
  continue-on-error vs fail-fast.

---

## Other
- **Stale restic-lock blocks nightly prune (FIXED 2026-09-21, but app should self-heal):**
  On the live box, `appdata_backups` failed its nightly `prune` on 09-20 and 09-21 —
  backups succeeded (non-exclusive lock) but prune (exclusive lock) hit a STALE
  non-exclusive lock left by a dead PID after a container restart. Cleared with
  `restic unlock` (verified via prune --dry-run). Two app improvements worth doing:
  (a) `backup-job.sh` should `restic unlock` (stale-only) before `forget --prune`, or
  detect a stale lock and retry; (b) the recorded error was an unhelpful empty
  "prune failed: " — `_first_error_line` didn't match restic's lock message; improve
  the error extraction so the run record shows the real cause.
- **Pre-existing failing test (own fix):** `tests/estimator/test_billing.py::test_forecast_parses`
  fails today (asserts month `2026-09`, gets `2026-10`) — a hardcoded-date/clock-drift
  bug, NOT caused by any recent work (fails on older commits too). Deserves its own fix.
- **Orphaned worktree:** `.kilo/worktrees/evanescent-seashore/` holds a pre-feature
  duplicate of provision.py/routes.py — clutter, not run by the app; consider pruning.
- **Recovery-passphrase nudge:** `RESTIC_PASSWORD` lives in the container's
  `secrets.env`; the backup is unrecoverable off-box without it. Prompt the user to
  store a copy safely.
- **Job-delete data:** deleting a job leaves its S3 data (by design; a note now warns
  for dedicated-bucket jobs). A separate "delete this job's data" action is still open.
