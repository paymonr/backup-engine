# tests/gui/test_permissions_engine.py — discover/apply/converge against an
# in-memory IAM (spec 2026-09-22 §2). FakeIAM answers exactly the aws calls the
# engine makes, mutates on writes, and records every argv for never-touch checks.
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.gui import config_io, permissions, provision

ACCOUNT = "123456789012"
USER = "backup-engine-runtime"
USER_ARN = f"arn:aws:iam::{ACCOUNT}:user/{USER}"
BUCKET = f"unraid-backup-{ACCOUNT}"
EXTRA_ARN = f"arn:aws:iam::{ACCOUNT}:policy/backup-engine-runtime-extra-buckets"
LEGACY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/backup-engine-runtime-object-only"
P = permissions.parse_principal(USER_ARN)
WANT = permissions.required_docs(P, BUCKET)
ADMIN = permissions.AdminCreds("AKIAADMINKEY", "ADMINSECRETVALUE")


def _opts(args):
    out, i = {}, 2
    while i < len(args):
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            out[args[i]] = args[i + 1]; i += 2
        else:
            out[args[i]] = True; i += 1
    return out


class FakeIAM:
    def __init__(self, *, inline=None, managed=None, attached=(), role=None, deny=(),
                 account=ACCOUNT):
        self.inline = dict(inline or {})
        self.managed = {}
        for arn, (name, docs) in (managed or {}).items():
            self.managed[arn] = {"name": name, "versions": [
                {"VersionId": f"v{i + 1}", "Document": d, "IsDefaultVersion": i == len(docs) - 1,
                 "CreateDate": f"2026-01-0{i + 1}T00:00:00Z"} for i, d in enumerate(docs)]}
        self.attached = set(attached)
        self.role = role
        self.deny = set(deny)
        self.account = account
        self.calls = []
        self._n = 100

    def _ok(self, obj=None, text=None):
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=text if text is not None else json.dumps(obj or {}))

    def _err(self, code, op, msg):
        return SimpleNamespace(returncode=254, stdout="",
                               stderr=f"\nAn error occurred ({code}) when calling the {op} operation: {msg}\n")

    def _default(self, arn):
        return next(v for v in self.managed[arn]["versions"] if v["IsDefaultVersion"])

    def __call__(self, args, *, region, key, secret, session_token=None):
        self.calls.append(list(args))
        op = "".join(w.capitalize() for w in args[1].split("-"))
        o = _opts(args)
        if op in self.deny:
            return self._err("AccessDenied", op,
                             f"User: arn:aws:iam::{ACCOUNT}:user/admin (key {key}, secret {secret}) "
                             f"is not authorized to perform: iam:{op}")
        if args[0] == "sts":
            if "--query" in o:
                return self._ok(text=self.account + "\n")
            return self._ok({"Account": ACCOUNT, "Arn": USER_ARN})
        if op == "GetAccountSummary":
            return self._ok({"SummaryMap": {}})
        if op == "ListUserPolicies":
            return self._ok({"PolicyNames": sorted(self.inline)})
        if op == "GetUserPolicy":
            if o["--policy-name"] not in self.inline:
                return self._err("NoSuchEntity", op, "no such policy")
            return self._ok({"PolicyDocument": self.inline[o["--policy-name"]]})
        if op == "PutUserPolicy":
            self.inline[o["--policy-name"]] = json.loads(o["--policy-document"])
            return self._ok()
        if op == "ListAttachedUserPolicies":
            return self._ok({"AttachedPolicies": [{"PolicyArn": a, "PolicyName": self.managed[a]["name"]}
                                                  for a in sorted(self.attached)]})
        if op == "GetPolicy":
            a = o["--policy-arn"]
            if a not in self.managed:
                return self._err("NoSuchEntity", op, f"Policy {a} was not found.")
            return self._ok({"Policy": {"Arn": a, "DefaultVersionId": self._default(a)["VersionId"],
                                        "AttachmentCount": int(a in self.attached)}})
        if op == "GetPolicyVersion":
            v = next(v for v in self.managed[o["--policy-arn"]]["versions"]
                     if v["VersionId"] == o["--version-id"])
            return self._ok({"PolicyVersion": {"Document": v["Document"], "VersionId": v["VersionId"]}})
        if op == "CreatePolicy":
            a = f"arn:aws:iam::{ACCOUNT}:policy/{o['--policy-name']}"
            if a in self.managed:
                return self._err("EntityAlreadyExists", op, "exists")
            self.managed[a] = {"name": o["--policy-name"], "versions": [
                {"VersionId": "v1", "Document": json.loads(o["--policy-document"]),
                 "IsDefaultVersion": True, "CreateDate": "2026-09-22T00:00:00Z"}]}
            return self._ok({"Policy": {"Arn": a}})
        if op == "AttachUserPolicy":
            if o["--policy-arn"] not in self.managed:
                return self._err("NoSuchEntity", op, "no such policy")
            self.attached.add(o["--policy-arn"])
            return self._ok()
        if op == "ListPolicyVersions":
            return self._ok({"Versions": [{k: v[k] for k in ("VersionId", "IsDefaultVersion", "CreateDate")}
                                          for v in self.managed[o["--policy-arn"]]["versions"]]})
        if op == "DeletePolicyVersion":
            vs = self.managed[o["--policy-arn"]]["versions"]
            vs[:] = [v for v in vs if v["VersionId"] != o["--version-id"]]
            return self._ok()
        if op == "CreatePolicyVersion":
            vs = self.managed[o["--policy-arn"]]["versions"]
            if len(vs) >= 5:
                return self._err("LimitExceeded", op, "A managed policy can have up to 5 versions.")
            for v in vs:
                v["IsDefaultVersion"] = False
            self._n += 1
            vs.append({"VersionId": f"v{self._n}", "Document": json.loads(o["--policy-document"]),
                       "IsDefaultVersion": True, "CreateDate": "2026-09-22T00:00:00Z"})
            return self._ok()
        if op == "GetRole":
            if self.role is None:
                return self._err("NoSuchEntity", op, "The role cannot be found.")
            return self._ok({"Role": {"RoleName": o["--role-name"],
                                      "AssumeRolePolicyDocument": self.role["trust"]}})
        if op == "CreateRole":
            if self.role is not None:
                return self._err("EntityAlreadyExists", op, "exists")
            self.role = {"trust": json.loads(o["--assume-role-policy-document"]), "policies": {}}
            return self._ok()
        if op == "UpdateAssumeRolePolicy":
            self.role["trust"] = json.loads(o["--policy-document"])
            return self._ok()
        if op == "GetRolePolicy":
            if self.role is None or o["--policy-name"] not in self.role["policies"]:
                return self._err("NoSuchEntity", op, "no such role policy")
            return self._ok({"PolicyDocument": self.role["policies"][o["--policy-name"]]})
        if op == "PutRolePolicy":
            self.role["policies"][o["--policy-name"]] = json.loads(o["--policy-document"])
            return self._ok()
        raise AssertionError(f"unexpected aws call: {args}")


