#!/usr/bin/env bash
# scripts/entrypoint.sh — container PID 1: validate, render, schedule, exec supercronic.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"
# shellcheck source=lib/config.sh
source "$HERE/lib/config.sh"
# shellcheck source=lib/rclone-conf.sh
source "$HERE/lib/rclone-conf.sh"

prepare() {
  load_config "${CONFIG_DIR:-/config}"
  validate_common
  mkdir -p "$CACHE_DIR" "$CACHE_DIR/logs" "$CACHE_DIR/state" "$CACHE_DIR/locks"
  # restic password file (so creds aren't passed on argv)
  printf '%s' "${RESTIC_PASSWORD:-}" >"$CACHE_DIR/restic-password"
  chmod 600 "$CACHE_DIR/restic-password"
  export RESTIC_PASSWORD_FILE="$CACHE_DIR/restic-password"
  render_rclone_conf "$CACHE_DIR/rclone.conf"
  version_banner
}

emit_crontab() {
  local ct="$CACHE_DIR/crontab"
  : >"$ct"
  [ -n "${APPDATA_SCHEDULE:-}" ] && printf '%s %s\n' "$APPDATA_SCHEDULE" "$HERE/backup-appdata.sh" >>"$ct"
  [ -n "${MEDIA_SCHEDULE:-}" ] && printf '%s %s\n' "$MEDIA_SCHEDULE" "$HERE/backup-media.sh" >>"$ct"
  log_info "wrote crontab:"; cat "$ct"
}

main() {
  prepare
  emit_crontab
  case "${1:-}" in
    --emit-crontab) return 0 ;;   # for tests / inspection
  esac
  if [ -n "${RUN_ONCE:-}" ]; then
    case "$RUN_ONCE" in
      appdata) exec "$HERE/backup-appdata.sh" ;;
      media) exec "$HERE/backup-media.sh" ;;
      *) die "RUN_ONCE must be 'appdata' or 'media'" ;;
    esac
  fi
  log_info "starting supercronic"
  exec supercronic "$CACHE_DIR/crontab"
}

main "$@"
