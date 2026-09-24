# app/gui/s3_rules_routes.py — S3 rules actions (spec 2026-09-23 §5). Phase A: Check now
# and acknowledging a tamper alarm. Task 12: Refresh now (a detached storage summary scan).
# Registered on the shared `gui` blueprint.
from __future__ import annotations

from urllib.parse import quote

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from ..engine import lifecycle
from . import config_io, jobs_io, ops, routes, runner, s3_rules, security
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


def _screen(*, key: str = "", error: str | None = None, pvw: dict | None = None, form=None):
    """Render the S3 rules screen with the editor/preview it currently needs. fix round 1, I2:
    a form error whose row no longer resolves (job deleted/renamed in another tab, bucket
    changed) can't build an editor to show that error in -- flash it and bounce home instead of
    silently dropping it."""
    cfg = current_app.config
    v = s3_rules.screen(cfg)
    ed = None
    if v["managed"] and not pvw:
        ed = s3_rules.editor(cfg, key, error=error, form=form)
        if error and ed is None:
            flash(error, "warning")
            return redirect("/setup/storage")
    return render_template("s3_rules.html", v=v, ed=ed, pvw=pvw, csrf=security.issue_csrf(),
                           refresh_ok=s3_rules.refresh_ok(cfg) if v["managed"] else False)


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
        return _screen(key=key, error=str(e), form=request.form)
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
            return _screen(key=key, error=str(e), form=request.form)
        # fix round 1, Minor: an honest flash -- don't claim "S3 applies it now" unless this
        # row's own target actually differs from what's applied. A resubmission identical to
        # what's already applied changes nothing (and apply_for is skipped); one that resaves a
        # value that's already waiting (own is empty, but the row still keeps less than the
        # baseline) still waits -- apply_for's own warning says so; this flash must not
        # contradict it.
        folder = key.partition("|")[2]
        # fix round 1, I2: the bucket-wide row covers TWO app rules -- housekeeping and,
        # since Task 16, versioning -- either or both can be what this edit actually changed
        # (a versioning-only edit must not be reported as "No change." and skip apply_for).
        if folder == "*":
            relevant = [c for c in pv.changes if c.rule_id in (lifecycle.HOUSEKEEPING_ID, "versioning")]
        else:
            relevant = [c for c in pv.changes if c.rule_id == lifecycle.rule_id(folder)]
        if not relevant:
            flash("No change.", "note")
            return redirect("/setup/storage")
        if any(c.kind == lifecycle.KEEPS_LESS for c in relevant):
            flash("Saved.", "note")
        else:
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
        if getattr(e, "rules_applied", False):
            # final fix wave M5: the rules half landed; only the versioning put failed
            flash(f"Confirmed — the S3 rules were applied, but versioning couldn't be changed "
                  f"({s3_rules.why(e.kind)}). Anything still waiting is listed here.", "warning")
        else:
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
    except Exception:                                        # noqa: BLE001 — never a 500 (O2)
        flash("Couldn't clear the S3 rules alarm — try again.", "warning")
        return redirect(_back())
    try:
        # parked P2: its own try -- the acknowledge above DID land; a follow-up read that fails
        # (e.g. a malformed jobs.json) must not be reported as "couldn't clear"
        still = s3_rules.open_alarm(cfg) is not None
    except Exception:                                        # noqa: BLE001
        still = False
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


@bp.post("/jobs/history/confirm")
def job_history_confirm():
    """R-B5: the wizard's preview confirmed -- save the job and apply its S3 rule
    (apply_confirmed), then what any job save does after (flashes, run_now). No crontab
    render here (fix round 1, Minor): apply_confirmed's own save (_save_edit_unlocked)
    already renders it whenever the job was actually saved -- before that, nothing changed
    to render for."""
    _csrf_or_400()
    cfg = current_app.config
    token, typed = request.form.get("token", ""), request.form.get("typed", "")
    t = lifecycle.load_preview(cfg["CACHE_DIR"], token) or {}
    name = ((t.get("edit") or {}).get("job") or {}).get("name") or request.form.get("name", "")
    if not jobs_io.valid_name(name):
        abort(400, description="That job name isn't valid.")
    existing = jobs_io.get(cfg["CONFIG_DIR"], name)
    back = f"/jobs/{name}/edit" if existing else "/jobs/new"
    try:
        res = lifecycle.apply_confirmed(cfg, token, typed)
    except lifecycle.PreviewError as e:
        # A wrong/empty typed name (fix round 1, Minor): re-preview with a FRESH token (the old
        # one is discarded) and show the error inline, the same way the S3 rules screen's own
        # /setup/storage/apply does -- not a bounce to the edit form, which would silently drop
        # what the owner was confirming.
        if e.kind == "typed" and t.get("bucket") and t.get("edit"):
            try:
                pv = lifecycle.preview(cfg, t["bucket"], t["edit"])
            except lifecycle.PreviewError:
                pv = None
            if pv is not None and pv.token:
                lifecycle.discard_preview(cfg["CACHE_DIR"], token)
                hidden = {"name": name}
                if request.form.get("run_now"):
                    hidden["run_now"] = "1"
                pvw = s3_rules.preview_view(cfg, pv, action="/jobs/history/confirm", hidden=hidden,
                                            cancel=f"/jobs/{name}" if existing else "/jobs/new", error=e.message)
                # The form shows what the owner was actually trying to save (the pending edit's
                # own job, exactly as posted -- string values, _saved_form_values coerces them
                # for display same as it does a real saved job), not the still-unchanged saved
                # job -- so re-typing the bucket name doesn't also look like their entry reverted.
                pending_job = (t.get("edit") or {}).get("job")
                fv = routes._saved_form_values(pending_job if isinstance(pending_job, dict) else existing) \
                    if (pending_job or existing) else routes._fresh_form_values()
                return routes._render_job_form(cfg, job=existing, fv=fv, s3_preview=pvw)
        # I1 (fix round 1): apply_confirmed can raise "stale" AFTER already saving the job --
        # the race-lost write (lifecycle._STALE_RACE, matched by identity, not by kind, since
        # both the before-save and after-save cases share kind "stale"). The "save the job
        # again" advice only makes sense for the UNSAVED kinds (stale before save/typed/
        # invalid); for the after-save race the job already IS the new value, so this goes to
        # the job page instead, where "a change is waiting for your confirmation" shows it.
        if e.message == lifecycle._STALE_RACE:
            flash(e.message, "warning")
            return redirect(url_for("gui.job_page", name=name))
        flash(f"{e.message} Save the job again to see a fresh preview.", "warning")
        return redirect(back)
    except lifecycle.LifecycleError as e:
        if getattr(e, "rules_applied", False):
            flash(f"Saved {name} — its S3 rules were applied, but versioning couldn't be changed "
                  f"({s3_rules.why(e.kind)}). Setup → S3 rules lists anything still waiting.", "warning")
        else:
            flash(f"Saved {name}, but S3 couldn't be updated ({s3_rules.why(e.kind)}) — the change waits for "
                  "your confirmation in Setup → S3 rules.", "warning")
        return redirect(url_for("gui.job_page", name=name))
    if res.lines:
        flash("S3 rules updated: " + "; ".join(res.lines[:3]) + (" …" if len(res.lines) > 3 else ""), "success")
    flash(f"Saved {name}.", "success")
    if request.form.get("run_now"):
        runner.trigger_job(cfg["SCRIPTS_DIR"], name)
    return redirect(url_for("gui.job_page", name=name))


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
