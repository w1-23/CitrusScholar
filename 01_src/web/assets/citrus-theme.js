/* Applied before any product page paints, with one storage key across routes. */
(function (root) {
  'use strict';
  var storageKey = 'citrus_theme';
  var names = Object.freeze(['sage', 'paper', 'navy']);
  var labels = Object.freeze({ sage: '浅绿白', paper: '暖纸白', navy: '夜间蓝' });
  var aliases = { light: 'sage', cartoon: 'paper', dark: 'navy', night: 'navy' };
  var html = document.documentElement;

  function normalize(value) {
    if (names.indexOf(value) !== -1) return value;
    return Object.hasOwn(aliases, value) ? aliases[value] : 'sage';
  }
  function current() { return normalize(html.getAttribute('data-theme')); }
  function apply(value, persist) {
    var next = normalize(value);
    var changed = html.getAttribute('data-theme') !== next;
    html.setAttribute('data-theme', next);
    if (persist) {
      try { root.localStorage.setItem(storageKey, next); } catch (_) {}
    }
    if (changed) root.dispatchEvent(new CustomEvent('citrus:themechange', { detail: next }));
    return next;
  }
  function read() {
    try { return root.localStorage.getItem(storageKey); } catch (_) { return null; }
  }
  var initial = read();
  root.CitrusTheme = Object.freeze({ names: names, labels: labels, normalize: normalize,
    current: current, set: function (name) { return apply(name, true); } });
  apply(initial, initial != null && initial !== normalize(initial));
  root.addEventListener('storage', function (event) {
    if (event.key !== storageKey && event.key !== null) return;
    try { if (event.storageArea === root.localStorage) apply(read(), false); } catch (_) {}
  });
})(window);
