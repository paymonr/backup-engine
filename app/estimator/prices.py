# app/estimator/prices.py — price-table type + loader. The ONLY reader of the bundled JSON.
from __future__ import annotations
from dataclasses import dataclass, replace
from pathlib import Path
import json

_MODULE_PRICES_DIR = Path(__file__).parent / "prices"

@dataclass(frozen=True)
class PriceTable:
    region: str
    date: str
    source: str
    storage_gb_month: dict[str, float]
    put_per_1k: float
    get_per_1k: float
    lifecycle_transition_per_1k: float
    retrieval_per_gb: dict[str, dict[str, float]]
    retrieval_request_per_1k: dict[str, float]
    data_transfer_out_per_gb: float
    min_billable_object_kb: float
    min_storage_duration_days: dict[str, int]

    @classmethod
    def from_dict(cls, d: dict) -> "PriceTable":
        req = d["requests"]
        ret = d["retrieval"]
        con = d["constraints"]
        return cls(
            region=d["region"], date=d["date"], source=d["source"],
            storage_gb_month=d["storage_gb_month"],
            put_per_1k=req["put_per_1k"], get_per_1k=req["get_per_1k"],
            lifecycle_transition_per_1k=req["lifecycle_transition_per_1k"],
            retrieval_per_gb=ret["per_gb"], retrieval_request_per_1k=ret["request_per_1k"],
            data_transfer_out_per_gb=d["data_transfer_out_per_gb"],
            min_billable_object_kb=con["min_billable_object_kb"],
            min_storage_duration_days=con["min_storage_duration_days"],
        )

_FALLBACK_REGION = "us-east-1"

def load_prices(region: str, prices_dir: Path | None = None, *,
                cache_dir: str | None = None, live: bool = False) -> PriceTable:
    prices_dir = prices_dir or _MODULE_PRICES_DIR
    path = prices_dir / f"{region}.json"
    if path.is_file():
        base = PriceTable.from_dict(json.loads(path.read_text()))
    else:
        # Only us-east-1.json is bundled. For an un-bundled (but real, GUI-editable)
        # region, fall back to the us-east-1 table as the REGION-INDEPENDENT constants
        # (min-object-KB / min-storage-duration are policy) + a labeled RATE
        # approximation, relabelled to the requested region — so the OFFLINE path
        # degrades to an approximation instead of raising (which 500'd the estimate
        # page for any non-us-east-1 AWS_REGION). Only raise if even the fallback is
        # absent (should never happen — us-east-1.json ships with the package).
        fallback = prices_dir / f"{_FALLBACK_REGION}.json"
        if not fallback.is_file():
            raise ValueError(f"no bundled price table for region '{region}' and no "
                             f"'{_FALLBACK_REGION}' fallback (looked in {prices_dir})")
        us = PriceTable.from_dict(json.loads(fallback.read_text()))
        base = replace(us, region=region,
                       source=f"bundled {_FALLBACK_REGION} rates (no table for {region})")
    if not live:
        return base
    try:
        from . import pricing_live
        # Live still fetches the TARGET region's real rates on top of the base, so a
        # real region gets correct live pricing; only the offline path approximates.
        return pricing_live.load_live(region, cache_dir, base)
    except Exception:  # any network/parse failure -> base (bundled or fallback)
        return base