def _level2_box(**kw):
    return FakeIAM(inline={permissions.RUNTIME_POLICY_NAME: json.loads(provision.render_policy(BUCKET))}, **kw)


def _current_box(**kw):
    return FakeIAM(inline={permissions.RUNTIME_POLICY_NAME: WANT["runtime"]},
                   role={"trust": WANT["trust"], "policies": {permissions.ROLE_POLICY_NAME: WANT["role"]}},
                   **kw)


def _converge(fake, dirs, template_path, **kw):
    return permissions.converge(P, bucket=BUCKET, region="us-east-1", admin=ADMIN,
                                config_dir=dirs["config"], template_path=template_path,
                                cache_dir=dirs["cache"], run=fake, **kw)


def _system_records(dirs):
    p = Path(dirs["cache"], "state", "_system.runs.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


_WRITE_OPS = {"create-role", "update-assume-role-policy", "put-role-policy", "put-user-policy",
              "create-policy-version", "delete-policy-version"}
_NEVER = {"create-access-key", "delete-access-key", "update-access-key", "delete-user",
          "delete-role", "delete-policy", "delete-user-policy", "delete-role-policy",
          "detach-user-policy", "detach-role-policy",
          # Prefix-only dedicated buckets (Addendum 2026-09-22): converge must never
          # create or attach a NEW managed policy -- only the legacy-managed-policy
          # repair path (create-policy-version on an EXISTING attached ARN) writes to
          # a managed policy at all, and even that is asserted separately above.
          "create-policy", "attach-user-policy"}


def _assert_never_touch(fake):
    for c in fake.calls:
        assert c[0] in ("iam", "sts"), f"non-IAM call: {c}"
        assert c[1] not in _NEVER, f"forbidden call: {c}"
        if c[1] == "create-policy-version":
            assert EXTRA_ARN not in c, "the extra-buckets policy contents must never be rewritten"


# --- runtime principal ---------------------------------------------------------

def test_runtime_principal_from_the_runtime_key():
    assert permissions.runtime_principal("us-east-1", "AKIARUN", "runsek", run=FakeIAM()) == P


def test_runtime_principal_failure_is_scrubbed():
    def boom(args, *, region, key, secret, session_token=None):
        return SimpleNamespace(returncode=255, stdout="", stderr=f"InvalidClientTokenId {key} {secret}")
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.runtime_principal("us-east-1", "AKIARUN", "runsek", run=boom)
    assert e.value.kind == "runtime_key"
    assert "runsek" not in e.value.detail and "AKIARUN" not in e.value.detail


# --- converge -------------------------------------------------------------------

def test_update_brings_the_level2_box_up_and_stamps(dirs, template_path):
    fake = _level2_box()
    out = _converge(fake, dirs, template_path)
    assert out.ok and out.applied and out.remaining == []
    assert [(s.id, s.status) for s in out.steps] == [("R1", "done"), ("R2", "done"), ("R3", "done")]
    assert fake.inline[permissions.RUNTIME_POLICY_NAME] == WANT["runtime"]
    assert fake.role["policies"][permissions.ROLE_POLICY_NAME] == WANT["role"]
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())
    assert env["PERMISSIONS_CHECKED_AT"].endswith("Z")
    assert env["BUCKET_ADMIN_ROLE_ARN"] == permissions.role_arn(ACCOUNT)
    # write_stamp no longer sets this key at all (Addendum 2026-09-22) -- a fresh
    # install never gets it, and an existing value would be left exactly as-is.
    assert env.get("RUNTIME_EXTRA_BUCKETS_POLICY_ARN", "") == ""
    kinds = [(r["kind"], r["event"]) for r in _system_records(dirs)]
    assert ("permissions", "start") in kinds and ("permissions", "end") in kinds
    _assert_never_touch(fake)


