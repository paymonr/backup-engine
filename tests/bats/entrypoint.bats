load test_helper

setup() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export CFG="$BATS_TEST_TMPDIR/config"; mkdir -p "$CFG"
  cat >"$CFG/backup.env" <<EOF
AWS_REGION=us-east-1
S3_BUCKET=b
APPDATA_SCHEDULE=0 3 * * *
MEDIA_SCHEDULE=0 4 * * 0
APPDATA_SRC=$BATS_TEST_TMPDIR/adata
MEDIA_SRC=$BATS_TEST_TMPDIR/media
MEDIA_INCLUDES=$CFG/includes-media.txt
EOF
  cat >"$CFG/secrets.env" <<EOF
AWS_ACCESS_KEY_ID=k
AWS_SECRET_ACCESS_KEY=s
RESTIC_PASSWORD=p
EOF
  : >"$CFG/includes-media.txt"
  mkdir -p "$BATS_TEST_TMPDIR/adata" "$BATS_TEST_TMPDIR/media"
  echo x >"$BATS_TEST_TMPDIR/adata/f"
  export CONFIG_DIR="$CFG"
}

@test "entrypoint --emit-crontab writes both schedules" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  grep -q "0 3 \* \* \* .*backup-appdata.sh" "$CACHE_DIR/crontab"
  grep -q "0 4 \* \* 0 .*backup-media.sh" "$CACHE_DIR/crontab"
}

@test "entrypoint renders rclone.conf and password file" {
  bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ -f "$CACHE_DIR/rclone.conf" ]
  [ -f "$CACHE_DIR/restic-password" ]
  [ "$(stat -c '%a' "$CACHE_DIR/rclone.conf")" = "600" ]
}

@test "entrypoint with GUI_ENABLED=false emits crontab and does not require the GUI" {
  echo "GUI_ENABLED=false" >>"$CFG/backup.env"
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  grep -q "backup-appdata.sh" "$CACHE_DIR/crontab"
}
