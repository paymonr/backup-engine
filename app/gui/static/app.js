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

// Create/edit job WIZARD (job_form.html, spec 5.8/5.9): the server owns the cost
// model — this fetches /jobs/estimate.json on every change and repaints every cell
// beside its control. It LAYERS ON TOP of the preserved folder browser, schedule
// builder and data-when-* visibility (spec 2.3) — it never touches their elements.
(function () {
  var form = document.getElementById("job-form");
  if (!form || !form.dataset.estUrl) return;
  var estUrl = form.dataset.estUrl, sizeUrl = form.dataset.sourceSizeUrl;
  var isEdit = form.dataset.edit === "1";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var whenKey = "typical", lastData = null, typeTouched = isEdit, timer;
  var acked = {};
  $$('input[name="acknowledge_blocker"]', form).forEach(function (i) { acked[i.value] = true; });
  var savedCmp = {};
  try { savedCmp = JSON.parse((document.getElementById("saved-cmp") || {}).textContent || "{}") || {}; } catch (e) {}

  function money(v) {
    if (v === null || v === undefined) return "—";
    return "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtBytes(b) {
    var gb = b / (1024 * 1024 * 1024);
    if (gb >= 1) return gb.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " GB";
    var mb = b / (1024 * 1024);
    if (mb >= 1) return mb.toLocaleString(undefined, { maximumFractionDigits: 1 }) + " MB";
    return Math.max(0, Math.round(b / 1024)).toLocaleString() + " KB";
  }
  function flash(el) { if (!el) return; el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
  function set(el, txt) { if (el && el.textContent !== txt) { el.textContent = txt; flash(el); } }
  function priceKind() { var t = $("#live-toggle"); return t ? t.dataset.kind : "bundled"; }

  // ---- folder measurement: fetch the real size async, thread it into size_gb +
  //      measured_bytes so the estimate (which never walks the disk) reflects it.
  function measure(path) {
    var sizeIn = $("#size-gb-input"), fcIn = $("#file-count-input"),
        mbIn = $("#measured-bytes-input"), capIn = $("#measured-capped-input"),
        atIn = $("#measured-at-input"), line = $("#measure-line"), sizing = $("#job-cost-sizing");
    if (!path) {
      sizeIn.value = fcIn.value = mbIn.value = capIn.value = ""; if (line) line.textContent = "";
      schedule(); return;
    }
    if (sizing) sizing.hidden = false;
    if (line) line.innerHTML = '<span class="n assumed">measuring…</span>';
    fetch(sizeUrl + "?path=" + encodeURIComponent(path))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (sizing) sizing.hidden = true;
        if (!d) { if (line) line.textContent = "Couldn't measure that folder."; return; }
        capIn.value = d.capped ? "1" : "";
        if (d.capped && !d.bytes) {
          sizeIn.value = ""; mbIn.value = ""; fcIn.value = "";
          if (line) line.innerHTML = "Couldn't finish measuring this folder — it may be extremely large or unreadable.";
          schedule(); return;
        }
        sizeIn.value = String(d.bytes / (1024 * 1024 * 1024));
        mbIn.value = d.capped ? "" : String(d.bytes);   // partial walk -> assumed
        fcIn.value = String(d.count || 0);
        atIn.value = "";
        if (line) line.innerHTML = '<span class="n">' + fmtBytes(d.bytes) + "</span> · <span class=\"mono\">" +
          Number(d.count || 0).toLocaleString() + " files</span> · measured just now" +
          (d.capped ? " (measurement may be incomplete)" : "") +
          ' · <button type="button" class="linklike" id="remeasure-btn">Re-measure</button>';
        schedule();
      })
      .catch(function () { if (sizing) sizing.hidden = true; });
  }
  var tree = document.getElementById("source-tree");
  if (tree && sizeUrl) {
    tree.addEventListener("change", function (ev) {
      if (!ev.target || ev.target.type !== "checkbox") return;
      undim(ev.target.checked);
      measure(ev.target.checked ? ev.target.value : "");
    });
  }
  function undim(on) {
    $$("section.sec").forEach(function (s, i) { if (i > 0) s.classList.toggle("dimmed", !on); });
  }

  // ---- the live repaint ----------------------------------------------------
  function paint(d) {
    lastData = d;
    // price stamp
    var stamp = $("#pricestamp"), lt = $("#live-toggle");
    if (stamp && lt) {
      var live = d.price_kind === "live";
      stamp.firstChild.nodeValue = "Prices: " + (live ? "live AWS price list" : "bundled table") +
        ", " + (d.price_region || "us-east-1") + ", " + (live ? "fetched " : "captured ") + (d.price_date || "—") +
        ". " + (d.live_failed ? "— the live list could not be fetched. " : "");
      lt.dataset.kind = d.price_kind; lt.textContent = live ? "Back to the bundled table ▸" : "Use live AWS prices ▸";
    }
    // class table
    (d.classes || []).forEach(function (c) {
      var m = $('[data-cls-monthly="' + c.class + '"]'), rq = $('[data-cls-restore="' + c.class + '"]'),
          row = $('[data-cls-row="' + c.class + '"]');
      if (m) { set(m, money(c.monthly)); m.classList.toggle("struck", !!c.blocked); }
      if (rq) {
        rq.classList.toggle("struck", !!c.blocked);
        rq.innerHTML = money(c.restore_once) + (c.reason ? '<span class="reason"> ' + c.reason + "</span>" : "");
      }
      if (row) row.classList.toggle("blocked", !!c.blocked);
    });
    // keep deltas + points
    (d.keep_options || []).forEach(function (o) {
      var el = $('.k-delta[data-keep="' + o.key + '"]');
      if (el) set(el, o.unbounded ? "still growing" : money(o.delta_monthly) + "/mo");
      var pts = $('[data-keep-points="' + o.key + '"]'); if (pts && o.points != null) set(pts, String(o.points));
      var rch = $('[data-keep-reach="' + o.key + '"]'); if (rch && o.reach_days != null) set(rch, String(o.reach_days));
    });
    paintKeepState(d);
    paintWhen(d);
    paintBlocker(d);
    paintWarnings(d);
    paintRecommendation(d);
    paintProvenance(d);
    paintDiff(d);
  }
  function curRadio(name) { var r = $('input[name="' + name + '"]:checked'); return r ? r.value : ""; }
  // Diff-aware `was:` on the edit screen (5.9): toggle each changed row vs the SAVED
  // values; the `was:` text itself is fixed (server-rendered), so this only shows/hides.
  function footChanged(d) {
    return isEdit && savedCmp.typical != null && d && d.this_job_monthly != null
      && Math.abs(savedCmp.typical - d.this_job_monthly) >= 0.005;
  }
  function paintDiff(d) {
    if (!isEdit) return;
    var cur = {
      storage_class: curRadio("storage_class"), change_rate_pct: curRadio("change_rate_pct"),
      source: ($("#source-input") || {}).value || "", retention_type: curRadio("retention_type"),
      schedule: ($("#sched-input") || {}).value || ""
    };
    Object.keys(cur).forEach(function (f) {
      var el = $('[data-was="' + f + '"]');
      if (el) el.hidden = String(cur[f]) === String(savedCmp[f] == null ? "" : savedCmp[f]);
    });
    var fw = $("#foot-was"); if (fw) fw.hidden = !footChanged(d);
  }
  function paintKeepState(d) {
    var head = $("#keep-head"), block = $("#keep-block"), cons = $("#keep-consequence");
    if (!head) return;
    var change = d.breakdown ? d.breakdown.change_rate_pct : 0;
    if (change === 0) {
      head.innerHTML = 'How long to keep old versions <span style="font-weight:400;font-size:13px;color:var(--warn)">' +
        '— no cost effect at 0% change · still bounds how far back you can restore</span>';
      if (block) block.classList.add("inert");
      if (cons) cons.innerHTML = 'Nothing gets replaced, so there are no old versions to store — every option above adds ' +
        '<span class="n">$0.00</span>. It is still a real choice: it bounds how far back you can restore, and how much ' +
        'one bad night can cost you.';
    } else {
      head.textContent = "How long to keep old versions";
      if (block) block.classList.remove("inert");
      if (cons) {
        if (d.projection.unbounded) {
          cons.innerHTML = 'Keep everything never plateaus, so no typical month is printed for it — only "still growing".';
        } else {
          cons.innerHTML = "At ~" + Math.round(change) + "% change, old versions settle at about " +
            (d.breakdown.old_multiplier).toFixed(2) + "× your data — <span class=\"n\">" + money(d.breakdown.versioning) +
            "</span> of the <span class=\"n\">" + money(d.this_job_monthly) +
            "</span>. Keeping less also limits how far back you can restore, which is worth something at 0% too.";
        }
      }
    }
  }
  var HEADS = { first: "your first bill", typical: "a typical month, per month", six: "the first 6 months, a total not a rate" };
  function jobFig(d, key) {
    if (key === "first") return money(d.projection.first_bill);
    if (key === "six") return money(d.projection.total_6mo);
    return d.projection.unbounded ? "still growing" : money(d.projection.steady_monthly);
  }
  function allFig(d, key) {
    if (key === "first") return money(d.all_jobs.first_bill);
    if (key === "six") return money(d.all_jobs.total_6mo);
    return d.all_jobs.unbounded ? "at least " + money(d.all_jobs.typical_floor) : money(d.all_jobs.typical);
  }
  function paintWhen(d) {
    var jt = jobFig(d, whenKey), at = allFig(d, whenKey);
    set($("#fig-job"), jt); set($("#fig-all"), at);
    set($("#foot-job"), jt); set($("#foot-all"), at);
    set($("#when-head"), HEADS[whenKey]);
    set($("#s-typical"), jobFig(d, "typical")); set($("#s-first"), money(d.projection.first_bill));
    var fr = $("#first-reason"); if (fr) fr.textContent = d.first_bill_reason_text || "";
    var foot = $("#foot-figs");
    if (foot) {
      var lead = HEADS[whenKey].replace(", per month", "").replace(", a total not a rate", "");
      // `(was $X)` follows this-job only when the typical-month cost changed on an edit (5.9).
      var wasHtml = (isEdit && whenKey === "typical")
        ? ' <span id="foot-was"' + (footChanged(d) ? "" : " hidden") + ">(was <span>" +
          money(savedCmp.typical) + "</span>)</span>" : "";
      foot.innerHTML = lead + ' — this job <span class="n" id="foot-job">' + jt + '</span>' + wasHtml +
        ' · all jobs <span class="n" id="foot-all">' + at + '</span> &nbsp;<a href="/cost">over time →</a>';
    }
    var wt = $("#working-text");
    if (wt && d.breakdown) {
      wt.innerHTML = (d.breakdown.billed_gb).toFixed(2) + " GB measured × <span class=\"mono\">$" +
        (d.breakdown.rate_gb_month).toFixed(3) + "</span>/GB·mo = <span class=\"mono\">" + money(d.breakdown.storage) +
        "</span> to store the files.";
    }
  }
  var NAME_RE = /^[A-Za-z0-9._-]+$/;
  // What (if anything) stops this form from saving, worded for the wizard's steps and
  // ordered like jobs_io.validate. A fresh job has no folder, no kind and no name, so
  // clicking Create just 200-re-renders with a buried error -- which reads as "the
  // button does nothing". Gate the footer instead, and say exactly what's missing.
  function missingReason() {
    if (isEdit) return "";                        // kind/name/source are locked & pre-set
    if (!((($("#source-input") || {}).value) || "")) return "Pick a folder above first.";
    if (!$('input[name="type"]:checked')) return "Choose what kind of backup this is (step 2).";
    var nm = ((($('[name="name"]') || {}).value) || "").trim();
    if (!nm) return "Name the job (step 4) to create it.";
    if (nm === "." || nm === ".." || !NAME_RE.test(nm)) return "Job names use letters, digits, dot, dash and underscore only.";
    return "";
  }
  function footerEnabled(on, reason) {
    var b = $("#create-btn"), r = $("#create-run-btn"), why = $("#create-why");
    if (b) b.disabled = !on; if (r) r.disabled = !on;
    if (why) { why.hidden = on; why.style.color = "var(--danger)"; if (!on) why.textContent = reason || "Fix the blocker above first."; }
  }
  // Combine the two gates: a standing unacknowledged blocker OR an incomplete form.
  function refreshFooter() {
    var blocked = !!(lastData && (lastData.blockers || []).some(function (b) { return !acked[b.code]; }));
    var reason = blocked ? "Fix the blocker above first." : missingReason();
    footerEnabled(!reason, reason);
  }
  function blockerOf(d, code) { return (d.blockers || []).filter(function (b) { return b.code === code; })[0]; }
  function paintBlocker(d) {
    var cb = $("#class-blocker");
    var cold = blockerOf(d, "snapshots_on_cold_class");
    if (cb) {
      cb.hidden = !cold;
      if (cold) $("#class-blocker-why").innerHTML = "A Snapshot backup can't read from <b>" + cold.plain + "</b> · " +
        '<span class="mono">' + cold.class + "</span>. <span class=\"term\" title=\"the snapshot engine\">restic</span> " +
        "re-reads its whole store every run, so every scheduled run would fail on a data read.";
    }
    var tb = $("#tiered-blocker"), tiered = blockerOf(d, "all_zero_tiered");
    if (tb) tb.hidden = !tiered;
    // The footer stays disabled while ANY blocker stands unacknowledged (an all-zero
    // tiered keep is a hard WON'T RUN — never overridable) OR the form is incomplete.
    refreshFooter();
  }
  function paintWarnings(d) {
    var box = $("#warnings"); if (!box) return;
    box.innerHTML = "";
    (d.warnings || []).forEach(function (w) {
      var div = document.createElement("div"); div.className = "sev heads"; div.setAttribute("data-warn", w.code);
      var fixHtml = w.fix ? '<div class="acts"><button type="button" class="btn btn-ghost btn-xs" data-warn-fix="' +
        w.code + '">' + w.fix.label + "</button></div>" : "";
      div.innerHTML = '<span class="sev-tab"><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">' +
        '<path fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" d="M6 1 11.2 10.6H.8z"/>' +
        "</svg> Heads up</span><p class=\"sm\">" + w.text + "</p>" + fixHtml;
      div._fix = w.fix; box.appendChild(div);
    });
  }
  function paintRecommendation(d) {
    var box = $("#rec-block"); if (!box) return;   // create screen only
    if (d.recommendation) {
      box.innerHTML = '<div class="sev rec" style="margin-top:8px"><span class="sev-tab">' +
        '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="none" ' +
        'stroke="currentColor" stroke-width="1.4"/><circle cx="6" cy="6" r="2" fill="currentColor"/></svg> ' +
        "Recommended for this folder</span><p class=\"sm faint measure\">" + d.recommendation.why +
        " Rule: " + d.recommendation.rule + ".</p></div>";
      if (!typeTouched) {
        var r = $('input[name="type"][value="' + d.recommendation.type + '"]');
        if (r && !r.checked) { r.checked = true; applyType(); }
      }
    } else if (d.provenance && d.provenance.size === "measured") {
      box.innerHTML = '<div class="sev note">No recommendation for this folder — none of the rules fit its shape. ' +
        "Pick the kind yourself; the table below is priced for all three.</div>";
    } else { box.innerHTML = ""; }
  }
  function paintProvenance(d) {
    var assumed = d.provenance && d.provenance.size === "assumed";
    ["#fig-job", "#fig-all"].forEach(function (s) { var e = $(s); if (e) e.classList.toggle("assumed", assumed); });
  }
  function applyType() {
    // nudge the preserved data-when-* visibility to re-evaluate.
    var t = $('input[name="type"]:checked'); if (t) t.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ---- fetch + debounce ----------------------------------------------------
  function update() {
    var qs = new URLSearchParams(new FormData(form)); qs.set("prices", priceKind());
    fetch(estUrl + "?" + qs.toString())
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        var err = $("#est-error");
        if (res.ok && !res.j.error) { if (err) err.hidden = true; paint(res.j); }
        else if (err) { err.hidden = false; err.textContent = res.j.error || "invalid input"; }
      })
      .catch(function () {});
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(update, 250); }
  form.addEventListener("input", schedule);
  form.addEventListener("change", schedule);
  // Re-gate the footer immediately on every edit (don't wait for the debounced fetch),
  // so naming the job or picking a folder enables the buttons at once.
  form.addEventListener("input", refreshFooter);
  form.addEventListener("change", refreshFooter);

  // ---- delegated clicks ----------------------------------------------------
  form.addEventListener("click", function (e) {
    var el = e.target;
    if (el.closest("#remeasure-btn")) { measure($("#source-input").value); return; }
    var tab = el.closest("[data-when]");
    if (tab) {
      whenKey = tab.getAttribute("data-when");
      $$("[data-when]", form).forEach(function (x) { x.setAttribute("aria-selected", String(x === tab)); });
      if (lastData) paintWhen(lastData); return;
    }
    var tog = el.closest("[data-toggle]");
    if (tog) { var tg = document.getElementById(tog.getAttribute("data-toggle"));
      if (tg) { tg.hidden = !tg.hidden; tog.setAttribute("aria-expanded", String(!tg.hidden)); } return; }
    if (el.closest("#advanced-keep")) { var adv = $("#tiered-advanced"); if (adv) adv.hidden = !adv.hidden; return; }
    if (el.closest("#fix-tiered-keeps")) {
      var kk = { keep_last: "3", keep_daily: "7", keep_weekly: "4", keep_monthly: "6" };
      Object.keys(kk).forEach(function (n) { var i = $('input[name="' + n + '"]'); if (i) i.value = kk[n]; });
      schedule(); return;
    }
    if (el.closest("#save-anyway")) {
      // Only the OVERRIDABLE cold blocker can be acknowledged (an all-zero tiered keep
      // is a hard WON'T RUN with its own fix — never a Save-anyway).
      var blk = lastData && blockerOf(lastData, "snapshots_on_cold_class"); if (!blk) return;
      acked[blk.code] = true;
      var h = document.createElement("input"); h.type = "hidden"; h.name = "acknowledge_blocker"; h.value = blk.code;
      form.appendChild(h);
      var b = el.closest("#save-anyway"); b.textContent = "Acknowledged — will save anyway"; b.disabled = true;
      refreshFooter();   // re-gate: acking clears the blocker, but completeness still applies
      var why = $("#create-why");
      if (why && !$("#create-btn").disabled) { why.hidden = false; why.style.color = "var(--faint)"; why.textContent = "Saving with the blocker acknowledged."; }
      return;
    }
    var ft = el.closest("[data-fix-type]");
    if (ft) { setRadio("type", ft.getAttribute("data-fix-type")); typeTouched = true; applyType(); schedule(); return; }
    var fc = el.closest("[data-fix-class]");
    if (fc) { setRadio("storage_class", fc.getAttribute("data-fix-class")); schedule(); return; }
    var wf = el.closest("[data-warn-fix]");
    if (wf) { var wrap = wf.closest("[data-warn]"); if (wrap && wrap._fix) applyFix(wrap._fix.set); schedule(); return; }
    if (el.closest("#use-suggest")) {
      var sc = el.closest("#use-suggest").getAttribute("data-suggest-cron"), si = $("#sched-input");
      if (si && sc) { si.value = sc; si.dispatchEvent(new Event("input", { bubbles: true })); } schedule(); return;
    }
    if (el.closest("#live-toggle")) {
      var lt = el.closest("#live-toggle"); lt.dataset.kind = lt.dataset.kind === "live" ? "bundled" : "live"; update(); return;
    }
  });
  function setRadio(name, value) {
    var r = $('input[name="' + name + '"][value="' + value + '"]'); if (r) r.checked = true;
  }
  function applyFix(set) {
    Object.keys(set || {}).forEach(function (k) {
      var v = set[k];
      var box = $('input[type="checkbox"][name="' + k + '"]');
      if (box) { box.checked = (v === "1" || v === true); return; }
      setRadio(k, v);
    });
  }
  // mark the change rate "set" the first time a radio is clicked (5.8 §3.1 tri-state)
  $$('input[name="change_rate_pct"]', form).forEach(function (r) {
    r.addEventListener("change", function () { var h = $("#change-rate-touched"); if (h) h.value = "1"; });
  });
  $$('input[name="type"]', form).forEach(function (r) {
    r.addEventListener("change", function () { typeTouched = true; });
  });

  refreshFooter();   // gate the footer at once (a fresh job starts incomplete)
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

