/* Deskhand console — a browser client for the checkpoint 02 HTTP contract.

   It calls exactly four endpoints and has no privileged access:
     GET  /tickets                      the picker
     POST /tickets                      start a run
     GET  /runs/{id}/events             the SSE stream
     POST /runs/{id}/approve            the human decision

   EventSource is the browser's native SSE client. It reconnects on its own and
   replays via Last-Event-ID, which is why the server persists every event
   before sending it. We must close() on a terminal event, otherwise the browser
   politely reconnects to a finished run forever.

   Dependency-free, same style as slides/deck.js. */
(function () {
  "use strict";

  // The node registry from nodes.py, in graph order. repair_plan and
  // handle_failure only light up when something goes wrong.
  var NODES = [
    "normalize_ticket",
    "triage",
    "fetch_order_data",
    "generate_resolution_plan",
    "validate_plan",
    "repair_plan",
    "approval_gate",
    "execute_actions",
    "draft_customer_reply",
    "finalize",
    "handle_failure"
  ];

  // The event contract from events.py.
  var EVENTS = [
    "run.status",
    "node.started",
    "node.completed",
    "approval.required",
    "reply.delta",
    "run.completed",
    "run.failed"
  ];

  // Both lists are hand-written copies of the server's contract.
  // tests/test_console.py checks them against nodes.NODES and events.py, so a
  // rename on the server fails a test instead of leaving a dead pill.

  // Nothing comes after these two. The server ends its side of the stream
  // too, but only the client can stop EventSource from reconnecting.
  var TERMINAL = { "run.completed": 1, "run.failed": 1 };

  // One run on screen at a time: start() closes the old stream first.
  var run = { id: null, source: null, startedAt: 0, timer: null, reply: "" };
  var tickets = [];
  var activeTag = "all";

  function $(id) { return document.getElementById(id); }

  // The one way this file puts text on the page: textContent, never
  // innerHTML. Ticket text, model output (plan, reply, rationale) and raw
  // event data can carry markup or prompt-injection payloads. textContent
  // shows them as characters, so nothing in them can run or reshape the page.
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /* ── ticket picker ─────────────────────────────────────── */

  function loadTickets() {
    fetch("/tickets")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tickets = data.tickets || [];
        $("ticket-count").textContent = tickets.length + " in dataset";
        renderTagFilters();
        renderTicketList();
      })
      .catch(function (err) {
        $("ticket-list").textContent = "Could not load /tickets — " + err.message;
      });
  }

  function renderTagFilters() {
    var counts = {};
    tickets.forEach(function (t) {
      (t.tags || []).forEach(function (tag) { counts[tag] = (counts[tag] || 0) + 1; });
    });
    var box = $("tag-filters");
    box.textContent = "";
    var names = ["all"].concat(Object.keys(counts).sort());
    names.forEach(function (name) {
      var label = name === "all" ? "all" : name.replace(/_/g, " ") + " " + counts[name];
      var btn = el("button", "tag" + (name === activeTag ? " on" : ""), label);
      btn.type = "button";
      btn.onclick = function () {
        activeTag = name;
        renderTagFilters();
        renderTicketList();
      };
      box.appendChild(btn);
    });
  }

  function renderTicketList() {
    var list = $("ticket-list");
    list.textContent = "";
    tickets
      .filter(function (t) {
        return activeTag === "all" || (t.tags || []).indexOf(activeTag) !== -1;
      })
      .forEach(function (t) {
        // textContent throughout: some of these tickets are prompt-injection
        // payloads, and the console is not where we start trusting input.
        var btn = el("button", "ticket");
        btn.type = "button";
        btn.dataset.ticketId = t.ticket_id;

        var top = el("div", "ticket-top");
        top.appendChild(el("span", "ticket-id", t.ticket_id));
        top.appendChild(el("span", "ticket-name", t.customer_name));
        btn.appendChild(top);

        btn.appendChild(el("div", "ticket-preview", t.preview));

        var tags = el("div", "ticket-tags");
        (t.tags || []).forEach(function (tag) {
          tags.appendChild(el("span", "chip t-" + tag, tag.replace(/_/g, " ")));
        });
        if (t.message_count > 1) {
          tags.appendChild(el("span", "chip", t.message_count + " msgs"));
        }
        if (!t.order_id) tags.appendChild(el("span", "chip", "no order ref"));
        btn.appendChild(tags);

        btn.onclick = function () { start({ ticket_id: t.ticket_id }, t.ticket_id); };
        list.appendChild(btn);
      });
  }

  /* ── starting a run ────────────────────────────────────── */

  // body is the POST /tickets contract: {ticket_id} from the picker or
  // {ticket_text} from the ad-hoc box. The reply is only a request_id; the
  // run's progress arrives on the stream opened next.
  function start(body, label) {
    stopStream();
    resetRunView(label);

    fetch("/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        // A 422 (bad body) or 404 (unknown ticket) shows its response body
        // in the failure panel.
        if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
        return r.json();
      })
      .then(function (data) {
        run.id = data.request_id;
        $("request-id").textContent = run.id;
        markSelected(body.ticket_id);
        openStream(run.id);
      })
      .catch(function (err) { showFailure("POST /tickets failed — " + err.message); });
  }

  function markSelected(ticketId) {
    var all = document.querySelectorAll(".ticket");
    for (var i = 0; i < all.length; i++) {
      all[i].classList.toggle("on", all[i].dataset.ticketId === ticketId);
    }
  }

  function resetRunView(label) {
    $("empty").hidden = true;
    $("run").hidden = false;
    $("approval").hidden = true;
    $("failure").hidden = true;
    $("note-wrap").hidden = true;
    $("approval-result").textContent = "";
    $("btn-approve").disabled = false;
    $("btn-reject").disabled = false;
    $("request-id").textContent = "…";
    $("run-ticket").textContent = label || "ad-hoc";
    $("events").textContent = "";
    $("plan").textContent = "waiting…";
    $("plan").classList.add("muted");
    $("reply").textContent = "waiting…";
    $("reply").classList.add("muted");
    $("reply").classList.remove("live");
    $("stat-cost").textContent = "$0.000000";
    run.reply = "";
    setStatus("queued");
    renderPipeline();

    // Wall-clock time seen by the browser, ticking every 100ms. Server-side
    // latency is per node, in node.completed.
    run.startedAt = Date.now();
    clearInterval(run.timer);
    run.timer = setInterval(function () {
      $("stat-elapsed").textContent = ((Date.now() - run.startedAt) / 1000).toFixed(1) + "s";
    }, 100);
  }

  /* ── the SSE stream ────────────────────────────────────── */

  // The run started before this stream opens, so its first events may
  // already be stored. That is fine: a first connect sends no Last-Event-ID,
  // and the server replays from the start.
  function openStream(requestId) {
    var source = new EventSource("/runs/" + requestId + "/events");
    run.source = source;

    source.onopen = function () { setConn("live", "streaming"); };

    source.onerror = function () {
      // readyState CONNECTING means the browser is retrying on its own — with
      // Last-Event-ID set, so nothing is lost. CLOSED means it gave up.
      if (source.readyState === EventSource.CONNECTING) {
        setConn("retry", "reconnecting…");
        logMeta("connection lost — browser retrying with Last-Event-ID");
      } else {
        setConn("closed", "disconnected");
      }
    };

    // Named SSE events need a listener each; onmessage only sees unnamed ones.
    EVENTS.forEach(function (name) {
      source.addEventListener(name, function (e) { onEvent(name, e); });
    });
  }

  function stopStream() {
    if (run.source) {
      run.source.close();
      run.source = null;
    }
    clearInterval(run.timer);
    setConn("idle", "idle");
  }

  function onEvent(name, e) {
    var data = {};
    try { data = JSON.parse(e.data); } catch (err) { /* keep the raw line below */ }

    logEvent(e.lastEventId, name, e.data);  // lastEventId: the server's per-run seq

    if (name === "run.status") setStatus(data.status);
    else if (name === "node.started") nodeStarted(data.node);
    else if (name === "node.completed") nodeCompleted(data);
    else if (name === "approval.required") showApproval(data);
    else if (name === "reply.delta") appendReply(data.text);
    else if (name === "run.completed") complete(data);
    else if (name === "run.failed") showFailure(data.user_visible_error);

    if (TERMINAL[name]) {
      // The terminal event is emitted from *inside* the last node, so that
      // node's own node.completed arrives after it — and we are about to stop
      // listening. Settle whatever is still mid-flight.
      settleActiveNodes(name === "run.failed");
      // Close first: an EventSource left open would reconnect to a finished run.
      stopStream();
      setConn("closed", "stream closed");
    }
  }

  /* ── rendering ─────────────────────────────────────────── */

  function renderPipeline() {
    var box = $("pipeline");
    box.textContent = "";
    NODES.forEach(function (name) {
      var node = el("span", "node");
      node.id = "node-" + name;
      node.appendChild(el("span", null, name));
      node.appendChild(el("span", "ms"));
      box.appendChild(node);
    });
  }

  function nodeStarted(name) {
    var node = $("node-" + name);
    if (!node) return;
    node.classList.remove("node-done");
    node.classList.add(name === "handle_failure" ? "node-error" : "node-active");
  }

  function nodeCompleted(data) {
    var node = $("node-" + data.node);
    if (!node) return;
    node.classList.remove("node-active");
    if (data.node !== "handle_failure") node.classList.add("node-done");
    node.querySelector(".ms").textContent = data.latency_ms + "ms";
    if (typeof data.cost_usd === "number") {
      $("stat-cost").textContent = "$" + data.cost_usd.toFixed(6);
    }
  }

  function settleActiveNodes(failed) {
    var active = document.querySelectorAll(".node-active");
    for (var i = 0; i < active.length; i++) {
      active[i].classList.remove("node-active");
      active[i].classList.add(failed ? "node-error" : "node-done");
    }
  }

  function setStatus(status) {
    var pill = $("status-pill");
    pill.textContent = String(status).replace(/_/g, " ");
    pill.className = "pill pill-" + status;
  }

  function setConn(kind, text) {
    $("conn").className = "conn conn-" + kind;
    $("conn-text").textContent = text;
  }

  // Only the customer reply streams token by token (events.py). The plan never
  // arrives in pieces; it shows up whole in run.completed. Setting textContent
  // replaces the pane's content, old caret included, so the caret is re-added.
  function appendReply(text) {
    var pane = $("reply");
    if (!run.reply) {
      pane.textContent = "";
      pane.classList.remove("muted");
      pane.classList.add("live");
    }
    run.reply += text;
    pane.textContent = run.reply;
    pane.appendChild(el("span", "caret", "▋"));
  }

  // The approval card, from the approval.required payload. Every value goes
  // through el() as text. The rationale is model output about a ticket that may
  // be an injection: it is evidence for the approver, never markup.
  function showApproval(plan) {
    var body = $("approval-body");
    body.textContent = "";

    function kv(key, value, cls) {
      var wrap = el("div", "kv");
      wrap.appendChild(el("span", "k", key));
      wrap.appendChild(el("span", "v" + (cls ? " " + cls : ""), value));
      body.appendChild(wrap);
    }

    kv("ticket", plan.ticket_id);
    if (plan.approval_reasons && plan.approval_reasons.length) {
      kv("why a person must sign", plan.approval_reasons.join("; "), "warn");
    }
    kv("category", plan.category);
    kv("actions", (plan.actions || []).join(", ") || "—");
    kv(
      "refund",
      plan.refund_amount_usd === null || plan.refund_amount_usd === undefined
        ? "—"
        : "$" + Number(plan.refund_amount_usd).toFixed(2),
      "money"
    );
    // The gap a reviewer must see: what was asked vs. what the plan gives,
    // what the order is worth, and whether the order is even theirs.
    var asked = plan.requested_refund_usd;
    if (asked !== null && asked !== undefined) {
      var given = plan.refund_amount_usd === null || plan.refund_amount_usd === undefined
        ? null : Number(plan.refund_amount_usd);
      var differs = given !== null && Math.abs(given - Number(asked)) >= 0.005;  // half a cent
      kv("customer asked for", "$" + Number(asked).toFixed(2), differs ? "money warn" : "money");
    }
    if (plan.order_total_usd !== null && plan.order_total_usd !== undefined) {
      kv("order total", "$" + Number(plan.order_total_usd).toFixed(2), "money");
    }
    // null means nobody vouched for the sender (free text without
    // customer_name). The row is left out; approval_reasons says so instead.
    if (plan.customer_match !== null && plan.customer_match !== undefined) {
      kv("order belongs to this customer", plan.customer_match ? "yes" : "NO",
         plan.customer_match ? "" : "warn");
    }
    kv("confidence", Number(plan.confidence).toFixed(2));
    if (plan.rationale) kv("model's rationale", plan.rationale, "prose");

    $("approval").hidden = false;
  }

  // Both buttons are disabled while the POST is in flight, so one click is one
  // decision; the server answers 409 for a run that is no longer waiting. No
  // new stream is opened: the stream stayed open through the pause, and the
  // resumed run's events arrive on it.
  function decide(approved) {
    $("btn-approve").disabled = true;
    $("btn-reject").disabled = true;
    $("approval-result").textContent = "sending…";

    fetch("/runs/" + run.id + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved: approved })
    })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(t); });
        return r.json();
      })
      .then(function (data) {
        $("approval-result").textContent = data.approval_status;
        logMeta("POST /approve → " + data.approval_status + " (run resumes)");
      })
      .catch(function (err) {
        $("approval-result").textContent = "failed: " + err.message;
        $("btn-approve").disabled = false;
        $("btn-reject").disabled = false;
      });
  }

  function complete(data) {
    $("approval").hidden = true;
    // The reply in run.completed replaces whatever the deltas built: the
    // deltas are a preview, the terminal event is the record.
    if (data.reply) {
      run.reply = data.reply;
      $("reply").textContent = data.reply;
      $("reply").classList.remove("muted");
      $("reply").classList.add("live");
    }
    if (data.plan) {
      $("plan").textContent = JSON.stringify(data.plan, null, 2);
      $("plan").classList.remove("muted");
    }
    // The internal note is not on the wire event — it lives in the persisted
    // state, which is the whole point of having a machine view.
    fetch("/runs/" + run.id)
      .then(function (r) { return r.json(); })
      .then(function (state) {
        if (state.internal_note) {
          $("note").textContent = state.internal_note;
          $("note-wrap").hidden = false;
        }
      })
      .catch(function () { /* the note is a nicety, not the lesson */ });
  }

  function showFailure(message) {
    $("failure-text").textContent = message || "unknown error";
    $("failure").hidden = false;
    $("empty").hidden = true;
    $("run").hidden = false;
  }

  /* ── raw event log ─────────────────────────────────────── */

  function logEvent(seq, name, raw) {
    var row = el("div", "ev kind-" + name.replace(/\./g, "-"));
    var head = el("div", "ev-head");
    head.appendChild(el("span", "ev-seq", seq ? "#" + seq : ""));
    head.appendChild(el("span", "ev-name", name));
    row.appendChild(head);
    row.appendChild(el("div", "ev-data", raw));
    appendLog(row);
  }

  function logMeta(text) {
    var row = el("div", "ev kind-meta");
    var head = el("div", "ev-head");
    head.appendChild(el("span", "ev-seq", ""));
    head.appendChild(el("span", "ev-name", text));
    row.appendChild(head);
    appendLog(row);
  }

  function appendLog(row) {
    var box = $("events");
    // Follow new rows only if the reader is already within 40px of the bottom,
    // so scrolling up to read an old event is not yanked away.
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.appendChild(row);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  /* ── wiring ────────────────────────────────────────────── */

  $("btn-approve").onclick = function () { decide(true); };
  $("btn-reject").onclick = function () { decide(false); };
  $("clear-events").onclick = function () { $("events").textContent = ""; };

  $("adhoc-submit").onclick = function () {
    var text = $("adhoc-text").value.trim();
    if (!text) return;
    markSelected(null);
    // No customer_name is sent, so nobody vouches for the sender: any refund
    // or replacement this text earns waits at the approval gate.
    start({ ticket_text: text }, "ad-hoc");
  };

  $("why-toggle").onclick = function () {
    var on = document.body.classList.toggle("show-why");
    $("why-toggle").classList.toggle("on", on);
  };

  // Closing the tab closes the stream; the run itself carries on server-side.
  window.addEventListener("beforeunload", stopStream);

  loadTickets();
})();
