load test_helper

setup() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export CFG="$BATS_TEST_TMPDIR/config"; mkdir -p "$CFG"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/config.sh"
}

write_min_config() {
  cat >"$CFG/backup.env" <<EOF
AWS_REGION=us-east-1
S3_BUCKET=my-bucket
EOF
  cat >"$CFG/secrets.env" <<EOF
AWS_ACCESS_KEY_ID=AKIA_TEST
AWS_SECRET_ACCESS_KEY=secret
RESTIC_PASSWORD=hunter2
EOF
  : >"$CFG/includes-media.txt"
}

@test "load_config derives RESTIC_REPOSITORY from region+bucket" {
  write_min_config
  load_config "$CFG"
  [ "$RESTIC_REPOSITORY" = "s3:s3.us-east-1.amazonaws.com/my-bucket/appdata" ]
}

@test "load_config honors S3_ENDPOINT override in repo URL" {
  write_min_config
  echo "S3_ENDPOINT=minio.local:9000" >>"$CFG/backup.env"
  load_config "$CFG"
  [ "$RESTIC_REPOSITORY" = "s3:minio.local:9000/my-bucket/appdata" ]
}

@test "load_config applies media defaults" {
  write_min_config
  load_config "$CFG"
  [ "$MEDIA_STORAGE_CLASS" = "DEEP_ARCHIVE" ]
  [ "$MEDIA_MIRROR" = "false" ]
  [ "$APPDATA_STORAGE_CLASS" = "STANDARD" ]
}

@test "load_config dies when secrets.env missing" {
  echo "AWS_REGION=us-east-1" >"$CFG/backup.env"
  echo "S3_BUCKET=b" >>"$CFG/backup.env"
  run load_config "$CFG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"secrets.env"* ]]
}

@test "validate_appdata fails fast naming the plugin when source empty" {
  write_min_config
  load_config "$CFG"
  export APPDATA_SRC="$BATS_TEST_TMPDIR/does-not-exist"
  run validate_appdata
  [ "$status" -eq 1 ]
  [[ "$output" == *"Appdata Backup plugin"* ]]
}

@test "validate_common names all missing creds" {
  write_min_config
  load_config "$CFG"
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  run validate_common
  [ "$status" -eq 1 ]
  [[ "$output" == *"AWS_ACCESS_KEY_ID"* ]]
  [[ "$output" == *"AWS_SECRET_ACCESS_KEY"* ]]
}
