# Optional per-job dedicated S3 buckets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user opt a job into its own S3 bucket, created just-in-time at job save via temporary AssumeRole credentials, while the everyday runtime key stays object-only.

**Architecture:** Additive. A new `app/engine/buckets.py` (aws-cli subprocess wrappers, injectable `runner=` like `app/engine/s3.py`) creates+configures buckets using short-lived creds from `sts assume-role`. Provisioning gains a `bucket-admin` IAM role + an "extra buckets" managed policy; the runtime user gains `sts:AssumeRole` + a `<base>-*` object wildcard. Jobs carry `bucket`/`dedicated`/`bucket_versioned`; the runner targets `${JOB_BUCKET:-$S3_BUCKET}`.

**Tech Stack:** Python 3 (Flask GUI, aws-cli via `subprocess`), bash runner (restic/rclone/aws-cli), OpenTofu, Jinja2, vanilla JS, pytest + bats.

**Spec:** `docs/superpowers/specs/2026-09-19-multi-bucket-design.md`

## Global Constraints

- **AWS access is aws-cli via `subprocess`, never boto3.** New AWS helpers take an injectable `runner=subprocess.run` param (mirror `app/engine/s3.py`) so tests stub them.
- **The runtime key stays object-only + `sts:AssumeRole`.** Bucket create/config/tagging happens ONLY with assumed-role temporary creds. Bucket *delete* perms live only on the role, used only by teardown.
- **Never auto-delete a job's bucket.** Job delete unlinks only.
- **Object Lock / WORM is out of scope.** Do not add it.
- **The estimator is FROZEN** (`tests/…/test_untouched.py` hash guard). Only `app/gui/estimate_io.py` / GUI adapters may change; never `app/estimator/*` model code.
- **Dedicated bucket config mirrors the base bucket:** AES256 SSE, all public-access blocks on, `BucketOwnerEnforced`, lifecycle = noncurrent-version expiration + abort-incomplete-multipart. Versioning per the job's toggle (default ON).
- **Naming:** dedicated bucket names default to `"<base>-<slug(jobname)>"`, editable. A name that still begins with `"<base>-"` is covered by the runtime wildcard (no IAM change); an off-prefix name requires an explicit grant.
- **Bucket tag `managed-by=backup-engine`, `job=<name>`** on every created bucket (teardown enumerates by this tag).
- **Commit after every task.** Do not deploy (the user deploys manually).

---

### Task 1: Config plumbing for role/policy ARNs + base-versioning flag

**Files:**
- Modify: `app/gui/config_io.py` (add read helpers near `read_backup_env`, line ~48)
- Modify: `config/backup.env.example` (add the new keys as commented placeholders)
- Test: `tests/gui/test_config_io.py`

**Interfaces:**
- Produces: `config_io.bucket_admin_role_arn(config_dir) -> str` (‑> `""` if unset); `config_io.extra_buckets_policy_arn(config_dir) -> str`; `config_io.base_bucket_versioned(config_dir) -> bool` (default True). All read `backup.env` via the existing `read_backup_env`.

- [ ] **Step 1: Write failing tests**

```python
# tests/gui/test_config_io.py
from app.gui import config_io

def test_role_and_policy_arns_default_empty(tmp_path):
    (tmp_path / "backup.env").write_text("S3_BUCKET=b\nAWS_REGION=us-east-1\n")
    assert config_io.bucket_admin_role_arn(str(tmp_path)) == ""
    assert config_io.extra_buckets_policy_arn(str(tmp_path)) == ""
    assert config_io.base_bucket_versioned(str(tmp_path)) is True

def test_role_and_policy_arns_read_back(tmp_path):
    (tmp_path / "backup.env").write_text(
        "S3_BUCKET=b\nAWS_REGION=us-east-1\n"
        "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::123:role/be-bucket-admin\n"
        "RUNTIME_EXTRA_BUCKETS_POLICY_ARN=arn:aws:iam::123:policy/be-extra\n"
        "BASE_BUCKET_VERSIONED=false\n")
    assert config_io.bucket_admin_role_arn(str(tmp_path)).endswith("be-bucket-admin")
    assert config_io.extra_buckets_policy_arn(str(tmp_path)).endswith("be-extra")
    assert config_io.base_bucket_versioned(str(tmp_path)) is False
```

- [ ] **Step 2: Run to verify fail**
Run: `python3 -m pytest tests/gui/test_config_io.py -k arns -v`
Expected: FAIL (AttributeError: module has no attribute 'bucket_admin_role_arn')

- [ ] **Step 3: Implement**

```python
# app/gui/config_io.py  (after read_backup_env)
def bucket_admin_role_arn(config_dir: str) -> str:
    return read_backup_env(config_dir).get("BUCKET_ADMIN_ROLE_ARN", "").strip()

def extra_buckets_policy_arn(config_dir: str) -> str:
    return read_backup_env(config_dir).get("RUNTIME_EXTRA_BUCKETS_POLICY_ARN", "").strip()

def base_bucket_versioned(config_dir: str) -> bool:
    return read_backup_env(config_dir).get("BASE_BUCKET_VERSIONED", "true").strip().lower() != "false"
```

