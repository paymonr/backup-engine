# app/gui/permissions.py — "Check & update AWS permissions" (spec
# docs/superpowers/specs/2026-09-22-permissions-converge-design.md, and the
# addendum at its end -- prefix-only dedicated buckets). Three layers:
#   levels   — provisioning/permissions.json (the required level + history) and the
#              PERMISSIONS_VERSION stamp in backup.env. Pure reads, safe at render.
#   engine   — the required IAM set (R1-R3), a pure planner, and discover/apply over
#              the aws CLI with TRANSIENT admin creds (never stored, always scrubbed).
#   fallback — a re-runnable bash script + runtime-key-only Verify probes.
from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from . import config_io, provision
from ..engine import buckets, runs

MANIFEST = provision.PROVISIONING_DIR / "permissions.json"
STAMP_KEY = "PERMISSIONS_VERSION"
CHECKED_KEY = "PERMISSIONS_CHECKED_AT"


# --- levels -------------------------------------------------------------------

def load_manifest(path: str | Path = MANIFEST) -> dict:
    return json.loads(Path(path).read_text())


def required_level(path: str | Path = MANIFEST) -> int:
    return int(load_manifest(path)["level"])


def history(path: str | Path = MANIFEST) -> list[dict]:
    return list(load_manifest(path)["history"])


def templates_sha256(prov_dir: str | Path = provision.PROVISIONING_DIR) -> str:
    """Fingerprint of every provisioning/*.tmpl (sorted by name). Pinned in the
    manifest, so editing a template without bumping the level fails a test."""
    h = hashlib.sha256()
    for p in sorted(Path(prov_dir).glob("*.tmpl"), key=lambda p: p.name):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def current_level(config_dir: str) -> int | None:
    raw = config_io.read_backup_env(config_dir).get(STAMP_KEY, "").strip()
    # isdigit() alone accepts non-decimal "digit" characters (e.g. superscript "²")
    # and isdecimal() alone still accepts non-ASCII decimal digits (e.g. Arabic-Indic
    # "١") that int() happily parses -- require BOTH so only plain ASCII 0-9 counts.
    return int(raw) if raw.isdecimal() and raw.isascii() else None


def checked_at(config_dir: str) -> str | None:
    return config_io.read_backup_env(config_dir).get(CHECKED_KEY, "").strip() or None


def feature_level(feature: str, path: str | Path = MANIFEST) -> int | None:
    for h in history(path):
        if feature in h.get("features", []):
            return int(h["level"])
    return None


def feature_available(config_dir: str, feature: str) -> bool:
    """History-driven gating: a feature is on once the stamp reaches the level that
    introduced it, so a later bump for something else never re-disables it."""
    have, need = current_level(config_dir), feature_level(feature)
    return have is not None and need is not None and have >= need


def level_status(config_dir: str) -> dict:
    """The stamp vs this build. Reads backup.env only -- safe on any GET."""
    have, need = current_level(config_dir), required_level()
    if have is None:
        state, missing = "unchecked", []
    elif have >= need:
        state, missing = "current", []
    else:
        state, missing = "behind", [h for h in history() if int(h["level"]) > have]
    return {"state": state, "level": have, "required": need, "missing": missing,
            "checked_at": checked_at(config_dir)}


def needs_you_row(config_dir: str) -> dict | None:
    """The Board's needs-you warning while the stamp is behind or missing."""
    if not config_io.is_provisioned(config_dir):
        return None
    st = level_status(config_dir)
    if st["state"] == "current":
        return None
    if st["state"] == "behind":
        text = ("This version of backup-engine needs an AWS permissions update for: "
                + "; ".join(h["adds"] for h in st["missing"]) + ".")
    else:
        text = ("They haven't been checked for this version of backup-engine. Features that "
                "need newer permissions, like dedicated per-job buckets, stay off until they are.")
    return {"level": "warning", "code": "permissions-update", "job": None,
            "strong": "AWS permissions need an update.", "text": text,
            "fix": {"label": "Update permissions", "href": "/setup/permissions"}}


