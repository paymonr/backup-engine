# app/gui/config_io.py — the ONLY reader/writer of the mounted config files.
from __future__ import annotations
from pathlib import Path
import re

SECRET_KEYS: tuple[str, ...] = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "RESTIC_PASSWORD")
# Cost Explorer creds are a SEPARATE, optional, read-only credential set — never
# the runtime key, and never looped by config.html (which only loops SECRET_KEYS).
# Managed exclusively on the Cost page via read_cost_explorer_creds/clear_cost_explorer_creds.
COST_EXPLORER_KEYS: tuple[str, ...] = (
    "COST_EXPLORER_ACCESS_KEY_ID", "COST_EXPLORER_SECRET_ACCESS_KEY", "COST_EXPLORER_SESSION_TOKEN",
)
# Both groups share one secrets.env file; write_secrets/clear_cost_explorer_creds must
# rebuild the file from this UNION so writing one group never drops the other.
_MANAGED_KEYS: tuple[str, ...] = SECRET_KEYS + COST_EXPLORER_KEYS
_KEY_RE = re.compile(r"^\s*#?\s*([A-Z0-9_]+)=")
_INLINE_COMMENT = re.compile(r"\s#.*$")

def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = _INLINE_COMMENT.sub("", v).strip().strip('"').strip("'")
    return out

def _read_secrets_raw(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v   # verbatim remainder — NO comment/quote/whitespace stripping
    return out

def template_keys(template_path: str) -> list[str]:
    keys: list[str] = []
    for line in Path(template_path).read_text().splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys

def read_backup_env(config_dir: str) -> dict[str, str]:
    p = Path(config_dir, "backup.env")
    return _parse_env(p.read_text()) if p.exists() else {}

def write_backup_env(template_path: str, config_dir: str, values: dict[str, str]) -> None:
    out: list[str] = []
    for line in Path(template_path).read_text().splitlines():
        m = _KEY_RE.match(line)
        if m and values.get(m.group(1), "") != "":
            safe_value = values[m.group(1)].replace("\n", " ").replace("\r", " ")
            out.append(f"{m.group(1)}={safe_value}")
        else:
            out.append(line)
    Path(config_dir, "backup.env").write_text("\n".join(out) + "\n")

def secrets_status(config_dir: str) -> dict[str, bool]:
    p = Path(config_dir, "secrets.env")
    vals = _read_secrets_raw(p)
    return {k: bool(vals.get(k)) for k in SECRET_KEYS}

def secrets_mode(config_dir: str) -> str | None:
    p = Path(config_dir, "secrets.env")
    return oct(p.stat().st_mode & 0o777)[2:] if p.exists() else None

def write_secrets(config_dir: str, values: dict[str, str]) -> None:
    p = Path(config_dir, "secrets.env")
    existing = _read_secrets_raw(p)
    for k in _MANAGED_KEYS:
        v = values.get(k, "")
        if v != "":
            if "\n" in v or "\r" in v:
                raise ValueError("secret value must not contain a newline")
            existing[k] = v
    body = "# backup-engine secrets — managed by the GUI (mode 600).\n"
    body += "".join(f"{k}={existing[k]}\n" for k in _MANAGED_KEYS if k in existing)
    p.write_text(body)
    p.chmod(0o600)

def read_cost_explorer_creds(config_dir: str) -> dict[str, str] | None:
    """Optional, read-only Cost Explorer creds mapped to the AWS_* keys billing.py
    expects. Returns None unless BOTH the key id and secret are present+non-empty
    (a partially-filled connect attempt is treated as not-connected). Never logged
    or handed to a template."""
    vals = _read_secrets_raw(Path(config_dir, "secrets.env"))
    key = vals.get("COST_EXPLORER_ACCESS_KEY_ID", "")
    secret = vals.get("COST_EXPLORER_SECRET_ACCESS_KEY", "")
    if not key or not secret:
        return None
    creds = {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret}
    token = vals.get("COST_EXPLORER_SESSION_TOKEN", "")
    if token:
        creds["AWS_SESSION_TOKEN"] = token
    return creds

def clear_cost_explorer_creds(config_dir: str) -> None:
    """Disconnect: drop the CE keys from secrets.env, keeping the core secrets
    (the write_secrets 'blank keeps existing' guard has no way to express
    delete, so this rewrites `existing` directly instead)."""
    p = Path(config_dir, "secrets.env")
    existing = _read_secrets_raw(p)
    for k in COST_EXPLORER_KEYS:
        existing.pop(k, None)
    body = "# backup-engine secrets — managed by the GUI (mode 600).\n"
    body += "".join(f"{k}={existing[k]}\n" for k in _MANAGED_KEYS if k in existing)
    p.write_text(body)
    p.chmod(0o600)
