#!/usr/bin/env bash
# scripts/backup-appdata.sh — ship the plugin's appdata archives to S3 via restic.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"
# shellcheck source=lib/config.sh
source "$HERE/lib/config.sh"

# Guard so a failure already recorded/notified by _fail() isn't double-reported
# by the catch-all EXIT trap below.
_USB_FAIL_HANDLED=0

main() {
  # Catch-all: any non-zero exit from here on (an explicit _fail, a die() in
  # validate_appdata/acquire_lock, or an unexpected `set -e` abort) must still
  # record failure state + notify. Installed first thing, right after
  # CACHE_DIR is known (common.sh defaults it as soon as it's sourced above).
  trap '_usb_exit_trap "$?"' EXIT

  # Allow tests to preset env; only load mounted files when present.
  if [ -f "${CONFIG_DIR:-/config}/backup.env" ]; then
    load_config "${CONFIG_DIR:-/config}"
  fi
  validate_appdata
  acquire_lock appdata
  version_banner

  # Retention knobs may not be set when load_config was skipped (e.g. tests
  # presetting env directly); fall back to the same defaults load_config uses.
  : "${KEEP_LAST:=3}"; : "${KEEP_DAILY:=7}"; : "${KEEP_WEEKLY:=4}"; : "${KEEP_MONTHLY:=6}"

  export RESTIC_CACHE_DIR="$CACHE_DIR/restic"
  mkdir -p "$RESTIC_CACHE_DIR" "$CACHE_DIR/state"
  local class_opt=(-o "s3.storage-class=$APPDATA_STORAGE_CLASS")
  local start; start="$(date +%s)"

  # init the repo if absent (idempotent)
  if ! restic -r "$RESTIC_REPOSITORY" cat config >/dev/null 2>&1; then
    log_info "initializing restic repository"
    restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" init
  fi

  log_info "backing up $APPDATA_SRC"
  local snap_id="unknown"
  if restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" backup "$APPDATA_SRC" \
       --tag appdata --json | tee "$CACHE_DIR/state/appdata-last.jsonl" >/dev/null; then
    snap_id="$(grep '"message_type":"summary"' "$CACHE_DIR/state/appdata-last.jsonl" \
                | grep -o '"snapshot_id":"[a-f0-9]*"' | head -n1 | cut -d'"' -f4)" || true
    snap_id="${snap_id:-unknown}"
  else
    _fail "restic backup failed"
  fi

  _forget_or_defer "${class_opt[@]}"

  local dur=$(( $(date +%s) - start ))
  printf '{"last_run":"%s","outcome":"success","snapshot_id":"%s","duration_s":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$snap_id" "$dur" >"$CACHE_DIR/state/appdata.json"
  log_info "appdata backup complete (snapshot=$snap_id, ${dur}s)"
  notify success "appdata backup OK" "snapshot $snap_id in ${dur}s"
}

_is_cold() {
  case "$APPDATA_STORAGE_CLASS" in GLACIER|DEEP_ARCHIVE|GLACIER_IR) return 0 ;; *) return 1 ;; esac
}

_forget_or_defer() {
  if _is_cold; then
    log_warn "APPDATA_STORAGE_CLASS=$APPDATA_STORAGE_CLASS is cold; skipping prune (requires a thaw). Retention forget deferred — run a thaw-then-prune manually or via the Phase-3 wizard."
    return 0
  fi
  log_info "applying retention (last=$KEEP_LAST daily=$KEEP_DAILY weekly=$KEEP_WEEKLY monthly=$KEEP_MONTHLY)"
  restic -r "$RESTIC_REPOSITORY" "$@" forget --prune \
    --keep-last "$KEEP_LAST" --keep-daily "$KEEP_DAILY" \
    --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" --tag appdata || \
    log_warn "restic forget/prune reported an error (non-fatal for this run)"
}

# _record_failure MSG [EXIT_CODE] — single place that writes the failure
# run-state and sends the failure notification. Sets the guard flag first so
# the EXIT trap (which may still fire right after this, via die()/exit) never
# double-notifies.
_record_failure() {
  local msg="$1" rc="${2:-1}"
  _USB_FAIL_HANDLED=1
  mkdir -p "$CACHE_DIR/state"
  printf '{"last_run":"%s","outcome":"failure","error":"%s","exit_code":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$msg" "$rc" >"$CACHE_DIR/state/appdata.json"
  notify failure "appdata backup FAILED" "$msg"
}

_fail() {
  local msg="$1"
  _record_failure "$msg" 1
  die "$msg"
}

# _usb_exit_trap RC — EXIT-trap catch-all. Fires for every exit of this
# script (success, an explicit _fail, or any other non-zero exit such as
# validate_appdata's or acquire_lock's die()). No-ops on success and no-ops
# if _record_failure already ran, so it only fires for the failure paths
# that _fail() didn't already handle.
_usb_exit_trap() {
  local rc="$1"
  [ "$rc" -eq 0 ] && return 0
  [ "$_USB_FAIL_HANDLED" -eq 1 ] && return 0
  _record_failure "pipeline exited with status $rc" "$rc"
}

main "$@"
