#!/usr/bin/env bash
# scripts/restore.sh <job> ... — GUI-driven, type-dispatched restore/thaw/download/test.
# Every MUTATING action (restore/download/thaw/test) takes the job lock and writes a
# run record (kind restore|download|thaw|test-restore) so the GUI's run-record page
# can render it live; read-only actions (list, thaw-status) take no lock and write no
# record. Mirrors backup-job.sh, but a failed restore must NEVER touch the job's
# last-BACKUP state file, notify a backup failure, or ping the backup healthcheck
# (spec 7.5.3): it records + notifies "restore FAILED" only.
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
# shellcheck source=lib/points.sh
source "$HERE/lib/points.sh"

_BE_FAIL_HANDLED=0
RESTORE_EXTRA=""          # extra JSON members a mutating branch parsed before it might fail;
                          # _fail folds them into the failed record (e.g. a download's bytes_restored).
# Capture any RESTIC_REPOSITORY already in the ambient environment (an explicit external
# override — some tests/operators set this) BEFORE config.sh's own derivation, and before the
# per-job env (JOB_BUCKET) is loaded, can touch it. derive_restic_repo() (called below, both
# from load_config and again after the JOB_* eval) honors this verbatim when non-empty.
_RESTIC_REPO_OVERRIDE="${RESTIC_REPOSITORY:-}"

usage() {
  cat <<EOF
usage:
  restore.sh <job> list [--json]
  restore.sh <job> browse [<relpath>] [--json]                                  (read-only; archive, versioned-files)
  restore.sh <job> restore <snapshot-id|latest> <target-dir> [--include <path>]   (versioned only)
  restore.sh <job> thaw <prefix|.> [--tier Bulk|Standard|Expedited] [--dry-run]   (archive: any prefix; versioned-files: "." only, via vfiles thaw; versioned: whole store)
  restore.sh <job> thaw-status <prefix|.>                                          (archive, versioned-files)
  restore.sh <job> download <prefix|.> <target-dir>                                (archive only)
  restore.sh <job> <path|.> <target> [--asof TS] [--tier Bulk|Standard|Expedited] (versioned-files; "." = every file)
  restore.sh <job> test                                                            (any type; one file)
EOF
}

# shellcheck disable=SC2015 # deliberate: no-op when backup.env is absent, not an if/else
_load() { [ -f "${CONFIG_DIR:-/config}/backup.env" ] && load_config "${CONFIG_DIR:-/config}" || true; }

_is_cold() { case "$1" in GLACIER|DEEP_ARCHIVE|GLACIER_IR) return 0 ;; *) return 1 ;; esac; }

# Path guard for `browse`: strip a leading '/', reject any '..' path segment
# (bare "..", "../x", "x/..", "x/../y" -- wrapping in slashes catches all of
# these as a "/../"  substring). Prints the cleaned relpath and returns 0, or
# returns 1 with nothing printed on a bad path.
_safe_rel() {
  local p="${1#/}"
  case "/$p/" in *"/../"*) return 1 ;; esac
  printf '%s' "$p"
}

# Failure path for a MUTATING restore action (spec 7.5.3 §2): a run record + a restore
# notification — no state file, no healthcheck. RESTORE_EXTRA carries any stats already
# parsed (so a download that copied N GB then errored still records bytes_restored).
_fail() { _BE_FAIL_HANDLED=1; runs_fail "$1" 1 "$RESTORE_EXTRA"; notify failure "restore '$job' FAILED" "$1"; die "$1"; }

_file_bytes() { stat -c%s "$1" 2>/dev/null || { wc -c <"$1" 2>/dev/null | tr -d ' '; } || printf '0'; }

