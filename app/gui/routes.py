# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import (config_io, runner, security, provision, fsbrowse, estimate_io, jobs_io,
               dirsize, attributions, status, vocab, points, readiness)
from .storage_advice import storage_class_info
from ..estimator.model import estimate, STORAGE_CLASSES
from ..estimator.prices import load_prices
from ..estimator import usage
from ..engine import cron, runs

bp = Blueprint("gui", __name__)

@bp.get("/about")
def about_page():
    return render_template("about.html", third_party=attributions.THIRD_PARTY,
                           version=current_app.config.get("VERSION", "0.1.0-dev"))

@bp.get("/")
def index():
    # First run (no runtime key + bucket yet) lands on the provisioning wizard;
    # once set up, the Board is home (spec 5.1, ruling R-H).
    if not config_io.is_provisioned(current_app.config["CONFIG_DIR"]):
        return redirect(url_for("gui.provision_home"))
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


def _board_cost(cfg) -> dict:
    """The Board cost strip (spec 5.1 band 4 / 8.1 `cost`), from CACHES ONLY.

    Ruling R-A: `estimate_io.board_cost` does not exist yet (Task 12 adds it). Here
    the strip reads `current_costs` (the priced usage cache) for the measured "in
    the bucket now" size and a PLACEHOLDER projected `model_monthly`, and the
    cache-only `read_billing_cache` for the invoice — never Cost Explorer, never the
    live model (the test asserts no CE call during render). Task 12 replaces this
    with the real `board_cost` and updates the Board test."""
    config_dir, cache_dir = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    region = estimate_io._region(config_dir)
    try:
        prices = load_prices(region, cache_dir=cache_dir, live=cfg["PRICES_LIVE"])
    except Exception:
        prices = None
    current = (estimate_io.current_costs(config_dir, cache_dir, prices)
               if prices is not None else {"available": False})
    billing = estimate_io.read_billing_cache(cache_dir)   # cache-only; no Cost Explorer

    price = ({"kind": prices.source, "region": region, "date": prices.date}
             if prices is not None else None)

    if not current.get("prefixes"):
        return {"in_bucket_bytes": None, "in_bucket_at": None, "prefix_count": 0,
                "invoice": _invoice_from_cache(billing), "model_monthly": None,
                "model_monthly_provenance": "projected", "model_floor": None,
                "delta": None, "why_high_note": None, "per_job": [], "price": price}

    prefixes = current["prefixes"]
    in_bucket_bytes = sum(p["bytes"] for p in prefixes)
    # PLACEHOLDER projected figure (R-A): the usage cache priced at today's storage
    # rate. Computed from measured sizes only, so it is `projected` (no mark, 4.6);
    # Task 12's real model adds old-versions and may make it `assumed`.
    model_monthly = current.get("total_monthly")
    invoice = _invoice_from_cache(billing)
    delta = _cost_delta(model_monthly, invoice)
    per_job = [{
        "name": p["prefix"].split("/")[-1], "size_bytes": p["bytes"], "size_provenance": "measured",
        "file_count": None, "ext": None, "old_versions_gb": None,
        "tier_label": vocab.CLASS_NAMES.get(p["class"], p["class"]),
        "storage_class": p["class"], "monthly": p["monthly"],
        "monthly_provenance": "projected", "settles": None,
    } for p in prefixes]
    return {
        "in_bucket_bytes": in_bucket_bytes, "in_bucket_at": current.get("fetched_at"),
        "prefix_count": len(prefixes), "invoice": invoice,
        "model_monthly": model_monthly, "model_monthly_provenance": "projected",
        "model_floor": None, "delta": delta, "why_high_note": None,
        "per_job": per_job, "price": price,
    }


def _invoice_from_cache(billing) -> dict | None:
    """The most recent month in the cached billing view, or None (8.1 `invoice`)."""
    months = billing.get("months") if billing.get("connected") else None
    if not months:
        return None
    last = months[-1]
    return {"month": last.get("month"), "amount": last.get("amount"),
            "tag_scoped": bool(billing.get("tag"))}


