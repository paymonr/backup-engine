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
  mkdir -p "$CACHE_DIR" "$CACHE_DIR/logs" "$CACHE_DIR/logs/runs" "$CACHE_DIR/state" "$CACHE_DIR/locks"
  # restic password file (so creds aren't passed on argv)
  printf '%s' "${RESTIC_PASSWORD:-}" >"$CACHE_DIR/restic-password"
  chmod 600 "$CACHE_DIR/restic-password"
  export RESTIC_PASSWORD_FILE="$CACHE_DIR/restic-password"
  render_rclone_conf "$CACHE_DIR/rclone.conf"
  version_banner
  # Reconcile run records left dangling by a crash/restart (a start with no end ->
  # aborted) and seed backfills from legacy per-job state, before the scheduler or
  # GUI can observe them (spec §7.1.7/§7.2). Never fatal; exits 0 always.
  CONFIG_DIR="${CONFIG_DIR:-/config}" python3 -m app.engine.runs boot \
    || log_warn "run reconcile at boot failed (non-fatal)"
  # After reconciling dangling runs, auto-resume any interrupted (aborted) run whose
  # job opts in (toggle default on) — best-effort, never fatal to container start.
  CONFIG_DIR="${CONFIG_DIR:-/config}" python3 -m app.engine.resume \
    || log_warn "auto-resume on boot failed (non-fatal)"
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
  # Hourly S3 rules check (spec 2026-09-23, R-B9): rules changed outside backup-engine are
  # caught between backup runs too. Same line + same condition as jobs_io.render_crontab
  # (S3_RULES_CHECK_LINE) -- crontab_stale compares the two renders byte for byte.
  if [ -s "$ct" ]; then
    printf '%s\n' '17 * * * * timeout 900 python3 -m app.engine.lifecycle check-all' >>"$ct"
  fi
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
    # -inotify: reload on crontab changes; the GUI ALSO SIGUSR2s this pid after
    # every write, since inotify may not fire on Unraid's FUSE share (§7.3).
    supercronic -inotify "$CACHE_DIR/crontab" &
    echo $! >"$CACHE_DIR/supercronic.pid"
    exec python3 -m app.gui.server
  fi
  log_info "GUI disabled; scheduler only"
  # exec keeps this shell's PID, so $$ is supercronic's pid once it takes over.
  echo $$ >"$CACHE_DIR/supercronic.pid"
  exec supercronic -inotify "$CACHE_DIR/crontab"
}

main "$@"
