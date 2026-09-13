# Cost Over Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show S3 backup cost as a live month-by-month timeline (ramp → plateau) with the one-time/first-month costs broken out and explained, plus no-versioning / rolling-30 comparison curves.

**Architecture:** One new pure function in the estimator model (`project()`) computes a per-month cost trajectory by reusing the existing per-job monthly functions and ramping only the versioning term. The GUI adapter assembles a bundle (primary + comparison variants + one-time breakdown); the existing `/estimate` + `/estimate.json` routes carry it; `app.js` draws an inline SVG from it (no external library); the template renders a Starting-out card + milestone table as the no-JS fallback.

**Tech Stack:** Python 3, Flask + Jinja2 (server-rendered GUI), vanilla ES5-style JS (matches existing `app.js`), pytest + bats. Prices from the bundled `us-east-1` table (offline).

**Spec:** `docs/superpowers/specs/2026-09-09-cost-over-time-design.md`

## Global Constraints

- **Model purity:** `app/estimator/model.py` does NO I/O, print, env, or AWS. All cost math lives there; the GUI adapter arranges results, it does not compute dollars.
- **No external JS/CSS libraries.** The chart is hand-drawn inline SVG (Unraid runs offline; must match the existing dark design system via CSS variables / `currentColor`).
- **Offline tests:** GUI routes run with `PRICES_LIVE=False` (bundled table, no network).
- **Retention consistency:** the versioning ramp MUST use the same effective retention the steady-state `versioning_monthly` uses — via the shared `job_retention_days(job, scenario)` helper. Never divide by a zero/None retention.
- **Days/month constant:** `_DAYS_PER_MONTH = 30.4` (matches `schedule.py`).
- **Commits:** conventional-commit messages (`feat:`/`test:`/`docs:`), one per task, no attribution lines. `docs/` is git-ignored — force-add spec/plan files only; code/tests/README add normally.
- **Default horizon:** 24 months. **Comparison curves:** `no_versioning` (retention→0), `rolling_30` (retention capped at 30d).

---

### Task 1: Projection + one-time math in the pure model

**Files:**
- Modify: `app/estimator/model.py` (add `job_retention_days`, `cold_lockin_onetime`, `MonthPoint`, `Projection`, `project`; refactor `versioning_monthly` to use the helper)
- Test: `tests/estimator/test_model.py`

**Interfaces:**
- Consumes: existing `storage_monthly`, `versioning_monthly`, `ingest_monthly`, `rotation_monthly`, `upfront_onetime`, `billed_gb`, `_rate`, `estimate`, `STORAGE_CLASSES`; `Scenario`, `JobInputs`, `PriceTable`.
- Produces:
  - `job_retention_days(p: JobInputs, scenario: Scenario) -> int`
  - `cold_lockin_onetime(p: JobInputs, prices: PriceTable) -> float`
  - `@dataclass MonthPoint(month:int, storage:float, versioning:float, ingest:float, rotation:float, onetime:float, total:float)`
  - `@dataclass Projection(months:list[MonthPoint], steady_state_month:int, steady_state_monthly:float)`
  - `project(scenario: Scenario, prices: PriceTable, months: int = 24) -> Projection`

- [ ] **Step 1: Write the failing tests**

Append to `tests/estimator/test_model.py` (uses the existing `J()`, `_scn()`, `prices` fixture):

