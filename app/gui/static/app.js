// app/gui/static/app.js — poll the log tail
(function () {
  var el = document.getElementById("log");
  if (!el) return;
  function refresh() {
    fetch("/logs?tail=200").then(function (r) { return r.text(); })
      .then(function (t) { el.textContent = t || "(log empty)"; el.scrollTop = el.scrollHeight; })
      .catch(function () {});
  }
  refresh();
  setInterval(refresh, 5000);
})();

// Job source: one confined folder tree over SOURCE_ROOT (#source-tree), single-
// select — a job has exactly one source. Checking a folder writes its path into
// #source-input (the posted name="source" field) and #source-shown, and
// unchecks any previously-checked node. Hydrates lazily from jobs/browse.
(function () {
  var root = document.getElementById("source-tree");
  if (!root) return;
  var sourceInput = document.getElementById("source-input");
  var sourceShown = document.getElementById("source-shown");
  var selected = root.dataset.selected || "";
  var checkboxes = [];
  function browse(path) {
    return fetch("/jobs/browse?path=" + encodeURIComponent(path))
      .then(function (r) { return r.ok ? r.json() : { entries: [] }; })
      .catch(function () { return { entries: [] }; });
  }
  function select(cb) {
    checkboxes.forEach(function (other) { if (other !== cb) other.checked = false; });
    if (sourceInput) sourceInput.value = cb.checked ? cb.value : "";
    if (sourceShown) sourceShown.textContent = cb.checked ? cb.value : "(none)";
  }
  function node(entry) {
    var li = document.createElement("li");
    var toggle = document.createElement("button");
    toggle.type = "button"; toggle.textContent = "▸"; toggle.className = "expand";
    var label = document.createElement("label");
    var cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = entry.path;
    checkboxes.push(cb);
    if (entry.path === selected) cb.checked = true;
    cb.addEventListener("change", function () { select(cb); });
    label.appendChild(cb); label.appendChild(document.createTextNode(" " + entry.name));
    var kids = document.createElement("ul"); kids.className = "tree"; kids.hidden = true;
    var loaded = false;
    toggle.addEventListener("click", function () {
      kids.hidden = !kids.hidden;
      toggle.textContent = kids.hidden ? "▸" : "▾";
      if (!loaded && !kids.hidden) {
        loaded = true;
        browse(entry.path).then(function (d) {
          d.entries.forEach(function (e) { kids.appendChild(node(e)); });
        });
      }
    });
    li.appendChild(toggle); li.appendChild(label); li.appendChild(kids);
    return li;
  }
  browse("").then(function (d) {
    d.entries.forEach(function (e) { root.appendChild(node(e)); });
  });
})();

