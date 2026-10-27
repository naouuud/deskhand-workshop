/* Deskhand Lab — the Saved state view: what a service's SQLite files hold.
   Session 4's "save after every node, so a crash resumes", made visible:
   one headline (what a restart would pick up), the runs table of runs.db
   as it is right now, the files on disk, and, per run, the row, its stored
   events and, on LangGraph, every step it kept.

   Nothing here is invented: the rows are GET /api/state/<cp>, which reads
   the checkpoint's runs.db read only. Which runs a restart would pick up
   is the checkpoint's own store.find_resumable(), asked by the server in a
   runner process: this file never decides it from a status.

   Layout, top to bottom: the pure functions (fromHash, toHash, pathOf,
   collapseEvents, matches, bytes, ago), which touch no DOM; then the page:
   loading and polling, the headline, the toolbar, the table, the files,
   the run popup, and show() / hide(), which views.js calls when #state is
   routed. #state?cp=03&run=<request_id> opens on that checkpoint and run.

   Reads from L: the DOM helpers, getJSON, panel, kv, chip, pill, note, pre,
   section, stepper, cmdBlock, commandsSection, citesRow, errorPanel,
   serviceOf, startService, followRun, cpName. The state holds ticket text
   and model replies: everything goes through el() / textContent. */
(function () {
  "use strict";
  var el = L.el, add = L.add, S = L.S;

  var POLL_MS = 3000;  // while the view is open and the tab visible

  /* ── pure functions ────────────────────────────────────── */

  // "#state?cp=03&run=abc" -> {cp: "03", run: "abc"}; missing parts are null.
  function fromHash(h) {
    var q = String(h || "").replace(/^#/, "").split("?")[1] || "", out = { cp: null, run: null };
    q.split("&").forEach(function (part) {
      var kv = part.split("="), v = decodeURIComponent(kv[1] || "");
      if (kv[0] === "cp" && /^\d\d$/.test(v)) out.cp = v;
      if (kv[0] === "run" && /^[A-Za-z0-9_-]{4,64}$/.test(v)) out.run = v;
    });
    return out;
  }

  function toHash(cp, run) {
    return "state" + (cp ? "?cp=" + cp + (run ? "&run=" + encodeURIComponent(run) : "") : "");
  }

  // The nodes a run went through, from its stored node.started and
  // node.completed events: [{node, state: "done" | "active", ms}]. A node
  // that started and never completed is the one a crash interrupted (or
  // the one running now).
  function pathOf(events) {
    var out = [], open = {};
    (events || []).forEach(function (e) {
      var node = e.data && e.data.node;
      if (!node) return;
      if (e.event === "node.started") {
        open[node] = out.length;
        out.push({ node: node, state: "active", ms: null });
      } else if (e.event === "node.completed" && open[node] !== undefined) {
        out[open[node]].state = "done";
        out[open[node]].ms = e.data.latency_ms === undefined ? null : e.data.latency_ms;
        delete open[node];
      }
    });
    return out;
  }

  // Consecutive reply.delta events (one per streamed word) folded into one
  // row: {event, seq, seqTo, count, ts, data: {text}}. Everything else as is.
  function collapseEvents(events) {
    var out = [];
    (events || []).forEach(function (e) {
      var last = out[out.length - 1];
      if (e.event === "reply.delta" && last && last.event === "reply.delta") {
        last.count += 1;
        last.seqTo = e.seq;
        last.data = { text: last.data.text + ((e.data && e.data.text) || "") };
        return;
      }
      out.push({ event: e.event, seq: e.seq, seqTo: e.seq, count: 1, ts: e.ts,
                 data: e.event === "reply.delta" ? { text: (e.data && e.data.text) || "" } : e.data });
    });
    return out;
  }

  // The search box: request id, ticket, customer, status, next node.
  function matches(row, q) {
    if (!q) return true;
    var hay = [row.request_id, row.ticket_id, row.customer_name, row.status, row.next_node].join(" ").toLowerCase();
    return hay.indexOf(q.toLowerCase()) !== -1;
  }

  function bytes(n) {
    if (n === null || n === undefined) return "—";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  // "12s ago", "5 min ago", "3 h ago", "4 days ago".
  function ago(iso, now) {
    var t = Date.parse(iso || "");
    if (isNaN(t)) return "—";
    var s = Math.max(0, Math.round((now - t) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    var d = Math.floor(s / 86400);
    return d + " day" + (d === 1 ? "" : "s") + " ago";
  }

  /* ── the page ──────────────────────────────────────────── */

  // P: page state. cp is the checkpoint shown; data its last
  // GET /api/state/<cp>, sig a fingerprint so a poll that changed nothing
  // redraws nothing. open: the request id the popup shows; detail its last
  // GET /api/state/<cp>/runs/<id>; returnTo: what had focus before it
  // opened. leaving: Follow is taking the page to #run.
  var P = { cp: null, data: null, sig: null, err: null, visible: false, timer: null, pending: null,
            q: "", status: "", built: false, up: null, open: null, detail: null, detailErr: null, returnTo: null, leaving: false };
  var E = {};

  function s(n) { return n === 1 ? "" : "s"; }

  function btn(text, cls) {
    var b = el("button", "btn btn-sm" + (cls ? " " + cls : ""), text);
    b.type = "button";
    return b;
  }

  function clock(iso) {
    var t = Date.parse(iso || "");
    return isNaN(t) ? "—" : new Date(t).toLocaleTimeString();
  }

  function when(iso) {
    var t = Date.parse(iso || "");
    if (isNaN(t)) return "—";
    var d = new Date(t);
    return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString() : d.toLocaleString();
  }

  function sources() { return (P.data && P.data.sources) || ["02", "03", "04"].filter(function (c) { return S.cpById[c]; }); }

  function isUp(cp) { var svc = L.serviceOf(cp); return !!(svc && svc.up); }

  // The rows store.find_resumable() returned, by request id.
  function resumableById() {
    var m = {}, r = P.data && P.data.resumable;
    ((r && r.ok && r.runs) || []).forEach(function (x) { m[x.request_id] = x; });
    return m;
  }

  function runsFile() {
    return ((P.data && P.data.files) || []).filter(function (f) { return f.name === "runs.db"; })[0] || null;
  }

  function ticketText(row) {
    if (!row.ticket_id) return "—";
    return /^adhoc-/.test(row.ticket_id) ? "free text" : row.ticket_id;
  }

  // Pill tone per status, the same reading as the Run report's outcome pill:
  // failed is "failed safely" there, so warn, not red.
  function statusPill(status) {
    var tone = status === "completed" ? "good" : status === "failed" ? "warn" : "live";
    return L.pill(String(status || "—").replace(/_/g, " "), tone);
  }

  /* ── loading and polling ───────────────────────────────── */

  function fingerprint(d) {
    return JSON.stringify([d.checkpoint, d.total, d.counts, d.rows, d.resumable, d.error,
      (d.files || []).map(function (f) { return [f.name, f.exists, f.tables, f.error]; })]);
  }

  // One read at a time: a second call while one is in flight gets the same promise.
  function load() {
    if (P.pending) return P.pending;
    clearTimeout(P.timer);
    var cp = P.cp;
    P.pending = L.getJSON("/api/state/" + cp).then(function (d) {
      if (cp !== P.cp) return;  // switched checkpoint meanwhile
      P.err = null;
      var sig = fingerprint(d);
      P.data = d;
      if (sig !== P.sig) {
        P.sig = sig;
        render();
        if (P.open && E.dialog.open) refreshOpen();
      }
    }).catch(function (e) {
      if (cp !== P.cp) return;
      P.err = e.message;
      if (!P.data) render();
    }).then(function () {
      P.pending = null;
      renderStatus();
      if (cp !== P.cp) load(); else schedule();
    });
    return P.pending;
  }

  function schedule() {
    clearTimeout(P.timer);
    P.timer = null;
    if (P.visible && !document.hidden) P.timer = setTimeout(load, POLL_MS);
  }
  document.addEventListener("visibilitychange", function () {
    if (P.visible && !document.hidden) load(); else schedule();
  });

  // Another checkpoint: forget the last one's rows, keep the filters.
  function setCp(cp) {
    if (cp === P.cp) return;
    P.cp = cp;
    P.data = P.sig = P.err = null;
    P.up = null;
    if (E.dialog && E.dialog.open) E.dialog.close();
    history.replaceState(null, "", "#" + toHash(cp));
    render();
    load();
  }

  /* ── build (once) ──────────────────────────────────────── */

  function build() {
    if (P.built) return;
    P.built = true;
    var root = L.$("state-root");
    E.head = el("div");

    var bar = el("div", "controls st-toolbar");
    E.seg = el("div", "seg st-seg");
    E.seg.setAttribute("role", "group");
    E.seg.setAttribute("aria-label", "Which service's state");
    E.search = el("input", "search st-search");
    E.search.type = "search";
    E.search.autocomplete = "off";
    E.search.placeholder = "Search: request id, ticket, customer, node…";
    E.search.setAttribute("aria-label", "Search saved runs");
    E.search.oninput = function () { P.q = this.value.trim(); renderTable(); };
    E.statusSel = el("select", "st-filter");
    E.statusSel.setAttribute("aria-label", "Show runs with this status");
    E.statusSel.onchange = function () { P.status = this.value; renderTable(); };
    E.status = el("span", "muted tiny st-status");
    E.status.setAttribute("aria-live", "polite");
    add(bar, E.seg, E.search, E.statusSel, E.status);

    E.table = el("div");
    E.files = el("div");
    E.foot = el("div");
    add(root, E.head, bar, E.table, E.files, E.foot);

    E.dialog = L.$("state-dialog");
    E.dialogTitle = L.$("state-dialog-title");
    E.dialogBody = L.$("state-dialog-body");
    L.$("state-dialog-close").onclick = function () { E.dialog.close(); };
    E.dialog.addEventListener("click", function (e) { if (e.target === E.dialog) E.dialog.close(); });
    E.dialog.addEventListener("close", function () {
      var id = P.open, back = P.returnTo;
      P.open = P.returnTo = P.detail = P.detailErr = null;
      if (P.visible && !P.leaving) history.replaceState(null, "", "#" + toHash(P.cp));
      P.leaving = false;
      if (back && document.body.contains(back) && back !== document.body) { back.focus(); return; }
      var row = id && E.table.querySelector('[data-run="' + id + '"]');
      if (row) row.focus();
    });
  }

  function render() {
    renderSeg();
    renderHead();
    renderFilter();
    renderTable();
    renderFiles();
    renderFoot();
    renderStatus();
  }

  function renderStatus() {
    if (!E.status) return;
    E.status.textContent = P.err ? "could not refresh: " + P.err : P.data ? "read " + clock(P.data.read_at) : "";
  }

  function renderSeg() {
    E.seg.textContent = "";
    sources().forEach(function (cp) {
      var b = el("button", cp === P.cp ? "on" : null, L.cpName(cp));
      b.type = "button";
      b.setAttribute("aria-pressed", String(cp === P.cp));
      b.onclick = function () { setCp(cp); };
      E.seg.appendChild(b);
    });
  }

  /* ── headline ──────────────────────────────────────────── */

  // One sentence: how many runs are saved and what a restart would do with
  // them, by store.find_resumable()'s answer. Amber when runs are stuck
  // with the service down: starting it is what resumes them.
  function renderHead() {
    E.head.textContent = "";
    P.up = isUp(P.cp);
    if (!P.data) {
      // A lab started before this page existed serves state.js but has no
      // /api/state route: FastAPI's bare "Not Found". Say what fixes it.
      var stale = S.health && S.health.server && S.health.server.stale;
      add(E.head, !P.err ? el("p", "muted small pad", "Reading checkpoint " + P.cp + "'s runs.db…")
        : P.err === "Not Found" || stale
          ? L.errorPanel("This lab server is older than the Saved state page: lab/server.py changed after it started. Stop it (Ctrl+C) and start it again.",
              "py lab\\server.py")
          : L.errorPanel("The lab could not read checkpoint " + P.cp + "'s saved state: " + P.err));
      return;
    }
    var d = P.data, file = runsFile(), res = d.resumable || { ok: true, runs: [] }, up = P.up;
    var tone, verdict, startBtn = false;
    if (d.error) {
      tone = "bad";
      verdict = d.error;
    } else if (!file || !file.exists) {
      tone = "warn";
      verdict = "Checkpoint " + P.cp + " has no runs.db yet. The service creates it the first time it starts (store.init_db).";
      startBtn = !up;
    } else if (!d.total) {
      tone = "good";
      verdict = "runs.db is empty: no run saved yet. Every run gets its row the moment POST /tickets accepts it.";
    } else if (!res.ok) {
      tone = "warn";
      verdict = d.total + " run" + s(d.total) + " saved. The lab could not ask store.find_resumable() which ones a restart picks up.";
    } else if (!res.runs.length) {
      tone = "good";
      verdict = d.total + " run" + s(d.total) + " saved. A restart would pick up none of them: " + countsText(d.counts) + ".";
      if (d.counts.awaiting_approval) verdict += " The runs at the approval gate wait for a person, not a restart.";
    } else {
      var list = res.runs.map(function (r) { return r.request_id + " at " + (r.next_node || "the start"); }).join(", ");
      if (up) {
        tone = "live";
        verdict = res.runs.length + " run" + s(res.runs.length) + " in flight. If the service stopped now, a restart would pick " +
          (res.runs.length === 1 ? "it" : "each one") + " up at its next node: " + list + ".";
      } else {
        tone = "warn";
        verdict = res.runs.length + " run" + s(res.runs.length) + " stopped mid-flight. Start checkpoint " + P.cp +
          " and resume_stuck_runs picks " + (res.runs.length === 1 ? "it" : "each one") + " up at its next node: " + list + ".";
        startBtn = true;
      }
    }

    var root = el("div", "headline tone-" + tone);
    var top = el("div", "headline-top");
    add(top, el("span", null, "checkpoint " + L.cpName(P.cp)), el("span", "dim", "·"), el("code", null, d.dir + "/runs.db"));
    var pl = L.pill(up ? "service running" : "service not running", up ? "good" : "");
    pl.style.marginLeft = "auto";
    top.appendChild(pl);
    var stats = el("div", "headline-stats");
    var lg = (d.files || []).filter(function (f) { return f.name === "runs.langgraph.db"; })[0];
    [["runs saved", L.num(d.total)],
     ["restart picks up", res.ok ? String(res.runs.length) : "?"],
     ["events stored", L.num(tableRows(file, "events"))],
     ["LangGraph steps", lg ? L.num(tableRows(lg, "checkpoints")) : "—"],
     ["runs.db on disk", bytes(file && file.exists ? file.size_bytes : null)]
    ].forEach(function (st) { add(stats, add(el("div", "stat"), el("span", "k", st[0]), el("span", "v", st[1]))); });
    add(root, top, el("div", "verdict", verdict), stats);
    if (!res.ok && res.error) add(root, L.pre(res.error, "short"));

    if (startBtn) {
      var row = el("div", "approval-actions");
      var b = btn("Start checkpoint " + P.cp, "btn-primary");
      b.onclick = function () {
        b.disabled = true;
        b.textContent = "starting…";
        L.startService(P.cp).then(load).catch(function (e) { b.disabled = false; b.textContent = "start failed: " + e.message; });
      };
      var cmd = (S.cpById[P.cp] || {}).start;
      add(row, b, cmd ? el("span", "muted small", "or in a terminal: ", el("code", null, cmd)) : null);
      root.appendChild(row);
    }
    E.head.appendChild(root);
  }

  function tableRows(file, name) {
    var t = file && (file.tables || []).filter(function (x) { return x.name === name; })[0];
    return t ? t.rows : null;
  }

  // {completed: 21, failed: 5} -> "21 completed, 5 failed", biggest first.
  function countsText(counts) {
    return Object.keys(counts || {}).sort(function (a, b) { return counts[b] - counts[a]; }).map(function (k) {
      return counts[k] + " " + k.replace(/_/g, " ");
    }).join(", ");
  }

  /* ── the filter and the table ──────────────────────────── */

  function renderFilter() {
    var keep = P.status, sel = E.statusSel;
    sel.textContent = "";
    var opts = [["", "every status"], ["resume", "a restart picks up"]];
    Object.keys((P.data && P.data.counts) || {}).sort().forEach(function (k) { opts.push([k, k.replace(/_/g, " ")]); });
    opts.forEach(function (o) {
      var opt = el("option", null, o[1]);
      opt.value = o[0];
      sel.appendChild(opt);
    });
    sel.value = keep;
    if (sel.value !== keep) { sel.value = ""; P.status = ""; }
  }

  // Six columns, seven when the checkpoint keeps LangGraph steps. The whole
  // row opens the popup, and is a focusable button for the keyboard.
  function renderTable() {
    E.table.textContent = "";
    if (!P.data || !P.data.rows) return;
    var d = P.data, res = resumableById(), now = Date.now();
    if (!d.rows.length) return;
    var rows = d.rows.filter(function (r) {
      return matches(r, P.q) && (!P.status || (P.status === "resume" ? !!res[r.request_id] : r.status === P.status));
    });
    if (!rows.length) {
      add(E.table, el("p", "muted small pad", "No saved run matches."));
      return;
    }
    var steps = d.rows.some(function (r) { return r.steps !== null && r.steps !== undefined; });
    var table = el("table", "tbl st-table");
    var hr = el("tr");
    ["request id", "ticket", "status", "next node", "saved", "events"].concat(steps ? ["steps"] : []).forEach(function (h) {
      add(hr, el("th", null, h));
    });
    add(table, el("thead", null, hr));
    var tb = el("tbody");
    rows.forEach(function (r) {
      var id = r.request_id, pick = res[id];
      var tr = el("tr", "st-row" + (pick ? " st-resume" : "") + (id === P.open ? " st-on" : ""));
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");
      tr.setAttribute("aria-label", "Run " + id + ", " + r.status + ": open its saved state");
      tr.dataset.run = id;
      tr.onclick = function () { openRun(id); };
      tr.onkeydown = function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRun(id); } };
      var idCell = el("td", "mono st-id", id);
      if (pick) add(idCell, " ", L.chip(P.up ? "restart picks it up" : "resumes on start", P.up ? "chip-cp" : "chip-warn",
        "store.find_resumable() lists this run: a restart continues it at " + (pick.next_node || "the start")));
      var ticket = el("td", null, ticketText(r));
      if (r.customer_name) add(ticket, el("span", "muted st-who", " · " + r.customer_name));
      var saved = el("td", "st-when", ago(r.updated_at, now));
      saved.title = r.updated_at;
      add(tr, idCell, ticket, add(el("td"), statusPill(r.status)), el("td", "mono", r.next_node || "—"), saved,
        el("td", "mono", L.num(r.events)));
      if (steps) add(tr, el("td", "mono", r.steps === null || r.steps === undefined ? "—" : L.num(r.steps)));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    var p = el("div", "panel st-panel", el("div", "tbl-wrap", table));
    if (d.total > d.rows.length) add(p, el("p", "muted tiny st-more", "The newest " + d.rows.length + " of " + L.num(d.total) + " rows."));
    E.table.appendChild(p);
  }

  /* ── the files ─────────────────────────────────────────── */

  var FILES_WHY = "<code>runs</code> keeps one row per run and overwrites it after every node: the latest state only, " +
    "which is all a restart needs. History lives elsewhere: <code>events</code> is every event the stream sent, numbered, " +
    "and <code>runs.langgraph.db</code> (LangGraph's <code>SqliteSaver</code>) keeps one checkpoint per step. " +
    "The <code>-wal</code> file beside each is SQLite's write-ahead log: it is why a reader never blocks a run that is saving. " +
    "The lab opens every file read only.";

  function renderFiles() {
    E.files.textContent = "";
    if (!P.data) return;
    var c = P.data.cites || {};
    var p = L.panel({ title: "The files on disk", sub: P.data.dir, why: FILES_WHY,
      cites: [c.schema, c.save_state, c.find_resumable] });
    var table = el("table", "tbl");
    var hr = el("tr");
    ["file", "tables", "on disk", "named in"].forEach(function (h) { add(hr, el("th", null, h)); });
    add(table, el("thead", null, hr));
    var tb = el("tbody");
    P.data.files.forEach(function (f) {
      var tables = f.exists ? (f.error ? "could not read: " + f.error
        : f.tables.map(function (t) { return t.name + " " + L.num(t.rows); }).join(" · ")) : "not created yet";
      var named = el("td", "mono", f.written_by);
      var link = L.citesRow([f.cite]);
      if (link) add(named, link);
      add(tb, el("tr", null, el("td", "mono", f.name), el("td", f.exists ? null : "muted", tables),
        el("td", "mono", f.exists ? bytes(f.size_bytes) : "—"), named));
    });
    table.appendChild(tb);
    p.body.appendChild(el("div", "tbl-wrap", table));
    E.files.appendChild(p.root);
  }

  /* ── footer: the terminal ──────────────────────────────── */

  function sqlCmd(dir, sql) {
    return "py -c \"import sqlite3; c = sqlite3.connect('" + dir + "/runs.db'); print(*c.execute('" + sql + "'), sep='\\n')\"";
  }

  function renderFoot() {
    E.foot.textContent = "";
    if (!P.data) return;
    var dir = P.data.dir, win = dir.replace(/\//g, "\\");
    E.foot.appendChild(L.commandsSection([
      { label: "The newest rows, as this table reads them", cmd: sqlCmd(dir, "SELECT request_id, status, next_node, updated_at FROM runs ORDER BY updated_at DESC LIMIT 10") },
      { label: "What a restart would pick up: the service's own query", cmd: "Push-Location " + win + "; py -c \"import store; print(*[(st.request_id, nxt) for st, nxt in store.find_resumable()], sep='\\n')\"; Pop-Location",
        note: "The same call resume_stuck_runs makes when the service starts. Empty output: nothing to resume." }
    ]));
  }

  /* ── the run popup ─────────────────────────────────────── */

  function openRun(id) {
    if (!E.dialog.open) P.returnTo = document.activeElement;
    var changed = P.open !== id;
    P.open = id;
    if (changed) { P.detail = P.detailErr = null; }
    history.replaceState(null, "", "#" + toHash(P.cp, id));
    fillDialog();
    if (!E.dialog.open) E.dialog.showModal();
    loadDetail(id);
  }

  function loadDetail(id) {
    var cp = P.cp;
    return L.getJSON("/api/state/" + cp + "/runs/" + encodeURIComponent(id)).then(function (d) {
      if (P.open !== id || P.cp !== cp) return;
      P.detail = d;
      P.detailErr = null;
      fillDialog();
    }).catch(function (e) {
      if (P.open !== id || P.cp !== cp) return;
      P.detailErr = e.message;
      fillDialog();
    });
  }

  // A poll changed the table: re-read the open run if its row moved on.
  function refreshOpen() {
    var row = (P.data.rows || []).filter(function (r) { return r.request_id === P.open; })[0];
    var d = P.detail;
    if (!d || !row || row.updated_at !== d.updated_at || row.events !== d.events_total || row.status !== d.status) loadDetail(P.open);
    else fillDialog();
  }

  function fillDialog() {
    var body = E.dialogBody, id = P.open, d = P.detail;
    body.textContent = "";
    if (!d) {
      E.dialogTitle.textContent = id;
      add(body, P.detailErr ? L.note("bad", "The lab could not read this run: " + P.detailErr) : el("p", "muted small", "Reading the row…"));
      return;
    }
    E.dialogTitle.textContent = id + " · " + String(d.status).replace(/_/g, " ");
    var st = d.state || {}, pick = resumableById()[id], up = P.up;

    var facts = el("div", "kvs");
    add(facts,
      L.kv("status", d.status),
      L.kv("next node", d.next_node || "none"),
      L.kv("saved", when(d.updated_at)),
      L.kv("ticket", st.ticket_id ? (/^adhoc-/.test(st.ticket_id) ? "free text" : st.ticket_id) + (st.customer_name ? " · " + st.customer_name : "") : "—"),
      L.kv("cost", L.usd(st.cost_usd)));
    body.appendChild(facts);

    body.appendChild(restartNote(d, pick, up));

    var actions = el("div", "approval-actions st-actions");
    var follow = btn("Follow in Run ▶", "btn-primary");
    follow.disabled = !up || !!S.busy.run;
    follow.title = up ? "Replay this run's events from event 0 in the Run view; nothing is submitted" : "Start checkpoint " + P.cp + " to follow it";
    follow.onclick = function () {
      P.leaving = true;  // the popup's close must not put #state back over #run
      E.dialog.close();
      L.followRun(P.cp, id);
    };
    var copy = L.copyBtn(id);
    copy.textContent = "copy request id";
    add(actions, follow, copy);
    if (!up) add(actions, el("span", "muted small", "start checkpoint " + P.cp + " to follow it"));
    body.appendChild(actions);

    var path = pathOf(d.events);
    if (path.length || d.next_node) {
      var block = el("div", "st-dialog-block");
      add(block, el("h4", null, "The path, from the stored events"));
      var steps = path.map(function (n, i) {
        return { name: "n" + i, label: n.node, state: n.state === "done" ? "done" : (pick || d.status === "running" ? "active" : "error"),
                 sub: n.ms === null ? (n.state === "done" ? "" : "no node.completed") : L.ms(n.ms) };
      });
      var lastDone = path.length && path[path.length - 1].state === "done";
      if (d.next_node && (lastDone || !path.length)) steps.push({ name: "next", label: d.next_node, state: "pending", sub: "next_node" });
      add(block, L.stepper(steps).root);
      body.appendChild(block);
    }

    var raw = L.section("The row: state_json, as runs.db holds it");
    add(raw.body, el("p", "muted tiny", "Overwritten after every node: this is the latest state, not a history. GET /runs/" + id + " on the service returns the same thing."),
      L.pre(JSON.stringify(d.state, null, 2)));
    body.appendChild(raw.root);

    body.appendChild(eventsSection(d));
    var lg = langgraphSection(d);
    if (lg) body.appendChild(lg);

    var svc = L.serviceOf(P.cp), port = svc && svc.ports && svc.ports[0] ? svc.ports[0] : (S.cpById[P.cp] || {}).port || 8000;
    var cmds = L.section("Read it in the terminal");
    add(cmds.body,
      L.cmdBlock({ label: "The saved state, from the running service", cmd: "Invoke-RestMethod http://localhost:" + port + "/runs/" + id + " | ConvertTo-Json -Depth 8" }),
      L.cmdBlock({ label: "The row itself, from the file", cmd: "py -c \"import sqlite3; c = sqlite3.connect('" + P.data.dir +
        "/runs.db'); print(c.execute('SELECT status, next_node, updated_at FROM runs WHERE request_id = ?', ('" + id + "',)).fetchone())\"" }));
    body.appendChild(cmds.root);
  }

  // What a restart does with this row, by store.find_resumable()'s answer.
  function restartNote(d, pick, up) {
    if (pick) {
      return L.note(up ? "info" : "warn", up
        ? "store.find_resumable() lists this run. It is in flight: if the service stopped now, a restart would pick it up at " + (pick.next_node || "the start") + " with this saved state."
        : "store.find_resumable() lists this run. The service is not running: start it and resume_stuck_runs picks the run up at " + (pick.next_node || "the start") + ", without redoing the nodes before it.");
    }
    var res = P.data && P.data.resumable;
    if (res && !res.ok) return L.note("warn", "The lab could not ask store.find_resumable() about this run.");
    if (d.status === "awaiting_approval") {
      return L.note("info", "Waiting at the approval gate. store.find_resumable() leaves it out: it waits for a person, not a restart. Follow it in Run to approve or reject.");
    }
    return L.note(d.status === "completed" ? "good" : "", "store.find_resumable() leaves it out: the run is " + String(d.status).replace(/_/g, " ") + ". A restart does nothing with it.");
  }

  function eventsSection(d) {
    var rows = collapseEvents(d.events);
    var sec = L.section("Stored events: " + L.num(d.events_total) + " (the stream's numbered log)");
    add(sec.body, el("p", "muted tiny", "Every event is saved before it is sent, with a number per run: a client that reconnects with Last-Event-ID gets the rest from here. " +
      "Consecutive reply.delta events (one per streamed piece of the reply) are folded into one row."));
    var table = el("table", "tbl st-events");
    var hr = el("tr");
    ["#", "time", "event", "data"].forEach(function (h) { add(hr, el("th", null, h)); });
    add(table, el("thead", null, hr));
    var tb = el("tbody");
    rows.forEach(function (e) {
      var data = e.event === "reply.delta" ? "× " + e.count + ": " + e.data.text : e.data === null || e.data === undefined ? "" : JSON.stringify(e.data);
      add(tb, el("tr", null, el("td", "mono", e.seqTo !== e.seq ? e.seq + "–" + e.seqTo : String(e.seq)), el("td", "mono", clock(e.ts)),
        el("td", "mono", e.event), el("td", "mono clip st-data", data.length > 240 ? data.slice(0, 240) + "…" : data)));
    });
    table.appendChild(tb);
    add(sec.body, el("div", "tbl-wrap", table));
    if (d.events_total > d.events.length) add(sec.body, el("p", "muted tiny", "The first " + d.events.length + " of " + L.num(d.events_total) + "."));
    return sec.root;
  }

  // LangGraph's own record of the same run: one checkpoint per step, all
  // kept. null when this checkpoint has no runs.langgraph.db.
  function langgraphSection(d) {
    var lg = d.langgraph;
    if (!lg) return null;
    var n = lg.steps.length;
    var sec = L.section("LangGraph's steps: " + n + " checkpoint" + s(n) + " in runs.langgraph.db");
    if (lg.error) { add(sec.body, L.note("warn", "Could not read runs.langgraph.db: " + lg.error)); return sec.root; }
    if (!n) {
      add(sec.body, el("p", "muted small", "No steps for this run: it ran on the plain runner (graph.py), which saves only the row in runs.db."));
      return sec.root;
    }
    add(sec.body, el("p", "muted tiny", "runs.db kept one row for this run and overwrote it after every node. LangGraph's SqliteSaver kept a checkpoint per step, " +
      "keyed by thread_id = request_id. The values are stored as msgpack; the step, and the node each step routed to, are plain text."));
    var table = el("table", "tbl st-steps");
    var hr = el("tr");
    ["step", "ran", "routed to", ""].forEach(function (h) { add(hr, el("th", null, h)); });
    add(table, el("thead", null, hr));
    var tb = el("tbody");
    lg.steps.forEach(function (x) {
      var notes = el("td");
      if (x.source === "input") add(notes, el("span", "muted", "the run's input"));
      if (x.special.indexOf("__interrupt__") !== -1) add(notes, L.chip("paused: interrupt()", "chip-warn", "The approval gate stopped the graph here"));
      if (x.special.indexOf("__resume__") !== -1) add(notes, " ", L.chip("answered: Command(resume=…)", "chip-good", "The person's decision came in, and the graph went on"));
      add(tb, el("tr", null, el("td", "mono", String(x.step)), el("td", "mono", x.ran || "—"),
        el("td", "mono", x.next.length ? x.next.join(", ") : "—"), notes));
    });
    table.appendChild(tb);
    add(sec.body, el("div", "tbl-wrap", table));
    return sec.root;
  }

  /* ── show / hide (views.js) ────────────────────────────── */

  // The first time: the checkpoint the hash names, else the one Run has
  // picked if it is a service, else the first service running, else 02.
  function pickCp(h) {
    var all = sources();
    if (h.cp && all.indexOf(h.cp) !== -1) return h.cp;
    if (P.cp) return P.cp;
    if (all.indexOf(S.cp) !== -1) return S.cp;
    return all.filter(isUp)[0] || all[0] || "02";
  }

  function show(hash) {
    build();
    var h = fromHash(hash);
    P.visible = true;
    // The students' copy grows a checkpoint at a time: before 02 there is no service to read.
    if (S.checkpoints.length && !sources().length) {
      E.head.textContent = "";
      add(E.head, L.note("info", "Saved state reads the services' runs.db (checkpoints 02, 03 and 04). This copy of the repo has none of them yet."));
      return;
    }
    var cp = pickCp(h);
    if (cp !== P.cp) {
      P.cp = cp;
      P.data = P.sig = P.err = null;
    }
    history.replaceState(null, "", "#" + toHash(P.cp, h.run));
    render();
    load().then(function () { if (h.run && P.data && P.open !== h.run) openRun(h.run); });
  }

  function hide() {
    P.visible = false;
    if (E.dialog && E.dialog.open) E.dialog.close();
    schedule();
  }

  // Setup started or stopped a service: the headline's pill and verdict
  // depend on it, and so do the table's chips.
  L.on("health", function () {
    if (!P.visible || !P.data) return;
    if (isUp(P.cp) !== P.up) { renderHead(); renderTable(); if (P.open && E.dialog.open) fillDialog(); }
  });

  L.state = {
    fromHash: fromHash, toHash: toHash, pathOf: pathOf, collapseEvents: collapseEvents, matches: matches,
    bytes: bytes, ago: ago, show: show, hide: hide,
    hash: function () { return toHash(P.cp); }
  };
})();
