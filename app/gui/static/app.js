// app/gui/static/app.js — poll the log tail
(function () {
  var el = document.getElementById("log");
  if (!el) return;
  function refresh() {
    fetch("logs?tail=200").then(function (r) { return r.text(); })
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
    return fetch("jobs/browse?path=" + encodeURIComponent(path))
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
    fetch("estimate.json?" + qs)
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
