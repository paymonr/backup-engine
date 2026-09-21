#!/usr/bin/env bash
# scripts/lib/points.sh — restore-point cache: $CACHE_DIR/state/<job>.points.json.
# Source, don't execute (backup-job.sh insertion 7, restore.sh list --json).
#
# points_refresh JOB TYPE  — render + write the cache atomically (temp + mv).
# points_render  JOB TYPE  — render the same JSON to stdout (restore.sh list --json).
#
# Per type (spec 7.5.4):
#   versioned       -> raw `restic snapshots --tag JOB --json`
#   archive         -> {"kind":"current-copy","folders":[…],"as_of":"<now>"} from `rclone lsf --dirs-only`
#   versioned-files -> nothing (the catalog $CACHE_DIR/<job>.sqlite is read directly by points.py)
#
# This lib shells ONLY to restic/rclone, never python: the run bookkeeping and
# any test that stubs `python3` must stay valid (spec 7.5.4 / risk row).
# shellcheck shell=bash

_points_now()  { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
_points_file() { printf '%s/state/%s.points.json\n' "$CACHE_DIR" "$1"; }
# JSON-escape a string body (backslash + double-quote; folder names never carry controls).
_points_esc()  { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"; }

# points_render JOB TYPE -> restore-point JSON on stdout (no file write).
points_render() {
  local job="$1" type="$2"
  case "$type" in
    versioned)       restic -r "$RESTIC_REPOSITORY" snapshots --tag "$job" --json ;;
    archive)         _points_render_archive "$job" ;;
    versioned-files) return 0 ;;
    *)               return 0 ;;
  esac
}

_points_render_archive() {
  local job="$1" f folders="" first=1
  : "${RCLONE_CONFIG:=$CACHE_DIR/rclone.conf}"
  # rclone lsf --dirs-only lists the immediate subdirectories, each with a
  # trailing slash. Every top-level folder becomes one scope option (5.2).
  while IFS= read -r f; do
    f="${f%/}"
    [ -n "$f" ] || continue
    if [ "$first" -eq 1 ]; then first=0; else folders+=","; fi
    folders+="\"$(_points_esc "$f")\""
  done < <(rclone --config "$RCLONE_CONFIG" lsf --dirs-only "s3:${JOB_BUCKET:-$S3_BUCKET}/media/$job/" 2>/dev/null)
  printf '{"kind":"current-copy","folders":[%s],"as_of":"%s"}\n' "$folders" "$(_points_now)"
}

# points_refresh JOB TYPE -> write state/<job>.points.json atomically.
# versioned-files writes nothing and returns 0. A tool failure (restic/rclone
# non-zero, or empty output for versioned) is non-fatal here: the temp file is
# removed and any previous cache is left in place, and the function returns 1 so
# the caller's `|| log_warn` records the (non-fatal) miss.
points_refresh() {
  local job="$1" type="$2"
  case "$type" in
    versioned-files) return 0 ;;
    versioned|archive) ;;
    *) return 0 ;;
  esac
  mkdir -p "$CACHE_DIR/state"
  local dst tmp
  dst="$(_points_file "$job")"
  tmp="$dst.tmp.$$"
  if points_render "$job" "$type" >"$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    mv -f "$tmp" "$dst"
    return 0
  fi
  rm -f "$tmp"
  return 1
}
