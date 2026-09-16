# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import (config_io, runner, security, provision, fsbrowse, estimate_io, jobs_io,
               dirsize, attributions, status, vocab)
from .storage_advice import storage_class_info
from ..estimator.model import estimate, STORAGE_CLASSES
from ..estimator.prices import load_prices
from ..estimator import usage

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
    try:
        jobs_io.delete(current_app.config["CONFIG_DIR"], name)
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
