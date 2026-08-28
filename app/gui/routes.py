# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from flask import (Blueprint, redirect, url_for, render_template, request, flash,
                   current_app, abort, Response, jsonify)
from . import config_io, runner, security, provision, media_shares, fsbrowse

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
    sd = cfg["MEDIA_SHARES_DIR"]
    if not request.form.get("enabled"):
        # Disable must work even if MEDIA_ROOT/<name> is no longer a directory
        # (unmounted/removed share) — it's the recovery path for a share that
        # would otherwise make every backup-media.sh run fail with no way to
        # remove it from the GUI. Only the enable path below needs the source
        # dir to exist.
        media_shares.disable(sd, name)
        flash(f"Disabled {name}.")
        return redirect(url_for("gui.shares_page"))
    try:
        # safe_resolve rejects an escaping-symlink share so a symlink target
        # outside MEDIA_ROOT can never be enabled for backup.
        share_dir = fsbrowse.safe_resolve(cfg["MEDIA_ROOT"], name)
    except fsbrowse.PathError:
        abort(404)
    if not share_dir.is_dir():
        abort(404)
    try:
        if request.form.get("mode") == "raw" and request.form.get("raw") is not None:
            media_shares.write_raw(sd, name, request.form["raw"])
        else:
            media_shares.write_selection(sd, name, bool(request.form.get("whole")),
                                         request.form.getlist("folder"))
    except ValueError:
        abort(400)  # no path echo
    flash(f"Saved {name}.")
    return redirect(url_for("gui.shares_page"))
