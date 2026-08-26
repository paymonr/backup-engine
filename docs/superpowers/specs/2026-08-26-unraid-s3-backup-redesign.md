# unraid-s3-backup — Design Spec (Redesign)

**Date:** 2026-08-26
**Status:** Approved
**Supersedes:** `2026-06-22-unraid-s3-backup-design.md` (kept for history). This redesign keeps the
original's two-pipeline core and reusability goals, and adds: an **AWS-first tiered storage model**,
a **full web GUI**, an **interactive cost estimator**, a **three-mode provisioning wizard**, and
**optional media packing** — all in service of a **near-turnkey, publicly distributable** tool.

---

## 1. Overview & role

The **off-site leg of a 3-2-1 backup strategy** for an Unraid server — not a primary backup engine.

- **Local tier (prerequisite, already exists):** the Unraid **Appdata Backup** plugin
  (`appdata.backup`) stops each container, archives its appdata, verifies, and restarts it,
  producing consistent compressed archives locally.
- **Off-site tier (this tool):** ships an encrypted copy of (a) those appdata archives and (b)
  selected large, mostly-static media collections to **AWS S3**, at low cost, on a schedule.

Two independent pipelines run inside one container. The tool is **near-turnkey**: a full web GUI
handles configuration, operation, restore, cost estimation, and destination provisioning, so an
adopter never has to touch OpenTofu (or even a terminal) unless they want to. Distributed as a
Community Applications template + a public multi-arch GHCR image, **MIT-licensed**.

| Pipeline | Source | Tool | Default class | Cadence | Restore profile |
|----------|--------|------|---------------|---------|-----------------|
| **appdata** | Appdata Backup plugin's archive dir | `restic` | Standard (any class allowed) | nightly (ships when a new archive appears) | instant if warm; thaw-then-restore if cold |
| **media** | curated media dirs (comics/books/light novels) | `rclone` | `DEEP_ARCHIVE` | weekly delta after a one-time bulk ingest | cheap, slow (12–48h thaw) |

### Backend

**AWS S3 is the blessed, fully-tiered backend.** Storage class is a first-class, per-pipeline
configuration axis (Standard / Standard-IA / Glacier Flexible / Glacier Deep Archive), because the
tiering is the whole point: ~$1/TB/mo cold storage vs. flat-rate alternatives. Backblaze B2 /
Cloudflare R2 support (via `S3_ENDPOINT`) is a **documented Phase-4 TODO**, not part of the initial
guided experience. The container ships backend-agnostic engine code so this future addition is
mostly configuration and docs.

---

## 2. Goals & non-goals

### Goals

- **Near-turnkey for a public audience.** Installable from Community Applications; a full GUI drives
  config, runs, restores, cost estimation, and provisioning. No Terraform literacy required.
- **Reusable / not tied to one setup.** All user-specific values live in mounted config; adopters
  change config only, never code.
- **Cost-transparent.** A built-in estimator prices the adopter's choices (class, versioning,
  rotation, ingress/egress, packing, bulk placement) *before* they commit real money.
- **Minimal runtime secret surface.** The only secrets the running tool holds are **runtime AWS
  keys + the restic password**. Admin-level AWS credentials are only ever handled *transiently*
  during provisioning, never persisted. No database credentials, ever.
- **Safe by construction.** Read-only source mounts; least-privilege IAM scoped to one bucket;
  bucket versioning; public-access-block; client-side encryption for appdata.
- **Turnkey, controlled updates.** Pinned tool versions baked into the image; config lives outside
  the image so updates/rollbacks never touch settings.

### Non-goals

- Not a replacement for the Appdata Backup plugin (that remains the required local tier).
- Does not back up the bulk re-downloadable library (movies/TV/anime/music).
- Does not orchestrate databases or hold DB credentials (the plugin handles DB consistency).
- Not a primary/only backup — it is explicitly the off-site copy.

---

## 3. Backup scope

Scope is fully configurable; these are the shipped defaults / reference deployment.

- **Appdata:** the entire Appdata Backup plugin destination directory (default
  `/mnt/appdata_system/appdata_backups/`), treated as opaque already-consistent archives.
- **Media (curated, hard-to-reacquire only):** an include-list of directories under the media root.
  Reference deployment: `comics` (Suwayomi/Komga, the large one, ~1.3 TB), `books` (Calibre),
  `lightnovels` (lncrawl). **Excluded:** movies, tv, anime, music, and general data shares
  (re-downloadable). Adopters edit the include-list to change this.

