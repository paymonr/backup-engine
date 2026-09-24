# tests/engine/test_lifecycle_preview.py — preview + typed confirmation (spec 2026-09-23 §3, R-B4).
import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.engine import lifecycle as lc, storage_summary
from tests.engine.test_lifecycle_sync import BASE, FakeS3, _live, _set_manga, cfg  # noqa: F401

M = "media/manga/"


@pytest.fixture
def pcfg(cfg, tmp_path):
    src = tmp_path / "src"
    (src / "media" / "manga").mkdir(parents=True)
    (src / "appdata").mkdir()
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    fake = FakeS3()
    lc.sync(cfg, BASE, run=fake)                          # manga 180 days + appdata 30 applied
    return dict(cfg, SOURCE_ROOT=str(src)), fake


def _jobs(cfg):
    return json.loads(Path(cfg["CONFIG_DIR"], "jobs.json").read_text())["jobs"]


def _manga_edit(cfg, retention):
    job = next(j for j in _jobs(cfg) if j["name"] == "manga")
    return {"kind": "job", "job": dict(job, retention=retention)}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(cfg, folder=M):
    """A fresh (right-now) summary -- fix round 2, Important 1: scanned_at must be relative to
    the test's own clock, not a hard-coded calendar date, or this goes stale (and the tests
    that depend on it start failing) once SUMMARY_STALE_DAYS elapses for real. Returns the
    scanned_at it wrote, so callers can assert against the exact same value."""
    scanned_at = _iso(datetime.now(timezone.utc))
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": scanned_at, "bucket": BASE, "folder": folder,
        "noncurrent_by_age_days": [[10, 5, 500], [100, 3, 300]], "noncurrent_by_rank": [[1, 8, 800]],
        "noncurrent_by_age_rank": [[10, 1, 5, 500], [100, 1, 3, 300]],
        "noncurrent_versions": 8, "noncurrent_bytes": 800, "delete_markers": 0,
        "current_objects": 8, "current_bytes": 8000})
    return scanned_at


def _token_path(cfg, token):
    return Path(cfg["CACHE_DIR"], "state", "lifecycle", "pending", f"{token}.json")


def test_a_change_that_keeps_more_needs_no_token(pcfg, monkeypatch):
    """fix round 2, Minor 3: monkeypatching lc.provision._run_aws has NO effect on anything --
    sync()/check()'s `run=provision._run_aws` default is bound at *definition* time, and
    preview() doesn't even take a `run` at all -- so that alone can never catch a regression.
    Patch the functions AWS-reaching code actually calls by name (looked up at call time, so a
    module-level monkeypatch reaches them) instead: role_creds/read_rules/write_rules cover
    sync()/check() (both funnel through _reconcile_locked), and check() itself is patched too
    in case preview() were ever made to call it directly."""
    cfg, fake = pcfg
    def boom(*a, **k):
        raise AssertionError("preview must never reach AWS")
    for name in ("role_creds", "read_rules", "write_rules", "check"):
        monkeypatch.setattr(lc, name, boom)
    calls = len(fake.calls)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 365}))
    assert pv.token is None and pv.keeps_less == [] and [c.kind for c in pv.changes] == [lc.KEEPS_MORE]
    assert len(fake.calls) == calls                        # a preview never reaches S3


def test_a_preview_that_deletes_asks_for_the_bucket_name(pcfg):
    cfg, _ = pcfg
    scanned_at = _summary(cfg)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    (c,) = pv.keeps_less
    assert c.folder == M and pv.needs_typed is True and pv.token
    imp = pv.impacts[c.rule_id]
    assert (imp["versions"], imp["bytes"], imp["oldest_age_days"], imp["scanned_at"]) == \
        (3, 300, 100, scanned_at)
    tok = json.loads(_token_path(cfg, pv.token).read_text())
    assert tok["bucket"] == BASE and tok["edit"]["kind"] == "job" and tok["needs_typed"] is True


def test_a_shorter_rule_that_removes_nothing_today_needs_no_typing(pcfg):
    cfg, _ = pcfg
    _summary(cfg)                                         # nothing is older than 100 days
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 150}))
    assert pv.keeps_less and pv.needs_typed is False
    assert pv.impacts["backup-engine:media/manga/"]["versions"] == 0


def test_no_summary_still_needs_the_typed_confirmation(pcfg):
    cfg, _ = pcfg
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    assert pv.impacts["backup-engine:media/manga/"] is None and pv.needs_typed is True


