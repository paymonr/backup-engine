# Optional per-job dedicated S3 buckets (just-in-time) — design

Status: draft for review · 2026-09-19 · owner: paymon

## Summary

Today backup-engine provisions **one** S3 bucket at setup and every job shares it
(prefixes `appdata/` for restic, `media/<job>/` for rclone). The runtime IAM key is
**object-only** (RW on those prefixes), with no bucket-creation power.

This feature makes a dedicated bucket an **opt-in, per-job choice**. Most jobs keep
using the shared **base bucket**; a job can instead be given **its own bucket**,
created **just-in-time** when the job is saved. Only the base bucket is provisioned
at setup. Bucket creation is performed with **temporary credentials from an assumed
IAM role**, so the everyday runtime key stays least-privilege.

Motivating drivers (chosen): **isolation / independent lifecycle / per-bucket cost
visibility**, and **per-bucket versioning control**. **Object Lock / WORM is out of
scope for v1** (it forces create-time immutability that conflicts with `restic
--prune`; deferred to a later feature).

## Goals / Non-goals

**Goals**
- Per-job opt-in dedicated bucket; default remains the shared base bucket.
- Just-in-time bucket creation at **job save** (fail fast), not at setup.
- Runtime key stays object-only; bucket creation via **STS AssumeRole** (temporary creds).
- Per-dedicated-bucket **versioning** chosen per job (default ON).
- All three job types eligible (versioned/restic, versioned-files, archive).
- Never auto-delete a job's bucket; deleting a job keeps the bucket + data (and says so).

**Non-goals (v1)**
- Object Lock / WORM (future feature).
- Renaming an existing S3 bucket (not possible in S3).
- Migrating existing data between buckets.
- Changing the frozen estimator (only its adapters may change).

## Decisions (from brainstorming Q&A)

| # | Decision |
|---|----------|
| Trigger | **Per-job opt-in**; default = base bucket. |
| Drivers | Isolation/lifecycle/cost + per-bucket versioning. **Not** Object Lock (v1). |
| Base versioning | **Configurable at setup** (on/off). |
| Credential model | **STS AssumeRole**: runtime key stays object-only + `sts:AssumeRole`; a `bucket-admin` role holds CreateBucket/config; app assumes it for short-lived creds, then discards. |
| Object Lock | **Out of v1.** |
| Bucket on job-delete | **Never auto-delete.** Unlink only; UI states plainly the bucket + data are kept and remain billable. |
| Naming | Prefilled, **editable** suggestion `"<base>-<jobslug>"`. Base name saved from provisioning, editable in Settings. Repointing base after data exists does **not** move data (warn). |
| Eligibility | **All three** job types. (A restic job in its own bucket gets its own repo — no cross-job dedup; acceptable.) |
| Create timing | **At job save**, fail fast (errors surface in the wizard). |
| RW scoping | **Hybrid**: runtime policy wildcard `arn:aws:s3:::<base>-*/*` covers prefix-matching names with no IAM change; if a chosen name is **off-prefix**, the bucket-admin flow **explicitly grants** that bucket's ARN to the runtime principal. |
| Dedicated versioning | **Per-job toggle, default ON.** |

## Architecture

### 1. Provisioning / IAM (setup, admin key)

In addition to today's runtime user + object-only inline policy, setup creates:

- **`<prefix>-bucket-admin` IAM role** with a policy allowing bucket create + config:
  `s3:CreateBucket`, `s3:PutBucketVersioning`, `s3:PutBucketPublicAccessBlock`,
  `s3:PutBucketOwnershipControls`, `s3:PutEncryptionConfiguration`,
  `s3:PutLifecycleConfiguration`, `s3:PutBucketTagging`, `s3:GetBucketLocation`,
  `s3:GetBucketVersioning`. `Put*` actions are resource-scoped to
  `arn:aws:s3:::<base>-*` where AWS allows; `CreateBucket` cannot be tightly
  resource-scoped (documented residual).