```python
from app.estimator.model import (
    job_retention_days, cold_lockin_onetime, project, MonthPoint, Projection,
)

# A versioned job with a 90-day retention gives a visible multi-month ramp
# (one calendar month ~30.4 days, so 30-day retention would fill within month 1).
def _ramp_job():
    return J(20, 5, "STANDARD", name="v", engine="versioned",
             backups_per_month=30, change_rate_pct=10, versioning_retention_days=90)

def test_job_retention_days_falls_back_to_scenario():
    j = J(20, 5, "STANDARD")  # versioning_retention_days=None
    assert job_retention_days(j, _scn(j, versioning_retention_days=45)) == 45
    j2 = J(20, 5, "STANDARD", versioning_retention_days=90)
    assert job_retention_days(j2, _scn(j2, versioning_retention_days=45)) == 90

def test_cold_lockin_deep_archive(prices):
    # billed_gb=2000 (size dominates floor); 2000 * 0.001 * (180/30) = 12.0
    assert math.isclose(cold_lockin_onetime(J(2000, 50000, "DEEP_ARCHIVE"), prices), 12.0)

def test_cold_lockin_zero_for_standard(prices):
    assert cold_lockin_onetime(J(20, 5, "STANDARD"), prices) == 0.0

def test_project_length_and_steady_month(prices):
    j = _ramp_job()
    proj = project(_scn(j), prices, months=24)
    assert isinstance(proj, Projection) and len(proj.months) == 24
    assert proj.months[0].month == 1 and proj.months[-1].month == 24
    assert proj.steady_state_month == 3  # ceil(90 / 30.4) == 3

def test_project_versioning_ramps_then_plateaus(prices):
    j = _ramp_job()
    m = project(_scn(j), prices, months=24).months
    # steady versioning = 20 * 0.10 * (30*90/30) = 180 GB * 0.02 = 3.60
    assert m[0].versioning < m[1].versioning < m[2].versioning
    assert math.isclose(m[2].versioning, 3.60, rel_tol=1e-9)   # filled at month 3
    assert math.isclose(m[23].versioning, m[2].versioning)     # flat after plateau

def test_project_month1_carries_onetime_only(prices):
    j = _ramp_job()
    m = project(_scn(j), prices, months=24).months
    assert math.isclose(m[0].onetime, upfront_onetime(j, prices))
    assert m[1].onetime == 0.0 and m[23].onetime == 0.0

def test_project_plateau_total_equals_monthly_total(prices):
    scn = _scn(_ramp_job())
    proj = project(scn, prices, months=24)
    # last month has no one-time, so its total is the steady monthly bill
    assert math.isclose(proj.months[-1].total, estimate(scn, prices).monthly_total)
    assert math.isclose(proj.steady_state_monthly, estimate(scn, prices).monthly_total)

def test_project_zero_retention_has_no_versioning(prices):
    j = J(20, 5, "STANDARD", versioning_retention_days=0,
          backups_per_month=30, change_rate_pct=10)
    m = project(_scn(j), prices, months=6).months  # must not ZeroDivisionError
    assert all(pt.versioning == 0.0 for pt in m)

def test_project_rejects_nonpositive_months(prices):
    with pytest.raises(ValueError):
        project(_scn(_ramp_job()), prices, months=0)

def test_project_no_jobs(prices):
    proj = project(_scn(), prices, months=12)
    assert len(proj.months) == 12
    assert all(pt.total == 0.0 for pt in proj.months)
    assert proj.steady_state_month == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/estimator/test_model.py -k "project or lockin or retention_days" -v`
Expected: FAIL — `cannot import name 'project'` / `job_retention_days` / `cold_lockin_onetime`.

- [ ] **Step 3: Implement in `app/estimator/model.py`**

Refactor `versioning_monthly` to use the shared helper (no behavior change), and add the new pieces. Add near the other pure functions:

```python
_DAYS_PER_MONTH = 30.4

def job_retention_days(p: JobInputs, scenario: Scenario) -> int:
    """The effective noncurrent-version retention (days) for a job: its own
    override when set, else the scenario-level window. Shared by the steady-state
    versioning term and the over-time ramp so the two never diverge."""
    return (p.versioning_retention_days if p.versioning_retention_days is not None
            else scenario.versioning_retention_days)

def cold_lockin_onetime(p: JobInputs, prices: PriceTable) -> float:
    """The minimum you pay for the INITIAL dataset in a cold class even if you
    deleted it the day after upload: billed_gb * $/GB * (min_days / 30). Zero for
    classes with no minimum-storage-duration (STANDARD)."""
    min_days = prices.min_storage_duration_days.get(p.storage_class, 0)
    if not min_days:
        return 0.0
    return billed_gb(p, prices) * _rate(prices, p.storage_class) * (min_days / 30)
```

Change `versioning_monthly` to compute retention via the helper:

```python
def versioning_monthly(p: JobInputs, scenario: Scenario, prices: PriceTable) -> float:
    retention = job_retention_days(p, scenario)
    noncurrent_gb = p.size_gb * (p.change_rate_pct / 100) * (
        p.backups_per_month * retention / 30)
    return noncurrent_gb * _rate(prices, p.storage_class)
```

Add the dataclasses next to `Estimate`:

```python
@dataclass
class MonthPoint:
    month: int
    storage: float
    versioning: float
    ingest: float
    rotation: float
    onetime: float
    total: float

@dataclass
class Projection:
    months: list[MonthPoint]
    steady_state_month: int
    steady_state_monthly: float
```

Add `project` after `estimate`:

```python
def _versioning_fill(retention_days: int, month: int) -> float:
    """Fraction of the steady-state noncurrent history accumulated by `month`:
    linear ramp to 1.0 at the retention horizon, flat after. Zero retention
    (versioning off) -> 0.0, never a divide-by-zero."""
    if retention_days <= 0:
        return 0.0
    return min(1.0, (month * _DAYS_PER_MONTH) / retention_days)

def project(scenario: Scenario, prices: PriceTable, months: int = 24) -> Projection:
    if months < 1:
        raise ValueError("months must be >= 1")
    per_job = []  # (storage, steady_versioning, ingest, rotation, onetime, retention)
    max_retention = 0
    for j in scenario.jobs:
        ret = job_retention_days(j, scenario)
        max_retention = max(max_retention, ret if ret and ret > 0 else 0)
        per_job.append((
            storage_monthly(j, prices), versioning_monthly(j, scenario, prices),
            ingest_monthly(j, prices), rotation_monthly(j, scenario, prices),
            upfront_onetime(j, prices), ret,
        ))
    pts: list[MonthPoint] = []
    for t in range(1, months + 1):
        s = v = ing = rot = one = 0.0
        for store, steady_ver, jing, jrot, jup, jret in per_job:
            s += store
            v += steady_ver * _versioning_fill(jret, t)
            ing += jing
            rot += jrot
            if t == 1:
                one += jup
        pts.append(MonthPoint(t, s, v, ing, rot, one, s + v + ing + rot + one))
    steady_month = min(months, max(1, ceil(max_retention / _DAYS_PER_MONTH))) if max_retention else 1
    steady_monthly = sum(store + sv + ing + rot for store, sv, ing, rot, _up, _r in per_job)
    return Projection(pts, steady_month, steady_monthly)
```

- [ ] **Step 4: Run tests to verify they pass (and nothing regressed)**

Run: `pytest tests/estimator/test_model.py -v`
Expected: PASS (new tests + all existing model tests, including `versioning_monthly`).

- [ ] **Step 5: Commit**

```bash
git add app/estimator/model.py tests/estimator/test_model.py
git commit -m "feat(cost): pure per-month projection + cold-storage lock-in in model"
```

---

### Task 2: Projection bundle + one-time breakdown in the GUI adapter

**Files:**
- Modify: `app/gui/estimate_io.py` (imports + `projection_bundle`)
- Test: `tests/gui/test_estimate_io.py`

**Interfaces:**
- Consumes: `project`, `job_retention_days`, `cold_lockin_onetime`, `upfront_onetime` from `..estimator.model`; `dataclasses.asdict`/`replace`; existing `scenario_from_jobs`.
- Produces: `projection_bundle(scenario, prices, months: int = 24) -> dict` with shape:
  `{"primary": <Projection asdict>, "comparison": {"no_versioning": <asdict>, "rolling_30": <asdict>}, "onetime": {"upload": float, "lockin": [{"job": str, "storage_class": str, "amount": float}], "first_month": float}, "steady_state_month": int}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/test_estimate_io.py` (uses existing `_cfg`, `VJOB`, `VFJOB`, `SRC`). VFJOB is DEEP_ARCHIVE with `retention_days: 45`:

```python
from app.estimator.prices import load_prices

def _prices():
    return load_prices("us-east-1", live=False)

def test_projection_bundle_shape(tmp_path):
    scn = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    b = estimate_io.projection_bundle(scn, _prices(), months=24)
    assert set(b) == {"primary", "comparison", "onetime", "steady_state_month"}
    assert len(b["primary"]["months"]) == 24
    assert set(b["comparison"]) == {"no_versioning", "rolling_30"}
    assert b["onetime"]["first_month"] == b["primary"]["months"][0]["total"]

def test_projection_bundle_no_versioning_curve_is_flat_zero(tmp_path):
    scn = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    nv = estimate_io.projection_bundle(scn, _prices())["comparison"]["no_versioning"]
    assert all(m["versioning"] == 0.0 for m in nv["months"])

def test_projection_bundle_lockin_lists_only_cold_jobs(tmp_path):
    # VJOB=STANDARD (no lock-in), VFJOB=DEEP_ARCHIVE (has lock-in)
    scn = estimate_io.scenario_from_jobs(_cfg(tmp_path, [VJOB, VFJOB]), SRC)
    lockin = estimate_io.projection_bundle(scn, _prices())["onetime"]["lockin"]
    names = {row["job"] for row in lockin}
    assert names == {"docs"}  # VFJOB
    assert all(row["amount"] > 0 for row in lockin)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/gui/test_estimate_io.py -k projection_bundle -v`
Expected: FAIL — `module 'app.gui.estimate_io' has no attribute 'projection_bundle'`.

- [ ] **Step 3: Implement `projection_bundle` in `app/gui/estimate_io.py`**

Add to the model imports at top:

```python
from ..estimator.model import (
    JobInputs, Scenario, STORAGE_CLASSES, effective_retention_days, estimate,
    restore_cost, project, job_retention_days, cold_lockin_onetime, upfront_onetime,
)
from dataclasses import asdict  # `replace` is already imported
```

