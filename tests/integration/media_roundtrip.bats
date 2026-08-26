load ../bats/test_helper
load minio_helper

setup() {
  minio_up
  export S3_BUCKET="usb-media-$$"; minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export MEDIA_SRC="$BATS_TEST_TMPDIR/media"; mkdir -p "$MEDIA_SRC/comics" "$MEDIA_SRC/movies"
  echo "chapter-1" >"$MEDIA_SRC/comics/ch1.cbz"
  echo "should-be-excluded" >"$MEDIA_SRC/movies/big.mkv"
  export MEDIA_INCLUDES="$BATS_TEST_TMPDIR/includes.txt"
  printf '+ /comics/**\n- **\n' >"$MEDIA_INCLUDES"
  export MEDIA_STORAGE_CLASS=STANDARD MEDIA_MIRROR=false RCLONE_TRANSFERS=4
  export RCLONE_CONFIG="$BATS_TEST_TMPDIR/rclone.conf"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/rclone-conf.sh"
  render_rclone_conf "$RCLONE_CONFIG"
}
teardown() { minio_down; }

@test "media copy uploads only included dirs" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/backup-media.sh"
  [ "$status" -eq 0 ]
  run rclone --config "$RCLONE_CONFIG" ls "s3:$S3_BUCKET/media"
  [[ "$output" == *"comics/ch1.cbz"* ]]
  [[ "$output" != *"movies/big.mkv"* ]]
}

@test "additive: deleting source file does not delete from destination" {
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-media.sh"
  rm "$MEDIA_SRC/comics/ch1.cbz"
  echo "chapter-2" >"$MEDIA_SRC/comics/ch2.cbz"
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-media.sh"
  run rclone --config "$RCLONE_CONFIG" ls "s3:$S3_BUCKET/media"
  [[ "$output" == *"comics/ch1.cbz"* ]]   # still present (copy, not sync)
  [[ "$output" == *"comics/ch2.cbz"* ]]
}

@test "restore: rclone copy back down round-trips content" {
  bash "$BATS_TEST_DIRNAME/../../scripts/backup-media.sh"
  local out="$BATS_TEST_TMPDIR/restored"; mkdir -p "$out"
  rclone --config "$RCLONE_CONFIG" copy "s3:$S3_BUCKET/media/comics" "$out"
  grep -q "chapter-1" "$out/ch1.cbz"
}
