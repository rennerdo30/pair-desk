// Pair Desk homepage: the loop in the hero, the screenshot viewer and the install tabs.
// Without this script the loop shows its first step, the viewer one capture in the visitor's
// colour scheme, and every install command at once.
(function () {
  'use strict';

  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------- the loop ----------
  (function () {
    var stage = document.querySelector('.stage');
    if (!stage) return;
    var buttons = stage.querySelectorAll('[data-goto]');
    var play = stage.querySelector('[data-play]');
    var caption = stage.querySelector('#stage-caption');
    var STEP_MS = 5200;
    var timer = null;
    var step = 1;

    function show(n, announce) {
      step = n;
      stage.setAttribute('data-step', String(n));
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(+b.getAttribute('data-goto') === n)); });
      caption.setAttribute('aria-live', announce ? 'polite' : 'off');
      // restart the teleport flash when step 4 sends the command again
      stage.classList.remove('again');
      if (n === 4) { void stage.offsetWidth; stage.classList.add('again'); }
    }

    function stop() {
      if (timer) clearInterval(timer);
      timer = null;
      play.setAttribute('aria-pressed', 'false');
      play.setAttribute('aria-label', 'Play the loop');
      play.title = 'Play the loop';
    }
    function start() {
      stop();
      play.setAttribute('aria-pressed', 'true');
      play.setAttribute('aria-label', 'Pause the loop');
      play.title = 'Pause the loop';
      timer = setInterval(function () { show(step % 4 + 1, false); }, STEP_MS);
    }

    buttons.forEach(function (b) {
      b.addEventListener('click', function () { stop(); show(+b.getAttribute('data-goto'), true); });
    });
    play.addEventListener('click', function () {
      if (timer) { stop(); return; }
      show(step % 4 + 1, false);
      start();
    });
    // Arrow keys move between the steps while one of them has focus.
    stage.querySelector('.steps').addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      var n = e.key === 'ArrowRight' ? step % 4 + 1 : (step + 2) % 4 + 1;
      stop();
      show(n, true);
      buttons[n - 1].focus();
      e.preventDefault();
    });

    show(1, false);
    // Plays once it scrolls into view, unless the visitor prefers less motion; any click takes over.
    if (!reduced && 'IntersectionObserver' in window) {
      var started = false;
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting && !started) { started = true; start(); }
          if (!entry.isIntersecting && timer) stop();
        });
      }, { threshold: 0.45 });
      io.observe(stage);
    }
  })();

  // ---------- the screenshot viewer ----------
  (function () {
    var root = document.querySelector('.viewer');
    if (!root) return;
    var img = root.querySelector('[data-shot]');
    var picture = img.parentNode;
    var caption = root.querySelector('#viewer-caption');
    var viewButtons = root.querySelectorAll('[data-view]');
    var themeButtons = root.querySelectorAll('[data-theme]');

    var views = {
      triage: {
        alt: 'The triage list with a check waiting for the owner; the open check shows its fields, description and a finished plan.',
        caption: 'Triage: what waits for you comes first. The open check carries its plan, ticked as commits landed.'
      },
      backlog: {
        alt: 'The backlog: open work grouped by area, sorted by priority and size.',
        caption: 'Backlog: open work by priority, then size, then area, in groups you can fold.'
      },
      handoff: {
        alt: 'The project handoff with its sections and a version picker.',
        caption: 'Handoff: where work stopped, what was verified, the next step and the traps. Every save is a version.'
      },
      failed: {
        alt: 'A check the owner answered Still broken, with its plan, a merged duplicate and the location.',
        caption: 'Still broken: your verdict moves the check and reaches the agent before its next prompt.'
      }
    };

    var dark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    var state = { view: 'triage', theme: dark && dark.matches ? 'dark' : 'light', chosen: false };

    function src(v, t) { return 'assets/shots/' + v + '-' + t + '.webp'; }
    function preload() {
      Object.keys(views).forEach(function (v) { ['light', 'dark'].forEach(function (t) { (new Image()).src = src(v, t); }); });
    }
    function show(announce) {
      var source = picture.querySelector('source');
      if (source) picture.removeChild(source);
      img.src = src(state.view, state.theme);
      img.alt = views[state.view].alt;
      caption.textContent = views[state.view].caption;
      caption.setAttribute('aria-live', announce ? 'polite' : 'off');
      viewButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-view') === state.view)); });
      themeButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-theme') === state.theme)); });
    }
    viewButtons.forEach(function (b) {
      b.addEventListener('click', function () { state.view = b.getAttribute('data-view'); show(true); });
    });
    themeButtons.forEach(function (b) {
      b.addEventListener('click', function () { state.theme = b.getAttribute('data-theme'); state.chosen = true; show(true); });
    });
    if (dark && dark.addEventListener) {
      dark.addEventListener('change', function (e) {
        if (state.chosen) return;
        state.theme = e.matches ? 'dark' : 'light';
        show(false);
      });
    }
    show(false);
    if ('requestIdleCallback' in window) window.requestIdleCallback(preload); else setTimeout(preload, 1500);
  })();

  // ---------- install tabs ----------
  (function () {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs [role="tab"]'));
    if (!tabs.length) return;
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t, false); });
      t.addEventListener('keydown', function (e) {
        var n = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : null;
        if (n === null) return;
        select(tabs[(n + tabs.length) % tabs.length], true);
        e.preventDefault();
      });
    });
    select(tabs[0], false);
  })();
})();