def test_second_run_plans_nothing_and_still_stamps(dirs, template_path):
    fake = _level2_box()
    _converge(fake, dirs, template_path)
    n = len(fake.calls)
    out = _converge(fake, dirs, template_path)
    assert out.ok and not out.applied and out.steps == []
    assert not any(c[1] in _WRITE_OPS for c in fake.calls[n:])


def test_already_current_install_is_just_stamped(dirs, template_path):
    fake = _current_box()
    out = _converge(fake, dirs, template_path, apply_changes=False, mode="check")
    assert out.ok and out.steps == []
    assert config_io.read_backup_env(dirs["config"])["PERMISSIONS_VERSION"] == str(permissions.required_level())
    assert not any(c[1] in _WRITE_OPS for c in fake.calls)


def test_preview_changes_nothing_and_does_not_stamp(dirs, template_path):
    fake = _level2_box()
    out = _converge(fake, dirs, template_path, apply_changes=False, mode="check")
    assert out.ok and not out.applied and len(out.steps) == 3
    assert all(s.status == "pending" for s in out.steps)
    assert not any(c[1] in _WRITE_OPS for c in fake.calls)
    assert "PERMISSIONS_VERSION" not in config_io.read_backup_env(dirs["config"])


def test_denied_step_stops_the_run_and_is_scrubbed(dirs, template_path):
    fake = _level2_box(deny={"PutRolePolicy"})
    out = _converge(fake, dirs, template_path)
    assert not out.ok and out.applied
    assert [(s.id, s.status) for s in out.steps] == [("R1", "done"), ("R2", "failed"), ("R3", "not-run")]
    err = out.steps[1].error
    assert "PutRolePolicy" in err
    assert ADMIN.key not in err and ADMIN.secret not in err
    assert "PERMISSIONS_VERSION" not in config_io.read_backup_env(dirs["config"])


def test_rerun_after_a_failure_finishes_the_job(dirs, template_path):
    fake = _level2_box(deny={"PutRolePolicy"})
    _converge(fake, dirs, template_path)
    fake.deny.clear()
    out = _converge(fake, dirs, template_path)
    assert out.ok and [s.id for s in out.steps] == ["R2", "R3"]


def test_account_mismatch_refuses_before_reading_iam(dirs, template_path):
    fake = _level2_box(account="999999999999")
    with pytest.raises(permissions.PermissionsError) as e:
        _converge(fake, dirs, template_path)
    assert e.value.kind == "account_mismatch"
    assert not any(c[0] == "iam" and c[1] != "get-account-summary" for c in fake.calls)


