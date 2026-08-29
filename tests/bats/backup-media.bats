load test_helper

setup() {
  setup_common
  export AWS_REGION=us-east-1 S3_BUCKET=my-bucket
  export AWS_ACCESS_KEY_ID=AKIA AWS_SECRET_ACCESS_KEY=secret RESTIC_PASSWORD=pw
  export APPRISE_URLS=""
  export MEDIA_ROOT="$BATS_TEST_TMPDIR/media"
  export MEDIA_INCLUDES="$BATS_TEST_TMPDIR/media-includes.txt"
  mkdir -p "$MEDIA_ROOT/Movies" "$MEDIA_ROOT/Photos"
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

@test "single filtered copy over MEDIA_ROOT -> media/" {
  printf '+ /Movies/**\n+ /Movies/\n- **\n' >"$MEDIA_INCLUDES"
  run_media
  [ "$status" -eq 0 ]
  grep -q "copy $MEDIA_ROOT s3:my-bucket/media" "$RCLONE_LOG"
  grep -q -- "--filter-from $MEDIA_INCLUDES" "$RCLONE_LOG"
  grep -q '"outcome":"success"' "$CACHE_DIR/state/media.json"
  grep -q '"selected":true' "$CACHE_DIR/state/media.json"
}

@test "nothing selected (- **) -> skip, success, no copy" {
  printf -- '- **\n' >"$MEDIA_INCLUDES"
  run_media
  [ "$status" -eq 0 ]
  run grep -E '^(copy|sync) ' "$RCLONE_LOG"
  [ "$status" -ne 0 ]
  grep -q '"selected":false' "$CACHE_DIR/state/media.json"
}

@test "no include-list file -> skip, success" {
  run_media
  [ "$status" -eq 0 ]
  grep -q '"selected":false' "$CACHE_DIR/state/media.json"
}

@test "MEDIA_MIRROR=true uses sync" {
  export MEDIA_MIRROR=true
  printf '+ /**\n' >"$MEDIA_INCLUDES"
  run_media
  grep -q "sync $MEDIA_ROOT s3:my-bucket/media" "$RCLONE_LOG"
}
