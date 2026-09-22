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
# Control flag for a graceful stop-with-resume: $CACHE_DIR/state/$JOB.control containing the
# literal string "pause". Checked by the TERM/INT traps (_be_stop_trap) and by _retry, so a
# pause request is terminal whether it's caught mid-attempt (via signal) or between attempts.
BE_CONTROL="$CACHE_DIR/state/$JOB.control"
# Capture any RESTIC_REPOSITORY already in the ambient environment (an explicit external
# override — some tests/operators set this) BEFORE config.sh's own derivation, and before the
# per-job env (JOB_BUCKET) is loaded, can touch it. derive_restic_repo() (called below, both
# from load_config and again after the JOB_* eval) honors this verbatim when non-empty.
_RESTIC_REPO_OVERRIDE="${RESTIC_REPOSITORY:-}"

: "${BE_MAX_ATTEMPTS:=3}"; : "${BE_RETRY_BASE_SECONDS:=30}"; : "${BE_SLEEP_CMD:=sleep}"

# _be_pause_requested — true when a graceful stop-with-resume has been requested via the
# control flag (set by the GUI/ops side; cleared by us once we've honored it).
_be_pause_requested() { [ -f "$BE_CONTROL" ] && [ "$(cat "$BE_CONTROL" 2>/dev/null)" = "pause" ]; }
# _be_kill_descendants PID SIG — send SIG to every LIVE descendant of PID (not PID itself),
# deepest first. Bash defers a trapped TERM/INT until the current foreground command finishes
# (a well-known gotcha), so the engine process actually blocking the run (restic/rclone/python3,
# several fork levels down from the script's own PID) never sees the signal unless something
# forwards it explicitly -- this does, walking /proc via pgrep -P.
_be_kill_descendants() {
  local pid="$1" sig="$2" c kids
  # pgrep exits 1 (no match) for every leaf in the tree -- completely normal, but this function
  # is called directly from a trap (not from an if/||-guarded context that would suspend
  # errexit), so without `|| true` that ordinary "no children" result would trip `set -e` and
  # kill the whole script the instant recursion reaches its first leaf, before a single `kill`
  # runs.
  kids="$(pgrep -P "$pid" 2>/dev/null)" || true
  for c in $kids; do
    _be_kill_descendants "$c" "$sig"
    kill -s "$sig" "$c" 2>/dev/null || true
  done
}
# _retry LOG CMD... — runs CMD; on failure, if LOG looks transient (per _is_transient_error)
# and attempts remain, backs off (base * 2^(n-1) seconds) and re-runs CMD -- the engine's own
# incremental/resume behavior means a re-run picks up where the failed attempt left off, not
# from scratch. Sets BE_ATTEMPTS to the number of attempts actually made (the run record reads
# it). Also exposes the in-progress attempt number at $CACHE_DIR/state/$JOB.attempt for the live
# progress UI -- a RUNNING run has no other way to surface a retry in progress, since the run
# record only carries `attempts` on its terminal "end" event -- removing it once this stops
# retrying (success or final failure).
#
# CMD runs in a backgrounded subshell that we explicitly `wait` on (rather than a plain
# foreground pipe) so a trapped TERM/INT interrupts the wait immediately instead of being
# deferred until CMD finishes -- see _be_kill_descendants above and _be_stop_trap below. The
# subshell captures PIPESTATUS[0] itself and re-exits with it, so `wait`'s $? is exactly CMD's
# own exit code (tee's exit status is deliberately ignored, same as before).
#
# `trap : TERM INT` as the subshell's OWN first statement matters: bash resets a forked
# subshell's inherited traps back to no-trap before running its body (this happens again for
# EACH further subshell forked for a pipe component, e.g. CMD itself when it's a function like
# _restic_backup_attempt -- which re-arms the same no-op trap as ITS OWN first statement, for
# the same reason). Without it, a broad `pkill -f "backup-job.sh $JOB"` (which matches these
# forked-but-not-exec'd subshells too, since fork doesn't touch /proc/pid/cmdline) kills them
# via bash's default TERM disposition -- INSTANTLY, since no trap means no deferral -- often
# faster than $$'s own reaction, orphaning the real engine process before $$'s descendant sweep
# ever reaches it. A real (non-ignore) handler, even a no-op one, defers that the same way any
# trap does, keeping the subshell alive until $$ explicitly kills its way down to it -- and
# because it's a real handler rather than SIG_IGN, it doesn't survive CMD's own exec (only
# SIG_IGN does), so the actual engine binary (restic/rclone/python3) starts with an untouched
# default disposition, free to install its own signal handling same as if none of this existed.
_retry() {
  local log="$1"; shift
  local n=0 rc=0 pid
  local attempt_marker="$CACHE_DIR/state/$JOB.attempt"
  while :; do
    n=$((n+1)); BE_ATTEMPTS="$n"
    printf '%s' "$n" >"$attempt_marker"
    : >"$log"
    { trap : TERM INT; "$@" 2>&1 | tee -a "$log" >/dev/null; exit "${PIPESTATUS[0]}"; } &
    pid=$!
    wait "$pid"; rc=$?
    if [ "$rc" -eq 0 ]; then rm -f "$attempt_marker"; return 0; fi
    if _be_pause_requested; then rm -f "$attempt_marker"; _BE_PAUSE_REQUESTED=1; exit 0; fi
    if [ "$n" -ge "$BE_MAX_ATTEMPTS" ] || ! _is_transient_error "$log"; then rm -f "$attempt_marker"; return "$rc"; fi
    log_warn "job '$JOB' attempt $n failed (transient); retrying (resumes where it left off)"
    "$BE_SLEEP_CMD" "$(( BE_RETRY_BASE_SECONDS * (1 << (n-1)) ))"
  done
}

