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
  # Full, valid job defs (the schema the GUI always writes). The crontab-render
  # path (jobs_io --list) now re-validates confinement/name/schedule/type/class on
  # this untrusted-at-read-time file, so minimal name/schedule stubs no longer
  # qualify — a malformed job is dropped from the schedule rather than emitted.
  cat >"$CFG/jobs.json" <<'EOF'
{
  "jobs": [
    {"name": "movies", "type": "archive", "source": "movies", "schedule": "0 4 * * 0", "enabled": true, "storage_class": "STANDARD", "mirror": false},
    {"name": "appdata", "type": "versioned", "source": "appdata", "schedule": "0 3 * * *", "enabled": true, "storage_class": "STANDARD", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}},
    {"name": "scratch", "type": "archive", "source": "scratch", "schedule": "0 2 * * *", "enabled": false, "storage_class": "STANDARD", "mirror": false}
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

# Task 10 security: schedule-time confinement — a hand-edited jobs.json whose
# source escapes the mount is dropped from the crontab, never scheduled.
@test "entrypoint --emit-crontab drops a job whose source escapes the mount" {
  cat >"$CFG/jobs.json" <<'EOF'
{
  "jobs": [
    {"name": "good", "type": "archive", "source": "movies", "schedule": "0 4 * * 0", "enabled": true, "storage_class": "STANDARD", "mirror": false},
    {"name": "evil", "type": "archive", "source": "../../etc", "schedule": "0 5 * * *", "enabled": true, "storage_class": "STANDARD", "mirror": false}
  ]
}
EOF
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  grep -q "backup-job.sh good" "$CACHE_DIR/crontab"
  ! grep -q "evil" "$CACHE_DIR/crontab"
  [ "$(wc -l <"$CACHE_DIR/crontab")" -eq 1 ]
}

# final-fix R-final-1: a corrupt jobs.json must NOT brick boot. load() exits 0 (empty
# schedule) and emits a diagnostic; with `2>/dev/null` gone from emit_crontab the
# diagnostic is visible in the container log/stderr, and pipefail no longer aborts PID 1.
@test "entrypoint --emit-crontab tolerates a corrupt jobs.json (exit 0, empty crontab, diagnostic visible)" {
  printf '%s' '{ this is not valid json' >"$CFG/jobs.json"
  run bash "$BATS_TEST_DIRNAME/../../scripts/entrypoint.sh" --emit-crontab
  [ "$status" -eq 0 ]
  [ -f "$CACHE_DIR/crontab" ]
  [ ! -s "$CACHE_DIR/crontab" ]
  [[ "$output" == *"jobs.json"* ]]
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
