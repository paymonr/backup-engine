#!/usr/bin/env bash
# scripts/lib/config.sh — load + validate mounted config/secrets. Source, don't execute.
# shellcheck shell=bash
# Requires common.sh to be sourced first (die, require_env, log_*).

load_config() {
  local dir="${1:-${CONFIG_DIR:-/config}}"
  CONFIG_DIR="$dir"
  [ -f "$dir/backup.env" ] || die "missing $dir/backup.env (copy backup.env.example and edit)"
  [ -f "$dir/secrets.env" ] || die "missing $dir/secrets.env (copy secrets.env.example and edit; mode 600)"
  set -a
  # shellcheck source=/dev/null
  . "$dir/backup.env"
  # shellcheck source=/dev/null
  . "$dir/secrets.env"
  set +a

  : "${CACHE_DIR:=/cache}"
  : "${APPDATA_SRC:=/backup/appdata}"
  : "${MEDIA_SRC:=/backup/media}"
  : "${APPDATA_STORAGE_CLASS:=STANDARD}"
  : "${MEDIA_STORAGE_CLASS:=DEEP_ARCHIVE}"
  : "${MEDIA_MIRROR:=false}"
  : "${KEEP_LAST:=3}"; : "${KEEP_DAILY:=7}"; : "${KEEP_WEEKLY:=4}"; : "${KEEP_MONTHLY:=6}"
  : "${RCLONE_TRANSFERS:=8}"; : "${RCLONE_BWLIMIT:=}"
  : "${MEDIA_INCLUDES:=$dir/includes-media.txt}"
  : "${LOG_FILE:=$CACHE_DIR/logs/unraid-s3-backup.log}"
  : "${NOTIFY_ON_SUCCESS:=false}"

  if [ -z "${RESTIC_REPOSITORY:-}" ]; then
    local host="${S3_ENDPOINT:-s3.${AWS_REGION:-}.amazonaws.com}"
    RESTIC_REPOSITORY="s3:${host}/${S3_BUCKET:-}/appdata"
  fi
  export CACHE_DIR APPDATA_SRC MEDIA_SRC APPDATA_STORAGE_CLASS MEDIA_STORAGE_CLASS \
    MEDIA_MIRROR KEEP_LAST KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY RCLONE_TRANSFERS \
    RCLONE_BWLIMIT MEDIA_INCLUDES LOG_FILE NOTIFY_ON_SUCCESS RESTIC_REPOSITORY
}

validate_common() {
  require_env AWS_REGION S3_BUCKET AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
}

validate_appdata() {
  validate_common
  require_env RESTIC_PASSWORD RESTIC_REPOSITORY
  if [ ! -d "$APPDATA_SRC" ] || [ -z "$(ls -A "$APPDATA_SRC" 2>/dev/null)" ]; then
    die "appdata source '$APPDATA_SRC' missing or empty — install/configure the Appdata Backup plugin (appdata.backup) and confirm its archive directory is mounted read-only here"
  fi
}

validate_media() {
  validate_common
  [ -d "$MEDIA_SRC" ] || die "media source '$MEDIA_SRC' not found (mount your media root read-only)"
  [ -f "$MEDIA_INCLUDES" ] || die "media include-list '$MEDIA_INCLUDES' not found (copy includes-media.txt.example)"
}
