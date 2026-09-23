# Check & update AWS permissions (provisioning that converges) — design

Status: draft for review · 2026-09-22 · owner: paymon

## Summary

Provisioning today is **create-only and one-shot**. Automated setup runs OpenTofu in a
throwaway temp dir and discards the state, so it cannot tell "already exists" from "needs
creating" — re-running it collides with the existing bucket and runtime user. Every time a
release needs new IAM permissions (08-26 initial, 09-11 archive prune, 09-21 dedicated
buckets), every already-provisioned install is stranded with no way forward short of
hand-written `aws` commands. backup-engine is a published app, so owners can't be expected
to diff IAM by hand.

This feature makes provisioning **converge**: a "Check & update AWS permissions" action
reads the live IAM, compares it to what this build requires, shows the plan, and applies
only the missing pieces — **never touching the bucket and never creating or rotating the
runtime key**. The same step finishes every initial setup, so there is one definition of
"correct IAM". A **permissions level** stamp in `backup.env` lets the app say "this version
needs a permissions update" without any IAM read power of its own.

It also fixes four gaps in initial provisioning found while designing this:
1. The **bucket-admin role has no IAM permissions**, so the off-prefix dedicated-bucket grant
   (`buckets.grant_object_access`, run under the assumed role) fails `AccessDenied` even on a
   freshly provisioned box. The multi-bucket spec called for scoped `iam:CreatePolicyVersion`
   etc.; the template never got it.
2. **`setup.sh` never prints** `BUCKET_ADMIN_ROLE_ARN` / `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`,
   so scripted installs never learn about the role.
3. **Guided-manual omits** the dedicated-bucket pieces entirely.
4. The wizard's **"own bucket" toggle** with no role configured produces a raw AWS error.

## Goals / Non-goals

**Goals**
- Bring an already-provisioned install's IAM up to the current build's requirement, in place.
- Two ways to apply: paste short-lived admin creds in the app, **or** run a generated,
  re-runnable script yourself (no admin creds ever reach the container) and Verify.
- Show the plan before (Preview) and the result after (per-step status + commands).
- One required-set definition; all three setup paths end verified + stamped.
- Surface "permissions behind" on Setup, Board, Destination and the job wizard — with no AWS calls at render.

**Non-goals**
- The bucket and its settings (encryption, public-access block, ownership, versioning,
  lifecycle) — IAM only. Lifecycle has its own Phase-2 backlog item.
- Creating, rotating or deleting the runtime access key or user.
- Continuous IAM drift detection (the stamp is advisory; Check is the authoritative read).
- Cross-account setups, a configurable name prefix (stays `backup-engine`), Object Lock.

## Decisions (from brainstorming Q&A)

| # | Decision |
|---|----------|
| Update paths | **Admin creds in-app (primary) + generated commands (fallback).** Scripted owners may also re-run `setup.sh` if they kept its tofu state. |
| Scope | **IAM only.** |
| Engine | **aws-CLI converge in Python** (approach A). Policy *content* stays in `provisioning/*.tmpl`; tofu keeps creating bucket/user/key; a drift-guard test pins `main.tf`'s IAM resource set to the converge table. Rejected: persisted tofu state + import (can't adopt the access-key secret; state holds the runtime secret in plaintext; plan is hard to render as commands), throwaway tofu + generated imports (same key problem, most fragile). |
| Required set | One set for everyone, including the dedicated-bucket pieces (owner: "this should all be part of initial provisioning"). |
| Detection | `PERMISSIONS_VERSION` stamp in `backup.env` vs the build's required level; missing stamp on a provisioned box = "not checked for this version". |
| Preview vs apply | Primary **Update permissions** = discover → plan → apply in one request. **Preview only** is read-only. Creds are never re-rendered, so applying after a preview means pasting again — the app never holds admin creds across requests. |
| Notice placement | Setup row + a non-dismissible Board notice + wizard toggle + Destination line. Not a site-wide banner. |

## Architecture

### 1. The required permission set and levels

**`provisioning/permissions.json`** — the single source of the level:

