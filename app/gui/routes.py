# app/gui/routes.py — view functions. Calls config_io/runner; never touches files/subprocess directly.
from __future__ import annotations
from flask import Blueprint, redirect, url_for

bp = Blueprint("gui", __name__)

@bp.get("/")
def index():
    return redirect(url_for("gui.config_page"))

# Temporary stub — replaced in Task 4.
@bp.get("/config")
def config_page():
    from flask import render_template
    return render_template("config.html")

# Temporary stub — replaced in Task 5.
@bp.get("/status")
def status_page():
    return ""
