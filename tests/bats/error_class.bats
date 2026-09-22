load test_helper
setup() { source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"; LOG="$BATS_TEST_TMPDIR/l"; }
@test "5xx / timeout / slowdown / reset are transient" {
  for m in "RequestTimeout: your socket connection" "http status 503 SlowDown" \
           "connection reset by peer" "net/http: TLS handshake timeout" "unexpected EOF" \
           "connection timeouts occurred repeatedly" "http status 503"; do
    printf '%s\n' "$m" >"$LOG"; run _is_transient_error "$LOG"; [ "$status" -eq 0 ]
  done
}
@test "AccessDenied / NoSuchBucket / generic are permanent" {
  for m in "AccessDenied: not authorized" "NoSuchBucket" "SignatureDoesNotMatch" "some random failure" \
           "processed 14500 files successfully" "uploaded object id 5001 done" "size=505000 bytes copied"; do
    printf '%s\n' "$m" >"$LOG"; run _is_transient_error "$LOG"; [ "$status" -ne 0 ]
  done
}
