load test_helper
setup() {
  setup_common
  export AWS_REGION=us-east-1 S3_BUCKET=my-bucket
  export AWS_ACCESS_KEY_ID=AKIA AWS_SECRET_ACCESS_KEY=secret RESTIC_PASSWORD=pw
  export RESTIC_REPOSITORY="s3:s3.us-east-1.amazonaws.com/my-bucket/appdata"
  export RCLONE_LOG="$BATS_TEST_TMPDIR/rclone.log" RESTIC_LOG="$BATS_TEST_TMPDIR/restic.log"
  : >"$RCLONE_LOG"; : >"$RESTIC_LOG"
  local b="$BATS_TEST_TMPDIR/bin"; mkdir -p "$b"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$RCLONE_LOG"\nexit 0\n' >"$b/rclone"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$RESTIC_LOG"\nexit 0\n' >"$b/restic"
  chmod +x "$b/rclone" "$b/restic"; export PATH="$b:$PATH"
  export JOBS_IO_STUB="$BATS_TEST_TMPDIR/jobsio.sh"
  export JOBS_IO_CMD="bash $JOBS_IO_STUB"
  export APPRISE_URLS=""                       # notify()/_fail must no-op without a target
  export REAL_PY; REAL_PY="$(command -v python3)"   # real python for record assertions (tests may stub python3)
}
run_restore() { local job="$1"; shift; run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh" "$job" "$@"; }

@test "versioned job -> restic snapshots --tag <job> for list" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run_restore cfg list
  [ "$status" -eq 0 ]
  grep -q -- "snapshots --tag cfg" "$RESTIC_LOG"
}

@test "versioned job -> restic restore <snap> --target <target> --tag <job>" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  [ "$status" -eq 0 ]
  grep -q -- "restore latest --target $out --tag cfg" "$RESTIC_LOG"
  [ -d "$out" ]
}

@test "versioned job with cold storage class warns before restoring" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  [ "$status" -eq 0 ]
  [[ "$output" == *"is cold"* ]]
}

@test "archive job -> rclone thaw lists media/<job>/<prefix>" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore movies thaw 2020/
  [ "$status" -eq 0 ]
  grep -q -- "lsf -R --files-only s3:my-bucket/media/movies/2020/" "$RCLONE_LOG"
}

@test "archive job -> rclone copy from media/<job>/<prefix> for download" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore movies download 2020/ "$out"
  [ "$status" -eq 0 ]
  grep -q -- "copy s3:my-bucket/media/movies/2020/ $out -v" "$RCLONE_LOG"
}

@test "versioned-files job -> dispatches to app.engine.vfiles module" {
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_restore vf list
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles restore vf list" "$PYTHON_LOG"
  [ ! -s "$RCLONE_LOG" ]
  [ ! -s "$RESTIC_LOG" ]
}

@test "versioned-files job -> restore with path/target/--asof/--tier passed through" {
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore vf a/b.txt "$out" --asof 1700000000 --tier Expedited
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles restore vf a/b.txt $out --asof 1700000000 --tier Expedited" "$PYTHON_LOG"
}

@test "unknown job -> clear error" {
  printf 'exit 3\n' >"$JOBS_IO_STUB"
  run_restore ghost list
  [ "$status" -ne 0 ]
  [[ "$output" == *"ghost"* ]]
}

@test "no job given -> usage" {
  run bash "$BATS_TEST_DIRNAME/../../scripts/restore.sh"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage:"* ]]
}

@test "versioned-files restore EXPORTS the job def to the python engine (regression)" {
  # restore.sh eval's the def then hands off to python3 (exec); the def must be
  # exported so the engine's os.environ reads (JOB_STORAGE_CLASS for cold-thaw, etc.)
  # succeed. Stub inspects its ENVIRONMENT, not argv, and fails if the def is absent.
  export PYENV_LOG="$BATS_TEST_TMPDIR/pyenv.log"; : >"$PYENV_LOG"
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/python3" <<'STUB'
#!/usr/bin/env bash
printf 'JOB_STORAGE_CLASS=%s\n' "${JOB_STORAGE_CLASS-UNSET}" >>"$PYENV_LOG"
[ -n "${JOB_STORAGE_CLASS:-}" ] || exit 7
exit 0
STUB
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_restore vf list
  [ "$status" -eq 0 ]
  grep -q "JOB_STORAGE_CLASS=DEEP_ARCHIVE" "$PYENV_LOG"
}

# --- Task 6: run records, lock, list --json, thaw, thaw-status, download, test ---

