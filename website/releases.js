// Pair Desk homepage: names the newest release in the install section, from the public GitHub API.
// Without this script (or without network) the sentence simply stays hidden.
(function () {
  'use strict';

  var API = 'https://api.github.com/repos/rennerdo30/pair-desk/releases/latest';
  var found = document.querySelector('[data-release="latest"][data-field="found"]');
  if (!found || !window.fetch) return;

  function field(name) { return document.querySelector('[data-release="latest"][data-field="' + name + '"]'); }

  fetch(API, { headers: { Accept: 'application/vnd.github+json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (r) {
      if (!r || r.draft || !/^v?\d+\.\d+\.\d+$/.test(r.tag_name || '')) return;
      var date = new Date(r.published_at);
      if (isNaN(date)) return;
      field('version').textContent = 'Pair Desk ' + r.tag_name.replace(/^v/, '');
      field('date').textContent = date.toLocaleDateString('en', { day: 'numeric', month: 'long', year: 'numeric' });
      found.hidden = false;
    })
    .catch(function () { /* offline or rate limited: keep the sentence hidden */ });
})();
