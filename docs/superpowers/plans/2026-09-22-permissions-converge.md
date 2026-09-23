# Check & Update AWS Permissions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any already-provisioned install bring its AWS IAM up to what the current build needs — in place, without touching the bucket or the runtime key — and make every initial setup path end verified and stamped.

**Architecture:** A new `app/gui/permissions.py` holds three layers: *levels* (a `provisioning/permissions.json` manifest + a `PERMISSIONS_VERSION` stamp in `backup.env`), a *converge engine* (pure planner over a small required IAM set R1–R5; discover/apply via the `aws` CLI with transient admin creds), and a *fallback* (a re-runnable bash script + runtime-key-only Verify probes). A new `app/gui/permissions_routes.py` serves `/setup/permissions`; Setup, Board, Destination and the job wizard read the stamp (no AWS at render). Automated setup finishes by converging; `setup.sh` prints the ARNs + level; guided-manual finishes on the commands path.

**Tech Stack:** Python 3 / Flask / Jinja, `aws` CLI via `subprocess` (never boto3), OpenTofu (`opentofu/`), bats for shell, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-permissions-converge-design.md`

## Global Constraints

- AWS access is **`aws` CLI via subprocess only — never boto3** (project constraint). Reuse `provision._run_aws(args, *, region, key, secret, session_token=None)`.
- **Admin creds are transient:** passed only in the subprocess env, never written to disk, never rendered back into HTML, scrubbed from every error (`provision._scrub`). Routes drop them in `finally`.
- **Never touch:** the bucket (no `s3`/`s3api` calls in the converge path), access keys (no `*-access-key`), users/roles/policies deletion (only `delete-policy-version` on the legacy managed runtime policy), and **never rewrite the extra-buckets policy's contents** after creating it.
- Fixed IAM names, prefix `backup-engine`: `backup-engine-runtime-object-only`, `backup-engine-bucket-admin`, `backup-engine-bucket-admin-create-config`, `backup-engine-runtime-extra-buckets`.
- **No AWS calls on any GET.** Everything that talks to AWS is a CSRF-checked POST (`security.verify_csrf`).
- **UI copy:** no `vocab.FORBIDDEN_TERMS` in visible text on the new/changed pages (notably `OpenTofu`, `prune`, `retention`, `restic`), and any text node that is only a number must sit under a mono-class ancestor (`span.mono`, `.stamp`, `table.classes`, …) — enforced by `tests/gui/test_vocabulary.py`.
- The estimator (`app/estimator/`) stays **frozen** — don't touch it.
- `docs/` is gitignored; only this plan/spec are force-added (`git add -f`). Commit after every task.
- Verification commands: `python3 -m pytest -q` (baseline **1148 passed**), `bats tests/bats/`, `shellcheck setup.sh scripts/*.sh scripts/lib/*.sh`, `(cd opentofu && tofu fmt -check)`.

## File Structure

| File | Responsibility |
|------|----------------|
| `provisioning/permissions.json` (new) | Required level, template fingerprint, level history + features |
| `provisioning/bucket-admin-policy.json.tmpl` | + `PolicyGrant` statement (the latent-bug fix) |
| `opentofu/main.tf`, `opentofu/outputs.tf` | Pass the extra-buckets ARN to the role policy; outputs `runtime_user_arn`, `permissions_level` |
| `config/backup.env.example` | + `PERMISSIONS_VERSION=`, `PERMISSIONS_CHECKED_AT=` |
| `app/gui/permissions.py` (new) | Levels + stamp, required set, planner, discover/apply/converge, script, verify, needs-you row |
| `app/gui/permissions_routes.py` (new) | `/setup/permissions` GET + preview/update/script/verify POSTs |
| `app/gui/templates/permissions.html` (new) | The page |
| `app/gui/provision.py` | `render_bucket_admin_policy` signature; `run_tofu_apply` returns `runtime_user_arn`; guided CLI/console steps go inline |
| `app/engine/buckets.py` | Pure `oldest_non_default_version` helper shared with the engine |
| `app/gui/readiness.py`, `app/gui/routes.py`, templates `setup.html`, `provision_home.html`, `provision_manual.html`, `provision_scripted.html`, `job_form.html` | Status surfaces, wizard gating, setup-path changes |
| `app/gui/__init__.py` | Import `permissions_routes` so its routes register on `bp` |
| `setup.sh` | Print the two ARNs + `PERMISSIONS_VERSION` |

---

### Task 1: Bucket-admin `PolicyGrant` fix + tofu outputs

The bucket-admin role must be able to version the extra-buckets managed policy (`buckets.grant_object_access` runs under the assumed role and today fails `AccessDenied`). Scope it to exactly that one policy ARN.

**Files:**
- Modify: `provisioning/bucket-admin-policy.json.tmpl`
- Modify: `app/gui/provision.py` (`render_bucket_admin_policy`, `run_tofu_apply`)
- Modify: `opentofu/main.tf` (the `aws_iam_role_policy.bucket_admin` `templatefile` vars)
- Modify: `opentofu/outputs.tf`
- Test: `tests/engine/test_iam_policy.py`, `tests/gui/test_provision.py`

**Interfaces:**
- Produces: `provision.render_bucket_admin_policy(bucket: str, extra_buckets_policy_arn: str, tmpl_path=...) -> str`; `run_tofu_apply(...)` result dict gains `"runtime_user_arn": str` (`""` if the output is absent); tofu output `runtime_user_arn`.

- [ ] **Step 1: Update the existing bucket-admin tests + add the PolicyGrant tests**

In `tests/engine/test_iam_policy.py`, add a module constant under the imports and pass it at every `render_bucket_admin_policy(...)` call (three call sites):

```python
EXTRA_ARN = "arn:aws:iam::123456789012:policy/backup-engine-runtime-extra-buckets"
```

```python
    doc = provision.render_bucket_admin_policy("unraid-backup-123", EXTRA_ARN)
```

Replace the tail of `test_bucket_admin_policy_config_actions_are_not_bucket_prefix_scoped` (the `len(...) == 2` assertion and the loop) with:

```python
    doc = provision.render_bucket_admin_policy("unraid-backup-123", EXTRA_ARN)
    assert "unraid-backup-123-*" not in doc
    parsed = json.loads(doc)
    stmts = {s["Sid"]: s for s in parsed["Statement"]}
    assert set(stmts) == {"CreateAndConfig", "Teardown", "PolicyGrant"}
    for sid in ("CreateAndConfig", "Teardown"):          # the S3 statements stay "*"
        assert stmts[sid]["Resource"] == "*"
```

Append:

```python
def test_bucket_admin_policy_grant_is_scoped_to_the_one_extra_buckets_policy():
    # Spec 2026-09-22 §1 (the latent bug): the role versions the extra-buckets managed
    # policy when a job's bucket is off-prefix -- IAM-write, so exactly ONE policy ARN.
    stmts = {s["Sid"]: s for s in json.loads(
        provision.render_bucket_admin_policy("unraid-backup-123", EXTRA_ARN))["Statement"]}
    grant = stmts["PolicyGrant"]
    assert grant["Effect"] == "Allow"
    assert grant["Resource"] == EXTRA_ARN
    assert sorted(grant["Action"]) == sorted([
        "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
        "iam:CreatePolicyVersion", "iam:DeletePolicyVersion"])


def test_bucket_admin_policy_never_grants_user_or_attach_writes():
    doc = provision.render_bucket_admin_policy("unraid-backup-123", EXTRA_ARN)
    for bad in ("iam:PutUserPolicy", "iam:AttachUserPolicy", "iam:CreatePolicy\"",
                "iam:*", "iam:PassRole"):
        assert bad not in doc


def test_tofu_passes_the_extra_buckets_arn_to_the_role_policy():
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    assert "extra_buckets_policy_arn = aws_iam_policy.runtime_extra_buckets.arn" in main_tf


def test_tofu_outputs_the_runtime_user_arn():
    outputs = (provision.OPENTOFU_DIR / "outputs.tf").read_text()
    assert 'output "runtime_user_arn"' in outputs
    assert "aws_iam_user.runtime.arn" in outputs
```

In `tests/gui/test_provision.py` append:

```python
def test_run_tofu_apply_returns_runtime_user_arn():
    import json as _json
    from types import SimpleNamespace
    out = {"runtime_access_key_id": {"value": "AKIARUN"},
           "runtime_secret_access_key": {"value": "runsek"},
           "bucket_name": {"value": "acme"}, "region": {"value": "us-east-1"},
           "runtime_user_arn": {"value": "arn:aws:iam::123456789012:user/backup-engine-runtime"}}

    def fake(args, *, cwd, env):
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=_json.dumps(out) if args[0] == "output" else "")

    r = provision.run_tofu_apply("acme", "us-east-1", "AK", "SK", run=fake)
    assert r["runtime_user_arn"] == "arn:aws:iam::123456789012:user/backup-engine-runtime"


def test_run_tofu_apply_tolerates_missing_runtime_user_arn():
    import json as _json
    from types import SimpleNamespace
    out = {"runtime_access_key_id": {"value": "A"}, "runtime_secret_access_key": {"value": "S"},
           "bucket_name": {"value": "b"}, "region": {"value": "us-east-1"}}

    def fake(args, *, cwd, env):
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=_json.dumps(out) if args[0] == "output" else "")

    assert provision.run_tofu_apply("b", "us-east-1", "AK", "SK", run=fake)["runtime_user_arn"] == ""
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/engine/test_iam_policy.py tests/gui/test_provision.py`
Expected: FAIL — `render_bucket_admin_policy() takes 1 positional argument` / `KeyError: 'PolicyGrant'` / `KeyError: 'runtime_user_arn'`.

- [ ] **Step 3: Implement**

`provisioning/bucket-admin-policy.json.tmpl` — full new content:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "CreateAndConfig", "Effect": "Allow",
      "Action": ["s3:CreateBucket","s3:PutBucketVersioning","s3:PutBucketPublicAccessBlock",
                 "s3:PutBucketOwnershipControls","s3:PutEncryptionConfiguration",
                 "s3:PutLifecycleConfiguration","s3:PutBucketTagging",
                 "s3:GetBucketLocation","s3:GetBucketVersioning"],
      "Resource": "*" },
    { "Sid": "Teardown", "Effect": "Allow",
      "Action": ["s3:ListAllMyBuckets","s3:GetBucketTagging","s3:ListBucketVersions",
                 "s3:DeleteObject","s3:DeleteObjectVersion","s3:DeleteBucket"],
      "Resource": "*" },
    { "Sid": "PolicyGrant", "Effect": "Allow",
      "Action": ["iam:GetPolicy","iam:GetPolicyVersion","iam:ListPolicyVersions",
                 "iam:CreatePolicyVersion","iam:DeletePolicyVersion"],
      "Resource": "${extra_buckets_policy_arn}" }
  ]
}
```

`app/gui/provision.py` — replace `render_bucket_admin_policy`:

```python
def render_bucket_admin_policy(bucket: str, extra_buckets_policy_arn: str,
                               tmpl_path: str | Path = BUCKET_ADMIN_POLICY_TEMPLATE) -> str:
    """The bucket-admin role's inline policy. `extra_buckets_policy_arn` scopes the
    PolicyGrant statement -- the role's only IAM-write -- to that ONE managed policy
    (spec 2026-09-22 §1). opentofu/main.tf renders the same template."""
    tmpl = Path(tmpl_path).read_text()
    return Template(tmpl).substitute(bucket=bucket,
                                     extra_buckets_policy_arn=extra_buckets_policy_arn)
```

In `run_tofu_apply`'s returned dict, after the `runtime_extra_buckets_policy_arn` entry, add:

```python
            "runtime_user_arn": data.get("runtime_user_arn", {}).get("value", ""),
```

`opentofu/main.tf` — the `aws_iam_role_policy.bucket_admin` resource becomes:

```hcl
resource "aws_iam_role_policy" "bucket_admin" {
  name = "${var.name_prefix}-bucket-admin-create-config"
  role = aws_iam_role.bucket_admin.name
  policy = templatefile("${path.module}/../provisioning/bucket-admin-policy.json.tmpl", {
    bucket                   = var.bucket_name
    extra_buckets_policy_arn = aws_iam_policy.runtime_extra_buckets.arn
  })
}
```

`opentofu/outputs.tf` — append:

```hcl
output "runtime_user_arn" {
  value = aws_iam_user.runtime.arn
}
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/engine/test_iam_policy.py tests/gui/test_provision.py` → PASS.
Run: `(cd opentofu && tofu fmt -check)` → exit 0 (if it reports a file, run `tofu fmt` and re-check).
Optional if network is available: `(cd opentofu && tofu init -backend=false -input=false >/dev/null && tofu validate)` → `Success!`. Do NOT commit `opentofu/.terraform/` or a new `.terraform.lock.hcl` (check `git status`).
Run: `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add provisioning/bucket-admin-policy.json.tmpl app/gui/provision.py opentofu/main.tf opentofu/outputs.tf tests/engine/test_iam_policy.py tests/gui/test_provision.py
git commit -m "fix(iam): bucket-admin role can version the extra-buckets policy (scoped PolicyGrant); tofu outputs runtime_user_arn"
```

---

### Task 2: Permission levels, the stamp, and Keys-page preservation

**Files:**
- Create: `provisioning/permissions.json`
- Create: `app/gui/permissions.py` (levels layer only)
- Modify: `opentofu/outputs.tf` (+ `permissions_level`)
- Modify: `config/backup.env.example`
- Modify: `app/gui/routes.py` (`_UI_HANDLED_KEYS`, `config_save`)
- Test: `tests/gui/test_permissions_levels.py` (new)

**Interfaces:**
- Produces (in `app/gui/permissions.py`): `MANIFEST: Path`, `STAMP_KEY = "PERMISSIONS_VERSION"`, `CHECKED_KEY = "PERMISSIONS_CHECKED_AT"`, `load_manifest(path=MANIFEST) -> dict`, `required_level(path=MANIFEST) -> int`, `history(path=MANIFEST) -> list[dict]` (each `{"level": int, "adds": str, "features": list[str]}`), `templates_sha256(prov_dir=...) -> str`, `current_level(config_dir) -> int | None`, `checked_at(config_dir) -> str | None`, `feature_level(feature, path=MANIFEST) -> int | None`, `feature_available(config_dir, feature) -> bool`, `level_status(config_dir) -> dict` with keys `state` (`"current"|"behind"|"unchecked"`), `level`, `required`, `missing` (history entries above the stamp), `checked_at`.

- [ ] **Step 1: Write the failing tests**

`tests/gui/test_permissions_levels.py`:

```python
# tests/gui/test_permissions_levels.py — the permissions level manifest + the
# PERMISSIONS_VERSION stamp (spec 2026-09-22 §1).
import json
from pathlib import Path

import pytest
from app.gui import config_io, create_app, permissions, provision


def _env(dirs, text):
    Path(dirs["config"], "backup.env").write_text(text)


def test_manifest_level_is_the_top_of_a_contiguous_history():
    levels = [h["level"] for h in permissions.history()]
    assert levels == list(range(1, len(levels) + 1))
    assert permissions.required_level() == levels[-1]
    for h in permissions.history():
        assert h["adds"] and isinstance(h["features"], list)


def test_templates_fingerprint_matches_the_manifest():
    # The bump rule: editing any provisioning/*.tmpl must bump `level`, add a history
    # line, and update `templates_sha256`. This fails until all three are done.
    assert permissions.load_manifest()["templates_sha256"] == permissions.templates_sha256(), (
        "provisioning/*.tmpl changed: bump `level` in provisioning/permissions.json, add a "
        "history entry saying what it adds, and set templates_sha256 to "
        f"{permissions.templates_sha256()}")


def test_dedicated_buckets_is_a_level_three_feature():
    assert permissions.feature_level("dedicated-buckets") == 3
    assert permissions.feature_level("no-such-feature") is None


def test_tofu_outputs_the_permissions_level_from_the_manifest():
    outputs = (provision.OPENTOFU_DIR / "outputs.tf").read_text()
    assert 'output "permissions_level"' in outputs
    assert "provisioning/permissions.json" in outputs


def test_no_stamp_is_unchecked(dirs):
    _env(dirs, "S3_BUCKET=acme\n")
    st = permissions.level_status(dirs["config"])
    assert st["state"] == "unchecked" and st["level"] is None and st["missing"] == []
    assert permissions.feature_available(dirs["config"], "dedicated-buckets") is False


def test_current_stamp(dirs):
    _env(dirs, f"PERMISSIONS_VERSION={permissions.required_level()}\n"
               "PERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n")
    st = permissions.level_status(dirs["config"])
    assert st["state"] == "current" and st["checked_at"] == "2026-09-22T10:00:00Z"
    assert permissions.feature_available(dirs["config"], "dedicated-buckets") is True


def test_behind_stamp_lists_what_an_update_adds(dirs):
    _env(dirs, "PERMISSIONS_VERSION=2\n")
    st = permissions.level_status(dirs["config"])
    assert st["state"] == "behind"
    assert [h["level"] for h in st["missing"]] == list(range(3, permissions.required_level() + 1))
    assert permissions.feature_available(dirs["config"], "dedicated-buckets") is False


def test_garbage_stamp_is_unchecked(dirs):
    _env(dirs, "PERMISSIONS_VERSION=three\n")
    assert permissions.current_level(dirs["config"]) is None


def test_template_carries_the_stamp_keys(template_path):
    keys = config_io.template_keys(template_path)
    assert "PERMISSIONS_VERSION" in keys and "PERMISSIONS_CHECKED_AT" in keys


# --- the Keys page never shows the stamp, and saving it keeps the stamp ------

@pytest.fixture
def client(dirs, template_path):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    _env(dirs, "S3_BUCKET=acme\nAWS_REGION=us-east-1\n"
               "PERMISSIONS_VERSION=3\nPERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n")
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SECRET_KEY": "test", "TESTING": True})
    return app.test_client()


def test_keys_page_does_not_render_the_stamp(client):
    body = client.get("/setup/keys").get_data(as_text=True)
    assert 'name="PERMISSIONS_VERSION"' not in body
    assert 'name="PERMISSIONS_CHECKED_AT"' not in body


def test_saving_keys_keeps_the_stamp(client, dirs):
    client.get("/setup/keys")
    with client.session_transaction() as s:
        token = s["_csrf"]
    r = client.post("/setup/keys", data={"csrf": token, "S3_BUCKET": "acme", "AWS_REGION": "us-east-1"})
    assert r.status_code in (302, 303)
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == "3"
    assert env["PERMISSIONS_CHECKED_AT"] == "2026-09-22T10:00:00Z"
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_levels.py`
Expected: FAIL — `ImportError: cannot import name 'permissions'`.

- [ ] **Step 3: Implement**

`app/gui/permissions.py` (new):

```python
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
from pathlib import Path

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
```

`provisioning/permissions.json` (new) — first write it with an empty fingerprint:

```json
{
  "level": 3,
  "templates_sha256": "",
  "history": [
    {"level": 1, "adds": "Back up to and restore from your bucket", "features": []},
    {"level": 2, "adds": "Clean up old versions of archived files", "features": ["archive-cleanup"]},
    {"level": 3, "adds": "Dedicated per-job buckets", "features": ["dedicated-buckets"]}
  ]
}
```

Then compute the fingerprint and paste it in as the `templates_sha256` value:

Run: `python3 -c "from app.gui import permissions; print(permissions.templates_sha256())"`

`opentofu/outputs.tf` — append:

```hcl
output "permissions_level" {
  value = jsondecode(file("${path.module}/../provisioning/permissions.json")).level
}
```

`config/backup.env.example` — after the `# --- Multi-bucket feature (optional) ---` block (after `BASE_BUCKET_VERSIONED=true`), add:

```
# --- AWS permissions level (written by Setup → AWS permissions; don't edit) ---
PERMISSIONS_VERSION=
PERMISSIONS_CHECKED_AT=
```

`app/gui/routes.py` — replace `_UI_HANDLED_KEYS = {"AUTO_RESUME_ON_BOOT"}` with:

```python
# Template keys the Keys page never renders as plain inputs. AUTO_RESUME_ON_BOOT is
# a checkbox; the permissions stamp is written only by Setup → AWS permissions.
_STAMP_KEYS = ("PERMISSIONS_VERSION", "PERMISSIONS_CHECKED_AT")
_UI_HANDLED_KEYS = {"AUTO_RESUME_ON_BOOT", *_STAMP_KEYS}
```

In `config_save`, directly after the `values["AUTO_RESUME_ON_BOOT"] = ...` line, add:

```python
    # The stamp is never on this form; carry the saved values through, or
    # write_backup_env would reset them to the template's blank.
    for k in _STAMP_KEYS:
        values[k] = before.get(k, "")
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_levels.py` → PASS.
Run: `(cd opentofu && tofu fmt -check)` → exit 0.
Run: `python3 -m pytest -q && bats tests/bats/` → all pass.

- [ ] **Step 5: Commit**

```bash
git add provisioning/permissions.json app/gui/permissions.py opentofu/outputs.tf config/backup.env.example app/gui/routes.py tests/gui/test_permissions_levels.py
git commit -m "feat(permissions): level manifest + PERMISSIONS_VERSION stamp (hidden from, and preserved by, the Keys page)"
```

---

### Task 3: The required IAM set, normalization, and the pure planner

**Files:**
- Modify: `app/gui/permissions.py` (engine part 1)
- Test: `tests/gui/test_permissions_plan.py` (new)

**Interfaces:**
- Consumes: `provision.render_policy(bucket, role_arn)`, `provision.render_bucket_admin_policy(bucket, extra_arn)` (Task 1).
- Produces: constants `PREFIX`, `RUNTIME_POLICY_NAME`, `ROLE_NAME`, `ROLE_POLICY_NAME`, `EXTRA_POLICY_NAME`, `EXTRA_POLICY_TEMPLATE`, `TOFU_RESOURCES: dict[str, str]`, `TOFU_UNMANAGED: set[str]`; `class PermissionsError(Exception)` with `.kind`, `.detail`, `.action`; `@dataclass(frozen=True) Principal(account, user, arn)`; `parse_principal(arn) -> Principal`; `role_arn(account)`, `extra_policy_arn(account)`, `runtime_managed_arn(account)`; `trust_doc(user_arn) -> dict`; `required_docs(principal, bucket) -> dict` with keys `extra`, `trust`, `role`, `runtime`; `normalize(doc) -> dict`; `same_policy(a, b) -> bool`; `diff_statements(live, want) -> list[str]`; `@dataclass Live`; `@dataclass Step(id, action, summary, argv, diff=[], status="pending", error="")` with `.command() -> str`; `plan(live, principal, bucket) -> list[Step]`; `_doc(v) -> dict | None`.

- [ ] **Step 1: Write the failing tests**

`tests/gui/test_permissions_plan.py`:

```python
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
    return permissions.Live(runtime_inline=WANT["runtime"], extra_exists=True, extra_attached=True,
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


def test_required_role_policy_grant_targets_the_extra_buckets_policy():
    stmts = {s["Sid"]: s for s in WANT["role"]["Statement"]}
    assert stmts["PolicyGrant"]["Resource"] == permissions.extra_policy_arn(ACCOUNT)


def test_tofu_iam_resources_match_the_converge_table():
    # Drift guard: every IAM resource main.tf creates is either converged (R1-R5) or
    # deliberately unmanaged (the user + its access key).
    main_tf = (provision.OPENTOFU_DIR / "main.tf").read_text()
    found = {f"{t}.{n}" for t, n in re.findall(r'^resource "(aws_iam_[a-z_]+)" "([a-z_]+)"', main_tf, re.M)}
    assert found == set(permissions.TOFU_RESOURCES) | permissions.TOFU_UNMANAGED
    assert sorted(permissions.TOFU_RESOURCES.values()) == ["R1", "R2", "R3", "R4", "R5"]


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


# --- the planner ----------------------------------------------------------------

def test_current_install_plans_nothing():
    assert permissions.plan(_current(), P, BUCKET) == []


def test_owners_level2_box_plans_all_five_in_order():
    live = permissions.Live(runtime_inline=_level2_runtime())
    steps = permissions.plan(live, P, BUCKET)
    assert [s.id for s in steps] == ["R1", "R2", "R3", "R4", "R5"]
    assert [s.action for s in steps] == ["create-policy", "attach", "create-role",
                                         "put-role-policy", "put-user-policy"]
    r5 = steps[-1]
    assert any(line.startswith("+ AssumeBucketAdminRole: sts:AssumeRole on "
                               f"{permissions.role_arn(ACCOUNT)}") for line in r5.diff)
    assert not any(line.startswith("- ") for line in r5.diff)   # nothing removed


def test_level1_runtime_policy_shows_a_changed_statement():
    old = _level2_runtime()
    for s in old["Statement"]:
        if s["Sid"] == "ListBucketScoped":
            s["Action"] = ["s3:ListBucket", "s3:GetBucketLocation"]
    live = _current()
    live.runtime_inline = old
    steps = permissions.plan(live, P, BUCKET)
    assert [s.id for s in steps] == ["R5"]
    assert any(line.startswith("~ ListBucketScoped") for line in steps[0].diff)


def test_extra_policy_present_but_unattached_only_attaches():
    live = _current()
    live.extra_attached = False
    steps = permissions.plan(live, P, BUCKET)
    assert [(s.id, s.action) for s in steps] == [("R2", "attach")]
    assert steps[0].argv == ["iam", "attach-user-policy", "--user-name", "backup-engine-runtime",
                             "--policy-arn", permissions.extra_policy_arn(ACCOUNT)]


def test_existing_extra_policy_contents_are_never_planned():
    live = _current()   # extra_exists=True; its (runtime-granted) contents are unknown to us
    assert not any(s.id == "R1" for s in permissions.plan(live, P, BUCKET))


def test_drifted_trust_is_reset():
    live = _current()
    live.role_trust = permissions.trust_doc(f"arn:aws:iam::{ACCOUNT}:user/someone-else")
    steps = permissions.plan(live, P, BUCKET)
    assert [(s.id, s.action) for s in steps] == [("R3", "update-trust")]


def test_legacy_managed_runtime_policy_gets_a_new_version():
    live = _current()
    live.runtime_inline = None
    live.runtime_managed_arn, live.runtime_managed_doc = LEGACY_ARN, _level2_runtime()
    steps = permissions.plan(live, P, BUCKET)
    assert [(s.id, s.action) for s in steps] == [("R5", "new-version")]
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
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_plan.py`
Expected: FAIL — `AttributeError: module 'app.gui.permissions' has no attribute 'parse_principal'`.

- [ ] **Step 3: Implement** — append to `app/gui/permissions.py` (and extend its imports):

Imports at the top become:

```python
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from . import config_io, provision
```

Append:

```python
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
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_plan.py` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/permissions.py tests/gui/test_permissions_plan.py
git commit -m "feat(permissions): required IAM set R1-R5, policy normalization + diff, pure planner, tofu drift guard"
```

---

### Task 4: Discover, apply, converge — with the stamp and an Activity record

**Files:**
- Modify: `app/engine/buckets.py` (pure `oldest_non_default_version`; `grant_object_access` uses it)
- Modify: `app/gui/permissions.py` (engine part 2)
- Test: `tests/gui/test_permissions_engine.py` (new), `tests/engine/test_buckets_ensure.py` (append)

**Interfaces:**
- Consumes: Task 3's `plan`, `Live`, `Step`, `Principal`, names; `provision.verify_admin_can_provision`, `provision.aws_account_id`, `provision._scrub`, `provision._run_aws`.
- Produces: `buckets.oldest_non_default_version(versions: list[dict], limit: int = 5) -> str | None`; in `permissions`: `@dataclass(frozen=True) AdminCreds(key, secret, token=None)`; `runtime_principal(region, key, secret, *, run=...) -> Principal`; `discover(principal, *, region, admin, run=...) -> Live`; `apply(steps, *, region, admin, run=...) -> list[Step]`; `write_stamp(config_dir, template_path, principal, *, now=None) -> None`; `record(cache_dir, *, mode: str, lines: list[str]) -> str`; `@dataclass Outcome(ok, applied, steps, remaining=[])`; `converge(principal, *, bucket, region, admin, config_dir, template_path, cache_dir, apply_changes=True, mode="update", run=...) -> Outcome` — raises `provision.AdminCapabilityError`, `provision.AccountLookupError` or `PermissionsError` before any change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_buckets_ensure.py`:

```python
def test_oldest_non_default_version_only_at_the_limit():
    from app.engine.buckets import oldest_non_default_version
    four = [{"VersionId": f"v{i}", "IsDefaultVersion": i == 4, "CreateDate": f"2026-01-0{i}"} for i in range(1, 5)]
    assert oldest_non_default_version(four) is None
    five = four + [{"VersionId": "v5", "IsDefaultVersion": False, "CreateDate": "2026-01-05"}]
    assert oldest_non_default_version(five) == "v1"
```

`tests/gui/test_permissions_engine.py`:

```python
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


def _current_box():
    return FakeIAM(inline={permissions.RUNTIME_POLICY_NAME: WANT["runtime"]},
                   managed={EXTRA_ARN: (permissions.EXTRA_POLICY_NAME, [WANT["extra"]])},
                   attached={EXTRA_ARN},
                   role={"trust": WANT["trust"], "policies": {permissions.ROLE_POLICY_NAME: WANT["role"]}})


def _converge(fake, dirs, template_path, **kw):
    return permissions.converge(P, bucket=BUCKET, region="us-east-1", admin=ADMIN,
                                config_dir=dirs["config"], template_path=template_path,
                                cache_dir=dirs["cache"], run=fake, **kw)


def _system_records(dirs):
    p = Path(dirs["cache"], "state", "_system.runs.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


_WRITE_OPS = {"create-policy", "attach-user-policy", "create-role", "update-assume-role-policy",
              "put-role-policy", "put-user-policy", "create-policy-version", "delete-policy-version"}
_NEVER = {"create-access-key", "delete-access-key", "update-access-key", "delete-user",
          "delete-role", "delete-policy", "delete-user-policy", "delete-role-policy",
          "detach-user-policy", "detach-role-policy"}


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
    assert [(s.id, s.status) for s in out.steps] == [("R1", "done"), ("R2", "done"), ("R3", "done"),
                                                      ("R4", "done"), ("R5", "done")]
    assert fake.inline[permissions.RUNTIME_POLICY_NAME] == WANT["runtime"]
    assert fake.role["policies"][permissions.ROLE_POLICY_NAME] == WANT["role"]
    assert EXTRA_ARN in fake.attached
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())
    assert env["PERMISSIONS_CHECKED_AT"].endswith("Z")
    assert env["BUCKET_ADMIN_ROLE_ARN"] == permissions.role_arn(ACCOUNT)
    assert env["RUNTIME_EXTRA_BUCKETS_POLICY_ARN"] == EXTRA_ARN
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
    assert out.ok and not out.applied and len(out.steps) == 5
    assert all(s.status == "pending" for s in out.steps)
    assert not any(c[1] in _WRITE_OPS for c in fake.calls)
    assert "PERMISSIONS_VERSION" not in config_io.read_backup_env(dirs["config"])


def test_denied_step_stops_the_run_and_is_scrubbed(dirs, template_path):
    fake = _level2_box(deny={"PutRolePolicy"})
    out = _converge(fake, dirs, template_path)
    assert not out.ok and out.applied
    assert [(s.id, s.status) for s in out.steps] == [("R1", "done"), ("R2", "done"), ("R3", "done"),
                                                      ("R4", "failed"), ("R5", "not-run")]
    err = out.steps[3].error
    assert "PutRolePolicy" in err
    assert ADMIN.key not in err and ADMIN.secret not in err
    assert "PERMISSIONS_VERSION" not in config_io.read_backup_env(dirs["config"])


def test_rerun_after_a_failure_finishes_the_job(dirs, template_path):
    fake = _level2_box(deny={"PutRolePolicy"})
    _converge(fake, dirs, template_path)
    fake.deny.clear()
    out = _converge(fake, dirs, template_path)
    assert out.ok and [s.id for s in out.steps] == ["R4", "R5"]


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
    fake = FakeIAM(managed={LEGACY_ARN: (permissions.RUNTIME_POLICY_NAME, [old] * 5),
                            EXTRA_ARN: (permissions.EXTRA_POLICY_NAME, [WANT["extra"]])},
                   attached={LEGACY_ARN, EXTRA_ARN},
                   role={"trust": WANT["trust"], "policies": {permissions.ROLE_POLICY_NAME: WANT["role"]}})
    out = _converge(fake, dirs, template_path)
    assert out.ok and [(s.id, s.action) for s in out.steps] == [("R5", "new-version")]
    deleted = [c for c in fake.calls if c[1] == "delete-policy-version"]
    assert deleted and deleted[0][deleted[0].index("--version-id") + 1] == "v1"
    assert fake._default(LEGACY_ARN)["Document"] == WANT["runtime"]
    _assert_never_touch(fake)


def test_create_that_races_an_existing_entity_counts_as_done():
    steps = [permissions.Step("R3", "create-role", "x", ["iam", "create-role", "--role-name", "r",
                                                         "--assume-role-policy-document", "{}"])]

    def exists(args, *, region, key, secret, session_token=None):
        return SimpleNamespace(returncode=254, stdout="",
                               stderr="An error occurred (EntityAlreadyExists) when calling the CreateRole operation")
    permissions.apply(steps, region="us-east-1", admin=ADMIN, run=exists)
    assert steps[0].status == "done"
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_engine.py tests/engine/test_buckets_ensure.py`
Expected: FAIL — `AttributeError: ... 'AdminCreds'` / `cannot import name 'oldest_non_default_version'`.

- [ ] **Step 3: Implement**

`app/engine/buckets.py` — add above `grant_object_access`:

```python
def oldest_non_default_version(versions: list[dict], limit: int = 5) -> str | None:
    """IAM keeps at most `limit` versions of a managed policy. At the limit, the
    oldest non-default version is the one to delete before publishing another."""
    if len(versions) < limit:
        return None
    non_default = [v for v in versions if not v.get("IsDefaultVersion")]
    if not non_default:
        return None
    return sorted(non_default, key=lambda v: v.get("CreateDate", ""))[0]["VersionId"]
```

and in `grant_object_access` replace the `if len(vlist) >= 5:` block with:

```python
    oldest = oldest_non_default_version(vlist)
    if oldest:
        _aws(runner, ["iam", "delete-policy-version", "--policy-arn", policy_arn,
                      "--version-id", oldest, "--region", region], creds)
```

`app/gui/permissions.py` — extend imports:

```python
from datetime import datetime, timezone

from . import config_io, provision
from ..engine import buckets, runs
```

Append:

```python
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
            return json.loads(cp.stdout or "{}")
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
    extra = extra_policy_arn(principal.account)
    live.extra_exists = get(["iam", "get-policy", "--policy-arn", extra]) is not None
    live.extra_attached = extra in attached
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
    victim = buckets.oldest_non_default_version(json.loads(cp.stdout or "{}").get("Versions", []))
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
        raced = s.action in ("create-policy", "create-role") and "EntityAlreadyExists" in (cp.stderr or "")
        if cp.returncode == 0 or raced:
            s.status = "done"
        else:
            s.status, s.error, halted = "failed", _scrub(admin, cp.stderr), True
    return steps


def _now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_stamp(config_dir: str, template_path: str, principal: Principal, *, now=None) -> None:
    """Record that this install matches this build: the two ARNs + level + time."""
    env = config_io.read_backup_env(config_dir)
    env.update({"BUCKET_ADMIN_ROLE_ARN": role_arn(principal.account),
                "RUNTIME_EXTRA_BUCKETS_POLICY_ARN": extra_policy_arn(principal.account),
                STAMP_KEY: str(required_level()), CHECKED_KEY: _now_iso(now)})
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
             run=provision._run_aws) -> Outcome:
    """Discover -> plan -> (apply -> re-discover -> re-plan must be empty) -> stamp.
    Raises provision.AdminCapabilityError / provision.AccountLookupError /
    PermissionsError before any change; step failures come back in the Outcome."""
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
    remaining = plan(discover(principal, region=region, admin=admin, run=run), principal, bucket)
    if remaining:
        return Outcome(ok=False, applied=True, steps=steps, remaining=remaining)
    write_stamp(config_dir, template_path, principal)
    record(cache_dir, mode=mode, lines=[s.summary for s in steps])
    return Outcome(ok=True, applied=True, steps=steps)
```

Also update the header comment of `app/gui/provision.py`: its first lines say it is "The ONLY module that renders the policy / calls aws / invokes tofu". Change that sentence to: "Together with permissions.py (the converge engine), the only GUI modules that render the policy / call aws / invoke tofu."

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_engine.py tests/engine/` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/engine/buckets.py app/gui/permissions.py app/gui/provision.py tests/gui/test_permissions_engine.py tests/engine/test_buckets_ensure.py
git commit -m "feat(permissions): discover/apply/converge over the aws CLI + stamp + Activity record"
```

---

### Task 5: The commands fallback — re-runnable script + runtime-key Verify

**Files:**
- Modify: `app/gui/permissions.py` (fallback layer)
- Test: `tests/gui/test_permissions_fallback.py` (new)

**Interfaces:**
- Consumes: Tasks 3–4 (`required_docs`, names, `Principal`, `provision.assume_role`).
- Produces: `script(principal, *, bucket, region) -> str` (raises `PermissionsError("script")`); `@dataclass Probe(name, ok, hint="", detail="")`; `verify(principal, *, bucket, region, key, secret, run=..., sleep=time.sleep, tries=3, wait_s=5.0) -> list[Probe]` (always 4 probes, in order: list versions, assume role, role can read the extra policy, extra policy attached).

- [ ] **Step 1: Write the failing tests**

`tests/gui/test_permissions_fallback.py`:

```python
# tests/gui/test_permissions_fallback.py — the no-admin-creds path: a convergent
# bash script + runtime-key-only Verify probes (spec 2026-09-22 §2).
import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest
from app.gui import permissions

ACCOUNT = "123456789012"
USER_ARN = f"arn:aws:iam::{ACCOUNT}:user/backup-engine-runtime"
BUCKET = f"unraid-backup-{ACCOUNT}"
P = permissions.parse_principal(USER_ARN)


def _script():
    return permissions.script(P, bucket=BUCKET, region="us-east-1")


def test_script_is_valid_bash():
    r = subprocess.run(["bash", "-n"], input=_script(), text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_script_embeds_exactly_the_required_documents():
    s = _script()
    docs = dict(re.findall(r"cat > \"\$d/([\w-]+)\.json\" <<'EOF'\n(.*?)\nEOF", s, re.S))
    want = permissions.required_docs(P, BUCKET)
    assert json.loads(docs["extra-buckets"]) == want["extra"]
    assert json.loads(docs["trust"]) == want["trust"]
    assert json.loads(docs["bucket-admin"]) == want["role"]
    assert json.loads(docs["runtime"]) == want["runtime"]


def test_script_only_creates_what_is_missing_and_never_touches_keys_or_data():
    s = _script()
    assert f"aws iam get-policy --policy-arn {permissions.extra_policy_arn(ACCOUNT)} >/dev/null 2>&1 || aws iam create-policy" in s
    assert "aws iam get-role --role-name backup-engine-bucket-admin >/dev/null 2>&1 || aws iam create-role" in s
    for bad in ("access-key", "s3api", "aws s3 ", "delete-", "create-policy-version"):
        assert bad not in s


def test_script_runs_in_order_against_a_stub_aws(tmp_path):
    log = tmp_path / "calls"
    stub = tmp_path / "bin"; stub.mkdir()
    (stub / "aws").write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$1 $2\" >> {log}\n"
        "for a in \"$@\"; do case \"$a\" in file://*) test -s \"${a#file://}\" || exit 9;; esac; done\n"
        "case \"$1 $2\" in 'iam get-policy'|'iam get-role') exit 1;; esac\n"
        "exit 0\n")
    (stub / "aws").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", _script()], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [
        "iam get-policy", "iam create-policy", "iam attach-user-policy",
        "iam get-role", "iam create-role", "iam update-assume-role-policy",
        "iam put-role-policy", "iam put-user-policy"]


@pytest.mark.parametrize("principal,bucket,region", [
    (permissions.Principal(ACCOUNT, "x; rm -rf /", USER_ARN), BUCKET, "us-east-1"),
    (permissions.Principal("12345", "backup-engine-runtime", USER_ARN), BUCKET, "us-east-1"),
    (P, "Bad_Bucket;id", "us-east-1"),
    (P, BUCKET, "us-east-1; id"),
])
def test_script_refuses_unexpected_values(principal, bucket, region):
    with pytest.raises(permissions.PermissionsError) as e:
        permissions.script(principal, bucket=bucket, region=region)
    assert e.value.kind == "script"


# --- verify ------------------------------------------------------------------------

def _fake(*, list_ok=True, assume_ok=True, get_ok=True, attached=1, flaky=0):
    state = {"flaky": flaky}

    def run(args, *, region, key, secret, session_token=None):
        if args[:2] == ["s3api", "list-object-versions"]:
            if state["flaky"] > 0:
                state["flaky"] -= 1
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied")
            return SimpleNamespace(returncode=0 if list_ok else 254, stdout="{}",
                                   stderr="" if list_ok else f"AccessDenied {secret}")
        if args[:2] == ["sts", "assume-role"]:
            if not assume_ok:
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied sts:AssumeRole")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Credentials": {
                "AccessKeyId": "ASIATMP", "SecretAccessKey": "tmpsek", "SessionToken": "tok"}}))
        if args[:2] == ["iam", "get-policy"]:
            assert session_token == "tok" and key == "ASIATMP"     # runs as the ROLE
            if not get_ok:
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied iam:GetPolicy")
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"Policy": {"AttachmentCount": attached}}))
        raise AssertionError(args)
    return run


def _verify(run, **kw):
    return permissions.verify(P, bucket=BUCKET, region="us-east-1", key="AKIARUN", secret="runsek",
                              run=run, sleep=lambda s: None, **kw)


def test_verify_all_good():
    probes = _verify(_fake())
    assert len(probes) == 4 and all(p.ok for p in probes)


def test_verify_names_the_script_step_when_the_role_is_missing():
    probes = _verify(_fake(assume_ok=False))
    assert [p.ok for p in probes] == [True, False, False, False]
    assert "step 3" in probes[1].hint


def test_verify_detects_a_missing_policy_grant():
    probes = _verify(_fake(get_ok=False))
    assert [p.ok for p in probes] == [True, True, False, False]
    assert "step 4" in probes[2].hint


def test_verify_detects_an_unattached_extra_policy():
    probes = _verify(_fake(attached=0))
    assert [p.ok for p in probes] == [True, True, True, False]
    assert "step 2" in probes[3].hint


def test_verify_retries_for_iam_propagation():
    slept = []
    probes = permissions.verify(P, bucket=BUCKET, region="us-east-1", key="AKIARUN", secret="runsek",
                                run=_fake(flaky=1), sleep=slept.append)
    assert all(p.ok for p in probes) and slept == [5.0]


def test_verify_scrubs_the_runtime_secret():
    probes = _verify(_fake(list_ok=False), tries=1)
    assert "runsek" not in probes[0].detail
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_fallback.py`
Expected: FAIL — `AttributeError: ... has no attribute 'script'`.

- [ ] **Step 3: Implement** — add `import time` to the imports and append to `app/gui/permissions.py`:

```python
# --- the commands fallback: a convergent script + runtime-key Verify ---------------------

_ACCOUNT_RE = re.compile(r"^\d{12}$")
_IAM_USER_RE = re.compile(r"^[\w+=,.@-]{1,64}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def script(principal: Principal, *, bucket: str, region: str) -> str:
    """A bash script that converges IAM with only idempotent commands (for CloudShell).
    Every interpolated value is shape-checked first, and the policy JSON goes in
    quoted heredocs, so nothing in it can reach the shell."""
    if not (_ACCOUNT_RE.match(principal.account) and _IAM_USER_RE.match(principal.user)
            and buckets.valid_bucket_name(bucket) and _REGION_RE.match(region or "")):
        raise PermissionsError("script", "the account, user, bucket or region has an unexpected shape")
    docs = required_docs(principal, bucket)
    user, extra = principal.user, extra_policy_arn(principal.account)

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
        *heredoc("extra-buckets", docs["extra"]),
        *heredoc("trust", docs["trust"]),
        *heredoc("bucket-admin", docs["role"]),
        *heredoc("runtime", docs["runtime"]),
        "# 1. The extra-buckets policy (created once; backup-engine adds grants to it later)",
        f"aws iam get-policy --policy-arn {extra} >/dev/null 2>&1 || "
        f"aws iam create-policy --policy-name {EXTRA_POLICY_NAME} "
        f"--policy-document \"file://$d/extra-buckets.json\" >/dev/null",
        "# 2. Attach it to the backup user",
        f"aws iam attach-user-policy --user-name {user} --policy-arn {extra}",
        "# 3. The bucket-admin role, which only the backup user may assume",
        f"aws iam get-role --role-name {ROLE_NAME} >/dev/null 2>&1 || "
        f"aws iam create-role --role-name {ROLE_NAME} "
        f"--assume-role-policy-document \"file://$d/trust.json\" >/dev/null",
        f"aws iam update-assume-role-policy --role-name {ROLE_NAME} "
        f"--policy-document \"file://$d/trust.json\"",
        "# 4. The bucket-admin role's policy",
        f"aws iam put-role-policy --role-name {ROLE_NAME} --policy-name {ROLE_POLICY_NAME} "
        f"--policy-document \"file://$d/bucket-admin.json\"",
        "# 5. The backup user's own policy",
        f"aws iam put-user-policy --user-name {user} --policy-name {RUNTIME_POLICY_NAME} "
        f"--policy-document \"file://$d/runtime.json\"",
        "# Older guided installs only: if the managed policy below is attached to the backup",
        "# user, it is now redundant and can be detached (optional):",
        f"#   aws iam detach-user-policy --user-name {user} "
        f"--policy-arn {runtime_managed_arn(principal.account)}",
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
    grant_probe = f"The role can manage {EXTRA_POLICY_NAME}"
    attach_probe = f"{EXTRA_POLICY_NAME} is attached to {principal.user}"
    out = []
    cp = run(["s3api", "list-object-versions", "--bucket", bucket, "--max-items", "1",
              "--output", "json"], region=region, key=key, secret=secret)
    ok = cp.returncode == 0
    out.append(Probe("Can list old versions in the backup bucket", ok,
                     "" if ok else "Step 5 of the script sets this — did it run?",
                     "" if ok else provision._scrub(cp.stderr or "", key, secret).strip()))
    try:
        creds = provision.assume_role(role_arn(principal.account), region=region, key=key,
                                      secret=secret, run=run)
    except provision.AssumeRoleError as e:
        out.append(Probe(role_probe, False, "Steps 3 and 5 of the script set this up — did step 3 run?",
                         provision._scrub(str(e), key, secret)))
        out.append(Probe(grant_probe, False, "Needs the role first (step 3)."))
        out.append(Probe(attach_probe, False, "Needs the role first (step 3)."))
        return out
    out.append(Probe(role_probe, True))
    rk, rs, rt = creds["AWS_ACCESS_KEY_ID"], creds["AWS_SECRET_ACCESS_KEY"], creds["AWS_SESSION_TOKEN"]
    cp = run(["iam", "get-policy", "--policy-arn", extra_policy_arn(principal.account), "--output", "json"],
             region=region, key=rk, secret=rs, session_token=rt)
    if cp.returncode != 0:
        out.append(Probe(grant_probe, False, "Steps 1 and 4 of the script set this up — did step 4 run?",
                         provision._scrub(cp.stderr or "", rk, rs, rt).strip()))
        out.append(Probe(attach_probe, False, "Needs the step above first."))
        return out
    out.append(Probe(grant_probe, True))
    count = json.loads(cp.stdout or "{}").get("Policy", {}).get("AttachmentCount", 0)
    out.append(Probe(attach_probe, count >= 1, "" if count else "Step 2 of the script attaches it — did it run?"))
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
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_fallback.py` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/permissions.py tests/gui/test_permissions_fallback.py
git commit -m "feat(permissions): convergent CloudShell script + runtime-key-only Verify probes"
```

---

### Task 6: Status surfaces — Setup row, Board needs-you row, Destination line, Activity label

**Files:**
- Modify: `app/gui/permissions.py` (+ `needs_you_row`)
- Modify: `app/gui/readiness.py` (`_check_permissions`, `setup_checks`)
- Modify: `app/gui/routes.py` (`_SETUP_CHECK_NAMES`, `_board_payload`, `provision_home`, `_WHAT_LABELS`, `_KIND_GROUPS`, import `permissions`)
- Modify: `app/gui/templates/setup.html`, `app/gui/templates/provision_home.html`
- Test: `tests/gui/test_permissions_surfaces.py` (new)

**Interfaces:**
- Consumes: `permissions.level_status`, `permissions.record` (Tasks 2, 4).
- Produces: `permissions.needs_you_row(config_dir) -> dict | None` (Board row shape: `level`, `code="permissions-update"`, `job=None`, `strong`, `text`, `fix={"label","href"}`); readiness row `code="permissions"`. Links use the literal path `/setup/permissions` (the route arrives in Task 7).

- [ ] **Step 1: Write the failing tests**

`tests/gui/test_permissions_surfaces.py`:

```python
# tests/gui/test_permissions_surfaces.py — where "permissions behind" shows up
# (spec 2026-09-22 §3). Reads backup.env only; no AWS at render.
from pathlib import Path

import pytest
from app.gui import config_io, create_app, permissions, readiness


def _provision(dirs, stamp=None):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    env = "S3_BUCKET=acme\nAWS_REGION=us-east-1\n"
    if stamp is not None:
        env += f"PERMISSIONS_VERSION={stamp}\nPERMISSIONS_CHECKED_AT=2026-09-22T10:00:00Z\n"
    Path(dirs["config"], "backup.env").write_text(env)


def _client(dirs, template_path):
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": dirs["cache"], "SECRET_KEY": "test", "TESTING": True,
                       "PRICES_LIVE": False}).test_client()


def _rows(dirs):
    rows = readiness.setup_checks({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"]})
    return {r["code"]: r for r in rows}


def test_no_permissions_row_when_unprovisioned(dirs):
    assert "permissions" not in _rows(dirs)


def test_row_warns_when_unchecked(dirs):
    _provision(dirs)
    r = _rows(dirs)["permissions"]
    assert r["state"] == "warn"
    assert r["sentence"] == "Not checked for this version of backup-engine"
    assert r["fix_url"] == "/setup/permissions"


def test_row_ok_when_current(dirs):
    _provision(dirs, stamp=permissions.required_level())
    r = _rows(dirs)["permissions"]
    assert r["state"] == "ok" and r["verified_at"] == "2026-09-22T10:00:00Z"


def test_row_names_what_an_update_adds_when_behind(dirs):
    _provision(dirs, stamp=2)
    r = _rows(dirs)["permissions"]
    assert r["state"] == "warn" and "Dedicated per-job buckets" in r["sentence"]


def test_needs_you_row_only_when_behind_or_unchecked(dirs):
    assert permissions.needs_you_row(dirs["config"]) is None           # unprovisioned
    _provision(dirs, stamp=permissions.required_level())
    assert permissions.needs_you_row(dirs["config"]) is None           # current
    _provision(dirs)
    row = permissions.needs_you_row(dirs["config"])
    assert row["level"] == "warning" and row["code"] == "permissions-update"
    assert row["fix"] == {"label": "Update permissions", "href": "/setup/permissions"}


def test_setup_page_shows_the_row_and_link(dirs, template_path):
    _provision(dirs)
    body = _client(dirs, template_path).get("/setup").get_data(as_text=True)
    assert 'data-check="permissions"' in body
    assert "AWS permissions up to date" in body
    assert 'href="/setup/permissions"' in body


def test_board_shows_the_warning_when_unchecked(dirs, template_path):
    _provision(dirs)
    body = _client(dirs, template_path).get("/").get_data(as_text=True)
    assert "AWS permissions need an update." in body


def test_board_is_quiet_when_current(dirs, template_path):
    _provision(dirs, stamp=permissions.required_level())
    body = _client(dirs, template_path).get("/").get_data(as_text=True)
    assert "AWS permissions need an update." not in body


def test_status_json_carries_the_row(dirs, template_path):
    _provision(dirs)
    js = _client(dirs, template_path).get("/status.json").get_json()
    assert "permissions-update" in [r.get("code") for r in js["needs_you"]]


def test_destination_page_shows_the_permissions_line(dirs, template_path):
    _provision(dirs, stamp=permissions.required_level())
    body = _client(dirs, template_path).get("/setup/destination").get_data(as_text=True)
    assert 'data-perm-line="current"' in body
    assert 'href="/setup/permissions"' in body


def test_activity_labels_a_permissions_record(dirs, template_path):
    _provision(dirs)
    permissions.record(dirs["cache"], mode="update", lines=["Create the extra-buckets policy"])
    body = _client(dirs, template_path).get("/activity").get_data(as_text=True)
    assert "permissions update" in body
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_surfaces.py`
Expected: FAIL — `KeyError: 'permissions'` / `AttributeError: ... 'needs_you_row'`.

- [ ] **Step 3: Implement**

Append to `app/gui/permissions.py` (in the levels section is fine):

```python
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
```

`app/gui/readiness.py` — change the import line to `from . import config_io, jobs_io, points, vocab, estimate_io, permissions`, add after `_check_restore_tested`:

```python
def _check_permissions(config_dir) -> dict | None:
    """The permissions stamp vs this build (spec 2026-09-22 §3). Only once the
    destination is set -- before that, the setup wizard owns the story."""
    if not config_io.is_provisioned(config_dir):
        return None
    st = permissions.level_status(config_dir)
    row = {"code": "permissions", "verified_at": st["checked_at"], "fix_label": None,
           "fix_url": "/setup/permissions"}
    if st["state"] == "current":
        row.update(state="ok", sentence="Everything this version needs")
    elif st["state"] == "behind":
        row.update(state="warn", sentence="Update needed for: "
                   + "; ".join(h["adds"] for h in st["missing"]))
    else:
        row.update(state="warn", sentence="Not checked for this version of backup-engine")
    return row
```

and in `setup_checks`, before the `if crontab_stale:` block:

```python
    perm = _check_permissions(config_dir)
    if perm:
        rows.append(perm)
```

`app/gui/routes.py`:
- Add `permissions` to the `from . import (...)` list.
- `_SETUP_CHECK_NAMES` gains `"permissions": "AWS permissions up to date",`.
- `_board_payload` becomes:

```python
def _board_payload(cfg) -> dict:
    """`status.board()` (Task 4) with the cost object merged in (spec 8.1). The
    board dict was deliberately shaped so the route attaches cost here -- and the
    permissions needs-you row (spec 2026-09-22 §3), which reads backup.env only."""
    board = status.board(cfg["CONFIG_DIR"], cfg["CACHE_DIR"], cfg["SCRIPTS_DIR"],
                         source_root=cfg["SOURCE_ROOT"])
    board["cost"] = _board_cost(cfg)
    row = permissions.needs_you_row(cfg["CONFIG_DIR"])
    if row:
        board["needs_you"].append(row)
    return board
```

- `_WHAT_LABELS` gains `"permissions": "permissions update",`; `_KIND_GROUPS["setup"]` becomes `{"usage-refresh", "billing-check", "probe", "provision", "permissions"}`.
- `provision_home` passes `perm=permissions.level_status(cfg["CONFIG_DIR"])` and `perm_checked=_fmt_verified(permissions.checked_at(cfg["CONFIG_DIR"]))` to `render_template`.

`app/gui/templates/setup.html`:
- The lead paragraph's first sentence `Five real checks.` becomes `Real checks.`
- In the Fix column's `{% if %}` chain, before `{% endif %}`, add:

```html
          {% elif c.code == 'permissions' and c.state != 'ok' %}
            <a class="linklike" href="/setup/permissions">Update permissions →</a>
```

`app/gui/templates/provision_home.html` — inside the `{% if provisioned %}` block, after the success `sig` div, add:

```html
  <p class="hint" style="margin-top:var(--sp-3)" data-perm-line="{{ perm.state }}">AWS permissions:
    {% if perm.state == 'current' %}up to date{% elif perm.state == 'behind' %}an update is available{% else %}not checked for this version{% endif %}{% if perm_checked %} · checked <span class="stamp">{{ perm_checked }}</span>{% endif %}
    · <a class="linklike" href="/setup/permissions">Check &amp; update →</a></p>
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_surfaces.py tests/gui/test_vocabulary.py tests/gui/test_readiness.py tests/gui/test_board_routes.py tests/gui/test_app.py` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/permissions.py app/gui/readiness.py app/gui/routes.py app/gui/templates/setup.html app/gui/templates/provision_home.html tests/gui/test_permissions_surfaces.py
git commit -m "feat(permissions): Setup row, Board needs-you warning, Destination line, Activity label"
```

---

### Task 7: The `/setup/permissions` page — status + admin-creds Update/Preview

**Files:**
- Create: `app/gui/permissions_routes.py`
- Create: `app/gui/templates/permissions.html`
- Modify: `app/gui/__init__.py` (import the routes module)
- Modify: `tests/gui/test_vocabulary.py` (`ALL_PAGES` += `"/setup/permissions"`)
- Test: `tests/gui/test_permissions_routes.py` (new)

**Interfaces:**
- Consumes: `permissions.converge`, `permissions.runtime_principal`, `permissions.level_status`, `permissions.AdminCreds`, `permissions.PermissionsError`, `permissions.Outcome`, `permissions.Step`; `routes.bp`, `routes._fmt_verified`, `routes._runtime_creds`.
- Produces: endpoints `gui.permissions_page` (GET `/setup/permissions`, `?mode=commands` supported), `gui.permissions_preview` (POST `/setup/permissions/preview`), `gui.permissions_update` (POST `/setup/permissions/update`); helpers in `permissions_routes`: `_page(status_code=200, **ctx) -> (html, status)`, `_perm_error_message(e) -> str`, `_bucket_region(cfg) -> tuple[str, str]`, `_principal_for(cfg, region) -> Principal`. Template context: `perm`, `checked_human`, `provisioned`, `mode`, `outcome`, `preview`, `error`, `error_detail`, `script`, `probes`, `csrf`.

- [ ] **Step 1: Write the failing tests**

`tests/gui/test_permissions_routes.py`:

```python
# tests/gui/test_permissions_routes.py — /setup/permissions (spec 2026-09-22 §3).
# The engine is monkeypatched: these tests pin the routes, not AWS.
from pathlib import Path

import pytest
from app.gui import config_io, create_app, permissions, provision

ACCOUNT = "123456789012"
P = permissions.parse_principal(f"arn:aws:iam::{ACCOUNT}:user/backup-engine-runtime")
CREDS = {"ADMIN_ACCESS_KEY_ID": "ADMINKEYVALUE", "ADMIN_SECRET_ACCESS_KEY": "ADMINSECRETVALUE"}


@pytest.fixture
def app(dirs, template_path):
    config_io.write_secrets(dirs["config"], {"AWS_ACCESS_KEY_ID": "AKIARUN", "AWS_SECRET_ACCESS_KEY": "runsek"})
    Path(dirs["config"], "backup.env").write_text(f"S3_BUCKET=unraid-backup-{ACCOUNT}\nAWS_REGION=us-east-1\n")
    return create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                       "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                       "SOURCE_ROOT": dirs["cache"], "SECRET_KEY": "test", "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def principal(monkeypatch):
    monkeypatch.setattr(permissions, "runtime_principal", lambda *a, **k: P)


def _csrf(client):
    client.get("/setup/permissions")
    with client.session_transaction() as s:
        return s["_csrf"]


def _steps(*statuses):
    ids = ["R1", "R2", "R3", "R4", "R5"]
    return [permissions.Step(ids[i], "attach", f"Summary {ids[i]}", ["iam", "attach-user-policy"],
                             status=st, error="boom" if st == "failed" else "")
            for i, st in enumerate(statuses)]


def test_get_unprovisioned_points_at_destination(dirs, template_path):
    app = create_app({"CONFIG_DIR": dirs["config"], "CACHE_DIR": dirs["cache"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SECRET_KEY": "test", "TESTING": True})
    body = app.test_client().get("/setup/permissions").get_data(as_text=True)
    assert "Set up the destination first" in body


def test_get_shows_unchecked_status_and_both_paths(client):
    body = client.get("/setup/permissions").get_data(as_text=True)
    assert 'data-perm-state="unchecked"' in body
    assert "Update permissions" in body and "Preview only" in body
    assert "Run the commands yourself" in body


def test_update_requires_csrf(client):
    assert client.post("/setup/permissions/update", data=CREDS).status_code == 400


def test_update_success_shows_steps_and_never_echoes_creds(client, monkeypatch):
    seen = {}

    def fake(principal, **kw):
        seen.update(kw, principal=principal)
        return permissions.Outcome(ok=True, applied=True, steps=_steps("done", "done", "done", "done", "done"))
    monkeypatch.setattr(permissions, "converge", fake)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert seen["principal"] == P and seen["apply_changes"] is True and seen["mode"] == "update"
    assert seen["admin"] == permissions.AdminCreds("ADMINKEYVALUE", "ADMINSECRETVALUE", None)
    assert seen["bucket"] == f"unraid-backup-{ACCOUNT}" and seen["region"] == "us-east-1"
    assert "Summary R5" in body and "AWS permissions updated." in body
    assert "delete that access key in AWS now" in body
    assert "ADMINKEYVALUE" not in body and "ADMINSECRETVALUE" not in body


def test_preview_changes_nothing(client, monkeypatch):
    seen = {}

    def fake(principal, **kw):
        seen.update(kw)
        return permissions.Outcome(ok=True, applied=False, steps=_steps("pending", "pending"))
    monkeypatch.setattr(permissions, "converge", fake)
    body = client.post("/setup/permissions/preview",
                       data={"csrf": _csrf(client), **CREDS}).get_data(as_text=True)
    assert seen["apply_changes"] is False and seen["mode"] == "check"
    assert "Preview — nothing was changed" in body and "Would do" in body


def test_already_current_redirects_with_a_flash(client, monkeypatch):
    monkeypatch.setattr(permissions, "converge",
                        lambda p, **kw: permissions.Outcome(ok=True, applied=False, steps=[]))
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS},
                    follow_redirects=True)
    assert "already in place" in r.get_data(as_text=True)


def test_failed_step_explains_rerun(client, monkeypatch):
    monkeypatch.setattr(permissions, "converge",
                        lambda p, **kw: permissions.Outcome(ok=False, applied=True,
                                                            steps=_steps("done", "failed", "not-run")))
    body = client.post("/setup/permissions/update",
                       data={"csrf": _csrf(client), **CREDS}).get_data(as_text=True)
    assert "Stopped at the first failure" in body and "Not run" in body


def test_admin_token_error_is_explained(client, monkeypatch):
    def boom(p, **kw):
        raise provision.AdminCapabilityError("token", "InvalidClientTokenId ADMINSECRETVALUE")
    monkeypatch.setattr(permissions, "converge", boom)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and "can't manage AWS IAM" in body
    assert "ADMINSECRETVALUE" not in body


def test_account_mismatch_is_explained(client, monkeypatch):
    def boom(p, **kw):
        raise permissions.PermissionsError("account_mismatch", "The admin credentials are for AWS account 999999999999, but the backup key belongs to account 123456789012.")
    monkeypatch.setattr(permissions, "converge", boom)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    assert r.status_code == 400 and "belongs to account" in r.get_data(as_text=True)


def test_runtime_key_problem_is_explained(client, monkeypatch):
    def boom(*a, **k):
        raise permissions.PermissionsError("runtime_key", "InvalidClientTokenId")
    monkeypatch.setattr(permissions, "runtime_principal", boom)
    r = client.post("/setup/permissions/update", data={"csrf": _csrf(client), **CREDS})
    assert r.status_code == 400 and "saved backup key" in r.get_data(as_text=True)
```

In `tests/gui/test_vocabulary.py`, add to `ALL_PAGES` (after `"/setup/about"`):

```python
    "/setup/permissions",             # AWS permissions
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_routes.py tests/gui/test_vocabulary.py`
Expected: FAIL — 404s on `/setup/permissions`.

- [ ] **Step 3: Implement**

`app/gui/permissions_routes.py` (new):

```python
# app/gui/permissions_routes.py — /setup/permissions (spec 2026-09-22 §3): the
# permissions status, the admin-creds Update/Preview path, and (Task 8) the
# commands fallback. Registered on the shared `gui` blueprint; app/gui/__init__.py
# imports this module before the blueprint is registered. No AWS call on GET.
from __future__ import annotations

from flask import abort, current_app, flash, redirect, render_template, request, url_for

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


_ADMIN_MESSAGES = {
    "token": ("These credentials can't manage AWS IAM. If they're temporary (the access key "
              "starts with ASIA — from sts get-session-token, SSO or CloudShell), use an "
              "MFA-authenticated session, an SSO role, or a permanent access key, and check the "
              "session token hasn't expired. Nothing was changed."),
    "permission": ("These credentials reached AWS but aren't allowed to manage IAM roles and "
                   "policies. Use an admin credential. Nothing was changed."),
}


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
        return _page(400, error=_ADMIN_MESSAGES.get(
            e.kind, "Couldn't verify these credentials can manage IAM. Nothing was changed."),
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
    if outcome.ok and not outcome.steps:
        flash("Everything's already in place — AWS permissions are up to date.", "success")
        return redirect(url_for("gui.permissions_page"))
    if outcome.ok and outcome.applied:
        flash("AWS permissions updated.", "success")
    if outcome.applied:
        flash("We never stored your admin key — delete that access key in AWS now.", "warning")
    return _page(outcome=outcome, preview=not apply_changes)


@bp.post("/setup/permissions/update")
def permissions_update():
    return _run_admin(apply_changes=True)


@bp.post("/setup/permissions/preview")
def permissions_preview():
    return _run_admin(apply_changes=False)
```

`app/gui/__init__.py` — directly after `from .routes import bp` (inside the app factory), add:

```python
    from . import permissions_routes  # noqa: F401 — registers /setup/permissions on bp
```

`app/gui/templates/permissions.html` (new):

```html
{% extends "base.html" %}{% block title %}AWS permissions — backup-engine{% endblock %}
{% block body %}
{# AWS permissions (spec 2026-09-22 §3): the stamp vs this build, the admin-creds
   Update / Preview path, and the commands fallback. Nothing here calls AWS on GET. #}
<section class="screen read" aria-label="AWS permissions">
  <p class="eyebrow">Setup · AWS permissions</p>
  <h1>AWS permissions</h1>
  <p class="lead">What backup-engine may do in your AWS account — and a way to bring that up
     to date for this version without touching your bucket, your data or your backup key.</p>

  <div class="sig sig-{{ 'success' if perm.state == 'current' else 'warning' }}" style="margin-top:var(--sp-5)">
    <span class="glyph" aria-hidden="true"></span>
    <div class="body" data-perm-state="{{ perm.state }}">
      {% if perm.state == 'current' %}Up to date for this version{% if checked_human %} · checked <span class="stamp">{{ checked_human }}</span>{% endif %}.
      {% elif perm.state == 'behind' %}An update is available. It adds:
        <ul style="margin:var(--sp-2) 0 0">{% for h in perm.missing %}<li>{{ h.adds }}</li>{% endfor %}</ul>
      {% else %}Not checked for this version of backup-engine yet. Checking is read-only —
        nothing changes unless you choose Update.{% endif %}
    </div>
  </div>

  {% if not provisioned %}
  <p class="hint" style="margin-top:var(--sp-5)">Set up the destination first —
     <a class="linklike" href="/setup/destination">Where backups go →</a></p>
  {% else %}

  {% if error %}
  <div class="sig sig-failure" data-flash="failure" style="margin-top:var(--sp-4)">
    <span class="glyph" aria-hidden="true"></span><div class="body">{{ error }}</div>
  </div>
  {% endif %}
  {% if error_detail %}
  <details class="error-detail" open style="margin-top:var(--sp-3)">
    <summary>What AWS reported</summary>
    <div class="errline" style="margin-top:var(--sp-2)">{{ error_detail }}</div>
  </details>
  {% endif %}

  {% if outcome %}
  <p class="slabel" style="margin-top:var(--sp-6)">{% if preview %}Preview — nothing was changed{% else %}What happened{% endif %}</p>
  {% if outcome.remaining %}
  <div class="sig sig-failure" style="margin-top:var(--sp-3)">
    <span class="glyph" aria-hidden="true"></span>
    <div class="body">Every step ran, but a fresh check still found differences. Run the update
      again; if it keeps happening, something else is changing these IAM settings.</div>
  </div>
  {% endif %}
  <table class="classes" style="margin-top:var(--sp-3)">
    <thead><tr><th style="width:1.4rem" aria-label="Status"></th><th style="text-align:left">Step</th><th style="text-align:left">Status</th></tr></thead>
    <tbody>
      {% for s in outcome.steps %}
      {%- set dot = 'dot-ok' if s.status == 'done' else ('dot-danger' if s.status == 'failed' else 'dot-warn') -%}
      <tr data-step="{{ s.id }}" data-status="{{ s.status }}">
        <td style="text-align:left"><span class="dot {{ dot }}" aria-hidden="true"></span></td>
        <td style="text-align:left">{{ s.summary }}
          <details class="tooldetail" style="margin-top:var(--sp-2)">
            <summary>Command{% if s.diff %} and policy changes{% endif %}</summary>
            <div class="cmd" style="margin-top:var(--sp-2)"><pre>{{ s.command() }}</pre></div>
            {% if s.diff %}<div class="cmd" style="margin-top:var(--sp-2)"><pre>{{ s.diff | join('\n') }}</pre></div>{% endif %}
          </details>
          {% if s.error %}<div class="errline" style="margin-top:var(--sp-2)">{{ s.error }}</div>{% endif %}
        </td>
        <td style="text-align:left">{{ {'pending': ('Would do' if preview else 'Pending'), 'done': 'Done', 'failed': 'Failed', 'not-run': 'Not run'}[s.status] }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% if outcome.steps | selectattr('status', 'equalto', 'failed') | list %}
  <p class="hint" style="margin-top:var(--sp-3)">Stopped at the first failure. Every step is safe
     to repeat — fix the cause and run the update again.</p>
  {% endif %}
  {% endif %}

  <p class="slabel" style="margin-top:var(--sp-6)">Use admin credentials</p>
  <div class="sig sig-warning" style="margin-top:var(--sp-3)">
    <span class="glyph" aria-hidden="true"></span>
    <div class="body">These admin credentials are <strong>transient</strong>: used for this one
      request, never written to disk or logs, and never shown again. Use a short-lived key and
      delete it afterwards.</div>
  </div>
  <form method="post" action="{{ url_for('gui.permissions_update') }}" class="form" style="margin-top:var(--sp-4)">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <div class="field"><label for="ak">Admin access key <code class="hint">ADMIN_ACCESS_KEY_ID</code></label>
      <input id="ak" name="ADMIN_ACCESS_KEY_ID" autocomplete="off" required></div>
    <div class="field"><label for="sk">Admin secret key <code class="hint">ADMIN_SECRET_ACCESS_KEY</code></label>
      <input id="sk" name="ADMIN_SECRET_ACCESS_KEY" type="password" autocomplete="off" required></div>
    <div class="field"><label for="st">Session token <code class="hint">ADMIN_SESSION_TOKEN</code> <small>(optional)</small></label>
      <input id="st" name="ADMIN_SESSION_TOKEN" type="password" autocomplete="off"></div>
    <div class="formfoot" data-progress="Working…">
      <button type="submit" class="btn btn-primary">Update permissions</button>
      <button type="submit" class="btn" formaction="{{ url_for('gui.permissions_preview') }}">Preview only</button>
    </div>
  </form>

  <p class="slabel" id="commands" style="margin-top:var(--sp-6)">Run the commands yourself</p>
  <p class="hint">Rather not paste admin credentials here? Generate a script, run it in AWS
     CloudShell (or any shell signed in as an admin), then Verify.</p>
  {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_routes.py tests/gui/test_vocabulary.py` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/permissions_routes.py app/gui/templates/permissions.html app/gui/__init__.py tests/gui/test_permissions_routes.py tests/gui/test_vocabulary.py
git commit -m "feat(permissions): /setup/permissions page with admin-creds Update + Preview"
```

---

### Task 8: The commands path on the page — Show the commands + Verify

**Files:**
- Modify: `app/gui/permissions_routes.py`
- Modify: `app/gui/templates/permissions.html` (the `#commands` section)
- Test: `tests/gui/test_permissions_routes.py` (append)

**Interfaces:**
- Consumes: `permissions.script`, `permissions.verify`, `permissions.write_stamp`, `permissions.record` (Tasks 4–5); Task 7's `_page`, `_bucket_region`, `_principal_for`, `_perm_error_message`.
- Produces: endpoints `gui.permissions_script` (POST `/setup/permissions/script`), `gui.permissions_verify` (POST `/setup/permissions/verify`).

- [ ] **Step 1: Write the failing tests** — append to `tests/gui/test_permissions_routes.py`:

```python
# --- the commands path -------------------------------------------------------------

def test_show_commands_renders_the_script(client):
    body = client.post("/setup/permissions/script", data={"csrf": _csrf(client)}).get_data(as_text=True)
    assert 'id="perm-script"' in body
    assert "put-user-policy" in body and 'data-copy-target="perm-script"' in body


def test_show_commands_requires_csrf(client):
    assert client.post("/setup/permissions/script").status_code == 400


def test_show_commands_runtime_key_failure(client, monkeypatch):
    def boom(*a, **k):
        raise permissions.PermissionsError("runtime_key", "InvalidClientTokenId")
    monkeypatch.setattr(permissions, "runtime_principal", boom)
    r = client.post("/setup/permissions/script", data={"csrf": _csrf(client)})
    assert r.status_code == 400 and "saved backup key" in r.get_data(as_text=True)


def test_verify_all_good_stamps_and_records(client, dirs, monkeypatch):
    monkeypatch.setattr(permissions, "verify",
                        lambda p, **kw: [permissions.Probe("A", True), permissions.Probe("B", True)])
    r = client.post("/setup/permissions/verify", data={"csrf": _csrf(client)}, follow_redirects=True)
    assert "Verified" in r.get_data(as_text=True)
    env = config_io.read_backup_env(dirs["config"])
    assert env["PERMISSIONS_VERSION"] == str(permissions.required_level())
    assert env["BUCKET_ADMIN_ROLE_ARN"] == permissions.role_arn(ACCOUNT)
    assert "permissions" in Path(dirs["cache"], "state", "_system.runs.jsonl").read_text()


def test_verify_failure_lists_probes_and_does_not_stamp(client, dirs, monkeypatch):
    monkeypatch.setattr(permissions, "verify", lambda p, **kw: [
        permissions.Probe("Can list old versions in the backup bucket", True),
        permissions.Probe("Can assume the role backup-engine-bucket-admin", False,
                          "Steps 3 and 5 of the script set this up — did step 3 run?")])
    body = client.post("/setup/permissions/verify", data={"csrf": _csrf(client)}).get_data(as_text=True)
    assert "✗" in body and "did step 3 run?" in body
    assert "PERMISSIONS_VERSION" not in config_io.read_backup_env(dirs["config"])


def test_commands_mode_explains_it_is_the_last_setup_step(client):
    body = client.get("/setup/permissions?mode=commands").get_data(as_text=True)
    assert "Last step of setup" in body
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_permissions_routes.py`
Expected: FAIL — 404/405 on `/setup/permissions/script` and `/verify`.

- [ ] **Step 3: Implement**

Append to `app/gui/permissions_routes.py`:

```python
def _guard():
    if not security.verify_csrf(request.form.get("csrf", "")):
        abort(400, description="csrf")
    return config_io.is_provisioned(current_app.config["CONFIG_DIR"])


@bp.post("/setup/permissions/script")
def permissions_script():
    if not _guard():
        return redirect(url_for("gui.provision_home"))
    cfg = current_app.config
    bucket, region = _bucket_region(cfg)
    try:
        text = permissions.script(_principal_for(cfg, region), bucket=bucket, region=region)
    except permissions.PermissionsError as e:
        return _page(400, mode="commands", error=_perm_error_message(e),
                     error_detail=e.detail or None)
    return _page(mode="commands", script=text)


@bp.post("/setup/permissions/verify")
def permissions_verify():
    if not _guard():
        return redirect(url_for("gui.provision_home"))
    cfg = current_app.config
    bucket, region = _bucket_region(cfg)
    key, secret = _runtime_creds(cfg)
    try:
        principal = permissions.runtime_principal(region, key, secret)
    except permissions.PermissionsError as e:
        return _page(400, mode="commands", error=_perm_error_message(e),
                     error_detail=e.detail or None)
    probes = permissions.verify(principal, bucket=bucket, region=region, key=key, secret=secret)
    if all(p.ok for p in probes):
        permissions.write_stamp(cfg["CONFIG_DIR"], cfg["TEMPLATE_PATH"], principal)
        permissions.record(cfg["CACHE_DIR"], mode="verify", lines=[p.name for p in probes])
        flash("Verified — AWS permissions are up to date.", "success")
        return redirect(url_for("gui.permissions_page"))
    return _page(mode="commands", probes=probes)
```

In `app/gui/templates/permissions.html`, replace the two lines after `<p class="slabel" id="commands" ...>` (the `<p class="hint">Rather not paste…</p>`) with:

```html
  {% if mode == 'commands' %}
  <div class="sig sig-note" style="margin-top:var(--sp-3)">
    <span class="glyph" aria-hidden="true"></span>
    <div class="body">Last step of setup: give the backup user everything this version needs.
      Generate the commands, run them, then Verify.</div>
  </div>
  {% endif %}
  <p class="hint" style="margin-top:var(--sp-3)">Rather not paste admin credentials here? Generate
     a script, run it in AWS CloudShell (or any shell signed in as an admin), then Verify. It only
     creates what's missing and is safe to run more than once. Set up with <code>setup.sh</code>?
     Re-running it from the same checkout does the same job.</p>
  {% if script %}
  <div class="cmd" style="margin-top:var(--sp-3)">
    <pre id="perm-script">{{ script }}</pre>
    <button type="button" class="btn btn-sm copy" data-copy-target="perm-script">Copy</button>
  </div>
  {% else %}
  <form method="post" action="{{ url_for('gui.permissions_script') }}#commands" style="margin-top:var(--sp-3)">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit" class="btn">Show the commands</button>
  </form>
  {% endif %}
  {% if probes %}
  <ul style="margin:var(--sp-4) 0 0;list-style:none;padding:0">
    {% for p in probes %}
    <li data-probe-ok="{{ 1 if p.ok else 0 }}" style="margin-top:var(--sp-2)">{{ '✓' if p.ok else '✗' }} {{ p.name }}{% if not p.ok and p.hint %} — {{ p.hint }}{% endif %}
      {% if p.detail %}<div class="errline" style="margin-top:var(--sp-1)">{{ p.detail }}</div>{% endif %}</li>
    {% endfor %}
  </ul>
  {% endif %}
  <form method="post" action="{{ url_for('gui.permissions_verify') }}#commands" style="margin-top:var(--sp-4)">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button type="submit" class="btn btn-primary">Verify</button>
  </form>
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_permissions_routes.py tests/gui/test_vocabulary.py tests/gui/test_static_app_js.py` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/permissions_routes.py app/gui/templates/permissions.html tests/gui/test_permissions_routes.py
git commit -m "feat(permissions): commands path on the page — show the script, Verify with the runtime key"
```

---

### Task 9: Gate the "own bucket" toggle on the permissions level

**Files:**
- Modify: `app/gui/routes.py` (`_render_job_form`, `job_save`)
- Modify: `app/gui/templates/job_form.html` (section 5, create branch)
- Modify: `tests/gui/test_job_form_routes.py` (fixture gains the stamp; new tests)

**Interfaces:**
- Consumes: `permissions.feature_available(config_dir, "dedicated-buckets")`, `config_io.bucket_admin_role_arn(config_dir)`.
- Produces: template var `dedicated_ok: bool`; the server-side guided error string `DEDICATED_NEEDS_UPDATE` (module constant in `routes.py`).

- [ ] **Step 1: Update the fixture + write the failing tests**

In `tests/gui/test_job_form_routes.py`, add `permissions` to the `from app.gui import ...` line, and in the `app` fixture's `backup.env` text append the stamp line:

```python
        "RUNTIME_EXTRA_BUCKETS_POLICY_ARN=arn:aws:iam::111111111111:policy/backup-engine-runtime-extra-buckets\n"
        f"PERMISSIONS_VERSION={permissions.required_level()}\n")
```

Append:

```python
# --- dedicated buckets need the permissions update (spec 2026-09-22 §3) -----

@pytest.fixture
def unstamped_client(tmp_path, source_root, template_path):
    cfg = tmp_path / "config2"; cfg.mkdir()
    config_io.write_secrets(str(cfg), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    (cfg / "backup.env").write_text("S3_BUCKET=bw-backups\nAWS_REGION=us-east-1\n")
    app = create_app({"CONFIG_DIR": str(cfg), "CACHE_DIR": str(tmp_path / "cache2"),
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": template_path,
                      "SOURCE_ROOT": str(source_root), "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


def test_toggle_disabled_until_permissions_are_updated(unstamped_client):
    body = unstamped_client.get("/jobs/new").get_data(as_text=True)
    assert 'name="dedicated"' not in body
    assert "Needs a one-time AWS permissions update" in body
    assert 'href="/setup/permissions"' in body


def test_toggle_enabled_when_current(client):
    assert 'name="dedicated"' in client.get("/jobs/new").get_data(as_text=True)


def test_dedicated_post_without_permissions_is_refused_without_aws(unstamped_client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not reach AWS")
    monkeypatch.setattr(provision, "assume_role", boom)
    unstamped_client.get("/jobs/new")
    with unstamped_client.session_transaction() as s:
        token = s["_csrf"]
    r = unstamped_client.post("/jobs", data={"csrf": token, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups-photos", "bucket_versioned": "1"})
    assert "one-time AWS permissions update" in r.get_data(as_text=True)
```

(Same fields as `test_dedicated_save_creates_bucket_then_redirects`, so the only thing that can stop this save is the new gate.)

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_job_form_routes.py`
Expected: the three new tests FAIL (toggle still rendered; POST reaches `assume_role`).

- [ ] **Step 3: Implement**

`app/gui/routes.py` — near `_RETENTION_DEFAULT_BY_TYPE` add:

```python
DEDICATED_NEEDS_UPDATE = ("Dedicated buckets need a one-time AWS permissions update first — "
                          "open Setup → AWS permissions.")


def _dedicated_ok(cfg) -> bool:
    """Dedicated buckets are usable once the permissions stamp reaches the level that
    introduced them AND the bucket-admin role is known (spec 2026-09-22 §3)."""
    return (permissions.feature_available(cfg["CONFIG_DIR"], "dedicated-buckets")
            and bool(config_io.bucket_admin_role_arn(cfg["CONFIG_DIR"])))
```

In `_render_job_form`'s `render_template(...)` call add `dedicated_ok=_dedicated_ok(cfg),`.

In `job_save`, at the top of the `elif f.get("dedicated"):` branch (before `bucket = f.get("bucket", "").strip()`):

```python
        if not _dedicated_ok(cfg):
            return _render_job_form(cfg, job=existing, fv=fv, errors={"form": DEDICATED_NEEDS_UPDATE})
```

`app/gui/templates/job_form.html` — in section 5's create branch (`{% else %}` after the edit branch), wrap the existing toggle + hint + `#bucket-wrap` block:

```html
    {% else %}
    {% if dedicated_ok %}
    <label class="radio" for="dedicated"><input type="checkbox" id="dedicated" name="dedicated" value="1" {{ 'checked' if fv.dedicated == '1' }}>
      ... existing markup unchanged, through the closing </div> of #bucket-wrap ...
    {% else %}
    <label class="radio" for="dedicated"><input type="checkbox" id="dedicated" disabled>
      <span>Give this job its own bucket</span></label>
    <p class="sm faint measure" style="margin-top:4px">Needs a one-time AWS permissions update —
      <a class="linklike" href="/setup/permissions">update permissions →</a>. Until then every job
      shares <span class="mono">s3://{{ bucket }}</span>.</p>
    {% endif %}
    {% endif %}
```

(The disabled checkbox has no `name`, so it never submits.)

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q tests/gui/test_job_form_routes.py tests/gui/test_vocabulary.py tests/gui/test_static_app_js.py` → PASS. Then `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/routes.py app/gui/templates/job_form.html tests/gui/test_job_form_routes.py
git commit -m "feat(permissions): dedicated-bucket toggle waits for the permissions update (guided, no raw AWS error)"
```

---

### Task 10: Automated setup ends by converging; `setup.sh` prints the ARNs + level

**Files:**
- Modify: `app/gui/routes.py` (`provision_automated_run` + helper `_finish_setup_permissions`)
- Modify: `setup.sh`
- Modify: `app/gui/templates/provision_scripted.html`
- Test: `tests/gui/test_provision_routes.py` (autouse stub + new tests), `tests/bats/setup_sh.bats`

**Interfaces:**
- Consumes: `permissions.parse_principal`, `permissions.converge`, `permissions.AdminCreds`; `run_tofu_apply(...)["runtime_user_arn"]` (Task 1); tofu outputs `permissions_level`, `bucket_admin_role_arn`, `runtime_extra_buckets_policy_arn`.
- Produces: `routes._finish_setup_permissions(cfg, result, admin) -> str | None` (a warning message, or None); `SETUP_PERMISSIONS_WARNING` constant.

- [ ] **Step 1: Write the failing tests**

In `tests/gui/test_provision_routes.py`, add an autouse fixture under `captured_probe` so no existing automated test reaches AWS:

```python
@pytest.fixture(autouse=True)
def converge_calls(monkeypatch):
    """Automated setup now ends with permissions.converge (spec 2026-09-22 §4).
    Stub it for every test here; tests that care inspect the recorded calls."""
    from app.gui import permissions
    calls = []

    def fake(principal, **kw):
        calls.append((principal, kw))
        return permissions.Outcome(ok=True, applied=False, steps=[])
    monkeypatch.setattr(permissions, "converge", fake)
    return calls
```

Append:

```python
_TOFU_OK = {"AWS_ACCESS_KEY_ID": "AKIARUN", "AWS_SECRET_ACCESS_KEY": "runsek",
            "bucket": "acme", "region": "us-east-1",
            "bucket_admin_role_arn": "arn:aws:iam::123456789012:role/backup-engine-bucket-admin",
            "runtime_extra_buckets_policy_arn": "arn:aws:iam::123456789012:policy/backup-engine-runtime-extra-buckets",
            "runtime_user_arn": "arn:aws:iam::123456789012:user/backup-engine-runtime"}


def _automated(client, monkeypatch, result):
    from app.gui import provision
    monkeypatch.setattr(provision, "verify_admin_can_provision", lambda *a, **k: None)
    monkeypatch.setattr(provision, "run_tofu_apply", lambda *a, **k: dict(result))
    token = _csrf(client, "/setup/destination/automated")
    return client.post("/setup/destination/automated",
                       data={"csrf": token, "bucket": "acme", "region": "us-east-1",
                             "ADMIN_ACCESS_KEY_ID": "ADMINK", "ADMIN_SECRET_ACCESS_KEY": "ADMINS"},
                       follow_redirects=True)


def test_automated_setup_finishes_by_converging_with_the_same_admin_creds(client, monkeypatch, converge_calls):
    from app.gui import permissions
    r = _automated(client, monkeypatch, _TOFU_OK)
    assert r.status_code == 200
    (principal, kw), = converge_calls
    assert principal.user == "backup-engine-runtime" and principal.account == "123456789012"
    assert kw["admin"] == permissions.AdminCreds("ADMINK", "ADMINS", None)
    assert kw["bucket"] == "acme" and kw["apply_changes"] is True and kw["mode"] == "setup"
    assert b"couldn't confirm its AWS permissions" not in r.data


def test_automated_setup_survives_a_converge_failure(client, dirs, monkeypatch):
    from app.gui import permissions

    def boom(principal, **kw):
        raise RuntimeError("IAM hiccup ADMINS")
    monkeypatch.setattr(permissions, "converge", boom)
    r = _automated(client, monkeypatch, _TOFU_OK)
    assert r.status_code == 200
    assert b"Destination set: acme in us-east-1" in r.data
    assert b"couldn't confirm its AWS permissions" in r.data
    assert b"ADMINS" not in r.data
    assert "AWS_ACCESS_KEY_ID=AKIARUN" in Path(dirs["config"], "secrets.env").read_text()


def test_automated_setup_warns_when_tofu_gave_no_user_arn(client, monkeypatch, converge_calls):
    r = _automated(client, monkeypatch, {k: v for k, v in _TOFU_OK.items() if k != "runtime_user_arn"})
    assert r.status_code == 200 and converge_calls == []
    assert b"couldn't confirm its AWS permissions" in r.data


def test_automated_setup_keeps_the_stamp_converge_wrote(client, dirs, monkeypatch):
    from app.gui import permissions

    def stamping(principal, **kw):
        permissions.write_stamp(kw["config_dir"], kw["template_path"], principal)
        return permissions.Outcome(ok=True, applied=False, steps=[])
    monkeypatch.setattr(permissions, "converge", stamping)
    _automated(client, monkeypatch, _TOFU_OK)
    be = Path(dirs["config"], "backup.env").read_text()
    assert f"PERMISSIONS_VERSION={permissions.required_level()}" in be
    assert "S3_BUCKET=acme" in be
```

`tests/bats/setup_sh.bats` — append:

```bash
@test "setup.sh prints the multi-bucket ARNs and the permissions level" {
  stub="$(mktemp -d)"
  cat > "$stub/tofu" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "output" ] && [ "$2" = "-raw" ]; then
  case "$3" in
    bucket_admin_role_arn) echo "arn:aws:iam::123456789012:role/backup-engine-bucket-admin" ;;
    runtime_extra_buckets_policy_arn) echo "arn:aws:iam::123456789012:policy/backup-engine-runtime-extra-buckets" ;;
    permissions_level) echo "3" ;;
    *) echo "val-$3" ;;
  esac
fi
exit 0
EOF
  chmod +x "$stub/tofu"
  export PATH="$stub:$PATH" AWS_PROFILE=stub
  run bash "$BATS_TEST_DIRNAME/../../setup.sh" my-bucket us-east-1
  rm -rf "$stub"
  [ "$status" -eq 0 ]
  [[ "$output" == *"BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::123456789012:role/backup-engine-bucket-admin"* ]]
  [[ "$output" == *"RUNTIME_EXTRA_BUCKETS_POLICY_ARN=arn:aws:iam::123456789012:policy/backup-engine-runtime-extra-buckets"* ]]
  [[ "$output" == *"PERMISSIONS_VERSION=3"* ]]
}
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_provision_routes.py && bats tests/bats/setup_sh.bats`
Expected: the four new pytest tests and the new bats test FAIL.

- [ ] **Step 3: Implement**

`app/gui/routes.py` — above `provision_automated_run`:

```python
SETUP_PERMISSIONS_WARNING = ("Destination is set, but backup-engine couldn't confirm its AWS "
                             "permissions. Open Setup → AWS permissions to finish — nothing is "
                             "broken meanwhile.")


def _finish_setup_permissions(cfg, result, admin) -> str | None:
    """Automated setup's last step (spec 2026-09-22 §4): converge + stamp with the SAME
    in-frame admin creds. It never fails setup -- tofu already created the bucket and
    key and its state is gone -- so any problem becomes a warning for the success page."""
    try:
        principal = permissions.parse_principal(result.get("runtime_user_arn", ""))
        outcome = permissions.converge(
            principal, bucket=result["bucket"], region=result["region"], admin=admin,
            config_dir=cfg["CONFIG_DIR"], template_path=cfg["TEMPLATE_PATH"],
            cache_dir=cfg["CACHE_DIR"], apply_changes=True, mode="setup")
    except Exception:  # noqa: BLE001 -- setup already succeeded; never lose it here
        return SETUP_PERMISSIONS_WARNING
    return None if outcome.ok else SETUP_PERMISSIONS_WARNING
```

In `provision_automated_run`: add `perm_warning = None` right before `try:`; directly after `result = provision.run_tofu_apply(...)` add:

```python
        perm_warning = _finish_setup_permissions(
            cfg, result, permissions.AdminCreds(admin_key, admin_secret, session_token))
```

and after the `flash("We never stored your admin key — ...", "warning")` line add:

```python
    if perm_warning:
        flash(perm_warning, "warning")
```

`setup.sh` — after the `S3_BUCKET=` echo line, add:

```bash
  echo "BUCKET_ADMIN_ROLE_ARN=$(tofu output -raw bucket_admin_role_arn)"
  echo "RUNTIME_EXTRA_BUCKETS_POLICY_ARN=$(tofu output -raw runtime_extra_buckets_policy_arn)"
  echo "PERMISSIONS_VERSION=$(tofu output -raw permissions_level)"
```

`app/gui/templates/provision_scripted.html` — replace the `<p class="hint" ...>It wraps <code>tofu apply</code>…</p>` paragraph with:

```html
  <p class="hint" style="margin-top:var(--sp-3)">It wraps <code>tofu apply</code> — creating
     the bucket, a least-privilege IAM user and the bucket-admin role — and prints the runtime
     key/secret plus the <code>backup.env</code> lines (region, bucket, the two role/policy ARNs
     and <code>PERMISSIONS_VERSION</code>). Keep the checkout: re-running it later updates the
     AWS permissions in place.</p>
```

and the final paragraph's last sentence becomes: `Test &amp; Validate finishes on AWS permissions, where Verify confirms the rest. Then set the recovery passphrase and create your first job.`

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q && bats tests/bats/ && shellcheck setup.sh scripts/*.sh scripts/lib/*.sh` → all pass, shellcheck silent.

- [ ] **Step 5: Commit**

```bash
git add app/gui/routes.py setup.sh app/gui/templates/provision_scripted.html tests/gui/test_provision_routes.py tests/bats/setup_sh.bats
git commit -m "feat(setup): automated setup ends by converging + stamping (soft-fail); setup.sh prints ARNs + level"
```

---

### Task 11: Guided-manual finishes on AWS permissions

**Files:**
- Modify: `app/gui/provision.py` (`render_console_steps`)
- Modify: `app/gui/templates/provision_manual.html` (policy hint copy)
- Modify: `app/gui/routes.py` (`provision_validate` redirect + flash)
- Test: `tests/gui/test_provision.py` (append), `tests/gui/test_provision_routes.py` (update two validate tests)

**Interfaces:**
- Consumes: endpoint `gui.permissions_page` (Task 7) with `mode=commands`.
- Produces: guided CLI uses `put-user-policy` (inline) instead of `create-policy` + `attach-user-policy`.

- [ ] **Step 1: Write/adjust the failing tests**

Append to `tests/gui/test_provision.py`:

```python
def test_guided_cli_uses_an_inline_user_policy():
    # Spec 2026-09-22 §4: the full policy later lands on the same inline name via
    # put-user-policy, so guided setup no longer creates a managed policy.
    cli = "\n".join(provision.render_console_steps("acme", "us-east-1")["cli"])
    assert ("aws iam put-user-policy --user-name backup-engine-runtime "
            "--policy-name backup-engine-runtime-object-only --policy-document file://iam-policy.json") in cli
    assert "create-policy" not in cli and "attach-user-policy" not in cli
    assert cli.index("create-user") < cli.index("put-user-policy") < cli.index("create-access-key")


def test_guided_console_steps_create_an_inline_policy():
    steps = " ".join(provision.render_console_steps("acme", "us-east-1")["steps"]).lower()
    assert "create inline policy" in steps
    assert "backup-engine-runtime-object-only" in steps
```

In `tests/gui/test_provision_routes.py`, change `test_validate_success_writes_secrets_and_lands_on_setup` to assert the new landing (rename it `..._lands_on_permissions`):

```python
    assert r.status_code in (302, 303)
    assert "/setup/permissions?mode=commands" in r.headers["Location"]
```

and in `test_validate_success_flashes_destination_set` replace the last assertion with:

```python
    assert b"One more step: AWS permissions" in r.data
```

- [ ] **Step 2: Run to see them fail**

Run: `python3 -m pytest -q tests/gui/test_provision.py tests/gui/test_provision_routes.py`
Expected: the new/changed tests FAIL.

- [ ] **Step 3: Implement**

`app/gui/provision.py` `render_console_steps` — the IAM part of `cli` becomes:

```python
        "aws iam create-user --user-name backup-engine-runtime",
        "aws iam put-user-policy --user-name backup-engine-runtime "
        "--policy-name backup-engine-runtime-object-only --policy-document file://iam-policy.json",
        "aws iam create-access-key --user-name backup-engine-runtime",
```

(remove the `create-policy` and `attach-user-policy` entries). In `steps`, replace the two IAM entries (`"In IAM, save the policy above…"` and `"Create an IAM user, attach that policy…"`) and the final `"Paste the runtime key/secret below…"` entry with:

```python
        "In IAM, create a user named backup-engine-runtime (no console access).",
        "Open that user → Permissions → Add permissions → Create inline policy → JSON, paste the "
        "policy above, and name it backup-engine-runtime-object-only.",
        "On the same user → Security credentials → Create access key — that pair is your runtime key/secret.",
        "Paste the runtime key/secret below and click Test & Validate. Setup then finishes on AWS "
        "permissions, which adds what dedicated per-job buckets need.",
```

`app/gui/templates/provision_manual.html` — in the step-1 hint, replace `(IAM → Policies → Create policy → JSON tab)` with `(IAM → your backup user → Add permissions → Create inline policy → JSON tab)`.

`app/gui/routes.py` `provision_validate` — replace the final `flash(...)` + `return redirect(url_for("gui.setup_page"))` with:

```python
    flash(f"Destination set: {bucket} in {region}. One more step: AWS permissions — then the "
          f"recovery passphrase and the first job.", "success")
    return redirect(url_for("gui.permissions_page", mode="commands", _anchor="commands"))
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gui/provision.py app/gui/templates/provision_manual.html app/gui/routes.py tests/gui/test_provision.py tests/gui/test_provision_routes.py
git commit -m "feat(setup): guided-manual uses an inline policy and finishes on AWS permissions"
```

---

### Task 12 (controller): Whole-branch checks, backlog, deploy + live dogfood

Run by the controller, not a subagent. Deploying and touching the real AWS account need the owner present.

- [ ] **Step 1:** Full verification: `python3 -m pytest -q`, `bats tests/bats/`, `shellcheck setup.sh scripts/*.sh scripts/lib/*.sh`, `(cd opentofu && tofu fmt -check)`. Record the counts.
- [ ] **Step 2:** Whole-branch review (superpowers:requesting-code-review) against the spec; fix wave for Critical/Important findings.
- [ ] **Step 3:** `docs/superpowers/BACKLOG.md`: mark data explorer / resilience / multi-bucket headings with their true shipped status; add this feature's parked minors; `git add -f docs/superpowers/BACKLOG.md` + commit.
- [ ] **Step 4:** With the owner: deploy per the Unraid deploy procedure; open `/setup/permissions`; **Preview** with the `opentofu-admin` credentials — expected plan R1 create-policy, R2 attach, R3 create-role, R4 put-role-policy, R5 put-user-policy; then **Update**; confirm the Board warning clears and the Setup row is OK; save a dedicated-bucket job and tear it down afterwards.

---

## Addendum — security fix + review fix wave (after the final review)

Spec: the "Addendum (2026-09-22, after the final review) — prefix-only dedicated buckets" section at the end of the spec is binding for Tasks 13–15. Global Constraints above still apply. Task 12 (controller) runs after these.

### Task 13: Prefix-only dedicated buckets — narrow the role, drop the extra-buckets machinery (engine side)

**Files:**
- Modify: `provisioning/bucket-admin-policy.json.tmpl` (full rewrite below)
- Delete: `provisioning/extra-buckets-policy.json.tmpl`
- Modify: `provisioning/permissions.json` (recompute `templates_sha256`; level stays 3)
- Modify: `opentofu/main.tf` (delete `aws_iam_policy.runtime_extra_buckets` + `aws_iam_user_policy_attachment.runtime_extra_buckets` and their comment block; the role-policy `templatefile` passes only `bucket`), `opentofu/outputs.tf` (delete `output "runtime_extra_buckets_policy_arn"`)
- Modify: `app/gui/provision.py` (`render_bucket_admin_policy(bucket, tmpl_path=...)` — back to one required arg; `run_tofu_apply` no longer returns `runtime_extra_buckets_policy_arn`)
- Modify: `app/gui/permissions.py` (required set R1–R3; see below)
- Test: `tests/engine/test_iam_policy.py`, `tests/gui/test_provision.py`, `tests/gui/test_permissions_plan.py`, `tests/gui/test_permissions_engine.py`, `tests/gui/test_permissions_fallback.py`, `tests/gui/test_permissions_levels.py` (only if it needs the new sha message), any other test the suite shows referencing the removed names

**Interfaces:**
- Produces: `provision.render_bucket_admin_policy(bucket: str, tmpl_path=...) -> str`; in `permissions`: names `RUNTIME_POLICY_NAME`, `ROLE_NAME`, `ROLE_POLICY_NAME` (EXTRA_* constants, `extra_policy_arn`, `EXTRA_POLICY_TEMPLATE` removed); `TOFU_RESOURCES = {"aws_iam_role.bucket_admin": "R1", "aws_iam_role_policy.bucket_admin": "R2", "aws_iam_user_policy.runtime": "R3"}`; `TOFU_UNMANAGED` unchanged; `required_docs(principal, bucket)` keys `trust`, `role`, `runtime`; `Live` without `extra_exists` / `extra_attached`; `plan()` emits ids R1 (create-role / update-trust), R2 (put-role-policy), R3 (put-user-policy / new-version); `write_stamp` writes `BUCKET_ADMIN_ROLE_ARN`, `PERMISSIONS_VERSION`, `PERMISSIONS_CHECKED_AT` only; `script()` has 3 numbered steps; `verify()` returns exactly 3 probes.

`provisioning/bucket-admin-policy.json.tmpl` — full new content:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "CreateAndConfig", "Effect": "Allow",
      "Action": ["s3:CreateBucket","s3:PutBucketVersioning","s3:PutBucketPublicAccessBlock",
                 "s3:PutBucketOwnershipControls","s3:PutEncryptionConfiguration",
                 "s3:PutLifecycleConfiguration","s3:PutBucketTagging",
                 "s3:GetBucketLocation","s3:GetBucketVersioning"],
      "Resource": "arn:aws:s3:::${bucket}-*" },
    { "Sid": "TeardownList", "Effect": "Allow",
      "Action": ["s3:ListAllMyBuckets"],
      "Resource": "*" },
    { "Sid": "Teardown", "Effect": "Allow",
      "Action": ["s3:GetBucketTagging","s3:ListBucketVersions","s3:DeleteBucket"],
      "Resource": "arn:aws:s3:::${bucket}-*" },
    { "Sid": "TeardownObjects", "Effect": "Allow",
      "Action": ["s3:DeleteObject","s3:DeleteObjectVersion"],
      "Resource": "arn:aws:s3:::${bucket}-*/*" }
  ]
}
```

`permissions.py` changes (keep everything else):
- `plan()`: drop the R1/R2 extra-policy branches; renumber the role step to `R1` (actions `create-role` / `update-trust`), the role policy to `R2` (`put-role-policy`), the runtime policy to `R3` (`put-user-policy` / `new-version`). Order stays role → role policy → runtime policy.
- `discover()`: drop the extra-policy `get-policy` read and `extra_attached`; attached managed policies are still listed (for the legacy `RUNTIME_POLICY_NAME` managed shape).
- `required_docs()`: drop `"extra"`; `"role"` = `json.loads(provision.render_bucket_admin_policy(bucket))`.
- `write_stamp()`: drop `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`.
- `script()`: drop the extra-buckets heredoc and steps 1–2; steps become `# 1. The bucket-admin role, which only the backup user may assume` (get-role || create-role, then update-assume-role-policy), `# 2. The bucket-admin role's policy` (put-role-policy), `# 3. The backup user's own policy` (put-user-policy). Keep the legacy managed-policy detach comment, and add another optional comment for installs that have the obsolete extra-buckets policy attached:
  `# Older installs only: backup-engine-runtime-extra-buckets is no longer used; you may detach it (optional):` / `#   aws iam detach-user-policy --user-name <user> --policy-arn arn:aws:iam::<acct>:policy/backup-engine-runtime-extra-buckets`
  (build the ARN from the principal's account; it's a comment line only).
