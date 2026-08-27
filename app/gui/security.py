# app/gui/security.py — CSRF token via Flask session + stdlib secrets (no extra dependency).
from __future__ import annotations
import secrets
from flask import session

def issue_csrf() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

def verify_csrf(token: str) -> bool:
    return bool(token) and secrets.compare_digest(token, session.get("_csrf", ""))
