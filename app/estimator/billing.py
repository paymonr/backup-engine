# app/estimator/billing.py — optional Cost Explorer via the `aws` CLI, using a
# SEPARATE read-only credential (never the runtime key). Creds go in the child
# env, not argv. CE is global (us-east-1). SERVICE filter is account-wide S3
# unless a cost-allocation tag is supplied.
from __future__ import annotations
import json, os, subprocess
from datetime import date, timedelta

class BillingError(Exception):
    pass

def _env(creds: dict) -> dict:
    e = dict(os.environ)
    e["AWS_ACCESS_KEY_ID"] = creds["AWS_ACCESS_KEY_ID"]
    e["AWS_SECRET_ACCESS_KEY"] = creds["AWS_SECRET_ACCESS_KEY"]
    if creds.get("AWS_SESSION_TOKEN"):
        e["AWS_SESSION_TOKEN"] = creds["AWS_SESSION_TOKEN"]
    e["AWS_DEFAULT_REGION"] = "us-east-1"
    return e

def _filter(tag: str | None) -> str:
    svc = {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}}
    if tag and "=" in tag:
        k, _, v = tag.partition("=")
        return json.dumps({"And": [svc, {"Tags": {"Key": k, "Values": [v]}}]})
    return json.dumps(svc)

def _month_start(d: date) -> date:
    return d.replace(day=1)

def monthly_costs(creds, *, months=3, tag=None, runner=subprocess.run) -> list[dict]:
    end = _month_start(date.today()) + timedelta(days=32)
    end = _month_start(end)
    start = _month_start(date.today())
    for _ in range(months - 1):
        start = _month_start(start - timedelta(days=1))
    cmd = ["aws", "ce", "get-cost-and-usage",
           "--time-period", f"Start={start.isoformat()},End={end.isoformat()}",
           "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
           "--filter", _filter(tag), "--output", "json"]
    p = runner(cmd, capture_output=True, text=True, env=_env(creds), timeout=60)
    if p.returncode != 0:
        raise BillingError(p.stderr.strip() or "cost explorer call failed")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        # rc==0 but unparseable stdout (truncated/HTML error page/etc): mirror the
        # rc!=0 path with a BillingError so billing_view catches it instead of a
        # bare JSONDecodeError 500ing the estimate page.
        raise BillingError("cost explorer returned malformed JSON") from e
    try:
        out = []
        for r in data.get("ResultsByTime", []):
            m = r["TimePeriod"]["Start"][:7]
            amt = float(r["Total"]["UnblendedCost"]["Amount"])
            out.append({"month": m, "amount": amt})
        return out
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        # rc==0 and valid JSON, but the wrong SHAPE: a JSON array/scalar makes
        # data.get(...) raise AttributeError; a missing/renamed key raises KeyError;
        # a non-numeric Amount raises ValueError; indexing a non-subscriptable value
        # raises TypeError. Mirror the malformed-JSON path with a BillingError so
        # billing_view catches it instead of a bare exception 500ing /estimate.
        raise BillingError("cost explorer returned an unexpected response shape") from e

def forecast(creds, *, runner=subprocess.run) -> dict | None:
    this_month = _month_start(date.today())
    start = _month_start(this_month + timedelta(days=32))
    end = _month_start(start + timedelta(days=32))
    cmd = ["aws", "ce", "get-cost-forecast",
           "--time-period", f"Start={start.isoformat()},End={end.isoformat()}",
           "--metric", "UNBLENDED_COST", "--granularity", "MONTHLY", "--output", "json"]
    p = runner(cmd, capture_output=True, text=True, env=_env(creds), timeout=60)
    if p.returncode != 0:
        return None
    try:
        # json.loads inside the try: JSONDecodeError is a ValueError, so malformed
        # rc==0 stdout degrades this best-effort forecast to None (never escapes).
        data = json.loads(p.stdout)
        return {"month": start.isoformat()[:7], "amount": float(data["Total"]["Amount"])}
    except (KeyError, ValueError):
        return None
