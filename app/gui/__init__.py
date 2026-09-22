# app/gui/__init__.py
from __future__ import annotations
import os
from flask import Flask, render_template, jsonify, request, current_app
from werkzeug.exceptions import HTTPException, default_exceptions

from . import vocab

# Werkzeug's own default description for each status, so the error handler can
# tell "the caller passed a description" from "this is the framework default".
_DEFAULT_DESC = {code: exc().description for code, exc in default_exceptions.items()}

# Title (h1) per status code (spec 5.14).
_TITLES = {
    400: "That request could not be understood",
    403: "That is not allowed from here",
    404: "There is no such page",
    405: "That page does not take this request",
    409: "That request conflicts with the current state",
    500: "Something broke inside backup-engine",
}
_LEADS = {
    400: "Go back, reload the page, and try again — nothing was changed.",
    500: "The details are in the container log. Nothing you typed was saved.",
}
_LEAD_DEFAULT = "Go back to the Board and try again."


def _wants_json() -> bool:
    """JSON endpoints answer {"error": <sentence>} instead of the HTML page
    (spec 5.14 / 9): anything under *.json, the run-record /log tail, and the
    two path-confined browsers."""
    p = request.path
    return (p.endswith(".json")
            or p.endswith("/log")
            or p.endswith("/jobs/browse")
            or p.endswith("/jobs/source-size"))


def _handle_error(e):
    code = e.code if isinstance(e, HTTPException) and e.code else 500
    desc = getattr(e, "description", None)
    # A description the caller set (abort(..., description=...)) rather than the
    # framework's boilerplate.
    custom = desc if (desc and desc != _DEFAULT_DESC.get(code)) else None
    is_csrf = custom == "csrf"

    if code == 500:
        current_app.logger.exception("Unhandled error rendering %s", request.path)

    if is_csrf:
        title = "That form had expired"
        lead = _LEADS[400]
    else:
        title = (custom if (code in (404, 409) and custom) else _TITLES.get(code, "Something went wrong"))
        lead = _LEADS.get(code, _LEAD_DEFAULT)

    if _wants_json():
        if is_csrf:
            msg = "That form had expired — reload and try again."
        elif code == 404:
            msg = custom or "There is no such page."
        else:
            msg = custom or _TITLES.get(code, "Something went wrong")
        return jsonify({"error": msg}), code

    return render_template("error.html", code=code, title=title, lead=lead), code


def register_error_handlers(app: Flask) -> None:
    for code in (400, 403, 404, 405, 409, 500):
        app.register_error_handler(code, _handle_error)


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        CONFIG_DIR=os.environ.get("CONFIG_DIR", "/config"),
        CACHE_DIR=os.environ.get("CACHE_DIR", "/cache"),
        SCRIPTS_DIR=os.environ.get("SCRIPTS_DIR", "/app/scripts"),
        TEMPLATE_PATH=os.environ.get("BACKUP_ENV_TEMPLATE", "/app/config/backup.env.example"),
        SOURCE_ROOT=os.environ.get("SOURCE_ROOT", "/backup/media"),
        # Restore destination (Task 6 / spec 7.5.1): restores are written into a new
        # dated folder under RESTORE_ROOT (container) / RESTORE_ROOT_HOST (what the
        # user sees). SOURCE_ROOT_HOST is the host prefix of the protected source, so
        # ops.validate_target can refuse a target that is (under) the live source.
        RESTORE_ROOT=os.environ.get("RESTORE_ROOT", "/restore"),
        RESTORE_ROOT_HOST=os.environ.get("RESTORE_ROOT_HOST", "/mnt/user/restore"),
        SOURCE_ROOT_HOST=os.environ.get("SOURCE_ROOT_HOST", "/mnt/user"),
        VERSION=os.environ.get("VERSION", "0.1.0-dev"),
        BUILD_DATE=os.environ.get("BUILD_DATE", "unknown"),   # About glossary stamp (5.13)
        # Live pricing is OPT-IN: production reads live rates, but tests override
        # this to False so the estimate routes never hit the network.
        PRICES_LIVE=os.environ.get("PRICES_LIVE", "true") != "false",
        SECRET_KEY=os.environ.get("GUI_SECRET_KEY") or os.urandom(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    # Templates use plain names through this global (spec 4.3/4.4, the mono law).
    app.jinja_env.globals["vocab"] = vocab

    @app.context_processor
    def _shell_globals():
        # The clock and footer name the container's zone (spec 5.15).
        return {"tz": os.environ.get("TZ", "UTC")}

    from .routes import bp
    from . import permissions_routes  # noqa: F401 — registers /setup/permissions on bp
    app.register_blueprint(bp)
    register_error_handlers(app)
    return app
