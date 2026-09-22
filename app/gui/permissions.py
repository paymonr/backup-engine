# app/gui/permissions.py — "Check & update AWS permissions" (spec
# docs/superpowers/specs/2026-09-22-permissions-converge-design.md). Three layers:
#   levels   — provisioning/permissions.json (the required level + history) and the
#              PERMISSIONS_VERSION stamp in backup.env. Pure reads, safe at render.
#   engine   — the required IAM set (R1-R5), a pure planner, and discover/apply over
#              the aws CLI with TRANSIENT admin creds (never stored, always scrubbed).
#   fallback — a re-runnable bash script + runtime-key-only Verify probes.
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from . import config_io, provision

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
    return int(raw) if raw.isdigit() else None


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


# --- the required IAM set (spec §1: R1-R5, fixed names) ------------------------------

PREFIX = "backup-engine"
RUNTIME_POLICY_NAME = f"{PREFIX}-runtime-object-only"
ROLE_NAME = f"{PREFIX}-bucket-admin"
ROLE_POLICY_NAME = f"{PREFIX}-bucket-admin-create-config"
EXTRA_POLICY_NAME = f"{PREFIX}-runtime-extra-buckets"
EXTRA_POLICY_TEMPLATE = provision.PROVISIONING_DIR / "extra-buckets-policy.json.tmpl"

# opentofu/main.tf resource -> required-set id. The drift-guard test pins main.tf's
# IAM resources to exactly these plus TOFU_UNMANAGED.
TOFU_RESOURCES = {
    "aws_iam_policy.runtime_extra_buckets": "R1",
    "aws_iam_user_policy_attachment.runtime_extra_buckets": "R2",
    "aws_iam_role.bucket_admin": "R3",
    "aws_iam_role_policy.bucket_admin": "R4",
    "aws_iam_user_policy.runtime": "R5",
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


_USER_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):user/(?:[\w+=,.@-]+/)*([\w+=,.@-]{1,64})$")


def parse_principal(arn: str) -> Principal:
    """The runtime key's identity must be an IAM user (not a role, session or root)."""
    arn = (arn or "").strip()
    m = _USER_ARN_RE.match(arn)
    if not m:
        raise PermissionsError("principal", f"{arn or '(empty)'} is not an IAM user")
    return Principal(account=m.group(1), user=m.group(2), arn=arn)


def role_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:role/{ROLE_NAME}"


def extra_policy_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:policy/{EXTRA_POLICY_NAME}"


def runtime_managed_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:policy/{RUNTIME_POLICY_NAME}"


def trust_doc(user_arn: str) -> dict:
    return {"Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"AWS": user_arn},
                           "Action": "sts:AssumeRole"}]}


def required_docs(principal: Principal, bucket: str) -> dict:
    return {
        "extra": json.loads(EXTRA_POLICY_TEMPLATE.read_text()),
        "trust": trust_doc(principal.arn),
        "role": json.loads(provision.render_bucket_admin_policy(
            bucket, extra_policy_arn(principal.account))),
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
        out[s.get("Sid") or json.dumps(s, sort_keys=True)] = s
    return out


def same_policy(a: dict | None, b: dict | None) -> bool:
    return normalize(a) == normalize(b)


def _describe(s: dict) -> str:
    sid = s.get("Sid") or "(unnamed)"
    acts = ", ".join(s.get("Action") or s.get("NotAction") or [])
    targets = s.get("Resource") or [v for vs in (s.get("Principal") or {}).values() for v in vs]
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
    extra_exists: bool = False
    extra_attached: bool = False
    role_exists: bool = False
    role_trust: dict | None = None
    role_policy: dict | None = None


@dataclass
class Step:
    id: str                      # R1..R5
    action: str                  # create-policy | attach | create-role | update-trust |
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
    """The steps (in dependency order R1..R5) that bring `live` to the required set."""
    want = required_docs(principal, bucket)
    user, extra = principal.user, extra_policy_arn(principal.account)
    steps: list[Step] = []
    if not live.extra_exists:
        steps.append(Step("R1", "create-policy", f"Create the extra-buckets policy {EXTRA_POLICY_NAME}",
                          ["iam", "create-policy", "--policy-name", EXTRA_POLICY_NAME,
                           "--policy-document", _j(want["extra"])]))
    if not live.extra_attached:
        steps.append(Step("R2", "attach", f"Attach {EXTRA_POLICY_NAME} to {user}",
                          ["iam", "attach-user-policy", "--user-name", user, "--policy-arn", extra]))
    if not live.role_exists:
        steps.append(Step("R3", "create-role",
                          f"Create the role {ROLE_NAME} (only {user} may assume it)",
                          ["iam", "create-role", "--role-name", ROLE_NAME,
                           "--assume-role-policy-document", _j(want["trust"])],
                          diff=diff_statements(None, want["trust"])))
    elif not same_policy(live.role_trust, want["trust"]):
        steps.append(Step("R3", "update-trust", f"Reset who may assume {ROLE_NAME} to {user} only",
                          ["iam", "update-assume-role-policy", "--role-name", ROLE_NAME,
                           "--policy-document", _j(want["trust"])],
                          diff=diff_statements(live.role_trust, want["trust"])))
    if live.role_policy is None or not same_policy(live.role_policy, want["role"]):
        verb = "Add" if live.role_policy is None else "Update"
        steps.append(Step("R4", "put-role-policy", f"{verb} the role's policy {ROLE_POLICY_NAME}",
                          ["iam", "put-role-policy", "--role-name", ROLE_NAME,
                           "--policy-name", ROLE_POLICY_NAME, "--policy-document", _j(want["role"])],
                          diff=diff_statements(live.role_policy, want["role"])))
    if live.runtime_inline is None and live.runtime_managed_arn:
        if not same_policy(live.runtime_managed_doc, want["runtime"]):
            steps.append(Step("R5", "new-version",
                              f"Publish a new version of {user}'s policy {RUNTIME_POLICY_NAME}",
                              ["iam", "create-policy-version", "--policy-arn", live.runtime_managed_arn,
                               "--policy-document", _j(want["runtime"]), "--set-as-default"],
                              diff=diff_statements(live.runtime_managed_doc, want["runtime"])))
    elif live.runtime_inline is None or not same_policy(live.runtime_inline, want["runtime"]):
        verb = "Add" if live.runtime_inline is None else "Update"
        steps.append(Step("R5", "put-user-policy", f"{verb} {user}'s policy {RUNTIME_POLICY_NAME}",
                          ["iam", "put-user-policy", "--user-name", user,
                           "--policy-name", RUNTIME_POLICY_NAME, "--policy-document", _j(want["runtime"])],
                          diff=diff_statements(live.runtime_inline, want["runtime"])))
    return steps