// Friendly schedule builder: drives the real name="schedule" field (#sched-input).
(function () {
  var input = document.getElementById("sched-input");
  var builder = document.getElementById("sched-builder");
  if (!input || !builder) return;
  var freq = document.getElementById("sched-freq");
  var time = document.getElementById("sched-time");
  var dow = document.getElementById("sched-dow");
  var dom = document.getElementById("sched-dom");
  var dowWrap = document.getElementById("sched-dow-wrap");
  var domWrap = document.getElementById("sched-dom-wrap");
  var human = document.getElementById("sched-human");
  var preview = document.getElementById("sched-preview");
  var rawBtn = document.getElementById("sched-advanced-toggle");
  var simpleBtn = document.getElementById("sched-simple-toggle");
  var raw = document.getElementById("sched-raw");
  var DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  function two(n) { return (n < 10 ? "0" : "") + n; }
  function build() {
    var hm = (time.value || "03:00").split(":");  // native time input -> "HH:MM"
    var mm = parseInt(hm[1], 10) || 0;
    var hh = parseInt(hm[0], 10) || 0;
    dowWrap.hidden = freq.value !== "weekly";
    domWrap.hidden = freq.value !== "monthly";
    var cron, txt;
    if (freq.value === "hourly") { cron = mm + " * * * *"; txt = "hourly at :" + two(mm); }
    else if (freq.value === "daily") { cron = mm + " " + hh + " * * *"; txt = "daily at " + two(hh) + ":" + two(mm); }
    else if (freq.value === "weekly") { cron = mm + " " + hh + " * * " + dow.value; txt = "every " + DOW[parseInt(dow.value, 10)] + " at " + two(hh) + ":" + two(mm); }
    else { var d = Math.min(28, Math.max(1, parseInt(dom.value, 10) || 1)); cron = mm + " " + hh + " " + d + " * *"; txt = "day " + d + " at " + two(hh) + ":" + two(mm); }
    preview.textContent = cron; human.textContent = txt;
    input.value = cron;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function parse(cron) {
    var f = (cron || "").split(/\s+/);
    if (f.length !== 5) return false;
    var mm = f[0], hh = f[1], d = f[2], mon = f[3], w = f[4];
    // Range-check minute/hour so an out-of-range value (e.g. "0 24 * * *", savable
    // via the unrestricted Advanced field) does NOT match here -- otherwise the
    // native time input would blank "24:00" and build() would silently rewrite
    // the schedule to the 03:00 default on load. Out-of-range stays in Advanced.
    if (!/^\d+$/.test(mm) || +mm > 59 || mon !== "*") return false;
    if (hh === "*" && d === "*" && w === "*") { freq.value = "hourly"; time.value = "00:" + two(+mm); return true; }
    if (!/^\d+$/.test(hh) || +hh > 23) return false;
    time.value = two(+hh) + ":" + two(+mm);
    if (d === "*" && w === "*") { freq.value = "daily"; return true; }
    if (d === "*" && /^[0-6]$/.test(w)) { freq.value = "weekly"; dow.value = w; return true; }
    if (/^([1-9]|1\d|2[0-8])$/.test(d) && w === "*") { freq.value = "monthly"; dom.value = d; return true; }
    return false;
  }
  function showAdvanced(on) { builder.hidden = on; raw.style.display = on ? "" : "none"; if (simpleBtn) simpleBtn.hidden = !on; }

  if (parse(input.value)) { showAdvanced(false); build(); }
  else { showAdvanced(true); }  // unparseable -> keep the raw field visible

  freq.addEventListener("change", build);
  time.addEventListener("input", build);
  dow.addEventListener("change", build);
  dom.addEventListener("input", build);
  if (rawBtn) rawBtn.addEventListener("click", function () { showAdvanced(true); });
  if (simpleBtn) simpleBtn.addEventListener("click", function () { if (parse(input.value)) { showAdvanced(false); build(); } });
})();

// Cost estimate: recompute live as inputs change (server owns the cost model).
(function () {
  var form = document.getElementById("est-form");
  if (!form) return;
  var errEl = document.getElementById("est-error");
  var timer;
  function fmt(v, kind) {
    if (v === null || v === undefined) return "—";
    var n = Number(v);
    if (kind === "money") return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (kind === "num") return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (kind === "int") return n.toLocaleString();
    return String(v);
  }
  function svgEl(name, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (var k in attrs) { if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]); }
    return e;
  }
  function strokeColor(sel, fallback) {
    var el = document.querySelector(sel);
    return el ? getComputedStyle(el).backgroundColor : fallback;
  }
  // Draw the cost-over-time curves into #cost-timeline from a /estimate.json
  // "projection" bundle (or the SSR'd #proj-data on load). No external library.
  function drawCostChart(proj) {
    var svg = document.getElementById("cost-timeline");
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!proj || !proj.primary) return;  // invalid input -> leave it blank
    var W = 720, H = 260, padL = 52, padB = 26, padT = 10, padR = 10;
    var prim = proj.primary.months, n = prim.length;
    var curves = [
      { ms: proj.comparison.no_versioning.months, c: strokeColor(".swatch-nover", "#8a94a6") },
      { ms: proj.comparison.rolling_30.months, c: strokeColor(".swatch-roll", "#4ecb8d") },
      { ms: prim, c: strokeColor(".swatch-primary", "#4ea1ff") }
    ];
    var maxY = 0;
    curves.forEach(function (s) { s.ms.forEach(function (m) { if (m.total > maxY) maxY = m.total; }); });
    if (maxY <= 0) maxY = 1;
    function x(i) { return padL + (W - padL - padR) * (n <= 1 ? 0 : i / (n - 1)); }
    function y(v) { return padT + (H - padT - padB) * (1 - v / maxY); }
    svg.appendChild(svgEl("line", { x1: padL, y1: y(0), x2: W - padR, y2: y(0), "class": "grid" }));
    svg.appendChild(svgEl("line", { x1: padL, y1: padT, x2: padL, y2: y(0), "class": "grid" }));
    var sIdx = (proj.steady_state_month || 1) - 1, sx = x(sIdx);
    svg.appendChild(svgEl("line", { x1: sx, y1: padT, x2: sx, y2: y(0), "class": "grid", "stroke-dasharray": "4 3" }));
    curves.forEach(function (s) {
      var pts = s.ms.map(function (m, i) { return x(i) + "," + y(m.total); }).join(" ");
      svg.appendChild(svgEl("polyline", { points: pts, fill: "none", stroke: s.c, "stroke-width": 2 }));
    });
    [0, maxY].forEach(function (v) {
      var t = svgEl("text", { x: padL - 6, y: y(v) + 3, "text-anchor": "end", "class": "axis" });
      t.textContent = "$" + v.toFixed(v >= 10 ? 0 : 2);
      svg.appendChild(t);
    });
    [[0, "M1"], [sIdx, "steady"], [n - 1, "M" + n]].forEach(function (pair) {
      var t = svgEl("text", { x: x(pair[0]), y: H - 8, "text-anchor": "middle", "class": "axis" });
      t.textContent = pair[1];
      svg.appendChild(t);
    });
  }
  function paint(data) {
    // Scalar top-level fields (monthly_total, region, …): a direct key lookup.
    var cells = document.querySelectorAll("[data-est]");
    for (var i = 0; i < cells.length; i++) {
      var key = cells[i].getAttribute("data-est");
      cells[i].textContent = fmt(data == null ? undefined : data[key], cells[i].getAttribute("data-fmt"));
    }
    // Per-job breakdown cells: data.jobs[<name>][<field>] — a DIRECT object lookup
    // by the job name, never a split on ".", since job names may contain dots.
    var jobs = (data && data.jobs) || {};
    var jobCells = document.querySelectorAll("[data-job]");
    for (var k = 0; k < jobCells.length; k++) {
      var el = jobCells[k];
      var li = jobs[el.getAttribute("data-job")];
      var v = li ? li[el.getAttribute("data-field")] : undefined;
      el.textContent = fmt(v, el.getAttribute("data-fmt"));
    }
    // Projection headline / starting-out cells + the chart.
    var proj = data && data.projection;
    var pcells = document.querySelectorAll("[data-est-proj]");
    for (var q = 0; q < pcells.length; q++) {
      var pk = pcells[q].getAttribute("data-est-proj"), pv;
      if (!proj) pv = undefined;
      else if (pk === "steady_state_monthly") pv = proj.primary.steady_state_monthly;
      else pv = proj.onetime[pk];  // first_month, upload
      pcells[q].textContent = fmt(pv, pcells[q].getAttribute("data-fmt"));
    }
    drawCostChart(proj);
  }
  function update() {
    var qs = new URLSearchParams(new FormData(form)).toString();
    fetch("/estimate.json?" + qs)
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.ok) {
          if (errEl) { errEl.hidden = true; errEl.textContent = ""; }
          paint(res.j);
        } else {
          if (errEl) { errEl.hidden = false; errEl.textContent = res.j.error || "invalid input"; }
          paint({});
        }
      })
      .catch(function () {});
  }
  form.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(update, 250); });
  form.addEventListener("change", function () { clearTimeout(timer); timer = setTimeout(update, 250); });
  // Initial draw from the server-embedded projection (page loads with the SVG empty).
  (function initChart() {
    var tag = document.getElementById("proj-data");
    if (!tag) return;
    try { drawCostChart(JSON.parse(tag.textContent)); } catch (e) {}
  })();
})();

