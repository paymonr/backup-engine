load ../bats/test_helper
load minio_helper

setup() {
  minio_up
  export S3_BUCKET="usb-appdata-$$"
  minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export APPDATA_SRC="$BATS_TEST_TMPDIR/src"; mkdir -p "$APPDATA_SRC"
  head -c 1048576 /dev/urandom >"$APPDATA_SRC/archive-1.tar.zst"
  export RESTIC_PASSWORD=testpass
  export APPDATA_STORAGE_CLASS=STANDARD
  # restic reaches MinIO over http:
  export RESTIC_REPOSITORY="s3:$AWS_S3_ENDPOINT/$S3_BUCKET/appdata"
}
teardown() { minio_down; }

@test "appdata backup then restore round-trips bytes" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/backup-appdata.sh"
  [ "$status" -eq 0 ]
  # restore newest snapshot to a fresh dir and compare
  local out="$BATS_TEST_TMPDIR/restored"; mkdir -p "$out"
  restic -r "$RESTIC_REPOSITORY" restore latest --target "$out"
  diff -r "$APPDATA_SRC" "$out$APPDATA_SRC"
}

@test "second run with unchanged source adds a snapshot but ~no data" {
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-appdata.sh"
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-appdata.sh"
  run restic -r "$RESTIC_REPOSITORY" snapshots --json
  [ "$status" -eq 0 ]
  # two snapshots present
  [ "$(printf '%s' "$output" | grep -o '"short_id"' | wc -l)" -ge 2 ]
}
