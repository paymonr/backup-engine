load ../bats/test_helper
load minio_helper

# Exercises backup-job.sh + restore.sh's per-job dispatch against a real MinIO
# for an archive job. The job type is read via JOBS_IO_CMD (a stub, same
# mechanism backup-job.bats/restore_appdata.bats use) so this doesn't depend
# on a config/jobs.json fixture; rclone itself is real (no restic here).
setup() {
  minio_up
  export S3_BUCKET="usb-archive-$$"; minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export SOURCE_ROOT="$BATS_TEST_TMPDIR/src"; mkdir -p "$SOURCE_ROOT/movies"
  echo "payload" >"$SOURCE_ROOT/movies/a.txt"
  export JOBS_IO_STUB="$BATS_TEST_TMPDIR/jobsio.sh"
  export JOBS_IO_CMD="bash $JOBS_IO_STUB"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_SOURCE=movies; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_MIRROR=false\n' >"$JOBS_IO_STUB"
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-job.sh" movies
}
teardown() { minio_down; }

@test "backup-job.sh movies uploads to s3:<bucket>/media/movies/" {
  run rclone --config "$CACHE_DIR/rclone.conf" lsf "s3:$S3_BUCKET/media/movies"
  [ "$status" -eq 0 ]
  [[ "$output" == *"a.txt"* ]]
}

@test "restore <job> download recovers the file" {
  # <prefix> is a subpath within the job's own media/movies/… destination
  # (restore.sh requires a non-empty prefix); "a.txt" is an exact-object
  # prefix here, which rclone copy resolves to just that one file.
  local out="$BATS_TEST_TMPDIR/out"; mkdir -p "$out"
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" movies download a.txt "$out"
  [ "$status" -eq 0 ]
  grep -rq "payload" "$out"
}

@test "restore of an unknown job fails clearly instead of touching the bucket" {
  printf 'exit 3\n' >"$JOBS_IO_STUB"
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" ghost download a.txt "$BATS_TEST_TMPDIR/out2"
  [ "$status" -ne 0 ]
  [[ "$output" == *"ghost"* ]]
}
