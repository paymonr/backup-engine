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

def test_load_prices_unbundled_region_falls_back_to_us_east_1():
    # An un-bundled region (only us-east-1.json ships) must NOT raise: it falls back
    # to the us-east-1 table as the region-independent constants + rate base, with
    # region relabeled to the requested region and the source noting the fallback.
    us = load_prices("us-east-1", live=False)
    pt = load_prices("eu-west-1", live=False)
    assert pt.region == "eu-west-1"
    assert pt.storage_gb_month == us.storage_gb_month          # us-east-1 rates
    assert pt.min_billable_object_kb == us.min_billable_object_kb   # region-independent constant
    assert pt.min_storage_duration_days == us.min_storage_duration_days
    assert "us-east-1" in pt.source                            # fallback is visible/labeled

def test_load_prices_empty_dir_still_raises(tmp_path):
    # A truly-empty prices dir (no us-east-1.json to fall back to either) still raises.
    with pytest.raises(ValueError) as e:
        load_prices("eu-west-1", prices_dir=tmp_path)
    assert "eu-west-1" in str(e.value)

def test_bundled_table_is_valid_json_with_date():
    p = Path("app/estimator/prices/us-east-1.json")
    data = json.loads(p.read_text())
    assert data["date"] and data["region"] == "us-east-1"
