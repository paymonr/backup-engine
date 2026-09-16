# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
import json
import math
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import (config_io, runner, security, provision, fsbrowse, estimate_io, jobs_io,
               dirsize, attributions, status, vocab, points, readiness, ops)
from ..estimator.prices import load_prices
from ..estimator import usage
from ..engine import cron, runs, errors

bp = Blueprint("gui", __name__)

@bp.get("/setup/about")
def about_page():
    # About = the glossary (spec 5.13): the tools this app drives, in plain language,
    # plus the version/build stamp and the third-party licences.
    return render_template("about.html", third_party=attributions.THIRD_PARTY,
                           version=current_app.config.get("VERSION", "0.1.0-dev"),
                           build_date=current_app.config.get("BUILD_DATE", "unknown"))

@bp.get("/about")
def about_redirect():
    # Old bookmark → the glossary's new home (spec 4.1 / 5.13).
    return redirect("/setup/about", code=301)

@bp.get("/")
def index():
    # First run (no runtime key + bucket yet) lands on Setup readiness; once set up,
    # the Board is home (spec 5.1 "302 → /setup", ruling R-H).
    if not config_io.is_provisioned(current_app.config["CONFIG_DIR"]):
        return redirect(url_for("gui.setup_page"))
    return render_template("board.html", status=_board_payload(current_app.config),
                           csrf=security.issue_csrf())


@bp.get("/status.json")
def status_json():
    # The Board's 30 s poll (spec 8.1). Same payload the page server-rendered from.
    return jsonify(_board_payload(current_app.config))


def _board_payload(cfg) -> dict:
    """`status.board()` (Task 4) with the cost object merged in (spec 8.1). The
    board dict was deliberately shaped so the route attaches cost here."""
    board = status.board(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], cfg["SCRIPTS_DIR"],
                         source_root=cfg["SOURCE_ROOT"])
    board["cost"] = _board_cost(cfg)
    return board


def _prices_for(cfg, kind=None):
    """Load the bundled/live price table, guarded: a pricing failure degrades a cost
    surface to `None` rather than 500 the page. `?prices=bundled|live` overrides
    PRICES_LIVE for that request (spec 7.9)."""
    live = cfg["PRICES_LIVE"] if kind not in ("bundled", "live") else (kind == "live")
    try:
        return load_prices(estimate_io._region(cfg["CONFIG_DIR"]),
                           cache_dir=cfg["CACHE_DIR"], live=live)
    except Exception:
        return None


def _board_cost(cfg) -> dict:
    """The Board cost strip (spec 5.1 band 4 / 8.1 `cost`): the real
    `estimate_io.board_cost`, from CACHES ONLY (priced usage cache + cache-only
    billing + the frozen model). Never Cost Explorer, never a live pricing call at
    render (Ruling R-A retired in Task 12)."""
    return estimate_io.board_cost(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], _prices_for(cfg))


# --- job page (spec 5.2) ---------------------------------------------------

def _tiered_label(k) -> str:
    parts = []
    if k.get("last"):
        parts.append(f"Last {int(k['last'])}")
    if k.get("daily"):
        parts.append(f"one a day for {int(k['daily'])} days")
    if k.get("weekly"):
        parts.append(f"one a week for {int(k['weekly'])} weeks")
    if k.get("monthly"):
        parts.append(f"one a month for {int(k['monthly'])} months")
    return " · ".join(parts) if parts else "Keep everything"


def _keep_rule_label(job) -> str:
    """A plain-language keep-rule label (spec 4.3) for `How it is set up` and the
    restore-point hint. §4.3's shared keep_rule_* formatter is a later task; this
    is the job-page-local minimal form and uses no forbidden vocabulary."""
    r = job.get("retention")
    if isinstance(r, dict):
        t = r.get("type")
        if t == "keep_all":
            return "Keep everything"
        if t == "days":
            return f"Kept for {int(r.get('days', 0))} days"
        if t == "count":
            return f"Keep the last {int(r.get('count', 0))}"
        if t == "tiered":
            return _tiered_label(r.get("keep") or {})
    if job.get("type") == "versioned" and isinstance(job.get("keep"), dict):
        return _tiered_label(job["keep"])
    if job.get("retention_days"):
        return f"Kept for {int(job['retention_days'])} days"
    return "Keep everything"


def _job_identity(cfg, job) -> dict:
    """The `.pathline` / `Where it goes` identity (spec 5.2 header + How it is set
    up). Versioned jobs share the fixed `appdata/` prefix and are told apart by a
    tag; others write their own `media/<name>/` prefix (estimate_io._size_for)."""
    env = config_io.read_backup_env(cfg["CONFIG_DIR"])
    bucket = (env.get("S3_BUCKET") or "").strip() or "your-bucket"
    name = job.get("name")
    if job.get("type") == "versioned":
        prefix, tag = "appdata", name
    else:
        prefix, tag = f"media/{name}", None
    source_host = (cfg.get("SOURCE_ROOT_HOST") or "").rstrip("/")
    src = job.get("source", "")
    source_display = f"{source_host}/{src}" if source_host else src
    return {"bucket": bucket, "prefix": prefix, "tag": tag,
            "s3": f"s3://{bucket}/{prefix}/", "source_display": source_display}


def _dow_date(iso, tz) -> str:
    """`Fri 28 Aug` — the weekday-and-date label (no time) for a job-page line
    that names a specific run (streak `broken`, the ledger's tallest bar; 5.2)."""
    d = status._parse_iso(iso)
    return status._iso_short(d, tz).rsplit(" ", 1)[0] if d else ""


def _ledger(cfg, name, tz, cells=30) -> dict:
    """The `Last 30 runs` ledger strip (spec 5.2): the newest `cells` backup runs,
    oldest-first, padded with dim placeholders, each cell carrying a squared-scale
    bar height. Built here (not from status.job's 14-cell board strip) because the
    job-page ledger is 30 wide; it reuses status._cell/_dim_cell so the cell shape
    matches the Board's exactly (kind == 'backup' only, spec 5.2/6.5)."""
    res = runs.read_runs(cfg["CACHE_DIR"], name)
    records = res.records
    backups = [r for r in records if r.kind in runs.BACKUP_KINDS]
    median_s = runs.median_duration_s(backups)
    window = list(reversed(backups[:cells]))                 # oldest-first
    row = [status._cell(r, median_s, tz) for r in window]
    padded = [status._dim_cell() for _ in range(cells - len(row))] + row
    durs = [c["duration_s"] for c in row if c.get("duration_s")]
    maxd = max(durs) if durs else 0
    for c in padded:
        d = c.get("duration_s")
        if d and maxd:
            c["bar_px"] = max(2, round(20 * (d / maxd) ** 2))
            c["tall"] = (d == maxd)
        else:
            c["bar_px"], c["tall"] = 2, False
    ok = sum(1 for c in row if c.get("outcome") == "ok")
    failed = sum(1 for c in row if c.get("outcome") in ("failed", "aborted"))
    fail_label = None
    for c in reversed(row):                                   # newest failed cell
        if c.get("outcome") in ("failed", "aborted"):
            fail_label = status._iso_short(status._parse_iso(c["started_at"]), tz)[:10]
            break
    if not row:
        aria = f"No runs on record for {name}."
    elif failed == 0:
        aria = f"Last {len(row)} runs for {name}: every run succeeded."
    else:
        aria = (f"Last {len(row)} runs for {name}: {ok} OK, {failed} failed"
                + (f" ({fail_label})." if fail_label else "."))
    # Ledger hint sentence 2 (spec 5.2): name the tallest bar — its weekday-date,
    # duration, how it compares to the usual, and whether it is a `slow` cell.
    tall = None
    for c in row:
        if c.get("duration_s") and c["duration_s"] == maxd:
            tall = c
            break
    tall_info = None
    if tall and median_s:
        ratio = tall["duration_s"] / median_s
        if ratio < 1.5:
            phrase = "about the usual"
        elif ratio <= 2.5:
            phrase = "twice the usual"
        else:
            phrase = f"{round(ratio)}× the usual"
        tall_info = {
            "date": _dow_date(tall["started_at"], tz),
            "duration_s": tall["duration_s"],
            "phrase": phrase,
            "slow": bool(tall.get("slow")),
        }
    return {"cells": padded, "shown": len(row), "ok": ok, "failed": failed,
            "oldest": row[0]["started_at"] if row else None,
            "aria": aria, "median_s": median_s, "maxd": maxd,
            "unreadable": res.corrupt_lines, "tall": tall_info}


def _job_cost_band(cfg, job, prices) -> dict:
    """`What this job costs` (spec 5.2 / 8.7): the real `estimate_io.job_cost_band`
    (First bill / By month 6 / Every month after + the change-rate assumption row),
    from CACHES ONLY — never Cost Explorer, never a live pricing call (Ruling R-I
    retired in Task 12)."""
    return estimate_io.job_cost_band(job, cfg["CONFIG_DIR"], cfg["CACHE_DIR"], prices)


@bp.get("/jobs/<name>")
def job_page(name):
    cfg = current_app.config
    job_def = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job_def is None:
        # Unknown job → the themed 404 (spec 5.14 / Task 7b).
        abort(404, description=f"There is no job called {name}")
    tz = cron.local_tz()
    keep_label = _keep_rule_label(job_def)
    st = status.job(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], cfg["SCRIPTS_DIR"], name,
                    source_root=cfg["SOURCE_ROOT"], job_def=job_def)
    pv = points.view(cfg["CACHE_DIR"], job_def, tz=tz,
                     keep_rule_label=keep_label, keep_rule_prose=keep_label)
    # Recovery rail + Get-data-back needs-line (readiness.recovery_summary, 7.7):
    # cache-only. Prices are guarded; the restore COST comes from the real
    # estimate_io.restore_quote (Task 12), so the rail and the cost band price live.
    # A pricing failure must degrade the rail, never 500 the page.
    prices = _prices_for(cfg)
    rec = readiness.recovery_summary(cfg, prices, crontab_stale=st.get("crontab_stale"))
    rjob = next((j for j in rec.get("jobs", []) if j["name"] == name), None)
    try:
        schedule_desc = cron.describe(job_def.get("schedule", ""))
    except Exception:
        schedule_desc = job_def.get("schedule", "")
    return render_template(
        "job.html", s=st, job=job_def, pv=pv, rec=rec, rjob=rjob,
        cost=_job_cost_band(cfg, job_def, prices), ident=_job_identity(cfg, job_def),
        ledger=_ledger(cfg, name, tz), keep_label=keep_label,
        test_restore_price=_test_restore_price(cfg, job_def, prices),
        restore=_restore_band_ctx(cfg, job_def, rec),
        sibling_cold=_sibling_cold(cfg, job_def),
        dowdate=lambda iso: _dow_date(iso, tz),
        schedule_desc=schedule_desc, csrf=security.issue_csrf())


@bp.post("/jobs/<name>/pause")
def job_pause(name):
    return _set_paused(name, True)


@bp.post("/jobs/<name>/resume")
def job_resume(name):
    return _set_paused(name, False)


