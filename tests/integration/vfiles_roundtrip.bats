load ../bats/test_helper
load minio_helper

# Exercises app.engine.vfiles (backup/restore) directly against a real MinIO --
# no stubs, no restic. Unlike archive_roundtrip.bats/restore_appdata.bats,
# vfiles' own CLI does NOT render rclone.conf itself (backup-job.sh's
# _run_vfiles and restore.sh's versioned-files branch both trust it's already
# at $CACHE_DIR/rclone.conf, rendered once by scripts/entrypoint.sh at
# container start) -- so this test renders it the same way
# tests/bats/rclone-conf.bats does: source scripts/lib/rclone-conf.sh and call
# render_rclone_conf directly. That keeps the [s3] remote definition identical
# to production instead of a hand-rolled config that might drift.
#
# Run from the repo root (as CI's `bats tests/integration/` does) so
# `python3 -m app.engine.vfiles` resolves the `app` package via cwd.
setup() {
  minio_up
  export S3_BUCKET="vfiles-rt-$$"; minio_make_bucket "$S3_BUCKET"
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"; mkdir -p "$CACHE_DIR"
  export SOURCE_ROOT="$BATS_TEST_TMPDIR/src"; mkdir -p "$SOURCE_ROOT/photos"
  echo "v1 payload" >"$SOURCE_ROOT/photos/pic.txt"

  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"
  # shellcheck source=/dev/null
  source "$BATS_TEST_DIRNAME/../../scripts/lib/rclone-conf.sh"
  render_rclone_conf "$CACHE_DIR/rclone.conf"

  JOB="pics"
}
teardown() { minio_down; }

# Every key vfiles uploads for this job's one file is media/pics/pic.txt@<ts>-<hex>;
# list recursively under the job prefix and keep only version-keys for that path
# (excludes the _catalog/catalog.sqlite entry).
_pic_keys() {
  rclone --config "$CACHE_DIR/rclone.conf" lsf -R "s3:$S3_BUCKET/media/$JOB" | grep '^pic\.txt@' || true
}

@test "backup, version, prune, restore round-trip against real MinIO" {
  # 1. Backup: first version lands under media/pics/pic.txt@... and the
  # catalog is uploaded to its durable key.
  JOB_SOURCE=photos JOB_STORAGE_CLASS=STANDARD JOB_RETENTION_DAYS=90 \
    run python3 -m app.engine.vfiles backup "$JOB"
  [ "$status" -eq 0 ]
  [[ "$output" == "uploaded=1 deleted=0 pruned=0" ]]

  run rclone --config "$CACHE_DIR/rclone.conf" lsf -R "s3:$S3_BUCKET/media/$JOB"
  [ "$status" -eq 0 ]
  [[ "$output" == *"_catalog/catalog.sqlite"* ]]
  run _pic_keys
  [ "${#lines[@]}" -eq 1 ]
  local v1_key="${lines[0]}"

  # 2. Version: modify the file (different size so diff() detects the change
  # even within the same wall-clock second) and back up again -> a SECOND
  # version-key exists, and the catalog shows 2 versions for the path.
  echo "v2 payload -- longer content than v1 so size differs" >"$SOURCE_ROOT/photos/pic.txt"
  JOB_SOURCE=photos JOB_STORAGE_CLASS=STANDARD JOB_RETENTION_DAYS=90 \
    run python3 -m app.engine.vfiles backup "$JOB"
  [ "$status" -eq 0 ]
  [[ "$output" == "uploaded=1 deleted=0 pruned=0" ]]

  run _pic_keys
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 2 ]

  run python3 -c "
import sqlite3
conn = sqlite3.connect('$CACHE_DIR/$JOB.sqlite')
print(conn.execute(\"SELECT COUNT(*) FROM versions WHERE path='pic.txt'\").fetchone()[0])
"
  [ "$status" -eq 0 ]
  [[ "$output" == "2" ]]

  # 3. Prune: back up again (no local change) with retention_days=0 -> the
  # OLD (non-current) version-key is deleted from MinIO; the current one
  # remains.
  JOB_SOURCE=photos JOB_STORAGE_CLASS=STANDARD JOB_RETENTION_DAYS=0 \
    run python3 -m app.engine.vfiles backup "$JOB"
  [ "$status" -eq 0 ]
  [[ "$output" == "uploaded=0 deleted=0 pruned=1" ]]

  run _pic_keys
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 1 ]
  [ "${lines[0]}" != "$v1_key" ]

  # 4. Restore: recovers the file with the LATEST (v2) content.
  local out="$BATS_TEST_TMPDIR/out"; mkdir -p "$out"
  JOB_STORAGE_CLASS=STANDARD \
    run python3 -m app.engine.vfiles restore "$JOB" pic.txt "$out"
  [ "$status" -eq 0 ]
  [[ "$output" == "restored: pic.txt"* ]]
  [ -f "$out/pic.txt" ]
  run cat "$out/pic.txt"
  [[ "$output" == "v2 payload -- longer content than v1 so size differs" ]]
}
