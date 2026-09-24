# tests/engine/test_lifecycle_final_wave.py — the Phase B/C final fix wave (findings I1–I5,
# M1, M2, M8, M9, M12, P1, P4, P7): the unattended pass never writes from an unreadable
# config, one definition of a "known" folder, no versioning write for a bucket no job uses,
# guided-manual console rules in the first-apply baseline, and alarm notifications.
import json
from pathlib import Path

import pytest
from app.engine import lifecycle as lc
from tests.engine.test_lifecycle_sync import (BASE, LEGACY, FakeS3, _events, _live,  # noqa: F401
                                              _set_manga, cfg)

M = "media/manga/"


def _ran(cfg, *job_names):
    for name in job_names:
        Path(cfg["CACHE_DIR"], "state", f"{name}.json").write_text("{}")


def _jobs(cfg):
    return json.loads(Path(cfg["CONFIG_DIR"], "jobs.json").read_text())["jobs"]


def _write_jobs(cfg, jobs):
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))


def _status(cfg, bucket=BASE):
    return lc.load_status(cfg["CACHE_DIR"]).get(bucket) or {}


# --- I1: the unattended pass never writes S3 from an unreadable config ------------------------

def test_a_corrupt_jobs_file_writes_nothing_and_says_so(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3({BASE: LEGACY})
    assert lc.check(cfg, BASE, run=fake) == "ok"                     # the owner's migration
    before = json.loads(json.dumps(fake.rules[BASE]))
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text("{not json")
    fake.calls.clear()
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.calls == []                                          # not even a read
    assert fake.rules[BASE] == before                               # every folder rule still there
    st = _status(cfg)
    assert st["state"] == "error" and "jobs file couldn't be read" in st["detail"]
    assert "S3 rules left as they are" in st["detail"]


def test_probe1_fixing_the_jobs_file_leaves_nothing_waiting(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    good = Path(cfg["CONFIG_DIR"], "jobs.json").read_text()
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text("{not json")
    lc.check(cfg, BASE, run=fake)
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(good)
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert lc.outstanding(cfg, BASE) == (False, [])
    assert len(fake.puts()) == 1                                     # only the migration ever wrote


def test_a_job_entry_with_an_unusable_name_counts_as_an_unreadable_jobs_file(cfg):
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    jobs = _jobs(cfg)
    jobs.append(dict(jobs[0], name="bad name"))
    _write_jobs(cfg, jobs)
    fake.calls.clear()
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.calls == [] and "jobs file couldn't be read" in _status(cfg)["detail"]


@pytest.mark.parametrize("text", ["{oops", "[]", '{"buckets": []}', '{"buckets": {"b": 7}}',
                                  '{"buckets": {"b": {"folders": []}}}',
                                  '{"buckets": {"b": {"folders": {"appdata/": 30}}}}'])
def test_an_unreadable_settings_file_writes_nothing(cfg, text):
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    Path(cfg["CONFIG_DIR"], "storage.json").write_text(text)
    fake.calls.clear()
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.calls == []
    assert "S3 rules settings couldn't be read" in _status(cfg)["detail"]
    assert Path(cfg["CONFIG_DIR"], "storage.json").read_text() == text        # never overwritten


def test_a_missing_settings_file_is_the_defaults(cfg):
    assert lc.load_settings_strict(cfg["CONFIG_DIR"]) == {"version": 1, "buckets": {}}


def test_probe7_a_confirmed_suspend_and_tier_survive_an_unreadable_settings_file(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake)
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended", "folders": {
        M: {"tier": {"class": "DEEP_ARCHIVE", "after_days": 30}}}}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert fake.versioning[BASE] == "Suspended"
    rules = json.loads(json.dumps(fake.rules[BASE]))
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    assert lc.check(cfg, BASE, run=fake) == "error"
    assert fake.versioning[BASE] == "Suspended" and fake.rules[BASE] == rules


def test_a_job_save_sync_on_an_unreadable_settings_file_raises_a_config_error(cfg):
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    fake = FakeS3()
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=fake)
    assert e.value.kind == "config" and fake.calls == []


def test_check_all_on_a_corrupt_jobs_file_writes_nothing(cfg, monkeypatch):
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text("{not json")
    fake.calls.clear()
    real = lc.check
    monkeypatch.setattr(lc, "check", lambda c, b, **kw: real(c, b, run=fake, **kw))
    lines = lc.check_all_lines(cfg)
    assert lines == [f"S3 rules check · {BASE}: couldn't be checked (see Setup)"]
    assert fake.calls == []