# --- aws s3api helpers (S3_ENDPOINT already carries a scheme by convention; prefix
#     https:// only when it doesn't, mirroring backup-job.sh / app.engine.s3) --------
_aws_restore_object() {
  local key="$1" tier="$2" dry="$3"
  local cmd=(aws s3api restore-object --bucket "${JOB_BUCKET:-$S3_BUCKET}" --key "$key"
    --restore-request "Days=7,GlacierJobParameters={Tier=$tier}")
  case "${S3_ENDPOINT:-}" in
    "") ;;
    http://*|https://*) cmd+=(--endpoint-url "$S3_ENDPOINT") ;;
    *) cmd+=(--endpoint-url "https://$S3_ENDPOINT") ;;
  esac
  if [ -n "$dry" ]; then printf '%s\n' "${cmd[*]}"; else "${cmd[@]}" || log_warn "restore-object failed for $key"; fi
}
_aws_head_restore() {
  local key="$1"
  local cmd=(aws s3api head-object --bucket "${JOB_BUCKET:-$S3_BUCKET}" --key "$key" --query Restore --output text)
  case "${S3_ENDPOINT:-}" in
    "") ;;
    http://*|https://*) cmd+=(--endpoint-url "$S3_ENDPOINT") ;;
    *) cmd+=(--endpoint-url "https://$S3_ENDPOINT") ;;
  esac
  "${cmd[@]}" 2>/dev/null || printf 'None\n'
}

# Upper-bound warm-up hours per class+tier (spec 7.6) — drives expected_ready_by.
_warmup_hours() {
  case "$1:$2" in
    DEEP_ARCHIVE:Standard) echo 12 ;; DEEP_ARCHIVE:*) echo 48 ;;
    GLACIER:Expedited) echo 1 ;; GLACIER:Standard) echo 5 ;; GLACIER:*) echo 12 ;;
    *) echo 0 ;;
  esac
}

# thaw.json (spec 7.6): the persisted warm-up request the GUI polls. Written on every thaw.
_thaw_json_write() {
  local job="$1" tier="$2" scope="$3" n="$4" now ready_by expires hours
  now="$(_runs_now)"
  hours="$(_warmup_hours "${JOB_STORAGE_CLASS:-STANDARD}" "$tier")"
  ready_by="$(date -u -d "+$hours hours" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf '%s' "$now")"
  expires="$(date -u -d '+7 days' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf '%s' "$now")"
  mkdir -p "$CACHE_DIR/state"
  printf '{"requested_at":"%s","run_id":"%s","tier":"%s","scope":"%s","days":7,"expected_ready_by":"%s","copy_expires_at":"%s","objects_requested":%s}\n' \
    "$now" "${BE_RUN_ID:-}" "$(_runs_esc "$tier")" "$(_runs_esc "$scope")" "$ready_by" "$expires" "$(_runs_num "$n")" \
    >"$CACHE_DIR/state/$job.thaw.json"
}

# Merge a fresh last_check block into thaw.json (pure bash: strip any prior block +
# the closing brace, then re-append). No python — bookkeeping stays in the shell.
_thaw_merge_last_check() {
  local job="$1" f="$CACHE_DIR/state/$1.thaw.json" base lc
  [ -f "$f" ] || return 0
  base="$(sed -E 's/,"last_check":\{[^}]*\}//; s/\}[[:space:]]*$//' "$f")"
  lc="\"last_check\":{\"at\":\"$(_runs_now)\",\"sampled\":$2,\"ready\":$3,\"pending\":$4,\"not_requested\":$5}"
  printf '%s,%s}\n' "$base" "$lc" >"$f.tmp.$$" && mv -f "$f.tmp.$$" "$f"
}

# Issue one aws restore-object per key under an S3 prefix; sets THAW_N to the count.
# Lists into a temp file first so a live "warmed N of ~TOTAL" line can be printed
# every 500 objects (spec 7.5.3 §7) without a second network listing.
_thaw_issue() {
  local remote="$1" tier="$2" dry="$3" listfile total n=0
  listfile="$(mktemp)"
  rclone --config "$RCLONE_CONFIG" lsf -R --files-only "s3:${JOB_BUCKET:-$S3_BUCKET}/$remote" >"$listfile" 2>/dev/null || true
  total="$(wc -l <"$listfile" | tr -d ' ')"
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    _aws_restore_object "$remote$key" "$tier" "$dry"
    n=$((n + 1))
    [ $((n % 500)) -eq 0 ] && log_info "warmed $n of ~$total"
  done <"$listfile"
  rm -f "$listfile"
  THAW_N="$n"
}

