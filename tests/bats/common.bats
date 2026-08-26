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
