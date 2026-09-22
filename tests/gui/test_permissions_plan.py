# tests/gui/test_permissions_plan.py — the required IAM set + the pure planner
# (spec 2026-09-22 §1-§2). No AWS: Live is built by hand.
import json
import re
from urllib.parse import quote

import pytest
from app.gui import permissions, provision

ACCOUNT = "123456789012"
USER_ARN = f"arn:aws:iam::{ACCOUNT}:user/backup-engine-runtime"
BUCKET = f"unraid-backup-{ACCOUNT}"
P = permissions.parse_principal(USER_ARN)
WANT = permissions.required_docs(P, BUCKET)
LEGACY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/backup-engine-runtime-object-only"


def _current():
    return permissions.Live(runtime_inline=WANT["runtime"],
                            role_exists=True, role_trust=WANT["trust"], role_policy=WANT["role"])


def _level2_runtime():
    # The owner's live policy today: level 2, no multi-bucket statements.
    return json.loads(provision.render_policy(BUCKET))


# --- principal ---------------------------------------------------------------

def test_parse_principal_reads_account_and_user():
    assert (P.account, P.user, P.arn) == (ACCOUNT, "backup-engine-runtime", USER_ARN)


def test_parse_principal_handles_an_iam_path():
    p = permissions.parse_principal(f"arn:aws:iam::{ACCOUNT}:user/ops/backups/be-user")
    assert p.user == "be-user"


@pytest.mark.parametrize("arn", [
    f"arn:aws:sts::{ACCOUNT}:assumed-role/admin/session",
    f"arn:aws:iam::{ACCOUNT}:root",
    f"arn:aws:iam::{ACCOUNT}:role/backup-engine-bucket-admin",
    "", "not-an-arn",
])
def test_parse_principal_refuses_non_users(arn):
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.parse_principal(arn)
    assert e.value.kind == "principal"


# --- the required set --------------------------------------------------------

def test_required_runtime_policy_includes_assume_role_on_the_real_role():
    stmts = {s["Sid"]: s for s in WANT["runtime"]["Statement"]}
    assert stmts["AssumeBucketAdminRole"]["Resource"] == permissions.role_arn(ACCOUNT)
    assert stmts["ObjectRWWildcard"]["Resource"] == f"arn:aws:s3:::{BUCKET}-*/*"


def test_required_trust_lets_only_the_runtime_user_assume():
    assert WANT["trust"]["Statement"] == [{"Effect": "Allow", "Principal": {"AWS": USER_ARN},
                                           "Action": "sts:AssumeRole"}]


def test_required_role_policy_has_no_policy_grant_and_is_prefix_scoped():
    # Addendum 2026-09-22 (prefix-only dedicated buckets): the role policy no
    # longer grants any IAM action, and its S3 statements are scoped to <bucket>-*.
    stmts = {s["Sid"]: s for s in WANT["role"]["Statement"]}
    assert "PolicyGrant" not in stmts
    assert stmts["CreateAndConfig"]["Resource"] == f"arn:aws:s3:::{BUCKET}-*"


def test_tofu_iam_resources_match_the_converge_table():
    # Drift guard: every IAM resource main.tf creates is either converged (R1-R3) or
    # deliberately unmanaged (the user + its access key).
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    found = {f"{t}.{n}" for t, n in re.findall(r'^resource "(aws_iam_[a-z_]+)" "([a-z_]+)"', main_tf, re.M)}
    assert found == set(permissions.TOFU_RESOURCES) | permissions.TOFU_UNMANAGED
    assert sorted(permissions.TOFU_RESOURCES.values()) == ["R1", "R2", "R3"]


# --- normalization -------------------------------------------------------------

def test_formatting_only_differences_are_the_same_policy():
    shuffled = json.loads(json.dumps(WANT["runtime"]))
    shuffled["Statement"].reverse()
    for s in shuffled["Statement"]:
        if isinstance(s["Action"], list):
            s["Action"] = list(reversed(s["Action"]))
    assert permissions.same_policy(shuffled, WANT["runtime"])


