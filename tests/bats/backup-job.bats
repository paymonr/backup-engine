load test_helper
setup() {
  setup_common
  export AWS_REGION=us-east-1 S3_BUCKET=my-bucket
  export AWS_ACCESS_KEY_ID=AKIA AWS_SECRET_ACCESS_KEY=secret RESTIC_PASSWORD=pw APPRISE_URLS=""
  export SOURCE_ROOT="$BATS_TEST_TMPDIR/src"; mkdir -p "$SOURCE_ROOT/media/movies" "$SOURCE_ROOT/appdata"
  export RCLONE_LOG="$BATS_TEST_TMPDIR/rclone.log" RESTIC_LOG="$BATS_TEST_TMPDIR/restic.log"
  : >"$RCLONE_LOG"; : >"$RESTIC_LOG"
  local b="$BATS_TEST_TMPDIR/bin"; mkdir -p "$b"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$RCLONE_LOG"\nexit 0\n' >"$b/rclone"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$RESTIC_LOG"\n[ "$1" = "cat" ] && exit 1 || exit 0\n' >"$b/restic"
  chmod +x "$b/rclone" "$b/restic"; export PATH="$b:$PATH"
  export JOBS_IO_STUB="$BATS_TEST_TMPDIR/jobsio.sh"
  export JOBS_IO_CMD="bash $JOBS_IO_STUB"
  export LIFECYCLE_CMD="true"
  export SUMMARY_CMD="true"
}
run_job() { run bash "$BATS_TEST_DIRNAME/../../scripts/backup-job.sh" "$1"; }

# Make notify()/healthcheck() observable: they no-op without their env, so point APPRISE/HEALTHCHECK
# at stubbed `apprise`/`curl` binaries that append to marker files. A test can then assert an alert
# fired (marker present) or was correctly suppressed (marker absent).
_install_alert_probes() {
  local b="$BATS_TEST_TMPDIR/bin"
  export NOTIFY_MARKER="$BATS_TEST_TMPDIR/notify.marker" HC_MARKER="$BATS_TEST_TMPDIR/hc.marker"
  printf '#!/usr/bin/env bash\nprintf "notify %%s\\n" "$*" >>"$NOTIFY_MARKER"\nexit 0\n' >"$b/apprise"
  printf '#!/usr/bin/env bash\nprintf "curl %%s\\n" "$*" >>"$HC_MARKER"\nexit 0\n' >"$b/curl"
  chmod +x "$b/apprise" "$b/curl"
  export APPRISE_URLS="json://marker.local/x" HEALTHCHECK_URL="http://hc.local/deadmans-uuid"
}

@test "archive job -> rclone copy to media/<name>" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
  grep -q '"outcome":"success"' "$CACHE_DIR/state/movies.json"
}

@test "archive mirror -> rclone sync" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=true; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  grep -q "sync $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
}

@test "versioned job -> restic backup with tag + keep" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=tiered; echo JOB_KEEP_LAST=3; echo JOB_KEEP_DAILY=7; echo JOB_KEEP_WEEKLY=4; echo JOB_KEEP_MONTHLY=6\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  grep -q "backup $SOURCE_ROOT/appdata" "$RESTIC_LOG"
  grep -q -- "--tag cfg" "$RESTIC_LOG"
  grep -q -- "--keep-last 3" "$RESTIC_LOG"
}

@test "versioned job days retention -> restic forget --keep-within Nd" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=days; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  grep -q -- "forget --prune --tag cfg --keep-within 30d" "$RESTIC_LOG"
}

@test "versioned job count retention -> restic forget --keep-last N" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=count; echo JOB_RETENTION_COUNT=5\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  grep -q -- "forget --prune --tag cfg --keep-last 5" "$RESTIC_LOG"
}

@test "versioned job keep_all retention -> no restic forget" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  grep -q "backup $SOURCE_ROOT/appdata" "$RESTIC_LOG"
  ! grep -q -- "forget" "$RESTIC_LOG"
}

@test "Plain copy never runs a history clean-up of its own (S3 rules own it)" {
  local b="$BATS_TEST_TMPDIR/bin"
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=days; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
  ! grep -q "archive_prune" "$PYTHON_LOG"
}

