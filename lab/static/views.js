/* Deskhand Lab — the views: Run, Compare, Repeat, Incidents; routing; boot. */

/* Loaded last, because it wires the buttons in index.html and boots the page
   with everything the other files put on L. It owns the four tabs and the
   hash routes (#run, #compare, #repeat, #incidents, #run/<lab run_id>).

   A run, end to end: startRun() builds the body (ticket + checkpoint
   options), L.report() lays out an empty Simple / Detailed pair, and
   L.runStream() feeds every NDJSON event to two live views at once: the
   checkpoint's own (live00, live01 or liveSvc) and liveSimple. When the
   final trace arrives, renderTrace() throws the live views away and draws
   both final reports from that one trace.

   Reads from L: S, the DOM helpers, runStream, report, the per-checkpoint
   live and render functions, renderSimple, liveSimple, registerCards,
   renderCards, serviceOf, selectSpec, loadHealth, loadTickets. */
(function () {
  "use strict";
  var S = L.S,
    $ = L.$,
    el = L.el,
    add = L.add,
    usd = L.usd,
    money = L.money,
    ms = L.ms,
    num = L.num,
    jsonish = L.jsonish,
    panel = L.panel;

  var VIEWS = ["run", "compare", "repeat", "incidents"];
  // 02, 03 and 04 are HTTP services; 00 and 01 run as a subprocess per run.
  // Checkpoint ids are two-digit strings, so string comparison orders them.
  var isSvc = function (cp) {
    return cp >= "02";
  };

  /* ── checkpoint cards for each view ────────────────────── */
  // Run and Repeat pick one checkpoint, Compare several. Each callback
  // updates S, then the controls and hints that depend on the choice.
  L.registerCards(
    $("cards-run"),
    "single",
    function () {
      return S.cp;
    },
    function (id) {
      S.cp = id;
      renderRunControls();
      updateHints();
    },
  );
  L.registerCards(
    $("cards-cmp"),
    "multi",
    function () {
      return S.cmp;
    },
    function (id, on) {
      S.cmp[id] = on;
      $("cmp-guard-wrap").hidden = !S.cmp["04"];
      updateHints();
    },
  );
  L.registerCards(
    $("cards-rep"),
    "single",
    function () {
      return S.repCp;
    },
    function (id) {
      S.repCp = id;
      renderRunControls();
      updateHints();
    },
  );

  // The teaching note under the Run controls: how the lab drives each
  // checkpoint without editing it. Authored HTML, set with innerHTML.
  var RUN_WHY = {
    "00":
      'Runs <code>bot.main()</code> unmodified, like <code>py bot.py "&lt;ticket&gt;"</code>, in a subprocess whose working directory is <code>checkpoints/00-broken-bot</code>. ' +
      "The lab swaps <code>bot.client</code> for a recording proxy and watches <code>resolve()</code> and <code>main()</code> with <code>sys.settrace</code>. bot.py is never edited: it stays broken.",
    "01":
      "Runs <code>main.main()</code> unmodified on a one-ticket copy of the dataset, in a subprocess in <code>checkpoints/01-triage</code>. Wrappers around <code>llm._client</code>, " +
      "<code>llm.structured_completion</code>, <code>triage.classify_ticket</code> and <code>errors.user_message</code> record what happened; every one of them calls straight through. " +
      "Fault injection only lowers <code>max_tokens</code> on the real provider call: the truncation, the error and the repair are all real.",
    "02":
      "The lab is a plain HTTP client of the running service, like the 02 console or curl: <code>POST /tickets</code>, then <code>GET /runs/{id}/events</code> (server-sent events), " +
      "<code>POST /runs/{id}/approve</code> when the run pauses, and <code>GET /runs/{id}</code> for the persisted state at the end. The log lines come from the checkpoint's own <code>logs/runs.jsonl</code>.",
    "03": "Same HTTP contract as 02. The service now runs on LangGraph only, with a <code>retrieve_policy</code> node between the order lookup and the plan. Try a <em>contradiction trigger</em> ticket (T-1010, T-1026).",
    "04": "Same HTTP contract. A <code>guard_input</code> node runs before triage, every call is charged against a per-run budget, and the reply passes an output guard. Try an <em>injection</em> ticket (T-1006, T-1018, T-1025).",
  };

  // Shows only the controls the chosen checkpoint understands: fault
  // injection (01), the input guard (04), the approval policy and Follow
  // (02–04), and the retrieval tools (03).
  function renderRunControls() {
    $("inject-wrap").hidden = S.cp !== "01";
    $("guard-wrap").hidden = S.cp !== "04";
    $("cmp-guard-wrap").hidden = !S.cmp["04"];
    $("approval-wrap").hidden = !isSvc(S.cp);
    $("rep-approval-wrap").hidden = !isSvc(S.repCp);
    $("follow-wrap").hidden = !isSvc(S.cp);
    $("search-wrap").hidden =
      $("distance-wrap").hidden =
      $("map-wrap").hidden =
        S.cp !== "03";
    $("run-why").innerHTML = RUN_WHY[S.cp] || "";
    if (isSvc(S.cp)) loadRecent();
    if (S.cp === "03") loadPolicyDocs();
  }

  // The "only this document" filter's options: the doc_ids read from
  // shared/policies, fetched once.
  var policyDocsLoaded = false;
  function loadPolicyDocs() {
    if (policyDocsLoaded) return;
    policyDocsLoaded = true;
    L.getJSON("/api/policy-docs")
      .then(function (res) {
        var sel = $("search-doc");
        (res.docs || []).forEach(function (d) {
          var opt = el("option", null, d.doc_id);
          opt.value = d.doc_id;
          sel.appendChild(opt);
        });
      })
      .catch(function () {
        policyDocsLoaded = false;
      });
  }

  // Can this checkpoint run right now? A service must be up; 00 and 01 need
  // working LLM credentials. Before the first health answer, 00 and 01
  // count as ready and the services do not.
  function serviceReady(cp) {
    if (!isSvc(cp)) {
      if (!S.health) return true;
      var sub = L.serviceOf(cp); // checkpoint 00 has its own readiness: it only knows Azure and OpenAI
      return sub && sub.ready !== undefined
        ? !!sub.ready
        : !!S.health.llm.ready;
    }
    var s = L.serviceOf(cp);
    return !!(s && s.up);
  }

  // Enables or disables the Run, Compare and Repeat buttons and writes the
  // hint beside each (cost estimate, which service is down). It runs on
  // every selection, card click and health poll, so the estimate call waits
  // 150ms and a burst of updates sends one request.
  var hintTimer = null;
  function updateHints() {
    clearTimeout(hintTimer);
    if (!S.sel) return;
    var ready = serviceReady(S.cp);
    $("run-btn").disabled = !ready || !!S.busy.run;
    $("follow-btn").disabled =
      $("search-btn").disabled =
      $("distance-btn").disabled =
      $("map-btn").disabled =
        !!S.busy.run;
    if (!ready) {
      var subp = L.serviceOf(S.cp);
      $("run-hint").textContent = isSvc(S.cp)
        ? "checkpoint " + S.cp + " is not running — start it on its card"
        : (subp && subp.not_ready) || "fill .env first (see Setup)";
      $("cmp-hint").textContent = "";
      $("rep-hint").textContent = "";
    }
    hintTimer = setTimeout(function () {
      var body = L.ticketBody();
      if (!body) return;
      body.checkpoints = [S.cp];
      body.runs = 1;
      if (ready) {
        L.postJSON("/api/estimate", body)
          .then(function (est) {
            var r = est.rows[0];
            $("run-hint").textContent =
              "one real run on " +
              (r.model || est.model || "the LLM") +
              " · ≈ " +
              usd(r.per_run_low) +
              (r.per_run_high !== r.per_run_low
                ? "–" + usd(r.per_run_high)
                : "");
          })
          .catch(function () {
            $("run-hint").textContent = "";
          });
      }
      var picked = Object.keys(S.cmp)
        .filter(function (k) {
          return S.cmp[k];
        })
        .sort();
      var down = picked.filter(function (cp) {
        return !serviceReady(cp);
      });
      $("cmp-hint").textContent =
        picked.length +
        " checkpoint" +
        (picked.length === 1 ? "" : "s") +
        (down.length
          ? " · not running: " + down.join(", ")
          : " · you confirm the cost first");
      $("cmp-btn").disabled =
        !picked.length || !!down.length || !!S.busy.compare;
      var repReady = serviceReady(S.repCp);
      $("rep-hint").textContent = repReady
        ? "you confirm the cost first · max " + S.maxRepeat
        : "checkpoint " + S.repCp + " is not running";
      $("rep-btn").disabled = !repReady || !!S.busy.repeat;
    }, 150);
  }

  L.on("select", function () {
    updateHints();
  });
  L.on("health", function () {
    updateHints();
  });

  /* ── views & routing ───────────────────────────────────── */
  // The hash is the only router state. A tab click sets location.hash, the
  // hashchange event calls route(), and route() shows one view. #run/<id>
  // opens a finished run: from S.runs when this page ran it, else from the
  // server, which keeps the last 300 traces in memory until it restarts.
  // So a projected #run/<id> link survives F5, but not a lab restart.

  function setView(v) {
    S.view = v;
    VIEWS.forEach(function (name) {
      $("view-" + name).hidden = name !== v;
    });
    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].dataset.view === v;
      tabs[i].classList.toggle("on", on);
      if (on) tabs[i].setAttribute("aria-current", "page");
      else tabs[i].removeAttribute("aria-current");
    }
  }

  function route() {
    var h = location.hash.replace(/^#/, "");
    var m = h.match(/^run\/(\w+)$/);
    if (m) {
      setView("run");
      var cached = S.runs[m[1]];
      var show = function (trace) {
        S.cp = trace.checkpoint;
        L.renderCards();
        renderRunControls();
        renderTrace(trace, $("run-out"));
      };
      if (cached) show(cached);
      else
        L.getJSON("/api/runs/" + m[1])
          .then(show)
          .catch(function (e) {
            $("run-out").textContent = "";
            add(
              $("run-out"),
              L.errorPanel("Run " + m[1] + " is not available: " + e.message),
            );
          });
      return;
    }
    setView(VIEWS.indexOf(h) !== -1 ? h : "run");
  }

  window.addEventListener("hashchange", route);
  var tabEls = document.querySelectorAll(".tab");
  for (var ti = 0; ti < tabEls.length; ti++) {
    tabEls[ti].onclick = function () {
      location.hash = this.dataset.view;
    };
  }

  // Compare and Repeat link to a run's full report through the router.
  function openInRun(trace) {
    S.runs[trace.run_id] = trace;
    location.hash = "run/" + trace.run_id;
  }

  // Every report is rendered twice: in plain words (simple.js) and in full.
  // The Simple / Detailed switch at the top of the report picks which shows.
  function renderTrace(trace, out) {
    var rep = L.report(out);
    rep.simple.appendChild(L.renderSimple(trace));
    if (trace.checkpoint === "00") L.render00(trace, rep.detailed);
    else if (trace.checkpoint === "01") L.render01(trace, rep.detailed);
    else L.renderSvc(trace, rep.detailed);
  }

  /* ── Run ───────────────────────────────────────────────── */

  // A Compare column can be "04+nemo": checkpoint 04 with a chosen input guard.
  function colCp(key) {
    return String(key).split("+")[0];
  }
  function colGuard(key) {
    return String(key).split("+")[1] || null;
  }
  var GUARD_LABEL = {
    heuristic: "regex + classifier",
    nemo: "NeMo Guardrails",
  };
  function colName(key) {
    var g = colGuard(key);
    return L.cpName(colCp(key)) + (g ? " · " + GUARD_LABEL[g] : "");
  }

  // The POST /api/run body for one column: the ticket plus the options that
  // checkpoint reads. approval is "ask", "approve" or "reject": with ask the
  // run waits for a click; the other two let the server answer the gate.
  // Compare and Repeat offer only those two, so a batch never stalls.
  function runBody(key, approval) {
    var cp = colCp(key);
    var body = L.ticketBody();
    body.checkpoint = cp;
    if (cp === "01") body.inject_truncate = Number($("inject").value);
    if (isSvc(cp)) body.approval = approval || $("approval").value;
    if (cp === "04") body.guard = colGuard(key) || $("guard").value;
    return body;
  }

  // One real run on the Run tab. Each event goes to both live views; the
  // CSS class on <body> decides which one is visible. replaceState records
  // #run/<id> without firing hashchange, so the report just drawn is not
  // drawn a second time, and the URL is ready to copy or reload.
  function startRun() {
    if (!S.sel || S.busy.run || !serviceReady(S.cp)) return;
    var cp = S.cp;
    var out = $("run-out");
    S.busy.run = true;
    $("run-btn").disabled = true;
    var rep = L.report(out);
    var live =
      cp === "00"
        ? L.live00(rep.detailed)
        : cp === "01"
          ? L.live01(rep.detailed)
          : L.liveSvc(rep.detailed, cp);
    var simple = L.liveSimple(cp);
    rep.simple.appendChild(simple.root);
    out.scrollIntoView({ behavior: "smooth", block: "start" });
    L.runStream(runBody(cp), function (ev) {
      live.onEvent(ev);
      simple.onEvent(ev);
    })
      .then(function (trace) {
        renderTrace(trace, out);
        history.replaceState(null, "", "#run/" + trace.run_id);
      })
      .catch(function (err) {
        add(
          out,
          L.errorPanel(err.message, err.detail, {
            needs_service: err.needs_service,
          }),
        );
        if (err.request_id) $("follow-id").value = err.request_id; // ready for Follow once the service is back
      })
      .then(function () {
        S.busy.run = false;
        updateHints();
        if (isSvc(cp)) loadRecent();
      });
  }

  $("run-btn").onclick = startRun;

  /* ── Follow a request ID (02–04) ───────────────────────────
     Attaches to a run by its request_id and replays it from event 0: after an
     F5, a lab restart, a crash, or a run submitted with curl. Renders exactly
     like a run started here, approval card included. Nothing is submitted. */

  // Up to three recent request_ids on this checkpoint, as one-click Follow buttons.
  function loadRecent() {
    var cp = S.cp;
    L.getJSON("/api/follow/recent")
      .then(function (d) {
        var box = $("follow-recent");
        box.textContent = "";
        var mine = (d.recent || [])
          .filter(function (r) {
            return r.checkpoint === cp;
          })
          .slice(0, 3);
        if (mine.length) add(box, el("span", "muted small", "recent:"));
        mine.forEach(function (r) {
          var b = el("button", "btn btn-sm btn-ghost mono", r.request_id);
          b.type = "button";
          b.title = r.label + " — follow it from event 0";
          b.onclick = function () {
            $("follow-id").value = r.request_id;
            startFollow();
          };
          box.appendChild(b);
        });
      })
      .catch(function () {
        /* an older lab server: the input still works */
      });
  }

  function startFollow() {
    var cp = S.cp,
      rid = $("follow-id").value.trim();
    if (!isSvc(cp) || S.busy.run) return;
    if (!/^[A-Za-z0-9_-]{4,64}$/.test(rid)) {
      $("follow-hint").textContent =
        "a request_id is the 12 characters POST /tickets returned (letters and digits)";
      $("follow-id").focus();
      return;
    }
    $("follow-hint").textContent =
      "replays an existing run from event 0; nothing is submitted";
    var out = $("run-out");
    S.busy.run = true;
    $("run-btn").disabled =
      $("follow-btn").disabled =
      $("search-btn").disabled =
      $("distance-btn").disabled =
      $("map-btn").disabled =
        true;
    var rep = L.report(out);
    var live = L.liveSvc(rep.detailed, cp, { followed: rid });
    var simple = L.liveSimple(cp, { followed: rid });
    rep.simple.appendChild(simple.root);
    out.scrollIntoView({ behavior: "smooth", block: "start" });
    L.runStream(
      { checkpoint: cp, request_id: rid },
      function (ev) {
        live.onEvent(ev);
        simple.onEvent(ev);
      },
      "/api/follow",
    )
      .then(function (trace) {
        renderTrace(trace, out);
        history.replaceState(null, "", "#run/" + trace.run_id);
      })
      .catch(function (err) {
        add(
          out,
          L.errorPanel(err.message, err.detail, {
            needs_service: err.needs_service,
          }),
        );
      })
      .then(function () {
        S.busy.run = false;
        $("follow-btn").disabled =
          $("search-btn").disabled =
          $("distance-btn").disabled =
          $("map-btn").disabled =
            false;
        updateHints();
        loadRecent();
      });
  }

  $("follow-btn").onclick = startFollow;
  $("follow-id").onkeydown = function (e) {
    if (e.key === "Enter") startFollow();
  };

  /* ── Search only (checkpoint 03) ───────────────────────────
     03's own retrieve() on a bare query, in a runner subprocess. The 03
     service holds the policy index while it runs (Qdrant local mode: one
     process per folder), so the lab says so and offers to stop it. */

  function startSearch(stopped) {
    var q = $("search-q").value.trim() || $("search-q").placeholder;
    if (S.busy.run) return;
    var out = $("run-out");
    S.busy.run = true;
    $("run-btn").disabled =
      $("follow-btn").disabled =
      $("search-btn").disabled =
      $("distance-btn").disabled =
      $("map-btn").disabled =
        true;
    out.textContent = "";
    add(
      out,
      el(
        "div",
        "muted small",
        el("span", "spinner"),
        " searching checkpoint 03's policy index…",
      ),
    );
    var actions = {
      stopped: !!stopped,
      stopAndSearch: function () {
        L.stopService("03")
          .then(function (r) {
            if (r.status !== "stopped")
              throw new Error(r.note || "could not stop it");
            startSearch(true);
          })
          .catch(function (e) {
            out.textContent = "";
            add(
              out,
              L.errorPanel("Could not stop checkpoint 03: " + e.message),
            );
          });
      },
      startAgain: function () {
        return L.startService("03");
      },
    };
    var doc = $("search-doc").value;
    L.postJSON("/api/search", { query: q, doc_ids: doc ? [doc] : null })
      .then(function (res) {
        L.renderSearch(res, out, actions);
        addSpend(res);
      })
      .catch(function (e) {
        out.textContent = "";
        add(out, L.errorPanel("The search failed: " + e.message));
      })
      .then(function () {
        S.busy.run = false;
        $("follow-btn").disabled =
          $("search-btn").disabled =
          $("distance-btn").disabled =
          $("map-btn").disabled =
            false;
        updateHints();
      });
  }

  // Search, distance and map calls cost a few embedding tokens: they add to
  // the session total but are not runs, so runCount stays put.
  function addSpend(res) {
    if (res.ok && typeof res.cost_usd === "number") {
      S.spend += res.cost_usd;
      $("session-cost").textContent =
        usd(S.spend) +
        " · " +
        S.runCount +
        " run" +
        (S.runCount === 1 ? "" : "s");
    }
  }

  $("search-btn").onclick = function () {
    startSearch(false);
  };
  $("search-q").onkeydown = function (e) {
    if (e.key === "Enter") startSearch(false);
  };

  /* ── How close are these phrases? (checkpoint 03's llm.embed_texts) ──
     One embedding call for 2 to 4 phrases, every pair's cosine. No index,
     so no lock: it runs while the 03 service is up. */

  // Two phrase inputs ship in index.html; the add button appends up to two more.
  function phraseInputs() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#phrase-list .phrase"),
    );
  }

  $("phrase-add").onclick = function () {
    var inputs = phraseInputs();
    if (inputs.length >= 4) return;
    var n = inputs.length;
    var input = el("input", "phrase");
    input.type = "text";
    input.id = "phrase-" + n;
    input.maxLength = 200;
    input.autocomplete = "off";
    input.placeholder = n === 2 ? "late delivery" : "my parcel never came";
    input.setAttribute("aria-label", "Phrase " + (n + 1));
    input.onkeydown = function (e) {
      if (e.key === "Enter") startDistance();
    };
    $("phrase-list").appendChild(input);
    input.focus();
    if (n + 1 >= 4) $("phrase-add").disabled = true;
  };

  function startDistance() {
    if (S.busy.run) return;
    var phrases = phraseInputs().map(function (i) {
      return i.value.trim() || i.placeholder;
    });
    var out = $("run-out");
    S.busy.run = true;
    $("run-btn").disabled =
      $("follow-btn").disabled =
      $("search-btn").disabled =
      $("distance-btn").disabled =
      $("map-btn").disabled =
        true;
    out.textContent = "";
    add(
      out,
      el(
        "div",
        "muted small",
        el("span", "spinner"),
        " embedding " +
          phrases.length +
          " phrases through checkpoint 03's llm.py…",
      ),
    );
    L.postJSON("/api/distance", { phrases: phrases })
      .then(function (res) {
        L.renderDistance(res, out);
        addSpend(res);
      })
      .catch(function (e) {
        out.textContent = "";
        add(out, L.errorPanel("The comparison failed: " + e.message));
      })
      .then(function () {
        S.busy.run = false;
        $("follow-btn").disabled =
          $("search-btn").disabled =
          $("distance-btn").disabled =
          $("map-btn").disabled =
            false;
        updateHints();
      });
  }

  $("distance-btn").onclick = startDistance;
  phraseInputs().forEach(function (i) {
    i.onkeydown = function (e) {
      if (e.key === "Enter") startDistance();
    };
  });

  /* ── The map (every section of the index on two PCA axes) ──
     Opens the index, so the same lock rules as search. */

  function startMap(stopped) {
    if (S.busy.run) return;
    var q = $("map-q").value.trim();
    var out = $("run-out");
    S.busy.run = true;
    $("run-btn").disabled =
      $("follow-btn").disabled =
      $("search-btn").disabled =
      $("distance-btn").disabled =
      $("map-btn").disabled =
        true;
    out.textContent = "";
    add(
      out,
      el(
        "div",
        "muted small",
        el("span", "spinner"),
        " reading every vector in checkpoint 03's policy index…",
      ),
    );
    var actions = {
      stopped: !!stopped,
      stopAndSearch: function () {
        L.stopService("03")
          .then(function (r) {
            if (r.status !== "stopped")
              throw new Error(r.note || "could not stop it");
            startMap(true);
          })
          .catch(function (e) {
            out.textContent = "";
            add(
              out,
              L.errorPanel("Could not stop checkpoint 03: " + e.message),
            );
          });
      },
      startAgain: function () {
        return L.startService("03");
      },
    };
    L.postJSON("/api/map", { query: q || null })
      .then(function (res) {
        L.renderMap(res, out, actions);
        addSpend(res);
      })
      .catch(function (e) {
        out.textContent = "";
        add(out, L.errorPanel("The map failed: " + e.message));
      })
      .then(function () {
        S.busy.run = false;
        $("follow-btn").disabled =
          $("search-btn").disabled =
          $("distance-btn").disabled =
          $("map-btn").disabled =
            false;
        updateHints();
      });
  }

  $("map-btn").onclick = function () {
    startMap(false);
  };
  $("map-q").onkeydown = function (e) {
    if (e.key === "Enter") startMap(false);
  };

  // Used by cross-links ("run this ticket on 01 →") and the incident board.
  // It runs the ticket already selected. Without an explicit inject_truncate,
  // a jump to 01 resets fault injection to off.
  L.runOn = function (cp, opts) {
    opts = opts || {};
    if (!S.cpById[cp]) return; // not in this copy of the repo yet
    S.cp = cp;
    if (opts.inject_truncate !== undefined)
      $("inject").value = String(opts.inject_truncate);
    else if (cp === "01") $("inject").value = "0";
    location.hash = "run";
    L.renderCards();
    renderRunControls();
    updateHints();
    if (serviceReady(cp)) startRun();
    else {
      $("run-out").textContent = "";
      add(
        $("run-out"),
        L.errorPanel("Checkpoint " + cp + " is not running yet.", null, {
          needs_service: cp,
        }),
      );
    }
  };

  /* ── Compare ───────────────────────────────────────────── */
  // The same ticket on several checkpoints at once: one table row per
  // question, one column per run, and a small live preview per column.
  // Every column runs in parallel and the table redraws as each finishes.

  function clip(text) {
    return el("div", "clip-text", text);
  }
  function dash() {
    return "—";
  }

  // One row per question you'd ask in a postmortem. (s = trace.summary)
  var CMP_ROWS = [
    [
      "Outcome",
      function (s, t) {
        return L.outcomePill(t);
      },
    ],
    [
      "What the customer got",
      function (s, t) {
        if (t.checkpoint === "01")
          return clip("— triage is internal; nothing reaches the customer yet");
        return clip(s.customer_text || "(nothing)");
      },
    ],
    [
      "Category",
      function (s) {
        return s.category === null || s.category === undefined
          ? "—"
          : jsonish(s.category);
      },
    ],
    [
      "Actions",
      function (s, t) {
        if (t.checkpoint === "00") return "whatever the model wrote, unread";
        if (t.checkpoint === "01") return "— (plans arrive in 02)";
        return (s.actions || []).length ? s.actions.join(", ") : "—";
      },
    ],
    [
      "Refund suggested",
      function (s, t) {
        if (t.checkpoint === "01") return "—";
        return s.refund_amount === null || s.refund_amount === undefined
          ? "—"
          : typeof s.refund_amount === "number"
            ? money(s.refund_amount)
            : jsonish(s.refund_amount);
      },
    ],
    [
      "Checked against the order",
      function (s, t) {
        if (t.checkpoint === "00") return "no — the order was never looked up";
        if (t.checkpoint === "01") return "—";
        return s.order_found
          ? "yes — order total " + money(s.order_total) + " (rule 1)"
          : "no order found → rule 1 forbids a refund";
      },
    ],
    [
      "Human approval",
      function (s, t) {
        if (t.checkpoint === "00") return "none";
        if (t.checkpoint === "01") return "—";
        if (!s.requires_approval)
          return s.actions && s.actions.length
            ? "not needed (≤ $50, no replacement)"
            : "—";
        return (
          (t.approval.decision || "undecided") +
          (t.approval.by ? " by " + t.approval.by : "")
        );
      },
    ],
    [
      "Refund executed",
      function (s, t) {
        if (t.checkpoint === "00") {
          var r = t.refund || {};
          return r.fired
            ? "YES, unchecked" +
                (r.exceeds_order_total ? " — over the order total" : "")
            : "no";
        }
        if (t.checkpoint === "01") return "no — triage can't spend money";
        return s.refund_fired
          ? "yes — " + s.refund_ids.join(", ") + ", with an idempotency key"
          : "no";
      },
    ],
    [
      "Validated",
      function (s, t) {
        if (t.checkpoint === "00") return "no — json.loads and hope";
        if (t.checkpoint === "01")
          return s.validated
            ? "schema: TriageResult"
            : "no — " + (s.error_code || t.outcome);
        if (s.validated)
          return (
            "schema + business rules" +
            (s.repairs
              ? " after " + s.repairs + " repair" + (s.repairs === 1 ? "" : "s")
              : "")
          );
        return s.error_code ? "failed safely: " + s.error_code : "no plan";
      },
    ],
    [
      "Grounded in policy",
      function (s, t) {
        if (t.checkpoint === "00")
          return "all 6 policy docs pasted into the prompt";
        if (t.checkpoint === "01") return "—";
        if (t.checkpoint === "02") return "no policy text at all";
        return (
          s.policy_sections +
          " sections retrieved · cited: " +
          (s.citations.length ? s.citations.join(", ") : "none")
        );
      },
    ],
    [
      "Injection guard",
      function (s, t) {
        if (t.checkpoint !== "04") return "none";
        var gi = t.facts && t.facts.guard_input;
        var who = GUARD_LABEL[t.guard] || "regex + classifier";
        if (!s.guard_input_tripped) return who + ": passed";
        var where =
          gi && gi.source && gi.source !== "ticket"
            ? " on the " + gi.source
            : "";
        return who + ": tripped" + where + " → routed to a human";
      },
    ],
    [
      "LLM calls",
      function (s) {
        return num(s.attempts);
      },
    ],
    [
      "Cost",
      function (s) {
        return usd(s.cost_usd);
      },
    ],
    [
      "Wall time",
      function (s) {
        return ms(s.latency_ms);
      },
    ],
    [
      "Greppable request_id",
      function (s) {
        return s.request_id || "none";
      },
    ],
  ];

  // With 04 ticked, the guard picker turns it into one or two columns
  // ("04+heuristic", "04+nemo"), so both guards can be compared on one ticket.
  $("cmp-btn").onclick = function () {
    if (!S.sel || S.busy.compare) return;
    var cps = Object.keys(S.cmp)
      .filter(function (k) {
        return S.cmp[k];
      })
      .sort();
    if (!cps.length) return;
    var g = $("cmp-guard").value;
    cps = cps.reduce(function (acc, cp) {
      if (cp !== "04") acc.push(cp);
      else if (g === "both") acc.push("04+heuristic", "04+nemo");
      else acc.push("04+" + g);
      return acc;
    }, []);
    L.confirmSpend(
      "Compare " + cps.length + " runs on " + S.sel.label,
      cps.map(colCp),
      1,
    )
      .then(function (ok) {
        if (ok) runCompare(cps);
      })
      .catch(function (e) {
        add($("cmp-out"), L.errorPanel(e.message));
      });
  };

  function runCompare(cps) {
    var out = $("cmp-out");
    out.textContent = "";
    S.busy.compare = true;
    $("cmp-btn").disabled = true;
    var approval = $("cmp-approval").value;
    var traces = {};
    var tablePanel = panel({
      title: "Side by side",
      sub: S.sel.label,
      why: "One row per question you'd ask in a postmortem. 00 answers most of them with “none”; every later checkpoint turns one more answer into a guarantee: a schema, a request id, an order lookup, a rule, a gate, a citation, a guard.",
    });
    var tableBox = el("div", "tbl-wrap");
    tablePanel.body.appendChild(tableBox);
    add(out, tablePanel.root);
    var colsBox = el("div", "cmp-cols");
    colsBox.style.gridTemplateColumns =
      "repeat(" + cps.length + ", minmax(0, 1fr))";
    add(out, colsBox);

    function renderTable() {
      tableBox.textContent = "";
      var tbl = el("table", "tbl");
      var hr = el("tr");
      hr.appendChild(el("th", null, ""));
      cps.forEach(function (cp) {
        hr.appendChild(el("th", null, colName(cp)));
      });
      tbl.appendChild(hr);
      CMP_ROWS.forEach(function (row) {
        var tr = el("tr", "rowhead");
        tr.appendChild(el("td", null, row[0]));
        cps.forEach(function (cp) {
          var td = el("td", "mono clip");
          var t = traces[cp];
          if (!t) td.appendChild(el("span", "spinner"));
          else if (t.failed) td.textContent = "lab error";
          else {
            var v = row[1](t.summary, t);
            if (v instanceof Node) td.appendChild(v);
            else td.textContent = v;
          }
          tr.appendChild(td);
        });
        tbl.appendChild(tr);
      });
      tableBox.appendChild(tbl);
    }
    renderTable();

    // Each column's preview is the one thing that checkpoint makes visible
    // while it runs: 00's raw stdout, 01's attempts, the services' reply.
    var jobs = cps.map(function (cp) {
      var col = panel({ title: colName(cp), sub: "running…" });
      colsBox.appendChild(col.root);
      var status = el("div", "small mono");
      var term = el("pre", "terminal");
      var reply = el("div", "reply muted", "…");
      if (cp === "00") col.body.appendChild(term);
      else if (cp === "01") col.body.appendChild(status);
      else col.body.appendChild(reply);
      var text = "",
        replyText = "";
      return L.runStream(runBody(cp, approval), function (ev) {
        if (ev.type === "stdout") {
          text += ev.text;
          term.textContent = text;
          term.scrollTop = term.scrollHeight;
        } else if (ev.type === "attempt.finished" && cp === "01")
          status.appendChild(
            el(
              "div",
              ev.error ? "bad" : "good",
              "attempt " +
                ev.n +
                (ev.error ? " ← " + ev.error.code : " ← parsed"),
            ),
          );
        else if (ev.type === "request_id") {
          if (col.sub) col.sub.textContent = "request " + ev.request_id;
        } else if (ev.type === "sse" && ev.event === "node.started") {
          if (col.sub) col.sub.textContent = ev.data.node;
        } else if (ev.type === "sse" && ev.event === "reply.delta") {
          if (!replyText) {
            reply.textContent = "";
            reply.classList.remove("muted");
          }
          replyText += ev.data.text || "";
          reply.textContent = replyText;
        } else if (ev.type === "approval.decided") {
          if (col.sub)
            col.sub.textContent =
              "approval gate: " +
              (ev.approved ? "approved" : "rejected") +
              " automatically";
        }
      })
        .then(function (t) {
          traces[cp] = t;
          var info = L.outcomeInfo(t);
          if (col.sub)
            col.sub.textContent =
              info[0] +
              " · " +
              usd(t.summary.cost_usd) +
              " · " +
              ms(t.summary.latency_ms);
          if (cp === "01") {
            status.textContent = "";
            add(
              status,
              L.pre(
                t.result
                  ? JSON.stringify(t.result, null, 2)
                  : t.error
                    ? t.error.code + "\n\n" + (t.user_message || "")
                    : t.crash || "",
                "short",
              ),
            );
          } else if (cp >= "02") {
            var v = L.svcVerdict(t);
            reply.textContent = v[0];
            reply.classList.remove("muted");
          }
          var b = el("button", "btn btn-sm", "open the full report →");
          b.type = "button";
          b.style.marginTop = "8px";
          b.onclick = function () {
            openInRun(t);
          };
          col.body.appendChild(b);
          renderTable();
        })
        .catch(function (err) {
          traces[cp] = { failed: true };
          col.body.appendChild(
            L.errorPanel(err.message, err.detail, {
              needs_service: err.needs_service,
            }),
          );
          renderTable();
        });
    });

    var cmdsBox = el("div");
    add(out, cmdsBox);
    Promise.all(jobs).then(function () {
      var cmds = [];
      cps.forEach(function (cp) {
        if (traces[cp] && traces[cp].commands)
          cmds.push(traces[cp].commands[0]);
      });
      cmdsBox.appendChild(L.commandsSection(cmds));
      S.busy.compare = false;
      updateHints();
    });
  }

  /* ── Repeat ────────────────────────────────────────────── */
  // The same ticket N times on one checkpoint, to show what varies between
  // runs. REP_FIELDS picks, per checkpoint, the trace fields worth counting:
  // [column label, function (trace) -> value].

  var REP_FIELDS = {
    "00": [
      [
        "outcome",
        function (t) {
          return t.outcome;
        },
      ],
      [
        "category",
        function (t) {
          return t.summary.category;
        },
      ],
      [
        "refund_amount",
        function (t) {
          return t.summary.refund_amount;
        },
      ],
      [
        "refund executed",
        function (t) {
          return t.refund.fired;
        },
      ],
      [
        "over order total",
        function (t) {
          return t.refund.exceeds_order_total;
        },
      ],
      [
        "attempts",
        function (t) {
          return t.summary.attempts;
        },
      ],
    ],
    "01": [
      [
        "outcome",
        function (t) {
          return t.outcome;
        },
      ],
      [
        "category",
        function (t) {
          return t.summary.category;
        },
      ],
      [
        "priority",
        function (t) {
          return t.summary.priority;
        },
      ],
      [
        "sentiment",
        function (t) {
          return t.summary.sentiment;
        },
      ],
      [
        "confidence",
        function (t) {
          return t.summary.confidence;
        },
      ],
      [
        "attempts",
        function (t) {
          return t.summary.attempts;
        },
      ],
    ],
    svc: [
      [
        "outcome",
        function (t) {
          return t.outcome;
        },
      ],
      [
        "category",
        function (t) {
          return t.summary.category;
        },
      ],
      [
        "actions",
        function (t) {
          return (t.summary.actions || []).join("+") || null;
        },
      ],
      [
        "refund",
        function (t) {
          return t.summary.refund_amount;
        },
      ],
      [
        "needs approval",
        function (t) {
          return t.summary.requires_approval;
        },
      ],
      [
        "citations",
        function (t) {
          return (t.summary.citations || []).join("+") || null;
        },
      ],
      [
        "repairs",
        function (t) {
          return t.summary.repairs;
        },
      ],
    ],
  };

  $("rep-btn").onclick = function () {
    if (!S.sel || S.busy.repeat) return;
    var n = Math.max(
      2,
      Math.min(S.maxRepeat, parseInt($("rep-n").value, 10) || 5),
    );
    $("rep-n").value = String(n);
    var cp = S.repCp;
    L.confirmSpend(
      "Run " + S.sel.label + " " + n + "× on " + L.cpName(cp),
      [cp],
      n,
    )
      .then(function (ok) {
        if (ok) runRepeat(cp, n);
      })
      .catch(function (e) {
        add($("rep-out"), L.errorPanel(e.message));
      });
  };

  function runRepeat(cp, n) {
    var out = $("rep-out");
    out.textContent = "";
    S.busy.repeat = true;
    $("rep-btn").disabled = true;
    var approval = $("rep-approval").value;
    var fields = REP_FIELDS[cp] || REP_FIELDS.svc;
    var traces = [];
    var done = 0,
      failed = 0;

    var prog = panel({
      title: n + " runs of " + S.sel.label + " on " + L.cpName(cp),
      sub: "three at a time",
    });
    var bar = el("div", "progress");
    var fill = el("div");
    fill.style.width = "0%";
    bar.appendChild(fill);
    var progText = el("div", "muted small", "0 / " + n + " done");
    add(prog.body, bar, progText);
    add(out, prog.root);

    var dist = panel({
      title: "What changed between runs",
      sub: "distinct values per field",
      cites: cp === "00" ? [S.cites.temperature] : [],
      why:
        cp === "00"
          ? "Same ticket, same prompt, same code — different resolutions. <code>temperature=1.0</code> samples a new answer every time, and with no schema and no rules, every sample is “valid”. That's incident #8."
          : "Temperature 0 plus a schema narrows what can vary to the allowed values; business rules narrow it further. Any spread left is the model, not the code. Checkpoint 04 gates releases on this as <code>determinism_agreement</code>.",
    });
    var distBox = el("div", "dist");
    dist.body.appendChild(distBox);
    var totals = el("div", "kvs");
    totals.style.marginTop = "12px";
    dist.body.appendChild(totals);
    add(out, dist.root);

    var tblP = panel({
      title: "Every run",
      sub: "highlighted cells differ from the most common value",
    });
    var tblBox = el("div", "tbl-wrap");
    tblP.body.appendChild(tblBox);
    add(out, tblP.root);
    var cmdBox = el("div");
    add(out, cmdBox);

    // The most common value, compared as JSON text so null, numbers and
    // strings count as distinct values. Table cells that differ from it are
    // highlighted.
    function mode(values) {
      var counts = {};
      values.forEach(function (v) {
        var k = jsonish(v);
        counts[k] = (counts[k] || 0) + 1;
      });
      var best = null;
      Object.keys(counts).forEach(function (k) {
        if (best === null || counts[k] > counts[best]) best = k;
      });
      return { best: best, counts: counts };
    }

    function render() {
      fill.style.width = Math.round((100 * done) / n) + "%";
      progText.textContent =
        done +
        " / " +
        n +
        " done" +
        (failed
          ? " · " + failed + " lab error" + (failed === 1 ? "" : "s")
          : "");
      var ok = traces.filter(Boolean);
      distBox.textContent = "";
      fields.forEach(function (f) {
        var m = mode(
          ok.map(function (t) {
            return f[1](t);
          }),
        );
        var box = el("div", "dist-field");
        var keys = Object.keys(m.counts).sort(function (a, b) {
          return m.counts[b] - m.counts[a];
        });
        add(box, el("h4", null, f[0] + " · " + keys.length + " distinct"));
        keys.forEach(function (k) {
          var row = el("div", "bar-row");
          var lab = el("div");
          add(lab, el("div", "bar-label", k === "null" ? "— (none)" : k));
          var b = el("div", "bar");
          b.style.width = Math.round((100 * m.counts[k]) / ok.length) + "%";
          lab.appendChild(b);
          add(row, lab, el("div", "mono small", "×" + m.counts[k]));
          box.appendChild(row);
        });
        distBox.appendChild(box);
      });
      totals.textContent = "";
      var cost = ok.reduce(function (s, t) {
        return s + (t.summary.cost_usd || 0);
      }, 0);
      add(
        totals,
        L.kv("total cost", usd(cost), "big"),
        L.kv(
          "LLM calls",
          ok.reduce(function (s, t) {
            return s + (t.summary.attempts || 0);
          }, 0),
        ),
      );
      if (cp === "00") {
        var fired = ok.filter(function (t) {
          return t.refund.fired;
        });
        var paid = fired.reduce(function (s, t) {
          return s + (t.refund.amount_parsed || 0);
        }, 0);
        var over = fired.filter(function (t) {
          return t.refund.exceeds_order_total;
        }).length;
        add(
          totals,
          L.kv("refunds executed", fired.length + " of " + ok.length),
          L.kv("money paid out", money(paid), "big" + (paid ? " warn" : "")),
          L.kv("over the order total", String(over), over ? "big bad" : "big"),
        );
      } else if (cp >= "02") {
        var refunds = ok.filter(function (t) {
          return t.summary.refund_fired;
        }).length;
        var paidSvc = ok.reduce(function (s, t) {
          return (
            s +
            (t.facts.refunds || []).reduce(function (x, r) {
              return x + (r.amount_usd || 0);
            }, 0)
          );
        }, 0);
        add(
          totals,
          L.kv("refunds executed", refunds + " of " + ok.length),
          L.kv("money paid out", money(paidSvc), "big"),
          L.kv(
            "failed safely",
            ok.filter(function (t) {
              return t.outcome !== "completed";
            }).length,
          ),
        );
      }

      tblBox.textContent = "";
      var tbl = el("table", "tbl");
      var hr = el("tr");
      ["#"]
        .concat(
          fields.map(function (f) {
            return f[0];
          }),
        )
        .concat(["cost", "time", ""])
        .forEach(function (h) {
          hr.appendChild(el("th", null, h));
        });
      tbl.appendChild(hr);
      var modes = fields.map(function (f) {
        return mode(
          ok.map(function (t) {
            return f[1](t);
          }),
        ).best;
      });
      traces.forEach(function (t, i) {
        var tr = el("tr");
        tr.appendChild(el("td", "mono", String(i + 1)));
        if (!t) {
          var td = el("td", "muted", "lab error");
          td.colSpan = fields.length + 3;
          tr.appendChild(td);
          tbl.appendChild(tr);
          return;
        }
        fields.forEach(function (f, j) {
          var v = jsonish(f[1](t));
          tr.appendChild(
            el(
              "td",
              "mono" + (ok.length > 1 && v !== modes[j] ? " diff" : ""),
              v,
            ),
          );
        });
        add(
          tr,
          el("td", "mono", usd(t.summary.cost_usd)),
          el("td", "mono", ms(t.summary.latency_ms)),
        );
        var b = el("button", "btn btn-sm btn-ghost", "report →");
        b.type = "button";
        b.onclick = function () {
          openInRun(t);
        };
        var tdb = el("td");
        tdb.appendChild(b);
        tr.appendChild(tdb);
        tbl.appendChild(tr);
      });
      tblBox.appendChild(tbl);
    }

    // Three workers share one queue, so at most three runs are in flight.
    // Each result goes in its own slot, so the table keeps run order even
    // when runs finish out of order. A failed run counts as a lab error.
    var queue = [];
    for (var i = 0; i < n; i++) queue.push(i);
    traces.length = 0;
    function worker() {
      if (!queue.length) return Promise.resolve();
      var idx = queue.shift();
      return L.runStream(runBody(cp, approval), function () {})
        .then(function (t) {
          traces[idx] = t;
        })
        .catch(function () {
          traces[idx] = null;
          failed += 1;
        })
        .then(function () {
          done += 1;
          render();
          return worker();
        });
    }
    render();
    Promise.all([worker(), worker(), worker()])
      .then(function () {
        var first = traces.filter(Boolean)[0];
        return L.postJSON(
          "/api/estimate",
          Object.assign(L.ticketBody(), { checkpoints: [cp], runs: n }),
        ).then(function (est) {
          var cmds =
            (est.commands && est.commands[cp]) ||
            (first ? first.commands.slice(0, 1) : []);
          cmdBox.appendChild(L.commandsSection(cmds));
        });
      })
      .catch(function () {
        /* the table is the result; the command is a nicety */
      })
      .then(function () {
        S.busy.repeat = false;
        updateHints();
      });
  }

  /* ── Incidents ─────────────────────────────────────────── */
  // The incident board (GET /api/incidents): each production incident gets a
  // "Reproduce on 00" button and a "See it retired on <checkpoint>" button.
  // A spec names the ticket ({ticket_id} or {incident}) and, for some, the
  // Repeat view or a fault-injection level.

  function specLabel(spec) {
    if (spec.ticket_id) return spec.ticket_id;
    if (spec.incident) return "Incident " + spec.incident;
    return "";
  }

  // Selects the spec's ticket, then runs it at once, or opens Repeat with
  // the checkpoint chosen and leaves the cost confirmation to its button.
  function go(spec, cp) {
    if (!L.selectSpec(spec)) return;
    if (spec.view === "repeat") {
      S.repCp = cp;
      location.hash = "repeat";
      L.renderCards();
      renderRunControls();
      updateHints();
      $("view-repeat").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    L.runOn(cp, { inject_truncate: spec.inject_truncate });
  }

  function loadBoard() {
    L.getJSON("/api/incidents")
      .then(function (d) {
        S.board = d.incidents;
        var box = $("incident-board");
        box.textContent = "";
        d.incidents.forEach(function (inc) {
          var card = el("div", "incident-card");
          add(
            card,
            el("div", "n", "incident #" + inc.n),
            el("div", "text", inc.text),
            el("div", "muted small", inc.watch),
          );
          var actions = el("div", "actions");
          var b1 = el(
            "button",
            "btn btn-sm",
            "Reproduce on 00 · " +
              specLabel(inc.reproduce) +
              (inc.reproduce.view === "repeat" ? " (Repeat)" : ""),
          );
          b1.type = "button";
          b1.onclick = function () {
            go(inc.reproduce, "00");
          };
          var r = inc.retired,
            b2;
          if (r.available === false) {
            // This copy of the repo doesn't have the fixing checkpoint yet.
            b2 = el(
              "span",
              "muted small",
              (r.partial ? "Narrowed on " : "Retired on ") +
                r.checkpoint +
                ", which arrives in a later session",
            );
          } else {
            // partial: the checkpoint narrows the incident but the decks keep it open.
            b2 = el(
              "button",
              "btn btn-sm btn-primary",
              (r.partial ? "See it narrowed on " : "See it retired on ") +
                r.checkpoint +
                " · " +
                specLabel(r) +
                (r.view === "repeat" ? " (Repeat)" : ""),
            );
            b2.type = "button";
            b2.onclick = function () {
              go(r, r.checkpoint);
            };
          }
          add(actions, b1, b2);
          card.appendChild(actions);
          box.appendChild(card);
        });
      })
      .catch(function (e) {
        $("incident-board").textContent =
          "Could not load the incident board: " + e.message;
      });
  }

  /* ── boot ──────────────────────────────────────────────── */
  // Routing waits for the ticket list, because #run/<id> and the hints need
  // S.sel and the checkpoint list. Health is polled every 10s, which is how the cards and
  // Setup notice a service started or stopped in a terminal.

  L.initToggles();
  L.getJSON("/api/cites/00")
    .then(function (c) {
      S.cites = c;
    })
    .catch(function () {});
  L.on("tickets", function () {
    renderRunControls();
    updateHints();
    route();
    loadBoard();
  });
  L.loadHealth();
  setInterval(L.loadHealth, 10000);
  L.loadTickets();
})();
