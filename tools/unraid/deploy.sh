#!/bin/bash
# Deploy master to the Unraid box (BE_BOX=root@<box-ip>): sync source (never tfstate / .terraform / VCS / agent scratch),
# then build + recreate + health-gate + auto-rollback on the box (deploy_on_box.sh).
set -euo pipefail
BOX="${BE_BOX:?set BE_BOX to the Unraid box, e.g. BE_BOX=root@<box-ip>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
[ "$(git branch --show-current)" = master ] || { echo "not on master"; exit 1; }
rsync -az --delete --delete-excluded \
  --exclude=.git --exclude=.claude --exclude=.superpowers --exclude=.kilo \
  --exclude='*.tfstate' --exclude='*.tfstate.*' --exclude='.terraform/' \
  ./ "$BOX:/root/backup-engine-build/"
ssh "$BOX" 'ls /root/backup-engine-build/opentofu/ | grep -E "tfstate|^\.terraform$" && echo "!! tfstate reached the box" || echo "-- no tfstate on the box (good)"'
scp -q "$HERE/deploy_on_box.sh" "$BOX:/root/deploy_on_box.sh"
ssh "$BOX" 'bash /root/deploy_on_box.sh'
