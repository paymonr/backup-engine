# unraid-s3-backup — Design Spec

**Date:** 2026-06-22
**Status:** Approved
**Project:** A reusable, configuration-driven Docker tool that ships an Unraid server's
backups **off-site to AWS S3**. Purpose-built for Unraid; nothing hardcoded to one user's
setup. Distributed as a Community Applications template + GHCR image, with an OpenTofu module
that provisions the AWS destination.

---

## 1. Overview & role

This tool is the **off-site leg of a 3-2-1 backup strategy**, not a primary backup engine:

- **Local tier (already exists, a prerequisite):** the Unraid **Appdata Backup** plugin
  (`appdata.backup`) produces consistent, verified, compressed appdata archives locally by
  stopping each container, archiving its appdata, verifying, and restarting it.
- **Off-site tier (this tool):** ships an **encrypted** copy of (a) those appdata archives and
  (b) selected large, mostly-static media collections to AWS S3, at low cost.

Two independent jobs run on a schedule inside one container:

| Job | Source | Tool | S3 storage class | Cadence | Restore profile |
|-----|--------|------|------------------|---------|-----------------|
| **appdata** | Appdata Backup plugin's archive dir | `restic` | Standard | nightly (ships when a new archive appears) | instant, point-in-time snapshots |
| **media** | curated media dirs (comics/books/light novels) | `rclone` | `DEEP_ARCHIVE` | weekly delta (after a one-time bulk ingest) | cheap, slow (12–48h thaw) |

### Goals

- **Reusable / not tied to one setup.** All user-specific values live in mounted config files
  with `.example` placeholders. An adopter changes config only — never code.
- **Unraid-native.** Ships as a CA template; integrates with the Appdata Backup plugin; sane
  Unraid paths as defaults.
- **Minimal secret surface.** The only secrets the tool ever holds are **AWS keys + the restic
  password**. It never holds database credentials.
- **Safe by construction.** Source mounts are read-only; least-privilege IAM; bucket versioning;
  client-side encryption for appdata.
- **Turnkey updates.** Pinned tool versions in the image; update via Unraid's normal container
  update flow; config lives outside the image so updates/rollbacks never touch settings.

### Non-goals

- Not a replacement for the Appdata Backup plugin (that remains the local tier and is required).
- Does not back up the bulk re-downloadable media library (movies/TV/anime) — too large to be
  cost-effective off-site.
- Does not orchestrate databases or hold DB credentials (the plugin handles DB consistency by
  stopping containers).

---

## 2. Backup scope

Scope is **fully configurable**; these are the shipped defaults / the reference deployment.

