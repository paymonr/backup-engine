#!/usr/bin/env bash
# scripts/restore.sh <job> ... — guided restore, dispatched by the job's type
# (read from config/jobs.json via app.gui.jobs_io), mirroring backup-job.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"
# shellcheck source=lib/config.sh
source "$HERE/lib/config.sh"
# shellcheck source=lib/rclone-conf.sh
source "$HERE/lib/rclone-conf.sh"

usage() {
  cat <<EOF
usage:
  restore.sh <job> list
  restore.sh <job> restore <snapshot-id|latest> <target-dir>
  restore.sh <job> thaw <prefix> [--tier Bulk|Standard|Expedited] [--dry-run]
  restore.sh <job> download <prefix> <target-dir>
EOF
}

# shellcheck disable=SC2015 # deliberate: no-op when backup.env is absent, not an if/else
_load() { [ -f "${CONFIG_DIR:-/config}/backup.env" ] && load_config "${CONFIG_DIR:-/config}" || true; }

_is_cold() { case "$1" in GLACIER|DEEP_ARCHIVE|GLACIER_IR) return 0 ;; *) return 1 ;; esac; }

# versioned jobs restore FROM S3, not from the job's local source tree — the
# primary restore scenario is a fresh/rebuilt machine where that source is
# absent or empty. So validate AWS + restic config only; do NOT require the
# job's local source (backup-job.sh's validate_source), which restore must
# not depend on. All versioned jobs share one repo, tag-scoped by job name.
_restore_versioned() {
  local job="$1"; shift
  validate_common
  require_env RESTIC_PASSWORD RESTIC_REPOSITORY
  export RESTIC_CACHE_DIR="$CACHE_DIR/restic"
  case "${1:-}" in
    list) restic -r "$RESTIC_REPOSITORY" snapshots --tag "$job" ;;
    restore)
      local snap="${2:?snapshot id or 'latest'}" target="${3:?target dir}"
      if _is_cold "${JOB_STORAGE_CLASS:-STANDARD}"; then
        log_warn "job '$job' class $JOB_STORAGE_CLASS is cold; restore needs thawed packs. If restore errors on a data read, thaw the appdata/ prefix first (see docs) then retry."
      fi
      mkdir -p "$target"
      # --tag only applies when snapshotID is "latest" (restic ignores it for
      # an explicit ID); scoping it here keeps "latest" job-specific in the
      # shared repo instead of picking the newest snapshot from any job.
      restic -r "$RESTIC_REPOSITORY" restore "$snap" --target "$target" --tag "$job"
      log_info "restored $snap to $target; unpack the plugin archive to recover per-app data"
      ;;
    *) usage; exit 2 ;;
  esac
}

# archive jobs restore FROM S3, not from the job's local source tree —
# validate AWS config only; do NOT require the job's local source, which
# restore doesn't need. Each archive job lives under its own media/<job>/
# sub-prefix.
_restore_archive() {
  local job="$1"; shift
  validate_common
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  case "${1:-}" in
    thaw)
      local prefix="${2:?prefix}" tier="Bulk" dry=""
      shift 2
      while [ $# -gt 0 ]; do case "$1" in --tier) tier="$2"; shift 2;; --dry-run) dry=1; shift;; *) shift;; esac; done
      log_info "issuing $tier Glacier restore for media/$job/$prefix objects"
      rclone --config "$RCLONE_CONFIG" lsf -R --files-only "s3:$S3_BUCKET/media/$job/$prefix" | while IFS= read -r key; do
        local full="media/$job/$prefix$key"
        local cmd=(aws s3api restore-object --bucket "$S3_BUCKET" --key "$full"
          --restore-request "Days=7,GlacierJobParameters={Tier=$tier}")
        if [ -n "${S3_ENDPOINT:-}" ]; then
          # S3_ENDPOINT already carries a scheme in our convention (e.g.
          # http://127.0.0.1:9000 for MinIO); prefixing http:// again would
          # double it to "http://http://...". Use it as-is when it already
          # has a scheme, otherwise assume https.
          case "$S3_ENDPOINT" in
            http://*|https://*) cmd+=(--endpoint-url "$S3_ENDPOINT") ;;
            *) cmd+=(--endpoint-url "https://$S3_ENDPOINT") ;;
          esac
        fi
        if [ -n "$dry" ]; then printf '%s\n' "${cmd[*]}"; else "${cmd[@]}" || log_warn "restore-object failed for $full"; fi
      done
      log_info "thaw requested; Deep Archive takes ~12-48h. Re-run '$job download' once objects are restored."
      ;;
    download)
      local prefix="${2:?prefix}" target="${3:?target dir}"
      mkdir -p "$target"
      rclone --config "$RCLONE_CONFIG" copy "s3:$S3_BUCKET/media/$job/$prefix" "$target" -v
      ;;
    *) usage; exit 2 ;;
  esac
}

main() {
  if [ $# -lt 1 ]; then usage; exit 2; fi
  local job="$1"; shift
  _load
  # load the job def (JOB_* vars). Overridable for tests via JOBS_IO_CMD,
  # mirroring backup-job.sh.
  local jobsio="${JOBS_IO_CMD:-python3 -m app.gui.jobs_io}"
  local def
  if ! def="$(CONFIG_DIR="${CONFIG_DIR:-/config}" $jobsio "$job")"; then
    die "job '$job' not found"
  fi
  eval "$def"
  case "$JOB_TYPE" in
    versioned) _restore_versioned "$job" "$@" ;;
    archive)   _restore_archive "$job" "$@" ;;
    *) die "job '$job' has unknown type '$JOB_TYPE'" ;;
  esac
}
main "$@"