def _cost_delta(model, invoice) -> dict | None:
    """model − invoice, with the short form and the 15/30 verdict bands (spec 4.6).
    None unless both figures exist."""
    if model is None or not invoice or invoice.get("amount") in (None, 0):
        return None
    amount = model - invoice["amount"]
    pct = amount / invoice["amount"] * 100.0
    ap = abs(pct)
    if ap < 0.05:
        direction, short = "matches", "±0.0% · matches"
    else:
        direction = "model runs high" if amount > 0 else "model runs low"
        short = f"{pct:+.1f}%".replace("-", "−") + f" · {direction}"
    if ap <= 15:
        verdict = ("close enough to trust, and it errs on the expensive side" if amount >= 0
                   else "close enough to trust, and it errs on the cheap side")
    elif ap <= 30:
        verdict = ("model runs high — worth a look at the assumptions" if amount >= 0
                   else "model runs low — worth a look at the assumptions")
    else:
        verdict = "far apart — check the assumptions and whether the invoice covers more than these backups"
    return {"amount": round(amount, 2), "pct": round(pct, 1), "verdict": verdict, "short": short}


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


def _ledger(cfg, name, tz, cells=30) -> dict:
    """The `Last 30 runs` ledger strip (spec 5.2): the newest `cells` backup runs,
    oldest-first, padded with dim placeholders, each cell carrying a squared-scale
    bar height. Built here (not from status.job's 14-cell board strip) because the
    job-page ledger is 30 wide; it reuses status._cell/_dim_cell so the cell shape
    matches the Board's exactly (kind == 'backup' only, spec 5.2/6.5)."""
    records = runs.read_runs(cfg["CACHE_DIR"], name).records
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
    return {"cells": padded, "shown": len(row), "ok": ok, "failed": failed,
            "oldest": row[0]["started_at"] if row else None,
            "aria": aria, "median_s": median_s, "maxd": maxd}


