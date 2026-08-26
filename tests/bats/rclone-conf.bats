load test_helper

setup() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/rclone-conf.sh"
}

@test "AWS branch (no S3_ENDPOINT) requests server-side AES256 encryption" {
  unset S3_ENDPOINT
  export AWS_ACCESS_KEY_ID=AKIA_TEST AWS_SECRET_ACCESS_KEY=secret AWS_REGION=us-east-1
  local out="$BATS_TEST_TMPDIR/rclone.conf"
  render_rclone_conf "$out"
  grep -q "provider = AWS" "$out"
  grep -q "server_side_encryption = AES256" "$out"
}

@test "Other branch (S3_ENDPOINT set) forces path-style and drops SSE" {
  export S3_ENDPOINT="http://127.0.0.1:9000"
  export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin AWS_REGION=us-east-1
  local out="$BATS_TEST_TMPDIR/rclone.conf"
  render_rclone_conf "$out"
  grep -q "provider = Other" "$out"
  grep -q "force_path_style = true" "$out"
  ! grep -q "server_side_encryption" "$out"
}
