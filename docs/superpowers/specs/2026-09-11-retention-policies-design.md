# Per-Job Retention Policies + Baseline Lifecycle Management — Design Spec

**Date:** 2026-09-11
**Status:** Draft (brainstorming) — design approved in chat; awaiting spec review before the implementation plan
**Branch:** `retention-policies`, off `impeccable-versioned-files`
**Slice:** Extends the multi-job backup engine (`app/engine/*`, `scripts/*`, `app/gui/*`, `app/estimator/*`, `opentofu/*`). No change to the one-bucket / per-prefix model.

---

## 1. Overview & role

Today the three job engines rotate old data inconsistently:

- **versioned (restic):** the backup run prunes via a restic keep-policy (last/daily/weekly/monthly). Full control, in-process.
- **versioned-files (vfiles engine):** prunes its own catalog + S3 objects by an age window ("keep old versions N days").
- **archive (rclone):** does **no** client-side pruning — it relies entirely on the **bucket lifecycle** (a global `noncurrent_version_expiration`, 30 days, set at provisioning). The wizard exposes only a "mirror" checkbox, no retention control.

So archive is the odd one out — no per-job retention, no wizard control, and its rotation is invisible/un-tunable from the app.

This feature introduces a **two-layer retention model**, consistent across all engines:

1. **Baseline bucket lifecycle** — the guaranteed outer bound. Set at provisioning, runs server-side even if the app never does. Optional (can be "none"). **Viewable + editable from a GUI Settings screen** via a separate, optional lifecycle-management credential (the Cost-Explorer credential pattern).
2. **Per-job client-side prune** — the real control. Each job carries a retention **policy** (Keep everything / Keep N days / Keep last N versions; restic also keeps tiered). Enforced in-process by each engine after each run, always **≤ the baseline**.

## 2. Goals & non-goals

### Goals
- A **per-job retention policy** on every job type, chosen in the wizard, stored in `jobs.json`, enforced client-side.
- Bring **archive** jobs up to par: a real per-job prune over their S3 object versions (they currently have none).
- A **consistent policy menu**: Keep everything / Keep N days / Keep last N versions for all types; restic additionally keeps its tiered last/daily/weekly/monthly.
- A **Settings screen to view and edit the baseline bucket lifecycle** (noncurrent-version expiration per prefix, multipart-abort) via an optional separate credential — never the runtime key.
- Keep the **baseline lifecycle optional** at provisioning (including "none"), with the multipart-abort rule always on (pure hygiene).
- The cost model reflects the chosen policy type.

### Non-goals (this slice)
- **No multi-bucket.** One bucket, per-job prefixes — unchanged. (Considered and rejected: per-job buckets make "add a job" a provisioning op, churn IAM, hit bucket limits, for benefits already covered by per-prefix lifecycle + cost-allocation tags.)
- **No lifecycle write via the runtime key.** Bucket-config permissions stay off the least-privilege runtime key; lifecycle management uses a separate optional credential.
- **No tiered (GFS) policy for the per-file engines** (versioned-files / archive) — daily/weekly/monthly of a single file's versions is awkward and not wanted for media. Tiered stays restic-only.
- **No new scheduler** — per-job prune runs at the tail of each job's existing backup run (as restic/vfiles already do).

## 3. The two-layer model

```
             ┌─────────────────────────────────────────────┐
  guaranteed │  Baseline bucket lifecycle (server-side)     │  <- outer bound; set at
  outer      │  noncurrent_version_expiration per prefix    │     provisioning; runs even
  bound      │  + abort_incomplete_multipart (always on)    │     if the app never does
             └─────────────────────────────────────────────┘
                          ⊇  (must be ≥ every per-job window)
             ┌─────────────────────────────────────────────┐
  the real   │  Per-job client-side prune (in-process)      │  <- the retention you chose;
  control    │  restic / vfiles / archive, after each run   │     runs on your schedule
             └─────────────────────────────────────────────┘
```

**Invariant:** every per-job window must be **≤ the baseline**. If the app prunes at the job window, the baseline never triggers; if the app has been off, the baseline eventually sweeps. The wizard/estimator **warns** when a per-job window exceeds the baseline (AWS would delete versions the job wanted to keep). The baseline is loosened at provisioning to a roomy default (e.g. 180 days) so per-job windows sit inside it.

## 4. Per-job retention policy model

A job's retention is one **policy**, stored in `jobs.json` under a single `retention` object:

```json
"retention": { "type": "days",     "days": 90 }
"retention": { "type": "count",    "count": 5 }
"retention": { "type": "keep_all" }
"retention": { "type": "tiered",   "keep": {"last":3,"daily":7,"weekly":4,"monthly":6} }   // versioned/restic only
```