- `verify()`/`_probe_once()`: three probes —
  1. `"Can list old versions in the backup bucket"` (unchanged call), failing hint `"Set by step 3 of the script — did it run?"`;
  2. `f"Can assume the role {ROLE_NAME}"`, failing hint `"Steps 1 and 3 of the script set this up — did step 1 run?"` (if it fails, probe 3 is `False` with hint `"Needs the role first (step 1)."`);
  3. `f"The role {ROLE_NAME} has its policy"` — with the assumed creds run `["s3api", "list-buckets", "--output", "json"]`; ok iff returncode 0; failing hint `"Set by step 2 of the script — did it run?"`; detail scrubbed of the assumed key/secret/token.

Tests to update (behaviour, not just renames):
- `tests/engine/test_iam_policy.py`: `render_bucket_admin_policy("unraid-backup-123")` (one arg). Replace the PolicyGrant tests and `test_bucket_admin_policy_config_actions_are_not_bucket_prefix_scoped` with: every statement except `TeardownList` has a Resource starting `arn:aws:s3:::unraid-backup-123-` (and objects `…-*/*`); `TeardownList` is exactly `["s3:ListAllMyBuckets"]` on `"*"`; the doc contains no `iam:` action at all; the base bucket ARN `arn:aws:s3:::unraid-backup-123"` (exact, no suffix) appears nowhere; `main.tf` has no `runtime_extra_buckets` and no `extra_buckets_policy_arn`; `outputs.tf` has no `runtime_extra_buckets_policy_arn`; keep `test_tofu_outputs_the_runtime_user_arn`.
- `tests/gui/test_provision.py`: drop `runtime_extra_buckets_policy_arn` expectations from `run_tofu_apply` tests.
- `tests/gui/test_permissions_plan.py`: owner's level-2 box plans `[("R1","create-role"),("R2","put-role-policy"),("R3","put-user-policy")]`; drift guard expects `sorted(values) == ["R1","R2","R3"]`; drop the extra-policy tests (unattached / never-planned); keep trust drift (`R1 update-trust`), legacy managed (`R3 new-version`), level-1 (`R3` `~ ListBucketScoped`); required role doc has no `PolicyGrant` and its CreateAndConfig resource is `arn:aws:s3:::<BUCKET>-*`.
- `tests/gui/test_permissions_engine.py`: FakeIAM stays; fixtures drop the extra policy; level-2 update → 3 steps done R1..R3; a new test: a box that already has the OLD unscoped role policy (build it from the old template text inline in the test) gets `R2` "Update" and afterwards the role policy equals the new scoped doc; a box that still has the obsolete extra-buckets policy attached is left untouched (no call mentions its ARN) and still converges to empty; `_assert_never_touch` keeps its rules.
- `tests/gui/test_permissions_fallback.py`: script has 3 steps in order `iam get-role, iam create-role, iam update-assume-role-policy, iam put-role-policy, iam put-user-policy` (stub-aws order test); embedded docs are exactly `trust`, `bucket-admin`, `runtime`; the extra-buckets detach line appears only as a comment; verify returns 3 probes with the new hints (assert lowercase `"step 1"`, `"step 2"`, `"step 3"` substrings); the list-buckets probe runs with the assumed creds.
- Recompute the manifest fingerprint with `python3 -c "from app.gui import permissions; print(permissions.templates_sha256())"` after the template edits and paste it into `provisioning/permissions.json`.

