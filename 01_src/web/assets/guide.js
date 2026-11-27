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
  var dialog, opener, onClose, current = 0;
  function mount() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.id = 'citrusGuideDialog';
    dialog.className = 'cg-dialog';
    dialog.setAttribute('aria-labelledby', 'citrusGuideTitle');
    dialog.innerHTML = '<header class="cg-header"><h1 id="citrusGuideTitle">使用指南</h1><div>' +
      '<button type="button" data-guide-zoom aria-label="放大图片" aria-pressed="false" title="放大图片">⊕</button>' +
      '<button type="button" data-guide-close aria-label="关闭使用指南" title="关闭">×</button></div></header>' +
      '<article id="citrusGuidePage" class="cg-page" tabindex="-1" aria-label="指南图示"></article>' +
      '<footer class="cg-footer"><button type="button" data-guide-prev aria-label="上一页" title="上一页">‹</button>' +
      '<nav class="cg-nav" aria-label="选择指南页">' + pages.map(function (page, index) {
        return '<button type="button" data-guide-page="' + index + '" aria-label="' + page.title + '" title="' + page.title + '"></button>';
      }).join('') + '</nav><span id="citrusGuideIndex" role="status"></span>' +
      '<button type="button" data-guide-next aria-label="下一页" title="下一页">›</button></footer>';
    document.body.appendChild(dialog);
    dialog.addEventListener('click', function (event) {
      var target = event.target.closest('button');
      if (!target || !dialog.contains(target)) return;
      if (target.hasAttribute('data-guide-close')) close();
      else if (target.hasAttribute('data-guide-zoom')) zoom(target.getAttribute('aria-pressed') !== 'true');
      else if (target.hasAttribute('data-guide-page')) go(Number(target.getAttribute('data-guide-page')));
      else if (target.hasAttribute('data-guide-prev')) go(current - 1);
      else if (target.hasAttribute('data-guide-next')) go(current + 1);
    });
    dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(); });
    dialog.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
      else if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        if (event.target.closest('.cg-page[data-zoom="true"]')) return;
        event.preventDefault(); event.stopPropagation(); go(current + (event.key === 'ArrowRight' ? 1 : -1));
      }
    });
  }
  function zoom(expanded) {
    var page = dialog.querySelector('.cg-page');
    var button = dialog.querySelector('[data-guide-zoom]');
    page.dataset.zoom = String(expanded);
    button.setAttribute('aria-pressed', String(expanded));
    button.setAttribute('aria-label', expanded ? '适应窗口' : '放大图片');
    button.title = button.getAttribute('aria-label');
    button.textContent = expanded ? '⊖' : '⊕';
    if (expanded) page.focus();
    else page.scrollTo(0, 0);
  }
  function go(index) {
    index = Math.max(0, Math.min(pages.length - 1, Number(index) || 0));
    var page = pages[index], article = dialog.querySelector('.cg-page');
    if (dialog.open && article.dataset.page === page.id) return;
    var direction = index < current ? 'back' : 'forward';
    current = index;
    zoom(false);
    article.dataset.page = page.id;
    article.dataset.direction = direction;
    var image = new Image(1672, 941);
    image.alt = page.title;
    image.src = '/assets/guide-' + page.image + '.png';
    article.replaceChildren(image);
    dialog.querySelectorAll('[data-guide-page]').forEach(function (button, i) {
      button.setAttribute('aria-current', i === current ? 'page' : 'false');
    });
    var prev = dialog.querySelector('[data-guide-prev]'), next = dialog.querySelector('[data-guide-next]');
    prev.disabled = current === 0;
    next.disabled = current === pages.length - 1;
    if (document.activeElement === prev && prev.disabled) next.focus();
    if (document.activeElement === next && next.disabled) prev.focus();
    var status = dialog.querySelector('#citrusGuideIndex');
    status.textContent = (current + 1) + ' / ' + pages.length;
    status.setAttribute('aria-label', status.textContent + ' · ' + page.title);
  }
  function open(page, options) {
    mount();
    options = options || {};
    if (!dialog.open) opener = document.activeElement;
    onClose = typeof options.onClose === 'function' ? options.onClose : null;
    page = Object.prototype.hasOwnProperty.call(aliases, page) ? aliases[page] : page;
    var index = typeof page === 'number' ? page - 1 : pages.findIndex(function (item) { return item.id === page; });
    go(index < 0 ? 0 : index);
    if (!dialog.open) dialog.showModal();
    dialog.querySelector('[data-guide-close]').focus();
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
  window.CitrusGuide = {open: open, close: close, pageCount: pages.length, version: 5};
  document.addEventListener('click', function (event) {
    var target = event.target.closest('[data-citrus-guide]');
    if (!target) return;
    event.preventDefault();
    open(target.getAttribute('data-citrus-guide') || 'conversation');
  });
  if (document.body && document.body.classList.contains('guide-page')) {
    open(location.hash.slice(1) || 'conversation');
  }
}());