@test "versioned restore -> writes a run record (kind restore) with target" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  [ "$status" -eq 0 ]
  grep -q -- "restore latest --target $out --tag cfg" "$RESTIC_LOG"
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"]; en=[r for r in recs if r["event"]=="end"]
assert len(st)==1 and len(en)==1, (len(st),len(en))
assert st[0]["kind"]=="restore", st[0]["kind"]
assert en[0]["outcome"]=="ok", en[0]["outcome"]
assert "target" in en[0], en[0]' <"$CACHE_DIR/state/cfg.runs.jsonl"
}

@test "archive download . -> record (kind download), copy argv, files/bytes parsed" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
cat <<'OUT'
Transferred:   	    2.000 GiB / 2.000 GiB, 100%, 8.912 MiB/s, ETA 0s
Errors:                 0
Transferred:          10 / 10, 100%
OUT
exit 0
STUB
  chmod +x "$b/rclone"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore movies download . "$out"
  [ "$status" -eq 0 ]
  grep -q -- "copy s3:my-bucket/media/movies/ $out -v" "$RCLONE_LOG"
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"][0]; e=[r for r in recs if r["event"]=="end"][0]
assert st["kind"]=="download", st["kind"]
assert e["outcome"]=="ok", e["outcome"]
assert e["bytes_restored"]==2147483648, e.get("bytes_restored")
assert e["files_restored"]==10, e.get("files_restored")' <"$CACHE_DIR/state/movies.runs.jsonl"
}

@test "archive download rclone exits 1 after a stats block -> failed record keeps bytes_restored" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
cat <<'OUT'
Transferred:   	    1.000 GiB / 1.000 GiB, 100%, 8.912 MiB/s, ETA 0s
Errors:                 2
Transferred:          5 / 7, 71%
OUT
exit 1
STUB
  chmod +x "$b/rclone"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore movies download 2020/ "$out"
  [ "$status" -ne 0 ]
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
e=[r for r in recs if r["event"]=="end"][0]
assert e["outcome"]=="failed", e["outcome"]
assert e["bytes_restored"]==1073741824, e.get("bytes_restored")
assert e["rclone_errors"]==2, e.get("rclone_errors")' <"$CACHE_DIR/state/movies.runs.jsonl"
}

@test "archive thaw -> one restore-object per key, thaw.json, record kind thaw" {
  local b="$BATS_TEST_TMPDIR/bin"
  export AWS_LOG="$BATS_TEST_TMPDIR/aws.log"; : >"$AWS_LOG"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$*" in *lsf*) printf '%s\n' "a.mp4" "b.mp4" ;; esac
exit 0
STUB
  cat >"$b/aws" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$AWS_LOG"
exit 0
STUB
  chmod +x "$b/rclone" "$b/aws"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore movies thaw 2020/ --tier Standard
  [ "$status" -eq 0 ]
  [ "$(grep -c 'restore-object' "$AWS_LOG")" -eq 2 ]
  grep -q -- '--key media/movies/2020/a.mp4' "$AWS_LOG"
  grep -q -- 'GlacierJobParameters={Tier=Standard}' "$AWS_LOG"
  [ -f "$CACHE_DIR/state/movies.thaw.json" ]
  grep -q '"objects_requested":2' "$CACHE_DIR/state/movies.thaw.json"
  grep -q '"scope":"2020/"' "$CACHE_DIR/state/movies.thaw.json"   # folder scope persisted (not always ".")
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"][0]; e=[r for r in recs if r["event"]=="end"][0]
assert st["kind"]=="thaw", st["kind"]
assert e["outcome"]=="ok", e["outcome"]
assert e["objects_requested"]==2, e.get("objects_requested")' <"$CACHE_DIR/state/movies.runs.jsonl"
}

@test "archive thaw whole-scope (.) -> thaw.json scope is '.'" {
  local b="$BATS_TEST_TMPDIR/bin"
  export AWS_LOG="$BATS_TEST_TMPDIR/aws.log"; : >"$AWS_LOG"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$*" in *lsf*) printf '%s\n' "a.mp4" ;; esac
exit 0
STUB
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$AWS_LOG"\nexit 0\n' >"$b/aws"
  chmod +x "$b/rclone" "$b/aws"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore movies thaw . --tier Standard
  [ "$status" -eq 0 ]
  grep -q '"scope":"."' "$CACHE_DIR/state/movies.thaw.json"
}

@test "archive thaw on a warm tier -> exit 2 with a FAILED end record (no dangling run)" {
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run_restore movies thaw 2020/ --tier Bulk
  [ "$status" -eq 2 ]
  [[ "$output" == *"thaw-first tier"* ]]
  # the start line must be closed by a failed end (Task 4 must not show it "running" forever)
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"]; en=[r for r in recs if r["event"]=="end"]
assert len(st)==1 and len(en)==1, (len(st),len(en))
e=en[0]
assert e["outcome"]=="failed", e["outcome"]
assert e["exit_code"]==2, e.get("exit_code")
assert "thaw-first tier" in (e["error"] or ""), e.get("error")' <"$CACHE_DIR/state/movies.runs.jsonl"
}