@test "missing source dir -> failure" {
  printf 'echo JOB_NAME=x; echo JOB_TYPE=archive; echo JOB_SOURCE=media/gone; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false\n' >"$JOBS_IO_STUB"
  run_job x
  [ "$status" -ne 0 ]
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/x.json"
  # the run started (lock held, start line written) so it also owns a per-run failure record
  grep -q '"outcome":"failed"' "$CACHE_DIR/state/x.runs.jsonl"
}

@test "unknown job -> failure" {
  printf 'exit 3\n' >"$JOBS_IO_STUB"
  run_job ghost
  [ "$status" -ne 0 ]
}

@test "versioned-files job -> dispatches to app.engine.vfiles module (in-process, not exec)" {
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job vf
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles backup vf" "$PYTHON_LOG"
  # dispatched to python, not the archive/versioned engines (version_banner's
  # own "rclone version"/"restic version" probes still land in these logs)
  ! grep -q -- "copy " "$RCLONE_LOG"
  ! grep -q -- "backup " "$RESTIC_LOG"
  # control returned to main() -- NOT exec'd away -- so the success-path
  # state-file write (read by routes.py's Jobs page) actually ran
  grep -q '"outcome":"success"' "$CACHE_DIR/state/vf.json"
}

@test "versioned-files job failure -> _fail records outcome:failure (not silently exec'd away)" {
  local b="$BATS_TEST_TMPDIR/bin"
  printf '#!/usr/bin/env bash\nexit 1\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job vf
  [ "$status" -ne 0 ]
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/vf.json"
}

# --- Task 10 security: the REAL jobs_io CLI re-validates untrusted jobs.json ---
# These bypass the stub (JOBS_IO_CMD -> the real python module) so backup-job.sh
# exercises run-time source confinement, not just the write path.
@test "hand-written jobs.json escaping the mount to an existing dir is refused (no rclone)" {
  export PYTHONPATH="$BATS_TEST_DIRNAME/../.."
  export JOBS_IO_CMD="python3 -m app.gui.jobs_io"
  export CONFIG_DIR="$BATS_TEST_TMPDIR/config"; mkdir -p "$CONFIG_DIR"
  # a real dir OUTSIDE SOURCE_ROOT — `[ -d ]` alone would happily accept it
  mkdir -p "$BATS_TEST_TMPDIR/secret"; echo topsecret >"$BATS_TEST_TMPDIR/secret/creds"
  printf '%s\n' '{"jobs":[{"name":"evil","type":"archive","source":"../secret","schedule":"0 4 * * 0","enabled":true,"storage_class":"STANDARD","mirror":false}]}' >"$CONFIG_DIR/jobs.json"
  run_job evil
  [ "$status" -ne 0 ]
  [ ! -s "$RCLONE_LOG" ]   # data OUTSIDE the mount must never be copied
  [ ! -s "$RESTIC_LOG" ]
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/evil.json"
}

@test "valid jobs.json runs through the real jobs_io CLI (archive -> rclone copy)" {
  export PYTHONPATH="$BATS_TEST_DIRNAME/../.."
  export JOBS_IO_CMD="python3 -m app.gui.jobs_io"
  export CONFIG_DIR="$BATS_TEST_TMPDIR/config"; mkdir -p "$CONFIG_DIR"
  printf '%s\n' '{"jobs":[{"name":"movies","type":"archive","source":"media/movies","schedule":"0 4 * * 0","enabled":true,"storage_class":"DEEP_ARCHIVE","mirror":false,"retention":{"type":"keep_all"}}]}' >"$CONFIG_DIR/jobs.json"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
}