Add the function (no dollar math here beyond arranging model outputs — the two sums call model functions):

```python
def projection_bundle(scenario: Scenario, prices, months: int = 24) -> dict:
    """Primary trajectory + comparison variants + the one-time/first-month
    breakdown, all as plain dicts for the template and /estimate.json. Pure over
    its inputs (prices are passed in, like wizard_estimate)."""
    primary = project(scenario, prices, months)

    def _retagged(scn, cap):
        return replace(
            scn,
            jobs=tuple(replace(j, versioning_retention_days=cap(job_retention_days(j, scn)))
                       for j in scn.jobs),
            versioning_retention_days=cap(scn.versioning_retention_days),
        )
    no_versioning = _retagged(scenario, lambda _r: 0)
    rolling_30 = _retagged(scenario, lambda r: min(r, 30))

    onetime = {
        "upload": sum(upfront_onetime(j, prices) for j in scenario.jobs),
        "lockin": [{"job": j.name, "storage_class": j.storage_class,
                    "amount": cold_lockin_onetime(j, prices)}
                   for j in scenario.jobs if cold_lockin_onetime(j, prices) > 0],
        "first_month": primary.months[0].total,
    }
    return {
        "primary": asdict(primary),
        "comparison": {
            "no_versioning": asdict(project(no_versioning, prices, months)),
            "rolling_30": asdict(project(rolling_30, prices, months)),
        },
        "onetime": onetime,
        "steady_state_month": primary.steady_state_month,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/gui/test_estimate_io.py -v`
Expected: PASS (new + existing adapter tests).

- [ ] **Step 5: Commit**

```bash
git add app/gui/estimate_io.py tests/gui/test_estimate_io.py
git commit -m "feat(cost): GUI adapter builds projection + one-time bundle"
```

---

### Task 3: Carry the bundle through the routes

**Files:**
- Modify: `app/gui/routes.py` (`_compute` returns prices; `/estimate.json` adds `projection`; `/estimate` passes `bundle` to the template)
- Test: `tests/gui/test_estimate_routes.py`

**Interfaces:**
- Consumes: `estimate_io.projection_bundle`; existing `_compute`, `load_prices`, `asdict`.
- Produces: `/estimate.json` returns `{**asdict(est), "projection": <bundle>}`; `/estimate` template context gains `bundle=<projection_bundle or None>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/gui/test_estimate_routes.py` (uses existing `client`/`app` fixtures over `[VJOB, AJOB]`):

```python
def test_estimate_json_includes_projection(client):
    j = client.get("/estimate.json").get_json()
    assert "projection" in j
    p = j["projection"]
    assert len(p["primary"]["months"]) == 24
    assert set(p["comparison"]) == {"no_versioning", "rolling_30"}
    assert "onetime" in p and "first_month" in p["onetime"]

def test_estimate_json_invalid_input_still_400(client):
    r = client.get("/estimate.json?appdata_size_gb=notanumber")
    assert r.status_code == 400
    assert "error" in r.get_json()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/gui/test_estimate_routes.py -k "projection or invalid_input_still" -v`
Expected: FAIL — `KeyError: 'projection'` on the first (400 test passes already; it guards the regression).

- [ ] **Step 3: Implement in `app/gui/routes.py`**

Change `_compute` to also return the loaded prices:

```python
def _compute(cfg, args):
    scenario = estimate_io.scenario_from_params(
        args, cfg["CONFIG_DIR"], cfg["SOURCE_ROOT"],
        usage=(usage.load_cached(cfg["CACHE_DIR"]) or {}).get("data"))
    prices = load_prices(scenario.region, cache_dir=cfg["CACHE_DIR"], live=cfg["PRICES_LIVE"])
    return scenario, estimate(scenario, prices), prices
```

> NOTE while implementing: open `_compute` first and keep its EXISTING body verbatim — only append `, prices` to the return and rename the local if needed. The `usage=` argument above mirrors whatever the current body passes; do not change that behavior.

Update `estimate_json`:

```python
@bp.get("/estimate.json")
def estimate_json():
    cfg = current_app.config
    try:
        scn, est, prices = _compute(cfg, request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    bundle = estimate_io.projection_bundle(scn, prices)
    return jsonify({**asdict(est), "projection": bundle})
```

Update `estimate_page`'s try-block to capture prices and build the bundle for the template:

```python
    est = None
    bundle = None
    error = None
    try:
        scn, est, prices_wf = _compute(cfg, request.args)
        bundle = estimate_io.projection_bundle(scn, prices_wf)
    except ValueError as e:
        error = str(e)
```

