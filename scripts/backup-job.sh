#!/usr/bin/env bash
# scripts/backup-job.sh <job-name> — run one backup job (restic or rclone) from its jobs.json def.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"
# shellcheck source=lib/config.sh
source "$HERE/lib/config.sh"
# shellcheck source=lib/rclone-conf.sh
source "$HERE/lib/rclone-conf.sh"
_BE_FAIL_HANDLED=0
JOB="${1:?usage: backup-job.sh <job-name>}"

main() {
  trap '_usb_exit_trap "$?"' EXIT
  [ -f "${CONFIG_DIR:-/config}/backup.env" ] && load_config "${CONFIG_DIR:-/config}"
  validate_source
  # load the job def (JOB_* vars). Overridable for tests via JOBS_IO_CMD.
  local jobsio="${JOBS_IO_CMD:-python3 -m app.gui.jobs_io}"
  local def; if ! def="$(CONFIG_DIR="${CONFIG_DIR:-/config}" $jobsio "$JOB")"; then _fail "job '$JOB' not found"; fi
  eval "$def"
  local src="$SOURCE_ROOT/$JOB_SOURCE"
  [ -d "$src" ] || _fail "job '$JOB' source '$src' missing"
  acquire_lock "$JOB"; version_banner
  : "${RESTIC_CACHE_DIR:=$CACHE_DIR/restic}"; mkdir -p "$CACHE_DIR/state"
  # RESTIC_REPOSITORY is normally computed by load_config(); when that's
  # skipped (no mounted backup.env — e.g. tests preset env directly, same
  # pattern the old fixed pipelines used), fall back to the same formula so
  # versioned jobs still resolve a repo. All versioned jobs share this one
  # repo, tag-scoped by job name (see config.sh).
  : "${RESTIC_REPOSITORY:=s3:${S3_ENDPOINT:-s3.${AWS_REGION:-}.amazonaws.com}/${S3_BUCKET:-}/appdata}"
  local start; start="$(date +%s)"
  case "$JOB_TYPE" in
    versioned) _run_versioned "$src" ;;
    archive)   _run_archive "$src" ;;
    *) _fail "job '$JOB' has unknown type '$JOB_TYPE'" ;;
  esac
  local dur=$(( $(date +%s) - start ))
  printf '{"last_run":"%s","outcome":"success","type":"%s","snapshot_id":"%s","duration_s":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$JOB_TYPE" "${SNAP_ID:-}" "$dur" >"$CACHE_DIR/state/$JOB.json"
  log_info "job '$JOB' complete ($JOB_TYPE, ${dur}s)"
  notify success "backup '$JOB' OK" "$JOB_TYPE finished in ${dur}s"; healthcheck success
}

_run_versioned() {
  local src="$1"; export RESTIC_CACHE_DIR; mkdir -p "$RESTIC_CACHE_DIR"
  local class_opt=(-o "s3.storage-class=$JOB_STORAGE_CLASS")
  restic -r "$RESTIC_REPOSITORY" cat config >/dev/null 2>&1 || restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" init
  log_info "restic backup $src (tag=$JOB)"
  if restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" backup "$src" --tag "$JOB" --json \
       | tee "$CACHE_DIR/state/$JOB-last.jsonl" >/dev/null; then
    SNAP_ID="$(grep '"message_type":"summary"' "$CACHE_DIR/state/$JOB-last.jsonl" | grep -o '"snapshot_id":"[a-f0-9]*"' | head -n1 | cut -d'"' -f4)" || true
  else _fail "restic backup failed for '$JOB'"; fi
  case "$JOB_STORAGE_CLASS" in
    GLACIER|DEEP_ARCHIVE|GLACIER_IR) log_warn "job '$JOB' class $JOB_STORAGE_CLASS is cold; deferring prune" ;;
    *) restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" forget --prune --tag "$JOB" \
         --keep-last "$JOB_KEEP_LAST" --keep-daily "$JOB_KEEP_DAILY" \
         --keep-weekly "$JOB_KEEP_WEEKLY" --keep-monthly "$JOB_KEEP_MONTHLY" \
         || log_warn "restic forget/prune for '$JOB' reported an error (non-fatal)" ;;
  esac
}

_run_archive() {
  local src="$1" verb="copy"; [ "${JOB_MIRROR:-false}" = "true" ] && verb="sync"
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  : "${RCLONE_TRANSFERS:=8}"; : "${RCLONE_BWLIMIT:=}"
  local args=("$verb" "$src" "s3:$S3_BUCKET/media/$JOB" --s3-storage-class "$JOB_STORAGE_CLASS"
    --transfers "$RCLONE_TRANSFERS" --stats-one-line --stats 30s -v)
  [ -n "$RCLONE_BWLIMIT" ] && args+=(--bwlimit "$RCLONE_BWLIMIT")
  log_info "rclone $verb $src -> s3:$S3_BUCKET/media/$JOB (class=$JOB_STORAGE_CLASS)"
  rclone "${args[@]}" || _fail "rclone $verb failed for '$JOB'"
  rclone check "$src" "s3:$S3_BUCKET/media/$JOB" --size-only || log_warn "rclone check differences for '$JOB' (size-only)"
}

_record_failure() { local msg="$1" rc="${2:-1}"; _BE_FAIL_HANDLED=1; mkdir -p "$CACHE_DIR/state"
  printf '{"last_run":"%s","outcome":"failure","error":"%s","exit_code":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$msg" "$rc" >"$CACHE_DIR/state/$JOB.json"
  notify failure "backup '$JOB' FAILED" "$msg"; healthcheck failure; }
_fail() { _record_failure "$1" 1; die "$1"; }
_usb_exit_trap() { local rc="$1"; [ "$rc" -eq 0 ] && return 0; [ "$_BE_FAIL_HANDLED" -eq 1 ] && return 0; _record_failure "job exited with status $rc" "$rc"; }
main "$@"
