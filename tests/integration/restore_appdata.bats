load ../bats/test_helper
load minio_helper

# Exercises restore.sh's per-job dispatch against a real restic repo + MinIO
# for a versioned job. The job type is read via JOBS_IO_CMD (a stub, same
# mechanism backup-job.bats uses) so this doesn't depend on a config/jobs.json
# fixture; restic/rclone themselves are real.
setup() {
  minio_up
  export S3_BUCKET="usb-restore-$$"; minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export SOURCE_ROOT="$BATS_TEST_TMPDIR/src"; mkdir -p "$SOURCE_ROOT/appdata"
  echo "payload" >"$SOURCE_ROOT/appdata/a.txt"
  export RESTIC_PASSWORD=testpass
  export RESTIC_REPOSITORY="s3:$AWS_S3_ENDPOINT/$S3_BUCKET/appdata"
  export JOBS_IO_STUB="$BATS_TEST_TMPDIR/jobsio.sh"
  export JOBS_IO_CMD="bash $JOBS_IO_STUB"
  printf 'echo JOB_NAME=appdata; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_KEEP_LAST=3; echo JOB_KEEP_DAILY=7; echo JOB_KEEP_WEEKLY=4; echo JOB_KEEP_MONTHLY=6\n' >"$JOBS_IO_STUB"
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-job.sh" appdata
  # Prove restore is source-independent: the primary restore scenario is a
  # fresh/rebuilt machine where the local source is absent or empty. Restore
  # reads FROM S3, not from SOURCE_ROOT, so empty it now — before the restore
  # assertions below — and the tests must still pass.
  rm -rf "${SOURCE_ROOT:?}/appdata"/*
}
teardown() { minio_down; }

@test "restore <job> list shows a snapshot tagged with the job name" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" appdata list
  [ "$status" -eq 0 ]
  [[ "$output" == *"appdata"* ]]
}

@test "restore <job> restore latest recovers the file" {
  local out="$BATS_TEST_TMPDIR/out"; mkdir -p "$out"
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" appdata restore latest "$out"
  [ "$status" -eq 0 ]
  grep -rq "payload" "$out"
}

@test "restore of an unknown job fails clearly instead of touching the repo" {
  printf 'exit 3\n' >"$JOBS_IO_STUB"
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" ghost list
  [ "$status" -ne 0 ]
  [[ "$output" == *"ghost"* ]]
}
