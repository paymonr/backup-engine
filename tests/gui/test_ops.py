import fcntl
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.gui import ops
from app.engine import runs


def _cfg(dirs, tmp_path):
    restore = Path(dirs["cache"], "restore"); restore.mkdir(exist_ok=True)
    source = Path(dirs["cache"], "source"); source.mkdir(exist_ok=True)
    return {
        "CACHE_DIR": dirs["cache"],
        "SCRIPTS_DIR": "/app/scripts",
        "RESTORE_ROOT": str(restore),
        "RESTORE_ROOT_HOST": "/mnt/user/restore",
        "SOURCE_ROOT": str(source),
        "SOURCE_ROOT_HOST": "/mnt/user",
    }


# --- launch: detached, pre-assigned id, env/flags -------------------------

def test_launch_passes_be_run_id_trigger_and_detach_flags(dirs, tmp_path, monkeypatch):
    calls = {}
    def fake_popen(cmd, **kw):
        calls["cmd"] = cmd; calls["kw"] = kw
        return object()
    monkeypatch.setattr(ops.subprocess, "Popen", fake_popen)
    cfg = _cfg(dirs, tmp_path)
    rid = ops.launch(cfg, ["/app/scripts/restore.sh", "appdata", "list"],
                     job="appdata", kind="restore", trigger="manual")
    assert runs.valid_run_id(rid)
    assert calls["cmd"] == ["bash", "/app/scripts/restore.sh", "appdata", "list"]
    kw = calls["kw"]
    assert kw["env"]["BE_RUN_ID"] == rid
    assert kw["env"]["BE_TRIGGER"] == "manual"
    assert kw["start_new_session"] is True
    assert kw["stdout"] is ops.subprocess.DEVNULL
    assert kw["stderr"] is ops.subprocess.DEVNULL


def test_launch_env_extra_merged(dirs, tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(ops.subprocess, "Popen", lambda cmd, **kw: calls.update(cmd=cmd, kw=kw) or object())
    cfg = _cfg(dirs, tmp_path)
    ops.launch(cfg, ["/x.sh"], job="appdata", kind="backup", env_extra={"BE_FOO": "bar"})
    assert calls["kw"]["env"]["BE_FOO"] == "bar"


def test_launch_returns_before_child_finishes_and_child_sees_preassigned_id(dirs, tmp_path):
    """Detachment: launch returns promptly (before the child's own sleep ends) and
    the child's first record already carries the pre-assigned BE_RUN_ID."""
    cfg = _cfg(dirs, tmp_path)
    out = tmp_path / "child.jsonl"
    script = tmp_path / "fake_runner.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'sleep 0.4\n'
        'printf \'{"id":"%s","event":"start"}\\n\' "$BE_RUN_ID" >> "$OUT"\n'
    )
    t0 = time.monotonic()
    rid = ops.launch(cfg, [str(script)], job="appdata", kind="backup",
                     env_extra={"OUT": str(out)})
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"launch blocked for {elapsed}s (not detached)"
    assert runs.valid_run_id(rid)
    for _ in range(60):
        if out.exists() and out.read_text().strip():
            break
        time.sleep(0.05)
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert rec["id"] == rid


def test_launch_py_runs_module_under_system(dirs, tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(ops.subprocess, "Popen", lambda cmd, **kw: calls.update(cmd=cmd, kw=kw) or object())
    cfg = _cfg(dirs, tmp_path)
    rid = ops.launch_py(cfg, "app.engine.sysop", ["usage-refresh"], kind="usage-refresh")
    assert calls["cmd"][1:] == ["-m", "app.engine.sysop", "usage-refresh"]
    assert calls["kw"]["env"]["BE_RUN_ID"] == rid
    assert Path(dirs["cache"], "logs", "runs", "_system").is_dir()


# --- run_sync: timeout -----------------------------------------------------

def test_run_sync_captures_output(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    cp = ops.run_sync(cfg, ["-c", "printf hello"], timeout=10)
    assert cp.stdout == "hello"


def test_run_sync_timeout_raises_ops_timeout(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    with pytest.raises(ops.OpsTimeout):
        ops.run_sync(cfg, ["-c", "sleep 5"], timeout=0.2)


# --- single-flight ---------------------------------------------------------

def test_is_busy_and_ensure_free(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    assert ops.is_busy(cfg, "appdata") is False
    ops.ensure_free(cfg, "appdata")  # no raise
    lock = Path(dirs["cache"], "locks", "appdata.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        assert ops.is_busy(cfg, "appdata") is True
        with pytest.raises(ops.OpsLocked):
            ops.ensure_free(cfg, "appdata")
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN); fh.close()


# --- validate_target -------------------------------------------------------

def test_validate_target_ok_new_folder(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"},
                            "/mnt/user/restore/appdata/2026-09-16/")
    assert r["ok"] is True
    assert r["container_path"] == str(Path(cfg["RESTORE_ROOT"], "appdata", "2026-09-16"))


def test_validate_target_empty_value(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"}, "  ")
    assert r["ok"] is False and r["code"] == "empty"


def test_validate_target_escape_outside_restore(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"},
                            "/mnt/user/restore/../etc")
    assert r["ok"] is False and r["code"] == "escape"


def test_validate_target_refuses_the_job_source(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"},
                            "/mnt/user/appdata_backups")
    assert r["ok"] is False and r["code"] == "source"


def test_validate_target_refuses_source_root_itself(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"}, "/mnt/user")
    assert r["ok"] is False and r["code"] == "source"


def test_validate_target_refuses_nonempty_folder(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    full = Path(cfg["RESTORE_ROOT"], "appdata", "full"); full.mkdir(parents=True)
    (full / "afile").write_text("x")
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"},
                            "/mnt/user/restore/appdata/full")
    assert r["ok"] is False and r["code"] == "nonempty"


def test_validate_target_allows_existing_empty_folder(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    empty = Path(cfg["RESTORE_ROOT"], "appdata", "empty"); empty.mkdir(parents=True)
    r = ops.validate_target(cfg, {"name": "appdata", "source": "appdata_backups"},
                            "/mnt/user/restore/appdata/empty")
    assert r["ok"] is True


# --- default_target auto-suffix -------------------------------------------

def test_default_target_dated_folder(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    assert ops.default_target(cfg, {"name": "appdata"}, now=now) == "/mnt/user/restore/appdata/2026-09-16/"


def test_default_target_auto_suffixes_when_taken(dirs, tmp_path):
    cfg = _cfg(dirs, tmp_path)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    taken = Path(cfg["RESTORE_ROOT"], "appdata", "2026-09-16"); taken.mkdir(parents=True)
    (taken / "f").write_text("x")
    assert ops.default_target(cfg, {"name": "appdata"}, now=now) == "/mnt/user/restore/appdata/2026-09-16-2/"
