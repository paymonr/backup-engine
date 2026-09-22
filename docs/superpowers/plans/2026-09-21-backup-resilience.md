# Backup Resilience / State Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backups survive interruption — bounded retry that resumes, auto-resume after a restart (a toggle, default on), and pause/resume of a running run — with clear run states.

**Architecture:** No new daemon. Extend `scripts/backup-job.sh` (retry loop + pause control-flag), `app/engine/runs.py` (a `paused` outcome + `attempts` + derived states + a boot auto-resume trigger), the entrypoint (call auto-resume after `runs boot`), and the GUI (stop/resume routes, job-page control, progress states, settings toggle). Resume relies on the engines' native incremental (restic dedup / rclone skip / vfiles catalog) — re-running a command continues where it left off.

**Tech Stack:** bash runner (restic/rclone/aws-cli), Python 3 (Flask GUI, `app/engine/*`), supercronic, Jinja2, vanilla JS, pytest + bats.

**Spec:** `docs/superpowers/specs/2026-09-21-backup-resilience-design.md`

## Global Constraints

- **No new checkpoint format** — resume = re-run the engine command; restic/rclone/vfiles resume natively. Never re-upload wholesale.
- **Retry is transient-only:** transient (HTTP 5xx, `RequestTimeout`, `SlowDown`, connection reset/EOF, `timeout`) → retry; anything else (incl. `AccessDenied`, `InvalidAccessKeyId`, `NoSuchBucket`, signature/permission, validation) → fail fast. Unknown = permanent.
- **A paused run is a clean terminal state** (`outcome: paused`), never an alert, never retried, never auto-resumed.
- **Defaults:** `AUTO_RESUME_ON_BOOT=true`, `BE_MAX_ATTEMPTS=3`, `BE_RETRY_BASE_SECONDS=30`, `BE_MAX_RESUMES=3`. Backoff sleep must be injectable (env override) so tests run fast.
- **Naming:** "Pause schedule" = the existing per-job `enabled` toggle (unchanged). "Pause / Resume" = the NEW control for a running run.
- **Estimator FROZEN** — do not touch `app/estimator/*` (hash-guard `test_untouched.py`).
- **AWS via subprocess/no boto3.** Commit per task. Do NOT deploy.

---

### Task 1: Config plumbing (resilience settings)

**Files:**
- Modify: `app/gui/config_io.py` (add read helpers after `base_bucket_versioned`)
- Modify: `config/backup.env.example`
- Test: `tests/gui/test_config_io.py`

**Interfaces:**
- Produces: `config_io.auto_resume_on_boot(config_dir) -> bool` (default True); `config_io.retry_settings(config_dir) -> dict` = `{"max_attempts": int(3), "base_seconds": int(30), "max_resumes": int(3)}` (parsed from backup.env with those defaults).

