load test_helper

# Task 6: the runner targets a per-job dedicated bucket (JOB_BUCKET) for
# RESTIC_REPOSITORY, falling back to the base S3_BUCKET. These tests exercise
# scripts/lib/config.sh's derive_restic_repo() directly (the unit the job
# scripts call twice: once at source time via load_config, and again AFTER
# JOB_* is eval'd from the per-job env, since JOB_BUCKET only exists then).

@test "RESTIC_REPOSITORY uses JOB_BUCKET when set" {
  S3_BUCKET=base JOB_BUCKET=be-1-photos AWS_REGION=us-east-1 \
    run bash -c 'source scripts/lib/config.sh; derive_restic_repo; echo "$RESTIC_REPOSITORY"'
  [[ "$output" == *"be-1-photos/appdata"* ]]
}

@test "RESTIC_REPOSITORY falls back to S3_BUCKET" {
  S3_BUCKET=base AWS_REGION=us-east-1 \
    run bash -c 'source scripts/lib/config.sh; derive_restic_repo; echo "$RESTIC_REPOSITORY"'
  [[ "$output" == *"base/appdata"* ]]
}

@test "derive_restic_repo re-derives after JOB_BUCKET is set post-source (ordering hazard)" {
  # Proves the fix for the real hazard: config.sh derives RESTIC_REPOSITORY at SOURCE
  # time, which is BEFORE the job scripts eval the per-job env (JOB_BUCKET). Source
  # config.sh first (as backup-job.sh/restore.sh do at their top), THEN set JOB_BUCKET
  # (as the later `eval "$def"` does), THEN call derive_restic_repo (as the job scripts
  # do right after that eval) -- the recomputed value must reflect the dedicated bucket.
  run bash -c '
    export S3_BUCKET=base AWS_REGION=us-east-1
    source scripts/lib/config.sh
    export JOB_BUCKET=be-2-docs
    derive_restic_repo
    echo "$RESTIC_REPOSITORY"
  '
  [ "$status" -eq 0 ]
  [[ "$output" == *"be-2-docs/appdata"* ]]
}

@test "derive_restic_repo honors an explicit RESTIC_REPOSITORY override even when JOB_BUCKET is set" {
  # An explicit external override (some tests/operators export RESTIC_REPOSITORY before the
  # job script runs) must still win over both S3_BUCKET and JOB_BUCKET. The job scripts
  # capture the pre-existing value into _RESTIC_REPO_OVERRIDE at the very top, before
  # config.sh's own derivation or the JOB_* eval can touch RESTIC_REPOSITORY.
  run bash -c '
    export S3_BUCKET=base JOB_BUCKET=be-3-vault AWS_REGION=us-east-1
    _RESTIC_REPO_OVERRIDE="s3:custom.example.com/manual-bucket/appdata"
    source scripts/lib/config.sh
    derive_restic_repo
    echo "$RESTIC_REPOSITORY"
  '
  [ "$status" -eq 0 ]
  [ "$output" = "s3:custom.example.com/manual-bucket/appdata" ]
}

# --- Fix round 1: scripts/lib/points.sh's archive restore-point listing was still
# hardcoded to $S3_BUCKET (missed in the original sweep, which only grepped
# backup-job.sh/restore.sh, not the libs they source). points_refresh (called by
# backup-job.sh) and points_render (called by restore.sh's archive `list --json`)
# both funnel through _points_render_archive's single rclone lsf call.

_points_bucket_setup() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR/state"
  export RCLONE_CONFIG="$CACHE_DIR/rclone.conf"; : >"$RCLONE_CONFIG"
  export S3_BUCKET=base-bucket
  local b="$BATS_TEST_TMPDIR/bin"; mkdir -p "$b"
  export RCLONE_LOG="$BATS_TEST_TMPDIR/rclone.log"; : >"$RCLONE_LOG"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$*" in *--dirs-only*) printf '%s\n' "2020/" ;; esac
exit 0
STUB
  chmod +x "$b/rclone"
  export PATH="$b:$PATH"
  source "$BATS_TEST_DIRNAME/../../scripts/lib/points.sh"
}

@test "points_refresh archive targets JOB_BUCKET (dedicated bucket), not the base S3_BUCKET" {
  _points_bucket_setup
  export JOB_BUCKET=be-1-photos
  points_refresh movies archive
  grep -q -- "lsf --dirs-only s3:be-1-photos/media/movies/" "$RCLONE_LOG"
  ! grep -q -- "s3:base-bucket" "$RCLONE_LOG"
  local f="$CACHE_DIR/state/movies.points.json"
  [ -f "$f" ]
  grep -q '"folders":\["2020"\]' "$f"
}

@test "points_render archive (restore.sh list --json path) targets JOB_BUCKET, not the base S3_BUCKET" {
  _points_bucket_setup
  export JOB_BUCKET=be-1-photos
  run points_render movies archive
  [ "$status" -eq 0 ]
  grep -q -- "lsf --dirs-only s3:be-1-photos/media/movies/" "$RCLONE_LOG"
  ! grep -q -- "s3:base-bucket" "$RCLONE_LOG"
}

@test "points_refresh archive falls back to S3_BUCKET when JOB_BUCKET is unset (base job, regression guard)" {
  _points_bucket_setup
  points_refresh movies archive
  grep -q -- "lsf --dirs-only s3:base-bucket/media/movies/" "$RCLONE_LOG"
}

@test "load_config (source-time, no JOB_BUCKET yet) still derives from S3_BUCKET" {
  # Sanity check that the pre-existing load_config path (config.bats already covers this in
  # depth) keeps working once its inline derivation was replaced by a call to
  # derive_restic_repo -- i.e. non-job contexts (no per-job env) are unaffected.
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  local cfg="$BATS_TEST_TMPDIR/config"; mkdir -p "$cfg"
  cat >"$cfg/backup.env" <<EOF
AWS_REGION=us-east-1
S3_BUCKET=my-bucket
EOF
  cat >"$cfg/secrets.env" <<EOF
AWS_ACCESS_KEY_ID=AKIA_TEST
AWS_SECRET_ACCESS_KEY=secret
RESTIC_PASSWORD=hunter2
EOF
  source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"
  source "$BATS_TEST_DIRNAME/../../scripts/lib/config.sh"
  load_config "$cfg"
  [ "$RESTIC_REPOSITORY" = "s3:s3.us-east-1.amazonaws.com/my-bucket/appdata" ]
}
