# Backup resilience / state monitor — design

Status: draft for review · 2026-09-21 · owner: paymon

## Summary

Make large (2 TB) backups survive interruption without silent loss or a full re-upload.
Three capabilities: **retry a failed run** (resuming, not restarting), **auto-resume a
run interrupted by a restart/reboot/deploy** (a toggle, default on), and **pause/resume a
currently-running backup** — all surfaced in the UI with clear run states. No new daemon:
extend the pieces that already exist (`backup-job.sh`, the entrypoint's boot step,
`app/engine/runs.py`, the GUI).

**Reassuring baseline (already true):** restic, rclone, and vfiles are all incremental —
an interrupted run never loses the data it already uploaded, and re-running continues from
where it left off (restic dedups against existing packs; rclone skips transferred files;
vfiles has a durable catalog). So "resume" is, mechanically, "run the same command again."
The work here is orchestration + visibility, not a new checkpoint format.

## Goals / Non-goals

**Goals**
- Retry a transiently-failed run automatically, resuming (not restarting) — bounded.
- Auto-resume a run cut short by a restart/reboot/deploy, as a setting (default ON).
- Pause a running backup (graceful) and resume it on demand.
- Clear run states (`paused`, `retrying`, `resuming`) in Activity / board / job page + the progress bar.

**Non-goals (v1)**
- A separate checkpoint/resume-token format (the engines already resume natively).
- A true frozen/suspended process (SIGSTOP) — "pause" is a graceful stop + resume.
- Changing the frozen estimator.

## Decisions (from Q&A)

| # | Decision |
|---|----------|
| Retry (#3) | **Always-on, bounded**: on a *transient* failure, retry up to 3× with exponential backoff; each attempt re-runs the backup command, so the engine **resumes from where it left off**. *Permanent* errors (AccessDenied, bad creds, NoSuchBucket, invalid config) **fail fast + notify** — no retry. |
| Auto-resume on boot (#2) | **A setting you can turn on/off, default ON.** On container start, re-trigger any job whose last run was interrupted by the restart (outcome `aborted`, and NOT intentionally paused), gated by: setting on + schedule enabled + job lock free + a per-run resume cap (prevents boot loops). |
| Pause a running run (#1) | **Graceful stop + resume-later.** Signal the run to stop; the engine exits cleanly leaving a valid partial; record outcome **`paused`** (intentional, distinct from `failed`/`aborted`). **Resume** re-triggers the job — the engine continues via its native incremental. Not a frozen process. |
| Naming (#4) | **"Pause schedule"** = existing per-job toggle (disables *this job's* scheduled runs; unchanged). **"Pause / Resume"** = new control for a *currently-running* backup. |

## Architecture (no new daemon)

### 1. Retry-with-backoff — `scripts/backup-job.sh`
Wrap the engine invocation (restic/rclone/vfiles) in a bounded retry loop:
- Classify the failure from the engine's output: **transient** (HTTP 5xx, `RequestTimeout`,
  `SlowDown`, connection reset/EOF, `timeout`) → retry; **permanent** (`AccessDenied`,
  `InvalidAccessKeyId`, `NoSuchBucket`, signature/permission, config/validation) → fail fast.
- Up to `BE_MAX_ATTEMPTS` (default 3), sleeping `base * 2^(n-1)` (e.g. 30s, 60s, 120s) between.
- Each attempt re-runs the SAME command; restic/rclone/vfiles resume incrementally.
- Record attempts on the run (`attempts: N`) and surface a `retrying` state between attempts.
- After the last attempt fails, `_fail` as today (with the real error via the improved
  `_first_error_line`). The prune phase keeps its own error handling (now self-healing).

### 2. Pause / Resume a running run — control flag + graceful signal
- A per-job **control flag** file `state/<job>.control` (values: empty / `pause`).
- **Pause** (`POST /jobs/<name>/stop`): write `pause` to the flag and send the run's process
  group a graceful signal (`SIGINT`/`SIGTERM` — `backup-job.sh` already traps both). The engine
  finishes its current chunk and exits cleanly; the wrapper sees the flag and records outcome
  **`paused`** (not `failed`), skipping retry.
- **Resume** (`POST /jobs/<name>/resume-run`): clear the flag and re-trigger the job
  (`runner.trigger_job`) — the engine resumes via incremental.
- The single-flight lock (`acquire_lock`) already prevents overlap; resume waits for the lock.

### 3. Auto-resume on boot — entrypoint + `runs boot`
- The entrypoint already runs `python3 -m app.engine.runs boot` (reconciles dangling runs to
  `aborted`). Extend it: after reconcile, for each job whose latest run is `aborted` (interrupted
  by the restart) — **if** the auto-resume setting is on, the job's schedule is enabled, its lock
  is free, and it hasn't exceeded a resume cap — **re-trigger** it.
- An intentionally **`paused`** run is never auto-resumed (that's the user's choice).
- Resume cap: track a per-run resume count; stop after `BE_MAX_RESUMES` (default 3) so a run
  that keeps dying on boot doesn't loop forever — it lands `failed` + notifies instead.

### 4. Run states + visibility — `app/engine/runs.py` + GUI
- New outcomes/derived states: **`paused`** (intentional), and transient UI states
  **`retrying`** / **`resuming`** (derived from `attempts`/a resume marker on a still-running run).
- Surface in Activity, the home board, and the job page, and drive the live progress bar
  ("resuming — 1.4 / 2 TB already done", "retry 2/3").
- Job page gains the **"Pause / Resume"** control for a running run, alongside the existing
  **"Pause schedule"** toggle.

## Data model / config

- `jobs.json` / run records: `attempts` (int) and `paused` outcome; a resume counter for the boot cap.
- Config (`backup.env`): `AUTO_RESUME_ON_BOOT` (default `true`), `BE_MAX_ATTEMPTS` (3),
  `BE_RETRY_BASE_SECONDS` (30), `BE_MAX_RESUMES` (3) — all with sane defaults; the auto-resume
  toggle is exposed in Settings.
- `state/<job>.control` — the pause flag.

## Error handling
- Transient vs permanent classification is conservative: unknown errors are treated as
  **permanent** (fail fast) so we never loop on a real problem. The class list is tunable.
- A paused run is a clean terminal state, never an alert; a retry-exhausted run alerts as a failure.
- Boot auto-resume is best-effort and never blocks container start (it already can't).

## Testing
- `backup-job.sh` retry: bats — transient stub fails then succeeds (asserts a retry happened +
  resumed command re-run); permanent stub fails once (asserts NO retry, fail fast). Backoff sleep
  is injectable/short in tests.
- Pause: bats — the control flag + trapped signal yields outcome `paused`, not `failed`, and no retry.
- Boot auto-resume: python test of the `runs boot` path — an `aborted` latest run re-triggers when
  the setting is on + lock free + under the cap; a `paused` run does not; cap stops the loop.
- Run states: `runs.py` unit tests for `paused`/`retrying`/`resuming`; GUI route/template tests.
- Settings: a real integration test that the auto-resume toggle persists.

## Open questions / future
- Auto-resume setting is **global** in v1 (one toggle). Per-job override is a possible follow-on.
- Retry class list may need tuning against real AWS error text once observed in the wild.
- Notification wording for paused vs. retrying vs. failed-after-retries.