- [ ] **Step 1: Write failing tests**
```python
# tests/gui/test_config_io.py
from app.gui import config_io
def test_auto_resume_defaults_true(tmp_path):
    (tmp_path/"backup.env").write_text("S3_BUCKET=b\nAWS_REGION=us-east-1\n")
    assert config_io.auto_resume_on_boot(str(tmp_path)) is True
    assert config_io.retry_settings(str(tmp_path)) == {"max_attempts":3,"base_seconds":30,"max_resumes":3}
def test_auto_resume_and_retry_overrides(tmp_path):
    (tmp_path/"backup.env").write_text(
        "S3_BUCKET=b\nAUTO_RESUME_ON_BOOT=false\nBE_MAX_ATTEMPTS=5\nBE_RETRY_BASE_SECONDS=10\nBE_MAX_RESUMES=2\n")
    assert config_io.auto_resume_on_boot(str(tmp_path)) is False
    assert config_io.retry_settings(str(tmp_path)) == {"max_attempts":5,"base_seconds":10,"max_resumes":2}
```
- [ ] **Step 2: Run → FAIL** (`python3 -m pytest tests/gui/test_config_io.py -k "auto_resume or retry" -v`)
- [ ] **Step 3: Implement**
```python
# app/gui/config_io.py
def auto_resume_on_boot(config_dir: str) -> bool:
    return read_backup_env(config_dir).get("AUTO_RESUME_ON_BOOT", "true").strip().lower() != "false"

def _int_env(config_dir, key, default):
    try: return int(read_backup_env(config_dir).get(key, "").strip() or default)
    except ValueError: return default

def retry_settings(config_dir: str) -> dict:
    return {"max_attempts": _int_env(config_dir, "BE_MAX_ATTEMPTS", 3),
            "base_seconds": _int_env(config_dir, "BE_RETRY_BASE_SECONDS", 30),
            "max_resumes": _int_env(config_dir, "BE_MAX_RESUMES", 3)}
```
Add to `config/backup.env.example`:
```
# Resilience (safe defaults; blank = default)
AUTO_RESUME_ON_BOOT=true
BE_MAX_ATTEMPTS=3
BE_RETRY_BASE_SECONDS=30
BE_MAX_RESUMES=3
```
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/config_io.py config/backup.env.example tests/gui/test_config_io.py && git commit -m "feat(config): resilience settings (auto-resume toggle + retry knobs)"`

---

### Task 2: Transient-vs-permanent error classifier (bash)

**Files:**
- Modify: `scripts/lib/common.sh` (add `_is_transient_error`)
- Test: `tests/bats/error_class.bats` (new)

**Interfaces:**
- Produces: `_is_transient_error <logfile>` → exit 0 if the log looks transient (retryable), non-zero otherwise. Conservative: matches transient patterns; everything else (incl. AccessDenied) is permanent.

- [ ] **Step 1: Write failing bats test**
```bash
# tests/bats/error_class.bats
load test_helper
setup() { source "$BATS_TEST_DIRNAME/../../scripts/lib/common.sh"; LOG="$BATS_TEST_TMPDIR/l"; }
@test "5xx / timeout / slowdown / reset are transient" {
  for m in "RequestTimeout: your socket connection" "http status 503 SlowDown" \
           "connection reset by peer" "net/http: TLS handshake timeout" "unexpected EOF"; do
    printf '%s\n' "$m" >"$LOG"; run _is_transient_error "$LOG"; [ "$status" -eq 0 ]
  done
}
@test "AccessDenied / NoSuchBucket / generic are permanent" {
  for m in "AccessDenied: not authorized" "NoSuchBucket" "SignatureDoesNotMatch" "some random failure"; do
    printf '%s\n' "$m" >"$LOG"; run _is_transient_error "$LOG"; [ "$status" -ne 0 ]
  done
}
```
- [ ] **Step 2: Run → FAIL** (`bats tests/bats/error_class.bats`)
- [ ] **Step 3: Implement**
```bash
# scripts/lib/common.sh
_is_transient_error() {  # exit 0 = retryable
  grep -qiE '(^|[^a-z])(50[0-9]|SlowDown|RequestTimeout|RequestTimeTooSkewed|Throttl|connection reset|connection refused|broken pipe|unexpected EOF|TLS handshake timeout|i/o timeout|timeout|temporarily unavailable|ServiceUnavailable|InternalError)([^a-z]|$)' "$1" 2>/dev/null
}
```
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add scripts/lib/common.sh tests/bats/error_class.bats && git commit -m "feat(runner): transient-vs-permanent error classifier for retry"`

---

### Task 3: Bounded retry-with-resume in the runner

**Files:**
- Modify: `scripts/backup-job.sh` (`_run_versioned` restic call; `_run_archive` rclone call)
- Test: `tests/bats/backup-job.bats`

