# tests/engine/test_resume.py — Task 6: auto-resume interrupted runs on boot.
import json
from pathlib import Path

from app.engine import resume, runs
from app.gui import config_io, jobs_io  # noqa: F401 — interfaces this module consumes


def _make_cfg(tmp_path, *, jobs, auto_resume=True, max_resumes=3):
    cache = tmp_path / "cache"; (cache / "state").mkdir(parents=True)
    config = tmp_path / "config"; config.mkdir()
    (config / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    (config / "backup.env").write_text(
        f"AUTO_RESUME_ON_BOOT={'true' if auto_resume else 'false'}\n"
        f"BE_MAX_RESUMES={max_resumes}\n"
    )
    return {"CACHE_DIR": str(cache), "CONFIG_DIR": str(config),
            "SCRIPTS_DIR": str(tmp_path / "scripts")}


def _seed(cache, job, outcome):
    runs.append_event(cache, job, {"v": 1, "id": "20260921T050000Z-aaaa", "job": job, "kind": "backup",
                                    "event": "start", "started_at": "2026-09-21T05:00:00Z"})
    if outcome != "running":
        runs.append_event(cache, job, {"v": 1, "id": "20260921T050000Z-aaaa", "job": job, "kind": "backup",
                                        "event": "end", "outcome": outcome, "finished_at": "2026-09-21T05:01:00Z"})


def test_resumes_only_aborted_enabled_when_setting_on(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}, {"name": "b", "enabled": True},
                                     {"name": "c", "enabled": False}], auto_resume=True)
    _seed(cfg["CACHE_DIR"], "a", "aborted"); _seed(cfg["CACHE_DIR"], "b", "ok"); _seed(cfg["CACHE_DIR"], "c", "aborted")
    fired = []; n = resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name))
    assert fired == ["a"] and n == 1        # b ok, c disabled


def test_paused_is_never_resumed(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}], auto_resume=True)
    _seed(cfg["CACHE_DIR"], "a", "paused")
    fired = []; resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name)); assert fired == []


def test_off_setting_resumes_nothing(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}], auto_resume=False)
    _seed(cfg["CACHE_DIR"], "a", "aborted")
    fired = []; resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name)); assert fired == []


def test_resume_cap_stops_the_loop(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}], auto_resume=True, max_resumes=1)
    _seed(cfg["CACHE_DIR"], "a", "aborted")
    t = lambda name, env=None: None
    assert resume.resume_interrupted(cfg, trigger=t) == 1
    _seed(cfg["CACHE_DIR"], "a", "aborted")   # still aborted after the resume attempt
    assert resume.resume_interrupted(cfg, trigger=t) == 0   # cap reached


def test_running_and_failed_are_not_resumed(tmp_path):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}, {"name": "b", "enabled": True}],
                     auto_resume=True)
    _seed(cfg["CACHE_DIR"], "a", "running")
    _seed(cfg["CACHE_DIR"], "b", "failed")
    fired = []; n = resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name))
    assert fired == [] and n == 0


def test_locked_job_is_skipped(tmp_path):
    import fcntl
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}], auto_resume=True)
    _seed(cfg["CACHE_DIR"], "a", "aborted")
    lock_path = runs.lock_path(cfg["CACHE_DIR"], "a")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        fired = []
        n = resume.resume_interrupted(cfg, trigger=lambda name, env=None: fired.append(name))
        assert fired == [] and n == 0
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def test_trigger_env_carries_be_resume_flag_and_bumps_counter(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}], auto_resume=True)
    _seed(cfg["CACHE_DIR"], "a", "aborted")
    monkeypatch.setenv("SOME_AMBIENT_VAR", "keepme")
    seen = {}
    def trig(name, env=None):
        seen["name"] = name; seen["env"] = env
    n = resume.resume_interrupted(cfg, trigger=trig)
    assert n == 1
    assert seen["name"] == "a"
    assert seen["env"]["BE_RESUME"] == "1"
    assert seen["env"]["SOME_AMBIENT_VAR"] == "keepme"   # ambient environ preserved, not replaced
    assert Path(cfg["CACHE_DIR"], "state", "a.resumes").read_text().strip() == "1"


def test_default_trigger_calls_runner_trigger_job(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path, jobs=[{"name": "a", "enabled": True}], auto_resume=True)
    _seed(cfg["CACHE_DIR"], "a", "aborted")
    seen = {}
    from app.gui import runner
    def fake_trigger_job(scripts_dir, name, env=None):
        seen.update(scripts_dir=scripts_dir, name=name, env=env)
    monkeypatch.setattr(runner, "trigger_job", fake_trigger_job)
    n = resume.resume_interrupted(cfg)
    assert n == 1
    assert seen == {"scripts_dir": cfg["SCRIPTS_DIR"], "name": "a", "env": seen["env"]}
    assert seen["env"]["BE_RESUME"] == "1"