Add to `config/backup.env.example`:
```
# Set by provisioning (multi-bucket feature); leave blank if unused.
BUCKET_ADMIN_ROLE_ARN=
RUNTIME_EXTRA_BUCKETS_POLICY_ARN=
BASE_BUCKET_VERSIONED=true
```

- [ ] **Step 4: Run to verify pass**
Run: `python3 -m pytest tests/gui/test_config_io.py -k arns -v` → PASS

- [ ] **Step 5: Commit**
```bash
git add app/gui/config_io.py config/backup.env.example tests/gui/test_config_io.py
git commit -m "feat(config): read bucket-admin role / extra-buckets policy ARNs + base-versioning flag"
```

---

### Task 2: Bucket-naming helpers (slug, suggestion, prefix classifier)

**Files:**
- Create: `app/engine/buckets.py`
- Test: `tests/engine/test_buckets_naming.py`

**Interfaces:**
- Produces: `buckets.slugify(name) -> str`; `buckets.suggest(base, jobname) -> str` (= `f"{base}-{slugify(jobname)}"`); `buckets.is_prefixed(base, bucket) -> bool` (True iff `bucket == base` or `bucket.startswith(base + "-")`); `buckets.valid_bucket_name(name) -> bool` (S3 DNS rules: 3–63 chars, lowercase letters/digits/hyphens, start/end alphanumeric, no `..`, not IP-like).

- [ ] **Step 1: Write failing tests**

```python
# tests/engine/test_buckets_naming.py
from app.engine import buckets

def test_slugify():
    assert buckets.slugify("My Photos!") == "my-photos"
    assert buckets.slugify("appdata_backups") == "appdata-backups"

def test_suggest():
    assert buckets.suggest("unraid-backup-123", "Photos") == "unraid-backup-123-photos"

def test_is_prefixed():
    assert buckets.is_prefixed("be-123", "be-123-photos") is True
    assert buckets.is_prefixed("be-123", "be-123") is True
    assert buckets.is_prefixed("be-123", "totally-other") is False

def test_valid_bucket_name():
    assert buckets.valid_bucket_name("be-123-photos") is True
    assert buckets.valid_bucket_name("BadCaps") is False
    assert buckets.valid_bucket_name("ab") is False            # too short
    assert buckets.valid_bucket_name("a..b") is False
    assert buckets.valid_bucket_name("192.168.1.1") is False   # IP-like
```

- [ ] **Step 2: Run to verify fail**
Run: `python3 -m pytest tests/engine/test_buckets_naming.py -v` → FAIL (module missing)

- [ ] **Step 3: Implement**

```python
# app/engine/buckets.py
"""Just-in-time S3 bucket creation/config for opt-in per-job dedicated buckets.
aws-cli via subprocess (runner injectable), NEVER boto3 (project constraint)."""
from __future__ import annotations
import re
import subprocess

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BUCKET_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,61})[a-z0-9]$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

def slugify(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")

def suggest(base: str, jobname: str) -> str:
    return f"{base}-{slugify(jobname)}"

def is_prefixed(base: str, bucket: str) -> bool:
    return bucket == base or bucket.startswith(base + "-")

def valid_bucket_name(name: str) -> bool:
    if not name or not _BUCKET_RE.match(name) or ".." in name or _IP_RE.match(name):
        return False
    return True
```

- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/engine/buckets.py tests/engine/test_buckets_naming.py
git commit -m "feat(buckets): naming helpers (slugify/suggest/is_prefixed/valid_bucket_name)"
```

---

### Task 3: STS assume-role helper

**Files:**
- Modify: `app/gui/provision.py` (add near `_run_aws`, line ~71)
- Test: `tests/engine/test_assume_role.py`

**Interfaces:**
- Consumes: `provision._run_aws` shape (args list; kwargs region/key/secret/session_token; returns `subprocess.CompletedProcess`).
- Produces: `provision.assume_role(role_arn, *, region, key, secret, session_token=None, run=_run_aws) -> dict` returning `{"AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","AWS_SESSION_TOKEN"}`. Raises `provision.AssumeRoleError` (secret-scrubbed) on failure.

- [ ] **Step 1: Write failing test**

```python
# tests/engine/test_assume_role.py
import json, types
from app.gui import provision

def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

def test_assume_role_parses_credentials():
    payload = json.dumps({"Credentials": {
        "AccessKeyId": "ASIA", "SecretAccessKey": "sk", "SessionToken": "tok"}})
    calls = []
    def run(args, **kw): calls.append(args); return _cp(out=payload)
    creds = provision.assume_role("arn:aws:iam::1:role/x", region="us-east-1",
                                  key="AKIA", secret="s", run=run)
    assert creds["AWS_ACCESS_KEY_ID"] == "ASIA"
    assert creds["AWS_SECRET_ACCESS_KEY"] == "sk"
    assert creds["AWS_SESSION_TOKEN"] == "tok"
    assert calls[0][:2] == ["sts", "assume-role"]

def test_assume_role_raises_on_failure():
    def run(args, **kw): return _cp(rc=255, err="AccessDenied")
    try:
        provision.assume_role("arn", region="us-east-1", key="AKIA", secret="s", run=run)
        assert False, "expected AssumeRoleError"
    except provision.AssumeRoleError as e:
        assert "AccessDenied" in str(e)
