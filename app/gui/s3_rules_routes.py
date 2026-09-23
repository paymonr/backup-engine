# app/gui/s3_rules_routes.py — S3 rules actions (spec 2026-09-23 §5). Phase A: Check now
# and acknowledging a tamper alarm. Task 12: Refresh now (a detached storage summary scan).
# Registered on the shared `gui` blueprint.
from __future__ import annotations

from urllib.parse import quote

from flask import abort, current_app, flash, redirect, request, url_for

from ..engine import lifecycle
from . import config_io, jobs_io, ops, s3_rules, security
from .routes import bp


def _csrf_or_400():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")


def _unprovisioned():
    """An unprovisioned install has no bucket/role to act on -- both actions are a
    no-op redirect rather than a call into lifecycle (which would need a bucket)."""
    return not config_io.is_provisioned(current_app.config["CONFIG_DIR"])


@bp.post("/setup/s3-rules/check")
def s3_rules_check():
    _csrf_or_400()
    if _unprovisioned():
        return redirect(url_for("gui.setup_page"))
    for category, msg in s3_rules.check_all(current_app.config):
        flash(msg, category)
    return redirect(url_for("gui.setup_page"))


@bp.post("/setup/s3-rules/acknowledge")
def s3_rules_acknowledge():
    _csrf_or_400()
    if _unprovisioned():
        return redirect(url_for("gui.setup_page"))
    cfg = current_app.config
    seen = (request.form.get("seen") or "").strip() or None
    try:
        lifecycle.acknowledge(cfg["CACHE_DIR"], seen=seen)
        still = s3_rules.open_alarm(cfg) is not None
    except Exception:                                        # noqa: BLE001 — never a 500 (O2)
        flash("Couldn't clear the S3 rules alarm — try again.", "warning")
        return redirect(url_for("gui.setup_page"))
    if still:
        flash("Noted. A newer S3 rules alarm arrived after this page loaded — it's still shown.", "warning")
    else:
        flash("Noted — the S3 rules alarm is cleared.", "success")
    return redirect(url_for("gui.setup_page"))


def _storage_url(key: str = "") -> str:
    """/setup/storage, reopening the side editor on `key` when given."""
    return "/setup/storage" + (f"?edit={quote(key, safe='')}" if key else "")


def _app_folders(cfg) -> set[tuple[str, str]]:
    base = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    return {(b, f.folder) for b in lifecycle.buckets_for(base, jobs) for f in lifecycle.folders_for(b, base, jobs)}


@bp.post("/setup/storage/refresh")
def setup_storage_refresh():
    """Refresh now (spec §4): a detached storage summary of one app folder (progress in Activity)."""
    _csrf_or_400()
    if _unprovisioned():
        return redirect(url_for("gui.setup_page"))
    cfg = current_app.config
    bucket, folder = request.form.get("bucket", "").strip(), request.form.get("folder", "")
    if (bucket, folder) not in _app_folders(cfg):
        abort(404, description="That folder isn't one of backup-engine's.")
    ops.launch_py(cfg, "app.engine.sysop", ["storage-summary", "--bucket", bucket, "--folder", folder],
                  kind="storage-summary")
    flash("Refreshing the storage summary — watch it in Activity →", "note")
    return redirect(_storage_url(request.form.get("key", "")))