and add `bundle=bundle,` to the `render_template("estimate.html", …)` kwargs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/gui/test_estimate_routes.py -v`
Expected: PASS (new + existing route tests, including the JSON-keyed-by-job-names test).

- [ ] **Step 5: Commit**

```bash
git add app/gui/routes.py tests/gui/test_estimate_routes.py
git commit -m "feat(cost): serve projection bundle from /estimate and /estimate.json"
```

---

### Task 4: Template — headline, Starting-out card, milestone table, chart mount

**Files:**
- Modify: `app/gui/templates/estimate.html`
- Test: `tests/gui/test_estimate_routes.py`

**Interfaces:**
- Consumes: `bundle` context var (may be `None` on invalid input or no prices — guard with `{% if bundle %}`).
- Produces (DOM contract for Task 5's JS): `<script type="application/json" id="proj-data">` holding `bundle|tojson`; an `<svg id="cost-timeline">` mount; milestone table `id="cost-milestones"`; Starting-out card with `data-est="first_month"` etc. for live updates.

- [ ] **Step 1: Write the failing test**

Append to `tests/gui/test_estimate_routes.py`:

```python
def test_estimate_page_has_timeline_card_and_data(client):
    body = client.get("/estimate").get_data(as_text=True)
    assert 'id="cost-timeline"' in body        # svg chart mount
    assert 'id="proj-data"' in body            # embedded projection JSON for the JS
    assert 'id="cost-milestones"' in body      # no-JS milestone table
    assert "Starting out" in body              # one-time card heading
    assert "First bill" in body                # month-1 headline figure
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gui/test_estimate_routes.py::test_estimate_page_has_timeline_card_and_data -v`
Expected: FAIL — none of those strings present yet.

- [ ] **Step 3: Edit `app/gui/templates/estimate.html`**

3a. In the `card-hero` totals `<ul class="totals">`, add two `<li>` after the existing three:

```html
    <li class="med"><span class="lbl">First bill</span><span data-est-proj="first_month" data-fmt="money">{{ money(bundle.onetime.first_month if bundle else none) }}</span></li>
    <li class="med"><span class="lbl">Steady-state / mo</span><span data-est-proj="steady_state_monthly" data-fmt="money">{{ money(bundle.primary.steady_state_monthly if bundle else none) }}</span></li>
```

3b. Immediately AFTER the `card-hero` div, add the Starting-out card:

```html
<div class="card">
  <h2>Starting out <small>— one-time &amp; first-month</small></h2>
  <ul class="totals">
    <li><span class="lbl">Upload requests (once)</span><span data-est-proj="upload" data-fmt="money">{{ money(bundle.onetime.upload if bundle else none) }}</span></li>
    <li><span class="lbl">First monthly bill</span><span data-est-proj="first_month" data-fmt="money">{{ money(bundle.onetime.first_month if bundle else none) }}</span></li>
  </ul>
  <p class="hint">The upload charge is the per-request cost to push every file up once — it's per-request, not per-GB, so it's usually pennies. Your first bill jumps straight to storing the whole dataset; the monthly cost then climbs as old versions pile up (see the timeline) until it plateaus.</p>
  {% if bundle and bundle.onetime.lockin %}
  <h3>Cold-storage lock-in</h3>
  <p class="hint">A cold class bills a <em>minimum storage duration</em> — even if you deleted this data the day after uploading, you'd still pay for the full minimum:</p>
  <ul class="totals" id="cost-lockin">
    {% for row in bundle.onetime.lockin %}
    <li><span class="lbl">{{ row.job }} <small>{{ row.storage_class }}</small></span><span>{{ money(row.amount) }}</span></li>
    {% endfor %}
  </ul>
  {% endif %}
</div>
```

3c. After the Starting-out card, add the timeline card (SVG is drawn by JS; the milestone table is the no-JS fallback). Embed the bundle as JSON for the JS:

```html
<div class="card">
  <h2>Cost over time <small>— {{ bundle.primary.months|length if bundle else 24 }} months</small></h2>
  <p class="hint">Monthly bill as versioned history accumulates, then plateaus at the steady state. Two comparison curves show the effect of your retention choice.</p>
  <div class="table-scroll">
    <svg id="cost-timeline" viewBox="0 0 720 260" role="img" aria-label="Projected monthly cost over time" style="width:100%;max-width:720px;height:auto"></svg>
  </div>
  <ul class="chart-legend" id="cost-legend">
    <li><span class="swatch swatch-primary"></span>your config</li>
    <li><span class="swatch swatch-nover"></span>no versioning</li>
    <li><span class="swatch swatch-roll"></span>rolling 30-day</li>
  </ul>
  {% if bundle %}<script type="application/json" id="proj-data">{{ bundle|tojson }}</script>{% endif %}

  <div class="table-scroll">
  <table id="cost-milestones">
    <thead><tr><th>Component</th>{% set idx = [0, 5, 11, 23] %}{% for i in idx %}<th class="num">M{{ i + 1 }}</th>{% endfor %}<th class="num">steady</th></tr></thead>
    <tbody>
      {%- set rows = [('storage','Storage'),('versioning','Versioning'),('ingest','Ingest'),('rotation','Rotation'),('onetime','One-time'),('total','Total')] -%}
      {% if bundle %}
      {% set m = bundle.primary.months %}
      {% set steady_i = bundle.steady_state_month - 1 %}
      {% for key, label in rows %}
      <tr><th>{{ label }}</th>
        {% for i in [0, 5, 11, 23] %}<td class="num">{{ money(m[i][key]) if i < m|length else '—' }}</td>{% endfor %}
        <td class="num">{{ money(m[steady_i][key]) }}</td>
      </tr>
      {% endfor %}
      {% else %}
      <tr><td colspan="6" class="muted">Enter valid inputs to see the projection.</td></tr>
      {% endif %}
    </tbody>
  </table>
  </div>
