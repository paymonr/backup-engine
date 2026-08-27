# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from flask import Blueprint, redirect, url_for, render_template, request, flash, current_app, abort, Response
from . import config_io, runner, security, provision

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

@bp.get("/provision")
def provision_home():
    return render_template("provision_home.html", csrf=security.issue_csrf())

@bp.get("/provision/manual")
def provision_manual():
    return render_template("provision_manual.html", csrf=security.issue_csrf(),
                           bucket="", region="", policy=None, console=None, error=None)

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
    abort(501)  # implemented in Task 7

@bp.post("/provision/automated")
def provision_automated_run():
    abort(501)  # implemented in Task 7