---

## 4. Architecture & component boundaries

**Governing principle:** the GUI and `setup.sh` are **thin front-ends over the same engine and the
same IaC**. No backup or provisioning logic is duplicated between headless and GUI paths, so both
behave identically. The GUI orchestrates and displays; it shells out to the same scripts and drives
the same OpenTofu module.

```
┌──────────────────────── unraid-s3-backup container ────────────────────────┐
│ entrypoint.sh → validate prereqs, render config/rclone.conf, install crons, │
│                 launch supercronic + GUI                                     │
│                                                                             │
│ ENGINE (shell, backend-agnostic):                                           │
│   backup-appdata.sh  → restic         → s3://<bucket>/appdata                │
│   backup-media.sh    → (pack?) rclone → s3://<bucket>/media                  │
│   restore.sh         → guided restore (both tiers, incl. cold thaw)         │
│   lib/common.sh      → logging, locking, notify (Apprise), version banner   │
│                                                                             │
│ ESTIMATOR (python, pure): inputs → cost breakdown                           │
│   ├─ bundled per-region price tables (dated JSON)                           │
│   └─ optional live refresh (AWS Pricing API)                                │
│                                                                             │
│ GUI (python web app): thin front-end — orchestrates engine, reads state,    │
│   never reimplements backup logic                                           │
│   config editor · media picker · run-now · log tail · status/history ·      │
│   restore wizard · cost estimator · provisioning wizard (3 modes)           │
│                                                                             │
│ PROVISIONING: one definition (opentofu/), three drivers                     │
│   setup.sh · GUI automated (transient admin creds) · GUI guided-manual      │
└─────────────────────────────────────────────────────────────────────────────┘
   mounts (read-only):  /backup/appdata   /backup/media
   mounts (read-write): /config (backup.env, includes-media.txt, secrets.env, rclone.conf)
                        /cache  (restic cache, logs, lockfiles, run state)
   destination: AWS S3 bucket, prefixes appdata/ and media/
```

### Units and their interfaces

- **Engine scripts** — input: config/secrets + read-only mounts; output: S3 objects + structured
  logs + run-state files under `/cache`. No knowledge of the GUI. Independently runnable headless.
- **Estimator** — a pure module: inputs (sizes, counts, class, versioning, rotation, packing,
  frequencies) → a cost breakdown. No side effects except loading price tables. Consumed by both the
  GUI and a CLI `estimate` command. Independently unit-testable.
- **GUI** — orchestrates the engine (run-now, tail logs, trigger restore), reads run-state, edits the
  mounted config files, and hosts the estimator and provisioning wizard. Contains no backup logic.
- **Provisioning** — the OpenTofu module is the single definition of what gets created; `setup.sh`
  and both GUI modes are drivers over it (or, for guided-manual, render instructions from the same
  least-privilege policy source).

Each unit answers cleanly: what it does, how you use it, what it depends on. A consumer can
understand any unit without reading another's internals.

---

## 5. Storage & tier model

- **Per-pipeline storage class** is configurable. **Upfront bulk placement:** the initial ingest
  writes objects *directly* in the chosen class (e.g. straight to `DEEP_ARCHIVE`) — no Standard
  landing + lifecycle-transition round-trip, so no transition charges and immediate cold pricing.
- **Media (`rclone`): any class, including Deep Archive** — the cost superpower. Illustrative (prices
  as of spec date, us-east-1): 2 TB of cold media ≈ **~$2/mo** on Deep Archive vs. ~$12/mo flat on a
  no-tier backend. The trade is a slow, egress-billed restore (see §7).
- **Appdata (`restic`): any class, cold allowed** (chosen deliberately), **defaulting to warm
  (Standard).** Why cold is workable: restic keeps its **index cached locally in `/cache`**, so a
  scheduled `restic backup` writes new packs and updates the index **without reading cold pack
  bodies** — nightly backups keep working against a cold repo. Only `prune`, `check --read-data`, and
  `restore` need to read pack bodies and therefore require a **thaw**, which the tool orchestrates
  and the GUI/docs warn about. When a cold class is selected for appdata, the estimator reflects the
  added retrieval cost and the maintenance schedule surfaces the thaw requirement.
