import json
import types
from app.engine import s3


def _proc(stdout, rc=0):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


def test_list_versions_parses():
    payload = {"Versions": [
        {"Key": "media/j/a", "VersionId": "v1", "IsLatest": True, "LastModified": "2026-09-01T00:00:00+00:00"},
        {"Key": "media/j/a", "VersionId": "v0", "IsLatest": False, "LastModified": "2026-08-01T00:00:00+00:00"}]}
    def runner(argv, **kw):
        assert "list-object-versions" in argv and "--prefix" in argv
        return _proc(json.dumps(payload))
    out = s3.list_versions("buck", "media/j/", runner=runner)
    assert {v["version_id"] for v in out} == {"v1", "v0"}
    latest = next(v for v in out if v["version_id"] == "v1")
    assert latest["is_latest"] is True and latest["last_modified"] > 0


# --- Fix 2: S3_ENDPOINT threaded into the aws-CLI calls (MinIO/B2/R2 support) ---

def test_list_versions_appends_endpoint_url_when_set():
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv
        return _proc(json.dumps({"Versions": []}))
    s3.list_versions("buck", "media/j/", endpoint="minio.local:9000", runner=runner)
    a = seen["argv"]
    assert "--endpoint-url" in a
    assert a[a.index("--endpoint-url") + 1] == "https://minio.local:9000"


def test_list_versions_omits_endpoint_url_when_unset():
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv
        return _proc(json.dumps({"Versions": []}))
    s3.list_versions("buck", "media/j/", runner=runner)
    assert "--endpoint-url" not in seen["argv"]


def test_list_versions_endpoint_with_scheme_passed_through_unchanged():
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv
        return _proc(json.dumps({"Versions": []}))
    s3.list_versions("buck", "media/j/", endpoint="http://127.0.0.1:9000", runner=runner)
    a = seen["argv"]
    assert a[a.index("--endpoint-url") + 1] == "http://127.0.0.1:9000"


def test_thaw_appends_endpoint_url_when_set():
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv
        return _proc("")
    s3.thaw("media/j/a", bucket="buck", endpoint="minio.local:9000", runner=runner)
    a = seen["argv"]
    assert a[a.index("--endpoint-url") + 1] == "https://minio.local:9000"


def test_thaw_omits_endpoint_url_when_unset():
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv
        return _proc("")
    s3.thaw("media/j/a", bucket="buck", runner=runner)
    assert "--endpoint-url" not in seen["argv"]