```json
{
  "level": 3,
  "templates_sha256": "<sha256 over the three provisioning/*.tmpl files, sorted by name>",
  "history": [
    {"level": 1, "adds": "Object access to the backup bucket", "features": []},
    {"level": 2, "adds": "Archive pruning (list + delete old object versions)", "features": ["archive-prune"]},
    {"level": 3, "adds": "Dedicated per-job buckets (bucket-admin role + extra-buckets policy)", "features": ["dedicated-buckets"]}
  ]
}
```

Read by Python (`permissions.required_level()`, `permissions.history()`) and by tofu
(`output "permissions_level"` via `jsondecode(file(...))`). **Bump rule:** any change to a
`provisioning/*.tmpl` bumps `level`, appends a history line and updates `templates_sha256`;
a test recomputes the hash and fails with that instruction if it doesn't match.

**Required set** (fixed names, prefix `backup-engine`):

| # | Resource | Name | Converge rule |
|---|----------|------|---------------|
| R1 | Extra-buckets managed policy | `backup-engine-runtime-extra-buckets` | Create with the placeholder doc if missing. **Contents never rewritten** — the app appends per-bucket grants at runtime. |
| R2 | R1 attached to the runtime user | — | Attach if missing. |
| R3 | Bucket-admin role + trust | `backup-engine-bucket-admin` | Create if missing; trust = only the runtime user may `sts:AssumeRole`; replace trust if it differs. |
| R4 | Role inline policy | `backup-engine-bucket-admin-create-config` | Put if missing or different. |
| R5 | Runtime user policy | `backup-engine-runtime-object-only` | Put (inline) if missing or different; legacy **managed** shape → publish a new default version. Rendered with the real role ARN (so it includes the AssumeRole + `<base>-*` wildcard statements). |

