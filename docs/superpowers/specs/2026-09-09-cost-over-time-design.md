# Cost Over Time — Design Spec

**Date:** 2026-09-09
**Status:** Draft (brainstorming) — design approved in chat; spec persisted before the implementation plan
**Branch:** `cost-over-time`, off `impeccable-versioned-files` (PR #10, the deployed top-of-stack — the versioning-cost projection leans on the versioned-files engine there)
**Slice:** Extends the existing cost subsystem (`app/estimator/*`, `app/gui/estimate_io.py`, `app/gui/templates/estimate.html`). No engine/backup-path changes.
**Note:** internal design docs are gitignored (`docs/`); tracked via `git add -f`, excluded from the distributable image (`.dockerignore`).

---

## 1. Overview & role

Today the Cost screen shows a single **steady-state** monthly number plus First-year and Full-restore totals, a per-job line-item table, a "current spend" panel (real bucket usage priced now), and an optional Cost Explorer invoice/forecast. A live what-if form recomputes the numbers server-side.

The gap: it does not show cost **over time**. A versioning / rolling-backup setup does not cost its steady-state figure on day one — old-version storage **ramps up** as history accumulates, then **plateaus** at the retention horizon. And the genuine **one-time / first-month** costs are either buried (initial upload PUT charge, shown as one column in a per-job table) or not surfaced at all (cold-storage minimum-duration lock-in; the month-1 jump to full-dataset storage).

This feature adds a **cost trajectory**: a live timeline of monthly cost over a horizon, with the one-time costs broken out and explained, and a couple of comparison curves so the effect of "versioning on" vs "rolling" vs "off" is visible on one chart.

## 2. Goals & non-goals

### Goals
- A **month-by-month projection** of the configured jobs' cost, computed in the pure model (no I/O), unit-testable.
- An inline-**SVG timeline** (no external JS library — works offline on Unraid; matches the dark design system), driven **live** by the existing what-if form, with a steady-state marker.
- **Comparison curves** on the same chart: the user's config vs **no-versioning** vs **rolling-30-day**, derived by re-running the projection on scenario variants (no new persisted state).
- A **"Starting out"** card breaking out the three one-time / first-time costs the user selected, each with a plain-English *why*.
- A **milestone table** (M1 / M6 / M12 / M24 / steady) beneath the chart.
- **Consistency:** the projection's plateau equals today's headline "per month" number — the existing figure is re-labeled as the *steady state*.

### Non-goals (this slice)
- **No saved/named custom scenarios** (Approach ② — the "scenario workbench"). Recorded as a README TODO instead.
- **No change to the backup/restore engine, jobs.json, or pricing sources.** Pure presentation + one new pure model function.
- **No change to current-spend or Cost Explorer panels.**
- **No new JS test harness.** The SVG renderer in `app.js` is verified indirectly (route asserts the chart container + embedded projection data); JS remains untested as today.

## 3. Resolved decisions

1. **Approach ①** (timeline + auto-comparison). ② deferred to a README TODO.
2. **Horizon:** 24 months. **Comparison curves:** no-versioning + rolling-30-day.
3. **Projection lives in the pure model** (`app/estimator/model.py`); the GUI adapter assembles the bundle; `app.js` only draws.
4. **Chart:** inline SVG, server-side-rendered for the initial view (noscript-safe) and redrawn client-side on each live recompute.
5. **One-time costs surfaced:** (a) initial upload request charge, (b) cold-storage minimum-duration lock-in, (c) first-month full-storage total.
6. **Commit after each green step** (reboot-safe).

## 4. Projection model (`app/estimator/model.py`)

New pure types + function. No I/O, no print, no AWS — same discipline as the rest of the module.

```python
@dataclass
class MonthPoint:
    month: int          # 1-based
    storage: float      # current-data storage that month
    versioning: float   # noncurrent / old-version storage that month (ramps)
    ingest: float       # request costs that month
    rotation: float     # early-deletion / min-duration proxy that month
    onetime: float      # one-time charges hitting this month (month 1: upload PUT)
    total: float        # storage + versioning + ingest + rotation + onetime

@dataclass
class Projection:
    months: list[MonthPoint]
    steady_state_month: int
    steady_state_monthly: float   # == Estimate.monthly_total (plateau, excl. one-time)

def project(scenario: Scenario, prices: PriceTable, months: int = 24) -> Projection: ...
```

### Per-job, per-month *t* (t = 1..months)
Reuses the existing per-job monthly functions; the only new idea is the versioning ramp.

- **`storage(t)`** = `storage_monthly(job)` — flat from month 1 (whole dataset uploaded upfront; model assumes fixed size).
- **`versioning(t)`** = `versioning_monthly(job, scenario) × fill(t)` where
  `fill(t) = min(1.0, (t × 30.4) / retention_days)` — linear ramp, plateau at the retention horizon. `retention_days` is the job's effective retention (versioned: restic keep-policy proxy; versioned-files: `retention_days`; archive: scenario noncurrent window).
- **`ingest(t)`** = `ingest_monthly(job)` — flat recurring.
- **`rotation(t)`** = `rotation_monthly(job, scenario)` — flat recurring from month 1 (conservative upper bound, consistent with the model's existing rotation stance).
- **`onetime(t)`** = `upfront_onetime(job)` if `t == 1` else `0.0`.

Scenario-level `MonthPoint` = sum across jobs. `steady_state_month = max(1, ceil(max_retention_days / 30.4))`. `steady_state_monthly = estimate(scenario, prices).monthly_total` (asserted equal to the plateau in tests).

### Edge cases
- `months < 1` → `ValueError`.
- No jobs → `months` points all zero, `steady_state_month = 1`.
- Zero/None retention (no versioning) → `fill` clamps so versioning term is flat 0.

## 5. One-time / "Starting out" breakdown

Assembled in the adapter from the priced scenario:

- **Upload request charge** = Σ `upfront_onetime(job)`. Copy: "the PUT cost to upload every file once — per-request, not per-GB, so it's usually pennies."
- **Cold-storage lock-in**, per cold-class job (STANDARD_IA / GLACIER_IR / GLACIER / DEEP_ARCHIVE) = `billed_gb(job) × rate(class) × min_days/30`. Copy: "even if you deleted everything the day after uploading, {class} still bills ~${x} for its {min_days}-day minimum." Omitted for warm (STANDARD) jobs.
- **First monthly bill** = `projection.months[0].total`. Shown prominently.

## 6. Comparison curves

`project()` re-run on `replace(scenario, …)` variants:

- **your config** — the scenario as configured/what-if'd.
- **no versioning** — every job's retention forced to 0 (versioning term → 0; a flat line at base).
- **rolling 30-day** — every job's effective retention capped at 30 days.

All three returned in the bundle; the template/JS overlay them. Curves that coincide with the primary (e.g. an archive-only setup) simply draw on top — acceptable and truthful.

## 7. Data flow

- **`app/gui/estimate_io.py`** — add `projection_bundle(scenario, prices, months=24) -> dict` returning `{"primary": Projection-as-dict, "comparison": {"no_versioning": …, "rolling_30": …}, "onetime": {...}, "steady_state_month": int}`. Pure over its inputs (no pricing I/O; prices passed in, matching `wizard_estimate`'s pattern).
- **`app/gui/routes.py`** — `/estimate` passes the bundle into the template context; `/estimate.json` returns `{**asdict(est), "projection": bundle}` so the existing live-recompute fetch carries it. On invalid input `/estimate.json` still 400s with `{"error": …}` as today.
- **`app/gui/static/app.js`** — extend `paint(data)` to call a new `drawChart(data.projection)` that redraws the SVG polylines + milestone cells + one-time cells from the returned bundle. On error, blank the chart (mirrors the empty-`paint({})` path).
- **`app/gui/templates/estimate.html`** — SSR the initial chart + card + table from the server-provided bundle so the page is correct with JS off.

## 8. Screen layout (extends the dark design-system components)

Order on the page:
1. **Headline** (`card-hero`) — add **First bill** and **Steady-state /mo** to the existing Per-month / First-year / Full-restore totals (Per-month re-labelled to read as the steady state).
2. **"Starting out"** card — the three one-time figures with their explanations.
3. **Timeline** card — inline SVG: x = months (0–24), y = $/mo, 3 legended curves, a vertical steady-state marker, one-time upload shown as a month-1 dot/annotation. `overflow-x:auto` wrapper.
4. **Milestone table** — columns M1 / M6 / M12 / M24 / steady; rows = component breakdown + total.
5. Existing **what-if form** (drives all of the above live), **Current spend**, **Cost Explorer**, **class reference** — unchanged.

## 9. CLI (`app/estimator/cli.py`)

Minor: include the projection milestones in `--json` output (`"projection": {...}`) so the CLI stays a faithful mirror of the model. The human table gains a compact "over time" block (M1 / M12 / steady). No new flags required; `--json` consumers get the full month array.

## 10. Files touched

- `app/estimator/model.py` — `MonthPoint`, `Projection`, `project()`.
- `app/gui/estimate_io.py` — `projection_bundle()` + one-time breakdown helper.
- `app/gui/routes.py` — bundle into `/estimate` context and `/estimate.json`.
- `app/gui/static/app.js` — `drawChart()`; call it from `paint()`.
- `app/gui/templates/estimate.html` — Starting-out card, SVG timeline, milestone table, headline additions.
- `app/estimator/cli.py` — projection in `--json` + compact table block.
- `tests/estimator/test_model.py`, `tests/gui/test_estimate_io.py`, `tests/gui/test_estimate_routes.py` — see §11.
- `README` — Approach ② (scenario workbench) as a TODO.

## 11. Testing (TDD — tests first per step)

**Model (`test_model.py`):**
- ramp: `fill` is linear and `versioning(t)` reaches steady state exactly at the retention horizon, flat after.
- plateau equals `estimate().monthly_total` (excl. one-time).
- month-1 `onetime` == Σ `upfront_onetime`; months ≥ 2 have `onetime == 0`.
- no-versioning variant → versioning term 0 for all months.
- cold lock-in math (`billed_gb × rate × min_days/30`) for each cold class; 0 for STANDARD.
- edge: `months < 1` raises; no-jobs scenario.

**Adapter (`test_estimate_io.py`):** `projection_bundle` shape — `primary`/`comparison`/`onetime` keys; comparison curves derived correctly; one-time breakdown numbers; threads live what-if params.

**Routes (`test_estimate_routes.py`):** `/estimate.json` includes `projection`; invalid input still 400s; `/estimate` HTML contains the chart container + embedded projection JSON + milestone table.

## 12. Deferred — README TODO (Approach ②)

Add to README a "Roadmap / TODO": **Cost scenario workbench** — define, name, and save multiple custom cost scenarios (per-scenario storage class, retention, versioning) and compare N curves on the timeline. Builds directly on the `project()` function and comparison-curve mechanism shipped here.
