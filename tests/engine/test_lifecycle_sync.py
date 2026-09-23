# tests/engine/test_lifecycle_sync.py — applying S3 rules through the role (spec §2).
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.engine import lifecycle as lc

BASE = "unraid-backup-123456789012"
ROLE = "arn:aws:iam::123456789012:role/backup-engine-bucket-admin"


class FakeS3:
    """sts assume-role + get/put-bucket-lifecycle-configuration, per bucket."""
    def __init__(self, rules=None, *, unsupported=False, deny_put=False, deny_assume=False):
        self.rules = {b: list(r) for b, r in (rules or {}).items()}
        self.unsupported, self.deny_put, self.deny_assume = unsupported, deny_put, deny_assume
        self.calls = []

    def __call__(self, args, *, region, key, secret, session_token=None):
        self.calls.append(list(args))
        if args[:2] == ["sts", "assume-role"]:
            if self.deny_assume:
                return SimpleNamespace(returncode=254, stdout="", stderr="AccessDenied sts:AssumeRole")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Credentials": {
                "AccessKeyId": "ASIAROLE", "SecretAccessKey": "rolesecret", "SessionToken": "roletok"}}))
        assert session_token == "roletok", f"bucket calls must run as the role: {args}"
        bucket = args[args.index("--bucket") + 1]
        if self.unsupported:
            return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (NotImplemented)")
        if args[:2] == ["s3api", "get-bucket-lifecycle-configuration"]:
            if not self.rules.get(bucket):
                return SimpleNamespace(returncode=254, stdout="",
                                       stderr="An error occurred (NoSuchLifecycleConfiguration)")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Rules": self.rules[bucket]}))
        if args[:2] == ["s3api", "put-bucket-lifecycle-configuration"]:
            if self.deny_put:
                return SimpleNamespace(returncode=254, stdout="",
                                       stderr="AccessDenied s3:PutLifecycleConfiguration rolesecret")
            self.rules[bucket] = json.loads(args[args.index("--lifecycle-configuration") + 1])["Rules"]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    def puts(self):
        return [c for c in self.calls if c[:2] == ["s3api", "put-bucket-lifecycle-configuration"]]


@pytest.fixture
def cfg(tmp_path):
    conf, cache = tmp_path / "config", tmp_path / "cache"
    conf.mkdir(); (cache / "state").mkdir(parents=True)
    (conf / "backup.env").write_text(
        f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\nBUCKET_ADMIN_ROLE_ARN={ROLE}\nPERMISSIONS_VERSION=4\n")
    (conf / "secrets.env").write_text("AWS_ACCESS_KEY_ID=AKIARUN\nAWS_SECRET_ACCESS_KEY=runsek\n")
    (conf / "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
         "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": 180}},
        {"name": "appdata_backups", "type": "versioned", "source": "appdata", "schedule": "0 5 * * *",
         "enabled": True, "storage_class": "STANDARD",
         "retention": {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}},
    ]}))
    return {"CONFIG_DIR": str(conf), "CACHE_DIR": str(cache)}


def _events(cfg):
    p = Path(cfg["CACHE_DIR"], "state", "_system.runs.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


LEGACY = [{"ID": "backstop-appdata", "Status": "Enabled", "Filter": {"Prefix": "appdata/"},
           "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
           "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
          {"ID": "backstop-media", "Status": "Enabled", "Filter": {"Prefix": "media/"},
           "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
           "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]
CONSOLE = {"ID": "archive-old-logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"},
           "Expiration": {"Days": 14}}


def test_first_sync_replaces_the_legacy_backstops_and_keeps_console_rules(cfg):
    fake = FakeS3({BASE: LEGACY + [CONSOLE]})
    res = lc.sync(cfg, BASE, run=fake)
    assert res.changed is True
    ids = [r["ID"] for r in fake.rules[BASE]]
    assert "backstop-appdata" not in ids and "backstop-media" not in ids
    assert fake.rules[BASE][0] == CONSOLE
    assert {"backup-engine:media/manga/", "backup-engine:appdata/", "backup-engine:housekeeping"} <= set(ids)
    assert lc.load_applied(cfg["CACHE_DIR"], BASE) == [r for r in fake.rules[BASE] if lc.is_app_rule(r)]
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["state"] == "ok"
    ev = [e for e in _events(cfg) if e["kind"] == "s3-rules"]
    assert [e["event"] for e in ev] == ["start", "end"]
    assert any("180 days" in l for l in res.lines)


def test_second_sync_changes_nothing(cfg):
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)
    res = lc.sync(cfg, BASE, run=fake)
    assert res.changed is False and len(fake.puts()) == 1


def test_empty_bucket_gets_rules(cfg):
    fake = FakeS3()
    assert lc.sync(cfg, BASE, run=fake).changed is True
    assert len(fake.puts()) == 1


def test_not_managed_below_level_four(cfg):
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text().replace("PERMISSIONS_VERSION=4", "PERMISSIONS_VERSION=3"))
    fake = FakeS3()
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=fake)
    assert e.value.kind == "not_managed" and fake.calls == []


def test_not_managed_without_a_role(cfg):
    env = Path(cfg["CONFIG_DIR"], "backup.env")
    env.write_text(env.read_text().replace(f"BUCKET_ADMIN_ROLE_ARN={ROLE}", "BUCKET_ADMIN_ROLE_ARN="))
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=FakeS3())
    assert e.value.kind == "not_managed"


def test_unsupported_storage_is_recorded(cfg):
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=FakeS3(unsupported=True))
    assert e.value.kind == "unsupported"
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["state"] == "unsupported"


def test_role_failure_is_scrubbed_and_recorded(cfg):
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=FakeS3(deny_put=True))
    assert e.value.kind == "aws" and "rolesecret" not in e.value.detail
    assert lc.load_status(cfg["CACHE_DIR"])[BASE]["state"] == "error"


def test_assume_role_failure(cfg):
    with pytest.raises(lc.LifecycleError) as e:
        lc.sync(cfg, BASE, run=FakeS3(deny_assume=True))
    assert e.value.kind == "role"


def test_sync_all_covers_the_base_and_dedicated_buckets(cfg):
    jobs_p = Path(cfg["CONFIG_DIR"], "jobs.json")
    data = json.loads(jobs_p.read_text())
    data["jobs"].append({"name": "photos", "type": "archive", "source": "media/photos",
                         "schedule": "0 3 * * *", "enabled": True, "storage_class": "STANDARD",
                         "retention": {"type": "count", "count": 10},
                         "dedicated": True, "bucket": f"{BASE}-photos", "bucket_versioned": True})
    jobs_p.write_text(json.dumps(data))
    fake = FakeS3()
    results = lc.sync_all(cfg, run=fake)
    assert [r.bucket for r in results] == [BASE, f"{BASE}-photos"]
    assert fake.rules[f"{BASE}-photos"][0]["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": 1, "NewerNoncurrentVersions": 10}


def test_sync_never_touches_objects_or_versions(cfg):
    fake = FakeS3({BASE: LEGACY})
    lc.sync(cfg, BASE, run=fake)
    for c in fake.calls:
        assert c[1] in ("assume-role", "get-bucket-lifecycle-configuration",
                        "put-bucket-lifecycle-configuration"), c