# Sample up to 20 keys under a prefix, head-object each, print the counts and fold
# them into thaw.json.last_check (spec 7.5.3 §8). Read-only: no lock, no record.
_thaw_status() {
  local job="$1" remote="$2" sampled=0 ready=0 pending=0 notreq=0 key r
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    [ "$sampled" -ge 20 ] && break
    sampled=$((sampled + 1))
    r="$(_aws_head_restore "$remote$key")"
    case "$r" in
      *'ongoing-request="true"'*)  pending=$((pending + 1)) ;;
      *'ongoing-request="false"'*) ready=$((ready + 1)) ;;
      *)                           notreq=$((notreq + 1)) ;;
    esac
  done < <(rclone --config "$RCLONE_CONFIG" lsf -R --files-only "s3:${JOB_BUCKET:-$S3_BUCKET}/$remote" 2>/dev/null)
  printf '{"sampled":%d,"ready":%d,"pending":%d,"not_requested":%d}\n' "$sampled" "$ready" "$pending" "$notreq"
  _thaw_merge_last_check "$job" "$sampled" "$ready" "$pending" "$notreq"
}

_first_restic_file() { grep -m1 '"type":"file"' 2>/dev/null | grep -o '"path":"[^"]*"' | head -n1 | cut -d'"' -f4; }

# tested.json (spec 7.5.3 §9): the "Restore ever tested" evidence the readiness screen reads.
_tested_json_write() {
  local job="$1" path="$2" bytes="$3"
  mkdir -p "$CACHE_DIR/state"
  printf '{"at":"%s","path":"%s","bytes":%s,"run_id":"%s"}\n' \
    "$(_runs_now)" "$(_runs_esc "$path")" "$(_runs_num "$bytes")" "${BE_RUN_ID:-}" \
    >"$CACHE_DIR/state/$job.tested.json"
}
# test-thaw.json (spec 7.5.3 §9): a cold test's pending warm-up, so a later `test` resumes it.
_json_get() { grep -o "\"$2\":\"[^\"]*\"" "$1" 2>/dev/null | head -n1 | cut -d'"' -f4; }
_test_thaw_json_write() {
  local job="$1" key="$2" ready_by="$3"
  mkdir -p "$CACHE_DIR/state"
  [ -n "$ready_by" ] || ready_by="$(date -u -d '+48 hours' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || _runs_now)"
  printf '{"key":"%s","requested_at":"%s","expected_ready_by":"%s","copy_expires_at":"%s","run_id":"%s"}\n' \
    "$(_runs_esc "$key")" "$(_runs_now)" "$ready_by" \
    "$(date -u -d '+7 days' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || _runs_now)" "${BE_RUN_ID:-}" \
    >"$CACHE_DIR/state/$job.test-thaw.json"
}

