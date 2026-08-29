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

@test "archive job -> rclone copy to media/<name>" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=false\n' >"$JOBS_IO_STUB"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
  grep -q '"outcome":"success"' "$CACHE_DIR/state/movies.json"
}

@test "archive mirror -> rclone sync" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=media/movies; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_MIRROR=true\n' >"$JOBS_IO_STUB"
  run_job movies
  grep -q "sync $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
}

@test "versioned job -> restic backup with tag + keep" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_KEEP_LAST=3; echo JOB_KEEP_DAILY=7; echo JOB_KEEP_WEEKLY=4; echo JOB_KEEP_MONTHLY=6\n' >"$JOBS_IO_STUB"
  run_job cfg
  [ "$status" -eq 0 ]
  grep -q "backup $SOURCE_ROOT/appdata" "$RESTIC_LOG"
  grep -q -- "--tag cfg" "$RESTIC_LOG"
  grep -q -- "--keep-last 3" "$RESTIC_LOG"
}

@test "missing source dir -> failure" {
  printf 'echo JOB_NAME=x; echo JOB_TYPE=archive; echo JOB_SOURCE=media/gone; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false\n' >"$JOBS_IO_STUB"
  run_job x
  [ "$status" -ne 0 ]
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/x.json"
}

@test "unknown job -> failure" {
  printf 'exit 3\n' >"$JOBS_IO_STUB"
  run_job ghost
  [ "$status" -ne 0 ]
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
  printf '%s\n' '{"jobs":[{"name":"movies","type":"archive","source":"media/movies","schedule":"0 4 * * 0","enabled":true,"storage_class":"DEEP_ARCHIVE","mirror":false}]}' >"$CONFIG_DIR/jobs.json"
  run_job movies
  [ "$status" -eq 0 ]
  grep -q "copy $SOURCE_ROOT/media/movies s3:my-bucket/media/movies" "$RCLONE_LOG"
}