# --- the required IAM set (spec §1 as narrowed by the addendum: R1-R3, fixed names) --

PREFIX = "backup-engine"
RUNTIME_POLICY_NAME = f"{PREFIX}-runtime-object-only"
ROLE_NAME = f"{PREFIX}-bucket-admin"
ROLE_POLICY_NAME = f"{PREFIX}-bucket-admin-create-config"

# opentofu/main.tf resource -> required-set id. The drift-guard test pins main.tf's
# IAM resources to exactly these plus TOFU_UNMANAGED.
TOFU_RESOURCES = {
    "aws_iam_role.bucket_admin": "R1",
    "aws_iam_role_policy.bucket_admin": "R2",
    "aws_iam_user_policy.runtime": "R3",
}
# Created once by setup and deliberately never converged (spec non-goals).
TOFU_UNMANAGED = {"aws_iam_user.runtime", "aws_iam_access_key.runtime"}


class PermissionsError(Exception):
    """A failure BEFORE any change. `kind`: runtime_key | principal | account_mismatch
    | user_missing | read | apply | script. `detail` is already secret-scrubbed;
    `action` names the denied IAM action when AWS said which."""
    def __init__(self, kind: str, detail: str = "", *, action: str | None = None):
        super().__init__(f"permissions: {kind}")
        self.kind, self.detail, self.action = kind, detail, action


@dataclass(frozen=True)
class Principal:
    account: str
    user: str
    arn: str


_USER_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):user/(?:[\w+=,.@-]+/)*([\w+=,.@-]{1,64})$",
                          re.ASCII)


def parse_principal(arn: str) -> Principal:
    """The runtime key's identity must be an IAM user (not a role, session or root)."""
    arn = (arn or "").strip()
    # fullmatch (not match): with `.match()`, a trailing "$" is lenient about a
    # trailing "\n" (it matches just before it) -- fullmatch requires the WHOLE
    # string to be consumed, closing that hole. re.ASCII keeps \d/\w from also
    # accepting Unicode look-alike digits (e.g. a full-width "２").
    m = _USER_ARN_RE.fullmatch(arn)
    if not m:
        raise PermissionsError("principal", f"{arn or '(empty)'} is not an IAM user")
    return Principal(account=m.group(1), user=m.group(2), arn=arn)


def role_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:role/{ROLE_NAME}"


def runtime_managed_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:policy/{RUNTIME_POLICY_NAME}"


def trust_doc(user_arn: str) -> dict:
    return {"Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"AWS": user_arn},
                           "Action": "sts:AssumeRole"}]}


_BASE_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", re.ASCII)


def _check_base_bucket(bucket: str) -> None:
    """The base bucket name goes into IAM Resource ARNs; anything but a plain S3 name
    (e.g. "*", "?", "${...}") could widen the <base>-* confinement."""
    if not _BASE_BUCKET_RE.fullmatch(bucket or "") or ".." in bucket:
        raise PermissionsError("bucket", f"{bucket!r} is not a plain S3 bucket name")


def required_docs(principal: Principal, bucket: str) -> dict:
    _check_base_bucket(bucket)
    return {
        "trust": trust_doc(principal.arn),
        "role": json.loads(provision.render_bucket_admin_policy(bucket)),
        "runtime": json.loads(provision.render_policy(bucket, role_arn(principal.account))),
    }


# --- comparing policies ----------------------------------------------------------------

def _doc(v) -> dict | None:
    """A policy document as the aws CLI returns it: a dict, or a URL-encoded string."""
    if v is None:
        return None
    return v if isinstance(v, dict) else json.loads(unquote(v))


def _as_sorted_list(v) -> list:
    return sorted(v) if isinstance(v, list) else [v]


