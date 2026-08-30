import json, types
from app.estimator import usage

def _runner(map_out):
    def run(cmd, **kw):
        # cmd is a list; find the s3:... arg to pick the canned reply
        target = next(a for a in cmd if str(a).startswith("s3:"))
        out = map_out.get(target)
        rc = 0 if out is not None else 1
        return types.SimpleNamespace(returncode=rc, stdout=out or "", stderr="")
    return run

def test_collect_archive_and_versioned():
    out = {
        "s3:b/media/movies": json.dumps({"count": 3, "bytes": 100}),
        "s3:b/appdata": json.dumps({"count": 9, "bytes": 500}),
    }
    got = usage.collect_usage("b", ["movies"], True, runner=_runner(out))
    assert got["media/movies"] == {"bytes": 100, "count": 3}
    assert got["appdata"] == {"bytes": 500, "count": 9}

def test_prefix_error_is_none():
    got = usage.collect_usage("b", ["ghost"], False, runner=_runner({}))
    assert got["media/ghost"] is None

def _null_runner(cmd, **kw):
    # rclone size --json returning null for a field (seen in the wild on empty/odd
    # prefixes): int(None) raises TypeError, which the guard must also swallow.
    return types.SimpleNamespace(
        returncode=0, stdout=json.dumps({"count": None, "bytes": None}), stderr="")

def test_size_null_fields_returns_none_not_typeerror():
    assert usage._size("b", "media/x", rclone_config=None, runner=_null_runner) is None

def test_collect_usage_null_fields_is_none_not_crash():
    got = usage.collect_usage("b", ["movies"], True, runner=_null_runner)
    assert got["media/movies"] is None
    assert got["appdata"] is None

def test_cache_roundtrip(tmp_path):
    usage.save_cached(str(tmp_path), {"appdata": {"bytes": 1, "count": 1}})
    loaded = usage.load_cached(str(tmp_path))
    assert loaded["data"]["appdata"] == {"bytes": 1, "count": 1}
    assert "fetched_at" in loaded
