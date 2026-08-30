import json, shlex, pytest
from pathlib import Path
from app.gui import jobs_io

def _write_raw(cfg, jobs):
    # Simulate a hand-edited / non-GUI-written jobs.json that bypasses upsert()'s
    # write-time validate(). This is the untrusted input the run/schedule path must
    # re-validate.
    Path(cfg, "jobs.json").write_text(json.dumps({"jobs": jobs}))

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

# --- final-fix R-final-1: load() is fail-SAFE on a corrupt/mis-shaped jobs.json ---
# A whole-FILE parse error must degrade to "no jobs" (so the crontab render and the
# Jobs page don't brick/500), emitting ONE stderr diagnostic — NOT raise.
def test_load_returns_empty_and_warns_on_invalid_json(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text("{ this is not valid json")
    assert jobs_io.load(cfg) == []            # does not raise
    assert "jobs.json" in capsys.readouterr().err

def test_load_returns_empty_and_warns_on_jobs_not_a_list(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text(json.dumps({"jobs": "x"}))
    assert jobs_io.load(cfg) == []
    assert "jobs.json" in capsys.readouterr().err

def test_load_returns_empty_and_warns_on_non_dict_entries(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text(json.dumps({"jobs": [1, 2]}))
    assert jobs_io.load(cfg) == []
    assert "jobs.json" in capsys.readouterr().err

def test_load_returns_empty_and_warns_on_non_dict_toplevel(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text(json.dumps([1, 2, 3]))
    assert jobs_io.load(cfg) == []
    assert "jobs.json" in capsys.readouterr().err

# --- final-fix R-final-2: load() drops a dict entry with no/invalid "name" ---
# A nameless (or bad-named) dict entry can't be keyed/rendered (routes do j["name"],
# estimate_io does j['name']) -> load() must DROP it (fail-safe read path) so /jobs,
# /estimate, /costs/refresh don't 500. The WRITE path (_load_strict) is unchanged.
def test_load_drops_nameless_entry(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text(json.dumps(
        {"jobs": [{"type": "archive", "source": "x", "schedule": "0 4 * * 0"}]}))
    assert jobs_io.load(cfg) == []            # entry dropped, does not raise

def test_load_keeps_valid_drops_invalid_named_entries(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text(json.dumps({"jobs": [
        {"type": "archive", "source": "x", "schedule": "0 4 * * 0"},                    # no name
        {"name": "a b", "type": "archive", "source": "x", "schedule": "0 4 * * 0"},     # bad name
        {"name": None, "type": "archive", "source": "x", "schedule": "0 4 * * 0"},      # null name
        {"name": "movies", "type": "archive", "source": "media/movies", "schedule": "0 5 * * 0"},
    ]}))
    assert [j["name"] for j in jobs_io.load(cfg)] == ["movies"]  # only the valid one survives

def test_main_list_on_corrupt_file_exits_0_prints_nothing(tmp_path, monkeypatch, capsys):
    # emit_crontab pipes `--list` under `set -euo pipefail`: a non-zero here bricks
    # container boot. A corrupt file must exit 0 with no stdout (empty crontab).
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text("{ not json")
    monkeypatch.setenv("CONFIG_DIR", cfg)
    rc = jobs_io._main(["--list"])
    assert rc == 0
    assert capsys.readouterr().out == ""

def test_main_get_on_corrupt_file_returns_3(tmp_path, monkeypatch, capsys):
    # <job> on a corrupt file -> get() None -> existing "no such job" -> exit 3, so
    # backup-job.sh's `if ! def=$(...)` _fail's cleanly (unchanged behaviour).
    cfg = _cfg(tmp_path)
    Path(cfg, "jobs.json").write_text("{ not json")
    monkeypatch.setenv("CONFIG_DIR", cfg)
    assert jobs_io._main(["somejob"]) == 3

def test_upsert_raises_and_preserves_bytes_on_corrupt_file(tmp_path):
    # WRITE path is fail-LOUD-without-clobber: never overwrite the user's
    # (unparseable but hand-fixable) bytes with a write built on the swallowed [].
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    raw = "{ this is not valid json"
    Path(cfg, "jobs.json").write_text(raw)
    with pytest.raises(ValueError):
        jobs_io.upsert(cfg, _job(), source_root=root)
    assert Path(cfg, "jobs.json").read_text() == raw   # untouched

def test_delete_raises_and_preserves_bytes_on_corrupt_file(tmp_path):
    cfg = _cfg(tmp_path)
    raw = "{ this is not valid json"
    Path(cfg, "jobs.json").write_text(raw)
    with pytest.raises(ValueError):
        jobs_io.delete(cfg, "movies")
    assert Path(cfg, "jobs.json").read_text() == raw

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

# --- Task 10 security: name charset self-containment (regex \Z, not $) ---
def test_valid_name_rejects_trailing_newline():
    # Python `$` also matches just before a trailing newline, so "a\n" would sneak
    # through the charset gate and reach restic --tag / rclone media/<name>/ / the
    # crontab name field. \Z (end-of-string) must reject it.
    assert jobs_io.valid_name("a\n") is False
    assert jobs_io.valid_name("appdata") is True
    assert jobs_io.valid_name("a b") is False and jobs_io.valid_name("a/b") is False

# --- Task 10 security: schedule must be a clean single-space 5-field cron ---
def test_validate_rejects_tab_in_schedule(tmp_path):
    # A tab passes len(sched.split())==5 but corrupts the --list TSV that the
    # entrypoint reads with IFS=$'\t' -> mis-columned/hijacked crontab line.
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        jobs_io.validate(_job(schedule="0\t3 * * *"), root)
    with pytest.raises(ValueError):
        jobs_io.validate(_job(schedule="0  3 * * *"), root)  # double space too

# --- Task 10 security: the CLI re-validates untrusted jobs.json at RUN time ---
def test_main_emit_rejects_traversing_source(tmp_path, monkeypatch, capsys):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [{"name": "evil", "type": "archive", "source": "../../etc",
                      "schedule": "0 4 * * 0", "enabled": True,
                      "storage_class": "STANDARD", "mirror": False}])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    rc = jobs_io._main(["evil"])
    assert rc != 0
    assert "JOB_SOURCE" not in capsys.readouterr().out

def test_main_emit_rejects_bad_name_and_schedule(tmp_path, monkeypatch, capsys):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [{"name": "a b", "type": "archive", "source": "media/movies",
                      "schedule": "0 4 * * 0", "enabled": True,
                      "storage_class": "STANDARD", "mirror": False}])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    assert jobs_io._main(["a b"]) != 0 and capsys.readouterr().out == ""

def test_main_emit_accepts_valid_job(tmp_path, monkeypatch, capsys):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [{"name": "movies", "type": "archive", "source": "media/movies",
                      "schedule": "0 4 * * 0", "enabled": True,
                      "storage_class": "DEEP_ARCHIVE", "mirror": False}])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    rc = jobs_io._main(["movies"])
    out = capsys.readouterr().out
    assert rc == 0 and "JOB_SOURCE=media/movies" in out and "JOB_NAME=movies" in out

def test_main_emit_allows_confined_but_absent_source(tmp_path, monkeypatch, capsys):
    # restore.sh shares this <job> emit path and MUST run on a fresh/rebuilt machine
    # where the local source is absent (it restores FROM S3). Confinement is enforced,
    # but existence is NOT — the backup path's own `[ -d "$src" ]` guards that. So a
    # confined-but-missing source still emits (else restore breaks on a fresh box).
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [{"name": "appdata", "type": "versioned", "source": "appdata_gone",
                      "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
                      "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    rc = jobs_io._main(["appdata"])
    out = capsys.readouterr().out
    assert rc == 0 and "JOB_SOURCE=appdata_gone" in out

def test_main_list_drops_invalid_jobs_keeps_valid(tmp_path, monkeypatch, capsys):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [
        {"name": "evil", "type": "archive", "source": "../../etc", "schedule": "0 4 * * 0",
         "enabled": True, "storage_class": "STANDARD", "mirror": False},
        {"name": "a b", "type": "archive", "source": "media/movies", "schedule": "0 4 * * 0",
         "enabled": True, "storage_class": "STANDARD", "mirror": False},
        {"name": "movies", "type": "archive", "source": "media/movies", "schedule": "0 5 * * 0",
         "enabled": True, "storage_class": "STANDARD", "mirror": False},
    ])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    rc = jobs_io._main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "evil" not in out and "a b" not in out           # traversal + charset dropped
    assert out.strip() == "1\t0 5 * * 0\tmovies"            # only the valid job scheduled

def test_main_list_does_not_require_source_to_exist(tmp_path, monkeypatch, capsys):
    # RULING: --list validates confinement/name/schedule but NOT dir existence, so a
    # transiently-unmounted (but confined) source still schedules; run-time re-checks it.
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [{"name": "movies", "type": "archive", "source": "media/not_yet_mounted",
                      "schedule": "0 4 * * 0", "enabled": True,
                      "storage_class": "STANDARD", "mirror": False}])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    assert jobs_io._main(["--list"]) == 0
    assert capsys.readouterr().out.strip() == "1\t0 4 * * 0\tmovies"

def test_emit_shell_quotes_metacharacters():
    # Item (3): a value with shell metacharacters (bypassing validate) round-trips as a
    # single inert literal when the runner eval's the emitted assignment.
    payload = "x; touch /pwned $(id) `id`"
    s = jobs_io.emit_shell({"name": "x", "type": "archive", "source": payload,
                            "storage_class": "STANDARD", "mirror": False})
    line = next(l for l in s.splitlines() if l.startswith("JOB_SOURCE="))
    assert shlex.split(line) == [f"JOB_SOURCE={payload}"]

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
