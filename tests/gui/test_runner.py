import json
from pathlib import Path
import pytest
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

def test_trigger_backup_launches_correct_script(dirs, monkeypatch):
    calls = {}
    def fake_popen(cmd, **kw):
        calls["cmd"] = cmd; calls["kw"] = kw
        class P: pass
        return P()
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner.trigger_backup("/app/scripts", "media")
    assert calls["cmd"] == ["bash", "/app/scripts/backup-media.sh"]
    assert calls["kw"].get("start_new_session") is True

def test_trigger_backup_unknown_pipeline_raises(dirs):
    with pytest.raises(ValueError):
        runner.trigger_backup("/app/scripts", "nope")