def normalize(doc: dict | None) -> dict:
    """Statements keyed by Sid, with Action/Resource/Principal lists sorted, so only a
    real difference compares unequal."""
    stmts = (doc or {}).get("Statement", []) or []
    if isinstance(stmts, dict):
        stmts = [stmts]
    out = {}
    for s in stmts:
        s = json.loads(json.dumps(s))
        for k in ("Action", "NotAction", "Resource", "NotResource"):
            if k in s:
                s[k] = _as_sorted_list(s[k])
        if isinstance(s.get("Principal"), dict):
            s["Principal"] = {k: _as_sorted_list(v) for k, v in s["Principal"].items()}
        # A live trust policy can also hold the string form ("Principal": "*")
        # instead of the {"AWS": [...]} dict form -- leave it as-is; comparisons
        # stay stable since both sides normalize the same way.
        out[s.get("Sid") or json.dumps(s, sort_keys=True)] = s
    return out


def same_policy(a: dict | None, b: dict | None) -> bool:
    return normalize(a) == normalize(b)


def _describe(s: dict) -> str:
    sid = s.get("Sid") or "(unnamed)"
    acts = ", ".join(s.get("Action") or s.get("NotAction") or [])
    principal = s.get("Principal")
    principal_targets = [principal] if isinstance(principal, str) else \
        [v for vs in (principal or {}).values() for v in vs]
    targets = s.get("Resource") or principal_targets
    return f"{sid}: {acts} on {', '.join(targets)}" if targets else f"{sid}: {acts}"