# Build the run record's params object from the sub's argv (spec 7.5.3 §2).
_build_params() {
  local sub="${1:-}"; [ $# -gt 0 ] && shift
  local point="" scope="." target="" tier="" asof="" include=""
  case "$sub" in
    restore)
      [ $# -gt 0 ] && { point="$1"; shift; }
      [ $# -gt 0 ] && { target="$1"; shift; }
      while [ $# -gt 0 ]; do
        if [ "$1" = "--include" ] && [ $# -ge 2 ]; then include="$2"; shift 2; else shift; fi
      done
      ;;
    download)
      [ $# -gt 0 ] && { scope="$1"; shift; }
      [ $# -gt 0 ] && { target="$1"; shift; }
      ;;
    thaw)
      [ $# -gt 0 ] && { scope="$1"; shift; }
      while [ $# -gt 0 ]; do
        if [ "$1" = "--tier" ] && [ $# -ge 2 ]; then tier="$2"; shift 2; else shift; fi
      done
      ;;
    test) : ;;
    *)   # versioned-files: sub itself is "." or a relpath
      scope="$sub"
      [ $# -gt 0 ] && { target="$1"; shift; }
      while [ $# -gt 0 ]; do
        case "$1" in
          --asof) if [ $# -ge 2 ]; then asof="$2"; shift 2; else shift; fi ;;
          --tier) if [ $# -ge 2 ]; then tier="$2"; shift 2; else shift; fi ;;
          *) shift ;;
        esac
      done
      ;;
  esac
  printf '"point":%s,"scope":%s,"target":%s,"tier":%s,"asof":%s,"include":%s' \
    "$(_runs_str "$point")" "$(_runs_str "$scope")" "$(_runs_str "$target")" \
    "$(_runs_str "$tier")" "$(_runs_str "$asof")" "$(_runs_str "$include")"
}

# ---------------------------------------------------------------------------
# versioned (restic) — restores FROM S3, never from the job's local source tree.
# ---------------------------------------------------------------------------
_restore_versioned() {
  local job="$1"; shift
  validate_common
  require_env RESTIC_PASSWORD RESTIC_REPOSITORY
  export RESTIC_CACHE_DIR="$CACHE_DIR/restic"
  case "${1:-}" in
    list)
      if [ "${2:-}" = "--json" ]; then restic -r "$RESTIC_REPOSITORY" snapshots --tag "$job" --json
      else restic -r "$RESTIC_REPOSITORY" snapshots --tag "$job"; fi
      ;;
    restore)
      local snap="${2:?snapshot id or 'latest'}" target="${3:?target dir}"; shift 3
      local include=""
      while [ $# -gt 0 ]; do case "$1" in --include) include="${2:-}"; shift 2;; *) shift;; esac; done
      if _is_cold "${JOB_STORAGE_CLASS:-STANDARD}"; then
        log_warn "job '$job' class $JOB_STORAGE_CLASS is cold; restore needs thawed packs. If restore errors on a data read, thaw the whole store first ('$job thaw .') then retry."
      fi
      mkdir -p "$target"
      local ra=(restore "$snap" --target "$target" --tag "$job")
      [ -n "$include" ] && ra+=(--include "$include")
      runs_set_command "restic -r $RESTIC_REPOSITORY ${ra[*]}"
      restic -r "$RESTIC_REPOSITORY" "${ra[@]}" || _fail "restic restore failed for '$job'"
      log_info "restored $snap to $target; unpack the plugin archive to recover per-app data"
      local n b
      n="$(find "$target" -type f 2>/dev/null | wc -l | tr -d ' ')" || true
      b="$(du -sb "$target" 2>/dev/null | awk '{print $1}')" || true
      runs_end ok 0 "" "\"files_restored\":$(_runs_num "$n"),\"bytes_restored\":$(_runs_num "$b"),\"target\":$(_runs_str "$target")" \
        || log_warn "could not record run"
      ;;
    thaw)        _thaw_versioned "$job" "$@" ;;
    # No versioned thaw-status: the spec limits thaw-status to archive/versioned-files
    # (§7.5.5/§7.5.6). Falls through to usage/exit 2 below.
    test)        _test_versioned "$job" ;;
    *) usage; exit 2 ;;
  esac
}

# versioned cold store: one restore-object per object under appdata/ (the shared repo).
_thaw_versioned() {
  local job="$1"; shift
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  local tier="Bulk" dry=""
  # $1 is the scope ("." = whole store); versioned has no sub-prefix.
  [ $# -gt 0 ] && shift
  while [ $# -gt 0 ]; do case "$1" in --tier) tier="${2:-Bulk}"; shift 2;; --dry-run) dry=1; shift;; *) shift;; esac; done
  if ! _is_cold "${JOB_STORAGE_CLASS:-STANDARD}"; then
    # Let runs_exit_trap write the failed END (do NOT set _BE_FAIL_HANDLED, or the
    # start line is left dangling and Task 4's reader shows it perpetually running).
    _BE_LAST_ERR="job is not on a thaw-first tier"; log_error "$_BE_LAST_ERR"; exit 2
  fi
  log_info "Warming up the whole snapshot store (every snapshot job shares it). This issues one request per object and can take a long time for a large store."
  _thaw_issue "appdata/" "$tier" "$dry"
  _thaw_json_write "$job" "$tier" "." "$THAW_N"
  runs_end ok 0 "" "\"objects_requested\":$(_runs_num "$THAW_N"),\"tier\":$(_runs_str "$tier")" || log_warn "could not record run"
}

_test_versioned() {
  local job="$1"
  validate_common; require_env RESTIC_PASSWORD RESTIC_REPOSITORY
  export RESTIC_CACHE_DIR="$CACHE_DIR/restic"
  local scratch="$CACHE_DIR/restore-test/$job/$BE_RUN_ID"; mkdir -p "$scratch"
  local path
  path="$(restic -r "$RESTIC_REPOSITORY" ls --json latest --tag "$job" 2>/dev/null | _first_restic_file)" || true
  [ -n "$path" ] || _fail "no file found in the latest snapshot to test-restore for '$job'"
  runs_set_command "restic restore latest --tag $job --include $path --target $scratch"
  restic -r "$RESTIC_REPOSITORY" restore latest --tag "$job" --include "$path" --target "$scratch" \
    || _fail "test restore failed for '$job'"
  local file bytes
  file="$(find "$scratch" -type f 2>/dev/null | head -n1)" || true
  { [ -n "$file" ] && [ -s "$file" ]; } || _fail "test restore produced no file for '$job'"
  bytes="$(_file_bytes "$file")"
  _tested_json_write "$job" "$path" "$bytes"
  rm -rf "$scratch"
  runs_end ok 0 "" "\"tested_path\":$(_runs_str "$path"),\"tested_bytes\":$(_runs_num "$bytes")" || log_warn "could not record run"
}

# ---------------------------------------------------------------------------
# archive (rclone) — restores FROM S3 under this job's own media/<job>/ prefix.
# ---------------------------------------------------------------------------
_restore_archive() {
  local job="$1"; shift
  validate_common
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
  [ -f "$RCLONE_CONFIG" ] || render_rclone_conf "$RCLONE_CONFIG"
  case "${1:-}" in
    list)
      if [ "${2:-}" = "--json" ]; then points_render "$job" archive
      else rclone --config "$RCLONE_CONFIG" lsf --dirs-only "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/"; fi
      ;;
    browse)  # read-only: no lock, no record (dispatched from main's list|thaw-status|browse case)
      shift  # drop "browse"; remaining: [relpath] [--json]
      local rel=""
      while [ $# -gt 0 ]; do case "$1" in --json) ;; *) rel="$1";; esac; shift; done
      rel="$(_safe_rel "$rel")" || { echo "explore: bad path" >&2; exit 2; }
      rclone --config "$RCLONE_CONFIG" lsjson "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/${rel:+$rel/}" 2>/dev/null \
        | python3 -m app.engine.browse --rclone "$rel"
      ;;
    thaw)
      local prefix="${2:?prefix}" tier="Bulk" dry=""; shift 2
      while [ $# -gt 0 ]; do case "$1" in --tier) tier="${2:-Bulk}"; shift 2;; --dry-run) dry=1; shift;; *) shift;; esac; done
      [ "$prefix" = "." ] && prefix=""
      if ! _is_cold "${JOB_STORAGE_CLASS:-STANDARD}"; then
        # Let runs_exit_trap write the failed END (do NOT set _BE_FAIL_HANDLED, or the
        # start line is left dangling and Task 4's reader shows it perpetually running).
        _BE_LAST_ERR="job is not on a thaw-first tier"; log_error "$_BE_LAST_ERR"; exit 2
      fi
      log_info "issuing $tier Glacier restore for media/$job/$prefix objects (one request per object)"
      _thaw_issue "media/$job/$prefix" "$tier" "$dry"
      # $2 and the positionals are gone (shift 2 + the flag loop); use `prefix` for the
      # persisted scope: "" for whole-scope -> ".", the folder ("2020/") otherwise.
      _thaw_json_write "$job" "$tier" "${prefix:-.}" "$THAW_N"
      runs_end ok 0 "" "\"objects_requested\":$(_runs_num "$THAW_N"),\"tier\":$(_runs_str "$tier")" || log_warn "could not record run"
      ;;
    thaw-status)
      local prefix="${2:-.}"; [ "$prefix" = "." ] && prefix=""
      _thaw_status "$job" "media/$job/$prefix"
      ;;
    download)
      local prefix="${2:?prefix}" target="${3:?target dir}"
      [ "$prefix" = "." ] && prefix=""
      mkdir -p "$target"
      runs_set_command "rclone copy s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/$prefix $target"
      local rlog="$CACHE_DIR/state/$job-rclone.log" rc=0; : >"$rlog"
      # errexit-safe: rc via `|| rc=$?` so a failing copy doesn't kill the script before
      # the stats (below) are parsed into the record (spec 7.5.3 §6).
      rclone --config "$RCLONE_CONFIG" copy "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/$prefix" "$target" -v 2>&1 | tee -a "$rlog" || rc=$?
      local fr br re
      fr="$(_rclone_stat_files "$rlog")" || true
      br="$(_rclone_stat_bytes "$rlog")" || true
      re="$(_rclone_stat_errors "$rlog")" || true
      RESTORE_EXTRA="\"files_restored\":$(_runs_num "$fr"),\"bytes_restored\":$(_runs_num "$br"),\"target\":$(_runs_str "$target"),\"rclone_errors\":$(_runs_num "$re")"
      [ "$rc" -eq 0 ] || _fail "download reported errors — $(_runs_num "$re") files were not ready yet (still warming up) or failed"
      runs_end ok 0 "" "$RESTORE_EXTRA" || log_warn "could not record run"
      ;;
    test) _test_archive "$job" ;;
    *) usage; exit 2 ;;
  esac
}

