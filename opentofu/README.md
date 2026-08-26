# opentofu/ — AWS destination module

This module is the single definition of the AWS destination for
`unraid-s3-backup`: one hardened S3 bucket plus a least-privilege,
object-only IAM user that the backup container uses at runtime.

It provisions:

- **`aws_s3_bucket`** — the off-site backup bucket, with:
  - **Versioning** enabled (protects against ransomware/accidental delete —
    `restic` and `rclone` both rely on this for safe pruning).
  - **Default server-side encryption** (SSE-S3 / `AES256`).
  - **Public access fully blocked** (`aws_s3_bucket_public_access_block`,
    all four flags `true`).
  - **`BucketOwnerEnforced`** object ownership (ACLs disabled — the bucket
    owner always owns every object).
  - A **lifecycle backstop** on the `appdata/` and `media/` prefixes:
    noncurrent object versions expire after
    `var.noncurrent_version_expiration_days` (default 30) and incomplete
    multipart uploads are aborted after `var.abort_incomplete_multipart_days`
    (default 7), so failed/interrupted uploads and old versions don't
    silently accumulate cost.
- **`aws_iam_user.runtime`** + a single inline policy — the credentials the
  container uses. It is scoped to:
  - `s3:ListBucket` / `s3:GetBucketLocation` on the bucket itself (required
    for `restic`/`rclone` to list and locate the bucket).
  - `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, multipart-upload
    actions, and `s3:RestoreObject` (needed to thaw Glacier/Deep Archive
    objects for media restores) — **only** on `appdata/*` and `media/*`
    object keys.
  - **No bucket-configuration permissions** (versioning, lifecycle, SSE,
    public-access-block, ownership controls) are granted to this user —
    those are creation-time, admin-only operations performed by whoever
    runs `tofu apply`. The runtime user can only read/write/delete objects
    under the two backup prefixes.

## Usage

Requires [OpenTofu](https://opentofu.org/) >= 1.6.0 and AWS admin
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
`storage_class`, `bucket`) for tooling (e.g. Task 9's `setup.sh`) that
renders `rclone.conf` from structured values rather than shelling out
per-field; read it with `tofu output -json rclone_remote`.

## Security note

**Admin credentials — the ones used to run `tofu apply` — must live only in
the environment of the machine/CLI running OpenTofu, and must never be
copied into the container's config, image, or environment.** The container
only ever receives the narrow `runtime` IAM user's access key, which is
incapable of touching bucket configuration (versioning, lifecycle,
encryption, public-access-block) or objects outside `appdata/` and
`media/`. Re-running `tofu apply` (e.g. to rotate the runtime key by
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