def _set_paused(name, paused):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    try:
        job = jobs_io.set_enabled(cfg["CONFIG_DIR"], name, not paused)
    except jobs_io.JobsFileError as e:
        flash(str(e))
        return redirect(url_for("gui.job_page", name=name))
    if job is None:
        abort(404, description=f"There is no job called {name}")
    # Re-render the crontab so status.crontab_stale does not read true right after
    # the toggle (Task-4 carry-forward): the on-disk file now matches the jobs.
    jobs_io.render_crontab(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], cfg["SCRIPTS_DIR"],
                           source_root=cfg["SOURCE_ROOT"])
    flash(f"{'Paused' if paused else 'Resumed'} {name}.")
    return redirect(url_for("gui.job_page", name=name))


# --- Get data back: confirm page + the mutating POSTs (spec 5.2/5.4/8.10) ---
#
# The safety spine: the job page's band is one GET form that only NAVIGATES here;
# `GET /jobs/<name>/restore` states the consequence and takes the typed-name confirm;
# and work starts ONLY on the single POST below, after `confirm == name` is verified
# server-side and `ops.validate_target` confines the target to RESTORE_ROOT (never
# the source). A prefetch or a bot GET can never start a restore.

_INTENTS = ("restore", "thaw", "download")

# Ruling R-B: `estimate_io.restore_quote` lands in Task 12; until then the confirm
# page quotes both retrieval speeds from the measured size and a cached per-GB rate,
# so the guard states an honest order of magnitude. Task 12 wires the real quote and
# updates the test. Placeholder rates (dollars/GB), NOT billing truth:
_STUB_EGRESS_PER_GB = 0.09
_STUB_WARMUP_PER_GB = {"Bulk": 0.0000025, "Standard": 0.0025, "Expedited": 0.03}

_MOUNT_BLOCKER = ("Nowhere to put restored files yet. Add a path mapping to this "
                  "container — host /mnt/user/restore → container /restore, read/write — "
                  "then restart it. Until then, restores run from the command line into "
                  "/cache/restore/<job> (see the README).")


def _restore_sh(cfg) -> str:
    return f'{cfg["SCRIPTS_DIR"]}/restore.sh'


def _mount_ok(cfg) -> bool:
    root = cfg["RESTORE_ROOT"]
    return os.path.isdir(root) and os.access(root, os.W_OK)


def _tier_options(cls) -> list[str]:
    """The retrieval speeds offered for a class (4.3), Standard first (GUI default,
    decision 29). An instant tier has none."""
    tiers = list(readiness.WARMUP.get(cls, {}).keys())
    if "Standard" in tiers:
        tiers = ["Standard"] + [t for t in tiers if t != "Standard"]
    return tiers


def _default_tier(cls) -> str:
    tiers = _tier_options(cls)
    return "Standard" if "Standard" in tiers else (tiers[0] if tiers else "Standard")


def _safe_scope(s) -> str:
    """A scope is `.` (everything), `file` (one file, File history) or a single
    top-level folder token. Anything with a slash or `..` falls back to `.`."""
    s = (s or "").strip()
    if not s or ".." in s or "/" in s:
        return "."
    return s


def _friendly_time(iso) -> str:
    dt = points._parse_ts(iso)
    return points._label(dt, cron.local_tz()) if dt else "later"


def _read_state_json(cfg, name, which):
    p = Path(cfg["CACHE_DIR"], "state", f"{name}.{which}.json")
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _restore_quote(cfg, prices, job, size_gb, file_count, tier):
    """Both-speeds restore price for the confirm page (spec 7.6): the real
    `estimate_io.restore_quote` (Ruling R-B retired in Task 12), degrading to a
    cache-derived placeholder only if the real quote raises so a bad input never
    breaks the confirm render. None when the size is unknown."""
    fn = getattr(estimate_io, "restore_quote", None)
    if fn is not None and prices is not None:
        try:
            return fn(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], prices, job["name"],
                      tier=tier, size_gb=size_gb, file_count=file_count)
        except (ValueError, KeyError, AttributeError, TypeError):
            pass                   # a quote must never break the confirm render
    if size_gb is None:
        return None
    cold = job.get("storage_class") in points.COLD_CLASSES
    warm = _STUB_WARMUP_PER_GB.get(tier, 0.0) * size_gb if cold else 0.0
    return {"amount": round(size_gb * _STUB_EGRESS_PER_GB + warm, 2), "tier": tier,
            "size_gb": size_gb, "file_count": file_count,
            "storage_class": job.get("storage_class"), "provenance": "assumed", "stub": True}


def _test_restore_price(cfg, job, prices) -> str | None:
    """Cold test-restore button price (spec 5.2 rail): `restore_quote` for ONE object
    at Bulk, rounded UP to the cent, minimum $0.01. None on a warm tier — its fixed
    ~$0.01 penny is a single GET, nothing to price."""
    if job.get("storage_class") not in points.COLD_CLASSES or prices is None:
        return None
    cached = (usage.load_cached(cfg["CACHE_DIR"]) or {}).get("data") or {}
    key = "appdata" if job.get("type") == "versioned" else f"media/{job['name']}"
    u = cached.get(key)
    size_gb = (u["bytes"] / (1024 ** 3)) if u else None
    file_count = (u.get("count") if u else None) or 1
    try:
        q = estimate_io.restore_quote(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], prices, job["name"],
                                      fraction=1.0 / max(1, file_count), tier="Bulk",
                                      size_gb=size_gb, file_count=file_count)
    except (ValueError, KeyError, AttributeError, TypeError):
        return None
    cents = max(1, math.ceil((q.get("amount") or 0.0) * 100))
    return f"${cents / 100:.2f}"


def _median_throughput(cfg, name) -> float | None:
    """Median bytes/s across this job's OK backup runs (spec 5.4 `Takes`); None when
    no OK run has both a byte count and a positive duration."""
    recs = runs.read_runs(cfg["CACHE_DIR"], name).records
    rates = sorted(r.bytes_total / r.duration_s for r in recs
                   if r.kind in runs.BACKUP_KINDS and r.outcome == "ok"
                   and r.bytes_total and r.duration_s and r.duration_s > 0)
    if not rates:
        return None
    n = len(rates)
    return rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2


