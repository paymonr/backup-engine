#!/usr/bin/env bash
# scripts/lib/rclone-conf.sh — render an rclone S3 remote named [s3] from env. Source, don't execute.
# shellcheck shell=bash
render_rclone_conf() {
  local path="$1"
  local provider="AWS" endpoint_line="" path_style_line="" sse_line="server_side_encryption = AES256"
  if [ -n "${S3_ENDPOINT:-}" ]; then
    provider="Other"
    endpoint_line="endpoint = ${S3_ENDPOINT}"
    # Non-AWS / IP-addressed S3 endpoints (e.g. standalone MinIO) don't support
    # virtual-hosted-style bucket addressing — force path-style so rclone talks
    # to http://host:port/bucket/key instead of http://bucket.host:port/key.
    path_style_line="force_path_style = true"
    # Bucket-level SSE requires a KMS backend; standalone MinIO (no KMS) 501s
    # on PutObject if we ask for it. Only request it against real AWS.
    sse_line=""
    # log_warn comes from common.sh; guard so this file can still be sourced
    # and unit-tested (render_rclone_conf called directly) without it.
    if command -v log_warn >/dev/null 2>&1; then
      log_warn "S3_ENDPOINT set — server-side encryption (AES256) not requested for a non-AWS endpoint; configure it manually if your backend supports/requires it."
    fi
  fi
  mkdir -p "$(dirname "$path")"
  cat >"$path" <<EOF
[s3]
type = s3
provider = ${provider}
env_auth = false
access_key_id = ${AWS_ACCESS_KEY_ID}
secret_access_key = ${AWS_SECRET_ACCESS_KEY}
region = ${AWS_REGION}
${endpoint_line}
${path_style_line}
no_check_bucket = true
${sse_line}
EOF
  chmod 600 "$path"
}