def test_string_vs_single_item_list_is_the_same_policy():
    a = {"Statement": [{"Sid": "X", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "r"}]}
    b = {"Statement": [{"Sid": "X", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["r"]}]}
    assert permissions.same_policy(a, b)


def test_url_encoded_documents_are_decoded():
    assert permissions._doc(quote(json.dumps(WANT["trust"]))) == WANT["trust"]
    assert permissions._doc(None) is None


def test_string_principal_is_handled_by_diff_and_same_policy():
    # A live AWS trust policy can hold a string form ("Principal": "*"), not just
    # a dict -- normalize()/_describe() must not assume dict and blow up with
    # AttributeError once discover() feeds live docs in.
    doc = {"Statement": [{"Sid": "X", "Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}]}
    assert permissions.diff_statements(None, doc) == ["+ X: sts:AssumeRole on *"]
    assert permissions.same_policy(doc, json.loads(json.dumps(doc)))


# --- the planner ----------------------------------------------------------------

def test_current_install_plans_nothing():
    assert permissions.plan(_current(), P, BUCKET) == []


def test_owners_level2_box_plans_all_three_in_order():
    live = permissions.Live(runtime_inline=_level2_runtime())
    steps = permissions.plan(live, P, BUCKET)
    assert [s.id for s in steps] == ["R1", "R2", "R3"]
    assert [s.action for s in steps] == ["create-role", "put-role-policy", "put-user-policy"]
    r3 = steps[-1]
    assert any(line.startswith("+ AssumeBucketAdminRole: sts:AssumeRole on "
                               f"{permissions.role_arn(ACCOUNT)}") for line in r3.diff)
    assert not any(line.startswith("- ") for line in r3.diff)   # nothing removed


def test_level1_runtime_policy_shows_a_changed_statement():
    old = _level2_runtime()
    for s in old["Statement"]:
        if s["Sid"] == "ListBucketScoped":
            s["Action"] = ["s3:ListBucket", "s3:GetBucketLocation"]
    live = _current()
    live.runtime_inline = old
    steps = permissions.plan(live, P, BUCKET)
    assert [s.id for s in steps] == ["R3"]
    assert any(line.startswith("~ ListBucketScoped") for line in steps[0].diff)


def test_drifted_trust_is_reset():
    live = _current()
    live.role_trust = permissions.trust_doc(f"arn:aws:iam::{ACCOUNT}:user/someone-else")
    steps = permissions.plan(live, P, BUCKET)
    assert [(s.id, s.action) for s in steps] == [("R1", "update-trust")]


def test_legacy_managed_runtime_policy_gets_a_new_version():
    live = _current()
    live.runtime_inline = None
    live.runtime_managed_arn, live.runtime_managed_doc = LEGACY_ARN, _level2_runtime()
    steps = permissions.plan(live, P, BUCKET)
    assert [(s.id, s.action) for s in steps] == [("R3", "new-version")]
    assert steps[0].argv[:4] == ["iam", "create-policy-version", "--policy-arn", LEGACY_ARN]
    assert "--set-as-default" in steps[0].argv


def test_legacy_managed_policy_already_current_plans_nothing():
    live = _current()
    live.runtime_inline = None
    live.runtime_managed_arn, live.runtime_managed_doc = LEGACY_ARN, WANT["runtime"]
    assert permissions.plan(live, P, BUCKET) == []


def test_step_command_abbreviates_policy_json():
    steps = permissions.plan(permissions.Live(runtime_inline=_level2_runtime()), P, BUCKET)
    cmd = steps[-1].command()
    assert cmd.startswith("aws iam put-user-policy --user-name backup-engine-runtime")
    assert "'<policy JSON>'" in cmd and '"Statement"' not in cmd
