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

@test "archive job days retention -> archive_prune invoked with --type days --days N" {
  local b="$BATS_TEST_TMPDIR/bin"
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=days; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.archive_prune movies --type days --days 30 --count 1" "$PYTHON_LOG"
}

@test "archive job keep_all retention -> no archive_prune call" {
  local b="$BATS_TEST_TMPDIR/bin"
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
  [ ! -s "$PYTHON_LOG" ]
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
  # keep_all isolates this test to its actual intent (real jobs_io CLI -> rclone copy). Without it the
  # default archive retention is days/180, which now (7.1.6, prune failures are failures) runs the real
  # archive_prune against a bucket that does not exist in the sandbox and correctly fails the job.
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
