# app/gui/runner.py — the ONLY subprocess launcher / cache reader.
from __future__ import annotations
from pathlib import Path
import json
import os
import subprocess

def read_state(cache_dir: str, name: str) -> dict | None:
    p = Path(cache_dir, "state", f"{name}.json")
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

def trigger_job(scripts_dir: str, name: str, env: dict | None = None) -> None:
    subprocess.Popen(
        ["bash", str(Path(scripts_dir, "backup-job.sh")), name],
        env=env or os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
