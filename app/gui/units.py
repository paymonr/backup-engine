# app/gui/units.py — the one shared SIZE formatter (owner request: sizes display in
# MB, not a flat two-decimal GB, so a small job stops reading "0.35 GB"). Pure, no
# Flask/template import surface, so every module that prints a byte or GB figure
# (routes.py, estimate_io.py, storage_advice.py, s3_rules.py, the Jinja `size` /
# `size_gb` filters registered in create_app, app.js's mirror of these same rules)
# can share it instead of hand-rolling its own thresholds.
#
# 1024-based, unit scales with magnitude so the decimals stay meaningful at every
# size: under 1 KB -> whole bytes; under 1 MB -> whole KB; under 1 GB -> MB to one
# decimal; under 1 TB -> GB to two decimals; else TB to two decimals. Thousands
# separators throughout. PRICES ($/GB, $/GB·mo) and INPUT fields (Bundle size (GB),
# Typical archive size ... GB) are a different concern and never go through this.
from __future__ import annotations

_KB = 1024
_MB = 1024 ** 2
_GB = 1024 ** 3
_TB = 1024 ** 4


def fmt_bytes(b) -> str:
    """A byte count as a human size, unit scaled to magnitude (see module doc).
    None (not yet measured) reads as an em dash, matching every other unmeasured
    figure in the app."""
    if b is None:
        return "—"
    b = float(b)
    if b < _KB:
        return f"{int(round(b)):,} B"
    if b < _MB:
        return f"{int(round(b / _KB)):,} KB"
    if b < _GB:
        return f"{b / _MB:,.1f} MB"
    if b < _TB:
        return f"{b / _GB:,.2f} GB"
    return f"{b / _TB:,.2f} TB"


def fmt_gb(gb) -> str:
    """The same scale, starting from a GB float (job sizes are stored/entered in GB
    today) rather than a raw byte count."""
    if gb is None:
        return "—"
    return fmt_bytes(gb * _GB)
