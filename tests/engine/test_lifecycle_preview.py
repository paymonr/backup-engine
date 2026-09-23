# tests/engine/test_lifecycle_preview.py — preview + typed confirmation (spec 2026-09-23 §3, R-B4).
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


def _summary(cfg, folder=M):
    storage_summary.save(cfg["CACHE_DIR"], {
        "v": 1, "scanned_at": "2026-09-23T05:00:00Z", "bucket": BASE, "folder": folder,
        "noncurrent_by_age_days": [[10, 5, 500], [100, 3, 300]], "noncurrent_by_rank": [[1, 8, 800]],
        "noncurrent_by_age_rank": [[10, 1, 5, 500], [100, 1, 3, 300]],
        "noncurrent_versions": 8, "noncurrent_bytes": 800, "delete_markers": 0,
        "current_objects": 8, "current_bytes": 8000})


def _token_path(cfg, token):
    return Path(cfg["CACHE_DIR"], "state", "lifecycle", "pending", f"{token}.json")


def test_a_change_that_keeps_more_needs_no_token(pcfg):
    cfg, fake = pcfg
    calls = len(fake.calls)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 365}))
    assert pv.token is None and pv.keeps_less == [] and [c.kind for c in pv.changes] == [lc.KEEPS_MORE]
    assert len(fake.calls) == calls                        # a preview never reaches S3


def test_a_preview_that_deletes_asks_for_the_bucket_name(pcfg):
    cfg, _ = pcfg
    _summary(cfg)
    pv = lc.preview(cfg, BASE, _manga_edit(cfg, {"type": "days", "days": 30}))
    (c,) = pv.keeps_less
    assert c.folder == M and pv.needs_typed is True and pv.token
    imp = pv.impacts[c.rule_id]
    assert (imp["versions"], imp["bytes"], imp["oldest_age_days"], imp["scanned_at"]) == \
        (3, 300, 100, "2026-09-23T05:00:00Z")
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
    Path(cfg["SOURCE_ROOT"], "media", "manga").rmdir()    # the source vanished since
    puts = len(fake.puts())
    with pytest.raises(lc.PreviewError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.kind == "invalid" and len(fake.puts()) == puts
