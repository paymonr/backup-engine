# app/engine/progress.py — live progress for a RUNNING job, read from the engine's
# own output that the runner ALREADY tees to $CACHE_DIR/state during a run:
#   restic (versioned)        -> <job>-last.jsonl   newline-delimited --json; last "status"
#   rclone (archive)          -> <job>-rclone.log   a --stats block every 30s
#   vfiles (versioned-files)  -> no machine-readable progress -> indeterminate
#
# Nothing new is captured; we only read the TAIL of a file that is already being
# written. This module never raises: any problem yields an indeterminate
# {"running": True, "percent": None}, so the UI is never worse than today's bar.
from __future__ import annotations

import json
import re
from pathlib import Path

from . import runs

_TAIL_BYTES = 16384          # restic status lines are ~120 B; this holds plenty


def _tail_text(path: Path, n: int = _TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > n:
                fh.seek(size - n)
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _eta_seconds(percent, bytes_done, bytes_total, seconds_elapsed):
    """Prefer a byte-rate estimate; fall back to a percent-based one; else None."""
    if bytes_done and bytes_total and seconds_elapsed and bytes_done > 0 and bytes_total > bytes_done:
        rate = bytes_done / seconds_elapsed
        if rate > 0:
            return int((bytes_total - bytes_done) / rate)
    if percent is not None and seconds_elapsed and 0 < percent < 100:
        return int(seconds_elapsed * (100 - percent) / percent)
    if percent is not None and percent >= 100:
        return 0
    return None


# --- restic (versioned) ----------------------------------------------------

def parse_restic_status(text: str) -> dict | None:
    """The LAST `status` line of a restic --json stream -> normalized progress, or None
    when there is no parseable status line (e.g. only a summary, or an empty file)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if '"message_type":"status"' not in line and '"message_type": "status"' not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict) or o.get("message_type") != "status":
            continue
        pd = _num(o.get("percent_done"))
        percent = round(pd * 100, 1) if pd is not None else None
        bd, bt = _num(o.get("bytes_done")), _num(o.get("total_bytes"))
        se = _num(o.get("seconds_elapsed"))
        return {
            "engine": "restic",
            "percent": percent,
            "bytes_done": bd,
            "bytes_total": bt,
            "files_done": _num(o.get("files_done")),
            "files_total": _num(o.get("total_files")),
            "seconds_elapsed": int(se) if se is not None else None,
            "eta_seconds": _eta_seconds(percent, bd, bt, se),
        }
    return None


# --- rclone (archive) — best-effort (no archive jobs yet) ------------------

_UNIT = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
         "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4, "Bytes": 1}
# rclone --stats bytes line: "Transferred:   \t 1.234 GiB / 5.678 GiB, 21%, 4 MiB/s, ETA 6m1s"
_RCLONE_XFER = re.compile(
    r"Transferred:\s+([\d.]+)\s*(\w+)\s*/\s*([\d.]+)\s*(\w+),\s*(\d+)%")
_RCLONE_ETA = re.compile(r"ETA\s+([\dhms.]+)")


def _to_bytes(val: str, unit: str):
    try:
        return int(float(val) * _UNIT[unit])
    except (ValueError, KeyError):
        return None


def _eta_to_seconds(s: str):
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", s)
    if not m or not any(m.groups()):
        return None
    h, mn, sec = m.group(1), m.group(2), m.group(3)
    return int((int(h or 0) * 3600) + (int(mn or 0) * 60) + float(sec or 0))


def parse_rclone_stats(text: str) -> dict | None:
    """The LAST rclone --stats block's transfer line -> normalized progress, or None."""
    xfers = list(_RCLONE_XFER.finditer(text))
    if not xfers:
        return None
    m = xfers[-1]
    bd = _to_bytes(m.group(1), m.group(2))
    bt = _to_bytes(m.group(3), m.group(4))
    percent = float(m.group(5))
    tail = text[m.end():]
    eta_m = _RCLONE_ETA.search(tail.splitlines()[0] if tail else "")
    eta = _eta_to_seconds(eta_m.group(1)) if eta_m else _eta_seconds(percent, bd, bt, None)
    return {
        "engine": "rclone", "percent": percent, "bytes_done": bd, "bytes_total": bt,
        "files_done": None, "files_total": None, "seconds_elapsed": None, "eta_seconds": eta,
    }


# --- the reader the route calls -------------------------------------------

_IDLE = {"running": False}


def read_progress(cache_dir, job, job_type) -> dict:
    """Live progress for `job` if a run is active, else {"running": False}.

    `job_type` picks the parser (versioned->restic, archive->rclone, else indeterminate).
    A running run with no parseable progress returns running:True/percent:None so the UI
    falls back to the indeterminate bar it shows today."""
    rec = runs.active_run(cache_dir, job)
    if rec is None:
        return dict(_IDLE)
    out = {"running": True, "kind": rec.kind, "engine": None, "percent": None,
           "bytes_done": None, "bytes_total": None, "files_done": None,
           "files_total": None, "seconds_elapsed": None, "eta_seconds": None}
    state = Path(cache_dir, "state")
    parsed = None
    if job_type == "versioned":
        out["engine"] = "restic"
        parsed = parse_restic_status(_tail_text(state / f"{job}-last.jsonl"))
    elif job_type == "archive":
        out["engine"] = "rclone"
        parsed = parse_rclone_stats(_tail_text(state / f"{job}-rclone.log"))
    elif job_type == "versioned-files":
        out["engine"] = "vfiles"
    if parsed:
        out.update(parsed)
    return out