_test_archive() {
  local job="$1"; local scratch="$CACHE_DIR/restore-test/$job/$BE_RUN_ID"; mkdir -p "$scratch"
  local ttjson="$CACHE_DIR/state/$job.test-thaw.json"
  if _is_cold "${JOB_STORAGE_CLASS:-STANDARD}"; then
    # Cold tier (ruling R11): a test warms one object, then a later `test` resumes.
    if [ -f "$ttjson" ]; then
      local key ready; key="$(_json_get "$ttjson" key)"; ready="$(_json_get "$ttjson" expected_ready_by)"
      case "$(_aws_head_restore "$key")" in
        *'ongoing-request="false"'*)
          local dest="${RESTORE_ROOT:-/restore}/$job/test"; mkdir -p "$dest"
          rclone --config "$RCLONE_CONFIG" copyto "s3:${JOB_BUCKET:-$S3_BUCKET}/$key" "$dest/$(basename "$key")" || _fail "test copy failed for '$job'"
          local f bytes; f="$dest/$(basename "$key")"; bytes="$(_file_bytes "$f")"
          _tested_json_write "$job" "$key" "$bytes"; rm -f "$ttjson"
          runs_end ok 0 "" "\"tested_path\":$(_runs_str "$key"),\"tested_bytes\":$(_runs_num "$bytes")" || log_warn "could not record run"
          ;;
        *)  # still warming — pressing Check now again costs nothing and changes nothing
          _test_thaw_json_write "$job" "$key" "$ready"
          runs_end ok 0 "" "\"tested_pending\":true" || log_warn "could not record run"
          ;;
      esac
    else
      # First cold test: warm the most recently modified object at Bulk, persist, exit pending.
      local key; key="$(rclone --config "$RCLONE_CONFIG" lsf -R --files-only --format tp "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/" 2>/dev/null | sort | tail -n1 | sed 's/^[^;]*;//')" || true
      [ -n "$key" ] || _fail "no objects found to test-restore for '$job'"
      _aws_restore_object "media/$job/$key" Bulk ""
      local ready; ready="$(date -u -d '+48 hours' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || _runs_now)"
      _test_thaw_json_write "$job" "media/$job/$key" "$ready"
      runs_end ok 0 "" "\"tested_pending\":true" || log_warn "could not record run"
    fi
  else
    # Warm tier: copy the first object straight down, verify, record.
    local key; key="$(rclone --config "$RCLONE_CONFIG" lsf -R --files-only "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/" 2>/dev/null | head -n1)" || true
    [ -n "$key" ] || _fail "no objects found to test-restore for '$job'"
    rclone --config "$RCLONE_CONFIG" copyto "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/$key" "$scratch/$(basename "$key")" || _fail "test copy failed for '$job'"
    local f bytes; f="$scratch/$(basename "$key")"
    [ -s "$f" ] || _fail "test restore produced no file for '$job'"
    bytes="$(_file_bytes "$f")"
    _tested_json_write "$job" "$key" "$bytes"; rm -rf "$scratch"
    runs_end ok 0 "" "\"tested_path\":$(_runs_str "$key"),\"tested_bytes\":$(_runs_num "$bytes")" || log_warn "could not record run"
  fi
}