@test "versioned-files dispatch EXPORTS the job def to the python engine (regression)" {
  # emit_shell emits bare `JOB_*=...`; backup-job.sh must export the eval'd def so
  # the python3 CHILD (app.engine.vfiles reads os.environ) actually sees
  # JOB_STORAGE_CLASS/JOB_SOURCE/JOB_RETENTION_DAYS. Unlike the argv-logging stub
  # above, this stub inspects its ENVIRONMENT and fails when the def is absent --
  # exactly as vfiles._require_env("JOB_STORAGE_CLASS") does at runtime. Before the
  # export fix the vars are unexported shell vars and never reach the child.
  export PYENV_LOG="$BATS_TEST_TMPDIR/pyenv.log"; : >"$PYENV_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/python3" <<'STUB'
#!/usr/bin/env bash
printf 'JOB_STORAGE_CLASS=%s\n' "${JOB_STORAGE_CLASS-UNSET}" >>"$PYENV_LOG"
printf 'JOB_SOURCE=%s\n' "${JOB_SOURCE-UNSET}" >>"$PYENV_LOG"
printf 'JOB_RETENTION_DAYS=%s\n' "${JOB_RETENTION_DAYS-UNSET}" >>"$PYENV_LOG"
[ -n "${JOB_STORAGE_CLASS:-}" ] || exit 7   # mirrors vfiles _require_env
exit 0
STUB
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job vf
  [ "$status" -eq 0 ]
  grep -q "JOB_STORAGE_CLASS=STANDARD" "$PYENV_LOG"
  grep -q "JOB_SOURCE=appdata" "$PYENV_LOG"
  grep -q "JOB_RETENTION_DAYS=30" "$PYENV_LOG"
  grep -q '"outcome":"success"' "$CACHE_DIR/state/vf.json"
}

# --- Task 2 (7.1.5): append-only run records, honest prune failures, stat parsing --------------
# JSON is parsed with a REAL python3 in the TEST only; the runner never shells to python for
# bookkeeping (constraint asserted by the archive_prune / vfiles argv-log tests above).

@test "archive run: start+end records, rclone stats parsed, argv drops --stats-one-line, manual trigger" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$1" in check|version) exit 0 ;; esac
cat <<'OUT'
Transferred:   	    3.100 GiB / 3.100 GiB, 100%, 8.912 MiB/s, ETA 0s
Errors:                 0
Transferred:         1204 / 1204, 100%
OUT
exit 0
STUB
  chmod +x "$b/rclone"
  export BE_TRIGGER=manual
  printf 'echo JOB_NAME=manga; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job manga
  [ "$status" -eq 0 ]
  ! grep -q -- "--stats-one-line" "$RCLONE_LOG"
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"]; en=[r for r in recs if r["event"]=="end"]
assert len(st)==1 and len(en)==1, (len(st),len(en))
assert st[0]["trigger"]=="manual" and st[0]["type"]=="archive" and st[0]["storage_class"]=="DEEP_ARCHIVE"
e=en[0]
assert e["outcome"]=="ok", e["outcome"]
assert e["files_added"]==1204 and e["bytes_added"]==3328599654, (e["files_added"], e["bytes_added"])
assert e["files_total"] is None and e["bytes_total"] is None and e["rclone_errors"]==0
assert e["command"].startswith("rclone copy ")' <"$CACHE_DIR/state/manga.runs.jsonl"
}

@test "versioned run: start+end records carry snapshot_id and restic summary stats" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/restic" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RESTIC_LOG"
cmd=""; for a in "$@"; do case "$a" in cat|init|backup|forget|snapshots) cmd="$a"; break ;; esac; done
case "$cmd" in
  cat) exit 1 ;;
  init) exit 0 ;;
  backup) printf '%s\n' '{"message_type":"summary","files_new":2,"files_changed":4,"data_added":228589568,"total_files_processed":533,"total_bytes_processed":56594862080,"snapshot_id":"a81f3c2e9d7bdeadbeef01"}'; exit 0 ;;
  forget) exit 0 ;;
  *) exit 0 ;;
esac
STUB
  chmod +x "$b/restic"
  printf 'echo JOB_NAME=appdata; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=count; echo JOB_RETENTION_COUNT=5\n' >"$JOBS_IO_STUB"
  run_job appdata
  [ "$status" -eq 0 ]
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
e=[r for r in recs if r["event"]=="end"][0]
assert e["outcome"]=="ok" and e["snapshot_id"]=="a81f3c2e", e["snapshot_id"]
assert e["files_new"]==2 and e["files_changed"]==4 and e["files_added"]==6
assert e["bytes_added"]==228589568 and e["files_total"]==533 and e["bytes_total"]==56594862080' <"$CACHE_DIR/state/appdata.runs.jsonl"
  grep -q '"snapshot_id":"a81f3c2e"' "$CACHE_DIR/state/appdata.json"
}

