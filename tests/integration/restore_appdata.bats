load ../bats/test_helper
load minio_helper

setup() {
  minio_up
  export S3_BUCKET="usb-restore-$$"; minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export APPDATA_SRC="$BATS_TEST_TMPDIR/src"; mkdir -p "$APPDATA_SRC"
  echo "payload" >"$APPDATA_SRC/a.txt"
  export RESTIC_PASSWORD=testpass APPDATA_STORAGE_CLASS=STANDARD
  export RESTIC_REPOSITORY="s3:$AWS_S3_ENDPOINT/$S3_BUCKET/appdata"
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-appdata.sh"
  # Prove restore is source-independent: the primary restore scenario is a
  # fresh/rebuilt machine where the local source is absent or empty. Restore
  # reads FROM S3, not from APPDATA_SRC, so empty it now — before the restore
  # assertions below — and the tests must still pass.
  rm -rf "${APPDATA_SRC:?}"/*
}
teardown() { minio_down; }

@test "restore appdata list shows a snapshot" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" appdata list
  [ "$status" -eq 0 ]
  [[ "$output" == *"appdata"* ]]
}

@test "restore appdata restore latest recovers the file" {
  local out="$BATS_TEST_TMPDIR/out"; mkdir -p "$out"
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" appdata restore latest "$out"
  [ "$status" -eq 0 ]
  grep -rq "payload" "$out"
}
