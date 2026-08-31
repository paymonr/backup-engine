# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import config_io, runner, security, provision, fsbrowse, estimate_io, jobs_io, dirsize, attributions
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
    return redirect(url_for("gui.jobs_page"))

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
        abort(400)
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
    return render_template("provision_home.html", csrf=security.issue_csrf())

@bp.get("/provision/manual")
def provision_manual():
    return render_template("provision_manual.html", csrf=security.issue_csrf(),
                           bucket="", region="us-east-1", policy=None, console=None, error=None)

@bp.post("/provision/manual/render")
def provision_manual_render():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
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
        abort(400)
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
                               error=f"Validation failed at the {e.step} step — nothing saved."), 400
    config_io.write_secrets(cfg["CONFIG_DIR"],
                            {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret})
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {**config_io.read_backup_env(cfg["CONFIG_DIR"]),
                                "AWS_REGION": region, "S3_BUCKET": bucket})
    flash("Runtime key validated and saved. Reminder: confirm bucket versioning is ON.")
    return redirect(url_for("gui.provision_home"))

@bp.get("/provision/automated")
def provision_automated():
    return render_template("provision_automated.html", csrf=security.issue_csrf(),
                           bucket="", region="us-east-1", error=None)


@bp.post("/provision/automated")
def provision_automated_run():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
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
    except provision.AccountLookupError:
        return render_template("provision_automated.html", csrf=security.issue_csrf(),
                               bucket=override, region=region,
                               error="Couldn't read your AWS account from those admin "
                                     "credentials — check the key and try again. Nothing was saved."), 400
    except provision.TofuError as e:
        return render_template("provision_automated.html", csrf=security.issue_csrf(),
                               bucket=override, region=region,
                               error=f"Automated provisioning failed at tofu {e.phase} — nothing saved."), 400
    finally:
        # discard transient admin creds from this frame regardless of outcome
        admin_key = admin_secret = session_token = None
    config_io.write_secrets(cfg["CONFIG_DIR"],
                            {"AWS_ACCESS_KEY_ID": result["AWS_ACCESS_KEY_ID"],
                             "AWS_SECRET_ACCESS_KEY": result["AWS_SECRET_ACCESS_KEY"]})
    config_io.write_backup_env(cfg["TEMPLATE_PATH"], cfg["CONFIG_DIR"],
                               {**config_io.read_backup_env(cfg["CONFIG_DIR"]),
                                "AWS_REGION": result["region"], "S3_BUCKET": result["bucket"]})
    flash(f"Provisioned {result['bucket']} in {result['region']} and saved the runtime key.")
    return redirect(url_for("gui.provision_home"))

@bp.get("/jobs")
def jobs_page():
    cfg = current_app.config
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    rows = [{**j, "state": runner.read_state(cfg["CACHE_DIR"], j["name"])} for j in jobs]
    return render_template("jobs.html", jobs=rows, csrf=security.issue_csrf())

@bp.get("/jobs/new")
def job_new():
    return render_template("job_form.html", job=None, source_root=current_app.config["SOURCE_ROOT"],
                           storage_classes=jobs_io.STORAGE_CLASSES, csrf=security.issue_csrf())

@bp.get("/jobs/<name>/edit")
def job_edit(name):
    job = jobs_io.get(current_app.config["CONFIG_DIR"], name)
    if job is None:
        abort(404)
    return render_template("job_form.html", job=job, source_root=current_app.config["SOURCE_ROOT"],
                           storage_classes=jobs_io.STORAGE_CLASSES, csrf=security.issue_csrf())

@bp.get("/jobs/browse")
def jobs_browse():
    cfg = current_app.config
    try:
        # Every browsed path is confined to SOURCE_ROOT via safe_resolve/list_dirs.
        dirs = fsbrowse.list_dirs(cfg["SOURCE_ROOT"], request.args.get("path", ""))
    except fsbrowse.PathError:
        abort(404)  # no path echo
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
        abort(404)  # no path echo
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
    try:
        result = estimate_io.wizard_estimate(request.args, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"], prices)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({**result, "price_source": prices.source, "price_date": prices.date})

@bp.post("/jobs")
def job_save():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
    cfg = current_app.config
    f = request.form
    job = {"name": f.get("name", "").strip(), "type": f.get("type", ""),
           "source": f.get("source", "").strip(), "schedule": f.get("schedule", "").strip(),
           "enabled": bool(f.get("enabled")), "storage_class": f.get("storage_class", "STANDARD")}
    if job["type"] == "versioned":
        job["keep"] = {k: f.get(f"keep_{k}", "0") for k in ("last", "daily", "weekly", "monthly")}
    elif job["type"] == "versioned-files":
        job["retention_days"] = f.get("retention_days", "90")
    else:
        job["mirror"] = bool(f.get("mirror"))
    try:
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
        abort(400)
    cfg = current_app.config
    if jobs_io.get(cfg["CONFIG_DIR"], name) is None:
        abort(404)
    runner.trigger_job(cfg["SCRIPTS_DIR"], name)
    flash(f"Started {name}.")
    return redirect(url_for("gui.jobs_page"))

@bp.post("/jobs/<name>/delete")
def job_delete(name):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
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
    return scenario, estimate(scenario, prices)

@bp.get("/estimate")
def estimate_page():
    cfg = current_app.config
    d = estimate_io.form_defaults(cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"])
    est = None
    error = None
    try:
        _scn, est = _compute(cfg, request.args)
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
    current = (estimate_io.current_costs(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], prices)
               if prices is not None else {"available": False})
    billing = estimate_io.billing_view(cfg["CONFIG_DIR"])
    return render_template("estimate.html", d=d, est=est, error=error,
                           storage_classes=STORAGE_CLASSES,
                           retrieval_tiers=estimate_io.RETRIEVAL_TIERS,
                           current=current, billing=billing,
                           csrf=security.issue_csrf())

@bp.get("/estimate.json")
def estimate_json():
    cfg = current_app.config
    try:
        _scn, est = _compute(cfg, request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(asdict(est))

@bp.post("/costs/refresh")
def costs_refresh():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
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
        abort(400)
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