@test "versioned prune AccessDenied -> failed, phase prune, copied true, snapshot_id + error carried" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/restic" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RESTIC_LOG"
cmd=""; for a in "$@"; do case "$a" in cat|init|backup|forget|snapshots) cmd="$a"; break ;; esac; done
case "$cmd" in
  cat) exit 1 ;;
  init) exit 0 ;;
  backup) printf '%s\n' '{"message_type":"summary","files_new":2,"files_changed":4,"data_added":228589568,"total_files_processed":533,"total_bytes_processed":56594862080,"snapshot_id":"a81f3c2edeadbeef"}'; exit 0 ;;
  forget) printf '%s\n' 'AccessDenied: s3:DeleteObjectVersion'; exit 1 ;;
  *) exit 0 ;;
esac
STUB
  chmod +x "$b/restic"
  printf 'echo JOB_NAME=appdata; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=count; echo JOB_RETENTION_COUNT=5\n' >"$JOBS_IO_STUB"
  run_job appdata
  [ "$status" -ne 0 ]
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
e=[r for r in recs if r["event"]=="end"][0]
assert e["outcome"]=="failed", e["outcome"]
assert e["phase"]=="prune", e.get("phase")
assert e["copied"] is True, e.get("copied")
assert e["snapshot_id"]=="a81f3c2e", e.get("snapshot_id")
assert "AccessDenied: s3:DeleteObjectVersion" in (e["error"] or ""), e.get("error")' <"$CACHE_DIR/state/appdata.runs.jsonl"
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/appdata.json"
}

@test "rclone copy prints a full stats block then exits 1 -> failed, phase copy, files_added still parsed" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$1" in check|version) exit 0 ;; esac
cat <<'OUT'
Transferred:   	    3.100 GiB / 3.100 GiB, 100%, 8.912 MiB/s, ETA 0s
Errors:                 3
Transferred:         1204 / 1204, 100%
OUT
exit 1
STUB
  chmod +x "$b/rclone"
  printf 'echo JOB_NAME=manga; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job manga
  [ "$status" -ne 0 ]
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
e=[r for r in recs if r["event"]=="end"][0]
assert e["outcome"]=="failed" and e["phase"]=="copy", (e["outcome"], e.get("phase"))
assert e["files_added"]==1204, e.get("files_added")
assert e["bytes_added"]==3328599654 and e["rclone_errors"]==3
assert e["copied"] is False, e.get("copied")' <"$CACHE_DIR/state/manga.runs.jsonl"
}

@test "lock collision before runs_start: no run record, legacy state untouched (GUI pre-set BE_RUN_ID)" {
  mkdir -p "$CACHE_DIR/locks" "$CACHE_DIR/state"
  local lock="$CACHE_DIR/locks/appdata.lock"; : >"$lock"
  printf '%s\n' '{"last_run":"2026-09-14T05:00:00Z","outcome":"success","type":"versioned"}' >"$CACHE_DIR/state/appdata.json"
  local before; before="$(cat "$CACHE_DIR/state/appdata.json")"
  flock -x "$lock" -c 'sleep 30' &
  local holder=$!
  sleep 0.4
  export BE_RUN_ID=20260915T050001Z-3f9a   # ops.launch pre-assigns it in the child env
  printf 'echo JOB_NAME=appdata; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job appdata
  kill "$holder" 2>/dev/null || true
  [ "$status" -ne 0 ]
  [ ! -e "$CACHE_DIR/state/appdata.runs.jsonl" ]
  [ "$(cat "$CACHE_DIR/state/appdata.json")" = "$before" ]
}

@test "acquire_lock (flock -w 5) outlasts a ~1s holder — the GUI poll probe must not skip a backup" {
  mkdir -p "$CACHE_DIR/locks"; local lock="$CACHE_DIR/locks/poll.lock"; : >"$lock"
  flock -x "$lock" -c 'sleep 1' &
  sleep 0.2
  run bash -c "CACHE_DIR='$CACHE_DIR' source '$BATS_TEST_DIRNAME/../../scripts/lib/common.sh'; acquire_lock poll"
  [ "$status" -eq 0 ]
}

