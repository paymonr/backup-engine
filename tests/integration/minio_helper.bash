# tests/integration/minio_helper.bash
# Starts a disposable STANDALONE MinIO (binary on PATH; no docker) and points
# restic/rclone/awscli at it over an http S3-compatible endpoint. The endpoint
# is exported WITH its http:// scheme so restic's repo URL and rclone's endpoint
# both talk plain http to local MinIO.
minio_up() {
  export MINIO_DATADIR; MINIO_DATADIR="$(mktemp -d)"
  export MINIO_PORT
  MINIO_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
  export MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin
  minio server "$MINIO_DATADIR" --address "127.0.0.1:$MINIO_PORT" \
    --console-address "127.0.0.1:0" >"$MINIO_DATADIR/minio.log" 2>&1 &
  export MINIO_PID=$!
  export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin AWS_REGION=us-east-1
  export S3_ENDPOINT="http://127.0.0.1:$MINIO_PORT"
  export AWS_S3_ENDPOINT="$S3_ENDPOINT"          # awscli / restic repo host
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$MINIO_PORT/minio/health/live" >/dev/null; then break; fi
    sleep 1
  done
}
minio_make_bucket() {
  local bucket="$1"
  AWS_EC2_METADATA_DISABLED=true aws --endpoint-url "$AWS_S3_ENDPOINT" \
    s3 mb "s3://$bucket" >/dev/null 2>&1 || true
}
minio_down() {
  [ -n "${MINIO_PID:-}" ] && kill "$MINIO_PID" >/dev/null 2>&1 || true
  [ -n "${MINIO_DATADIR:-}" ] && rm -rf "$MINIO_DATADIR" || true
}
