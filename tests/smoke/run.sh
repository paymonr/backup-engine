#!/usr/bin/env bash
# tests/smoke/run.sh — run the one-shot real-AWS S3 rules smoke test (s3rules_smoke.py, next to this
# file) on the Unraid box, in YOUR OWN terminal:
#
#   bash /root/backup-engine-smoke/tests/smoke/run.sh
#
# Needs the image built from this branch first:
#   docker build -t backup-engine:s3rules-smoke /root/backup-engine-smoke
#
# It asks for your admin AWS keys (typed, never echoed, never on a command line, never written
# anywhere), runs the test in a throwaway container of that image -- the live `backup-engine`
# container is never used or changed; its /config is mounted READ-ONLY so the test can learn the
# base bucket, the region and the live dedicated buckets -- and prints where the report is.
# The live buckets are only ever read; the test works in three scratch buckets it creates and
# always deletes again (Ctrl-C included -- wait for the CLEANUP lines).
# Optional: SMOKE_TOFU=1 bash run.sh  also checks `tofu init` + `tofu validate` (downloads the AWS provider).
set -euo pipefail

IMAGE="backup-engine:s3rules-smoke"
CONTAINER="backup-engine"
OUT_DIR="${SMOKE_OUT_DIR:-/root/s3rules-smoke-out}"
SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SMOKE_DIR/../.." && pwd)"

die() { printf 'run.sh: %s\n' "$*" >&2; exit 1; }
forget_keys() { unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN key secret token; }
trap forget_keys EXIT

command -v docker >/dev/null 2>&1 || die "docker isn't available here"
[ -f "$SMOKE_DIR/s3rules_smoke.py" ] || die "s3rules_smoke.py isn't next to this script ($SMOKE_DIR)"
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "image $IMAGE not found — build it first: docker build -t $IMAGE $REPO_DIR"

docker container inspect "$CONTAINER" >/dev/null 2>&1 \
  || die "no container named '$CONTAINER' — the test reads the live install's /config from it"
LIVE_CONFIG="$(docker container inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")"
[ -n "$LIVE_CONFIG" ] || die "container '$CONTAINER' has no /config mount"
[ -f "$LIVE_CONFIG/backup.env" ] || die "no backup.env in $LIVE_CONFIG (the live /config)"

mkdir -p "$OUT_DIR"
GIT_REV="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"

echo "S3 rules smoke test — image $IMAGE (git $GIT_REV), live config $LIVE_CONFIG (read-only)"
echo "Your admin AWS keys are used for this run only: not echoed, not stored, not logged."
key="" secret="" token=""
read -rsp "Admin AWS access key ID: " key; echo
read -rsp "Admin AWS secret access key: " secret; echo
read -rsp "Session token (press Enter if none): " token; echo
if [ -z "$key" ] || [ -z "$secret" ]; then die "the access key ID and the secret are both needed"; fi

export AWS_ACCESS_KEY_ID="$key" AWS_SECRET_ACCESS_KEY="$secret"
if [ -n "$token" ]; then export AWS_SESSION_TOKEN="$token"; else unset AWS_SESSION_TOKEN; fi
unset key secret token

rc=0
# `-e NAME` with no value passes the variable from this process only (and not at all when unset),
# so the keys never appear on a command line or in `ps`.
docker run --rm --entrypoint python3 \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  -e SMOKE_TOFU -e SMOKE_GIT_REV="$GIT_REV" \
  -v "$SMOKE_DIR":/smoke:ro \
  -v "$OUT_DIR":/out \
  -v "$LIVE_CONFIG":/liveconfig:ro \
  "$IMAGE" /smoke/s3rules_smoke.py || rc=$?
forget_keys

report=""
for f in "$OUT_DIR"/s3rules-smoke-*.txt; do
  [ -e "$f" ] && report="$f"          # names sort by their UTC timestamp: the last one is newest
done
echo
if [ -n "$report" ]; then
  echo "Report: $report"
else
  echo "No report was written (see the output above)."
fi
echo "Before-snapshot of the live buckets: $OUT_DIR/before-level4-*.json"
exit "$rc"