def test_a_first_apply_seed_never_overwrites_an_unreadable_settings_file(cfg, tmp_path):
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    long = [dict(r, NoncurrentVersionExpiration={"NoncurrentDays": 90}) for r in LEGACY]
    with pytest.raises(lc.SettingsFileError):
        lc.seed_undo_days(cfg["CONFIG_DIR"], BASE, long, lc.folders_for(BASE, BASE, _jobs(cfg)))
    assert Path(cfg["CONFIG_DIR"], "storage.json").read_text() == "{oops"


# --- I1: a folder whose job's history setting can't be read holds its baseline rule ---------

def test_an_unreadable_history_setting_holds_the_rule_s3_already_has(cfg):
    _ran(cfg, "manga")
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)                                     # manga 180 days applied
    _set_manga(cfg, {"type": "count", "count": "x"})                  # hand-edited, unreadable
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert len(fake.puts()) == 1
    assert lc.outstanding(cfg, BASE) == (False, [])
    assert "manga" in _status(cfg)["detail"]


def test_an_unreadable_history_setting_on_a_first_apply_keeps_what_the_legacy_rule_does(cfg):
    _ran(cfg, "manga")
    _set_manga(cfg, {"type": "nope"})
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


def test_an_unreadable_history_setting_is_held_through_a_confirmed_apply(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3({BASE: LEGACY})
    lc.check(cfg, BASE, run=fake)
    _set_manga(cfg, {"type": "count", "count": "x"})
    settings = {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 7}}}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    assert [c.folder for c in pv.keeps_less] == ["appdata/"]           # manga isn't a change
    lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}


def test_a_settings_edit_never_overwrites_an_unreadable_settings_file(cfg):
    # The GUI reads storage.json fail-safe (defaults); saving an edit built on those defaults
    # would silently replace the owner's (hand-fixable) file -- refused, like jobs.json's.
    Path(cfg["CONFIG_DIR"], "storage.json").write_text("{oops")
    edit = {"kind": "settings", "settings": {"version": 1, "buckets": {BASE: {"abort_uploads_days": 9}}}}
    with pytest.raises(ValueError) as e:
        lc.save_edit(cfg, edit)
    assert "storage.json" in str(e.value)
    assert Path(cfg["CONFIG_DIR"], "storage.json").read_text() == "{oops"


# --- I2: one definition of a "known" folder on every pass ------------------------------------

def _add_job(cfg, name, retention, **kw):
    jobs = _jobs(cfg)
    jobs.append(dict({"name": name, "type": "archive", "source": f"media/{name}", "schedule": "0 3 * * *",
                      "enabled": True, "storage_class": "STANDARD", "retention": retention}, **kw))
    _write_jobs(cfg, jobs)


def _set(cfg, name, retention):
    jobs = _jobs(cfg)
    for j in jobs:
        if j["name"] == name:
            j["retention"] = retention
    _write_jobs(cfg, jobs)


def _rule_ids(fake, bucket=BASE):
    return {r["ID"] for r in fake.rules.get(bucket, [])}


def test_probe4_a_job_that_ran_after_a_failed_save_sync_waits_for_its_shortened_rule(cfg):
    _write_jobs(cfg, [])
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake)                            # setup's first apply: housekeeping only
    _add_job(cfg, "tv", {"type": "days", "days": 180})
    fake.deny_put = True
    assert lc.check(cfg, BASE, run=fake) == "error"          # the save-time sync failed
    fake.deny_put = False
    _ran(cfg, "tv")                                          # ...and the job ran for weeks
    _set(cfg, "tv", {"type": "days", "days": 7})             # the owner shortens it
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert "backup-engine:media/tv/" not in _rule_ids(fake)  # S3 keeps everything until confirmed
    _, waiting = lc.outstanding(cfg, BASE)
    assert [c.folder for c in waiting] == ["media/tv/"]
    pv = lc.preview(cfg, BASE, {"kind": "confirm"})
    assert pv.token and [c.folder for c in pv.keeps_less] == ["media/tv/"]