def diff_statements(live: dict | None, want: dict) -> list[str]:
    a, b = normalize(live), normalize(want)
    lines = [f"+ {_describe(b[k])}" for k in sorted(b.keys() - a.keys())]
    lines += [f"~ {_describe(b[k])}" for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
    lines += [f"- {_describe(a[k])}" for k in sorted(a.keys() - b.keys())]
    return lines


# --- the planner (pure) --------------------------------------------------------------------

@dataclass
class Live:
    """What discover() read. None/False = absent."""
    runtime_inline: dict | None = None
    runtime_managed_arn: str | None = None     # legacy guided shape (managed + attached)
    runtime_managed_doc: dict | None = None
    role_exists: bool = False
    role_trust: dict | None = None
    role_policy: dict | None = None


@dataclass
class Step:
    id: str                      # R1..R3
    action: str                  # create-role | update-trust |
                                 # put-role-policy | put-user-policy | new-version
    summary: str
    argv: list[str]              # aws argv without the leading "aws"
    diff: list[str] = field(default_factory=list)
    status: str = "pending"      # pending | done | failed | not-run
    error: str = ""

    def command(self) -> str:
        """The command for display, with policy-JSON arguments abbreviated."""
        shown = ["'<policy JSON>'" if a.startswith("{") else shlex.quote(a) for a in self.argv]
        return " ".join(["aws", *shown])


def _j(doc: dict) -> str:
    return json.dumps(doc, separators=(",", ":"))


def plan(live: Live, principal: Principal, bucket: str) -> list[Step]:
    """The steps (in dependency order R1..R3) that bring `live` to the required set."""
    want = required_docs(principal, bucket)
    user = principal.user
    steps: list[Step] = []
    if not live.role_exists:
        steps.append(Step("R1", "create-role",
                          f"Create the role {ROLE_NAME} (only {user} may assume it)",
                          ["iam", "create-role", "--role-name", ROLE_NAME,
                           "--assume-role-policy-document", _j(want["trust"])],
                          diff=diff_statements(None, want["trust"])))
    elif not same_policy(live.role_trust, want["trust"]):
        steps.append(Step("R1", "update-trust", f"Reset who may assume {ROLE_NAME} to {user} only",
                          ["iam", "update-assume-role-policy", "--role-name", ROLE_NAME,
                           "--policy-document", _j(want["trust"])],
                          diff=diff_statements(live.role_trust, want["trust"])))
    if live.role_policy is None or not same_policy(live.role_policy, want["role"]):
        verb = "Add" if live.role_policy is None else "Update"
        steps.append(Step("R2", "put-role-policy", f"{verb} the role's policy {ROLE_POLICY_NAME}",
                          ["iam", "put-role-policy", "--role-name", ROLE_NAME,
                           "--policy-name", ROLE_POLICY_NAME, "--policy-document", _j(want["role"])],
                          diff=diff_statements(live.role_policy, want["role"])))
    if live.runtime_inline is None and live.runtime_managed_arn:
        if not same_policy(live.runtime_managed_doc, want["runtime"]):
            steps.append(Step("R3", "new-version",
                              f"Publish a new version of {user}'s policy {RUNTIME_POLICY_NAME}",
                              ["iam", "create-policy-version", "--policy-arn", live.runtime_managed_arn,
                               "--policy-document", _j(want["runtime"]), "--set-as-default"],
                              diff=diff_statements(live.runtime_managed_doc, want["runtime"])))
    elif live.runtime_inline is None or not same_policy(live.runtime_inline, want["runtime"]):
        verb = "Add" if live.runtime_inline is None else "Update"
        steps.append(Step("R3", "put-user-policy", f"{verb} {user}'s policy {RUNTIME_POLICY_NAME}",
                          ["iam", "put-user-policy", "--user-name", user,
                           "--policy-name", RUNTIME_POLICY_NAME, "--policy-document", _j(want["runtime"])],
                          diff=diff_statements(live.runtime_inline, want["runtime"])))
    return steps


# --- discover / apply / converge (I/O; admin creds are transient) --------------------------

@dataclass(frozen=True)
class AdminCreds:
    key: str
    secret: str
    token: str | None = None


def _scrub(admin: AdminCreds, text: str) -> str:
    return provision._scrub(text or "", admin.key, admin.secret, admin.token or "").strip()


def _denied_action(stderr: str) -> str | None:
    m = re.search(r"when calling the (\w+) operation", stderr or "")
    return f"iam:{m.group(1)}" if m else None


def _caller(admin: AdminCreds, region: str, run):
    def call(argv):
        return run(argv, region=region, key=admin.key, secret=admin.secret, session_token=admin.token)
    return call


def runtime_principal(region: str, key: str, secret: str, *, run=provision._run_aws) -> Principal:
    """Who the stored runtime key is (any key may call sts get-caller-identity)."""
    cp = run(["sts", "get-caller-identity", "--output", "json"], region=region, key=key, secret=secret)
    if cp.returncode != 0:
        raise PermissionsError("runtime_key", provision._scrub(cp.stderr or "", key, secret).strip())
    try:
        arn = json.loads(cp.stdout)["Arn"]
    except (ValueError, KeyError):
        raise PermissionsError("runtime_key", "unreadable sts get-caller-identity response")
    return parse_principal(arn)


def discover(principal: Principal, *, region: str, admin: AdminCreds, run=provision._run_aws) -> Live:
    call = _caller(admin, region, run)

    def get(argv, *, missing_ok=True):
        cp = call([*argv, "--output", "json"])
        if cp.returncode == 0:
            try:
                data = json.loads(cp.stdout or "{}")
                # Syntactically valid JSON that isn't an object (e.g. a bare list)
                # would otherwise sail through here and blow up as an AttributeError
                # the first time a caller does `.get(...)` on it -- treat it the
                # same as unparseable JSON.
                if not isinstance(data, dict):
                    raise ValueError("AWS response was not a JSON object")
            except ValueError as e:
                raise PermissionsError("read", "unreadable AWS response") from e
            return data
        if missing_ok and "NoSuchEntity" in (cp.stderr or ""):
            return None
        raise PermissionsError("read", _scrub(admin, cp.stderr), action=_denied_action(cp.stderr))

    user = principal.user
    inline = get(["iam", "list-user-policies", "--user-name", user])
    if inline is None:
        raise PermissionsError("user_missing", f"IAM user {user} no longer exists")
    live = Live()
    if RUNTIME_POLICY_NAME in inline.get("PolicyNames", []):
        live.runtime_inline = _doc(get(["iam", "get-user-policy", "--user-name", user,
                                        "--policy-name", RUNTIME_POLICY_NAME],
                                       missing_ok=False)["PolicyDocument"])
    attached = {p["PolicyArn"]: p["PolicyName"] for p in
                (get(["iam", "list-attached-user-policies", "--user-name", user]) or {})
                .get("AttachedPolicies", [])}
    for arn, name in attached.items():
        if name == RUNTIME_POLICY_NAME:
            pol = get(["iam", "get-policy", "--policy-arn", arn], missing_ok=False)["Policy"]
            ver = get(["iam", "get-policy-version", "--policy-arn", arn,
                       "--version-id", pol["DefaultVersionId"]], missing_ok=False)
            live.runtime_managed_arn = arn
            live.runtime_managed_doc = _doc(ver["PolicyVersion"]["Document"])
    role = get(["iam", "get-role", "--role-name", ROLE_NAME])
    if role is not None:
        live.role_exists = True
        live.role_trust = _doc(role["Role"].get("AssumeRolePolicyDocument"))
        rp = get(["iam", "get-role-policy", "--role-name", ROLE_NAME, "--policy-name", ROLE_POLICY_NAME])
        live.role_policy = _doc(rp["PolicyDocument"]) if rp else None
    return live


def _make_room(call, admin: AdminCreds, policy_arn: str) -> None:
    cp = call(["iam", "list-policy-versions", "--policy-arn", policy_arn, "--output", "json"])
    if cp.returncode != 0:
        raise PermissionsError("apply", _scrub(admin, cp.stderr), action=_denied_action(cp.stderr))
    try:
        data = json.loads(cp.stdout or "{}")
        # Syntactically valid JSON that isn't an object (e.g. a bare list) would
        # otherwise blow up as an AttributeError on the `.get(...)` below --
        # treat it the same as unparseable JSON.
        if not isinstance(data, dict):
            raise ValueError("AWS response was not a JSON object")
    except ValueError as e:
        raise PermissionsError("apply", "unreadable AWS response") from e
    victim = buckets.oldest_non_default_version(data.get("Versions", []))
    if victim:
        cp = call(["iam", "delete-policy-version", "--policy-arn", policy_arn, "--version-id", victim])
        if cp.returncode != 0:
            raise PermissionsError("apply", _scrub(admin, cp.stderr), action=_denied_action(cp.stderr))


def apply(steps: list[Step], *, region: str, admin: AdminCreds, run=provision._run_aws) -> list[Step]:
    """Run the steps in order; stop at the first failure (the rest become not-run).
    Every step is idempotent, so a re-run after fixing the cause is safe."""
    call = _caller(admin, region, run)
    halted = False
    for s in steps:
        if halted:
            s.status = "not-run"
            continue
        try:
            if s.action == "new-version":
                _make_room(call, admin, s.argv[s.argv.index("--policy-arn") + 1])
            cp = call(s.argv)
        except PermissionsError as e:
            s.status, s.error, halted = "failed", e.detail, True
            continue
        raced = s.action == "create-role" and "EntityAlreadyExists" in (cp.stderr or "")
        if cp.returncode == 0 or raced:
            s.status = "done"
        else:
            s.status, s.error, halted = "failed", _scrub(admin, cp.stderr), True
    return steps


def _now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_stamp(config_dir: str, template_path: str, principal: Principal, *, now=None) -> None:
    """Record that this install matches this build: the role ARN + level + time."""
    env = config_io.read_backup_env(config_dir)
    env.update({"BUCKET_ADMIN_ROLE_ARN": role_arn(principal.account),
                STAMP_KEY: str(required_level()), CHECKED_KEY: _now_iso(now)})
    config_io.write_backup_env(template_path, config_dir, env)


def clear_stamp(config_dir: str, template_path: str) -> None:
    """Erase the stamp + recorded role ARN: a new destination (a different bucket,
    or a new runtime key) is not the install the stamp was written for, and a
    final converge that failed must not leave a level claimed that was never
    actually reached. Blanks BUCKET_ADMIN_ROLE_ARN, PERMISSIONS_VERSION and
    PERMISSIONS_CHECKED_AT; every other key is left exactly as-is."""
    env = config_io.read_backup_env(config_dir)
    env.update({"BUCKET_ADMIN_ROLE_ARN": "", STAMP_KEY: "", CHECKED_KEY: ""})
    config_io.write_backup_env(template_path, config_dir, env)


def record(cache_dir: str, *, mode: str, lines: list[str]) -> str:
    """Append a `permissions` run record so Activity shows it (the same start+end
    shape provision.record_setup writes)."""
    run_id = runs.new_run_id()
    ts = _now_iso()
    log_rel = f"logs/runs/{runs.SYSTEM_JOB}/{run_id}.log"
    log_path = Path(cache_dir, log_rel)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"{ts} AWS permissions ({mode}): level {required_level()} · OK"]
    body += [f"{ts} {line}" for line in lines]
    log_path.write_text("\n".join(body) + "\n")
    runs.append_event(cache_dir, None, {
        "v": 1, "id": run_id, "job": None, "kind": "permissions", "event": "start",
        "trigger": "manual", "started_at": ts, "log": log_rel})
    runs.append_event(cache_dir, None, {
        "v": 1, "id": run_id, "job": None, "kind": "permissions", "event": "end",
        "outcome": "ok", "finished_at": ts, "duration_s": 0, "exit_code": 0, "error": None})
    return run_id


