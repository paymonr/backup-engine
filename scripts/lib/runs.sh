#!/usr/bin/env bash
# scripts/lib/runs.sh — per-job run records (append-only JSONL) + per-run logs. Source, don't execute.
RUNS_KEEP_LINES="${RUNS_KEEP_LINES:-400}"    # 2 lines/run -> the newest 200 runs survive a rotation
RUNS_ROTATE_AT="${RUNS_ROTATE_AT:-500}"
RUNS_LOG_KEEP_DAYS="${RUNS_LOG_KEEP_DAYS:-120}"
_RUNS_ID_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
_runs_esc() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
  printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037'; }
_runs_now()   { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
_runs_num()   { case "${1:-}" in ''|*[!0-9]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }
# _runs_str VALUE -> a JSON string when non-empty, the literal null when empty. Use it for EVERY
# optional member; `${V:+"$V"}${V:-null}` is wrong bash (`:-` yields the VALUE when V is set, so a set
# V prints the string twice) and produces an unparseable line.
_runs_str()   { if [ -n "${1:-}" ]; then printf '"%s"' "$(_runs_esc "$1")"; else printf 'null'; fi; }
runs_new_id() { printf '%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"; }
runs_file()   { printf '%s/state/%s.runs.jsonl\n' "$CACHE_DIR" "$1"; }
runs_set_command() { BE_RUN_COMMAND="${1:0:500}"; }
# runs_start JOB KIND [EXTRA_MEMBERS]  — EXTRA_MEMBERS = already-escaped JSON members
runs_start() {
  local job="$1" kind="$2" extra="${3:-}"
  mkdir -p "$CACHE_DIR/state" "$CACHE_DIR/logs/runs/$job"
  # shellcheck disable=SC2254
  case "${BE_RUN_ID:-}" in $_RUNS_ID_GLOB) ;; *) BE_RUN_ID="$(runs_new_id)" ;; esac
  export BE_RUN_ID
  BE_RUN_JOB="$job"; BE_RUN_KIND="$kind"; BE_RUN_START_EPOCH="$(date +%s)"; BE_RUN_COMMAND=""; BE_RUN_ENDED=0
  BE_RUN_STARTED=1          # the ONLY flag that says a start line was written (BE_RUN_ID is pre-set by the GUI)
  BE_RUN_LOG="logs/runs/$job/$BE_RUN_ID.log"; export BE_RUN_LOG
  printf '{"v":1,"id":"%s","job":"%s","kind":"%s","event":"start","trigger":"%s","started_at":"%s","pid":%d,"log":"%s"%s}\n' \
    "$BE_RUN_ID" "$(_runs_esc "$job")" "$kind" "$(_runs_esc "${BE_TRIGGER:-scheduled}")" "$(_runs_now)" "$$" \
    "$BE_RUN_LOG" "${extra:+,$extra}" >>"$(runs_file "$job")"
}
# runs_end OUTCOME EXIT_CODE [ERROR] [EXTRA_MEMBERS]  — idempotent; no-op if runs_start never ran.
# The guard MUST key on BE_RUN_STARTED, never on BE_RUN_ID: `ops.launch` pre-assigns BE_RUN_ID in the
# child's environment, so a run that dies before runs_start (lock collision, job not found, missing
# source) still has an id — keying on the id would append an end line with BE_RUN_JOB/KIND/START_EPOCH
# unset and abort inside the EXIT trap under `set -u`.
runs_end() {
  [ "${BE_RUN_STARTED:-0}" -eq 1 ] && [ "${BE_RUN_ENDED:-0}" -eq 0 ] || return 0
  local outcome="$1" rc="${2:-0}" err="${3:-}" extra="${4:-}" f errj="null"
  f="$(runs_file "$BE_RUN_JOB")"
  [ -n "$err" ] && errj="\"$(_runs_esc "${err:0:1000}")\""
  printf '{"v":1,"id":"%s","job":"%s","kind":"%s","event":"end","outcome":"%s","finished_at":"%s","duration_s":%d,"exit_code":%d,"error":%s,"command":"%s"%s}\n' \
    "$BE_RUN_ID" "$(_runs_esc "$BE_RUN_JOB")" "$BE_RUN_KIND" "$outcome" "$(_runs_now)" \
    "$(( $(date +%s) - BE_RUN_START_EPOCH ))" "$rc" "$errj" "$(_runs_esc "$BE_RUN_COMMAND")" "${extra:+,$extra}" >>"$f"
  BE_RUN_ENDED=1
  _runs_rotate "$f" "$BE_RUN_JOB"
}
_runs_rotate() { local f="$1" job="$2" n; n="$(wc -l <"$f" 2>/dev/null || echo 0)"
  if [ "$n" -gt "$RUNS_ROTATE_AT" ]; then tail -n "$RUNS_KEEP_LINES" "$f" >"$f.tmp.$$" && mv -f "$f.tmp.$$" "$f"; fi
  find "$CACHE_DIR/logs/runs/$job" -name '*.log' -type f -mtime +"$RUNS_LOG_KEEP_DAYS" -delete 2>/dev/null || true; }
