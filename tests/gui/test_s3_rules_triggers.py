# tests/gui/test_s3_rules_triggers.py — rules follow the jobs (spec §2 triggers).
from pathlib import Path

import pytest
from app.engine import lifecycle
from app.gui import config_io, create_app, s3_rules

BASE = "unraid-backup-123456789012"


@pytest.fixture
def cfg(tmp_path, template_path):
    conf, cache = tmp_path / "config", tmp_path / "cache"
    conf.mkdir(); (cache / "state").mkdir(parents=True)
    config_io.write_secrets(str(conf), {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "sek"})
    (conf / "backup.env").write_text(f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\n"
                                     "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::1:role/r\nPERMISSIONS_VERSION=4\n")
    return {"CONFIG_DIR": str(conf), "CACHE_DIR": str(cache), "TEMPLATE_PATH": template_path}


def test_apply_for_reports_changes(cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "sync", lambda c, b, **k: lifecycle.SyncResult(
        b, True, ["now: media/m/: old versions removed 180 days after being replaced"]))
    msgs = s3_rules.apply_for(cfg, [BASE])
    assert msgs == [("success", "S3 rules updated: now: media/m/: old versions removed 180 days after being replaced")]


def test_apply_for_is_silent_when_unchanged_or_not_managed(cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "sync", lambda c, b, **k: lifecycle.SyncResult(b, False, []))
    assert s3_rules.apply_for(cfg, [BASE]) == []

    def not_managed(c, b, **k):
        raise lifecycle.LifecycleError("not_managed")
    monkeypatch.setattr(lifecycle, "sync", not_managed)
    assert s3_rules.apply_for(cfg, [BASE]) == []


def test_apply_for_warns_on_failure_and_never_raises(cfg, monkeypatch):
    def boom(c, b, **k):
        raise lifecycle.LifecycleError("aws", "AccessDenied")
    monkeypatch.setattr(lifecycle, "sync", boom)
    (level, text), = s3_rules.apply_for(cfg, [BASE])
    assert level == "warning" and "couldn't be updated" in text


def test_job_buckets(cfg):
    assert s3_rules.job_buckets(cfg, {"name": "x"}) == [BASE]
    assert s3_rules.job_buckets(cfg, {"name": "x", "dedicated": True, "bucket": f"{BASE}-x"}) == [f"{BASE}-x"]


# --- routes -----------------------------------------------------------------------------

@pytest.fixture
def client(cfg, tmp_path):
    src = tmp_path / "src"; (src / "media" / "movies").mkdir(parents=True)
    app = create_app({"CONFIG_DIR": cfg["CONFIG_DIR"], "CACHE_DIR": cfg["CACHE_DIR"],
                      "SCRIPTS_DIR": "/app/scripts", "TEMPLATE_PATH": cfg["TEMPLATE_PATH"],
                      "SOURCE_ROOT": str(src), "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


def _csrf(client, path="/jobs/new"):
    client.get(path)
    with client.session_transaction() as s:
        return s["_csrf"]


def test_saving_a_job_applies_its_bucket_rules(client, monkeypatch):
    seen = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda cfg, buckets: seen.append(buckets) or [("success", "S3 rules updated: x")])
    r = client.post("/jobs", data={"csrf": _csrf(client), "name": "movies", "type": "archive",
                                   "source": "media/movies", "schedule": "0 5 * * *",
                                   "storage_class": "STANDARD", "enabled": "1",
                                   "retention_type": "days", "retention_days": "180"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert seen == [[BASE]]
    assert "S3 rules updated: x" in r.get_data(as_text=True)


def test_deleting_a_job_applies_its_bucket_rules(client, cfg, monkeypatch):
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(
        '{"jobs":[{"name":"movies","type":"archive","source":"media/movies","schedule":"0 5 * * *",'
        '"enabled":true,"storage_class":"STANDARD","retention":{"type":"days","days":180}}]}')
    seen = []
    monkeypatch.setattr(s3_rules, "apply_for", lambda cfg, buckets: seen.append(buckets) or [])
    r = client.post("/jobs/movies/delete", data={"csrf": _csrf(client)})
    assert r.status_code in (302, 303) and seen == [[BASE]]