**Appdata (via the plugin's output):** the entire Appdata Backup plugin destination directory
(default `/mnt/appdata_system/appdata_backups/`). Because the plugin already curates, stops,
archives, and verifies, this tool treats those archives as opaque inputs. DB consistency
(e.g. PostgreSQL) is inherited from the plugin's stop→archive→start mechanism — no dumps, no
credentials here.

**Media (curated, hard-to-reacquire only):** include-list of directories under the media root.
Reference deployment includes:

- `comics` (Suwayomi/Tachidesk + Komga library) — the large one (~1.3 TB at design time)
- `books` (Calibre library)
- `lightnovels` (lncrawl downloads)

Explicitly **excluded** from the reference deployment (re-downloadable / not chosen): movies, tv,
anime, music, tiktoks, and any general data shares. Adopters edit the include-list to change this.

---

## 3. Architecture

```
┌──────────────────────────── unraid-s3-backup container ────────────────────────────┐
│  entrypoint.sh: load config + secrets, render rclone.conf, validate, install crons  │
│  supercronic ──┬─ APPDATA_SCHEDULE → backup-appdata.sh → restic → s3://bucket/appdata│
│                └─ MEDIA_SCHEDULE   → backup-media.sh   → rclone → s3://bucket/media  │
│  lib/common.sh: logging, locking, notifications (Apprise), version banner            │
│  restore.sh: guided restore for both tiers                                           │
└──────────────────────────────────────────────────────────────────────────────────┘
   mounts (read-only):                            destination:
   - plugin archive dir  → /backup/appdata:ro     AWS S3 bucket (provisioned by opentofu/)
   - media root          → /backup/media:ro       prefixes: appdata/  media/
   mounts (read-write):
   - config dir          → /config   (backup.env, includes-media.txt, secrets.env, rclone.conf)
   - state/cache         → /cache    (restic cache, logs, lockfiles)
```

- **Internal scheduler (supercronic)** keeps the tool self-contained — no dependency on the
  Unraid User Scripts plugin. Schedules come from config (cron expressions).
- **Read-only source mounts** guarantee the tool can never mutate source data.
- **Config + secrets are mounted, never baked into the image** — so published images carry
  nothing user-specific, and updates/rollbacks preserve settings.

---

## 4. Repository layout

One repository (not split). The OpenTofu module lives in a subdirectory and is independently
usable via a git source; split into a dedicated Terraform-Registry repo only if that is ever
desired (cheap later via `git filter-repo`).

```
unraid-s3-backup/
  Dockerfile                  # pinned RESTIC_VERSION / RCLONE_VERSION, checksum-verified
  docker-compose.yml          # local/Dockhand deploy
  my-unraid-s3-backup.xml     # Community Applications template (turnkey installer)
  scripts/
    entrypoint.sh             # load config/secrets, render rclone.conf, validate, install crons
    backup-appdata.sh         # restic pipeline
    backup-media.sh           # rclone pipeline
    restore.sh                # guided restore (both tiers)
    lib/common.sh             # logging, locking, notify (Apprise), version banner
  config/
    backup.env.example        # all tunables, placeholders only
    includes-media.txt.example# media dirs to include/exclude
    secrets.env.example       # AWS keys + restic password (placeholders)
  opentofu/                   # reusable, account-agnostic IaC module
    main.tf  variables.tf  outputs.tf  versions.tf  README.md
  docs/
    superpowers/specs/        # this design
    superpowers/plans/        # implementation plan
  README.md                   # adopter setup + restore runbook; lists prerequisites
  .gitignore
```

---

## 5. Appdata pipeline (`restic`)

**Source:** the plugin's archive directory (read-only mount).
**Repository:** `s3:s3.<region>.amazonaws.com/<bucket>/appdata`, Standard storage class.

**Per run (`backup-appdata.sh`):**

1. Validate the source dir exists and is non-empty; if not, **fail fast** with a message naming
   the Appdata Backup plugin as the missing prerequisite (and notify).
2. `restic backup` the mounted archive dir.
3. `restic forget --prune` per the retention policy.
4. Weekly: `restic check` (integrity); periodic `--read-data-subset` for bit-rot sampling.

**Why restic here (even though inputs are already-compressed archives):**

- **Client-side encryption** — appdata archives contain secrets (API keys, tokens). restic
  encrypts before upload, so plaintext never reaches S3 (defense in depth over SSE-S3).
- **Dedup** — between plugin runs the archive dir is byte-identical, so nightly restic snapshots
  add ~zero bytes. The repo grows by roughly one archive generation per plugin run.
- **Independent off-site retention** — keep more history off-site than the plugin's local 7 days.

**Retention (default):** `--keep-last 3 --keep-daily 7 --keep-weekly 4 --keep-monthly 6`
(~6 months off-site). Configurable.

**Effective RPO:** the plugin's cadence (weekly by default). Nightly restic runs re-ship only when
a new plugin archive exists.

**Restore:** `restore.sh` lists snapshots and restores a chosen snapshot to a target dir;
the operator then unpacks the plugin archive and restores per-app as usual.

---

## 6. Media pipeline (`rclone`)

**Source:** media include-list (read-only mount). **Destination:** `<bucket>/media`,
`--s3-storage-class DEEP_ARCHIVE`, SSE-S3.

**Delete-safety = additive.** Uses `rclone copy` (not `sync`): new/changed files are uploaded;
files removed from the NAS are **never** deleted from S3. Combined with bucket versioning, a NAS
wipe or ransomware cannot erase the off-site copy. (`MEDIA_MIRROR=true` opts into `rclone sync`
for those who want an exact mirror; versioning still applies.)

**Incremental detection** is by size + modtime via cheap metadata `HEAD`s — works on Deep Archive
without thawing object bodies.

**Initial ingest:** the first run uploads the full curated set (~1.3 TB in the reference
deployment) — a one-time, multi-day operation documented as a higher-throughput command
(tuned `--transfers`, optional `--bwlimit`). Subsequent scheduled runs upload only deltas.

**Integrity caveat:** Deep Archive object bodies cannot be read without a thaw, so `rclone check`
can only verify size/metadata, not content hashes. Durability relies on S3 (11 nines) +
versioning. This is documented.

**Encryption:** SSE-S3 (AES-256, AWS-managed keys). The media library is not secret; real
filenames are preserved for easy browsing/restore. (Client-side `rclone crypt` is a documented
opt-in for adopters who want it.)

**Restore:** documented thaw→download runbook — issue a Glacier restore (bulk/standard tier),
wait 12–48h, then `rclone copy` back down.

---

## 7. AWS resources (`opentofu/` module)

Parameterized and account-agnostic (variables for name prefix, region, bucket name, retention
days). Provisions:

- **One S3 bucket** with prefixes `appdata/` and `media/`.
  - Versioning **enabled** (accidental-delete + ransomware backstop).
  - Default SSE-S3 (AES-256) encryption.
  - All public access blocked; bucket-owner-enforced ownership.
- **Lifecycle rules:**
  - `media/` — expire **noncurrent** versions after N days (default 30); abort incomplete
    multipart uploads after 7 days. (Current objects are written directly as `DEEP_ARCHIVE` by
    rclone, so no transition rule is needed for them.)
  - `appdata/` — expire noncurrent versions after N days; abort incomplete MPUs. (restic manages
    its own retention; this is a backstop.)
- **Least-privilege IAM user** scoped to **this bucket only**: `s3:ListBucket` on the bucket;
  `s3:GetObject`/`PutObject`/`DeleteObject` and multipart actions on the two prefixes; nothing else.
- **Outputs:** bucket name, region, IAM access key id + secret (sensitive), the restic repository
  URL, and rclone remote settings — fed into the container's `secrets.env`/`backup.env`.

A consumer's own root stack (e.g. an existing `infra/main`) may instantiate this module via a git
source; it is also `tofu apply`-able standalone.

---

## 8. Configuration & secrets model

All mounted; nothing user-specific in the image.

- **`config/backup.env`** — `TZ`, `LOG_LEVEL`, `APPDATA_SCHEDULE`, `MEDIA_SCHEDULE`, `AWS_REGION`,
  `S3_BUCKET`, optional `S3_ENDPOINT` (any S3-compatible store), `RESTIC_REPOSITORY`, retention
  (`KEEP_*`), `MEDIA_STORAGE_CLASS`, `MEDIA_MIRROR`, `RCLONE_TRANSFERS`, `RCLONE_BWLIMIT`,
  `APPDATA_SRC` (default `/backup/appdata`), `MEDIA_SRC` (default `/backup/media`),
  notification (`APPRISE_URLS`), optional `HEALTHCHECK_URL`.
- **`config/includes-media.txt`** — include/exclude globs selecting media subdirs.
- **`config/secrets.env`** — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD`.
  Mode `600`, gitignored, `.example` provided. The entrypoint sources it, writes
  `RESTIC_PASSWORD_FILE`, and renders `rclone.conf` from env so creds aren't duplicated.

**Secret surface = AWS keys + restic password only. No database credentials, ever.**

---

## 9. Scheduling, notifications, logging, monitoring

- **Scheduling:** `supercronic` runs the two jobs from cron expressions in `backup.env`.
- **Notifications:** **Apprise** (portable — Discord, Telegram, ntfy, email, etc.) via
  `APPRISE_URLS`; notify on failure always, success optionally. Optional Unraid host-notify if the
  adopter bind-mounts the host notify script.
- **Logging:** structured logs to stdout (visible in the Unraid Docker log) and to a logfile under
  `/cache`. Each run logs bundled tool versions, snapshot id / bytes transferred, duration, and
  outcome.
- **Concurrency:** lockfiles prevent overlapping runs of the same job.
- **Health:** weekly `restic check`; optional healthchecks.io-style ping on success/failure.

---

## 10. Packaging & updates

- **Image:** built from a small base (e.g. Alpine); `restic` + `rclone` + `supercronic` +
  `apprise` installed at **pinned versions** via build args (`RESTIC_VERSION`, `RCLONE_VERSION`)
  with checksum verification. Published to `ghcr.io/<owner>/unraid-s3-backup`, tagged semver +
  `latest`.
- **Updates:** via Unraid's normal "Check for Updates → Apply" (pulls a new pinned image,
  recreates the container). Config lives outside the image, so settings survive updates; rollback
  = pin the previous image tag. Tool versions change only on a deliberate release. Recommended:
  update deliberately (read release notes) rather than blind auto-update, because a much-newer
  restic can bump the repo format such that older binaries can't read it (newer-reads-older is
  fine; rclone writes plain S3 objects, so it is low-risk).
- **Interop with a host install:** the bundled binaries are isolated from any restic/rclone on the
  host; the shared artifacts (restic repo, rclone remote) are standard formats, so an adopter's own
  host tools can read the backups for ad-hoc restores.

---

## 11. Security considerations

- **No DB credentials** held by the tool (DB consistency comes from the plugin's container stops).
- **No Docker socket.** The tool does not mount `/var/run/docker.sock` (avoids the exposure a prior
  homelab security audit flagged).
- **Read-only source mounts** — cannot mutate source data.
- **Least-privilege IAM** scoped to the single bucket/prefixes.
- **Encryption:** appdata client-side encrypted by restic; bucket default SSE-S3; bucket versioning
  + public-access-block.
- **Secrets** in a mode-`600`, gitignored `secrets.env`; `.example` templates only in the repo.

---

## 12. Testing strategy

- **Static:** `shellcheck` on all scripts; `tofu fmt -check` + `tofu validate` + `tflint` on the
  module.
- **Integration:** spin up **MinIO** (S3-compatible) in CI; run a full restic appdata
  backup→forget→restore cycle and an rclone media copy→re-copy(delta)→restore cycle against MinIO;
  assert restored bytes match source. (Storage-class is a no-op on MinIO; the logic is what's
  validated.)
- **Config:** `bats` tests for include/exclude parsing and fail-fast validation (missing plugin
  dir, missing secrets).
- **Restore drill:** documented manual runbook in README for both tiers, including the Deep
  Archive thaw flow.

---

## 13. Prerequisites (README)

- An **Unraid** server (paths/assumptions target Unraid).
- The **Appdata Backup plugin** (`appdata.backup`) installed and configured — **required**; this
  tool ships its output and fails fast if its archive directory is absent.
- An **AWS account**; the `opentofu/` module applied to create the bucket + IAM (outputs feed the
  container config).
- Docker (the container itself).

---

## 14. Locked decisions

1. Two pipelines: `restic` (appdata) + `rclone` (media), one container, internal `supercronic`.
2. Appdata via **Model 1** — ship the Appdata Backup plugin's archives; the plugin is a required
   prerequisite. No DB handling or credentials in this tool.
3. Media tier: **additive** (`rclone copy`) + bucket versioning; `DEEP_ARCHIVE`; SSE-S3.
4. Appdata retention: `last 3 / 7 daily / 4 weekly / 6 monthly`.
5. Notifications: **Apprise** (optional Unraid host-notify).
6. One repo; IaC in `opentofu/`; image on GHCR with pinned tool versions; CA template.
7. Reference scope: media = comics + books + lightnovels (configurable).

---

## 15. Out of scope (future)

- Backing up the bulk media library (movies/tv/anime/music).
- Client-side encryption of the media tier by default (available as opt-in `rclone crypt`).
- Publishing the `opentofu/` module to the Terraform Registry (would motivate a repo split).
- A web UI / dashboard (logs + notifications are the interface).