@dataclass
class Outcome:
    ok: bool
    applied: bool
    steps: list[Step]
    remaining: list[Step] = field(default_factory=list)


def converge(principal: Principal, *, bucket: str, region: str, admin: AdminCreds,
             config_dir: str, template_path: str, cache_dir: str,
             apply_changes: bool = True, mode: str = "update",
             run=provision._run_aws, sleep=time.sleep,
             settle_tries: int = 3, settle_s: float = 3.0) -> Outcome:
    """Discover -> plan -> (apply -> re-discover -> re-plan must be empty) -> stamp.
    Raises provision.AdminCapabilityError / provision.AccountLookupError /
    PermissionsError before any change; step failures come back in the Outcome.

    The post-apply re-check is retried up to `settle_tries` times (sleeping
    `settle_s` between tries) because IAM reads can briefly still show the
    pre-write document even after a successful write (eventual consistency) --
    without this, a real convergence could be misreported as "remaining" work."""
    provision.verify_admin_can_provision(region, admin.key, admin.secret, admin.token, run=run)
    account = provision.aws_account_id(region, admin.key, admin.secret, admin.token, run=run)
    if account != principal.account:
        raise PermissionsError(
            "account_mismatch",
            f"The admin credentials are for AWS account {account}, but the backup key "
            f"belongs to account {principal.account}.")
    steps = plan(discover(principal, region=region, admin=admin, run=run), principal, bucket)
    if not steps:
        write_stamp(config_dir, template_path, principal)
        record(cache_dir, mode=mode, lines=["Everything was already in place."])
        return Outcome(ok=True, applied=False, steps=[])
    if not apply_changes:
        return Outcome(ok=True, applied=False, steps=steps)
    apply(steps, region=region, admin=admin, run=run)
    if any(s.status == "failed" for s in steps):
        return Outcome(ok=False, applied=True, steps=steps)
    remaining: list[Step] = []
    for attempt in range(settle_tries):
        remaining = plan(discover(principal, region=region, admin=admin, run=run), principal, bucket)
        if not remaining or attempt == settle_tries - 1:
            break
        sleep(settle_s)
    if remaining:
        return Outcome(ok=False, applied=True, steps=steps, remaining=remaining)
    write_stamp(config_dir, template_path, principal)
    record(cache_dir, mode=mode, lines=[s.summary for s in steps])
    return Outcome(ok=True, applied=True, steps=steps)


