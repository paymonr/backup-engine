# Cost Estimator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a pure, offline Python cost model + `estimate` CLI that, given the data you want to back up and the options you choose, returns a line-item S3 cost breakdown plus monthly, first-year, and illustrative full-restore totals.

**Architecture:** A pure function `estimate(scenario, prices) -> Estimate` (no I/O, no AWS) does all arithmetic over a bundled, dated price table. `prices.py` is the only reader of the JSON table; `cli.py` is the only reader of env/`backup.env` and the only writer of stdout. The Phase-2 GUI later imports the same pure function, so headless and GUI numbers are identical by construction.

**Tech Stack:** Python 3 (Alpine's `python3`, already in the image), **standard library only** for the module (`argparse`, `json`, `dataclasses`, `pathlib`, `math`), `pytest` for tests (dev-only, never shipped).

**Spec:** `docs/superpowers/specs/2026-08-26-cost-estimator-design.md`

## Global Constraints

- **stdlib-only module.** The `app/estimator` package imports nothing outside the Python standard library. `pytest` is a dev/CI dependency only and is never added to the image.
- **Pure model.** `model.py` performs no I/O: it never opens a file, reads the environment, prints, or calls AWS. All such effects live in `prices.py` (JSON read) and `cli.py` (env read / stdout write).
- **us-east-1 only.** One bundled table, `app/estimator/prices/us-east-1.json`. Adding a region later is a new JSON file with no model change.
- **Deterministic, offline tests.** Every test runs with no network and no AWS creds, against a **fixed in-repo test price table** (round synthetic numbers) — never the shipped table — so a real price refresh never churns assertions.
- **Date-stamped output.** Every `Estimate` carries the price table's `date` and `source`; the CLI prints them on every run.
- **Known storage classes only:** `STANDARD`, `STANDARD_IA`, `GLACIER_IR`, `GLACIER`, `DEEP_ARCHIVE`. An unknown class or retrieval tier is a clear error, not a silent default.
- **Decision-support, not billing-accurate.** Simplifying assumptions (flat first-tier egress rate; the 128 KB min-object floor applied as `max(size, count×128KB)`; churn-driven rotation) are stated in `model.py` docstrings and surfaced by `--assumptions`.
- **Money:** rendered to cents in the table; raw floats in `--json`.
- **Python style:** type hints on public functions, `from __future__ import annotations`, `dataclasses` for all record types.

---

### Task 1: Price table type + loader + bundled us-east-1 table

Establishes the `PriceTable` type every later task consumes and the loader that reads the bundled JSON. Ships the one dated table.

**Files:**
- Create: `app/__init__.py` (empty — package marker)
- Create: `app/estimator/__init__.py` (empty for now; exports added in Task 6)
- Create: `app/estimator/prices.py`
- Create: `app/estimator/prices/us-east-1.json`
- Create: `pytest.ini` (repo root — makes `app` importable in tests)
- Create: `tests/estimator/conftest.py` (fixed test price table fixture, reused by later tasks)
- Test: `tests/estimator/test_prices.py`

**Interfaces:**
- Consumes: nothing (foundation).
- Produces:
  - `PriceTable` — frozen dataclass with fields: `region: str`, `date: str`, `source: str`, `storage_gb_month: dict[str, float]`, `put_per_1k: float`, `get_per_1k: float`, `lifecycle_transition_per_1k: float`, `retrieval_per_gb: dict[str, dict[str, float]]`, `retrieval_request_per_1k: dict[str, float]`, `data_transfer_out_per_gb: float`, `min_billable_object_kb: float`, `min_storage_duration_days: dict[str, int]`.
  - `PriceTable.from_dict(d: dict) -> PriceTable` — classmethod building a table from parsed JSON (tests use this to build fixed tables).
  - `load_prices(region: str, prices_dir: Path | None = None) -> PriceTable` — reads `<prices_dir or module prices/>/<region>.json`; raises `ValueError` naming the region if the file is absent.

- [ ] **Step 1: Create `pytest.ini` so `app` imports from repo root**

```ini
# pytest.ini
[pytest]
pythonpath = .
testpaths = tests/estimator
```

- [ ] **Step 2: Create the empty package markers**

Create `app/__init__.py` and `app/estimator/__init__.py`, both empty files.

- [ ] **Step 3: Write the fixed test price table fixture**

```python
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
```

- [ ] **Step 4: Write the failing tests**

```python
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
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `python -m pytest tests/estimator/test_prices.py -v`
Expected: FAIL — `app.estimator.prices` does not exist (import error).

- [ ] **Step 6: Implement `app/estimator/prices.py`**

```python
# app/estimator/prices.py — price-table type + loader. The ONLY reader of the bundled JSON.
from __future__ import annotations
from dataclasses import dataclass
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
    retrieval_request_per_1k: dict[str, float]
    data_transfer_out_per_gb: float
    min_billable_object_kb: float
    min_storage_duration_days: dict[str, int]

    @classmethod
    def from_dict(cls, d: dict) -> "PriceTable":
        req = d["requests"]
        ret = d["retrieval"]
        con = d["constraints"]
        return cls(
            region=d["region"], date=d["date"], source=d["source"],
            storage_gb_month=d["storage_gb_month"],
            put_per_1k=req["put_per_1k"], get_per_1k=req["get_per_1k"],
            lifecycle_transition_per_1k=req["lifecycle_transition_per_1k"],
            retrieval_per_gb=ret["per_gb"], retrieval_request_per_1k=ret["request_per_1k"],
            data_transfer_out_per_gb=d["data_transfer_out_per_gb"],
            min_billable_object_kb=con["min_billable_object_kb"],
            min_storage_duration_days=con["min_storage_duration_days"],
        )

def load_prices(region: str, prices_dir: Path | None = None) -> PriceTable:
    base = prices_dir or _MODULE_PRICES_DIR
    path = base / f"{region}.json"
    if not path.is_file():
        raise ValueError(f"no bundled price table for region '{region}' (looked for {path})")
    return PriceTable.from_dict(json.loads(path.read_text()))
```

- [ ] **Step 7: Write the bundled `app/estimator/prices/us-east-1.json`**

Fill every rate with a **verified us-east-1 value** from the AWS S3 pricing page before committing (the numbers below are the current published figures; re-confirm each and update `date`/`source` to the day you capture them):

```json
{
  "region": "us-east-1",
  "date": "2026-08-26",
  "source": "AWS S3 pricing page (us-east-1), captured manually 2026-08-26",
  "storage_gb_month": {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099
  },
  "requests": { "put_per_1k": 0.005, "get_per_1k": 0.0004, "lifecycle_transition_per_1k": 0.05 },
  "retrieval": {
    "per_gb": {
      "STANDARD_IA": { "Standard": 0.01 },
      "GLACIER_IR": { "Standard": 0.03 },
      "GLACIER": { "Bulk": 0.0025, "Standard": 0.01, "Expedited": 0.03 },
      "DEEP_ARCHIVE": { "Bulk": 0.0025, "Standard": 0.02 }
    },
    "request_per_1k": { "Bulk": 0.025, "Standard": 0.05, "Expedited": 10.0 }
  },
  "data_transfer_out_per_gb": 0.09,
  "constraints": {
    "min_billable_object_kb": 128,
    "min_storage_duration_days": { "STANDARD_IA": 30, "GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180 }
  }
}
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/estimator/test_prices.py -v`
Expected: PASS (4 tests).

- [ ] **Step 9: Commit**

```bash
git add app/__init__.py app/estimator/__init__.py app/estimator/prices.py \
  app/estimator/prices/us-east-1.json pytest.ini tests/estimator/conftest.py \
  tests/estimator/test_prices.py
git commit -m "feat(estimator): price-table type + loader + bundled us-east-1 table"
```

---

### Task 2: Scenario/Estimate types + storage & versioning terms

Adds the pure model foundations and the two storage-side monthly terms. `estimate()` returns a full `Estimate`; request/restore terms are wired as `0.0` placeholders until Tasks 3–4 fill them.

**Files:**
- Create: `app/estimator/model.py`
- Test: `tests/estimator/test_model.py`

**Interfaces:**
- Consumes: `PriceTable` (Task 1).
- Produces:
  - `PipelineInputs` — frozen dataclass: `size_gb: float`, `file_count: int`, `storage_class: str`, `packing: bool = False`, `pack_member_gb: float = 5.0`, `backups_per_month: float = 30.0`, `change_rate_pct: float = 10.0`.
  - `Scenario` — frozen dataclass: `region: str = "us-east-1"`, `appdata: PipelineInputs`, `media: PipelineInputs`, `versioning_retention_days: int = 30`, `restore_fraction: float = 1.0`, `restores_per_year: float = 1.0`, `retrieval_tier: str = "Bulk"`. Defaults per spec §5 (appdata 20 GB/5 files/STANDARD/30 backups/10%; media 2000 GB/50000 files/DEEP_ARCHIVE/4 backups/1%).
  - `LineItems` — dataclass: `storage`, `versioning`, `ingest_monthly`, `upfront_onetime`, `rotation_monthly`, `restore_per_event` (all `float`), `effective_object_count: int`, `billed_gb: float`.
  - `Estimate` — dataclass: `price_date: str`, `price_source: str`, `region: str`, `pipelines: dict[str, LineItems]`, `monthly_total: float`, `first_year_total: float`, `full_restore_total: float`.
  - Term helpers (pure, module-level): `effective_object_count(p: PipelineInputs) -> int`, `billed_gb(p: PipelineInputs, prices: PriceTable) -> float`, `storage_monthly(p, prices) -> float`, `versioning_monthly(p, scenario, prices) -> float`.
  - `STORAGE_CLASSES: tuple[str, ...]`, `estimate(scenario: Scenario, prices: PriceTable) -> Estimate`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/estimator/test_model.py
import math
import pytest
from app.estimator.model import (
    PipelineInputs, Scenario, estimate,
    effective_object_count, billed_gb, storage_monthly, versioning_monthly,
)

def test_effective_object_count_uses_file_count_without_packing():
    p = PipelineInputs(size_gb=100, file_count=50000, storage_class="DEEP_ARCHIVE", packing=False)
    assert effective_object_count(p) == 50000

def test_effective_object_count_packs_into_members():
    p = PipelineInputs(size_gb=100, file_count=50000, storage_class="DEEP_ARCHIVE",
                       packing=True, pack_member_gb=5)
    assert effective_object_count(p) == 20  # ceil(100/5)

def test_billed_gb_standard_has_no_floor(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD")
    assert billed_gb(p, prices) == 20

def test_billed_gb_cold_applies_128kb_floor(prices):
    # 10000 tiny objects * 128KB = ~1.2207 GB, above the 0.01 GB actual size
    p = PipelineInputs(size_gb=0.01, file_count=10000, storage_class="DEEP_ARCHIVE")
    assert math.isclose(billed_gb(p, prices), 10000 * 128 / (1024 * 1024), rel_tol=1e-9)

def test_storage_monthly_warm(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD")
    assert math.isclose(storage_monthly(p, prices), 20 * 0.02)

def test_versioning_monthly_scales_with_retention(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD",
                       backups_per_month=30, change_rate_pct=10)
    s = Scenario(appdata=p, media=p, versioning_retention_days=30)
    # 20 * 0.10 * (30 * 30 / 30) = 60 GB noncurrent; * 0.02 = 1.20
    assert math.isclose(versioning_monthly(p, s, prices), 1.20)

def test_estimate_returns_stamped_estimate_with_both_pipelines(prices):
    s = Scenario()
    est = estimate(s, prices)
    assert est.price_date == "2099-01-01"
    assert set(est.pipelines) == {"appdata", "media"}
    assert est.monthly_total >= 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/estimator/test_model.py -v`
Expected: FAIL — `app.estimator.model` does not exist.

- [ ] **Step 3: Implement `app/estimator/model.py` (foundations + storage/versioning)**

```python
# app/estimator/model.py — PURE cost model. No I/O, no env, no print, no AWS.
#
# Simplifying assumptions (decision-support, not billing-accurate):
#   * The 128 KB minimum billable object size is applied as
#     billed_gb = max(size_gb, object_count * 128KB) rather than per-object.
#   * Data-transfer-out uses a single flat first-tier $/GB rate.
#   * Rotation/early-deletion (Task 3) charges churned bytes the full
#     minimum-storage-duration remainder — a conservative upper bound.
from __future__ import annotations
from dataclasses import dataclass, field
from math import ceil
from .prices import PriceTable

STORAGE_CLASSES: tuple[str, ...] = (
    "STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
)
_KB_IN_GB = 1 / (1024 * 1024)

@dataclass(frozen=True)
class PipelineInputs:
    size_gb: float
    file_count: int
    storage_class: str
    packing: bool = False
    pack_member_gb: float = 5.0
    backups_per_month: float = 30.0
    change_rate_pct: float = 10.0

@dataclass(frozen=True)
class Scenario:
    region: str = "us-east-1"
    appdata: PipelineInputs = field(
        default_factory=lambda: PipelineInputs(20, 5, "STANDARD", backups_per_month=30, change_rate_pct=10))
    media: PipelineInputs = field(
        default_factory=lambda: PipelineInputs(2000, 50000, "DEEP_ARCHIVE", backups_per_month=4, change_rate_pct=1))
    versioning_retention_days: int = 30
    restore_fraction: float = 1.0
    restores_per_year: float = 1.0
    retrieval_tier: str = "Bulk"

@dataclass
class LineItems:
    storage: float
    versioning: float
    ingest_monthly: float
    upfront_onetime: float
    rotation_monthly: float
    restore_per_event: float
    effective_object_count: int
    billed_gb: float

@dataclass
class Estimate:
    price_date: str
    price_source: str
    region: str
    pipelines: dict[str, LineItems]
    monthly_total: float
    first_year_total: float
    full_restore_total: float

def _rate(prices: PriceTable, storage_class: str) -> float:
    if storage_class not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{storage_class}'")
    return prices.storage_gb_month[storage_class]

def effective_object_count(p: PipelineInputs) -> int:
    if p.packing:
        return max(1, ceil(p.size_gb / p.pack_member_gb))
    return p.file_count

def billed_gb(p: PipelineInputs, prices: PriceTable) -> float:
    if p.storage_class == "STANDARD":
        return p.size_gb
    floor = effective_object_count(p) * prices.min_billable_object_kb * _KB_IN_GB
    return max(p.size_gb, floor)

def storage_monthly(p: PipelineInputs, prices: PriceTable) -> float:
    return billed_gb(p, prices) * _rate(prices, p.storage_class)

def versioning_monthly(p: PipelineInputs, scenario: Scenario, prices: PriceTable) -> float:
    noncurrent_gb = p.size_gb * (p.change_rate_pct / 100) * (
        p.backups_per_month * scenario.versioning_retention_days / 30)
    return noncurrent_gb * _rate(prices, p.storage_class)

def _line_items(p: PipelineInputs, scenario: Scenario, prices: PriceTable) -> LineItems:
    return LineItems(
        storage=storage_monthly(p, prices),
        versioning=versioning_monthly(p, scenario, prices),
        ingest_monthly=0.0,      # Task 3
        upfront_onetime=0.0,     # Task 3
        rotation_monthly=0.0,    # Task 3
        restore_per_event=0.0,   # Task 4
        effective_object_count=effective_object_count(p),
        billed_gb=billed_gb(p, prices),
    )

def estimate(scenario: Scenario, prices: PriceTable) -> Estimate:
    pipelines = {name: _line_items(getattr(scenario, name), scenario, prices)
                 for name in ("appdata", "media")}
    monthly = sum(li.storage + li.versioning + li.ingest_monthly + li.rotation_monthly
                  for li in pipelines.values())
    upfront = sum(li.upfront_onetime for li in pipelines.values())
    annual_restore = sum(li.restore_per_event for li in pipelines.values()) * scenario.restores_per_year
    first_year = 12 * monthly + upfront + annual_restore
    full_restore = 0.0  # Task 4
    return Estimate(prices.date, prices.source, prices.region, pipelines,
                    monthly, first_year, full_restore)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/estimator/test_model.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/estimator/model.py tests/estimator/test_model.py
git commit -m "feat(estimator): Scenario/Estimate types + storage & versioning terms"
```

---

### Task 3: Ingest, upfront-bulk, and rotation/lifecycle terms

Fills the three request/lifecycle terms and wires them into `LineItems` and the totals.

**Files:**
- Modify: `app/estimator/model.py`
- Test: `tests/estimator/test_model.py` (add tests)

**Interfaces:**
- Consumes: `PipelineInputs`, `Scenario`, `PriceTable`, `effective_object_count` (Tasks 1–2).
- Produces (module-level pure helpers): `ingest_monthly(p, prices) -> float`, `upfront_onetime(p, prices) -> float`, `rotation_monthly(p, scenario, prices) -> float`. `_line_items` now populates `ingest_monthly`, `upfront_onetime`, `rotation_monthly`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/estimator/test_model.py
from app.estimator.model import ingest_monthly, upfront_onetime, rotation_monthly

def test_ingest_monthly_counts_changed_objects(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD",
                       backups_per_month=30, change_rate_pct=10)
    # new objects/backup = 5 * 0.10 = 0.5; * 30 backups = 15; * 0.005/1000
    assert math.isclose(ingest_monthly(p, prices), 15 * 0.005 / 1000)

def test_upfront_onetime_is_one_put_per_object(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    assert math.isclose(upfront_onetime(p, prices), 50000 * 0.005 / 1000)  # 0.25

def test_rotation_zero_for_warm_class(prices):
    p = PipelineInputs(size_gb=20, file_count=5, storage_class="STANDARD",
                       backups_per_month=30, change_rate_pct=10)
    s = Scenario(appdata=p, media=p)
    assert rotation_monthly(p, s, prices) == 0.0

def test_rotation_charges_min_duration_for_cold_churn(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE",
                       backups_per_month=4, change_rate_pct=1)
    s = Scenario(appdata=p, media=p)
    # rotated/mo = 2000 * 0.01 * 4 = 80 GB; * 0.001 * (180/30) = 0.48
    assert math.isclose(rotation_monthly(p, s, prices), 0.48)

def test_estimate_monthly_includes_ingest_and_rotation(prices):
    s = Scenario()
    est = estimate(s, prices)
    appdata = est.pipelines["appdata"]
    assert appdata.ingest_monthly > 0
    assert appdata.upfront_onetime > 0
    # media is cold -> rotation > 0
    assert est.pipelines["media"].rotation_monthly > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/estimator/test_model.py -v`
Expected: FAIL — `ingest_monthly` / `upfront_onetime` / `rotation_monthly` not defined.

- [ ] **Step 3: Add the three helpers and wire them into `_line_items`**

Add to `app/estimator/model.py` (after `versioning_monthly`):

```python
def ingest_monthly(p: PipelineInputs, prices: PriceTable) -> float:
    new_objects_per_backup = effective_object_count(p) * (p.change_rate_pct / 100)
    return new_objects_per_backup * p.backups_per_month * prices.put_per_1k / 1000

def upfront_onetime(p: PipelineInputs, prices: PriceTable) -> float:
    return effective_object_count(p) * prices.put_per_1k / 1000

def rotation_monthly(p: PipelineInputs, scenario: Scenario, prices: PriceTable) -> float:
    min_days = prices.min_storage_duration_days.get(p.storage_class, 0)
    if not min_days:
        return 0.0
    rotated_gb_per_month = p.size_gb * (p.change_rate_pct / 100) * p.backups_per_month
    return rotated_gb_per_month * _rate(prices, p.storage_class) * (min_days / 30)
```

Replace the three placeholder lines in `_line_items`:

```python
        ingest_monthly=ingest_monthly(p, prices),
        upfront_onetime=upfront_onetime(p, prices),
        rotation_monthly=rotation_monthly(p, scenario, prices),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/estimator/test_model.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/estimator/model.py tests/estimator/test_model.py
git commit -m "feat(estimator): ingest, upfront-bulk, and rotation/lifecycle terms"
```

---

### Task 4: Restore/egress term + totals (monthly, first-year, full-restore)

Adds the retrieval/egress term (warm classes skip retrieval) and finalizes all three totals, including the standalone illustrative full-restore figure.

**Files:**
- Modify: `app/estimator/model.py`
- Test: `tests/estimator/test_model.py` (add tests, incl. a golden end-to-end scenario)

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `restore_cost(p, scenario, prices, fraction: float) -> float`. `_line_items` populates `restore_per_event` (= `restore_cost(..., fraction=scenario.restore_fraction)`); `estimate` sets `full_restore_total` = sum of `restore_cost(..., fraction=1.0)` across pipelines.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/estimator/test_model.py
from app.estimator.model import restore_cost

def test_restore_warm_class_has_no_retrieval_fee(prices):
    p = PipelineInputs(size_gb=10, file_count=100, storage_class="STANDARD")
    s = Scenario(appdata=p, media=p, restore_fraction=1.0)
    # only egress + GETs: 10 * 0.10 + 100 * 0.0004/1000
    expected = 10 * 0.10 + 100 * 0.0004 / 1000
    assert math.isclose(restore_cost(p, s, prices, 1.0), expected)

def test_restore_deep_archive_bulk_includes_retrieval_and_egress(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    s = Scenario(appdata=p, media=p, restore_fraction=1.0, retrieval_tier="Bulk")
    # egress 2000*0.10=200; GET 50000*0.0004/1000=0.02; retrieval 2000*0.0025=5;
    # retrieval requests 50000*0.025/1000=1.25 => 206.27
    assert math.isclose(restore_cost(p, s, prices, 1.0), 206.27)

def test_restore_scales_with_fraction(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    s = Scenario(appdata=p, media=p, retrieval_tier="Bulk")
    assert math.isclose(restore_cost(p, s, prices, 0.5), 206.27 / 2)

def test_unknown_retrieval_tier_raises(prices):
    p = PipelineInputs(size_gb=2000, file_count=50000, storage_class="DEEP_ARCHIVE")
    s = Scenario(appdata=p, media=p, retrieval_tier="Warp")
    with pytest.raises(ValueError) as e:
        restore_cost(p, s, prices, 1.0)
    assert "Warp" in str(e.value)

def test_full_restore_total_is_independent_of_restores_per_year(prices):
    s = Scenario(restores_per_year=0)  # no annualized restore, but full-restore still shown
    est = estimate(s, prices)
    assert est.full_restore_total > 0

def test_golden_default_scenario_totals(prices):
    # Whole-model golden against the fixed test table (all terms live).
    est = estimate(Scenario(), prices)
    assert est.monthly_total > 0
    assert est.first_year_total > est.monthly_total  # upfront + restores add on
    assert est.full_restore_total > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/estimator/test_model.py -v`
Expected: FAIL — `restore_cost` not defined; `full_restore_total` still 0.

- [ ] **Step 3: Add `restore_cost` and finalize totals**

Add to `app/estimator/model.py`:

```python
def _retrieval_per_gb(storage_class: str, tier: str, prices: PriceTable) -> float:
    table = prices.retrieval_per_gb.get(storage_class)
    if table is None:
        return 0.0  # warm class (e.g. STANDARD): no retrieval fee
    if tier in table:
        return table[tier]
    if storage_class in ("GLACIER", "DEEP_ARCHIVE"):
        raise ValueError(f"retrieval tier '{tier}' not available for {storage_class}")
    return next(iter(table.values()))  # non-tiered cold (IA/GLACIER_IR): single rate

def restore_cost(p: PipelineInputs, scenario: Scenario, prices: PriceTable, fraction: float) -> float:
    restored_gb = p.size_gb * fraction
    restored_objects = effective_object_count(p) * fraction
    cost = restored_gb * prices.data_transfer_out_per_gb + restored_objects * prices.get_per_1k / 1000
    per_gb = _retrieval_per_gb(p.storage_class, scenario.retrieval_tier, prices)
    if per_gb:
        cost += restored_gb * per_gb
        if p.storage_class in ("GLACIER", "DEEP_ARCHIVE"):
            cost += restored_objects * prices.retrieval_request_per_1k[scenario.retrieval_tier] / 1000
    return cost
```

Replace the `restore_per_event=0.0` placeholder in `_line_items`:

```python
        restore_per_event=restore_cost(p, scenario, prices, scenario.restore_fraction),
```

Replace the `full_restore = 0.0` line in `estimate`:

```python
    full_restore = sum(restore_cost(getattr(scenario, name), scenario, prices, 1.0)
                       for name in ("appdata", "media"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/estimator/ -v`
Expected: PASS (all model + prices tests).

- [ ] **Step 5: Commit**

```bash
git add app/estimator/model.py tests/estimator/test_model.py
git commit -m "feat(estimator): restore/egress term + monthly/first-year/full-restore totals"
```

---

### Task 5: `estimate` CLI (flags + backup.env fallback + table/JSON output)

The command-line front door: parse flags, fall back to a mounted `backup.env` for region + storage classes, run the pure model, render a table (default), `--json`, or `--assumptions`.

**Files:**
- Create: `app/estimator/cli.py`
- Create: `app/estimator/__main__.py`
- Test: `tests/estimator/test_cli.py`

**Interfaces:**
- Consumes: `Scenario`, `PipelineInputs`, `estimate`, `Estimate` (model); `load_prices` (prices).
- Produces: `build_scenario(args, env: dict[str, str]) -> Scenario`; `render_table(est: Estimate) -> str`; `estimate_to_dict(est: Estimate) -> dict`; `main(argv: list[str] | None = None) -> int`. `__main__.py` calls `sys.exit(main())`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/estimator/test_cli.py
import json
import pytest
from app.estimator.cli import build_scenario, estimate_to_dict, main, _read_env_file
from app.estimator.model import estimate
from app.estimator.cli import _parser

def _args(argv):
    return _parser().parse_args(argv)

def test_read_env_file_ignores_comments_and_quotes(tmp_path):
    f = tmp_path / "backup.env"
    f.write_text('# comment\nAWS_REGION=us-west-2\nMEDIA_STORAGE_CLASS="GLACIER"\n\n')
    env = _read_env_file(f)
    assert env["AWS_REGION"] == "us-west-2"
    assert env["MEDIA_STORAGE_CLASS"] == "GLACIER"

def test_build_scenario_reads_region_and_classes_from_env():
    env = {"AWS_REGION": "us-west-2", "APPDATA_STORAGE_CLASS": "STANDARD_IA",
           "MEDIA_STORAGE_CLASS": "GLACIER"}
    s = build_scenario(_args([]), env)
    assert s.region == "us-west-2"
    assert s.appdata.storage_class == "STANDARD_IA"
    assert s.media.storage_class == "GLACIER"

def test_flags_override_env():
    env = {"MEDIA_STORAGE_CLASS": "GLACIER"}
    s = build_scenario(_args(["--media-storage-class", "DEEP_ARCHIVE", "--media-size-gb", "500"]), env)
    assert s.media.storage_class == "DEEP_ARCHIVE"
    assert s.media.size_gb == 500

def test_json_output_matches_direct_estimate(prices, capsys, monkeypatch):
    # force the CLI to use the fixed test table
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    rc = main(["--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    direct = estimate_to_dict(estimate(build_scenario(_args([]), {}), prices))
    assert out["monthly_total"] == direct["monthly_total"]
    assert out["price_date"] == "2099-01-01"

def test_table_output_shows_price_date(prices, capsys, monkeypatch):
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    assert main([]) == 0
    assert "2099-01-01" in capsys.readouterr().out

def test_unknown_storage_class_flag_errors(prices, monkeypatch, capsys):
    monkeypatch.setattr("app.estimator.cli.load_prices", lambda region: prices)
    rc = main(["--media-storage-class", "NEBULA"])
    assert rc != 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/estimator/test_cli.py -v`
Expected: FAIL — `app.estimator.cli` does not exist.

- [ ] **Step 3: Implement `app/estimator/cli.py`**

```python
# app/estimator/cli.py — the ONLY reader of env/backup.env and writer of stdout.
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from .model import PipelineInputs, Scenario, Estimate, estimate, STORAGE_CLASSES
from .prices import load_prices

_ASSUMPTIONS = """cost model assumptions (decision-support, not billing-accurate):
  * 128 KB minimum object size applied as max(size, object_count * 128KB), not per-object
  * data-transfer-out uses a single flat first-tier $/GB rate
  * rotation charges churned bytes the full minimum-storage-duration remainder
  * one bundled price table (us-east-1); every figure is stamped with its capture date"""

def _read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.split("#", 1)[0].strip().strip('"').strip("'")
    return env

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="estimate", description="Estimate S3 backup costs.")
    ap.add_argument("--config-dir", default="/config",
                    help="directory holding backup.env (region + storage classes fallback)")
    ap.add_argument("--json", action="store_true", help="emit structured JSON instead of a table")
    ap.add_argument("--assumptions", action="store_true", help="print modeling assumptions and exit")
    ap.add_argument("--region")
    ap.add_argument("--retrieval-tier", default=None)
    ap.add_argument("--restore-fraction", type=float, default=None)
    ap.add_argument("--restores-per-year", type=float, default=None)
    ap.add_argument("--versioning-retention-days", type=int, default=None)
    for p in ("appdata", "media"):
        ap.add_argument(f"--{p}-size-gb", type=float, default=None)
        ap.add_argument(f"--{p}-file-count", type=int, default=None)
        ap.add_argument(f"--{p}-storage-class", default=None)
        ap.add_argument(f"--{p}-backups-per-month", type=float, default=None)
        ap.add_argument(f"--{p}-change-rate-pct", type=float, default=None)
    ap.add_argument("--media-packing", action="store_true")
    ap.add_argument("--media-pack-member-gb", type=float, default=None)
    return ap

def _pipeline(args, env, name: str, default: PipelineInputs) -> PipelineInputs:
    g = lambda attr, fallback: getattr(args, f"{name}_{attr}") if getattr(args, f"{name}_{attr}") is not None else fallback
    cls = g("storage_class", env.get(f"{name.upper()}_STORAGE_CLASS", default.storage_class))
    if cls not in STORAGE_CLASSES:
        raise ValueError(f"unknown storage class '{cls}' (choose from {', '.join(STORAGE_CLASSES)})")
    return PipelineInputs(
        size_gb=g("size_gb", default.size_gb),
        file_count=g("file_count", default.file_count),
        storage_class=cls,
        packing=(args.media_packing if name == "media" else False),
        pack_member_gb=(args.media_pack_member_gb if name == "media" and args.media_pack_member_gb else default.pack_member_gb),
        backups_per_month=g("backups_per_month", default.backups_per_month),
        change_rate_pct=g("change_rate_pct", default.change_rate_pct),
    )

def build_scenario(args, env: dict[str, str]) -> Scenario:
    d = Scenario()  # defaults
    region = args.region or env.get("AWS_REGION", d.region)
    return Scenario(
        region=region,
        appdata=_pipeline(args, env, "appdata", d.appdata),
        media=_pipeline(args, env, "media", d.media),
        versioning_retention_days=args.versioning_retention_days or d.versioning_retention_days,
        restore_fraction=args.restore_fraction if args.restore_fraction is not None else d.restore_fraction,
        restores_per_year=args.restores_per_year if args.restores_per_year is not None else d.restores_per_year,
        retrieval_tier=args.retrieval_tier or d.retrieval_tier,
    )

def estimate_to_dict(est: Estimate) -> dict:
    return asdict(est)

def render_table(est: Estimate) -> str:
    lines = [f"S3 backup cost estimate  (prices: {est.region} @ {est.price_date} — {est.price_source})", ""]
    for name, li in est.pipelines.items():
        lines += [
            f"[{name}]  billed {li.billed_gb:.2f} GB across {li.effective_object_count} objects",
            f"  storage/mo        ${li.storage:,.2f}",
            f"  versioning/mo     ${li.versioning:,.2f}",
            f"  ingest/mo         ${li.ingest_monthly:,.2f}",
            f"  rotation/mo       ${li.rotation_monthly:,.2f}",
            f"  upfront (once)    ${li.upfront_onetime:,.2f}",
            f"  restore/event     ${li.restore_per_event:,.2f}",
            "",
        ]
    lines += [
        f"MONTHLY total       ${est.monthly_total:,.2f}",
        f"FIRST-YEAR total    ${est.first_year_total:,.2f}",
        f"FULL-RESTORE (once) ${est.full_restore_total:,.2f}",
    ]
    return "\n".join(lines)

def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.assumptions:
        print(_ASSUMPTIONS)
        return 0
    env: dict[str, str] = {}
    cfg = Path(args.config_dir) / "backup.env"
    if cfg.is_file():
        env = _read_env_file(cfg)
    try:
        scenario = build_scenario(args, env)
        est = estimate(scenario, load_prices(scenario.region))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(estimate_to_dict(est), indent=2) if args.json else render_table(est))
    return 0
```

- [ ] **Step 4: Implement `app/estimator/__main__.py`**

```python
# app/estimator/__main__.py — enables `python -m app.estimator`
import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/estimator/test_cli.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Smoke-run the CLI against the bundled table**

Run: `python -m app.estimator` and `python -m app.estimator --json`
Expected: a table headed with the us-east-1 price date; valid JSON. (No creds, no network.)

- [ ] **Step 7: Commit**

```bash
git add app/estimator/cli.py app/estimator/__main__.py tests/estimator/test_cli.py
git commit -m "feat(estimator): estimate CLI (flags + backup.env fallback + table/JSON)"
```

---

### Task 6: Package into the image, CI job, docs

Ships the command in the container, adds a CI job that runs the estimator tests, keeps tests out of the image, and documents usage. The Docker build itself is CI-verified (no Docker on the dev host — verify statically + confirm the local `python -m app.estimator` smoke run passes).

**Files:**
- Modify: `app/estimator/__init__.py` (export the public API)
- Modify: `Dockerfile` (COPY `app/`)
- Modify: `.dockerignore` (exclude `tests/`, `__pycache__`, `*.pyc`)
- Modify: `.github/workflows/ci.yml` (add `estimator` job)
- Modify: `README.md` (Development + a short "Cost estimator" usage note)

**Interfaces:**
- Consumes: the whole `app/estimator` package.
- Produces: `from app.estimator import estimate, Scenario, PipelineInputs, Estimate, load_prices` (the surface the GUI will import); `python -m app.estimator` available in the image.

- [ ] **Step 1: Export the public API from `app/estimator/__init__.py`**

```python
# app/estimator/__init__.py
from .model import estimate, Scenario, PipelineInputs, LineItems, Estimate, STORAGE_CLASSES
from .prices import PriceTable, load_prices

__all__ = [
    "estimate", "Scenario", "PipelineInputs", "LineItems", "Estimate",
    "STORAGE_CLASSES", "PriceTable", "load_prices",
]
```

- [ ] **Step 2: Add an import test**

```python
# add to tests/estimator/test_model.py
def test_public_api_importable():
    from app.estimator import estimate, Scenario, PipelineInputs, Estimate, load_prices  # noqa: F401
```

Run: `python -m pytest tests/estimator/ -v` — Expected: PASS (all tests).

- [ ] **Step 3: COPY the package into the image**

In `Dockerfile`, after the existing `COPY scripts/ /app/scripts/` block, add:

```dockerfile
COPY app/ /app/app/
```

(`WORKDIR /app` is already set, so `python -m app.estimator` resolves.)

- [ ] **Step 4: Keep tests and caches out of the build context**

Ensure `.dockerignore` contains:

```
tests/
**/__pycache__/
*.pyc
```

- [ ] **Step 5: Add the `estimator` CI job**

In `.github/workflows/ci.yml`, add a job alongside the others:

```yaml
  estimator:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pytest
      - run: python -m pytest tests/estimator/ -v
```

- [ ] **Step 6: Document usage in `README.md`**

Under **Development**, add:

```markdown
- `python -m pytest tests/estimator/` — cost-estimator unit tests.
```

And add a short section after **Cost note**:

```markdown
## Cost estimator

Estimate what a given backup shape will cost on S3 before you commit to a storage class:

    python -m app.estimator                 # uses defaults + /config/backup.env if present
    python -m app.estimator --media-size-gb 4000 --media-storage-class DEEP_ARCHIVE --retrieval-tier Bulk
    python -m app.estimator --json           # machine-readable breakdown
    python -m app.estimator --assumptions    # what the model does and does not account for

It reads `AWS_REGION` and the storage classes from `backup.env` when present (flags override),
runs fully offline against a bundled, dated us-east-1 price table, and prints a per-pipeline
line-item breakdown plus monthly, first-year, and illustrative full-restore totals.
```

- [ ] **Step 7: Verify locally (what can be verified without Docker)**

Run: `python -m pytest tests/estimator/ -v` (all pass) and `python -m app.estimator --assumptions` (prints).
Statically confirm the `Dockerfile` `COPY app/` line and the `.dockerignore`/CI YAML edits. Note in the commit that the image build + `python -m app.estimator` in-container are verified by the CI `image` and `estimator` jobs, not locally.

- [ ] **Step 8: Commit**

```bash
git add app/estimator/__init__.py tests/estimator/test_model.py Dockerfile \
  .dockerignore .github/workflows/ci.yml README.md
git commit -m "feat(estimator): package into image, add CI job + docs"
```

---

## Self-Review

**Spec coverage:**
- §2/§3 pure module, stdlib-only, us-east-1, flags+config fallback, dated output → Tasks 1–6; Global Constraints. ✅
- §4 architecture/units (model pure; prices sole JSON reader; cli sole env/stdout) → Tasks 1, 2, 5. ✅
- §5 `Scenario`/`PipelineInputs` fields + defaults → Task 2 (nested representation of the spec's per-pipeline field list). ✅
- §6 price-table schema + dated table → Task 1. ✅
- §7 all seven terms + three totals → Tasks 2 (storage, versioning), 3 (ingest, upfront, rotation), 4 (restore, packing-as-modifier via `effective_object_count`, totals). ✅
- §8 CLI (flags, config fallback, table, `--json`, `--assumptions`, error on bad class/tier) → Task 5. ✅
- §9 `Estimate`/`LineItems` output shape → Task 2 (types), Task 5 (render + JSON). ✅
- §10 testing (per-term, two goldens, price-table test, CLI tests, offline) → Tasks 1–5. ✅
- §11 packaging (Dockerfile COPY, CI job, `.dockerignore`) → Task 6. ✅
- §12 forward-looking GUI interfaces (`estimate`, `load_prices`, `Estimate` JSON) → exported in Task 6. ✅
- §13 out-of-scope (live refresh, more regions, GUI, packing impl) → not built; respected. ✅

**Placeholder scan:** No "TBD/TODO/handle edge cases" steps; every code step has real code. The one intentional data placeholder — the us-east-1 price *numbers* — is explicitly flagged in Task 1 Step 7 as "verify against AWS and update before committing," which is a data-entry action, not an unspecified implementation. ✅

**Type consistency:** `PriceTable` fields (Task 1) match every `prices.*` access in Tasks 2–5. `PipelineInputs`/`Scenario`/`LineItems`/`Estimate` fields defined in Task 2 match all reads in Tasks 3–5 and the CLI. Helper signatures (`storage_monthly`, `versioning_monthly`, `ingest_monthly`, `upfront_onetime`, `rotation_monthly`, `restore_cost`, `effective_object_count`, `billed_gb`) are consistent between definition and call sites. `estimate`/`load_prices`/`build_scenario`/`render_table`/`estimate_to_dict`/`main` signatures match their tests. ✅