- **Trust policy** on the role: allow the runtime IAM user to `sts:AssumeRole`
  (scoped to that user's ARN; optional `sts:ExternalId`).
- **Off-prefix grant capability** (only for the hybrid RW path): a dedicated
  **customer-managed policy** (e.g. `<prefix>-runtime-extra-buckets`) attached to
  the runtime user, which the role may version via `iam:CreatePolicyVersion` /
  `iam:DeletePolicyVersion` **scoped to that one policy ARN**. This is preferred
  over `iam:PutUserPolicy` (which would let the role rewrite the user's whole
  policy). Flag: any IAM-write grant is sensitive — keep it to the single policy ARN.

Runtime user gains:
- `sts:AssumeRole` on the `bucket-admin` role ARN.
- Object RW + List **wildcard**: `arn:aws:s3:::<base>-*` (List) and
  `arn:aws:s3:::<base>-*/*` (objects), alongside the existing base-bucket statements.

Setup persists: base bucket name, region, `bucket-admin` role ARN, and the
extra-buckets managed-policy ARN (in `backup.env` / config).

### 2. Job model

`jobs.json` job gains:
- `bucket`: the target bucket name (absent/empty ⇒ base bucket).
- `dedicated`: bool (true ⇒ this job owns `bucket`).
- `bucket_versioned`: bool (only meaningful when `dedicated`; default true).

The create-job wizard gets a **Storage** section: a "Give this job its own bucket"
toggle → reveals an **editable bucket name** (prefilled `"<base>-<jobslug>"`) and a
**"Versioned" toggle** (default on). The wizard shows which RW path applies:
"name keeps your prefix → covered automatically" vs "off-prefix → we'll grant access
to this bucket explicitly."

### 3. Just-in-time bucket creation (at job save)

When a job is saved with `dedicated=true` and the bucket does not yet exist:
1. Runtime key calls `sts:AssumeRole(<bucket-admin>)` → temporary creds.
2. With temp creds: `CreateBucket(name, region)` → `PutPublicAccessBlock` (all on),
   `PutBucketOwnershipControls` (BucketOwnerEnforced), `PutEncryptionConfiguration`
   (AES256), `PutBucketVersioning` (per toggle), `PutLifecycleConfiguration`
   (noncurrent-version expiration + abort-incomplete-multipart, mirroring base),
   `PutBucketTagging` (e.g. `managed-by=backup-engine`, `job=<name>`).
3. If the name is **off-prefix**, add its ARN to the runtime `extra-buckets` managed
   policy (new policy version).
4. Discard temp creds. Idempotent: `BucketAlreadyOwnedByYou` ⇒ reuse; verify config.

This runs behind the existing **capability-preflight** style so failures are guided.

### 4. Runner / engine target derivation

The job's `bucket` flows into the run environment (e.g. `JOB_BUCKET`), and the engines
target it instead of the single `S3_BUCKET`:
- restic: `RESTIC_REPOSITORY = s3:<host>/<JOB_BUCKET>/appdata`
- rclone: `s3:<JOB_BUCKET>/media/<job>`
- vfiles: catalog/repo under `<JOB_BUCKET>` with its existing prefix.

Base jobs keep today's derivation (`JOB_BUCKET` defaults to base). `scripts/lib/config.sh`
and `backup-job.sh` are parameterized by `JOB_BUCKET`; `jobs_io`/`runner` thread it in.

### 5. Settings

A Settings surface shows the **base bucket name** (prefilled from provisioning,
editable) + region. Editing the base name **repoints** future base-job writes; it does
**not** move existing data — shown with a clear warning. (Also drives the `<base>-…`
suggestion prefix for new dedicated buckets.)

### 6. Teardown

JIT buckets are created **outside OpenTofu** (by the app via assume-role), so
`tofu destroy` will not see them. Teardown must **enumerate** backup-engine-managed
buckets (by tag `managed-by=backup-engine`) and empty+delete them explicitly. Deleting
a bucket needs empty-then-delete perms (`DeleteObject[Version]` + `DeleteBucket`) —
granted to the **bucket-admin role for teardown only**, never to the everyday runtime
key. (Consistent with "never auto-delete on job delete.")

### 7. Cost model

Per-bucket storage cost math is unchanged (cost is per object/GB regardless of bucket).
Real-spend/usage adapters that group by prefix may gain per-bucket grouping. **The
estimator stays frozen** (hash-guarded); only `estimate_io` / GUI adapters change.

## Data model / config changes

- `jobs.json`: `bucket`, `dedicated`, `bucket_versioned` per job.
- config (`backup.env` or new): `S3_BUCKET` (base), `AWS_REGION`, `BUCKET_ADMIN_ROLE_ARN`,
  `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`, base-versioning flag.
- `provisioning/iam-policy.json.tmpl`: add STS + wildcard RW; new template(s) for the
  bucket-admin role and the extra-buckets managed policy.
- `opentofu/main.tf`: add role + trust policy + managed policy; base-versioning made a
  variable; base bucket unchanged otherwise.

## Error handling

- **CreateBucket failures** surfaced in the wizard (fail fast): name taken globally
  (suggest a new suffix), `TooManyBuckets` (AWS 100-bucket soft cap → link to raise
  quota), `AccessDenied` (guided: role/trust misconfigured), region mismatch.
- **AssumeRole failure** → guided message (trust policy / runtime `sts:AssumeRole`).
- **Off-prefix grant failure** (policy size/version limits) → explain and suggest a
  prefix-matching name (which needs no IAM change).
- Idempotency: re-saving a job whose bucket exists reconciles config, never errors.

## Security considerations

- Everyday runtime key remains object-only + `sts:AssumeRole`; elevated bucket ops use
  **short-lived** assumed-role creds (default ~1h), fully in CloudTrail.
- `CreateBucket` can't be tightly resource-scoped (AWS limitation) — documented residual.
- The off-prefix path's IAM-write is confined to **one managed policy ARN** via
  `iam:CreatePolicyVersion`, not `PutUserPolicy` — smallest viable blast radius.
- Bucket-delete perms live only on the bucket-admin role and are used only by teardown.

## Testing

- IAM policy templates: unit-render + assert least-privilege shape (mirrors
  `tests/engine/test_iam_policy.py`).
- Bucket-name derivation + prefix/off-prefix classification: pure-function tests.
- JIT creation flow: mock STS+S3 (moto or stubs) — create/config idempotency, error mapping.
- Runner target derivation: `JOB_BUCKET` → restic/rclone/vfiles targets (bats + py).
- Wizard: dedicated toggle renders name+versioning; off-prefix hint; save persists fields.
- Teardown: enumerates tagged buckets; empties+deletes; leaves untagged buckets alone.

## Open questions / future

- **Object Lock / WORM** as a follow-on (needs the prune-conflict design).
- Per-bucket lifecycle customization (vs mirroring base defaults) — v1 mirrors base.
- Cross-region dedicated buckets — v1 uses the base region.
- Bucket-quota UX when approaching the 100-bucket cap.
