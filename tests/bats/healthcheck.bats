load test_helper

# Starts a tiny disposable HTTP server (stdlib http.server, no deps beyond
# python3 which is already required by tests/integration/minio_helper.bash)
# that appends every requested path to HC_LOG and answers 200. Lets us assert
# healthcheck() actually issues the request it claims to, not just that it
# doesn't error.
setup() {
  setup_common
  HC_LOG="$BATS_TEST_TMPDIR/requests.log"
  : >"$HC_LOG"
  HC_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
  cat >"$BATS_TEST_TMPDIR/hc_server.py" <<'PYEOF'
import http.server
import sys

log_file = sys.argv[2]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(log_file, "a") as f:
            f.write(self.path + "\n")
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PYEOF
  python3 "$BATS_TEST_TMPDIR/hc_server.py" "$HC_PORT" "$HC_LOG" &
  HC_PID=$!
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$HC_PORT/__ready" >/dev/null 2>&1; then break; fi
    sleep 0.2
  done
  : >"$HC_LOG"   # discard the readiness probe request captured above
}

teardown() {
  [ -n "${HC_PID:-}" ] && kill "$HC_PID" >/dev/null 2>&1 || true
}

@test "healthcheck success is a no-op when HEALTHCHECK_URL is empty" {
  unset HEALTHCHECK_URL
  run healthcheck success
  [ "$status" -eq 0 ]
  [ ! -s "$HC_LOG" ]
}

@test "healthcheck success pings the base URL" {
  export HEALTHCHECK_URL="http://127.0.0.1:$HC_PORT"
  run healthcheck success
  [ "$status" -eq 0 ]
  grep -qx "/" "$HC_LOG"
}

@test "healthcheck failure pings the /fail suffix" {
  export HEALTHCHECK_URL="http://127.0.0.1:$HC_PORT"
  run healthcheck failure
  [ "$status" -eq 0 ]
  grep -qx "/fail" "$HC_LOG"
}

@test "healthcheck failure strips a trailing slash before appending /fail" {
  export HEALTHCHECK_URL="http://127.0.0.1:$HC_PORT/"
  run healthcheck failure
  [ "$status" -eq 0 ]
  grep -qx "/fail" "$HC_LOG"
}

@test "healthcheck never fails the caller when the URL is unreachable" {
  export HEALTHCHECK_URL="http://127.0.0.1:1/unreachable"
  run healthcheck success
  [ "$status" -eq 0 ]
  [[ "$output" == *"healthcheck ping failed"* ]]
}
