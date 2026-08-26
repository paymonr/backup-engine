#!/usr/bin/env bash
# setup.sh — scripted provisioning (mode 2). Admin creds come from YOUR env, never the container.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { echo "usage: setup.sh <bucket-name> <region>"; }

main() {
  [ $# -eq 2 ] || { usage; exit 2; }
  local bucket="$1" region="$2"
  if [ -z "${AWS_ACCESS_KEY_ID:-}" ] && [ -z "${AWS_PROFILE:-}" ]; then
    echo "error: no AWS admin credentials in env (set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE)." >&2
    exit 1
  fi
  cd "$HERE/opentofu"
  tofu init -input=false
  tofu apply -input=false -auto-approve -var "bucket_name=$bucket" -var "region=$region"
  echo "# ---- paste into config/secrets.env ----"
  echo "AWS_ACCESS_KEY_ID=$(tofu output -raw runtime_access_key_id)"
  echo "AWS_SECRET_ACCESS_KEY=$(tofu output -raw runtime_secret_access_key)"
  echo "# ---- paste into config/backup.env ----"
  echo "AWS_REGION=$(tofu output -raw region)"
  echo "S3_BUCKET=$(tofu output -raw bucket_name)"
}
main "$@"