def test_apply_confirmed_saves_the_job_and_writes_the_rule(pcfg):
    cfg, fake = pcfg
    _summary(cfg)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    res = lc.apply_confirmed(cfg, pv.token, f"  {BASE} ", run=fake)
    assert res.changed is True
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert next(j for j in _jobs(cfg) if j["name"] == "manga")["retention"] == {"type": "days", "days": 30}
    assert not _token_path(cfg, pv.token).exists()
    assert lc.outstanding(cfg, BASE) == (False, [])


def test_the_bucket_name_must_be_typed_exactly(pcfg):
    cfg, fake = pcfg
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    puts = len(fake.puts())
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, "not-the-bucket", run=fake)
    assert e.value.kind == "typed" and BASE in e.value.message
    assert len(fake.puts()) == puts and _token_path(cfg, pv.token).exists()
    assert next(j for j in _jobs(cfg) if j["name"] == "manga")["retention"] == {"type": "days", "days": 180}


def test_a_preview_goes_stale_when_the_jobs_change(pcfg):
    cfg, fake = pcfg
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    _set_manga(cfg, {"type": "days", "days": 365})        # saved elsewhere in between
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "stale" and not _token_path(cfg, pv.token).exists()


def test_a_preview_goes_stale_after_an_hour(pcfg):
    cfg, fake = pcfg
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    p = _token_path(cfg, pv.token)
    tok = json.loads(p.read_text())
    tok["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(tok))
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "stale"
    assert not p.exists()                                 # the expired token file is deleted


@pytest.mark.parametrize("token", ["", "../../etc/passwd", "nope"])
def test_an_unknown_token_is_stale(pcfg, token):
    cfg, fake = pcfg
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, token, BASE, run=fake)
    assert e.value.kind == "stale"


def test_confirming_what_is_waiting_applies_it(pcfg):
    cfg, fake = pcfg
    _set_manga(cfg, {"type": "days", "days": 30})         # saved; held by the gate
    lc.sync(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    pv = lc.preview(cfg, BASE, {"kind": "confirm"})
    assert [c.folder for c in pv.keeps_less] == [M]
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


def test_a_settings_edit_confirms_a_shorter_undo_window(pcfg):
    cfg, fake = pcfg
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 7}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert [c.folder for c in pv.keeps_less] == ["appdata/"]
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert lc.load_settings(cfg["CONFIG_DIR"])["buckets"][BASE]["folders"]["appdata/"]["undo_days"] == 7
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}


def test_the_preview_lists_what_already_waits_too(pcfg):
    cfg, fake = pcfg
    _set_manga(cfg, {"type": "days", "days": 30})
    lc.sync(cfg, BASE, run=fake)                          # manga waits
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 7}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert sorted(c.folder for c in pv.keeps_less) == ["appdata/", M]   # the write confirms both


def test_preview_before_the_first_check_says_so(cfg):
    with pytest.raises(lc.PreviewError) as e:
        lc.preview(cfg, BASE, {"kind": "confirm"})
    assert e.value.kind == "not_checked"


def test_preview_below_level_four_is_not_managed(pcfg):
    cfg, _ = pcfg
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text().replace("PERMISSIONS_VERSION=4", "PERMISSIONS_VERSION=3"))
    with pytest.raises(lc.PreviewError) as e:
        lc.preview(cfg, BASE, {"kind": "confirm"})
    assert e.value.kind == "not_managed"


def test_a_job_edit_that_fails_validation_saves_nothing(pcfg):
    cfg, fake = pcfg
    edit = _manga_edit(cfg, {"type": "days", "days": 30})
    pv = lc.preview(cfg, BASE, edit)
    before = Path(cfg["CONFIG_DIR"], "jobs.json").read_bytes()
    Path(cfg["SOURCE_ROOT"], "media", "manga").rmdir()    # the source vanished since
    puts = len(fake.puts())
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "invalid" and len(fake.puts()) == puts
    assert Path(cfg["CONFIG_DIR"], "jobs.json").read_bytes() == before
    assert lc.load_preview(cfg["CACHE_DIR"], pv.token) is not None   # kept on invalid: fix it and retry


# --- fix round 1 (task-13-fix1.md) -----------------------------------------------------------

