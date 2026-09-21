import json, types
from app.gui import provision

def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

def test_assume_role_parses_credentials():
    payload = json.dumps({"Credentials": {
        "AccessKeyId": "ASIA", "SecretAccessKey": "sk", "SessionToken": "tok"}})
    calls = []
    def run(args, **kw): calls.append(args); return _cp(out=payload)
    creds = provision.assume_role("arn:aws:iam::1:role/x", region="us-east-1",
                                  key="AKIA", secret="s3kr3t-xyz", run=run)
    assert creds["AWS_ACCESS_KEY_ID"] == "ASIA"
    assert creds["AWS_SECRET_ACCESS_KEY"] == "sk"
    assert creds["AWS_SESSION_TOKEN"] == "tok"
    assert calls[0][:2] == ["sts", "assume-role"]

def test_assume_role_raises_on_failure():
    def run(args, **kw): return _cp(rc=255, err="AccessDenied for s3kr3t-xyz")
    try:
        provision.assume_role("arn", region="us-east-1", key="AKIA", secret="s3kr3t-xyz", run=run)
        assert False, "expected AssumeRoleError"
    except provision.AssumeRoleError as e:
        assert "AccessDenied" in str(e)
        assert "s3kr3t-xyz" not in str(e)
