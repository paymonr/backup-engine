# app/gui/s3_rules.py — the GUI side of S3 rules (spec 2026-09-23): apply the rules
# when jobs change or setup finishes (never failing the caller), and the Setup/Board
# status built from state files (no AWS on render).
from __future__ import annotations

import math

from ..engine import lifecycle, storage_summary
from . import config_io, permissions, vocab

_WHY = {"role": "couldn't use the bucket-admin role", "aws": "AWS refused the change",
        "unsupported": "this storage doesn't support S3 rules",
        "config": "a settings file couldn't be read"}


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
        if res.waiting:
            where = ", ".join("versioning" if c.folder is None else (c.folder or "whole bucket")
                              for c in res.waiting)
            msgs.append(("warning", f"S3 keeps the current rule for {where} — a change that keeps less "
                                    "waits for your confirmation in Setup → S3 rules."))
        # A waiting change is already its own warning above -- don't repeat it in the
        # success flash too (fix round 1, Minor).
        applied_lines = [ln for ln in res.lines if not ln.startswith("Waiting for your confirmation")]
        if res.changed and applied_lines:
            shown = applied_lines[:3] + (["…"] if len(applied_lines) > 3 else [])
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


def _outstanding(cfg, buckets: list[str]) -> tuple[bool, int]:
    """(any bucket not yet given what the jobs want, how many keeps-less changes wait for the
    owner) -- state files + jobs only, never AWS (R-B3). Any error reads as (False, 0): a
    GET never 500s on this."""
    try:
        not_reached, waiting = False, 0
        for b in buckets:
            nr, w = lifecycle.outstanding({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}, b)
            not_reached = not_reached or nr
            waiting += len(w)
        return not_reached, waiting
    except Exception:                                        # noqa: BLE001
        return False, 0


def console_cap_words(cap: dict) -> str:
    """Owner words for a console rule that removes old versions sooner than the app's rule
    (lifecycle.console_caps; final fix wave I4) -- the Setup row's sentence. The job page and
    wizard render the same words from _console_cap.html with the numbers in mono spans."""
    if cap["newest"] and cap["days"] > 1:
        what = (f"keeps only the newest {cap['newest']} old versions of each file and removes the rest "
                f"after {cap['days']} days")
    elif cap["newest"]:
        what = f"keeps only the newest {cap['newest']} old versions of each file"
    else:
        what = f"removes old versions after {cap['days']} days"
    return (f"A rule you added in the AWS console ({cap['id']}) {what} — delete it there to keep the "
            "longer history")


def _folder_cap(cfg, bucket: str, folder: str, want) -> dict | None:
    """The first console rule (from the last read of the bucket's rules, live.json -- never
    AWS) that removes `folder`'s old versions sooner than the app's own rule for it wants."""
    live = lifecycle.load_live(cfg["CACHE_DIR"], bucket)
    if not live:
        return None
    caps = lifecycle.console_caps(live["rules"], folder, want.rules.get(lifecycle.rule_id(folder)))
    return caps[0] if caps else None


