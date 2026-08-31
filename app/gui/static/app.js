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
  var timer;

  function sizing(on) { if (sizingEl) sizingEl.hidden = !on; }

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
  function paint(data) {
    thisEl.textContent = money(data && data.this_job_monthly);
    totalEl.textContent = money(data && data.new_total_monthly);
    if (dateEl) dateEl.textContent = (data && data.price_date) || "—";
    if (restoreEl) restoreEl.textContent = money(data && data.this_job_restore);
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
      if (!path) { sizeInput.value = ""; sizing(false); schedule(); return; }
      // The estimate is NEVER blocked on the folder walk: recompute right away with
      // the current/default size, show a "sizing…" hint, and fetch the real folder
      // size async. When it returns, seed #size-gb-input and recompute once more.
      sizeInput.value = "";
      sizing(true);
      schedule();
      fetch("/jobs/source-size?path=" + encodeURIComponent(path))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          sizing(false);
          if (d) { sizeInput.value = String(d.bytes / (1024 * 1024 * 1024)); schedule(); }
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
