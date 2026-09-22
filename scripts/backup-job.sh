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
# shellcheck source=lib/runs.sh
source "$HERE/lib/runs.sh"
# lib/points.sh is added by a later increment; guard the source so its absence can't trip errexit
# before it lands. The points_refresh call below carries its own `|| log_warn` for the same reason.
# shellcheck source=lib/points.sh
[ -f "$HERE/lib/points.sh" ] && source "$HERE/lib/points.sh"
_BE_FAIL_HANDLED=0
JOB="${1:?usage: backup-job.sh <job-name>}"
# Capture any RESTIC_REPOSITORY already in the ambient environment (an explicit external
# override — some tests/operators set this) BEFORE config.sh's own derivation, and before the
# per-job env (JOB_BUCKET) is loaded, can touch it. derive_restic_repo() (called below, both
# from load_config and again after the JOB_* eval) honors this verbatim when non-empty.
_RESTIC_REPO_OVERRIDE="${RESTIC_REPOSITORY:-}"

main() {
  trap '_usb_exit_trap "$?"' EXIT; trap 'exit 143' TERM; trap 'exit 130' INT
  [ -f "${CONFIG_DIR:-/config}/backup.env" ] && load_config "${CONFIG_DIR:-/config}"
  local jobsio="${JOBS_IO_CMD:-python3 -m app.gui.jobs_io}"
  local def; if ! def="$(CONFIG_DIR="${CONFIG_DIR:-/config}" $jobsio "$JOB")"; then _fail "job '$JOB' not found"; fi
  # `set -a` exports every var the def assigns (emit_shell emits bare `JOB_*=`, no `export`) so the
  # versioned-files engine, a python3 CHILD reading them from os.environ, inherits JOB_STORAGE_CLASS/etc.
  set -a; eval "$def"; set +a
  # Re-derive RESTIC_REPOSITORY now that JOB_BUCKET (if this is a dedicated-bucket job) is in
  # scope — config.sh's source-time derivation (inside load_config, above) ran before the
  # per-job env existed and could only ever see the base S3_BUCKET.
  derive_restic_repo
  acquire_lock "$JOB"                                                                          # (1)
  runs_start "$JOB" backup "\"type\":\"$JOB_TYPE\",\"storage_class\":\"$JOB_STORAGE_CLASS\""   # (2)
  exec > >(tee -a "$CACHE_DIR/$BE_RUN_LOG") 2>&1; _BE_TEE_PID=$!                              # (3)
  version_banner
  validate_source                                                                              # (4)
  local src="$SOURCE_ROOT/$JOB_SOURCE"
  [ -d "$src" ] || _fail "job '$JOB' source '$src' missing"
  : "${RESTIC_CACHE_DIR:=$CACHE_DIR/restic}"
  RUN_STATS=""; COPIED=0
  case "$JOB_TYPE" in
    versioned) _run_versioned "$src" ;; archive) _run_archive "$src" ;; versioned-files) _run_vfiles "$JOB" ;;
    *) _fail "job '$JOB' has unknown type '$JOB_TYPE'" ;;
  esac
  local dur=$(( $(date +%s) - BE_RUN_START_EPOCH ))
  runs_end ok 0 "" "\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS}" || log_warn "could not record run"   # (5)
  _write_state success "" 0                                                                    # (6)
  points_refresh "$JOB" "$JOB_TYPE" || log_warn "restore-point cache refresh failed for '$JOB' (non-fatal)"   # (7)
  log_info "job '$JOB' complete ($JOB_TYPE, ${dur}s)"
  notify success "backup '$JOB' OK" "$JOB_TYPE finished in ${dur}s"; healthcheck success
}

