load test_helper

setup() {
  setup_common
  export AWS_REGION=us-east-1 S3_BUCKET=my-bucket
  export AWS_ACCESS_KEY_ID=AKIA AWS_SECRET_ACCESS_KEY=secret RESTIC_PASSWORD=pw
  export APPRISE_URLS=""
  export MEDIA_ROOT="$BATS_TEST_TMPDIR/media"
  export MEDIA_SHARES_DIR="$BATS_TEST_TMPDIR/shares"
  mkdir -p "$MEDIA_ROOT/comics" "$MEDIA_ROOT/books" "$MEDIA_SHARES_DIR"
  export RCLONE_LOG="$BATS_TEST_TMPDIR/rclone.log"; : >"$RCLONE_LOG"
  local stub="$BATS_TEST_TMPDIR/bin"; mkdir -p "$stub"
  cat >"$stub/rclone" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
exit 0
EOF
  chmod +x "$stub/rclone"
  export PATH="$stub:$PATH"
}

run_media() { run bash "$BATS_TEST_DIRNAME/../../scripts/backup-media.sh"; }

@test "one rclone copy per enabled share, to media/<share>" {
  printf '+ /**\n' >"$MEDIA_SHARES_DIR/comics.txt"
  printf '+ /**\n' >"$MEDIA_SHARES_DIR/books.txt"
  run_media
  [ "$status" -eq 0 ]
  grep -q "copy $MEDIA_ROOT/comics s3:my-bucket/media/comics" "$RCLONE_LOG"
  grep -q "copy $MEDIA_ROOT/books s3:my-bucket/media/books" "$RCLONE_LOG"
  grep -q '"outcome":"success"' "$CACHE_DIR/state/media.json"
}

@test "no enabled shares -> skip, success, no copy" {
  run_media
  [ "$status" -eq 0 ]
  run grep -E '^(copy|sync) ' "$RCLONE_LOG"
  [ "$status" -ne 0 ]   # no copy/sync line
  grep -q '"shares":0' "$CACHE_DIR/state/media.json"
  grep -q '"duration_s":0' "$CACHE_DIR/state/media.json"  # shape matches the populated-success JSON
}

@test "configured share whose source is missing -> failure" {
  printf '+ /**\n' >"$MEDIA_SHARES_DIR/ghost.txt"   # no $MEDIA_ROOT/ghost
  run_media
  [ "$status" -ne 0 ]
  grep -q '"outcome":"failure"' "$CACHE_DIR/state/media.json"
}

@test "MEDIA_MIRROR=true uses sync" {
  export MEDIA_MIRROR=true
  printf '+ /**\n' >"$MEDIA_SHARES_DIR/comics.txt"
  run_media
  grep -q "sync $MEDIA_ROOT/comics s3:my-bucket/media/comics" "$RCLONE_LOG"
}