def test_a_job_that_ran_without_its_first_rule_ever_applied_waits_for_that_rule_too(cfg):
    _write_jobs(cfg, [])
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake)
    _add_job(cfg, "tv", {"type": "days", "days": 180})
    _ran(cfg, "tv")
    lc.check(cfg, BASE, run=fake)
    assert "backup-engine:media/tv/" not in _rule_ids(fake)
    assert [c.folder for c in lc.outstanding(cfg, BASE)[1]] == ["media/tv/"]


RUN = "20260924T030000Z-ab12"


def _runs(cfg, name, *ids):
    Path(cfg["CACHE_DIR"], "state", f"{name}.runs.jsonl").write_text(
        "".join(json.dumps({"v": 1, "id": i, "job": name, "event": "start"}) + "\n" for i in ids))


def test_probe3_a_new_jobs_own_first_run_applies_its_first_rule(cfg):
    _write_jobs(cfg, [])
    _add_job(cfg, "manga", {"type": "days", "days": 180})
    _runs(cfg, "manga", RUN)                                 # runs_start wrote it just before the check
    fake = FakeS3()
    assert lc.check(cfg, BASE, run=fake, run_id=RUN) == "ok"
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert lc.outstanding(cfg, BASE) == (False, [])


def test_an_earlier_run_still_makes_the_folder_known(cfg):
    _write_jobs(cfg, [])
    _add_job(cfg, "manga", {"type": "days", "days": 180})
    _runs(cfg, "manga", "20260901T030000Z-0000", RUN)       # a paused/aborted earlier run
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake, run_id=RUN)
    assert "backup-engine:media/manga/" not in _rule_ids(fake)


def test_an_unreadable_runs_line_counts_as_a_run(cfg):
    _write_jobs(cfg, [])
    _add_job(cfg, "manga", {"type": "days", "days": 180})
    Path(cfg["CACHE_DIR"], "state", "manga.runs.jsonl").write_text("{torn\n")
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake, run_id=RUN)
    assert "backup-engine:media/manga/" not in _rule_ids(fake)


def test_without_a_run_id_any_run_record_counts(cfg):
    _write_jobs(cfg, [])
    _add_job(cfg, "manga", {"type": "days", "days": 180})
    _runs(cfg, "manga", RUN)                                 # e.g. the hourly check during a first run
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake)
    assert "backup-engine:media/manga/" not in _rule_ids(fake)


def test_the_check_cli_passes_the_runs_id_through(cfg, monkeypatch, capsys):
    monkeypatch.setenv("CONFIG_DIR", cfg["CONFIG_DIR"])
    monkeypatch.setenv("CACHE_DIR", cfg["CACHE_DIR"])
    monkeypatch.setenv("BE_RUN_ID", RUN)
    seen = {}

    def fake_check(c, b, **kw):
        seen.update(kw)
        return "ok"
    monkeypatch.setattr(lc, "check", fake_check)
    assert lc.main(["check", "--bucket", BASE]) == 0
    assert seen.get("run_id") == RUN


# --- I3: never write versioning for a bucket no job uses -------------------------------------

DED = BASE + "-tv"


def test_probe5_deleting_a_dedicated_job_never_turns_its_buckets_versioning_on(cfg):
    _write_jobs(cfg, [{"name": "tv", "type": "archive", "source": "media/tv", "schedule": "0 3 * * *",
                       "enabled": True, "storage_class": "STANDARD", "dedicated": True, "bucket": DED,
                       "bucket_versioned": False, "retention": {"type": "days", "days": 30}}])
    fake = FakeS3({}, versioning={DED: None})
    lc.sync(cfg, DED, run=fake)
    assert fake.versioning[DED] is None
    _write_jobs(cfg, [])                                     # routes.job_delete -> apply_for(its bucket)
    res = lc.sync(cfg, DED, run=fake)
    assert fake.versioning[DED] is None                      # never versioned: still never versioned
    assert [c for c in fake.calls if c[:2] == ["s3api", "put-bucket-versioning"]] == []
    assert not any("versioning" in line for line in res.lines)


@pytest.mark.parametrize("stored", [None, "on", "suspended"])
def test_a_bucket_no_job_uses_has_no_versioning_intent(stored):
    settings = {"buckets": {DED: {"versioning": stored}}} if stored else {}
    assert lc.versioning_intent(DED, BASE, [], settings) is None
    assert lc.desired(DED, BASE, [], settings).versioning is None
    assert lc.versioning_intent(BASE, BASE, [], {}) == "on"            # the base bucket always has one