def test_i1_a_concurrent_change_after_the_check_is_caught_before_the_write(pcfg, monkeypatch):
    """I1(a): _reconcile_locked re-reads jobs.json itself; a job save that lands in the window
    between apply_confirmed's own pre-check and that re-read must never ride along ungated."""
    cfg, fake = pcfg
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 7}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    real = lc.discard_preview
    def sneak_in(cache, token):
        real(cache, token)
        _set_manga(cfg, {"type": "days", "days": 30})   # a concurrent job save lands right here
    monkeypatch.setattr(lc, "discard_preview", sneak_in)
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "stale"
    # nothing that keeps less was written -- both stay at their old, longer rule
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    # but the settings edit itself WAS saved
    assert lc.load_settings(cfg["CONFIG_DIR"])["buckets"][BASE]["folders"]["appdata/"]["undo_days"] == 7


def test_i1_a_job_edit_normalizes_the_name_like_jobs_io_will(pcfg):
    """I1(b): edited() must normalize a job edit (e.g. trim the name) exactly as jobs_io.upsert's
    validate() will, so the preview's target is computed from the job as it will really be saved."""
    cfg, fake = pcfg
    _summary(cfg)
    job = next(j for j in _jobs(cfg) if j["name"] == "manga")
    edit = {"kind": "job", "job": dict(job, name="manga ", retention={"type": "days", "days": 30})}
    pv = lc.preview(cfg, BASE, edit)
    assert M in [c.folder for c in pv.keeps_less]        # the real folder, name trimmed like upsert will
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert [j["name"] for j in _jobs(cfg)].count("manga") == 1
    assert next(j for j in _jobs(cfg) if j["name"] == "manga")["retention"] == {"type": "days", "days": 30}


def test_i2_an_old_summary_still_needs_the_typed_name(pcfg):
    cfg, _ = pcfg
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket": BASE, "folder": M, "noncurrent_by_age_days": [], "noncurrent_by_rank": [],
        "noncurrent_by_age_rank": [], "noncurrent_versions": 0, "noncurrent_bytes": 0,
        "delete_markers": 0, "current_objects": 0, "current_bytes": 0})
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    assert pv.impacts["backup-engine:media/manga/"]["versions"] == 0
    assert pv.needs_typed is True                          # 8 days old: counts as missing


def test_i2_a_one_day_old_summary_does_not_need_the_typed_name(pcfg):
    cfg, _ = pcfg
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket": BASE, "folder": M, "noncurrent_by_age_days": [], "noncurrent_by_rank": [],
        "noncurrent_by_age_rank": [], "noncurrent_versions": 0, "noncurrent_bytes": 0,
        "delete_markers": 0, "current_objects": 0, "current_bytes": 0})
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    assert pv.impacts["backup-engine:media/manga/"]["versions"] == 0
    assert pv.needs_typed is False


def test_preview_rejects_a_bucket_that_is_not_this_installs(pcfg):
    cfg, _ = pcfg
    with pytest.raises(lc.PreviewError) as e:
        lc.preview(cfg, "not-a-real-bucket", {"kind": "confirm"})
    assert e.value.kind == "invalid"


def test_own_waiting_split_a_keeps_more_edit_leaves_a_waiting_change_untouched(pcfg):
    """A keeps-more edit must not silently grab a token for an unrelated change that's
    already waiting -- Task 15's route runs a GATED sync when token is None, which leaves
    a waiting item waiting rather than writing it."""
    cfg, fake = pcfg
    _set_manga(cfg, {"type": "days", "days": 30})
    lc.sync(cfg, BASE, run=fake)                          # manga waits (gated)
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 60}}}   # unrelated: keeps more
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert pv.token is None
    assert M in [c.folder for c in pv.keeps_less]         # still shown -- informational


def test_own_waiting_split_a_keeps_less_edit_gets_a_token_and_lists_both(pcfg):
    cfg, fake = pcfg
    _set_manga(cfg, {"type": "days", "days": 30})
    lc.sync(cfg, BASE, run=fake)                          # manga waits
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 7}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert pv.token is not None
    assert sorted(c.folder for c in pv.keeps_less) == ["appdata/", M]
    assert [c.folder for c in pv.own] == ["appdata/"]


