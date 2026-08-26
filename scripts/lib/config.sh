#!/usr/bin/env bash
# scripts/lib/config.sh — load + validate mounted config/secrets. Source, don't execute.
# shellcheck shell=bash
# Requires common.sh to be sourced first (die, require_env, log_*).

# _load_env_file FILE — load KEY=VALUE pairs from FILE into the current
# shell and export them. Unlike a plain `source`/`.`, this treats the whole
# remainder of a line as the literal value (no word-splitting, no pathname
# expansion, no command execution), so unquoted values containing spaces or
# glob characters — e.g. a cron expression like `APPDATA_SCHEDULE=0 3 * * *`
# — survive intact instead of bash parsing "3 * * *" as a command line.
# Blank lines and lines starting with `#` are skipped. A value wrapped in
# matching quotes is taken verbatim; otherwise a trailing ` #comment` is
# stripped, mirroring what `source` already does for single-word values.
_load_env_file() {
  local file="$1" line key val
  while IFS= read -r line || [ -n "$line" ]; do
    while [ "${line:0:1}" = " " ] || [ "${line:0:1}" = $'\t' ]; do line="${line:1}"; done
    [ -z "$line" ] && continue
    [ "${line:0:1}" = "#" ] && continue
    case "$line" in
      [A-Za-z_]*=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    case "$val" in
      \"*\"|\'*\')
        val="${val#?}"
        val="${val%?}"
        ;;
      *)
        val="${val%% #*}"
        val="${val%"${val##*[![:space:]]}"}"
        ;;
    esac
    export "$key=$val"
  done <"$file"
}

load_config() {
  local dir="${1:-${CONFIG_DIR:-/config}}"
  CONFIG_DIR="$dir"
  [ -f "$dir/backup.env" ] || die "missing $dir/backup.env (copy backup.env.example and edit)"
  [ -f "$dir/secrets.env" ] || die "missing $dir/secrets.env (copy secrets.env.example and edit; mode 600)"
  _load_env_file "$dir/backup.env"
  _load_env_file "$dir/secrets.env"

  : "${CACHE_DIR:=/cache}"
  : "${APPDATA_SRC:=/backup/appdata}"
  : "${MEDIA_SRC:=/backup/media}"
  : "${APPDATA_STORAGE_CLASS:=STANDARD}"
  : "${MEDIA_STORAGE_CLASS:=DEEP_ARCHIVE}"
  : "${MEDIA_MIRROR:=false}"
  : "${KEEP_LAST:=3}"; : "${KEEP_DAILY:=7}"; : "${KEEP_WEEKLY:=4}"; : "${KEEP_MONTHLY:=6}"
  : "${RCLONE_TRANSFERS:=8}"; : "${RCLONE_BWLIMIT:=}"
  : "${MEDIA_INCLUDES:=$dir/includes-media.txt}"
  : "${LOG_FILE:=$CACHE_DIR/logs/backup-engine.log}"
  : "${NOTIFY_ON_SUCCESS:=false}"

  if [ -z "${RESTIC_REPOSITORY:-}" ]; then
    local host="${S3_ENDPOINT:-s3.${AWS_REGION:-}.amazonaws.com}"
    RESTIC_REPOSITORY="s3:${host}/${S3_BUCKET:-}/appdata"
  fi
  export CACHE_DIR APPDATA_SRC MEDIA_SRC APPDATA_STORAGE_CLASS MEDIA_STORAGE_CLASS \
    MEDIA_MIRROR KEEP_LAST KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY \
    MEDIA_INCLUDES LOG_FILE NOTIFY_ON_SUCCESS RESTIC_REPOSITORY

  # RCLONE_TRANSFERS/RCLONE_BWLIMIT are user-facing config vars that
  # backup-media.sh reads in-process to build its --transfers/--bwlimit CLI
  # flags — they must stay ordinary (non-exported) shell vars. rclone ALSO
  # reads these exact names as its own environment-variable overrides for
  # those same flags, unconditionally, regardless of what's on the CLI. The
  # shipped default leaves RCLONE_BWLIMIT unset/blank; if it's exported,
  # rclone sees RCLONE_BWLIMIT="" and hard-fails every run ("CRITICAL:
  # Invalid value when setting --bwlimit ... empty string") before copying
  # anything. Never add these two to the export list above — `export -n`
  # here also strips the export bit if a user uncommented RCLONE_BWLIMIT in
  # backup.env (the parser exports whatever it finds).
  export -n RCLONE_TRANSFERS RCLONE_BWLIMIT
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