```

- [ ] **Step 2: Run to verify fail** → FAIL (no `assume_role`)

- [ ] **Step 3: Implement**

```python
# app/gui/provision.py
class AssumeRoleError(RuntimeError):
    def __init__(self, detail: str = ""):
        super().__init__(f"could not assume the bucket-admin role: {detail}".rstrip(": "))

def assume_role(role_arn, *, region, key, secret, session_token=None, run=_run_aws) -> dict:
    cp = run(["sts", "assume-role", "--role-arn", role_arn,
              "--role-session-name", "backup-engine-buckets", "--output", "json"],
             region=region, key=key, secret=secret, session_token=session_token)
    if cp.returncode != 0:
        raise AssumeRoleError(_scrub(cp.stderr.strip(), secret))
    try:
        c = json.loads(cp.stdout)["Credentials"]
        return {"AWS_ACCESS_KEY_ID": c["AccessKeyId"],
                "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
                "AWS_SESSION_TOKEN": c["SessionToken"]}
    except (ValueError, KeyError) as e:
        raise AssumeRoleError(f"unparseable assume-role response ({e})")
```
(Add `import json` if not already imported.)

- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/gui/provision.py tests/engine/test_assume_role.py
git commit -m "feat(provision): sts assume-role helper for temporary bucket-admin creds"
```

---

### Task 4: Bucket create/configure service (+ off-prefix grant)

**Files:**
- Modify: `app/engine/buckets.py`
- Test: `tests/engine/test_buckets_ensure.py`

**Interfaces:**
- Consumes: `buckets.valid_bucket_name`, `buckets.is_prefixed`.
- Produces:
  - `buckets.ensure_bucket(name, *, region, versioned, creds, runner=subprocess.run) -> None` — idempotent create + configure. `creds` is the dict from `provision.assume_role` (exported into the aws-cli env). Raises `buckets.BucketError(kind, detail)` with `kind` ∈ {`name_taken`,`too_many`,`access_denied`,`invalid_name`,`other`}.
  - `buckets.grant_object_access(policy_arn, bucket_name, account_id, *, region, creds, runner=subprocess.run) -> None` — add the bucket's ARNs to the managed policy via a new default version (used only for off-prefix names).

- [ ] **Step 1: Write failing tests** (drive the aws-cli sequence + error mapping with a stub)

```python
# tests/engine/test_buckets_ensure.py
import types
from app.engine import buckets

def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

def test_ensure_bucket_runs_full_config_sequence():
    seen = []
    def runner(argv, **kw):
        seen.append(argv[:3]); return _cp()
    buckets.ensure_bucket("be-1-photos", region="us-east-1", versioned=True,
                          creds={"AWS_ACCESS_KEY_ID":"ASIA","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                          runner=runner)
    joined = [" ".join(a) for a in seen]
    assert any("s3api create-bucket" in j for j in joined)
    assert any("put-public-access-block" in j for j in joined)
    assert any("put-bucket-encryption" in j for j in joined)
    assert any("put-bucket-versioning" in j for j in joined)
    assert any("put-bucket-lifecycle-configuration" in j for j in joined)
    assert any("put-bucket-tagging" in j for j in joined)

def test_ensure_bucket_idempotent_on_already_owned():
    def runner(argv, **kw):
        if "create-bucket" in argv:
            return _cp(rc=254, err="BucketAlreadyOwnedByYou")
        return _cp()
    buckets.ensure_bucket("be-1-photos", region="us-east-1", versioned=False,
                          creds={"AWS_ACCESS_KEY_ID":"ASIA","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                          runner=runner)   # must NOT raise

def test_ensure_bucket_maps_name_taken():
    def runner(argv, **kw):
        return _cp(rc=254, err="BucketAlreadyExists") if "create-bucket" in argv else _cp()
    try:
        buckets.ensure_bucket("taken", region="us-east-1", versioned=True,
                              creds={"AWS_ACCESS_KEY_ID":"A","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                              runner=runner)
        assert False
    except buckets.BucketError as e:
        assert e.kind == "name_taken"

def test_ensure_bucket_rejects_invalid_name():
    try:
        buckets.ensure_bucket("Bad_Name", region="us-east-1", versioned=True,
                              creds={}, runner=lambda *a, **k: _cp())
        assert False
    except buckets.BucketError as e:
        assert e.kind == "invalid_name"
```

- [ ] **Step 2: Run to verify fail** → FAIL

- [ ] **Step 3: Implement**