Verify: `python3 -m pytest -q`, `(cd opentofu && tofu fmt -check)`. Commit: `fix(security): prefix-only dedicated buckets — role scoped to <base>-*, no IAM write, extra-buckets policy dropped from the required set`.

### Task 14: Prefix-only dedicated buckets (app side)

**Files:**
- Modify: `app/gui/routes.py` (`job_save` dedicated branch; `provision_automated_run` backup.env write)
- Modify: `app/engine/buckets.py` (delete `grant_object_access` and `_grants_bucket`/`_GRANT_ACTIONS` if only it used them; keep `oldest_non_default_version`, `is_prefixed`, `suggest`, `valid_bucket_name`)
- Modify: `app/gui/config_io.py` (delete `extra_buckets_policy_arn`)
- Modify: `config/backup.env.example` (delete the `RUNTIME_EXTRA_BUCKETS_POLICY_ARN=` line; keep `BUCKET_ADMIN_ROLE_ARN=` and the section comment)
- Modify: `setup.sh` (delete the `RUNTIME_EXTRA_BUCKETS_POLICY_ARN=` echo)
- Modify: `app/gui/templates/job_form.html` + `app/gui/static/app.js` (prefix hint copy)
- Test: `tests/gui/test_job_form_routes.py`, `tests/engine/test_buckets_ensure.py`, `tests/gui/test_config_io.py`, `tests/gui/test_provision_routes.py`, `tests/bats/setup_sh.bats`

