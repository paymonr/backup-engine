load test_helper
setup() {
  setup_common
  export AWS_REGION=us-east-1 S3_BUCKET=my-bucket
  export AWS_ACCESS_KEY_ID=AKIA AWS_SECRET_ACCESS_KEY=secret RESTIC_PASSWORD=pw
  export RESTIC_REPOSITORY="s3:s3.us-east-1.amazonaws.com/my-bucket/appdata"
  export RCLONE_LOG="$BATS_TEST_TMPDIR/rclone.log" RESTIC_LOG="$BATS_TEST_TMPDIR/restic.log"
  : >"$RCLONE_LOG"; : >"$RESTIC_LOG"
  local b="$BATS_TEST_TMPDIR/bin"; mkdir -p "$b"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$RCLONE_LOG"\nexit 0\n' >"$b/rclone"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$RESTIC_LOG"\nexit 0\n' >"$b/restic"
  chmod +x "$b/rclone" "$b/restic"; export PATH="$b:$PATH"
  export JOBS_IO_STUB="$BATS_TEST_TMPDIR/jobsio.sh"
  export JOBS_IO_CMD="bash $JOBS_IO_STUB"
}
run_restore() { local job="$1"; shift; run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" "$job" "$@"; }

@test "versioned job -> restic snapshots --tag <job> for list" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run_restore cfg list
  [ "$status" -eq 0 ]
  grep -q -- "snapshots --tag cfg" "$RESTIC_LOG"
}

@test "versioned job -> restic restore <snap> --target <target> --tag <job>" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  [ "$status" -eq 0 ]
  grep -q -- "restore latest --target $out --tag cfg" "$RESTIC_LOG"
  [ -d "$out" ]
}

@test "versioned job with cold storage class warns before restoring" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  [ "$status" -eq 0 ]
  [[ "$output" == *"is cold"* ]]
}

@test "archive job -> rclone thaw lists media/<job>/<prefix>" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore movies thaw 2020/
  [ "$status" -eq 0 ]
  grep -q -- "lsf -R --files-only s3:my-bucket/media/movies/2020/" "$RCLONE_LOG"
}

@test "archive job -> rclone copy from media/<job>/<prefix> for download" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore movies download 2020/ "$out"
  [ "$status" -eq 0 ]
  grep -q -- "copy s3:my-bucket/media/movies/2020/ $out -v" "$RCLONE_LOG"
}

@test "versioned-files job -> dispatches to app.engine.vfiles module" {
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_restore vf list
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles restore vf list" "$PYTHON_LOG"
  [ ! -s "$RCLONE_LOG" ]
  [ ! -s "$RESTIC_LOG" ]
}

@test "versioned-files job -> restore with path/target/--asof/--tier passed through" {
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore vf a/b.txt "$out" --asof 1700000000 --tier Expedited
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles restore vf a/b.txt $out --asof 1700000000 --tier Expedited" "$PYTHON_LOG"
}

@test "unknown job -> clear error" {
  printf 'exit 3\n' >"$JOBS_IO_STUB"
  run_restore ghost list
  [ "$status" -ne 0 ]
  [[ "$output" == *"ghost"* ]]
}

@test "no job given -> usage" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage:"* ]]
}