**Interfaces:**
- Consumes: `_is_transient_error` (Task 2); env `BE_MAX_ATTEMPTS`/`BE_RETRY_BASE_SECONDS` (Task 1 exports them via jobs env/config — read with defaults 3/30); `BE_SLEEP_CMD` (default `sleep`, overridable in tests).
- Produces: retries the engine command on transient failure; sets `BE_ATTEMPTS` (int) for the run record (Task 6 reads it); on exhaustion `_fail`s with the real error.

- [ ] **Step 1: Write failing bats tests** (the harness stubs `restic`/`rclone` and logs `$*`)
```bash
# add to tests/bats/backup-job.bats
@test "versioned backup retries a transient failure then succeeds (resumes, same command)" {
  # restic backup fails once with a 503, then succeeds; cat->init as usual
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
if [[ "$*" == *"backup"* ]]; then
  n=$(grep -c backup "$RESTIC_LOG")
  if [ "$n" -eq 1 ]; then echo "http status 503 SlowDown" ; exit 1; fi
fi
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job cfg
  [ "$status" -eq 0 ]
  [ "$(grep -c 'backup .*--tag cfg' "$RESTIC_LOG")" -ge 2 ]   # retried
  grep -q '"outcome":"success"' "$CACHE_DIR/state/cfg.json"
}
@test "versioned backup does NOT retry a permanent (AccessDenied) failure" {
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
[[ "$*" == *"backup"* ]] && { echo "AccessDenied: not authorized"; exit 1; }
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  BE_RETRY_BASE_SECONDS=0 BE_SLEEP_CMD=true run_job cfg
  [ "$status" -ne 0 ]
  [ "$(grep -c 'backup .*--tag cfg' "$RESTIC_LOG")" -eq 1 ]   # NOT retried
  grep -q '"outcome":"failed"' "$CACHE_DIR/state/cfg.runs.jsonl"
}
```
- [ ] **Step 2: Run → FAIL** (`bats tests/bats/backup-job.bats -f retry` / permanent)
- [ ] **Step 3: Implement** — add a retry helper and use it around the restic backup (and the rclone `_run_archive` call). Read attempts/base once at top of `main` or in these functions:
```bash
# scripts/backup-job.sh  (helper, near the top-level helpers)
: "${BE_MAX_ATTEMPTS:=3}"; : "${BE_RETRY_BASE_SECONDS:=30}"; : "${BE_SLEEP_CMD:=sleep}"
# _retry ATTEMPT_LOG CMD...  — runs CMD; on failure, if the log looks transient and we have
# attempts left, backs off and re-runs (the engine resumes). Sets BE_ATTEMPTS. Returns CMD's rc.
_retry() {
  local log="$1"; shift
  local n=0 rc=0
  while :; do
    n=$((n+1)); BE_ATTEMPTS="$n"
    : >"$log"
    "$@" 2>&1 | tee -a "$log" >/dev/null; rc=${PIPESTATUS[0]}
    [ "$rc" -eq 0 ] && return 0
    if [ "$n" -ge "$BE_MAX_ATTEMPTS" ] || ! _is_transient_error "$log"; then return "$rc"; fi
    log_warn "job '$JOB' attempt $n failed (transient); retrying (resumes where it left off)"
    "$BE_SLEEP_CMD" "$(( BE_RETRY_BASE_SECONDS * (1 << (n-1)) ))"
  done
}
```
In `_run_versioned`, replace the `if restic ... backup ... | tee "$f"; then` block so the backup runs through `_retry` (writing the --json stream to `$f`); keep the summary parse on success and `_fail "restic backup failed for '$JOB'"` on the returned rc. In `_run_archive`, wrap the `rclone "${args[@]}"` call in `_retry` too. (The prune step keeps the Task-from-earlier stale-lock self-heal.)
> The restic `--json` stream must still land in `$f` for the progress monitor; have the retried command be the full `restic … backup … --json` and `tee "$f"` inside `_retry`'s invocation (pass a wrapper function).
- [ ] **Step 4: Run → PASS** (both new tests + the existing versioned/archive tests)
- [ ] **Step 5: Commit** `git add scripts/backup-job.sh tests/bats/backup-job.bats && git commit -m "feat(runner): bounded retry-with-resume on transient failures"`