```python
# app/engine/buckets.py  (append)
import json, os

class BucketError(RuntimeError):
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(f"{kind}: {detail}".rstrip(": "))

def _env(creds: dict) -> dict:
    e = os.environ.copy()
    e.update({k: v for k, v in (creds or {}).items() if v})
    return e

def _aws(runner, argv, creds, *, ok_substrings=()):
    cp = runner(["aws", *argv], env=_env(creds), capture_output=True, text=True)
    if cp.returncode != 0:
        err = (cp.stderr or "")
        if any(s in err for s in ok_substrings):
            return cp
        if "BucketAlreadyExists" in err:
            raise BucketError("name_taken", "that bucket name is already taken globally")
        if "TooManyBuckets" in err:
            raise BucketError("too_many", "this AWS account is at its bucket limit")
        if "AccessDenied" in err or "not authorized" in err:
            raise BucketError("access_denied", err.strip())
        raise BucketError("other", err.strip())
    return cp

_LIFECYCLE = {"Rules": [{"ID": "backup-engine", "Status": "Enabled", "Filter": {},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]}

def ensure_bucket(name, *, region, versioned, creds, runner=subprocess.run) -> None:
    if not valid_bucket_name(name):
        raise BucketError("invalid_name", f"{name!r} is not a valid S3 bucket name")
    loc = [] if region == "us-east-1" else ["--create-bucket-configuration", f"LocationConstraint={region}"]
    _aws(runner, ["s3api", "create-bucket", "--bucket", name, "--region", region, *loc],
         creds, ok_substrings=("BucketAlreadyOwnedByYou",))
    _aws(runner, ["s3api", "put-public-access-block", "--bucket", name,
                  "--public-access-block-configuration",
                  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"], creds)
    _aws(runner, ["s3api", "put-bucket-ownership-controls", "--bucket", name,
                  "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"], creds)
    _aws(runner, ["s3api", "put-bucket-encryption", "--bucket", name,
                  "--server-side-encryption-configuration",
                  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'], creds)
    _aws(runner, ["s3api", "put-bucket-versioning", "--bucket", name,
                  "--versioning-configuration", f"Status={'Enabled' if versioned else 'Suspended'}"], creds)
    _aws(runner, ["s3api", "put-bucket-lifecycle-configuration", "--bucket", name,
                  "--lifecycle-configuration", json.dumps(_LIFECYCLE)], creds)
    _aws(runner, ["s3api", "put-bucket-tagging", "--bucket", name,
                  "--tagging", "TagSet=[{Key=managed-by,Value=backup-engine}]"], creds)

def grant_object_access(policy_arn, bucket_name, account_id, *, region, creds, runner=subprocess.run) -> None:
    # Read the current default version, append this bucket's ARNs, publish a new default.
    cur = _aws(runner, ["iam", "get-policy", "--policy-arn", policy_arn, "--output", "json"], creds)
    ver = json.loads(cur.stdout)["Policy"]["DefaultVersionId"]
    doc = _aws(runner, ["iam", "get-policy-version", "--policy-arn", policy_arn,
                        "--version-id", ver, "--output", "json"], creds)
    document = json.loads(doc.stdout)["PolicyVersion"]["Document"]
    arn = f"arn:aws:s3:::{bucket_name}"
    stmts = document.setdefault("Statement", [])
    stmts.append({"Sid": f"be{slugify(bucket_name).replace('-','')}", "Effect": "Allow",
                  "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketVersions",
                             "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                             "s3:DeleteObjectVersion", "s3:AbortMultipartUpload",
                             "s3:ListMultipartUploadParts", "s3:RestoreObject"],
                  "Resource": [arn, arn + "/*"]})
    _aws(runner, ["iam", "create-policy-version", "--policy-arn", policy_arn,
                  "--policy-document", json.dumps(document), "--set-as-default"], creds)
```

- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/engine/buckets.py tests/engine/test_buckets_ensure.py
git commit -m "feat(buckets): idempotent create+configure + off-prefix IAM grant (aws-cli)"
```

---

### Task 5: Job model — bucket / dedicated / bucket_versioned fields + JOB_BUCKET export

**Files:**
- Modify: `app/gui/jobs_io.py` (`validate` ~209; the `JOB_*` export ~384)
- Test: `tests/gui/test_jobs_io.py`

**Interfaces:**
- Consumes: existing `validate(job, source_root, *, require_exists)` return dict.
- Produces: `validate` result now carries `bucket` (str, may be ""), `dedicated` (bool), `bucket_versioned` (bool). The `JOB_*` export emits `JOB_BUCKET=<bucket>` **only when `dedicated` and `bucket` is set** (base jobs omit it so the runner falls back to `$S3_BUCKET`).

- [ ] **Step 1: Write failing tests**

```python
# tests/gui/test_jobs_io.py
from app.gui import jobs_io

def test_validate_keeps_dedicated_bucket_fields(tmp_path):
    (tmp_path / "appdata").mkdir()
    out = jobs_io.validate({"name":"photos","type":"archive","source":"appdata",
        "schedule":"0 5 * * *","storage_class":"STANDARD","enabled":True,
        "dedicated":True,"bucket":"be-1-photos","bucket_versioned":True},
        str(tmp_path))
    assert out["dedicated"] is True and out["bucket"] == "be-1-photos"
    assert out["bucket_versioned"] is True

def test_validate_defaults_to_base_bucket(tmp_path):
    (tmp_path / "appdata").mkdir()
    out = jobs_io.validate({"name":"a","type":"archive","source":"appdata",
        "schedule":"0 5 * * *","storage_class":"STANDARD","enabled":True}, str(tmp_path))
    assert out.get("dedicated", False) is False and out.get("bucket", "") == ""