// Wizard live cost (job_form.html): recompute "this job" + "new total" as the
// create/edit form changes, and look up a picked source folder's real size so an
// un-backed-up job's estimate reflects it. Guarded by the wizard's cost card so
// this never runs on other pages; does not touch the source-tree IIFE above or
// the /estimate page's own IIFE below — absolute paths since this template is
// served from both /jobs/new and /jobs/<name>/edit (different path depths).
(function () {
  var card = document.getElementById("job-cost");
  var form = document.getElementById("job-form");
  if (!card || !form) return;
  var thisEl = document.getElementById("job-cost-this");
  var totalEl = document.getElementById("job-cost-total");
  var dateEl = document.getElementById("job-cost-date");
  var errEl = document.getElementById("job-cost-error");
  var sizeInput = document.getElementById("size-gb-input");
  var sourceTree = document.getElementById("source-tree");
  var sizingEl = document.getElementById("job-cost-sizing");
  var restoreEl = document.getElementById("job-cost-restore");
  var adviceEl = document.getElementById("job-advice");
  var infoEl = document.getElementById("source-info");
  var firstEl = document.getElementById("job-cost-first");
  var breakdownEl = document.getElementById("job-cost-breakdown");
  var futureEl = document.getElementById("job-cost-future");
  var milestonesEl = document.getElementById("job-cost-milestones");
  var guidanceEl = document.getElementById("job-guidance");
  var timer;

  function sizing(on) { if (sizingEl) sizingEl.hidden = !on; }
  function fmtBytes(b) {
    var gb = b / (1024 * 1024 * 1024);
    if (gb >= 1) return gb.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " GB";
    var mb = b / (1024 * 1024);
    if (mb >= 1) return mb.toLocaleString(undefined, { maximumFractionDigits: 1 }) + " MB";
    return Math.max(0, Math.round(b / 1024)).toLocaleString() + " KB";
  }
  function showInfo(d) {
    if (!infoEl) return;
    if (!d) { infoEl.hidden = true; infoEl.textContent = ""; return; }
    if (d.capped && !d.bytes) {
      infoEl.textContent = "Couldn't finish measuring this folder — it may be extremely large or unreadable.";
      infoEl.hidden = false;
      return;
    }
    var msg = "This folder holds " + fmtBytes(Number(d.bytes || 0));
    if (Number(d.count || 0) > 0) msg += " across " + Number(d.count).toLocaleString() + " files";
    if (d.capped) msg += " (measurement may be incomplete)";
    infoEl.textContent = msg + ".";
    infoEl.hidden = false;
  }

  function money(v) {
    if (v === null || v === undefined) return "—";
    return "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function paintAdvice(list) {
    if (!adviceEl) return;
    while (adviceEl.firstChild) adviceEl.removeChild(adviceEl.firstChild);
    if (!list || !list.length) { adviceEl.hidden = true; return; }
    for (var i = 0; i < list.length; i++) {
      var item = document.createElement("p");
      item.className = "advice-item advice-" + (list[i].level || "info");
      item.textContent = list[i].text;
      adviceEl.appendChild(item);
    }
    adviceEl.hidden = false;
  }
  function fmtGb(gb) { return Number(gb).toLocaleString(undefined, { maximumFractionDigits: 2 }) + " GB"; }
  function paintBreakdown(b) {
    if (!breakdownEl) return;
    if (!b) { breakdownEl.hidden = true; breakdownEl.textContent = ""; return; }
    var parts = ["Storage " + fmtGb(b.billed_gb) + ": " + money(b.storage) + "/mo"];
    if (b.versioning > 0) {
      var window = (b.retention_days != null) ? b.retention_days + "d, " : "";
      parts.push("old versions (" + window + "~" + b.change_rate_pct + "% churn): " + money(b.versioning) + "/mo");
    }
    if (b.rotation > 0) parts.push("early-deletion: " + money(b.rotation) + "/mo");
    var onetime = (b.upload_onetime || 0) + (b.lockin_onetime || 0);
    if (onetime > 0) parts.push("one-time: " + money(onetime));
    breakdownEl.textContent = parts.join(" · ");
    breakdownEl.hidden = false;
  }
  // Always shows something (unlike the old ramp-only line): a flat job gets a
  // "steady, no increase" summary; a ramping job gets start → rise → steady plus
  // the monthly milestones. Both surface the cumulative next-6-months figure.
  function paintFuture(p) {
    if (!futureEl) return;
    if (!p) {
      futureEl.hidden = true; futureEl.textContent = "";
      if (milestonesEl) { milestonesEl.hidden = true; milestonesEl.textContent = ""; }
      return;
    }
    var first = p.first_bill || 0, steady = p.steady_monthly || 0;
    var sixmo = (p.total_6mo != null) ? money(p.total_6mo) : "—";
    if (p.unbounded) {
      // keep_all ("Keep everything"): never settles — show the growth, not a plateau.
      futureEl.textContent = "Keeps growing — “Keep everything” has no steady state: " +
        "versions pile up to ~" + money(p.at_12) + "/mo by year 1, ~" + money(p.at_24) +
        "/mo by year 2, and rising. About " + sixmo + " total over the next 6 months.";
      futureEl.hidden = false;
      if (milestonesEl) {
        milestonesEl.textContent = "Monthly bill — M1 " + money(first) + " · M6 " + money(p.at_6) +
          " · M12 " + money(p.at_12) + " · M24 " + money(p.at_24) + " (climbing).";
        milestonesEl.hidden = false;
      }
      return;
    }
    var flat = Math.abs(first - steady) < 0.005;
    if (flat) {
      futureEl.textContent = "≈ " + money(steady) + "/mo, steady — no monthly increase. " +
        "About " + sixmo + " total over the next 6 months.";
    } else {
      futureEl.textContent = "Starts " + money(first) + "/mo and rises " + money(steady - first) +
        " to " + money(steady) + "/mo by month " + p.steady_month + ". " +
        "About " + sixmo + " total over the next 6 months.";
    }
    futureEl.hidden = false;
    if (milestonesEl) {
      // Always show the monthly milestones — for a flat job the equal numbers make
      // "it doesn't grow" concrete rather than leaving the trajectory blank.
      milestonesEl.textContent = "Monthly bill — M1 " + money(first) + " · M6 " + money(p.at_6) +
        " · M12 " + money(p.at_12) + " · M24 " + money(p.at_24) + ".";
      milestonesEl.hidden = false;
    }
  }
  // Reactive "which type / class, and why" — updates through the same live
  // estimate round-trip whenever the type or storage-class selection changes.
  function paintGuidance(g) {
    if (!guidanceEl) return;
    while (guidanceEl.firstChild) guidanceEl.removeChild(guidanceEl.firstChild);
    if (!g) { guidanceEl.hidden = true; return; }
    var when = document.createElement("p");
    when.className = "advice-item advice-info";
    when.textContent = g.type_label + " — " + g.type_when;
    guidanceEl.appendChild(when);
    var cls = document.createElement("p");
    if (g.on_recommended === true) {
      cls.className = "advice-item advice-good";
      cls.textContent = "✓ " + g.recommend_class + " is the recommended storage class for this type.";
    } else {
      cls.className = "advice-item advice-info";
      cls.textContent = "Recommended storage class: " + g.recommend_class + " — " + g.class_reason;
    }
    guidanceEl.appendChild(cls);
    guidanceEl.hidden = false;
  }
  function paint(data) {
    if (firstEl) firstEl.textContent = money(data && data.projection && data.projection.first_bill);
    thisEl.textContent = money(data && data.this_job_monthly);
    totalEl.textContent = money(data && data.new_total_monthly);
    if (dateEl) dateEl.textContent = (data && data.price_date) || "—";
    if (restoreEl) restoreEl.textContent = money(data && data.this_job_restore);
    paintBreakdown(data && data.breakdown);
    paintFuture(data && data.projection);
    paintGuidance(data && data.guidance);
    paintAdvice(data && data.advice);
  }
  function update() {
    var qs = new URLSearchParams(new FormData(form)).toString();
    fetch("/jobs/estimate.json?" + qs)
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.ok) {
          if (errEl) { errEl.hidden = true; errEl.textContent = ""; }
          paint(res.j);
        } else {
          if (errEl) { errEl.hidden = false; errEl.textContent = res.j.error || "invalid input"; }
          paint({});
        }
      })
      .catch(function () {});
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(update, 250); }

  form.addEventListener("input", schedule);
  form.addEventListener("change", schedule);

  if (sourceTree && sizeInput) {
    sourceTree.addEventListener("change", function (ev) {
      var t = ev.target;
      if (!t || t.type !== "checkbox") return;
      var path = t.checked ? t.value : "";
      if (!path) { sizeInput.value = ""; sizing(false); showInfo(null); schedule(); return; }
      // The estimate is NEVER blocked on the folder walk: recompute right away with
      // the current/default size, show the "calculating…" line, and fetch the real
      // folder size + file count async. When it returns, seed #size-gb-input, show
      // what we found, and recompute once more.
      sizeInput.value = "";
      showInfo(null);
      sizing(true);
      schedule();
      fetch("/jobs/source-size?path=" + encodeURIComponent(path))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          sizing(false);
          if (d) { sizeInput.value = String(d.bytes / (1024 * 1024 * 1024)); showInfo(d); schedule(); }
        })
        .catch(function () { sizing(false); });
    });
  }

  update();
})();

