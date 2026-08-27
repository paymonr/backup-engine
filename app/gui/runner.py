# app/gui/runner.py — the ONLY subprocess launcher / cache reader.
from __future__ import annotations
from pathlib import Path
import json
import os
import subprocess

PIPELINES: dict[str, str] = {"appdata": "backup-appdata.sh", "media": "backup-media.sh"}

def read_state(cache_dir: str, pipeline: str) -> dict | None:
    p = Path(cache_dir, "state", f"{pipeline}.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None

def tail_log(cache_dir: str, n: int = 200) -> str:
    p = Path(cache_dir, "logs", "backup-engine.log")
    if not p.exists():
        return ""
    return "\n".join(p.read_text(errors="replace").splitlines()[-n:])

def trigger_backup(scripts_dir: str, pipeline: str, env: dict | None = None) -> None:
    if pipeline not in PIPELINES:
        raise ValueError(f"unknown pipeline '{pipeline}'")
    script = str(Path(scripts_dir, PIPELINES[pipeline]))
    subprocess.Popen(
        ["bash", script],
        env=env or os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
