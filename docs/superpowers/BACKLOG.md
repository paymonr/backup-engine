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

## 3. One-bucket-per-job-type architecture  — DESIGN PAUSED

**What:** move from one shared bucket (prefixes `appdata/`, `media/`) to one bucket
per job type, to use AWS built-in controls (Object Lock/WORM, versioning, lifecycle)
per type instead of hand-rolling them per prefix.

**Status:** classified architectural; brainstorming started, then paused for the
create-job bug, the progress monitor, and the resilience discussion. Open design
questions captured earlier: per-type (not per-job) to preserve restic shared-repo
dedup; Object Lock scope (v1 vs follow-on); versioning defaults per type; IAM
granularity; restic repo derivation per bucket; migration (clean slate — no real
data yet); cost-model adapters (per-bucket vs per-prefix); the OpenTofu rewrite.

---

## Smaller deferred notes
- **Job-delete UX gap:** deleting a job leaves its S3 data (by design). Add a clear
  warning on delete, and optionally a separate "delete this job's data" action.
- **Recovery-passphrase nudge:** `RESTIC_PASSWORD` lives in the container's
  `secrets.env`; the backup is unrecoverable off-box without it. Prompt the user to
  store a copy safely.
