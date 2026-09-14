# tests/estimator/test_cold_pricing.py — per-class request pricing and the
# Glacier/Deep-Archive per-object overhead (the many-small-files-on-cold penalty).
from app.estimator.prices import load_prices
from app.estimator.model import (
    JobInputs, billed_gb, storage_monthly, upfront_onetime, cold_object_overhead_monthly,
)

PT = load_prices("us-east-1")


def _job(files, cls, size_gb=1000.0):
    return JobInputs(name="j", engine="archive", size_gb=size_gb, file_count=files,
                     storage_class=cls, change_rate_pct=0.0, backups_per_month=4)


def test_put_rate_is_per_class_cold_is_10x_standard():
    assert PT.put_rate("STANDARD") == 0.005
    assert PT.put_rate("DEEP_ARCHIVE") == 0.05
    assert PT.put_rate("DEEP_ARCHIVE") == 10 * PT.put_rate("STANDARD")


def test_upfront_onetime_uses_per_class_put_rate():
    n = 1_000_000
    std = upfront_onetime(_job(n, "STANDARD"), PT)
    deep = upfront_onetime(_job(n, "DEEP_ARCHIVE"), PT)
    assert std == 1_000_000 / 1000 * 0.005          # $5
    assert deep == 1_000_000 / 1000 * 0.05           # $50
    assert deep == 10 * std


def test_128kb_floor_applies_to_ia_not_to_deep_archive():
    # 10M objects of 10KB each = ~95GB actual, but 128KB floor => ~1220GB on IA.
    tiny = _job(10_000_000, "STANDARD_IA", size_gb=95.0)
    assert billed_gb(tiny, PT) > 1000       # floor bit hard
    # Same objects on DEEP_ARCHIVE: NO 128KB floor (overhead is charged instead).
    tiny_deep = _job(10_000_000, "DEEP_ARCHIVE", size_gb=95.0)
    assert billed_gb(tiny_deep, PT) == 95.0


def test_cold_overhead_scales_with_object_count_and_is_zero_off_cold():
    assert cold_object_overhead_monthly(_job(1000, "STANDARD"), PT) == 0.0
    assert cold_object_overhead_monthly(_job(1000, "STANDARD_IA"), PT) == 0.0
    few = cold_object_overhead_monthly(_job(232_021, "DEEP_ARCHIVE"), PT)
    many = cold_object_overhead_monthly(_job(232_021 * 30, "DEEP_ARCHIVE"), PT)
    assert few > 0
    assert many == 30 * few                          # linear in object count


def test_storage_monthly_folds_in_cold_overhead():
    j = _job(7_000_000, "DEEP_ARCHIVE", size_gb=1800.0)
    base = billed_gb(j, PT) * PT.storage_gb_month["DEEP_ARCHIVE"]
    assert storage_monthly(j, PT) > base            # overhead added on top
    assert storage_monthly(j, PT) == base + cold_object_overhead_monthly(j, PT)


def test_bundling_slashes_the_one_time_cost_on_deep_archive():
    # The manga case: 232k .cbz vs ~7M loose pages, same 1.78TB.
    cbz = upfront_onetime(_job(232_021, "DEEP_ARCHIVE", 1824.0), PT)
    loose = upfront_onetime(_job(232_021 * 30, "DEEP_ARCHIVE", 1824.0), PT)
    assert loose > 20 * cbz                          # ~30x, dominated by request count
