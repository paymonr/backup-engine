# Per-Job Retention Policies (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every backup job a per-job retention **policy** (keep-everything / keep-N-days / keep-last-N-versions; restic also tiered), chosen in the wizard and enforced client-side by each engine after a run.

**Architecture:** A single `retention` object on each job (in `jobs.json`) is normalized + migrated by `jobs_io`, emitted as `JOB_RETENTION_*` shell vars, and enforced per engine: restic maps it to `forget` flags; the versioned-files prune gains count/keep-all; archive gains a brand-new client-side S3 version prune (`app/engine/archive_prune.py`) using the aws CLI. The cost model learns the count/keep-all shapes. This is Phase 1 of the spec; Phase 2 (baseline-lifecycle Settings screen) is a separate plan.

**Tech Stack:** Python 3 (pure model/engine + Flask/Jinja GUI), bash (`scripts/backup-job.sh`), restic, rclone, aws CLI (`s3api`), pytest + bats.

**Spec:** `docs/superpowers/specs/2026-09-11-retention-policies-design.md`

## Global Constraints

- **One retention shape everywhere:** `{"type":"keep_all"}` | `{"type":"days","days":N}` | `{"type":"count","count":N}` | `{"type":"tiered","keep":{last,daily,weekly,monthly}}`. `tiered` is **versioned-only**. `days ≥ 0`, `count ≥ 1`.
- **Back-compat:** a job with no `retention` but an old `keep`/`retention_days` is migrated on read; archive jobs with neither → `{"type":"days","days":180}`.
- **Engine purity:** `app/engine/*` does NO env/print; S3 I/O goes through `app/engine/s3.py`; `archive_prune` is pure + `runner`-injectable, like `vfiles`.
- **Prune scope guard (load-bearing):** a version prune MUST refuse any key outside the job's own `media/<job>/` prefix and MUST never delete a current (latest) version.
- **aws CLI for versions:** rclone can't target a `VersionId`; version list/delete use `aws s3api` (like `s3.thaw`).
- **IAM:** runtime policy gains `s3:ListBucketVersions` + `s3:DeleteObjectVersion` only. No bucket-config perms.
- **Commit** after each green task (conventional-commit messages, no attribution lines). `docs/` is git-ignored (force-add spec/plan only).
- **Baseline default** noncurrent window: **180 days** (used as the archive migration default).

---

### Task 1: Retention schema — normalize, migrate, validate (`jobs_io`)

**Files:**
- Modify: `app/gui/jobs_io.py` (add `_RETENTION_TYPES`, `_normalize_retention`; call it in `validate`)
- Test: `tests/gui/test_jobs_io.py`

**Interfaces:**
- Produces: `validate(job, source_root)` returns a dict whose `["retention"]` is one normalized policy object (shapes above). `_normalize_retention(job: dict, typ: str) -> dict`.

- [ ] **Step 1: Write the failing tests** — append to `tests/gui/test_jobs_io.py`:

```python
import pytest
from app.gui import jobs_io

def _base(**kw):
    d = {"name": "j", "type": "archive", "source": "movies",
         "schedule": "0 4 * * 0", "storage_class": "STANDARD"}
    d.update(kw); return d

def _val(job, tmp_path):
    (tmp_path / "movies").mkdir(exist_ok=True); (tmp_path / "appdata").mkdir(exist_ok=True)
    return jobs_io.validate(job, str(tmp_path))["retention"]

def test_retention_explicit_days(tmp_path):
    assert _val(_base(retention={"type": "days", "days": 30}), tmp_path) == {"type": "days", "days": 30}

def test_retention_count_and_keep_all(tmp_path):
    assert _val(_base(retention={"type": "count", "count": 5}), tmp_path) == {"type": "count", "count": 5}
    assert _val(_base(retention={"type": "keep_all"}), tmp_path) == {"type": "keep_all"}

def test_tiered_only_for_versioned(tmp_path):
    t = {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}
    assert _val(_base(type="versioned", source="appdata", retention=t), tmp_path)["type"] == "tiered"
    with pytest.raises(ValueError):
        _val(_base(type="archive", retention=t), tmp_path)   # tiered on archive -> reject

def test_migrate_legacy_versioned_keep(tmp_path):
    r = _val(_base(type="versioned", source="appdata", keep={"last": 2, "daily": 5, "weekly": 1, "monthly": 0}), tmp_path)
    assert r == {"type": "tiered", "keep": {"last": 2, "daily": 5, "weekly": 1, "monthly": 0}}

def test_migrate_legacy_versioned_files_retention_days(tmp_path):
    r = _val(_base(type="versioned-files", source="movies", retention_days=45), tmp_path)
    assert r == {"type": "days", "days": 45}

def test_archive_default_is_days_180(tmp_path):
    assert _val(_base(type="archive"), tmp_path) == {"type": "days", "days": 180}

def test_bad_policy_rejected(tmp_path):
    for bad in ({"type": "nope"}, {"type": "days", "days": -1}, {"type": "count", "count": 0}):
        with pytest.raises(ValueError):
            _val(_base(retention=bad), tmp_path)
```