main() {
  trap '_usb_exit_trap "$?"' EXIT
  trap '_be_stop_trap TERM 143' TERM
  trap '_be_stop_trap INT 130' INT
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
  runs_end ok 0 "" "\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS},\"attempts\":${BE_ATTEMPTS:-1}" || log_warn "could not record run"   # (5)
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
  local rlog="$CACHE_DIR/state/$JOB-retry.log"
  # restic's --json stream must keep landing in $f on EVERY attempt (retries included) -- the
  # live progress monitor tails it -- but the transient classifier must not be fed that (huge,
  # per-status-line) stream. So this wrapper sends restic's stderr, plus only the non-"status"
  # lines of its --json stdout (the summary, and any plain-text error restic prints), to $rlog
  # for _retry to classify; the full raw --json stream still goes to $f untouched, as before.
  _restic_backup_attempt() {
    trap : TERM INT   # see _retry's comment: re-armed per forked subshell, not inherited
    restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" backup "$src" --tag "$JOB" --json \
        2>>"$rlog" | tee "$f" >/dev/null
    local rc=${PIPESTATUS[0]}
    grep -vE '"message_type":[[:space:]]*"status"' "$f" >>"$rlog" 2>/dev/null || true
    return "$rc"
  }
  if _retry "$rlog" _restic_backup_attempt; then
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
  local rlog="$CACHE_DIR/state/$JOB-rclone.log" rc=0
  # rclone has no separate progress stream to protect (unlike restic's --json -> -last.jsonl);
  # its combined output can go straight into the one log _retry both writes to and classifies on.
  _retry "$rlog" rclone "${args[@]}" || rc=$?
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
  runs_end failed "$rc" "$msg" "\"phase\":\"$phase\",\"copied\":$([ "${COPIED:-0}" -eq 1 ] && echo true || echo false),\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS},\"attempts\":${BE_ATTEMPTS:-1}" || true
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
# _record_paused — the graceful-stop counterpart to _record_failure: a `paused` end record is a
# clean terminal state, not a failure, so it skips _write_state/notify/healthcheck entirely and
# removes the control flag (the request has now been honored).
_record_paused() { _BE_FAIL_HANDLED=1
  runs_end paused 0 "paused by request" "\"attempts\":${BE_ATTEMPTS:-1}" || true
  rm -f "$BE_CONTROL"
}
# _be_stop_trap SIG RC — installed for TERM/INT. A plain stop keeps today's behavior: exit
# immediately with RC, which _usb_exit_trap records as a failure same as always. A stop
# requested via the control flag is a graceful pause instead: forward SIG into every descendant
# so the blocked engine call (restic/rclone/python3) actually exits -- see _be_kill_descendants
# -- then exit RC ourselves; _usb_exit_trap sees _BE_PAUSE_REQUESTED and records "paused".
_be_stop_trap() {
  local sig="$1" rc="$2"
  # $$ always reports the TOP-LEVEL script's PID, even inside a forked-but-not-exec'd subshell
  # (a pipeline component of _retry's own pipe, say) -- and such a subshell, having been forked
  # AFTER these traps were installed, inherits them too. Since it still carries the top-level
  # script's argv (fork doesn't touch /proc/pid/cmdline, only exec does), a broad `pkill -f
  # "backup-job.sh $JOB"` can match it directly and it would ALSO run this trap. $BASHPID is the
  # process's own real PID, so this guard makes sure only the genuine top-level process performs
  # the descendant sweep -- letting several matched processes each independently walk and kill
  # the same tree is a pure race (one can kill a node the other is mid-`pgrep -P` into, dropping
  # its subtree from the sweep entirely). Any other matched process still just exits on the signal.
  if [ "$BASHPID" = "$$" ] && _be_pause_requested; then
    _BE_PAUSE_REQUESTED=1
    _be_kill_descendants "$$" "$sig"
  fi
  exit "$rc"
}
_usb_exit_trap() { local rc="$1"
  if [ "${_BE_PAUSE_REQUESTED:-0}" -eq 1 ]; then
    _record_paused
  elif [ "$rc" -ne 0 ] && [ "$_BE_FAIL_HANDLED" -eq 0 ]; then
    _record_failure "${_BE_LAST_ERR:-job exited with status $rc}" "$rc"
  fi
  [ -n "${_BE_TEE_PID:-}" ] && { exec 1>&- 2>&-; wait "$_BE_TEE_PID" 2>/dev/null || true; }; }
main "$@"