def test_missing_user_is_a_clear_error(dirs, template_path):
    class Gone(FakeIAM):
        def __call__(self, args, **kw):
            if args[:2] == ["iam", "list-user-policies"]:
                self.calls.append(list(args))
                return self._err("NoSuchEntity", "ListUserPolicies", "The user cannot be found.")
            return super().__call__(args, **kw)
    with pytest.raises(permissions.PermissionsError) as e:
        _converge(Gone(), dirs, template_path)
    assert e.value.kind == "user_missing"


def test_admin_without_iam_reach_raises_the_setup_preflight_error(dirs, template_path):
    fake = _level2_box(deny={"GetAccountSummary"})
    with pytest.raises(provision.AdminCapabilityError):
        _converge(fake, dirs, template_path)


def test_legacy_managed_policy_at_the_version_limit_makes_room(dirs, template_path):
    old = json.loads(provision.render_policy(BUCKET))
    fake = FakeIAM(managed={LEGACY_ARN: (permissions.RUNTIME_POLICY_NAME, [old] * 5)},
                   attached={LEGACY_ARN},
                   role={"trust": WANT["trust"], "policies": {permissions.ROLE_POLICY_NAME: WANT["role"]}})
    out = _converge(fake, dirs, template_path)
    assert out.ok and [(s.id, s.action) for s in out.steps] == [("R3", "new-version")]
    deleted = [c for c in fake.calls if c[1] == "delete-policy-version"]
    assert deleted and deleted[0][deleted[0].index("--version-id") + 1] == "v1"
    assert fake._default(LEGACY_ARN)["Document"] == WANT["runtime"]
    _assert_never_touch(fake)


# --- prefix-only dedicated buckets: narrowing + obsolete leftovers ---------------

OLD_UNSCOPED_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Sid": "CreateAndConfig", "Effect": "Allow",
         "Action": ["s3:CreateBucket", "s3:PutBucketVersioning", "s3:PutBucketPublicAccessBlock",
                    "s3:PutBucketOwnershipControls", "s3:PutEncryptionConfiguration",
                    "s3:PutLifecycleConfiguration", "s3:PutBucketTagging",
                    "s3:GetBucketLocation", "s3:GetBucketVersioning"],
         "Resource": "*"},
        {"Sid": "Teardown", "Effect": "Allow",
         "Action": ["s3:ListAllMyBuckets", "s3:GetBucketTagging", "s3:ListBucketVersions",
                    "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:DeleteBucket"],
         "Resource": "*"},
        {"Sid": "PolicyGrant", "Effect": "Allow",
         "Action": ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
                    "iam:CreatePolicyVersion", "iam:DeletePolicyVersion"],
         "Resource": EXTRA_ARN},
    ],
}


def test_old_unscoped_role_policy_is_narrowed_by_converge(dirs, template_path):
    # An install from before the addendum: the role policy is unscoped ("*")
    # and still has the IAM-write PolicyGrant statement. Converge must reset it
    # to the new prefix-scoped, IAM-free doc.
    fake = FakeIAM(inline={permissions.RUNTIME_POLICY_NAME: WANT["runtime"]},
                   role={"trust": WANT["trust"],
                         "policies": {permissions.ROLE_POLICY_NAME: OLD_UNSCOPED_ROLE_POLICY}})
    out = _converge(fake, dirs, template_path)
    assert out.ok and out.applied
    assert [(s.id, s.action) for s in out.steps] == [("R2", "put-role-policy")]
    assert out.steps[0].summary.startswith("Update")
    assert fake.role["policies"][permissions.ROLE_POLICY_NAME] == WANT["role"]
    _assert_never_touch(fake)


def test_obsolete_extra_buckets_policy_is_left_untouched_and_still_converges_to_empty(dirs, template_path):
    # An install that still has the old extra-buckets managed policy attached
    # (now inert -- nothing can version it any more). Converge must not touch
    # it at all, and a fully-current install still plans nothing.
    fake = _current_box(
        managed={EXTRA_ARN: ("backup-engine-runtime-extra-buckets",
                             [{"Version": "2012-10-17", "Statement": [
                                 {"Sid": "NoExtraBucketsYet", "Effect": "Deny", "Action": "s3:*",
                                  "Resource": "arn:aws:s3:::__no-extra-buckets-configured__"}]}])},
        attached={EXTRA_ARN})
    out = _converge(fake, dirs, template_path)
    assert out.ok and not out.applied and out.steps == []
    assert not any(EXTRA_ARN in c for c in fake.calls)
    _assert_never_touch(fake)


