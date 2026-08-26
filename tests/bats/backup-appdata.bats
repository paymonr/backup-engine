load test_helper

setup() { setup_common; }

@test "missing/empty appdata source fails fast and records failure state" {
  export AWS_REGION=us-east-1
  export S3_BUCKET=my-bucket
  export AWS_ACCESS_KEY_ID=AKIA_TEST
  export AWS_SECRET_ACCESS_KEY=secret
  export RESTIC_PASSWORD=testpass
  export RESTIC_REPOSITORY="s3:s3.us-east-1.amazonaws.com/my-bucket/appdata"
  export APPDATA_SRC="$BATS_TEST_TMPDIR/empty-src"; mkdir -p "$APPDATA_SRC"
  export APPRISE_URLS=""

  run bash "$BATS_TEST_DIRNAME/../../scripts/backup-appdata.sh"
  [ "$status" -ne 0 ]
  [ -f "$CACHE_DIR/state/appdata.json" ]
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/appdata.json"
}