# --- I4: guided-manual installs' own console rules ---------------------------------------------

GUIDED = [{"ID": "expire-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
           "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
           "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
          {"ID": "expire-appdata", "Status": "Enabled", "Filter": {"Prefix": "appdata/"},
           "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
           "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]


def _guided_jobs(cfg):
    _write_jobs(cfg, [
        {"name": "photos", "type": "archive", "source": "media/photos", "schedule": "0 3 * * *",
         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "keep_all"}},
        {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": 180}},
        {"name": "appdata_backups", "type": "versioned", "source": "appdata", "schedule": "0 5 * * *",
         "enabled": True, "storage_class": "STANDARD",
         "retention": {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}}])
    _ran(cfg, "photos", "manga", "appdata_backups")


def test_probe6_a_guided_installs_console_rules_count_in_the_first_apply_baseline(cfg):
    _guided_jobs(cfg)
    fake = FakeS3({BASE: json.loads(json.dumps(GUIDED))})
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert lc.outstanding(cfg, BASE) == (False, [])          # not a wall of confirmations
    assert fake.rules[BASE][:2] == GUIDED                    # console rules kept byte-for-byte
    assert _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 180}
    assert "backup-engine:media/photos/" not in _rule_ids(fake)
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}


def _base(live, folders=(M,), known=None):
    want = lc.RuleSet({}, frozenset(folders))
    return lc.baseline_from_live(live, want, known)


def test_only_a_plain_prefix_covering_the_folder_counts():
    tag = dict(GUIDED[0], ID="tagged", Filter={"And": {"Prefix": "media/", "Tags": [{"Key": "k", "Value": "v"}]}})
    size = dict(GUIDED[0], ID="small", Filter={"ObjectSizeGreaterThan": 10})
    inner = dict(GUIDED[0], ID="inner", Filter={"Prefix": "media/manga/sub/"})
    off = dict(GUIDED[0], ID="off", Status="Disabled")
    other = dict(GUIDED[0], ID="other", Filter={"Prefix": "media/tv/"})
    assert _base([tag, size, inner, off, other]).rules == {}
    whole = dict(GUIDED[0], ID="whole", Filter={"Prefix": ""})
    old_form = {k: v for k, v in GUIDED[0].items() if k != "Filter"} | {"ID": "old", "Prefix": "media/"}
    for rule in (GUIDED[0], whole, old_form):
        assert _base([rule]).rules["backup-engine:media/manga/"]["NoncurrentVersionExpiration"] == {
            "NoncurrentDays": 30}


def test_the_stricter_of_a_console_and_a_legacy_rule_is_the_baseline():
    console = dict(GUIDED[0], NoncurrentVersionExpiration={"NoncurrentDays": 10})
    rules = _base([console] + LEGACY).rules
    assert rules["backup-engine:media/manga/"]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 10}


def test_console_caps_name_a_console_rule_that_removes_versions_sooner_than_the_apps():
    rule180 = lc.plain_rule(M, {"type": "days", "days": 180})
    assert lc.console_caps(GUIDED, M, rule180) == [{"id": "expire-media", "days": 30, "newest": 0}]
    assert lc.console_caps(GUIDED, M, None) == [{"id": "expire-media", "days": 30, "newest": 0}]
    assert lc.console_caps(GUIDED, M, lc.plain_rule(M, {"type": "days", "days": 30})) == []
    assert lc.console_caps(GUIDED, M, lc.plain_rule(M, {"type": "days", "days": 7})) == []
    newest = dict(GUIDED[0], NoncurrentVersionExpiration={"NoncurrentDays": 90, "NewerNoncurrentVersions": 3})
    assert lc.console_caps([newest], M, rule180) == [{"id": "expire-media", "days": 90, "newest": 3}]
    off = dict(GUIDED[0], Status="Disabled")
    assert lc.console_caps([off], M, rule180) == []
    inner = dict(GUIDED[0], Filter={"Prefix": "media/manga/sub/"})
    assert lc.console_caps([inner], M, rule180)              # overlaps part of the folder: still a cap
    assert lc.console_caps([inner], "media/", None, covering=True) == []
    assert lc.console_caps(GUIDED, "media/", None, covering=True)
    app = lc.plain_rule(M, {"type": "days", "days": 1})
    assert lc.console_caps([app], M, rule180) == []          # the app's own rules never count


