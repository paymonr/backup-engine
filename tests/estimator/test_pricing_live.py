# tests/estimator/test_pricing_live.py
import json, pathlib, pytest
from app.estimator.prices import load_prices
from app.estimator import pricing_live as pl

FIX = pathlib.Path(__file__).parent / "fixtures" / "s3_offer_sample.json"

def _bundled():
    return load_prices("us-east-1", live=False)

def test_map_offer_extracts_known_rates():
    offer = json.loads(FIX.read_text())
    table = pl.map_offer_to_rates(offer, _bundled())
    # values chosen to match the trimmed fixture
    assert table.storage_gb_month["STANDARD"] == pytest.approx(0.023, rel=0.001)
    assert table.put_per_1k > 0 and table.get_per_1k > 0
    assert table.source.startswith("aws-price-list")

def test_constants_come_from_bundled_not_offer():
    offer = json.loads(FIX.read_text())
    base = _bundled()
    table = pl.map_offer_to_rates(offer, base)
    assert table.min_billable_object_kb == base.min_billable_object_kb
    assert table.min_storage_duration_days == base.min_storage_duration_days

def test_missing_class_falls_back_per_field(monkeypatch):
    base = _bundled()
    table = pl.map_offer_to_rates({"products": {}, "terms": {"OnDemand": {}}}, base)
    # no products -> every rate falls back to bundled, never raises
    assert table.storage_gb_month["DEEP_ARCHIVE"] == base.storage_gb_month["DEEP_ARCHIVE"]

def test_load_prices_live_fetch_failure_falls_back(monkeypatch, tmp_path):
    def boom(*a, **k): raise OSError("network down")
    monkeypatch.setattr(pl, "fetch_offer", boom)
    table = load_prices("us-east-1", cache_dir=str(tmp_path), live=True)
    assert table.source  # bundled table, no exception
    assert table.storage_gb_month["STANDARD"] > 0

def test_load_prices_uses_fresh_cache(monkeypatch, tmp_path):
    calls = {"n": 0}
    real = pl.fetch_offer
    def counting(region, **k):
        calls["n"] += 1
        return json.loads(FIX.read_text())
    monkeypatch.setattr(pl, "fetch_offer", counting)
    load_prices("us-east-1", cache_dir=str(tmp_path), live=True)
    load_prices("us-east-1", cache_dir=str(tmp_path), live=True)
    assert calls["n"] == 1  # second call served from cache

def test_deep_archive_staging_sku_does_not_overwrite_storage():
    # Mirrors the REAL AWS S3 offer: the only Storage SKU tagged volumeType
    # "Glacier Deep Archive" is the restore-STAGING metric (~$0.021), and there is
    # no TimedStorage-GDA-ByteHrs at all. The staging price must NOT become the
    # Deep Archive storage rate -- the bundled $0.00099 stands. (Regression: the
    # old volumeType-only mapping set DEEP_ARCHIVE to 0.021, ~21x too high, which
    # made Deep Archive look pricier than STANDARD.)
    base = _bundled()
    offer = {
        "publicationDate": "2026-08-18T00:00:00Z",
        "products": {
            "GDA_STAGING": {"productFamily": "Storage", "attributes": {
                "volumeType": "Glacier Deep Archive", "usagetype": "TimedStorage-GDA-Staging"}},
            "STD": {"productFamily": "Storage", "attributes": {
                "volumeType": "Standard", "usagetype": "TimedStorage-ByteHrs"}},
        },
        "terms": {"OnDemand": {
            "GDA_STAGING": {"t1": {"priceDimensions": {"d1": {"pricePerUnit": {"USD": "0.0210000000"}}}}},
            "STD": {"t2": {"priceDimensions": {"d2": {"pricePerUnit": {"USD": "0.0230000000"}}}}},
        }},
    }
    table = pl.map_offer_to_rates(offer, base)
    assert table.storage_gb_month["DEEP_ARCHIVE"] == base.storage_gb_month["DEEP_ARCHIVE"]
    assert table.storage_gb_month["DEEP_ARCHIVE"] < 0.01  # bundled 0.00099, not staging 0.021
    assert table.storage_gb_month["STANDARD"] == pytest.approx(0.023)  # real ByteHrs still live
