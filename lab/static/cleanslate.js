/* Deskhand Lab — the Clean slate button in the top bar, on every view.

   It deletes what earlier runs left behind, so a demo starts from nothing:
   01–04's logs/runs.jsonl, the rows in 02–04's SQLite files, the traces the
   lab server keeps in memory, and the mock's refund till (a restart). Code,
   tickets, policies, .env and the Qdrant index are never touched.

   It always asks first. GET /api/clean-slate says what each part holds
   right now; nothing is deleted until the red button is clicked. The dialog
   is index.html's #confirm, the one Compare and Repeat ask with. When it is
   done the page reloads, so this page's run history and session cost go too
   (the toggles stay: they live in localStorage). */
(function () {
  "use strict";
  var $ = L.$, el = L.el, add = L.add, S = L.S;

  function bytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }
  function plural(n, word) { return L.num(n) + " " + word + (n === 1 ? "" : "s"); }
  function sum(list, key) { return list.reduce(function (t, x) { return t + (x[key] || 0); }, 0); }

  // One checkbox row: a title, what it holds now, the files it covers.
  // disabled: nothing this button can delete there (the reason is the detail).
  function item(key, title, detail, files, disabled) {
    var box = el("input");
    box.type = "checkbox";
    box.id = "cs-" + key;
    box.checked = !disabled;
    box.disabled = !!disabled;
    var text = el("span", "cs-text", el("b", null, title), el("span", "muted", detail));
    if (files && files.length) add(text, el("span", "cs-files mono tiny", files.join("  ·  ")));
    return { row: el("label", "cs-item" + (disabled ? " off" : ""), box, text), box: box, key: key, off: !!disabled };
  }

  function buildItems(p) {
    var logs = p.logs.filter(function (f) { return f.exists; });
    var lines = sum(logs, "lines"), logBytes = sum(logs, "size_bytes");
    var rows = sum(p.state, "rows"), stBytes = sum(p.state, "size_bytes");
    var t = p.till;
    var tillDetail = !t.up ? "The mock isn't running, so there is no till to empty."
      : !t.managed ? "The mock was started outside the lab: restart it in its own terminal to empty its till."
      : (t.refunds === null ? "The till" : plural(t.refunds, "refund")) + " in the mock's memory. The lab restarts the mock (same chaos flags) to empty it.";
    return [
      item("logs", "Logs", logs.length
        ? plural(lines, "line") + " · " + bytes(logBytes) + ". The files are deleted; the next run starts a new one."
        : "Already empty.", logs.map(function (f) { return f.path; }), !logs.length),
      item("state", "Saved state", p.state.length
        ? plural(rows, "row") + " · " + bytes(stBytes) + ". Every row goes; the files and tables stay, so a running service keeps working."
        : "No SQLite files yet.", p.state.map(function (f) { return f.path; }), !p.state.length),
      item("runs", "Run history", plural(p.runs.traces, "run report") + " in the lab server's memory (the #run links), " +
        "the costs Compare and Repeat estimate from, Follow's recent list and this page's session cost.",
        null, false),
      item("till", "Refund till", tillDetail, null, !(t.up && t.managed))
    ];
  }

  function resultList(done) {
    var ul = el("ul", "cs-results");
    done.forEach(function (d) {
      add(ul, el("li", d.ok ? "good" : "bad", (d.ok ? "✓ " : "✗ ") + d.what + ": ", el("span", "muted", d.note)));
    });
    if (!done.length) add(ul, el("li", "muted", "Nothing to delete."));
    return ul;
  }

  // The view the page reloads into: a finished run's #run/<id> and the Saved
  // state popup's run=<id> would point at what was just deleted.
  function cleanHash() {
    var h = location.hash.replace(/^#/, "");
    if (/^run\//.test(h)) return "run";
    return h.replace(/([?&])run=[^&]*&?/, "$1").replace(/[?&]$/, "");
  }

  function open() {
    var d = $("confirm"), box = $("confirm-body"), yes = $("confirm-yes"), no = $("confirm-no");
    if (d.open) return;
    function reset() {  // #confirm is shared: hand it back the way Compare and Repeat expect it
      yes.textContent = "Run it";
      yes.className = "btn btn-primary";
      yes.disabled = false;
      no.hidden = false;
      no.disabled = false;
    }
    function close() { d.close(); reset(); }
    $("confirm-title").textContent = "Clean slate";
    box.textContent = "";
    add(box, el("p", "muted", "Reading what there is to delete…"));
    yes.textContent = "Delete";
    yes.className = "btn btn-bad";
    yes.disabled = true;
    no.onclick = close;
    d.oncancel = function () { reset(); };
    d.showModal();
    no.focus();

    L.getJSON("/api/clean-slate").then(function (p) {
      var items = buildItems(p);
      var busy = Object.keys(S.busy).filter(function (k) { return S.busy[k]; }).length;
      box.textContent = "";
      add(box, el("p", null, "Deletes what earlier runs left behind, so the next run starts from nothing. " +
        "Code, tickets, policies, .env and the policy index are not touched. ", el("b", null, "This cannot be undone.")));
      var list = el("div", "cs-list");
      items.forEach(function (it) { add(list, it.row); });
      add(box, list);
      var running = p.open_runs || busy;
      if (running) add(box, el("p", "warn", (running === 1 ? "A run is" : running + " runs are") + " still going. Let it finish (or answer its approval) first."));
      function update() {
        yes.disabled = !!running || !items.some(function (it) { return it.box.checked; });
      }
      items.forEach(function (it) { it.box.onchange = update; });
      update();
      yes.onclick = function () {
        var body = {};
        items.forEach(function (it) { body[it.key] = it.box.checked; });
        yes.disabled = no.disabled = true;
        items.forEach(function (it) { it.box.disabled = true; });
        yes.textContent = "Deleting…";
        L.postJSON("/api/clean-slate", body).then(function (r) {
          box.textContent = "";
          add(box, resultList(r.done));
          add(box, el("p", "muted", "The page reloads to start fresh."));
          no.hidden = true;
          yes.textContent = "Reload";
          yes.className = "btn btn-primary";
          yes.disabled = false;
          yes.onclick = function () { location.hash = cleanHash(); location.reload(); };
          d.oncancel = yes.onclick;
          yes.focus();
        }).catch(function (e) {
          add(box, el("p", "bad", "The clean slate failed: " + e.message));
          items.forEach(function (it) { it.box.disabled = it.off; });
          yes.textContent = "Delete";
          no.disabled = false;
          update();
        });
      };
    }).catch(function (e) {
      box.textContent = "";
      add(box, el("p", "bad", "Could not read what there is to delete: " + e.message));
    });
  }

  $("clean-slate-btn").onclick = open;
})();
