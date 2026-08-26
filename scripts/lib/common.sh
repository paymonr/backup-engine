#!/usr/bin/env bash
# scripts/lib/common.sh — shared engine helpers. Source, don't execute.
# shellcheck shell=bash

BACKUP_ENGINE_VERSION="${VERSION:-0.1.0-dev}"
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

# healthcheck EVENT — best-effort ping to a healthchecks.io-style dead-man's
# switch. EVENT is "success" or "failure". No-op when HEALTHCHECK_URL is
# unset/empty. Never fails the caller (mirrors notify()'s best-effort shape):
# a curl error is logged and swallowed, not propagated.
healthcheck() {
  local event="$1"
  [ -n "${HEALTHCHECK_URL:-}" ] || return 0
  local url="$HEALTHCHECK_URL"
  [ "$event" = "failure" ] && url="${HEALTHCHECK_URL%/}/fail"
  if ! curl -fsS -m 10 -o /dev/null "$url"; then
    log_warn "healthcheck ping failed (event=$event)"
  fi
  return 0
}

# _tool_version NAME CMD... — prints the first line of `CMD...`'s version
# output, or "unknown" when NAME isn't on PATH or the command produces no
# output. Never invokes a missing binary (so the shell can't leak a
# "command not found" error into the result) and never propagates a
# failure status, regardless of the caller's pipefail/errexit settings.
_tool_version() {
  local name="$1"
  local out=""
  if command -v "$name" >/dev/null 2>&1; then
    out="$("$@" 2>&1 | head -n1)" || true
  fi
  if [ -z "$out" ]; then
    printf 'unknown\n'
  else
    printf '%s\n' "$out"
  fi
}

version_banner() {
  log_info "backup-engine $BACKUP_ENGINE_VERSION"
  log_info "restic:      $(_tool_version restic version)"
  log_info "rclone:      $(_tool_version rclone version)"
  log_info "supercronic: $(_tool_version supercronic -version)"
}