# --- generic failure path, shared by backup-job.sh and restore.sh -------------
# runs_fail MSG [RC] [EXTRA] — record-only: no state file, no notify, no healthcheck.
runs_fail() { runs_end failed "${2:-1}" "$1" "${3:-}" || true; }
# runs_exit_trap RC — install as: trap 'runs_exit_trap "$?"' EXIT
runs_exit_trap() { local rc="$1"
  if [ "$rc" -ne 0 ] && [ "${_BE_FAIL_HANDLED:-0}" -eq 0 ]; then runs_fail "${_BE_LAST_ERR:-exited with status $rc}" "$rc"; fi
  [ -n "${_BE_TEE_PID:-}" ] && { exec 1>&- 2>&-; wait "$_BE_TEE_PID" 2>/dev/null || true; }; }
# tool-stat parsers (end-of-run, from files the run wrote)
_restic_summary_field() { grep '"message_type":"summary"' "$1" 2>/dev/null | tail -n1 | grep -o "\"$2\":[0-9]*" | head -n1 | cut -d: -f2; }
_restic_snapshot_id()   { grep '"message_type":"summary"' "$1" 2>/dev/null | tail -n1 | grep -o '"snapshot_id":"[a-f0-9]*"' | head -n1 | cut -d'"' -f4 | cut -c1-8; }
# rclone prints TWO lines that start with "Transferred:" in every stats block — bytes first
# ("Transferred:   3.100 GiB / 3.100 GiB, 100%, 8.912 MiB/s, ETA 0s") then files
# ("Transferred:            1204 / 1204, 100%"). The bytes grep therefore REQUIRES a unit token, and the
# files grep requires the "N / M," shape; without that, `tail -n1` returns the files line for both and
# bytes_added silently becomes the file count.
_rclone_stat_bytes()  { grep -E '^Transferred:[[:space:]]+[0-9.]+ [KMGTPE]?i?B / ' "$1" 2>/dev/null | tail -n1 |
  awk '{n=$2; u=$3; m=1; if(u~/^Ki?B?/)m=1024; else if(u~/^Mi?B?/)m=1024^2; else if(u~/^Gi?B?/)m=1024^3; else if(u~/^Ti?B?/)m=1024^4; else if(u~/^Pi?B?/)m=1024^5; printf "%d", n*m}'; }
_rclone_stat_files()  { grep -E '^Transferred:[[:space:]]+[0-9]+ / [0-9]+,' "$1" 2>/dev/null | tail -n1 | awk '{print $2}'; }
_rclone_stat_errors() { grep -E '^Errors:[[:space:]]+[0-9]+' "$1" 2>/dev/null | tail -n1 | awk '{print $2}'; }
_vfiles_stat() { grep -o "$2=[0-9]*" "$1" 2>/dev/null | tail -n1 | cut -d= -f2; }
_first_error_line() { grep -m1 -E 'AccessDenied|Error|error|denied|failed' "$1" 2>/dev/null | cut -c1-300; }
