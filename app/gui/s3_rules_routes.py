# app/gui/s3_rules_routes.py — S3 rules actions (spec 2026-09-23 §5). Phase A: Check now
# and acknowledging a tamper alarm. Registered on the shared `gui` blueprint.
from __future__ import annotations

from flask import abort, current_app, flash, redirect, request, url_for

from ..engine import lifecycle
from . import config_io, s3_rules, security
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
    lifecycle.acknowledge(current_app.config["CACHE_DIR"])
    flash("Noted — the S3 rules alarm is cleared.", "success")
    return redirect(url_for("gui.setup_page"))
