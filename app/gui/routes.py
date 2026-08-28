# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from dataclasses import asdict
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import config_io, runner, security, estimate_io
from ..estimator.model import estimate, STORAGE_CLASSES, effective_retention_days
from ..estimator.prices import load_prices

bp = Blueprint("gui", __name__)

@bp.get("/")
def index():
    return redirect(url_for("gui.config_page"))

@bp.get("/config")
def config_page():
    cfg = current_app.config
    keys = config_io.template_keys(cfg["TEMPLATE_PATH"])
    values = config_io.read_backup_env(cfg["CONFIG_DIR"])
    fields = [{"key": k, "value": values.get(k, "")} for k in keys if k not in config_io.SECRET_KEYS]
    return render_template("config.html",
                           fields=fields,
                           includes=config_io.read_includes(cfg["CONFIG_DIR"]),
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
    if "includes" in request.form:
        config_io.write_includes(cfg["CONFIG_DIR"], request.form["includes"])
    config_io.write_secrets(cfg["CONFIG_DIR"], {k: request.form.get(k, "") for k in config_io.SECRET_KEYS})
    flash("Configuration saved.")
    return redirect(url_for("gui.config_page"))

@bp.get("/status")
def status_page():
    cfg = current_app.config
    states = {p: runner.read_state(cfg["CACHE_DIR"], p) for p in runner.PIPELINES}
    return render_template("status.html", states=states, csrf=security.issue_csrf())

@bp.post("/run/<pipeline>")
def run(pipeline):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
    if pipeline not in runner.PIPELINES:
        abort(404)
    runner.trigger_backup(current_app.config["SCRIPTS_DIR"], pipeline)
    flash(f"Started {pipeline} backup.")
    return redirect(url_for("gui.status_page"))

@bp.get("/logs")
def logs():
    n = request.args.get("tail", default=200, type=int)
    return Response(runner.tail_log(current_app.config["CACHE_DIR"], n), mimetype="text/plain")

def _compute(config_dir, params):
    scenario = estimate_io.scenario_from_params(params, config_dir)
    return scenario, estimate(scenario, load_prices(scenario.region))

@bp.get("/estimate")
def estimate_page():
    cfg = current_app.config
    d = estimate_io.form_defaults(cfg["CONFIG_DIR"])
    est = None
    error = None
    appdata_retention = effective_retention_days(
        keep_last=d["keep_last"], keep_daily=d["keep_daily"],
        keep_weekly=d["keep_weekly"], keep_monthly=d["keep_monthly"])
    try:
        scenario, est = _compute(cfg["CONFIG_DIR"], request.args)
        appdata_retention = scenario.appdata.versioning_retention_days
    except ValueError as e:
        error = str(e)
    return render_template("estimate.html", d=d, est=est, error=error,
                           appdata_retention=appdata_retention,
                           storage_classes=STORAGE_CLASSES,
                           retrieval_tiers=estimate_io.RETRIEVAL_TIERS)

@bp.get("/estimate.json")
def estimate_json():
    cfg = current_app.config
    try:
        scenario, est = _compute(cfg["CONFIG_DIR"], request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    payload = asdict(est)
    payload["appdata_retention_days"] = scenario.appdata.versioning_retention_days
    return jsonify(payload)