def test_job_env_emits_JOB_BUCKET_only_when_dedicated(capsys):
    job = {"name":"photos","type":"archive","source":"appdata","schedule":"0 5 * * *",
           "storage_class":"STANDARD","dedicated":True,"bucket":"be-1-photos",
           "retention":{"type":"keep_all"}}
    text = jobs_io.job_env_text(job)          # see note in Step 3 re: helper name
    assert "JOB_BUCKET='be-1-photos'" in text
    base = dict(job); base.update(dedicated=False, bucket="")
    assert "JOB_BUCKET" not in jobs_io.job_env_text(base)
```

- [ ] **Step 2: Run to verify fail** → FAIL

- [ ] **Step 3: Implement**
In `validate`, after the existing `out = {...}` assembly, carry the fields:
```python
    out["dedicated"] = bool(job.get("dedicated"))
    out["bucket"] = str(job.get("bucket", "")).strip() if out["dedicated"] else ""
    out["bucket_versioned"] = bool(job.get("bucket_versioned", True))
```
In the `JOB_*` export block (~384), after the `JOB_STORAGE_CLASS` line, add:
```python
    if job.get("dedicated") and job.get("bucket"):
        lines.append(f"JOB_BUCKET={q(job['bucket'])}")
```
> If the export is currently inline in `__main__`, extract it into a small
> `job_env_text(job) -> str` returning the joined lines and have `__main__`
> print it — so the test above can call it directly.

- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/gui/jobs_io.py tests/gui/test_jobs_io.py
git commit -m "feat(jobs): dedicated-bucket fields + JOB_BUCKET export (base jobs unchanged)"
```

---

### Task 6: Runner targets `${JOB_BUCKET:-$S3_BUCKET}` (restic + rclone + vfiles)

**Files:**
- Modify: `scripts/lib/config.sh:59` (RESTIC_REPOSITORY)
- Modify: `scripts/backup-job.sh` (rclone dest ~92/95; vfiles bucket)
- Test: `tests/bats/config_bucket.bats` (new)

**Interfaces:**
- Consumes: `JOB_BUCKET` (from Task 5, may be unset), `S3_BUCKET` (base).
- Produces: all engines write under the job's bucket when `JOB_BUCKET` set, else base.

- [ ] **Step 1: Write failing bats test**

```bash
# tests/bats/config_bucket.bats
@test "RESTIC_REPOSITORY uses JOB_BUCKET when set" {
  S3_BUCKET=base JOB_BUCKET=be-1-photos AWS_REGION=us-east-1 \
    run bash -c 'source scripts/lib/config.sh; derive_restic_repo; echo "$RESTIC_REPOSITORY"'
  [[ "$output" == *"be-1-photos/appdata"* ]]
}
@test "RESTIC_REPOSITORY falls back to S3_BUCKET" {
  S3_BUCKET=base AWS_REGION=us-east-1 \
    run bash -c 'source scripts/lib/config.sh; derive_restic_repo; echo "$RESTIC_REPOSITORY"'
  [[ "$output" == *"base/appdata"* ]]
}
```

- [ ] **Step 2: Run to verify fail**
Run: `bats tests/bats/config_bucket.bats` → FAIL

- [ ] **Step 3: Implement**
`scripts/lib/config.sh:59` — change the derivation to prefer `JOB_BUCKET`:
```bash
    RESTIC_REPOSITORY="s3:${host}/${JOB_BUCKET:-${S3_BUCKET:-}}/appdata"
```
`scripts/backup-job.sh` rclone (both the `args=(... s3:$S3_BUCKET/media/$JOB ...)` and the `runs_set_command`/`rclone check`/restore references): replace `$S3_BUCKET` with `${JOB_BUCKET:-$S3_BUCKET}`. Do the same for the vfiles bucket argument.
> If `config.sh` computes `RESTIC_REPOSITORY` inline at source-time, wrap that in a
> `derive_restic_repo()` function (called where it is today) so the bats test can invoke it.

- [ ] **Step 4: Run to verify pass** → PASS. Also `grep -n 'S3_BUCKET/media' scripts/backup-job.sh scripts/restore.sh` and confirm each now uses `${JOB_BUCKET:-$S3_BUCKET}`.
- [ ] **Step 5: Commit**
```bash
git add scripts/lib/config.sh scripts/backup-job.sh tests/bats/config_bucket.bats
git commit -m "feat(runner): target per-job JOB_BUCKET, base bucket as fallback"
```

---

### Task 7: Provisioning — bucket-admin role, extra-buckets policy, runtime STS + wildcard

**Files:**
- Create: `provisioning/bucket-admin-policy.json.tmpl`
- Create: `provisioning/extra-buckets-policy.json.tmpl` (initial empty-but-valid doc)
- Modify: `provisioning/iam-policy.json.tmpl` (add `sts:AssumeRole` + `<base>-*` wildcard RW/List)
- Modify: `opentofu/main.tf` (role + role policy + managed policy + attach + variable `base_bucket_versioned`)
- Modify: `app/gui/provision.py` (`render_policy` already exists; add renderers for the new templates; capture role/policy ARNs from `tofu output` into `backup.env`)
- Test: `tests/engine/test_iam_policy.py`

**Interfaces:**
- Produces: rendered policies; after `tofu apply`, `backup.env` gains `BUCKET_ADMIN_ROLE_ARN`, `RUNTIME_EXTRA_BUCKETS_POLICY_ARN`.