# ---------------------------------------------------------------------------
# versioned-files — hands the actual tool work to the Python engine IN-PROCESS
# (not exec) so runs_end/_fail run here; JOB_* are already exported at the eval.
# ---------------------------------------------------------------------------
_restore_vfiles() {
  local job="$1"; shift
  case "${1:-}" in
    list)  # read-only: no lock, no record; catalog listing (optionally --json)
      python3 -m app.engine.vfiles restore "$job" "$@"
      ;;
    browse)  # read-only: no lock, no record (dispatched from main's list|thaw-status|browse case)
      shift  # drop "browse"; remaining: [relpath] [--json]
      local rel=""
      while [ $# -gt 0 ]; do case "$1" in --json) ;; *) rel="$1";; esac; shift; done
      rel="$(_safe_rel "$rel")" || { echo "explore: bad path" >&2; exit 2; }
      python3 -m app.engine.vfiles browse "$job" "$rel" --json
      ;;
    thaw)
      shift  # drop "thaw"; remaining: <scope|.> [--tier T] [--asof TS]
      local tier="Bulk" a prev=""
      for a in "$@"; do [ "$prev" = "--tier" ] && tier="$a"; prev="$a"; done
      local vlog="$CACHE_DIR/state/$job-vfiles.log" rc=0; : >"$vlog"
      runs_set_command "python3 -m app.engine.vfiles thaw $job $*"
      python3 -m app.engine.vfiles thaw "$job" "$@" 2>&1 | tee -a "$vlog" || rc=$?
      local tn; tn="$(_vfiles_stat "$vlog" thaw_requested)" || true
      RESTORE_EXTRA="\"objects_requested\":$(_runs_num "$tn"),\"thaw_requested\":$(_runs_num "$tn"),\"tier\":$(_runs_str "$tier")"
      [ "$rc" -eq 0 ] || _fail "file-history thaw failed for '$job'"
      _thaw_json_write "$job" "$tier" "." "${tn:-0}"
      runs_end ok 0 "" "$RESTORE_EXTRA" || log_warn "could not record run"
      ;;
    thaw-status)
      local scope="${2:-.}"; [ "$scope" = "." ] && scope=""
      : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"; export RCLONE_CONFIG
      _thaw_status "$job" "media/$job/$scope"
      ;;
    test) _test_vfiles "$job" ;;
    *)     # "." (restore_all) or a single relpath: <path|.> <target> [--asof][--tier]
      local vlog="$CACHE_DIR/state/$job-vfiles.log" rc=0; : >"$vlog"
      runs_set_command "python3 -m app.engine.vfiles restore $job $*"
      python3 -m app.engine.vfiles restore "$job" "$@" 2>&1 | tee -a "$vlog" || rc=$?
      local rn bn tn
      rn="$(_vfiles_stat "$vlog" restored)" || true
      bn="$(_vfiles_stat "$vlog" bytes)" || true
      tn="$(_vfiles_stat "$vlog" thaw_requested)" || true
      RESTORE_EXTRA="\"files_restored\":$(_runs_num "$rn"),\"bytes_restored\":$(_runs_num "$bn"),\"thaw_requested\":$(_runs_num "$tn")"
      [ "$rc" -eq 0 ] || _fail "file-history restore failed for '$job'"
      runs_end ok 0 "" "$RESTORE_EXTRA" || log_warn "could not record run"
      ;;
  esac
}