**Interfaces:**
- Produces: `routes.DEDICATED_NAME_RULE` message constant; helper `routes._dedicated_name_ok(base: str, bucket: str) -> bool` = `bucket.startswith(base + "-") and len(bucket) > len(base) + 1`.

Changes:
- `job_save`, dedicated create branch: after `valid_bucket_name`, refuse `not _dedicated_name_ok(base, bucket)` with `errors={"form": DEDICATED_NAME_RULE}` where
  `DEDICATED_NAME_RULE = "A dedicated bucket's name must start with the shared bucket's name followed by a dash (for example {base}-photos) — backup-engine can only reach buckets named that way."` rendered with `.format(base=base)`. This check runs BEFORE `assume_role` (no AWS on refusal). Delete the `if not buckets.is_prefixed(...): buckets.grant_object_access(...)` block.
- `provision_automated_run`: stop writing `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`.
- `job_form.html` `#bucket-prefix-hint` copy: `This name must start with <span class="mono">{{ bucket }}-</span> — backup-engine can only reach buckets named that way.` app.js `updateHint` shows it when the value is non-empty and does NOT start with `base + "-"` (a value equal to `base` must now SHOW the hint too). Update the app.js comment ("off-prefix hint" → "prefix rule hint").
- Tests: replace the off-prefix-grant tests in `test_job_form_routes.py` with: off-prefix name → guided refusal, `assume_role` never called (monkeypatch it to raise), nothing saved; name equal to the base → refused; `<base>-photos` still creates (existing test). Remove `grant_object_access` tests from `test_buckets_ensure.py` (keep the `oldest_non_default_version` test). Update `test_config_io.py` / `test_provision_routes.py` / `setup_sh.bats` for the removed key/line (the bats test now asserts `RUNTIME_EXTRA_BUCKETS_POLICY_ARN` is NOT printed). Run `tests/gui/test_static_app_js.py` and `tests/gui/test_vocabulary.py`.

