# Cost Estimator — Design Spec

**Date:** 2026-08-26
**Status:** Approved (brainstorming) — ready for implementation plan
**Parent spec:** `2026-08-26-unraid-s3-backup-redesign.md` §8 (Cost estimator), §17 Phase 2
**Slice:** First bounded piece of Phase 2. The pricing/cost model + `estimate` CLI only; the web GUI that later wraps it is a separate slice.

---

## 1. Overview & role

A pure, offline cost model for the two backup pipelines (restic appdata + rclone media),
surfaced as an `estimate` command. Given the data you want to back up and the options you
choose (storage class, packing, retention, restore tier), it returns a line-item cost
breakdown plus monthly, first-year, and illustrative full-restore totals.

It exists to support the two decisions an adopter actually makes:

1. **Which storage class** — the "~$2/mo on Deep Archive vs ~$12/mo on a flat backend" choice.
2. **What a restore will cost** — the retrieval + egress "bill shock" that cold classes hide.

Same philosophy as the Phase-1 engine: **usable headless**. The estimator is a CLI today; the
Phase-2 GUI later calls the same pure function, so headless and GUI numbers are identical by
construction.

## 2. Goals & non-goals

### Goals
- A **pure function** `estimate(scenario, prices) -> Estimate` with no I/O and no AWS calls —
  deterministic given inputs + a price table, and therefore exhaustively unit-testable.
- Model the **full §8 term set**: storage per class, versioning overhead, ingest, upfront bulk
  placement, egress/restore, rotation/lifecycle effects, and packing on/off.
- A thin `estimate` **CLI**: scenario values via flags, `region` + storage classes read from a
  mounted `backup.env` when present, sensible defaults so a bare `estimate` runs. Human table
  output + `--json`.
- **stdlib-only** module (`argparse`, `json`, `dataclasses`) — nothing added to the image.
- Every estimate **stamped with the price-table date**.

### Non-goals (this slice)
- No web GUI (separate Phase-2 slice; this module is what it will import).
- No **live AWS Pricing API refresh** — bundled table only.
- **us-east-1 only** — one bundled region table; more regions are additional JSON files later.
- Not a billing-accurate oracle. It is a **decision-support estimate** with explicitly stated
  simplifying assumptions (§7), not a reproduction of every AWS pricing edge case.
- Does not *implement* media packing in the pipeline — it only **models** the on/off cost effect
  (packing itself is a later §17 Phase-3 item).

## 3. Resolved decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python, stdlib-only | GUI is Python; `python3` already in image; pricing math needs no deps |
| Model depth | Full §8 term set | User-selected: complete now |
| Regions | us-east-1 only | Keep v1 lean; regions are additive JSON |
| Live price refresh | Deferred | Its own slice (needs online + creds + AWS Pricing API) |
| Input surface | Flags + `backup.env` fallback + defaults | Scriptable, testable, GUI-reusable |
| Tests | `pytest` (dev-only) against fixed table | Deterministic, offline; pytest never ships in image |

## 4. Architecture & units

```
app/estimator/
  __init__.py        # exports estimate(), Scenario, Estimate, load_prices()
  model.py           # PURE cost model: estimate(Scenario, PriceTable) -> Estimate  (no I/O)
  prices.py          # PriceTable dataclass + load_prices(region) -> PriceTable (reads JSON)
  prices/
    us-east-1.json   # dated price table (the only bundled region for v1)
  cli.py             # argparse front door: flags + backup.env → Scenario → estimate() → render
  __main__.py        # `python -m app.estimator` entrypoint → cli.main()
tests/estimator/
  test_model.py      # one test per cost term + 2 golden end-to-end scenarios
  test_prices.py     # JSON loads, schema present, date stamped
  test_cli.py        # flag parsing, backup.env fallback, table + --json output
```

**Boundaries:**
- `model.py` is pure arithmetic over dataclasses. It never opens a file, reads env, or prints.
  This is the unit the GUI imports.
- `prices.py` is the only file that reads the bundled JSON. Swapping/adding a region = adding a
  JSON file; no model change.
- `cli.py` is the only file that reads `backup.env` / the environment and writes stdout.

**Invocation:** `python -m app.estimator [flags]`. (A short `estimate` wrapper on `PATH` in the
image is a packaging nicety, decided in the plan — not core.)

## 5. Inputs — `Scenario`

A frozen dataclass; every field has a default so a bare run works. Per-pipeline fields are
carried for **appdata** and **media** independently.

