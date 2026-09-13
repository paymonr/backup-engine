import time
from app.engine import archive_prune as ap

DAY = 86400
def _v(key, vid, latest, age_days, now): return {"key": key, "version_id": vid, "is_latest": latest, "last_modified": now - age_days * DAY}

def test_days_deletes_old_noncurrent_only():
    now = time.time()
    vs = [_v("media/j/a", "cur", True, 1, now), _v("media/j/a", "old", False, 40, now), _v("media/j/a", "new", False, 5, now)]
    got = ap.select_prunable(vs, {"type": "days", "days": 30}, now)
    assert got == [{"key": "media/j/a", "version_id": "old"}]   # only the >30d noncurrent; never the latest

def test_count_keeps_n_most_recent_per_key():
    now = time.time()
    vs = [_v("media/j/a", f"v{i}", i == 0, i, now) for i in range(5)]  # v0 latest, v1..v4 older
    got = {d["version_id"] for d in ap.select_prunable(vs, {"type": "count", "count": 2}, now)}
    assert got == {"v2", "v3", "v4"}   # keep 2 newest (v0 latest + v1), drop the rest

def test_keep_all_deletes_nothing():
    now = time.time()
    vs = [_v("media/j/a", "old", False, 999, now)]
    assert ap.select_prunable(vs, {"type": "keep_all"}, now) == []

def test_prune_scope_guard_rejects_foreign_key():
    now = time.time()
    vs = [_v("media/OTHER/x", "v", False, 999, now)]
    import pytest
    with pytest.raises(ap.PruneScopeError):
        ap.prune("j", {"type": "days", "days": 1}, bucket="b", now=now,
                 runner=None, _versions=vs, _deleter=lambda *a, **k: None)

def test_count_1_keeps_only_latest():
    now = time.time()
    vs = [_v("media/j/a", "cur", True, 1, now), _v("media/j/a", "old", False, 40, now), _v("media/j/a", "old2", False, 60, now)]
    got = {d["version_id"] for d in ap.select_prunable(vs, {"type": "count", "count": 1}, now)}
    assert got == {"old", "old2"}   # keep only the 1 most recent (the latest), delete all noncurrent

def test_count_0_never_selects_latest():
    now = time.time()
    vs = [_v("media/j/a", "cur", True, 1, now), _v("media/j/a", "old", False, 40, now)]
    got = ap.select_prunable(vs, {"type": "count", "count": 0}, now)
    # count=0 should delete all noncurrent (but never the is_latest)
    assert got == [{"key": "media/j/a", "version_id": "old"}]
    # Ensure no is_latest version is in the result
    assert all(v["is_latest"] == False for v in [vs[i] for i in range(len(vs)) if vs[i]["version_id"] in {t["version_id"] for t in got}])

def test_prune_scope_guard_rejects_path_traversal():
    now = time.time()
    vs = [_v("media/j/../other/a", "v", False, 999, now)]
    import pytest
    with pytest.raises(ap.PruneScopeError):
        ap.prune("j", {"type": "days", "days": 1}, bucket="b", now=now,
                 runner=None, _versions=vs, _deleter=lambda *a, **k: None)

# --- Fix 2b: S3_ENDPOINT threaded from prune() down into s3.list_versions/delete_version ---

def test_prune_threads_endpoint_to_s3_list_and_delete_calls():
    import json, types
    now = time.time()
    payload = {"Versions": [
        {"Key": "media/j/a", "VersionId": "cur", "IsLatest": True, "LastModified": "2026-09-01T00:00:00+00:00"},
        {"Key": "media/j/a", "VersionId": "old", "IsLatest": False, "LastModified": "2026-01-01T00:00:00+00:00"},
    ]}
    seen = []
    def runner(argv, **kw):
        seen.append(argv)
        if "list-object-versions" in argv:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    n = ap.prune("j", {"type": "days", "days": 30}, bucket="buck", now=now,
                 endpoint="minio.local:9000", runner=runner)
    assert n == 1
    assert len(seen) == 2   # one list-object-versions, one delete-object
    for argv in seen:
        assert "--endpoint-url" in argv
        assert argv[argv.index("--endpoint-url") + 1] == "https://minio.local:9000"


def test_prune_omits_endpoint_when_none():
    import json, types
    now = time.time()
    payload = {"Versions": []}
    seen = []
    def runner(argv, **kw):
        seen.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ap.prune("j", {"type": "days", "days": 30}, bucket="buck", now=now, runner=runner)
    assert "--endpoint-url" not in seen[0]


# --- Fix 3: archive_prune.main CLI hardening ---

def test_main_missing_bucket_errors_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    rc = ap.main(["myjob", "--type", "keep_all"])
    assert rc != 0
    assert capsys.readouterr().err.strip()


def test_main_unknown_type_errors_cleanly(monkeypatch, capsys):
    monkeypatch.setenv("S3_BUCKET", "buck")
    rc = ap.main(["myjob", "--type", "bogus"])
    assert rc != 0
    assert "bogus" in capsys.readouterr().err


def test_main_keep_all_is_clean_noop(monkeypatch, capsys):
    # keep_all must remain a clean no-op, not be rejected as an "unknown type".
    monkeypatch.setenv("S3_BUCKET", "buck")
    captured = {}
    def fake_prune(job, policy, *, bucket, endpoint=None, **kw):
        captured["policy"] = policy
        return 0
    monkeypatch.setattr(ap, "prune", fake_prune)
    rc = ap.main(["myjob", "--type", "keep_all"])
    assert rc == 0
    assert captured["policy"] == {"type": "keep_all"}


def test_main_reads_s3_endpoint_env_and_threads_to_prune(monkeypatch):
    captured = {}
    def fake_prune(job, policy, *, bucket, endpoint=None, **kw):
        captured["endpoint"] = endpoint
        return 0
    monkeypatch.setattr(ap, "prune", fake_prune)
    monkeypatch.setenv("S3_BUCKET", "buck")
    monkeypatch.setenv("S3_ENDPOINT", "minio.local:9000")
    ap.main(["myjob", "--type", "keep_all"])
    assert captured["endpoint"] == "minio.local:9000"
