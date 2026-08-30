import json, types, pytest
from app.estimator import billing

CE_OUT = json.dumps({"ResultsByTime": [
    {"TimePeriod": {"Start": "2026-07-01"}, "Total": {"UnblendedCost": {"Amount": "12.34"}}},
    {"TimePeriod": {"Start": "2026-08-01"}, "Total": {"UnblendedCost": {"Amount": "9.10"}}},
]})
FC_OUT = json.dumps({"Total": {"Amount": "13.00"}, "ForecastResultsByTime": [
    {"TimePeriod": {"Start": "2026-09-01"}, "MeanValue": "13.00"}]})

def _runner(out, captured):
    def run(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = env
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    return run

def test_monthly_costs_parses_and_uses_env_creds():
    cap = {}
    creds = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s3cr3t"}
    got = billing.monthly_costs(creds, months=2, runner=_runner(CE_OUT, cap))
    assert got == [{"month": "2026-07", "amount": 12.34}, {"month": "2026-08", "amount": 9.10}]
    assert cap["env"]["AWS_ACCESS_KEY_ID"] == "AKIA"
    assert "s3cr3t" not in " ".join(map(str, cap["cmd"]))  # secret never on argv

def test_forecast_parses():
    cap = {}
    got = billing.forecast({"AWS_ACCESS_KEY_ID": "A", "AWS_SECRET_ACCESS_KEY": "B"},
                           runner=_runner(FC_OUT, cap))
    assert got == {"month": "2026-09", "amount": 13.0}

def test_forecast_malformed_rc0_stdout_returns_none():
    # forecast is best-effort (None on any failure). rc==0 + non-JSON stdout must
    # yield None, not a bare JSONDecodeError — billing_view calls forecast after
    # monthly_costs and only catches BillingError, so an escaping decode error 500s.
    def run(cmd, env=None, **kw):
        return types.SimpleNamespace(returncode=0, stdout="<html>not json</html>", stderr="")
    assert billing.forecast({"AWS_ACCESS_KEY_ID": "A", "AWS_SECRET_ACCESS_KEY": "B"}, runner=run) is None

def test_error_raises_billingerror():
    def run(cmd, env=None, **kw):
        return types.SimpleNamespace(returncode=255, stdout="", stderr="AccessDenied")
    with pytest.raises(billing.BillingError):
        billing.monthly_costs({"AWS_ACCESS_KEY_ID": "A", "AWS_SECRET_ACCESS_KEY": "B"}, runner=run)

def test_malformed_rc0_stdout_raises_billingerror():
    # rc==0 but stdout is not JSON must raise BillingError (mirror the rc!=0 path),
    # never a bare JSONDecodeError — billing_view only catches BillingError, so an
    # unwrapped decode error would 500 the estimate page.
    def run(cmd, env=None, **kw):
        return types.SimpleNamespace(returncode=0, stdout="<html>not json</html>", stderr="")
    with pytest.raises(billing.BillingError):
        billing.monthly_costs({"AWS_ACCESS_KEY_ID": "A", "AWS_SECRET_ACCESS_KEY": "B"}, runner=run)