def _human_rate(bps) -> str:
    for unit, div in (("GB/s", 1024 ** 3), ("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


def _takes_line(cold, hi, size_bytes, median_bps) -> str:
    """The `Takes` sentence (spec 5.4): the median-throughput variant `about <N> min
    at <speed>` when the median bytes/s is known, else the honest fallback; cold →
    warm-up first."""
    if cold:
        return f"up to {hi or 12} h warm-up, then the copy"
    if median_bps and size_bytes:
        minutes = max(1, round(size_bytes / median_bps / 60))
        return f"about {minutes} min at {_human_rate(median_bps)}"
    return "starts immediately; the copy runs as fast as your line allows"


def _restore_band_ctx(cfg, job, rec) -> dict:
    """The Get-data-back band's live-chooser context (spec 5.2 / 8.9 `restore`):
    the default dated target, the mount state, the retrieval speeds and the source
    host so the client can refuse a target under the live source."""
    cls = job.get("storage_class", "STANDARD")
    mount_ok = (rec.get("restore_mount") or {}).get("state") == "ok"
    try:
        default_target = ops.default_target(cfg, job)
    except Exception:
        default_target = f'{cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore")}/{job["name"]}/'
    source_host = (cfg.get("SOURCE_ROOT_HOST") or "").rstrip("/")
    src = job.get("source", "")
    return {"mount_ok": mount_ok, "default_target": default_target,
            "restore_root_host": cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore"),
            "source_under": f"{source_host}/{src}" if source_host else src,
            "tiers": _tier_options(cls), "default_tier": _default_tier(cls)}


def _sibling_cold(cfg, job):
    """A sibling job on a cold tier while THIS one is not (5.2 sibling warning)."""
    if job.get("storage_class") in points.COLD_CLASSES:
        return None
    for j in jobs_io.load(cfg["CONFIG_DIR"]):
        if j.get("name") != job.get("name") and j.get("storage_class") in points.COLD_CLASSES:
            return j.get("name")
    return None


def _vfiles_asof(cfg, job, point):
    """The epoch of a File history restore point (its run id) for `--asof`."""
    for p in points.view(cfg["CACHE_DIR"], job).get("points", []):
        if p.get("id") == point:
            return p.get("asof")
    return None


def _restore_argv(cfg, job, intent, container, point, scope, path, tier) -> list[str]:
    """The exact restore.sh invocation for this action (spec 7.5.6)."""
    sh = _restore_sh(cfg)
    name = job["name"]
    typ = job.get("type")
    if intent == "thaw":
        if typ == "versioned":
            return [sh, name, "thaw", "."]                     # whole snapshot store
        if typ == "versioned-files":
            return [sh, name, "thaw", ".", "--tier", tier]     # catalog-selected current versions
        return [sh, name, "thaw", scope or ".", "--tier", tier]  # archive
    if intent == "download":                                    # archive (warm, or cold+ready)
        return [sh, name, "download", scope or ".", container]
    # intent == "restore"
    if typ == "versioned":
        argv = [sh, name, "restore", point or "latest", container]
        if path:
            argv += ["--include", path]
        return argv
    if typ == "versioned-files":
        first = path if (scope == "file" and path) else "."
        argv = [sh, name, first, container]
        asof = _vfiles_asof(cfg, job, point)
        if asof is not None:
            argv += ["--asof", str(asof)]
        if tier:
            argv += ["--tier", tier]
        return argv
    return [sh, name, "download", scope or ".", container]      # safety net


def _confirm_choice(cfg, job, form) -> dict:
    """The already-defaulted choice (5.4 query contract). Ill-formed values fall
    back to their default IN PLACE, so a hand-typed URL always renders 200."""
    typ = job.get("type")
    cls = job.get("storage_class", "STANDARD")
    intent = form.get("intent") or "restore"
    if intent not in _INTENTS:
        intent = "restore"
    if typ == "archive" and intent == "restore":
        intent = "download"                                    # a Plain copy is downloaded (7.5.5)
    tier = form.get("tier") if form.get("tier") in estimate_io.RETRIEVAL_TIERS else _default_tier(cls)
    target = (form.get("target") or "").strip()
    if not target:
        try:
            target = ops.default_target(cfg, job)
        except Exception:
            target = f'{cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore")}/{job["name"]}/'
    return {"intent": intent, "point": (form.get("point") or "").strip(),
            "scope": _safe_scope(form.get("scope")), "path": (form.get("path") or "").strip(),
            "target": target, "tier": tier}


def _render_confirm(cfg, job, form, *, errors=None, blocker=None, status_code=200,
                    intent_override=None):
    """Server-render the confirmation page (5.4). Reused for the GET render and for
    every 400/409 POST re-render (values intact, the field error shown).

    `intent_override` pins the resolved intent for a POST re-render: the thaw confirm
    form omits the `intent` field (its POST goes to /thaw), so without this a 400
    re-render of a warm-up would recompute the intent from the intent-less form and
    silently fall back to restore/download — the wrong page. (§5.4 "values intact".)"""
    name = job["name"]
    typ = job.get("type")
    cls = job.get("storage_class", "STANDARD")
    cold = cls in points.COLD_CLASSES
    tz = cron.local_tz()
    choice = _confirm_choice(cfg, job, form)
    if intent_override is not None:
        choice["intent"] = intent_override
    intent = choice["intent"]

    # Impossible intents render a BLOCKER and no primary button (5.4), unless the
    # caller already supplied one (a POST-side mount/busy blocker wins).
    if blocker is None:
        if intent == "thaw" and not cold:
            blocker = ("This job is on an instant tier — there is nothing to warm up. "
                       "Files can be read the second you ask.")
        elif typ == "archive" and choice["point"]:
            blocker = ("This is a Plain copy — it keeps no dated restore points, only the "
                       "current copy. There is nothing to pick by date.")

    region = estimate_io._region(cfg["CONFIG_DIR"])
    try:
        prices = load_prices(region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
    except Exception:
        prices = None
    rec = readiness.recovery_summary(cfg, prices)
    rjob = next((j for j in rec.get("jobs", []) if j["name"] == name), None)
    pv = points.view(cfg["CACHE_DIR"], job, tz=tz)

    size_bytes = rjob.get("size_bytes") if rjob else None
    size_provenance = rjob.get("size_provenance", "assumed") if rjob else "assumed"
    file_count = rjob.get("file_count") if rjob else None
    size_gb = (size_bytes / 1_000_000_000) if isinstance(size_bytes, (int, float)) else None

    tiers = _tier_options(cls)
    quote = _restore_quote(cfg, prices, job, size_gb, file_count, choice["tier"])
    alt_quote = None
    if cold and len(tiers) > 1:
        alt = next((t for t in tiers if t != choice["tier"]), None)
        if alt:
            alt_quote = _restore_quote(cfg, prices, job, size_gb, file_count, alt)

    # `Takes` (5.4): the median-throughput variant when the median bytes/s of this
    # job's OK runs is known, else the honest "as fast as your line allows" fallback.
    hi = (rjob.get("warmup") or {}).get("hours_hi") if rjob else None
    takes = _takes_line(cold, hi, size_bytes, _median_throughput(cfg, name))

    point_label = None
    for p in pv.get("points", []):
        if p.get("id") == choice["point"]:
            point_label = p.get("label")
            break
    if point_label is None and pv.get("points"):
        point_label = pv["points"][0].get("label")

    return render_template(
        "restore.html", job=job, name=name, intent=intent, choice=choice,
        rec=rec, rjob=rjob, pv=pv, cold=cold, cls=cls,
        is_versioned=(typ == "versioned"), is_archive=(typ == "archive"),
        is_vfiles=(typ == "versioned-files"),
        size_bytes=size_bytes, size_provenance=size_provenance, file_count=file_count,
        tiers=tiers, quote=quote, alt_quote=alt_quote, point_label=point_label,
        takes=takes, blocker=blocker, errors=errors or {},
        restore_root_host=cfg.get("RESTORE_ROOT_HOST", "/mnt/user/restore"),
        csrf=security.issue_csrf()), status_code


@bp.get("/jobs/<name>/restore")
def restore_confirm(name):
    # The confirmation page (5.4): renders the consequence + the typed-name confirm.
    # A GET mutates nothing and needs no CSRF; it 404s only for an unknown job.
    cfg = current_app.config
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    return _render_confirm(cfg, job, request.args)


@bp.post("/jobs/<name>/restore")
def restore_start(name):
    return _mutate_restore(name, thaw_route=False)


@bp.post("/jobs/<name>/thaw")
def thaw_start(name):
    return _mutate_restore(name, thaw_route=True)


def _mutate_restore(name, *, thaw_route):
    """The ONE place a restore/download/warm-up actually starts (5.4 / 8.10). Every
    guard is server-side; a wrong or missing typed name never launches."""
    cfg = current_app.config
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    f = request.form
    typ = job.get("type")
    cls = job.get("storage_class", "STANDARD")
    cold = cls in points.COLD_CLASSES
    intent = "thaw" if thaw_route else (f.get("intent") or "restore")
    if intent not in _INTENTS:
        intent = "restore"
    if typ == "archive" and intent == "restore":
        intent = "download"

    # The thaw form has no `intent` field (its POST goes to /thaw), so every re-render
    # on this path must pin the resolved thaw intent — otherwise _render_confirm would
    # recompute it from the intent-less form and fall back to restore/download (§5.4
    # "values intact").
    intent_override = intent if thaw_route else None

    # An intent the tier cannot do → 400 re-render with the blocker, launch nothing.
    if intent == "thaw" and not cold:
        return _render_confirm(cfg, job, f, status_code=400, intent_override=intent_override,
                               blocker=("This job is on an instant tier — there is nothing to "
                                        "warm up. Files can be read the second you ask."))

    writes = intent in ("restore", "download")
    if writes and not _mount_ok(cfg):
        return _render_confirm(cfg, job, f, blocker=_MOUNT_BLOCKER, status_code=400,
                               intent_override=intent_override)

    # The typed-name confirm is the real gate (5.4): no correct name → no work.
    if (f.get("confirm") or "").strip() != name:
        return _render_confirm(cfg, job, f, status_code=400, intent_override=intent_override,
                               errors={"confirm": "Type the job name exactly as shown to start."})

    container = None
    if writes:
        v = ops.validate_target(cfg, job, (f.get("target") or "").strip())
        if not v.get("ok"):
            return _render_confirm(cfg, job, f, status_code=400, intent_override=intent_override,
                                   errors={"target": v["message"]})
        container = v["container_path"]

    try:
        ops.ensure_free(cfg, name)
    except ops.OpsLocked:
        return _render_confirm(cfg, job, f, status_code=409, intent_override=intent_override,
                               blocker=(f"{name} is busy — a backup or restore is already "
                                        "running. Wait for it to finish."))

    kind = {"restore": "restore", "download": "download", "thaw": "thaw"}[intent]
    argv = _restore_argv(cfg, job, intent, container, (f.get("point") or "").strip(),
                         _safe_scope(f.get("scope")), (f.get("path") or "").strip(),
                         f.get("tier") if f.get("tier") in estimate_io.RETRIEVAL_TIERS
                         else _default_tier(cls))
    run_id = ops.launch(cfg, argv, job=name, kind=kind, trigger="manual")
    return redirect(url_for("gui.run_record", name=name, run_id=run_id))


@bp.post("/jobs/<name>/thaw/check")
def thaw_check(name):
    # Poll a scoped warm-up for a REAL restore (thaw.json). Starts nothing, costs
    # nothing. NOT the cold test's test-thaw.json — the two files are not
    # interchangeable (decision 54).
    cfg = current_app.config
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    thaw = _read_state_json(cfg, name, "thaw")
    if not thaw:
        abort(400, description="No warm-up is in progress.")
    scope = thaw.get("scope") or "."
    try:
        cp = ops.run_sync(cfg, [_restore_sh(cfg), name, "thaw-status", scope], timeout=90)
    except ops.OpsTimeout:
        flash("The warm-up check did not finish in time — try again.", "note")
        return redirect(url_for("gui.job_page", name=name))
    flash(_thaw_check_flash(cp), "note")
    return redirect(url_for("gui.job_page", name=name))


def _thaw_check_flash(cp) -> str:
    try:
        d = json.loads((cp.stdout or "").strip().splitlines()[-1])
    except Exception:
        return "Checked the warm-up."
    sampled, ready, pending = d.get("sampled", 0), d.get("ready", 0), d.get("pending", 0)
    if pending:
        return f"Checked {sampled} files: {ready} ready, {pending} still warming."
    return f"Checked {sampled} files: {ready} ready."


@bp.post("/jobs/<name>/restore-points/refresh")
def restore_points_refresh(name):
    # Re-list the restore points into the cache (7.5.6). A cache refresh, not a
    # restore: it starts and costs nothing.
    cfg = current_app.config
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    if points.refresh(cfg, name, job.get("type")):
        flash("Restore points refreshed.", "success")
    else:
        flash("Could not list the restore points — check the destination and try again.", "failure")
    return redirect(url_for("gui.job_page", name=name))


@bp.post("/jobs/<name>/test-restore")
def test_restore(name):
    # One-file recovery drill. On a warm tier it launches and lands on the run
    # record; on a cold tier the FIRST press warms one object and a later press
    # ("Check now") re-POSTs this same route to resume it (R11 / decision 54).
    cfg = current_app.config
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    try:
        ops.ensure_free(cfg, name)
    except ops.OpsLocked:
        abort(409, description=f"{name} is busy — a backup or restore is already running. "
                              "Wait for it to finish.")
    pending = _read_state_json(cfg, name, "test-thaw")
    if pending:
        # Check now: resume the pending cold test synchronously so the flash reflects
        # the outcome. The script decides "still warming" vs "download it now".
        try:
            ops.run_sync(cfg, [_restore_sh(cfg), name, "test"], timeout=90)
        except ops.OpsTimeout:
            flash("The warm-up check did not finish in time — try again.", "note")
            return redirect(url_for("gui.job_page", name=name))
        still = _read_state_json(cfg, name, "test-thaw")
        if still:
            flash(f"Still warming up — ready by ~{_friendly_time(still.get('expected_ready_by'))}.",
                  "note")
        else:
            flash("Test restore complete — the file read back and is kept for you.", "success")
        return redirect(url_for("gui.job_page", name=name))
    run_id = ops.launch(cfg, [_restore_sh(cfg), name, "test"], job=name,
                        kind="test-restore", trigger="manual")
    return redirect(url_for("gui.run_record", name=name, run_id=run_id))


# --- run record + Activity (spec 5.3, 5.5, 8.3, 8.4) -----------------------

# The one user-facing name for each operation (spec 5.5 `What` column). `backup`
# depends on the trigger; everything else is a fixed label.
_WHAT_LABELS = {
    "restore": "restore", "download": "download", "thaw": "warm-up",
    "test-restore": "test restore", "usage-refresh": "usage refresh",
    "billing-check": "billing check", "probe": "destination probe",
    "provision": "destination setup",
}
_OUTCOME_LABELS = {"ok": "OK", "failed": "Failed", "running": "Running", "aborted": "Stopped"}
# The record kinds each Activity `kind` filter selects (spec 8.4).
_KIND_GROUPS = {
    "runs": set(runs.BACKUP_KINDS),
    "restores": set(runs.OP_KINDS),
    "setup": {"usage-refresh", "billing-check", "probe", "provision"},
}
# The outcomes each Activity `outcome` filter selects (spec 5.5).
_OUTCOME_GROUPS = {"ok": {"ok"}, "failed": {"failed", "aborted"}, "running": {"running"}}


def _what_label(rec) -> str:
    if rec.kind in runs.BACKUP_KINDS:
        return "manual run" if rec.trigger == "manual" else "scheduled run"
    return _WHAT_LABELS.get(rec.kind, rec.kind)


def _outcome_label(outcome) -> str:
    return _OUTCOME_LABELS.get(outcome, (outcome or "").title() or "—")


def _record_href(rec) -> str:
    """Where this record's page lives: a job record under its job, a `_system`
    record (job is null) under /activity (spec 8.4)."""
    if rec.job:
        return f"/jobs/{rec.job}/runs/{rec.id}"
    return f"/activity/{rec.id}"


# rclone progress lines: `Transferred: 1.234 GiB / 52.700 GiB, 2%, …, ETA 4h12m`.
_XFER_RE = re.compile(r"Transferred:\s*([\d.]+)\s*([KMGTP]?i?B)\s*/\s*([\d.]+)\s*([KMGTP]?i?B)", re.I)
_ETA_RE = re.compile(r"ETA\s*([0-9dhms.]+)", re.I)
_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "PB": 10**15,
          "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40, "PIB": 2**50}


def _to_bytes(num: str, unit: str) -> int:
    return int(float(num) * _UNITS.get(unit.upper(), 1))


def _parse_eta(s: str) -> int | None:
    total = 0.0
    for val, unit in re.findall(r"([\d.]+)\s*([dhms])", s):
        total += float(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return int(total) if total else None


def _progress_from_log(cache_dir, rec) -> dict | None:
    """`{"done_bytes","total_bytes","eta_s"}` parsed from the log tail while a run
    is live (spec 5.3/8.3), else None. Reads only the last few KB, and never raises."""
    if rec is None or rec.outcome != "running" or not rec.log:
        return None
    p = runs.log_file(cache_dir, rec)
    if p is None:
        return None
    try:
        size = p.stat().st_size
        text, _, _ = runs.read_log(cache_dir, rec, offset=max(0, size - 8192), max_bytes=8192)
    except OSError:
        return None
    done = total = eta = None
    for line in text.splitlines():
        m = _XFER_RE.search(line)
        if m:
            done, total = _to_bytes(m.group(1), m.group(2)), _to_bytes(m.group(3), m.group(4))
            e = _ETA_RE.search(line)
            eta = _parse_eta(e.group(1)) if e else None
    if done is None:
        return None
    return {"done_bytes": done, "total_bytes": total, "eta_s": eta}


def _error_class_dict(cache_dir, rec) -> dict | None:
    """The run's `errors.classify` class as a dict (spec 8.2 shape), classified from
    the run's error field AND its own log tail (the run record is where the full log
    is available). None for a healthy or still-running record."""
    if rec is None:
        return None
    tail = ""
    if rec.log:
        try:
            p = runs.log_file(cache_dir, rec)
            size = p.stat().st_size if p is not None else 0
            tail, _, _ = runs.read_log(cache_dir, rec, offset=max(0, size - 65536), max_bytes=65536)
        except Exception:
            tail = ""
    ec = errors.classify(rec.error, rec.exit_code, rec.outcome, tail)
    if ec is None:
        return None
    return {"code": ec.code, "short": ec.short, "verdict": ec.verdict, "cause": ec.cause,
            "board": ec.board, "fix": ec.fix, "fix_label": ec.fix_label,
            "fix_route": ec.fix_route, "blocker": ec.blocker}


def _run_record_json(rec, *, live, median_s, error_class, progress, is_system=False) -> dict:
    d = asdict(rec)
    d["started_at"] = status._iso(rec.started_at)
    d["finished_at"] = status._iso(rec.finished_at)
    d["live"] = live
    d["median_s"] = median_s
    d["error_class"] = error_class
    d["progress"] = progress
    if is_system:
        # A `_system` record has NO snapshot_id and NO median_s (spec 8.3) — absent,
        # not null, so a consumer checking `"median_s" in record` reads it correctly.
        d.pop("snapshot_id", None)
        d.pop("median_s", None)
    return d


def _lookup_run(cache_dir, job, run_id):
    """Return (records, rec) for `job` (a name or `_system`), never reconciling — a
    start-line-only record must read as `running`, not be aborted by a free-lock
    probe (5.3), and a live `_system` op is guarded by the reader elsewhere."""
    records = runs.read_runs(cache_dir, job, reconcile=False).records
    rec = next((r for r in records if r.id == run_id), None)
    return records, rec


def _humanbytes(b) -> str | None:
    if b is None:
        return None
    for lim, unit, dec in ((2**40, "TB", 2), (2**30, "GB", 2), (2**20, "MB", 1)):
        if b >= lim:
            return f"{b / lim:.{dec}f} {unit}"
    return f"{int(b)} B"


def _human_dur(s) -> str:
    if s is None:
        return "—"
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} m {s % 60:02d} s"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d} m"
    return f"{s // 86400} d {(s % 86400) // 3600:02d} h"


_STARTED_BY = {"scheduled": "the schedule", "manual": "you (Run now)", "command": "the command line"}
_KIND_TITLE_PREFIX = {"restore": "Restore", "download": "Download", "thaw": "Warm-up",
                      "test-restore": "Test restore"}


def _what_it_did(rec) -> str | None:
    """The `What it did` defgrid line, from the record's stats (spec 5.3). Only the
    pieces present are shown; None when nothing meaningful is recorded."""
    if rec is None:
        return None
    if rec.kind in runs.BACKUP_KINDS:
        left = []
        if rec.files_changed is not None:
            left.append(f"{rec.files_changed:,} files changed")
        if rec.bytes_added is not None:
            left.append(f"{_humanbytes(rec.bytes_added)} new")
        right = []
        if rec.files_total is not None:
            right.append(f"{rec.files_total:,} files")
        if rec.bytes_total is not None:
            right.append(f"{_humanbytes(rec.bytes_total)} in the folder")
        parts = [", ".join(left)] if left else []
        if right:
            parts.append(", ".join(right))
        return " · ".join(p for p in parts if p) or None
    if rec.kind in ("restore", "download"):
        if rec.files_restored is not None or rec.bytes_restored is not None:
            fc = f"{rec.files_restored:,} files" if rec.files_restored is not None else ""
            bs = _humanbytes(rec.bytes_restored) if rec.bytes_restored is not None else ""
            head = ", ".join(x for x in (fc, bs) if x)
            tgt = (rec.params or {}).get("target")
            return f"{head} written to {tgt}" if tgt else (f"{head} written" if head else None)
    if rec.kind == "thaw" and rec.objects_requested is not None:
        return f"{rec.objects_requested:,} files requested"
    return None


def _render_run_record(cfg, *, name, run_id, records, rec, is_system):
    """Server-render the run-record page (5.3) for a job record or a `_system`
    record. `rec` is None only in the job pending state."""
    cache = cfg["CACHE_DIR"]
    tzobj = cron.local_tz()
    live = bool(rec and rec.outcome == "running")
    pending = rec is None
    median_s = None
    if not is_system:
        median_s = runs.median_duration_s([r for r in records if r.kind in runs.BACKUP_KINDS])
    error_class = _error_class_dict(cache, rec)
    log_text, log_offset, log_eof = "", 0, True
    if rec is not None and rec.log:
        try:
            log_text, log_offset, log_eof = runs.read_log(cache, rec)
        except Exception:
            log_text, log_offset, log_eof = "", 0, True
    elapsed_s = None
    if live and rec.started_at:
        elapsed_s = max(0, int((datetime.now(timezone.utc) - rec.started_at).total_seconds()))

    def _short(dt):
        return status._iso_short(dt, tzobj) if dt else None            # "Sun 13 Sep 04:00"

    def _full(dt):
        if dt is None:
            return None
        d = dt.astimezone(tzobj)
        return (f"{status._WEEKDAYS[d.weekday()][:3]} {d.day} {status._MONTHS[d.month - 1][:3]} "
                f"{d.year} {d:%H:%M:%S}")

    outcome_label = _outcome_label(rec.outcome) if rec else "Running"
    what = _what_label(rec) if rec else "run record"

    # eyebrow / back link / poll base
    if is_system:
        eyebrow = f"system · {what}"
        base_url, back_href, back_label = f"/activity/{run_id}", "/activity", "← All activity"
    else:
        eyebrow = (f"{name} · {what}" if (rec and rec.kind not in runs.BACKUP_KINDS)
                   else f"{name} · run record")
        base_url = f"/jobs/{name}/runs/{run_id}"
        back_href, back_label = f"/jobs/{name}", f"← {name}"

    # h1
    if rec is None:
        title = "Starting…"
    elif is_system:
        title = f"{what[:1].upper()}{what[1:]} {_short(rec.started_at)} — {outcome_label}"
    elif rec.kind in runs.BACKUP_KINDS:
        title = f"{_short(rec.started_at)} — {outcome_label}"
    else:
        prefix = _KIND_TITLE_PREFIX.get(rec.kind, what[:1].upper() + what[1:])
        title = f"{prefix} {_short(rec.started_at)} — {outcome_label}"

    # lead
    started_by = _STARTED_BY.get(rec.trigger, rec.trigger) if rec else None
    if rec is None:
        lead = "Starting the run — waiting for it to report in."
    elif rec.outcome == "running":
        lead = f"Started by {started_by}. Running for {_human_dur(elapsed_s)}."
    elif rec.outcome == "aborted":
        lead = (f"Started by {started_by}. Stopped without reporting — the container was probably "
                f"restarted or the process was killed.")
    elif rec.outcome == "failed":
        tail = f" with exit code {rec.exit_code}." if rec.exit_code is not None else "."
        lead = f"Started by {started_by}. Stopped after {_human_dur(rec.duration_s)}{tail}"
    else:
        lead = f"Started by {started_by}. Finished in {_human_dur(rec.duration_s)}."

    detail = None
    if rec is not None:
        detail = {
            "started_by": started_by,
            "started_full": _full(rec.started_at),
            "finished_full": _full(rec.finished_at),
            "took_txt": _human_dur(rec.duration_s) if rec.duration_s is not None else None,
            "typical_txt": (f"~{_human_dur(median_s)} typical" if median_s else None),
            "what_it_did": _what_it_did(rec),
            # Restore point only for a successful Snapshot backup (5.3).
            "restore_point": (rec.snapshot_id if (rec.kind in runs.BACKUP_KINDS
                                                  and rec.outcome == "ok" and rec.snapshot_id)
                              else None),
        }

    return render_template(
        "run_record.html", name=name, run_id=run_id, rec=rec, is_system=is_system,
        pending=pending, pending_since=status._iso(runs.run_id_started_at(run_id)),
        live=live, median_s=median_s, error_class=error_class, elapsed_s=elapsed_s,
        log_text=log_text, log_offset=log_offset, log_eof=log_eof,
        what=what, eyebrow=eyebrow, base_url=base_url, back_href=back_href, back_label=back_label,
        outcome_label=outcome_label, title=title, lead=lead, detail=detail,
        humanbytes=_humanbytes)


def _run_record_json_response(cfg, *, run_id, records, rec, is_system):
    live = bool(rec and rec.outcome == "running")
    median_s = None
    if not is_system:
        median_s = runs.median_duration_s([r for r in records if r.kind in runs.BACKUP_KINDS])
    error_class = _error_class_dict(cfg["CACHE_DIR"], rec)
    progress = _progress_from_log(cfg["CACHE_DIR"], rec)
    return jsonify(_run_record_json(rec, live=live, median_s=median_s,
                                    error_class=error_class, progress=progress,
                                    is_system=is_system))


def _log_response(cfg, rec, *, pending):
    """The …/log tail (spec 8.3): a text/plain chunk with X-Log-Offset / X-Log-Eof.
    Pending → empty 200; a record with no log → 404 JSON."""
    if pending:
        r = Response("", mimetype="text/plain")
        r.headers["X-Log-Offset"], r.headers["X-Log-Eof"] = "0", "0"
        return r
    if not rec.log:
        abort(404, description="This run has no log of its own.")
    offset = request.args.get("offset", default=0, type=int)
    text, new_offset, eof = runs.read_log(cfg["CACHE_DIR"], rec, offset=offset)
    r = Response(text, mimetype="text/plain")
    r.headers["X-Log-Offset"], r.headers["X-Log-Eof"] = str(new_offset), "1" if eof else "0"
    return r


@bp.get("/jobs/<name>/runs/<run_id>")
def run_record(name, run_id):
    cfg = current_app.config
    if not runs.valid_run_id(run_id):
        abort(404, description=f"There is no run {run_id} for {name}")
    if jobs_io.get(cfg["CONFIG_DIR"], name) is None:
        abort(404, description=f"There is no job called {name}")
    records, rec = _lookup_run(cfg["CACHE_DIR"], name, run_id)
    if rec is None and not runs.is_pending(run_id):
        abort(404, description=f"There is no run {run_id} for {name}")
    return _render_run_record(cfg, name=name, run_id=run_id, records=records, rec=rec,
                              is_system=False)


@bp.get("/jobs/<name>/runs/<run_id>.json")
def run_record_json(name, run_id):
    cfg = current_app.config
    if not runs.valid_run_id(run_id):
        abort(404, description=f"There is no run {run_id} for {name}")
    if jobs_io.get(cfg["CONFIG_DIR"], name) is None:
        abort(404, description=f"There is no job called {name}")
    records, rec = _lookup_run(cfg["CACHE_DIR"], name, run_id)
    if rec is None:
        if not runs.is_pending(run_id):
            abort(404, description=f"There is no run {run_id} for {name}")
        return jsonify({"generated_at": status._iso(datetime.now(timezone.utc)),
                        "tz": status._tzname(cron.local_tz()), "id": run_id, "job": name,
                        "outcome": "pending", "live": True,
                        "pending_since": status._iso(runs.run_id_started_at(run_id))})
    return _run_record_json_response(cfg, run_id=run_id, records=records, rec=rec, is_system=False)


@bp.get("/jobs/<name>/runs/<run_id>/log")
def run_record_log(name, run_id):
    cfg = current_app.config
    if not runs.valid_run_id(run_id):
        abort(404, description=f"There is no run {run_id} for {name}")
    if jobs_io.get(cfg["CONFIG_DIR"], name) is None:
        abort(404, description=f"There is no job called {name}")
    _records, rec = _lookup_run(cfg["CACHE_DIR"], name, run_id)
    if rec is None:
        if not runs.is_pending(run_id):
            abort(404, description=f"There is no run {run_id} for {name}")
        return _log_response(cfg, None, pending=True)
    return _log_response(cfg, rec, pending=False)


def _activity_items(cfg, *, job=None, kind=None, outcome=None, limit=100):
    """Merged job + `_system` records, newest-first, filtered (spec 5.5/8.4). Read
    without reconcile so a live run stays `running` in the feed."""
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    names = list(dict.fromkeys([j["name"] for j in jobs_io.load(config_dir)]
                               + runs.all_jobs_with_runs(cache)))
    recs = runs.read_all(cache, names, limit=10**9, job=job)
    kinds = _KIND_GROUPS.get(kind)
    outcomes = _OUTCOME_GROUPS.get((outcome or "").lower())
    if kinds is not None:
        recs = [r for r in recs if r.kind in kinds]
    if outcomes is not None:
        recs = [r for r in recs if r.outcome in outcomes]
    recs = recs[:limit]
    items = [{
        "id": r.id, "job": r.job, "kind": r.kind, "what": _what_label(r),
        "trigger": r.trigger, "outcome": r.outcome, "label": _outcome_label(r.outcome),
        "started_at": status._iso(r.started_at), "finished_at": status._iso(r.finished_at),
        "duration_s": r.duration_s, "error": r.error, "record": _record_href(r),
        "log": bool(r.log),
    } for r in recs]
    return items


@bp.get("/activity")
def activity():
    cfg = current_app.config
    job = request.args.get("job") or None
    if job in ("all", ""):
        job = None
    kind = request.args.get("kind") or None
    outcome = request.args.get("outcome") or None
    limit = request.args.get("limit", default=100, type=int)
    items = _activity_items(cfg, job=job, kind=kind, outcome=outcome, limit=limit)
    job_names = [j["name"] for j in jobs_io.load(cfg["CONFIG_DIR"])]
    # The first enabled job powers the empty-state "Run <job> now" button (5.5).
    first_enabled = next((j for j in jobs_io.load(cfg["CONFIG_DIR"]) if j.get("enabled", True)), None)
    return render_template("activity.html", items=items, jobs=job_names,
                           filters={"job": job or "all", "kind": kind or "all",
                                    "outcome": outcome or "all", "limit": limit},
                           first_enabled=first_enabled["name"] if first_enabled else None,
                           csrf=security.issue_csrf())


@bp.get("/activity.json")
def activity_json():
    cfg = current_app.config
    job = request.args.get("job") or None
    if job in ("all", ""):
        job = None
    kind = request.args.get("kind") or None
    outcome = request.args.get("outcome") or None
    limit = request.args.get("limit", default=100, type=int)
    items = _activity_items(cfg, job=job, kind=kind, outcome=outcome, limit=limit)
    return jsonify({"generated_at": status._iso(datetime.now(timezone.utc)),
                    "tz": status._tzname(cron.local_tz()),
                    "filters": {"job": job or "all", "kind": kind or "all",
                                "outcome": outcome or "all", "limit": limit},
                    "items": items})


@bp.get("/activity/<run_id>")
def activity_record(run_id):
    cfg = current_app.config
    if not runs.valid_run_id(run_id):
        abort(404, description=f"There is no record {run_id}")
    records, rec = _lookup_run(cfg["CACHE_DIR"], runs.SYSTEM_JOB, run_id)
    if rec is None:                    # no pending state for system ops (5.5/8.3)
        abort(404, description=f"There is no record {run_id}")
    return _render_run_record(cfg, name=None, run_id=run_id, records=records, rec=rec,
                              is_system=True)


@bp.get("/activity/<run_id>.json")
def activity_record_json(run_id):
    cfg = current_app.config
    if not runs.valid_run_id(run_id):
        abort(404, description=f"There is no record {run_id}")
    records, rec = _lookup_run(cfg["CACHE_DIR"], runs.SYSTEM_JOB, run_id)
    if rec is None:
        abort(404, description=f"There is no record {run_id}")
    return _run_record_json_response(cfg, run_id=run_id, records=records, rec=rec, is_system=True)


@bp.get("/activity/<run_id>/log")
def activity_record_log(run_id):
    cfg = current_app.config
    if not runs.valid_run_id(run_id):
        abort(404, description=f"There is no record {run_id}")
    _records, rec = _lookup_run(cfg["CACHE_DIR"], runs.SYSTEM_JOB, run_id)
    if rec is None:
        abort(404, description=f"There is no record {run_id}")
    return _log_response(cfg, rec, pending=False)


# --- Setup readiness (spec 5.10) -------------------------------------------

# code -> the "Check" column name (spec 5.10 table). The scheduler row is the
# informational sixth row, present only when crontab_stale (7.3).
_SETUP_CHECK_NAMES = {
    "destination": "Destination reachable",
    "passphrase": "Recovery passphrase",
    "versioning": "Old versions protected",
    "jobs_scheduled": "At least one job scheduled",
    "restore_tested": "Restore ever tested",
    "scheduler": "Scheduler up to date",
}


@bp.get("/setup")
def setup_page():
    # "Is this install able to back up — and able to restore?" (spec 5.10): the five
    # readiness checks + the informational crontab_stale row. Works unprovisioned
    # (never redirects — it is where an unprovisioned `/` lands).
    cfg = current_app.config
    stale = status.crontab_stale(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], cfg["SCRIPTS_DIR"],
                                 source_root=cfg.get("SOURCE_ROOT"))
    checks = readiness.setup_checks(cfg, crontab_stale=stale)
    for c in checks:                       # a human "Verified" stamp per row (5.10)
        c["verified_human"] = _fmt_verified(c.get("verified_at"))
    return render_template("setup.html", checks=checks, check_names=_SETUP_CHECK_NAMES,
                           csrf=security.issue_csrf())


def _fmt_verified(iso) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %H:%M")
    except (ValueError, AttributeError):
        return None


@bp.post("/setup/probe")
def setup_probe():
    # "Probe now" (spec 5.10): a detached destination probe recorded as an operation
    # (sysop writes state/_probe.json). It starts and costs nothing at render.
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    ops.launch_py(cfg, "app.engine.sysop", ["probe"], kind="probe")
    flash("Probing the destination — watch it in Activity →", "note")
    return redirect(url_for("gui.setup_page"))


@bp.post("/setup/versioning-confirmed")
def setup_versioning_confirmed():
    # "Mark as confirmed" (spec 5.10): when an object-only key can't read bucket
    # versioning, the owner confirms it by hand. Merge into state/_probe.json so the
    # destination probe result is preserved.
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    state_dir = Path(cfg["CACHE_DIR"], "state")
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "_probe.json"
    try:
        probe = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (ValueError, OSError):
        probe = {}
    if not isinstance(probe, dict):
        probe = {}
    probe["versioning"] = {"state": "confirmed_by_hand", "checked_at": _now_iso()}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(probe), encoding="utf-8")
    os.replace(tmp, p)
    flash("Marked bucket versioning as confirmed.", "success")
    return redirect(url_for("gui.setup_page"))


# --- Keys & secrets (spec 5.12) --------------------------------------------

# Every write-only field the Keys screen edits (runtime key + the read-only CE
# billing credential). This is the ONLY surface that writes COST_EXPLORER_* now
# (spec 5.6 band 5 / 5.12): POST /costs/billing was deleted in Task 12.
_KEY_SECRET_FIELDS = tuple(config_io.SECRET_KEYS) + tuple(config_io.COST_EXPLORER_KEYS)


def _keys_groups(cfg):
    """The four grouped rows for /setup/keys (spec 5.12). Group membership is
    `config_io.KEY_GROUPS`; template keys in no group fall into This machine.
    Secret rows carry a three-state token; their value is never returned."""
    template = config_io.template_keys(cfg["TEMPLATE_PATH"])
    values = config_io.read_backup_env(cfg["CONFIG_DIR"])
    status3 = config_io.secrets_status_3(cfg["CONFIG_DIR"])
    secret_all = set(_KEY_SECRET_FIELDS)

    def row(k):
        return {"key": k, "secret": k in secret_all,
                "value": values.get(k, ""),
                "status": status3.get(k) if k in secret_all else None}

    groups, seen = [], set()
    for gname, keys in config_io.KEY_GROUPS.items():
        rows = []
        for k in keys:
            seen.add(k)
            rows.append(row(k))
        groups.append({"name": gname, "id": gname.lower().replace(" ", "-"), "rows": rows})
    extra = [k for k in template if k not in seen and k not in secret_all]
    if extra:
        tm = next(g for g in groups if g["name"] == "This machine")
        tm["rows"].extend(row(k) for k in extra)

    region = (values.get("AWS_REGION") or "us-east-1").strip()
    bucket = (values.get("S3_BUCKET") or "your-bucket").strip()
    restic_repo = f"s3:s3.{region}.amazonaws.com/{bucket}/appdata"
    return groups, restic_repo


@bp.get("/setup/keys")
def config_page():
    cfg = current_app.config
    groups, restic_repo = _keys_groups(cfg)
    return render_template("config.html", groups=groups, restic_repo=restic_repo,
                           secret_mode=config_io.secrets_mode(cfg["CONFIG_DIR"]),
                           csrf=security.issue_csrf())


@bp.post("/setup/keys")
def config_save():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    f = request.form
    env_keys = [k for k in config_io.template_keys(cfg["TEMPLATE_PATH"])
                if k not in _KEY_SECRET_FIELDS]
    before = config_io.read_backup_env(cfg["CONFIG_DIR"])
    before_ce = config_io.read_cost_explorer_creds(cfg["CONFIG_DIR"])
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {k: f.get(k, "") for k in env_keys})
    # Write the runtime key AND the Cost Explorer billing credential in one pass —
    # write_secrets rebuilds secrets.env from the managed UNION, so writing one group
    # never drops the other, and a blank field keeps what is there (write-only, 5.12).
    config_io.write_secrets(cfg["CONFIG_DIR"], {k: f.get(k, "") for k in _KEY_SECRET_FIELDS})
    after_ce = config_io.read_cost_explorer_creds(cfg["CONFIG_DIR"])
    flash("Saved.", "success")
    if f.get("TZ", "").strip() and f.get("TZ", "").strip() != (before.get("TZ", "") or "").strip():
        flash("TZ changes take effect after a restart.", "note")
    if before_ce is None and after_ce is not None:
        flash("Connected AWS billing.", "success")
    elif before_ce is not None and after_ce is None:
        flash("Disconnected AWS billing.", "success")
    return redirect(url_for("gui.config_page"))