| Policy | Meaning | Types |
|---|---|---|
| `keep_all` | never auto-delete old versions | all |
| `days` | delete versions older than N days | all |
| `count` | keep the N most recent versions of each file, drop older | all |
| `tiered` | restic keep-policy (last/daily/weekly/monthly) | **versioned only** |

**Back-compat / migration:** existing jobs are mapped on load (`jobs_io`): a versioned job's `keep` → `retention:{type:tiered,keep}`; a versioned-files job's `retention_days` → `retention:{type:days,days}`; an archive job (no retention today) → `retention:{type:days, days:<baseline default = 180>}` (matches prior effective behavior — resolved in §14). The old fields are read if `retention` is absent, and rewritten to the new shape on next save.

## 5. Per-engine enforcement

Each engine already runs a prune step (or, for archive, gains one). The policy maps cleanly:

- **restic (`scripts/backup-job.sh` versioned path):**
  - `tiered` → `restic forget --keep-last/--keep-daily/--keep-weekly/--keep-monthly … --prune` (today's behavior).
  - `days` → `restic forget --keep-within <N>d --prune`.
  - `count` → `restic forget --keep-last <N> --prune`.
  - `keep_all` → skip `forget`/`prune`.
- **versioned-files (`app/engine/vfiles.py` prune):** it already deletes catalog rows + S3 objects older than a day-window. Extend the prune to accept a policy:
  - `days` → today's behavior.
  - `count` → keep the N newest non-tombstone versions per path, delete older (their S3 keys).
  - `keep_all` → skip prune.
- **archive (new prune in the archive path):** after `rclone`, run a client-side S3 version prune over `media/<job>/`:
  - list object versions (`ListObjectVersions`), group by key,
  - `days` → delete noncurrent versions with `LastModified` older than N days,
  - `count` → per key, keep the N most recent versions (current + noncurrent), delete the rest,
  - `keep_all` → no-op,
  - delete via `DeleteObject` with `VersionId` (runtime key already allows `s3:DeleteObject`).
  - **Scope guard** (mirrors vfiles): never delete a key outside the job's own `media/<job>/` prefix; never delete a current version.

The archive prune is a small pure-Python module (`app/engine/archive_prune.py`) with all S3 I/O behind `app.engine.s3`, unit-testable with a fake runner — same shape as `vfiles`.

## 6. IAM changes

- **Runtime key** (`provisioning/iam-policy.json.tmpl`): add **`s3:ListBucketVersions`** to the `ListBucketScoped` statement (needed to enumerate object versions for the archive prune). `DeleteObject` (version-scoped delete) is already granted. No bucket-config permissions added.
- **New optional lifecycle-management credential** (§8): a separate IAM credential the admin creates with `s3:GetBucketLifecycleConfiguration` + `s3:PutBucketLifecycleConfiguration` on the bucket. Documented in the README's manual-provisioning IAM section and emitted/optional in the OpenTofu module (a separate `*-lifecycle` user, key output marked sensitive) — but the app never requires it; it's connect-in-the-GUI like Cost Explorer.

## 7. Wizard UI (per-job policy)

`app/gui/templates/job_form.html` §4 "Storage & retention": replace the type-specific retention controls with a **policy-type selector** + the field(s) for the choice:

```
Retention policy: ( ) Keep everything
                  (o) Keep for [ 90 ] days
                  ( ) Keep last [ 5 ] versions
                  ( ) Tiered (last/daily/weekly/monthly)     <- shown only for Versioned
```

- The selector is shown for all types; the **Tiered** option is `data-when-type="versioned"` only.
- The restic keep fieldset and the versioned-files "keep N days" field are folded into this one selector (their values become the `tiered`/`days` policy).
- `estimate_io` maps the selected policy into the model's per-job retention; the live cost updates as before.
- A **warning line** appears when the chosen window/effective retention exceeds the connected baseline (fetched from the lifecycle settings, or the provisioning default when not connected).

## 8. Baseline lifecycle: provisioning + Settings screen

### Provisioning (OpenTofu, already per-prefix)
- Keep the existing `aws_s3_bucket_lifecycle_configuration` but drive it from tunable vars: `noncurrent_version_expiration_days` (default loosened to **180**; `0`/null → **omit the expiration rule** = "none"), `abort_incomplete_multipart_days` (**always emitted**, default 7).
- README manual-provisioning steps updated to match, and to describe the optional lifecycle-management IAM user.

### Settings screen (`GET/POST /settings/lifecycle` or a panel on the existing costs/settings page)
- **Connect** the optional lifecycle-management credential (write-only fields, exactly like the Cost-Explorer connect form): `LIFECYCLE_ACCESS_KEY_ID`, `LIFECYCLE_SECRET_ACCESS_KEY`, `LIFECYCLE_SESSION_TOKEN` (optional).
- **View:** when connected, call `GetBucketLifecycleConfiguration` and render the current rules per prefix (noncurrent-expiration days, multipart-abort days). When not connected, show the provisioning default as reference + "connect to view/manage live."
- **Edit:** a form to set noncurrent-expiration days per prefix (and toggle "none") + multipart-abort days → `PutBucketLifecycleConfiguration`. Guard-railed so it can't set a window **below** any job's per-job retention (validation error listing the offending jobs) — preserving the invariant.
- The credential is stored/cleared via `config_io`, mirroring `read_cost_explorer_creds`/`clear_cost_explorer_creds`: new `LIFECYCLE_KEYS`, `read_lifecycle_creds`, `clear_lifecycle_creds`, added to `_MANAGED_KEYS`. Never returned to the client.
- S3 calls go through a small `app/gui/lifecycle_io.py` (boto3 or aws-cli via the runner), returning plain dicts; no secrets in responses; `{"connected": False}` when no credential.

## 9. Cost model impact (`app/estimator`)

The model's versioning term is age-based today (`versioning_retention_days`). Extend per policy:
- `days` → current formula (retention window in days).
- `count` → noncurrent GB ≈ `size × change% × min(count, backups_per_month × horizon)` — bounded by N versions rather than a day-window; the projection plateaus at the count.
- `keep_all` → noncurrent grows unbounded over the projection horizon (no plateau) — surfaced honestly in the timeline.
- `tiered` → today's restic keep-policy proxy (`effective_retention_days`).
The wizard/estimator threads the policy through `estimate_io`; the cost-over-time timeline already exists to show the resulting curve.

## 10. Data flow summary

- **Backup run:** engine backs up → runs its prune per the job's `retention` policy (client-side).
- **Baseline:** AWS enforces the bucket lifecycle server-side, independently.
- **Wizard:** policy selector → `jobs_io` (validates against baseline) → `jobs.json`; live cost via `estimate_io`.
- **Settings/lifecycle:** GUI ↔ `lifecycle_io` (separate credential) ↔ S3 Get/Put lifecycle.

## 11. Testing (TDD)

- **Model:** count-based and keep_all versioning terms; policy → retention mapping.
- **vfiles prune:** count-based keeps N newest per path, deletes older S3 keys; keep_all no-ops; scope guard holds.
- **archive_prune:** age + count selection over a fake version listing; scope guard rejects out-of-prefix keys; never deletes current version; DeleteObject calls carry the right VersionId.
- **restic mapping (bats):** each policy → the right `restic forget` flags; keep_all skips forget.
- **jobs_io:** back-compat mapping of old `keep`/`retention_days` → new `retention`; round-trips.
- **wizard routes:** policy selector renders per type; over-baseline warning; `jobs.json` shape.
- **lifecycle_io / settings routes:** `{"connected": False}` with no cred; view renders rules from a fake Get; edit rejects a window below a job's retention; secrets never returned.

## 12. Files touched

`app/engine/vfiles.py` (+ `catalog.py`), new `app/engine/archive_prune.py`; `scripts/backup-job.sh` (restic forget mapping + call archive prune); `app/gui/jobs_io.py` (retention schema + migration), `app/gui/estimate_io.py`, `app/gui/config_io.py` (lifecycle creds), new `app/gui/lifecycle_io.py`, `app/gui/routes.py` (+ settings/lifecycle), `app/gui/templates/job_form.html` + a lifecycle settings template; `app/estimator/model.py` + `cli.py`; `opentofu/*` (lifecycle vars, optional lifecycle IAM user) + `provisioning/iam-policy.json.tmpl` (ListBucketVersions); README (retention policies, lifecycle credential); tests across `tests/`.

## 13. Suggested phasing (two implementation plans)

- **Phase 1 — per-job retention policies:** §4–§7, §9 (policy menu, jobs.json + migration, three engine prunes, `ListBucketVersions`, wizard selector, cost model). Delivers working per-job rotation on its own.
- **Phase 2 — baseline lifecycle management:** §8 (lifecycle credential, Settings view/edit screen, provisioning tunables). Builds on Phase 1's invariant/validation.

## 14. Resolved decisions (spec review — 2026-09-11)

1. **Archive default policy on migration:** map existing archive jobs to **`retention:{type:days, days:180}`** (= the baseline), so upgrade behavior is unchanged. (Not `keep_all`.)
2. **Baseline default window:** **180 days** noncurrent-version expiration (loosened from today's 30) as the roomy outer bound.
3. **Settings location:** a **panel on the existing Cost/Settings screen next to the Cost-Explorer connect form** (same credential-connect pattern) — not a separate `/settings/lifecycle` page.
