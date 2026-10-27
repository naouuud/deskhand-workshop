/* Deskhand Lab — core: shared state, DOM helpers, server calls, and the
   building blocks every view uses (headline, stepper, panel, section).

   The lab is a harness, not a reimplementation: every number on this page
   comes from a trace the server recorded around the REAL checkpoint code.
   These files only render those traces.

   Conventions: no build step, no npm; textContent for anything that came
   from a ticket or a model (some tickets are prompt-injection payloads);
   teaching notes (.why) and code links (.cites) are hidden until toggled. */

/* Load order (index.html): core.js first, because it defines window.L.
   Every later file reads L.S and the helpers below and hangs its own
   functions on L: setup.js (health, Setup, checkpoint cards), picker.js
   (sidebar), cp00.js / cp01.js / cpsvc.js (live view and final report per
   checkpoint), simple.js (the plain-words copy of every report), logs.js
   (the Logs view), orders.js (the Orders view), state.js (the Saved state
   view), cleanslate.js (the top bar's Clean slate), and views.js last (tabs,
   routing, boot). A file may call an L function that
   a later file defines, as long as the call happens after boot. */
window.L = (function () {
  "use strict";

  // The page's whole state, shared by every file as L.S. It lives in memory
  // only: a reload starts fresh (the toggles live in localStorage instead).
  //   tickets, incidents, checkpoints, cpById, maxRepeat: GET /api/tickets (picker.js)
  //   tag, search, sel: the sidebar filter and the selected ticket
  //     (sel.kind is "dataset", "incident" or "custom"; see ticketBody below)
  //   view: the visible tab. cp: Run's checkpoint, repCp: Repeat's, cmp: Compare's {id: on}
  //   health: the last GET /api/health (polled every 10s); starting: services mid-start
  //   spend, runCount: the session cost in the top bar
  //   runs: finished traces by lab run_id, so #run/<id> opens without a fetch
  //   busy: {run, compare, repeat, incident} while one is in flight; a second click does nothing
  //   cites: checkpoint 00's code links (Repeat's teaching note uses one)
  //   board: the incident board from GET /api/incidents
  var S = {
    tickets: [],
    incidents: [],
    checkpoints: [],
    cpById: {},
    tag: "all",
    search: "",
    sel: null,
    view: "run",
    cp: "00",
    repCp: "00",
    cmp: { "00": true, "02": true },
    health: null,
    starting: {},
    spend: 0,
    runCount: 0,
    runs: {},
    busy: {},
    maxRepeat: 20,
    cites: {},
    board: [],
  };

  /* ── tiny pub/sub ──────────────────────────────────────── */
  // Three events keep the files decoupled: "tickets" (picker.js, once
  // /api/tickets has loaded), "health" (setup.js, after every health poll
  // and around a service start) and "select" (picker.js, a ticket was picked).
  var listeners = {};
  function on(name, fn) {
    (listeners[name] = listeners[name] || []).push(fn);
  }
  function emit(name, arg) {
    (listeners[name] || []).forEach(function (fn) {
      fn(arg);
    });
  }

  /* ── DOM helpers ───────────────────────────────────────── */
  function $(id) {
    return document.getElementById(id);
  }

  // The safe way to put text on the page, used everywhere instead of
  // innerHTML. A string child becomes a text node, so markup inside a ticket
  // or a model reply shows as literal characters and never runs. Some
  // dataset tickets are prompt-injection payloads (T-1006, T-1018, T-1025),
  // and the model can echo them back. null, undefined and false children are
  // skipped, so callers can write `cond ? node : null` inline.
  // el(tag, className, ...children): children are text or nodes.
  function el(tag, className) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    for (var i = 2; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === undefined || child === null || child === false) continue;
      node.appendChild(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
    }
    return node;
  }

  // add(parent, ...children): the same rule on an existing node; returns
  // parent so calls nest. Children must be strings or nodes: unlike el(), a
  // number here throws, so format it first (num, usd, String).
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined || child === false) continue;
      parent.appendChild(
        typeof child === "string" ? document.createTextNode(child) : child,
      );
    }
    return parent;
  }

  // Formatters. Each prints "—" for a missing value, so a field the trace
  // lacks reads as a dash, never "undefined". usd() defaults to 4 decimals
  // because one run usually costs a fraction of a cent; money() is for order
  // totals and refunds.
  function usd(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return "$" + Number(n).toFixed(digits === undefined ? 4 : digits);
  }
  function money(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return (
      "$" +
      Number(n).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    );
  }
  function ms(n) {
    return n === null || n === undefined
      ? "—"
      : n >= 1000
        ? (n / 1000).toFixed(1) + "s"
        : n + "ms";
  }
  // A 00/01 trace's startup {wait_ms, warm}: "pre-loaded" when the lab's warm
  // pool had the runner ready, else how long the run waited for Python to
  // start and import the SDKs. Not part of the wall time.
  function startup(s) {
    if (!s || s.wait_ms === null || s.wait_ms === undefined) return "—";
    return s.warm ? "pre-loaded" : ms(s.wait_ms);
  }
  function num(n) {
    return n === null || n === undefined ? "—" : Number(n).toLocaleString();
  }
  function short(sha) {
    return sha ? sha.slice(0, 10) + "…" : "—";
  }
  function jsonish(v) {
    return v === undefined ? "—" : JSON.stringify(v);
  }
  function basename(p) {
    return String(p || "")
      .split(/[\\/]/)
      .pop();
  }
  function titleCase(s) {
    return String(s || "").replace(/_/g, " ");
  }

  function cpName(id) {
    var c = S.cpById[id];
    return c ? id + " · " + c.name : "checkpoint " + id;
  }

  // Static, authored HTML only (the teaching notes). Never pass data here.
  // panel({why}) and views.js's RUN_WHY end up here. The <p class="why"> is
  // hidden by lab.css unless <body> has show-why (the Teaching notes toggle).
  function why(html) {
    var p = el("p", "why small");
    p.innerHTML = html;
    return p;
  }

  // One code link. c comes from the server's anchor table ({file, line, text,
  // href}): the server finds each anchor by searching the checkpoint file for
  // a fixed string, so the line number follows the code when it moves. href
  // is a vscode://file/ URL that opens that line in VS Code. No line means the
  // anchor text is gone: the link is drawn dashed (.moved) and its tooltip
  // names the missing text. Hidden unless <body> has show-cites.
  function citeLink(c, label, inline) {
    if (!c) return null;
    var a = el(
      "a",
      "cite" + (c.line ? "" : " moved") + (inline ? " inline" : ""),
      label || basename(c.file) + (c.line ? ":" + c.line : ""),
    );
    a.href = c.href;
    a.title = c.line
      ? c.file + ":" + c.line + "\n" + c.text
      : "anchor not found in " +
        c.file +
        " — the code moved: " +
        (c.moved || "");
    return a;
  }

  // A row of code links, or null when none resolved (add() skips null).
  function citesRow(list) {
    var row = el("div", "cites");
    (list || []).forEach(function (c) {
      if (c) row.appendChild(citeLink(c));
    });
    return row.childNodes.length ? row : null;
  }

  // The standard evidence card. o: {title, sub?, chips?, cls?, cites?, why?}.
  // The caller fills .body; the code links and the teaching note always sit
  // at the bottom. The parts come back so a live view can update sub in place.
  function panel(o) {
    var root = el("div", "panel" + (o.cls ? " " + o.cls : ""));
    var head = el("div", "panel-head");
    var left = el("div");
    add(left, el("h3", null, o.title));
    if (o.sub) add(left, el("div", "panel-sub", o.sub));
    var right = el("div", "right");
    (o.chips || []).forEach(function (c) {
      right.appendChild(c);
    });
    add(head, left, right);
    var body = el("div");
    add(root, head, body);
    if (o.cites) add(root, citesRow(o.cites));
    if (o.why) add(root, why(o.why));
    return {
      root: root,
      body: body,
      head: head,
      right: right,
      sub: left.querySelector(".panel-sub"),
    };
  }

  // A <details> for raw material (events, log lines, state JSON, commands).
  // Closed by default, so the projected page stays headline-first.
  function section(title, open) {
    var root = el("details", "section");
    if (open) root.open = true;
    var body = el("div");
    add(root, el("summary", null, title), body);
    return { root: root, body: body };
  }

  function chip(text, cls, title) {
    var c = el("span", "chip" + (cls ? " " + cls : ""), text);
    if (title) c.title = title;
    return c;
  }

  function pill(text, tone) {
    return el("span", "pill" + (tone ? " pill-" + tone : ""), text);
  }

  function pre(text, cls) {
    return el("pre", "code" + (cls ? " " + cls : ""), text);
  }

  function kv(k, v, cls) {
    var w = el("div", "kv");
    add(w, el("span", "k", k));
    var val = el("span", "v" + (cls ? " " + cls : ""));
    if (v instanceof Node) val.appendChild(v);
    else val.textContent = v === null || v === undefined ? "—" : String(v);
    w.appendChild(val);
    return w;
  }

  function note(cls, text) {
    var n = el("div", "note-box " + (cls || ""));
    if (text instanceof Node) n.appendChild(text);
    else if (text !== undefined) n.textContent = text;
    return n;
  }

  // items: [{ok, text, detail?}]. ok true draws ✓, false ✗, null a neutral ·
  // for "not applicable". Falsy items are skipped, like el()'s children.
  function checklist(items) {
    var ul = el("ul", "checklist");
    items.forEach(function (it) {
      if (!it) return;
      var li = el("li");
      var mark = el(
        "span",
        "mark " + (it.ok === true ? "good" : it.ok === false ? "bad" : "muted"),
        it.ok === true ? "✓" : it.ok === false ? "✗" : "·",
      );
      var txt = el("span");
      if (it.text instanceof Node) txt.appendChild(it.text);
      else txt.textContent = it.text;
      if (it.detail) {
        var d = el("span", "checklist-detail");
        if (it.detail instanceof Node) d.appendChild(it.detail);
        else d.textContent = it.detail;
        txt.appendChild(d);
      }
      add(li, mark, txt);
      ul.appendChild(li);
    });
    return ul;
  }

  // navigator.clipboard needs a secure context; the hidden-textarea fallback
  // covers the rest. The button reads "copied" for a moment either way.
  function copyText(text, btn) {
    function done() {
      var old = btn.textContent;
      btn.textContent = "copied";
      setTimeout(function () {
        btn.textContent = old;
      }, 1200);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else fallback();
    function fallback() {
      var ta = el("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        done();
      } catch (e) {
        /* select-and-copy by hand */
      }
      document.body.removeChild(ta);
    }
  }

  function copyBtn(text) {
    var b = el("button", "btn btn-sm btn-ghost", "copy");
    b.type = "button";
    b.onclick = function () {
      copyText(text, b);
    };
    return b;
  }

  function cmdBlock(c) {
    var w = el("div", "cmd");
    var head = el("div", "cmd-head");
    add(head, el("span", "cmd-label", c.label), copyBtn(c.cmd));
    add(w, head, pre(c.cmd, "short"));
    if (c.note) add(w, el("div", "muted tiny", c.note));
    return w;
  }

  // The closing "Do the same in the terminal" section of a report. The
  // commands are built by the server for this ticket and run (trace.commands,
  // or /api/estimate's commands for a Repeat batch): [{label, cmd, note?}].
  function commandsSection(cmds, extraWhy) {
    var s = section("Do the same in the terminal");
    add(
      s.body,
      el(
        "p",
        "muted small",
        "PowerShell, from the repo root. Every command does what the lab just did; diffing checkpoints, pytest and grepping logs/runs.jsonl stay in the terminal on purpose.",
      ),
    );
    (cmds || []).forEach(function (c) {
      s.body.appendChild(cmdBlock(c));
    });
    if (extraWhy) add(s.body, why(extraWhy));
    return s.root;
  }

  // What a failed lab run shows instead of a report. opts.needs_service is
  // set when the server's error event said a checkpoint service is down:
  // the panel then offers to start it on the spot.
  function errorPanel(message, detail, opts) {
    opts = opts || {};
    var p = panel({
      title: "The lab could not finish this run",
      cls: "panel-bad",
    });
    add(p.body, el("p", null, message));
    if (detail) add(p.body, pre(detail));
    if (opts.needs_service) {
      var row = el("div", "approval-actions");
      var b = el(
        "button",
        "btn btn-primary",
        "Start checkpoint " + opts.needs_service + " now",
      );
      b.type = "button";
      b.onclick = function () {
        b.disabled = true;
        b.textContent = "starting…";
        L.startService(opts.needs_service)
          .then(function () {
            b.textContent = "started — press Run (or Follow) again";
          })
          .catch(function (e) {
            b.disabled = false;
            b.textContent = "start failed: " + e.message;
          });
      };
      add(row, b, el("span", "muted small", "or open Setup (top right)"));
      p.body.appendChild(row);
    }
    return p.root;
  }

  // FastAPI errors arrive as {detail: ...}: show the detail, or the raw body
  // when it is not JSON.
  function errText(t) {
    try {
      var j = JSON.parse(t);
      return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (e) {
      return t;
    }
  }

  /* ── headline + stepper ────────────────────────────────── */

  // One card at the top of every run: who/what, the verdict sentence, a few numbers.
  // o.tone (live, good, warn, bad) colours the card; o.pill is [text, tone];
  // o.stats is [[label, value, cls?], ...]. Live views update a stat by its
  // position (head.stats.children[i]), so keep each view's order fixed.
  function headline(o) {
    var root = el("div", "headline tone-" + (o.tone || "live"));
    var top = el("div", "headline-top");
    add(
      top,
      el(
        "span",
        null,
        "checkpoint " +
          o.checkpoint +
          (S.cpById[o.checkpoint] ? " · " + S.cpById[o.checkpoint].name : ""),
      ),
      el("span", "dim", "·"),
      el("span", null, o.label || ""),
      el("span", "dim", "·"),
      el("span", null, "lab run "),
      el("code", null, o.run_id || "…"),
    );
    var pillEl = o.pill ? pill(o.pill[0], o.pill[1]) : el("span");
    pillEl.style.marginLeft = "auto";
    top.appendChild(pillEl);
    var verdict = el("div", "verdict");
    if (o.verdict instanceof Node) verdict.appendChild(o.verdict);
    else verdict.textContent = o.verdict || "";
    var stats = el("div", "headline-stats");
    (o.stats || []).forEach(function (s) {
      var st = el("div", "stat");
      add(st, el("span", "k", s[0]));
      var v = el("span", "v" + (s[2] ? " " + s[2] : ""));
      if (s[1] instanceof Node) v.appendChild(s[1]);
      else
        v.textContent =
          s[1] === null || s[1] === undefined ? "—" : String(s[1]);
      st.appendChild(v);
      stats.appendChild(st);
    });
    add(root, top, verdict, stats);
    return {
      root: root,
      verdict: verdict,
      stats: stats,
      pill: pillEl,
      top: top,
    };
  }

  // Horizontal pipeline of steps. update(name, {state, sub}) relights a step.
  // steps: [{name, label?, state?, sub?, title?}]. The state is a CSS class
  // on the node: pending, active, done, warn, error, paused or skip.
  function stepper(steps) {
    var root = el("div", "stepper");
    var byName = {};
    steps.forEach(function (s, i) {
      if (i) root.appendChild(el("span", "arrow", "→"));
      var n = el("span", "stepnode " + (s.state || "pending"));
      if (s.title) n.title = s.title;
      add(
        n,
        el("span", "nm", s.label || s.name),
        el("span", "ms", s.sub || ""),
      );
      root.appendChild(n);
      byName[s.name] = n;
    });
    return {
      root: root,
      update: function (name, patch) {
        var n = byName[name];
        if (!n) return;
        if (patch.state) n.className = "stepnode " + patch.state;
        if (patch.sub !== undefined)
          n.querySelector(".ms").textContent = patch.sub;
        if (patch.label !== undefined)
          n.querySelector(".nm").textContent = patch.label;
        if (patch.title !== undefined) n.title = patch.title;
      },
    };
  }

  // trace.outcome -> [pill text, tone]. 00 ends parsed, gave_up, crashed,
  // parsed_non_object or not_called; 01 validated, failed or crashed; the
  // services 02–04 completed or failed. "failed" is warn, not bad: failing
  // safely with a coded error is the behaviour checkpoints 01+ teach.
  var OUTCOMES = {
    parsed: ["parsed, nothing checked", "warn"],
    gave_up: ["gave up", "bad"],
    crashed: ["crashed", "bad"],
    parsed_non_object: ["parsed (not an object)", "bad"],
    not_called: ["never ran", "bad"],
    validated: ["validated", "good"],
    failed: ["failed safely", "warn"],
    completed: ["completed", "good"],
  };
  // An input-guard stop is the right result, so it reads green. Checkpoint
  // ids are two-digit strings, so ">= '02'" compares them in order.
  function outcomeInfo(trace) {
    var m = OUTCOMES[trace.outcome] || [trace.outcome, ""];
    if (
      trace.checkpoint >= "02" &&
      trace.outcome === "failed" &&
      trace.summary &&
      trace.summary.error_code === "GUARDRAIL_INJECTION_SUSPECTED"
    ) {
      return ["stopped by the input guard", "good"];
    }
    return m;
  }
  function outcomePill(trace) {
    var m = outcomeInfo(trace);
    return pill(m[0], m[1]);
  }

  /* ── server calls ──────────────────────────────────────── */

  // Both reject with Error(detail) on a non-2xx answer, so a .catch can
  // show e.message as it is.
  function getJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok)
        return r.text().then(function (t) {
          throw new Error(errText(t));
        });
      return r.json();
    });
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      if (!r.ok)
        return r.text().then(function (t) {
          throw new Error(errText(t));
        });
      return r.json();
    });
  }

  /* The run protocol. POST /api/run (and /api/follow) answers with NDJSON:
     one JSON object per line, flushed as it happens, so the page draws the
     run while it is still going. Every object has a type:
       start             first line: lab run_id, label, ticket text, node order
       stdout            text the checkpoint printed (00's customer screen)
       attempt.started / .finished / .exception
                         00 and 01: one LLM call, recorded around the real code
       request_id        02–04: the id the service returned from POST /tickets
       sse               02–04: one server-sent event {seq, event, data},
                         e.g. node.started, approval.required, reply.delta
       approval.waiting  paused at the gate with nobody set to auto-decide:
                         the live views show Approve / Reject
       approval.decided  the lab answered the gate itself (auto-approve/reject)
       service.down, service.up
                         the service dropped mid-run; the server reconnects
                         with Last-Event-ID and the service replays what was missed
       lab               a note from the lab server itself
       error             the lab could not finish (may carry needs_service, request_id)
       trace             last line: the full record the final report renders
     The caller passes every event to two live views (detailed and simple).
     When the trace arrives, views.js replaces both with the final report. */

  // One run. Resolves with the final trace; onEvent sees every NDJSON event.
  // url: "/api/run" (default) or "/api/follow" (an existing request_id).
  function runStream(body, onEvent, url) {
    return fetch(url || "/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok)
        return r.text().then(function (t) {
          throw new Error(errText(t));
        });
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = "",
        trace = null,
        err = null;
      function handle(line) {
        if (!line.trim()) return;
        var ev;
        try {
          ev = JSON.parse(line);
        } catch (e) {
          return;
        }
        if (ev.type === "trace") trace = ev;
        if (ev.type === "error") err = ev;
        onEvent(ev);
      }
      // A network chunk can end in the middle of a line, or of a multi-byte
      // character ({stream: true} keeps those whole): only complete lines
      // are parsed, and the tail waits in buf for the next chunk.
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            handle(buf);
            if (trace) {
              remember(trace);
              return trace;
            }
            var e = new Error(
              err ? err.message : "the run ended without a trace",
            );
            e.detail = err ? err.detail : null;
            e.needs_service = err ? err.needs_service : null;
            e.request_id = err ? err.request_id : null; // a run the service still has: Follow can pick it up
            throw e;
          }
          buf += dec.decode(res.value, { stream: true });
          var lines = buf.split("\n");
          buf = lines.pop();
          lines.forEach(handle);
          return pump();
        });
      }
      return pump();
    });
  }

  // Every finished run streamed from this page lands here, Compare and Repeat
  // included: cache the trace for #run/<id> and add its cost to the top bar.
  // Reopening a run through GET /api/runs/<id> does not, so nothing counts twice.
  function remember(trace) {
    S.runs[trace.run_id] = trace;
    var cost = trace.summary && trace.summary.cost_usd;
    if (typeof cost === "number") S.spend += cost;
    S.runCount += 1;
    $("session-cost").textContent =
      usd(S.spend) +
      " · " +
      S.runCount +
      " run" +
      (S.runCount === 1 ? "" : "s");
  }

  // The ticket part of every request body, from the sidebar selection. The
  // server turns it into the ticket text and record (resolve_ticket in
  // server.py); the page never sends the dataset text itself.
  function ticketBody() {
    var s = S.sel;
    if (!s) return null;
    if (s.kind === "dataset") return { ticket_id: s.id };
    if (s.kind === "incident") return { incident: s.letter };
    var body = { text: s.text };
    if (s.from) body.customer_name = s.from; // 02–04 send it as customer_name; 00/01 ignore it
    return body;
  }

  /* ── confirm dialog (every multi-run action) ───────────── */

  // Compare and Repeat start several real, billed runs, so they ask first.
  // The range comes from POST /api/estimate: the cost observed on an earlier
  // run of this ticket (or checkpoint) when there is one, else a rough
  // token count. Resolves true or false; Esc on the <dialog> counts as No.
  function confirmSpend(title, checkpoints, runs) {
    var body = ticketBody();
    body.checkpoints = checkpoints;
    body.runs = runs;
    return postJSON("/api/estimate", body).then(function (est) {
      return new Promise(function (resolve) {
        var d = $("confirm");
        $("confirm-title").textContent = title;
        var box = $("confirm-body");
        box.textContent = "";
        var n = runs * checkpoints.length;
        add(
          box,
          el(
            "p",
            null,
            n +
              " real LLM run" +
              (n === 1 ? "" : "s") +
              " on " +
              (est.model || "the configured model") +
              ". A run can make several calls (00 retries up to 3×, 01 repairs once, 02–04 make 3 calls plus up to 2 repairs).",
          ),
        );
        var t = el("table", "tbl");
        var hr = el("tr");
        ["checkpoint", "per run", "basis"].forEach(function (h) {
          hr.appendChild(el("th", null, h));
        });
        t.appendChild(hr);
        est.rows.forEach(function (r) {
          var tr = el("tr");
          add(
            tr,
            el("td", "mono", r.checkpoint),
            el("td", "mono", usd(r.per_run_low) + " – " + usd(r.per_run_high)),
            el("td", "muted", r.basis),
          );
          t.appendChild(tr);
        });
        add(box, t);
        add(
          box,
          el(
            "p",
            "verdict small-verdict",
            "≈ " + usd(est.total_low) + " – " + usd(est.total_high) + " total",
          ),
        );
        if (!est.priced)
          add(
            box,
            el(
              "p",
              "warn small",
              "The model isn't in checkpoint 01's PRICE_TABLE, so these runs are priced at $0 — tokens are still real.",
            ),
          );
        function close(ok) {
          d.close();
          resolve(ok);
        }
        $("confirm-yes").onclick = function () {
          close(true);
        };
        $("confirm-no").onclick = function () {
          close(false);
        };
        d.oncancel = function () {
          resolve(false);
        };
        d.showModal();
      });
    });
  }

  /* ── toggles ───────────────────────────────────────────── */

  // ?notes=1&cites=1 in the URL switches a toggle on for that page load (handy
  // for a link you project in class); otherwise the last choice is remembered.
  // Each toggle is a class on <body> (show-why, show-cites) that lab.css keys
  // on, so flipping one re-renders nothing. A URL value applies to this load
  // and is not saved; a click saves to localStorage (lab.why, lab.cites).
  // localStorage can throw (private mode, blocked storage), hence the try
  // blocks: the page works without it.
  function initToggles() {
    var params = new URLSearchParams(location.search);
    function wire(id, cls, key, param) {
      var saved = null;
      try {
        saved = localStorage.getItem(key);
      } catch (e) {
        /* private mode */
      }
      var on = params.has(param) ? params.get(param) !== "0" : saved === "1";
      document.body.classList.toggle(cls, on);
      $(id).classList.toggle("on", on);
      $(id).setAttribute("aria-pressed", String(on));
      $(id).onclick = function () {
        var now = document.body.classList.toggle(cls);
        $(id).classList.toggle("on", now);
        $(id).setAttribute("aria-pressed", String(now));
        try {
          localStorage.setItem(key, now ? "1" : "0");
        } catch (e) {
          /* ignore */
        }
      };
    }
    wire("why-toggle", "show-why", "lab.why", "notes");
    wire("cites-toggle", "show-cites", "lab.cites", "cites");
    initTheme(params);
    trackTopbar();
  }

  // Theme: the <head> script already set html[data-theme] before first paint
  // (?theme= wins, then the saved choice, then the OS). The button flips it and
  // remembers the choice; with nothing saved, the page keeps following the OS.
  function initTheme(params) {
    var root = document.documentElement,
      btn = $("theme-toggle");
    function apply(theme) {
      root.dataset.theme = theme;
      var next = theme === "light" ? "dark" : "light";
      btn.setAttribute("aria-label", "Switch to " + next + " theme");
      btn.title = "Switch to " + next + " theme";
    }
    apply(root.dataset.theme === "light" ? "light" : "dark");
    btn.onclick = function () {
      var theme = root.dataset.theme === "light" ? "dark" : "light";
      apply(theme);
      try {
        localStorage.setItem("lab.theme", theme);
      } catch (e) {
        /* ignore */
      }
    };
    var mq =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: light)");
    if (mq && mq.addEventListener)
      mq.addEventListener("change", function (e) {
        var saved = null;
        try {
          saved = localStorage.getItem("lab.theme");
        } catch (err) {
          /* private mode */
        }
        if (!saved && !params.has("theme")) apply(e.matches ? "light" : "dark");
      });
  }

  // The sticky sidebar and the Setup popover sit under the top bar, which wraps
  // onto two rows on narrow screens: keep --topbar-h equal to its real height.
  function trackTopbar() {
    var bar = document.querySelector(".topbar");
    if (!bar || !window.ResizeObserver) return;
    new ResizeObserver(function () {
      document.documentElement.style.setProperty(
        "--topbar-h",
        bar.offsetHeight + "px",
      );
    }).observe(bar);
  }

  /* ── the approval gate, explained ──────────────────────────
     Why a service run (02/03/04) stopped for a person, and what each button
     does. The input is the approval.required payload, or the same fields
     read from the final state: {actions, refund_amount_usd,
     requested_refund_usd, order_total_usd, order_id, customer_match,
     confidence}. This describes the facts the checkpoint already decided on;
     it does not re-run rules.py. The $50 limit is rules.AUTO_APPROVE_LIMIT_USD. */
  var AUTO_APPROVE_LIMIT = 50;
  function isNum(n) {
    return typeof n === "number" && !isNaN(n);
  }

  // -> {reasons: [{tone, rule, text, detail}], approve, reject, over}.
  // The order of the checks matters: reasons[0] becomes the "Paused: ..."
  // headline in both live views. over is true when the ask is above the
  // order total (incident C), which the verdicts lead with.
  function gateCase(d) {
    d = d || {};
    var actions = d.actions || [],
      reasons = [];
    var refund = d.refund_amount_usd,
      asked = d.requested_refund_usd,
      total = d.order_total_usd;
    var refunds = actions.indexOf("issue_refund") !== -1,
      replaces = actions.indexOf("send_replacement") !== -1;
    if (isNum(asked) && isNum(total) && asked > total) {
      reasons.push({
        tone: "bad",
        rule: "rule 1",
        text:
          "The customer asked for " +
          money(asked) +
          " on an order worth " +
          money(total) +
          ".",
        detail:
          "That is " +
          money(asked - total) +
          " more than they paid" +
          (total > 0 && asked / total >= 2
            ? " (" + Math.round(asked / total) + "× the order)"
            : "") +
          ". A request above the order total goes to a person: the bot may not shrink it into an amount that passes the rules.",
      });
    }
    if (d.customer_match === false) {
      reasons.push({
        tone: "bad",
        rule: "rule 1",
        text: "The order belongs to someone else.",
        detail:
          "The name on the order does not match the customer who wrote in. No refund or replacement goes out on another person's order.",
      });
    }
    if (
      refunds &&
      isNum(refund) &&
      isNum(asked) &&
      Math.round(refund * 100) !== Math.round(asked * 100)
    ) {
      reasons.push({
        tone: "warn",
        rule: "rule 2",
        text:
          "The bot offers " +
          money(refund) +
          ", not the " +
          money(asked) +
          " the customer asked for.",
        detail:
          "Paying a different amount than was asked for is a negotiation, and a person signs negotiations.",
      });
    }
    if (refunds && isNum(refund) && refund > AUTO_APPROVE_LIMIT) {
      reasons.push({
        tone: "warn",
        rule: "rule 2",
        text:
          "The refund of " +
          money(refund) +
          " is above the " +
          money(AUTO_APPROVE_LIMIT) +
          " the bot may pay on its own.",
        detail:
          "Small refunds go straight through; anything larger waits for a person.",
      });
    }
    if (replaces) {
      reasons.push({
        tone: "warn",
        rule: "rule 2",
        text: "The plan ships a replacement.",
        detail:
          "A replacement sends out a product, so it always needs a person's OK.",
      });
    }
    if (isNum(d.confidence) && d.confidence < 0.5) {
      reasons.push({
        tone: "warn",
        rule: "rule 3",
        text:
          "The AI is unsure about this ticket (confidence " +
          Math.round(d.confidence * 100) +
          "%).",
        detail: "Below 50% the plan has to hand the ticket to a person.",
      });
    }
    if (!reasons.length) {
      reasons.push({
        tone: "info",
        rule: "the model's own flag",
        text: "No rule required a person here: the AI flagged the plan for review itself.",
        detail:
          "The rules only insist on a person for refunds above " +
          money(AUTO_APPROVE_LIMIT) +
          ", replacements, changed amounts and requests above the order total. This plan asked for a review on its own.",
      });
    }

    var does = [];
    if (refunds)
      does.push(
        "refund " +
          (isNum(refund) ? money(refund) : "an amount the plan did not set") +
          (d.order_id ? " on order " + d.order_id : ""),
      );
    if (replaces) does.push("ship a replacement");
    if (actions.indexOf("escalate_to_human") !== -1)
      does.push("hand the ticket to a support specialist");
    if (actions.indexOf("request_more_info") !== -1)
      does.push("ask the customer for more details");
    if (actions.indexOf("reply_only") !== -1)
      does.push("reply without any action");
    var approve =
      (does.length ? does.join(", then ") : "carry out the plan") +
      ", then write the customer's reply.";
    approve = approve.charAt(0).toUpperCase() + approve.slice(1);
    if (!refunds && !replaces)
      approve += " No money moves and nothing ships either way.";
    return {
      reasons: reasons,
      approve: approve,
      reject:
        "Nothing is carried out. The customer is told a specialist will follow up.",
      over: isNum(asked) && isNum(total) && asked > total,
    };
  }

  // The body of an approval card: why it stopped, what the customer wrote, the
  // AI's reasoning, and what each button does. o.rules adds the rule numbers
  // (detailed view); o.ticket is the customer's text. Ticket and rationale
  // are untrusted, so they only ever go in via textContent.
  function gateDetails(d, o) {
    o = o || {};
    var g = gateCase(d),
      box = el("div", "gate");
    add(box, el("div", "sub", "Why it stopped"));
    box.appendChild(
      checklist(
        g.reasons.map(function (r) {
          return {
            ok: r.tone === "info" ? null : false,
            text: r.text + (o.rules ? " (" + r.rule + ")" : ""),
            detail: r.detail,
          };
        }),
      ),
    );
    if (o.ticket)
      add(
        box,
        el("div", "sub", "What the customer wrote"),
        el("blockquote", "gate-quote", o.ticket),
      );
    if (d.rationale)
      add(
        box,
        el(
          "div",
          "sub",
          "The AI's reasoning" +
            (o.rules ? " (plan.rationale, never sent to the customer)" : ""),
        ),
        el("blockquote", "gate-quote", d.rationale),
      );
    var opts = el("dl", "gate-opts");
    add(
      opts,
      el("dt", "good", "Approve"),
      el("dd", null, g.approve),
      el("dt", "bad", "Reject"),
      el("dd", null, g.reject),
    );
    add(box, el("div", "sub", "What each button does"), opts);
    return box;
  }

  /* ── the planted refund-window contradiction ───────────────
     refund-policy#window says 14 days from delivery, returns-and-exchanges
     #returns-window says 30. They only disagree for an order delivered more
     than 14 and at most 30 days before the ticket. So "the model refunded
     anyway" is claimed only when both sections were retrieved, the plan
     refunds, and the order's delivered_at (from the order lookup) puts the
     ticket in that window. With no delivery date, the lab asks instead.
     kind: none | not_refunded | inside | outside | disputed | escalated | unknown */
  var WINDOW_SECTIONS = [
    "refund-policy#window",
    "returns-and-exchanges#returns-window",
  ];
  var WINDOW_DAYS = { refund: 14, returns: 30 };
  function windowCase(t) {
    var st = t.state || {},
      plan = st.resolution_plan || {};
    var ids = (st.policy_chunks || []).map(function (c) {
      return c.doc_id + "#" + c.section_id;
    });
    var both = WINDOW_SECTIONS.every(function (id) {
      return ids.indexOf(id) !== -1;
    });
    var actions = (plan.actions || []).map(function (a) {
      return a.type;
    });
    var refunded = actions.indexOf("issue_refund") !== -1,
      escalated = actions.indexOf("escalate_to_human") !== -1;
    var delivered = (st.order_data || {}).delivered_at,
      days = null;
    // Days are counted to when the run started, not to now, so a run
    // reopened later through #run/<id> keeps the same verdict.
    if (delivered) {
      var at = t.started_at ? t.started_at * 1000 : Date.now();
      days = (at - Date.parse(delivered)) / 86400000;
      if (isNaN(days)) days = null;
    }
    var kind;
    if (!both) kind = "none";
    else if (!refunded) kind = "not_refunded";
    else if (days !== null && days <= WINDOW_DAYS.refund) kind = "inside";
    else if (days !== null && days > WINDOW_DAYS.returns) kind = "outside";
    else if (escalated) kind = "escalated";
    else kind = days === null ? "unknown" : "disputed";
    return {
      kind: kind,
      both: both,
      refunded: refunded,
      escalated: escalated,
      days: days === null ? null : Math.floor(days),
    };
  }

  // The same fields as approval.required, read from a finished run's state.
  function gateInput(state) {
    var plan = (state && state.resolution_plan) || {},
      order = (state && state.order_data) || {};
    return {
      actions: (plan.actions || []).map(function (a) {
        return a.type;
      }),
      refund_amount_usd: plan.refund_amount_usd,
      requested_refund_usd: plan.requested_refund_usd,
      order_total_usd: order.total_usd,
      order_id: order.order_id,
      customer_match: state ? state.customer_match : null,
      confidence: plan.confidence,
      rationale: plan.rationale,
    };
  }

  // L's starting surface. Later files add their own entries (L.loadHealth,
  // L.report, L.render00, L.runOn, ...); each file's last lines list them.
  return {
    S: S,
    on: on,
    emit: emit,
    $: $,
    el: el,
    add: add,
    usd: usd,
    money: money,
    ms: ms,
    startup: startup,
    num: num,
    short: short,
    jsonish: jsonish,
    basename: basename,
    titleCase: titleCase,
    cpName: cpName,
    why: why,
    citeLink: citeLink,
    citesRow: citesRow,
    panel: panel,
    section: section,
    chip: chip,
    pill: pill,
    pre: pre,
    kv: kv,
    note: note,
    checklist: checklist,
    copyBtn: copyBtn,
    cmdBlock: cmdBlock,
    commandsSection: commandsSection,
    errorPanel: errorPanel,
    headline: headline,
    stepper: stepper,
    outcomeInfo: outcomeInfo,
    outcomePill: outcomePill,
    getJSON: getJSON,
    postJSON: postJSON,
    runStream: runStream,
    remember: remember,
    ticketBody: ticketBody,
    confirmSpend: confirmSpend,
    initToggles: initToggles,
    gateCase: gateCase,
    gateInput: gateInput,
    gateDetails: gateDetails,
    windowCase: windowCase,
    WINDOW_DAYS: WINDOW_DAYS,
  };
})();
