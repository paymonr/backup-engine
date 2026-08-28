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
