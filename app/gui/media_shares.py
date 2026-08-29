# app/gui/media_shares.py — the ONLY reader/writer of the media include-list
# (config/media-includes.txt): one rclone filter over MEDIA_ROOT that selects
# which folders get backed up. Translates the GUI folder selection <-> rclone
# filter rules. Never writes under MEDIA_ROOT (that mount is read-only).
from __future__ import annotations
import re
from pathlib import Path

INCLUDES_FILE = "media-includes.txt"

_LEAF_RE = re.compile(r"^\+ (/.+)/\*\*$")   # "+ /movies/**" or "+ /a/b/**"
_ANCESTOR_RE = re.compile(r"^\+ /.+/$")      # "+ /a/"  (helper line so rclone descends)

def _validate_folder(f: str) -> None:
    # A folder value only ever becomes a filter-file line (via generate_rules),
    # but reject anything that could inject an extra rule line or escape the root
    # before it gets that far.
    if "\n" in f or "\r" in f:
        raise ValueError(f"folder value contains a newline: {f!r}")
    if f.startswith("/"):
        raise ValueError(f"folder value must be relative, not absolute: {f!r}")
    if ".." in f.split("/"):
        raise ValueError(f"folder value must not contain '..': {f!r}")

def generate_rules(whole: bool, folders: list[str]) -> str:
    """rclone --filter-from rules for the selection. whole -> everything; an empty
    selection -> nothing (default-exclude); otherwise include each chosen folder
    (with ancestor lines so rclone descends) and exclude the rest."""
    if whole:
        return "+ /**\n"
    clean = [f.strip("/") for f in folders if f and f.strip("/")]
    if not clean:
        return "- **\n"
    leaves: list[str] = []
    ancestors: set[str] = set()
    for f in clean:
        parts = [p for p in f.split("/") if p]
        leaves.append("+ /" + "/".join(parts) + "/**")
        for i in range(1, len(parts) + 1):
            ancestors.add("+ /" + "/".join(parts[:i]) + "/")
    lines = leaves + sorted(ancestors) + ["- **"]
    return "\n".join(lines) + "\n"

def parse_rules(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines == ["+ /**"]:
        return {"whole": True, "folders": [], "raw": None}
    folders: list[str] = []
    for ln in lines:
        if ln == "- **" or _ANCESTOR_RE.match(ln):
            continue
        m = _LEAF_RE.match(ln)
        if m:
            folders.append(m.group(1).strip("/"))
            continue
        return {"whole": False, "folders": [], "raw": text}  # non-canonical => custom
    return {"whole": False, "folders": folders, "raw": None}

def _path(config_dir) -> Path:
    return Path(config_dir, INCLUDES_FILE)

def read_selection(config_dir) -> dict:
    f = _path(config_dir)
    if not f.exists():
        return {"whole": False, "folders": [], "raw": None}
    return parse_rules(f.read_text())

def write_selection(config_dir, whole, folders) -> None:
    for folder in folders:
        _validate_folder(folder)
    _path(config_dir).write_text(generate_rules(whole, folders))

def write_raw(config_dir, text) -> None:
    _path(config_dir).write_text(text if text.endswith("\n") else text + "\n")