- **Versioning enabled** on the bucket (accidental-delete + ransomware backstop).
- **Delete-safety = additive.** Media uses `rclone copy` (never deletes from S3); `MEDIA_MIRROR=true`
  opts into `rclone sync` for those who want an exact mirror (versioning still applies).
- **Rotation / lifecycle.** restic manages appdata retention (`--keep-last 3 --keep-daily 7
  --keep-weekly 4 --keep-monthly 6`, configurable). Bucket lifecycle is a backstop: expire noncurrent
  versions after N days (default 30) and abort incomplete multipart uploads after 7 days, per prefix.

---

## 6. Appdata pipeline (`restic`)

**Source:** the plugin's archive dir (read-only mount). **Repository:**
`s3:s3.<region>.amazonaws.com/<bucket>/appdata`, configurable storage class (default Standard).

Per run (`backup-appdata.sh`):

1. Validate the source dir exists and is non-empty; if not, **fail fast** naming the Appdata Backup
   plugin as the missing prerequisite (and notify).
2. `restic backup` the mounted archive dir.
3. `restic forget --prune` per the retention policy. *(If the repo is on a cold class, prune requires
   a thaw; the tool detects this, warns, and can orchestrate a thaw-then-prune or defer.)*
4. Weekly `restic check`; periodic `--read-data-subset` bit-rot sampling *(thaw-gated when cold)*.

**Why restic even though inputs are already-compressed archives:** client-side encryption (appdata
archives contain secrets, so plaintext never reaches S3 — defense in depth over SSE-S3); dedup
(byte-identical archives between plugin runs add ~zero bytes); independent, longer off-site
retention than the plugin's local window.

**Effective RPO:** the plugin's cadence (weekly by default). Nightly restic runs re-ship only when a
new plugin archive exists.

**Restore:** `restore.sh` / GUI wizard lists snapshots and restores a chosen one to a target dir
(thawing first if cold); the operator then unpacks the plugin archive and restores per-app as usual.

---

## 7. Media pipeline (`rclone`) + optional packing

**Source:** media include-list (read-only mount). **Destination:** `<bucket>/media`, configurable
class (default `DEEP_ARCHIVE`), SSE-S3.

- **Incremental detection** by size + modtime via cheap metadata `HEAD`s — works on cold classes
  without thawing object bodies.
- **Optional packing** (configurable, default OFF):
  - **OFF (default):** upload files as-is, `.cbz`-granular — browsable/restorable per-file in S3.
    Because comics are stored as one `.cbz` per chapter, object counts stay reasonable (hundreds of
    thousands, not millions), so ingest PUT cost and per-object overhead are modest.
  - **ON (opt-in):** tar groups of chapters into large archives before cold upload — near-zero
    ingest/restore-request cost, at the price of coarser (bundle-level) restores. Packing state is an
    estimator input.
- **Initial ingest:** first run uploads the full curated set (a one-time, multi-day operation)
  documented with tuned `--transfers` / optional `--bwlimit`. Subsequent runs upload only deltas.
- **Encryption:** SSE-S3 (AES-256) by default; real filenames preserved for easy browsing. Client-side
  `rclone crypt` is a documented opt-in.
- **Integrity caveat:** cold object bodies can't be read without a thaw, so `rclone check` verifies
  size/metadata only. Durability relies on S3 (11 nines) + versioning. Documented.
- **Restore:** GUI wizard / documented runbook — issue a Glacier restore (Bulk / Standard /
  Expedited = cost/time tradeoff), wait 12–48h, then `rclone copy` down. Restore cost is
  estimator-modeled (retrieval + request + egress).

---

## 8. Cost estimator

A pure module surfaced in the GUI and as a CLI `estimate` command.

- **Inputs:** per-pipeline data size, file count / avg file size, storage class, **packing on/off**,
  versioning retention days, backup frequency + change rate, expected restore frequency + retrieval
  tier, region.
- **Models:** storage cost per class; **ingress** (PUT + request counts, which move with packing);
  **egress / restore** (retrieval + request + data-transfer-out); **versioning** overhead (retained
  noncurrent versions); **rotation / lifecycle** effects; **upfront bulk placement** (direct-to-cold
  ingest). Outputs a line-item breakdown plus monthly + first-year + illustrative full-restore totals.