# --- the commands fallback: a convergent script + runtime-key Verify ---------------------

# re.ASCII: without it \d/\w also match Unicode look-alikes (e.g. a full-width
# "２"), which could sneak a 12-"digit"-looking string past the account check.
# fullmatch (not match+"$") below: match()+"$" tolerates a trailing "\n" -- it
# matches just before it -- fullmatch requires the whole string to be consumed.
_ACCOUNT_RE = re.compile(r"^\d{12}$", re.ASCII)
_IAM_USER_RE = re.compile(r"^[\w+=,.@-]{1,64}$", re.ASCII)
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$", re.ASCII)


def script(principal: Principal, *, bucket: str, region: str) -> str:
    """A bash script that converges IAM with only idempotent commands (for CloudShell).
    Every interpolated value is shape-checked first, and the policy JSON goes in
    quoted heredocs, so nothing in it can reach the shell."""
    if not (_ACCOUNT_RE.fullmatch(principal.account) and _IAM_USER_RE.fullmatch(principal.user)
            and buckets.valid_bucket_name(bucket) and _REGION_RE.fullmatch(region or "")):
        raise PermissionsError("script", "the account, user, bucket or region has an unexpected shape")
    docs = required_docs(principal, bucket)
    user = principal.user
    obsolete_extra_arn = f"arn:aws:iam::{principal.account}:policy/{PREFIX}-runtime-extra-buckets"

    def heredoc(name: str, doc: dict) -> list[str]:
        return [f"cat > \"$d/{name}.json\" <<'EOF'", json.dumps(doc, indent=2), "EOF"]

    lines = [
        "#!/usr/bin/env bash",
        f"# backup-engine — AWS permissions update to level {required_level()}.",
        "# Safe to run more than once: it creates what's missing and resets the rest to",
        "# exactly what this version of backup-engine needs. It never touches your",
        "# bucket, your data or the backup user's access key.",
        "# Run it in AWS CloudShell (or any shell signed in as an admin), then click",
        "# Verify in backup-engine.",
        "set -euo pipefail",
        f"export AWS_DEFAULT_REGION={region}",
        'd="$(mktemp -d)"',
        *heredoc("trust", docs["trust"]),
        *heredoc("bucket-admin", docs["role"]),
        *heredoc("runtime", docs["runtime"]),
        "# 1. The bucket-admin role, which only the backup user may assume",
        f"aws iam get-role --role-name {ROLE_NAME} >/dev/null 2>&1 || "
        f"aws iam create-role --role-name {ROLE_NAME} "
        f"--assume-role-policy-document \"file://$d/trust.json\" >/dev/null",
        f"aws iam update-assume-role-policy --role-name {ROLE_NAME} "
        f"--policy-document \"file://$d/trust.json\"",
        "# 2. The bucket-admin role's policy",
        f"aws iam put-role-policy --role-name {ROLE_NAME} --policy-name {ROLE_POLICY_NAME} "
        f"--policy-document \"file://$d/bucket-admin.json\"",
        "# 3. The backup user's own policy",
        f"aws iam put-user-policy --user-name {user} --policy-name {RUNTIME_POLICY_NAME} "
        f"--policy-document \"file://$d/runtime.json\"",
        "# Older guided installs only: if the managed policy below is attached to the backup",
        "# user, it is now redundant and can be detached (optional):",
        f"#   aws iam detach-user-policy --user-name {user} "
        f"--policy-arn {runtime_managed_arn(principal.account)}",
        "# Older installs only: backup-engine-runtime-extra-buckets is no longer used; you may detach it (optional):",
        f"#   aws iam detach-user-policy --user-name {user} --policy-arn {obsolete_extra_arn}",
        'rm -rf "$d"',
        'echo "Done. Go back to backup-engine and click Verify."',
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Probe:
    name: str
    ok: bool
    hint: str = ""
    detail: str = ""


def _probe_once(principal: Principal, *, bucket, region, key, secret, run) -> list[Probe]:
    role_probe = f"Can assume the role {ROLE_NAME}"
    policy_probe = f"The role {ROLE_NAME} has its policy"
    scoped_probe = "The role can't reach the shared bucket"
    out = []
    cp = run(["s3api", "list-object-versions", "--bucket", bucket, "--max-items", "1",
              "--output", "json"], region=region, key=key, secret=secret)
    ok = cp.returncode == 0
    out.append(Probe("Can list old versions in the backup bucket", ok,
                     "" if ok else "Set by step 3 of the script — did it run?",
                     "" if ok else provision._scrub(cp.stderr or "", key, secret).strip()))
    try:
        creds = provision.assume_role(role_arn(principal.account), region=region, key=key,
                                      secret=secret, run=run)
    except provision.AssumeRoleError as e:
        out.append(Probe(role_probe, False, "Steps 1 and 3 of the script set this up — did step 1 run?",
                         provision._scrub(str(e), key, secret)))
        out.append(Probe(policy_probe, False, "Needs the role first (step 1)."))
        out.append(Probe(scoped_probe, False, "Needs the role first (step 1)."))
        return out
    out.append(Probe(role_probe, True))
    rk, rs, rt = creds["AWS_ACCESS_KEY_ID"], creds["AWS_SECRET_ACCESS_KEY"], creds["AWS_SESSION_TOKEN"]
    cp = run(["s3api", "list-buckets", "--output", "json"], region=region, key=rk, secret=rs, session_token=rt)
    ok2 = cp.returncode == 0
    out.append(Probe(policy_probe, ok2,
                     "" if ok2 else "Set by step 2 of the script — did it run?",
                     "" if ok2 else provision._scrub(cp.stderr or "", rk, rs, rt).strip()))
    # Negative probe: an OLD unscoped role (S3 on "*") would pass the three probes
    # above too, so a leaked runtime key could still reach every bucket via the
    # role. The narrowed role grants GetBucketVersioning on <base>-* only, so
    # this call against the BASE bucket must come back AccessDenied.
    cp = run(["s3api", "get-bucket-versioning", "--bucket", bucket, "--output", "json"],
             region=region, key=rk, secret=rs, session_token=rt)
    denied = cp.returncode != 0 and "AccessDenied" in (cp.stderr or "")
    if denied:
        out.append(Probe(scoped_probe, True))
    elif cp.returncode == 0:
        out.append(Probe(scoped_probe, False,
                         "The role still has an older, wider policy — did step 2 of the script run?"))
    else:
        out.append(Probe(scoped_probe, False,
                         "Couldn't confirm the role is limited to this app's buckets — try Verify again.",
                         provision._scrub(cp.stderr or "", rk, rs, rt).strip()))
    return out


def verify(principal: Principal, *, bucket: str, region: str, key: str, secret: str,
           run=provision._run_aws, sleep=time.sleep, tries: int = 3,
           wait_s: float = 5.0) -> list[Probe]:
    """Functional checks with ONLY the runtime key, retried briefly because IAM
    changes take a few seconds to reach every AWS endpoint."""
    probes: list[Probe] = []
    for attempt in range(tries):
        probes = _probe_once(principal, bucket=bucket, region=region, key=key, secret=secret, run=run)
        if all(p.ok for p in probes) or attempt == tries - 1:
            break
        sleep(wait_s)
    return probes