@bp.get("/config")
def config_redirect():
    # Old bookmark → Keys & secrets (spec 4.1 / 5.12).
    return redirect("/setup/keys", code=301)

@bp.get("/logs")
def logs():
    n = request.args.get("tail", default=200, type=int)
    return Response(runner.tail_log(current_app.config["CACHE_DIR"], n), mimetype="text/plain")

# --- Destination (spec 5.11): the preserved three-path picker, restyled onto
# /setup/destination. The provisioning SAFETY MODEL in provision.py is unchanged;
# only the routes' URLs, the success flash and the landing page are restyled. Old
# /provision* paths 301 to the new ones (spec 4.1).

@bp.get("/setup/destination")
def provision_home():
    cfg = current_app.config
    env = config_io.read_backup_env(cfg["CONFIG_DIR"])
    return render_template("provision_home.html", csrf=security.issue_csrf(),
                           provisioned=config_io.is_provisioned(cfg["CONFIG_DIR"]),
                           bucket=env.get("S3_BUCKET", ""), region=env.get("AWS_REGION", ""))

@bp.get("/provision")
def provision_redirect():
    return redirect("/setup/destination", code=301)

@bp.get("/setup/destination/manual")
def provision_manual():
    return render_template("provision_manual.html", csrf=security.issue_csrf(),
                           bucket="", region="us-east-1", policy=None, console=None, error=None)