# --- I5: an alarm leaves the GUI -- the app's own notification, once per alarm ---------------

@pytest.fixture
def sent(cfg, monkeypatch):
    out = []
    monkeypatch.setattr(lc, "_send_notification", lambda urls, title, body: out.append((urls, title, body)) or True)
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text() + "APPRISE_URLS=ntfy://example/topic tgram://x\n")
    return out


def _applied_bucket(cfg, fake):
    lc.seed_new_bucket(cfg["CACHE_DIR"], BASE)
    lc.sync(cfg, BASE, run=fake)


def _tamper(fake):
    _live(fake, "backup-engine:media/manga/")["NoncurrentVersionExpiration"] = {"NoncurrentDays": 1}


def test_a_restored_tamper_is_notified_once(cfg, sent):
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert len(sent) == 1
    urls, title, body = sent[0]
    assert urls == ["ntfy://example/topic", "tgram://x"]
    assert "changed outside backup-engine" in title and "restored" in title and BASE in title
    assert "media/manga/" in body
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert len(sent) == 1


def test_a_not_restored_alarm_is_notified_once_while_it_persists(cfg, sent):
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    fake.deny_put = True
    assert lc.check(cfg, BASE, run=fake) == "not_restored"
    assert lc.check(cfg, BASE, run=fake) == "not_restored"
    assert len(sent) == 1 and "NOT restored" in sent[0][1]


def test_a_new_console_rule_is_notified_once(cfg, sent):
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    fake.rules[BASE].append({"ID": "wipe", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 1}})
    assert lc.check(cfg, BASE, run=fake) == "console_rule"
    assert lc.check(cfg, BASE, run=fake) == "ok"
    assert len(sent) == 1
    assert "could delete or move backups" in sent[0][1] and "wipe" in sent[0][2]
    assert "left it in place" in sent[0][2]


def test_an_acknowledged_alarm_that_happens_again_is_notified_again(cfg, sent):
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    lc.check(cfg, BASE, run=fake)
    lc.acknowledge(cfg["CACHE_DIR"])
    _tamper(fake)
    assert lc.check(cfg, BASE, run=fake) == "restored"
    assert len(sent) == 2


def test_a_failed_send_never_fails_the_check(cfg, monkeypatch):
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text() + "APPRISE_URLS=ntfy://example/topic\n")

    def boom(*a, **k):
        raise OSError("no apprise here")
    monkeypatch.setattr(lc, "_send_notification", boom)
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    assert lc.check(cfg, BASE, run=fake) == "restored"


