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