| Field | Applies | Default | Meaning |
|---|---|---|---|
| `region` | global | `us-east-1` (from `AWS_REGION`) | selects the price table |
| `{p}_size_gb` | per pipeline | appdata 20, media 2000 | logical data size |
| `{p}_file_count` | per pipeline | appdata 5, media 50000 | object count before packing (→ avg object size) |
| `{p}_storage_class` | per pipeline | appdata STANDARD, media DEEP_ARCHIVE (from config) | one of the 5 classes |
| `media_packing` | media | false | model consolidation into large archive members |
| `pack_member_gb` | media | 5 | target size of a packed member (sets packed object count) |
| `versioning_retention_days` | global | 30 | noncurrent-version retention window (matches module default) |
| `backups_per_month` | per pipeline | appdata 30, media 4 | derived from schedule or given |
| `change_rate_pct` | per pipeline | appdata 10, media 1 | fraction of data rewritten per backup |
| `restore_fraction` | global | 1.0 | fraction of stored data in the illustrative restore |
| `restores_per_year` | global | 1 | for the annualized restore line |
| `retrieval_tier` | global | Bulk | cold-class retrieval tier (Bulk/Standard/Expedited) |

`region` and the two `storage_class` values are read from `backup.env` when it is present; any
flag overrides. Storage-class values are validated against the five known classes.

## 6. Price table — `prices/us-east-1.json`

Dated JSON, hand-authored from AWS public S3 pricing. Shape (illustrative):

```json
{
  "region": "us-east-1",
  "date": "2026-08-26",
  "source": "AWS S3 pricing page (us-east-1), captured manually",
  "storage_gb_month": {
    "STANDARD": 0.023, "STANDARD_IA": 0.0125, "GLACIER_IR": 0.004,
    "GLACIER": 0.0036, "DEEP_ARCHIVE": 0.00099
  },
  "requests": {
    "put_per_1k": 0.005, "get_per_1k": 0.0004, "lifecycle_transition_per_1k": 0.05
  },
  "retrieval": {
    "per_gb": { "GLACIER": {"Bulk": 0.0025, "Standard": 0.01, "Expedited": 0.03},
                "DEEP_ARCHIVE": {"Bulk": 0.0025, "Standard": 0.02},
                "GLACIER_IR": {"Standard": 0.03} },
    "request_per_1k": { "Bulk": 0.025, "Standard": 0.05, "Expedited": 10.0 }
  },
  "data_transfer_out_per_gb": 0.09,
  "constraints": {
    "min_billable_object_kb": 128,
    "min_storage_duration_days": { "STANDARD_IA": 30, "GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180 }
  }
}
```

Numbers above are placeholders to be filled with verified us-east-1 values during
implementation. `date`/`source` are surfaced in every estimate. Warm classes (STANDARD) have
no `min_billable_object_kb` penalty and no `min_storage_duration`.

## 7. Cost model (full §8) — terms & formulas

Computed per pipeline, then summed. Let `S = storage_gb_month[class]`. Derived quantities:

- **Effective object count** — `packed ? ceil(size_gb / pack_member_gb) : file_count`.
- **Billed storage** — for classes with a 128 KB floor, each object bills at ≥128 KB:
  `billed_gb = max(size_gb, effective_object_count × 128KB)`. STANDARD: `billed_gb = size_gb`.

1. **Storage / month** = `billed_gb × S`.
2. **Versioning overhead / month** = `noncurrent_gb × S`, where
   `noncurrent_gb = size_gb × (change_rate_pct/100) × (backups_per_month × versioning_retention_days / 30)`
   — the churned bytes retained as noncurrent versions over the window.
3. **Ingest / month** (recurring deltas) = `new_objects_per_backup × backups_per_month × put_per_1k/1000`,
   `new_objects_per_backup = effective_object_count × (change_rate_pct/100)`. Data transfer *in* is free.
4. **Upfront bulk placement** (one-time) = `effective_object_count × put_per_1k/1000` — the initial
   direct-to-cold ingest of the whole dataset. Counted in first-year, not in the monthly figure.
5. **Egress / restore** (per restore event) =
   `restored_gb × retrieval.per_gb[class][tier]`
   `+ restored_objects × retrieval.request_per_1k[tier]/1000`
   `+ restored_gb × data_transfer_out_per_gb`
   `+ restored_objects × get_per_1k/1000`,
   with `restored_gb = size_gb × restore_fraction`, `restored_objects = effective_object_count × restore_fraction`.
   Warm classes skip the retrieval terms (no thaw). Annualized = `× restores_per_year`.
