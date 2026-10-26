/* Deskhand Lab — the simple view: the same run in plain words, for people who
   want the outcome without the evidence. Every report (live and final) is
   rendered twice, into .report-simple and .report-detailed; the Simple /
   Detailed switch at the top of the report picks which one shows.

   Nothing here is computed: it reads the same trace the full report reads
   and rewords it. No jargon: no nodes, schemas, request ids or SSE. */

/* Layout, top to bottom: the Simple / Detailed switch and report(), which
   views.js calls to lay out every report; small building blocks (head,
   card, journey, facts); cited(), which turns citation ids into a document
   name, the section heading and its bolded rule; then the verdicts
   (verdict00 / verdict01 / verdictSvc, each [sentence, tone, badge]), the
   journeys (the steps in plain words), the final report's cards
   (customerCard, decisionCard, checksCard), render(), and live().

   live() handles the same NDJSON events as the detailed live views and
   draws its own Approve / Reject card for the same run, so the switch can
   flip mid-run without losing the gate.
   The mode is a class on <body> ("simple"), saved as lab.simple, with a
   ?simple=1 or ?simple=0 URL override for this load.
   Reads from L: S and the DOM helpers, checklist, gateCase, gateInput,
   gateDetails, windowCase, jsonish, postJSON. */
(function () {
  "use strict";
  var S = L.S, el = L.el, add = L.add, money = L.money, usd = L.usd, ms = L.ms, titleCase = L.titleCase;

  // Node names in plain words: past tense for the finished journey, the
  // -ing form below for the live headline. Unknown names fall back to the
  // node name with spaces.
  var STEP_WORDS = {
    normalize_ticket: "Read the ticket",
    guard_input: "Checked the ticket for suspicious content",
    triage: "Worked out what the ticket is about",
    fetch_order_data: "Looked up the order",
    retrieve_policy: "Found the relevant policy sections",
    generate_resolution_plan: "Drafted a plan",
    validate_plan: "Checked the plan against the rules",
    repair_plan: "Fixed the plan and tried again",
    approval_gate: "Asked a person to approve",
    execute_actions: "Carried out the actions",
    draft_customer_reply: "Wrote the reply to the customer",
    finalize: "Saved the result",
    handle_failure: "Stopped safely"
  };
  var LIVE_WORDS = {
    normalize_ticket: "Reading the ticket", guard_input: "Checking the ticket for suspicious content", triage: "Working out what the ticket is about",
    fetch_order_data: "Looking up the order", retrieve_policy: "Finding the relevant policy sections", generate_resolution_plan: "Drafting a plan",
    validate_plan: "Checking the plan against the rules", repair_plan: "Fixing the plan and trying again", approval_gate: "Checking whether a person needs to approve",
    execute_actions: "Carrying out the actions", draft_customer_reply: "Writing the reply to the customer", finalize: "Saving the result", handle_failure: "Stopping safely"
  };
  var ACTION_WORDS = {
    issue_refund: "issue a refund", send_replacement: "send a replacement", escalate_to_human: "hand it to a person",
    request_more_info: "ask the customer for more details", reply_only: "reply, no action needed"
  };
  var ACTION_BADGE = {
    issue_refund: "Refunded", send_replacement: "Replacement sent", escalate_to_human: "Handed to a person",
    request_more_info: "Asked for details", reply_only: "Replied"
  };
  var DOC_WORDS = {
    "refund-policy": "Refund policy", "returns-and-exchanges": "Returns & exchanges", "shipping-sla": "Shipping promises",
    warranty: "Warranty", "escalation-matrix": "Escalation rules", "promo-terms": "Promo terms"
  };
  // How many cited sections the decision card lists before "and n more".
  var CITES_SHOWN = 3;

  /* ── the Simple / Detailed switch ───────────────────────── */

  // Flips body.simple, which lab.css uses to hide one of the two report
  // copies, and syncs every switch on the page. Nothing re-renders. Only a
  // click persists; the URL override and the startup read do not.
  function setMode(simple, persist) {
    document.body.classList.toggle("simple", simple);
    var segs = document.querySelectorAll(".seg[data-mode] button");
    for (var i = 0; i < segs.length; i++) {
      var on = (segs[i].dataset.simple === "1") === simple;
      segs[i].classList.toggle("on", on);
      segs[i].setAttribute("aria-pressed", String(on));
    }
    if (persist) { try { localStorage.setItem("lab.simple", simple ? "1" : "0"); } catch (e) { /* private mode */ } }
  }

  function initMode() {
    var params = new URLSearchParams(location.search);
    var saved = null;
    try { saved = localStorage.getItem("lab.simple"); } catch (e) { /* private mode */ }
    setMode(params.has("simple") ? params.get("simple") !== "0" : saved === "1", false);
  }

  // The switch is drawn inside each report, so it sits next to what it changes.
  function modeBar() {
    var bar = el("div", "mode-bar");
    var seg = el("div", "seg");
    seg.dataset.mode = "report";
    [["Simple", "1", "Plain words: what happened, what the customer got, what was checked"],
     ["Detailed", "0", "The full report: pipeline, plan, money, policy, events, logs"]].forEach(function (b) {
      var btn = el("button", null, b[0]);
      btn.type = "button";
      btn.dataset.simple = b[1];
      btn.title = b[2];
      var on = document.body.classList.contains("simple") === (b[1] === "1");
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", String(on));
      btn.onclick = function () { setMode(b[1] === "1", true); };
      seg.appendChild(btn);
    });
    add(bar, el("span", null, "view"), seg);
    return bar;
  }

  // Empties `out` and lays out one report: the switch, then the two views.
  function report(out) {
    out.textContent = "";
    var simple = el("div", "report-simple"), detailed = el("div", "report-detailed");
    add(out, modeBar(), simple, detailed);
    return { simple: simple, detailed: detailed };
  }

  /* ── building blocks ────────────────────────────────────── */

  // The plain-words headline: sentence, badge and tone. The live view
  // rewrites title, badge and tone as events arrive.
  function head(o) {
    var root = el("div", "simple-head tone-" + (o.tone || "live"));
    var top = el("div", "simple-top");
    var sub = el("span", "muted small", o.sub || "");
    var badge = el("span", "simple-badge badge-" + (o.tone || "live"), o.badge || "");
    add(top, sub, badge);
    var title = el("div", "title", o.title || "");
    add(root, top, title);
    return { root: root, title: title, badge: badge, sub: sub };
  }

  function card(title, cls) {
    var root = el("div", "simple-card" + (cls ? " " + cls : ""));
    add(root, el("h3", null, title));
    var body = el("div");
    root.appendChild(body);
    return { root: root, body: body };
  }

  // The vertical list of steps. items: [{text, state, sub?}], where state is
  // done, error, paused, warn, skip or active (active draws a spinner).
  var ICONS = { done: "✓", error: "✗", paused: "⏸", warn: "!", skip: "·", active: "" };
  function journey(items) {
    var ul = el("ul", "journey");
    items.forEach(function (it) {
      var li = el("li", it.state || "skip");
      var ico = el("span", "ico");
      if (it.state === "active") ico.appendChild(el("span", "spinner")); else ico.textContent = ICONS[it.state] || "·";
      var txt = el("span");
      add(txt, el("span", "nm", it.text), it.sub ? el("span", "hint", it.sub) : null);
      add(li, ico, txt);
      ul.appendChild(li);
    });
    return ul;
  }

  // A labelled value in the facts grid; null entries in facts() are skipped,
  // so a card can include a fact conditionally.
  function fact(k, v, cls) {
    var w = el("div", "fact");
    add(w, el("span", "k", k), el("span", "v" + (cls ? " " + cls : ""), v === null || v === undefined || v === "" ? "—" : String(v)));
    return w;
  }
  function facts(list) { var g = el("div", "facts"); list.forEach(function (f) { if (f) g.appendChild(f); }); return g; }

  function reasonsFact(k, reasons) {
    var w = el("div", "fact wide");
    add(w, el("span", "k", k));
    var ul = el("ul", "reasons");
    reasons.forEach(function (r) { add(ul, add(el("li"), el("span", "v", r.text), el("span", "rule", r.detail))); });
    w.appendChild(ul);
    return w;
  }
  function quoteFact(k, text) {
    var w = el("div", "fact wide");
    add(w, el("span", "k", k), el("blockquote", "gate-quote", text));
    return w;
  }

  // Small formatters. who() reads "by you" for a click on this page and
  // credits the lab's auto policy otherwise.
  function pct(c) { return typeof c === "number" ? Math.round(c * 100) + "%" : "—"; }
  function who(a) { return a && a.by === "you" ? "by you" : "automatically by the lab"; }
  function amount(r) { return r.amount_parsed !== null && r.amount_parsed !== undefined ? money(r.amount_parsed) : L.jsonish(r.amount); }

  /* The plan's citations ("refund-policy#window") in plain words: the document's
     name, the section's own heading (a chunk's text starts with it), and the rule
     itself, taken as the first sentence the policy writers put in bold. */
  function cited(t) {
    var chunks = t.state.policy_chunks || [];
    return ((t.state.resolution_plan || {}).citations || []).map(function (id) {
      var doc = id.split("#")[0], sec = id.split("#")[1] || "", chunk = null;
      chunks.forEach(function (c) { if (!chunk && c.doc_id === doc && c.section_id === sec) chunk = c; });
      var lines = chunk ? String(chunk.text || "").split("\n") : [];
      var heading = lines.length && lines[0].trim() ? lines[0].trim() : sec.replace(/-/g, " ").replace(/^./, function (x) { return x.toUpperCase(); });
      var body = lines.slice(1).filter(function (l) { return !/^\s*\|/.test(l); }) // tables don't read as a sentence
        .map(function (l) { return l.replace(/^\s*([-*]|\d+\.)\s+/, "").trim(); }).filter(Boolean).join(" ")
        .replace(/(\w)- (\w)/g, "$1-$2"); // a hyphen the markdown wrapped onto the next line
      var sentences = body.split(/(?<=[.!?])\s+/);
      var rule = (sentences.filter(function (x) { return x.indexOf("**") !== -1; })[0] || sentences[0] || "").replace(/\*\*|`/g, "");
      if (rule.length > 180) rule = rule.slice(0, 180).replace(/\s+\S*$/, "") + "…";
      return { label: (DOC_WORDS[doc] || doc) + ": " + heading, rule: rule };
    });
  }
  function citedLabels(list, n) {
    var names = list.slice(0, n).map(function (c) { return c.label; });
    return names.join("; ") + (list.length > n ? " and " + (list.length - n) + " more" : "");
  }
  function citesFact(list) {
    var w = el("div", "fact wide");
    add(w, el("span", "k", "policy it relied on"));
    if (!list.length) { w.appendChild(el("span", "v", "none")); return w; }
    var ul = el("ul", "cites");
    list.slice(0, CITES_SHOWN).forEach(function (c) {
      add(ul, add(el("li"), el("span", "v", c.label), c.rule ? el("span", "rule", "“" + c.rule + "”") : null));
    });
    if (list.length > CITES_SHOWN) ul.appendChild(el("li", "muted", "and " + (list.length - CITES_SHOWN) + " more (see Detailed)"));
    w.appendChild(ul);
    return w;
  }

  function footer(s) {
    var f = el("div", "simple-foot");
    add(f, el("span", null, "cost " + usd(s.cost_usd)), el("span", null, "took " + ms(s.latency_ms)),
      el("span", null, (s.attempts === null || s.attempts === undefined ? "—" : s.attempts) + " call" + (s.attempts === 1 ? "" : "s") + " to the AI"));
    var b = el("button", "btn btn-sm btn-ghost", "see the full report →");
    b.type = "button";
    b.onclick = function () { setMode(false, true); window.scrollTo({ top: 0, behavior: "smooth" }); };
    f.appendChild(b);
    return f;
  }

  /* ── verdicts in plain words: [sentence, tone, badge] ───── */

  function verdict00(t) {
    var r = t.refund || {}, n = (t.attempts || []).length, order = r.order || {};
    if (t.outcome === "gave_up") return ["The bot tried " + n + " times, failed every time and gave up. The customer watched it happen.", "bad", "Gave up"];
    if (t.outcome === "crashed") return ["The bot crashed before it could answer.", "bad", "Crashed"];
    if (t.outcome === "parsed_non_object" || t.outcome === "not_called") return ["The bot did not produce a usable answer.", "bad", "No answer"];
    if (r.fired) {
      if (r.exceeds_order_total) return ["The bot refunded " + amount(r) + " on an order worth " + money(order.total_usd) + ". Nobody checked the amount and nobody approved it.", "bad", "Refund over the order total"];
      return ["The bot refunded " + amount(r) + " without checking anything. It stayed within the order total this time, by luck.", "warn", "Unchecked refund"];
    }
    return ["The bot answered" + (n > 1 ? " on try " + n : "") + ", but nothing checked that answer before it went out.", "warn", "Answered, unchecked"];
  }

  function verdict01(t) {
    var r = t.result || {}, n = (t.attempts || []).length;
    if (t.outcome === "validated") {
      var low = r.confidence < 0.5;
      return ["The ticket was sorted as “" + titleCase(r.category) + "”, " + r.priority + " priority, and the customer sounds " + r.sentiment + "." +
        (n > 1 ? " The first answer came back in the wrong format and was corrected." : "") + (low ? " The AI was not confident about this one." : ""),
        low ? "warn" : "good", low ? "Sorted, low confidence" : "Sorted"];
    }
    if (t.outcome === "failed") return ["The AI could not give a usable answer, so the customer got a safe holding message instead of an error.", "warn", "Stopped safely"];
    return ["Something unexpected went wrong and the run stopped.", "bad", "Went wrong"];
  }

  function verdictSvc(t) {
    var s = t.summary, st = t.state, f = t.facts, a = t.approval || {};
    if (t.outcome !== "completed") {
      var code = s.error_code || "unknown";
      var detail = (f.node_errors[0] || {}).detail || "";
      if (code === "GUARDRAIL_INJECTION_SUSPECTED") return ["The ticket looked like an attempt to trick the bot. It was handed to a person and nothing was done automatically.", "good", "Handed to a person"];
      if (code === "KILL_SWITCH_ENGAGED") return ["An operator has switched the automation off, so the bot stopped and handed the ticket to a person. " +
        (f.refunds.length ? "A refund had already gone out before the switch was turned on." : "Nothing was refunded or shipped."), "warn", "Automation switched off"];
      if (code === "BUDGET_EXCEEDED") return ["The run was stopped because it was costing more than allowed. Nothing was done.", "warn", "Stopped: over budget"];
      if (code === "REPAIR_EXHAUSTED") return ["The AI could not come up with a plan that follows the rules, so nothing was done. The customer got a safe holding message.", "warn", "Stopped safely"];
      if (code === "INTERNAL_UNEXPECTED" && /already accessed by another instance of Qdrant/.test(detail)) {
        return ["A technical problem stopped the run: the policy index is in use by another program. Nothing was done. Close that program and run again.", "bad", "Technical problem"];
      }
      if (code === "INTERNAL_UNEXPECTED") return ["A technical problem stopped the run. Nothing was done, and the customer got a safe holding message.", "bad", "Technical problem"];
      return ["The run stopped safely. Nothing was done, and the customer got a safe holding message.", "warn", "Stopped safely"];
    }
    var order = st.order_data || {}, plan = st.resolution_plan || {};
    var lead = L.gateCase(L.gateInput(st)).over ? "The customer asked for " + money(plan.requested_refund_usd) + " on an order worth only " + money(order.total_usd) + ". " : "";
    if (a.required && a.decision === "rejected") return [lead + "A person rejected the plan. Nothing was done; the customer was told a specialist will follow up.", "warn", "Rejected by a person"];
    var actions = s.actions || [], parts = [];
    if (actions.indexOf("issue_refund") !== -1) {
      var refunded = f.refunds.length ? f.refunds[0] : null;
      parts.push((refunded ? "refunded " + money(refunded.amount_usd) : "planned a refund of " + money(s.refund_amount)) +
        " on order " + (order.order_id || "?") + " (worth " + money(order.total_usd) + ")" +
        (a.required ? ", approved " + who(a) : ", no approval needed because it is under $50"));
    }
    if (actions.indexOf("send_replacement") !== -1) parts.push("a replacement is being sent" + (a.required && actions.indexOf("issue_refund") === -1 ? ", approved " + who(a) : ""));
    if (actions.indexOf("escalate_to_human") !== -1) parts.push("the ticket was handed to a person");
    if (actions.indexOf("request_more_info") !== -1) parts.push("the customer was asked for more details");
    if (actions.indexOf("reply_only") !== -1) parts.push("the customer got a reply and no action was needed");
    var text = parts.length ? parts.join("; ") : "finished without doing anything";
    text = lead + text.charAt(0).toUpperCase() + text.slice(1) + ".";
    var tone = "good", badge = ACTION_BADGE[actions[0]] || "Done";
    // The planted 14 vs 30 day contradiction: only claimed when this order falls where they disagree (L.windowCase).
    var w = t.checkpoint >= "03" ? L.windowCase(t) : { kind: "none" };
    if (w.kind === "disputed") {
      text += " Note: the order arrived " + w.days + " days ago, and the two policy documents disagree about that (one allows 14 days, the other 30). The bot was told to hand such cases to a person, but it refunded anyway.";
      tone = "warn";
    } else if (w.kind === "unknown") {
      text += " Question: the two policy documents give different refund windows (14 and 30 days), and the bot refunded. Which one did it follow? The order has no delivery date to check.";
      tone = "warn";
    } else if (w.kind === "outside") {
      text += " Note: the order arrived " + w.days + " days ago, past both refund windows, and the bot refunded anyway.";
      tone = "warn";
    }
    if (f.guard_output) { text += " The drafted reply was swapped for a standard safe message because it promised something the plan does not include."; tone = tone === "good" ? "warn" : tone; }
    return [text, tone, badge];
  }

  function verdict(t) { return t.checkpoint === "00" ? verdict00(t) : t.checkpoint === "01" ? verdict01(t) : verdictSvc(t); }

  /* ── the journey (steps in plain words) ─────────────────── */

  function journey00(t, live) {
    var out = [];
    (t.attempts || []).forEach(function (a) {
      out.push({ text: a.n === 1 ? "Asked the AI for an answer" : "Asked again, with exactly the same question",
        state: a.exception || a.api_error ? "error" : (a.latency_ms === undefined && live ? "active" : "done"),
        sub: a.exception ? "the answer could not be read" : a.api_error ? "the AI service returned an error" : "" });
    });
    if (!live) {
      var r = t.refund || {};
      out.push({ text: "Got a usable answer", state: t.outcome === "parsed" ? "done" : "error", sub: t.outcome === "parsed" ? "nothing checked it" : "" });
      out.push({ text: "Issued a refund", state: r.fired ? (r.exceeds_order_total ? "error" : "warn") : "skip",
        sub: r.fired ? amount(r) + ", with no checks and no approval" : "no refund this time" });
    }
    return out;
  }

  function journey01(t, live) {
    var out = [];
    (t.attempts || []).forEach(function (a) {
      var e = a.error, sub = "";
      if (e) sub = e.code === "LLM_MALFORMED_OUTPUT" ? "the answer was in the wrong format" : e.code === "REPAIR_EXHAUSTED" ? "still wrong after a correction" : "the AI service had a problem";
      out.push({ text: a.n === 1 ? "Asked the AI to sort the ticket" : "Asked again, pointing out what was wrong",
        state: e ? "error" : (a.done === false && live ? "active" : "done"), sub: sub });
    });
    if (!live) {
      out.push({ text: "Checked the answer against the required format", state: t.outcome === "validated" ? "done" : t.outcome === "failed" ? "warn" : "error",
        sub: t.outcome === "validated" ? "passed" : t.outcome === "failed" ? "no usable answer; the customer got a safe message" : "" });
    }
    return out;
  }

  function journeySvc(order, rows, t) {
    var visits = {}, last = {}, raised = {};
    rows.forEach(function (r) { visits[r.node] = (visits[r.node] || 0) + 1; last[r.node] = r; if (r.raised) raised[r.node] = true; });
    var items = [];
    order.forEach(function (name) {
      var r = last[name];
      if (!r && (name === "repair_plan" || name === "handle_failure")) return;
      var it = { text: STEP_WORDS[name] || titleCase(name), state: "skip", sub: "not reached" };
      if (r) {
        it.state = r.latency_ms === null ? "active" : "done";
        it.sub = "";
        if (name === "repair_plan") { it.state = "warn"; it.sub = "the first plan broke a rule" + (visits[name] > 1 ? "; it took " + visits[name] + " corrections" : ""); }
        if (name === "approval_gate") {
          var a = t && t.approval;
          if (a && a.required) {
            it.state = a.decision === "approved" ? "done" : a.decision === "rejected" ? "error" : "paused";
            it.sub = a.decision ? a.decision + " " + who(a) : "waiting for a decision";
          } else if (it.state === "done") it.sub = "not needed for this plan";
        }
        if (name === "guard_input" && t && t.facts && t.facts.guard_input) { it.state = "warn"; it.sub = "suspicious content found; handed to a person"; }
        if (raised[name]) { it.state = "error"; it.sub = "this is where it went wrong"; }
        if (name === "handle_failure") { it.state = "error"; it.sub = "nothing was done; the customer got a safe message"; }
      }
      items.push(it);
    });
    return items;
  }

  /* ── the cards of the final report ──────────────────────── */

  function customerCard(t) {
    var c = card("What the customer gets");
    if (t.checkpoint === "00") {
      var plan = t.plan && typeof t.plan === "object" ? t.plan : null;
      if (plan && plan.customer_reply) {
        add(c.body, el("div", "simple-reply", String(plan.customer_reply)));
        add(c.body, el("p", "muted small", "Nobody reviewed this reply before it went out."));
        var det = el("details");
        add(det, el("summary", null, "what actually appeared on the customer's screen, including the failed tries"), el("pre", "terminal", t.stdout || "(nothing)"));
        c.body.appendChild(det);
      } else {
        add(c.body, el("p", "muted small", "This is exactly what appeared on the customer's screen, including every failed try:"));
        add(c.body, el("pre", "terminal", t.stdout || "(nothing printed)"));
      }
      return c.root;
    }
    if (t.checkpoint === "01") {
      if (t.outcome === "failed" && t.user_message) {
        add(c.body, el("div", "simple-reply", t.user_message));
        add(c.body, el("p", "muted small", "A standard safe message. The technical reason stays with the engineers."));
      } else {
        add(c.body, el("p", "muted", "Nothing yet. Sorting the ticket is an internal step; replies, refunds and approvals come at the next checkpoint."));
      }
      return c.root;
    }
    var st = t.state, f = t.facts;
    if (t.outcome !== "completed") {
      add(c.body, el("div", "simple-reply", st.user_visible_error || "(no message)"));
      add(c.body, el("p", "muted small", "A standard safe message. The technical reason stays with the engineers."));
    } else {
      add(c.body, el("div", "simple-reply", st.customer_reply_draft || "(no reply drafted)"));
      if (f.guard_output) add(c.body, el("p", "warn small", "This is the standard safe message: the AI's own draft was held back because it promised something the plan does not include."));
      else if (st.approval_status === "rejected") add(c.body, el("p", "muted small", "The plan was rejected, so the reply promises nothing and hands the ticket to a specialist."));
    }
    return c.root;
  }

  function decisionCard(t) {
    var c = card("The decision");
    var s = t.summary;
    if (t.checkpoint === "00") {
      var r = t.refund || {}, order = r.order;
      add(c.body, facts([
        fact("ticket is about", s.category ? titleCase(s.category) : "—"),
        fact("refund", r.amount === null || r.amount === undefined ? "none" : amount(r), r.fired ? "big " + (r.exceeds_order_total ? "bad" : "warn") : "big"),
        fact("order worth", order ? money(order.total_usd) : "no order number in the ticket", "big"),
        fact("approved by", "nobody"),
        fact("checked by", "nothing")
      ]));
      return c.root;
    }
    if (t.checkpoint === "01") {
      var r1 = t.result;
      if (!r1) { add(c.body, el("p", "muted", "No decision: the AI's answer could not be used.")); return c.root; }
      add(c.body, facts([
        fact("ticket is about", titleCase(r1.category), "big"), fact("priority", r1.priority, "big"),
        fact("customer sounds", r1.sentiment, "big"), fact("AI confidence", pct(r1.confidence), r1.confidence < 0.5 ? "warn" : "")
      ]));
      return c.root;
    }
    var st = t.state, plan = st.resolution_plan, a = t.approval || {}, order2 = st.order_data;
    if (!plan) { add(c.body, el("p", "muted", "No plan was made: the run stopped before that step.")); return c.root; }
    var approval = !a.required ? ((plan.actions || []).length ? "not needed: small refund, no replacement" : "—")
      : a.decision ? a.decision + " " + who(a) : "still waiting";
    var gate = L.gateCase(L.gateInput(st)), asked = plan.requested_refund_usd;
    add(c.body, facts([
      fact("ticket is about", titleCase(plan.category), "big"),
      fact("what the bot decided", (plan.actions || []).map(function (x) { return ACTION_WORDS[x.type] || titleCase(x.type); }).join("; ") || "nothing", "big"),
      fact("customer asked for", asked === null || asked === undefined ? "no amount" : money(asked), "big" + (gate.over ? " bad" : "")),
      fact("refund", plan.refund_amount_usd === null || plan.refund_amount_usd === undefined ? "none" : money(plan.refund_amount_usd), "big"),
      fact("order worth", order2 ? money(order2.total_usd) : "no order found"),
      st.customer_match === false ? fact("whose order", "someone else's", "bad") : null,
      fact("a person's approval", approval),
      fact("AI confidence", pct(plan.confidence), plan.confidence < 0.5 ? "warn" : ""),
      a.required ? reasonsFact("why a person was asked", gate.reasons) : null,
      plan.rationale ? quoteFact("the AI's reasoning", plan.rationale) : null,
      t.checkpoint >= "03" ? citesFact(cited(t)) : null
    ]));
    return c.root;
  }

  function checksCard(t) {
    var c = card("Safety checks");
    var items = [];
    if (t.checkpoint === "00") {
      var r = t.refund || {};
      items = [
        { ok: false, text: "The answer was checked before it was used: no" },
        { ok: false, text: "The order was looked up before refunding: no" },
        { ok: false, text: "The refund was limited to the order total: no" + (r.exceeds_order_total ? " (and it went over)" : "") },
        { ok: false, text: "A person approved the refund: no" },
        { ok: false, text: "A record was kept for later: no" }
      ];
    } else if (t.checkpoint === "01") {
      var props = (t.schema && t.schema.properties) || {};
      function choices(field, fallback) { return ((props[field] && props[field].enum) || fallback).map(function (x) { return x.replace(/_/g, " "); }).join(", "); }
      var formatRule = "The AI must reply with exactly four things: a category (one of " +
        choices("category", ["refund_request", "replacement", "shipping_delay", "product_defect", "account", "other"]) +
        "), a priority (" + choices("priority", ["low", "medium", "high", "urgent"]) +
        "), how the customer sounds (" + choices("sentiment", ["calm", "frustrated", "angry"]) +
        ") and a confidence score from 0 to 1. Anything else, like a made-up category or a missing field, is rejected.";
      var tries = (t.attempts || []).length;
      var code = t.error && t.error.code;
      var problem = code === "LLM_TIMEOUT" ? "the AI service took too long to answer"
        : code === "LLM_RATE_LIMITED" ? "the AI service refused because of too many requests"
        : code === "REPAIR_EXHAUSTED" || code === "LLM_MALFORMED_OUTPUT" ? "the AI's answer was in the wrong format, even after being asked to fix it"
        : "the AI service had a problem";
      items = [
        { ok: t.outcome === "validated", text: "The answer was checked against a required format: " + (t.outcome === "validated" ? "yes" : "it failed the check, so it was not used"),
          detail: formatRule + (t.outcome === "validated" && tries > 1 ? " This time the first answer was rejected, the AI was told what was wrong, and its second answer passed." : "") +
            (t.outcome === "failed" ? " The AI gets one chance to correct a bad answer; after that the bot stops instead of guessing." : "") },
        { ok: t.outcome !== "crashed", text: t.outcome === "crashed" ? "The customer was protected from the error: no" : "The customer is protected from technical errors: yes",
          detail: t.outcome === "crashed" ? "Something unexpected broke the program itself. The customer gets no answer at all, and the engineers get a raw error dump."
            : t.outcome === "failed" ? "It happened on this run: " + problem + ". The customer did not see an error code or a crash. They got this instead: “" + (t.user_message || "a standard safe message") + "” The technical reason (" + code + ") went to the log for the engineers."
            : "Nothing went wrong on this run. If the AI service had timed out, been overloaded, or kept answering in the wrong format, the customer would get a plain “we've passed this to a person” message, never an error code or a broken page." },
        { ok: !!t.request_id, text: "A record was kept for later: " + (t.request_id ? "yes" : "no"),
          detail: t.request_id ? "Every step of this run was written to the log file (logs/runs.jsonl) under reference number " + t.request_id + ", with how long it took and what it cost. If a customer complains, an engineer can search for that number and see exactly what happened."
            : "No reference number was created, so this run cannot be looked up later." },
        { ok: null, text: "Money: this step cannot refund or ship anything", detail: "Sorting a ticket only labels it. Refunds and replacements come in later checkpoints." }
      ];
    } else {
      var s = t.summary, st = t.state, f = t.facts, a = t.approval || {}, plan = st.resolution_plan;
      var refundPlanned = (s.actions || []).indexOf("issue_refund") !== -1;
      if (t.checkpoint === "04") {
        var ranGuard = t.nodes.some(function (n) { return n.node === "guard_input"; });
        var checker = t.guard === "nemo" ? " (by the NeMo guard, an AI safety check on every ticket)" : " (by the pattern guard, a keyword filter with an AI check only on a match)";
        items.push({ ok: ranGuard ? true : null, text: "The ticket was checked for suspicious content" + (ranGuard ? checker : "") + ": " + (f.guard_input ? "yes, and it was caught" : ranGuard ? "yes, nothing found" : "not reached") });
      }
      items.push({ ok: plan ? (s.validated ? true : false) : null, text: "The plan was checked against the business rules: " + (plan ? (s.validated ? "yes" + (s.repairs ? ", after " + s.repairs + " correction" + (s.repairs === 1 ? "" : "s") : "") : "it kept failing") : "no plan was made") });
      if (refundPlanned) {
        items.push({ ok: s.order_found, text: "The order was looked up before refunding: " + (s.order_found ? "yes" : "no order found, so no refund is allowed") });
        items.push({ ok: s.order_found && s.refund_amount <= s.order_total, text: "The refund was limited to the order total: " + (s.order_found ? (s.refund_amount <= s.order_total ? "yes" : "no") : "—") });
      } else if (plan) {
        items.push({ ok: null, text: "Money: the plan does not include a refund" });
      }
      var gate = L.gateCase(L.gateInput(st));
      if (gate.over) items.push({ ok: !refundPlanned, text: "A request above the order total went to a person: " + (refundPlanned ? "no, the bot refunded anyway" : "yes"),
        detail: "The customer asked for " + money(plan.requested_refund_usd) + " on an order worth " + money((st.order_data || {}).total_usd) + ". The bot may not shrink an ask like that into an amount that passes the rules." });
      var more = gate.over ? gate.reasons.slice(1) : gate.reasons; // the over-total reason has its own line above
      if (a.required) items.push({ ok: a.decision === "approved" ? true : a.decision === "rejected" ? false : null, text: "A person approved it: " + (a.decision ? a.decision + " " + who(a) : "still waiting"),
        detail: more.length ? (gate.over ? "Also: " : "Why a person was asked: ") + more.map(function (r) { return r.text.charAt(0).toLowerCase() + r.text.slice(1, -1); }).join("; ") + "." : null });
      else if (plan) items.push({ ok: null, text: "A person's approval: not needed (under $50, no replacement)" });
      if (t.checkpoint >= "03" && plan) {
        var needs = (plan.actions || []).some(function (x) { return x.type === "issue_refund" || x.type === "send_replacement"; });
        var cites = cited(t);
        items.push({ ok: cites.length ? true : (needs ? false : null), text: "The decision points to a policy section: " + (cites.length ? "yes (" + citedLabels(cites, 2) + ")" : "no") });
      }
      if (t.checkpoint === "04" && st.customer_reply_draft) items.push({ ok: !f.guard_output, text: "The reply was checked before sending: " + (f.guard_output ? "it failed and was replaced by a safe message" : "yes") });
      items.push({ ok: true, text: "A record was kept for later: yes" });
    }
    c.body.appendChild(L.checklist(items));
    return c.root;
  }

  function render(t) {
    var root = el("div");
    var v = verdict(t);
    var cp = S.cpById[t.checkpoint];
    add(root, head({ title: v[0], tone: v[1], badge: v[2], sub: (t.label || "") + " · checkpoint " + t.checkpoint + (cp ? " (" + cp.name + ")" : "") }).root);
    var grid = el("div", "simple-grid");
    var left = el("div", "col"), right = el("div", "col");
    var j = card("What the bot did");
    j.body.appendChild(journey(t.checkpoint === "00" ? journey00(t) : t.checkpoint === "01" ? journey01(t) : journeySvc(t.node_order, t.nodes, t)));
    add(left, j.root, checksCard(t));
    add(right, customerCard(t), decisionCard(t));
    add(grid, left, right);
    add(root, grid, footer(t.summary || {}));
    return root;
  }

  /* ── the live view ──────────────────────────────────────── */

  // opts.followed: a request_id the lab attaches to instead of submitting a ticket.
  function live(cp, opts) {
    opts = opts || {};
    var root = el("div");
    var cpInfo = S.cpById[cp];
    var ticketText = opts.followed ? null : S.sel && S.sel.text;
    var h = head({ title: opts.followed ? "Looking up that request…" : "Working on the ticket…", tone: "live", badge: "Running",
      sub: (opts.followed ? "request " + opts.followed : S.sel.label) + " · checkpoint " + cp + (cpInfo ? " (" + cpInfo.name + ")" : "") });
    var grid = el("div", "simple-grid");
    var left = el("div", "col"), right = el("div", "col");
    var j = card("What the bot is doing");
    var jBox = el("div");
    j.body.appendChild(jBox);
    var approvalBox = el("div");
    var c = card(cp === "00" ? "What the customer sees right now" : "What the customer gets");
    var reply = el("div", cp === "00" ? "terminal" : "simple-reply muted", cp === "00" ? "" : cp === "01" ? "Nothing yet: sorting the ticket is an internal step." : "The reply appears here as it is written…");
    c.body.appendChild(reply);
    add(left, j.root);
    add(right, approvalBox, c.root);
    add(grid, left, right);
    var foot = el("div", "simple-foot");
    var elapsed = el("span", null, "working…");
    add(foot, elapsed);
    add(root, h.root, grid, foot);

    var order = (cpInfo && cpInfo.nodes) || [];
    var rows = [], attempts = [], pseudo = { approval: null }, runId = null, text = "", t0 = Date.now(), done = false;
    var timer = setInterval(function () { if (!done) elapsed.textContent = "working for " + ((Date.now() - t0) / 1000).toFixed(1) + "s"; }, 100);
    function redraw() {
      jBox.textContent = "";
      jBox.appendChild(journey(cp === "00" ? journey00({ attempts: attempts }, true) : cp === "01" ? journey01({ attempts: attempts }, true) : journeySvc(order, rows, pseudo)));
    }
    function showApproval(plan) {
      approvalBox.textContent = "";
      var p = card("A person has to decide", "simple-card-warn");
      add(p.body, el("p", null, "The bot wants to " + ((plan.actions || []).map(function (x) { return ACTION_WORDS[x] || titleCase(x); }).join(" and ") || "do nothing") +
        (plan.refund_amount_usd !== null && plan.refund_amount_usd !== undefined ? " (" + money(plan.refund_amount_usd) + ")" : "") + ". Nothing happens until someone clicks."));
      var over = L.gateCase(plan).over, none = function (v, text) { return v === null || v === undefined ? text : money(v); };
      add(p.body, facts([
        fact("customer asked for", none(plan.requested_refund_usd, "no amount"), "big" + (over ? " bad" : "")),
        fact("order worth", none(plan.order_total_usd, "no order found"), "big"),
        fact("bot would refund", none(plan.refund_amount_usd, "nothing"), "big"),
        plan.customer_match === false ? fact("whose order", "someone else's", "bad") : null
      ]));
      p.body.appendChild(L.gateDetails(plan, { ticket: ticketText }));
      var row = el("div", "approval-actions");
      var ok = el("button", "btn btn-good", "Approve"), no = el("button", "btn btn-bad", "Reject"), res = el("span", "muted small");
      ok.type = no.type = "button";
      function decide(approved) {
        ok.disabled = no.disabled = true;
        res.textContent = "sending…";
        L.postJSON("/api/runs/" + runId + "/approve", { approved: approved }).then(function () {
          res.textContent = approved ? "approved, carrying on" : "rejected, carrying on";
        }).catch(function (e) { res.textContent = "failed: " + e.message; ok.disabled = no.disabled = false; });
      }
      ok.onclick = function () { decide(true); };
      no.onclick = function () { decide(false); };
      add(row, ok, no, res);
      p.body.appendChild(row);
      approvalBox.appendChild(p.root);
    }
    redraw();
    return {
      root: root,
      onEvent: function (ev) {
        if (ev.type === "start") { runId = ev.run_id; if (ev.followed) ticketText = ev.ticket_text; }
        else if (ev.type === "service.down") {
          h.title.textContent = "The service stopped answering. Nothing is lost: the lab keeps trying for " + ev.retry_s + " seconds while it comes back.";
          h.badge.textContent = "Service down"; h.badge.className = "simple-badge badge-warn"; h.root.className = "simple-head tone-warn";
        }
        else if (ev.type === "service.up") {
          h.title.textContent = "The service is back. Carrying on where it stopped…";
          h.badge.textContent = "Running"; h.badge.className = "simple-badge badge-live"; h.root.className = "simple-head tone-live";
        }
        else if (ev.type === "stdout" && cp === "00") { text += ev.text; reply.textContent = text; reply.scrollTop = reply.scrollHeight; }
        else if (ev.type === "attempt.started") {
          if (cp === "00") attempts.push({ n: ev.n }); else attempts.push({ n: ev.n, done: false });
          h.title.textContent = ev.n === 1 ? "Asking the AI…" : "The answer could not be used. Asking again…";
          redraw();
        }
        else if (ev.type === "attempt.finished") {
          if (cp === "00") attempts[ev.attempt.n - 1] = ev.attempt;
          else if (attempts[ev.n - 1]) { attempts[ev.n - 1].done = true; attempts[ev.n - 1].error = ev.error; }
          redraw();
        }
        else if (ev.type === "attempt.exception") { if (attempts[ev.n - 1]) attempts[ev.n - 1].exception = ev.exception; redraw(); }
        else if (ev.type === "sse") {
          var d = ev.data || {};
          if (ev.event === "node.started") { rows.push({ node: d.node, latency_ms: null }); h.title.textContent = (LIVE_WORDS[d.node] || STEP_WORDS[d.node] || titleCase(d.node)) + "…"; redraw(); }
          else if (ev.event === "node.completed") {
            var r = null; rows.forEach(function (x) { if (x.node === d.node && x.latency_ms === null) r = x; });
            if (r) r.latency_ms = d.latency_ms; else rows.push({ node: d.node, latency_ms: d.latency_ms });
            redraw();
          }
          else if (ev.event === "approval.required") { pseudo.approval = { required: true, decision: null }; var why0 = L.gateCase(d).reasons[0].text; h.title.textContent = "Paused: " + why0.charAt(0).toLowerCase() + why0.slice(1) + " A person has to decide."; h.badge.textContent = "Waiting for a decision"; h.badge.className = "simple-badge badge-warn"; h.root.className = "simple-head tone-warn"; pseudo.plan = d; redraw(); }
          else if (ev.event === "reply.delta") { if (reply.classList.contains("muted")) { reply.textContent = ""; reply.classList.remove("muted"); } reply.textContent += d.text || ""; }
          else if (ev.event === "run.completed" || ev.event === "run.failed") { done = true; h.title.textContent = "Finished. Writing it up…"; }
        }
        else if (ev.type === "approval.waiting") showApproval(pseudo.plan || {});
        else if (ev.type === "approval.decided") {
          pseudo.approval = { required: true, decision: ev.approved ? "approved" : "rejected", by: ev.by };
          approvalBox.textContent = "";
          h.title.textContent = (ev.approved ? "Approved" : "Rejected") + " " + who(pseudo.approval) + ". Carrying on…";
          h.badge.textContent = "Running"; h.badge.className = "simple-badge badge-live"; h.root.className = "simple-head tone-live";
          redraw();
        }
        else if (ev.type === "trace" || ev.type === "error") { done = true; clearInterval(timer); if (ev.type === "error") { h.title.textContent = "The lab could not finish this run."; h.badge.textContent = "Lab error"; h.badge.className = "simple-badge badge-bad"; h.root.className = "simple-head tone-bad"; } }
      }
    };
  }

  initMode();
  L.report = report;
  L.renderSimple = render;
  L.liveSimple = live;
  L.simpleVerdict = verdict;
  L.setSimple = setMode;
})();
