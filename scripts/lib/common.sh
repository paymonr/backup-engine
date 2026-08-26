#!/usr/bin/env bash
# scripts/lib/common.sh — shared engine helpers. Source, don't execute.
# shellcheck shell=bash

USB_VERSION="${VERSION:-0.1.0-dev}"
CACHE_DIR="${CACHE_DIR:-/cache}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

_level_num() {
  case "$1" in
    DEBUG) echo 10 ;; INFO) echo 20 ;; WARN) echo 30 ;; ERROR) echo 40 ;; *) echo 20 ;;
  esac
}

log() {
  local level="$1"; shift
  local msg="$*"
  if [ "$(_level_num "$level")" -lt "$(_level_num "$LOG_LEVEL")" ]; then
    return 0
  fi
  local line
  line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$level] $msg"
  printf '%s\n' "$line"
  if [ -n "${LOG_FILE:-}" ]; then
    local dir; dir="$(dirname "$LOG_FILE")"
    if mkdir -p "$dir" 2>/dev/null && { [ -w "$dir" ] || [ -w "$LOG_FILE" ] 2>/dev/null; }; then
      printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
    fi
  fi
}

log_info() { log INFO "$*"; }
log_warn() { log WARN "$*"; }
log_error() { log ERROR "$*"; }

die() { log_error "$*"; exit 1; }

require_env() {
  local missing=() v
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then missing+=("$v"); fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "missing required configuration: ${missing[*]}"
  fi
}

acquire_lock() {
  local name="$1"
  local dir="$CACHE_DIR/locks"
  mkdir -p "$dir"
  local lock="$dir/$name.lock"
  exec 9>"$lock"
  if ! flock -n 9; then
    die "another $name run is in progress (lock: $lock)"
  fi
}

notify() {
  local event="$1" title="$2" body="$3"
  [ -n "${APPRISE_URLS:-}" ] || return 0
  if [ "$event" != "failure" ] && [ "${NOTIFY_ON_SUCCESS:-false}" != "true" ]; then
    return 0
  fi
  # shellcheck disable=SC2086 # APPRISE_URLS is intentionally word-split into multiple targets
  if ! apprise -t "$title" -b "$body" $APPRISE_URLS >/dev/null 2>&1; then
    log_warn "apprise notification failed (event=$event)"
  fi
  return 0
}

version_banner() {
  log_info "unraid-s3-backup $USB_VERSION"
  log_info "restic:     $(restic version 2>/dev/null | head -n1 || echo unknown)"
  log_info "rclone:     $(rclone version 2>/dev/null | head -n1 || echo unknown)"
  log_info "supercronic: $(supercronic -version 2>&1 | head -n1 || echo unknown)"
}
