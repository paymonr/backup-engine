# app/gui/s3_rules.py — the GUI side of S3 rules (spec 2026-09-23): apply the rules
# when jobs change or setup finishes (never failing the caller), and the Setup/Board
# status built from state files (no AWS on render).
from __future__ import annotations

from ..engine import lifecycle
from . import config_io

_WHY = {"role": "couldn't use the bucket-admin role", "aws": "AWS refused the change",
        "unsupported": "this storage doesn't support S3 rules"}


def job_buckets(cfg, job: dict) -> list[str]:
    if job.get("dedicated") and job.get("bucket"):
        return [job["bucket"]]
    return [config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()]


def apply_for(cfg, buckets: list[str]) -> list[tuple[str, str]]:
    """Bring each bucket's app rules in step with the jobs. Returns (category, message)
    flashes; never raises — a job save or setup must not fail because of S3 rules."""
    msgs: list[tuple[str, str]] = []
    for bucket in [b for b in buckets if b]:
        try:
            res = lifecycle.sync({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}, bucket)
        except lifecycle.LifecycleError as e:
            if e.kind != "not_managed":
                msgs.append(("warning", f"Saved, but S3 rules couldn't be updated ({_WHY.get(e.kind, e.kind)}) "
                                        "— Setup → S3 rules shows the details."))
            continue
        except Exception:                                  # noqa: BLE001 — never fail the caller
            msgs.append(("warning", "Saved, but S3 rules couldn't be updated — Setup → S3 rules shows the details."))
            continue
        if res.changed and res.lines:
            shown = res.lines[:3] + (["…"] if len(res.lines) > 3 else [])
            msgs.append(("success", "S3 rules updated: " + "; ".join(shown)))
    return msgs
