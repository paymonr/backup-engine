# tests/estimator/conftest.py
import pytest
from app.estimator.prices import PriceTable

# Round synthetic numbers so every expected value is hand-computable.
TEST_PRICES_DICT = {
    "region": "test-region",
    "date": "2099-01-01",
    "source": "fixed test table",
    "storage_gb_month": {
        "STANDARD": 0.02, "STANDARD_IA": 0.01, "GLACIER_IR": 0.005,
        "GLACIER": 0.004, "DEEP_ARCHIVE": 0.001,
    },
    "requests": {"put_per_1k": 0.005, "get_per_1k": 0.0004, "lifecycle_transition_per_1k": 0.05},
    "retrieval": {
        "per_gb": {
            "STANDARD_IA": {"Standard": 0.01}, "GLACIER_IR": {"Standard": 0.03},
            "GLACIER": {"Bulk": 0.0025, "Standard": 0.01, "Expedited": 0.03},
            "DEEP_ARCHIVE": {"Bulk": 0.0025, "Standard": 0.02},
        },
        "request_per_1k": {"Bulk": 0.025, "Standard": 0.05, "Expedited": 10.0},
    },
    "data_transfer_out_per_gb": 0.10,
    "constraints": {
        "min_billable_object_kb": 128,
        "min_storage_duration_days": {"STANDARD_IA": 30, "GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180},
    },
}

@pytest.fixture
def prices() -> PriceTable:
    return PriceTable.from_dict(TEST_PRICES_DICT)
