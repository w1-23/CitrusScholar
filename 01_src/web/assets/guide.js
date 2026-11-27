(function () {
  'use strict';
  if (window.CitrusGuide) return;
  var pages = [
    {id: 'conversation', title: '会话界面指南', image: 'conversation'},
    {id: 'features', title: '功能场景指南', image: 'features'},
    {id: 'workbench', title: '图件工作台指南', image: 'workbench'}
  ];
  var aliases = {upload: 'conversation', account: 'conversation', research: 'conversation',
    data: 'features', sources: 'features', delivery: 'features', figures: 'workbench'};
  var dialog, opener, onClose;

  function guideSections() {
    return pages.map(function (page) {
      return '<figure class="cg-section" id="' + page.id + '">' +
        '<figcaption>' + page.title + '</figcaption>' +
        '<img src="/assets/guide-' + page.image + '.png" width="1672" height="941" alt="' + page.title + '">' +
        '</figure>';
    }).join('');
  }

  function resolveSection(page) {
    if (typeof page === 'number') page = pages[page - 1] && pages[page - 1].id;
    page = Object.prototype.hasOwnProperty.call(aliases, page) ? aliases[page] : page;
    return pages.some(function (item) { return item.id === page; }) ? page : pages[0].id;
  }

  function mount() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.id = 'citrusGuideDialog';
    dialog.className = 'cg-dialog';
    dialog.setAttribute('aria-labelledby', 'citrusGuideTitle');
    var closeIcon = window.AppIcon ? window.AppIcon.html('close') :
      '<svg class="ui-icon" aria-hidden="true" focusable="false"><use href="/assets/citrus-icons.svg#ui-close"/></svg>';
    dialog.innerHTML = '<header class="cg-header"><h1 id="citrusGuideTitle">使用指南</h1>' +
      '<button type="button" data-guide-close aria-label="关闭使用指南" title="关闭">' + closeIcon + '</button></header>' +
      '<div class="cg-pages" tabindex="0" role="region" aria-label="全部指南图示">' + guideSections() + '</div>';
    document.body.appendChild(dialog);
    dialog.querySelector('[data-guide-close]').addEventListener('click', function () { close(); });
    dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(); });
  }

  function open(page, options) {
    mount();
    options = options || {};
    if (!dialog.open) {
      opener = document.activeElement;
      dialog.showModal();
    }
    if (typeof options.onClose === 'function') onClose = options.onClose;
    dialog.querySelector('[data-guide-close]').focus({preventScroll: true});
    var content = dialog.querySelector('.cg-pages');
    var section = content.querySelector('#' + resolveSection(page));
    content.scrollTop = section.offsetTop;
  }
  function close(notify) {
    if (!dialog || !dialog.open) return;
    var callback = onClose;
    onClose = null;
    dialog.close();
    if (opener && opener.isConnected && typeof opener.focus === 'function') {
      opener.focus();
      // Resizing can hide the sidebar containing the original guide button.
      var fallbackSelector = opener.getAttribute('data-guide-focus-fallback');
      if (document.activeElement !== opener && fallbackSelector) {
        var fallback = document.querySelector(fallbackSelector);
        if (fallback && !fallback.disabled) fallback.focus();
      }
    }
    opener = null;
    if (notify !== false && callback) callback();
  }
  window.CitrusGuide = {open: open, close: close, pageCount: pages.length, version: 7};
  document.addEventListener('click', function (event) {
    var target = event.target.closest('[data-citrus-guide]');
    if (!target) return;
    event.preventDefault();
    open(target.getAttribute('data-citrus-guide') || 'conversation');
  });
  var landing = document.getElementById('citrusGuideSections');
  if (landing) {
    landing.innerHTML = guideSections();
    if (location.hash) landing.querySelector('#' + resolveSection(location.hash.slice(1))).scrollIntoView();
  }
}());