6. **Rotation / lifecycle / early-deletion** = for cold classes, bytes rotated out before their
   `min_storage_duration_days` still bill for the remainder:
   `rotated_gb_per_month × S × (min_storage_duration_days/30)`, where `rotated_gb_per_month`
   derives from `change_rate_pct` (mirror/prune churn). Warm classes: 0. Plus any lifecycle
   transition requests (0 for the direct-to-cold default).
7. **Packing on/off** is not a separate line but a **modifier**: it sets `effective_object_count`
   (few large members vs. `file_count`), which flows into terms 1, 3, 4, 5 — capturing both the
   request-count savings and the avoided 128 KB min-object penalty.

**Totals:**
- **Monthly total** = terms 1 + 2 + 3 + 6.
- **First-year total** = `12 × monthly total` + term 4 (upfront) + annualized term 5.
- **Illustrative full-restore total** = term 5 computed once at `restore_fraction = 1.0` — the
  standalone "restore everything" figure, shown regardless of `restores_per_year`.

All simplifying assumptions (flat first-tier data-transfer rate; 128 KB floor as a max() rather
than per-object; churn-driven rotation) are stated in-line in `model.py` docstrings and echoed in
a `--assumptions` note.

## 8. CLI

`python -m app.estimator [flags]`

- **Config fallback:** if `--config-dir` (default `/config`) has a `backup.env`, read `AWS_REGION`,
  `APPDATA_STORAGE_CLASS`, `MEDIA_STORAGE_CLASS` from it as defaults. Flags override. The
  estimator reuses the engine's existing safe env-file parser rather than sourcing the file.
- **Flags:** one per `Scenario` field (`--media-size-gb`, `--media-packing`, `--retrieval-tier`,
  `--restore-fraction`, …), all optional.
- **Output (default):** a labeled line-item table per pipeline + a combined totals block, headed
  with the price-table `date`/`source`.
- **`--json`:** the full `Estimate` as structured JSON (same fields the GUI will consume).
- **`--assumptions`:** print the modeling assumptions and exit.
- Exit non-zero with a clear message on an unknown storage class or retrieval tier.

## 9. Output — `Estimate`

A dataclass mirrored to both the table renderer and `--json`:

```
Estimate(
  price_date, price_source, region,
  pipelines = { "appdata": LineItems, "media": LineItems },
  monthly_total, first_year_total, full_restore_total
)
LineItems(storage, versioning, ingest_monthly, upfront_onetime,
          restore_per_event, rotation_monthly, effective_object_count, billed_gb)
```

Money is rendered to cents; the JSON carries raw floats.

## 10. Testing strategy

`pytest`, fully offline, against a **fixed in-repo test price table** (not the shipped one, so
price refreshes don't churn assertions):

- **Per-term unit tests** — each of the 7 terms in isolation with hand-computed expected values,
  including: the 128 KB floor engaging for many small files, warm classes skipping retrieval,
  packing collapsing object count, versioning overhead scaling with retention.
- **Two golden end-to-end scenarios** — (a) warm STANDARD appdata, small; (b) DEEP_ARCHIVE media,
  large, with a full restore — asserting monthly / first-year / full-restore totals.
- **Price-table test** — the shipped `us-east-1.json` loads, has every required key, and is
  `date`-stamped.
- **CLI tests** — flag parsing, `backup.env` fallback (region/class picked up), table renders,
  `--json` round-trips to the same numbers as calling `estimate()` directly.

## 11. Packaging & CI

- **Dockerfile:** add `COPY app/ /app/app/` so `python -m app.estimator` runs in the container.
  No new packages (stdlib-only). Optional `estimate` shim on `PATH` — plan's call.
- **CI:** a new `estimator` job — `pip install pytest`, `pytest tests/estimator/`. No AWS, no
  network. Existing lint job extends to any new shell shim.
- **`.dockerignore`:** ensure `tests/` and `__pycache__` stay out of the image context.

## 12. Interfaces the GUI will later consume (forward-looking, not built here)

- `estimate(scenario: Scenario, prices: PriceTable) -> Estimate` — the pure entry point.
- `load_prices(region: str) -> PriceTable` — table loader.
- `Estimate` as JSON — the GUI's cost screen renders this directly and re-runs `estimate()` on
  every "what-if" slider change.

## 13. Out of scope / future

- Live "refresh from AWS Pricing API" action.
- Additional region tables (add `prices/<region>.json`; no model change).
- GUI cost screen (separate Phase-2 slice).
- Actual media packing in the pipeline (§17 Phase 3) — modeled here, not implemented.
- Non-AWS backends (B2/R2) — Phase 4.