- **Pricing source:** **bundled per-region tables** (dated JSON, works fully offline, no AWS creds),
  with an **optional "refresh from AWS Pricing API"** action for current numbers when online. Every
  estimate is labeled with the price-table date.
- **Boundary:** deterministic given inputs + a price table; unit-tested against fixed tables.

---

## 9. GUI (full ops)

A small Python web app (Flask/FastAPI), matching the homelab's existing custom-UI stack. Served on a
configurable port; intended to sit behind the adopter's own reverse proxy / SSO if they have one.

Screens:

- **Config editor** — reads/writes the mounted config files (single source of truth); no separate DB.
- **Media-dir picker** — browses the read-only media mount to build the include-list.
- **Run-now / status / history / live log tail** — reads engine run-state + logs under `/cache`.
- **Restore wizard** — both tiers; for cold data, walks the thaw-tier choice → wait → download.
- **Cost estimator** — interactive, re-runnable "what-if" over the pricing module.
- **Provisioning wizard** — three modes (§10).

The GUI never reimplements backup logic — every action shells out to the same engine scripts, so
headless and GUI behavior are identical.

---

## 10. Provisioning (three modes, one definition)

The **least-privilege IAM policy is defined once** and rendered into every path — the OpenTofu
module, the GUI's copy-paste instructions, and the validator's permission checks — so no path can
drift from what the others create. **Runtime keys are object-only** (`s3:ListBucket` on the bucket;
`GetObject`/`PutObject`/`DeleteObject` + multipart on the `appdata/` and `media/` prefixes).
Bucket-level configuration (versioning, lifecycle, SSE, public-access-block) is a one-time creation
step done by OpenTofu or by the guided clicks — never granted to the running tool.

1. **Automated (GUI).** User pastes **transient** admin creds → wizard drives the OpenTofu module in
   the container → **discards the admin creds** → writes only the runtime least-privilege keys into
   `secrets.env`. The transient-cred handling is an explicit, security-reviewed step (used in-session
   for one apply, never written to disk or logs).
2. **Scripted.** `setup.sh` wraps `tofu apply`; the user supplies admin creds to their own CLI/env —
   the tool never touches them. No `.tf` editing required.
3. **Guided manual (GUI) — the "no admin creds" path.** The wizard asks only for **bucket name +
   region**, then renders instructions specific to those values:
   - The exact **least-privilege IAM policy JSON**, pre-scoped to the user's bucket + prefixes,
     copy-paste ready; plus equivalent `aws iam` / `aws s3api` CLI commands as an alternative.
   - Numbered console steps for the **whole destination**: create the bucket (enable versioning, SSE,
     block-all-public-access, region), add lifecycle rules (noncurrent expiry, abort-incomplete-MPU),
     create the IAM policy (paste JSON), create the IAM user, attach, create an access key.
   - A **"Test & validate"** button: the user pastes the finished runtime key/secret; the wizard does
     a real list/put/get/delete of a tiny test object against the bucket to confirm keys +
     permissions work **before saving**, and shows a checklist reminder for the bucket-level toggles
     it cannot verify with object-only keys (e.g. "confirm versioning is ON").

---

## 11. AWS resources (`opentofu/` module)

Parameterized and account-agnostic (name prefix, region, bucket name, retention days). Provisions:

- **One S3 bucket**, prefixes `appdata/` and `media/`: versioning enabled; default SSE-S3 (AES-256);
  all public access blocked; bucket-owner-enforced ownership.
- **Lifecycle rules:** per prefix — expire noncurrent versions after N days (default 30); abort
  incomplete multipart uploads after 7 days. (Current media objects are written directly in their
  target class by rclone; restic manages appdata retention — lifecycle is a backstop.)
- **Least-privilege IAM user** scoped to this bucket only (the single policy definition of §10).
- **Outputs:** bucket name, region, runtime access key id + secret (sensitive), restic repository
  URL, rclone remote settings — fed into `secrets.env` / `backup.env`.

Usable standalone (`tofu apply`) or as a git-sourced module from a consumer's own root stack.

---

## 12. Configuration & secrets model

All mounted; nothing user-specific in the image. The GUI edits these same files.

