# app/estimator/pricing_live.py — fetch + map the PUBLIC S3 Price List Bulk offer
# file (no credentials) into PriceTable RATE fields; policy constants come from the
# bundled table. Cache to <cache_dir>/prices/<region>.json; fall back to bundled
# on any failure. Never raises to the caller (load_prices handles fallback).
from __future__ import annotations
import json, time, urllib.request
from dataclasses import replace
from pathlib import Path
from .prices import PriceTable

OFFER_URL = ("https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
             "AmazonS3/current/{region}/index.json")
_MAX_BYTES = 60 * 1024 * 1024  # offer files are large; bound the read

# AWS offer volumeType/storageClass strings -> our canonical classes (tolerant).
_CLASS_BY_VOLUME = {
    "standard": "STANDARD",
    "standard - infrequent access": "STANDARD_IA",
    "glacier instant retrieval": "GLACIER_IR",
    "amazon glacier": "GLACIER",
    "glacier": "GLACIER",
    "glacier flexible retrieval": "GLACIER",
    "glacier deep archive": "DEEP_ARCHIVE",
}

def fetch_offer(region: str, *, timeout: float = 20.0) -> dict:
    url = OFFER_URL.format(region=region)
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (fixed https host)
        return json.loads(r.read(_MAX_BYTES).decode("utf-8"))

def _ondemand_usd(terms: dict, sku: str) -> float | None:
    for term in (terms.get("OnDemand", {}).get(sku, {}) or {}).values():
        for dim in (term.get("priceDimensions", {}) or {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            try:
                return float(usd)
            except (TypeError, ValueError):
                continue
    return None

def map_offer_to_rates(offer: dict, base: PriceTable) -> PriceTable:
    """Overwrite base's RATE fields with anything found in the offer; keep base's
    constants. Partial/failed extraction just leaves the bundled value in place."""
    products = offer.get("products", {}) or {}
    terms = offer.get("terms", {}) or {}
    storage = dict(base.storage_gb_month)
    put = base.put_per_1k
    get = base.get_per_1k
    for sku, prod in products.items():
        fam = prod.get("productFamily")
        attr = prod.get("attributes", {}) or {}
        price = _ondemand_usd(terms, sku)
        if price is None:
            continue
        if fam == "Storage":
            vol = (attr.get("volumeType") or attr.get("storageClass") or "").strip().lower()
            cls = _CLASS_BY_VOLUME.get(vol)
            if cls:
                storage[cls] = price
        elif fam == "API Request":
            group = (attr.get("group") or "").strip()
            if group == "S3-API-Tier1":
                put = price * 1000
            elif group == "S3-API-Tier2":
                get = price * 1000
    return replace(base, storage_gb_month=storage, put_per_1k=put, get_per_1k=get,
                   source="aws-price-list (live)", date=offer.get("publicationDate", base.date))

def load_live(region: str, cache_dir: str | None, base: PriceTable,
              *, max_age_days: float = 7.0) -> PriceTable:
    cache = Path(cache_dir, "prices", f"{region}.json") if cache_dir else None
    if cache and cache.is_file():
        age = time.time() - cache.stat().st_mtime
        if age < max_age_days * 86400:
            return map_offer_to_rates(json.loads(cache.read_text()), base)
    offer = fetch_offer(region)          # may raise -> caught by load_prices
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(offer))
    return map_offer_to_rates(offer, base)
