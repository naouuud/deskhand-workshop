/* Deskhand Lab — checkpoint 01 (triage): live view and final report. */

/* Like 00, 01 runs as a subprocess (lab/runners/cp01.py): main.main() on a
   one-ticket copy of the dataset, with pass-through wrappers that record
   each provider call, each repair and the user message. Fault injection
   (the Run tab's select, sent as inject_truncate) lowers max_tokens on the
   first one or two provider calls, so the class sees a real truncation, a
   real LLM_MALFORMED_OUTPUT and the real repair re-prompt.
   The trace adds result (the TriageResult), error {code, detail}, the
   request_id and the log lines main.py appended, billed and reported cost.

   The report: headline and stepper (attempt 1, repair 1, then the result
   or the error), the result or the safe failure beside the attempts, the
   log lines beside the cost, then the output contract in a closed section.
   This file also defines L.logTable, which cpsvc.js reuses.
   Reads from L: S and the DOM helpers, headline, stepper, panel, checklist,
   section, why, citesRow, outcomeInfo, errorPanel, copyBtn, commandsSection. */
(function () {
  "use strict";
  var S = L.S,
    el = L.el,
    add = L.add,
    kv = L.kv,
    pre = L.pre,
    chip = L.chip,
    usd = L.usd,
    ms = L.ms,
    num = L.num,
    panel = L.panel,
    note = L.note,
    citeLink = L.citeLink;

  // The headline sentence and tone: validated, failed safely, or crashed.
  function verdict(t) {
    var r = t.result,
      n = (t.attempts || []).length;
    if (t.outcome === "validated") {
      return [
        "Validated TriageResult: " +
          r.category +
          ", " +
          r.priority +
          " priority, " +
          r.sentiment +
          ", confidence " +
          Number(r.confidence).toFixed(2) +
          (n > 1 ? " — after one repair." : " — first try."),
        r.confidence < 0.5 ? "warn" : "good",
      ];
    }
    if (t.outcome === "failed")
      return [
        "Failed safely with " +
          t.error.code +
          ". The customer saw a safe message; the detail went to the log under a request ID.",
        "warn",
      ];
    return [
      "Crashed: an error escaped the taxonomy and propagated out of main.py.",
      "bad",
    ];
  }

  // Attempt 1, then one node per repair; the tooltip names the injected
  // fault. The final report adds the end state: TriageResult,
  // DeskhandError (failed safely) or crash.
  function steps(t, live) {
    var out = [];
    (t.attempts || []).forEach(function (a) {
      var call = a.provider_call || {};
      out.push({
        name: "a" + a.n,
        label: a.n === 1 ? "attempt 1" : "repair " + (a.n - 1),
        state: a.error ? "error" : a.done === false && live ? "active" : "done",
        sub: a.error
          ? a.error.code || "uncaught"
          : call.latency_ms !== undefined
            ? ms(call.latency_ms)
            : a.done === false
              ? ""
              : "parsed",
        title: call.injected || "",
      });
    });
    if (!live) {
      out.push({
        name: "end",
        label:
          t.outcome === "validated"
            ? "TriageResult"
            : t.outcome === "failed"
              ? "DeskhandError"
              : "crash",
        state:
          t.outcome === "validated"
            ? "done"
            : t.outcome === "failed"
              ? "warn"
              : "error",
        sub:
          t.outcome === "validated"
            ? "validated"
            : t.outcome === "failed"
              ? t.error.code
              : "",
      });
    }
    return out;
  }

  /* ── live ── */
  // Triage has nothing for the customer to watch, so the live view is a
  // running log of the runner's events: request_id, then each attempt and
  // how it ended. An injected attempt is tagged with the runner's
  // "max_tokens=5 (lab fault injection)" note.
  function live(out) {
    out.textContent = "";
    var head = L.headline({
      run_id: "…",
      checkpoint: "01",
      label: S.sel.label,
      verdict: "Running main.main() on this ticket…",
      tone: "live",
      pill: ["running", "live"],
      stats: [
        ["request id", "…"],
        ["elapsed", "0.0s"],
      ],
    });
    var stepBox = el("div");
    var p = panel({ title: "Live", sub: "events from the runner" });
    var log = el("div", "small mono");
    p.body.appendChild(log);
    add(out, head.root, stepBox, p.root);
    var attempts = [],
      t0 = Date.now();
    var timer = setInterval(function () {
      head.stats.children[1].querySelector(".v").textContent =
        ((Date.now() - t0) / 1000).toFixed(1) + "s";
    }, 100);
    function line(text, cls) {
      log.appendChild(el("div", cls || null, text));
    }
    function redraw() {
      stepBox.textContent = "";
      stepBox.appendChild(L.stepper(steps({ attempts: attempts }, true)).root);
    }
    return {
      onEvent: function (ev) {
        if (ev.type === "start")
          head.top.querySelector("code").textContent = ev.run_id;
        else if (ev.type === "request_id") {
          head.stats.children[0].querySelector(".v").textContent =
            ev.request_id;
          line("request_id " + ev.request_id + " (main.py generated it)");
        } else if (ev.type === "attempt.started") {
          attempts.push({
            n: ev.n,
            done: false,
            provider_call: { injected: ev.injected },
          });
          redraw();
          line(
            "attempt " +
              ev.n +
              " → structured_completion" +
              (ev.injected ? "   [" + ev.injected + "]" : ""),
            ev.injected ? "warn" : null,
          );
        } else if (ev.type === "attempt.finished") {
          var a = attempts[ev.n - 1];
          if (a) {
            a.done = true;
            a.error = ev.error;
          }
          redraw();
          line(
            "attempt " +
              ev.n +
              " ← " +
              (ev.error
                ? (ev.error.code || "uncaught") +
                  ": " +
                  (ev.error.detail || ev.error.uncaught || "").slice(0, 140)
                : "parsed into TriageResult"),
            ev.error ? "bad" : "good",
          );
        } else if (ev.type === "trace" || ev.type === "error") {
          clearInterval(timer);
          if (ev.type === "error")
            add(out, L.errorPanel(ev.message, ev.detail));
        }
      },
    };
  }

  /* ── final ── */
  function render(t, out) {
    out.textContent = "";
    var c = t.cites || {};
    var attempts = t.attempts || [];
    var call0 = (attempts[0] && attempts[0].provider_call) || {};
    var v = verdict(t);
    add(
      out,
      L.headline({
        run_id: t.run_id,
        checkpoint: "01",
        label: t.label,
        verdict: v[0],
        tone: v[1],
        pill: L.outcomeInfo(t),
        stats: [
          ["LLM calls", attempts.length],
          ["cost (billed)", usd(t.billed_cost_usd)],
          ["wall time", ms(t.duration_ms)],
          ["model", t.model],
          ["temperature", call0.temperature],
          ["request id", t.request_id || "—"],
          ["log lines", (t.log_lines || []).length],
          ["startup", L.startup(t.startup)],
        ],
      }).root,
    );
    add(out, L.stepper(steps(t)).root);
    if (t.injected) {
      add(
        out,
        note(
          "warn",
          "Lab fault injection: max_tokens=5 on the first " +
            t.injected.truncate_attempts +
            " provider call" +
            (t.injected.truncate_attempts === 1 ? "" : "s") +
            ". The truncation, the SDK error, and everything triage.py did about it are real.",
        ),
      );
    }

    var cols = el("div", "columns");
    var c1 = el("div", "col"),
      c2 = el("div", "col");
    add(c1, resultPanel(t, c));
    add(c1, logsPanel(t, c));
    add(c2, attemptsPanel(t, c));
    add(c2, costPanel(t, c));
    add(cols, c1, c2);
    add(out, cols);

    var det = L.section(
      "The output contract: schema, system prompt, terminal output",
    );
    add(det.body, contractBody(t, c, attempts));
    add(
      det.body,
      el("div", "sub", "what main.py printed for this one ticket"),
      el("pre", "terminal", t.stdout || "(nothing printed)"),
    );
    add(out, det.root);
    add(out, L.commandsSection(t.commands));
  }

  // One of three panels, by outcome: the validated result, the safe failure
  // (internal code beside the customer's message), or the crash traceback.
  function resultPanel(t, c) {
    if (t.outcome === "validated") {
      var r = t.result;
      var p = panel({
        title: "Validated TriageResult",
        sub: "the provider's structured-output mode, parsed by Pydantic",
        cls: "panel-good",
        cites: [c.triage_result, c.parse, c.parsed_none],
        why:
          "<em>Never trust LLM output directly.</em> The schema goes to the provider as <code>response_format=TriageResult</code> and the reply is parsed back into the Pydantic model, " +
          "so category, priority and sentiment can only be one of the allowed values. Confidence is the model's own estimate: README “Try this” #2 asks whether it's honest on vague tickets.",
      });
      var k = el("div", "kvs");
      add(
        k,
        kv("category", r.category, "big"),
        kv("priority", r.priority, "big"),
        kv("sentiment", r.sentiment, "big"),
        kv(
          "confidence",
          typeof r.confidence === "number"
            ? r.confidence.toFixed(2)
            : r.confidence,
          "big" + (r.confidence < 0.5 ? " warn" : ""),
        ),
      );
      p.body.appendChild(k);
      add(
        p.body,
        el(
          "div",
          "muted small",
          "Triage stops here. Refunds, replies and approvals are added by checkpoint 02's workflow.",
        ),
      );
      return p.root;
    }
    if (t.outcome === "failed") {
      var pf = panel({
        title: "Failed safely",
        sub: "an internal code for engineers, a separate message for the customer",
        cls: "panel-warn",
        cites: [
          c.exhausted,
          c.user_messages,
          c.user_message,
          c.main_user_message,
        ],
        why:
          "<em>Never expose raw failures to users.</em> The internal code and detail go to the log and the table; the customer text comes from " +
          "<code>errors.user_message(code)</code>, chosen per code, never from <code>str(exception)</code>. Compare 00, where the customer saw the raw stream and “hmm, something went wrong”.",
      });
      var cols = el("div", "columns");
      var a = note("bad"),
        b = note("good");
      add(
        a,
        el("div", "sub", "internal — logs and engineers"),
        el("div", "mono", t.error.code),
        pre(t.error.detail || "", "short"),
      );
      add(
        b,
        el("div", "sub", "customer-facing — what main.py printed"),
        el("div", null, t.user_message || "(main.py didn't call user_message)"),
      );
      add(cols, a, b);
      pf.body.appendChild(cols);
      return pf.root;
    }
    var pc = panel({
      title: "Crashed: an error escaped the taxonomy",
      cls: "panel-bad",
      cites: [c.length_error, c.rate_limited, c.not_repairable],
      why:
        "<code>llm.py</code> maps every provider exception to a taxonomy code: truncation to <code>LLM_MALFORMED_OUTPUT</code>, rate limits and timeouts to their own codes, " +
        "anything else (a bad key, a content-filter 400) to <code>INTERNAL_UNEXPECTED</code>. So a crash here came from outside the provider boundary: a key missing from <code>.env</code> " +
        "(<code>_require</code> raises before any call), or a bug. The traceback below says which.",
    });
    pc.body.appendChild(pre(t.crash || ""));
    return pc.root;
  }

  // One card per provider call. A repair card first shows the messages
  // appended to the conversation (what the model was told was wrong), then
  // the raw output and what triage.py did with it.
  function attemptsPanel(t, c) {
    var attempts = t.attempts || [];
    var p = panel({
      title: "Attempts and repair",
      sub:
        "MAX_REPAIR_ATTEMPTS = " +
        t.max_repair_attempts +
        " → at most " +
        (t.max_repair_attempts + 1) +
        " calls",
      cites: [
        c.max_repair,
        c.repair_message,
        c.not_repairable,
        c.repair_log,
        c.length_error,
      ],
      why:
        "Bounded repair, not blind retry. On malformed output, <code>triage.py</code> appends <em>what was wrong</em> to the same conversation and asks once more, then gives up with " +
        "<code>REPAIR_EXHAUSTED</code>. Only <code>LLM_MALFORMED_OUTPUT</code> is repaired: a rate limit or timeout surfaces with its own code, because re-prompting can't fix it. " +
        "Compare 00, which resent the identical prompt three times.",
    });
    attempts.forEach(function (a, i) {
      var call = a.provider_call || {};
      var card = el("div", "attempt");
      var h = el("div", "attempt-head");
      add(
        h,
        el("b", null, i === 0 ? "Attempt 1" : "Repair " + i),
        call.injected ? chip("injected: " + call.injected, "chip-warn") : null,
        chip(ms(call.latency_ms)),
        call.usage
          ? chip(
              num(call.usage.prompt_tokens) +
                " in / " +
                num(call.usage.completion_tokens) +
                " out",
            )
          : null,
        chip(usd(call.cost_usd)),
        call.finish_reason ? chip("finish: " + call.finish_reason) : null,
        chip(
          "max_tokens " +
            (call.injected
              ? "5 (was " + call.max_tokens + ")"
              : call.max_tokens),
        ),
      );
      card.appendChild(h);
      if (i > 0 && a.added_messages && a.added_messages.length) {
        add(
          card,
          el(
            "div",
            "muted tiny",
            "re-asked with — appended to the same conversation",
          ),
        );
        a.added_messages.forEach(function (m) {
          card.appendChild(pre("[" + m.role + "] " + m.content, "short added"));
        });
      }
      add(
        card,
        el("div", "muted tiny", "raw provider output"),
        pre(
          call.raw === null || call.raw === undefined ? "(none)" : call.raw,
          "short",
        ),
      );
      if (a.error) {
        var x = el("div", "exc");
        if (a.error.code) {
          add(x, el("span", "etype", a.error.code + ": "), a.error.detail);
          var next =
            i + 1 < attempts.length
              ? "→ triage.py logged triage.repair and re-prompted with this detail."
              : t.error && t.error.code === "REPAIR_EXHAUSTED"
                ? "→ repair budget spent: REPAIR_EXHAUSTED."
                : "→ not repairable: raised to main.py with its own code.";
          add(x, el("div", "muted small", next));
        } else {
          add(x, el("span", "etype", "uncaught: "), a.error.uncaught);
        }
        card.appendChild(x);
      } else {
        add(card, note("good", "Parsed into TriageResult."));
      }
      p.body.appendChild(card);
    });
    if (attempts.length === 1 && !attempts[0].error) {
      add(
        p.body,
        el(
          "div",
          "muted small",
          "No repair needed this time. Set fault injection to “truncate attempt 1” to watch the real repair loop run.",
        ),
      );
    }
    return p.root;
  }

  // Two numbers on purpose: "billed" sums every provider call the lab saw,
  // "reported" is what classify_ticket() handed back. They differ when a
  // call failed to parse, and that gap is the teaching point.
  function costPanel(t, c) {
    var calls = (t.attempts || []).map(function (a) {
      return a.provider_call || {};
    });
    var tin = calls.reduce(function (s, x) {
      return s + ((x.usage && x.usage.prompt_tokens) || 0);
    }, 0);
    var tout = calls.reduce(function (s, x) {
      return s + ((x.usage && x.usage.completion_tokens) || 0);
    }, 0);
    var p = panel({
      title: "Cost",
      sub: "priced by llm.py's PRICE_TABLE, per call",
      cites: [c.price_table, c.cost_fn, c.cost_add],
      why:
        "Every call is priced at the provider boundary (<code>llm._cost_usd</code>) and logged with its tokens. “Reported” is what <code>classify_ticket()</code> returned to main.py; " +
        "“billed” is the sum over every provider call the lab saw, including calls whose output failed to parse.",
    });
    var k = el("div", "kvs");
    add(
      k,
      kv("billed (all calls)", usd(t.billed_cost_usd, 6), "big"),
      kv(
        "reported by classify_ticket",
        t.reported_cost_usd === null
          ? "— (raised)"
          : usd(t.reported_cost_usd, 6),
        "big",
      ),
      kv("tokens in / out", num(tin) + " / " + num(tout)),
      kv("calls", calls.length),
    );
    p.body.appendChild(k);
    var gap = (t.billed_cost_usd || 0) - (t.reported_cost_usd || 0);
    if (t.reported_cost_usd === null && t.billed_cost_usd) {
      add(
        p.body,
        note(
          "warn",
          "classify_ticket raised before returning a cost, so main.py's total doesn't include this ticket — the provider still billed " +
            usd(t.billed_cost_usd, 6) +
            ".",
        ),
      );
    } else if (gap > 1e-9) {
      var nb = note("warn");
      add(
        nb,
        "triage.py adds a call's cost only after it parses (",
        citeLink(c.cost_add, null, true),
        "), so the failed attempt's " +
          usd(gap, 6) +
          " was billed but isn't in the reported cost.",
      );
      p.body.appendChild(nb);
    }
    return p.root;
  }

  function logsPanel(t, c) {
    var p = panel({
      title: "request_id and its log lines",
      sub:
        (t.log_file || "logs/runs.jsonl") +
        " — only the lines this run appended",
      cites: [
        c.main_request_id,
        c.main_run_started,
        c.log_path,
        c.log_event,
        c.completed_log,
        c.repair_log,
      ],
      why:
        "One <code>request_id</code> per run, one JSON object per line, appended to <code>logs/runs.jsonl</code>. A failed run is now greppable: " +
        "copy the ID and use the command under “Do the same in the terminal”. Searching the whole log stays in the terminal on purpose; the lab only shows what this run wrote.",
    });
    var row = el("div", "cmd-head");
    add(
      row,
      el("code", null, t.request_id || "— (never generated)"),
      t.request_id ? L.copyBtn(t.request_id) : null,
    );
    p.body.appendChild(row);
    p.body.appendChild(logTable(t.log_lines));
    return p.root;
  }

  // The log lines one run appended to logs/runs.jsonl: time, event, and the
  // remaining fields as JSON (ts, request_id and event have their own
  // columns or are the same on every row). cpsvc.js uses it as L.logTable.
  function logTable(lines) {
    var tbl = el("table", "tbl");
    var hr = el("tr");
    ["time (UTC)", "event", "fields"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    tbl.appendChild(hr);
    (lines || []).forEach(function (rec) {
      var rest = {};
      Object.keys(rec).forEach(function (k) {
        if (["ts", "request_id", "event"].indexOf(k) === -1) rest[k] = rec[k];
      });
      var tr = el("tr");
      add(
        tr,
        el("td", "mono nowrap", String(rec.ts || "").slice(11, 23)),
        el("td", "mono", rec.event),
        el("td", "mono", JSON.stringify(rest)),
      );
      tbl.appendChild(tr);
    });
    var wrap = el("div", "tbl-wrap");
    wrap.appendChild(tbl);
    return wrap;
  }

  // The schema sent to the provider (t.schema, TriageResult as JSON schema)
  // and the first attempt's messages exactly as sent.
  function contractBody(t, c, attempts) {
    var box = el("div");
    add(
      box,
      L.why(
        "No more “respond with JSON with these keys”: the Pydantic model <em>is</em> the contract, sent as a JSON schema, and the system prompt treats ticket text as untrusted data. " +
          "Temperature 0 instead of 1.0. Rename a field in <code>models.py</code> (README “Try this” #1) and watch where the failure surfaces.",
      ),
    );
    add(box, L.citesRow([c.triage_result, c.system_prompt, c.temperature]));
    add(
      box,
      el("div", "sub", "TriageResult (models.py)"),
      pre(JSON.stringify(t.schema, null, 2), "short"),
    );
    var sys = attempts[0] && attempts[0].messages && attempts[0].messages[0];
    if (sys) {
      var det = el("details");
      add(
        det,
        el("summary", null, "the system prompt and the ticket as sent"),
        pre(
          attempts[0].messages
            .map(function (m) {
              return "[" + m.role + "]\n" + m.content;
            })
            .join("\n\n"),
        ),
      );
      box.appendChild(det);
    }
    return box;
  }

  L.live01 = live;
  L.render01 = render;
  L.logTable = logTable;
})();