_run_versioned() {
  local src="$1"; export RESTIC_CACHE_DIR; mkdir -p "$RESTIC_CACHE_DIR"
  local class_opt=(-o "s3.storage-class=$JOB_STORAGE_CLASS")
  restic -r "$RESTIC_REPOSITORY" cat config >/dev/null 2>&1 || restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" init
  log_info "restic backup $src (tag=$JOB)"
  runs_set_command "restic -r $RESTIC_REPOSITORY backup $src --tag $JOB"
  local f="$CACHE_DIR/state/$JOB-last.jsonl"
  if restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" backup "$src" --tag "$JOB" --json \
       | tee "$f" >/dev/null; then
    SNAP_ID="$(_restic_snapshot_id "$f")" || true; COPIED=1
    local fn fc da ftp btp fa=null
    fn="$(_restic_summary_field "$f" files_new)" || true
    fc="$(_restic_summary_field "$f" files_changed)" || true
    da="$(_restic_summary_field "$f" data_added)" || true
    ftp="$(_restic_summary_field "$f" total_files_processed)" || true
    btp="$(_restic_summary_field "$f" total_bytes_processed)" || true
    if [ -n "$fn" ] || [ -n "$fc" ]; then fa=$(( ${fn:-0} + ${fc:-0} )); fi
    RUN_STATS="\"files_new\":$(_runs_num "$fn"),\"files_changed\":$(_runs_num "$fc"),\"files_added\":$fa,\"bytes_added\":$(_runs_num "$da"),\"files_total\":$(_runs_num "$ftp"),\"bytes_total\":$(_runs_num "$btp")"
  else _fail "restic backup failed for '$JOB'"; fi
  local forget_args=()
  case "$JOB_RETENTION_TYPE" in
    keep_all) : ;;                                             # no forget
    days)  forget_args=(--keep-within "${JOB_RETENTION_DAYS}d") ;;
    count) forget_args=(--keep-last "$JOB_RETENTION_COUNT") ;;
    *)     forget_args=(--keep-last "$JOB_KEEP_LAST" --keep-daily "$JOB_KEEP_DAILY" \
                        --keep-weekly "$JOB_KEEP_WEEKLY" --keep-monthly "$JOB_KEEP_MONTHLY") ;;
  esac
  if [ "$JOB_RETENTION_TYPE" != keep_all ]; then
    case "$JOB_STORAGE_CLASS" in
      GLACIER|DEEP_ARCHIVE|GLACIER_IR) log_warn "job '$JOB' class $JOB_STORAGE_CLASS is cold; deferring prune" ;;
      *) local plog="$CACHE_DIR/state/$JOB-prune.log" rc=0; : >"$plog"
         # A stale lock (e.g. left by a process killed on a container restart) is
         # non-exclusive, so it lets nightly backups through but blocks the EXCLUSIVE
         # prune lock forever. Clear stale locks first; restic `unlock` never removes a
         # live process's lock, so a concurrent backup is unaffected.
         restic -r "$RESTIC_REPOSITORY" unlock 2>&1 | tee -a "$plog" >/dev/null || true
         restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" forget --prune --tag "$JOB" "${forget_args[@]}" 2>&1 | tee -a "$plog" >/dev/null || rc=$?
         [ "$rc" -eq 0 ] || _fail_phase prune "prune failed: $(_first_error_line "$plog")" ;;
    esac
  fi
}

_run_archive() {
  local src="$1" verb="copy"; [ "${JOB_MIRROR:-false}" = "true" ] && verb="sync"
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  : "${RCLONE_TRANSFERS:=8}"; : "${RCLONE_BWLIMIT:=}"
  local args=("$verb" "$src" "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$JOB" --s3-storage-class "$JOB_STORAGE_CLASS"
    --transfers "$RCLONE_TRANSFERS" --stats 30s -v)
  [ -n "$RCLONE_BWLIMIT" ] && args+=(--bwlimit "$RCLONE_BWLIMIT")
  log_info "rclone $verb $src -> s3:${JOB_BUCKET:-$S3_BUCKET}/media/$JOB (class=$JOB_STORAGE_CLASS)"
  runs_set_command "rclone $verb $src s3:${JOB_BUCKET:-$S3_BUCKET}/media/$JOB --s3-storage-class $JOB_STORAGE_CLASS"
  local rlog="$CACHE_DIR/state/$JOB-rclone.log" rc=0; : >"$rlog"
  rclone "${args[@]}" 2>&1 | tee -a "$rlog" || rc=$?
  # The stats block is written even on a partial failure, so build RUN_STATS BEFORE inspecting rc —
  # the record still says how far it got.
  local fa ba re
  fa="$(_rclone_stat_files "$rlog")" || true
  ba="$(_rclone_stat_bytes "$rlog")" || true
  re="$(_rclone_stat_errors "$rlog")" || true
  RUN_STATS="\"files_added\":$(_runs_num "$fa"),\"bytes_added\":$(_runs_num "$ba"),\"files_total\":null,\"bytes_total\":null,\"rclone_errors\":$(_runs_num "$re")"
  [ "$rc" -eq 0 ] || _fail "rclone $verb failed for '$JOB'"
  COPIED=1
  rclone check "$src" "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$JOB" --size-only || log_warn "rclone check differences for '$JOB' (size-only)"
  if [ "$JOB_RETENTION_TYPE" != keep_all ]; then
    local plog="$CACHE_DIR/state/$JOB-prune.log" rc=0; : >"$plog"
    python3 -m app.engine.archive_prune "$JOB" --type "$JOB_RETENTION_TYPE" \
      --days "${JOB_RETENTION_DAYS:-0}" --count "${JOB_RETENTION_COUNT:-1}" 2>&1 | tee -a "$plog" >/dev/null || rc=$?
    [ "$rc" -eq 0 ] || _fail_phase prune "$(_first_error_line "$plog")"
  fi
}

