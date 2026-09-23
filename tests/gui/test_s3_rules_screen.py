# tests/gui/test_s3_rules_screen.py — the S3 rules screen and its actions (spec 2026-09-23 §3, §5).
import json
from pathlib import Path

import pytest
from app.engine import lifecycle, runs, storage_summary
from app.gui import config_io, create_app, ops

BASE = "unraid-backup-123456789012"
JOBS = [
    {"name": "manga", "type": "archive", "source": "media/manga", "schedule": "0 3 * * *",
     "enabled": True, "storage_class": "STANDARD", "retention": {"type": "days", "days": 180}},
    {"name": "appdata_backups", "type": "versioned", "source": "appdata", "schedule": "0 5 * * *",
     "enabled": True, "storage_class": "STANDARD",
     "retention": {"type": "tiered", "keep": {"last": 3, "daily": 7, "weekly": 4, "monthly": 6}}},
]
# Captured before any test patches them (the integrated tests put them back).
REAL = {n: getattr(lifecycle, n) for n in ("role_creds", "read_rules", "write_rules")}


@pytest.fixture
def cfg(tmp_path, template_path):
    conf, cache = tmp_path / "config", tmp_path / "cache"
    conf.mkdir(); (cache / "state").mkdir(parents=True)
    src = tmp_path / "src"; (src / "media" / "manga").mkdir(parents=True); (src / "appdata").mkdir()
    config_io.write_secrets(str(conf), {"AWS_ACCESS_KEY_ID": "AKIARUN", "AWS_SECRET_ACCESS_KEY": "runsek"})
    (conf / "backup.env").write_text(f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\n"
                                     "BUCKET_ADMIN_ROLE_ARN=arn:aws:iam::1:role/r\nPERMISSIONS_VERSION=4\n")
    (conf / "jobs.json").write_text(json.dumps({"jobs": JOBS}))
    return {"CONFIG_DIR": str(conf), "CACHE_DIR": str(cache), "SOURCE_ROOT": str(src),
            "TEMPLATE_PATH": template_path}


@pytest.fixture
def client(cfg):
    app = create_app({**cfg, "SCRIPTS_DIR": "/app/scripts", "SECRET_KEY": "test", "TESTING": True,
                      "PRICES_LIVE": False})
    return app.test_client()


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    """Nothing here reaches AWS unless a test puts the real engine back on a FakeS3 itself."""
    def boom(*a, **k):
        raise AssertionError("S3 rules screen tests never reach AWS")
    for name in REAL:
        monkeypatch.setattr(lifecycle, name, boom)
    monkeypatch.setattr(ops, "launch_py", lambda *a, **k: pytest.fail("unexpected detached launch"))


def _csrf(client):
    client.get("/setup")
    with client.session_transaction() as s:
        return s["_csrf"]


def _applied(cfg, jobs=JOBS, settings=None, bucket=BASE):
    want = lifecycle.desired(bucket, BASE, jobs, settings or {})
    lifecycle.save_applied(cfg["CACHE_DIR"], bucket, list(want.rules.values()), folders=sorted(want.folders))


# --- Refresh now (Task 12) ------------------------------------------------------------------

def test_refresh_now_launches_a_detached_scan_of_that_folder(client, monkeypatch):
    seen = []
    monkeypatch.setattr(ops, "launch_py", lambda c, module, args, **kw: seen.append((module, args, kw)) or "rid")
    r = client.post("/setup/storage/refresh", data={"csrf": _csrf(client), "bucket": BASE,
                                                   "folder": "media/manga/", "key": f"{BASE}|media/manga/"})
    assert r.status_code in (302, 303) and "/setup/storage" in r.headers["Location"]
    assert seen == [("app.engine.sysop", ["storage-summary", "--bucket", BASE, "--folder", "media/manga/"],
                     {"kind": "storage-summary"})]


def test_refresh_now_only_scans_an_app_folder(client):
    r = client.post("/setup/storage/refresh", data={"csrf": _csrf(client), "bucket": BASE, "folder": "logs/"})
    assert r.status_code == 404


def test_refresh_now_requires_csrf(client):
    assert client.post("/setup/storage/refresh", data={"bucket": BASE, "folder": "appdata/"}).status_code == 400


def _flashes(client):
    with client.session_transaction() as s:
        return dict(s["_flashes"]) if "_flashes" in s else {}


def test_refresh_now_warns_instead_of_launching_when_s3_rules_arent_managed(client, cfg):
    # fix round 1, Minor 2: below level 4 / no role, Refresh now must not silently do
    # nothing -- the autouse `no_aws` fixture fails this test outright if a launch is
    # attempted (ops.launch_py -> pytest.fail), so a passing test here already proves none was.
    token = _csrf(client)
    Path(cfg["CONFIG_DIR"], "backup.env").write_text(f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\n")
    r = client.post("/setup/storage/refresh", data={"csrf": token, "bucket": BASE, "folder": "media/manga/"})
    assert r.status_code in (302, 303)
    assert "AWS permissions update" in " ".join(_flashes(client).values())


def test_refresh_now_warns_instead_of_launching_for_a_custom_s3_endpoint(client, cfg):
    token = _csrf(client)
    Path(cfg["CONFIG_DIR"], "backup.env").write_text(
        f"S3_BUCKET={BASE}\nAWS_REGION=us-east-1\nBUCKET_ADMIN_ROLE_ARN=arn:aws:iam::1:role/r\n"
        "PERMISSIONS_VERSION=4\nS3_ENDPOINT=https://minio.example\n")
    r = client.post("/setup/storage/refresh", data={"csrf": token, "bucket": BASE, "folder": "media/manga/"})
    assert r.status_code in (302, 303)
    assert "doesn't support storage summaries" in " ".join(_flashes(client).values())


def test_refresh_now_scans_a_dedicated_buckets_whole_bucket_folder(client, cfg, monkeypatch):
    dedicated_bucket = "vault-dedicated-987654321098"
    jobs = [*JOBS, {"name": "vault", "type": "versioned", "source": "vault", "schedule": "0 6 * * *",
                    "enabled": True, "storage_class": "STANDARD", "dedicated": True,
                    "bucket": dedicated_bucket, "retention": {"type": "keep_all"}}]
    Path(cfg["CONFIG_DIR"], "jobs.json").write_text(json.dumps({"jobs": jobs}))
    seen = []
    monkeypatch.setattr(ops, "launch_py", lambda c, module, args, **kw: seen.append((module, args, kw)) or "rid")
    r = client.post("/setup/storage/refresh", data={"csrf": _csrf(client), "bucket": dedicated_bucket,
                                                   "folder": "", "key": f"{dedicated_bucket}|"})
    assert r.status_code in (302, 303)
    assert seen == [("app.engine.sysop", ["storage-summary", "--bucket", dedicated_bucket, "--folder", ""],
                     {"kind": "storage-summary"})]


def test_activity_labels_storage_summaries(client, cfg):
    runs.record_system(cfg["CACHE_DIR"], kind="storage-summary", summary="storage summary · media/manga/")
    body = client.get("/activity?kind=setup").get_data(as_text=True)
    assert "storage summary" in body
