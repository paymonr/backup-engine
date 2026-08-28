# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import config_io, runner, security, media_shares, fsbrowse

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

@bp.get("/shares")
def shares_page():
    cfg = current_app.config
    shares = media_shares.list_shares(cfg["MEDIA_ROOT"], cfg["MEDIA_SHARES_DIR"])
    return render_template("shares.html", shares=shares, csrf=security.issue_csrf())

@bp.get("/shares/browse")
def shares_browse():
    cfg = current_app.config
    share = request.args.get("share", "")
    rel = request.args.get("path", "")
    if not media_shares.valid_name(share):
        abort(404)
    try:
        # Resolve the share segment through safe_resolve too: a symlink directly
        # under MEDIA_ROOT must not let the browse root escape confinement.
        share_root = fsbrowse.safe_resolve(cfg["MEDIA_ROOT"], share)
        dirs = fsbrowse.list_dirs(share_root, rel)
    except fsbrowse.PathError:
        abort(404)  # no path echo
    base = rel.strip("/")
    entries = [{"name": d, "path": f"{base}/{d}" if base else d} for d in dirs]
    return jsonify({"entries": entries})

@bp.post("/shares/<name>")
def shares_save(name):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400)
    cfg = current_app.config
    if not media_shares.valid_name(name):
        abort(404)
    try:
        # safe_resolve rejects an escaping-symlink share so a symlink target
        # outside MEDIA_ROOT can never be enabled for backup.
        share_dir = fsbrowse.safe_resolve(cfg["MEDIA_ROOT"], name)
    except fsbrowse.PathError:
        abort(404)
    if not share_dir.is_dir():
        abort(404)
    sd = cfg["MEDIA_SHARES_DIR"]
    if not request.form.get("enabled"):
        media_shares.disable(sd, name)
        flash(f"Disabled {name}.")
        return redirect(url_for("gui.shares_page"))
    if request.form.get("mode") == "raw" and request.form.get("raw") is not None:
        media_shares.write_raw(sd, name, request.form["raw"])
    else:
        media_shares.write_selection(sd, name, bool(request.form.get("whole")),
                                     request.form.getlist("folder"))
    flash(f"Saved {name}.")
    return redirect(url_for("gui.shares_page"))