Verify: `python3 -m pytest -q`, `bats tests/bats/`, `shellcheck setup.sh scripts/*.sh scripts/lib/*.sh`. Commit: `fix(security): dedicated buckets must be named <base>-…; off-prefix grant path removed`.

### Task 15: Review fix wave — stale stamp, IAM settle retry, hardening, tfstate hygiene

**Files:** `app/gui/permissions.py`, `app/gui/permissions_routes.py`, `app/gui/routes.py`, `app/gui/provision.py`, `app/engine/buckets.py`, `.dockerignore`, tests (`tests/gui/test_permissions_*.py`, `tests/gui/test_provision_routes.py`, `tests/gui/test_provision.py`, `tests/engine/test_buckets_naming.py`, `tests/gui/test_permissions_levels.py`).

**Interfaces:** Produces `permissions.clear_stamp(config_dir: str, template_path: str) -> None` (writes `""` for `PERMISSIONS_VERSION`, `PERMISSIONS_CHECKED_AT`, `BUCKET_ADMIN_ROLE_ARN`); `converge(..., sleep=time.sleep, settle_tries=3, settle_s=3.0)`.

1. **Stale stamp (review Important 2).** Call `permissions.clear_stamp(...)`:
   - in `provision_validate` after it writes the new bucket/region (guided re-setup = new destination; the permissions page then verifies);
   - in `_finish_setup_permissions` whenever it returns the warning (exception or `not outcome.ok`), before returning;
   - in `config_save` instead of carrying the stamp when `new_bucket != before S3_BUCKET` or a non-blank submitted `AWS_ACCESS_KEY_ID` differs from the stored one (`sysop._runtime_key(cfg["CONFIG_DIR"])[0]`). Here set `values[STAMP_KEY] = values[CHECKED_KEY] = ""` (the role ARN is a visible Keys field — leave it as submitted).
   Tests: guided re-validate on a stamped install → stamp + role ARN gone, `/jobs/new` shows the disabled toggle; Keys save with a changed bucket → stamp gone; Keys save with the same bucket and blank key → stamp kept (existing test); Keys POST that smuggles `PERMISSIONS_VERSION=99` is ignored; automated setup whose converge fails on a previously stamped install → stamp gone.
