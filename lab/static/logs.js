/* Deskhand Lab — the Logs view: every checkpoint's logs/runs.jsonl as one
   live stream, the way a production log explorer shows a service. Spec:
   specs/logs-page.md (the R numbers below).

   Nothing here is invented: every row is a real line the server read from
   01–04's logs/runs.jsonl (GET /api/logs). The level and the one-line
   message are the lab's reading of the event; obslog writes neither.

   Layout, top to bottom: the pure functions (message, defaultState,
   matches, facets, toHash / fromHash, terminal, runSummary, merge), which
   touch no DOM, so they run anywhere; then the page: loading and the live
   tail, the toolbar, the chips, the run strip, the chart, the facets, the
   table, and show() / hide(), which views.js calls when #logs is routed.

   Reads from L: the DOM helpers, usd, money, ms, getJSON, cmdBlock,
   citesRow, pill. Every value from a log line goes through el() /
   textContent: replies and guard reasons carry model text and injection
   payloads. */
(function () {
  "use strict";
  var el = L.el, add = L.add, usd = L.usd, money = L.money, ms = L.ms;

  var SOURCES = ["01", "02", "03", "04"];
  var LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"];
  var RANK = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };
  var RANGES = { "15m": 15 * 60000, "1h": 3600000, "24h": 86400000, all: null };
  var RANGE_LABEL = { "15m": "15 min", "1h": "1 h", "24h": "24 h", all: "all loaded" };
  var CUT = 160;          // a message is one line of at most this many characters
  var MAX_LINES = 5000;   // the most lines the page holds
  var TAIL = 1000;        // the first load
  var POLL_MS = 2000;     // the live tail
  var PAGE_ROWS = 500;    // rows drawn at a time; "show more" adds this many

  /* ── pure functions ────────────────────────────────────── */

  // One line, at most CUT characters, ending in … when cut.
  function cut(s) {
    var first = String(s === null || s === undefined ? "" : s).split(/\r?\n/)[0];
    return first.length > CUT ? first.slice(0, CUT - 1) + "…" : first;
  }

  function has(v) { return v !== null && v !== undefined && v !== ""; }

  // Parts joined with " · " (or a space), missing ones left out: older lines
  // carry fewer fields.
  function dots(parts) { return parts.filter(has).join(" · "); }
  function words(parts) { return parts.filter(has).join(" "); }

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  // obslog writes six fractional digits; Date.parse is only required to read three.
  function tsMs(ts) {
    if (!ts) return NaN;
    return Date.parse(String(ts).replace(/(\.\d{3})\d+/, "$1"));
  }

  function fieldsText(rec) {
    var out = [];
    for (var k in rec) {
      if (!Object.prototype.hasOwnProperty.call(rec, k) || k === "ts" || k === "request_id" || k === "event") continue;
      var v = rec[k];
      out.push(k + "=" + (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v)));
    }
    return out.join(" ");
  }

  // R6: what the event says, in one line.
  var MESSAGES = {
    "run.accepted": function (r) { return "ticket " + r.ticket_id; },
    "run.started": function (r) { return "ticket " + r.ticket_id; },
    "node.started": function (r) { return "→ " + r.node; },
    "node.completed": function (r) {
      var cost = has(r.cost_delta_usd) ? "+" + usd(r.cost_delta_usd) + (has(r.cost_usd) ? " (run " + usd(r.cost_usd) + ")" : "") : null;
      return dots(["✓ " + r.node + (has(r.latency_ms) ? " " + ms(r.latency_ms) : ""), cost]);
    },
    "node.error": function (r) { return "✗ " + words([r.node, r.internal_error_code]) + (has(r.detail) ? ": " + r.detail : ""); },
    "triage.completed": function (r) {
      return dots([r.category, r.priority, has(r.confidence) ? String(r.confidence) : null,
        has(r.attempt) ? "attempt " + r.attempt : null, has(r.latency_ms) ? ms(r.latency_ms) : null,
        has(r.cost_usd) ? usd(r.cost_usd) : null]);
    },
    "triage.repair": function (r) { return r.detail; },
    "order.fetched": function (r) {
      return r.order_id + " · customer match: " + (r.customer_match === true ? "yes" : r.customer_match === false ? "no" : "unknown");
    },
    "order.not_found": function (r) { return words([r.order_id, r.internal_error_code]); },
    "order.no_reference": function () { return "no order reference"; },
    "policy.retrieved": function (r) {
      return dots([plural((r.sections || []).length, "section"), has(r.top_score) ? "top " + Number(r.top_score).toFixed(2) : null]);
    },
    "plan.invalid": function (r) {
      return r.internal_error_code + " after " + plural(r.repair_attempts || 0, "repair") + ": " + ((r.violations || [])[0] || "");
    },
    "approval.required": function (r) {
      var refund = has(r.refund_amount_usd) ? "refund " + money(r.refund_amount_usd) + (has(r.order_total_usd) ? " of " + money(r.order_total_usd) : "") : null;
      return dots([(r.actions || []).join(", "), refund, has(r.confidence) ? "confidence " + r.confidence : null]);
    },
    "approval.decided": function (r) { return r.approved ? "approved" : "rejected"; },
    "refund.issued": function (r) { return dots([money(r.amount_usd), r.refund_id, has(r.idempotency_key) ? "key " + r.idempotency_key : null]); },
    "guardrail.input_tripped": function (r) { return words([r.guard, r.internal_error_code]) + (has(r.reason) ? ": " + r.reason : ""); },
    "run.completed": function (r) { return r.reply; },
    "run.failed": function (r) { return r.user_visible_error; },
    "run.status": function (r) { return r.status; },
    "run.resumed": function (r) { return "resumed at " + r.next_node; },
    "recovery.resumed_runs": function (r) { return "resumed " + plural((r.request_ids || []).length, "run"); },
    "service.runtime": function (r) { return "runtime " + r.runtime; }
  };

  function message(line) {
    var rec = line.rec || {};
    if (has(rec.raw) && !rec.event) return cut(rec.raw);
    var fn = MESSAGES[rec.event];
    return cut(fn ? fn(rec) : fieldsText(rec));
  }

  function defaultState() {
    return { svc: SOURCES.slice(), level: "INFO", range: "all", q: "", rid: null, event: null, code: null, live: true };
  }

  function lineText(line) {
    if (line._lc === undefined) line._lc = (line.rec && has(line.rec.raw) ? line.rec.raw : JSON.stringify(line.rec)).toLowerCase();
    return line._lc;
  }

  // R10, as the names of the checks that fail ([] when the line passes).
  // facets() needs to know which check failed, matches() only whether one did.
  function failures(line, st, now) {
    var rec = line.rec || {}, out = [];
    if (st.svc.indexOf(line.src) === -1) out.push("svc");
    if (RANK[line.level] < RANK[st.level]) out.push("level");
    if (st.rid && rec.request_id !== st.rid) out.push("rid");
    if (st.event && rec.event !== st.event) out.push("event");
    if (st.code && rec.internal_error_code !== st.code) out.push("code");
    if (RANGES[st.range]) {
      var t = tsMs(line.ts);  // a line with no ts passes every range
      if (!isNaN(t) && now - t > RANGES[st.range]) out.push("range");
    }
    if (st.q && lineText(line).indexOf(st.q.toLowerCase()) === -1) out.push("q");
    return out;
  }

  function matches(line, st, now) { return failures(line, st, now).length === 0; }

  // R11: each facet counts the lines that pass every filter but its own.
  function facets(lines, st, now) {
    var out = { svc: {}, level: {}, event: {}, code: {} };
    function bump(map, key) { if (has(key)) map[key] = (map[key] || 0) + 1; }
    lines.forEach(function (line) {
      var f = failures(line, st, now), rec = line.rec || {};
      if (f.length > 1) return;
      var only = f[0];
      if (!only || only === "svc") bump(out.svc, line.src);
      if (!only || only === "level") bump(out.level, line.level);
      if (!only || only === "event") bump(out.event, rec.event);
      if (!only || only === "code") bump(out.code, rec.internal_error_code);
    });
    return out;
  }

  // R14: the state in the hash, only what differs from the default.
  function toHash(st) {
    var d = defaultState(), parts = [];
    if (st.svc.join(",") !== d.svc.join(",")) parts.push("svc=" + st.svc.join(","));
    if (st.level !== d.level) parts.push("level=" + st.level.toLowerCase());
    if (st.range !== d.range) parts.push("range=" + st.range);
    if (st.q) parts.push("q=" + encodeURIComponent(st.q));
    if (st.rid) parts.push("rid=" + encodeURIComponent(st.rid));
    if (st.event) parts.push("event=" + encodeURIComponent(st.event));
    if (st.code) parts.push("code=" + encodeURIComponent(st.code));
    if (!st.live) parts.push("live=0");
    return parts.length ? "logs?" + parts.join("&") : "logs";
  }

  function fromHash(hash) {
    var st = defaultState(), query = String(hash || "").replace(/^#/, "").split("?")[1] || "";
    query.split("&").forEach(function (pair) {
      var i = pair.indexOf("="), k = i === -1 ? pair : pair.slice(0, i), v;
      try { v = decodeURIComponent(i === -1 ? "" : pair.slice(i + 1)); } catch (e) { return; }
      if (k === "svc") {
        var svc = SOURCES.filter(function (s) { return v.split(",").indexOf(s) !== -1; });
        if (svc.length) st.svc = svc;
      } else if (k === "level" && RANK[v.toUpperCase()] !== undefined) st.level = v.toUpperCase();
      else if (k === "range" && RANGES.hasOwnProperty(v)) st.range = v;
      else if ((k === "q" || k === "rid" || k === "event" || k === "code") && v) st[k] = v;
      else if (k === "live") st.live = v !== "0";
    });
    return st;
  }

  // R13: the same filter as PowerShell, one line per selected service.
  // Level and time range are the lab's reading; the command does not apply them.
  function terminal(st, sources) {
    var paths = {};
    (sources || []).forEach(function (s) { paths[s.id] = s.path.replace(/\//g, "\\"); });
    function quote(v) { return "'" + String(v).replace(/'/g, "''") + "'"; }
    var conds = [];
    if (st.rid) conds.push(["request_id", st.rid]);
    if (st.event) conds.push(["event", st.event]);
    if (st.code) conds.push(["internal_error_code", st.code]);
    return SOURCES.filter(function (id) { return st.svc.indexOf(id) !== -1 && paths[id]; }).map(function (id) {
      var p = paths[id];
      if (!st.q && !conds.length) return "Get-Content " + p + " -Tail 50";
      if (!conds.length) return "Select-String -Path " + p + " -SimpleMatch " + quote(st.q);
      if (!st.q && conds.length === 1 && st.rid) return "Select-String -Path " + p + " -SimpleMatch " + quote(st.rid);
      var where = conds.length === 1
        ? "Where-Object " + conds[0][0] + " -eq " + quote(conds[0][1])
        : "Where-Object { " + conds.map(function (c) { return "$_." + c[0] + " -eq " + quote(c[1]); }).join(" -and ") + " }";
      var head = st.q ? "Get-Content " + p + " | Select-String -SimpleMatch " + quote(st.q) + " | ForEach-Object Line" : "Get-Content " + p;
      return head + " | ConvertFrom-Json | " + where;
    });
  }

  // R12: one run's lines (oldest first), summed up for the run strip.
  function runSummary(lines) {
    var src = lines.length ? lines[0].src : null;
    var s = { src: src, ticket: null, first: null, last: null, duration_ms: null, nodes: 0, cost_usd: null,
      outcome: "none", follow: src !== null && src !== "01" };
    lines.forEach(function (line) {
      var r = line.rec || {};
      if (line.ts) { s.first = s.first || line.ts; s.last = line.ts; }
      if (!s.ticket && has(r.ticket_id)) s.ticket = r.ticket_id;
      if (r.event === "node.completed") {
        s.nodes += 1;
        if (has(r.cost_usd)) s.cost_usd = r.cost_usd;  // cumulative in 02–04
      }
      if (r.event === "triage.completed" && has(r.cost_usd)) s.cost_usd = (s.cost_usd || 0) + r.cost_usd;  // one per call in 01
      if (r.event === "run.completed" || r.event === "triage.completed") s.outcome = "completed";
      if (r.event === "run.failed" || r.event === "triage.failed") s.outcome = "failed";
    });
    var a = tsMs(s.first), b = tsMs(s.last);
    if (!isNaN(a) && !isNaN(b)) s.duration_ms = b - a;
    return s;
  }

  // R8: newer lines appended, a line already held skipped, the oldest dropped past max.
  function merge(lines, incoming, max) {
    var seen = {}, out = lines.slice();
    lines.forEach(function (l) { seen[l.id] = true; });
    incoming.forEach(function (l) { if (!seen[l.id]) { seen[l.id] = true; out.push(l); } });
    var dropped = Math.max(0, out.length - max);
    return { lines: dropped ? out.slice(dropped) : out, dropped: dropped };
  }

  /* ── the page ──────────────────────────────────────────── */
  // P: everything the page holds. lines: every loaded line, oldest first.
  // run: the request_id= reply while a request id is filtered (null else).
  // win: a chart bucket the reader clicked ({from, to} ms), not in the hash.
  // unseen: new lines that arrived while the reader was scrolled into the
  // table or had a row open; they wait behind the "new lines" pill.
  var P = {
    st: defaultState(), lines: [], sources: [], cursor: null, loaded: false, loading: false,
    run: null, runFor: null, win: null, open: {}, fresh: {}, unseen: 0, dropped: 0,
    shown: PAGE_ROWS, visible: false, timer: null, polling: false, status: "", cites: null, built: false
  };
  var $ = function (id) { return document.getElementById(id); };
  var E = {};  // the page's fixed elements, built once by build()

  function pad(n, w) { n = String(n); while (n.length < (w || 2)) n = "0" + n; return n; }
  // Local time for the reader; the UTC ts is in the tooltip.
  function clockAt(t, withMs) {
    if (isNaN(t)) return "—";
    var d = new Date(t);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + (withMs ? "." + pad(d.getMilliseconds(), 3) : "");
  }
  function clock(ts, withMs) { return clockAt(tsMs(ts), withMs); }
  // With the date in front, for a chart that spans more than a day.
  function stampAt(t, days) {
    return days ? new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + clockAt(t) : clockAt(t);
  }
  function sourceLabel(id) {
    for (var i = 0; i < P.sources.length; i++) if (P.sources[i].id === id) return P.sources[i].label;
    return id;
  }
  function btn(text, cls, title) {
    var b = el("button", "btn btn-sm" + (cls ? " " + cls : ""), text);
    b.type = "button";
    if (title) b.title = title;
    return b;
  }
  function pressed(b, on) { b.classList.toggle("on", !!on); b.setAttribute("aria-pressed", on ? "true" : "false"); return b; }

  // Every change of filter goes through here: the hash, the run strip, a redraw.
  function setState(patch) {
    for (var k in patch) if (patch.hasOwnProperty(k)) P.st[k] = patch[k];
    if (patch.hasOwnProperty("rid")) { P.run = null; P.runFor = null; if (P.st.rid) loadRun(P.st.rid); }
    P.shown = PAGE_ROWS;
    history.replaceState(null, "", "#" + toHash(P.st));
    render();
    if (patch.hasOwnProperty("live")) { if (P.st.live) poll(); else schedule(); }
  }

  /* ── loading and the live tail ─────────────────────────── */

  function load() {
    if (P.loading) return;
    P.loading = true;
    P.status = "reading the logs…";
    renderStatus();
    L.getJSON("/api/logs?tail=" + TAIL).then(function (d) {
      P.lines = d.lines; P.sources = d.sources; P.cursor = d.cursor; P.loaded = true; P.status = "";
      render();
    }).catch(function (e) { P.status = "could not read the logs: " + e.message; renderStatus(); })
      .then(function () { P.loading = false; schedule(); });
  }

  function loadRun(rid) {
    L.getJSON("/api/logs?request_id=" + encodeURIComponent(rid)).then(function (d) {
      if (P.st.rid !== rid) return;  // the filter moved on while this was in flight
      P.run = d; P.runFor = rid;
      render();
    }).catch(function (e) { P.status = "could not read run " + rid + ": " + e.message; renderStatus(); });
  }

  // R7, R9: poll while the tab is visible, the view is open and Live is on.
  function schedule() {
    clearTimeout(P.timer);
    P.timer = null;
    if (P.visible && P.loaded && P.st.live && !document.hidden) P.timer = setTimeout(poll, POLL_MS);
  }

  // One poll: as many after= calls as it takes to catch up (more: true).
  function poll() {
    clearTimeout(P.timer);
    if (P.polling || !P.cursor) return schedule();
    P.polling = true;
    pollOnce().catch(function (e) { P.status = "lab server not answering: " + e.message; renderStatus(); })
      .then(function () { P.polling = false; schedule(); });
  }

  function pollOnce() {
    return L.getJSON("/api/logs?after=" + encodeURIComponent(P.cursor)).then(function (d) {
      P.sources = d.sources;
      P.cursor = d.cursor;
      var incoming = d.lines.slice();
      (d.resets || []).forEach(function (src) {
        // A divider row where a log was cleared under us.
        incoming.unshift({ id: "reset:" + src + ":" + Date.now(), src: src, ts: new Date().toISOString(), level: "WARN", divider: true, rec: {} });
      });
      if (P.status) { P.status = ""; renderStatus(); }
      if (incoming.length) addLines(incoming);
      if (d.more) return pollOnce();
    });
  }

  function addLines(incoming) {
    var m = merge(P.lines, incoming, MAX_LINES);
    P.dropped += m.dropped;
    P.lines = m.lines;
    if (P.run && P.st.rid) {
      incoming.forEach(function (l) { if (l.rec && l.rec.request_id === P.st.rid) P.run.lines.push(l); });
    }
    var now = Date.now(), news = incoming.filter(function (l) { return passes(l, now); });
    // R8: no jumping while the reader is in the table or has a row open.
    if (awayFromTop() || hasOpenRow()) {
      P.unseen += news.length;
      renderAround();
      return;
    }
    news.forEach(function (l) { P.fresh[l.id] = true; });
    render();
  }

  function awayFromTop() {
    if (!E.table) return false;
    var top = E.table.getBoundingClientRect().top;
    return top < (parseInt(getComputedStyle(document.documentElement).getPropertyValue("--topbar-h"), 10) || 56) - 8;
  }
  function hasOpenRow() { for (var k in P.open) if (P.open[k]) return true; return false; }

  /* ── what is shown ─────────────────────────────────────── */

  // The lines the filters apply to: the loaded lines, or with a request id
  // filtered, that whole run from the server.
  function pool() {
    if (P.st.rid && P.run && P.runFor === P.st.rid) return P.run.lines;
    return P.lines;
  }

  function passes(line, now) {
    if (line.divider) return P.st.svc.indexOf(line.src) !== -1;
    if (!matches(line, P.st, now)) return false;
    if (P.win) {
      var t = tsMs(line.ts);
      if (!isNaN(t) && (t < P.win.from || t >= P.win.to)) return false;
    }
    return true;
  }

  // The whole view from P. Rows marked fresh (just arrived) animate in once.
  function render() {
    if (!P.built) return;
    var now = Date.now(), lines = pool();
    var rows = [];
    for (var i = lines.length - 1; i >= 0; i--) if (passes(lines[i], now)) rows.push(lines[i]);  // newest first
    P.unseen = 0;
    renderToolbar(rows.length, lines.length);
    renderChips();
    renderTerminal();
    renderRunStrip();
    renderChart(lines, now);
    renderFacets(lines, now);
    renderTable(rows);
    renderNewPill();
    renderStatus();
    P.fresh = {};
  }

  // Everything but the table, for lines that arrived while the reader was away.
  function renderAround() {
    var now = Date.now(), lines = pool();
    renderChart(lines, now);
    renderFacets(lines, now);
    renderNewPill();
    renderStatus();
  }

  /* ── toolbar, chips, terminal line ─────────────────────── */

  function build() {
    var root = $("logs-root");
    if (!root || P.built) return;
    E.bar = el("div", "lg-toolbar");
    E.live = btn("", "lg-live", "Pause or resume the live tail (Space)");
    E.live.onclick = function () { setState({ live: !P.st.live }); };
    E.search = el("input", "lg-search");
    E.search.type = "search";
    E.search.placeholder = "Search the raw lines…  ( / )";
    E.search.setAttribute("aria-label", "Search the raw log lines");
    E.search.autocomplete = "off";
    E.search.spellcheck = false;
    var typing = null;
    E.search.oninput = function () {
      clearTimeout(typing);
      typing = setTimeout(function () { setState({ q: E.search.value.trim() }); }, 200);
    };
    E.range = el("div", "seg lg-seg");
    E.range.setAttribute("role", "group");
    E.range.setAttribute("aria-label", "Time range");
    ["15m", "1h", "24h", "all"].forEach(function (r) {
      var b = el("button", null, RANGE_LABEL[r]);
      b.type = "button";
      b.dataset.range = r;
      b.onclick = function () { P.win = null; setState({ range: r }); };
      E.range.appendChild(b);
    });
    E.level = el("div", "seg lg-seg");
    E.level.setAttribute("role", "group");
    E.level.setAttribute("aria-label", "Lowest level shown");
    LEVELS.forEach(function (lv) {
      var b = el("button", null, lv);
      b.type = "button";
      b.dataset.level = lv;
      b.title = lv === "DEBUG" ? "Every line, node steps included" : lv + " and above";
      b.onclick = function () { setState({ level: lv }); };
      E.level.appendChild(b);
    });
    E.count = el("span", "lg-count muted small");
    E.count.setAttribute("aria-live", "polite");
    add(E.bar, E.live, E.search, el("span", "muted tiny", "range"), E.range, el("span", "muted tiny", "level ≥"), E.level, E.count);

    E.chips = el("div", "lg-chips");
    E.term = el("div", "lg-term");
    E.run = el("div", "lg-run");
    E.chart = el("div", "lg-chart");
    E.chart.setAttribute("role", "group");
    E.chart.setAttribute("aria-label", "Lines over time, by level");
    E.facets = el("aside", "lg-facets");
    E.facets.setAttribute("aria-label", "Facets");
    E.newPill = btn("", "lg-new");
    E.newPill.hidden = true;
    E.newPill.onclick = function () { P.open = {}; render(); E.bar.scrollIntoView({ block: "start" }); window.scrollBy(0, -80); };
    E.table = el("div", "lg-table");
    E.table.setAttribute("role", "list");
    E.table.setAttribute("aria-label", "Log lines, newest first");
    E.foot = el("div", "lg-foot muted small");
    E.status = el("div", "lg-status small");
    E.status.setAttribute("role", "status");
    var main = el("div", "lg-main");
    add(main, E.newPill, E.table, E.foot);
    var grid = el("div", "lg-grid");
    add(grid, E.facets, main);
    add(root, E.bar, E.status, E.chips, E.run, E.term, E.chart, grid);
    P.built = true;

    L.getJSON("/api/cites/04").then(function (c) {
      var row = L.citesRow([c.log_event, c.publish]);
      if (row) $("logs-cites").appendChild(row);
    }).catch(function () { /* code links are optional */ });

    document.addEventListener("visibilitychange", function () {
      if (!P.visible) return;
      if (document.hidden || !P.st.live) schedule(); else poll();  // R9: catch up from the cursor
    });

    // Space pauses / resumes, "/" jumps to the search, while Logs is open and
    // focus is not in a text field (a focused button keeps Space for itself).
    document.addEventListener("keydown", function (e) {
      if (!P.visible || e.ctrlKey || e.metaKey || e.altKey) return;
      var t = e.target, tag = t && t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
      if (e.key === "/") { e.preventDefault(); E.search.focus(); }
      else if (e.key === " " && tag !== "BUTTON" && tag !== "SUMMARY" && tag !== "A") { e.preventDefault(); setState({ live: !P.st.live }); }
    });
  }

  function renderStatus() {
    if (!E.status) return;
    E.status.textContent = P.status;
    E.status.hidden = !P.status;
  }

  function renderToolbar(shown, total) {
    E.live.textContent = P.st.live ? "● Live" : "❚❚ Paused";
    pressed(E.live, P.st.live);
    if (document.activeElement !== E.search) E.search.value = P.st.q;
    var i, b;
    for (i = 0; i < E.range.children.length; i++) { b = E.range.children[i]; pressed(b, !P.win && b.dataset.range === P.st.range); }
    for (i = 0; i < E.level.children.length; i++) { b = E.level.children[i]; pressed(b, b.dataset.level === P.st.level); }
    var newest = null;
    for (i = P.lines.length - 1; i >= 0 && !newest; i--) if (P.lines[i].ts && !P.lines[i].divider) newest = P.lines[i].ts;
    E.count.textContent = shown.toLocaleString() + " of " + total.toLocaleString() + " lines" + (newest ? " · newest " + clock(newest) : "");
  }

  function renderChips() {
    E.chips.textContent = "";
    var st = P.st, list = [];
    if (st.rid) list.push(["request id", st.rid, { rid: null }]);
    if (st.event) list.push(["event", st.event, { event: null }]);
    if (st.code) list.push(["code", st.code, { code: null }]);
    if (st.q) list.push(["text", st.q, { q: "" }]);
    if (st.svc.length !== SOURCES.length) list.push(["services", st.svc.map(sourceLabel).join(", "), { svc: SOURCES.slice() }]);
    if (P.win) list.push(["window", clockAt(P.win.from) + "–" + clockAt(P.win.to), null]);
    list.forEach(function (c) {
      var b = btn("", "lg-chip", "Remove this filter");
      add(b, el("span", "muted", c[0] + ": "), el("span", "mono", c[1]), el("span", "lg-x", " ✕"));
      b.setAttribute("aria-label", "Remove filter " + c[0] + " " + c[1]);
      b.onclick = function () { if (c[2]) setState(c[2]); else { P.win = null; render(); } };
      E.chips.appendChild(b);
    });
    if (list.length > 1) {
      var all = btn("clear all", "btn-ghost");
      all.onclick = function () { P.win = null; var d = defaultState(); d.live = P.st.live; setState(d); };
      E.chips.appendChild(all);
    }
    E.chips.hidden = !list.length;
  }

  function renderTerminal() {
    E.term.textContent = "";
    var lines = terminal(P.st, P.sources);
    if (!lines.length) return;
    add(E.term, L.cmdBlock({ label: "Do the same in the terminal (from the repo root)", cmd: lines.join("\n") }));
    add(E.term, el("p", "muted tiny lg-term-note", "Level and time range are the lab's own reading of each line; the command does not apply them."));
  }

  /* ── R12: the run strip ────────────────────────────────── */

  function renderRunStrip() {
    E.run.textContent = "";
    E.run.hidden = !P.st.rid;
    if (!P.st.rid) return;
    var rid = P.st.rid;
    var head = el("div", "lg-run-head");
    add(head, el("span", "muted small", "run"), el("b", "mono", rid));
    if (!P.run || P.runFor !== rid) {
      add(head, el("span", "muted small", "reading every line of this run…"));
      E.run.appendChild(head);
      return;
    }
    var s = runSummary(P.run.lines);
    if (!P.run.lines.length) {
      add(head, el("span", "muted small", "no line in the logs carries this request id"));
      E.run.appendChild(head);
      return;
    }
    var tone = s.outcome === "completed" ? "good" : s.outcome === "failed" ? "bad" : "warn";
    add(head, L.pill(s.outcome === "none" ? "no terminal event" : s.outcome, tone));
    if (s.follow && L.followRun) {
      var open = btn("Open in Run", "btn-primary", "Replay this run in the Run view (Follow): nothing is submitted");
      open.onclick = function () { L.followRun(s.src, rid); };
      add(head, open);
    }
    var kvs = el("div", "kvs");
    add(kvs, L.kv("service", sourceLabel(s.src)), L.kv("ticket", s.ticket), L.kv("started", clock(s.first, true)),
      L.kv("duration", s.duration_ms === null ? null : ms(s.duration_ms)), L.kv("nodes run", String(s.nodes)),
      L.kv("cost", s.cost_usd === null ? null : usd(s.cost_usd)), L.kv("lines", String(P.run.lines.length)));
    add(E.run, head, kvs);
    if (P.run.truncated) add(E.run, el("p", "muted tiny", "Searched the last 20 MB of each log; an older part of this run may be missing."));
  }

  /* ── R4: lines over time ───────────────────────────────── */

  function renderChart(lines, now) {
    E.chart.textContent = "";
    // The whole range, with the filters; a clicked bucket is highlighted, not cut out.
    var st = P.st, span = RANGES[st.range], t0, t1, i, t;
    var pts = [];
    for (i = 0; i < lines.length; i++) {
      if (lines[i].divider || !matches(lines[i], st, now)) continue;
      t = tsMs(lines[i].ts);
      if (!isNaN(t)) pts.push([t, lines[i].level]);
    }
    if (!pts.length) { E.chart.hidden = true; return; }
    E.chart.hidden = false;
    t1 = span ? now : pts[pts.length - 1][0] + 1;
    t0 = span ? now - span : pts[0][0];
    for (i = 0; i < pts.length; i++) { if (pts[i][0] < t0) t0 = pts[i][0]; if (pts[i][0] >= t1) t1 = pts[i][0] + 1; }
    var n = 60, size = Math.max(1000, Math.ceil((t1 - t0) / n));
    var buckets = [];
    for (i = 0; i < n; i++) buckets.push({ DEBUG: 0, INFO: 0, WARN: 0, ERROR: 0, total: 0 });
    pts.forEach(function (p) {
      var b = buckets[Math.min(n - 1, Math.floor((p[0] - t0) / size))];
      b[p[1]] += 1; b.total += 1;
    });
    var peak = 0, days = n * size > 86400000;
    buckets.forEach(function (b) { if (b.total > peak) peak = b.total; });
    var bars = el("div", "lg-bars");
    buckets.forEach(function (b, k) {
      var from = t0 + k * size, to = from + size;
      var bar = el("button", "lg-bar" + (P.win && P.win.from === from ? " on" : ""));
      bar.type = "button";
      var label = stampAt(from, days) + "–" + clockAt(to) + ": " + b.total + " lines" +
        (b.ERROR ? ", " + b.ERROR + " error" : "") + (b.WARN ? ", " + b.WARN + " warn" : "");
      bar.title = label;
      bar.setAttribute("aria-label", label);
      if (!b.total) bar.disabled = true;
      ["DEBUG", "INFO", "WARN", "ERROR"].forEach(function (lv) {
        if (!b[lv]) return;
        var seg = el("span", "lg-seg-" + lv.toLowerCase());
        seg.style.height = (100 * b[lv] / peak) + "%";
        bar.appendChild(seg);
      });
      bar.onclick = function () {
        P.win = P.win && P.win.from === from ? null : { from: from, to: to };
        P.shown = PAGE_ROWS;
        render();
      };
      bars.appendChild(bar);
    });
    var axis = el("div", "lg-axis muted tiny");
    add(axis, el("span", null, stampAt(t0, days)), el("span", null, "peak " + peak + " per bar"), el("span", null, stampAt(t0 + n * size, days)));
    add(E.chart, bars, axis);
  }

  /* ── R11: facets ───────────────────────────────────────── */

  function facetGroup(title, counts, order, isOn, onClick, empty) {
    var box = el("div", "lg-facet");
    add(box, el("h3", "lg-facet-title", title));
    var keys = order || Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a] || (a < b ? -1 : 1); });
    if (!keys.length) add(box, el("p", "muted tiny", empty || "none"));
    keys.slice(0, 14).forEach(function (k) {
      var b = el("button", "lg-facet-item");
      b.type = "button";
      pressed(b, isOn(k));
      add(b, el("span", "lg-facet-name" + (title === "Level" ? " lv-text-" + k.toLowerCase() : ""), k),
        el("span", "lg-facet-n", (counts[k] || 0).toLocaleString()));
      b.onclick = function () { onClick(k); };
      box.appendChild(b);
    });
    return box;
  }

  function renderFacets(lines, now) {
    E.facets.textContent = "";
    var f = facets(lines, P.st, now), st = P.st;
    var svcBox = el("div", "lg-facet");
    add(svcBox, el("h3", "lg-facet-title", "Service"));
    P.sources.forEach(function (s) {
      var b = el("button", "lg-facet-item");
      b.type = "button";
      pressed(b, st.svc.indexOf(s.id) !== -1);
      add(b, el("span", "lg-facet-name", s.label), el("span", "lg-facet-n", s.exists ? (f.svc[s.id] || 0).toLocaleString() : "no log yet"));
      b.title = s.path;
      b.onclick = function () {
        var on = st.svc.indexOf(s.id) !== -1;
        var next = on ? st.svc.filter(function (x) { return x !== s.id; }) : st.svc.concat([s.id]);
        next = SOURCES.filter(function (x) { return next.indexOf(x) !== -1; });
        setState({ svc: next.length ? next : SOURCES.slice() });
      };
      svcBox.appendChild(b);
    });
    add(E.facets,
      svcBox,
      facetGroup("Level", f.level, LEVELS.slice().reverse(), function (k) { return RANK[k] >= RANK[st.level]; },
        function (k) { setState({ level: k }); }),
      facetGroup("Event", f.event, null, function (k) { return st.event === k; },
        function (k) { setState({ event: st.event === k ? null : k }); }),
      facetGroup("Error code", f.code, null, function (k) { return st.code === k; },
        function (k) { setState({ code: st.code === k ? null : k }); }, "no error codes in view"));
  }

  /* ── the table ─────────────────────────────────────────── */

  function ridTag(rid) {
    if (rid === "startup") return el("span", "chip lg-tag", "system");
    if (/^eval-/.test(rid || "")) return el("span", "chip lg-tag", "eval");
    return null;
  }

  function row(line) {
    var rec = line.rec || {}, open = !!P.open[line.id];
    if (line.divider) {
      var div = el("div", "lg-divider", sourceLabel(line.src) + " log was reset (cleared or deleted); reading it again from the start");
      div.setAttribute("role", "listitem");
      return div;
    }
    var wrap = el("div", "lg-item" + (P.fresh[line.id] ? " fresh" : "") + (open ? " open" : ""));
    wrap.setAttribute("role", "listitem");
    var r = el("div", "lg-row lv-row-" + line.level.toLowerCase());
    var exp = el("button", "lg-exp", open ? "▾" : "▸");
    exp.type = "button";
    exp.setAttribute("aria-expanded", open ? "true" : "false");
    exp.setAttribute("aria-label", (open ? "Hide" : "Show") + " the whole line");
    exp.onclick = function (e) { e.stopPropagation(); toggle(line.id); };
    var time = el("span", "lg-time mono", clock(line.ts, true));
    time.title = line.ts || "no ts on this line";
    var lv = el("span", "lv lv-" + line.level.toLowerCase(), line.level);
    var svc = el("button", "lg-cell lg-svc mono", sourceLabel(line.src));
    svc.type = "button";
    svc.title = "Show only " + sourceLabel(line.src);
    svc.onclick = function (e) { e.stopPropagation(); setState({ svc: [line.src] }); };
    var ridCell = el("span", "lg-rid");
    if (has(rec.request_id)) {
      var rb = el("button", "lg-cell mono", String(rec.request_id).slice(0, 8));
      rb.type = "button";
      rb.title = rec.request_id + " — every line of this run";
      rb.onclick = function (e) { e.stopPropagation(); setState({ rid: P.st.rid === rec.request_id ? null : rec.request_id }); };
      add(ridCell, rb, ridTag(rec.request_id));
    } else add(ridCell, el("span", "dim", "—"));
    var evCell;
    if (has(rec.event)) {
      evCell = el("button", "lg-cell lg-event mono", rec.event);
      evCell.type = "button";
      evCell.title = "Show only " + rec.event;
      evCell.onclick = function (e) { e.stopPropagation(); setState({ event: P.st.event === rec.event ? null : rec.event }); };
    } else evCell = el("span", "lg-event mono dim", has(rec.raw) ? "unparseable" : "—");
    var msg = el("span", "lg-msg", message(line));
    add(r, exp, time, lv, svc, ridCell, evCell, msg);
    r.onclick = function () { if (!window.getSelection || !String(window.getSelection())) toggle(line.id); };
    wrap.appendChild(r);
    if (open) wrap.appendChild(detail(line));
    return wrap;
  }

  // R3: the whole line, pretty-printed, with Copy.
  function detail(line) {
    var rec = line.rec || {};
    var text = has(rec.raw) && !rec.event ? rec.raw : JSON.stringify(rec, null, 2);
    var box = el("div", "lg-detail");
    var head = el("div", "lg-detail-head");
    add(head, el("span", "muted tiny mono", "line " + line.id + " · " + (sourceLabel(line.src)) + "/logs/runs.jsonl"), L.copyBtn(text));
    add(box, head, L.pre(text));
    return box;
  }

  function toggle(id) {
    if (P.open[id]) delete P.open[id]; else P.open[id] = true;
    render();
  }

  function renderTable(rows) {
    E.table.textContent = "";
    if (!P.loaded) { E.table.appendChild(el("p", "muted small pad", "Reading the logs…")); return; }
    if (!rows.length) {
      E.table.appendChild(el("p", "muted small pad", P.lines.length
        ? "No line matches these filters. DEBUG is hidden by default: set the level to DEBUG to see every node step."
        : "The logs are empty. Run a ticket on any checkpoint (or curl a service) and it shows up here within a few seconds."));
    }
    var head = el("div", "lg-row lg-head");
    head.setAttribute("aria-hidden", "true");
    add(head, el("span"), el("span", null, "time"), el("span", null, "level"), el("span", null, "service"),
      el("span", null, "request id"), el("span", null, "event"), el("span", null, "message"));
    if (rows.length) E.table.appendChild(head);
    var frag = document.createDocumentFragment();
    rows.slice(0, P.shown).forEach(function (l) { frag.appendChild(row(l)); });
    E.table.appendChild(frag);
    E.newPill.hidden = true;
    E.foot.textContent = "";
    if (rows.length > P.shown) {
      var more = btn("show " + Math.min(PAGE_ROWS, rows.length - P.shown) + " more (of " + (rows.length - P.shown).toLocaleString() + ")", "btn-ghost");
      more.onclick = function () { P.shown += PAGE_ROWS; render(); };
      E.foot.appendChild(more);
    }
    add(E.foot, el("span", null, P.dropped
      ? " The page keeps the newest " + MAX_LINES.toLocaleString() + " lines; " + P.dropped.toLocaleString() + " older ones were dropped. The files still have them."
      : " The first load reads the newest " + TAIL.toLocaleString() + " lines of each log. Older lines: the terminal command above."));
  }

  // The "N new lines" pill, while the reader is away from the top.
  function renderNewPill() {
    E.newPill.hidden = !P.unseen;
    E.newPill.textContent = P.unseen.toLocaleString() + " new line" + (P.unseen === 1 ? "" : "s") + " ↑";
  }

  /* ── show / hide (views.js) ────────────────────────────── */

  function show(hash) {
    build();
    P.st = fromHash(hash);
    P.visible = true;
    if (P.st.rid && P.runFor !== P.st.rid) { P.run = null; loadRun(P.st.rid); }
    if (!P.loaded) load();
    else { render(); if (P.st.live) poll(); }
  }

  function hide() {
    P.visible = false;
    schedule();
  }

  L.logs = {
    message: message, defaultState: defaultState, matches: matches, facets: facets,
    toHash: toHash, fromHash: fromHash, terminal: terminal, runSummary: runSummary, merge: merge,
    show: show, hide: hide, hash: function () { return toHash(P.st); }
  };
})();
