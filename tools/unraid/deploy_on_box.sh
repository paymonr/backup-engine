#!/bin/bash
# Runs ON the Unraid box. Recreates the backup-engine container from a freshly
# built image, preserving the live container's env/mounts/ports/labels/network
# (captured from its own `docker inspect`). Health-checks; auto-rolls-back on failure.
set -uo pipefail
BUILD=/root/backup-engine-build
IMG=backup-engine:cost
CON=backup-engine

echo "== deploy start $(date -u +%FT%TZ) =="

docker inspect "$CON" >/dev/null 2>&1 || { echo "FATAL: container $CON not running"; exit 1; }

# 1) Capture the LIVE runtime spec (whole KEY=VAL / src:dst lines — no re-splitting)
mapfile -t ENVS   < <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CON")
mapfile -t BINDS  < <(docker inspect -f '{{range .HostConfig.Binds}}{{println .}}{{end}}' "$CON")
mapfile -t LABELS < <(docker inspect -f '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' "$CON")
mapfile -t MOUNTS < <(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}:{{.Destination}}{{println}}{{end}}{{end}}' "$CON")
NET=$(docker inspect -f '{{range $n,$c := .NetworkSettings.Networks}}{{$n}}{{end}}' "$CON")
RESTART=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CON")
PORTBINDS=$(docker inspect -f '{{range $p,$b := .HostConfig.PortBindings}}{{range $b}}{{if .HostPort}}{{.HostPort}}:{{$p}} {{end}}{{end}}{{end}}' "$CON")

# Assemble docker run args
ARGS=(-d --name "$CON")
[ -n "$RESTART" ] && [ "$RESTART" != "no" ] && ARGS+=(--restart "$RESTART")
[ -n "$NET" ] && ARGS+=(--network "$NET")
for e in "${ENVS[@]}";  do [ -n "$e" ] && ARGS+=(--env  "$e"); done
for b in "${BINDS[@]}"; do [ -n "$b" ] && ARGS+=(--volume "$b"); done
# named volumes not already covered by Binds
for m in "${MOUNTS[@]}"; do
  [ -z "$m" ] && continue
  dup=0; for b in "${BINDS[@]}"; do [ "$b" = "$m" ] && dup=1; done
  [ "$dup" = 0 ] && ARGS+=(--volume "$m")
done
for l in "${LABELS[@]}"; do [ -n "$l" ] && ARGS+=(--label "$l"); done
if [ -n "$PORTBINDS" ]; then for pb in $PORTBINDS; do ARGS+=(--publish "${pb%%/*}"); done
else ARGS+=(--publish 8099:8099); fi

echo "-- captured: ${#ENVS[@]} env, ${#BINDS[@]} binds, ${#MOUNTS[@]} volumes, net=$NET restart=$RESTART ports=${PORTBINDS:-8099:8099}"

# 2) Save current image as rollback, then build the new image
docker tag "$IMG" backup-engine:prev
echo "-- building $IMG from $BUILD ..."
if ! docker build -t "$IMG" "$BUILD"; then
  echo "FATAL: build failed — live container untouched, nothing changed."; exit 1
fi

# 3) Recreate: stop old, rename to -prev (drop any stale -prev first), run new
docker stop "$CON"
docker rm -f backup-engine-prev >/dev/null 2>&1 || true
docker rename "$CON" backup-engine-prev
echo "-- starting new container ..."
if ! docker run "${ARGS[@]}" "$IMG"; then
  echo "run failed — rolling back"; docker rm -f "$CON" >/dev/null 2>&1 || true
  docker rename backup-engine-prev "$CON"; docker start "$CON"; docker tag backup-engine:prev "$IMG"
  echo "ROLLED BACK to previous container."; exit 1
fi

# 4) Health-check (retry ~40s); accept 2xx/3xx
code=000
for _ in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8099/ 2>/dev/null || echo 000)
  case "$code" in 2??|3??) break;; esac
  sleep 2
done
echo "-- health: GET / -> HTTP $code"
case "$code" in
  2??|3??)
    echo "== DEPLOY OK ($code). New image live; previous kept as container 'backup-engine-prev' + image 'backup-engine:prev' for rollback. =="
    docker ps --filter name=backup-engine --format '{{.Names}}  {{.Image}}  {{.Status}}'
    ;;
  *)
    echo "HEALTH FAILED ($code) — rolling back"
    docker rm -f "$CON" >/dev/null 2>&1 || true
    docker rename backup-engine-prev "$CON"; docker start "$CON"; docker tag backup-engine:prev "$IMG"
    sleep 3; rb=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8099/ 2>/dev/null || echo 000)
    echo "ROLLED BACK; previous container health -> HTTP $rb"; exit 1
    ;;
esac
