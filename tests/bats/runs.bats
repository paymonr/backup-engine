load test_helper

# Tests for scripts/lib/runs.sh — the append-only per-run record writer (spec 7.1.1-7.1.4).
# JSON is validated with a REAL python3 json.loads IN THE TEST ONLY; the runner never shells to
# python for bookkeeping (tests/bats/backup-job.bats:74-84 constraint).

setup() {
  export CACHE_DIR="$BATS_TEST_TMPDIR/cache"
  mkdir -p "$CACHE_DIR/state"
  RUNS_SH="$BATS_TEST_DIRNAME/../../scripts/lib/runs.sh"
  FIX="$BATS_TEST_DIRNAME/fixtures"
}

# python3 -c that parses every non-blank stdin line as JSON; fails the test if any line is bad.
_parse_all() { python3 -c 'import json,sys
n=0
for l in sys.stdin:
    if l.strip():
        json.loads(l); n+=1
assert n>0, "no lines"'; }

@test "runs_start + ok runs_end: two JSON lines, snapshot_id a81f3c2e round-trips (versioned run)" {
  source "$RUNS_SH"
  export BE_TRIGGER=scheduled
  runs_start appdata backup '"type":"versioned","storage_class":"STANDARD"'
  SNAP_ID=a81f3c2e
  RUN_STATS='"files_new":2,"files_changed":4,"files_added":6,"bytes_added":228589568,"files_total":533,"bytes_total":56594862080'
  # exactly the form main() uses at insertion point (5)
  runs_end ok 0 "" "\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")${RUN_STATS:+,$RUN_STATS}"
  local f; f="$(runs_file appdata)"
  [ -f "$f" ]
  [ "$(wc -l <"$f")" -eq 2 ]
  _parse_all <"$f"
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
start=[r for r in recs if r["event"]=="start"][0]
end=[r for r in recs if r["event"]=="end"][0]
assert start["v"]==1 and start["job"]=="appdata" and start["kind"]=="backup"
assert start["trigger"]=="scheduled" and start["type"]=="versioned" and start["storage_class"]=="STANDARD"
assert start["id"]==end["id"], (start["id"], end["id"])
assert end["outcome"]=="ok" and end["exit_code"]==0 and end["error"] is None
assert end["snapshot_id"]=="a81f3c2e", end["snapshot_id"]
assert end["files_new"]==2 and end["files_changed"]==4 and end["files_added"]==6
assert end["bytes_added"]==228589568 and end["files_total"]==533 and end["bytes_total"]==56594862080' <"$f"
}

@test "runs_end with empty SNAP_ID renders snapshot_id null (not a broken double-print)" {
  source "$RUNS_SH"
  runs_start appdata backup
  SNAP_ID=""
  runs_end ok 0 "" "\"snapshot_id\":$(_runs_str "${SNAP_ID:-}")"
  local f; f="$(runs_file appdata)"
  _parse_all <"$f"
  python3 -c 'import json,sys
end=[json.loads(l) for l in sys.stdin if l.strip() and "\"event\":\"end\"" in l][0]
assert end["snapshot_id"] is None, end["snapshot_id"]' <"$f"
}

@test "_runs_str: empty -> null, value -> quoted, quotes escaped" {
  source "$RUNS_SH"
  [ "$(_runs_str "")" = "null" ]
  [ "$(_runs_str "a81f3c2e")" = '"a81f3c2e"' ]
  [ "$(_runs_str 'a"b')" = '"a\"b"' ]
}

@test "_runs_num: numeric passes through, non-numeric/empty -> null" {
  source "$RUNS_SH"
  [ "$(_runs_num 42)" = "42" ]
  [ "$(_runs_num "")" = "null" ]
  [ "$(_runs_num "x9")" = "null" ]
}

@test "runs_end escapes quotes/newlines/tabs in the error and stays valid JSON" {
  source "$RUNS_SH"
  runs_start appdata backup
  local msg=$'he said "hi"\nand\ttabbed'
  runs_end failed 1 "$msg"
  local f; f="$(runs_file appdata)"
  _parse_all <"$f"
  python3 -c 'import json,sys
end=[json.loads(l) for l in sys.stdin if l.strip() and "\"event\":\"end\"" in l][0]
assert end["outcome"]=="failed" and end["exit_code"]==1
assert end["error"]=="he said \"hi\"\nand\ttabbed", repr(end["error"])' <"$f"
}

@test "_runs_rotate keeps the newest RUNS_KEEP_LINES once the file passes RUNS_ROTATE_AT" {
  export RUNS_KEEP_LINES=4 RUNS_ROTATE_AT=6
  source "$RUNS_SH"
  local f; f="$(runs_file appdata)"
  local i; for i in $(seq 1 8); do printf 'line %d\n' "$i" >>"$f"; done
  _runs_rotate "$f" appdata
  [ "$(wc -l <"$f")" -eq 4 ]
  head -n1 "$f" | grep -q "line 5"
  tail -n1 "$f" | grep -q "line 8"
}

@test "runs_end with BE_RUN_ID set but no runs_start appends nothing and returns 0 under set -u" {
  run bash -c '
    set -u
    export CACHE_DIR="'"$CACHE_DIR"'"
    export BE_RUN_ID=20260915T050001Z-3f9a
    source "'"$RUNS_SH"'"
    runs_end ok 0
    echo "runs_end_rc=$?"
  '
  [ "$status" -eq 0 ]
  [[ "$output" == *"runs_end_rc=0"* ]]
  [ ! -e "$CACHE_DIR/state/appdata.runs.jsonl" ]
}

@test "runs_new_id matches the run-id regex" {
  source "$RUNS_SH"
  local id; id="$(runs_new_id)"
  [[ "$id" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$ ]]
}

@test "runs_start honours a well-formed pre-assigned BE_RUN_ID, replaces a malformed one" {
  source "$RUNS_SH"
  export BE_RUN_ID=20260915T050001Z-3f9a
  runs_start appdata backup
  [ "$BE_RUN_ID" = "20260915T050001Z-3f9a" ]
  # malformed -> regenerated
  local badenv
  badenv="$(BE_RUN_ID=not-a-valid-id bash -c 'source "'"$RUNS_SH"'"; CACHE_DIR="'"$CACHE_DIR"'" runs_start appdata backup >/dev/null; printf %s "$BE_RUN_ID"')"
  [[ "$badenv" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$ ]]
}

@test "_rclone_stat_bytes and _rclone_stat_files never return the same number" {
  source "$RUNS_SH"
  local fx="$FIX/rclone-final-stats.txt"
  [ "$(_rclone_stat_bytes "$fx")" -eq 3328599654 ]
  [ "$(_rclone_stat_files "$fx")" -eq 1204 ]
  [ "$(_rclone_stat_errors "$fx")" -eq 0 ]
}

@test "restic summary parsers read the summary line, snapshot id truncated to 8 hex" {
  source "$RUNS_SH"
  local fx="$FIX/restic-backup-summary.jsonl"
  [ "$(_restic_snapshot_id "$fx")" = "a81f3c2e" ]
  [ "$(_restic_summary_field "$fx" files_new)" -eq 2 ]
  [ "$(_restic_summary_field "$fx" files_changed)" -eq 4 ]
  [ "$(_restic_summary_field "$fx" data_added)" -eq 228589568 ]
  [ "$(_restic_summary_field "$fx" total_files_processed)" -eq 533 ]
  [ "$(_restic_summary_field "$fx" total_bytes_processed)" -eq 56594862080 ]
}
