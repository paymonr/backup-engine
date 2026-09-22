import types
from app.engine import buckets

def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

def test_ensure_bucket_runs_full_config_sequence():
    seen = []
    def runner(argv, **kw):
        seen.append(argv[:3]); return _cp()
    buckets.ensure_bucket("be-1-photos", region="us-east-1", versioned=True,
                          creds={"AWS_ACCESS_KEY_ID":"ASIA","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                          runner=runner)
    joined = [" ".join(a) for a in seen]
    assert any("s3api create-bucket" in j for j in joined)
    assert any("put-public-access-block" in j for j in joined)
    assert any("put-bucket-encryption" in j for j in joined)
    assert any("put-bucket-versioning" in j for j in joined)
    assert any("put-bucket-lifecycle-configuration" in j for j in joined)
    assert any("put-bucket-tagging" in j for j in joined)

def test_ensure_bucket_idempotent_on_already_owned():
    def runner(argv, **kw):
        if "create-bucket" in argv:
            return _cp(rc=254, err="BucketAlreadyOwnedByYou")
        return _cp()
    buckets.ensure_bucket("be-1-photos", region="us-east-1", versioned=False,
                          creds={"AWS_ACCESS_KEY_ID":"ASIA","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                          runner=runner)   # must NOT raise

def test_ensure_bucket_maps_name_taken():
    def runner(argv, **kw):
        return _cp(rc=254, err="BucketAlreadyExists") if "create-bucket" in argv else _cp()
    try:
        buckets.ensure_bucket("taken", region="us-east-1", versioned=True,
                              creds={"AWS_ACCESS_KEY_ID":"A","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                              runner=runner)
        assert False
    except buckets.BucketError as e:
        assert e.kind == "name_taken"

def test_ensure_bucket_rejects_invalid_name():
    try:
        buckets.ensure_bucket("Bad_Name", region="us-east-1", versioned=True,
                              creds={}, runner=lambda *a, **k: _cp())
        assert False
    except buckets.BucketError as e:
        assert e.kind == "invalid_name"

def test_ensure_bucket_maps_too_many_buckets():
    def runner(argv, **kw):
        return _cp(rc=254, err="TooManyBuckets") if "create-bucket" in argv else _cp()
    try:
        buckets.ensure_bucket("some-bucket", region="us-east-1", versioned=True,
                              creds={"AWS_ACCESS_KEY_ID":"A","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                              runner=runner)
        assert False
    except buckets.BucketError as e:
        assert e.kind == "too_many"

def test_ensure_bucket_maps_access_denied():
    def runner(argv, **kw):
        return _cp(rc=254, err="AccessDenied") if "create-bucket" in argv else _cp()
    try:
        buckets.ensure_bucket("some-bucket", region="us-east-1", versioned=True,
                              creds={"AWS_ACCESS_KEY_ID":"A","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                              runner=runner)
        assert False
    except buckets.BucketError as e:
        assert e.kind == "access_denied"

def test_ensure_bucket_non_us_east_1_sets_location_constraint():
    seen = []
    def runner(argv, **kw):
        seen.append(argv); return _cp()
    buckets.ensure_bucket("be-1-photos", region="eu-west-1", versioned=True,
                          creds={"AWS_ACCESS_KEY_ID":"A","AWS_SECRET_ACCESS_KEY":"s","AWS_SESSION_TOKEN":"t"},
                          runner=runner)
    create_call = next(c for c in seen if "create-bucket" in c)
    assert "--create-bucket-configuration" in create_call
    assert "LocationConstraint=eu-west-1" in create_call

def test_oldest_non_default_version_only_at_the_limit():
    from app.engine.buckets import oldest_non_default_version
    four = [{"VersionId": f"v{i}", "IsDefaultVersion": i == 4, "CreateDate": f"2026-01-0{i}"} for i in range(1, 5)]
    assert oldest_non_default_version(four) is None
    five = four + [{"VersionId": "v5", "IsDefaultVersion": False, "CreateDate": "2026-01-05"}]
    assert oldest_non_default_version(five) == "v1"
