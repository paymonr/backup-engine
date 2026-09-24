# opentofu/ — AWS destination module

This module is the single definition of the AWS destination for
`backup-engine`: one hardened S3 bucket plus a least-privilege,
object-only IAM user that the backup container uses at runtime.

It provisions:

- **`aws_s3_bucket`** — the off-site backup bucket, with:
  - **Versioning** enabled on first apply (protects against
    ransomware/accidental delete — `restic` and `rclone` both rely on this
    for safe pruning). After that, backup-engine owns it (one owner per
    setting, like lifecycle rules below): `aws_s3_bucket_versioning` carries
    a `lifecycle { ignore_changes = [versioning_configuration] }`, so a
    later `tofu apply` (e.g. to rotate the runtime key) never flips an
    app-side suspend/resume back to `base_bucket_versioned`'s value.
  - **Default server-side encryption** (SSE-S3 / `AES256`).
  - **Public access fully blocked** (`aws_s3_bucket_public_access_block`,
    all four flags `true`).
  - **`BucketOwnerEnforced`** object ownership (ACLs disabled — the bucket
    owner always owns every object).
  - **No lifecycle rules** (and no lifecycle variables). backup-engine
    writes the bucket's S3 lifecycle rules itself once it is set up, through
    the bucket-admin role below: a Plain copy job's history setting *is* its
    folder's rule, Snapshot and File history folders get an undo window
    (default 30 days), and one housekeeping rule clears abandoned uploads
    and leftover delete markers. Until the app applies them the bucket keeps
    every old version (the safe direction). Re-running `tofu apply` with
    state from an older version of this module only *forgets* its old
    lifecycle configuration (a `removed` block) — nothing in the bucket
    changes, so the rules backup-engine wrote and any rule you added in the
    console stay exactly as they are.
- **`aws_iam_user.runtime`** + a single inline policy — the credentials the
  container uses. It is scoped to:
  - `s3:ListBucket` / `s3:GetBucketLocation` on the bucket itself (required
    for `restic`/`rclone` to list and locate the bucket).
  - `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, multipart-upload
    actions, and `s3:RestoreObject` (needed to thaw Glacier/Deep Archive
    objects for media restores) — **only** on `appdata/*` and `media/*`
    object keys.
  - **No bucket-configuration permissions** (versioning, lifecycle, SSE,
    public-access-block, ownership controls) are granted to this user
    itself — encryption, public access and ownership are set by whoever
    runs `tofu apply`; lifecycle rules and versioning go through the
    bucket-admin role below. The runtime user can only read/write/delete objects
    under the two backup prefixes. It has **no `s3:DeleteObjectVersion`**:
    the key can only soft-delete, so the undo window is the recovery window.
  - `sts:AssumeRole` on the bucket-admin role, plus the same list/object
    rights on `<bucket>-*` dedicated buckets.
- **`aws_iam_role.bucket_admin`** — assumed by the runtime user only when
  needed: it manages the base bucket's lifecycle rules and versioning
  (backup-engine's S3 rules), and creates, configures and tears down
  `<bucket>-*` dedicated buckets. Its policy is
  `provisioning/bucket-admin-policy.json.tmpl`.

## Usage

Requires [OpenTofu](https://opentofu.org/) >= 1.7.0 and AWS admin
credentials available to your shell (e.g. via `AWS_PROFILE`,
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, or an SSO session) — **not**
inside the backup container. This module is meant to be run once (or
whenever the destination configuration changes) from your own machine.

```bash
cd opentofu
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars   # set region + a globally-unique bucket_name

tofu init
tofu plan  -var-file=terraform.tfvars
tofu apply -var-file=terraform.tfvars
```

(You can also pass `-var region=... -var bucket_name=...` directly instead
of a tfvars file.)

### Reading outputs into the container's config

The module's outputs map directly onto the values the engine expects in
`config/backup.env` and `config/secrets.env` (see
`config/backup.env.example` / `config/secrets.env.example`):

```bash
# config/backup.env
AWS_REGION=$(tofu output -raw region)
S3_BUCKET=$(tofu output -raw bucket_name)
RESTIC_REPOSITORY=$(tofu output -raw restic_repository)

# config/secrets.env (chmod 600, never commit)
AWS_ACCESS_KEY_ID=$(tofu output -raw runtime_access_key_id)
AWS_SECRET_ACCESS_KEY=$(tofu output -raw runtime_secret_access_key)
```

`rclone_remote` is exposed as a map (`type`, `provider`, `region`,
`storage_class`, `bucket`) as a convenience for adopters who want to inspect
or script against the provisioned destination in rclone's own field names
(`tofu output -json rclone_remote`). It's purely informational — `setup.sh`
does not read it, and the running container renders its own `rclone.conf`
directly from `backup.env`/`secrets.env` at startup (see
`scripts/lib/rclone-conf.sh`), not from this output.

## Security note

**Admin credentials — the ones used to run `tofu apply` — must live only in
the environment of the machine/CLI running OpenTofu, and must never be
copied into the container's config, image, or environment.** The container
only ever receives the narrow `runtime` IAM user's access key, which cannot
touch bucket configuration directly or objects outside `appdata/` and
`media/`. Through the bucket-admin role it can manage the base bucket's
lifecycle rules and versioning — backup-engine checks its own rules before
every backup run, puts back anything changed outside the app and flags it,
and flags any new rule that could delete or move backups. Re-running `tofu apply` (e.g. to rotate the runtime key by
tainting `aws_iam_access_key.runtime`) still only requires admin
credentials on the operator's machine, never inside the running backup
stack.

## State

This module keeps state locally by default (no backend is configured).
Treat `terraform.tfstate` as sensitive — it contains the runtime user's
secret access key in plaintext — and never commit it (`.gitignore` already
excludes `*.tfstate*`, `.terraform/`, and `*.tfvars` other than
`*.tfvars.example`). If you manage multiple installs or want shared/remote
state, add a `backend` block to `versions.tf` appropriate to your
environment (S3+DynamoDB, Terraform Cloud, etc.) before running `tofu
init`.