@test "acquire_lock (flock -w 5) fails when a genuine holder keeps the lock past the wait" {
  mkdir -p "$CACHE_DIR/locks"; local lock="$CACHE_DIR/locks/busy.lock"; : >"$lock"
  flock -x "$lock" -c 'sleep 30' &
  local holder=$!
  sleep 0.3
  run bash -c "CACHE_DIR='$CACHE_DIR' source '$BATS_TEST_DIRNAME/../../scripts/lib/common.sh'; acquire_lock busy"
  kill "$holder" 2>/dev/null || true
  [ "$status" -eq 1 ]
  [[ "$output" == *"in progress"* ]]
}

@test "lock collision sends NO failure notification and does NOT trip the healthcheck" {
  _install_alert_probes
  mkdir -p "$CACHE_DIR/locks" "$CACHE_DIR/state"
  local lock="$CACHE_DIR/locks/appdata.lock"; : >"$lock"
  printf '%s\n' '{"last_run":"2026-09-14T05:00:00Z","outcome":"success","type":"versioned"}' >"$CACHE_DIR/state/appdata.json"
  local before; before="$(cat "$CACHE_DIR/state/appdata.json")"
  flock -x "$lock" -c 'sleep 30' &
  local holder=$!
  sleep 0.4
  export BE_RUN_ID=20260915T050001Z-3f9a   # ops.launch pre-assigns it in the child env
  printf 'echo JOB_NAME=appdata; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job appdata
  kill "$holder" 2>/dev/null || true
  [ "$status" -ne 0 ]
  # the collision's only report is the die() WARN in the shared log: no per-run record, legacy state
  # untouched, and crucially NO failure alert and NO dead-man ping (a run IS in progress).
  [ ! -e "$CACHE_DIR/state/appdata.runs.jsonl" ]
  [ "$(cat "$CACHE_DIR/state/appdata.json")" = "$before" ]
  [ ! -e "$NOTIFY_MARKER" ]
  [ ! -e "$HC_MARKER" ]
}

@test "a post-start failure DOES notify + ping the healthcheck (the guard is not over-broad)" {
  _install_alert_probes
  # a missing source dir fails AFTER runs_start (BE_RUN_STARTED=1), so the alert block must run
  printf 'echo JOB_NAME=x; echo JOB_TYPE=archive; echo JOB_SOURCE=media/gone; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job x
  [ "$status" -ne 0 ]
  grep -q '"outcome":"failed"' "$CACHE_DIR/state/x.runs.jsonl"
  [ -s "$NOTIFY_MARKER" ] && grep -q "notify" "$NOTIFY_MARKER"
  [ -s "$HC_MARKER" ] && grep -q "/fail" "$HC_MARKER"
}

@test "versioned prune clears stale locks first (restic unlock before forget)" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=days; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  grep -q -- "unlock" "$RESTIC_LOG"
  # unlock must be issued BEFORE forget --prune (else a stale lock blocks the prune)
  ul=$(grep -n -- "unlock" "$RESTIC_LOG" | head -1 | cut -d: -f1)
  fg=$(grep -n -- "forget --prune" "$RESTIC_LOG" | head -1 | cut -d: -f1)
  [ -n "$ul" ] && [ -n "$fg" ] && [ "$ul" -lt "$fg" ]
}

@test "keep_all versioned job does not unlock or forget" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  ! grep -q -- "unlock" "$RESTIC_LOG"
}

@test "_first_error_line surfaces a restic lock message via fallback (no 'error' word)" {
  source "$BATS_TEST_DIRNAME/../../scripts/lib/runs.sh"
  log="$BATS_TEST_TMPDIR/plog"
  printf 'repository is already locked by PID 398\nthe unlock command can be used to remove stale locks\n' >"$log"
  run _first_error_line "$log"
  [ -n "$output" ]
  [[ "$output" == *"locked"* || "$output" == *"unlock"* ]]
}

# --- Task 3: bounded retry-with-resume on transient failures ---------------------------------

