# app/estimator/prices.py — price-table type + loader. The ONLY reader of the bundled JSON.
from __future__ import annotations
from dataclasses import dataclass, field, replace
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
    # Per-class, per-tier restore-request price: {storage_class: {tier: $/1k}}.
    # Only the async-restore classes (GLACIER / DEEP_ARCHIVE) carry these.
    retrieval_request_per_1k: dict[str, dict[str, float]]
    data_transfer_out_per_gb: float
    min_billable_object_kb: float
    min_storage_duration_days: dict[str, int]
    # Per-class PUT price (cold classes charge ~10x Standard). Empty -> every class
    # uses the flat put_per_1k (back-compat with older bundled tables).
    put_per_1k_by_class: dict[str, float] = field(default_factory=dict)
    # Classes to which the min_billable_object_kb floor applies (IA-family). Cold
    # Glacier/Deep-Archive classes DON'T have a 128KB floor — they add per-object
    # overhead instead (below). Empty -> legacy behaviour (floor on every non-STANDARD).
    min_billable_classes: tuple[str, ...] = ()
    # Glacier/Deep-Archive per-object metadata overhead: standard_kb billed at the
    # STANDARD rate + archive_kb billed at the object's own (cold) rate, per object.
    cold_overhead_classes: tuple[str, ...] = ()
    cold_overhead_standard_kb: float = 0.0
    cold_overhead_archive_kb: float = 0.0
    # Per-class GET price (IA/GIR cost more than the flat STANDARD rate). Empty ->
    # every class uses the flat get_per_1k.
    get_per_1k_by_class: dict[str, float] = field(default_factory=dict)
    # Per-destination lifecycle-transition price (currently unused by the model —
    # jobs write directly to their class, no Standard-then-lifecycle round trip).
    lifecycle_transition_per_1k_by_class: dict[str, float] = field(default_factory=dict)
    # Internet egress: monthly free allowance + cumulative tiers [(up_to_gb|None, rate)].
    # Empty tiers -> flat data_transfer_out_per_gb from the first GB (legacy).
    egress_free_gb: float = 0.0
    egress_tiers: tuple[tuple, ...] = ()

    def put_rate(self, storage_class: str) -> float:
        """PUT $/1k for a class — its own rate if the table carries one, else the flat rate."""
        return self.put_per_1k_by_class.get(storage_class, self.put_per_1k)

    def get_rate(self, storage_class: str) -> float:
        """GET $/1k for a class — its own rate if present, else the flat rate."""
        return self.get_per_1k_by_class.get(storage_class, self.get_per_1k)

    def retrieval_request_rate(self, storage_class: str, tier: str) -> float:
        """Restore-request $/1k for a cold class + tier, 0.0 if the class has none
        (warm classes) or the tier isn't offered (falls back to the class's Standard)."""
        by_class = self.retrieval_request_per_1k.get(storage_class)
        if not by_class:
            return 0.0
        if tier in by_class:
            return by_class[tier]
        return by_class.get("Standard", 0.0)

    def egress_cost(self, gb: float) -> float:
        """Internet-egress $ for `gb` this month: the free allowance is subtracted
        first, then cumulative tiers apply. Falls back to the flat rate if no tiers."""
        billable = max(0.0, gb - self.egress_free_gb)
        if not self.egress_tiers:
            return billable * self.data_transfer_out_per_gb
        cost = 0.0
        prev = 0.0
        remaining = billable
        for up_to, rate in self.egress_tiers:
            if remaining <= 0:
                break
            span = (up_to - prev) if up_to is not None else remaining
            take = min(remaining, max(0.0, span))
            cost += take * rate
            remaining -= take
            prev = up_to if up_to is not None else prev
        return cost

    @classmethod
    def from_dict(cls, d: dict) -> "PriceTable":
        req = d["requests"]
        ret = d["retrieval"]
        con = d["constraints"]
        overhead = con.get("cold_object_overhead", {})
        egress = d.get("data_transfer_out", {})
        egress_tiers = tuple((t.get("up_to_gb"), t["rate"]) for t in egress.get("tiers", ()))
        return cls(
            region=d["region"], date=d["date"], source=d["source"],
            storage_gb_month=d["storage_gb_month"],
            put_per_1k=req["put_per_1k"], get_per_1k=req["get_per_1k"],
            lifecycle_transition_per_1k=req["lifecycle_transition_per_1k"],
            retrieval_per_gb=ret["per_gb"], retrieval_request_per_1k=ret["request_per_1k"],
            data_transfer_out_per_gb=d["data_transfer_out_per_gb"],
            min_billable_object_kb=con["min_billable_object_kb"],
            min_storage_duration_days=con["min_storage_duration_days"],
            put_per_1k_by_class=req.get("put_per_1k_by_class", {}),
            min_billable_classes=tuple(con.get("min_billable_classes", ())),
            cold_overhead_classes=tuple(overhead.get("classes", ())),
            cold_overhead_standard_kb=overhead.get("standard_tier_kb", 0.0),
            cold_overhead_archive_kb=overhead.get("archive_tier_kb", 0.0),
            get_per_1k_by_class=req.get("get_per_1k_by_class", {}),
            lifecycle_transition_per_1k_by_class=req.get("lifecycle_transition_per_1k_by_class", {}),
            egress_free_gb=egress.get("free_gb_per_month", 0.0),
            egress_tiers=egress_tiers,
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