_run_vfiles() {
  # Runs the Python engine IN-PROCESS (not exec) so control returns to main() -- the success-path
  # state-file write + notify/healthcheck, and the EXIT trap's failure-path recording, apply to
  # versioned-files jobs the same as versioned/archive. JOB_* are exported at the `set -a` eval above.
  runs_set_command "python3 -m app.engine.vfiles backup $1"
  local vlog="$CACHE_DIR/state/$JOB-vfiles.log" rc=0; : >"$vlog"
  python3 -m app.engine.vfiles backup "$1" 2>&1 | tee -a "$vlog" || rc=$?
  local up by ft bt
  up="$(_vfiles_stat "$vlog" uploaded)" || true
  by="$(_vfiles_stat "$vlog" bytes)" || true
  ft="$(_vfiles_stat "$vlog" files_total)" || true
  bt="$(_vfiles_stat "$vlog" bytes_total)" || true
  RUN_STATS="\"files_added\":$(_runs_num "$up"),\"bytes_added\":$(_runs_num "$by"),\"files_total\":$(_runs_num "$ft"),\"bytes_total\":$(_runs_num "$bt")"
  [ "$rc" -eq 0 ] || _fail "file-history backup failed for '$1'"
  COPIED=1
}

_write_state() { local outcome="$1" msg="$2" rc="$3"; mkdir -p "$CACHE_DIR/state"
  printf '{"last_run":"%s","outcome":"%s","type":"%s","snapshot_id":"%s","duration_s":%d,"error":"%s","exit_code":%d,"run_id":"%s","started_at":"%s","finished_at":"%s"}\n' \
    "$(_runs_now)" "$outcome" "${JOB_TYPE:-}" "${SNAP_ID:-}" "$(( $(date +%s) - ${BE_RUN_START_EPOCH:-$(date +%s)} ))" \
    "$(_runs_esc "${msg:0:1000}")" "$rc" "${BE_RUN_ID:-}" "$(date -u -d "@${BE_RUN_START_EPOCH:-$(date +%s)}" '+%Y-%m-%dT%H:%M:%SZ')" "$(_runs_now)" \
    >"$CACHE_DIR/state/$JOB.json"; }
_record_failure() { local msg="$1" rc="${2:-1}" phase="${3:-copy}"; _BE_FAIL_HANDLED=1
  runs_end failed "$rc" "$msg" "\"phase\":\"$phase\",\"copied\":$([ "${COPIED:-0}" -eq 1 ] && echo true || echo false),\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS}" || true
  # Same guard as runs_end, and for the same reason: only a run that actually started owns the legacy
  # state file AND its failure alerting. A lock collision (BE_RUN_ID pre-set by the GUI, runs_start
  # never reached) must leave state/<job>.json exactly as the last real run wrote it and send NO
  # failure notify / healthcheck ping — a run IS in progress and the holder owns it; the collision's
  # only report is the die() WARN in the shared log. The `-z JOB_TYPE` arm keeps a pre-lock config
  # failure ("job not found") visible in the legacy file and alerting, which is all that exists for it.
  if [ "${BE_RUN_STARTED:-0}" -eq 1 ] || [ -z "${JOB_TYPE:-}" ]; then
    _write_state failure "$msg" "$rc"
    notify failure "backup '$JOB' FAILED" "$msg"; healthcheck failure
  fi; }
_fail()       { _record_failure "$1" 1 copy; die "$1"; }
_fail_phase() { _record_failure "$2" 1 "$1"; die "$2"; }
_usb_exit_trap() { local rc="$1"
  if [ "$rc" -ne 0 ] && [ "$_BE_FAIL_HANDLED" -eq 0 ]; then _record_failure "${_BE_LAST_ERR:-job exited with status $rc}" "$rc"; fi
  [ -n "${_BE_TEE_PID:-}" ] && { exec 1>&- 2>&-; wait "$_BE_TEE_PID" 2>/dev/null || true; }; }
main "$@"
