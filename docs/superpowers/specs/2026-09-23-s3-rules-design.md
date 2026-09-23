# S3 rules — one owner per kind of history — design

Status: draft for review · 2026-09-23 · owner: paymon

## Summary

Today the app and S3 both try to govern backup history, and they disagree. The base bucket's
lifecycle rules (`backstop-appdata`, `backstop-media`, created once by OpenTofu) expire old object
versions after **30 days**, dedicated buckets get a hardcoded 30-day rule (`buckets._LIFECYCLE`),
while job history settings default to 180 days (Plain copy) and 90 days (File history). For
**Plain copy** — whose history exists *only* as S3 noncurrent versions of the same key — the job's
setting is silently cut to 30 days and "keep everything" is impossible. The retention spec
(2026-09-11) had decided to loosen the baseline to 180 days, but the code still ships 30. Nothing
shows or edits these rules after setup.

This feature gives **each kind of history exactly one owner**:

| History | Owner | S3's role |
|---|---|---|
| Plain copy (old versions of the same key) | **S3 lifecycle rules** — the job's history setting *is* its S3 rule | the whole policy |
| Snapshot backup (restic snapshots) | the app (`restic forget --prune`), unchanged | **undo window** for data the job already deleted |
| File history (distinct keys + catalog) | the app (catalog prune), unchanged | **undo window** for data the job already deleted |
| Cheaper tier, versioning, housekeeping | **S3 only** | — |

…and a new **S3 rules** screen to see and change every rule, with **impact previews and typed
confirmation** before anything that keeps less, a **tamper alarm** when rules change outside the
app, and a nightly **storage summary** so previews are instant even for 230k-file folders.

## Goals / Non-goals

**Goals**
- Plain copy's history setting (N days / newest N per file / forever) is exactly what S3 enforces.
- Snapshot/File history get a configurable per-folder undo window (default 30 days).
- One screen shows the live rules per bucket (app rules + rules the owner added in the console).
- The owner can change: Plain copy keep time, newest-N, undo windows, cheaper tier for old
  versions, versioning (on/suspend), abandoned-upload cleanup, delete-marker cleanup.
- Nothing that keeps less (or suspends versioning, or moves history to a cheaper tier) applies
  without a preview of its concrete impact and a typed confirmation.
- Changes that keep more (longer windows, new jobs, removed tier) apply automatically, with an
  explanation.
- Rules changed outside the app are detected before every backup run, restored, and alarmed.
- The backup key can no longer permanently delete old versions (soft deletes only).

**Non-goals**
- Rules that expire or transition **current** objects (would delete live backups or make restic
  packs unreadable). Never created; if a console rule does this it is shown read-only with a danger flag.
- S3 Inventory (daily AWS-generated listings) — possible later for very large buckets.
- Object Lock / WORM (still the deferred v2 from the multi-bucket spec).
- Changing how Snapshot or File history prune their own history.
- Full lifecycle support on non-AWS S3 endpoints (see §9).
- Pricing the cheaper tier in the frozen estimator (a note instead).

## Decisions (from brainstorming Q&A)