- [ ] **Step 1: Write failing tests** (extend the existing IAM policy test)

```python
# tests/engine/test_iam_policy.py
from app.gui import provision

def test_runtime_policy_has_sts_and_wildcard(tmp_path):
    doc = provision.render_policy("unraid-backup-123")
    assert "sts:AssumeRole" in doc
    assert "arn:aws:s3:::unraid-backup-123-*/*" in doc

def test_bucket_admin_policy_has_create_and_config():
    doc = provision.render_bucket_admin_policy("unraid-backup-123")
    for a in ("s3:CreateBucket","s3:PutBucketVersioning","s3:PutEncryptionConfiguration",
              "s3:PutLifecycleConfiguration","s3:PutBucketTagging","s3:PutBucketPublicAccessBlock"):
        assert a in doc
    assert "s3:DeleteBucket" not in doc     # delete lives on a separate teardown grant
```

- [ ] **Step 2: Run to verify fail** → FAIL

- [ ] **Step 3: Implement**
Add to `provisioning/iam-policy.json.tmpl` a statement granting `sts:AssumeRole` on `${bucket_admin_role_arn}` and extend the object/list statements with `arn:aws:s3:::${bucket}-*` and `arn:aws:s3:::${bucket}-*/*`. (Because `render_policy` currently takes only `bucket`, thread the role ARN through as a second templated var with a sensible default of `*` for local render/tests, resolved concretely by tofu.)

Create `provisioning/bucket-admin-policy.json.tmpl`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "CreateAndConfig", "Effect": "Allow",
      "Action": ["s3:CreateBucket","s3:PutBucketVersioning","s3:PutBucketPublicAccessBlock",
                 "s3:PutBucketOwnershipControls","s3:PutEncryptionConfiguration",
                 "s3:PutLifecycleConfiguration","s3:PutBucketTagging",
                 "s3:GetBucketLocation","s3:GetBucketVersioning"],
      "Resource": ["arn:aws:s3:::${bucket}-*"] },
    { "Sid": "CreateBucketGlobal", "Effect": "Allow",
      "Action": ["s3:CreateBucket"], "Resource": ["*"] }
  ]
}
```
Add `provision.render_bucket_admin_policy(bucket)` mirroring `render_policy`.

In `opentofu/main.tf` add: `aws_iam_role.bucket_admin` (trust = the runtime user), `aws_iam_role_policy` from the bucket-admin template, `aws_iam_policy.runtime_extra_buckets` from the (initially minimal) extra-buckets template attached to the runtime user, and a `variable "base_bucket_versioned" { default = true }` driving `aws_s3_bucket_versioning.backup` (`Enabled`/`Suspended`). Add `output "bucket_admin_role_arn"` and `output "runtime_extra_buckets_policy_arn"`. In `provision.py`, after `tofu apply`, read these via `tofu output -json` and persist into `backup.env` (use `config_io.write_backup_env`).

- [ ] **Step 4: Run to verify pass** → PASS. Also `cd opentofu && tofu validate`.
- [ ] **Step 5: Commit**
```bash
git add provisioning/ opentofu/main.tf app/gui/provision.py tests/engine/test_iam_policy.py
git commit -m "feat(provision): bucket-admin role + extra-buckets policy + runtime STS/wildcard; base-versioning var"
```

---

### Task 8: Wizard — dedicated-bucket toggle + JIT creation on save

**Files:**
- Modify: `app/gui/templates/job_form.html` (new "Storage" section)
- Modify: `app/gui/static/app.js` (reveal name+versioning; prefill suggestion; off-prefix hint)
- Modify: `app/gui/routes.py` (`_fresh_form_values`, `_form_values_from_request`, `job_save` ~1871, `_render_job_form`)
- Test: `tests/gui/test_job_form_routes.py`

**Interfaces:**
- Consumes: `buckets.suggest/is_prefixed/valid_bucket_name`, `buckets.ensure_bucket`, `buckets.grant_object_access`, `provision.assume_role`, `config_io.bucket_admin_role_arn/extra_buckets_policy_arn`.
- Produces: on a dedicated save, the bucket exists+configured before the job is written; job persists `dedicated/bucket/bucket_versioned`; on failure the wizard re-renders 200 with a "Not saved" banner (reuse the Task-from-earlier banner) naming the `BucketError.kind`.

- [ ] **Step 1: Write failing tests** (stub AWS so no network)

```python
# tests/gui/test_job_form_routes.py
import app.engine.buckets as buckets
import app.gui.provision as provision

def test_dedicated_save_creates_bucket_then_redirects(client, app, monkeypatch):
    made = {}
    monkeypatch.setattr(provision, "assume_role", lambda *a, **k: {"AWS_ACCESS_KEY_ID":"ASIA"})
    monkeypatch.setattr(buckets, "ensure_bucket", lambda name, **k: made.setdefault("name", name))
    (app.config["CONFIG_DIR"] and None)  # cfg already has S3_BUCKET + role arn (fixture)
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups-photos", "bucket_versioned": "1"})
    assert r.status_code in (302, 303)
    assert made["name"] == "bw-backups-photos"
    assert _jobs(app)[0]["dedicated"] is True