def test_create_that_races_an_existing_entity_counts_as_done():
    steps = [permissions.Step("R3", "create-role", "x", ["iam", "create-role", "--role-name", "r",
                                                         "--assume-role-policy-document", "{}"])]

    def exists(args, *, region, key, secret, session_token=None):
        return SimpleNamespace(returncode=254, stdout="",
                               stderr="An error occurred (EntityAlreadyExists) when calling the CreateRole operation")
    permissions.apply(steps, region="us-east-1", admin=ADMIN, run=exists)
    assert steps[0].status == "done"


# --- IAM settle retry (review Minor 4): the post-apply re-check tolerates ONE ----
# stale read (IAM eventual consistency) before giving up.

class _StaleRoleRead(FakeIAM):
    """The FIRST get-role-policy call after a put-role-policy still returns the doc
    from just before that write -- simulating one stale post-write IAM read."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self._stale_doc = None
        self._armed = False

    def __call__(self, args, *, region, key, secret, session_token=None):
        if args[:2] == ["iam", "put-role-policy"] and not self._armed:
            name = _opts(args)["--policy-name"]
            self._stale_doc = (self.role or {}).get("policies", {}).get(name)
            self._armed = True
        if args[:2] == ["iam", "get-role-policy"] and self._armed:
            self._armed = False
            self.calls.append(list(args))
            if self._stale_doc is None:
                return self._err("NoSuchEntity", "GetRolePolicy", "no such role policy")
            return self._ok({"PolicyDocument": self._stale_doc})
        return super().__call__(args, region=region, key=key, secret=secret, session_token=session_token)


def test_settle_retries_once_on_a_stale_post_apply_read_then_stamps(dirs, template_path):
    # An install that only needs R2 narrowed (like the old-unscoped-policy case
    # above), but whose FIRST post-apply read of the role policy is stale.
    fake = _StaleRoleRead(inline={permissions.RUNTIME_POLICY_NAME: WANT["runtime"]},
                          role={"trust": WANT["trust"],
                                "policies": {permissions.ROLE_POLICY_NAME: OLD_UNSCOPED_ROLE_POLICY}})
    slept = []
    out = _converge(fake, dirs, template_path, sleep=slept.append)
    assert out.ok and out.applied and out.remaining == []
    assert slept == [3.0]
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())


def test_settle_does_not_sleep_when_the_first_recheck_is_already_empty(dirs, template_path):
    # Every OTHER converge test in this file never sleeps -- the fakes are
    # immediately consistent, so the settle loop's single re-check already comes
    # back empty and breaks before ever calling sleep.
    fake = _level2_box()
    slept = []
    out = _converge(fake, dirs, template_path, sleep=slept.append)
    assert out.ok and slept == []


# --- unparseable AWS JSON (review Minor 8): a clean error, not a crash ----------

def test_discover_raises_a_clean_error_on_unparseable_json():
    def run(args, *, region, key, secret, session_token=None):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.discover(P, region="us-east-1", admin=ADMIN, run=run)
    assert e.value.kind == "read"
    assert "unreadable" in e.value.detail


def test_make_room_raises_a_clean_error_on_unparseable_json():
    def call(argv):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")
    with pytest.raises(permissions.PermissionsError) as e:
        permissions._make_room(call, ADMIN, f"arn:aws:iam::{ACCOUNT}:policy/{permissions.RUNTIME_POLICY_NAME}")
    assert e.value.kind == "apply"
    assert "unreadable" in e.value.detail


def test_discover_raises_a_clean_error_on_non_dict_json():
    # Syntactically VALID JSON that isn't an object (e.g. a bare list) must not
    # sail through and blow up as an AttributeError the first time a caller does
    # `.get(...)` on it -- it's just as "unreadable" as malformed JSON.
    def run(args, *, region, key, secret, session_token=None):
        return SimpleNamespace(returncode=0, stdout="[1, 2, 3]", stderr="")
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.discover(P, region="us-east-1", admin=ADMIN, run=run)
    assert e.value.kind == "read"
    assert "unreadable" in e.value.detail


def test_make_room_raises_a_clean_error_on_non_dict_json():
    def call(argv):
        return SimpleNamespace(returncode=0, stdout="[1, 2, 3]", stderr="")
    with pytest.raises(permissions.PermissionsError) as e:
        permissions._make_room(call, ADMIN, f"arn:aws:iam::{ACCOUNT}:policy/{permissions.RUNTIME_POLICY_NAME}")
    assert e.value.kind == "apply"
    assert "unreadable" in e.value.detail
