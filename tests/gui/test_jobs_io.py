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

def _vfjob(**kw):
    base = {"name": "docs", "type": "versioned-files", "source": "media/movies",
            "schedule": "0 2 * * *", "enabled": True, "storage_class": "DEEP_ARCHIVE",
            "retention_days": 90}
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

# --- Task 4: versioned-files job type ---

def test_validate_accepts_versioned_files_on_deep_archive(tmp_path):
    root = _root(tmp_path)
    v = jobs_io.validate(_vfjob(storage_class="DEEP_ARCHIVE"), root)
    assert v["type"] == "versioned-files"
    assert v["storage_class"] == "DEEP_ARCHIVE"
    assert v["retention_days"] == 90

def test_validate_accepts_versioned_files_on_standard(tmp_path):
    root = _root(tmp_path)
    v = jobs_io.validate(_vfjob(storage_class="STANDARD"), root)
    assert v["storage_class"] == "STANDARD"

def test_validate_versioned_files_defaults_retention_to_90(tmp_path):
    root = _root(tmp_path)
    job = _vfjob(); del job["retention_days"]
    v = jobs_io.validate(job, root)
    assert v["retention_days"] == 90

def test_validate_versioned_files_rejects_negative_retention(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        jobs_io.validate(_vfjob(retention_days=-1), root)

def test_validate_versioned_files_rejects_non_int_retention(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        jobs_io.validate(_vfjob(retention_days="soon"), root)
    with pytest.raises(ValueError):
        jobs_io.validate(_vfjob(retention_days=None), root)

def test_validate_versioned_files_requires_confined_source(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        jobs_io.validate(_vfjob(source="../../etc"), root)

def test_validate_versioned_files_requires_existing_source_when_required(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        jobs_io.validate(_vfjob(source="media/nope"), root)

def test_emit_shell_versioned_files(tmp_path):
    j = jobs_io.validate(_vfjob(storage_class="GLACIER", retention_days=120), _root(tmp_path))
    s = jobs_io.emit_shell(j)
    assert "JOB_TYPE=versioned-files" in s
    assert "JOB_SOURCE=media/movies" in s
    assert "JOB_STORAGE_CLASS=GLACIER" in s
    assert "JOB_RETENTION_DAYS=120" in s

def test_upsert_then_load_versioned_files(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _vfjob(), source_root=root)
    loaded = jobs_io.get(cfg, "docs")
    assert loaded["type"] == "versioned-files" and loaded["retention_days"] == 90

def test_main_emit_versioned_files_job(tmp_path, monkeypatch, capsys):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    _write_raw(cfg, [{"name": "docs", "type": "versioned-files", "source": "media/movies",
                      "schedule": "0 2 * * *", "enabled": True, "storage_class": "DEEP_ARCHIVE",
                      "retention_days": 45}])
    monkeypatch.setenv("CONFIG_DIR", cfg); monkeypatch.setenv("SOURCE_ROOT", root)
    rc = jobs_io._main(["docs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "JOB_TYPE=versioned-files" in out and "JOB_RETENTION_DAYS=45" in out

def test_emit_shell_archive(tmp_path):
    s = jobs_io.emit_shell(_job())
    assert "JOB_TYPE=archive" in s and "JOB_SOURCE=media/movies" in s
    assert "JOB_STORAGE_CLASS=DEEP_ARCHIVE" in s and "JOB_MIRROR=false" in s

def test_emit_shell_versioned_keep(tmp_path):
    root = _root(tmp_path)
    j = _job(name="cfg", type="versioned", source="appdata", storage_class="STANDARD",
             keep={"last": 3, "daily": 7, "weekly": 4, "monthly": 6})
    j.pop("mirror", None)
    validated = jobs_io.validate(j, root)
    s = jobs_io.emit_shell(validated)
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

# --- Task 1: Retention schema normalization and migration ---

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

def test_tiered_all_zero_rejected(tmp_path):
    # Fix 1a: an all-zero tiered keep policy would flow to `restic forget --prune
    # --keep-last 0 --keep-daily 0 --keep-weekly 0 --keep-monthly 0` and destroy
    # every snapshot for the job's tag. Must be rejected at validate() time.
    t = {"type": "tiered", "keep": {"last": 0, "daily": 0, "weekly": 0, "monthly": 0}}
    with pytest.raises(ValueError):
        _val(_base(type="versioned", source="appdata", retention=t), tmp_path)

def test_tiered_one_nonzero_still_valid(tmp_path):
    # Regression guard: at least one non-zero keep value must still validate.
    t = {"type": "tiered", "keep": {"last": 1, "daily": 0, "weekly": 0, "monthly": 0}}
    r = _val(_base(type="versioned", source="appdata", retention=t), tmp_path)
    assert r == {"type": "tiered", "keep": {"last": 1, "daily": 0, "weekly": 0, "monthly": 0}}

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

def test_legacy_fields_derived_from_retention(tmp_path):
    # Regression: legacy fields (keep, retention_days) must never diverge from retention.
    # Test versioned job with new-schema-only retention (no top-level keep).
    root = _root(tmp_path)
    retention_spec = {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}
    v = jobs_io.validate(
        {"name": "cfg", "type": "versioned", "source": "appdata", "schedule": "0 3 * * *",
         "storage_class": "STANDARD", "retention": retention_spec},
        root
    )
    # Both paths should have the same keep values
    assert v["keep"] == v["retention"]["keep"]
    # Test versioned-files job with new-schema-only retention (no top-level retention_days).
    retention_spec = {"type": "days", "days": 45}
    v = jobs_io.validate(
        {"name": "docs", "type": "versioned-files", "source": "media/movies", "schedule": "0 2 * * *",
         "storage_class": "STANDARD", "retention": retention_spec},
        root
    )
    # Both paths should have the same days value
    assert v["retention_days"] == v["retention"]["days"]
    assert v["retention_days"] == 45

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

# =====================================================================
# Task 4: created_at / assumptions / measured / acknowledged;
#         set_enabled; render_crontab; delete removes cache files.
# =====================================================================

def test_validate_defaults_assumptions_when_absent(tmp_path):
    # 7.8: assumptions is backfilled with defaults on read so an edit never
    # resets them (change_rate 0%, not bundled, 0.05 GB pack member).
    v = jobs_io.validate(_job(), _root(tmp_path))
    assert v["assumptions"]["change_rate_pct"] == 0.0
    assert v["assumptions"]["bundled"] is False
    assert v["assumptions"]["pack_member_gb"] == 0.05

def test_validate_passes_assumptions_through_and_typechecks(tmp_path):
    root = _root(tmp_path)
    a = {"change_rate_pct": 10, "bundled": True, "pack_member_gb": 2.0, "set_at": "2026-09-12T00:00:00Z"}
    v = jobs_io.validate(_job(assumptions=a), root)
    assert v["assumptions"]["change_rate_pct"] == 10.0
    assert v["assumptions"]["bundled"] is True
    assert v["assumptions"]["pack_member_gb"] == 2.0
    assert v["assumptions"]["set_at"] == "2026-09-12T00:00:00Z"
    # wrong-typed members fall back to defaults rather than 500
    v2 = jobs_io.validate(_job(assumptions={"change_rate_pct": "lots", "bundled": "yes"}), root)
    assert v2["assumptions"]["change_rate_pct"] == 0.0
    assert v2["assumptions"]["bundled"] is True   # any truthy -> bool

def test_validate_measured_present_only_when_valid(tmp_path):
    root = _root(tmp_path)
    m = {"bytes": 56594862080, "count": 533, "at": "2026-09-15T05:00:01Z", "capped": True}
    v = jobs_io.validate(_job(measured=m), root)
    assert v["measured"] == {"bytes": 56594862080, "count": 533,
                             "at": "2026-09-15T05:00:01Z", "capped": True}
    # measured with a non-int bytes is unusable -> the key is dropped, never a 500
    v2 = jobs_io.validate(_job(measured={"bytes": "big"}), root)
    assert "measured" not in v2
    # absent measured stays absent
    assert "measured" not in jobs_io.validate(_job(), root)

def test_validate_acknowledged_round_trip_and_drop_bad(tmp_path):
    root = _root(tmp_path)
    ack = [{"code": "snapshots_on_cold_class", "class": "DEEP_ARCHIVE", "at": "2026-09-12T00:00:00Z"}]
    v = jobs_io.validate(_job(acknowledged=ack), root)
    assert v["acknowledged"] == ack
    assert "acknowledged" not in jobs_io.validate(_job(acknowledged="nope"), root)

def test_validate_created_at_kept_when_parseable_dropped_when_garbage(tmp_path):
    root = _root(tmp_path)
    v = jobs_io.validate(_job(created_at="2026-09-01T00:00:00Z"), root)
    assert v["created_at"] == "2026-09-01T00:00:00Z"
    assert "created_at" not in jobs_io.validate(_job(created_at="not-a-date"), root)

def test_new_fields_round_trip_through_upsert_load(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(
        measured={"bytes": 100, "count": 5, "at": "2026-09-15T05:00:01Z", "capped": False},
        acknowledged=[{"code": "x", "class": "DEEP_ARCHIVE", "at": "2026-09-12T00:00:00Z"}],
    ), source_root=root)
    loaded = jobs_io.get(cfg, "movies")
    assert loaded["measured"]["bytes"] == 100
    assert loaded["acknowledged"][0]["code"] == "x"
    assert loaded["assumptions"]["pack_member_gb"] == 0.05
    assert "created_at" in loaded          # upsert stamped it

def test_upsert_stamps_created_at_for_new_and_copies_on_edit(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(schedule="0 4 * * 0"), source_root=root)
    created = jobs_io.get(cfg, "movies")["created_at"]
    assert created and isinstance(created, str)
    # editing the same name must not change created_at (the job's birthday is fixed)
    jobs_io.upsert(cfg, _job(schedule="0 5 * * 0"), source_root=root)
    assert jobs_io.get(cfg, "movies")["created_at"] == created

def test_validate_rejects_all_zero_tiered_via_upsert(tmp_path):
    # the all-zero tiered keep must be rejected at the write path too (never persisted)
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    bad = _job(name="cfg", type="versioned", source="appdata",
               retention={"type": "tiered", "keep": {"last": 0, "daily": 0, "weekly": 0, "monthly": 0}})
    bad.pop("mirror", None)
    with pytest.raises(ValueError):
        jobs_io.upsert(cfg, bad, source_root=root)

def test_set_enabled_toggles(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(enabled=True), source_root=root)
    out = jobs_io.set_enabled(cfg, "movies", False)
    assert out["enabled"] is False
    assert jobs_io.get(cfg, "movies")["enabled"] is False
    jobs_io.set_enabled(cfg, "movies", True)
    assert jobs_io.get(cfg, "movies")["enabled"] is True

def test_set_enabled_raises_on_corrupt_file(tmp_path):
    cfg = _cfg(tmp_path)
    raw = "{ not json"
    Path(cfg, "jobs.json").write_text(raw)
    with pytest.raises(jobs_io.JobsFileError):
        jobs_io.set_enabled(cfg, "movies", False)
    assert Path(cfg, "jobs.json").read_text() == raw   # untouched

def test_render_crontab_dry_run_returns_lines_without_writing(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(name="movies", schedule="0 4 * * 0", enabled=True), source_root=root)
    cache = str(tmp_path / "cache")
    text = jobs_io.render_crontab(cfg, cache, "/app/scripts", dry_run=True, source_root=root)
    assert text == "0 4 * * 0 /app/scripts/backup-job.sh movies\n"
    assert not Path(cache, "crontab").exists()   # dry run never writes

def test_render_crontab_writes_and_skips_disabled_and_invalid(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(name="movies", schedule="0 4 * * 0", enabled=True), source_root=root)
    jobs_io.upsert(cfg, _job(name="paused", schedule="0 3 * * *", enabled=False), source_root=root)
    # a hand-edited invalid job (bad source) must not reach the crontab
    _write_raw(cfg, [
        {"name": "movies", "type": "archive", "source": "media/movies", "schedule": "0 4 * * 0",
         "enabled": True, "storage_class": "STANDARD"},
        {"name": "evil", "type": "archive", "source": "../../etc", "schedule": "0 2 * * *",
         "enabled": True, "storage_class": "STANDARD"},
    ])
    cache = str(tmp_path / "cache")
    text = jobs_io.render_crontab(cfg, cache, "/app/scripts", source_root=root)
    assert text == "0 4 * * 0 /app/scripts/backup-job.sh movies\n"
    assert Path(cache, "crontab").read_text() == text     # written for real
    assert "evil" not in text and "paused" not in text

def test_delete_removes_cache_files(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(name="movies"), source_root=root)
    cache = tmp_path / "cache"
    state = cache / "state"; state.mkdir(parents=True)
    (state / "movies.runs.jsonl").write_text("{}\n")
    (state / "movies.json").write_text("{}")
    (state / "movies.points.json").write_text("{}")
    (state / "movies.thaw.json").write_text("{}")
    logdir = cache / "logs" / "runs" / "movies"; logdir.mkdir(parents=True)
    (logdir / "x.log").write_text("hi")
    # an unrelated job's caches must survive
    (state / "other.runs.jsonl").write_text("{}\n")
    jobs_io.delete(cfg, "movies", str(cache))
    assert jobs_io.load(cfg) == []
    assert not (state / "movies.runs.jsonl").exists()
    assert not (state / "movies.json").exists()
    assert not (state / "movies.points.json").exists()
    assert not logdir.exists()
    assert (state / "other.runs.jsonl").exists()   # untouched

def test_delete_removes_browse_cache_dir(tmp_path):
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(name="movies"), source_root=root)
    cache = tmp_path / "cache"
    d = Path(cache, "state", "movies.browse"); d.mkdir(parents=True)
    (d / "deadbeef.json").write_text("[]")
    jobs_io.delete(cfg, "movies", str(cache))
    assert not d.exists()

def test_delete_removes_resilience_markers(tmp_path):
    """A deleted job's stale control/resume markers must not survive to confuse
    a job later recreated with the same name (7.1.9 fix)."""
    cfg, root = _cfg(tmp_path), _root(tmp_path)
    jobs_io.upsert(cfg, _job(name="movies"), source_root=root)
    cache = tmp_path / "cache"
    state = cache / "state"; state.mkdir(parents=True)
    (state / "movies.control").write_text("pause")
    (state / "movies.resumes").write_text("1")
    jobs_io.delete(cfg, "movies", str(cache))
    assert not (state / "movies.control").exists()
    assert not (state / "movies.resumes").exists()

# --- Task 5: dedicated-bucket fields + JOB_BUCKET export ---

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

# --- S3 rules (spec 2026-09-23 §1): Plain copy "newest N + days" ------------

def test_count_retention_may_carry_days():
    from app.gui.jobs_io import _normalize_retention
    r = _normalize_retention({"retention": {"type": "count", "count": "10", "days": "30"}}, "archive")
    assert r == {"type": "count", "count": 10, "days": 30}


def test_count_retention_without_days_is_unchanged():
    from app.gui.jobs_io import _normalize_retention
    assert _normalize_retention({"retention": {"type": "count", "count": 5}}, "archive") == {"type": "count", "count": 5}
    assert _normalize_retention({"retention": {"type": "count", "count": 5, "days": ""}}, "archive") == {"type": "count", "count": 5}


@pytest.mark.parametrize("bad", ["0", "-3", "x"])
def test_count_retention_days_must_be_positive(bad):
    from app.gui.jobs_io import _normalize_retention
    with pytest.raises(ValueError):
        _normalize_retention({"retention": {"type": "count", "count": 5, "days": bad}}, "archive")