@test "versioned thaw on a warm tier -> exit 2 with a FAILED end record (no dangling run)" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run_restore cfg thaw .
  [ "$status" -eq 2 ]
  [[ "$output" == *"thaw-first tier"* ]]
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"]; en=[r for r in recs if r["event"]=="end"]
assert len(st)==1 and len(en)==1, (len(st),len(en))
assert en[0]["outcome"]=="failed" and en[0]["exit_code"]==2, en[0]' <"$CACHE_DIR/state/cfg.runs.jsonl"
}

@test "versioned thaw-status is unsupported -> usage/exit 2 (spec limits it to archive/versioned-files)" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore cfg thaw-status .
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage:"* ]]
  [ ! -e "$CACHE_DIR/state/cfg.runs.jsonl" ]   # read-only path: no record either
}

@test "versioned-files . restore -> in-process record (kind restore), dispatches vfiles restore ." {
  local b="$BATS_TEST_TMPDIR/bin"
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nprintf "restored=3 thaw_requested=0 skipped=0 bytes=99\\n"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  local out="$BATS_TEST_TMPDIR/out"
  run_restore vf . "$out" --asof 1700000000 --tier Bulk
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles restore vf . $out --asof 1700000000 --tier Bulk" "$PYTHON_LOG"
  "$REAL_PY" -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"][0]; e=[r for r in recs if r["event"]=="end"][0]
assert st["kind"]=="restore", st["kind"]
assert e["outcome"]=="ok", e["outcome"]
assert e["files_restored"]==3, e.get("files_restored")
assert e["bytes_restored"]==99, e.get("bytes_restored")' <"$CACHE_DIR/state/vf.runs.jsonl"
}

@test "versioned-files thaw . -> dispatches vfiles thaw, thaw.json, record kind thaw" {
  local b="$BATS_TEST_TMPDIR/bin"
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PYTHON_LOG"\nprintf "thaw_requested=4 skipped=1\\n"\nexit 0\n' >"$b/python3"
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_restore vf thaw . --tier Standard
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles thaw vf . --tier Standard" "$PYTHON_LOG"
  [ -f "$CACHE_DIR/state/vf.thaw.json" ]
  "$REAL_PY" -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"][0]; e=[r for r in recs if r["event"]=="end"][0]
assert st["kind"]=="thaw", st["kind"]
assert e["objects_requested"]==4, e.get("objects_requested")' <"$CACHE_DIR/state/vf.runs.jsonl"
}

@test "poll-probe lock: restore waits out a ~1s holder and still runs (never skipped)" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  mkdir -p "$CACHE_DIR/locks"; local lock="$CACHE_DIR/locks/cfg.lock"; : >"$lock"
  flock -x "$lock" -c 'sleep 1' &
  local holder=$!
  sleep 0.2
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  wait "$holder" 2>/dev/null || true
  [ "$status" -eq 0 ]
  grep -q '"kind":"restore"' "$CACHE_DIR/state/cfg.runs.jsonl"
  grep -q '"outcome":"ok"' "$CACHE_DIR/state/cfg.runs.jsonl"
}

@test "genuine lock holder -> restore aborts (another run in progress), no run record" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  mkdir -p "$CACHE_DIR/locks"; local lock="$CACHE_DIR/locks/cfg.lock"; : >"$lock"
  flock -x "$lock" -c 'sleep 30' &
  local holder=$!
  sleep 0.3
  local out="$BATS_TEST_TMPDIR/out"
  run_restore cfg restore latest "$out"
  kill "$holder" 2>/dev/null || true
  [ "$status" -ne 0 ]
  [[ "$output" == *"in progress"* ]]
  [ ! -e "$CACHE_DIR/state/cfg.runs.jsonl" ]
}

@test "versioned list --json -> snapshots --json, no lock, no record" {
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run_restore cfg list --json
  [ "$status" -eq 0 ]
  grep -q -- "snapshots --tag cfg --json" "$RESTIC_LOG"
  [ ! -e "$CACHE_DIR/state/cfg.runs.jsonl" ]
}

@test "archive list --json -> current-copy folders json, no record" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$*" in *--dirs-only*) printf '%s\n' "2019/" "2020/" ;; esac
exit 0
STUB
  chmod +x "$b/rclone"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore movies list --json
  [ "$status" -eq 0 ]
  [[ "$output" == *'"kind":"current-copy"'* ]]
  [[ "$output" == *'"2019"'* ]]
  [ ! -e "$CACHE_DIR/state/movies.runs.jsonl" ]
}

