# unraid-s3-backup

Off-site, encrypted AWS S3 backups for an Unraid server — the off-site leg of a 3-2-1 strategy.
A single configuration-driven Docker container ships two things to S3:

- **appdata** — the [Appdata Backup plugin](https://forums.unraid.net/topic/137710-plugin-appdatabackup/)'s
  archives, via `restic` (encrypted, deduplicated, point-in-time snapshots) → S3 Standard.
- **media** — selected large, mostly-static collections (comics/books/etc.), via `rclone` → S3
  **Glacier Deep Archive** (cheapest cold storage).

Reusable and not tied to any one setup: all user-specific values live in mounted config files;
the AWS destination is provisioned by the included OpenTofu module.

> **Status:** in design. See the design spec:
> [`docs/superpowers/specs/2026-06-22-unraid-s3-backup-design.md`](docs/superpowers/specs/2026-06-22-unraid-s3-backup-design.md).

## Prerequisites

- An **Unraid** server.
- The **Appdata Backup plugin** (`appdata.backup`) installed and configured — **required**
  (this tool ships its output off-site).
- An **AWS account** (the `opentofu/` module creates the bucket + least-privilege IAM user).

Setup, configuration, and restore runbooks will be documented here as implementation lands.
