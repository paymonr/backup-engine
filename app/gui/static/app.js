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

// Media shares: lazy folder tree. Each <ul class="tree" data-root data-selected> hydrates from /shares/browse.
(function () {
  var trees = document.querySelectorAll("ul.tree");
  if (!trees.length) return;
  function browse(share, path) {
    return fetch("shares/browse?share=" + encodeURIComponent(share) + "&path=" + encodeURIComponent(path))
      .then(function (r) { return r.ok ? r.json() : { entries: [] }; })
      .catch(function () { return { entries: [] }; });
  }
  // node() materializes one tree entry. `pending` maps a still-unmaterialized
  // selected path to the hidden <input> standing in for it (see below); once
  // the real checkbox for that path exists, the hidden input is removed so
  // the checkbox alone governs submission (and unticking it works).
  function node(share, entry, selected, pending) {
    var li = document.createElement("li");
    var label = document.createElement("label");
    var cb = document.createElement("input");
    cb.type = "checkbox"; cb.name = "folder"; cb.value = entry.path;
    if (selected.indexOf(entry.path) !== -1) {
      cb.checked = true;
      if (pending[entry.path]) {
        pending[entry.path].parentNode.removeChild(pending[entry.path]);
        delete pending[entry.path];
      }
    }
    label.appendChild(cb); label.appendChild(document.createTextNode(" " + entry.name));
    var toggle = document.createElement("button");
    toggle.type = "button"; toggle.textContent = "▸"; toggle.className = "expand";
    var kids = document.createElement("ul"); kids.className = "tree"; kids.hidden = true;
    var loaded = false;
    toggle.addEventListener("click", function () {
      kids.hidden = !kids.hidden;
      toggle.textContent = kids.hidden ? "▸" : "▾";
      if (!loaded && !kids.hidden) {
        loaded = true;
        browse(share, entry.path).then(function (d) {
          d.entries.forEach(function (e) { kids.appendChild(node(share, e, selected, pending)); });
        });
      }
    });
    li.appendChild(toggle); li.appendChild(label); li.appendChild(kids);
    return li;
  }
  trees.forEach(function (ul) {
    if (!ul.dataset.root) return; // only top-level trees self-hydrate
    var share = ul.dataset.root;
    var selected = (ul.dataset.selected || "").split(",").filter(Boolean);
    // Preserve-then-reconcile: only top-level nodes render on load, so a
    // selected nested path (e.g. "manga/raw") whose checkbox never
    // materializes would otherwise be silently dropped from the submit and
    // the share would revert to whole-share on save. Stand in with a hidden
    // input for every selected path up front; node() retires the stand-in
    // once (if) the real checkbox for that path is expanded into view.
    var pending = {};
    selected.forEach(function (p) {
      var hidden = document.createElement("input");
      hidden.type = "hidden"; hidden.name = "folder"; hidden.value = p;
      pending[p] = hidden;
      ul.appendChild(hidden);
    });
    browse(share, "").then(function (d) {
      d.entries.forEach(function (e) { ul.appendChild(node(share, e, selected, pending)); });
    });
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
  function dig(obj, path) {
    return path.split(".").reduce(function (o, k) { return (o == null) ? undefined : o[k]; }, obj);
  }
  function paint(data) {
    var cells = document.querySelectorAll("[data-est]");
    for (var i = 0; i < cells.length; i++) {
      cells[i].textContent = fmt(dig(data, cells[i].getAttribute("data-est")), cells[i].getAttribute("data-fmt"));
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
