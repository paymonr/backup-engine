#!/bin/bash
# Build a SEPARATE smoke-test image of the s3-rules branch on the Unraid box.
# Never touches the live `backup-engine` container or its image tag: it syncs the
# working tree to its own directory and builds the tag backup-engine:s3rules-smoke.
set -euo pipefail
BOX="${BE_BOX:?set BE_BOX to the Unraid box, e.g. BE_BOX=root@<box-ip>}"
DEST=/root/backup-engine-smoke
cd "$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
[ "$(git branch --show-current)" = s3-rules ] || { echo "not on s3-rules"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "working tree not clean"; exit 1; }
echo "== syncing s3-rules @ $(git rev-parse --short HEAD) to $BOX:$DEST"
rsync -az --delete --delete-excluded \
  --exclude=.git --exclude=.claude --exclude=.superpowers --exclude=.kilo \
  --exclude='*.tfstate' --exclude='*.tfstate.*' --exclude='.terraform/' \
  --exclude='__pycache__/' \
  ./ "$BOX:$DEST/"
ssh "$BOX" "ls $DEST/opentofu/ | grep -E 'tfstate|^\.terraform\$' && echo '!! tfstate reached the box' || echo '-- no tfstate on the box (good)'"
echo "== building backup-engine:s3rules-smoke (the live container is not touched)"
ssh "$BOX" "docker build -q -t backup-engine:s3rules-smoke $DEST && docker image ls backup-engine:s3rules-smoke --format '{{.Repository}}:{{.Tag}}  {{.ID}}  {{.CreatedSince}}'"
echo "== ready. Next, in YOUR OWN terminal on the box:  bash $DEST/tests/smoke/run.sh"