- [ ] **Step 2: Run — expect FAIL**
Run: `python3 -m pytest tests/gui/test_jobs_io.py -k "retention or tiered or migrate or archive_default or bad_policy" -q`
Expected: FAIL (`KeyError: 'retention'` — `validate` doesn't emit it yet).

- [ ] **Step 3: Implement in `app/gui/jobs_io.py`** — add near the constants:

```python
_RETENTION_TYPES = ("keep_all", "days", "count", "tiered")

def _normalize_retention(job: dict, typ: str) -> dict:
    """One retention policy per job. Explicit `retention` wins; else migrate the
    legacy per-type fields; else default. `tiered` is versioned-only."""
    r = job.get("retention")
    if not isinstance(r, dict):   # migrate legacy shapes
        if typ == "versioned" and job.get("keep"):
            r = {"type": "tiered", "keep": job["keep"]}
        elif typ == "versioned-files" and job.get("retention_days") is not None:
            r = {"type": "days", "days": job["retention_days"]}
        else:
            r = {"type": "days", "days": 180}   # archive default / anything unset
    t = r.get("type")
    if t not in _RETENTION_TYPES:
        raise ValueError(f"unknown retention type {t!r}")
    if t == "tiered":
        if typ != "versioned":
            raise ValueError("tiered retention is only valid for versioned (restic) jobs")
        keep = r.get("keep") or {}
        return {"type": "tiered", "keep": {k: max(0, int(keep.get(k, 0))) for k in _KEEP_KEYS}}
    if t == "days":
        d = int(r.get("days", 0))
        if d < 0:
            raise ValueError("retention days must be >= 0")
        return {"type": "days", "days": d}
    if t == "count":
        c = int(r.get("count", 0))
        if c < 1:
            raise ValueError("retention count must be >= 1")
        return {"type": "count", "count": c}
    return {"type": "keep_all"}
```

Then in `validate`, replace the per-type `keep`/`retention_days`/`mirror` block's retention handling by adding one line before `return out` (keep `mirror` for archive; keep is now folded into retention):

```python
    if typ == "archive":
        out["mirror"] = bool(job.get("mirror", False))
    out["retention"] = _normalize_retention(job, typ)
    return out
```
(Remove the old `if typ == "versioned": out["keep"]=…` and `elif versioned-files: out["retention_days"]=…` branches — they're now inside `_normalize_retention`.)

- [ ] **Step 4: Run — expect PASS** (new + existing jobs_io tests)
Run: `python3 -m pytest tests/gui/test_jobs_io.py -q`

- [ ] **Step 5: Commit**
```bash
git add app/gui/jobs_io.py tests/gui/test_jobs_io.py
git commit -m "feat(retention): normalize+migrate a per-job retention policy in jobs_io"
```

---

### Task 2: Emit retention as shell vars (`jobs_io.emit_shell`)

**Files:**
- Modify: `app/gui/jobs_io.py` (`emit_shell`)
- Test: `tests/gui/test_jobs_io.py`

**Interfaces:**
- Produces: `emit_shell(job)` outputs `JOB_RETENTION_TYPE`, and `JOB_RETENTION_DAYS`/`JOB_RETENTION_COUNT`/`JOB_KEEP_*` as applicable — consumed by `scripts/backup-job.sh` (Task 6) and the vfiles engine (Task 5).

- [ ] **Step 1: Write the failing test**:

```python
def test_emit_shell_retention_vars(tmp_path):
    def emit(job): return jobs_io.emit_shell(jobs_io.validate(job, str(tmp_path)))
    (tmp_path / "movies").mkdir(exist_ok=True); (tmp_path / "appdata").mkdir(exist_ok=True)
    assert "JOB_RETENTION_TYPE=days" in emit(_base(retention={"type": "days", "days": 30}))
    assert "JOB_RETENTION_DAYS=30" in emit(_base(retention={"type": "days", "days": 30}))
    assert "JOB_RETENTION_COUNT=5" in emit(_base(retention={"type": "count", "count": 5}))
    v = emit(_base(type="versioned", source="appdata",
                   retention={"type": "tiered", "keep": {"last": 2, "daily": 5, "weekly": 1, "monthly": 0}}))
    assert "JOB_RETENTION_TYPE=tiered" in v and "JOB_KEEP_LAST=2" in v and "JOB_KEEP_DAILY=5" in v
    assert "JOB_RETENTION_TYPE=keep_all" in emit(_base(retention={"type": "keep_all"}))
```

- [ ] **Step 2: Run — expect FAIL** (`JOB_RETENTION_TYPE` absent).

- [ ] **Step 3: Implement** — replace `emit_shell`'s per-type block with a retention-driven one (keep the `JOB_NAME/TYPE/SOURCE/STORAGE_CLASS` and `JOB_MIRROR` lines):

```python
    r = job.get("retention") or {"type": "days", "days": 180}
    lines.append(f"JOB_RETENTION_TYPE={q(r['type'])}")
    if r["type"] == "days":
        lines.append(f"JOB_RETENTION_DAYS={int(r['days'])}")
    elif r["type"] == "count":
        lines.append(f"JOB_RETENTION_COUNT={int(r['count'])}")
    elif r["type"] == "tiered":
        keep = r.get("keep", {})
        lines += [f"JOB_KEEP_{k.upper()}={int(keep.get(k, 0))}" for k in _KEEP_KEYS]
```
(Archive keeps its `JOB_MIRROR` line; drop the old `JOB_KEEP_*`/`JOB_RETENTION_DAYS` per-type lines.)

- [ ] **Step 4: Run — expect PASS**: `python3 -m pytest tests/gui/test_jobs_io.py -q`
- [ ] **Step 5: Commit**: `git commit -am "feat(retention): emit JOB_RETENTION_* shell vars"`

---

### Task 3: aws-CLI version helpers (`s3.py`)

**Files:**
- Modify: `app/engine/s3.py` (add `list_versions`, `delete_version`)
- Test: `tests/engine/test_s3.py` (or the existing engine s3 test module)

**Interfaces:**
- Produces:
  - `list_versions(bucket, prefix, *, runner=subprocess.run) -> list[dict]` — each `{"key","version_id","is_latest":bool,"last_modified":float}` (epoch seconds), across pages.
  - `delete_version(bucket, key, version_id, *, runner=subprocess.run) -> None`.

- [ ] **Step 1: Write failing tests** — use a fake runner returning canned `aws s3api` JSON:

```python
import json, types
from app.engine import s3

def _proc(stdout, rc=0): return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")

def test_list_versions_parses(monkeypatch):
    payload = {"Versions": [
        {"Key": "media/j/a", "VersionId": "v1", "IsLatest": True, "LastModified": "2026-09-01T00:00:00+00:00"},
        {"Key": "media/j/a", "VersionId": "v0", "IsLatest": False, "LastModified": "2026-08-01T00:00:00+00:00"}]}
    def runner(argv, **kw):
        assert "list-object-versions" in argv and "--prefix" in argv
        return _proc(json.dumps(payload))
    out = s3.list_versions("buck", "media/j/", runner=runner)
    assert {v["version_id"] for v in out} == {"v1", "v0"}
    latest = next(v for v in out if v["version_id"] == "v1")
    assert latest["is_latest"] is True and latest["last_modified"] > 0

def test_delete_version_calls_s3api(monkeypatch):
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv; return _proc("")
    s3.delete_version("buck", "media/j/a", "v0", runner=runner)
    a = seen["argv"]
    assert "delete-object" in a and "--version-id" in a and "v0" in a and "media/j/a" in a
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: module 'app.engine.s3' has no attribute 'list_versions'`).

- [ ] **Step 3: Implement** in `app/engine/s3.py` (mirror `thaw`'s aws-CLI style; parse ISO8601 to epoch):

```python
import json
from datetime import datetime

def list_versions(bucket, prefix, *, runner=subprocess.run) -> list[dict]:
    argv = ["aws", "s3api", "list-object-versions", "--bucket", bucket,
            "--prefix", prefix, "--output", "json"]
    proc = runner(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise S3Error((getattr(proc, "stderr", "") or "").strip() or "list-object-versions failed")
    data = json.loads(proc.stdout or "{}")
    out = []
    for v in data.get("Versions", []):
        lm = v.get("LastModified", "")
        try:
            ts = datetime.fromisoformat(lm.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            ts = 0.0
        out.append({"key": v["Key"], "version_id": v["VersionId"],
                    "is_latest": bool(v.get("IsLatest")), "last_modified": ts})
    return out

def delete_version(bucket, key, version_id, *, runner=subprocess.run) -> None:
    _run(runner, ["aws", "s3api", "delete-object", "--bucket", bucket,
                  "--key", key, "--version-id", version_id])
```
(Check the actual `S3Error`/`_run` names at the top of `s3.py` and match them.)

- [ ] **Step 4: Run — expect PASS**. **Step 5: Commit** `feat(retention): s3 list_versions/delete_version via aws-cli`.

---

### Task 4: Archive version prune (pure engine)

**Files:**
- Create: `app/engine/archive_prune.py`
- Test: `tests/engine/test_archive_prune.py`

**Interfaces:**
- Consumes: `s3.list_versions`, `s3.delete_version` (Task 3).
- Produces:
  - `select_prunable(versions: list[dict], policy: dict, now: float) -> list[dict]` — pure; returns `[{"key","version_id"}]` to delete. Never selects a version with `is_latest`.
  - `prune(job, policy, *, bucket, now, runner) -> int` — lists, selects, deletes (scope-guarded); returns count pruned.
  - CLI `python3 -m app.engine.archive_prune <job> --type T [--days N] [--count N]`.

- [ ] **Step 1: Write failing tests**:

```python
import time
from app.engine import archive_prune as ap

DAY = 86400
def _v(key, vid, latest, age_days, now): return {"key": key, "version_id": vid, "is_latest": latest, "last_modified": now - age_days * DAY}

def test_days_deletes_old_noncurrent_only():
    now = time.time()
    vs = [_v("media/j/a", "cur", True, 1, now), _v("media/j/a", "old", False, 40, now), _v("media/j/a", "new", False, 5, now)]
    got = ap.select_prunable(vs, {"type": "days", "days": 30}, now)
    assert got == [{"key": "media/j/a", "version_id": "old"}]   # only the >30d noncurrent; never the latest

def test_count_keeps_n_most_recent_per_key():
    now = time.time()
    vs = [_v("media/j/a", f"v{i}", i == 0, i, now) for i in range(5)]  # v0 latest, v1..v4 older
    got = {d["version_id"] for d in ap.select_prunable(vs, {"type": "count", "count": 2}, now)}
    assert got == {"v2", "v3", "v4"}   # keep 2 newest (v0 latest + v1), drop the rest

def test_keep_all_deletes_nothing():
    now = time.time()
    vs = [_v("media/j/a", "old", False, 999, now)]
    assert ap.select_prunable(vs, {"type": "keep_all"}, now) == []

def test_prune_scope_guard_rejects_foreign_key():
    now = time.time()
    vs = [_v("media/OTHER/x", "v", False, 999, now)]
    import pytest
    with pytest.raises(ap.PruneScopeError):
        ap.prune("j", {"type": "days", "days": 1}, bucket="b", now=now,
                 runner=None, _versions=vs, _deleter=lambda *a, **k: None)
```

- [ ] **Step 2: Run — expect FAIL**.

- [ ] **Step 3: Implement `app/engine/archive_prune.py`**:

```python
from __future__ import annotations
import argparse, subprocess, sys, time
from app.engine import s3

_DAY = 86400

class PruneScopeError(Exception):
    """A prunable version pointed outside the job's own media/<job>/ prefix."""

def _prefix(job: str) -> str:
    return f"media/{job}/"

def select_prunable(versions, policy, now):
    t = policy.get("type")
    if t == "keep_all":
        return []
    by_key = {}
    for v in versions:
        by_key.setdefault(v["key"], []).append(v)
    out = []
    for key, vs in by_key.items():
        vs = sorted(vs, key=lambda v: v["last_modified"], reverse=True)  # newest first
        noncurrent = [v for v in vs if not v["is_latest"]]
        if t == "days":
            cutoff = now - policy["days"] * _DAY
            out += [v for v in noncurrent if v["last_modified"] < cutoff]
        elif t == "count":
            # keep the N most recent versions total (latest + newest noncurrent), drop the rest
            out += vs[policy["count"]:]
    return [{"key": v["key"], "version_id": v["version_id"]} for v in out]

def prune(job, policy, *, bucket, now=None, runner=subprocess.run, _versions=None, _deleter=None) -> int:
    now = now if now is not None else time.time()
    prefix = _prefix(job)
    versions = _versions if _versions is not None else s3.list_versions(bucket, prefix, runner=runner)
    targets = select_prunable(versions, policy, now)
    deleter = _deleter or (lambda k, v: s3.delete_version(bucket, k, v, runner=runner))
    n = 0
    for t in targets:
        if not t["key"].startswith(prefix) or ".." in t["key"].split("/"):
            raise PruneScopeError(f"refusing to delete outside {prefix!r}: {t['key']!r}")
        deleter(t["key"], t["version_id"]); n += 1
    return n

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="archive_prune")
    ap.add_argument("job"); ap.add_argument("--type", required=True)
    ap.add_argument("--days", type=int, default=0); ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--bucket", default=None)
    a = ap.parse_args(argv)
    import os
    bucket = a.bucket or os.environ.get("S3_BUCKET")
    policy = {"type": a.type}
    if a.type == "days": policy["days"] = a.days
    elif a.type == "count": policy["count"] = a.count
    n = prune(a.job, policy, bucket=bucket)
    print(f"archive prune '{a.job}': removed {n} old version(s)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run — expect PASS**. **Step 5: Commit** `feat(retention): client-side archive S3 version prune (age/count/keep_all)`.

---

### Task 5: versioned-files prune — count + keep_all

**Files:**
- Modify: `app/engine/vfiles.py` (prune step), `app/engine/catalog.py` (a count-based selector)
- Test: `tests/engine/test_vfiles.py`, `tests/engine/test_catalog.py`

**Interfaces:**
- Consumes: the job's policy, threaded to `vfiles.backup(...)` (today it takes `retention_days`).
- Produces: `catalog.prunable_beyond_count(conn, keep_n) -> list[Row]` (non-current versions beyond the newest `keep_n` per path); `backup()` accepts `policy: dict` and prunes by it.

- [ ] **Step 1: Read the current invocation** — open `app/engine/vfiles.py` `__main__`/`backup()` and note how `retention_days` reaches it (env `JOB_RETENTION_DAYS` or arg). The new code threads `JOB_RETENTION_TYPE`/`_DAYS`/`_COUNT` the same way.

- [ ] **Step 2: Write failing tests** for `catalog.prunable_beyond_count` (keeps newest N non-tombstone versions per path, returns the rest) and a `vfiles` backup where `policy={"type":"count","count":2}` deletes the 3rd-oldest S3 key while keeping the 2 newest; and `policy={"type":"keep_all"}` deletes nothing. (Model on the existing `test_vfiles.py` prune tests — reuse its fake `s3`/catalog fixtures.)

- [ ] **Step 3: Implement** — add `prunable_beyond_count` in `catalog.py`:
```python
def prunable_beyond_count(conn, keep_n):
    """Non-current, non-tombstone versions beyond the newest `keep_n` per path."""
    rows = conn.execute("SELECT * FROM versions WHERE deleted = 0 ORDER BY path, uploaded_at DESC").fetchall()
    out, seen = [], {}
    for r in rows:
        seen[r["path"]] = seen.get(r["path"], 0) + 1
        if seen[r["path"]] > keep_n and r["is_current"] == 0:
            out.append(r)
    return out
```
In `vfiles.backup()`, branch the prune on `policy["type"]`: `days` → today's `catalog.prunable(conn, before)`; `count` → `catalog.prunable_beyond_count(conn, policy["count"])`; `keep_all` → skip. Delete each row's S3 key with the same scope guard as today, then `delete_version`… (vfiles uses distinct keys, so `s3.delete(key)` as today — NOT version ids).

- [ ] **Step 4: Run — expect PASS** (`pytest tests/engine/test_vfiles.py tests/engine/test_catalog.py -q`).
- [ ] **Step 5: Commit** `feat(retention): versioned-files prune supports count + keep_all`.

---

### Task 6: Wire the policy into the backup runner (`backup-job.sh`)

**Files:**
- Modify: `scripts/backup-job.sh` (`_run_versioned` forget mapping; `_run_archive` prune call; `_run_vfiles` pass policy)
- Test: `tests/unit/*.bats` (the restic-mapping/dispatch bats suite)

**Interfaces:** consumes `JOB_RETENTION_TYPE`/`JOB_RETENTION_DAYS`/`JOB_RETENTION_COUNT`/`JOB_KEEP_*` (Task 2); calls `python3 -m app.engine.archive_prune` (Task 4).

- [ ] **Step 1: Write failing bats** (mock `restic`/`python3` on PATH capturing args): a `days` versioned job calls `restic … forget --prune --keep-within 30d`; a `count` job → `--keep-last 5`; `keep_all` → no `forget`; an archive `days` job runs `app.engine.archive_prune … --type days --days 30`; `keep_all` archive → no prune call. (Follow the existing unit-bats mocking pattern.)

- [ ] **Step 2: Run — expect FAIL**.

- [ ] **Step 3: Implement** — in `_run_versioned`, replace the fixed `--keep-*` forget with:
```bash
  local forget_args=()
  case "$JOB_RETENTION_TYPE" in
    keep_all) : ;;                                             # no forget
    days)  forget_args=(--keep-within "${JOB_RETENTION_DAYS}d") ;;
    count) forget_args=(--keep-last "$JOB_RETENTION_COUNT") ;;
    *)     forget_args=(--keep-last "$JOB_KEEP_LAST" --keep-daily "$JOB_KEEP_DAILY" \
                        --keep-weekly "$JOB_KEEP_WEEKLY" --keep-monthly "$JOB_KEEP_MONTHLY") ;;
  esac
  if [ "$JOB_RETENTION_TYPE" != keep_all ]; then
    case "$JOB_STORAGE_CLASS" in
      GLACIER|DEEP_ARCHIVE|GLACIER_IR) log_warn "job '$JOB' class $JOB_STORAGE_CLASS is cold; deferring prune" ;;
      *) restic -r "$RESTIC_REPOSITORY" "${class_opt[@]}" forget --prune --tag "$JOB" "${forget_args[@]}" \
           || log_warn "restic forget/prune for '$JOB' reported an error (non-fatal)" ;;
    esac
  fi
```
In `_run_archive`, after the `rclone check` line, add:
```bash
  if [ "$JOB_RETENTION_TYPE" != keep_all ]; then
    python3 -m app.engine.archive_prune "$JOB" --type "$JOB_RETENTION_TYPE" \
      --days "${JOB_RETENTION_DAYS:-0}" --count "${JOB_RETENTION_COUNT:-1}" \
      || log_warn "archive prune for '$JOB' reported an error (non-fatal)"
  fi
```
In `_run_vfiles`, ensure `JOB_RETENTION_TYPE/_DAYS/_COUNT` are exported to the python child (they're already emitted into the sourced env; confirm `_run_vfiles` runs `python3 -m app.engine.vfiles` with that env).

- [ ] **Step 4: Run — expect PASS** (`bats tests/unit/`). **Step 5: Commit** `feat(retention): map per-job policy to restic forget + archive prune in runner`.

---

### Task 7: IAM — version list/delete permissions

**Files:**
- Modify: `provisioning/iam-policy.json.tmpl`
- Test: `tests/unit/test_iam_policy.bats` (or a JSON-shape pytest) — assert the two actions present.

- [ ] **Step 1: Write failing test** asserting the rendered/committed policy contains `s3:ListBucketVersions` (in `ListBucketScoped`) and `s3:DeleteObjectVersion` (in `ObjectRW`).
- [ ] **Step 2: Run — expect FAIL**.
- [ ] **Step 3: Implement** — add `"s3:ListBucketVersions"` to the `ListBucketScoped` `Action` array and `"s3:DeleteObjectVersion"` to the `ObjectRW` `Action` array.
- [ ] **Step 4: Run — expect PASS**. **Step 5: Commit** `feat(iam): grant ListBucketVersions + DeleteObjectVersion for archive prune`.

---

### Task 8: Cost model — count + keep_all versioning

**Files:**
- Modify: `app/estimator/model.py` (`JobInputs` gains `retention_type`, `retention_count`; `versioning_monthly`/`project` branch), `app/gui/estimate_io.py` (map policy → these fields), `app/estimator/cli.py` if needed
- Test: `tests/estimator/test_model.py`, `tests/gui/test_estimate_io.py`

**Interfaces:** `JobInputs.retention_type: str = "days"`, `JobInputs.retention_count: int = 0`. `versioning_monthly` uses count when `retention_type=="count"`, unbounded growth when `"keep_all"`.

- [ ] **Step 1: Write failing tests** — `versioning_monthly` for a `count` job ≈ `size × change% × count × rate` (bounded by N versions, independent of retention days); a `keep_all` job's projection grows across months (no plateau). (Reuse the `prices`/`J()` helpers.)
- [ ] **Step 2: Run — expect FAIL**.
- [ ] **Step 3: Implement** — extend `JobInputs`; in `versioning_monthly`, when `retention_type=="count"`, `noncurrent_gb = size × change% × count`; when `"keep_all"`, use the full projection horizon (grows). Thread `retention_type/count` through `estimate_io._job_inputs` from the job's `retention` policy.
- [ ] **Step 4: Run — expect PASS** (`pytest tests/estimator tests/gui -q`, minus the known pre-existing `test_billing.py::test_forecast_parses`). **Step 5: Commit** `feat(cost): model count + keep_all retention shapes`.

---

### Task 9: Wizard — retention-policy selector

**Files:**
- Modify: `app/gui/templates/job_form.html` (§4 Storage & retention), `app/gui/static/app.js` (show the field for the chosen policy type), `app/gui/estimate_io.py`/`routes.py` if the wizard param plumbing needs the new fields
- Test: `tests/gui/test_jobs_routes.py`, `tests/gui/test_jobs_estimate_routes.py`

**Interfaces:** form posts `retention_type` + `retention_days`/`retention_count` (+ existing `keep_*` for tiered). `jobs_io.validate` (Task 1) already accepts a `retention` object — the route assembles it from these params.

- [ ] **Step 1: Write failing test** — GET `/jobs/new` renders a `name="retention_type"` control with options `keep_all/days/count` (+ `tiered` present but `data-when-type="versioned"`); POST a `count` archive job round-trips to `jobs.json` as `retention:{type:count,count:N}`.
- [ ] **Step 2: Run — expect FAIL**.
- [ ] **Step 3: Implement** — replace the three separate retention controls in §4 with the policy selector from spec §7 (radio/select `retention_type` + a `retention_days` and `retention_count` input + the existing keep fieldset for `tiered`, `data-when-type="versioned"`). In `routes.py` job-save, build `job["retention"]` from the posted `retention_type` + the matching field before calling `jobs_io.upsert`. Add a small `app.js` handler to show only the active policy's field.
- [ ] **Step 4: Run — expect PASS** (`pytest tests/gui -q`). **Step 5: Commit** `feat(retention): wizard retention-policy selector`.

---

### Task 10: Docs

**Files:** Modify `README.md` (retention policies per job type; note the two added IAM actions; note the baseline lifecycle is the outer bound — Phase 2 will add the Settings screen).

- [ ] **Step 1**: Update the README "Configure"/job-types and IAM sections. **Step 2**: `git diff README.md` reads well. **Step 3**: `git commit -am "docs(retention): per-job retention policies + IAM note"`.

---

## Self-Review

**Spec coverage:** §4 policy shapes → Task 1/2. §5 enforcement: restic → Task 6; versioned-files → Task 5; archive → Tasks 3+4+6. §6 IAM → Task 7 (both actions). §7 wizard → Task 9. §9 cost → Task 8. §4 migration → Task 1. Phase-2 items (§8 lifecycle Settings, provisioning tunables) are intentionally out of this plan. Covered.

**Placeholder scan:** Task 5 Step 1 is a deliberate "read the current invocation" (the one spot whose exact current wiring wasn't captured); Task 9's app.js handler is described with its behavior. No "add error handling"/"TBD". Real code + expected values throughout.

**Type consistency:** `retention` object shape identical across Tasks 1/2/4/5/8/9. `JOB_RETENTION_TYPE/_DAYS/_COUNT/_KEEP_*` identical in Tasks 2/6 and vfiles. `select_prunable`/`prune`/`PruneScopeError` (Task 4) match their tests. `list_versions`/`delete_version` signatures match Tasks 3↔4. `JobInputs.retention_type/retention_count` consistent Tasks 8↔(estimate_io).
