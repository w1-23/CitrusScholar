/* Applied before any product page paints. Scaling never sets a figure's zoom. */
(function (root) {
  'use strict';
  var html = document.documentElement;
  var key = 'citrus_ui_scale';
  var desktop = root.matchMedia('(min-width: 1025px)');
  var preference = 125;
  html.setAttribute('data-theme', 'porcelain');
  try {
    var oldTheme = root.localStorage.getItem('citrus_theme');
    if (oldTheme && oldTheme !== 'porcelain') root.localStorage.setItem('citrus_theme', 'porcelain');
  } catch (_) {}

  function effective() { return desktop.matches ? preference / 100 : 1; }
  function updateControls() {
    document.querySelectorAll('[data-ui-scale-pick]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(Number(button.dataset.uiScalePick) === preference));
    });
  }
  function set(value, persist) {
    preference = Number(value) === 150 ? 150 : 125;
    html.setAttribute('data-ui-scale', String(preference));
    if (persist) {
      try { root.localStorage.setItem(key, String(preference)); } catch (_) {}
    }
    updateControls();
    root.dispatchEvent(new CustomEvent('citrus:scalechange', {detail: {preference: preference, scale: effective()}}));
    return preference;
  }
  function read() {
    try { return root.localStorage.getItem(key); } catch (_) { return null; }
  }
  root.CitrusTheme = Object.freeze({ current: function () { return 'porcelain'; },
    scale: Object.freeze({ get: function () { return preference; }, effective: effective,
      set: function (value) { return set(value, true); } }) });
  set(read(), false);
  desktop.addEventListener('change', function () { set(preference, false); });
  document.addEventListener('DOMContentLoaded', updateControls);
  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-ui-scale-pick]');
    if (button) set(button.dataset.uiScalePick, true);
  });
  root.addEventListener('storage', function (event) {
    if (event.key !== key && event.key !== null) return;
    try { if (event.storageArea === root.localStorage) set(read(), false); } catch (_) {}
  });
})(window);