</div>
```

3d. Add minimal legend/swatch styling to `app/gui/static/style.css` (reuse existing color vars; pick three that exist in the palette — check `style.css` for the accent variable names and substitute):

```css
.chart-legend{list-style:none;display:flex;gap:1rem;flex-wrap:wrap;padding:0;margin:.5rem 0 0;font-size:.85rem}
.chart-legend .swatch{display:inline-block;width:.75rem;height:.75rem;border-radius:2px;margin-right:.35rem;vertical-align:middle}
.swatch-primary{background:var(--accent, #4ea1ff)}
.swatch-nover{background:var(--muted, #8a94a6)}
.swatch-roll{background:var(--ok, #4ecb8d)}
#cost-timeline .grid{stroke:var(--border, #2a2f3a);stroke-width:1}
#cost-timeline .axis{fill:var(--muted, #8a94a6);font-size:11px}
```

> While implementing 3d: open `style.css`, find the real variable names for accent/border/muted/positive colors and replace the fallbacks so the chart matches the design system exactly.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/gui/test_estimate_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/gui/templates/estimate.html app/gui/static/style.css tests/gui/test_estimate_routes.py
git commit -m "feat(cost): Starting-out card, milestone table, and chart mount"
```

---

### Task 5: Draw + live-update the SVG chart (`app.js`)

**Files:**
- Modify: `app/gui/static/app.js` (add `drawCostChart`; call from the estimate IIFE's `paint`, and once on load)

**Interfaces:**
- Consumes: `#proj-data` JSON on load; `data.projection` from `/estimate.json` on live update; `#cost-timeline` svg; `[data-est-proj]` cells.
- Produces: rendered polylines + axes in `#cost-timeline`; updated headline/starting-out cells.

- [ ] **Step 1: Implement `drawCostChart` in `app/gui/static/app.js`**

Inside the estimate IIFE (the one that defines `paint`), add a renderer and extend `paint`. Vanilla, no deps, ES5-style to match the file:

```js
  function svgEl(name, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function polyline(months, cls, x, y) {
    var pts = months.map(function (m, i) { return x(i) + "," + y(m.total); }).join(" ");
    var p = svgEl("polyline", { points: pts, fill: "none", "stroke-width": 2 });
    p.setAttribute("class", cls);
    // stroke via CSS class swatches would need fill->stroke; set stroke explicitly:
    return p;
  }
  function drawCostChart(proj) {
    var svg = document.getElementById("cost-timeline");
    if (!svg || !proj || !proj.primary) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var W = 720, H = 260, padL = 52, padB = 26, padT = 10, padR = 10;
    var prim = proj.primary.months;
    var all = [prim, proj.comparison.no_versioning.months, proj.comparison.rolling_30.months];
    var maxY = 0, n = prim.length;
    all.forEach(function (ms) { ms.forEach(function (m) { if (m.total > maxY) maxY = m.total; }); });
    if (maxY <= 0) maxY = 1;
    function x(i) { return padL + (W - padL - padR) * (n <= 1 ? 0 : i / (n - 1)); }
    function y(v) { return padT + (H - padT - padB) * (1 - v / maxY); }
    // axes
    svg.appendChild(svgEl("line", { x1: padL, y1: y(0), x2: W - padR, y2: y(0), class: "grid" }));
    svg.appendChild(svgEl("line", { x1: padL, y1: padT, x2: padL, y2: y(0), class: "grid" }));
    // steady-state marker
    var sx = x((proj.steady_state_month || 1) - 1);
    svg.appendChild(svgEl("line", { x1: sx, y1: padT, x2: sx, y2: y(0), class: "grid", "stroke-dasharray": "4 3" }));
    // curves: color by CSS var via inline stroke read from swatch elements
    function stroke(sel) {
      var el = document.querySelector(sel);
      return el ? getComputedStyle(el).backgroundColor : "#4ea1ff";
    }
    var series = [
      { ms: proj.comparison.no_versioning.months, c: stroke(".swatch-nover") },
      { ms: proj.comparison.rolling_30.months, c: stroke(".swatch-roll") },
      { ms: prim, c: stroke(".swatch-primary") }
    ];
    series.forEach(function (s) {
      var pl = polyline(s.ms, "", x, y);
      pl.setAttribute("stroke", s.c);
      svg.appendChild(pl);
    });
    // y labels (0 and max) + x labels (M1, steady, Mlast)
    [{ v: 0 }, { v: maxY }].forEach(function (t) {
      var lab = svgEl("text", { x: padL - 6, y: y(t.v) + 3, "text-anchor": "end", class: "axis" });
      lab.textContent = "$" + t.v.toFixed(t.v >= 10 ? 0 : 2);
      svg.appendChild(lab);
    });
    [[0, "M1"], [(proj.steady_state_month || 1) - 1, "steady"], [n - 1, "M" + n]].forEach(function (pair) {
      var lab = svgEl("text", { x: x(pair[0]), y: H - 8, "text-anchor": "middle", class: "axis" });
      lab.textContent = pair[1];
      svg.appendChild(lab);
    });
  }
```

Then extend `paint(data)` (append at its end) to update the projection cells and redraw:

```js
    // Projection headline / starting-out cells + the chart.
    var proj = data && data.projection;
    var pcells = document.querySelectorAll("[data-est-proj]");
    for (var q = 0; q < pcells.length; q++) {
      var pk = pcells[q].getAttribute("data-est-proj");
      var pv;
      if (!proj) pv = undefined;
      else if (pk === "steady_state_monthly") pv = proj.primary.steady_state_monthly;
      else if (pk === "first_month" || pk === "upload") pv = proj.onetime[pk];
      else pv = undefined;
      pcells[q].textContent = fmt(pv, pcells[q].getAttribute("data-fmt"));
    }
    if (proj) drawCostChart(proj);
```

And draw once on initial load from the embedded JSON (place just before the IIFE's closing `})();`, after the `form.addEventListener` lines):

```js
  (function initChart() {
    var tag = document.getElementById("proj-data");
    if (!tag) return;
    try { drawCostChart(JSON.parse(tag.textContent)); } catch (e) {}
  })();
```

- [ ] **Step 2: Verify the full test suite still passes (no JS harness in repo)**

Run: `pytest tests/gui -v`
Expected: PASS (JS is not unit-tested here; this confirms no template/route regressions).

- [ ] **Step 3: Manual smoke check**

Run the app locally (see the `run` skill / README) with at least one versioned job, open `/estimate`, and confirm: three curves render, the primary ramps then plateaus at the dashed steady-state line, and editing "Change rate (%)" or "S3 old-version days" redraws the chart and updates "First bill" / "Steady-state / mo". With JS disabled, the milestone table still shows numbers.

- [ ] **Step 4: Commit**

```bash
git add app/gui/static/app.js
git commit -m "feat(cost): draw live SVG cost-over-time chart with comparison curves"
```

---

### Task 6: CLI mirrors the projection

**Files:**
- Modify: `app/estimator/cli.py` (`--json` gains `projection`; table gains a compact over-time block)
- Test: `tests/estimator/test_cli.py`

**Interfaces:**
- Consumes: `project` from `.model`; existing `build_scenario`, `load_prices`, `estimate`, `render_table`.
- Produces: `--json` output dict gains a top-level `"projection"` key (Projection asdict); human table gains lines for month 1, month 12, and steady state.

- [ ] **Step 1: Write the failing tests**

Append to `tests/estimator/test_cli.py` (match its existing invocation style — inspect the top of the file for how it seeds a config dir and calls `main`/`render_table`; reuse that helper):

```python
def test_cli_json_includes_projection(tmp_path, capsys):
    # Reuse whatever the file's existing helper is to seed a jobs.json config dir,
    # then run main(["--config-dir", cfg, "--json"]) and parse stdout.
    cfg = _seed(tmp_path)  # existing helper in this test module
    from app.estimator import cli
    assert cli.main(["--config-dir", cfg, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "projection" in out
    assert len(out["projection"]["months"]) == 24

def test_cli_table_has_over_time_block(tmp_path, capsys):
    cfg = _seed(tmp_path)
    from app.estimator import cli
    assert cli.main(["--config-dir", cfg]) == 0
    out = capsys.readouterr().out
    assert "OVER TIME" in out and "steady" in out.lower()
```

> If `test_cli.py` has no reusable seed helper, add a small local one mirroring `tests/gui/test_estimate_io.py::_cfg` (write `{"jobs": [...]}` to `<cfg>/jobs.json`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/estimator/test_cli.py -k "projection or over_time" -v`
Expected: FAIL — no `projection` key / no "OVER TIME" text.

- [ ] **Step 3: Implement in `app/estimator/cli.py`**

Import `project` and `Projection`:

```python
from .model import Scenario, Estimate, estimate, project
```

In `main`, after `est = estimate(scenario, prices)` compute the projection and thread it through:

```python
        est = estimate(scenario, prices)
        proj = project(scenario, prices)
```

Extend the JSON branch and the table call:

```python
    if args.json:
        payload = estimate_to_dict(est)
        payload["projection"] = asdict(proj)
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(est, proj))
    return 0
```

Update `render_table` to accept the projection and append a compact block:

```python
def render_table(est: Estimate, proj=None) -> str:
    lines = [f"S3 backup cost estimate  (prices: {est.region} @ {est.price_date} — {est.price_source})", ""]
    # ... existing per-job + totals lines unchanged ...
    if proj is not None and proj.months:
        m1, m12 = proj.months[0], proj.months[min(11, len(proj.months) - 1)]
        steady = proj.months[min(proj.steady_state_month - 1, len(proj.months) - 1)]
        lines += [
            "", "OVER TIME (monthly bill)",
            f"  month 1             ${m1.total:,.2f}",
            f"  month 12            ${m12.total:,.2f}",
            f"  steady (mo {proj.steady_state_month:>2})       ${steady.total:,.2f}",
        ]
    return "\n".join(lines)
```

(Keep the existing per-job/total lines exactly; only add the trailing block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/estimator/test_cli.py -v`
Expected: PASS (including existing CLI tests — the `render_table` signature change is backward compatible via `proj=None`).

- [ ] **Step 5: Commit**

```bash
git add app/estimator/cli.py tests/estimator/test_cli.py
git commit -m "feat(cost): CLI mirrors the projection (--json + table over-time block)"
```

---

### Task 7: README — record Approach ② as a TODO

**Files:**
- Modify: `README.md` (add a Roadmap/TODO entry)

- [ ] **Step 1: Add the TODO**

Find the README's roadmap/TODO section (or add a `## Roadmap` section near the end if none exists) and add:

```markdown
- **Cost scenario workbench** — define, name, and save multiple custom cost scenarios (per-scenario storage class, retention, versioning) and compare N curves on the cost-over-time timeline. Builds on the `project()` model function and comparison-curve mechanism.
```

- [ ] **Step 2: Verify it reads correctly**

Run: `git diff README.md`
Expected: the new bullet under a roadmap/TODO heading, well-formed markdown.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(cost): add scenario-workbench (Approach 2) to roadmap TODO"
```

---

## Self-Review

**Spec coverage:**
- §4 projection model → Task 1. §5 one-time breakdown → `cold_lockin_onetime` (Task 1) + `projection_bundle.onetime` (Task 2) + Starting-out card (Task 4). §6 comparison curves → Task 2 + drawn in Task 5. §7 data flow → Tasks 2–3 (adapter/routes) + Task 5 (JS). §8 layout → Task 4. §9 CLI → Task 6. §12 README TODO → Task 7. All covered.

**Placeholder scan:** No TBD/TODO in code steps; the only "TODO" is the literal README roadmap content (Task 7, intended). Test-seed helper references in Task 6 point at the file's existing helper with a concrete fallback. No "add error handling" hand-waves — the div-by-zero guard is explicit (`_versioning_fill`), invalid-input 400 is a real test.

**Type consistency:** `project(scenario, prices, months=24) -> Projection`; `Projection.months: list[MonthPoint]`, `.steady_state_month`, `.steady_state_monthly` used identically in Tasks 1/2/3/6. `projection_bundle` shape used by Task 2 tests matches Task 3 (`/estimate.json`) and Task 4 (`bundle.primary.months`, `bundle.onetime.first_month/upload/lockin`, `bundle.steady_state_month`). `job_retention_days` used by model (Task 1) and adapter (Task 2). `render_table(est, proj=None)` back-compatible. DOM ids (`cost-timeline`, `proj-data`, `cost-milestones`, `data-est-proj`) match between Task 4 (template) and Task 5 (JS).

One refinement vs. spec §7/§8: the SVG itself is drawn by JS (not server-side); the server-rendered **milestone table** is the no-JS fallback (numbers, not the graphic). This keeps a single chart renderer (DRY) while remaining usable without JS.