@bp.get("/provision/manual")
def provision_manual_redirect():
    return redirect("/setup/destination/manual", code=301)

@bp.post("/setup/destination/manual/render")
def provision_manual_render():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    bucket = request.form.get("bucket", "").strip()
    region = request.form.get("region", "").strip()
    if not bucket or not region:
        return render_template("provision_manual.html", csrf=security.issue_csrf(),
                               bucket=bucket, region=region, policy=None, console=None,
                               error="Bucket and region are required."), 400
    return render_template("provision_manual.html", csrf=security.issue_csrf(),
                           bucket=bucket, region=region,
                           policy=provision.render_policy(bucket),
                           console=provision.render_console_steps(bucket, region),
                           error=None)

@bp.get("/setup/destination/scripted")
def provision_scripted():
    return render_template("provision_scripted.html", csrf=security.issue_csrf())

@bp.get("/provision/scripted")
def provision_scripted_redirect():
    return redirect("/setup/destination/scripted", code=301)

@bp.post("/setup/destination/validate")
def provision_validate():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    bucket = request.form.get("bucket", "").strip()
    region = request.form.get("region", "").strip()
    key = request.form.get("AWS_ACCESS_KEY_ID", "").strip()
    secret = request.form.get("AWS_SECRET_ACCESS_KEY", "").strip()
    try:
        provision.validate_runtime_key(bucket, region, key, secret)
    except provision.ValidationError as e:
        return render_template("provision_manual.html", csrf=security.issue_csrf(),
                               bucket=bucket, region=region, policy=None, console=None,
                               error=f"Validation failed at the {e.step} step — nothing saved.",
                               error_detail=e.detail), 400
    config_io.write_secrets(cfg["CONFIG_DIR"],
                            {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret})
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {**config_io.read_backup_env(cfg["CONFIG_DIR"]),
                                "AWS_REGION": region, "S3_BUCKET": bucket})
    # Record the successful destination setup so Activity shows it (spec 5.5).
    provision.record_setup(cfg["CACHE_DIR"], bucket=bucket, region=region, mode="validate")
    flash(f"Destination set: {bucket} in {region}. Next: the recovery passphrase, "
          f"then the first job.", "success")
    return redirect(url_for("gui.setup_page"))

