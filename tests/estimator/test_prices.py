# tests/estimator/test_prices.py
import json
from pathlib import Path
import pytest
from app.estimator.prices import PriceTable, load_prices

def test_from_dict_populates_fields(prices):
    assert prices.storage_gb_month["DEEP_ARCHIVE"] == 0.001
    assert prices.put_per_1k == 0.005
    assert prices.retrieval_per_gb["GLACIER"]["Bulk"] == 0.0025
    assert prices.min_billable_object_kb == 128
    assert prices.min_storage_duration_days["DEEP_ARCHIVE"] == 180

def test_load_prices_reads_bundled_us_east_1():
    pt = load_prices("us-east-1")
    assert pt.region == "us-east-1"
    assert pt.date  # non-empty, stamped
    for cls in ("STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"):
        assert cls in pt.storage_gb_month

def test_load_prices_unknown_region_raises():
    with pytest.raises(ValueError) as e:
        load_prices("moon-base-1")
    assert "moon-base-1" in str(e.value)

def test_bundled_table_is_valid_json_with_date():
    p = Path("app/estimator/prices/us-east-1.json")
    data = json.loads(p.read_text())
    assert data["date"] and data["region"] == "us-east-1"
