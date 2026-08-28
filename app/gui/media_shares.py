# app/gui/media_shares.py — the ONLY reader/writer of MEDIA_SHARES_DIR (per-share
# rclone filter files). Enumerates shares under MEDIA_ROOT and translates folder
# selections <-> rclone filter rules. Never writes under MEDIA_ROOT (read-only there).
from __future__ import annotations
import re
from pathlib import Path
from . import fsbrowse

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LEAF_RE = re.compile(r"^\+ (/.+)/\*\*$")   # "+ /manga/raw/**"
_ANCESTOR_RE = re.compile(r"^\+ /.+/$")      # "+ /manga/"  (helper line)

def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and name not in (".", "..")

def generate_rules(whole: bool, folders: list[str]) -> str:
    clean = [f.strip("/") for f in folders if f and f.strip("/")]
    if whole or not clean:
        return "+ /**\n"
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
    if not lines or lines == ["+ /**"]:
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

def _file(shares_dir, name: str) -> Path:
    if not valid_name(name):
        raise ValueError("invalid share name")
    return Path(shares_dir) / f"{name}.txt"

def list_shares(media_root, shares_dir) -> list[dict]:
    try:
        names = fsbrowse.list_dirs(media_root, "")
    except fsbrowse.PathError:
        names = []
    out: list[dict] = []
    for name in names:
        if not valid_name(name):
            continue  # unrepresentable share names are skipped, not backed up
        sel = read_selection(shares_dir, name)
        enabled = (Path(shares_dir) / f"{name}.txt").exists()
        out.append({"name": name, "enabled": enabled, "whole": sel["whole"],
                    "folders": sel["folders"], "custom": sel["raw"] is not None,
                    "folders_raw": sel["raw"] or ""})
    return out

def read_selection(shares_dir, name) -> dict:
    f = _file(shares_dir, name)
    if not f.exists():
        return {"whole": False, "folders": [], "raw": None}
    return parse_rules(f.read_text())

def write_selection(shares_dir, name, whole, folders) -> None:
    f = _file(shares_dir, name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(generate_rules(whole, folders))

def write_raw(shares_dir, name, text) -> None:
    f = _file(shares_dir, name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if text.endswith("\n") else text + "\n")

def disable(shares_dir, name) -> None:
    _file(shares_dir, name).unlink(missing_ok=True)
