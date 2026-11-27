(function (root) {
  'use strict';
  var names = new Set(root.AppIconNames || []);
  function html(name, options) {
    options = options || {};
    if (!names.has(name)) throw new RangeError('Unknown app icon: ' + name);
    var size = options.size;
    if (size != null && ![16, 18, 20, 24].includes(size)) throw new RangeError('Invalid app icon size');
    var inverse = options.tone === 'inverse';
    if (inverse && name !== 'send' && name !== 'stop') throw new RangeError('Invalid inverse icon');
    var label = options.label;
    var a11y = label ? ' role="img" aria-label="' + String(label).replace(/[&<>"']/g, function (char) {
      return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char];
    }) + '"' : ' aria-hidden="true"';
    return '<svg class="ui-icon app-icon' + (inverse ? ' app-icon-inverse' : '') + '"' + a11y +
      (size ? ' data-icon-size="' + size + '"' : '') + ' focusable="false"><use href="/assets/citrus-icons.svg#ui-' +
      name + '"></use></svg>';
  }
  root.AppIcon = Object.freeze({html: html});
})(window);