@test "versioned backup retries a transient failure then succeeds (resumes, same command)" {
  # restic backup fails once with a 503, then succeeds; cat->init as usual
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
if [[ "$*" == *"backup"* ]]; then
  n=$(grep -c backup "$RESTIC_LOG")
  if [ "$n" -eq 1 ]; then echo "http status 503 SlowDown" ; exit 1; fi
fi
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job cfg
  [ "$status" -eq 0 ]
  [ "$(grep -c 'backup .*--tag cfg' "$RESTIC_LOG")" -ge 2 ]   # retried
  grep -q '"outcome":"success"' "$CACHE_DIR/state/cfg.json"
}

@test "versioned backup does NOT retry a permanent (AccessDenied) failure" {
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
[[ "$*" == *"backup"* ]] && { echo "AccessDenied: not authorized"; exit 1; }
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job cfg
  [ "$status" -ne 0 ]
  [ "$(grep -c 'backup .*--tag cfg' "$RESTIC_LOG")" -eq 1 ]   # NOT retried
  grep -q '"outcome":"failed"' "$CACHE_DIR/state/cfg.runs.jsonl"
}

# RULING P1: restic's --json stdout must keep landing in <job>-last.jsonl on every attempt (the
# live progress monitor tails it), while the transient classifier reads a SEPARATE log fed from
# restic's real stderr -- not the bulk --json status stream. This test uses a true stderr error
# (unlike the stdout-based fake error above) and checks -last.jsonl still carries the final
# successful attempt's summary after the retry.
@test "versioned backup: stderr transient error retries, and -last.jsonl still carries the final --json summary (dual sink)" {
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
if [[ "$*" == *"backup"* ]]; then
  n=$(grep -c backup "$RESTIC_LOG")
  if [ "$n" -eq 1 ]; then echo "connection reset by peer" >&2; exit 1; fi
  echo '{"message_type":"summary","snapshot_id":"deadbeefsnap0102"}'
fi
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job cfg
  [ "$status" -eq 0 ]
  [ "$(grep -c 'backup .*--tag cfg' "$RESTIC_LOG")" -ge 2 ]
  grep -q '"snapshot_id":"deadbeefsnap0102"' "$CACHE_DIR/state/cfg-last.jsonl"
  grep -q '"outcome":"success"' "$CACHE_DIR/state/cfg.json"
}

# RULING P2: the live attempt marker. Written at the start of each attempt, visible to a RUNNING
# run (a later progress-UI task reads it), and removed once retrying stops -- here via exhaustion
# (BE_MAX_ATTEMPTS=2), the "final failure" removal path.
@test "retry writes the live attempt number to <job>.attempt and removes it once retries are exhausted" {
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
if [[ "$*" == *"backup"* ]]; then
  { cat "$CACHE_DIR/state/cfg.attempt" 2>/dev/null || echo MISSING; } >>"$ATTEMPTS_SEEN"
  printf '\n' >>"$ATTEMPTS_SEEN"
  echo "http status 503 SlowDown"
  exit 1
fi
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  export ATTEMPTS_SEEN="$BATS_TEST_TMPDIR/attempts_seen.log"; : >"$ATTEMPTS_SEEN"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_MAX_ATTEMPTS=2 BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job cfg
  [ "$status" -ne 0 ]
  [ "$(grep -c 'backup .*--tag cfg' "$RESTIC_LOG")" -eq 2 ]
  [ "$(sed -n '1p' "$ATTEMPTS_SEEN")" = "1" ]
  [ "$(sed -n '2p' "$ATTEMPTS_SEEN")" = "2" ]
  [ ! -e "$CACHE_DIR/state/cfg.attempt" ]
}

@test "archive job: rclone copy retries a transient failure then succeeds" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/rclone" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RCLONE_LOG"
case "$1" in check|version) exit 0 ;; esac
n=$(grep -c '^copy ' "$RCLONE_LOG")
if [ "$n" -eq 1 ]; then echo "RequestTimeout: your socket connection was not read from"; exit 1; fi
exit 0
EOF
  chmod +x "$b/rclone"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job movies
  [ "$status" -eq 0 ]
  [ "$(grep -c '^copy ' "$RCLONE_LOG")" -ge 2 ]
  grep -q '"outcome":"success"' "$CACHE_DIR/state/movies.json"
}

