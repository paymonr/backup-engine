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