def test_dedicated_save_surfaces_bucket_error(client, app, monkeypatch):
    monkeypatch.setattr(provision, "assume_role", lambda *a, **k: {})
    def boom(name, **k): raise buckets.BucketError("name_taken", "taken")
    monkeypatch.setattr(buckets, "ensure_bucket", boom)
    t = _csrf(client)
    r = client.post("/jobs", data={"csrf": t, "name": "photos", "type": "archive",
        "source": "media/movies", "schedule": "0 5 * * *", "storage_class": "STANDARD",
        "enabled": "1", "retention_type": "days", "retention_days": "180",
        "dedicated": "1", "bucket": "bw-backups-photos", "bucket_versioned": "1"})
    assert r.status_code == 200
    assert "Not saved" in r.get_data(as_text=True)
    assert _jobs(app) == []
```
(The `app` fixture must set `BUCKET_ADMIN_ROLE_ARN`/`RUNTIME_EXTRA_BUCKETS_POLICY_ARN` in `backup.env`.)

- [ ] **Step 2: Run to verify fail** → FAIL

- [ ] **Step 3: Implement**
- `_fresh_form_values`: add `"dedicated": "", "bucket": "", "bucket_versioned": "1"`.
- `_form_values_from_request`: read `dedicated`, `bucket`, `bucket_versioned` from the form.
- `job_save` (before `jobs_io.upsert`): if `dedicated`:
```python
    if f.get("dedicated"):
        bucket = f.get("bucket", "").strip()
        if not buckets.valid_bucket_name(bucket):
            return _render_job_form(cfg, job=existing, fv=fv, errors={"form": f"invalid bucket name {bucket!r}"})
        role = config_io.bucket_admin_role_arn(cfg["CONFIG_DIR"])
        base = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("S3_BUCKET","")
        region = config_io.read_backup_env(cfg["CONFIG_DIR"]).get("AWS_REGION","us-east-1")
        akey, asec = _runtime_creds(cfg)          # from secrets.env (existing sysop helper)
        try:
            creds = provision.assume_role(role, region=region, key=akey, secret=asec)
            buckets.ensure_bucket(bucket, region=region, versioned=bool(f.get("bucket_versioned")), creds=creds)
            if not buckets.is_prefixed(base, bucket):
                acct = provision.aws_account_id(region=region, key=akey, secret=asec)
                buckets.grant_object_access(config_io.extra_buckets_policy_arn(cfg["CONFIG_DIR"]),
                                            bucket, acct, region=region, creds=creds)
        except (provision.AssumeRoleError, buckets.BucketError) as e:
            return _render_job_form(cfg, job=existing, fv=fv, errors={"form": str(e)})
        job["dedicated"] = True; job["bucket"] = bucket
        job["bucket_versioned"] = bool(f.get("bucket_versioned"))
```
- `job_form.html`: add a "Storage" section — a checkbox `name="dedicated"`, a text `name="bucket"` (prefilled via JS), and a checkbox `name="bucket_versioned"` (checked). Hidden until `dedicated` is on (`data-when-*` pattern).
- `app.js`: when `#dedicated` toggles, reveal the fields; prefill `#bucket` with `suggest(base, name)` (expose `base` via a `data-base-bucket` attr on the form) and update on name change until the user edits it; show the off-prefix hint when `#bucket` value doesn't start with `base + "-"`.

- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/gui/templates/job_form.html app/gui/static/app.js app/gui/routes.py tests/gui/test_job_form_routes.py
git commit -m "feat(gui): opt-in dedicated bucket in the create-job wizard (JIT create on save, fail fast)"
```

---

### Task 9: Job-delete UI states the bucket is kept

**Files:**
- Modify: `app/gui/templates/job.html` (the `···` delete control / confirm)
- Test: `tests/gui/test_job_page_routes.py`

**Interfaces:**
- Consumes: the job's `dedicated`/`bucket` fields on the job page context.

- [ ] **Step 1: Write failing test**
```python
def test_job_page_delete_notes_bucket_kept_for_dedicated(client, app):
    _seed(app, {"name":"photos","type":"archive","source":"media/movies","schedule":"0 5 * * *",
                "enabled":True,"storage_class":"STANDARD","dedicated":True,"bucket":"bw-backups-photos",
                "retention":{"type":"keep_all"}})
    body = client.get("/jobs/photos").get_data(as_text=True)
    assert "bw-backups-photos" in body
    assert "won't be deleted" in body or "will not be deleted" in body
```

- [ ] **Step 2: Run to verify fail** → FAIL
- [ ] **Step 3: Implement** — in `job.html`, near the delete control, when `job.dedicated`, render a line: `Deleting this job won't delete its bucket <code>{{ job.bucket }}</code> or its data (it stays in S3 and keeps billing).`
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/gui/templates/job.html tests/gui/test_job_page_routes.py
git commit -m "feat(gui): job delete states the dedicated bucket + data are kept"
```

---

### Task 10: Settings — base bucket name editable (repoint, with warning)

**Files:**
- Modify: `app/gui/templates/config.html` (base bucket field)
- Modify: `app/gui/routes.py` (the config POST handler that writes `backup.env`)
- Test: `tests/gui/test_config_routes.py` (or the existing config route test)

**Interfaces:**
- Consumes: `config_io.read_backup_env`, `config_io.write_backup_env`.

- [ ] **Step 1: Write failing test**
```python
def test_settings_updates_base_bucket_name(client, app):
    t = _csrf(client)
    r = client.post("/config", data={"csrf": t, "S3_BUCKET": "new-base-name"})
    assert r.status_code in (200, 302, 303)
    from app.gui import config_io
    assert config_io.read_backup_env(app.config["CONFIG_DIR"])["S3_BUCKET"] == "new-base-name"
