import json
import types
from pathlib import Path

from app.engine import buckets


def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


_CREDS = {"AWS_ACCESS_KEY_ID": "ASIA", "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t"}


def test_list_managed_filters_by_tag():
    def runner(argv, **kw):
        if "list-buckets" in argv:
            return _cp(out=json.dumps({"Buckets": [
                {"Name": "be-1-photos"}, {"Name": "be-1-docs"}, {"Name": "someone-elses-bucket"},
            ]}))
        if "get-bucket-tagging" in argv:
            bucket = argv[argv.index("--bucket") + 1]
            if bucket == "be-1-photos":
                return _cp(out=json.dumps({"TagSet": [{"Key": "managed-by", "Value": "backup-engine"}]}))
            if bucket == "be-1-docs":
                return _cp(out=json.dumps({"TagSet": [{"Key": "other", "Value": "thing"}]}))
            return _cp(rc=254, err="An error occurred (NoSuchTagSet) when calling the GetBucketTagging operation")
        return _cp()
    names = buckets.list_managed("us-east-1", _CREDS, runner=runner)
    assert names == ["be-1-photos"]


def test_list_managed_skips_untagged_or_errored_without_crashing():
    def runner(argv, **kw):
        if "list-buckets" in argv:
            return _cp(out=json.dumps({"Buckets": [{"Name": "untagged"}, {"Name": "denied"}]}))
        if "get-bucket-tagging" in argv:
            bucket = argv[argv.index("--bucket") + 1]
            if bucket == "untagged":
                return _cp(rc=254, err="An error occurred (NoSuchTagSet) when calling the GetBucketTagging operation")
            return _cp(rc=255, err="An error occurred (AccessDenied) when calling the GetBucketTagging operation")
        return _cp()
    names = buckets.list_managed("us-east-1", _CREDS, runner=runner)   # must NOT raise
    assert names == []


def test_empty_and_delete_removes_versions_then_bucket():
    seen = []
    def runner(argv, **kw):
        seen.append(argv)
        if "list-object-versions" in argv:
            return _cp(out=json.dumps({
                "Versions": [{"Key": "a.txt", "VersionId": "v1"}],
                "DeleteMarkers": [{"Key": "b.txt", "VersionId": "v2"}],
            }))
        return _cp()
    buckets.empty_and_delete("be-1-photos", region="us-east-1", creds=_CREDS, runner=runner)
    kinds = []
    for argv in seen:
        if "delete-objects" in argv:
            kinds.append("delete-objects")
        elif "delete-bucket" in argv:
            kinds.append("delete-bucket")
    assert kinds == ["delete-objects", "delete-bucket"]   # order matters: objects before bucket
    assert kinds.count("delete-bucket") == 1
    delete_call = next(a for a in seen if "delete-objects" in a)
    payload = json.loads(delete_call[delete_call.index("--delete") + 1])
    keys = {(o["Key"], o["VersionId"]) for o in payload["Objects"]}
    assert keys == {("a.txt", "v1"), ("b.txt", "v2")}


def test_empty_and_delete_skips_delete_objects_when_bucket_already_empty():
    seen = []
    def runner(argv, **kw):
        seen.append(argv)
        if "list-object-versions" in argv:
            return _cp(out=json.dumps({}))
        return _cp()
    buckets.empty_and_delete("be-1-empty", region="us-east-1", creds=_CREDS, runner=runner)
    assert not any("delete-objects" in a for a in seen)
    assert sum(1 for a in seen if "delete-bucket" in a) == 1


def test_empty_and_delete_batches_over_1000_objects():
    seen = []
    versions = [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(1500)]
    def runner(argv, **kw):
        seen.append(argv)
        if "list-object-versions" in argv:
            return _cp(out=json.dumps({"Versions": versions, "DeleteMarkers": []}))
        return _cp()
    buckets.empty_and_delete("be-1-big", region="us-east-1", creds=_CREDS, runner=runner)
    delete_calls = [a for a in seen if "delete-objects" in a]
    bucket_calls = [a for a in seen if "delete-bucket" in a]
    assert len(delete_calls) == 2       # batched at 1000 (aws delete-objects limit)
    assert len(bucket_calls) == 1
    assert seen.index(bucket_calls[0]) > seen.index(delete_calls[-1])   # bucket deleted last
    sizes = [len(json.loads(a[a.index("--delete") + 1])["Objects"]) for a in delete_calls]
    assert sizes == [1000, 500]


def test_bucket_admin_template_has_teardown_actions():
    root = Path(__file__).parent.parent.parent
    admin_txt = (root / "provisioning" / "bucket-admin-policy.json.tmpl").read_text()
    for action in ("s3:ListAllMyBuckets", "s3:GetBucketTagging", "s3:ListBucketVersions",
                   "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:DeleteBucket"):
        assert action in admin_txt, f"{action} missing from bucket-admin template"


def test_runtime_template_never_gets_account_wide_teardown_actions():
    # DeleteObject/DeleteObjectVersion/ListBucketVersions already legitimately exist on the
    # runtime key, scoped to its own prefixes -- those are NOT teardown grants. Only the
    # account-wide enumerate/delete-bucket actions must stay off the runtime key entirely.
    root = Path(__file__).parent.parent.parent
    runtime_txt = (root / "provisioning" / "iam-policy.json.tmpl").read_text()
    for action in ("s3:ListAllMyBuckets", "s3:GetBucketTagging", "s3:DeleteBucket"):
        assert action not in runtime_txt, f"{action} must not be granted to the runtime key"