def _job_cost_band(cfg, job) -> dict:
    """`What this job costs` (spec 5.2), CACHE-ONLY per Ruling R-I.

    `estimate_io.job_cost_band` (the real four figures: First bill / By month 6 /
    Every month after) is added in Task 12; until then the time-based figures are
    None → rendered `not priced yet`. Here we reuse the Board's cache-only cost
    (`_board_cost`, Ruling R-A) for this job's measured size and its projected
    monthly, plus the whole-account invoice/delta — NEVER Cost Explorer, never the
    live model. Task 12 wires `job_cost_band` and updates the test."""
    name = job.get("name")
    bc = _board_cost(cfg)
    per = next((p for p in bc.get("per_job") or [] if p["name"] == name), None)
    cached = usage.load_cached(cfg["CACHE_DIR"]) or {}
    key = "appdata" if job.get("type") == "versioned" else f"media/{name}"
    file_count = ((cached.get("data") or {}).get(key) or {}).get("count")
    shared_by = sum(1 for j in jobs_io.load(cfg["CONFIG_DIR"]) if j.get("type") == "versioned")
    return {
        "in_bucket_bytes": per["size_bytes"] if per else None,
        "in_bucket_at": bc.get("in_bucket_at"),
        "size_provenance": per["size_provenance"] if per else "measured",
        "file_count": file_count,
        "monthly": per["monthly"] if per else None,
        "monthly_provenance": per["monthly_provenance"] if per else "projected",
        "tier_label": (per["tier_label"] if per else None),
        "storage_class": per["storage_class"] if per else job.get("storage_class"),
        "model_monthly": bc.get("model_monthly"),
        "invoice": bc.get("invoice"), "delta": bc.get("delta"), "price": bc.get("price"),
        # whole snapshot store shared by N versioned jobs (spec 5.2 note)
        "shared_store": job.get("type") == "versioned" and shared_by > 1,
        "shared_by": shared_by,
        # Task 12 (estimate_io.job_cost_band) fills these; None → "not priced yet".
        "first_bill": None, "by_month_6": None, "settled": None,
    }


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
    # cache-only. Prices are guarded like the Board; the restore COST is None until
    # estimate_io.restore_quote exists (Task 12) — the template prints "not priced
    # yet". A pricing failure must degrade the rail, never 500 the page.
    region = estimate_io._region(cfg["CONFIG_DIR"])
    try:
        prices = load_prices(region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
    except Exception:
        prices = None
    rec = readiness.recovery_summary(cfg, prices, crontab_stale=st.get("crontab_stale"))
    rjob = next((j for j in rec.get("jobs", []) if j["name"] == name), None)
    try:
        schedule_desc = cron.describe(job_def.get("schedule", ""))
    except Exception:
        schedule_desc = job_def.get("schedule", "")
    return render_template(
        "job.html", s=st, job=job_def, pv=pv, rec=rec, rjob=rjob,
        cost=_job_cost_band(cfg, job_def), ident=_job_identity(cfg, job_def),
        ledger=_ledger(cfg, name, tz), keep_label=keep_label,
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

@bp.get("/config")
def config_page():
    cfg = current_app.config
    keys = config_io.template_keys(cfg["TEMPLATE_PATH"])
    values = config_io.read_backup_env(cfg["CONFIG_DIR"])
    fields = [{"key": k, "value": values.get(k, "")} for k in keys if k not in config_io.SECRET_KEYS]
    return render_template("config.html",
                           fields=fields,
                           secret_keys=config_io.SECRET_KEYS,
                           secret_status=config_io.secrets_status(cfg["CONFIG_DIR"]),
                           secret_mode=config_io.secrets_mode(cfg["CONFIG_DIR"]),
                           csrf=security.issue_csrf())

@bp.post("/config")
def config_save():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    keys = [k for k in config_io.template_keys(cfg["TEMPLATE_PATH"]) if k not in config_io.SECRET_KEYS]
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {k: request.form.get(k, "") for k in keys})
    config_io.write_secrets(cfg["CONFIG_DIR"], {k: request.form.get(k, "") for k in config_io.SECRET_KEYS})
    flash("Configuration saved.")
    return redirect(url_for("gui.config_page"))

@bp.get("/logs")
def logs():
    n = request.args.get("tail", default=200, type=int)
    return Response(runner.tail_log(current_app.config["CACHE_DIR"], n), mimetype="text/plain")

@bp.get("/provision")
def provision_home():
    cfg = current_app.config
    env = config_io.read_backup_env(cfg["CONFIG_DIR"])
    return render_template("provision_home.html", csrf=security.issue_csrf(),
                           provisioned=config_io.is_provisioned(cfg["CONFIG_DIR"]),
                           bucket=env.get("S3_BUCKET", ""), region=env.get("AWS_REGION", ""))

@bp.get("/provision/manual")
def provision_manual():
    return render_template("provision_manual.html", csrf=security.issue_csrf(),
                           bucket="", region="us-east-1", policy=None, console=None, error=None)

@bp.post("/provision/manual/render")
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

@bp.get("/provision/scripted")
def provision_scripted():
    return render_template("provision_scripted.html", csrf=security.issue_csrf())

@bp.post("/provision/validate")
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
    flash("Runtime key validated and saved. Reminder: confirm bucket versioning is ON. Next: create your first backup job.")
    return redirect(url_for("gui.jobs_page"))

@bp.get("/provision/automated")
def provision_automated():
    return render_template("provision_automated.html", csrf=security.issue_csrf(),
                           bucket="", region="us-east-1", error=None)


@bp.post("/provision/automated")
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
    flash(f"Provisioned {result['bucket']} in {result['region']} and saved the runtime key. Next: create your first backup job.")
    return redirect(url_for("gui.jobs_page"))

@bp.get("/jobs")
def jobs_page():
    # The job table folded into the Board (spec 5.1, ruling R-H): `/jobs` is now a
    # permanent redirect home. Save/run/delete still redirect here by name, which
    # lands the user on the Board.
    return redirect(url_for("gui.index"), code=301)

def _class_panel_context(cfg):
    """Pricing-derived storage-class panel data for the job form. Guarded: a
    pricing failure must degrade the panel to empty rather than 500 the page."""
    region = estimate_io._region(cfg["CONFIG_DIR"])
    try:
        prices = load_prices(region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
        class_info = storage_class_info(prices)
        price_stamp = {"source": prices.source, "date": prices.date}
    except Exception:
        class_info, price_stamp = [], {"source": None, "date": None}
    return class_info, price_stamp

@bp.get("/jobs/new")
def job_new():
    cfg = current_app.config
    class_info, price_stamp = _class_panel_context(cfg)
    return render_template("job_form.html", job=None, source_root=cfg["SOURCE_ROOT"],
                           storage_classes=jobs_io.STORAGE_CLASSES,
                           class_info=class_info, price_stamp=price_stamp,
                           csrf=security.issue_csrf())

@bp.get("/jobs/<name>/edit")
def job_edit(name):
    cfg = current_app.config
    job = jobs_io.get(cfg["CONFIG_DIR"], name)
    if job is None:
        abort(404, description=f"There is no job called {name}")
    class_info, price_stamp = _class_panel_context(cfg)
    return render_template("job_form.html", job=job, source_root=cfg["SOURCE_ROOT"],
                           storage_classes=jobs_io.STORAGE_CLASSES,
                           class_info=class_info, price_stamp=price_stamp,
                           csrf=security.issue_csrf())

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
    # Live wizard cost: GET, side-effect-free -> no CSRF needed.
    cfg = current_app.config
    region = estimate_io._region(cfg["CONFIG_DIR"])
    try:
        prices = load_prices(region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
    except Exception:
        # Belt-and-suspenders: load_prices no longer raises for an un-bundled region
        # (it falls back to us-east-1), but any future pricing failure must degrade
        # the wizard to "—" rather than 500.
        return jsonify({"this_job_monthly": None, "new_total_monthly": None,
                        "price_source": None, "price_date": None})
    name = str(request.args.get("name", "")).strip()
    saved = jobs_io.get(cfg["CONFIG_DIR"], name) if name else None
    saved_class = saved.get("storage_class") if saved else None
    try:
        result = estimate_io.wizard_estimate(request.args, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"],
                                             prices, saved_class=saved_class)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({**result, "price_source": prices.source, "price_date": prices.date})

@bp.post("/jobs")
def job_save():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    f = request.form
    job = {"name": f.get("name", "").strip(), "type": f.get("type", ""),
           "source": f.get("source", "").strip(), "schedule": f.get("schedule", "").strip(),
           "enabled": bool(f.get("enabled")), "storage_class": f.get("storage_class", "STANDARD")}
    try:
        # The wizard's retention-policy selector posts retention_type + the matching
        # field (retention_days/retention_count/keep_*); jobs_io.upsert -> validate ->
        # _normalize_retention normalizes/validates it. Older/direct callers (no
        # retention_type) fall back to the pre-selector per-type params, same as
        # before. retention_from_form raises ValueError on an unrecognized
        # retention_type -- inside this try so it 400s like any other bad-input
        # ValueError, rather than an unhandled 500.
        if "retention_type" in f:
            job["retention"] = estimate_io.retention_from_form(f)
        elif job["type"] == "versioned":
            job["keep"] = {k: f.get(f"keep_{k}", "0") for k in ("last", "daily", "weekly", "monthly")}
        elif job["type"] == "versioned-files":
            job["retention_days"] = f.get("retention_days", "90")
        if job["type"] == "archive":
            job["mirror"] = bool(f.get("mirror"))
        jobs_io.upsert(cfg["CONFIG_DIR"], job, source_root=cfg["SOURCE_ROOT"])
    except jobs_io.JobsFileError as e:
        # The on-disk jobs.json is corrupt: don't clobber the user's bytes, and
        # don't 500 — tell them to fix the file (message has no path echo).
        flash(str(e))
        return redirect(url_for("gui.jobs_page"))
    except ValueError:
        abort(400)  # normal validation failure; no echo of paths
    flash(f"Saved job {job['name']}.")
    return redirect(url_for("gui.jobs_page"))

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

def _compute(cfg, params):
    cached = usage.load_cached(cfg["CACHE_DIR"])
    scenario = estimate_io.scenario_from_params(params, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"],
                                                usage=(cached or {}).get("data"))
    prices = load_prices(scenario.region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
    return scenario, estimate(scenario, prices), prices

@bp.get("/estimate")
def estimate_page():
    cfg = current_app.config
    d = estimate_io.form_defaults(cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"])
    est = None
    bundle = None
    error = None
    try:
        _scn, est, prices_wf = _compute(cfg, request.args)
        bundle = estimate_io.projection_bundle(_scn, prices_wf)
    except ValueError as e:
        error = str(e)
    # Current spend is independent of the (possibly invalid) live what-if params —
    # it prices the last refreshed real usage, so compute it off the saved region.
    # Guard the pricing load: with FIX 1 load_prices no longer raises for an
    # un-bundled region, but a total pricing failure must degrade current-spend to
    # "unavailable" rather than 500 the whole page.
    region = estimate_io._region(cfg["CONFIG_DIR"])
    try:
        prices = load_prices(region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
    except Exception:
        prices = None
    class_info = storage_class_info(prices) if prices is not None else []
    current = (estimate_io.current_costs(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], prices)
               if prices is not None else {"available": False})
    billing = estimate_io.billing_view(cfg["CONFIG_DIR"])
    return render_template("estimate.html", d=d, est=est, error=error, bundle=bundle,
                           storage_classes=STORAGE_CLASSES,
                           retrieval_tiers=estimate_io.RETRIEVAL_TIERS,
                           current=current, billing=billing, class_info=class_info,
                           csrf=security.issue_csrf())

@bp.get("/estimate.json")
def estimate_json():
    cfg = current_app.config
    try:
        scn, est, prices = _compute(cfg, request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    bundle = estimate_io.projection_bundle(scn, prices)
    return jsonify({**asdict(est), "projection": bundle})

@bp.post("/costs/refresh")
def costs_refresh():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    bucket = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()
    if not bucket:
        flash("Set an S3 bucket in Config before refreshing usage.")
        return redirect(url_for("gui.estimate_page"))
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    # Both "archive" and "versioned-files" jobs write to their own media/<name> S3
    # prefix (see estimate_io._size_for) -- both must be scanned for current spend.
    media_jobs = [j["name"] for j in jobs if j.get("type") in ("archive", "versioned-files")]
    has_versioned = any(j.get("type") == "versioned" for j in jobs)
    # The container's rendered rclone.conf already carries the runtime key +
    # endpoint (scripts/lib/rclone-conf.sh) — no creds needed here, and none new.
    rclone_config = str(Path(cfg["CACHE_DIR"], "rclone.conf"))
    data = usage.collect_usage(bucket, media_jobs, has_versioned, rclone_config=rclone_config)
    usage.save_cached(cfg["CACHE_DIR"], data)
    flash("Usage refreshed.")
    return redirect(url_for("gui.estimate_page"))

@bp.post("/costs/billing")
def costs_billing():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    if request.form.get("disconnect"):
        config_io.clear_cost_explorer_creds(cfg["CONFIG_DIR"])
        flash("Disconnected AWS billing.")
        return redirect(url_for("gui.estimate_page"))
    config_io.write_secrets(cfg["CONFIG_DIR"],
                            {k: request.form.get(k, "") for k in config_io.COST_EXPLORER_KEYS})
    tag = request.form.get("COST_EXPLORER_TAG", "").strip()
    if tag:
        config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                                   {**config_io.read_backup_env(cfg["CONFIG_DIR"]),
                                    "COST_EXPLORER_TAG": tag})
    flash("Connected AWS billing.")
    return redirect(url_for("gui.estimate_page"))
