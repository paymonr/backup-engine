import json, pytest
from pathlib import Path
from app.gui import jobs_io

def _root(tmp_path):
    r = tmp_path / "src"; (r / "media" / "movies").mkdir(parents=True); (r / "appdata").mkdir()
    return str(r)

def _cfg(tmp_path):
    c = tmp_path / "config"; c.mkdir(); return str(c)

def _job(**kw):
    base = {"name": "movies", "type": "archive", "source": "media/movies",
            "schedule": "0 4 * * 0", "enabled": True, "storage_class": "DEEP_ARCHIVE", "mirror": False}
    base.update(kw); return base

def test_upsert_then_load_and_get(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(), source_root=root)
    assert [j["name"] for j in jobs_io.load(cfg)] == ["movies"]
    assert jobs_io.get(cfg, "movies")["source"] == "media/movies"
    assert Path(cfg, "jobs.json").exists()

def test_upsert_replaces_same_name(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(schedule="0 4 * * 0"), source_root=root)
    jobs_io.upsert(cfg, _job(schedule="0 5 * * 0"), source_root=root)
    jobs = jobs_io.load(cfg)
    assert len(jobs) == 1 and jobs[0]["schedule"] == "0 5 * * 0"

def test_delete(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(), source_root=root); jobs_io.delete(cfg, "movies")
    assert jobs_io.load(cfg) == []

def test_load_absent_is_empty(tmp_path):
    assert jobs_io.load(_cfg(tmp_path)) == []

def test_validate_rejects_bad_name(tmp_path):
    with pytest.raises(ValueError):
        jobs_io.validate(_job(name="../evil"), _root(tmp_path))

def test_validate_rejects_source_outside_root(tmp_path):
    with pytest.raises(ValueError):
        jobs_io.validate(_job(source="../../etc"), _root(tmp_path))

def test_validate_rejects_missing_source_dir(tmp_path):
    with pytest.raises(ValueError):
        jobs_io.validate(_job(source="media/nope"), _root(tmp_path))

def test_validate_rejects_bad_type_and_class(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        jobs_io.validate(_job(type="magic"), root)
    with pytest.raises(ValueError):
        jobs_io.validate(_job(storage_class="NEBULA"), root)

def test_emit_shell_archive(tmp_path):
    s = jobs_io.emit_shell(_job())
    assert "JOB_TYPE=archive" in s and "JOB_SOURCE=media/movies" in s
    assert "JOB_STORAGE_CLASS=DEEP_ARCHIVE" in s and "JOB_MIRROR=false" in s

def test_emit_shell_versioned_keep(tmp_path):
    j = _job(name="cfg", type="versioned", source="appdata", storage_class="STANDARD",
             keep={"last": 3, "daily": 7, "weekly": 4, "monthly": 6})
    j.pop("mirror", None)
    s = jobs_io.emit_shell(j)
    assert "JOB_TYPE=versioned" in s and "JOB_KEEP_LAST=3" in s and "JOB_KEEP_MONTHLY=6" in s

def test_main_list_prints_enabled_schedule_name_per_job(tmp_path, monkeypatch, capsys):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(name="movies", schedule="0 4 * * 0", enabled=True), source_root=root)
    jobs_io.upsert(cfg, _job(name="appdata", type="versioned", source="appdata",
                              schedule="0 3 * * *", enabled=False), source_root=root)
    monkeypatch.setenv("CONFIG_DIR", cfg)
    rc = jobs_io._main(["--list"])
    assert rc == 0
    lines = capsys.readouterr().out.strip("\n").split("\n")
    assert sorted(lines) == sorted([
        "1\t0 4 * * 0\tmovies",
        "0\t0 3 * * *\tappdata",
    ])

def test_main_list_on_missing_jobs_file_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", _cfg(tmp_path))
    rc = jobs_io._main(["--list"])
    assert rc == 0
    assert capsys.readouterr().out == ""

def test_emit_shell_is_injection_safe(tmp_path):
    # a name/source can only be the validated charset/path; emit uses single-quote escaping.
    # shlex.quote only wraps strings containing shell-special characters, so a name drawn from
    # the validated charset (letters/digits/._-) comes back unquoted.
    s = jobs_io.emit_shell(_job(name="a-b.c"))
    assert "JOB_NAME=a-b.c" in s
    # prove injection-safety directly: a value needing quoting (bypassing validate) IS quoted.
    unsafe = jobs_io.emit_shell({"name": "x", "type": "archive", "source": "a b",
                                  "storage_class": "STANDARD", "mirror": False})
    assert "'" in unsafe