// Copy buttons (".copy", spec: provision_automated/manual/scripted, job.html,
// run_record.html): most carry data-copy="<literal text>"; run_record.html's log
// button instead carries data-copy-target="runlog" and copies that element's live
// textContent (the log can grow/stream in place, so it isn't inlined into an
// attribute). One document-level delegated listener so current AND future copy
// buttons work. Pure progressive enhancement: guarded end to end, never throws.
//
// navigator.clipboard requires a secure context (https or localhost); this app is
// served over plain http on a LAN IP (e.g. http://192.168.1.227:8099), where it's
// undefined. Feature-detect it and fall back to the legacy hidden-textarea +
// document.execCommand("copy") approach.
(function () {
  function readText(btn) {
    var targetId = btn.getAttribute("data-copy-target");
    if (targetId) {
      var el = document.getElementById(targetId);
      return el ? el.textContent : "";
    }
    return btn.getAttribute("data-copy") || "";
  }
  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      ta.style.left = "-1000px";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e) {}
  }
  function flash(btn) {
    try {
      var original = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () {
        try { btn.textContent = original; } catch (e) {}
      }, 1500);
    } catch (e) {}
  }
  document.addEventListener("click", function (ev) {
    try {
      var btn = ev.target && ev.target.closest && ev.target.closest(".copy");
      if (!btn) return;
      var text = readText(btn);
      if (!text) return;
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () {
          flash(btn);
        }, function () {
          legacyCopy(text);
          flash(btn);
        });
      } else {
        legacyCopy(text);
        flash(btn);
      }
    } catch (e) {}
  });
})();
