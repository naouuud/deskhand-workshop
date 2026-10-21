/* Deskhand workshop — shared slide runtime.
   Slides are <section class="slide"> children of <main class="deck">.
   Keys: arrows / space / PageUp-PageDown navigate, Home/End jump,
         's' toggles speaker notes, 'n' toggles reading notes,
         'f' toggles fullscreen.
   Click: right third = next, left third = previous.
   URL hash deep-links: #5 opens slide 5.
   Reading notes (<aside class="learn">) are for students reading on their own:
   the current slide's note shows in a panel beside the slide. On by default
   when <html data-copy="student"> (the students' copy), or with ?learn=1.
   Dependency-free. */
(function () {
  "use strict";

  var STAGE_W = 1600,
    STAGE_H = 900; // the size every slide is checked at

  var deck = null;
  var slides = [];
  var current = 0;
  var hasLearn = false;
  var learnOn = false;
  var panel = null;

  function clamp(n) {
    return Math.max(0, Math.min(slides.length - 1, n));
  }

  function show(n) {
    current = clamp(n);
    for (var i = 0; i < slides.length; i++) {
      slides[i].classList.toggle("current", i === current);
    }
    var counter = document.getElementById("deck-counter");
    if (counter) counter.textContent = current + 1 + " / " + slides.length;
    var bar = document.getElementById("deck-progress");
    if (bar) bar.style.width = ((current + 1) / slides.length) * 100 + "%";
    if (learnOn) fillLearn();
    if (window.location.hash !== "#" + (current + 1)) {
      history.replaceState(null, "", "#" + (current + 1));
    }
  }

  function next() {
    show(current + 1);
  }
  function prev() {
    show(current - 1);
  }

  function fromHash() {
    var n = parseInt(window.location.hash.slice(1), 10);
    show(isNaN(n) ? 0 : n - 1);
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen();
    }
  }

  // ---- reading notes ----

  function buildPanel() {
    panel = document.createElement("aside");
    panel.id = "deck-learn";
    panel.setAttribute("aria-label", "Notes for this slide");
    var head = document.createElement("div");
    head.className = "learn-head";
    var body = document.createElement("div");
    body.className = "learn-body";
    var foot = document.createElement("div");
    foot.className = "learn-foot";
    foot.textContent = "← → move between slides · n hides these notes";
    panel.appendChild(head);
    panel.appendChild(body);
    panel.appendChild(foot);
    document.body.appendChild(panel);
  }

  function fillLearn() {
    var head = panel.querySelector(".learn-head");
    var body = panel.querySelector(".learn-body");
    head.textContent =
      "Notes · slide " + (current + 1) + " of " + slides.length;
    while (body.firstChild) body.removeChild(body.firstChild);
    var note = slides[current].querySelector("aside.learn");
    if (note) {
      for (var i = 0; i < note.childNodes.length; i++) {
        body.appendChild(note.childNodes[i].cloneNode(true));
      }
    } else {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Nothing to add on this slide.";
      body.appendChild(empty);
    }
    body.scrollTop = 0;
  }

  // Fit the 1600x900 slide into whatever the panel leaves free.
  function layout() {
    document.body.classList.toggle("learn", learnOn);
    if (!learnOn) {
      document.body.classList.remove("learn-side", "learn-bottom");
      deck.style.transform = "";
      return;
    }
    var vw = window.innerWidth,
      vh = window.innerHeight;
    var side = vw / vh > 1.45;
    document.body.classList.toggle("learn-side", side);
    document.body.classList.toggle("learn-bottom", !side);
    var box = panel.getBoundingClientRect();
    var freeW = side ? vw - box.width : vw;
    var freeH = side ? vh : vh - box.height;
    var k = Math.min(freeW / STAGE_W, freeH / STAGE_H);
    var x = (freeW - STAGE_W * k) / 2; // centred across, flush with the top
    deck.style.transform = "translate(" + x + "px,0) scale(" + k + ")";
  }

  function toggleLearn() {
    if (!hasLearn) return;
    learnOn = !learnOn;
    layout();
    if (learnOn) fillLearn();
  }

  function onKey(e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    switch (e.key) {
      case "ArrowRight":
      case "ArrowDown":
      case "PageDown":
      case " ":
        e.preventDefault();
        next();
        break;
      case "ArrowLeft":
      case "ArrowUp":
      case "PageUp":
        e.preventDefault();
        prev();
        break;
      case "Home":
        e.preventDefault();
        show(0);
        break;
      case "End":
        e.preventDefault();
        show(slides.length - 1);
        break;
      case "s":
      case "S":
        document.body.classList.toggle("show-notes");
        break;
      case "n":
      case "N":
        toggleLearn();
        break;
      case "f":
      case "F":
        toggleFullscreen();
        break;
    }
  }

  function onClick(e) {
    if (e.target.closest("a, button, input, textarea, select, #deck-learn"))
      return;
    var box = deck.getBoundingClientRect();
    var x = (e.clientX - box.left) / box.width;
    if (x > 2 / 3) next();
    else if (x < 1 / 3) prev();
  }

  function init() {
    deck = document.querySelector("main.deck");
    if (!deck) return;
    slides = Array.prototype.slice.call(deck.querySelectorAll("section.slide"));
    if (!slides.length) return;

    var bar = document.createElement("div");
    bar.id = "deck-progress";
    document.body.appendChild(bar);

    var counter = document.createElement("div");
    counter.id = "deck-counter";
    document.body.appendChild(counter);

    hasLearn = !!deck.querySelector("aside.learn");
    if (hasLearn) {
      buildPanel();
      learnOn =
        document.documentElement.getAttribute("data-copy") === "student" ||
        /[?&]learn=1\b/.test(window.location.search);
      layout();
      window.addEventListener("resize", layout);
    }

    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onClick);
    window.addEventListener("hashchange", fromHash);

    fromHash();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
