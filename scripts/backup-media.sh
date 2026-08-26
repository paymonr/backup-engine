#!/usr/bin/env bash
# scripts/backup-media.sh — ship curated media dirs to S3 via rclone (additive by default).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"
# shellcheck source=lib/config.sh
source "$HERE/lib/config.sh"
# shellcheck source=lib/rclone-conf.sh
source "$HERE/lib/rclone-conf.sh"

# Guard so a failure already recorded/notified by _fail() isn't double-reported
# by the catch-all EXIT trap below.
_BE_FAIL_HANDLED=0

main() {
  # Catch-all: any non-zero exit from here on (an explicit _fail, a die() in
  # validate_media/acquire_lock, or an unexpected `set -e` abort) must still
  # record failure state + notify. Installed first thing, right after
  # CACHE_DIR is known (common.sh defaults it as soon as it's sourced above).
  trap '_usb_exit_trap "$?"' EXIT

  # Allow tests to preset env; only load mounted files when present.
  if [ -f "${CONFIG_DIR:-/config}/backup.env" ]; then
    load_config "${CONFIG_DIR:-/config}"
  fi
  validate_media
  acquire_lock media
  version_banner

  # Optional knobs may not be set when load_config was skipped (e.g. tests
  # presetting env directly); fall back to the same defaults load_config uses.
  : "${MEDIA_MIRROR:=false}"
  : "${MEDIA_STORAGE_CLASS:=DEEP_ARCHIVE}"
  : "${RCLONE_TRANSFERS:=8}"
  : "${RCLONE_BWLIMIT:=}"

  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"
  export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  mkdir -p "$CACHE_DIR/state"

  local verb="copy"
  [ "$MEDIA_MIRROR" = "true" ] && verb="sync"
  local args=(
    "$verb" "$MEDIA_SRC" "s3:$S3_BUCKET/media"
    --filter-from "$MEDIA_INCLUDES"
    --s3-storage-class "$MEDIA_STORAGE_CLASS"
    --transfers "$RCLONE_TRANSFERS"
    --stats-one-line --stats 30s -v
  )
  [ -n "$RCLONE_BWLIMIT" ] && args+=(--bwlimit "$RCLONE_BWLIMIT")

  local start; start="$(date +%s)"
  log_info "rclone $verb $MEDIA_SRC -> s3:$S3_BUCKET/media (class=$MEDIA_STORAGE_CLASS)"
  if ! rclone "${args[@]}"; then
    _fail "rclone $verb failed"
  fi
  # metadata-only integrity check (cold-safe)
  rclone check "$MEDIA_SRC" "s3:$S3_BUCKET/media" --filter-from "$MEDIA_INCLUDES" --size-only || \
    log_warn "rclone check reported differences (size-only; expected during in-flight deltas)"

  local dur=$(( $(date +%s) - start ))
  printf '{"last_run":"%s","outcome":"success","mode":"%s","duration_s":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$verb" "$dur" >"$CACHE_DIR/state/media.json"
  log_info "media backup complete (${verb}, ${dur}s)"
  notify success "media backup OK" "$verb finished in ${dur}s"
  healthcheck success
}

# _record_failure MSG [EXIT_CODE] — single place that writes the failure
# run-state and sends the failure notification. Sets the guard flag first so
# the EXIT trap (which may still fire right after this, via die()/exit) never
# double-notifies.
_record_failure() {
  local msg="$1" rc="${2:-1}"
  _BE_FAIL_HANDLED=1
  mkdir -p "$CACHE_DIR/state"
  printf '{"last_run":"%s","outcome":"failure","error":"%s","exit_code":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$msg" "$rc" >"$CACHE_DIR/state/media.json"
  notify failure "media backup FAILED" "$msg"
  healthcheck failure
}

_fail() {
  local msg="$1"
  _record_failure "$msg" 1
  die "$msg"
}

# _usb_exit_trap RC — EXIT-trap catch-all. Fires for every exit of this
# script (success, an explicit _fail, or any other non-zero exit such as
# validate_media's or acquire_lock's die()). No-ops on success and no-ops
# if _record_failure already ran, so it only fires for the failure paths
# that _fail() didn't already handle.
_usb_exit_trap() {
  local rc="$1"
  [ "$rc" -eq 0 ] && return 0
  [ "$_BE_FAIL_HANDLED" -eq 1 ] && return 0
  _record_failure "pipeline exited with status $rc" "$rc"
}

main "$@"
