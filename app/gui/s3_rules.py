# app/gui/s3_rules.py — the GUI side of S3 rules (spec 2026-09-23): apply the rules
# when jobs change or setup finishes (never failing the caller), and the Setup/Board
# status built from state files (no AWS on render).
from __future__ import annotations

from ..engine import lifecycle
from . import config_io, permissions

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


# --- status (state files only — safe on any GET) ---------------------------------------

def _buckets(cfg) -> list[str]:
    from . import jobs_io
    base = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()
    return lifecycle.buckets_for(base, jobs_io.load(cfg["CONFIG_DIR"]))


def _alarm(status: dict, buckets: list[str]) -> dict | None:
    alarms = [(status.get(b) or {}).get("alarm") for b in buckets]
    alarms = [a for a in alarms if a]
    if not alarms:
        return None
    return next((a for a in alarms if a.get("kind") == "not_restored"), alarms[0])


def setup_row(cfg) -> dict | None:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not config_io.is_provisioned(config_dir):
        return None
    row = {"code": "s3_rules", "verified_at": None, "fix_label": None, "fix_url": "/setup"}
    if not permissions.feature_available(config_dir, "s3-rules"):
        row.update(state="warn", sentence="Needs the AWS permissions update", fix_url="/setup/permissions")
        return row
    buckets = _buckets(cfg)
    status = lifecycle.load_status(cache)
    alarm = _alarm(status, buckets)
    if alarm:
        word = "NOT restored" if alarm.get("kind") == "not_restored" else "restored"
        row.update(state="fail", blocker=True, verified_at=alarm.get("at"),
                   sentence=f"S3 rules were changed outside backup-engine — {word}")
        return row
    entries = [status.get(b) or {} for b in buckets]
    states = [e.get("state") for e in entries]
    checked = [e.get("checked_at") for e in entries if e.get("checked_at")]
    row["verified_at"] = min(checked) if checked else None
    if "unsupported" in states:
        row.update(state="warn", sentence="This storage doesn't support S3 rules — Plain copy keeps all old versions")
    elif "error" in states:
        row.update(state="warn", sentence="S3 rules couldn't be checked — try Check now")
    elif None in states:
        row.update(state="warn", sentence="Not checked yet")
    else:
        row.update(state="ok", sentence="Your jobs' S3 rules are in place")
    return row


def needs_you_row(cfg) -> dict | None:
    if not config_io.is_provisioned(cfg["CONFIG_DIR"]):
        return None
    alarm = _alarm(lifecycle.load_status(cfg["CACHE_DIR"]), _buckets(cfg))
    if not alarm:
        return None
    restored = alarm.get("kind") != "not_restored"
    text = ("They were put back the way your jobs need them." if restored else
            "backup-engine couldn't put them back — check the AWS console and the Activity entry.")
    word = "restored" if restored else "NOT restored"
    return {"level": "blocker", "code": "s3-rules-tampered", "job": None,
            "strong": "S3 rules were changed outside backup-engine.",
            "text": f"{text} ({word})",
            "fix": {"label": "Review", "href": "/setup"}}


def check_all(cfg) -> list[tuple[str, str]]:
    ctx = {"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}
    try:
        lifecycle.sync_all(ctx)
    except lifecycle.LifecycleError as e:
        if e.kind == "not_managed":
            return [("warning", "S3 rules need the AWS permissions update first.")]
    except Exception:                                        # noqa: BLE001
        pass
    states = [lifecycle.check(ctx, b) for b in _buckets(cfg)]
    if all(s == "ok" for s in states):
        return [("success", "S3 rules checked — all in place.")]
    return [("warning", "S3 rules checked — see Setup for what needs attention.")]
