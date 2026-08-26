load test_helper

setup() { setup_common; }

@test "log writes level-tagged line to stdout" {
  run log INFO "hello world"
  [ "$status" -eq 0 ]
  [[ "$output" == *"[INFO]"* ]]
  [[ "$output" == *"hello world"* ]]
}

@test "log appends to LOG_FILE" {
  log INFO "persisted line"
  grep -q "persisted line" "$LOG_FILE"
}

@test "DEBUG suppressed unless LOG_LEVEL=DEBUG" {
  run log DEBUG "noisy"
  [ -z "$output" ]
  LOG_LEVEL=DEBUG run log DEBUG "noisy"
  [[ "$output" == *"noisy"* ]]
}

@test "die logs ERROR and exits 1" {
  run die "boom"
  [ "$status" -eq 1 ]
  [[ "$output" == *"[ERROR]"* ]]
  [[ "$output" == *"boom"* ]]
}

@test "require_env names every missing var" {
  unset FOO BAR
  run require_env FOO BAR
  [ "$status" -eq 1 ]
  [[ "$output" == *"FOO"* ]]
  [[ "$output" == *"BAR"* ]]
}

@test "acquire_lock blocks a second holder" {
  acquire_lock demo
  run bash -c "CACHE_DIR='$CACHE_DIR' source '$BATS_TEST_DIRNAME/../../scripts/lib/common.sh'; acquire_lock demo"
  [ "$status" -eq 1 ]
  [[ "$output" == *"in progress"* ]]
}

@test "notify is a no-op with empty APPRISE_URLS" {
  export APPRISE_URLS=""
  run notify failure "t" "b"
  [ "$status" -eq 0 ]
}

@test "version_banner logs unknown for missing tools without leaking errors" {
  # Build a minimal PATH containing only what log()/version_banner need
  # (date, head) but none of restic/rclone/supercronic, so we can assert
  # the "missing binary" fallback without any real tool interfering.
  local stub_bin="$BATS_TEST_TMPDIR/stubbin"
  mkdir -p "$stub_bin"
  ln -s "$(command -v date)" "$stub_bin/date"
  ln -s "$(command -v head)" "$stub_bin/head"
  local bash_bin; bash_bin="$(command -v bash)"

  run env -u LOG_FILE PATH="$stub_bin" "$bash_bin" -c \
    "source '$BATS_TEST_DIRNAME/../../scripts/lib/common.sh'; version_banner"

  [ "$status" -eq 0 ]
  [[ "$output" == *"restic:"* ]]
  [[ "$output" == *"rclone:"* ]]
  [[ "$output" == *"supercronic:"* ]]
  [[ "$output" != *"command not found"* ]]
  [[ "$output" != *"common.sh"* ]]
  local count
  count=$(grep -o "unknown" <<<"$output" | wc -l)
  [ "$count" -eq 3 ]
}