```
(Adapt the exact route/field names to the existing config screen.)

- [ ] **Step 2: Run to verify fail** → FAIL
- [ ] **Step 3: Implement** — render the base bucket name (prefilled from `backup.env`) as an editable field with a warning that changing it repoints future writes and does **not** move existing data; the POST validates and calls `write_backup_env`.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/gui/templates/config.html app/gui/routes.py tests/gui/test_config_routes.py
git commit -m "feat(gui): editable base bucket name in settings (repoint, with warning)"
```

---

### Task 11: Teardown enumerates + deletes tagged buckets

**Files:**
- Modify: `app/engine/buckets.py` (add `list_managed(...)`, `empty_and_delete(...)`)
- Create/Modify: the teardown entry point (a CLI in `buckets.py` `__main__`, invoked by the existing teardown flow)
- Test: `tests/engine/test_buckets_teardown.py`

**Interfaces:**
- Produces: `buckets.list_managed(region, creds, runner=...) -> list[str]` (buckets whose `managed-by=backup-engine` tag is present); `buckets.empty_and_delete(name, *, region, creds, runner=...) -> None` (delete all object versions, then `delete-bucket`).

- [ ] **Step 1: Write failing tests** (stub the aws-cli list/get-tagging/delete calls)
```python
def test_list_managed_filters_by_tag():
    ...  # stub s3api list-buckets + get-bucket-tagging; assert only tagged names returned
def test_empty_and_delete_removes_versions_then_bucket():
    ...  # stub list-object-versions + delete-objects + delete-bucket; assert order
```
(Write concrete stubs mirroring `tests/engine/test_buckets_ensure.py`.)

- [ ] **Step 2: Run to verify fail** → FAIL
- [ ] **Step 3: Implement** `list_managed` (list-buckets → get-bucket-tagging, keep tagged) and `empty_and_delete` (list-object-versions + delete-markers → batched delete-objects → delete-bucket). Wire a `__main__` subcommand the teardown path calls with **role creds that include delete perms** (grant `s3:DeleteBucket` + `s3:DeleteObject*` to the bucket-admin role in Task 7's template under a `Teardown` sid, or a separate teardown role — keep it off the everyday runtime key).
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit**
```bash
git add app/engine/buckets.py tests/engine/test_buckets_teardown.py
git commit -m "feat(buckets): teardown enumerates + empties + deletes managed buckets"
```

---

### Task 12: Cost workbench groups spend by bucket (adapter only)

**Files:**
- Modify: `app/gui/estimate_io.py` and/or the cost GUI adapter (NOT `app/estimator/*`)
- Test: `tests/gui/test_estimate_io.py`

**Interfaces:**
- Consumes: existing per-job cost inputs; now a job may name a distinct `bucket`.

- [ ] **Step 1: Write failing test** — assert per-job cost still resolves when a job has a dedicated `bucket` (the model math is unchanged; only grouping/labels differ). If Cost Explorer real-spend grouping keys off bucket, assert dedicated buckets are attributed to their job.
- [ ] **Step 2: Run to verify fail** → FAIL
- [ ] **Step 3: Implement** the minimal adapter change so a dedicated bucket doesn't break attribution. **Do not touch the frozen estimator.**
- [ ] **Step 4: Run to verify pass** → PASS; also run the estimator freeze guard: `python3 -m pytest -k untouched`.
- [ ] **Step 5: Commit**
```bash
git add app/gui/estimate_io.py tests/gui/test_estimate_io.py
git commit -m "feat(cost): attribute dedicated-bucket jobs correctly (adapter only; estimator untouched)"
```

---

## Self-Review

**Spec coverage:** trigger/opt-in → T8; drivers (isolation/lifecycle/cost + per-bucket versioning) → T4/T6/T8; base versioning configurable at setup → T7; AssumeRole credential model → T3/T7/T8; Object Lock out → (omitted, per constraint); bucket-on-delete kept → T9; naming (editable suggestion + base editable) → T2/T8/T10; eligibility all three types → T5/T6; create-at-save fail-fast → T8; hybrid RW scoping (wildcard + off-prefix grant) → T4/T6/T7/T8; per-bucket versioning default on → T5/T8; teardown → T11; cost → T12; config plumbing → T1. No gaps.

**Placeholder scan:** code steps carry real code; the two "adapt to existing route/field names" notes (T10/T12) point at concrete existing files and are integration reconnaissance, not deferred logic — the executor confirms exact selectors when they open those files.

**Type consistency:** `ensure_bucket(name, *, region, versioned, creds, runner)`, `assume_role(...) -> creds dict`, `grant_object_access(policy_arn, bucket, account_id, ...)`, `is_prefixed(base, bucket)`, `JOB_BUCKET` env, `BucketError.kind` — names match across T2–T8/T11.
