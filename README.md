# backup-engine

Off-site, encrypted AWS S3 backups for an Unraid server — the off-site leg of a 3-2-1 strategy.
A single configuration-driven Docker container backs up any number of user-defined **jobs**, each
one folder chosen under a single read-only source mount. Each job is one of:

- **versioned** — via `restic` (encrypted, deduplicated, point-in-time snapshots) into one shared
  S3 repo, tag-scoped per job. Good for anything you want history/rollback on, e.g. the
  [Appdata Backup plugin](https://forums.unraid.net/topic/137710-plugin-appdatabackup/)'s archives.
- **archive** — via `rclone` into its own S3 prefix, typically to **Glacier Deep Archive**
  (cheapest cold storage) for large, mostly-static shares (comics/books/media/etc.).

Jobs are created, scheduled, and run from the GUI's Jobs screen.

Reusable and not tied to any one setup: all user-specific values live in mounted config files;
the AWS destination is provisioned either by the included OpenTofu module or by hand (see
[Provision the destination](#provision-the-destination) below).

> **Status:** the Phase-1 engine — an N-job model (versioned/archive, any number of jobs),
> per-job restore, scheduling, notifications, and AWS provisioning — is implemented, usable
> headless, and now has a [GUI](#gui) alongside it — config editor, AWS provisioning wizard, a
> Jobs screen (create/edit/run-now/status), and a live cost estimate. Still deferred: a restore
> wizard and OIDC login — see the [Roadmap](#roadmap).

## Prerequisites

- An **Unraid** server (or any Docker host — the container isn't Unraid-specific, only the CA
  template is).
- The **Appdata Backup plugin** (`appdata.backup`) installed and configured — **required**
  (this tool ships its output off-site).
- An **AWS account** (either the `opentofu/` module or the manual steps below create the bucket +
  least-privilege IAM user).

## Install

### Community Applications (Unraid)

1. In the Unraid UI, go to **Apps → Settings → look for the "backup-engine" template**, or add
   this repo's template directly: **Apps → Template Repositories**, add
   `https://github.com/paymonr/backup-engine`.
2. Install **backup-engine** from Apps (or **Docker → Add Container → select
   `backup-engine.xml`**). Review the three required paths and fix them if your setup
   differs from the defaults:
   - `Source root (ro)` → parent of everything you might back up (e.g. `/mnt/user`); create
     backup jobs for folders under it in the GUI → Jobs
   - `Config` → where `backup.env` / `secrets.env` / `jobs.json` (GUI-managed) live
   - `Cache/state` → restic cache, logs, run-state, lockfiles
3. Populate the config files (see [Configure](#configure)) **before** starting the container —
   `entrypoint.sh` validates on boot and refuses to run with missing/invalid config.
4. Start the container.

### Plain docker-compose (any Docker host)

```bash
git clone https://github.com/paymonr/backup-engine.git
cd backup-engine
cp config/backup.env.example config/backup.env
cp config/secrets.env.example config/secrets.env
chmod 600 config/secrets.env
$EDITOR config/backup.env config/secrets.env   # see Configure below

docker compose up -d
```

`docker-compose.yml` bind-mounts `/mnt/user` (parent of everything you might back up) read-only,
`./config` and `./cache` read-write, and publishes the (Phase 2) GUI port `8099` — edit the
source path to match your host. Which folders actually ship, and how (versioned vs. archive), is
chosen afterward by creating jobs in the GUI's Jobs screen, not by editing a config file.

## Provision the destination

The engine needs one S3 bucket and one least-privilege IAM user (object-only access to
`appdata/*` and `media/*` — never bucket configuration). Phase 1 ships two ways to get there;
a third, fully interactive GUI wizard is planned for a later phase.

### Mode: scripted (`setup.sh`) — recommended

Wraps the `opentofu/` module. Your AWS **admin** credentials stay on your own machine/shell —
they're never written into the container or its config.

```bash
export AWS_ACCESS_KEY_ID=...      # or AWS_PROFILE=...
export AWS_SECRET_ACCESS_KEY=...
./setup.sh my-globally-unique-bucket-name us-east-1
```

This runs `tofu apply` and prints the values to paste into `config/backup.env` and
`config/secrets.env`. See [`opentofu/README.md`](opentofu/README.md) for what the module
provisions (versioning, default SSE-S3, all-public-access blocked, lifecycle backstop) and how to
re-run it later (e.g. to rotate the runtime key).

### Mode: guided manual (no OpenTofu, no admin creds in any tool)

If you'd rather click through the AWS Console (or run the equivalent `aws` CLI commands
yourself), replicate what the module does:

1. **Create the bucket** in your chosen region. Enable **versioning**, **default encryption**
   (SSE-S3/AES-256), and **block all public access** (all four settings). Set object ownership to
   **Bucket owner enforced** (disables ACLs).
2. **Add lifecycle rules** on the `appdata/` and `media/` prefixes: expire noncurrent versions
   after 30 days, abort incomplete multipart uploads after 7 days (both match the module's
   defaults; adjust if you like).
3. **Create an IAM policy** scoped to just this bucket and just the two prefixes — object actions
   only, no bucket-configuration permissions:

   <!-- keep in sync with opentofu/main.tf data.aws_iam_policy_document.runtime -->
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ListBucketScoped",
         "Effect": "Allow",
         "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME"
       },
       {
         "Sid": "ObjectRW",
         "Effect": "Allow",
         "Action": [
           "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
           "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
           "s3:RestoreObject"
         ],
         "Resource": [
           "arn:aws:s3:::YOUR-BUCKET-NAME/appdata/*",
           "arn:aws:s3:::YOUR-BUCKET-NAME/media/*"
         ]
       }
     ]
   }
   ```

4. **Create an IAM user**, attach that policy, and create an access key for it.
5. Put the bucket name/region into `config/backup.env` and the access key into
   `config/secrets.env` (see [Configure](#configure)), then start the container and confirm a
   run succeeds — that's your end-to-end validation that the keys and permissions work.

### Mode: automated GUI wizard (planned, later phase)

A GUI flow that takes transient admin credentials, drives the OpenTofu module for you, and
discards the admin credentials afterward, writing only the runtime keys to `secrets.env`. Not
part of the Phase-1 headless engine — see the [Roadmap](#roadmap).

## Configure

Two files plus one GUI-managed file live under the `Config` path (`/config` in the container):

| File | Purpose | Committed template |
|---|---|---|
| `backup.env` | Non-secret global settings: AWS region/bucket, `SOURCE_ROOT`, rclone/notify/GUI knobs | `config/backup.env.example` |
| `secrets.env` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD` — `chmod 600`, never commit | `config/secrets.env.example` |
| `jobs.json` | GUI-managed: your backup jobs — name, type, source folder, schedule, storage class, retention/mirror | `config/jobs.json.example` (reference/hand-editing only) |

Copy `backup.env.example` and `secrets.env.example`, drop the `.example` suffix, and edit.
`jobs.json` isn't a hand-edited file to copy — create and edit jobs from the GUI's Jobs screen
(see [GUI](#gui)), which writes it for you; `config/jobs.json.example` is there for reference (or
if you'd rather hand-edit it directly — see `app/gui/jobs_io.py` for the exact schema it validates
against). Key knobs in `backup.env` (all global; everything per-job lives in `jobs.json`):

- `SOURCE_ROOT` — the single read-only mount everything is backed up from (e.g. `/mnt/user`); a
  job's `source` is a folder path relative to it (e.g. `appdata`, `movies`).
- `S3_ENDPOINT` — leave unset for AWS; set (with a `http://` or `https://` scheme) to point the
  engine at an S3-compatible backend instead (MinIO, B2, R2, etc.).
- `RCLONE_TRANSFERS` / `RCLONE_BWLIMIT` — archive-job rclone tuning (parallel transfers; optional
  bandwidth cap, e.g. `20M`).
- `APPRISE_URLS` / `NOTIFY_ON_SUCCESS` — failure notifications always fire when set; success
  notifications are opt-in.

Per-job settings — schedule, storage class, retention (restic keep-policy for versioned jobs), and
mirror mode (archive jobs) — are set per job in the GUI's Jobs create/edit wizard and stored in
`config/jobs.json`. A cold storage class (`GLACIER`/`DEEP_ARCHIVE`/`GLACIER_IR`) works fine for
archive jobs — they're plain objects. For versioned jobs it's discouraged: `restic` has to read
the repository's `config`/`keys` objects on every run, and it can't do that against a cold repo
without a thaw first — that thaw-then-run orchestration is a Phase-3 feature (see the
[Restore runbook](#restore-runbook)). Jobs write directly in their chosen class on first upload —
no Standard-then-lifecycle round-trip, so no extra transition charges.

The container validates all of this on start (and before each run) and fails fast with a specific
error — e.g. a missing source root — rather than silently skipping a backup.

## Restore runbook

Every job restores via `scripts/restore.sh <job> ...`, run inside the container
(`docker exec -it backup-engine /app/scripts/restore.sh <job> ...`) or with the same image/config
locally. The subcommand depends on the job's type (read from `config/jobs.json`); use the job's
real name, not a pipeline name — e.g. `appdata` or `movies`, the two example jobs in
[`config/jobs.json.example`](config/jobs.json.example).

### Versioned jobs (restic)

```bash
# list snapshots tagged for this job
restore.sh appdata list

# restore a snapshot (or "latest") to a target directory, then unpack the
# plugin archive from there to recover per-app data as usual
restore.sh appdata restore latest /cache/restore/appdata
```

All versioned jobs share one restic repo (`s3:<bucket>/appdata`), tag-scoped by job name, so
`list` / `restore latest` only ever see that job's own snapshots. Because they share that one
repo, schedule versioned jobs at **different minutes** — two firing the same minute collide on
the restic repo lock; one fails loudly and recovers on its next run, but staggering avoids the
churn. Keep versioned jobs on
`storage_class: STANDARD`. A cold class (`GLACIER`/`DEEP_ARCHIVE`/`GLACIER_IR`) is not usable for
a versioned job in Phase 1: restic needs to read the repository's `config`/`keys` objects for
*every* operation, including `restore.sh <job> list`, not just the final data read, so a cold
repo can't be driven through a manual thaw the way an archive job can. Automated thaw-then-restore
orchestration for versioned jobs is a Phase-3 feature.

### Archive jobs (rclone) — including the Deep Archive thaw flow

Each archive job ships to its own S3 prefix, `media/<job>/…` (e.g. a job named `movies` lands
under `media/movies/…`), so restore is scoped per job — `<prefix>` is a required, non-empty
subpath *within* that job's prefix (a subfolder name or an exact file path), not the job name
itself.

Deep Archive objects aren't readable until thawed. Two-step restore:

```bash
# 1. Request a thaw for everything under a prefix (defaults to Bulk tier —
#    cheapest, ~48h; use --tier Standard for ~12h or --tier Expedited for
#    faster-but-pricier). Add --dry-run to preview without issuing requests.
#    <prefix> is a subpath within the "movies" job's own media/movies/…
#    destination, e.g. a subfolder ("some-series") or an exact file.
restore.sh movies thaw some-series --tier Standard

# 2. Wait for the thaw window (12-48h depending on tier), then download —
#    this will only succeed for objects that have finished thawing.
restore.sh movies download some-series /cache/restore/movies
```

`thaw` walks every object under the given prefix and issues an S3 Glacier restore request
(`Days=7`) for each; `download` then does a normal `rclone copy` down once objects are back to a
readable state. Re-run `download` if it's issued too early — objects still thawing simply won't be
copyable yet.

## Cost note

Storage class is the main lever. Illustrative pricing (us-east-1, subject to change): ~2 TB of
media on **Deep Archive** runs roughly **$2/mo** in storage vs. roughly **$12/mo** on a flat,
no-tier backend — at the cost of a slow, egress-billed restore (12–48h thaw + retrieval/egress
fees, see the runbook above). Versioned jobs (e.g. appdata) should stay on Standard — they're
usually much smaller, and a cold class isn't usable there yet in Phase 1 (see the
[Configure](#configure) storage-class note and the restore runbook). Cold versioned jobs are a
Phase-3 feature. A GUI cost-estimate screen with live
what-if now ships (see [GUI](#gui)); multi-region price tables are still a later phase. The headless
`estimate` CLI below is also available.

## Cost estimator

Estimate what a given backup shape will cost on S3 before you commit to a storage class:

    python3 -m app.estimator                 # uses defaults + /config/backup.env if present
    python3 -m app.estimator --media-size-gb 4000 --media-storage-class DEEP_ARCHIVE --retrieval-tier Bulk
    python3 -m app.estimator --json           # machine-readable breakdown
    python3 -m app.estimator --assumptions    # what the model does and does not account for

It reads `AWS_REGION` and the storage classes from `backup.env` when present (flags override),
runs fully offline against a bundled, dated us-east-1 price table, and prints a per-pipeline
line-item breakdown plus monthly, first-year, and illustrative full-restore totals.

The same model backs the live **Cost estimate** screen in the [GUI](#gui): the form is prefilled
from your `backup.env` (storage classes + restic keep-policy), and the per-pipeline breakdown and
totals update as you change inputs. Retention is surfaced — the restic keep-policy drives an
effective appdata history window, and S3 noncurrent-version retention is its own input.

## GUI

A small web UI (config editor + run/status/logs + a live cost-estimate screen) ships in the container, served on `GUI_PORT`
(default 8099). Reach it at `http://<host>:8099`.

> ⚠ **No authentication.** The GUI has no login of its own — put it behind your reverse proxy /
> SSO and never expose it directly to the internet. Set `GUI_ENABLED=false` in `backup.env` to
> disable it and run scheduler-only/headless.

- **Config editor** — edits `backup.env` (regenerated from the bundled `backup.env.example`
  template). Secret fields (AWS keys, restic password) are **write-only**: they never display
  existing values; leave a field blank to keep it, fill it to overwrite.
- **Jobs** — lists every backup job, with a create/edit wizard. Each job picks one source folder
  from a confined browser over `SOURCE_ROOT` (the single read-only mount, e.g. `/mnt/user` —
  mounted once, so the container can see everything without a mount per folder), an intent
  (**versioned**, via restic, or **archive**, via rclone), a cron schedule, and — depending on
  type — restic retention (keep last/daily/weekly/monthly) or archive mirror mode. Saving writes
  `config/jobs.json`. Each job's row also has **Run now** (trigger it immediately) and its
  last-run outcome; **Edit**/**Delete** manage the job (see [Configure](#configure) and the
  [Restore runbook](#restore-runbook) for the resulting `appdata` (shared repo) /
  `media/<job>/…` S3 layout).

> **First run:** copy *both* example files — `backup.env.example` **and** `secrets.env.example` —
> into `/config` before starting the container. The engine's `entrypoint.sh` (`prepare()` /
> `load_config`) exits at startup if either `backup.env` or `secrets.env` is missing, so the GUI
> never gets a chance to boot without them.

Prefer to manage secrets by hand? Create the file directly instead of using the form:

    cp config/secrets.env.example /config/secrets.env
    chmod 600 /config/secrets.env
    $EDITOR /config/secrets.env   # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, RESTIC_PASSWORD

Set `GUI_SECRET_KEY` in the environment to a stable random value (e.g.
`python3 -c "import secrets; print(secrets.token_hex(32))"`) so sessions/CSRF tokens survive
container restarts — without it, a new key is generated per process, which invalidates any
in-flight session/CSRF token on every restart.

### Provisioning wizard

The GUI can set up the AWS destination three ways (**Provision** in the nav):

- **Guided-manual** — no admin credentials. Enter your bucket + region and the wizard
  renders the exact least-privilege IAM policy (from the canonical
  `provisioning/iam-policy.json.tmpl`) plus the console/CLI steps. After you create the
  key, **Test & Validate** performs a real list→put→get→delete against the bucket and only
  then saves the runtime key (write-only).
- **Scripted** — a panel showing `./setup.sh <bucket> <region>`; admin credentials stay in
  your own shell and never reach the container.
- **Automated** — paste **transient** admin credentials; the wizard runs the bundled
  OpenTofu module once, reads the runtime key from `tofu output`, saves it, and discards the
  admin credentials. It is a one-shot create — teardown/update stay in `setup.sh`.

The runtime IAM policy is defined **once** in `provisioning/iam-policy.json.tmpl` and rendered
into both the OpenTofu module and the GUI, so there is no drift. As always, keep this GUI
behind a reverse proxy / SSO — the automated mode handles admin credentials, so never expose
it directly.

## Roadmap

Planned, not yet built:

- **Restore wizard** — guided per-job restore in the GUI (incl. the Glacier/Deep Archive thaw flow).
- **OIDC authentication** — native OpenID Connect login, so the GUI can stand on its own without an external proxy.
- **Per-run history** — a persisted run history beyond the last-run state.
- **Scheduler liveness / health endpoint** — surface whether the background scheduler (supercronic) is still running, so a silent crash is visible in the GUI.

## Development

- `shellcheck scripts/*.sh scripts/lib/*.sh setup.sh` — lint.
- `bats tests/bats/` — unit tests.
- `bats tests/integration/` — integration tests against a standalone MinIO binary.
- `cd opentofu && tofu fmt -check && tofu validate` — module lint/validate.
- `docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from backup-engine` —
  containerized smoke run against Dockerized MinIO.
- `python3 -m pytest tests/estimator/` — cost-estimator unit tests.
- `python3 -m pytest tests/gui/` — GUI unit tests (Flask test client).

All of the above run in CI on every push/PR (`.github/workflows/ci.yml`).
