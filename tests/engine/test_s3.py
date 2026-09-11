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


def test_delete_version_calls_s3api():
    seen = {}
    def runner(argv, **kw):
        seen["argv"] = argv
        return _proc("")
    s3.delete_version("buck", "media/j/a", "v0", runner=runner)
    a = seen["argv"]
    assert "delete-object" in a and "--version-id" in a and "v0" in a and "media/j/a" in a