def test_an_s3_failure_after_the_save_still_saved_and_leaves_it_waiting(pcfg):
    from types import SimpleNamespace
    cfg, fake = pcfg
    _summary(cfg)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    def boom(args, **kw):
        if "put-bucket-lifecycle-configuration" in args:
            return SimpleNamespace(returncode=255, stdout="", stderr="AccessDenied")
        return fake(args, **kw)
    with pytest.raises(lc.LifecycleError):
        lc.apply_confirmed(cfg, pv.token, BASE, run=boom)
    assert next(j for j in _jobs(cfg) if j["name"] == "manga")["retention"] == {"type": "days", "days": 30}
    assert lc.load_preview(cfg["CACHE_DIR"], pv.token) is None
    waiting_state, waiting = lc.outstanding(cfg, BASE)
    assert [c.folder for c in waiting] == [M]


def test_a_storage_json_change_makes_the_preview_stale(pcfg):
    cfg, fake = pcfg
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 7}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    other = lc.load_settings(cfg["CONFIG_DIR"])
    other["buckets"][f"{BASE}-other"] = {"folders": {}}      # an unrelated settings write lands in between
    lc.save_settings(cfg["CONFIG_DIR"], other)
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "stale"


def test_aba_an_applied_change_between_preview_and_confirm_is_caught(pcfg):
    """jobs.json ends up byte-identical to what it was at preview time, but the applied
    baseline moved in between (a concurrent sync) -- a byte-only hash would miss this."""
    cfg, fake = pcfg
    _summary(cfg)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    _set_manga(cfg, {"type": "days", "days": 200})
    lc.sync(cfg, BASE, run=fake)                          # moves the applied baseline to 200
    _set_manga(cfg, {"type": "days", "days": 180})        # jobs.json back to its original bytes
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "stale"


def test_needs_typed_false_applies_without_typing(pcfg):
    cfg, fake = pcfg
    _summary(cfg)                                          # nothing older than 100 days
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 150}))
    assert pv.needs_typed is False
    res = lc.apply_confirmed(cfg, pv.token, "", run=fake)   # no typing needed
    assert res.changed is True
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 150}


def test_typed_must_be_a_string(pcfg):
    cfg, fake = pcfg
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, ["x"], run=fake)
    assert e.value.kind == "typed"


@pytest.mark.parametrize("edit", [
    {"kind": "job"},                                            # job kind needs a job
    {"kind": "job", "job": "nope"},
    {"kind": "settings"},                                       # settings kind needs settings
    {"kind": "settings", "settings": "nope"},
    {"kind": "settings", "job": {"name": "x"}, "settings": {}}, # settings kind must carry no job
    {"kind": "confirm", "job": {"name": "x"}},                  # confirm carries neither
    {"kind": "confirm", "settings": {}},
    {"kind": "bogus"},
    "not-a-dict",
])
def test_edited_enforces_kind_and_payload(pcfg, edit):
    cfg, _ = pcfg
    with pytest.raises(lc.PreviewError) as e:
        lc.preview(cfg, BASE, edit)
    assert e.value.kind == "invalid"


def test_save_edit_takes_settings_lock_and_renders_the_crontab(pcfg, monkeypatch):
    cfg, _ = pcfg
    lock_calls = []
    real_lock = lc.settings_lock
    def spy_lock(config_dir):
        lock_calls.append(1)
        return real_lock(config_dir)
    monkeypatch.setattr(lc, "settings_lock", spy_lock)
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 14}}}
    lc.save_edit(cfg, {"kind": "settings", "settings": settings})
    assert lock_calls == [1]
    assert lc.load_settings(cfg["CONFIG_DIR"])["buckets"][BASE]["folders"]["appdata/"]["undo_days"] == 14

    render_calls = []
    real_render = lc.jobs_io.render_crontab
    def spy_render(*a, **k):
        render_calls.append((a, k))
        return real_render(*a, **k)
    monkeypatch.setattr(lc.jobs_io, "render_crontab", spy_render)
    job = next(j for j in _jobs(cfg) if j["name"] == "manga")
    lc.save_edit(cfg, {"kind": "job", "job": dict(job, retention={"type": "days", "days": 200})})
    assert len(render_calls) == 1