**Template fix (level 3):** `bucket-admin-policy.json.tmpl` gains a `PolicyGrant` statement —
`iam:GetPolicy`, `iam:GetPolicyVersion`, `iam:ListPolicyVersions`, `iam:CreatePolicyVersion`,
`iam:DeletePolicyVersion` with `Resource` = **exactly** the R1 policy ARN (new template var
`${extra_buckets_policy_arn}`; `render_bucket_admin_policy` and `main.tf`'s `templatefile()`
pass it — no dependency cycle, R1 doesn't reference the role).

**Runtime principal discovery:** the runtime user is never assumed by name. Its identity comes
from the stored runtime key via `sts get-caller-identity` (callable by any key). If that ARN is
not `arn:aws:iam::<acct>:user/<name>` (role, assumed-role, root) → refuse. A guided owner who
named their user differently still works, and the action can't be aimed at another user.
Automated setup is the exception: it takes the user's ARN from a new tofu output
(`runtime_user_arn`), because a brand-new key can fail STS for a few seconds (propagation).

### 2. Engine — `app/gui/permissions.py`

Split so the logic is testable without AWS:

- **`discover(admin_creds, runtime_principal, region, run=...) -> Live`** — I/O. Preflight via the
  existing `provision.verify_admin_can_provision`; admin account (`provision.aws_account_id`) must
  equal the runtime principal's account, else refuse. Reads: runtime user's inline policies +
  attached managed policies (+ default-version doc of a legacy managed
  `backup-engine-runtime-object-only`), `get-role` (trust), `get-role-policy`, `get-policy` for R1.
  NoSuchEntity → "absent", not an error.
- **`plan(live, required) -> list[Step]`** — pure. Each `Step` = `{id, resource, action
  (create|attach|update-trust|put|new-version), summary, argv, diff}`. Policy comparison is
  **normalized** (parse JSON; `Action`/`Resource` string→sorted list; statements keyed by `Sid`;
  key order ignored), so a formatting-only difference is no step. `diff` is per-statement
  (`+ AssumeBucketAdminRole: sts:AssumeRole on role/backup-engine-bucket-admin`, `~ ObjectRW: …`, `- …`).
- **`apply(steps, admin_creds, run=...) -> Result`** — I/O. Dependency order **R1 → R2 → R3 →
  R4 → R5** (R5 references the role ARN). Every step is idempotent. Stop at the first failure;
  `Result` marks each step done / failed / not-run and carries the scrubbed error. The
  managed-version path deletes the oldest non-default version at the 5-version limit
  (factor the existing logic out of `buckets.grant_object_access` into a shared helper).
- **`converge(..., apply_changes=True)`** (Update) = discover → plan → apply → **re-discover + re-plan must be empty** → only then
  write `BUCKET_ADMIN_ROLE_ARN`, `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`, `PERMISSIONS_VERSION`,
  `PERMISSIONS_CHECKED_AT` to `backup.env` and append a `permissions` record to Activity
  (`_system.runs.jsonl`, same shape as `provision.record_setup`) with the step summaries.
- **`converge(..., apply_changes=False)`** (Preview / Check) = discover → plan, no AWS writes. An empty plan writes the two ARNs + the
  stamp to `backup.env` (local file only) and records Activity — that's how an install that's
  already correct gets marked current.

**Never-touch guarantees** (enforced by tests on the fake runner): no `s3`/`s3api` calls; no
`create-access-key`/`delete-access-key`/`update-access-key`; no `delete-*` IAM calls except
`delete-policy-version` on R5's legacy managed policy; R1's document is never written after creation.

**Commands fallback — `script(account, user, bucket, region) -> str`.** No admin creds and no
live read: the runtime key supplies account + user; the app renders **one bash script** that is
itself convergent (`get-policy … >/dev/null 2>&1 || create-policy …`; `get-role … || create-role …`;
then the always-idempotent `update-assume-role-policy`, `put-role-policy`, `attach-user-policy`,
`put-user-policy`). Policy docs are embedded as quoted heredocs (`<<'EOF'`) written to a
`mktemp -d` dir and passed via `file://`. Written for AWS CloudShell. For a legacy managed-shape
install the script puts the inline policy and includes a commented, optional `detach-user-policy`
for the old managed one (the union is still least-privilege). **Injection guard:** account
(`^\d{12}$`), IAM user name (`^[\w+=,.@-]{1,64}$`), bucket (`buckets.valid_bucket_name`) and region
(`^[a-z]{2}(-[a-z]+)+-\d$`) are validated before interpolation; anything else refuses to render.

**Verify (runtime key only)** — functional probes after the owner runs the script:
1. `s3api list-object-versions --bucket <base> --max-items 1` → level-2 statements present.
2. `sts assume-role` on the derived role ARN → R3 exists, trust OK, R5's AssumeRole statement OK.
3. With the assumed creds, `iam get-policy` on the derived R1 ARN → R4's PolicyGrant fix works and
   R1 exists; `AttachmentCount ≥ 1` → R2.

Probes retry briefly (3 tries, ~5 s apart) for IAM propagation. All pass → write ARNs + stamp +
Activity record. Any fail → per-probe ✓/✗ lines with a hint naming the script step.

### 3. Screens

No render calls AWS; everything below reads `backup.env` only.

- **Setup readiness row "AWS permissions"** (`readiness._check_permissions`): `ok` when stamp =
  required; `warn` with the missing history lines ("Dedicated buckets need a permissions update")
  when behind; `warn` "Not checked for this version of backup-engine" when a provisioned install
  has no stamp. Fix link → `/setup/permissions`. Absent when not provisioned (the wizard owns that).
- **Board notice** under the header when behind/unchecked: "This version needs an AWS permissions
  update — **Update →**". Not dismissible; gone once current.
- **Feature gating is history-driven:** a feature is available when the stamp ≥ the level whose
  history entry lists it (`permissions.feature_available(config_dir, "dedicated-buckets")`), so a
  future bump for an unrelated feature doesn't re-disable dedicated buckets.
- **Job wizard:** "own bucket" toggle disabled with "Needs a one-time AWS permissions update →"
  when `dedicated-buckets` isn't available or `BUCKET_ADMIN_ROLE_ARN` is empty. Server-side, a dedicated create with that
  state returns the same guided message instead of a raw AWS error.
- **Destination page (provisioned):** "Permissions: level N of 3 · checked 22 Sep" + button.
- **`/setup/permissions`:**
  - Header: current vs required level + the history lines that are missing.
  - **Use admin credentials** (key, secret, optional session token): **Update permissions**
    (primary) → results page with every step's status, expandable command + policy diff, and the
    persistent "delete that admin access key in AWS now" warning. **Preview only** → the plan, no
    writes, creds cleared.
  - **Run the commands yourself:** a **Show the commands** POST (it needs one runtime-key STS call,
    so not on GET) → the script with a copy button + CloudShell hint, then **Verify** → per-probe
    results. Scripted owners get a note: re-run `setup.sh` if you still have its tofu state.

Routes (all POSTs CSRF-checked): `GET /setup/permissions`, `POST /setup/permissions/preview`,
`POST /setup/permissions/update`, `POST /setup/permissions/script`, `POST /setup/permissions/verify`.

### 4. Initial setup paths

- **Automated:** after `run_tofu_apply` succeeds, call `permissions.converge(...)` with the same
  in-frame admin creds and the tofu-output runtime user name. Normally the plan is empty → stamp
  only; a non-empty plan is repaired and recorded. **If this step fails, setup still succeeds**
  (the bucket + key are real and tofu's state is already gone — failing would strand a half-saved
  install): save the runtime key as today and flash a warning linking to `/setup/permissions`.
- **Scripted:** `setup.sh` also prints `BUCKET_ADMIN_ROLE_ARN`, `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`
  and `PERMISSIONS_VERSION` (from tofu outputs). Re-running it with its local state is already an update.
- **Guided-manual → two phases.** Phase 1 = today's flow (bucket, user, base policy, key, Validate),
  except the base policy becomes an **inline** user policy (`put-user-policy` / console "Create
  inline policy") rather than managed, so the later full policy overwrites the same name. On
  Validate success, redirect to `/setup/permissions` with the commands path open (the runtime key
  now yields account + user) → run script → Verify → stamped.

`config/backup.env.example` gains `PERMISSIONS_VERSION=` and `PERMISSIONS_CHECKED_AT=`
(`write_backup_env` only persists template keys). `provision.py`'s "the ONLY module that…calls
aws" header is updated to name `permissions.py` alongside it.

## Error handling

| Case | Behaviour |
|------|-----------|
| Admin creds can't reach IAM / expired session | Reuse `AdminCapabilityError` messages (token vs permission). Nothing changed. |
| Admin creds lack one IAM action mid-apply | Stop; name the denied action from the scrubbed error; completed steps listed; "safe to run again". |
| Admin account ≠ runtime key's account | Refuse before any read. |
| Runtime key's identity isn't an IAM user / user gone | Refuse with a plain message (the key or user was deleted/replaced). |
| Legacy managed policy at 5 versions | Delete oldest non-default, then publish. |
| Verify fails right after the script | Built-in short retry; then per-probe ✗ with the script step to re-check. |
| Automated setup's final update fails | Setup succeeds; warning flash → `/setup/permissions`. |
| Any output | Admin key/secret/token scrubbed (`provision._scrub`). |

## Security considerations

- Admin creds: transient, env-only to the `aws` subprocess, never stored, never re-rendered,
  scrubbed from all output, dropped from the frame in `finally` — identical to Automated setup.
- Confinement: fixed `backup-engine-*` names only; runtime user resolved from its own key; account match enforced.
- The new role permission is IAM-write on **one** policy ARN (the multi-bucket spec's smallest
  viable blast radius); the role remains assumable only by the runtime user.
- The stamp only controls messages; editing it grants nothing.
- Generated script: strict validation of every interpolated value; policy JSON in quoted heredocs.

## Testing

- **Planner** (table-driven fake `Live`): level-1 install, level-2 install (the owner's box: R1–R4
  absent, R5 lacking AssumeRole/wildcards), fully current (empty plan), legacy managed-shape R5,
  drifted trust, formatting-only difference (no step), 5-version limit.
- **Never-touch guarantees** on the fake runner (see §2).
- **Apply:** R1→R5 order; stop-on-first-failure report; second run → empty plan; stamp written
  only after the re-plan is empty.
- **Drift guards:** `main.tf` IAM resource names ↔ the required-set table; `templates_sha256` ↔ templates.
- **Templates:** `PolicyGrant` resource is exactly the R1 ARN (least-privilege shape, alongside
  `tests/*/test_iam_policy.py`).
- **Script:** snapshot, injection cases (hostile user/bucket/region refused), `bash -n`.
- **Routes:** CSRF; creds never in HTML; scrubbed errors; Verify results; Board notice; Setup row;
  disabled wizard toggle + server-side guided message; automated-setup soft-fail path.
- **`setup.sh`:** bats test for the three new output lines.
- **Live dogfood:** deploy to the box; Preview with `opentofu-admin` — expected plan = create R1,
  attach R2, create R3, put R4, update R5; Update; Board notice clears; save a dedicated-bucket job
  then tear it down.

## Rollout

Existing installs have no stamp → Setup row + Board notice; nothing breaks, gated features stay
disabled with a link. New installs are stamped by whichever setup path they use. Future IAM
changes follow the bump rule, and every install gets the same notice → Update path.

## Addendum (2026-09-22, after the final review) — prefix-only dedicated buckets

**Why.** The final whole-branch review found an account-level privilege escalation in the
multi-bucket design this feature was converging to: the runtime key may `sts:AssumeRole` the
bucket-admin role; the role (after the "PolicyGrant" fix in §1) may `iam:CreatePolicyVersion
--set-as-default` on the extra-buckets policy; and that policy is attached to the runtime user.
A leaked `secrets.env` → publish `{"Action":"*","Resource":"*"}` → full admin. Separately, the
role's S3 statements were on `"*"`, so a leaked key could (via the role) empty/delete or
re-lifecycle ANY bucket in the account. Owner decision: **prefix-only dedicated buckets.**

**Changes (supersede §1's R1/R2 and the PolicyGrant fix):**
- A dedicated bucket's name MUST be `<base>-<suffix>` (non-empty suffix). The runtime policy's
  existing `<base>-*` wildcard statements already grant it — no IAM write is ever needed at runtime.
  `job_save` refuses any other name with a guided message; the off-prefix grant path
  (`buckets.grant_object_access`) is deleted.
- The extra-buckets managed policy and its attachment are **removed from the required set**, from
  OpenTofu, from the script, from `backup.env` (`RUNTIME_EXTRA_BUCKETS_POLICY_ARN`), from
  `setup.sh` output, and from Verify. Installs that already have them keep them (inert: placeholder
  Deny, and nothing can version it any more); the script prints an optional detach command.
- The bucket-admin role's S3 statements are scoped to `arn:aws:s3:::<base>-*` (bucket) and
  `arn:aws:s3:::<base>-*/*` (objects); only `s3:ListAllMyBuckets` stays on `"*"` (list-only;
  AWS doesn't support scoping it). No IAM actions at all. The base bucket itself does not match
  `<base>-*`, so the role can never touch it.
- **Required set is now R1 role + trust, R2 role inline policy, R3 runtime user policy.** On the
  owner's box the expected plan is R1 create-role, R2 put-role-policy, R3 put-user-policy.
  Existing installs with the old unscoped role policy get R2 "Update" — converge narrows them.
- Level 3 is unreleased (nothing is stamped at 3 anywhere), so it keeps its number and meaning
  ("Dedicated per-job buckets"); only `templates_sha256` changes.
- Verify probes: (1) runtime key lists object versions in the base bucket; (2) runtime key
  assumes the role; (3) the assumed role can `s3api list-buckets` (proves the role policy is on).

**Also folded in (review Important 2, Minors 4–10, deploy hygiene):** a new provision/re-setup
clears the stamp (+ role ARN on guided Validate and on an automated setup whose final converge
fails; a Keys save that changes the bucket or submits a new runtime key clears the stamp only — the
role ARN is a visible Keys field, and gating needs the stamp anyway); converge's post-apply re-check retries
briefly for IAM eventual consistency; the delete-admin-key reminder also follows a Preview that
found nothing to do; shape checks use `re.fullmatch` + `re.ASCII`; `current_level` uses
`isdecimal`; unparseable AWS JSON becomes a clean error; the static messages drop `Markup` (tests
compare unescaped text); `_run_admin` reuses `_guard`; `*.tfstate*` / `.terraform/` never reach
the image (`.dockerignore`) or the automated-setup temp copy; a regression test proves no GET
calls AWS.

**Follow-up (fix-wave re-review, owner-approved):** Verify gains a 4th probe — under the assumed role,
`s3api get-bucket-versioning --bucket <base>` must be **AccessDenied** (the narrowed role reaches only
`<base>-*`; an old unscoped role would succeed), so Verify can never stamp an un-narrowed role. And the
base bucket name is shape-checked (`[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]`, no `..`, ASCII, fullmatch) before
it is rendered into any IAM policy document, so a Keys edit like `*` can't widen the `<base>-*` confinement.
