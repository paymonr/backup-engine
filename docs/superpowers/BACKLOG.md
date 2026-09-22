# backup-engine — backlog

Open build/design items, most recent first. Each has a status so work can be
resumed after a break. Design items follow the brainstorming → spec → plan flow.

---

## Data explorer — parked minors (from the 2026-09-22 final whole-branch review)
The data-explorer feature (browse + targeted restore across all 3 backup types; top-level
Explore nav) shipped on branch `data-explorer` (fbc2638..HEAD). Final review = CHANGES-NEEDED;
the one Critical (vfiles version-picker off-by-fractional-second → wrong version) was fixed in
the final fix wave. These five Minors were parked:
- **Cold routing keys off job-level storage class** (`app/gui/routes.py` `explore_get`): the per-row
  `storage_class` form field is posted but never read; the cold decision uses `job.storage_class`.
  Harmless today (storage class is uniform per job + the vfiles engine self-thaws a cold version),
  but fragile if per-object lifecycle transitions are ever added. Read `f.get("storage_class")` then.
- **Folder rows aren't recoverable from Explore** (`explore.html` dir loop): folders are navigable but
  have no restore/download action — only file rows do. Spec's "restore a file/folder/version" is half-met.
  v2: add a folder-scope action (archive→`download <prefix>`; versioned→`restore --include <dir>`;
  vfiles→`restore_all` under the path).
- **Cold thaw from Explore has no completion path**: `thawed=1` is documented as client-set after warm-up,
  but nothing sets it (Task 9 ships HTML-swap navigation, not thaw polling). A cold "Warm up" re-thaws;
  the actual fetch is done from the job page after warm-up. v1 deferral — add thaw-status polling to Explore.
- **`list.json` is effectively dead**: the shipped app.js enhancement fetches the full `?path=` HTML and
  swaps `#explore-pane` (reusing server forms), so it never calls `list.json`; the plan's `{"building":true}`
  restic-warming state is unimplemented, and `GET /explore/<job>/list.json` for a versioned job with no
  `snapshot` param returns an error. Unused by the UI — either wire it or remove it in a cleanup.
- **T3 test file naming**: `tests/engine/test_vfiles.py` (browse CLI tests) vs the repo convention
  `test_vfiles_cli.py` — cosmetic, fold in during a cleanup pass.

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
- **Stale restic-lock blocks nightly prune — FIXED + self-heal SHIPPED (2026-09-21):**
  On the live box, `appdata_backups` failed its nightly `prune` on 09-20 and 09-21 —
  backups succeeded (non-exclusive lock) but prune (exclusive lock) hit a STALE
  non-exclusive lock left by a dead PID after a container restart. Immediate fix:
  `restic unlock` (verified via prune --dry-run). Permanent self-heal now in code:
  (a) `backup-job.sh` runs `restic unlock` (stale-only) before `forget --prune`;
  (b) `_first_error_line` (runs.sh) broadened + last-line fallback so the run record
  shows restic's real message instead of a bare "prune failed:". Covered by new
  backup-job.bats tests. See [[stale-restic-lock-blocks-prune]].
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

## Backup resilience — parked minors (from the 2026-09-21 final whole-branch review)
The feature (bounded retry-with-resume, auto-resume-on-boot toggle, pause/resume a
running run, run states) shipped on `ui-redesign-nightshift` (0bc5557..HEAD). Final
review = SHIP-WITH-NITS; the one Important finding was fixed. These four Minors were
parked (each is cosmetic/low-likelihood or a spec-acknowledged tuning item):
- **No dedicated status arm for a run-`paused` job** (`app/gui/status.py:191-202`): a
  paused latest run (lock released, schedule still enabled) shows as `NOT_RUN_YET`
  (or `OVERDUE` later). Cosmetic only — `paused` is excluded from `failed_14`, never
  routed to `errors.classify`, and the job-page Resume button keys off
  `s.last.outcome=='paused'` so resume still works. The label just misreads. Add a
  PAUSED-run state arm + board/job-page label when polishing the state machine.
- **Pause descendant-sweep also kills the run-log tee** (`scripts/backup-job.sh:287-289`):
  `_be_kill_descendants "$$"` signals the `tee` writing `$BE_RUN_LOG` too, tearing
  logging down a beat early on pause. Harmless (records go to files via `runs_end`,
  notify skipped on pause). Optionally exclude `_BE_TEE_PID` from the sweep.
- **`resume-run` doesn't wait for the paused run's lock** (`app/gui/routes.py:317-323`):
  a very fast Resume right after Pause can race the exiting run's `flock`; the resumed
  run then dies silently in `acquire_lock`. Low likelihood (lock held by `$$`, released
  on script exit). Consider a brief retry/backoff or a user-facing "still stopping" note.
- **Transient classifier matches bare substrings** (`scripts/lib/common.sh` `_is_transient_error`):
  `timeout`/`Throttl` match unanchored over the combined log, so a permanent failure
  whose verbose output merely contains "timeout" burns up to BE_MAX_ATTEMPTS retries
  (bounded — never an infinite loop). Already a spec open-question: tune the class list
  against real AWS error text once observed in the wild.
