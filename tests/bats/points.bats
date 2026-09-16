load test_helper

# Tests for scripts/lib/points.sh — the restore-point cache writer (spec 7.5.4).
# points_refresh writes $CACHE_DIR/state/<job>.points.json atomically (temp+mv).
# It shells only to restic/rclone (never python) so the python-stubbing tests
# elsewhere stay valid. JSON is validated with a REAL python3 IN THE TEST ONLY.

setup() {
  setup_common
  mkdir -p "$CACHE_DIR/state"
  export S3_BUCKET=my-bucket AWS_REGION=us-east-1
  export RESTIC_REPOSITORY="s3:s3.us-east-1.amazonaws.com/my-bucket/appdata"
  export RCLONE_CONFIG="$CACHE_DIR/rclone.conf"; : >"$RCLONE_CONFIG"
  POINTS_SH="$BATS_TEST_DIRNAME/../../scripts/lib/points.sh"
  local b="$BATS_TEST_TMPDIR/bin"; mkdir -p "$b"
  cat >"$b/restic" <<'EOF'
#!/usr/bin/env bash
[ "${RESTIC_FAIL:-0}" = "1" ] && exit 3
for a in "$@"; do [ "$a" = "snapshots" ] && { printf '%s' "$RESTIC_SNAP_OUT"; exit 0; }; done
exit 0
EOF
  cat >"$b/rclone" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *"lsf --dirs-only"*) printf '%s' "$RCLONE_LSF_OUT" ;;
esac
exit 0
EOF
  chmod +x "$b/restic" "$b/rclone"; export PATH="$b:$PATH"
  export RESTIC_SNAP_OUT='[{"id":"a81f3c2e00000000","short_id":"a81f3c2e","time":"2026-09-15T05:00:01.123456789+00:00","tags":["appdata"]}]'
  export RCLONE_LSF_OUT=$'2019/\n2020/\n2021/\n'
}

_valid_json() { python3 -c 'import json,sys; json.load(sys.stdin)'; }

@test "points_refresh versioned writes the raw restic snapshots JSON to the cache" {
  source "$POINTS_SH"
  points_refresh appdata versioned
  local f="$CACHE_DIR/state/appdata.points.json"
  [ -f "$f" ]
  _valid_json <"$f"
  python3 -c 'import json,sys
d=json.load(sys.stdin)
assert isinstance(d,list) and d[0]["short_id"]=="a81f3c2e", d' <"$f"
}

@test "points_refresh archive writes current-copy with folders and as_of" {
  source "$POINTS_SH"
  points_refresh movies archive
  local f="$CACHE_DIR/state/movies.points.json"
  [ -f "$f" ]
  _valid_json <"$f"
  python3 -c 'import json,sys
d=json.load(sys.stdin)
assert d["kind"]=="current-copy", d
assert d["folders"]==["2019","2020","2021"], d["folders"]
assert d["as_of"], d' <"$f"
}

@test "points_refresh archive with no folders writes an empty list, still valid JSON" {
  source "$POINTS_SH"
  export RCLONE_LSF_OUT=""
  points_refresh movies archive
  local f="$CACHE_DIR/state/movies.points.json"
  [ -f "$f" ]
  python3 -c 'import json,sys
d=json.load(sys.stdin)
assert d["kind"]=="current-copy" and d["folders"]==[], d' <"$f"
}

@test "points_refresh versioned-files writes nothing and returns 0" {
  source "$POINTS_SH"
  run points_refresh photos versioned-files
  [ "$status" -eq 0 ]
  [ ! -e "$CACHE_DIR/state/photos.points.json" ]
}

@test "points_refresh leaves a pre-existing cache intact when the tool fails" {
  source "$POINTS_SH"
  local f="$CACHE_DIR/state/appdata.points.json"
  printf '[{"short_id":"OLD"}]' >"$f"
  export RESTIC_FAIL=1
  run points_refresh appdata versioned
  [ "$status" -ne 0 ]
  # old cache preserved, no leftover temp file
  grep -q OLD "$f"
  ! ls "$CACHE_DIR/state/"*.tmp.* >/dev/null 2>&1
}

@test "points_render archive prints the current-copy JSON to stdout without writing a file" {
  source "$POINTS_SH"
  run points_render movies archive
  [ "$status" -eq 0 ]
  printf '%s' "$output" | python3 -c 'import json,sys
d=json.load(sys.stdin); assert d["kind"]=="current-copy" and d["folders"]==["2019","2020","2021"], d'
  [ ! -e "$CACHE_DIR/state/movies.points.json" ]
}