def test_the_real_sender_never_raises(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("apprise")
    monkeypatch.setattr(subprocess, "run", boom)
    assert lc._send_notification(["x://y"], "t", "b") is False


def test_nothing_is_sent_without_apprise_urls(cfg, monkeypatch):
    monkeypatch.delenv("APPRISE_URLS", raising=False)
    monkeypatch.setattr(lc, "_send_notification", lambda *a: pytest.fail("no target configured"))
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    assert lc.check(cfg, BASE, run=fake) == "restored"


def test_the_container_env_is_used_when_backup_env_has_no_apprise_urls(cfg, monkeypatch):
    out = []
    monkeypatch.setattr(lc, "_send_notification", lambda urls, t, b: out.append(urls) or True)
    monkeypatch.setenv("APPRISE_URLS", "json://env/x")
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    lc.check(cfg, BASE, run=fake)
    assert out == [["json://env/x"]]


def test_a_job_save_sync_never_notifies(cfg, sent):
    fake = FakeS3()
    _applied_bucket(cfg, fake)
    _tamper(fake)
    assert lc.sync(cfg, BASE, run=fake).state == "restored"      # the owner is right there (a flash)
    assert sent == []


# --- M1: one hung call can't eat the budget; a killed check says so -------------------------

def test_lifecycle_aws_calls_retry_at_most_once_in_standard_mode(monkeypatch):
    import subprocess
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    lc._run_aws(["s3api", "get-bucket-versioning"], region="us-east-1", key="AK", secret="SK", session_token="T")
    assert seen["cmd"][0] == "aws"
    assert seen["env"]["AWS_MAX_ATTEMPTS"] == "2" and seen["env"]["AWS_RETRY_MODE"] == "standard"
    assert seen["env"]["AWS_ACCESS_KEY_ID"] == "AK" and seen["env"]["AWS_SESSION_TOKEN"] == "T"


@pytest.mark.parametrize("fn", ["role_creds", "read_lifecycle", "write_rules", "read_versioning",
                                "write_versioning", "sync", "sync_all", "check", "apply_confirmed"])
def test_every_lifecycle_entry_point_defaults_to_the_lifecycle_runner(fn):
    assert getattr(lc, fn).__kwdefaults__["run"] is lc._run_aws


def test_a_killed_check_records_that_it_timed_out(cfg):
    import signal
    lc._ACTIVE.update(cache=cfg["CACHE_DIR"], bucket=BASE)
    try:
        with pytest.raises(SystemExit):
            lc._on_term(signal.SIGTERM, None)
    finally:
        lc._ACTIVE.update(cache=None, bucket=None)
    st = _status(cfg)
    assert st["state"] == "error" and "timed out" in st["detail"]


def test_a_kill_while_the_status_file_is_locked_never_deadlocks(cfg):
    import signal
    import threading
    done = []

    def term():
        try:
            lc._on_term(signal.SIGTERM, None)
        except SystemExit:
            done.append(True)
    lc._ACTIVE.update(cache=cfg["CACHE_DIR"], bucket=BASE)
    try:
        with lc._status_lock(cfg["CACHE_DIR"]):            # e.g. killed inside set_status
            t = threading.Thread(target=term)
            t.start()
            t.join(5)
            assert not t.is_alive() and done == [True]
    finally:
        lc._ACTIVE.update(cache=None, bucket=None)


def test_a_kill_between_checks_records_nothing(cfg):
    import signal
    with pytest.raises(SystemExit):
        lc._on_term(signal.SIGTERM, None)
    assert _status(cfg) == {}


def test_the_cli_check_killed_by_timeout_records_timed_out(cfg, tmp_path):
    # End to end through the real CLI: a stand-in `aws` (never the real one) that hangs, and
    # `timeout` sending SIGTERM -- the handler records the state for the bucket being checked.
    import os
    import subprocess
    import sys
    import time
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_dir / "aws").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CONFIG_DIR=cfg["CONFIG_DIR"],
               CACHE_DIR=cfg["CACHE_DIR"], APPRISE_URLS="")
    start = time.monotonic()
    cp = subprocess.run(["timeout", "3", sys.executable, "-m", "app.engine.lifecycle", "check", "--bucket", BASE],
                        env=env, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2])
    assert time.monotonic() - start < 20
    assert cp.returncode != 0
    st = _status(cfg)
    assert st["state"] == "error" and "timed out" in st["detail"]


# --- M5: a confirmed apply whose rules landed but whose versioning put failed says so ---------

class _NoVersioningPut(FakeS3):
    def __call__(self, args, **kw):
        if args[:2] == ["s3api", "put-bucket-versioning"]:
            self.calls.append(list(args))
            from types import SimpleNamespace
            return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied s3:PutBucketVersioning")
        return super().__call__(args, **kw)


def test_a_partial_confirmed_apply_marks_the_error_rules_applied(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = _NoVersioningPut()
    lc.check(cfg, BASE, run=fake)
    settings = {"version": 1, "buckets": {BASE: {"versioning": "suspended",
                                                 "folders": {"appdata/": {"undo_days": 7}}}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    with pytest.raises(lc.LifecycleError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert e.value.rules_applied is True
    assert _live(fake, "backup-engine:appdata/")["NoncurrentVersionExpiration"] == {"NoncurrentDays": 7}


def test_a_failed_apply_that_wrote_nothing_is_not_marked_rules_applied(cfg):
    _ran(cfg, "manga", "appdata_backups")
    fake = FakeS3()
    lc.check(cfg, BASE, run=fake)
    settings = {"version": 1, "buckets": {BASE: {"folders": {"appdata/": {"undo_days": 7}}}}}
    pv = lc.preview(cfg, BASE, {"kind": "settings", "settings": settings})
    fake.deny_put = True
    with pytest.raises(lc.LifecycleError) as e:
        lc.apply_confirmed(cfg, pv.token, BASE, run=fake)
    assert getattr(e.value, "rules_applied", False) is False
