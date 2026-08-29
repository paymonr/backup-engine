load test_helper

setup() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export CFG="$BATS_TEST_TMPDIR/config"; mkdir -p "$CFG"
  cat >"$CFG/backup.env" <<EOF
AWS_REGION=us-east-1
S3_BUCKET=b
EOF
  cat >"$CFG/secrets.env" <<EOF
AWS_ACCESS_KEY_ID=k
AWS_SECRET_ACCESS_KEY=s
RESTIC_PASSWORD=p
EOF
  export CONFIG_DIR="$CFG"
}

write_jobs_json() {
  cat >"$CFG/jobs.json" <<'EOF'
{
  "jobs": [
    {"name": "movies", "schedule": "0 4 * * 0", "enabled": true},
    {"name": "appdata", "schedule": "0 3 * * *", "enabled": true},
    {"name": "scratch", "schedule": "0 2 * * *", "enabled": false}
  ]
}
EOF
}

@test "entrypoint --emit-crontab writes one line per enabled job" {
  write_jobs_json
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  grep -q "^0 4 \* \* 0 .*backup-job.sh movies$" "$CACHE_DIR/crontab"
  grep -q "^0 3 \* \* \* .*backup-job.sh appdata$" "$CACHE_DIR/crontab"
  ! grep -q "scratch" "$CACHE_DIR/crontab"
  [ "$(wc -l <"$CACHE_DIR/crontab")" -eq 2 ]
}

@test "entrypoint --emit-crontab with no jobs.json writes an empty crontab" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  [ -f "$CACHE_DIR/crontab" ]
  [ ! -s "$CACHE_DIR/crontab" ]
}

@test "entrypoint renders rclone.conf and password file" {
  write_jobs_json
  bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ -f "$CACHE_DIR/rclone.conf" ]
  [ -f "$CACHE_DIR/restic-password" ]
  [ "$(stat -c '%a' "$CACHE_DIR/rclone.conf")" = "600" ]
}

@test "entrypoint with GUI_ENABLED=false emits crontab and does not require the GUI" {
  write_jobs_json
  echo "GUI_ENABLED=false" >>"$CFG/backup.env"
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  grep -q "backup-job.sh movies" "$CACHE_DIR/crontab"
}
