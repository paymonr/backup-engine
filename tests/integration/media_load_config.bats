load ../bats/test_helper
load minio_helper

# Unlike media_roundtrip.bats (which presets MEDIA_SRC/RCLONE_TRANSFERS/etc
# directly and skips load_config entirely — CONFIG_DIR/backup.env never
# exists there), this test writes a real config/-style backup.env +
# secrets.env and lets backup-media.sh's real `load_config` path populate
# everything, exactly like a production container mounting /config. This is
# the path that leaked RCLONE_BWLIMIT="" into rclone's environment via
# load_config's blanket `export`, breaking every media run under the
# shipped default config (RCLONE_BWLIMIT commented out/absent).
setup() {
  minio_up
  export S3_BUCKET="usb-media-cfg-$$"; minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"

  local media_src="$BATS_TEST_TMPDIR/media"
  mkdir -p "$media_src/comics"
  echo "chapter-1" >"$media_src/comics/ch1.cbz"

  local includes="$BATS_TEST_TMPDIR/includes.txt"
  printf '+ /comics/**\n- **\n' >"$includes"

  export CONFIG_DIR="$BATS_TEST_TMPDIR/config"; mkdir -p "$CONFIG_DIR"
  cat >"$CONFIG_DIR/backup.env" <<EOF
AWS_REGION=$AWS_REGION
S3_BUCKET=$S3_BUCKET
S3_ENDPOINT=$S3_ENDPOINT
MEDIA_SRC=$media_src
MEDIA_INCLUDES=$includes
MEDIA_STORAGE_CLASS=STANDARD
MEDIA_MIRROR=false
#RCLONE_BWLIMIT=            # left commented/absent, exactly like the shipped config/backup.env.example
EOF
  cat >"$CONFIG_DIR/secrets.env" <<EOF
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
RESTIC_PASSWORD=testpass
EOF
}
teardown() { minio_down; }

@test "media copy succeeds through the real load_config path with default (unset) RCLONE_BWLIMIT" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/backup-media.sh"
  [ "$status" -eq 0 ]
  run rclone --config "$CACHE_DIR/rclone.conf" ls "s3:$S3_BUCKET/media"
  [ "$status" -eq 0 ]
  [[ "$output" == *"comics/ch1.cbz"* ]]
}