def _console_cap(cfg, buckets: list[str]) -> dict | None:
    """The first app folder, on any bucket, whose history a console rule cuts short (files
    only; any error reads as none -- a GET never 500s on this)."""
    from . import jobs_io
    try:
        base = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()
        jobs = jobs_io.load(cfg["CONFIG_DIR"])
        ctx = {"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}
        for b in buckets:
            _before, want = lifecycle.applied_view(ctx, b, jobs)
            for f in lifecycle.folders_for(b, base, jobs):
                cap = _folder_cap(cfg, b, f.folder, want)
                if cap:
                    return cap
    except Exception:                                        # noqa: BLE001
        return None
    return None


def setup_row(cfg) -> dict | None:
    return _setup_state(cfg)[0]


def _setup_state(cfg) -> tuple[dict | None, list[str], dict | None]:
    """(the Setup row, the buckets, their merged open alarm) -- computed ONCE and shared by
    setup_row() and screen() (parked P5: the screen used to recompute buckets/status/alarm)."""
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not config_io.is_provisioned(config_dir):
        return None, [], None
    row = {"code": "s3_rules", "verified_at": None, "fix_label": None, "fix_url": "/setup"}
    if not permissions.feature_available(config_dir, "s3-rules"):
        row.update(state="warn", sentence="Needs the AWS permissions update", fix_url="/setup/permissions")
        return row, [], None
    buckets = _buckets(cfg)
    status = lifecycle.load_status(cache)
    alarm = _alarm(status, buckets)
    if alarm:
        row.update(state="fail", blocker=True, verified_at=alarm.get("at"), sentence=_sentence(alarm),
                   alarm_seen=alarm.get("latest") or alarm.get("at"))
        return row, buckets, alarm
    entries = [status.get(b) or {} for b in buckets]
    states = [e.get("state") for e in entries]
    checked = [e.get("checked_at") for e in entries if e.get("checked_at")]
    row["verified_at"] = min(checked) if checked else None
    not_reached, waiting = _outstanding(cfg, buckets)
    # final fix wave I1: a pass that stopped on an unreadable jobs/settings file wrote nothing
    # -- say which file (not "try Check now", which would stop the same way), ahead of anything
    # computed from those files (a "waiting" count from an unreadable file would be invented).
    config = next((e.get("detail") for e in entries if e.get("state") == "error"
                   and e.get("detail") in lifecycle.CONFIG_ERRORS), None)
    if "not_restored" in states:
        # The alarm was acknowledged (popped), but the bucket's rules are still not
        # what the jobs need — an acknowledged not_restored must not read as "ok"
        # until the next successful Check now/backup run fixes it (or re-alarms).
        row.update(state="warn", sentence="S3 rules still differ from what your jobs need — try Check now")
    elif config:
        row.update(state="warn", sentence=config[0].upper() + config[1:])
    elif not_reached:
        row.update(state="warn", sentence="Your latest job settings haven't reached S3 yet — try Check now")
    elif waiting:
        row.update(state="warn", fix_url="/setup/storage",
                   sentence=f"{waiting} change{'' if waiting == 1 else 's'} waiting for your confirmation")
    elif cap := _console_cap(cfg, buckets):
        row.update(state="warn", fix_url="/setup/storage", sentence=console_cap_words(cap))
    elif "unsupported" in states:
        row.update(state="warn", sentence="This storage doesn't support S3 rules — Plain copy keeps all old versions")
    elif "error" in states:
        row.update(state="warn", sentence="S3 rules couldn't be checked — try Check now")
    elif None in states:
        row.update(state="warn", sentence="Not checked yet")
    else:
        row.update(state="ok", sentence="Your jobs' S3 rules are in place")
    return row, buckets, None


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


# --- the S3 rules screen (spec §5, layout A) — state files only, never AWS --------------------

def _human_time(iso) -> str | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return None


def console_view(rules: list[dict], folders) -> list[dict]:
    """Console rules, read-only (spec §1/§5): what each does, a danger flag for current-file
    expiry/moves, an overlap flag for old-version actions on an app folder."""
    out = []
    for key, r in lifecycle.console_rules(rules):
        on = r.get("Status") != "Disabled"
        exp = r.get("Expiration") if isinstance(r.get("Expiration"), dict) else {}
        danger = on and ("Days" in exp or "Date" in exp or bool(r.get("Transitions") or r.get("Transition")))
        noncurrent = on and any(k in r for k in ("NoncurrentVersionExpiration", "NoncurrentVersionTransitions",
                                                 "NoncurrentVersionTransition"))
        prefix = lifecycle.rule_prefix(r)
        out.append({"id": key, "where": lifecycle._where(r), "disabled": not on,
                    "words": lifecycle.describe(r).split(": ", 1)[-1],
                    "danger": danger,
                    "overlap": noncurrent and any(f.startswith(prefix) or prefix.startswith(f) for f in folders)})
    return out


def _bucket_view(cfg, bucket: str, base: str, jobs: list[dict], settings: dict) -> dict:
    cache = cfg["CACHE_DIR"]
    before, want = lifecycle.applied_view(cfg, bucket, jobs, settings)
    live = lifecycle.load_live(cache, bucket)
    # What S3 was last given. Before the first apply (final fix wave M10): what S3 does now, when
    # a check has read it (its app/legacy/console rules mapped to folders) -- else what the jobs
    # want, labelled "after the first check" so it never reads as already in force.
    if before is not None:
        shown, first_pending = before.rules, False
    elif live:
        shown, first_pending = lifecycle.baseline_from_live(live["rules"], want).rules, False
    else:
        shown, first_pending = want.rules, True
    waiting = ({c.rule_id: c for c in lifecycle.classify(before, want) if c.kind == lifecycle.KEEPS_LESS}
               if before is not None else {})
    types = {j.get("name"): j.get("type") for j in jobs}
    bset = lifecycle.bucket_settings(settings, bucket)
    rows = []
    for f in lifecycle.folders_for(bucket, base, jobs):
        rid = lifecycle.rule_id(f.folder)
        rule = shown.get(rid)
        d, n = lifecycle.expiry(rule)
        w = waiting.get(rid)
        # Task 18b: the tier column shows what the LAST APPLIED rule (`rule`) actually
        # does; `tier_off` is a tier the owner set (storage.json) but that dropped out of
        # `want` -- e.g. it couldn't move anything before removal (fix round 1, M3b/M5) --
        # so the row can say it's set but not doing anything, rather than silently hiding it.
        set_tier = lifecycle.folder_tier(bset, f.folder)
        rows.append({"key": f"{bucket}|{f.folder}", "folder": f.folder, "where": f.folder or "whole bucket",
                     "kind": f.kind, "jobs": list(f.jobs), "type": vocab.TYPE_NAMES.get(types.get(f.jobs[0]), ""),
                     "keeps": {"days": None if d == float("inf") else d, "newer": n or None},
                     "waiting": w.words.split(": ", 1)[-1] if w else None, "note": f.note,
                     "first_pending": first_pending,
                     "tier": _tier_view(rule),
                     "tier_off": (_tier_off_words(set_tier, lifecycle._folder_storage_class(jobs, f.jobs))
                                  if set_tier and lifecycle.tier_of(want.rules.get(rid)) is None else None)})
    live_versioning = (live or {}).get("versioning")
    # fix round 1, Minor: neutral chip styling when versioning is suspended/never-on BY THE
    # OWNER'S CHOICE (it matches the current intent and nothing is waiting) -- warning only
    # when it differs from intent (not yet reconciled, or tampered) or a suspend is waiting
    # (the waiting box below already offers "Review and confirm…", so the link is redundant).
    versioning_waiting = "versioning" in waiting
    versioning_ok = (live_versioning is not None
                     and lifecycle.versioning_matches(live_versioning, want.versioning)
                     and not versioning_waiting)
    return {"name": bucket, "dedicated": bucket != base, "applied": before is not None, "rows": rows,
            "console": console_view(live["rules"], want.folders) if live else [],
            "live_read_at": _human_time((live or {}).get("read_at")),
            "versioning": live_versioning, "versioning_ok": versioning_ok,
            "versioning_waiting": versioning_waiting,
            "housekeeping": {"key": f"{bucket}|*", "abort_days": bset["abort_uploads_days"],
                             "markers": bset["delete_marker_cleanup"]},
            "waiting": [c.words for c in waiting.values()]}


def _tier_off_words(tier: dict, storage_class: str) -> str:
    """Why a tier the owner set does nothing (final fix wave M3): the folder's files already
    upload as that class or colder, or S3 removes the old versions before they'd move."""
    if lifecycle.tier_rank(tier["class"]) <= lifecycle.tier_rank(storage_class):
        return (f"Off — files here already upload as {lifecycle._storage_words(storage_class)}, "
                "so this tier moves nothing.")
    return "Off — S3 removes these old versions before they would move."


def screen(cfg) -> dict:
    """The S3 rules screen's view model (spec §5)."""
    from . import jobs_io
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    v = {"provisioned": config_io.is_provisioned(config_dir), "managed": False, "buckets": [],
         "status": None, "alarm": None, "alarm_seen": None}
    if not v["provisioned"]:
        return v
    v["managed"] = lifecycle.managed(config_dir)
    if not v["managed"]:
        return v
    ctx = {"CONFIG_DIR": config_dir, "CACHE_DIR": cache}
    base = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
    jobs, settings = jobs_io.load(config_dir), lifecycle.load_settings(config_dir)
    row, buckets, alarm = _setup_state(cfg)             # P5: the Setup row's own computation
    row = row or {}
    v["alarm"] = alarm
    v["alarm_seen"] = (alarm or {}).get("latest") or (alarm or {}).get("at")
    v["status"] = {"level": "blocker" if alarm else ("ok" if row.get("state") == "ok" else "warn"),
                   "sentence": row.get("sentence", ""), "checked_at": row.get("verified_at"),
                   "checked_human": _human_time(row.get("verified_at"))}
    v["buckets"] = [_safe_bucket_view(ctx, b, base, jobs, settings) for b in buckets]
    if not lifecycle.config_readable(config_dir):
        # I1: the files these waiting items would be computed from can't be read -- the status
        # line says which one; never list changes invented from the fail-safe defaults
        for b in v["buckets"]:
            b["waiting"], b["versioning_waiting"] = [], False
            for r in b.get("rows") or []:
                r["waiting"] = None
    return v


def _safe_bucket_view(ctx, bucket: str, base: str, jobs: list[dict], settings: dict) -> dict:
    """final fix wave M11: one bucket whose state files can't be made sense of (a hand-edited
    applied.json, say) shows a warning card -- the screen never 500s over it."""
    try:
        return _bucket_view(ctx, bucket, base, jobs, settings)
    except Exception:                                        # noqa: BLE001 — a GET never 500s on this
        return {"name": bucket, "dedicated": bucket != base, "broken": True}


# --- the side editor, impact line and preview (spec §3, §5 layout B) -----------------------------

MAX_DAYS = 36500                                # fix round 1: an upper bound on every day count here
_DAYS_MSG = f"Enter a whole number of days, 1 to {MAX_DAYS}."
_ABORT_MSG = f"Clear abandoned uploads after 1 to {MAX_DAYS} days."
_COUNT_MSG = f"S3 can keep 1 to {lifecycle.MAX_NEWER} old versions per file."


def why(kind: str) -> str:
    return _WHY.get(kind, kind)


def _human_bytes(b) -> str:
    for lim, unit, dec in ((2 ** 40, "TB", 2), (2 ** 30, "GB", 2), (2 ** 20, "MB", 1)):
        if b >= lim:
            return f"{b / lim:.{dec}f} {unit}"
    return f"{int(b)} B"


def _whole(v, msg: str, *, max_: int | None = None) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(msg)
    if n < 1 or (max_ is not None and n > max_):
        raise ValueError(msg)
    return n


def refresh_ok(cfg) -> bool:
    """Whether Refresh now may be offered: S3 rules managed here and not a custom S3 endpoint --
    /setup/storage/refresh (Task 12) refuses both with a warning otherwise, so the button (and
    its wording) is never offered where it would just bounce (fix round 1; shared by the route,
    the editor's and the preview's initial/live impact text)."""
    return (lifecycle.managed(cfg["CONFIG_DIR"])
           and not config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_ENDPOINT", "").strip())


def _context(cfg):
    from . import jobs_io
    config_dir = cfg["CONFIG_DIR"]
    base = config_io.read_backup_env(config_dir).get("S3_BUCKET", "").strip()
    return base, jobs_io.load(config_dir), lifecycle.load_settings(config_dir)


def _plain_days(r: dict) -> int:
    """The days value the plain-copy editor shows: what S3 actually applies (plain_rule's own
    max(1, ...) clamp), not the raw stored value -- a stored `days: 0` must show as 1, the
    figure S3 really uses, not the "no value at all" placeholder (fix round 1, Minor)."""
    days = r.get("days")
    return 180 if days is None else max(1, int(days))


def editor(cfg, key: str | None, *, error: str | None = None, form=None) -> dict | None:
    """The side editor for one row (layout B): only the fields that row has. `form` (fix round
    1, I2/Minor), when given, is the just-submitted, still-invalid POST -- its own entries and
    radio choice are shown instead of the stored values, so a form error never silently reverts
    what the owner typed."""
    if not key or "|" not in key:
        return None
    base, jobs, settings = _context(cfg)
    bucket, _, folder = key.partition("|")
    if bucket not in lifecycle.buckets_for(base, jobs):
        return None
    bset = lifecycle.bucket_settings(settings, bucket)
    ed = {"key": key, "bucket": bucket, "folder": folder, "error": error}
    if folder == "*":
        intent = lifecycle.versioning_intent(bucket, base, jobs, settings,
                                             base_versioned=config_io.base_bucket_versioned(cfg["CONFIG_DIR"]))
        if form is not None:
            abort_days, markers = form.get("abort_days", bset["abort_uploads_days"]), bool(form.get("markers"))
            versioning = form.get("versioning", intent)
        else:
            abort_days, markers = bset["abort_uploads_days"], bset["delete_marker_cleanup"]
            versioning = intent
        ed.update(kind="bucket", title="Bucket-wide", where="whole bucket",
                  abort_days=abort_days, markers=markers, versioning=versioning)
        return ed
    f = next((x for x in lifecycle.folders_for(bucket, base, jobs) if x.folder == folder), None)
    if f is None:
        return None
    summary = storage_summary.load(cfg["CACHE_DIR"], bucket, folder)
    ed.update(where=folder or "whole bucket", jobs=list(f.jobs),
              summary_at=_human_time((summary or {}).get("scanned_at")))
    if f.kind == "plain":
        r = f.retention or {"type": "keep_all"}
        stored_keep = {"keep_all": "all", "days": "days"}.get(r.get("type"), "both" if r.get("days") else "count")
        stored_days, stored_count = _plain_days(r), r.get("count") or 10
        if form is not None:
            keep = form.get("keep", stored_keep)
            days, count = form.get("days", stored_days), form.get("count", stored_count)
        else:
            keep, days, count = stored_keep, stored_days, stored_count
        ed.update(kind="plain", title=f"{f.jobs[0]} · Plain copy", job=f.jobs[0], keep=keep,
                  days=days, count=count)
        args = {"key": key, "keep": keep, "days": str(days), "count": str(count)}
    else:
        stored_undo = lifecycle.undo_days(bset, folder)
        undo_days = form.get("undo_days", stored_undo) if form is not None else stored_undo
        ed.update(kind="undo", title=f"{', '.join(f.jobs)} · undo window", undo_days=undo_days)
        args = {"key": key, "undo_days": str(undo_days)}
    # Task 18b: the cheaper-tier fields, for both kinds -- fix round 1, I2 applies here
    # too: a just-submitted, still-invalid `form`'s own tier_class/tier_days are shown
    # instead of the stored ones, so a tier form error never silently reverts what the
    # owner typed.
    stored_tier = lifecycle.folder_tier(bset, folder)
    if form is not None:
        tier_class = form.get("tier_class", (stored_tier or {}).get("class", ""))
        tier_days = form.get("tier_days", (stored_tier or {}).get("after_days", 30))
    else:
        tier_class, tier_days = (stored_tier or {}).get("class", ""), (stored_tier or {}).get("after_days", 30)
    ed.update(tier_class=tier_class, tier_days=tier_days,
              tier_classes=[(c, f"{lifecycle._storage_words(c)} — {_TIER_HINT[c]}")
                            for c in lifecycle.TIER_CLASSES])
    args["tier_class"], args["tier_days"] = tier_class, str(tier_days)
    # fix round 1, I1: the true impact line on the very first render (impact.json's JS then
    # keeps it live as the owner changes values) -- the same function, the row's own values.
    ed["impact"] = impact_line(cfg, args)
    return ed


def retention_from_editor(form) -> dict:
    """The Plain copy editor's keep choice as a job history setting (spec §1 shapes)."""
    keep = form.get("keep")
    if keep == "all":
        return {"type": "keep_all"}
    if keep == "days":
        return {"type": "days", "days": _whole(form.get("days"), _DAYS_MSG, max_=MAX_DAYS)}
    if keep in ("count", "both"):
        n = _whole(form.get("count"), _COUNT_MSG, max_=lifecycle.MAX_NEWER)
        r = {"type": "count", "count": n}
        if keep == "both":
            r["days"] = _whole(form.get("days"), _DAYS_MSG, max_=MAX_DAYS)
        return r
    raise ValueError("Pick how long S3 keeps old versions.")


def _bucket_entry(settings: dict, bucket: str) -> dict:
    b = settings["buckets"].get(bucket)
    b = dict(b) if isinstance(b, dict) else {}
    settings["buckets"][bucket] = b
    return b


def _folder_entry(settings: dict, bucket: str, folder: str) -> dict:
    b = _bucket_entry(settings, bucket)
    fs = dict(b["folders"]) if isinstance(b.get("folders"), dict) else {}
    b["folders"] = fs
    e = dict(fs[folder]) if isinstance(fs.get(folder), dict) else {}
    fs[folder] = e
    return e


# fix round 1, I1: the editor's <select> options in owner words (lifecycle._storage_words),
# never the raw AWS constant -- the constant stays out of the dropdown's visible text entirely
# (it's already on the <option value="..."> attribute, which the vocabulary lint never reads).
_TIER_HINT = {"GLACIER_IR": "instant reads, archive price", "DEEP_ARCHIVE": "restores take hours"}


def _tier_from_form(form) -> dict | None:
    """The editor's tier_class/tier_days fields as a storage.json tier entry, or None
    when "None" is picked (tier_days is then ignored -- fix round: no client min on it,
    so a hidden-invalid number can never silently block the submit; the server is the
    gate, via tier_error below)."""
    cls = (form.get("tier_class") or "").strip()
    if not cls:
        return None
    try:
        days = int(str(form.get("tier_days") or "").strip())
    except ValueError:
        days = 0
    return {"class": cls, "after_days": days}


def _tier_view(rule) -> dict | None:
    t = lifecycle.tier_of(rule)
    return {"class": t[0], "label": vocab.tier_label(t[0]), "days": t[1]} if t else None


def _edit_from_form(cfg, form):
    """(base, jobs, settings, bucket, edit) -- the shared computation edit_from_form and
    impact_line both need, loading jobs.json/storage.json once (fix round 1, Minor: impact_line
    used to load them a second time via lifecycle.edited's own jobs_io.load/load_settings call)."""
    base, jobs, settings = _context(cfg)
    bucket, _, folder = (form.get("key") or "").partition("|")
    if bucket not in lifecycle.buckets_for(base, jobs):
        raise ValueError("That bucket isn't one of backup-engine's.")
    if form.get("what") == "waiting":
        return base, jobs, settings, bucket, {"kind": "confirm"}
    if folder == "*":
        b = _bucket_entry(settings, bucket)
        b["abort_uploads_days"] = _whole(form.get("abort_days"), _ABORT_MSG, max_=MAX_DAYS)
        b["delete_marker_cleanup"] = bool(form.get("markers"))
        v = form.get("versioning")
        if v is not None:
            if v not in lifecycle.VERSIONING_STATES:
                raise ValueError("Pick versioning on or suspended.")
            b["versioning"] = v
        return base, jobs, settings, bucket, {"kind": "settings", "settings": settings}
    f = next((x for x in lifecycle.folders_for(bucket, base, jobs) if x.folder == folder), None)
    if f is None:
        raise ValueError("That folder isn't one of backup-engine's.")
    # Task 18b: a cheaper tier lives in storage.json for BOTH kinds (plain and undo) --
    # written into the same folder entry as undo_days, and validated (tier_error) against
    # the rule this edit is about to produce, with the folder's own upload storage class
    # (Task 18a final: tier_error refuses a tier no colder than that).
    tier = _tier_from_form(form)
    entry = _folder_entry(settings, bucket, folder)
    if tier is None:
        entry.pop("tier", None)
    else:
        entry["tier"] = tier
    storage_class = lifecycle._folder_storage_class(jobs, f.jobs)
    if f.kind == "undo":
        entry["undo_days"] = _whole(form.get("undo_days"), _DAYS_MSG, max_=MAX_DAYS)
        err = lifecycle.tier_error(tier, lifecycle.undo_rule(folder, entry["undo_days"]), storage_class)
        if err:
            raise ValueError(err)
        return base, jobs, settings, bucket, {"kind": "settings", "settings": settings}
    retention = retention_from_editor(form)
    err = lifecycle.tier_error(tier, lifecycle.plain_rule(folder, retention), storage_class)
    if err:
        raise ValueError(err)
    job = next(j for j in jobs if j.get("name") == f.jobs[0])
    # A Plain copy edit now carries `settings` too (the folder's tier lives there), not
    # just `job` -- edited()/_save_edit_unlocked/save_edit already handle both keys
    # together regardless of `kind` (fix round: this only needed the shape, not new code).
    return base, jobs, settings, bucket, {"kind": "job", "job": dict(job, retention=retention),
                                          "settings": settings}


def edit_from_form(cfg, form) -> tuple[str, dict]:
    """The editor's (or the waiting list's) POST as a lifecycle edit: (bucket, edit).
    Raises ValueError with owner words on a bad value -- nothing is previewed then."""
    *_, bucket, edit = _edit_from_form(cfg, form)
    return bucket, edit


def impact_line(cfg, args) -> dict:
    """The live impact line (GET, files only): what the editor's values would remove that S3
    keeps today, from the stored summary."""
    try:
        _base, jobs, settings, bucket, edit = _edit_from_form(cfg, args)
    except ValueError as e:
        return {"line": str(e)}
    folder = (args.get("key") or "").partition("|")[2]
    if folder == "*" or edit["kind"] == "confirm":
        return {"line": ""}
    try:
        jobs, settings = lifecycle.edited(jobs, settings, edit, source_root=cfg.get("SOURCE_ROOT"))
    except lifecycle.PreviewError as e:
        return {"line": e.message}
    ctx = {"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"]}
    before, want = lifecycle.applied_view(ctx, bucket, jobs, settings)
    if before is None:
        return {"line": "Not checked yet — press Check now first."}
    summary = storage_summary.load(cfg["CACHE_DIR"], bucket, folder)
    if summary is None:
        tail = " Refresh now for exact figures." if refresh_ok(cfg) else ""
        return {"line": f"No storage summary for this folder yet.{tail}"}
    rid = lifecycle.rule_id(folder)
    imp = storage_summary.impact(summary, before.rules.get(rid), want.rules.get(rid))
    when = _human_time(summary.get("scanned_at")) or "the last scan"
    if not imp["versions"]:
        line = f"Nothing S3 keeps today would be removed · as of {when}"
    else:
        line = (f"S3 would permanently delete about {imp['versions']:,} old versions "
                f"({_human_bytes(imp['bytes'])}) within about a day · as of {when}")
    return {"line": line, "versions": imp["versions"], "bytes": imp["bytes"]}


def damage_notes(change, kind: str | None, min_size: str | None = None) -> list[str]:
    """Spec §3's damage warnings for one keeps-less change, in words. `min_size` (Task 18a
    final: live.json's TransitionDefaultMinimumObjectSize) gates the small-object warning --
    it's only true when S3's default applies (the reading is S3's own default or unknown);
    a bucket the owner set to "varies_by_storage_class" moves small objects too, so claiming
    otherwise would be a straight lie."""
    if change.rule_id == "versioning":
        return ["Overwritten or deleted files in this bucket are gone immediately from now on; Plain copy "
                "history stops; existing old versions stay until their rule removes them."]
    if change.folder is None:
        return []
    bd, bn = lifecycle.expiry(change.before)
    ad, an = lifecycle.expiry(change.after)
    notes = []
    if kind == "undo" and ad != math.inf:
        was = lifecycle._days(int(bd)) if bd != math.inf else "being kept for good"
        notes.append(f"Data these jobs already deleted will be unrecoverable after {lifecycle._days(int(ad))} "
                     f"instead of {was}.")
    if an:
        notes.append(f"Files with more than {an} old versions lose the oldest ones.")
    elif bn and ad != math.inf:
        notes.append("The newest old versions of each file are no longer protected — they go by age like the rest.")
    t = lifecycle.tier_of(change.after)
    if t and lifecycle.tier_keeps_less(change.before, change.after):
        # fix round 1, I1: the owner-word map, never the raw AWS constant, in running prose
        # (the overview column and job page keep the constant in its own <code> chip instead --
        # this is the one spot that folds it into a sentence, so it's the one that must not).
        name = lifecycle._storage_words(t[0])
        notes.append(f"Old versions moved to {name} are charged for at least "
                     f"{lifecycle.TIER_MIN_STORAGE_DAYS.get(t[0], 0)} days, even if S3 removes them sooner.")
        if min_size != "varies_by_storage_class":
            notes.append("Old versions under 128 KB are not moved (S3's default).")
        notes.append("Getting an old version back from this tier takes hours and costs money."
                     if t[0] == "DEEP_ARCHIVE" else
                     "Reading an old version back from this tier costs a fee for every GB read.")
        # fix round 1, M6: the gate is per RULE, not per dimension -- a longer expiry bundled
        # into the same submit as a newly-active (or earlier) tier still waits as a whole,
        # because the row overall keeps less (the tier). Say so explicitly, or the owner reads
        # "keeps more" language nowhere and wonders why their longer retention didn't apply.
        if ad > bd or an > bn:
            notes.append("This row waits as a whole — the longer history applies once you "
                         "confirm the tier.")
    return notes


def _impact_view(imp: dict) -> dict:
    from datetime import datetime, timedelta
    scanned = imp.get("scanned_at")
    oldest = None
    try:
        if imp.get("oldest_age_days") is not None and scanned:
            at = datetime.fromisoformat(scanned.replace("Z", "+00:00")) - timedelta(days=imp["oldest_age_days"])
            oldest = at.strftime("%d %b")
    except ValueError:
        oldest = None
    return {"zero": imp["versions"] == 0, "versions": f"{imp['versions']:,}", "bytes": _human_bytes(imp["bytes"]),
            "oldest": oldest, "as_of": _human_time(scanned),
            "moved": ({"versions": f"{imp['moved']['versions']:,}", "bytes": _human_bytes(imp["moved"]["bytes"])}
                      if (imp.get("moved") or {}).get("versions") else None)}


def preview_view(cfg, pv, *, action: str = "/setup/storage/apply", hidden: dict | None = None,
                 cancel: str = "/setup/storage", error: str | None = None) -> dict:
    """The preview component's view model (spec §3): every change in words; for one that keeps
    less, what S3 would permanently delete and the damage warnings."""
    base, jobs, _settings = _context(cfg)
    kinds = {f.folder: f.kind for f in lifecycle.folders_for(pv.bucket, base, jobs)}
    # Task 18b: the small-object warning is only true when the bucket's own min-size
    # reading (Task 18a final, live.json) says so -- read once for the whole preview.
    min_size = (lifecycle.load_live(cfg["CACHE_DIR"], pv.bucket) or {}).get("min_size")
    rows = []
    removes = suspends = moves = False
    for c in pv.changes:
        less = c.kind == lifecycle.KEEPS_LESS
        imp = pv.impacts.get(c.rule_id) if less else None
        if less:
            # final fix wave M4: the typed-confirmation headline says what THIS preview does --
            # it only "permanently removes backup history" when an old-version expiry keeps less
            # AND the summary finds something to remove (or can't tell: none, or one too old).
            if c.rule_id == "versioning":
                suspends = True
            elif c.folder is not None:
                if _expiry_keeps_less(c.before, c.after) and (imp is None or imp["versions"] > 0
                                                              or not _impact_fresh(imp)):
                    removes = True
                if lifecycle.tier_keeps_less(c.before, c.after):
                    moves = True
        rows.append({"words": c.words, "less": less, "impact": _impact_view(imp) if imp else None,
                     "no_summary": less and c.folder is not None and imp is None,
                     "notes": damage_notes(c, kinds.get(c.folder), min_size) if less else [],
                     "refresh": ({"bucket": pv.bucket, "folder": c.folder}
                                 if less and c.folder is not None else None)})
    guard = [line for flag, line in (
        (removes, "This permanently removes backup history."),
        (suspends, "This stops S3 keeping old versions in this bucket from now on."),
        (moves, "This moves old versions to a cheaper tier — getting them back takes longer and costs money."),
    ) if flag]
    return {"bucket": pv.bucket, "token": pv.token, "needs_typed": pv.needs_typed, "rows": rows,
            "guard": " ".join(guard), "action": action, "hidden": dict(hidden or {}), "cancel": cancel,
            "error": error}


def _expiry_keeps_less(before, after) -> bool:
    """The old-version EXPIRY half of keeps-less (the tier half aside): `after` can remove an
    old version `before` keeps."""
    bd, bn = lifecycle.expiry(before)
    ad, an = lifecycle.expiry(after)
    return ad != math.inf and (ad < bd or an < bn)


def _impact_fresh(imp: dict) -> bool:
    return lifecycle._summary_fresh({"scanned_at": imp.get("scanned_at")})


# --- the wizard (R-B5) and the job page (spec §5) ----------------------------------------------

def history_gate(cfg, job: dict, *, run_now: bool = False) -> dict | None:
    """R-B5: when saving a Plain copy job would keep less history than S3 keeps today for ITS
    folder, the preview (view model, with a token) to show instead of saving. None = save as
    usual: it keeps more, S3 rules aren't managed, nothing was applied yet (the pass then
    holds it), or anything unexpected -- a save is never blocked by this. `run_now` (fix round
    1, Minor) carries "Save and run it now" through the preview -- its confirm route re-reads
    `hidden["run_now"]` and triggers the run after a successful confirm."""
    from . import jobs_io
    if job.get("type") != "archive":
        return None
    try:
        base, jobs, _settings = _context(cfg)
        bucket = job_buckets(cfg, job)[0]
        target = lifecycle.folder_of_job(base, [j for j in jobs if j.get("name") != job.get("name")] + [job],
                                         job.get("name"))
        # A reduced ctx like preview()'s other callers, but WITH SOURCE_ROOT: preview() ->
        # edited() -> jobs_io.validate() checks the source folder exists on disk, so without it
        # this would 404 the folder and (invalid) skip the gate instead of raising it.
        pv = lifecycle.preview({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"],
                                "SOURCE_ROOT": cfg.get("SOURCE_ROOT")}, bucket,
                               {"kind": "job", "job": job})
    except Exception:                                        # noqa: BLE001 — a save is never blocked by this
        return None
    if not pv.token:
        return None
    if target is None or not any(c.folder == target[1] for c in pv.keeps_less):
        lifecycle.discard_preview(cfg["CACHE_DIR"], pv.token)
        return None
    hidden = {"name": job["name"]}
    if run_now:
        hidden["run_now"] = "1"
    return preview_view(cfg, pv, action="/jobs/history/confirm", hidden=hidden,
                        cancel=f"/jobs/{job['name']}" if jobs_io.valid_name(job.get("name", "")) else "/")


def job_history(cfg, job: dict) -> dict | None:
    """The job page's "History in S3" line (spec §5) -- state files only. `versioning_off` (fix
    round 1, I2) takes priority over the rule figures: with versioning suspended (a bucket-wide
    choice, Task 16) or a dedicated bucket that was never versioned, S3 keeps nothing regardless
    of what any rule says, so showing the rule's days/count would be a straight lie."""
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not config_io.is_provisioned(config_dir):
        return None
    if not lifecycle.managed(config_dir):
        return {"state": "not_managed"}
    try:
        base, jobs, settings = _context(cfg)
        target = lifecycle.folder_of_job(base, jobs, job.get("name"))
        if target is None:
            return None
        bucket, folder = target
        kind = next(f.kind for f in lifecycle.folders_for(bucket, base, jobs) if f.folder == folder)
        ctx = {"CONFIG_DIR": config_dir, "CACHE_DIR": cache}
        before, want = lifecycle.applied_view(ctx, bucket, jobs, settings)
        if want.versioning != "on":
            return {"state": "versioning_off", "kind": kind}
        if (lifecycle.load_status(cache).get(bucket) or {}).get("state") == "unsupported":
            return {"state": "unsupported", "kind": kind}
        rule = (before.rules if before is not None else want.rules).get(lifecycle.rule_id(folder))
        d, n = lifecycle.expiry(rule)
        waiting = before is not None and any(c.folder == folder and c.kind == lifecycle.KEEPS_LESS
                                             for c in lifecycle.classify(before, want))
        return {"state": "ok", "kind": kind, "days": None if d == math.inf else d, "newer": n or None,
                "waiting": waiting, "applied": before is not None, "tier": _tier_view(rule),
                "console_cap": _folder_cap(cfg, bucket, folder, want)}
    except Exception:                                        # noqa: BLE001 — a GET never 500s on this
        return None


def wizard_copy(cfg, job: dict | None) -> dict:
    """The wizard's "Kept by S3"/undo-window note (spec §3, fix round 1 Minor): whether S3
    rules actually apply to this job's bucket (else a short honest note instead of "Kept by S3
    inside AWS"), and the REAL undo days (storage.json) for an existing Snapshot/File history
    job's own folder -- not the hardcoded default. Files only, safe before/while editing --
    `job` is the SAVED job (None on create, or a job with no target yet: the base bucket's own
    state and the default undo window stand in, since no folder-specific override could exist
    for a job that isn't saved yet)."""
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    out = {"managed": lifecycle.managed(config_dir), "unsupported": False,
           "undo_days": lifecycle.DEFAULT_UNDO_DAYS, "console_cap": None, "console_cap_types": "archive"}
    if not out["managed"]:
        return out
    try:
        base, jobs, settings = _context(cfg)
        target = (lifecycle.folder_of_job(base, jobs, job["name"])
                 if job and job.get("name") else None)
        bucket = target[0] if target else base
        if (lifecycle.load_status(cache).get(bucket) or {}).get("state") == "unsupported":
            out["unsupported"] = True
            return out
        if target is not None:
            bset = lifecycle.bucket_settings(settings, target[0])
            out["undo_days"] = lifecycle.undo_days(bset, target[1])
            # final fix wave I4: a console rule cutting this job's own history short
            _before, want = lifecycle.applied_view({"CONFIG_DIR": config_dir, "CACHE_DIR": cache},
                                                   target[0], jobs, settings)
            out["console_cap"] = _folder_cap(cfg, target[0], target[1], want)
            out["console_cap_types"] = job.get("type") or "archive"
        else:
            # a new job's folder doesn't exist yet: any console rule covering ALL of media/ will
            # apply to a new Plain copy job's folder too
            live = lifecycle.load_live(cache, base)
            caps = lifecycle.console_caps(live["rules"], "media/", None, covering=True) if live else []
            out["console_cap"] = caps[0] if caps else None
    except Exception:                                        # noqa: BLE001 — a GET never 500s on this
        pass
    return out
