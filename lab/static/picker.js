/* Deskhand Lab — the ticket picker (sidebar) and the selected-ticket strip. */

/* Owns S.tickets, S.incidents, S.checkpoints, S.tag, S.search and S.sel.
   Three kinds of ticket can be selected: one of the 60 in
   shared/data/tickets.json, a Session 1 incident (A, B, ...) from 00's
   incidents.py, or text typed into "Write your own ticket". S.sel holds
   whichever it is; L.ticketBody() turns it into the request body.
   Ticket text is untrusted and can carry prompt-injection payloads, so
   every preview, name and tag goes in through L.el / L.add as text.
   Reads from L: S, $, el, add, getJSON, chip, titleCase, emit, and, once the page has
   booted, orders.orderIdsIn / orders.view (orders.js loads after this file). */
(function () {
  "use strict";
  var S = L.S, $ = L.$, el = L.el, add = L.add;

  // Tags drawn in the warn colour in the list and the strip: the tickets that
  // break something (injection, the 14 vs 30 day contradiction, long threads).
  var HOT_TAGS = { injection: 1, contradiction_trigger: 1, long_thread: 1 };

  // One GET returns everything the page needs at boot: the dataset, the
  // incidents, the checkpoint list (with each service's node order) and the
  // Repeat cap. With nothing selected, the last incident is picked so Run
  // works on the first click. "tickets" then lets views.js route and draw.
  function loadTickets() {
    return L.getJSON("/api/tickets").then(function (data) {
      S.tickets = data.tickets;
      S.incidents = data.incidents;
      S.checkpoints = data.checkpoints;
      S.cpById = {};
      data.checkpoints.forEach(function (c) { S.cpById[c.id] = c; });
      // Compare starts on 00 and 02; a copy without 02 yet compares 00 and 01.
      Object.keys(S.cmp).forEach(function (k) { if (!S.cpById[k]) delete S.cmp[k]; });
      if (!S.cmp["02"] && S.cpById["01"]) S.cmp["01"] = true;
      S.maxRepeat = data.max_repeat;
      $("rep-n").max = String(data.max_repeat);
      $("ticket-count").textContent = S.tickets.length + " tickets";
      if (data.incidents_error) add($("incident-list"), el("p", "bad small pad", "Could not read incidents.py: " + data.incidents_error));
      renderTags();
      renderIncidents();
      renderTickets();
      if (!S.sel && S.incidents.length) selectIncident(S.incidents[S.incidents.length - 1], true);
      L.emit("tickets", data);
    }).catch(function (err) {
      $("ticket-list").textContent = "Could not load /api/tickets — " + err.message;
    });
  }

  // One filter button per tag in the dataset, with its count.
  function renderTags() {
    var counts = {};
    S.tickets.forEach(function (t) { (t.tags || []).forEach(function (tag) { counts[tag] = (counts[tag] || 0) + 1; }); });
    var box = $("tag-filters");
    box.textContent = "";
    ["all"].concat(Object.keys(counts).sort()).forEach(function (name) {
      var b = el("button", "tag" + (name === S.tag ? " on" : ""), name === "all" ? "all" : L.titleCase(name) + " " + counts[name]);
      b.type = "button";
      b.dataset.tag = name;
      b.setAttribute("aria-pressed", String(name === S.tag));
      b.onclick = function () {
        S.tag = name; renderTags(); renderTickets();
        // renderTags rebuilt the buttons: keep keyboard focus on the one just pressed.
        var again = box.querySelector('[data-tag="' + name + '"]');
        if (again) again.focus();
      };
      box.appendChild(b);
    });
  }

  function renderIncidents() {
    var box = $("incident-list");
    box.textContent = "";
    S.incidents.forEach(function (inc) {
      var b = el("button", "ticket incident");
      b.type = "button";
      b.dataset.key = inc.key;
      var top = el("div", "ticket-top");
      add(top, el("span", "ticket-id", "Incident " + inc.letter), el("span", "ticket-name", inc.title.replace(/^Incident [A-Z] — /, "")));
      add(b, top, el("div", "ticket-preview", inc.text));
      b.onclick = function () { selectIncident(inc); };
      box.appendChild(b);
    });
  }

  // The tag filter, then a case-insensitive substring search over id,
  // customer, order id, tags and the full thread text.
  function matches(t) {
    if (S.tag !== "all" && (t.tags || []).indexOf(S.tag) === -1) return false;
    if (!S.search) return true;
    var q = S.search.toLowerCase();
    return (t.ticket_id + " " + t.customer_name + " " + (t.order_id || "") + " " + (t.tags || []).join(" ") + " " + t.text).toLowerCase().indexOf(q) !== -1;
  }

  function renderTickets() {
    var list = $("ticket-list");
    list.textContent = "";
    var shown = 0;
    S.tickets.filter(matches).forEach(function (t) {
      shown++;
      var b = el("button", "ticket");
      b.type = "button";
      b.dataset.key = t.ticket_id;
      var top = el("div", "ticket-top");
      add(top, el("span", "ticket-id", t.ticket_id), el("span", "ticket-name", t.customer_name));
      var tagText = (t.tags || []).map(L.titleCase).join(", ");
      if (t.message_count > 1) tagText += (tagText ? " · " : "") + t.message_count + " msgs";
      if (!t.order_id) tagText += (tagText ? " · " : "") + "no order";
      var hot = (t.tags || []).some(function (x) { return HOT_TAGS[x]; });
      add(top, el("span", "ticket-tag" + (hot ? " hot" : ""), tagText));
      add(b, top, el("div", "ticket-preview", t.messages[0].text));
      b.onclick = function () { selectTicket(t); };
      list.appendChild(b);
    });
    if (!shown) add(list, el("p", "muted small pad", "No ticket matches."));
    markSelected();
  }

  // Highlights the selected entry by its data-key, in both lists.
  function markSelected() {
    var all = document.querySelectorAll(".ticket");
    for (var i = 0; i < all.length; i++) {
      var on = !!S.sel && all[i].dataset.key === S.sel.key;
      all[i].classList.toggle("on", on);
      all[i].setAttribute("aria-pressed", String(on));
    }
  }

  // The three ways to fill S.sel. key matches the list buttons' data-key;
  // label is what headlines and confirm dialogs show. auto marks the demo
  // the page picks itself at start, so the Orders view opens no popup for it.
  function selectTicket(t) {
    S.sel = { kind: "dataset", key: t.ticket_id, id: t.ticket_id, label: t.ticket_id + " · " + t.customer_name, text: t.text, tags: t.tags, order_id: t.order_id };
    showSelected();
  }
  function selectIncident(inc, auto) {
    S.sel = { kind: "incident", key: inc.key, letter: inc.letter, label: inc.title, text: inc.text, watch: inc.watch, auto: !!auto };
    showSelected();
  }
  function selectCustom(text, from) {
    S.sel = { kind: "custom", key: "custom", label: "your own ticket" + (from ? " from " + from : ""), text: text, from: from || null };
    showSelected();
  }

  // Select by a {ticket_id} | {incident} spec (the incident board uses this).
  function selectSpec(spec) {
    if (spec.ticket_id) {
      var t = S.tickets.filter(function (x) { return x.ticket_id === spec.ticket_id; })[0];
      if (t) { selectTicket(t); return true; }
    }
    if (spec.incident) {
      var inc = S.incidents.filter(function (x) { return x.letter === spec.incident; })[0];
      if (inc) { selectIncident(inc); return true; }
    }
    return false;
  }

  // The strip above the run controls: label, chips, the ticket text, and an
  // incident's "What to watch". It emits "select" so the hints refresh.
  function showSelected() {
    markSelected();
    var s = S.sel;
    $("sel-label").textContent = s.label;
    var chips = $("sel-chips");
    chips.textContent = "";
    if (s.kind === "incident") add(chips, L.chip("Session 1 demo", "chip-warn"));
    if (s.kind === "custom") add(chips, L.chip("custom text", "chip-violet"),
      s.from ? L.chip("from " + s.from, "", "sent to 02–04 as customer_name; 00 and 01 ignore it") : L.chip("no sender", "", "02–04 skip the order-ownership rule for a ticket nobody vouches for"));
    (s.tags || []).forEach(function (tag) { add(chips, L.chip(L.titleCase(tag), HOT_TAGS[tag] ? "chip-warn" : "")); });
    // The order this ticket is about, as the order system holds it, in the
    // Orders view's popup (orders.js). A dataset ticket names it in order_id;
    // a demo or custom ticket only in its text.
    var orderIds = s.order_id ? [s.order_id] : s.kind === "dataset" ? [] : L.orders.orderIdsIn(s.text);
    orderIds.forEach(function (id) {
      var b = el("button", "btn btn-sm btn-ghost", "View order " + id);
      b.type = "button";
      b.title = "What the order system holds for " + id + ": items, total, delivery, refunds";
      b.onclick = function () { L.orders.view(id); };
      add(chips, b);
    });
    var box = $("sel-text");
    box.textContent = s.text;
    if (s.watch) add(box, el("div", "watch", "What to watch: " + s.watch));
    $("sel-toggle").hidden = false;
    L.emit("select", s);
  }

  $("sel-toggle").onclick = function () {
    var box = $("sel-text");
    box.hidden = !box.hidden;
    $("sel-toggle").textContent = box.hidden ? "show text" : "hide text";
  };

  $("custom-use").onclick = function () {
    var text = $("custom-text").value.trim();
    if (text) selectCustom(text, $("custom-from").value.trim().slice(0, 120));
  };

  $("ticket-search").oninput = function () { S.search = this.value.trim(); renderTickets(); };

  L.loadTickets = loadTickets;
  L.selectSpec = selectSpec;
})();