- **`config/backup.env`** — `TZ`, `LOG_LEVEL`, `APPDATA_SCHEDULE`, `MEDIA_SCHEDULE`, `AWS_REGION`,
  `S3_BUCKET`, optional `S3_ENDPOINT`, `RESTIC_REPOSITORY`, `APPDATA_STORAGE_CLASS`,
  `MEDIA_STORAGE_CLASS`, retention (`KEEP_*`), `MEDIA_MIRROR`, `MEDIA_PACK` (+ packing group size),
  `RCLONE_TRANSFERS`, `RCLONE_BWLIMIT`, `APPDATA_SRC`, `MEDIA_SRC`, `APPRISE_URLS`, optional
  `HEALTHCHECK_URL`, `GUI_PORT`.
- **`config/includes-media.txt`** — include/exclude globs selecting media subdirs.
- **`config/secrets.env`** — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD`. Mode
  `600`, gitignored, `.example` provided. The entrypoint sources it, writes `RESTIC_PASSWORD_FILE`,
  and renders `rclone.conf` from env so creds aren't duplicated.

**Runtime secret surface = runtime AWS keys + restic password only.** Admin creds transient-only.

---

## 13. Scheduling, notifications, logging, monitoring

- **Scheduling:** `supercronic` runs both pipelines from cron expressions in `backup.env` — no
  dependency on the Unraid User Scripts plugin.
- **Notifications:** **Apprise** (Discord, Telegram, ntfy, email, etc.) via `APPRISE_URLS`; notify on
  failure always, success optionally. Optional Unraid host-notify if the adopter bind-mounts the host
  notify script.
- **Logging:** structured logs to stdout (Unraid Docker log) and a logfile under `/cache`. Each run
  logs bundled tool versions, snapshot id / bytes transferred, duration, and outcome. The GUI tails
  these.
- **Concurrency:** lockfiles prevent overlapping runs of the same pipeline.
- **Health:** weekly `restic check` (thaw-gated when cold); optional healthchecks.io-style ping.

---

## 14. Packaging, distribution & license

- **Image:** small base (Alpine); `restic` + `rclone` + `supercronic` + `apprise` + a slim Python
  runtime (GUI + estimator) + OpenTofu, all at **pinned versions** via build args with checksum
  verification. **Public multi-arch GHCR image** (`ghcr.io/<owner>/unraid-s3-backup`), semver +
  `latest`.
- **Install:** a **Community Applications template** (`my-unraid-s3-backup.xml`) as the turnkey
  installer, submitted to the CA store; also a `docker-compose.yml` for Dockhand/compose users.
- **Updates:** via Unraid's normal update flow (pulls a new pinned image, recreates). Config lives
  outside the image, so settings survive; rollback = pin the previous tag. Deliberate-update guidance
  documented (a much-newer restic can bump the repo format so older binaries can't read it;
  newer-reads-older is fine).
- **License:** **MIT**.

---

## 15. Security considerations

- **No database credentials** held by the tool (DB consistency comes from the plugin's container
  stops).
- **Admin AWS creds are transient-only** (provisioning): used in-session for one apply, never written
  to disk or logs. **Runtime keys are object-only**, scoped to one bucket/prefixes.
- **No Docker socket** mounted.
- **Read-only source mounts** — cannot mutate source data.
- **Encryption:** appdata client-side encrypted by restic; bucket default SSE-S3; versioning +
  public-access-block.
- **Secrets** in a mode-`600`, gitignored `secrets.env`; `.example` templates only in the repo.
- **GUI** carries no persisted admin creds and should sit behind the adopter's reverse proxy / SSO.

---

## 16. Testing strategy

- **Static:** `shellcheck` on all scripts; `tofu fmt -check` + `tofu validate` + `tflint` on the
  module; Python lint/type-check on the GUI + estimator.
- **Estimator unit tests:** fixed price tables → assert line-item + total math across classes,
  versioning, rotation, packing on/off, and restore scenarios.
- **Integration (engine):** spin up **MinIO** (S3-compatible) in CI; run a full restic appdata
  backup→forget→restore cycle and an rclone media copy→delta→restore cycle; assert restored bytes
  match source. (Storage class is a no-op on MinIO; the logic is validated.)
- **Provisioning:** validate the single policy definition renders identically into the OpenTofu module
  and the GUI copy-paste path; test the "Test & validate" flow against MinIO.
- **Config:** `bats` tests for include/exclude parsing and fail-fast validation (missing plugin dir,
  missing secrets).
- **Restore drill:** documented manual runbook for both tiers, including the Deep Archive thaw flow.

---

## 17. Build phases (engine-first)

One coherent spec; sequenced implementation. Each phase is independently shippable.

1. **Engine.** Both pipelines (restic appdata + rclone media), config/secrets, `supercronic`
   scheduler, Apprise, `restore.sh`, the `opentofu/` module + `setup.sh`, and the CA template. Usable
   headless / via the CA-template form on day one.
2. **GUI + cost estimator.** Full ops GUI (config editor, media picker, run/logs/status/history,
   restore wizard) + the estimator (bundled prices + optional live refresh).
3. **Provisioning wizard + optional packing + upfront-bulk-placement polish.** The three-mode wizard
   (automated / scripted / guided-manual), media packing mode, direct-to-cold ingest polish.
4. **TODO / future.** Backblaze B2 / Cloudflare R2 backend, CA-store submission, multi-arch + public
   GHCR hardening.

---

## 18. Repository layout

```
unraid-s3-backup/
  Dockerfile                  # pinned RESTIC/RCLONE/SUPERCRONIC/APPRISE + python + tofu, checksum-verified
  docker-compose.yml          # local / Dockhand deploy
  my-unraid-s3-backup.xml     # Community Applications template
  setup.sh                    # scripted provisioning (wraps tofu)
  scripts/
    entrypoint.sh
    backup-appdata.sh
    backup-media.sh
    restore.sh
    lib/common.sh
  app/                        # GUI + estimator (python)
    ...                       # web app, pricing module, bundled price tables (dated JSON)
  opentofu/                   # account-agnostic IaC module (single policy definition lives here)
    main.tf  variables.tf  outputs.tf  versions.tf  README.md
  config/
    backup.env.example
    includes-media.txt.example
    secrets.env.example
  docs/
    superpowers/specs/        # this design
    superpowers/plans/        # implementation plan(s)
  README.md                   # adopter setup + restore runbook + prerequisites + cost notes
  LICENSE                     # MIT
  .gitignore
