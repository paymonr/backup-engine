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
        if res.state == "restored":
            msgs.append(("warning", "S3 rules had been changed outside backup-engine — they're back the way "
                                    "your jobs need them. Setup shows the details."))
        elif res.state == "console_rule":
            msgs.append(("warning", "A new S3 rule could delete or move backups — backup-engine left it in "
                                    "place. Setup shows the details."))
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
    """Every bucket's open alarm as one (most severe first, every console rule named)."""
    return lifecycle.merge_alarms([(status.get(b) or {}).get("alarm") for b in buckets])


_CONSOLE = "A new S3 rule could delete or move backups"


def _sentence(alarm: dict) -> str:
    rules = ", ".join(alarm.get("rules") or [])
    if alarm.get("kind") == "console_rule":
        return f"{_CONSOLE}: {rules}"
    word = "NOT restored" if alarm.get("kind") == "not_restored" else "restored"
    tail = f"; a new S3 rule could delete or move backups: {rules}" if rules else ""
    return f"S3 rules were changed outside backup-engine — {word}{tail}"


def _pending(cfg, buckets: list[str]) -> bool:
    """A bucket whose last-applied app rules aren't what the jobs want now (e.g. a job
    save whose S3 write failed). State files + jobs only — no AWS."""
    from . import jobs_io
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    try:
        base = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
        jobs, settings = jobs_io.load(config_dir), lifecycle.load_settings(config_dir)
        for b in buckets:
            applied = lifecycle.load_applied(cache, b)
            if applied is not None and lifecycle.app_rules_differ(
                    applied, lifecycle.desired_rules(b, base, jobs, settings)):
                return True
    except Exception:                                        # noqa: BLE001 — a GET never 500s on this
        return False
    return False


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
        row.update(state="fail", blocker=True, verified_at=alarm.get("at"), sentence=_sentence(alarm),
                   alarm_seen=alarm.get("latest") or alarm.get("at"))
        return row
    entries = [status.get(b) or {} for b in buckets]
    states = [e.get("state") for e in entries]
    checked = [e.get("checked_at") for e in entries if e.get("checked_at")]
    row["verified_at"] = min(checked) if checked else None
    if "not_restored" in states:
        # The alarm was acknowledged (popped), but the bucket's rules are still not
        # what the jobs need — an acknowledged not_restored must not read as "ok"
        # until the next successful Check now/backup run fixes it (or re-alarms).
        row.update(state="warn", sentence="S3 rules still differ from what your jobs need — try Check now")
    elif _pending(cfg, buckets):
        row.update(state="warn", sentence="Your latest job settings haven't reached S3 yet — try Check now")
    elif "unsupported" in states:
        row.update(state="warn", sentence="This storage doesn't support S3 rules — Plain copy keeps all old versions")
    elif "error" in states:
        row.update(state="warn", sentence="S3 rules couldn't be checked — try Check now")
    elif None in states:
        row.update(state="warn", sentence="Not checked yet")
    else:
        row.update(state="ok", sentence="Your jobs' S3 rules are in place")
    return row


def open_alarm(cfg) -> dict | None:
    """Every bucket's open alarm as one (state files only)."""
    return _alarm(lifecycle.load_status(cfg["CACHE_DIR"]), _buckets(cfg))


def needs_you_row(cfg) -> dict | None:
    if not config_io.is_provisioned(cfg["CONFIG_DIR"]):
        return None
    alarm = _alarm(lifecycle.load_status(cfg["CACHE_DIR"]), _buckets(cfg))
    if not alarm:
        return None
    rules = alarm.get("rules") or []
    named = (f"The rule {rules[0]} was added or changed outside backup-engine, which left it in place"
             if len(rules) == 1 else
             f"The rules {', '.join(rules)} were added or changed outside backup-engine, which left them in place") \
        + " — check the AWS console."
    if alarm.get("kind") == "console_rule":
        return {"level": "blocker", "code": "s3-rules-console-rule", "job": None,
                "strong": f"{_CONSOLE}.", "text": named,
                "fix": {"label": "Review", "href": "/setup"}}
    restored = alarm.get("kind") != "not_restored"
    text = ("They were put back the way your jobs need them." if restored else
            "backup-engine couldn't put them back — check the AWS console and the Activity entry.")
    word = "restored" if restored else "NOT restored"
    return {"level": "blocker", "code": "s3-rules-tampered", "job": None,
            "strong": "S3 rules were changed outside backup-engine.",
            "text": f"{text} ({word})" + (f" {_CONSOLE}: {', '.join(rules)}." if rules else ""),
            "fix": {"label": "Review", "href": "/setup"}}


_ATTENTION = ("warning", "S3 rules checked — see Setup for what needs attention.")


def check_all(cfg) -> list[tuple[str, str]]:
    """Check now: the engine check on every bucket, as a manual run (it applies what the
    jobs want and alarms drift itself). Never raises -- the route must not 500;
    anything unexpected is a generic warning and the state files carry the detail."""
    ctx = {"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}
    try:
        buckets = _buckets(cfg)
    except Exception:                                        # noqa: BLE001
        return [_ATTENTION]
    states = []
    for b in buckets:
        try:
            states.append(lifecycle.check(ctx, b, trigger="manual"))
        except Exception:                                    # noqa: BLE001
            states.append("error")
    if "not_managed" in states:
        return [("warning", "S3 rules need the AWS permissions update first.")]
    try:
        open_alarm = _alarm(lifecycle.load_status(cfg["CACHE_DIR"]), buckets)
    except Exception:                                        # noqa: BLE001
        open_alarm = True
    if states and all(s == "ok" for s in states) and not open_alarm:
        return [("success", "S3 rules checked — all in place.")]
    return [_ATTENTION]