_test_vfiles() {
  local job="$1"; local scratch="$CACHE_DIR/restore-test/$job/$BE_RUN_ID"; mkdir -p "$scratch"
  local path
  path="$(python3 -m app.engine.vfiles restore "$job" list 2>/dev/null | head -n1 | cut -f1)" || true
  [ -n "$path" ] || _fail "no files found in the catalog to test-restore for '$job'"
  runs_set_command "python3 -m app.engine.vfiles restore $job $path $scratch"
  local out rc=0
  out="$(python3 -m app.engine.vfiles restore "$job" "$path" "$scratch" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || _fail "test restore failed for '$job'"
  case "$out" in
    *thaw-requested*|*'thaw requested'*)   # cold: the same pending state as a cold archive test
      _test_thaw_json_write "$job" "$path" ""
      runs_end ok 0 "" "\"tested_pending\":true" || log_warn "could not record run"
      return ;;
  esac
  local file bytes
  file="$(find "$scratch" -type f 2>/dev/null | head -n1)" || true
  { [ -n "$file" ] && [ -s "$file" ]; } || _fail "test restore produced no file for '$job'"
  bytes="$(_file_bytes "$file")"
  _tested_json_write "$job" "$path" "$bytes"; rm -rf "$scratch"
  runs_end ok 0 "" "\"tested_path\":$(_runs_str "$path"),\"tested_bytes\":$(_runs_num "$bytes")" || log_warn "could not record run"
}