@test "_first_error_line still prefers an explicit error line" {
  source "$BATS_TEST_DIRNAME/../../scripts/lib/runs.sh"
  log="$BATS_TEST_TMPDIR/plog"
  printf 'some info line\nFatal: AccessDenied for that key\ntrailing\n' >"$log"
  run _first_error_line "$log"
  [[ "$output" == *"AccessDenied"* ]]
}

# --- Task 4: paused outcome — runner honours a stop flag --------------------------------------

@test "a stop flag + SIGTERM records outcome paused, not failed, and does not retry" {
  # restic backup blocks until signalled; the control flag is pre-set to 'pause'
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
[[ "$*" == *"backup"* ]] && { trap 'exit 130' TERM INT; while :; do sleep 0.2; done; }
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  mkdir -p "$CACHE_DIR/state"; echo pause >"$CACHE_DIR/state/cfg.control"
  ( sleep 1; pkill -TERM -f "backup-job.sh cfg" ) &
  BE_SLEEP_CMD=true run_job cfg
  grep -q '"outcome":"paused"' "$CACHE_DIR/state/cfg.runs.jsonl"
  ! grep -q '"outcome":"failed"' "$CACHE_DIR/state/cfg.runs.jsonl"
  [ ! -f "$CACHE_DIR/state/cfg.control" ]   # flag cleared
}

# --- Task 6: auto-resume markers (app.engine.resume re-triggers with BE_RESUME=1) --------------

@test "BE_RESUME=1 run creates .resuming at start and removes it when the run ends" {
  local b="$BATS_TEST_TMPDIR/bin"
  export RESUMING_SEEN="$BATS_TEST_TMPDIR/resuming_seen.log"; : >"$RESUMING_SEEN"
  cat >"$b/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
if [[ "$*" == *"backup"* ]]; then
  { [ -f "$CACHE_DIR/state/cfg.resuming" ] && echo PRESENT || echo MISSING; } >>"$RESUMING_SEEN"
  printf '%s\n' '{"message_type":"summary","snapshot_id":"deadbeefsnap0102"}'
fi
exit 0
EOF
  chmod +x "$b/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RESUME=1 run_job cfg
  [ "$status" -eq 0 ]
  [ "$(cat "$RESUMING_SEEN")" = "PRESENT" ]     # present while the run was in progress
  [ ! -f "$CACHE_DIR/state/cfg.resuming" ]      # removed once the run ended
}

@test "BE_RESUME=1 run still removes .resuming on a failed run (not just success)" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
[[ "$*" == *"backup"* ]] && { echo "AccessDenied: not authorized"; exit 1; }
exit 0
EOF
  chmod +x "$b/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true BE_RESUME=1 run_job cfg
  [ "$status" -ne 0 ]
  [ ! -f "$CACHE_DIR/state/cfg.resuming" ]
}

@test "a successful run clears the job's resume-cap counter (state/<job>.resumes)" {
  mkdir -p "$CACHE_DIR/state"; printf '2' >"$CACHE_DIR/state/cfg.resumes"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  [ ! -f "$CACHE_DIR/state/cfg.resumes" ]
}

# --- Task 5: S3 rules tamper check runs before every backup ------------------------------------

@test "the S3 rules check runs before the backup, for the job's bucket" {
  local stub="$BATS_TEST_TMPDIR/lifecycle.sh"; export ORDER_LOG="$BATS_TEST_TMPDIR/order.log"; : >"$ORDER_LOG"
  printf '#!/usr/bin/env bash\necho "lifecycle $*" >>"$ORDER_LOG"\nexit 0\n' >"$stub"
  export LIFECYCLE_CMD="bash $stub"
  printf '#!/usr/bin/env bash\necho "rclone $1" >>"$ORDER_LOG"\nexit 0\n' >"$BATS_TEST_TMPDIR/bin/rclone"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=days; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  # version_banner (run earlier in main()) also calls `rclone version`, which the rclone stub
  # above logs to ORDER_LOG too -- so assert ORDER, not that the lifecycle check is literally
  # the first line (controller ruling, task-5-brief.md's defect).
  local lc_line copy_line
  lc_line=$(grep -n "^lifecycle check --bucket my-bucket" "$ORDER_LOG" | head -1 | cut -d: -f1)
  copy_line=$(grep -n "^rclone copy" "$ORDER_LOG" | head -1 | cut -d: -f1)
  # each on its own line (not `A && B && C`): under bats' `set -e`, only the LAST command
  # of a `&&`/`||` chain is allowed to fail the test -- a failure of an earlier command in
  # such a chain is silently swallowed (classic bash gotcha), which would let this assertion
  # pass even when the lifecycle check never ran.
  [ -n "$lc_line" ]
  [ -n "$copy_line" ]
  [ "$lc_line" -lt "$copy_line" ]
}

