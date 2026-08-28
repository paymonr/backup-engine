#!/usr/bin/env bash
# scripts/restore.sh — guided restore for both tiers.
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
  restore.sh appdata list
  restore.sh appdata restore <snapshot-id|latest> <target-dir>
  restore.sh media thaw <prefix> [--tier Bulk|Standard|Expedited] [--dry-run]
  restore.sh media download <prefix> <target-dir>
EOF
}

# shellcheck disable=SC2015 # deliberate: no-op when backup.env is absent, not an if/else
_load() { [ -f "${CONFIG_DIR:-/config}/backup.env" ] && load_config "${CONFIG_DIR:-/config}" || true; }

_is_cold() { case "$1" in GLACIER|DEEP_ARCHIVE|GLACIER_IR) return 0 ;; *) return 1 ;; esac; }

# appdata restores FROM S3, not from the local APPDATA_SRC tree — the primary
# restore scenario is a fresh/rebuilt machine where that source is absent or
# empty. So validate AWS + restic config only; do NOT call validate_appdata
# (it requires a populated local source, which restore must not depend on).
appdata() {
  _load
  validate_common
  require_env RESTIC_PASSWORD RESTIC_REPOSITORY
  : "${APPDATA_STORAGE_CLASS:=STANDARD}"
  export RESTIC_CACHE_DIR="$CACHE_DIR/restic"
  case "${1:-}" in
    list) restic -r "$RESTIC_REPOSITORY" snapshots --tag appdata ;;
    restore)
      local snap="${2:?snapshot id or 'latest'}" target="${3:?target dir}"
      if _is_cold "$APPDATA_STORAGE_CLASS"; then
        log_warn "repo class $APPDATA_STORAGE_CLASS is cold; restore needs thawed packs. If restore errors on a data read, thaw the appdata/ prefix first (see docs) then retry."
      fi
      mkdir -p "$target"
      restic -r "$RESTIC_REPOSITORY" restore "$snap" --target "$target"
      log_info "restored $snap to $target; unpack the plugin archive to recover per-app data"
      ;;
    *) usage; exit 2 ;;
  esac
}

# media reads FROM S3, not from the local MEDIA_ROOT tree — validate AWS
# config only; do NOT call validate_media (it requires a local MEDIA_ROOT,
# which restore doesn't need).
media() {
  _load
  validate_common
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  case "${1:-}" in
    thaw)
      local prefix="${2:?prefix}" tier="Bulk" dry=""
      shift 2
      while [ $# -gt 0 ]; do case "$1" in --tier) tier="$2"; shift 2;; --dry-run) dry=1; shift;; *) shift;; esac; done
      log_info "issuing $tier Glacier restore for media/$prefix objects"
      rclone --config "$RCLONE_CONFIG" lsf -R --files-only "s3:$S3_BUCKET/media/$prefix" | while IFS= read -r key; do
        local full="media/$prefix$key"
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
      log_info "thaw requested; Deep Archive takes ~12-48h. Re-run 'media download' once objects are restored."
      ;;
    download)
      local prefix="${2:?prefix}" target="${3:?target dir}"
      mkdir -p "$target"
      rclone --config "$RCLONE_CONFIG" copy "s3:$S3_BUCKET/media/$prefix" "$target" -v
      ;;
    *) usage; exit 2 ;;
  esac
}

case "${1:-}" in
  appdata) shift; appdata "$@" ;;
  media) shift; media "$@" ;;
  *) usage; exit 2 ;;
esac