@bp.get("/setup/destination/automated")
def provision_automated():
    return render_template("provision_automated.html", csrf=security.issue_csrf(),
                           bucket="", region="us-east-1", error=None)

@bp.get("/provision/automated")
def provision_automated_redirect():
    return redirect("/setup/destination/automated", code=301)


@bp.post("/setup/destination/automated")
def provision_automated_run():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    override = request.form.get("bucket", "").strip()
    region = request.form.get("region", "").strip() or "us-east-1"
    admin_key = request.form.get("ADMIN_ACCESS_KEY_ID", "").strip()
    admin_secret = request.form.get("ADMIN_SECRET_ACCESS_KEY", "").strip()
    session_token = request.form.get("ADMIN_SESSION_TOKEN", "").strip() or None
    try:
        # No override -> auto-name unraid-backup-<account>, read from the admin creds.
        bucket = override or provision.derive_bucket_name(
            provision.aws_account_id(region, admin_key, admin_secret, session_token))
        result = provision.run_tofu_apply(bucket, region, admin_key, admin_secret, session_token)
    except provision.AccountLookupError as e:
        return render_template("provision_automated.html", csrf=security.issue_csrf(),
                               bucket=override, region=region,
                               error="Couldn't read your AWS account from those admin "
                                     "credentials — check the key and try again. Nothing was saved.",
                               error_detail=e.detail), 400
    except provision.TofuError as e:
        return render_template("provision_automated.html", csrf=security.issue_csrf(),
                               bucket=override, region=region,
                               error=f"Automated provisioning failed at tofu {e.phase} — nothing saved.",
                               error_detail=e.detail), 400
    finally:
        # discard transient admin creds from this frame regardless of outcome
        admin_key = admin_secret = session_token = None
    config_io.write_secrets(cfg["CONFIG_DIR"],
                            {"AWS_ACCESS_KEY_ID": result["AWS_ACCESS_KEY_ID"],
                             "AWS_SECRET_ACCESS_KEY": result["AWS_SECRET_ACCESS_KEY"]})
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {**config_io.read_backup_env(cfg["CONFIG_DIR"]),
                                "AWS_REGION": result["region"], "S3_BUCKET": result["bucket"]})
    # Record the successful automated provisioning so Activity shows it (spec 5.5).
    provision.record_setup(cfg["CACHE_DIR"], bucket=result["bucket"], region=result["region"],
                           mode="automated")
    flash(f"Destination set: {result['bucket']} in {result['region']}. Next: the recovery "
          f"passphrase, then the first job.", "success")
    return redirect(url_for("gui.setup_page"))

@bp.get("/jobs")
def jobs_page():
    # The job table folded into the Board (spec 5.1, ruling R-H): `/jobs` is now a
    # permanent redirect home. Save/run/delete still redirect here by name, which
    # lands the user on the Board.
    return redirect(url_for("gui.index"), code=301)

# --- create/edit job wizard (spec 5.8 / 5.9 / 8.6 / 8.10) ------------------
#
# The "Ledger Runbook" create-job screen: every cost consequence beside the
# control that moves it. The figures come from estimate_io.wizard_estimate (the
# frozen model, read-only); the template renders an initial server-computed state
# so the page is honest with JS off, and app.js re-fetches /jobs/estimate.json on
# every change to repaint live.

_RETENTION_DEFAULT_BY_TYPE = {"versioned": "tiered", "versioned-files": "days",
                              "archive": "days"}

# `was:` idioms for the diff-aware edit screen (spec 5.9). Each renders a saved value
# in the SAME idiom the control shows it in, so a changed row reads e.g.
# `was: Instant · STANDARD` / `was: A little — rare replacements (~1%)`.
_CLASS_WAS = {c: f"{estimate_io._CLASS_PLAIN[c]} · {c}" for c in jobs_io.STORAGE_CLASSES}
_CHANGE_WAS = {0: "Nothing — files only get added (0%)", 1: "A little — rare replacements (~1%)",
               10: "Some — regular edits (~10%)", 30: "A lot — churny (~30%)"}
_RETENTION_WAS = {"keep_all": "Keep everything", "tiered": "Thin them out over time",
                  "days": "Keep for N days", "count": "Keep the last N versions"}


def _edit_diff(saved, fv, saved_typical, current_typical):
    """Per-field `was:` treatment for the edit screen (spec 5.9): a control whose
    submitted value differs from the SAVED value carries the old value in its idiom;
    the footer gains `(was $X)` when the typical-month cost changed. Reverting a field
    (submitted == saved) removes both. `{}` on create (no saved job)."""
    if saved is None:
        return {}
    d = {}

    def _row(field, was):
        d[field] = {"changed": str(saved.get(field, "")) != str(fv.get(field, "")), "was": was}
    _row("storage_class", _CLASS_WAS.get(saved.get("storage_class"), saved.get("storage_class")))
    try:
        chg = int(float(saved.get("change_rate_pct") or 0))
    except (TypeError, ValueError):
        chg = 0
    _row("change_rate_pct", _CHANGE_WAS.get(chg, f"{chg}%"))
    _row("source", saved.get("source"))
    _row("retention_type", _RETENTION_WAS.get(saved.get("retention_type"), saved.get("retention_type")))
    try:
        sched_human = cron.describe(saved.get("schedule", ""))
    except Exception:
        sched_human = saved.get("schedule", "")
    d["schedule"] = {"changed": str(saved.get("schedule", "")) != str(fv.get("schedule", "")),
                     "was": sched_human}
    d["footer_was"] = None
    if (saved_typical is not None and current_typical is not None
            and abs(saved_typical - current_typical) >= 0.005):
        d["footer_was"] = f"${saved_typical:.2f}"
    d["saved_sched_human"] = sched_human
    return d


