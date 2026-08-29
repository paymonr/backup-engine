import json
from pathlib import Path
from app.gui import runner

def test_read_state_present(dirs):
    Path(dirs["cache"], "state", "appdata.json").write_text(json.dumps({"outcome": "success", "snapshot_id": "abc"}))
    st = runner.read_state(dirs["cache"], "appdata")
    assert st["outcome"] == "success" and st["snapshot_id"] == "abc"

def test_read_state_absent_returns_none(dirs):
    assert runner.read_state(dirs["cache"], "media") is None

def test_tail_log_returns_last_n(dirs):
    Path(dirs["cache"], "logs", "backup-engine.log").write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    assert runner.tail_log(dirs["cache"], n=3).splitlines() == ["line7", "line8", "line9"]

def test_trigger_job_launches_backup_job_script(dirs, monkeypatch):
    calls = {}
    def fake_popen(cmd, **kw):
        calls["cmd"] = cmd; calls["kw"] = kw
        class P: pass
        return P()
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner.trigger_job("/app/scripts", "movies")
    assert calls["cmd"] == ["bash", "/app/scripts/backup-job.sh", "movies"]
    assert calls["kw"].get("start_new_session") is True
    assert calls["kw"].get("stdout") is runner.subprocess.DEVNULL
    assert calls["kw"].get("stderr") is runner.subprocess.DEVNULL

def test_trigger_job_defaults_env_to_os_environ(dirs, monkeypatch):
    calls = {}
    def fake_popen(cmd, **kw):
        calls["kw"] = kw
        class P: pass
        return P()
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner.trigger_job("/app/scripts", "movies")
    assert calls["kw"].get("env") == runner.os.environ.copy()

def test_trigger_job_passes_through_explicit_env(dirs, monkeypatch):
    calls = {}
    def fake_popen(cmd, **kw):
        calls["kw"] = kw
        class P: pass
        return P()
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    custom_env = {"FOO": "bar"}
    runner.trigger_job("/app/scripts", "movies", env=custom_env)
    assert calls["kw"].get("env") is custom_env