# route by type; the per-type function's inner case handles the sub (existing model).
_dispatch() {
  local job="$1"; shift
  case "$JOB_TYPE" in
    versioned)       _restore_versioned "$job" "$@" ;;
    archive)         _restore_archive "$job" "$@" ;;
    versioned-files) _restore_vfiles "$job" "$@" ;;
    *) die "job '$job' has unknown type '$JOB_TYPE'" ;;
  esac
}

main() {
  if [ $# -lt 1 ]; then usage; exit 2; fi
  local job="$1"; shift
  _load
  # load the job def (JOB_* vars). Overridable for tests via JOBS_IO_CMD, mirroring backup-job.sh.
  local jobsio="${JOBS_IO_CMD:-python3 -m app.gui.jobs_io}"
  local def
  if ! def="$(CONFIG_DIR="${CONFIG_DIR:-/config}" $jobsio "$job")"; then
    die "job '$job' not found"
  fi
  # `set -a` exports the def so the versioned-files engine (a python3 child reading
  # os.environ) inherits JOB_STORAGE_CLASS/etc. Harmless to versioned/archive.
  set -a; eval "$def"; set +a
  # Re-derive RESTIC_REPOSITORY now that JOB_BUCKET (if this is a dedicated-bucket job) is in
  # scope — config.sh's source-time derivation (inside load_config, via _load above) ran before
  # the per-job env existed and could only ever see the base S3_BUCKET.
  derive_restic_repo

  local sub="${1:-}"
  case "$sub" in ""|-h|--help) usage; exit 2 ;; esac

  # Read-only actions: no lock, no run record, no EXIT trap (spec 7.5.3 §2).
  case "$sub" in
    list|thaw-status|browse) _dispatch "$job" "$@"; exit $? ;;
  esac

  # Mutating actions: kind, then lock -> record -> tee -> dispatch. HONORS a
  # pre-assigned BE_RUN_ID (ops.launch sets it) via runs_start's own guard.
  local kind
  case "$sub" in
    thaw)     kind="thaw" ;;
    test)     kind="test-restore" ;;
    download) kind="download" ;;
    *)        kind="restore" ;;   # restore | . | <path>
  esac
  trap 'runs_exit_trap "$?"' EXIT
  acquire_lock "$job"                                                   # die: "another <job> run is in progress"
  runs_start "$job" "$kind" "\"params\":{$(_build_params "$@")}"
  exec > >(tee -a "$CACHE_DIR/$BE_RUN_LOG") 2>&1; _BE_TEE_PID=$!
  _dispatch "$job" "$@"
}
main "$@"