def _wizard_prices(cfg, kind=None):
    """(prices, live_failed) honouring ?prices=bundled|live (spec 7.9). Guarded so a
    pricing failure degrades to (None, False) rather than 500 the wizard."""
    live = cfg["PRICES_LIVE"] if kind not in ("bundled", "live") else (kind == "live")
    try:
        prices = load_prices(estimate_io._region(cfg["CONFIG_DIR"]),
                             cache_dir=cfg["CACHE_DIR"], live=live)
    except Exception:
        return None, False
    live_failed = kind == "live" and not str(prices.source or "").startswith("aws-price-list")
    return prices, live_failed


def _other_jobs(cfg, exclude=None):
    """The OTHER saved jobs (spec 8.9 `other_jobs`): schedule-collision + all-jobs
    total. Excludes the job being edited so an edit never collides with itself."""
    return [{"name": j.get("name"), "schedule": j.get("schedule", ""),
             "type": j.get("type"), "enabled": j.get("enabled", True)}
            for j in jobs_io.load(cfg["CONFIG_DIR"]) if j.get("name") != exclude]


def _keep_defaults(job):
    """The tiered fieldset prefill (Fix 1b carry-forward): the saved keep tiers, or
    3/7/4/6 when the derived legacy `keep` mirror is all-zero (a count/days job)."""
    k = (job or {}).get("keep") or {}
    if any(int(k.get(x, 0) or 0) for x in ("last", "daily", "weekly", "monthly")):
        return {x: int(k.get(x, 0) or 0) for x in ("last", "daily", "weekly", "monthly")}
    return {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}


def _fresh_form_values():
    """Create-screen defaults (spec 5.8): Snapshot backup, STANDARD, daily 05:00,
    the ~1% change default (Appendix B #17), tiered keep 3/7/4/6."""
    return {"type": "versioned", "source": "", "storage_class": "STANDARD",
            "schedule": "0 5 * * *", "enabled": "1", "name": "",
            "retention_type": "tiered", "retention_days": "180", "retention_count": "30",
            "keep_last": "3", "keep_daily": "7", "keep_weekly": "4", "keep_monthly": "6",
            "change_rate_pct": "1", "change_rate_touched": "", "packing": "",
            "pack_member_gb": "0.05", "mirror": "0", "size_gb": "", "file_count": "",
            "measured_bytes": "", "measured_capped": "", "measured_at": ""}


def _saved_form_values(job):
    """Edit-screen prefill from the saved job (spec 5.9): retention → the selector,
    the persisted assumptions, and the saved measurement. change_rate is always
    'set' on edit (a saved assumption is a real answer, 5.8 §3.1)."""
    fv = _fresh_form_values()
    ret = job.get("retention") or {}
    rtype = ret.get("type") or _RETENTION_DEFAULT_BY_TYPE.get(job.get("type"), "days")
    keep = _keep_defaults(job)
    a = job.get("assumptions") or {}
    m = job.get("measured") or {}
    fv.update({
        "type": job.get("type", "versioned"), "source": job.get("source", ""),
        "storage_class": job.get("storage_class", "STANDARD"),
        "schedule": job.get("schedule", ""), "name": job.get("name", ""),
        "enabled": "1" if job.get("enabled", True) else "",
        "retention_type": rtype,
        "retention_days": str(ret.get("days", 180)) if rtype == "days" else "180",
        "retention_count": str(ret.get("count", 30)) if rtype == "count" else "30",
        "keep_last": str(keep["last"]), "keep_daily": str(keep["daily"]),
        "keep_weekly": str(keep["weekly"]), "keep_monthly": str(keep["monthly"]),
        "change_rate_pct": _g(a.get("change_rate_pct"), "0"), "change_rate_touched": "1",
        "packing": "1" if a.get("bundled") else "",
        "pack_member_gb": _g(a.get("pack_member_gb"), "0.05"),
        "mirror": "1" if job.get("mirror") else "0",
    })
    if isinstance(m.get("bytes"), (int, float)) and m["bytes"] > 0:
        fv["size_gb"] = repr(m["bytes"] / (1024 ** 3))
        fv["measured_bytes"] = str(int(m["bytes"]))
        fv["file_count"] = str(int(m.get("count", 0)))
        fv["measured_capped"] = "1" if m.get("capped") else ""
        if isinstance(m.get("at"), str):
            fv["measured_at"] = m["at"]
    return fv


def _g(v, default):
    return default if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def _form_values_from_request(f):
    """The submitted form as a plain dict (POST re-render / recalc), so every typed
    value round-trips intact (spec 5.8 §8). Missing keys fall back to the fresh
    default, so a control the browser omitted (an unchecked box) still renders."""
    fv = _fresh_form_values()
    for k in list(fv):
        if f.get(k) is not None:
            fv[k] = f.get(k)
    # checkboxes: absent means unchecked
    fv["enabled"] = "1" if f.get("enabled") else ""
    fv["packing"] = "1" if f.get("packing") else ""
    fv["change_rate_touched"] = "1" if f.get("change_rate_touched") else ""
    return fv


def _render_job_form(cfg, *, job, fv, errors=None, jobsfile_error=None,
                     status_code=200, acknowledged=None):
    """Server-render the wizard (create or edit), computing the initial figures from
    the frozen model so the page is honest with JS off (spec 5.8 §8). Reused by
    GET /jobs/new, /jobs/<name>/edit, the POST re-render paths and the recalc path."""
    kind = fv.get("prices")
    prices, live_failed = _wizard_prices(cfg, kind)
    saved_class = job.get("storage_class") if job else None
    other = _other_jobs(cfg, exclude=(job or {}).get("name"))
    est, est_error = None, None
    if prices is not None:
        try:
            est = estimate_io.wizard_estimate(fv, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"],
                                              prices, saved_class=saved_class,
                                              other_jobs=other, live_failed=live_failed)
        except ValueError as e:
            est_error = str(e)
    price_stamp = {"kind": (est or {}).get("price_kind", "bundled"),
                   "date": prices.date if prices else None,
                   "region": prices.region if prices else "us-east-1",
                   "live_failed": live_failed}
    # Server-side blocker enforcement is UX-mirrored here: the footer is disabled and
    # the block opens whenever an unacknowledged server blocker stands (5.8 §3.3).
    ack = set(acknowledged or [])
    blockers = (est or {}).get("blockers") or []
    unacked = [b for b in blockers if b["code"] not in ack]
    # Diff-aware `was:` on the edit screen (5.9): compare the SUBMITTED/current values
    # against the SAVED job (re-priced once for the footer's `(was $X)`).
    diff, saved_typical, saved_cmp = {}, None, {}
    if job is not None:
        saved_fv = _saved_form_values(job)
        try:
            saved_typical = estimate_io.wizard_estimate(
                saved_fv, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"], prices, saved_class=saved_class,
                other_jobs=other)["this_job_monthly"] if prices is not None else None
        except ValueError:
            saved_typical = None
        diff = _edit_diff(saved_fv, fv, saved_typical, (est or {}).get("this_job_monthly"))
        # The JS diffs the LIVE form against these SAVED values (never `fv`, which on a
        # re-render is the submitted state); plus the saved typical + schedule prose.
        saved_cmp = {k: saved_fv[k] for k in ("storage_class", "change_rate_pct", "source",
                                              "retention_type", "schedule")}
        saved_cmp["typical"] = saved_typical
        saved_cmp["sched_human"] = diff.get("saved_sched_human")
    bucket = (config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET") or "bw-backups").strip() or "bw-backups"
    return render_template(
        "job_form.html", is_edit=bool(job), job=job, fv=fv, est=est, est_error=est_error,
        has_source=bool(fv.get("source")),
        measured=((est or {}).get("provenance") or {}).get("size") == "measured",
        source_root_host=cfg.get("SOURCE_ROOT_HOST", "/mnt/user"),
        source_root=cfg["SOURCE_ROOT"], storage_classes=jobs_io.STORAGE_CLASSES,
        other_jobs=other, saved_json=(json.dumps(job) if job else "null"),
        price_stamp=price_stamp, errors=errors or {}, jobsfile_error=jobsfile_error,
        blockers=blockers, unacked=unacked, acknowledged=sorted(ack),
        diff=diff, saved_typical=saved_typical, saved_cmp=saved_cmp, bucket=bucket,
        csrf=security.issue_csrf()), status_code


@bp.get("/jobs/new")
def job_new():
    cfg = current_app.config
    fv = _fresh_form_values()
    if request.args.get("prices"):
        fv["prices"] = request.args.get("prices")
    return _render_job_form(cfg, job=None, fv=fv)


@bp.get("/jobs/<name>/edit")
def job_edit(name):
    cfg = current_app.config
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    fv = _saved_form_values(job)
    if request.args.get("prices"):
        fv["prices"] = request.args.get("prices")
    # A blocker already acknowledged on the saved job renders pre-acknowledged (5.9).
    ack = [e.get("code") for e in (job.get("acknowledged") or [])
           if e.get("class") == job.get("storage_class")]
    return _render_job_form(cfg, job=job, fv=fv, acknowledged=ack)


@bp.get("/jobs/browse")
def jobs_browse():
    cfg = current_app.config
    try:
        # Every browsed path is confined to SOURCE_ROOT via safe_resolve/list_dirs.
        dirs = fsbrowse.list_dirs(cfg["SOURCE_ROOT"], request.args.get("path", ""))
    except fsbrowse.PathError:
        abort(404, description="That folder is outside the source root.")  # no path echo
    base = request.args.get("path", "").strip("/")
    return jsonify({"entries": [{"name": d, "path": f"{base}/{d}" if base else d} for d in dirs]})

@bp.get("/jobs/source-size")
def jobs_source_size():
    cfg = current_app.config
    try:
        # Confined via dirsize.dir_size -> fsbrowse.safe_resolve; same no-echo 404
        # contract as /jobs/browse above. The wizard only ever passes FOLDER paths.
        d = dirsize.dir_size(cfg["SOURCE_ROOT"], request.args.get("path", ""))
    except fsbrowse.PathError:
        abort(404, description="That folder is outside the source root.")  # no path echo
    return jsonify(d)

@bp.get("/jobs/estimate.json")
def jobs_estimate_json():
    # Live wizard cost: GET, side-effect-free -> no CSRF needed. `?prices=` overrides
    # PRICES_LIVE for this request (spec 7.9); the extended 8.6 response carries the
    # classes/keep_options/all_jobs/blockers/warnings the create screen re-paints from.
    cfg = current_app.config
    prices, live_failed = _wizard_prices(cfg, request.args.get("prices"))
    if prices is None:
        # Any pricing failure degrades the wizard to "—" rather than 500 it.
        return jsonify({"this_job_monthly": None, "new_total_monthly": None,
                        "price_source": None, "price_date": None})
    name = str(request.args.get("name", "")).strip()
    saved = jobs_io.get(cfg["CONFIG_DIR"], name) if name else None
    saved_class = saved.get("storage_class") if saved else None
    other = _other_jobs(cfg, exclude=name)
    try:
        result = estimate_io.wizard_estimate(request.args, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"],
                                             prices, saved_class=saved_class,
                                             other_jobs=other, live_failed=live_failed)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({**result, "price_source": prices.source, "price_date": prices.date})


@bp.post("/jobs/<name>/assumptions")
def job_assumptions(name):
    # Persist one job's per-job assumptions (change-rate/bundling) so the Cost
    # workbench and the job page light up their [Apply to <job>] control (spec 8.10,
    # Task-12 carry-forward). Accepts unprefixed fields (the contract) OR the cost
    # lever form's `<name>_`-prefixed ones, so both surfaces post here with no JS.
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    if jobs_io.get(cfg["CONFIG_DIR"], name) is None:
        abort(404, description=f"There is no job called {name}")
    f = request.form

    def _pick(key):
        v = f.get(key)
        return v if v is not None else f.get(f"{name}_{key}")
    try:
        change_rate = float(_pick("change_rate_pct") or 0)
        pack_member = float(_pick("pack_member_gb") or 0.05)
    except (TypeError, ValueError):
        abort(400, description="assumptions must be numbers")
    packing = str(_pick("packing") or "").lower() in ("1", "true", "on")
    try:
        jobs_io.set_assumptions(cfg["CONFIG_DIR"], name,
                                {"change_rate_pct": change_rate, "bundled": packing,
                                 "pack_member_gb": pack_member})
    except jobs_io.JobsFileError as e:
        flash(str(e))
        return redirect(url_for("gui.cost_page_view"))
    flash(f"Saved assumptions for {name}.", "success")
    return redirect(url_for("gui.cost_page_view"))


@bp.post("/jobs")
def job_save():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    f = request.form
    fv = _form_values_from_request(f)

    # The edit screen LOCKS the kind and the name (spec 5.9): for an existing job the
    # saved type/name win server-side, so a posted `type` for an existing job is
    # ignored and can never move what is already stored under a recovery model.
    posted_name = f.get("name", "").strip()
    existing = jobs_io.get(cfg["CONFIG_DIR"], posted_name) if posted_name else None
    engine = existing.get("type") if existing else f.get("type", "")
    fv["type"] = engine or fv["type"]

    # The <noscript> Recalculate button (5.8 §8): re-render server-side, SAVE NOTHING.
    if f.get("recalc"):
        return _render_job_form(cfg, job=existing, fv=fv)

    # SERVER-SIDE blocker enforcement (correctness, not just UX): a Snapshot backup on
    # a cold class WON'T RUN. The client-disabled footer is UX only — the server must
    # refuse to save unless every raised blocker is acknowledged (5.8 §3.3 / 8.10).
    cls = f.get("storage_class", "STANDARD")
    acked = set(f.getlist("acknowledge_blocker"))
    blockers = estimate_io._wizard_blockers(engine or "versioned", cls)
    unacked = [b["code"] for b in blockers if b["code"] not in acked]
    if unacked:
        return _render_job_form(cfg, job=existing, fv=fv, acknowledged=acked,
                                status_code=200)

    job = {"name": posted_name, "type": engine, "source": f.get("source", "").strip(),
           "schedule": f.get("schedule", "").strip(),
           "enabled": bool(f.get("enabled")), "storage_class": cls}
    # Persist the change-rate/bundling assumption (7.8) and the measurement (7.8/8.6)
    # taken on the form so the job page and Cost workbench read them back.
    try:
        job["assumptions"] = {"change_rate_pct": float(f.get("change_rate_pct") or 0),
                              "bundled": bool(f.get("packing")),
                              "pack_member_gb": float(f.get("pack_member_gb") or 0.05),
                              "set_at": _now_iso()}
        mb = f.get("measured_bytes")
        if mb and str(mb).strip():
            job["measured"] = {"bytes": int(float(mb)),
                               "count": int(float(f.get("file_count") or 0)),
                               "capped": bool(f.get("measured_capped")),
                               "at": f.get("measured_at") or _now_iso()}
    except (TypeError, ValueError):
        return _render_job_form(cfg, job=existing, fv=fv,
                                errors={"size": "measurement must be numeric"})
    # An acknowledged blocker is recorded on the job (7.8) so the Board never nags.
    if acked:
        job["acknowledged"] = [{"code": c, "class": cls, "at": _now_iso()} for c in acked]
    try:
        if "retention_type" in f:
            job["retention"] = estimate_io.retention_from_form(
                f, default_type=_RETENTION_DEFAULT_BY_TYPE.get(engine, "days"))
        elif engine == "versioned":
            job["keep"] = {k: f.get(f"keep_{k}", "0") for k in ("last", "daily", "weekly", "monthly")}
        elif engine == "versioned-files":
            job["retention_days"] = f.get("retention_days", "90")
        if engine == "archive":
            job["mirror"] = bool(f.get("mirror"))
        jobs_io.upsert(cfg["CONFIG_DIR"], job, source_root=cfg["SOURCE_ROOT"])
    except jobs_io.JobsFileError as e:
        # Corrupt jobs.json (5.8 §8): a 200 RE-RENDER with the sig-failure and every
        # typed value intact — NOT the error page, NOT a flash-and-redirect that would
        # throw away four sections the owner just filled in.
        return _render_job_form(cfg, job=existing, fv=fv, jobsfile_error=str(e))
    except ValueError as e:
        # A validation failure re-renders this page (200) with the message anchored to
        # its section (5.8 §8), keeping every typed value — never the error page.
        return _render_job_form(cfg, job=existing, fv=fv, errors={"form": str(e)})

    # After a successful save re-render the crontab so status.crontab_stale doesn't
    # read true right after creating/editing (Task-4 carry-forward).
    jobs_io.render_crontab(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], cfg["SCRIPTS_DIR"],
                           source_root=cfg["SOURCE_ROOT"])
    flash(f"Saved {job['name']}.", "success")
    if f.get("run_now"):
        runner.trigger_job(cfg["SCRIPTS_DIR"], job["name"])
    return redirect(url_for("gui.job_page", name=job["name"]))