---

### Task 4: `paused` outcome — runner honours a stop flag

**Files:**
- Modify: `scripts/backup-job.sh` (signal trap + control-flag check)
- Test: `tests/bats/backup-job.bats`

**Interfaces:**
- Consumes: control flag `$CACHE_DIR/state/$JOB.control` containing `pause`.
- Produces: when the run receives SIGINT/SIGTERM AND the control flag says `pause`, the run ends with `runs_end paused ...` (outcome `paused`) and does NOT retry or alert; the flag is cleared on exit.

- [ ] **Step 1: Write failing bats test**
```bash
@test "a stop flag + SIGTERM records outcome paused, not failed, and does not retry" {
  # restic backup blocks until signalled; the control flag is pre-set to 'pause'
  cat >"$BATS_TEST_TMPDIR/bin/restic" <<'EOF'
#!/usr/bin/env bash
printf "%s\n" "$*" >>"$RESTIC_LOG"
[ "$1" = "cat" ] && exit 1
[[ "$*" == *"backup"* ]] && { trap 'exit 130' TERM INT; while :; do sleep 0.2; done; }
exit 0
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/restic"
  printf 'echo JOB_NAME=cfg; echo JOB_TYPE=versioned; echo JOB_SOURCE=appdata; echo JOB_STORAGE_CLASS=STANDARD; echo JOB_RETENTION_TYPE=keep_all\n' >"$JOBS_IO_STUB"
  mkdir -p "$CACHE_DIR/state"; echo pause >"$CACHE_DIR/state/cfg.control"
  ( sleep 1; pkill -TERM -f "backup-job.sh cfg" ) &
  BE_SLEEP_CMD=true run_job cfg
  grep -q '"outcome":"paused"' "$CACHE_DIR/state/cfg.runs.jsonl"
  ! grep -q '"outcome":"failed"' "$CACHE_DIR/state/cfg.runs.jsonl"
  [ ! -f "$CACHE_DIR/state/cfg.control" ]   # flag cleared
}
```
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — in `backup-job.sh`, set `BE_CONTROL="$CACHE_DIR/state/$JOB.control"` after the job is known; change the TERM/INT traps so that, if `$BE_CONTROL` contains `pause`, the exit handler records `runs_end paused 0 "paused by request"` (skipping `_record_failure`/notify) and removes the flag; otherwise keep today's behavior. Ensure `_retry` treats a paused stop as terminal (do not retry): check the flag before a retry and break to the paused path. Guard `_usb_exit_trap`/`runs_exit_trap` so a `paused` exit isn't recorded as failed.
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add scripts/backup-job.sh tests/bats/backup-job.bats && git commit -m "feat(runner): graceful pause -> outcome 'paused' (no retry, no alert)"`

---

### Task 5: `paused` + `attempts` in the run reader

**Files:**
- Modify: `app/engine/runs.py` (`RunRecord`, `_fold_group`, `_NUM_FIELDS`)
- Test: `tests/engine/test_runs.py`

**Interfaces:**
- Consumes: run-record `end` events with `outcome:"paused"` and/or `attempts:N` (written by Tasks 3–4).
- Produces: `RunRecord.outcome` can be `"paused"`; `RunRecord.attempts: int | None`; `is_paused`/`attempts` available to the GUI. `read_runs`/`_fold_group` pass `paused` through unchanged (not reconciled to aborted).

- [ ] **Step 1: Write failing test**
```python
# tests/engine/test_runs.py
def test_paused_outcome_and_attempts_fold(tmp_path):
    from app.engine import runs
    c = str(tmp_path)
    runs.append_event(c, "cfg", {"v":1,"id":"20260921T050000Z-aaaa","job":"cfg","kind":"backup","event":"start","started_at":"2026-09-21T05:00:00Z"})
    runs.append_event(c, "cfg", {"v":1,"id":"20260921T050000Z-aaaa","job":"cfg","kind":"backup","event":"end","outcome":"paused","finished_at":"2026-09-21T05:01:00Z","attempts":2})
    rec = runs.read_runs(c, "cfg", reconcile=False).records[0]
    assert rec.outcome == "paused" and rec.attempts == 2
