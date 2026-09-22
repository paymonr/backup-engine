# app/gui/permissions_routes.py — /setup/permissions (spec 2026-09-22 §3): the
# permissions status, the admin-creds Update/Preview path, and (Task 8) the
# commands fallback. Registered on the shared `gui` blueprint; app/gui/__init__.py
# imports this module before the blueprint is registered. No AWS call on GET.
from __future__ import annotations

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from markupsafe import Markup

from . import config_io, permissions, provision, security
from .routes import _fmt_verified, _runtime_creds, bp


def _page(status_code: int = 200, **ctx):
    cfg = current_app.config
    perm = permissions.level_status(cfg["CONFIG_DIR"])
    base = dict(csrf=security.issue_csrf(), perm=perm, checked_human=_fmt_verified(perm["checked_at"]),
                provisioned=config_io.is_provisioned(cfg["CONFIG_DIR"]),
                mode=request.args.get("mode", "admin"), outcome=None, preview=False,
                error=None, error_detail=None, script=None, probes=None)
    base.update(ctx)
    return render_template("permissions.html", **base), status_code


def _bucket_region(cfg) -> tuple[str, str]:
    env = config_io.read_backup_env(cfg["CONFIG_DIR"])
    return env.get("S3_BUCKET", "").strip(), (env.get("AWS_REGION") or "us-east-1").strip()


def _principal_for(cfg, region: str) -> permissions.Principal:
    key, secret = _runtime_creds(cfg)
    return permissions.runtime_principal(region, key, secret)


## Fixed, developer-authored copy only — never interpolated with admin/AWS
## output — so it is safe to mark non-escaping: Jinja's autoescape would
## otherwise turn the apostrophes here into `&#39;`, breaking the plain-text
## reading of this sentence for no security benefit (nothing here is
## attacker-controlled).
_ADMIN_MESSAGES = {
    "token": Markup("These credentials can't manage AWS IAM. If they're temporary (the access key "
              "starts with ASIA — from sts get-session-token, SSO or CloudShell), use an "
              "MFA-authenticated session, an SSO role, or a permanent access key, and check the "
              "session token hasn't expired. Nothing was changed."),
    "permission": Markup("These credentials reached AWS but aren't allowed to manage IAM roles and "
                   "policies. Use an admin credential. Nothing was changed."),
}
_ADMIN_MESSAGE_DEFAULT = Markup(
    "Couldn't verify these credentials can manage IAM. Nothing was changed.")


def _perm_error_message(e: permissions.PermissionsError) -> str:
    if e.kind == "runtime_key":
        return ("The saved backup key couldn't identify itself to AWS — check it on Keys & "
                "secrets. Nothing was changed.")
    if e.kind == "principal":
        return ("The saved backup key doesn't belong to an IAM user, so there's no user to give "
                "permissions to. Nothing was changed.")
    if e.kind == "account_mismatch":
        return f"{e.detail} Use admin credentials for the backup key's account. Nothing was changed."
    if e.kind == "user_missing":
        return ("The backup key's IAM user no longer exists in AWS. Set up the destination "
                "again. Nothing was changed.")
    if e.kind == "script":
        return "Couldn't generate the commands for this install's settings."
    what = e.action or "the current IAM setup"
    return f"These admin credentials couldn't read {what}. Nothing was changed."


@bp.get("/setup/permissions")
def permissions_page():
    return _page()


def _run_admin(apply_changes: bool):
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    cfg = current_app.config
    if not config_io.is_provisioned(cfg["CONFIG_DIR"]):
        return redirect(url_for("gui.provision_home"))
    bucket, region = _bucket_region(cfg)
    f = request.form
    admin = permissions.AdminCreds(f.get("ADMIN_ACCESS_KEY_ID", "").strip(),
                                   f.get("ADMIN_SECRET_ACCESS_KEY", "").strip(),
                                   f.get("ADMIN_SESSION_TOKEN", "").strip() or None)
    try:
        principal = _principal_for(cfg, region)
        outcome = permissions.converge(
            principal, bucket=bucket, region=region, admin=admin,
            config_dir=cfg["CONFIG_DIR"], template_path=cfg["TEMPLATE_PATH"],
            cache_dir=cfg["CACHE_DIR"], apply_changes=apply_changes,
            mode="update" if apply_changes else "check")
    except provision.AdminCapabilityError as e:
        return _page(400, error=_ADMIN_MESSAGES.get(e.kind, _ADMIN_MESSAGE_DEFAULT),
            error_detail=e.detail if e.kind not in _ADMIN_MESSAGES else None)
    except provision.AccountLookupError as e:
        return _page(400, error="Couldn't read your AWS account from those admin credentials — "
                                "check the key and try again. Nothing was changed.",
                     error_detail=e.detail)
    except permissions.PermissionsError as e:
        return _page(400, error=_perm_error_message(e),
                     error_detail=e.detail if e.kind not in ("account_mismatch", "script") else None)
    finally:
        admin = None   # discard transient admin creds from this frame regardless of outcome
    # converge() sends the admin creds to AWS (verify_admin_can_provision, the account
    # lookup, discover()) on EVERY Update request that gets this far, even when the
    # plan turns out empty -- so the delete-the-key reminder applies to every Update
    # outcome, not just one that actually applied a step. It never applies to Preview:
    # the owner is about to paste the same key again to apply for real.
    if apply_changes:
        flash("We never stored your admin key — delete that access key in AWS now.", "warning")
    if outcome.ok and not outcome.steps:
        flash("Everything's already in place — AWS permissions are up to date.", "success")
        return redirect(url_for("gui.permissions_page"))
    if outcome.ok and outcome.applied:
        flash("AWS permissions updated.", "success")
    return _page(outcome=outcome, preview=not apply_changes)


@bp.post("/setup/permissions/update")
def permissions_update():
    return _run_admin(apply_changes=True)


@bp.post("/setup/permissions/preview")
def permissions_preview():
    return _run_admin(apply_changes=False)
