#!/bin/bash
# Runs ON the Unraid box. Recreates the backup-engine container from a freshly
# built image, preserving the live container's env/mounts/ports/labels/network
# (captured from its own `docker inspect`). Health-checks; auto-rolls-back on failure.
set -uo pipefail
BUILD=/root/backup-engine-build
IMG=backup-engine:cost
CON=backup-engine

# keep_volume NAME DEST ANONYMOUS(0|1) "DECLARED DESTS" — exit 0 to carry the volume onto the new
# container. Named volumes are always kept. An anonymous volume only exists because an image declared
# VOLUME DEST; carry it only while the new image still declares DEST, so a removed VOLUME doesn't
# survive every deploy as an empty, unmapped volume.
keep_volume() {
  local dest=$2 anonymous=$3 d
  [ "$anonymous" = 0 ] && return 0
  for d in $4; do [ "$d" = "$dest" ] && return 0; done
  return 1
}

# volume_is_anonymous NAME — exit 0 if Docker labels NAME as an anonymous volume
volume_is_anonymous() {
  docker volume inspect -f '{{json .Labels}}' "$1" 2>/dev/null | grep -q '"com.docker.volume.anonymous"'
}

# carried_mounts "DECLARED DESTS" BIND... -- MOUNT... — print, one per line, the specs to pass as
# --volume to the new container: every host-path bind, named volumes, and anonymous volumes only while
# keep_volume says so. A volume can appear in both HostConfig.Binds (passed as --volume NAME:DEST by an
# earlier deploy) and .Mounts; each destination is emitted at most once. Drops are reported on stderr.
carried_mounts() {
  local declared=$1; shift
  local -a binds=() mounts=()
  local x after_sep=0
  for x in "$@"; do
    if [ "$x" = "--" ]; then after_sep=1; continue; fi
    [ -z "$x" ] && continue
    if [ "$after_sep" = 0 ]; then binds+=("$x"); else mounts+=("$x"); fi
  done
  local -A handled=()
  local spec src rest dest anon
  for spec in "${binds[@]}" "${mounts[@]}"; do
    src=${spec%%:*}; rest=${spec#*:}; dest=${rest%%:*}
    [ -n "${handled[$dest]:-}" ] && continue
    handled[$dest]=1
    if [ "${src:0:1}" = / ]; then echo "$spec"; continue; fi
    anon=0; volume_is_anonymous "$src" && anon=1
    if keep_volume "$src" "$dest" "$anon" "$declared"; then
      echo "$spec"
    else
      echo "-- not carrying anonymous volume $src: $dest is no longer declared by the new image" >&2
    fi
  done
}

main() {
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

# Binds + volumes, after the build: anonymous volumes the new image no longer declares are dropped
DECLARED=$(docker image inspect -f '{{range $k,$v := .Config.Volumes}}{{$k}} {{end}}' "$IMG")
mapfile -t CARRY < <(carried_mounts "$DECLARED" "${BINDS[@]}" -- "${MOUNTS[@]}")
for v in "${CARRY[@]}"; do [ -n "$v" ] && ARGS+=(--volume "$v"); done

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
}

# Run only when executed (the bats test sources this file for keep_volume)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
