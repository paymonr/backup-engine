# tests/gui/test_units.py -- the one shared size formatter (owner request: sizes
# display in MB, not a flat two-decimal GB, so a small job stops reading "0.35 GB").
# 1024-based, unit scales with magnitude: B -> KB (0 dp) -> MB (1 dp) -> GB (2 dp)
# -> TB (2 dp), thousands separators, None -> "-".
from app.gui import units

KB = 1024
MB = 1024 ** 2
GB = 1024 ** 3
TB = 1024 ** 4


def test_none_is_an_em_dash():
    assert units.fmt_bytes(None) == "—"


def test_zero_bytes():
    assert units.fmt_bytes(0) == "0 B"


def test_under_1kb_is_bytes_no_decimals():
    assert units.fmt_bytes(1023) == "1,023 B"


def test_exactly_1kb_switches_to_kb():
    assert units.fmt_bytes(1024) == "1 KB"


def test_kb_has_no_decimals():
    assert units.fmt_bytes(1536) == "2 KB"          # 1.5 KB rounds to 2 KB, no decimal


def test_just_under_1mb_is_still_kb():
    assert units.fmt_bytes(MB - 1) == "1,024 KB"     # rounds up at the boundary, still < MB


def test_exactly_1mb_switches_to_mb_one_decimal():
    assert units.fmt_bytes(MB) == "1.0 MB"


def test_small_job_reads_in_mb_not_gb():
    # the owner's own example: a small job used to print "0.35 GB"
    assert units.fmt_bytes(int(0.35 * GB)) == "358.4 MB"


def test_just_under_1gb_is_still_mb():
    assert units.fmt_bytes(GB - 1) == "1,024.0 MB"


def test_exactly_1gb_switches_to_gb_two_decimals():
    assert units.fmt_bytes(GB) == "1.00 GB"


def test_gb_range_matches_prior_two_decimal_style():
    assert units.fmt_bytes(int(52.71 * GB)) == "52.71 GB"


def test_just_under_1tb_is_still_gb():
    assert units.fmt_bytes(TB - 1) == "1,024.00 GB"


def test_exactly_1tb_switches_to_tb_two_decimals():
    assert units.fmt_bytes(TB) == "1.00 TB"


def test_large_value_gets_thousands_separators():
    assert units.fmt_bytes(12345 * GB) == "12.06 TB"
    assert units.fmt_bytes(1024 * TB) == "1,024.00 TB"


def test_fmt_gb_matches_fmt_bytes_scaled():
    assert units.fmt_gb(0.35) == "358.4 MB"
    assert units.fmt_gb(52.71) == "52.71 GB"
    assert units.fmt_gb(1024) == "1.00 TB"           # 1,024 GB = 1 TB


def test_fmt_gb_none_is_an_em_dash():
    assert units.fmt_gb(None) == "—"


def test_fmt_gb_zero():
    assert units.fmt_gb(0) == "0 B"
