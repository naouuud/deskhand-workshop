/* Deskhand Lab — checkpoint 00 (the broken bot): live view and final report. */

/* 00 has no server and no log, so everything here comes from the 00 runner
   (lab/runners/cp00.py). It runs bot.main() unmodified in a subprocess,
   records each LLM call through a proxy client, forwards bot.py's prints as
   stdout events, and finds the "refund issued" line by tracing main().
   The trace carries attempts (raw output, tokens, prompt sha256, the
   swallowed exception), stdout, plan, refund {fired, amount, order,
   exceeds_order_total} and prompt.

   The report, top to bottom: headline, stepper (one node per attempt, then
   resolve() and the refund), what the customer saw beside what really
   happened (attemptCard, outcomeNote), the refund (refundPanel) beside the
   prompt and its cost, and debugSection: what v1 leaves you to debug with.
   The stdout and raw model output go in as text: the model can echo an
   injection payload from the ticket.
   Reads from L: S and the DOM helpers, headline, stepper, panel, checklist,
   outcomeInfo, errorPanel, commandsSection, runOn. */
(function () {
  "use strict";
  var S = L.S,
    el = L.el,
    add = L.add,
    kv = L.kv,
    pre = L.pre,
    chip = L.chip,
    usd = L.usd,
    money = L.money,
    ms = L.ms,
    num = L.num,
    short = L.short,
    jsonish = L.jsonish,
    panel = L.panel,
    note = L.note,
    citeLink = L.citeLink;

  // The headline sentence and tone, one case per outcome.
  function verdict(t) {
    var r = t.refund || {},
      n = (t.attempts || []).length,
      order = r.order || {};
    if (t.outcome === "gave_up")
      return [
        "Gave up after " +
          n +
          " identical retries. The customer watched every attempt; nothing was logged.",
        "bad",
      ];
    if (t.outcome === "crashed")
      return [
        "bot.main() crashed: in production that is a 500 with no context.",
        "bad",
      ];
    if (t.outcome === "parsed_non_object")
      return [
        "json.loads returned a " +
          typeof t.plan +
          ", not a plan. There is no schema, so any JSON “passes”.",
        "bad",
      ];
    if (t.outcome === "not_called") return ["resolve() never ran.", "bad"];
    if (r.fired) {
      if (r.exceeds_order_total)
        return [
          "Refund of " +
            jsonish(r.amount) +
            " “issued” with no checks: " +
            money(r.amount_parsed - order.total_usd) +
            " more than the order total of " +
            money(order.total_usd) +
            ".",
          "bad",
        ];
      return [
        "Refund of " +
          jsonish(r.amount) +
          " “issued” with no checks. Within the order total this time, by luck, not by design.",
        "warn",
      ];
    }
    return [
      "json.loads succeeded on attempt " +
        n +
        ". Nothing validated the plan: category is free text, the reply is unreviewed.",
      "warn",
    ];
  }

  // 00 has no pipeline, so the stepper shows one node per LLM attempt. The
  // live view passes only the attempts so far; the final report adds what
  // resolve() returned and whether main() "issued" the refund.
  function steps(t, live) {
    var out = [];
    (t.attempts || []).forEach(function (a) {
      out.push({
        name: "a" + a.n,
        label: "attempt " + a.n,
        state:
          a.exception || a.api_error
            ? "error"
            : a.latency_ms === undefined && live
              ? "active"
              : "done",
        sub: a.latency_ms === undefined ? "" : ms(a.latency_ms),
        title: a.exception ? a.exception.type : "",
      });
    });
    if (!live) {
      out.push({
        name: "resolve",
        label: "resolve() →",
        state: t.outcome === "parsed" ? "done" : "error",
        sub:
          t.outcome === "parsed"
            ? "dict"
            : t.outcome === "gave_up"
              ? "None"
              : t.outcome,
      });
      var r = t.refund || {};
      out.push({
        name: "refund",
        label: "refund issued!",
        state: r.fired ? (r.exceeds_order_total ? "error" : "warn") : "skip",
        sub: r.fired ? jsonish(r.amount) : "skipped",
      });
    }
    return out;
  }

  /* ── live ── */
  // Left: a terminal that replays bot.py's stdout as it streams, which is
  // what the customer sees. Right: one card per attempt, so the class
  // watches identical retries pile up. The stepper is rebuilt on each event.
  function live(out) {
    out.textContent = "";
    var head = L.headline({
      run_id: "…",
      checkpoint: "00",
      label: S.sel.label,
      verdict: "Running bot.main() on this ticket…",
      tone: "live",
      pill: ["running", "live"],
      stats: [
        ["attempts", "0"],
        ["elapsed", "0.0s"],
      ],
    });
    var stepBox = el("div");
    var cols = el("div", "columns");
    var c1 = el("div", "col"),
      c2 = el("div", "col");
    var term = el("pre", "terminal");
    var caret = el("span", "caret", "▋");
    term.appendChild(caret);
    var p1 = panel({
      title: "What the customer sees",
      sub: "bot.py prints the raw stream straight to the customer, live",
    });
    p1.body.appendChild(term);
    var attemptsBox = el("div");
    var p2 = panel({
      title: "What is actually happening",
      sub: "recorded by the lab around bot.resolve()",
    });
    add(
      p2.body,
      attemptsBox,
      el("p", "muted small", "Waiting for the first call…"),
    );
    add(c1, p1.root);
    add(c2, p2.root);
    add(cols, c1, c2);
    add(out, head.root, stepBox, cols);
    var cards = {},
      text = "",
      attempts = [],
      t0 = Date.now();
    var timer = setInterval(function () {
      head.stats.children[1].querySelector(".v").textContent =
        ((Date.now() - t0) / 1000).toFixed(1) + "s";
    }, 100);
    function redraw() {
      stepBox.textContent = "";
      stepBox.appendChild(L.stepper(steps({ attempts: attempts }, true)).root);
      head.stats.children[0].querySelector(".v").textContent = String(
        attempts.length,
      );
    }
    return {
      onEvent: function (ev) {
        if (ev.type === "start") {
          head.top.querySelector("code").textContent = ev.run_id;
        } else if (ev.type === "stdout") {
          // Reset the whole text each time (as text, never HTML), then put
          // the blinking caret back at the end.
          text += ev.text;
          term.textContent = text;
          term.appendChild(caret);
          term.scrollTop = term.scrollHeight;
        } else if (ev.type === "attempt.started") {
          var last = p2.body.querySelector("p.muted");
          if (last) last.remove();
          attempts.push({ n: ev.n });
          redraw();
          var card = el("div", "attempt live");
          var h = el("div", "attempt-head");
          add(
            h,
            el("b", null, "Attempt " + ev.n),
            el("span", "spinner"),
            ev.n === 1
              ? null
              : chip(
                  ev.identical_to_first
                    ? "identical prompt to attempt 1"
                    : "prompt changed",
                  ev.identical_to_first ? "chip-warn" : "chip-good",
                ),
          );
          add(card, h);
          attemptsBox.appendChild(card);
          cards[ev.n] = card;
        } else if (ev.type === "attempt.finished") {
          var a = ev.attempt,
            cd = cards[a.n];
          attempts[a.n - 1] = a;
          redraw();
          if (!cd) return;
          cd.classList.remove("live");
          var sp = cd.querySelector(".spinner");
          if (sp) sp.remove();
          var hh = cd.querySelector(".attempt-head");
          add(
            hh,
            chip(ms(a.latency_ms)),
            a.usage
              ? chip(
                  num(a.usage.prompt_tokens) +
                    " in / " +
                    num(a.usage.completion_tokens) +
                    " out",
                )
              : null,
            a.finish_reason ? chip("finish: " + a.finish_reason) : null,
          );
          if (a.api_error) add(cd, el("div", "exc", a.api_error));
        } else if (ev.type === "attempt.exception") {
          var c = cards[ev.n];
          if (attempts[ev.n - 1]) attempts[ev.n - 1].exception = ev.exception;
          redraw();
          if (c) {
            var x = el("div", "exc");
            add(
              x,
              el("span", "etype", ev.exception.type + ": "),
              ev.exception.message,
              el(
                "div",
                "muted tiny",
                "swallowed by the bare except — retrying the same prompt",
              ),
            );
            c.appendChild(x);
          }
        } else if (ev.type === "trace" || ev.type === "error") {
          clearInterval(timer);
          if (ev.type === "error")
            add(
              out,
              L.errorPanel(ev.message, ev.detail, {
                needs_service: ev.needs_service,
              }),
            );
        }
      },
    };
  }

  /* ── final ── */
  // c holds this checkpoint's code links by name (c.bare_except, c.give_up,
  // ...), sent with the trace; missing names give no link.
  function render(t, out) {
    out.textContent = "";
    var c = t.cites || {};
    var attempts = t.attempts || [];
    var u1 = (attempts[0] && attempts[0].usage) || {};
    var v = verdict(t);
    add(
      out,
      L.headline({
        run_id: t.run_id,
        checkpoint: "00",
        label: t.label,
        verdict: v[0],
        tone: v[1],
        pill: L.outcomeInfo(t),
        stats: [
          ["LLM calls", attempts.length],
          ["cost", usd(t.cost_usd)],
          ["wall time", ms(t.duration_ms)],
          ["model", t.model],
          ["temperature", attempts[0] ? attempts[0].temperature : "—"],
          ["request id", "none"],
          ["log lines", "0"],
          ["startup", L.startup(t.startup)],
        ],
      }).root,
    );
    add(out, L.stepper(steps(t)).root);

    var cols = el("div", "columns");
    var c1 = el("div", "col"),
      c2 = el("div", "col");

    var p1 = panel({
      title: "What the customer saw",
      sub: "everything bot.py printed, in order",
      cites: [c.stream_print, c.bare_except, c.give_up, c.refund_issued],
      why:
        'In v1, stdout <em>is</em> the customer channel: <code>print(delta, end="", flush=True)</code> sends every token the moment it arrives. ' +
        "So the customer sees markdown fences, half-built JSON, the bot's own <em>“hmm, something went wrong, retrying…”</em>, and every retry streamed again. " +
        "That's v1 incident #3: partial failures streamed to the screen. From checkpoint 02 on, only the reply text streams and machine output arrives whole or not at all.",
    });
    p1.body.appendChild(el("pre", "terminal", t.stdout || "(nothing printed)"));
    add(c1, p1.root);

    var p2 = panel({
      title: "What actually happened",
      sub: "each call, its raw output, and the exception bot.py threw away",
      cites: [c.loop, c.json_loads, c.bare_except, c.give_up, c.return_none],
      why:
        "Every retry resent the <em>identical</em> prompt (same SHA-256) at temperature 1.0. A bare <code>except Exception:</code> can't tell a markdown fence from a rate limit from a bug, " +
        "so it does the same thing for all of them: try again, unchanged, then return <code>None</code>. That's incident #4, <em>retries are not always effective</em>. " +
        "Checkpoint 01 replaces this with one bounded repair that tells the model what was wrong.",
    });
    var allSame =
      attempts.length > 1 &&
      attempts.every(function (a) {
        return a.identical_to_first;
      });
    if (attempts.length > 1) {
      add(
        p2.body,
        note(
          allSame ? "warn" : "good",
          allSame
            ? "All " +
                attempts.length +
                " attempts sent the identical prompt (sha256 " +
                short(attempts[0].sha256) +
                ", " +
                num(attempts[0].prompt_chars) +
                " chars)."
            : "The prompt changed between attempts.",
        ),
      );
    }
    attempts.forEach(function (a) {
      p2.body.appendChild(attemptCard(a, t, c));
    });
    p2.body.appendChild(outcomeNote(t, c));
    add(c2, p2.root);
    add(cols, c1, c2);
    add(out, cols);

    var cols2 = el("div", "columns");
    var d1 = el("div", "col"),
      d2 = el("div", "col");
    add(d1, refundPanel(t, c));

    // The bill: how much of the prompt is policy text, and the monthly
    // projection the server computed from this run's cost.
    var pr = t.prompt || {};
    var share = pr.chars
      ? Math.round((100 * pr.policies_chars) / pr.chars)
      : null;
    var totalIn = attempts.reduce(function (s, a) {
      return s + ((a.usage && a.usage.prompt_tokens) || 0);
    }, 0);
    var p3 = panel({
      title: "The prompt and the bill",
      sub: "one f-string: all six policies + the ticket + “Be generous”",
      cites: [
        c.prompt,
        c.policies_glob,
        c.generous,
        c.temperature,
        t.pricing && t.pricing.source,
      ],
      why:
        "<code>load_all_policies()</code> globs <em>every</em> policy file into every call, whatever the ticket is about, and the whole thing is resent on every retry. " +
        "No request ID, no token count, no cost tracking: v1 can't tell you what a resolution costs. The lab reads token counts from the provider and prices them with " +
        "checkpoint 01's <code>PRICE_TABLE</code> (v1 has none). That's incident #7 and README “Try this” #2; checkpoint 03 retrieves only the relevant sections.",
    });
    var k = el("div", "kvs");
    add(
      k,
      kv("prompt size", num(pr.chars) + " chars"),
      kv("policy text", share === null ? "—" : share + "% of it"),
      kv("tokens sent, all attempts", num(totalIn)),
      kv("cost per attempt", usd(attempts[0] && attempts[0].cost_usd)),
      kv("this run", usd(t.cost_usd)),
      kv(
        "at " +
          num(t.projection && t.projection.tickets_per_month) +
          " tickets/mo",
        t.projection ? money(t.projection.usd_per_month) : "—",
        "big",
      ),
    );
    p3.body.appendChild(k);
    var det = el("details");
    add(
      det,
      el(
        "summary",
        null,
        "the full prompt (" +
          num(pr.chars) +
          " chars, " +
          (pr.policy_titles || []).length +
          " policy documents)",
      ),
      pre(pr.text || ""),
    );
    p3.body.appendChild(det);
    if (t.pricing)
      add(
        p3.body,
        el(
          "div",
          "muted tiny",
          t.pricing.note +
            (t.pricing.per_million
              ? " — $" +
                t.pricing.per_million.input +
                " / $" +
                t.pricing.per_million.output +
                " per 1M tokens in/out for “" +
                t.pricing.model +
                "”."
              : " — this model isn't in that table, so it's priced at $0."),
        ),
      );
    add(d2, p3.root);
    add(cols2, d1, d2);
    add(out, cols2);

    add(out, debugSection(t, c));
    add(out, L.commandsSection(t.commands));
  }

  // One LLM call: its chips, the raw output, and the exception bot.py's bare
  // except threw away, with a plain diagnosis of the usual causes (a
  // markdown fence, empty output, prose instead of JSON).
  function attemptCard(a, t, c) {
    var card = el("div", "attempt");
    var h = el("div", "attempt-head");
    add(
      h,
      el("b", null, "Attempt " + a.n),
      chip(ms(a.latency_ms)),
      a.usage
        ? chip(
            num(a.usage.prompt_tokens) +
              " in / " +
              num(a.usage.completion_tokens) +
              " out",
          )
        : null,
      chip(usd(a.cost_usd)),
      a.finish_reason ? chip("finish: " + a.finish_reason) : null,
      a.n > 1
        ? chip(
            a.identical_to_first ? "identical prompt" : "prompt changed",
            a.identical_to_first ? "chip-warn" : "chip-good",
          )
        : null,
    );
    card.appendChild(h);
    add(
      card,
      el("div", "muted tiny", "raw model output"),
      pre(a.raw || "(no text)", "short"),
    );
    if (a.api_error) {
      var x0 = el("div", "exc");
      add(x0, el("span", "etype", "provider error: "), a.api_error);
      card.appendChild(x0);
    }
    if (a.exception) {
      var e = a.exception;
      var x = el("div", "exc");
      add(x, el("span", "etype", e.type + ": "), e.message);
      var where = el("div", "small");
      add(
        where,
        "raised in ",
        el("code", null, e.raised_in || "?"),
        ", reached ",
        citeLink(e.cite, null, true),
        " ",
        el("code", null, e.code),
        " → swallowed by the bare except; nothing was logged.",
      );
      x.appendChild(where);
      var trimmed = (a.raw || "").trim();
      var diag = null;
      if (/^```/.test(trimmed))
        diag =
          "The output is JSON wrapped in a markdown fence (```json … ```). json.loads wants bare JSON, so it fails on the first backtick. The prompt never asked for bare JSON, and nothing enforces a format.";
      else if (!trimmed) diag = "The stream produced no text at all.";
      else if (!/^[\[{]/.test(trimmed))
        diag = "The model answered in prose instead of JSON.";
      if (diag) add(x, el("div", "muted small", diag));
      card.appendChild(x);
    }
    return card;
  }

  function outcomeNote(t, c) {
    var n = (t.attempts || []).length;
    var box = note(t.outcome === "parsed" ? "warn" : "bad");
    if (t.outcome === "gave_up") {
      add(
        box,
        el(
          "b",
          null,
          n +
            " attempts, " +
            n +
            " swallowed exceptions → resolve() returned None. ",
        ),
        "main() printed “RESOLUTION: None”. No error code, no log line, no request ID.",
      );
    } else if (t.outcome === "parsed") {
      add(
        box,
        el("b", null, "json.loads succeeded on attempt " + n + ". "),
        "Nothing checked the fields: category is free text, refund_amount is whatever the model wrote, customer_reply is unreviewed.",
      );
    } else if (t.outcome === "crashed") {
      add(
        box,
        el("b", null, "bot.main() crashed. "),
        "The exception escaped main().",
      );
      box.appendChild(pre(t.crash || ""));
    } else if (t.outcome === "parsed_non_object") {
      add(
        box,
        el(
          "b",
          null,
          "json.loads returned a " + typeof t.plan + ", not an object. ",
        ),
        "There is no schema, so any valid JSON “passes”.",
      );
    } else {
      add(box, "resolve() never ran.");
      if (t.crash) box.appendChild(pre(t.crash));
    }
    return box;
  }

  function refundPanel(t, c) {
    var r = t.refund || {};
    var order = r.order;
    var p = panel({
      title: "The refund",
      sub: "what main() did with resolve()'s return value",
      cls: r.fired ? (r.exceeds_order_total ? "panel-bad" : "panel-warn") : "",
      cites: [c.refund_if, c.refund_issued, c.generous],
      why:
        '<code>main()</code> runs <code>if plan and plan.get("refund_amount")</code> and “issues” the refund: no order lookup, no cap at the order total, ' +
        "no human approval, no idempotency key. The prompt even says <em>“Be generous”</em>. That's incident #6, <em>a refund larger than the order total</em>. " +
        "Checkpoint 02 adds all four guards: business rules, the approval gate above $50, and an <code>Idempotency-Key</code> on every refund.",
    });
    var k = el("div", "kvs");
    add(
      k,
      kv(
        "category",
        t.plan && t.plan.category !== undefined
          ? jsonish(t.plan.category)
          : "—",
      ),
      kv(
        "refund_amount",
        r.amount === null || r.amount === undefined ? "—" : jsonish(r.amount),
        r.amount ? "big warn" : "big",
      ),
      kv(
        order ? "order " + order.order_id + " total" : "order total",
        order ? money(order.total_usd) : "no order id",
        "big",
      ),
    );
    p.body.appendChild(k);
    var items = [];
    items.push({
      ok: false,
      text:
        "order looked up: never (the lab fetched the total: " +
        (order ? order.source : "no order id in the ticket") +
        ")",
    });
    items.push({ ok: false, text: "amount capped at the order total: no" });
    items.push({ ok: false, text: "human approval: none" });
    items.push({
      ok: false,
      text: "idempotency key: none — a retry pays again",
    });
    if (r.fired === true) {
      items.unshift({
        ok: false,
        text:
          "bot.py printed “refund issued!” for " +
          jsonish(r.amount) +
          (r.exceeds_order_total
            ? " — " +
              money(r.amount_parsed - order.total_usd) +
              " over the order total"
            : ""),
      });
    } else if (r.fired === false) {
      items.unshift({
        ok: null,
        text:
          t.outcome === "gave_up" || t.plan === null
            ? "no refund — resolve() returned None, so main() skipped the branch (luck, not a guard)"
            : r.amount
              ? "the refund branch didn't run"
              : "no refund — refund_amount was " +
                jsonish(r.amount) +
                "; nothing would have stopped a bigger number",
      });
    } else {
      items.unshift({
        ok: null,
        text: "the lab couldn't find the “refund issued” line in bot.py, so it can't tell whether the refund ran",
      });
    }
    p.body.appendChild(L.checklist(items));
    if (
      r.fired &&
      !r.exceeds_order_total &&
      order &&
      order.total_usd !== null
    ) {
      add(
        p.body,
        el(
          "div",
          "muted small",
          "At temperature 1.0 that's a sample, not a guarantee. Try Repeat on this ticket.",
        ),
      );
    }
    if (t.plan !== null && t.plan !== undefined) {
      var det = el("details");
      add(
        det,
        el("summary", null, "the plan resolve() returned"),
        pre(JSON.stringify(t.plan, null, 2)),
      );
      p.body.appendChild(det);
    }
    return p.root;
  }

  function debugSection(t, c) {
    var n = (t.attempts || []).length;
    var swallowed = (t.attempts || []).filter(function (a) {
      return a.exception;
    }).length;
    var s = L.section("What v1 left you to debug with: nothing");
    add(
      s.body,
      el(
        "p",
        "muted small",
        "README “Try this” #3: a run just failed for a customer on the phone. Everything above was recorded by the lab from the outside. Without it you'd have the customer's screen and nothing else (incident #10).",
      ),
    );
    var tbl = el("table", "tbl");
    [
      ["request ID", "none"],
      ["log lines written", "0"],
      ["error code", "none — every failure is “hmm, something went wrong”"],
      [
        "exceptions kept",
        "0 of " + swallowed + " (the lab caught them; bot.py discarded them)",
      ],
      [
        "cost recorded",
        "none (the lab measured " +
          usd(t.cost_usd) +
          " over " +
          n +
          " call" +
          (n === 1 ? "" : "s") +
          ")",
      ],
      ["prompt / model version", "none"],
    ].forEach(function (row) {
      var tr = el("tr", "rowhead");
      add(tr, el("td", null, row[0]), el("td", "mono", row[1]));
      tbl.appendChild(tr);
    });
    s.body.appendChild(tbl);
    var b = el("button", "btn btn-sm", "Run this ticket on checkpoint 01 →");
    b.type = "button";
    b.style.marginTop = "10px";
    b.onclick = function () {
      L.runOn("01");
    };
    s.body.appendChild(b);
    return s.root;
  }

  L.live00 = live;
  L.render00 = render;
})();
