# app/gui/s3_rules_routes.py — S3 rules actions (spec 2026-09-23 §5). Phase A: Check now
# and acknowledging a tamper alarm. Task 12: Refresh now (a detached storage summary scan).
# Registered on the shared `gui` blueprint.
from __future__ import annotations

from urllib.parse import quote

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

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


def _back() -> str:
    """Where check/acknowledge return: the S3 rules screen when it posted them, else Setup."""
    return "/setup/storage" if request.form.get("back") == "storage" else url_for("gui.setup_page")


def _refresh_ok(cfg) -> bool:
    """Whether the editor/preview may offer Refresh now: only when S3 rules are managed here
    and this isn't a custom S3 endpoint -- /setup/storage/refresh (Task 12) refuses both with
    a warning, so the button is never offered when it would just bounce (Task 15 context)."""
    return (lifecycle.managed(cfg["CONFIG_DIR"])
           and not config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_ENDPOINT", "").strip())


def _screen(*, key: str = "", error: str | None = None, pvw: dict | None = None):
    cfg = current_app.config
    v = s3_rules.screen(cfg)
    ed = s3_rules.editor(cfg, key, error=error) if (v["managed"] and not pvw) else None
    return render_template("s3_rules.html", v=v, ed=ed, pvw=pvw, csrf=security.issue_csrf(),
                           refresh_ok=_refresh_ok(cfg) if v["managed"] else False)


@bp.get("/setup/storage")
def setup_storage():
    """S3 rules (spec §5): state files only -- no AWS call on GET. ?edit=<bucket>|<folder> opens
    the side editor (<bucket>|* = bucket-wide)."""
    return _screen(key=request.args.get("edit", ""))


@bp.get("/setup/storage/impact.json")
def setup_storage_impact():
    """The side editor's live impact line: files only (the stored summary), never AWS."""
    return jsonify(s3_rules.impact_line(current_app.config, request.args))


@bp.post("/setup/storage/preview")
def setup_storage_preview():
    """Preview change (spec §3): keeps more -> saved and applied now; keeps less -> the preview."""
    _csrf_or_400()
    if _unprovisioned():
        return redirect(url_for("gui.setup_page"))
    cfg = current_app.config
    key = request.form.get("key", "")
    try:
        bucket, edit = s3_rules.edit_from_form(cfg, request.form)
    except ValueError as e:
        return _screen(key=key, error=str(e))
    try:
        pv = lifecycle.preview(cfg, bucket, edit)
    except lifecycle.PreviewError as e:
        flash(e.message, "warning")
        return redirect(_storage_url("" if edit["kind"] == "confirm" else key))
    if pv.token is None:
        if edit["kind"] == "confirm":
            flash("Nothing is waiting for your confirmation.", "note")
            return redirect("/setup/storage")
        try:
            lifecycle.save_edit(cfg, edit)
        except ValueError as e:
            return _screen(key=key, error=str(e))
        flash("Saved — this keeps more, so S3 applies it now.", "success")
        for category, msg in s3_rules.apply_for(cfg, [bucket]):
            flash(msg, category)
        return redirect("/setup/storage")
    return _screen(pvw=s3_rules.preview_view(cfg, pv, hidden={"key": key}, cancel=_storage_url(key)))


@bp.post("/setup/storage/apply")
def setup_storage_apply():
    """Apply a previewed change (R-B4): token + typed bucket name; stale -> Preview again."""
    _csrf_or_400()
    if _unprovisioned():
        return redirect(url_for("gui.setup_page"))
    cfg = current_app.config
    token, typed, key = (request.form.get("token", ""), request.form.get("typed", ""),
                         request.form.get("key", ""))
    try:
        res = lifecycle.apply_confirmed(cfg, token, typed)
    except lifecycle.PreviewError as e:
        t = lifecycle.load_preview(cfg["CACHE_DIR"], token) if e.kind == "typed" else None
        if t is not None:
            try:
                pv = lifecycle.preview(cfg, t["bucket"], t["edit"])
            except lifecycle.PreviewError:
                pv = None
            if pv is not None and pv.token:
                lifecycle.discard_preview(cfg["CACHE_DIR"], token)
                return _screen(pvw=s3_rules.preview_view(cfg, pv, hidden={"key": key},
                                                         cancel=_storage_url(key), error=e.message))
        flash(e.message, "warning")
        return redirect(_storage_url(key))
    except lifecycle.LifecycleError as e:
        flash(f"Saved, but S3 couldn't be updated ({s3_rules.why(e.kind)}) — the change still waits "
              "for your confirmation here.", "warning")
        return redirect("/setup/storage")
    shown = "; ".join(res.lines[:3]) + (" …" if len(res.lines) > 3 else "")
    flash(f"Confirmed — S3 rules updated{': ' + shown if shown else '.'}", "success")
    return redirect("/setup/storage")


@bp.post("/setup/s3-rules/check")
def s3_rules_check():
    _csrf_or_400()
    if _unprovisioned():
        return redirect(_back())
    for category, msg in s3_rules.check_all(current_app.config):
        flash(msg, category)
    return redirect(_back())


@bp.post("/setup/s3-rules/acknowledge")
def s3_rules_acknowledge():
    _csrf_or_400()
    if _unprovisioned():
        return redirect(_back())
    cfg = current_app.config
    seen = (request.form.get("seen") or "").strip() or None
    try:
        lifecycle.acknowledge(cfg["CACHE_DIR"], seen=seen)
        still = s3_rules.open_alarm(cfg) is not None
    except Exception:                                        # noqa: BLE001 — never a 500 (O2)
        flash("Couldn't clear the S3 rules alarm — try again.", "warning")
        return redirect(_back())
    if still:
        flash("Noted. A newer S3 rules alarm arrived after this page loaded — it's still shown.", "warning")
    else:
        flash("Noted — the S3 rules alarm is cleared.", "success")
    return redirect(_back())


def _storage_url(key: str = "") -> str:
    """/setup/storage, reopening the side editor on `key` when given."""
    return "/setup/storage" + (f"?edit={quote(key, safe='')}" if key else "")


def _app_folders(cfg) -> set[tuple[str, str]]:
    base = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET", "").strip()
    jobs = jobs_io.load(cfg["CONFIG_DIR"])
    return {(b, f.folder) for b in lifecycle.buckets_for(base, jobs) for f in lifecycle.folders_for(b, base, jobs)}


@bp.post("/setup/storage/refresh")
def setup_storage_refresh():
    """Refresh now (spec §4): a detached storage summary of one app folder (progress in Activity).
    Never a silent no-op (fix round 1, Minor 2): when there's nothing sysop's own op could
    actually scan -- S3 rules not managed here, or a custom S3 endpoint -- says so instead
    of flashing a success-shaped note over a launch that wouldn't have done anything."""
    _csrf_or_400()
    if _unprovisioned():
        return redirect(url_for("gui.setup_page"))
    cfg = current_app.config
    bucket, folder = request.form.get("bucket", "").strip(), request.form.get("folder", "")
    if (bucket, folder) not in _app_folders(cfg):
        abort(404, description="That folder isn't one of backup-engine's.")
    if config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_ENDPOINT", "").strip():
        flash("This storage doesn't support storage summaries.", "warning")
    elif not lifecycle.managed(cfg["CONFIG_DIR"]):
        flash("Storage summaries need the AWS permissions update.", "warning")
    else:
        ops.launch_py(cfg, "app.engine.sysop", ["storage-summary", "--bucket", bucket, "--folder", folder],
                      kind="storage-summary")
        flash("Refreshing the storage summary — watch it in Activity →", "note")
    return redirect(_storage_url(request.form.get("key", "")))
