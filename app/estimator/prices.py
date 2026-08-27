# app/estimator/prices.py — price-table type + loader. The ONLY reader of the bundled JSON.
from __future__ import annotations
from dataclasses import dataclass
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

def load_prices(region: str, prices_dir: Path | None = None) -> PriceTable:
    base = prices_dir or _MODULE_PRICES_DIR
    path = base / f"{region}.json"
    if not path.is_file():
        raise ValueError(f"no bundled price table for region '{region}' (looked for {path})")
    return PriceTable.from_dict(json.loads(path.read_text()))
