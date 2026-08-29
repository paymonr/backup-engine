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
  local ct="$CACHE_DIR/crontab"; : >"$ct"
  # No `2>/dev/null`: jobs_io.load() now exits 0 on a corrupt/mis-shaped jobs.json
  # (emitting nothing, so pipefail no longer aborts PID 1) and writes ONE diagnostic
  # to stderr — let it reach the container log instead of swallowing why no jobs ran.
  CONFIG_DIR="${CONFIG_DIR:-/config}" python3 -m app.gui.jobs_io --list | \
  while IFS=$'\t' read -r enabled schedule name; do
    [ "$enabled" = "1" ] || continue
    printf '%s %s %s\n' "$schedule" "$HERE/backup-job.sh" "$name" >>"$ct"
  done
  log_info "wrote crontab:"; cat "$ct"
}

main() {
  prepare
  emit_crontab
  case "${1:-}" in
    --emit-crontab) return 0 ;;   # for tests / inspection
  esac
  if [ -n "${RUN_ONCE:-}" ]; then
    exec "$HERE/backup-job.sh" "$RUN_ONCE"
  fi
  if [ "${GUI_ENABLED:-true}" != "false" ]; then
    log_info "starting scheduler (background) + GUI on port ${GUI_PORT:-8099}"
    supercronic "$CACHE_DIR/crontab" &
    exec python3 -m app.gui.server
  fi
  log_info "GUI disabled; scheduler only"
  exec supercronic "$CACHE_DIR/crontab"
}

main "$@"