@test "archive thaw-status -> samples keys and counts ready, no record" {
  local b="$BATS_TEST_TMPDIR/bin"
  export AWS_LOG="$BATS_TEST_TMPDIR/aws.log"; : >"$AWS_LOG"
  cat >"$b/rclone" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RCLONE_LOG"
case "$*" in *lsf*) printf '%s\n' "a.mp4" "b.mp4" ;; esac
exit 0
STUB
  cat >"$b/aws" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$AWS_LOG"
printf 'ongoing-request="false", expiry-date="Wed, 01 Jan 2031 00:00:00 GMT"\n'
exit 0
STUB
  chmod +x "$b/rclone" "$b/aws"
  printf 'echo JOB_NAME=movies; echo JOB_TYPE=archive; echo JOB_STORAGE_CLASS=DEEP_ARCHIVE\n' >"$JOBS_IO_STUB"
  run_restore movies thaw-status 2020/
  [ "$status" -eq 0 ]
  [[ "$output" == *'"sampled":2'* ]]
  [[ "$output" == *'"ready":2'* ]]
  [ ! -e "$CACHE_DIR/state/movies.runs.jsonl" ]
}

@test "versioned test -> restores one file, tested.json, record kind test-restore" {
  local b="$BATS_TEST_TMPDIR/bin"
  cat >"$b/restic" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$RESTIC_LOG"
sub=""; for a in "$@"; do case "$a" in ls|restore|snapshots) sub="$a"; break ;; esac; done
case "$sub" in
  ls) printf '%s\n' '{"type":"dir","path":"/appdata"}' '{"type":"file","path":"/appdata/x.conf","size":6}' ;;
  restore)
    tgt=""; prev=""; for a in "$@"; do [ "$prev" = "--target" ] && tgt="$a"; prev="$a"; done
    mkdir -p "$tgt/appdata"; printf 'hello\n' >"$tgt/appdata/x.conf" ;;
esac
exit 0
STUB
  chmod +x "$b/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_STORAGE_CLASS=STANDARD\n' >"$JOBS_IO_STUB"
  run_restore cfg test
  [ "$status" -eq 0 ]
  grep -q -- "ls --json latest --tag cfg" "$RESTIC_LOG"
  grep -q -- "restore latest --tag cfg --include /appdata/x.conf" "$RESTIC_LOG"
  [ -f "$CACHE_DIR/state/cfg.tested.json" ]
  grep -q '"path":"/appdata/x.conf"' "$CACHE_DIR/state/cfg.tested.json"
  python3 -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"][0]; e=[r for r in recs if r["event"]=="end"][0]
assert st["kind"]=="test-restore", st["kind"]
assert e["outcome"]=="ok", e["outcome"]
assert e["tested_path"]=="/appdata/x.conf", e.get("tested_path")
assert e["tested_bytes"]==6, e.get("tested_bytes")' <"$CACHE_DIR/state/cfg.runs.jsonl"
}

@test "versioned-files test -> restores first catalog file, tested.json, record kind test-restore" {
  local b="$BATS_TEST_TMPDIR/bin"
  export PYTHON_LOG="$BATS_TEST_TMPDIR/python.log"; : >"$PYTHON_LOG"
  cat >"$b/python3" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$PYTHON_LOG"
mode=""; for a in "$@"; do case "$a" in list) mode=list; break ;; esac; done
if [ "$mode" = list ]; then printf 'docs/a.txt\t123.0\tSTANDARD\n'; exit 0; fi
tgt="${!#}"; mkdir -p "$tgt/docs"; printf 'data\n' >"$tgt/docs/a.txt"
exit 0
STUB
  chmod +x "$b/python3"
  printf 'echo JOB_NAME=vf; echo JOB_TYPE=versioned-files; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_DAYS=30\n' >"$JOBS_IO_STUB"
  run_restore vf test
  [ "$status" -eq 0 ]
  grep -q -- "-m app.engine.vfiles restore vf list" "$PYTHON_LOG"
  grep -q -- "-m app.engine.vfiles restore vf docs/a.txt" "$PYTHON_LOG"
  [ -f "$CACHE_DIR/state/vf.tested.json" ]
  "$REAL_PY" -c 'import json,sys
recs=[json.loads(l) for l in sys.stdin if l.strip()]
st=[r for r in recs if r["event"]=="start"][0]; e=[r for r in recs if r["event"]=="end"][0]
assert st["kind"]=="test-restore", st["kind"]
assert e["outcome"]=="ok", e["outcome"]
assert e["tested_path"]=="docs/a.txt", e.get("tested_path")' <"$CACHE_DIR/state/vf.runs.jsonl"
}