2. **IAM settle retry (Minor 4).** After `apply()` succeeds, re-discover + re-plan up to `settle_tries` times, calling `sleep(settle_s)` between tries, until the plan is empty; only then stamp. Test with a FakeIAM subclass whose first post-apply `get-role-policy` returns the old doc (stale read once) → outcome ok, stamped, `sleep` called once. Existing converge tests must not sleep (plan empties on the first re-check).
3. **Delete-key reminder (Minor 5).** In `_run_admin`, flash the warning when `apply_changes or not outcome.steps` (a Preview that found nothing is terminal). Test it.
4. **Shape checks (Minor 6).** `_USER_ARN_RE.fullmatch`; the script guards use `re.fullmatch` with `re.ASCII` on compiled patterns; `buckets.valid_bucket_name` uses `_BUCKET_RE.fullmatch` and `re.ASCII`. Tests: `"us-east-1\n"`, an account `"12345678901２"` (full-width digit), a user with a trailing `"\n"`, and a bucket `"good-name\n"` are all refused.
5. **`current_level` (Minor 7).** Use `raw.isdecimal()` (and `.isascii()`); `"²"` → `None`. Test.
6. **Unparseable AWS JSON (Minor 8).** `discover`'s `get` and `_make_room` wrap `json.loads` → `PermissionsError("read" / "apply", "unreadable AWS response")`. Test with a runner returning rc 0 + `"not json"`.
7. **Drop `Markup` (Minor 9).** `_ADMIN_MESSAGES`, the default message and `SETUP_PERMISSIONS_WARNING` become plain `str`; remove the `markupsafe` imports; tests that assert apostrophe copy compare against `html.unescape(response_text)`.
8. **`_run_admin` reuses `_guard` (Minor 10).**
9. **Log the swallowed automated-setup error (T10 minor).** `current_app.logger.warning("automated setup: AWS permissions check failed (%s)", type(e).__name__ + (f" {e.kind}" if getattr(e, "kind", None) else ""))` — class and kind only, never the message.
10. **tfstate hygiene.** `.dockerignore` gains `**/*.tfstate`, `**/*.tfstate.*`, `**/.terraform/`. `run_tofu_apply` copies the module with `shutil.copytree(module_src, tf_dir, ignore=shutil.ignore_patterns("*.tfstate", "*.tfstate.*", ".terraform"))` (the lock file stays). Test: a `module_src` temp dir containing `terraform.tfstate`, `terraform.tfstate.backup` and `.terraform/` → the fake runner sees none of them in `cwd`, but sees `main.tf` and `.terraform.lock.hcl`.
11. **No AWS on GET (T7 minor, Global Constraint).** One test: monkeypatch `app.gui.provision.subprocess.run` to raise, provision an install (no stamp), then GET `/`, `/status.json`, `/setup`, `/setup/destination`, `/setup/permissions`, `/jobs/new` → all 200.