@bp.post("/jobs/<name>/run")
def job_run(name):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    if jobs_io.get(cfg["CONFIG_DIR"], name) is None:
        abort(404, description=f"There is no job called {name}")
    runner.trigger_job(cfg["SCRIPTS_DIR"], name)
    flash(f"Started {name}.")
    return redirect(url_for("gui.jobs_page"))

@bp.post("/jobs/<name>/delete")
def job_delete(name):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    try:
        # Pass cache_dir so the job's caches go with it (Task-4 carry-forward):
        # state/<job>.json, runs.jsonl and points.json are all removed (7.1.9).
        jobs_io.delete(cfg["CONFIG_DIR"], name, cache_dir=cfg["CACHE_DIR"])
    except jobs_io.JobsFileError as e:
        # Corrupt jobs.json: surface a flash rather than a 500, and leave the
        # file untouched (delete builds on _load_strict, which raised).
        flash(str(e))
        return redirect(url_for("gui.jobs_page"))
    flash(f"Deleted {name}.")
    return redirect(url_for("gui.jobs_page"))

# --- Cost workbench (spec 5.6) ---------------------------------------------

@bp.get("/estimate")
def estimate_page():
    # The old cost estimate is now the Cost workbench (spec 5.6 / ruling R-H): the
    # bookmark 301s so it keeps working.
    return redirect(url_for("gui.cost_page_view"), code=301)


@bp.get("/cost")
def cost_page_view():
    cfg = current_app.config
    prices = _prices_for(cfg, request.args.get("prices"))
    cost, error, scrub_month = None, None, 1
    if prices is not None:
        try:
            cost = estimate_io.cost_page(request.args, cfg["CONFIG_DIR"], cfg["CACHE_DIR"],
                                         prices, cfg["SOURCE_ROOT"])
        except ValueError as e:
            # A bad lever value: render the page from the saved scenario so nothing is
            # a dead control, and show the message (the <noscript> path re-renders live).
            error = str(e)
            cost = estimate_io.cost_page({}, cfg["CONFIG_DIR"], cfg["CACHE_DIR"],
                                         prices, cfg["SOURCE_ROOT"])
    if cost is not None:
        months = cost["projection"]["primary"]["months"]
        try:
            scrub_month = max(1, min(len(months), int(request.args.get("month", 1))))
        except (TypeError, ValueError):
            scrub_month = 1
    return render_template("cost.html", cost=cost, error=error, scrub_month=scrub_month,
                           retrieval_tiers=estimate_io.RETRIEVAL_TIERS,
                           csrf=security.issue_csrf())


@bp.get("/cost.json")
@bp.get("/estimate.json")          # alias: /estimate.json and /cost.json are the same JSON
def cost_json():
    cfg = current_app.config
    prices = _prices_for(cfg, request.args.get("prices"))
    if prices is None:
        return jsonify({"error": "Prices are unavailable right now."}), 503
    try:
        return jsonify(estimate_io.cost_page(request.args, cfg["CONFIG_DIR"],
                                             cfg["CACHE_DIR"], prices, cfg["SOURCE_ROOT"]))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_cost_scenario(config_dir, obj) -> None:
    """Atomically write $CONFIG_DIR/cost.json (temp + os.replace) — the persisted
    scenario-wide levers (spec 5.6 band 3)."""
    p = Path(config_dir, "cost.json")
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, p)


@bp.post("/costs/scenario")
def costs_scenario():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    f = request.form
    try:
        rf = float(f.get("restore_fraction"))
        if rf not in (1.0, 0.5, 0.1):
            raise ValueError("Choose how much you'd get back: all of it, half, or a tenth.")
        ry = int(f.get("restores_per_year"))
        if ry < 0:
            raise ValueError("Restores a year must be zero or a positive whole number.")
        tier = f.get("retrieval_tier")
        if tier not in estimate_io.RETRIEVAL_TIERS:
            raise ValueError("Pick a retrieval speed.")
    except (TypeError, ValueError) as e:
        abort(400, description=str(e))          # nothing written on a bad parse (8.10)
    _write_cost_scenario(cfg["CONFIG_DIR"], {"restore_fraction": rf, "restores_per_year": ry,
                                             "retrieval_tier": tier, "set_at": _now_iso()})
    flash("Saved.", "success")
    return redirect(url_for("gui.cost_page_view"))


@bp.post("/costs/refresh")
def costs_refresh():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    bucket = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()
    if not bucket:
        flash("Set an S3 bucket in Setup before refreshing usage.", "blocker")
        return redirect(url_for("gui.cost_page_view"))
    # Detached sysop op (spec 5.6 / 7.7.3): the refresh runs as an operation record,
    # NOT synchronously in the request — no live network at render.
    ops.launch_py(cfg, "app.engine.sysop", ["usage-refresh"], kind="usage-refresh")
    flash("Refreshing usage — watch it in Activity →", "note")
    return redirect(url_for("gui.cost_page_view"))


@bp.post("/costs/billing/refresh")
def costs_billing_refresh():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    ops.launch_py(cfg, "app.engine.sysop", ["billing-check"], kind="billing-check")
    flash("Checking the bill — watch it in Activity →", "note")
    return redirect(url_for("gui.cost_page_view"))


@bp.route("/costs/billing", methods=["GET", "POST"])
def costs_billing():
    # The Cost Explorer credential is edited only at Keys & secrets now (spec 5.6
    # band 5 / 5.12): the old write-only form here is deleted and the route 301s.
    return redirect("/setup/keys#billing", code=301)
