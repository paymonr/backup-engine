# app/gui/__init__.py
from __future__ import annotations
import os
from flask import Flask

def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        CONFIG_DIR=os.environ.get("CONFIG_DIR", "/config"),
        CACHE_DIR=os.environ.get("CACHE_DIR", "/cache"),
        SCRIPTS_DIR=os.environ.get("SCRIPTS_DIR", "/app/scripts"),
        TEMPLATE_PATH=os.environ.get("BACKUP_ENV_TEMPLATE", "/app/config/backup.env.example"),
        SECRET_KEY=os.environ.get("GUI_SECRET_KEY") or os.urandom(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    from .routes import bp
    app.register_blueprint(bp)
    return app