Verify: `python3 -m pytest -q`, `bats tests/bats/`, `shellcheck ...`, `(cd opentofu && tofu fmt -check)`. Commit (one or several): `fix(permissions): clear stale stamp on re-setup; IAM settle retry; hardening; keep tfstate out of the image`.

### Task 16: Verify rejects an un-narrowed role; shape-check the base bucket before rendering IAM

Spec: the "Follow-up (fix-wave re-review, owner-approved)" paragraph at the end of the spec addendum.

**Files:** `app/gui/permissions.py`, `app/gui/permissions_routes.py`, `tests/gui/test_permissions_fallback.py`, `tests/gui/test_permissions_plan.py`, `tests/gui/test_permissions_routes.py` (and `tests/gui/test_permissions_engine.py` if a fixture needs the new guard).

1. **Negative scope probe.** `_probe_once` gains a 4th probe after "The role … has its policy": `"The role can't reach the shared bucket"`. Under the SAME assumed creds run `["s3api", "get-bucket-versioning", "--bucket", bucket, "--output", "json"]`. The probe is ok ONLY when `returncode != 0` and `"AccessDenied"` is in stderr (the narrowed role grants `GetBucketVersioning` on `<base>-*` only). A success means the role still has an older, wider policy → not ok, hint `"The role still has an older, wider policy — did step 2 of the script run?"`. Any other failure (no AccessDenied) → not ok, hint `"Couldn't confirm the role is limited to this app's buckets — try Verify again."`, detail scrubbed of the assumed key/secret/token. When assume-role fails, this probe is `False` with hint `"Needs the role first (step 1)."` (like probe 3). `verify()` therefore returns exactly 4 probes; the existing retry loop covers IAM propagation after the owner runs the script. Update the fakes in `tests/gui/test_permissions_fallback.py` so `get-bucket-versioning` under the assumed creds returns AccessDenied by default; add tests: (a) old wide role (get-bucket-versioning succeeds) → probe 4 fails with the "older, wider policy" hint and `all(p.ok)` is False; (b) a non-AccessDenied error → probe 4 fails with the "try Verify again" hint and the assumed secret/token is scrubbed from its detail; (c) the probe runs with the assumed creds (assert `session_token`); (d) the all-good path has 4 ok probes. Adjust any existing assertion on the probe count (3 → 4). Also update the route test fixtures in `tests/gui/test_permissions_routes.py` only if they assert a probe count.
2. **Base-bucket guard before rendering IAM.** Add in `permissions.py`:
   ```python
   _BASE_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", re.ASCII)

   def _check_base_bucket(bucket: str) -> None:
       """The base bucket name goes into IAM Resource ARNs; anything but a plain S3 name
       (e.g. "*", "?", "${...}") could widen the <base>-* confinement."""
       if not _BASE_BUCKET_RE.fullmatch(bucket or "") or ".." in bucket:
           raise PermissionsError("bucket", f"{bucket!r} is not a plain S3 bucket name")
   ```
   Call it at the top of `required_docs()` (the single place converge/plan and `script()` render policies). In `permissions_routes._perm_error_message` add `kind == "bucket"` → `"The shared bucket name on Keys & secrets isn't a plain S3 bucket name, so no AWS permissions were built from it. Fix it there first. Nothing was changed."`. Tests: `required_docs` refuses `"*"`, `"a*b"`, `"${aws:username}"`, `"Bad_Name"`, `"x..y"`, `"name\n"` and accepts `"unraid-backup-123456789012"` and a legacy dotted `"my.backups.bucket"`; a route test: provisioned install whose `S3_BUCKET=*` → POST `/setup/permissions/update` returns 400 with that message and `converge`… — monkeypatch `permissions.runtime_principal` and let the REAL `converge` run with a fake `run` that raises if any `iam` write is attempted (the guard must fire before any AWS write; preflight/account calls may be stubbed via monkeypatching `provision.verify_admin_can_provision` and `provision.aws_account_id`).

Verify: `python3 -m pytest -q`, `bats tests/bats/`. Commit: `fix(permissions): Verify rejects an un-narrowed role; shape-check the base bucket before rendering IAM`.