```
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — add `attempts: int | None = None` to `RunRecord`; add `"attempts"` to `_NUM_FIELDS`; set `attempts=num("attempts")` in `_fold_group`; ensure `_fold_group`'s `outcome = merged.get("outcome") or "ok"` already carries `paused` through (it does). Confirm `reconcile` never rewrites a `paused` run (it only targets `running`).
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/engine/runs.py tests/engine/test_runs.py && git commit -m "feat(runs): paused outcome + attempts on RunRecord"`

---

### Task 6: Auto-resume interrupted runs on boot

**Files:**
- Create: `app/engine/resume.py`
- Modify: `scripts/entrypoint.sh` (call after `runs boot`)
- Test: `tests/engine/test_resume.py`

**Interfaces:**
- Consumes: `runs.read_runs`/`runs.is_locked`, `config_io.auto_resume_on_boot`/`retry_settings`, `jobs_io.load`, and a `trigger` callable (default `runner.trigger_job`).
- Produces: `resume.resume_interrupted(cfg, *, trigger=None, log=print) -> int` — count of jobs re-triggered. Re-triggers a job iff: auto-resume on; job enabled; its latest run is `aborted` (interrupted, NOT `paused`/`ok`/`failed`); lock free; and its resume count `< max_resumes`. Increments a resume counter (`state/<job>.resumes`) and re-triggers with `BE_RESUME=1` in the env.

