/* Deskhand Lab — checkpoints 02, 03 and 04 (the services): live view and
   final report. The lab talks to each service over its own HTTP contract
   (POST /tickets, GET /runs/{id}/events, POST /approve, GET /runs/{id});
   the trace is that state plus the events and the log lines of the run. */

/* The lab server is the HTTP client; this page only sees what it relays.
   During a run each service SSE arrives as {type: "sse", seq, event, data}
   and drives the stepper (node.started / node.completed), the status pill
   (run.status), the approval card (approval.required, then the lab's
   approval.waiting) and the streaming reply (reply.delta).
   The final trace fields this file reads: state (GET /runs/{id}), events,
   log_lines, nodes (one row per node visit), node_order, summary, facts
   (what the server pulled out of events and logs: refunds, order,
   node_errors, plan_invalid, guard_input, guard_output), approval
   {required, decision, by}, cites and commands.

   One report serves 02, 03 and 04: policyPanel appears from 03, guardPanel
   on 04. The bottom half also draws the 03 retrieval tools (search, phrase
   distance, the PCA map). Customer text, plan rationale and retrieved
   policy text all go in via L.el as text.
   Reads from L: S and the DOM helpers, headline, stepper, panel, section,
   checklist, why, citesRow, gateCase, gateInput, gateDetails, windowCase,
   outcomeInfo, errorPanel, logTable, copyBtn, cmdBlock, chip, postJSON. */
