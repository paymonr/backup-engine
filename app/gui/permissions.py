# app/gui/permissions.py — "Check & update AWS permissions" (spec
# docs/superpowers/specs/2026-09-22-permissions-converge-design.md). Three layers:
#   levels   — provisioning/permissions.json (the required level + history) and the
#              PERMISSIONS_VERSION stamp in backup.env. Pure reads, safe at render.
#   engine   — the required IAM set (R1-R5), a pure planner, and discover/apply over
#              the aws CLI with TRANSIENT admin creds (never stored, always scrubbed).
#   fallback — a re-runnable bash script + runtime-key-only Verify probes.
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config_io, provision

MANIFEST = provision.PROVISIONING_DIR / "permissions.json"
STAMP_KEY = "PERMISSIONS_VERSION"
CHECKED_KEY = "PERMISSIONS_CHECKED_AT"


# --- levels -------------------------------------------------------------------

def load_manifest(path: str | Path = MANIFEST) -> dict:
    return json.loads(Path(path).read_text())


def required_level(path: str | Path = MANIFEST) -> int:
    return int(load_manifest(path)["level"])


def history(path: str | Path = MANIFEST) -> list[dict]:
    return list(load_manifest(path)["history"])


def templates_sha256(prov_dir: str | Path = provision.PROVISIONING_DIR) -> str:
    """Fingerprint of every provisioning/*.tmpl (sorted by name). Pinned in the
    manifest, so editing a template without bumping the level fails a test."""
    h = hashlib.sha256()
    for p in sorted(Path(prov_dir).glob("*.tmpl"), key=lambda p: p.name):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def current_level(config_dir: str) -> int | None:
    raw = config_io.read_backup_env(config_dir).get(STAMP_KEY, "").strip()
    return int(raw) if raw.isdigit() else None


def checked_at(config_dir: str) -> str | None:
    return config_io.read_backup_env(config_dir).get(CHECKED_KEY, "").strip() or None


def feature_level(feature: str, path: str | Path = MANIFEST) -> int | None:
    for h in history(path):
        if feature in h.get("features", []):
            return int(h["level"])
    return None


def feature_available(config_dir: str, feature: str) -> bool:
    """History-driven gating: a feature is on once the stamp reaches the level that
    introduced it, so a later bump for something else never re-disables it."""
    have, need = current_level(config_dir), feature_level(feature)
    return have is not None and need is not None and have >= need


def level_status(config_dir: str) -> dict:
    """The stamp vs this build. Reads backup.env only -- safe on any GET."""
    have, need = current_level(config_dir), required_level()
    if have is None:
        state, missing = "unchecked", []
    elif have >= need:
        state, missing = "current", []
    else:
        state, missing = "behind", [h for h in history() if int(h["level"]) > have]
    return {"state": state, "level": have, "required": need, "missing": missing,
            "checked_at": checked_at(config_dir)}