@test "a failing S3 rules check never stops the backup" {
  local stub="$BATS_TEST_TMPDIR/lifecycle.sh"
  printf '#!/usr/bin/env bash\nexit 1\n' >"$stub"
  export LIFECYCLE_CMD="bash $stub"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
}

@test "a hung S3 rules check is cut off by the timeout and the backup still runs" {
  local stub="$BATS_TEST_TMPDIR/lifecycle.sh"
  printf '#!/usr/bin/env bash\nexec sleep 30\n' >"$stub"
  export LIFECYCLE_CMD="bash $stub" LIFECYCLE_TIMEOUT=1
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  local start=$SECONDS
  run_job movies
  [ "$status" -eq 0 ]
  [ $((SECONDS - start)) -lt 15 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
  [[ "$output" == *"S3 rules check could not run"* ]]
}

@test "the S3 rules check runs under a timeout" {
  local stub="$BATS_TEST_TMPDIR/lifecycle.sh" log="$BATS_TEST_TMPDIR/lc.log"
  printf '#!/usr/bin/env bash\necho "$*" >>"%s"\n' "$log" >"$stub"
  export LIFECYCLE_CMD="bash $stub"
  local b="$BATS_TEST_TMPDIR/bin" tlog="$BATS_TEST_TMPDIR/timeout.log"
  # a `timeout` shim that records its duration argument then runs the command
  printf '#!/usr/bin/env bash\necho "timeout $1" >>"%s"\nshift\nexec "$@"\n' "$tlog" >"$b/timeout"; chmod +x "$b/timeout"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -qx "timeout 120" "$tlog"
  grep -q "^check --bucket my-bucket" "$log"
}

@test "the S3 rules check is told who started the run" {
  local stub="$BATS_TEST_TMPDIR/lifecycle.sh" log="$BATS_TEST_TMPDIR/lc.log"
  printf '#!/usr/bin/env bash\necho "$*" >>"%s"\n' "$log" >"$stub"
  export LIFECYCLE_CMD="bash $stub" BE_TRIGGER=manual
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -qx "check --bucket my-bucket --trigger manual" "$log"
}

@test "a scheduled run's S3 rules check says scheduled" {
  local stub="$BATS_TEST_TMPDIR/lifecycle.sh" log="$BATS_TEST_TMPDIR/lc.log"
  printf '#!/usr/bin/env bash\necho "$*" >>"%s"\n' "$log" >"$stub"
  export LIFECYCLE_CMD="bash $stub"
  unset BE_TRIGGER
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -qx "check --bucket my-bucket --trigger scheduled" "$log"
}

@test "a finished run starts a detached storage summary of its folder, with no run id of its own" {
  local stub="$BATS_TEST_TMPDIR/summary.sh" out="$BATS_TEST_TMPDIR/summary.log"
  printf '#!/usr/bin/env bash\necho "$* trigger=${BE_TRIGGER:-unset} run=${BE_RUN_ID:-unset}" >>"%s"\n' "$out" >"$stub"
  export SUMMARY_CMD="bash $stub"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  for _ in $(seq 1 50); do [ -s "$out" ] && break; sleep 0.1; done
  grep -qx "storage-summary --job movies trigger=scheduled run=unset" "$out"
}

@test "a storage summary that can't start never fails the run" {
  export SUMMARY_CMD="$BATS_TEST_TMPDIR/no-such-command"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
}