- [ ] **Step 1: Write failing tests** (stub `trigger`, seed run records)
```python
# tests/engine/test_resume.py
from app.engine import resume, runs
from app.gui import config_io, jobs_io  # adjust imports as needed
def _seed(cache, job, outcome):
    runs.append_event(cache,job,{"v":1,"id":"20260921T050000Z-aaaa","job":job,"kind":"backup","event":"start","started_at":"2026-09-21T05:00:00Z"})
    if outcome != "running":
        runs.append_event(cache,job,{"v":1,"id":"20260921T050000Z-aaaa","job":job,"kind":"backup","event":"end","outcome":outcome,"finished_at":"2026-09-21T05:01:00Z"})
def test_resumes_only_aborted_enabled_when_setting_on(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name":"a","enabled":True},{"name":"b","enabled":True},{"name":"c","enabled":False}], auto_resume=True)
    _seed(cfg["CACHE_DIR"],"a","aborted"); _seed(cfg["CACHE_DIR"],"b","ok"); _seed(cfg["CACHE_DIR"],"c","aborted")
    fired=[]; n = resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name))
    assert fired == ["a"] and n == 1        # b ok, c disabled
def test_paused_is_never_resumed(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name":"a","enabled":True}], auto_resume=True)
    _seed(cfg["CACHE_DIR"],"a","paused")
    fired=[]; resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name)); assert fired == []
def test_off_setting_resumes_nothing(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name":"a","enabled":True}], auto_resume=False)
    _seed(cfg["CACHE_DIR"],"a","aborted")
    fired=[]; resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name)); assert fired == []
def test_resume_cap_stops_the_loop(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name":"a","enabled":True}], auto_resume=True, max_resumes=1)
    _seed(cfg["CACHE_DIR"],"a","aborted")
    t=lambda name, env=None: None
    assert resume.resume_interrupted(cfg, trigger=t) == 1
    _seed(cfg["CACHE_DIR"],"a","aborted")   # still aborted after the resume attempt
    assert resume.resume_interrupted(cfg, trigger=t) == 0   # cap reached
```
(Write a small `_make_cfg` helper that creates config/cache dirs + backup.env with the AUTO_RESUME/BE_MAX_RESUMES values + jobs.json.)
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement `app/engine/resume.py`**
```python
from __future__ import annotations
from pathlib import Path
from . import runs
from ..gui import config_io, jobs_io, runner

def _resume_count(cache, job) -> int:
    p = Path(cache, "state", f"{job}.resumes")
    try: return int(p.read_text().strip() or 0)
    except (OSError, ValueError): return 0

def _bump_resume(cache, job) -> None:
    p = Path(cache, "state", f"{job}.resumes"); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(_resume_count(cache, job) + 1))

def resume_interrupted(cfg, *, trigger=None, log=print) -> int:
    config_dir, cache = cfg["CONFIG_DIR"], cfg["CACHE_DIR"]
    if not config_io.auto_resume_on_boot(config_dir):
        return 0
    trigger = trigger or (lambda name, env=None: runner.trigger_job(cfg["SCRIPTS_DIR"], name, env=env))
    max_resumes = config_io.retry_settings(config_dir)["max_resumes"]
    n = 0
    for j in jobs_io.load(config_dir):
        name = j.get("name")
        if not name or not j.get("enabled", True):
            continue
        recs = runs.read_runs(cache, name, kinds=runs.BACKUP_KINDS, reconcile=False).records
        if not recs or recs[0].outcome != "aborted":
            continue
        if runs.is_locked(cache, name) or _resume_count(cache, name) >= max_resumes:
            continue
        _bump_resume(cache, name)
        import os; env = os.environ.copy(); env["BE_RESUME"] = "1"
        trigger(name, env=env); n += 1
        log(f"resume: re-triggered interrupted job '{name}'")
    return n
```
Add a CLI: `python3 -m app.engine.resume` calls `resume_interrupted(_cfg_from_env())` (mirror `sysop`'s cfg-from-env). A successful (`ok`) run should reset the counter — do that in the runner's success path (`rm -f state/<job>.resumes`) as part of this task's backup-job.sh edit, and cover it in a bats assertion.
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/engine/resume.py scripts/entrypoint.sh scripts/backup-job.sh tests/engine/test_resume.py && git commit -m "feat(resume): auto-resume aborted runs on boot (toggle + cap)"`

---

### Task 7: Entrypoint calls auto-resume after boot reconcile

**Files:**
- Modify: `scripts/entrypoint.sh`
- Test: `tests/bats/entrypoint.bats`

**Interfaces:**
- Consumes: `python3 -m app.engine.resume` (Task 6).

- [ ] **Step 1: Write failing bats test** — assert that with a stubbed `python3` recording module args, the entrypoint invokes `app.engine.resume` after `app.engine.runs boot` (order matters). Follow the existing entrypoint.bats stubbing of `python3`/`supercronic`.
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — after the existing `python3 -m app.engine.runs boot` line, add:
```bash
CONFIG_DIR="${CONFIG_DIR:-/config}" python3 -m app.engine.resume \
  || log_warn "auto-resume on boot failed (non-fatal)"
```
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add scripts/entrypoint.sh tests/bats/entrypoint.bats && git commit -m "feat(entrypoint): run auto-resume after boot reconcile"`

---

### Task 8: Stop / Resume routes for a running run

**Files:**
- Modify: `app/gui/routes.py` (near `job_pause`/`job_resume`, ~line 247)
- Test: `tests/gui/test_job_page_routes.py`

**Interfaces:**
- Consumes: `runs.active_run` (the running record + its `pid`), `runner.trigger_job`.
- Produces: `POST /jobs/<name>/stop` (CSRF) — writes `state/<name>.control=pause` and, if there's an active run with a pid, `os.kill(pid, SIGTERM)`; flash + redirect. `POST /jobs/<name>/resume-run` (CSRF) — clears the flag and `runner.trigger_job`; flash + redirect. Both 404 unknown jobs.

- [ ] **Step 1: Write failing tests** (monkeypatch `os.kill` + `runner.trigger_job`)
```python
def test_stop_writes_control_flag_and_signals(client, app, monkeypatch):
    _seed(app, {"name":"cfg","type":"versioned","source":"appdata","schedule":"0 5 * * *","enabled":True,"storage_class":"STANDARD","retention":{"type":"keep_all"}})
    import app.engine.runs as runs, app.gui.routes as routes
    monkeypatch.setattr(runs, "active_run", lambda c,j: runs.RunRecord(id="x",job="cfg",kind="backup",trigger="manual",outcome="running",started_at=None,finished_at=None,duration_s=None,exit_code=None,error=None,pid=4242))
    killed=[]; monkeypatch.setattr(routes.os, "kill", lambda pid,sig: killed.append((pid,sig)))
    t=_csrf(client); r=client.post("/jobs/cfg/stop", data={"csrf":t})
    assert r.status_code in (302,303)
    import pathlib; assert pathlib.Path(app.config["CACHE_DIR"],"state","cfg.control").read_text().strip()=="pause"
    assert killed and killed[0][0]==4242
def test_resume_run_clears_flag_and_triggers(client, app, monkeypatch):
    _seed(app, {"name":"cfg","type":"versioned","source":"appdata","schedule":"0 5 * * *","enabled":True,"storage_class":"STANDARD","retention":{"type":"keep_all"}})
    import pathlib, app.gui.routes as routes
    p=pathlib.Path(app.config["CACHE_DIR"],"state"); p.mkdir(parents=True,exist_ok=True); (p/"cfg.control").write_text("pause")
    fired=[]; monkeypatch.setattr(routes.runner,"trigger_job",lambda sd,name,env=None: fired.append(name))
    t=_csrf(client); r=client.post("/jobs/cfg/resume-run", data={"csrf":t})
    assert r.status_code in (302,303) and fired==["cfg"] and not (p/"cfg.control").exists()
```
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** the two routes (CSRF via `security.verify_csrf`; 404 via `jobs_io.get`; write/clear `Path(cfg["CACHE_DIR"],"state",f"{name}.control")`; `import os, signal`; guard `os.kill` in try/except ProcessLookupError). Mirror the existing `_set_paused` structure.
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/routes.py tests/gui/test_job_page_routes.py && git commit -m "feat(gui): stop/resume routes for a running backup"`

---

### Task 9: Job-page "Pause / Resume" control + paused state

**Files:**
- Modify: `app/gui/templates/job.html` (actions area, ~line 113)
- Test: `tests/gui/test_job_page_routes.py`

**Interfaces:**
- Consumes: `s.state` (RUNNING) and the run outcome (`paused`) from the job-page context.

- [ ] **Step 1: Write failing test** — a RUNNING job's page shows a "Pause" button posting to `/jobs/<name>/stop`; a job whose latest run is `paused` shows a "Resume" button posting to `/jobs/<name>/resume-run`; both distinct from the existing "Pause schedule" toggle.
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — in `.actions`, when `s.state == 'RUNNING'` render a CSRF form POSTing to `/jobs/{{ s.name }}/stop` labeled "Pause"; when the latest run outcome is `paused` render a "Resume" form POSTing to `/jobs/{{ s.name }}/resume-run`. Leave the existing "Pause schedule" (enable/disable) menu item as-is.
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/templates/job.html tests/gui/test_job_page_routes.py && git commit -m "feat(gui): job-page Pause/Resume control for a running run"`

---

### Task 10: Progress bar shows retrying / resuming

**Files:**
- Modify: `app/engine/progress.py` (add state) and `app/gui/static/app.js`
- Test: `tests/engine/test_progress.py`

**Interfaces:**
- Consumes: the active run's `attempts` (Task 5) and the `BE_RESUME` marker; the existing `read_progress` (progress monitor).
- Produces: `read_progress(...)` includes `"state": "retrying"|"resuming"|"running"` and `"attempt"` when applicable; `app.js` shows "retry N/M" / "resuming" on the bar.

- [ ] **Step 1: Write failing test**
```python
def test_progress_reports_retrying(tmp_path, monkeypatch):
    from app.engine import progress, runs
    monkeypatch.setattr(runs, "active_run", lambda c,j: runs.RunRecord(id="x",job="cfg",kind="backup",trigger="manual",outcome="running",started_at=None,finished_at=None,duration_s=None,exit_code=None,error=None,attempts=2))
    out = progress.read_progress(str(tmp_path), "cfg", "versioned")
    assert out["running"] is True and out.get("attempt") == 2 and out.get("state") == "retrying"
```
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — in `read_progress`, when the active run's `attempts` (if the RunRecord exposes it on the running record) is > 1 set `state="retrying"`, `attempt=<n>`; when `BE_RESUME`/a resume marker is set, `state="resuming"`; else `state="running"`. In `app.js`, when the polled JSON has `state==="retrying"` show "retry N" and `state==="resuming"` show "resuming…" prefixed on the progress text.
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/engine/progress.py app/gui/static/app.js tests/engine/test_progress.py && git commit -m "feat(gui): progress bar shows retrying/resuming state"`

---

### Task 11: Settings toggle for auto-resume-on-boot

**Files:**
- Modify: `app/gui/templates/config.html`, `app/gui/routes.py` (the config POST handler)
- Test: `tests/gui/test_config_routes.py`

**Interfaces:**
- Consumes: `config_io.auto_resume_on_boot` / `config_io.write_backup_env`.

- [ ] **Step 1: Write failing test** — a real integration test: GET the settings page shows the toggle reflecting the current value; POST toggling it persists `AUTO_RESUME_ON_BOOT` via `config_io.read_backup_env`.
- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** — render a checkbox for `AUTO_RESUME_ON_BOOT` (checked when `auto_resume_on_boot()` is true) with a one-line explanation ("Automatically resume a backup that was interrupted by a restart"); the config POST maps the checkbox to `"true"`/`"false"` and persists via `write_backup_env`. Follow the existing config-route pattern (CSRF, redirect/flash).
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git add app/gui/templates/config.html app/gui/routes.py tests/gui/test_config_routes.py && git commit -m "feat(gui): settings toggle for auto-resume-on-boot"`

---

## Self-Review

**Spec coverage:** retry-with-resume (transient-only, bounded) → T2+T3; permanent fail-fast → T2+T3; pause a running run (graceful → `paused`) → T4+T5+T8+T9; auto-resume-on-boot (toggle default on + cap) → T1+T6+T7+T11; run states (paused/retrying/resuming) → T5+T9+T10; config/defaults → T1; naming ("Pause schedule" vs "Pause / Resume") → T9. No spec requirement is unmapped.

**Placeholder scan:** every code step carries real code; the few "follow the existing pattern" notes (T8/T9/T11) point at concrete existing handlers/templates and are integration reconnaissance, not deferred logic. The `_make_cfg` test helper (T6) is described with its exact contents to create.

**Type consistency:** `_is_transient_error <logfile> -> exit code` (T2) used by `_retry` (T3); `BE_ATTEMPTS`/`attempts` written by T3/T4 → `RunRecord.attempts` (T5) → `read_progress` (T10); `resume.resume_interrupted(cfg, *, trigger, log)` (T6) called by the entrypoint CLI (T7); control flag path `state/<job>.control` consistent across T4/T8/T9; `config_io.auto_resume_on_boot`/`retry_settings` (T1) consumed by T6/T11. Names line up.