```

---

## 19. Prerequisites (README)

- An **Unraid** server (paths/assumptions target Unraid).
- The **Appdata Backup plugin** (`appdata.backup`) installed and configured — **required**; this tool
  ships its output and fails fast if its archive directory is absent.
- An **AWS account**. The bucket + least-privilege IAM are created by the provisioning wizard
  (automated, scripted, or guided-manual) — no Terraform literacy required.
- Docker (the container itself). Nothing else to install — `restic`/`rclone`/etc. are bundled.

---

## 20. Locked decisions

1. Two pipelines (`restic` appdata + `rclone` media), one container, internal `supercronic`.
2. Appdata via **Model 1** — ship the plugin's archives; the plugin is a required prerequisite; no DB
   handling or credentials in this tool.
3. **AWS-first, fully tiered.** Per-pipeline storage class is a first-class config axis; B2/R2 is a
   Phase-4 TODO. Upfront bulk placement (direct-to-cold ingest) supported.
4. **Appdata may use cold classes** (default warm); nightly backups work off the local index cache;
   prune/check/restore are thaw-gated with clear warnings.
5. Media tier: **additive** (`rclone copy`) + bucket versioning; **optional packing** (default OFF).
6. **Cost estimator** — bundled per-region price tables + optional live refresh; models storage,
   ingress, egress/restore, versioning, rotation, packing, bulk placement; in GUI + CLI.
7. **Full web GUI** — config, run/logs/status/history, restore wizard, estimator, provisioning wizard.
8. **Three-mode provisioning** — automated (transient admin creds) / scripted (`setup.sh`) /
   guided-manual (no creds; generated policy JSON + validate). Single least-privilege policy
   definition; runtime keys object-only.
9. Notifications: **Apprise** (optional Unraid host-notify).
10. One repo; IaC in `opentofu/`; public multi-arch GHCR image with pinned versions; CA template.
11. **MIT license.**
12. Reference scope: media = comics + books + lightnovels (configurable).

---

## 21. Out of scope / future

- Backing up the bulk media library (movies/tv/anime/music).
- Backblaze B2 / Cloudflare R2 as guided backends (Phase 4).
- Client-side encryption of the media tier by default (available as opt-in `rclone crypt`).
- Publishing the `opentofu/` module to the Terraform Registry (would motivate a repo split).
- A hosted/multi-tenant control plane; the per-adopter GUI is the interface.