| # | Decision |
|---|----------|
| Scope | Correctness + visibility + owner control over S3 lifecycle settings; warn before anything that could destroy data. |
| Conflicts | Jobs set the floor → superseded by "one owner per kind of history" (below). |
| Ownership | **One owner each**: Plain copy history = S3 rules only (app's archive prune removed); Snapshot/File history keep app retention + S3 undo window; tier/versioning/housekeeping S3-only. |
| Permissions | Bucket-admin role may read/write lifecycle + versioning on the **base** bucket (it already can on `<base>-*`), shipped as permissions **level 4**. |
| Tamper | Role edits + **tamper alarm**: live app rules/versioning checked before every backup run; drift → restore + Board blocker. |
| Knobs | Keep time, newest-N per file, cheaper tier, versioning + housekeeping (all four). |
| Approach | **A**: local desired state + jobs → computed rules → diff → preview → apply; console rules preserved. |
| Impact data | **Stored per-folder summary** refreshed by a background scan after each backup run; "Refresh now" for exact figures. |
| Screen | **A's overview + B's side editor** (mockups in `.superpowers/brainstorm/`). |

## 1. Rules model

**Rule IDs.** App-owned rules are `backup-engine:<folder>` (e.g. `backup-engine:media/manga/`,
`backup-engine:appdata/`, `backup-engine:bucket` for a dedicated bucket's single job rule) and
`backup-engine:housekeeping`. Legacy app IDs `backstop-appdata`, `backstop-media` (base bucket,
OpenTofu) and `backup-engine` (dedicated buckets, `buckets._LIFECYCLE`) are recognised as app-owned
and replaced. Any other ID is a **console rule**: preserved verbatim on every write, shown read-only.

**Folder rules** (noncurrent actions only — never current-object actions):

| Folder | Job type | Rule |
|---|---|---|
| `media/<job>/` (base) or whole bucket (dedicated) | Plain copy, history `days: N` | `NoncurrentVersionExpiration {NoncurrentDays: N}` |
| same | Plain copy, `count: N` (1–100) | `NoncurrentVersionExpiration {NoncurrentDays: 1, NewerNoncurrentVersions: N}` |
| same | Plain copy, `keep_all` | no expiration |
| same | Plain copy, days D **and** newest N (new combined form) | `{NoncurrentDays: D, NewerNoncurrentVersions: N}` — S3 semantics: the newest N old versions are kept indefinitely; older ones expire D days after being replaced |
| `appdata/` (base; all Snapshot jobs share it) / whole bucket (dedicated) | Snapshot | undo window W: `{NoncurrentDays: W}` |
| `media/<job>/` / whole bucket | File history | undo window W: `{NoncurrentDays: W}` |

**Job setting shape.** Plain copy's `count` retention gains an optional `days` field —
`{"type": "count", "count": N, "days": D}` is the combined form (`jobs_io` validates `1 ≤ N ≤ 100`,
`D ≥ 1`; without `days` it means `D = 1`). The wizard's Plain copy history choice offers days /
newest N / newest N + days / everything; `tiered` stays Snapshot-only.

Any folder rule may also carry `NoncurrentVersionTransitions [{NoncurrentDays: T, StorageClass}]`
(tier: `GLACIER_IR` or `DEEP_ARCHIVE`; `T < N/W` when both apply, AWS minimums enforced).

**Housekeeping rule** (one per bucket, bucket-wide filter, no noncurrent actions so it cannot
conflict): `AbortIncompleteMultipartUpload {DaysAfterInitiation: U}` (default 7) and
`Expiration {ExpiredObjectDeleteMarker: true}` (default on).

**No overlap.** App folder rules never overlap each other (one per folder; `appdata/` and
`media/<job>/` are disjoint). A console rule with noncurrent actions whose prefix overlaps an app
folder is flagged ("S3 applies the shorter expiry where rules overlap").

**Job lifecycle.** A new job adds its folder rule; deleting a job removes its rule (its data and
history are kept, as job deletion does today — nothing expires them). A dedicated bucket's rules
come from its single job.

**Versioning** is per bucket: `on` or `suspended` (S3 cannot return to never-versioned).
Suspending means Plain copy history stops and future overwrites/deletes are immediate.

## 2. Engine — `app/engine/lifecycle.py`

- `desired(bucket, jobs, settings) -> Config` — **pure**: app rules + housekeeping + versioning
  intent for one bucket, from `jobs.json` and `config/storage.json`.
- `read_live(bucket, creds) -> Live` — `get-bucket-lifecycle-configuration` (NoSuchLifecycleConfiguration
  → empty) + `get-bucket-versioning`, via the assumed bucket-admin role.
- `merge(live, app_rules) -> full rule list` — console rules from `live` + the app rules (S3 replaces
  a bucket's whole configuration on every put).
- `apply(bucket, config, creds)` — `put-bucket-lifecycle-configuration` (+ `put-bucket-versioning`
  when the intent changed); records `state/lifecycle/<bucket>.applied.json` (the app rules +
  versioning last applied) and an Activity record `kind: "s3-rules"`.
- `classify(applied, desired) -> list[Change]` each marked **keeps-more** or **keeps-less**.
  Keeps-less: shorter `NoncurrentDays`; `NewerNoncurrentVersions` added or lowered; expiration
  added where there was none; tier added or moved earlier; versioning → suspended. Everything else
  (longer, removed limits, removed tier, housekeeping days) is keeps-more.

**`config/storage.json`** (owner intent; Plain copy keep time is NOT here — it is the job's setting):
```json
{"version": 1,
 "buckets": {
   "<bucket>": {
     "versioning": "on",
     "abort_uploads_days": 7,
     "delete_marker_cleanup": true,
     "folders": {
       "appdata/":         {"undo_days": 30, "tier": null},
       "media/documents/": {"undo_days": 30, "tier": null},
       "media/manga/":     {"tier": {"class": "DEEP_ARCHIVE", "after_days": 30}}
     }}}}
```
Missing entries take the defaults (undo 30, uploads 7, markers on, versioning on, no tier).

**Triggers.**
- **Job save/delete:** recompute; keeps-more changes apply immediately (flash: "S3 now keeps
  manga's old versions for 180 days"). Shortening a Plain copy job's history in the wizard runs
  the §3 preview before the save; confirming saves the job and applies the rule.
- **S3 rules screen:** keeps-more apply on save; keeps-less need preview + typed confirmation.
- **Setup:** automated setup applies the initial rules right after the permissions step;
  `buckets.ensure_bucket` asks the engine for the dedicated bucket's rules (replacing `_LIFECYCLE`).
- **Pending:** if `desired` keeps less than `applied` and nothing confirmed it (e.g. after the
  level-4 migration), the change is listed on the S3 rules screen as "waiting for your
  confirmation" and the Setup row warns. Nothing keeps-less is ever auto-applied.

**Tamper check** (`python3 -m app.engine.lifecycle check --bucket <b>`), run by `backup-job.sh`
before every backup run for the job's bucket (and by **Check now**): read live via the role;
compare the live **app-owned** rules and versioning with `applied.json` (not with `desired` —
pending changes are not tampering; console-rule edits are not tampering). On a difference:
re-apply `applied` (merged with the current console rules), record an `s3-rules` event
"restored", and write `state/_lifecycle.json`
`{<bucket>: {"state": "ok"|"restored"|"not_restored"|"unsupported", "checked_at", "detail"}}`.
A missing lifecycle configuration counts as tampering. **A failed check never blocks the backup.**

## 3. Previews and damage warnings

Every keeps-less change produces a **preview** before it can apply, per affected folder/job:
- the current vs new rule, in words;
- **what S3 will permanently delete**, from the stored summary (§4): "about 1,240 old versions
  (38 GB), oldest from 12 Mar, within about a day of applying", stamped "as of <scan time>", with
  **Refresh now**;
- undo-window changes: "data these jobs already deleted will be unrecoverable after N days instead of M";
- newest-N: files with more than N old versions lose the oldest ones;
- versioning suspend: "overwritten or deleted files in this bucket are gone immediately from now on;
  Plain copy history stops; existing old versions stay until their rule expires them";
- tier: minimum storage charge (Glacier Instant Retrieval 90 days, Deep Archive 180 days), that
  objects under 128 KB are not moved, and that restoring an old version from the tier takes hours
  and costs money.

**Confirmation.** A preview stores a pending token `state/lifecycle/pending/<token>.json` (bucket,
exact proposed config hash, created_at). Apply requires the token, the config must still hash the
same (jobs/rules unchanged), the token must be < 1 hour old, and — whenever the preview deletes
anything or suspends versioning — the owner types the bucket name. Otherwise: "Preview again".

The job wizard uses the same preview for shortening a Plain copy job's history. Shortening a
Snapshot/File history job's own setting is unchanged (the app's prune) — it only notes that the
undo window still applies.

**Informational warnings:** tamper blocker (§2); overlapping console rule; endpoint without
lifecycle support (§9).

## 4. Storage summary — `app/engine/storage_summary.py`

- After each backup run, a background operation (kind `storage-summary`) scans that job's folder
  with `list-object-versions` (paginated; the backup key already has `ListBucketVersions`) and
  writes `state/storage/<bucket>__<folder-slug>.json`:
  ```json
  {"scanned_at": "...", "bucket": "...", "folder": "media/manga/",
   "noncurrent_by_age_days": [[0, 12, 402653184], [1, 3, 1048576], ...],
   "noncurrent_by_rank": [[1, 912, 3.1e10], [2, 210, 9.0e9], ...],
   "delete_markers": 57, "current_objects": 232021, "current_bytes": 1957000000000}
  ```
  `age` = whole days since the version was replaced (successor's LastModified); `rank` = 1 for the
  newest old version of a key, 2 the next, … (ranks > 100 folded into 101). Entries are
  `[bucket, versions, bytes]`.
- **Refresh now** re-scans one folder on demand (background op, progress in the workbar).
- `impact(summary, old_rule, new_rule) -> {versions, bytes, oldest}` is **pure** and exact with
  respect to the summary.
- Scans are read-only; they replace the full version listing the removed archive prune did on
  every Plain copy run, so there is no new AWS cost.

## 5. Screens

**`/setup/storage` — "S3 rules"** (A's overview + B's side editor):
- Status line from `state/_lifecycle.json`: "All rules match · checked before last night's run"
  + **Check now**; blocker on tamper; warning on unsupported endpoint.
- One card per bucket (shared first, then dedicated): versioning chip; table
  *folder · job & type · what S3 keeps (in words) · cheaper tier · Change…*; console rules greyed
  and read-only (flagged if overlapping); bucket-wide line (abandoned uploads, delete markers,
  **Suspend versioning…**); "waiting for your confirmation" items.
- **Change…** opens the side editor beside the table with only the relevant fields (Plain copy:
  days / newest N / both / forever + tier; undo rows: days + tier; bucket-wide: uploads days,
  markers, versioning). A live impact line updates from the summary as values change.
  **Preview change** → diff + impact + (if needed) typed confirmation → **Apply**. For Plain copy
  the editor says "This is also manga's history setting" (same value, saved to the job).
- No AWS call on GET; Check now / Refresh now / Preview / Apply are CSRF POSTs.

**Elsewhere:** job wizard copy (Plain copy: "Kept by S3 inside AWS"; others: undo-window note +
link); job page line ("History: S3 keeps old versions 180 days · Deep Archive after 30"); Setup
readiness row **"S3 rules match your jobs"** (ok / warn pending / blocker tamper / warn
unsupported); Board blocker for tamper; Activity labels "S3 rules update" and "storage summary"
(setup group). Visible copy follows the vocabulary tests (no "prune"/"retention").

## 6. Permissions level 4 — "Keep S3 rules in step with your jobs"

- **Bucket-admin role** (`bucket-admin-policy.json.tmpl`): new statement
  `BaseBucketRules` — `s3:GetLifecycleConfiguration`, `s3:PutLifecycleConfiguration`,
  `s3:GetBucketVersioning`, `s3:PutBucketVersioning` on exactly `arn:aws:s3:::${bucket}`; add
  `s3:GetLifecycleConfiguration` to `CreateAndConfig` (`<base>-*`).
- **Runtime policy** (`iam-policy.json.tmpl`): remove `s3:DeleteObjectVersion` from `ObjectRW` and
  `ObjectRWWildcard` (its only user, the archive prune, is removed). The backup key can then only
  soft-delete; the undo window is the recovery window.
- `provisioning/permissions.json`: level 4, history entry "Keep S3 rules in step with your jobs",
  `features: ["s3-rules"]`, new `templates_sha256`. The S3 rules screen and automatic applies
  require `feature_available("s3-rules")`; below level 4 the screen shows "Needs the permissions
  update →" and nothing is written.
- **Verify probe change (cross-feature):** the permissions Verify's negative scope probe currently
  requires the role session's `get-bucket-versioning --bucket <base>` to be AccessDenied. Level 4
  grants that, so the probe switches to `s3api get-bucket-tagging --bucket <base>`, which the
  narrowed role still cannot do (Teardown's `GetBucketTagging` is on `<base>-*` only) while the old
  unscoped role could (a permitted call without tags returns `NoSuchTagSet`, not AccessDenied → not ok).
- Residual stolen-key reach after this feature: shorten rules / suspend versioning via the role
  (tamper-alarmed, delayed ~1 day by S3), and empty `<base>-*` dedicated buckets via the teardown
  permission (pre-existing; "make teardown admin-only" goes to the backlog).

## 7. Removals and setup changes

- `app/engine/archive_prune.py` and its call in `scripts/backup-job.sh` are removed.
- `opentofu/main.tf`: remove `aws_s3_bucket_lifecycle_configuration.backup` and the variables
  `noncurrent_version_expiration_days`, `abort_incomplete_multipart_days`. Until the app applies
  rules, the bucket keeps everything (safe direction). A scripted re-run with saved state deletes
  the old configuration; the next check/job save/run re-applies the app rules.
- `app/engine/buckets.py`: `_LIFECYCLE` removed; `ensure_bucket` applies the engine's rules for the
  new dedicated bucket (with the role creds it already holds).
- Guided-manual console steps: drop "create lifecycle rule … 180 days"; keep "turn versioning on".
- Automated setup: after the permissions converge, apply the initial lifecycle (soft-fail with a
  warning, like the permissions step).

## 8. Migration (existing installs)

After the level-4 update, the first check/job save computes `desired` and replaces
`backstop-appdata`/`backstop-media` (and dedicated `backup-engine` rules) with folder rules:
- Plain copy: 30-day backstop → the job's own setting (usually longer) — keeps more → automatic.
- Snapshot/File history: undo window 30 → 30 — same → automatic.
- `keep_all` Plain copy → no expiration — keeps more → automatic.
- Any job whose setting is shorter than the old 30 days → keeps less → listed as waiting for
  confirmation (the old rule stays until confirmed).
Before the update nothing breaks: Plain copy history stays under the old 30-day rule (what
effectively happens today) and the Board/Setup prompt the update.

## 9. Custom S3 endpoints

With `S3_ENDPOINT` set, lifecycle/versioning APIs and the IAM role may be unavailable. When
`get-bucket-lifecycle-configuration` returns NotImplemented/MethodNotAllowed (or no role is
configured), the bucket's state is `unsupported`: the S3 rules screen, job page and Setup row say
"This storage doesn't support S3 rules — Plain copy keeps all old versions", and no writes are
attempted. (The removed archive prune was the only Plain copy history limit there; the owner
accepted this trade-off.)

## 10. Cost estimates (frozen model; adapters only)

`estimate_io` feeds each job's `JobInputs.versioning_retention_days` from its effective S3 window
(Plain copy: its days setting; count keeps using the model's existing retention-count path;
`keep_all` → the model's no-expiry handling) and the undo window for Snapshot/File history. A
cheaper tier is not priced; cost screens show "Estimate doesn't include moving old versions to a
cheaper tier" when any tier is set.

## Error handling

| Case | Behaviour |
|------|-----------|
| Below level 4 | Screen read-only notice + update link; no writes; archive history under the old rule. |
| Role assume / read / put fails | Clear per-bucket error on the screen; Activity record; job saves still succeed with a warning; tamper state `not_restored`. |
| Stale / mismatched preview token | "Preview again"; nothing applied. |
| Summary missing or old | Preview shows "no summary yet — Refresh now" and requires the typed confirmation anyway. |
| Console rule overlaps an app folder | Warning on the screen; app rule still applied. |
| Invalid values (newest-N ∉ 1–100, tier days below AWS minimum, tier ≥ expiry) | Form error; nothing previewed. |
| Unsupported endpoint | `unsupported` state everywhere; no writes. |

## Testing

- **Rules:** table-driven `desired()` per job type/setting; exact S3 JSON (IDs, Filter, actions);
  limits; no overlap among app rules; console rules preserved byte-for-byte; overlap detection;
  legacy IDs replaced.
- **classify:** every keeps-more / keeps-less case.
- **apply / tamper:** fake AWS; merge keeps console rules; drift → restore + record + state;
  restore failure → `not_restored`; pending changes and console edits are not tampering; missing
  configuration is tampering.
- **Summary / impact:** paginated fake `list-object-versions`; age/rank bucketing; exact impact for
  days, newest-N and combined rules.
- **Migration:** backstop rules → folder rules; keeps-less items become pending.
- **Permissions level 4:** role `BaseBucketRules` resource is exactly the base bucket ARN; runtime
  policy has no `DeleteObjectVersion`; fingerprint + history bump; Verify negative probe now uses
  `get-bucket-tagging` (NoSuchTagSet → not ok; AccessDenied → ok).
- **Routes:** CSRF; no AWS on GET; token binding/expiry; typed confirmation; vocab + mono laws.
- **bats:** archive prune gone from `backup-job.sh`; the tamper check runs before the run and a
  failing check doesn't stop it; `tofu fmt -check`.

## Build order (one plan, three phases)

- **A — engine & safety:** rules model + `desired`/read/merge/apply, `storage.json` (defaults +
  per-folder undo days), permissions level 4 (+ Verify probe switch), removals (§7), job save/delete
  and setup apply the rules (a job's own setting applies immediately, exactly as the removed archive
  prune enforced it), tamper check of the app's lifecycle rules before every run + acknowledge, Setup
  row + Board blocker + Activity label, migration (legacy rules replaced on first apply).
- **B — visibility & control:** `classify` + keeps-less gating (the preview/confirm flow, incl. the
  wizard's Plain copy shortening), storage summaries + impact, the S3 rules screen (A overview + B
  editor), versioning intent + its tamper coverage, housekeeping editing, the combined
  "newest N + days" wizard option, job page line.
- **C — cheaper tier:** tier fields in rules/editor/preview + the estimate note.

Estimator (§10): the adapters already model Plain copy days/count/keep_all from the job's setting,
which S3 now honours exactly — no adapter change in A; the tier note lands in C.