// Current spend: "Refresh usage" / "Connect AWS billing" are plain CSRF-protected
// form POSTs (the server does the work) — just guard against a double submit.
(function () {
  var forms = document.querySelectorAll('form[action$="/costs/refresh"], form[action$="/costs/billing"]');
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener("submit", function (ev) {
      var buttons = ev.target.querySelectorAll('button[type="submit"]');
      for (var j = 0; j < buttons.length; j++) buttons[j].disabled = true;
    });
  }
})();

// Wizard: show only the controls that apply to the chosen backup type
// ([data-when-type], e.g. the tiered fieldset / archive mirror) AND the chosen
// retention policy ([data-when-retention], e.g. the retention_days/retention_count
// fields / the tiered fieldset). An element with both attributes needs both to
// match. Toggled on load and whenever the type or retention_type radio changes.
(function () {
  var form = document.getElementById("job-form");
  if (!form) return;
  var conds = form.querySelectorAll("[data-when-type], [data-when-retention], [data-when-packing]");
  function curType() {
    var c = form.querySelector('input[name="type"]:checked');
    return c ? c.value : "";
  }
  function curRetention() {
    var c = form.querySelector('input[name="retention_type"]:checked');
    return c ? c.value : "";
  }
  function curPacking() {
    var c = form.querySelector('input[name="packing"]');
    return (c && c.checked) ? "1" : "0";
  }
  function applyVisibility() {
    var t = curType();
    // "tiered" is versioned-only (its own radio label carries
    // data-when-type="versioned"); if the backup type changes away from versioned
    // while Tiered is still selected, the radio would stay checked while hidden --
    // submitting would silently POST retention_type=tiered for a non-versioned job
    // and 400 server-side. Snap the selection to "days" first so what's checked
    // (and what's rendered below) always matches what's shown.
    if (t !== "versioned") {
      var tiered = form.querySelector('input[name="retention_type"][value="tiered"]');
      if (tiered && tiered.checked) {
        tiered.checked = false;
        var days = form.querySelector('input[name="retention_type"][value="days"]');
        if (days) days.checked = true;
      }
    }
    var rt = curRetention();
    var pk = curPacking();
    for (var i = 0; i < conds.length; i++) {
      var el = conds[i];
      var wantType = el.getAttribute("data-when-type");
      var wantRetention = el.getAttribute("data-when-retention");
      var wantPacking = el.getAttribute("data-when-packing");
      var hide = false;
      if (wantType && wantType.split(/\s+/).indexOf(t) === -1) hide = true;         // may list several types
      if (wantRetention && wantRetention.split(/\s+/).indexOf(rt) === -1) hide = true;
      if (wantPacking && wantPacking !== pk) hide = true;
      el.hidden = hide;
    }
  }
  var toggles = form.querySelectorAll('input[name="type"], input[name="retention_type"], input[name="packing"]');
  for (var j = 0; j < toggles.length; j++) toggles[j].addEventListener("change", applyVisibility);
  applyVisibility();
})();