(function () {
  "use strict";
  var S = L.S, el = L.el, add = L.add, kv = L.kv, pre = L.pre, usd = L.usd, money = L.money,
    ms = L.ms, panel = L.panel, note = L.note;

  // Short stepper labels for the node names in nodes.py.
  var SHORT = {
    normalize_ticket: "normalize", guard_input: "guard input", triage: "triage", fetch_order_data: "fetch order",
    retrieve_policy: "retrieve policy", generate_resolution_plan: "plan", validate_plan: "validate", repair_plan: "repair",
    approval_gate: "approval gate", execute_actions: "execute", draft_customer_reply: "draft reply", finalize: "finalize",
    handle_failure: "handle failure"
  };
  var ACTION_WORDS = {
    issue_refund: "refund", send_replacement: "replacement", escalate_to_human: "escalate to a human",
    request_more_info: "ask for more info", reply_only: "reply only"
  };
  // Who answered the gate: "you" (a click here) or the lab's auto policy.
  function byFor(approval) { return approval && approval.by ? (approval.by === "you" ? "by you" : "by the lab, automatically") : ""; }

  // The sentence the headline adds for the planted 14 vs 30 day contradiction
  // (L.windowCase decides which case this run is). Only "disputed" claims the
  // model ignored the conflict; "unknown" asks, the other cases add nothing.
  function windowSentence(t) {
    var w = L.windowCase(t);
    if (w.kind === "disputed") return " Delivered " + w.days + " days ago, where the 14-day and 30-day windows disagree: both sections were retrieved and the prompt says to escalate, but the model refunded anyway.";
    if (w.kind === "unknown") return " Both refund windows (14 and 30 days) were retrieved and the plan refunded: which window did it apply? The order has no delivery date to tell.";
    if (w.kind === "outside") return " Delivered " + w.days + " days ago, past both refund windows, and the plan refunds anyway.";
    return "";
  }

  function flagText(flags) { return (flags || []).map(function (f) { return f.label; }).join(", "); }

  /* ── verdict ── */
  // -> [sentence, tone]. A failed run is explained by its error code; a
  // completed one by what the plan did, with the 14 vs 30 day warning (03+)
  // and an output-guard swap (04) appended. Compare reuses it as L.svcVerdict.
  function verdict(t) {
    var s = t.summary, st = t.state, f = t.facts, a = t.approval;
    if (t.outcome !== "completed") {
      var code = s.error_code || "unknown";
      var errNode = (f.node_errors[0] || {}).node;
      if (code === "GUARDRAIL_INJECTION_SUSPECTED") return ["Stopped at the input guard: prompt injection suspected. No triage, no plan, nothing executed. The customer got a neutral routing message.", "good"];
      if (code === "KILL_SWITCH_ENGAGED") return ["Kill switch on (DESKHAND_KILL_SWITCH): the run stopped before " + (errNode || "its next node") + ", with no LLM or tool call there. " +
        (f.refunds.length ? "A refund had already gone out before the switch tripped." : "Nothing was refunded or shipped.") + " The ticket went to the human queue.", "warn"];
      if (code === "BUDGET_EXCEEDED") return ["Budget cap hit at " + usd(st.cost_usd) + ": the run failed fast with BUDGET_EXCEEDED instead of burning tokens.", "warn"];
      if (code === "REPAIR_EXHAUSTED") return ["The plan still broke business rules after " + st.repair_attempts + " repair" + (st.repair_attempts === 1 ? "" : "s") + ". Failed safely with REPAIR_EXHAUSTED; the customer got a safe message.", "warn"];
      var detail = (f.node_errors[0] || {}).detail || "";
      if (code === "INTERNAL_UNEXPECTED" && /already accessed by another instance of Qdrant/.test(detail)) {
        return ["Failed in retrieve_policy: another process has this checkpoint's policy index open (an eval run, a test, or a second service on the same folder). " +
          "Qdrant's local mode allows one process per folder. Wait for it to finish or stop it, then run again.", "bad"];
      }
      if (code === "INTERNAL_UNEXPECTED") return ["Failed with INTERNAL_UNEXPECTED in " + (errNode || "a node") + ": " + detail.slice(0, 160), "bad"];
      return ["Failed safely with " + code + (errNode ? " in " + errNode : "") + ". The customer got the taxonomy's message, the log has the detail.", "warn"];
    }
    var order = st.order_data || {}, plan = st.resolution_plan || {};
    // Lead with the ask when it is the whole story (incident C: $249.99 on a $24.99 order).
    var lead = L.gateCase(L.gateInput(st)).over ? "The customer asked for " + money(plan.requested_refund_usd) + " on an order worth " + money(order.total_usd) + ". " : "";
    if (a.required && a.decision === "rejected") {
      return [lead + "Plan rejected " + byFor(a) + ": nothing was executed. The customer was told a specialist will follow up.", "warn"];
    }
    var actions = s.actions || [], parts = [];
    if (actions.indexOf("issue_refund") !== -1) {
      var refunded = f.refunds.length ? f.refunds[0] : null;
      parts.push((refunded ? "Refund of " + money(refunded.amount_usd) + " issued" : "Refund of " + money(s.refund_amount) + " planned") +
        " on order " + (order.order_id || "?") + " (total " + money(order.total_usd) + ")" +
        (a.required ? ", approved " + byFor(a) : ", under the $50 auto-approve limit"));
    }
    if (actions.indexOf("send_replacement") !== -1) parts.push("replacement queued" + (a.required && actions.indexOf("issue_refund") === -1 ? ", approved " + byFor(a) : ""));
    if (actions.indexOf("escalate_to_human") !== -1) parts.push("escalated to a human");
    if (actions.indexOf("request_more_info") !== -1) parts.push("asked the customer for more information");
    if (actions.indexOf("reply_only") !== -1) parts.push("replied without any action");
    var text = parts.length ? parts.join("; ") : "Completed with no actions";
    text = lead + text.charAt(0).toUpperCase() + text.slice(1) + ".";
    var tone = "good";
    var win = t.checkpoint >= "03" ? windowSentence(t) : "";
    if (win) { text += win; tone = "warn"; }
    if (f.guard_output) { text += " The output guard replaced the drafted reply with the safe fallback."; tone = tone === "good" ? "warn" : tone; }
    if (s.citations && s.citations.length) text += " Cites " + s.citations.length + " policy section" + (s.citations.length === 1 ? "" : "s") + ".";
    return [text, tone];
  }

  /* ── pipeline stepper ── */
  // order: every node in graph order, which the server reads from the
  // checkpoint's nodes.py, so the stepper never drifts from the code.
  // rows: one entry per node visit {node, latency_ms, raised?}; a null
  // latency means still running. Unvisited nodes show as skipped, a node
  // visited more than once (the repair loop) as ×n. t is null while live.
  function pipelineSteps(order, rows, t) {
    var visits = {};
    rows.forEach(function (r) { visits[r.node] = (visits[r.node] || 0) + 1; });
    var raisedIn = rows.filter(function (r) { return r.raised; }).map(function (r) { return r.node; });
    return order.map(function (name) {
      var last = null;
      rows.forEach(function (r) { if (r.node === name) last = r; });
      var state = "skip", sub = "";
      if (last) {
        state = last.latency_ms === null ? "active" : "done";
        sub = last.latency_ms === null || last.latency_ms === undefined ? "" : ms(last.latency_ms);
        if (visits[name] > 1) sub = "×" + visits[name] + (sub ? " · " + sub : "");
        if (raisedIn.indexOf(name) !== -1) { state = "error"; sub = "raised"; }
        if (name === "handle_failure") state = "error";
        if (name === "approval_gate" && t && t.approval && t.approval.required) { state = "paused"; sub = t.approval.decision || "paused"; }
        if (name === "repair_plan" && state === "done") state = "warn";
      }
      return { name: name, label: SHORT[name] || name, state: state, sub: sub, title: name };
    });
  }

  /* ── live ── */
  // opts.followed: a request_id the lab attaches to instead of submitting a ticket.
  // Layout: headline, stepper, an approval slot that stays empty until the
  // run pauses, then the streaming reply beside the raw event log. Headline
  // stats by position: 0 status, 1 elapsed, 2 cost so far, 3 request id.
  function live(out, cp, opts) {
    opts = opts || {};
    out.textContent = "";
    var order = (S.cpById[cp] && S.cpById[cp].nodes) || [];
    var ticketText = opts.followed ? null : S.sel && S.sel.text;
    var head = L.headline({ run_id: "…", checkpoint: cp, label: opts.followed ? "request " + opts.followed : S.sel.label,
      verdict: opts.followed ? "Asking the checkpoint " + cp + " service for request " + opts.followed + "…" : "Submitting the ticket to the checkpoint " + cp + " service…", tone: "live",
      pill: ["queued", "live"], stats: [["status", "queued"], ["elapsed", "0.0s"], ["cost so far", "$0.0000"], ["request id", "…"]] });
    var step = L.stepper(pipelineSteps(order, [], null));
    var approvalBox = el("div");
    var cols = el("div", "columns");
    var c1 = el("div", "col"), c2 = el("div", "col");
    var p1 = panel({ title: "Customer reply", sub: "the only thing that streams token by token" });
    var reply = el("div", "reply muted", "waiting for draft_customer_reply…");
    p1.body.appendChild(reply);
    var p2 = panel({ title: "Live events", sub: "raw server-sent events, as curl would print them" });
    var evBox = el("div", "events");
    p2.body.appendChild(evBox);
    add(c1, p1.root);
    add(c2, p2.root);
    add(cols, c1, c2);
    add(out, head.root, step.root, approvalBox, cols);

    // lastApproval: the approval.required payload, kept until the lab says
    // the run is waiting for a person. beforeDown: the headline's look before
    // a service.down, restored on service.up.
    var rows = [], replyText = "", runId = null, t0 = Date.now(), done = false, lastApproval = null, gateButtons = null, beforeDown = null;
    function setTone(pillText, tone) {
      head.pill.textContent = pillText;
      head.pill.className = "pill pill-" + tone;
      head.root.className = "headline tone-" + tone;
    }
    var timer = setInterval(function () { if (!done) stat(1, ((Date.now() - t0) / 1000).toFixed(1) + "s"); }, 100);
    function stat(i, v) { head.stats.children[i].querySelector(".v").textContent = v; }
    // One row in the event log. It only follows the newest row when the
    // view is already near the bottom, so scrolling back to read stays put.
    function logEv(seq, name, data, cls) {
      var row = el("div", "ev kind-" + String(name).replace(/\./g, "-") + (cls ? " " + cls : ""));
      add(row, el("span", "seq", seq ? "#" + seq : ""), el("span", "name", name), el("span", "data", typeof data === "string" ? data : JSON.stringify(data)));
      var atBottom = evBox.scrollHeight - evBox.scrollTop - evBox.clientHeight < 40;
      evBox.appendChild(row);
      if (atBottom) evBox.scrollTop = evBox.scrollHeight;
    }
    function redraw() { var fresh = L.stepper(pipelineSteps(order, rows, null)); step.root.replaceWith(fresh.root); step = fresh; }
    // The approval card. A click posts to the lab (/api/runs/<lab run_id>/approve),
    // which forwards it to the service's POST /runs/{id}/approve; the run then
    // resumes on the same event stream. plan is the approval.required payload.
    function showApproval(plan) {
      approvalBox.textContent = "";
      var p = panel({ title: "The run is paused: a human has to decide", sub: "nothing is refunded or shipped until you click", cls: "panel-warn",
        why: "The run is <em>not</em> sitting in memory waiting: its state is saved, and the decision restarts it at <code>execute_actions</code> (approve) or <code>draft_customer_reply</code> (reject). Refunds carry an idempotency key, so a double-click cannot pay twice." });
      var over = L.gateCase(plan).over;
      var k = el("div", "kvs");
      add(k, kv("customer asked for", plan.requested_refund_usd === null || plan.requested_refund_usd === undefined ? "no amount" : money(plan.requested_refund_usd), "big" + (over ? " bad" : "")),
        kv("order total", plan.order_total_usd === null || plan.order_total_usd === undefined ? "no order found" : money(plan.order_total_usd), "big"),
        kv("plan refunds", plan.refund_amount_usd === null || plan.refund_amount_usd === undefined ? "nothing" : money(plan.refund_amount_usd), "big"),
        kv("actions", (plan.actions || []).map(function (a) { return ACTION_WORDS[a] || a; }).join(", ") || "—"),
        kv("category", plan.category),
        kv("order owner", plan.customer_match === true ? "the customer" : plan.customer_match === false ? "someone else" : "unknown", plan.customer_match === false ? "bad" : ""),
        kv("confidence", plan.confidence === undefined ? "—" : Number(plan.confidence).toFixed(2), plan.confidence < 0.5 ? "warn" : ""));
      p.body.appendChild(k);
      p.body.appendChild(L.gateDetails(plan, { rules: true, ticket: ticketText }));
      var row = el("div", "approval-actions");
      var ok = el("button", "btn btn-good", "Approve"), no = el("button", "btn btn-bad", "Reject"), res = el("span", "muted small");
      ok.type = no.type = "button";
      gateButtons = { ok: ok, no: no, res: res };
      function decide(approved) {
        ok.disabled = no.disabled = true;
        res.textContent = "sending…";
        L.postJSON("/api/runs/" + runId + "/approve", { approved: approved }).then(function (r) {
          res.textContent = r.approval_status + " — the run resumes";
          logEv("", "lab", "POST /approve → " + r.approval_status, "kind-lab");
        }).catch(function (e) { res.textContent = "failed: " + e.message; ok.disabled = no.disabled = false; });
      }
      ok.onclick = function () { decide(true); };
      no.onclick = function () { decide(false); };
      add(row, ok, no, res);
      p.body.appendChild(row);
      approvalBox.appendChild(p.root);
    }
    return {
      onEvent: function (ev) {
        if (ev.type === "start") {
          runId = ev.run_id; head.top.querySelector("code").textContent = ev.run_id;
          if (ev.followed) { ticketText = ev.ticket_text; head.top.children[2].textContent = ev.label; }
        }
        else if (ev.type === "request_id") {
          stat(3, ev.request_id);
          if (ev.followed) {
            head.verdict.textContent = "Following request " + ev.request_id + " (" + String(ev.status || "?").replace(/_/g, " ") + "). Replaying its events from #1…";
            logEv("", "lab", "GET " + ev.base_url + "/runs/" + ev.request_id + " → " + ev.status + "; following its events from 0", "kind-lab");
          } else {
            head.verdict.textContent = "Accepted as request " + ev.request_id + ". Watching the event stream…";
            logEv("", "lab", "POST " + ev.submitted.url + " → request_id " + ev.request_id, "kind-lab");
          }
        }
        // The service dropped (crash flag, Stop, restart). The server keeps
        // retrying with Last-Event-ID; the run is saved in the service's
        // database, so it carries on from the next event once it is back.
        else if (ev.type === "service.down") {
          beforeDown = { pill: head.pill.textContent, pillCls: head.pill.className, root: head.root.className };
          setTone("service down · reconnecting", "warn");
          stat(0, "service down");
          head.verdict.textContent = "The checkpoint " + cp + " service stopped answering (it crashed, was stopped, or is restarting). The run is saved in its database. " +
            "The lab keeps retrying for " + ev.retry_s + "s: start the service again (Setup) and the run carries on from event #" + (ev.last_seq + 1) + ".";
          logEv("", "lab", "connection lost (" + ev.detail + "); retrying for " + ev.retry_s + "s with Last-Event-ID: " + ev.last_seq, "kind-lab");
        }
        else if (ev.type === "service.up") {
          if (beforeDown) { head.pill.textContent = beforeDown.pill; head.pill.className = beforeDown.pillCls; head.root.className = beforeDown.root; }
          stat(0, "running");
          head.verdict.textContent = "The service is back. Carrying on from event #" + (ev.last_seq + 1) + "…";
          logEv("", "lab", ev.text, "kind-lab");
        }
        else if (ev.type === "sse") {
          var d = ev.data || {};
          logEv(ev.seq, ev.event, d);
          if (ev.event === "node.started") { rows.push({ node: d.node, latency_ms: null }); redraw(); head.verdict.textContent = "Running " + d.node + "…"; }
          else if (ev.event === "node.completed") {
            var r = null; rows.forEach(function (x) { if (x.node === d.node && x.latency_ms === null) r = x; });
            if (r) r.latency_ms = d.latency_ms; else rows.push({ node: d.node, latency_ms: d.latency_ms });
            if (typeof d.cost_usd === "number") stat(2, usd(d.cost_usd));
            redraw();
          }
          else if (ev.event === "run.status") {
            stat(0, String(d.status).replace(/_/g, " ")); head.pill.textContent = String(d.status).replace(/_/g, " ");
            if (d.status === "awaiting_approval") { head.pill.className = "pill pill-warn"; head.root.className = "headline tone-warn"; }
            else if (gateButtons && !gateButtons.ok.disabled) {  // answered elsewhere (curl, another tab)
              gateButtons.ok.disabled = gateButtons.no.disabled = true;
              gateButtons.res.textContent = "decided elsewhere — the run moved on";
            }
          }
          else if (ev.event === "approval.required") { lastApproval = d; var why0 = L.gateCase(d).reasons[0].text; head.verdict.textContent = "Paused at the approval gate: " + why0.charAt(0).toLowerCase() + why0.slice(1, -1) + ". A human decides."; }
          // Model text: rebuilt with textContent on every delta, never HTML.
          else if (ev.event === "reply.delta") {
            if (!replyText) { reply.textContent = ""; reply.classList.remove("muted"); }
            replyText += d.text || "";
            reply.textContent = replyText;
            reply.appendChild(el("span", "caret", "▋"));
          }
          else if (ev.event === "run.completed" || ev.event === "run.failed") { done = true; head.verdict.textContent = "Finished. Building the report…"; }
        }
        else if (ev.type === "approval.waiting") showApproval(lastApproval || {});
        else if (ev.type === "approval.decided") { logEv("", "lab", "approval " + (ev.approved ? "approved" : "rejected") + " by " + ev.by, "kind-lab"); head.verdict.textContent = "Decision sent (" + (ev.approved ? "approved" : "rejected") + ", " + ev.by + "). Resuming…"; }
        else if (ev.type === "lab") logEv("", "lab", ev.text, "kind-lab");
        else if (ev.type === "error") { clearInterval(timer); add(out, L.errorPanel(ev.message, ev.detail, { needs_service: ev.needs_service })); }
        else if (ev.type === "trace") clearInterval(timer);
      }
    };
  }

  /* ── final ── */
  // Headline and stepper, then two columns: what the customer gets and the
  // plan on the left; money, policy (03+), guardrails (04) and triage on the
  // right. Raw material goes last, in closed sections: events, log lines,
  // persisted state, terminal commands.
  function render(t, out) {
    out.textContent = "";
    var c = t.cites || {}, s = t.summary, st = t.state;
    var v = verdict(t);
    add(out, L.headline({
      run_id: t.run_id, checkpoint: t.checkpoint, label: t.label, verdict: v[0], tone: v[1], pill: L.outcomeInfo(t),
      stats: [["LLM calls", s.attempts], ["cost", usd(st.cost_usd)], ["wall time", ms(t.duration_ms)], ["nodes run", t.nodes.length],
        ["request id", t.request_id], ["log lines", (t.log_lines || []).length], t.runtime ? ["runtime", t.runtime] : null,
        t.service_flags && t.service_flags.length ? ["service flags", flagText(t.service_flags), "warn"] : null,
        t.followed ? ["followed", "attached by request id"] : null].filter(Boolean)
    }).root);
    add(out, L.stepper(pipelineSteps(t.node_order, t.nodes, t)).root);

    var cols = el("div", "columns");
    var c1 = el("div", "col"), c2 = el("div", "col");
    add(c1, replyPanel(t, c));
    add(c1, planPanel(t, c));
    add(c2, moneyPanel(t, c));
    if (t.checkpoint >= "03") add(c2, policyPanel(t, c));
    if (t.checkpoint === "04") add(c2, guardPanel(t, c));
    add(c2, triagePanel(t, c));
    add(cols, c1, c2);
    add(out, cols);

    add(out, eventsSection(t, c));
    add(out, logSection(t, c));
    add(out, stateSection(t));
    add(out, L.commandsSection(t.commands));
  }

  // The reply as saved in state, or, for a failed run, the safe message the
  // customer got, with the internal code and detail underneath.
  function replyPanel(t, c) {
    var st = t.state, f = t.facts;
    var failed = t.outcome !== "completed";
    var p = panel({
      title: "What the customer gets", sub: failed ? "a safe message chosen by error code, never the exception" : "drafted from the validated plan; the only output that streamed",
      cls: failed ? "panel-warn" : "", cites: failed ? [c.handle_failure, c.user_messages, c.user_message_for] : [c.draft_reply, c.reply_delta, c.events_contract, c.rejected_reply],
      why: failed ? "<code>handle_failure</code> picks <code>USER_MESSAGES[internal_error_code]</code>: deliberately boring and honest. The internal code and detail are in the log line below, under the request id. Compare 00, where the raw stream and “hmm, something went wrong” reached the customer."
        : "<code>reply.delta</code> is the <em>only</em> event that streams partially. Prose can arrive a word at a time; the plan arrives whole in <code>run.completed</code> or not at all. That retires incident #3. The draft is written <em>from</em> the plan, so it cannot promise what the plan doesn't do" + (t.checkpoint === "04" ? "; checkpoint 04's output guard checks that too." : ".")
    });
    if (failed) {
      add(p.body, el("div", "reply", st.user_visible_error || "(no message)"));
      var err = f.node_errors[0];
      if (err) add(p.body, el("div", "muted small", "Internal code " + (err.internal_error_code || st.internal_error_code) + " raised in " + err.node + ": " + (err.detail || "").slice(0, 300)));
    } else {
      add(p.body, el("div", "reply", st.customer_reply_draft || "(no reply drafted)"));
      if (f.guard_output) add(p.body, note("warn", "Output guard: the model's draft was replaced by the safe fallback because " + f.guard_output.violations.join("; ") + "."));
      if (st.approval_status === "rejected") add(p.body, el("div", "muted small", "The plan was rejected, so the reply promises nothing and hands the ticket to a specialist."));
    }
    return p.root;
  }

  // The ResolutionPlan, then one line per rejected attempt (facts.plan_invalid
  // holds each plan.invalid log line with its rule violations), and the
  // rationale, which the customer never sees.
  function planPanel(t, c) {
    var st = t.state, f = t.facts, plan = st.resolution_plan;
    var p = panel({
      title: "The plan", sub: plan ? "a ResolutionPlan: machine output, validated by schema and business rules" : "no plan was produced",
      cls: plan && !(st.validation_errors || []).length ? "panel-good" : "",
      cites: [c.generate_plan, c.plan_prompt, c.validate_plan, c.rule1, c.rule2, c.rule3, c.rule4, c.rule5, c.repair_plan, c.max_repair],
      why: "Pydantic proves the plan is well-formed; <code>rules.check_plan</code> proves it is <em>allowed</em>: no refund without order data or above the total (rule 1), approval above $50 or for replacements (rule 2), " +
        "escalate below 0.5 confidence (rule 3), consistent actions (rule 4)" + (t.checkpoint >= "03" ? ", citations for refunds and replacements, from the retrieved set only (rule 5)" : "") +
        ". A violation goes back to the model with the reasons, at most twice, then <code>REPAIR_EXHAUSTED</code>. Compare 00, where the plan was never checked at all."
    });
    if (!plan) {
      add(p.body, el("div", "muted small", "The run failed before generate_resolution_plan."));
      return p.root;
    }
    var k = el("div", "kvs");
    var asked = plan.requested_refund_usd, total = (st.order_data || {}).total_usd;
    add(k, kv("category", plan.category, "big"), kv("actions", (plan.actions || []).map(function (a) { return a.type; }).join(", ") || "—", "big"),
      kv("customer asked for", asked === null || asked === undefined ? "—" : money(asked), "big" + (typeof total === "number" && asked > total ? " bad" : "")),
      kv("refund", plan.refund_amount_usd === null ? "—" : money(plan.refund_amount_usd), "big"),
      kv("needs approval", plan.requires_approval ? "yes" : "no"), kv("confidence", Number(plan.confidence).toFixed(2), plan.confidence < 0.5 ? "warn" : ""));
    p.body.appendChild(k);
    var items = [];
    var repairs = f.plan_invalid || [];
    if (repairs.length) {
      repairs.forEach(function (r, i) {
        items.push({ ok: false, text: "attempt " + (i + 1) + " broke " + r.violations.length + " rule" + (r.violations.length === 1 ? "" : "s") + ": " + r.violations.join("; ") });
      });
      if (!(st.validation_errors || []).length) items.push({ ok: true, text: "attempt " + (repairs.length + 1) + " passed every rule after the repair re-prompt" });
      else items.push({ ok: false, text: "still invalid after " + st.repair_attempts + " repairs → REPAIR_EXHAUSTED" });
    } else {
      items.push({ ok: true, text: "passed every business rule on the first attempt" });
    }
    if (t.checkpoint >= "03") {
      var needs = (plan.actions || []).some(function (a) { return a.type === "issue_refund" || a.type === "send_replacement"; });
      items.push({ ok: (plan.citations || []).length ? true : (needs ? false : null), text: (plan.citations || []).length ? "cites " + plan.citations.join(", ") : "no citations" });
    }
    p.body.appendChild(L.checklist(items));
    if (plan.rationale) add(p.body, el("div", "sub", "the AI's reasoning (plan.rationale, never sent to the customer)"), el("blockquote", "gate-quote", plan.rationale));
    var det = el("details");
    add(det, el("summary", null, "the full plan as JSON"), pre(JSON.stringify(plan, null, 2), "short"));
    p.body.appendChild(det);
    return p.root;
  }

  // A checklist from the order lookup to the refund: what the tools
  // returned, which rules and gates applied, and the refunds that went out
  // with their idempotency keys. The rules themselves ran in the checkpoint;
  // this only lays out the facts it recorded.
  function moneyPanel(t, c) {
    var st = t.state, f = t.facts, a = t.approval, plan = st.resolution_plan || {};
    var order = st.order_data;
    var refundPlanned = (plan.actions || []).some(function (x) { return x.type === "issue_refund"; });
    var p = panel({
      title: "The order and the money", sub: "what the tools did, and what the gates allowed",
      cls: f.refunds.length ? "panel-good" : "",
      cites: [c.extract_order_id, c.lookup_order, c.retry_policy, c.fetch_order, c.order_not_found, c.rule1, c.auto_approve_limit, c.approval_gate, c.pause, c.interrupt, c.execute_actions, c.issue_refund, c.idempotency_key],
      why: "<code>lookup_order</code> and <code>issue_refund</code> are LangChain tools with a validated argument schema and a failure model (5s timeout, two retries, taxonomy codes); the nodes call them, the model never does. " +
        "Rule 1 caps any refund at the order total; the approval gate pauses the run above $50 or for replacements; every refund carries <code>Idempotency-Key = request_id:index</code>, so a crash-and-resume cannot pay twice (incidents #5, #6, #9)."
    });
    var items = [];
    var orderEvent = (f.order || {}).event;
    if (orderEvent === "order.fetched" && order) items.push({ ok: true, text: "order " + order.order_id + " looked up: total " + money(order.total_usd) + (order.items ? " · " + order.items.map(function (i) { return i.name || i; }).join(", ") : "") + (order.delivered_at ? " · delivered " + String(order.delivered_at).slice(0, 10) : "") });
    else if (orderEvent === "order.not_found") items.push({ ok: false, text: "order " + (f.order.order_id || "?") + " not found (404) → the plan may not refund (rule 1)" });
    else if (orderEvent === "order.no_reference") items.push({ ok: null, text: "no order id in the ticket → no refund possible (rule 1)" });
    else if (t.nodes.some(function (n) { return n.node === "fetch_order_data"; })) items.push({ ok: null, text: "order lookup ran" });
    else items.push({ ok: null, text: "the run stopped before the order lookup" });
    var gate = L.gateCase(L.gateInput(st));
    var writes = refundPlanned || (plan.actions || []).some(function (x) { return x.type === "send_replacement"; });
    if (gate.over) {
      items.push({ ok: !refundPlanned, text: "customer asked for " + money(plan.requested_refund_usd) + ", more than the order total " + money(order.total_usd) + " → " +
        (refundPlanned ? "the plan refunds anyway" : "no refund; the plan escalates") + " (rule 1)",
        detail: "A request above the total must go to a person. Capping it to the total and refunding would hide the ask from the approver." });
    }
    if (st.customer_match === false) {
      items.push({ ok: !writes, text: "order " + (order ? order.order_id : "?") + " belongs to " + (order && order.customer_name ? order.customer_name : "another customer") + " → " + (writes ? "the plan still writes to it" : "no refund or replacement") + " (rule 1)" });
    }
    if (refundPlanned) {
      items.push({ ok: !!order && plan.refund_amount_usd <= order.total_usd, text: "refund " + money(plan.refund_amount_usd) + " checked against the order total " + money(order ? order.total_usd : null) });
    }
    if (a.required) {
      items.push({ ok: a.decision === "approved" ? true : a.decision === "rejected" ? false : null,
        text: "approval gate: paused → " + (a.decision || "undecided") + " " + byFor(a),
        detail: (gate.over ? gate.reasons.slice(1) : gate.reasons).length ? (gate.over ? "Also: " : "Why it paused: ") +
          (gate.over ? gate.reasons.slice(1) : gate.reasons).map(function (r) { return r.text.replace(/\.$/, "") + " (" + r.rule + ")"; }).join("; ") + "." : null });
    } else if (plan.actions) {
      items.push({ ok: null, text: "approval gate: not needed (refund ≤ $50, no replacement)" });
    }
    if (f.refunds.length) {
      f.refunds.forEach(function (r) { items.push({ ok: true, text: "refund " + r.refund_id + " issued for " + money(r.amount_usd) + " with Idempotency-Key " + r.idempotency_key }); });
    } else if (refundPlanned) {
      items.push({ ok: false, text: a.decision === "rejected" ? "no refund issued: the plan was rejected" : "no refund issued" });
    } else if (plan.actions) {
      items.push({ ok: null, text: "no money moved: the plan has no issue_refund" });
    }
    p.body.appendChild(L.checklist(items));
    if (order) {
      var det = el("details");
      add(det, el("summary", null, "the order record the tool returned"), pre(JSON.stringify(order, null, 2), "short"));
      p.body.appendChild(det);
    }
    return p.root;
  }

  // 03 and 04: the sections retrieve_policy handed the planner, with cited
  // ones marked, and the refund-window note chosen by L.windowCase.
  function policyPanel(t, c) {
    var st = t.state, plan = st.resolution_plan || {}, chunks = st.policy_chunks || [];
    var cited = plan.citations || [];
    var p = panel({
      title: "Policy retrieved", sub: chunks.length ? "top " + chunks.length + " sections by similarity, from Qdrant; cited ones are marked" : "nothing retrieved",
      cites: [c.retrieve_policy, c.top_k, c.build_query, c.embed, c.cite_only_given, c.contradiction_rule, c.rule5],
      why: "Checkpoint 00 pasted all six policy documents into every call; 02 had no policy text at all. 03 embeds <code>category: ticket text</code>, retrieves the six closest sections and hands only those to the planner. " +
        "Rule 5 then requires refunds and replacements to cite a section <em>from this list</em>: grounding is enforced, not hoped for. The planted contradiction (14 days vs 30 days) is the lesson: the prompt says escalate, nothing in <code>rules.py</code> makes it."
    });
    if (!chunks.length) { add(p.body, el("div", "muted small", t.nodes.some(function (n) { return n.node === "retrieve_policy"; }) ? "retrieve_policy returned nothing." : "The run stopped before retrieve_policy.")); return p.root; }
    var tbl = el("table", "tbl");
    var hr = el("tr");
    ["score", "section", "version", ""].forEach(function (h) { hr.appendChild(el("th", null, h)); });
    tbl.appendChild(hr);
    chunks.forEach(function (ch) {
      var id = ch.doc_id + "#" + ch.section_id;
      var tr = el("tr");
      var isCited = cited.indexOf(id) !== -1;
      add(tr, el("td", "mono", Number(ch.score).toFixed(3)), el("td", "mono" + (isCited ? " good" : ""), id), el("td", "mono muted", "v" + ch.version),
        el("td", isCited ? "good" : "muted", isCited ? "cited" : ""));
      tbl.appendChild(tr);
    });
    var wrap = el("div", "tbl-wrap");
    wrap.appendChild(tbl);
    p.body.appendChild(wrap);
    var w = L.windowCase(t);
    if (w.both) {
      var both = "Both refund-window sections were retrieved: refund-policy#window says 14 days from delivery, returns-and-exchanges#returns-window says 30. ";
      var age = w.days === null ? "" : "This order was delivered " + w.days + " day" + (w.days === 1 ? "" : "s") + " ago";
      var msg = {
        not_refunded: [null, both + "The plan does not refund, so the conflict decided nothing here" + ((plan.actions || []).some(function (a) { return a.type === "escalate_to_human"; }) ? "; it escalated." : ".")],
        inside: [null, both + age + ": inside both windows, so the two sections agree for this ticket and the refund is not a conflict."],
        outside: ["warn", both + age + ": past both windows, and the plan still refunds. Neither section allows that; check what the plan cited."],
        escalated: ["good", both + age + (age ? ": between 14 and 30 days, exactly where they disagree. " : "") + "The prompt says to escalate on a contradiction, and this run escalated."],
        disputed: ["warn", both + age + ": between 14 and 30 days, exactly where they disagree. The prompt says to escalate on a contradiction; this run refunded anyway. Every check is green and the decision is wrong: a prompt is not a guard (README “Try this” #1)."],
        unknown: ["warn", both + "The plan refunded: which window did it apply? The order has no delivery date, so the lab cannot tell whether this ticket falls where they disagree (more than 14, at most 30 days)."]
      }[w.kind];
      if (msg) p.body.appendChild(note(msg[0] || "info", msg[1]));
    }
    var det = el("details");
    add(det, el("summary", null, "the retrieved text, as the planner saw it"));
    chunks.forEach(function (ch) { add(det, el("div", "sub", ch.doc_id + "#" + ch.section_id + " · score " + ch.score), pre(ch.text, "short")); });
    p.body.appendChild(det);
    return p.root;
  }

  // 04 only. t.guard says which input guard this run used (Compare can run
  // both). The budget cap shown is the start flag when Setup set one, else
  // the .env value from health.
  function guardPanel(t, c) {
    var st = t.state, f = t.facts;
    var h = S.health && S.health.llm;
    var p = panel({
      title: "Guardrails and budget", sub: "untrusted input in, checked output out, a cap on spend",
      cls: f.guard_input ? "panel-good" : "",
      cites: [c.guard_input, c.injection_patterns, c.guard_classifier, c.output_guard, c.output_guard_call, c.safe_fallback, c.budget_cap, c.charge, c.model_triage, c.model_plan],
      why: "Input guard: cheap regex heuristics first, and only a suspicious ticket pays for an LLM second opinion; a confirmed injection becomes <code>GUARDRAIL_INJECTION_SUSPECTED</code> and goes to a human, the workflow never argues with an attacker. " +
        "Output guard: the reply may not mention amounts absent from the plan or make policy claims without citations. Budget: every call is charged through <code>budget.charge</code> and the run fails fast past the cap. Routing: triage on the cheap model, the plan on the strong one."
    });
    var items = [];
    var guardRow = t.nodes.filter(function (n) { return n.node === "guard_input"; })[0];
    var nemo = t.guard === "nemo";
    items.push({ ok: null, text: "input guard: " + (nemo ? "the NeMo guard: NeMo Guardrails' self check input rail on every ticket (guardrails_nemo.py, rails/)" : "the pattern guard: regex patterns, an LLM classifier only on a hit (guardrails.py)") });
    var where = f.guard_input && f.guard_input.source && f.guard_input.source !== "ticket" ? " on the " + f.guard_input.source : "";
    if (f.guard_input) items.push({ ok: true, text: "input guard tripped" + where + ": " + f.guard_input.reason });
    else if (guardRow) items.push({ ok: true, text: "input guard passed" + (nemo ? " (the rail judged it genuine; one LLM call)" : guardRow.cost_delta_usd ? " (a heuristic hit; the LLM classifier cleared it)" : " (no heuristic hit, so no LLM call was spent)") });
    if (st.customer_reply_draft !== null && st.customer_reply_draft !== undefined) {
      items.push(f.guard_output ? { ok: false, text: "output guard tripped: " + f.guard_output.violations.join("; ") + " → safe fallback sent" } : { ok: true, text: "output guard passed: every amount in the reply is in the plan" });
    }
    var capFlag = (t.service_flags || []).filter(function (x) { return x.field === "budget_usd"; })[0];
    var cap = capFlag ? Number(capFlag.value) : h && h.budget_per_run ? Number(h.budget_per_run) : null;
    items.push({ ok: st.internal_error_code !== "BUDGET_EXCEEDED", text: "spent " + usd(st.cost_usd) + (cap ? " of the " + usd(cap, cap < 0.01 ? 4 : 2) + " per-run cap" + (capFlag ? " (set at start in Setup)" : "") : "") + (st.internal_error_code === "BUDGET_EXCEEDED" ? " → BUDGET_EXCEEDED" : "") });
    if (h) items.push({ ok: null, text: "routing: triage on " + (h.deployment || "?") + ", plan on " + (h.strong_deployment || h.deployment || "?") + (h.strong_deployment ? "" : " (no strong model configured, so the same model does both)") });
    p.body.appendChild(L.checklist(items));
    return p.root;
  }

  function triagePanel(t, c) {
    var tr = t.state.triage, f = t.facts;
    var p = panel({
      title: "Triage", sub: tr ? "the same bounded repair as checkpoint 01, now a node" : "triage did not run",
      cites: [c.triage_node, c.triage_repair, c.structured_output],
      why: "The first node that calls the model: a <code>TriageResult</code> through the provider's structured-output mode, one repair re-prompt on malformed output. Its category drives the retrieval query and the business rules."
    });
    if (!tr) { add(p.body, el("div", "muted small", "The run stopped before triage.")); return p.root; }
    var k = el("div", "kvs");
    add(k, kv("category", tr.category), kv("priority", tr.priority), kv("sentiment", tr.sentiment), kv("confidence", Number(tr.confidence).toFixed(2), tr.confidence < 0.5 ? "warn" : ""),
      kv("repairs", f.triage_repairs));
    p.body.appendChild(k);
    return p.root;
  }

  // Every SSE the run produced, in order. The reply.delta events in a row
  // collapse into one "× n tokens" line, or the list would be mostly deltas.
  function eventsSection(t, c) {
    var s = L.section("Event stream: " + t.events.length + " server-sent events");
    add(s.body, L.why("Exactly what <code>curl -N /runs/&lt;id&gt;/events</code> prints. Every event was written to SQLite <em>before</em> it was sent, so a client that reconnects with <code>Last-Event-ID</code> replays what it missed. Reply deltas are collapsed here."));
    add(s.body, L.citesRow([c.publish, c.append_event, c.events_since, c.events_endpoint]));
    var box = el("div", "events");
    var deltas = 0;
    function flush() { if (deltas) { var row = el("div", "ev kind-reply-delta"); add(row, el("span", "seq", ""), el("span", "name", "reply.delta"), el("span", "data", "× " + deltas + " tokens")); box.appendChild(row); deltas = 0; } }
    t.events.forEach(function (e) {
      if (e.event === "reply.delta") { deltas++; return; }
      flush();
      var row = el("div", "ev kind-" + e.event.replace(/\./g, "-"));
      add(row, el("span", "seq", "#" + e.seq), el("span", "name", e.event), el("span", "data", JSON.stringify(e.data)));
      box.appendChild(row);
    });
    flush();
    s.body.appendChild(box);
    return s.root;
  }

  function logSection(t, c) {
    var s = L.section("Log lines: " + (t.log_lines || []).length + " in " + (t.log_file || "logs/runs.jsonl") + " under request " + t.request_id);
    add(s.body, L.why("One JSON line per event, the same <code>obslog.log_event</code> since checkpoint 01. Node errors, rule violations, refunds and guardrail trips all land here with the request id" + (t.checkpoint === "04" ? ", and <code>dashboard.py</code> is one pass over this file." : ".")));
    add(s.body, L.citesRow([c.log_event, c.wrap_error]));
    var row = el("div", "cmd-head");
    add(row, el("code", null, t.request_id), L.copyBtn(t.request_id));
    s.body.appendChild(row);
    s.body.appendChild(L.logTable(t.log_lines));
    return s.root;
  }

  function stateSection(t) {
    var s = L.section("Persisted state: GET /runs/" + t.request_id);
    add(s.body, L.why("The full <code>TicketResolutionState</code> as saved after the last node: this is what the API shows and what a restart resumes from. Customer text and machine output live in separate fields on purpose."));
    s.body.appendChild(pre(JSON.stringify(t.state, null, 2)));
    return s.root;
  }

  /* ── search only (checkpoint 03) ── */
  // res: POST /api/search. ok=false explains why (the service holds the index,
  // no index yet, locked by another process); actions.stopAndSearch and
  // actions.startAgain are offered when they apply.
  // The index could not be opened (no index, the 03 service holds it, another
  // process holds it, or the runner failed). Shared by search and the map.
  function notRun(res, out, actions, title, verb) {
    var p0 = panel({ title: title + ": not run", sub: "checkpoint 03's policy index", cls: "panel-warn" });
    add(p0.body, el("p", null, res.message));
    if (res.cmd) p0.body.appendChild(L.cmdBlock({ label: "Build the index", cmd: res.cmd }));
    if (res.detail) add(p0.body, pre(res.detail, "short"));
    if (res.code === "service_running") {
      var row = el("div", "approval-actions");
      var b = el("button", "btn btn-primary", "Stop checkpoint 03, then " + verb);
      b.type = "button";
      b.onclick = function () { b.disabled = true; b.textContent = "stopping…"; actions.stopAndSearch(); };
      add(row, b, el("span", "muted small", res.managed ? "a run in progress on 03 would stop too" : "it was started outside the lab; the lab stops it only if the port really serves checkpoint 03"));
      p0.body.appendChild(row);
    }
    add(out, p0.root);
  }

  function startAgainRow(actions, what) {
    var again = el("div", "approval-actions");
    var sb = el("button", "btn", "Start checkpoint 03 again");
    sb.type = "button";
    sb.onclick = function () { sb.disabled = true; sb.textContent = "starting…"; actions.startAgain().then(function () { sb.textContent = "started"; }, function (e) { sb.disabled = false; sb.textContent = "start failed: " + e.message; }); };
    add(again, sb, el("span", "muted small", "the lab stopped it for this " + what + "; runs on 03 need it back"));
    return again;
  }

  // A ranked table of retrieved sections. mark(chunk) returns an extra class
  // (" dropped" strikes out a row the post-filter would throw away).
  function sectionTable(chunks, mark) {
    var tbl = el("table", "tbl");
    var hr = el("tr");
    ["#", "score", "section", "version", "effective"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
    tbl.appendChild(hr);
    chunks.forEach(function (ch, i) {
      var tr = el("tr");
      var cls = mark ? mark(ch) : "";
      add(tr, el("td", "mono", String(i + 1)), el("td", "mono" + cls, Number(ch.score).toFixed(3)), el("td", "mono" + cls, ch.doc_id + "#" + ch.section_id),
        el("td", "mono muted", "v" + ch.version), el("td", "mono muted", ch.effective || "—"));
      tbl.appendChild(tr);
    });
    var wrap = el("div", "tbl-wrap");
    wrap.appendChild(tbl);
    return wrap;
  }

  // With a document chosen, the server also returns res.unfiltered (the same
  // search with no filter), so the page can show filter-inside-the-search
  // beside drop-rows-afterwards.
  function renderSearch(res, out, actions) {
    out.textContent = "";
    if (!res.ok) { notRun(res, out, actions, "Search only", "search"); return; }
    var ids = res.chunks.map(function (c) { return c.doc_id + "#" + c.section_id; });
    var only = res.doc_ids && res.doc_ids.length ? res.doc_ids : null;
    var p = panel({
      title: "Policy search: top " + res.chunks.length + " section" + (res.chunks.length === 1 ? "" : "s") + (only ? ", only " + only.join(", ") : ""),
      sub: "checkpoint 03's own retrieve(), no triage, no planner" + (only ? "; the document filter ran inside the search" : ""),
      chips: [L.chip(res.embed_model || "embedding", "chip-cp"), L.chip(res.tokens + " tokens · " + usd(res.cost_usd, 6)), L.chip(ms(res.duration_ms))],
      cites: [res.cites.retrieve, res.cites.top_k, res.cites.embed, res.cites.build_query, res.cites.retrieve_policy].concat(only ? [res.cites.doc_filter] : []),
      why: "The same <code>retrieve.retrieve()</code> the <code>retrieve_policy</code> node calls: one embedding of the query, then the closest sections in the local Qdrant index. " +
        "A real run does not search your bare words: <code>build_query</code> sends <code>category: ticket text</code> (first 500 characters), so its scores differ from these." +
        (only ? " With a document chosen, <code>retrieve()</code> hands Qdrant a <code>filter=</code> on the stored <code>doc_id</code> label: the <code>WHERE</code> of Session 8's slide, applied inside the search, not to the rows afterwards." : "")
    });
    add(p.body, el("div", "sub", "query"), el("blockquote", "gate-quote", res.query));
    p.body.appendChild(sectionTable(res.chunks));
    if (only && res.unfiltered) {
      var asked = res.k, kept = res.unfiltered.filter(function (c) { return only.indexOf(c.doc_id) !== -1; }).length;
      var foreign = res.unfiltered.length - kept;
      add(p.body, el("div", "sub", "the other way: search everything, then drop the rows from other documents"));
      p.body.appendChild(sectionTable(res.unfiltered, function (c) { return only.indexOf(c.doc_id) === -1 ? " dropped" : ""; }));
      p.body.appendChild(note(foreign ? "warn" : "info",
        "Filter inside the search: " + asked + " asked, " + res.chunks.length + " returned, every one from " + only.join(", ") +
        (res.chunks.length < asked ? " (the document has no more sections, so k is a ceiling, not a promise)." : ".") +
        " Drop rows afterwards: " + kept + " of " + res.unfiltered.length + " kept, and the " + foreign + " struck-out row" + (foreign === 1 ? "" : "s") +
        " had already left the store, passed through this code and could have reached a log. With tenants or permissions in the label, that is a leak, not a bug ticket."));
    }
    if (ids.indexOf("refund-policy#window") !== -1 && ids.indexOf("returns-and-exchanges#returns-window") !== -1) {
      p.body.appendChild(note("info", "Both refund-window sections are in the top " + res.chunks.length + ": refund-policy#window (14 days) and returns-and-exchanges#returns-window (30 days). A planner given this set sees the planted contradiction."));
    }
    var det = el("details");
    add(det, el("summary", null, "the retrieved text, as a planner would see it"));
    res.chunks.forEach(function (ch) { add(det, el("div", "sub", ch.doc_id + "#" + ch.section_id + " · score " + ch.score), pre(ch.text, "short")); });
    p.body.appendChild(det);
    if (actions.stopped) p.body.appendChild(startAgainRow(actions, "search"));
    add(out, p.root);
    add(out, L.commandsSection([{ label: "The same search in the terminal (stop the 03 service first)", cmd: res.cmd }]));
  }

  /* ── how close are these phrases? (POST /api/distance) ── */
  // res.phrases, res.pairs [{a, b, score}] sorted best first, res.dims.
  function renderDistance(res, out) {
    out.textContent = "";
    if (!res.ok) {
      var p0 = panel({ title: "Phrases: not compared", sub: "checkpoint 03's llm.embed_texts", cls: "panel-warn" });
      add(p0.body, el("p", null, res.message));
      if (res.detail) add(p0.body, pre(res.detail, "short"));
      add(out, p0.root);
      return;
    }
    var p = panel({
      title: "How close are these " + res.phrases.length + " phrases?",
      sub: "cosine similarity between their embeddings: 1 is the same direction, 0 is a right angle",
      chips: [L.chip(res.embed_model || "embedding", "chip-cp"), L.chip(res.dims + " numbers each"), L.chip(res.tokens + " tokens · " + usd(res.cost_usd, 6)), L.chip(ms(res.duration_ms))],
      cites: [res.cites.embed],
      why: "One call to <code>llm.embed_texts</code> with all the phrases, then every pair's cosine (the dot product of the two unit-length lists). " +
        "There is no fixed line that says \"related\": the numbers only mean something next to each other, and the search keeps the highest ones. " +
        "Shared words are not needed; a different language is not a problem either."
    });
    var tbl = el("table", "tbl");
    var hr = el("tr");
    ["", "phrase", "phrase", "score"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
    tbl.appendChild(hr);
    res.pairs.forEach(function (pair, i) {
      var tr = el("tr", i === 0 ? "best" : null);
      var bar = el("div", "bar");
      bar.style.width = Math.max(0, Math.min(1, pair.score)) * 100 + "%";
      var cell = el("td", "mono");
      add(cell, Number(pair.score).toFixed(3), bar);
      add(tr, el("td", "muted small", i === 0 ? "closest" : ""), el("td", null, res.phrases[pair.a]), el("td", null, res.phrases[pair.b]), cell);
      tbl.appendChild(tr);
    });
    var wrap = el("div", "tbl-wrap");
    wrap.appendChild(tbl);
    p.body.appendChild(wrap);
    if (res.pairs.length > 1) {
      var best = res.pairs[0], last = res.pairs[res.pairs.length - 1];
      add(p.body, el("p", "muted small", "Closest: “" + res.phrases[best.a] + "” and “" + res.phrases[best.b] + "”. Furthest: “" +
        res.phrases[last.a] + "” and “" + res.phrases[last.b] + "”. Keyword search would score every pair here 0 unless they share a word."));
    }
    add(out, p.root);
  }

  /* ── the map (POST /api/map) ── */
  // res.points [{doc_id, section_id, x, y}], res.query {text, x, y} | null,
  // res.nearest (the real top-k for the query), res.explained [v1, v2].
  // SVG elements need createElementNS; el() would make inert HTML elements.
  // Dot colours come from the s1..s6 classes in lab.css, so both themes apply.
  var SVG_NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function renderMap(res, out, actions) {
    out.textContent = "";
    if (!res.ok) { notRun(res, out, actions, "The map", "draw the map"); return; }
    var docs = [];
    res.points.forEach(function (pt) { if (docs.indexOf(pt.doc_id) === -1) docs.push(pt.doc_id); });
    docs.sort();
    var series = function (doc) { return "s" + (docs.indexOf(doc) % 6 + 1); };
    var kept = Math.round((res.explained[0] + res.explained[1]) * 100);
    var nearIds = (res.nearest || []).map(function (c) { return c.doc_id + "#" + c.section_id; });
    var p = panel({
      title: "The index on two axes: " + res.points.length + " sections" + (res.query ? ", and your query" : ""),
      sub: "every stored vector, squeezed from " + res.dims + " numbers to 2 (PCA); these two axes keep " + kept + "% of the variation",
      chips: [L.chip(res.embed_model || "embedding", "chip-cp"), L.chip(res.query ? res.tokens + " tokens · " + usd(res.cost_usd, 6) : "no embedding call"), L.chip(ms(res.duration_ms))],
      cites: [res.cites.retrieve, res.cites.top_k, res.cites.embed],
      why: "The runner reads every vector from the Qdrant index and keeps the two directions along which the sections differ most (PCA, done with numpy). " +
        "It is a sketch: " + (100 - kept) + "% of the variation is not on this page, so two dots that look close may be far apart in the real " + res.dims + "-D space. " +
        "The ringed sections are the real top " + res.top_k + " for the query, found in the full space with the store's own search, not on this picture."
    });
    // Scale the PCA coordinates (query included) into the viewBox; y is
    // flipped because SVG y grows downwards.
    var W = 760, H = 440, PAD = 34;
    var all = res.points.slice();
    if (res.query) all.push(res.query);
    var xs = all.map(function (q) { return q.x; }), ys = all.map(function (q) { return q.y; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs), minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    var sx = function (x) { return PAD + (maxX === minX ? 0.5 : (x - minX) / (maxX - minX)) * (W - 2 * PAD); };
    var sy = function (y) { return H - PAD - (maxY === minY ? 0.5 : (y - minY) / (maxY - minY)) * (H - 2 * PAD); };
    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "map-svg", role: "img" });
    var title = svg("title");
    title.textContent = "Every policy section as a dot on two axes, coloured by document" + (res.query ? "; the query is the diamond" : "");
    root.appendChild(title);
    res.points.forEach(function (pt) {
      var id = pt.doc_id + "#" + pt.section_id;
      var near = nearIds.indexOf(id) !== -1;
      var dot = svg("circle", { cx: sx(pt.x).toFixed(1), cy: sy(pt.y).toFixed(1), r: near ? 7 : 5.5, class: "map-dot " + series(pt.doc_id) + (near ? " near" : "") });
      var t = svg("title");
      t.textContent = id;
      dot.appendChild(t);
      root.appendChild(dot);
      if (near) {
        var label = svg("text", { x: (sx(pt.x) + 10).toFixed(1), y: (sy(pt.y) + 4).toFixed(1), class: "map-label" });
        label.textContent = pt.section_id;
        root.appendChild(label);
      }
    });
    if (res.query) {
      var qx = sx(res.query.x), qy = sy(res.query.y);
      var diamond = svg("path", { d: "M" + qx + " " + (qy - 10) + " L" + (qx + 10) + " " + qy + " L" + qx + " " + (qy + 10) + " L" + (qx - 10) + " " + qy + " Z", class: "map-q" });
      var qt = svg("title");
      qt.textContent = "your query: " + res.query.text;
      diamond.appendChild(qt);
      root.appendChild(diamond);
      var ql = svg("text", { x: (qx + 13).toFixed(1), y: (qy - 8).toFixed(1), class: "map-label q" });
      ql.textContent = "your query";
      root.appendChild(ql);
    }
    p.body.appendChild(root);
    var legend = el("div", "legend");
    docs.forEach(function (doc) { add(legend, el("span", null, el("span", "swatch " + series(doc)), doc)); });
    if (res.query) add(legend, el("span", null, el("span", "swatch q"), "your query"), el("span", null, el("span", "swatch ring"), "its top " + res.top_k));
    p.body.appendChild(legend);
    if (res.query) {
      add(p.body, el("div", "sub", "the real top " + res.top_k + " for “" + res.query.text + "”, scored in the full space"), sectionTable(res.nearest));
    }
    add(p.body, el("p", "muted small", "Hover a dot for its section. Same-document sections tend to sit together because they share vocabulary; the search ignores documents and takes the nearest sections wherever they are."));
    if (actions.stopped) p.body.appendChild(startAgainRow(actions, "map"));
    add(out, p.root);
  }

  L.liveSvc = live;
  L.renderSearch = renderSearch;
  L.renderDistance = renderDistance;
  L.renderMap = renderMap;
  L.renderSvc = render;
  L.svcVerdict = verdict;
})();