def test_apply_confirmed_takes_settings_lock_exactly_once_for_a_settings_edit(pcfg, monkeypatch):
    """flock is per open file description: taking settings_lock twice in the same process
    (once in apply_confirmed's check-then-save window, again inside save_edit) would
    self-deadlock -- save_edit must use the unlocked inner path when the caller already
    holds the lock. fix round 2, Minor 5: assert the lock is actually HELD while the save
    runs, not just acquired-and-released once -- a fresh, independent, NON-BLOCKING
    (LOCK_NB) flock attempt on the same lock file must fail while _save_edit_unlocked is
    in flight; LOCK_NB means a regression fails the assertion instead of hanging."""
    cfg, fake = pcfg
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 7}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    lock_calls = []
    real_lock = lc.settings_lock
    def spy_lock(config_dir):
        lock_calls.append(1)
        return real_lock(config_dir)
    monkeypatch.setattr(lc, "settings_lock", spy_lock)
    lock_path = Path(cfg["CONFIG_DIR"], f".{lc.SETTINGS_FILE}.lock")
    probe = {}
    real_unlocked = lc._save_edit_unlocked
    def probe_while_saving(cfg_, edit):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)   # non-blocking: never hangs
                probe["got_it"] = True                           # would mean the lock was NOT held
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                probe["got_it"] = False                          # the real lock was held -- correct
        return real_unlocked(cfg_, edit)
    monkeypatch.setattr(lc, "_save_edit_unlocked", probe_while_saving)
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert lock_calls == [1]
    assert probe.get("got_it") is False


# --- fix round 2 (task-13-fix2.md) -----------------------------------------------------------

def test_only_the_target_hash_differing_is_enough_to_go_stale(pcfg):
    """Minor 4: isolate the target_hash check from the inputs_hash check -- change ONLY the
    token's stored target_hash (leave the on-disk jobs.json/storage.json/applied record
    exactly as they were at preview time, so inputs_hash still matches) and confirm that
    alone is enough to raise stale and write nothing."""
    cfg, fake = pcfg
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    p = _token_path(cfg, pv.token)
    tok = json.loads(p.read_text())
    tok["target_hash"] = "not-the-real-hash"
    p.write_text(json.dumps(tok))
    puts = len(fake.puts())
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "stale"
    assert len(fake.puts()) == puts
    assert next(j for j in _jobs(cfg) if j["name"] == "manga")["retention"] == {"type": "days", "days": 180}


@pytest.mark.parametrize("edit", [
    {"kind": "job"},
    {"kind": "job", "job": "nope"},
    {"kind": "settings"},
    {"kind": "settings", "settings": "nope"},
    {"kind": "settings", "job": {"name": "x"}, "settings": {}},
    {"kind": "confirm", "job": {"name": "x"}},
    {"kind": "confirm", "settings": {}},
    {"kind": "bogus"},
    "not-a-dict",
])
def test_save_edit_enforces_kind_and_payload(pcfg, edit):
    """Minor 2: save_edit is a public, standalone entry point (Task 15's keeps-more-edit
    caller uses it directly, without going through edited()/preview() first) and must enforce
    the same kind<->payload shape edited() does."""
    cfg, _ = pcfg
    before = Path(cfg["CONFIG_DIR"], "jobs.json").read_bytes()
    with pytest.raises(ValueError):
        lc.save_edit(cfg, edit)
    assert Path(cfg["CONFIG_DIR"], "jobs.json").read_bytes() == before


def test_own_counts_an_edit_that_further_tightens_an_already_waiting_rule(pcfg):
    """Minor 6: manga is already waiting (baseline 180, current on-disk 30, gated); an edit
    that tightens it FURTHER, to 10, must still count as `own` -- the edit alters that very
    rule relative to what's on disk right now -- and get a token, even though the rule was
    already keeps-less without the edit."""
    cfg, fake = pcfg
    _set_manga(cfg, {"type": "days", "days": 30})
    lc.sync(cfg, BASE, run=fake)                          # manga waits: baseline 180, current 30
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 10}))   # tightens further
    assert pv.token is not None
    assert [c.folder for c in pv.own] == [M]
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 10}


def test_own_still_excludes_an_unrelated_waiting_rule_the_edit_never_touches(pcfg):
    """Regression guard for the Minor 6 fix: the pure "keeps-more edit with something else
    already waiting" case (own round 1's own test) must still get no token."""
    cfg, fake = pcfg
    _set_manga(cfg, {"type": "days", "days": 30})
    lc.sync(cfg, BASE, run=fake)                          # manga waits (gated)
    settings = lc.load_settings(cfg["CONFIG_DIR"])
    settings["buckets"][BASE] = {"folders": {"appdata/": {"undo_days": 60}}}   # unrelated: keeps more
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert pv.token is None
    assert M in [c.folder for c in pv.keeps_less]         # still shown -- informational
