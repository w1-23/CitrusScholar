// 会话文件（session files）前端：列表、预览、下载、删除。
//
// 边界（与本轮改造范围一致）：
//  - 账号私有资料库：列表、预览、下载与当前轮引用；不写入共享知识库。
//  - 打开预览是普通 HTTP 读取，不调用模型，也不把内容送进检索或知识库。
//  - 生成成果由科研管线拥有，这里只展示与下载，不提供删除。

(function () {
  'use strict';

  var panelEl = null;
  var apiBase = '';
  var fileCache = Object.create(null);
  var libraryRows = [];
  var retentionIdleDays = 0;
  var FILE_ID = /^[a-f0-9]{32}$/;
  var SHA256 = /^[a-f0-9]{64}$/;
  var FIGURE_SOURCE_KEY = 'citrus:figure-source';
  var catalogScope = 0;
  var catalogRequestSerial = 0, latestCatalogRequest = null;
  var defaultReferenceLimits = {max_turn_files: 8, max_reference_bytes: 20 * 1024 * 1024};
  var referenceLimits = defaultReferenceLimits;

  function resetSessionFiles() {
    catalogScope += 1;
    try { window.sessionStorage.removeItem(FIGURE_SOURCE_KEY); } catch (_) {}
    latestCatalogRequest = null;
    referenceLimits = defaultReferenceLimits;
    clearTableTiles();
    closeMoreMenus(null);
    fileCache = Object.create(null);
    libraryRows = [];
    if (el("librarySearch")) el("librarySearch").value = "";
    if (el("libraryKind")) el("libraryKind").value = "";
    if (el("sessionFilesStats")) el("sessionFilesStats").textContent = "";
    ['sessionFilesUploaded', 'sessionFilesGenerated'].forEach(function (id) {
      if (el(id)) el(id).replaceChildren();
    });
    retentionIdleDays = 0;
    listLoaded = false;
    closeSessionFilePreview();
  }

  function el(id) { return document.getElementById(id); }

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  var PRIMARY_FILE_ACTIONS = {
    preview: ['eye', '预览', '预览'], export: ['export', '导出', '导出已有成果'],
    download: ['download', '原件', '下载原件']
  };

  function fileIcon(name, size) {
    return window.AppIcon ? window.AppIcon.html(name, {size: size || 16}) : '';
  }

  function fileActionContent(icon, label) {
    return fileIcon(icon) + '<span>' + esc(label) + '</span>';
  }

  function filePrimaryActionHtml(action, file, ariaLabel) {
    var spec = PRIMARY_FILE_ACTIONS[action];
    return '<button type="button" class="file-primary-action' + (action === 'preview' ? ' session-file-preview-btn' : '') +
      '" data-file-act="' + action + '" data-file-id="' + esc(file.file_id) +
      '" title="' + esc(ariaLabel) + '" aria-label="' + esc(ariaLabel) + '"' +
      (action === 'download' ? '' : ' aria-haspopup="dialog"') + '>' + fileActionContent(spec[0], spec[1]) + '</button>';
  }

  function fmtSize(n) {
    n = Number(n || 0);
    if (!n) return '0 B';
    var u = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
  }

  function fmtTime(v) {
    var ts = Number(v || 0);
    if (!ts) return '';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString();
  }

  var KIND_LABEL = {
    pdf: 'PDF', image: '图片', html: 'HTML 报告', table: '表格',
    docx: 'Word', text: '文本', download: '文件', svg: 'SVG'
  };

  function previewMetaText(file) {
    var meta = '资料库文件 · ' + (KIND_LABEL[displayKind(file)] || '文件');
    var last = fmtTime(file.last_accessed_at);
    if (last) meta += ' · 最近访问 ' + last;
    return meta;
  }

  function currentSessionId() {
    if (typeof window.currentSessionId === 'function') return window.currentSessionId();
    try { return sessionStorage.getItem('citrus_session_id') || ''; } catch (e) { return ''; }
  }

  function itemHtml(f) {
    var kind = displayKind(f);
    var label = KIND_LABEL[kind] || '文件';
    var deletable = f.origin === 'uploaded';
    var svg = isSvgFile(f);
    var jump = (f.turn_request_id && f.source_session_id);
    var menuId = 'session-file-menu-' + String(f.file_id || '');
    var lastAccess = fmtTime(f.last_accessed_at);
    var ext = (String(f.filename || '').match(/\.([^.]+)$/) || ['', ''])[1].toUpperCase().slice(0, 5);
    var thumbnail = (kind === 'image' || kind === 'svg') &&
      Number(f.size_bytes) <= 2*1024*1024 && safePreviewUrl(f.preview_url);
    var tableTile = kind === 'table' && /^[a-f0-9]{32}$/i.test(String(f.file_id || ''));
    var thumbnailHtml = '<span class="session-file-thumb-fallback">' +
      fileIcon(kind === 'table' ? 'table' : kind === 'svg' ? 'canvas' : kind === 'image' ? 'image' : 'file', 24) +
      (ext ? '<small>' + esc(ext) + '</small>' : '') + '</span>' + (thumbnail
      ? '<img src="' + esc(thumbnail + (thumbnail.includes('?') ? '&' : '?') + 'library_tile=1') +
        '" alt="" loading="lazy" decoding="async">' : '');
    // Keep the primary actions in the same order as on generated chat cards.
    // File-specific secondary operations retain their existing menu semantics.
    return '' +
      '<div class="session-file-item" data-file-id="' + esc(f.file_id) + '"' +
      '     data-kind="' + esc(kind) + '" data-filename="' + esc(f.filename) + '">' +
      '  <div class="session-file-thumb k-' + esc(kind) + '" aria-hidden="true"' +
           (tableTile ? ' data-table-tile="' + esc(f.file_id) + '"' : '') + '>' + thumbnailHtml + '</div>' +
      '  <div class="session-file-open">' +
      '    <span class="session-file-name" title="' + esc(f.filename) + '">' + esc(f.filename) + '</span>' +
      '    <div class="session-file-sub" title="' + esc((lastAccess ? '最近使用 ' + lastAccess : fmtTime(f.created_at))) + '">' +
      '      <span class="session-file-badge k-' + esc(kind) + '">' + esc(label) + '</span> ' + esc(fmtSize(f.size_bytes)) + '</div>' +
      '  </div>' +
      '  <div class="session-file-actions">' +
      '    <div class="file-primary-actions">' +
             filePrimaryActionHtml('preview', f, '预览 ' + f.filename) +
             filePrimaryActionHtml('export', f, '导出已有成果 ' + f.filename) +
             filePrimaryActionHtml('download', f, '下载原件 ' + f.filename) + '</div>' +
      '    <button type="button" class="session-file-ref-pill" data-file-act="reference" data-file-id="' +
           esc(f.file_id) + '" title="在当前对话中引用" aria-label="引用 ' +
           esc(f.filename) + '">' + fileActionContent('plus', '引用') + '</button>' +
      '    <button type="button" class="session-file-act session-file-more-btn" data-file-act="more" data-file-id="' +
           esc(f.file_id) + '" title="更多操作" aria-label="更多操作（' + esc(f.filename) +
           '）" aria-haspopup="menu" aria-controls="' + esc(menuId) + '" aria-expanded="false">' + fileIcon('more') + '</button>' +
      '  </div>' +
      '  <div class="session-file-more" id="' + esc(menuId) + '" data-file-menu="' + esc(f.file_id) +
      '" role="menu" aria-label="更多操作" hidden>' +
      '    <button type="button" role="menuitem" data-file-act="info" data-file-id="' + esc(f.file_id) + '">' + fileActionContent('info', '文件信息') + '</button>' +
      (svg
        ? '    <button type="button" role="menuitem" data-file-act="copy-svg" data-file-id="' + esc(f.file_id) + '">' + fileActionContent('copy', '复制 SVG 源码') + '</button>'
        : '') +
      (canImportSvg(f)
        ? '    <button type="button" role="menuitem" data-file-act="edit-svg-copy" data-file-id="' + esc(f.file_id) + '">' + fileActionContent('edit', '尝试创建可编辑 SVG 副本') + '</button>'
        : '') +
      (jump
        ? '    <button type="button" role="menuitem" data-file-act="jump" data-file-id="' + esc(f.file_id) + '">' + fileActionContent('external', '定位到来源会话') + '</button>'
        : '') +
      // Generated deliverables are owned by the research pipeline: the library
      // never offers deletion for them. Uploaded files may be removed here.
      (deletable
        ? '    <button type="button" role="menuitem" class="danger" data-file-act="delete" data-file-id="' +
          esc(f.file_id) + '">' + fileActionContent('trash', '删除此上传文件') + '</button>'
        : '') +
      '  </div>' +
      '</div>';
  }

  function renderGroup(listEl, groupEl, files) {
    listEl.replaceChildren();
    groupEl.hidden = !files.length;
    if (!files.length) return;
    var shown = 0, batchSize = 50;
    var more = document.createElement('button');
    more.type = 'button';
    more.className = 'session-file-preview-btn session-file-list-more';
    more.setAttribute('aria-controls', listEl.id);
    function appendBatch() {
      var next = Math.min(shown + batchSize, files.length);
      var batch = document.createElement('div');
      batch.innerHTML = files.slice(shown, next).map(itemHtml).join('');
      var first = batch.querySelector('[data-file-act="preview"]');
      var tiles = batch.querySelectorAll('[data-table-tile]');
      batch.querySelectorAll('img').forEach(bindImageTile);
      while (batch.firstChild) listEl.insertBefore(batch.firstChild, more.parentNode ? more : null);
      if (!el('sessionFilesPanel').hidden) watchTableTiles(tiles);
      shown = next;
      more.hidden = shown === files.length;
      if (!more.hidden) more.textContent = '再显示 ' + Math.min(batchSize, files.length - shown) + ' 个文件';
      return first;
    }
    appendBatch();
    if (more.hidden) return;
    listEl.appendChild(more);
    more.addEventListener('click', function () {
      var first = appendBatch();
      if (first) first.focus();
    });
  }

  function bindImageTile(img) {
    function showImage() { if (img.naturalWidth) img.classList.add('is-ready'); }
    img.addEventListener('load', showImage);
    if (img.complete) showImage();
  }

  // Passive samples use a separate, no-retention-touch account route. Never
  // substitute the explicit preview/download route while browsing the list.
  var tableTileObserver = null;
  var tableTilePending = new Set();
  var tableTileActive = new Map();

  function clearTableTiles() {
    if (tableTileObserver) tableTileObserver.disconnect();
    tableTileObserver = null;
    tableTilePending.clear();
    tableTileActive.forEach(function (request) { request.abort(); });
    tableTileActive.clear();
  }

  function watchTableTiles(tiles) {
    if (!tiles.length || !window.IntersectionObserver || !window.AbortController || !window.accountFetch) return;
    if (!tableTileObserver) {
      tableTileObserver = new window.IntersectionObserver(function (entries, observer) {
        if (observer !== tableTileObserver) return;
        entries.forEach(function (entry) {
          var tile = entry.target;
          if (entry.isIntersecting && !tableTileActive.has(tile)) tableTilePending.add(tile);
          else tableTilePending.delete(tile);
        });
        fillTableTiles();
      }, {root: el('sessionFilesBody')});
    }
    tiles.forEach(function (tile) { tableTileObserver.observe(tile); });
  }

  function fillTableTiles() {
    while (tableTileObserver && tableTileActive.size < 2 && tableTilePending.size) {
      var tile = tableTilePending.values().next().value;
      tableTilePending.delete(tile);
      tableTileObserver.unobserve(tile);
      if (!el('sessionFilesPanel').hidden && document.contains(tile)) fetchTableTile(tile);
    }
  }

  function fetchTableTile(tile) {
    var request = new window.AbortController();
    var fileId = String(tile.dataset.tableTile || '');
    if (!/^[a-f0-9]{32}$/i.test(fileId)) return;
    var timer = window.setTimeout(function () { request.abort(); }, 8000);
    tableTileActive.set(tile, request);
    window.accountFetch('/api/v2/files/' + encodeURIComponent(fileId) + '/table-sample', {
      credentials: 'same-origin', headers: {'Accept': 'application/json'}, signal: request.signal
    }).then(function (res) { return res.ok ? res.json() : null; }).then(function (payload) {
      if (!request.signal.aborted && document.contains(tile) && tableTileActive.get(tile) === request) {
        renderTableTile(tile, payload && payload.rows);
      }
    }).catch(function () { /* The format icon remains when no sample is available. */ })
      .finally(function () {
        window.clearTimeout(timer);
        if (tableTileActive.get(tile) === request) {
          tableTileActive.delete(tile);
          fillTableTiles();
        }
      });
  }

  function renderTableTile(tile, rows) {
    if (!Array.isArray(rows) || !rows.length) return;
    var tbody = document.createElement('tbody');
    var hasValues = false;
    rows.slice(0, 3).forEach(function (row) {
      if (!Array.isArray(row) || !row.length) return;
      var tr = document.createElement('tr');
      row.slice(0, 4).forEach(function (value) {
        var td = document.createElement('td');
        td.textContent = typeof value === 'string' ? value.slice(0, 80) : '';
        if (td.textContent.trim()) hasValues = true;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    if (!hasValues) return;
    var sample = document.createElement('table');
    sample.className = 'session-file-table-sample';
    sample.appendChild(tbody);
    tile.appendChild(sample);
    tile.classList.add('has-table-sample');
  }

  function retentionSentence(days) {
    var n = Math.floor(Number(days) || 0);
    if (n > 0) {
      return '浏览资料库不会续期。预览、下载、引用或计算只刷新所用文件的保留期限；连续 ' +
             n + ' 天未使用且没有已保存项目引用的普通文件会自动清理。';
    }
    return '账号资料库 · 跨会话可引用；长期未使用的文件会自动清理。';
  }

  function renderFilteredLibrary() {
    clearTableTiles();
    closeMoreMenus(null);
    var empty = el('sessionFilesEmpty');
    var query = String(el('librarySearch').value || '').trim().toLowerCase();
    var kind = el('libraryKind').value;
    var order = el('librarySort').value;
    var known = ['table','image','svg','pdf','docx','html'];
    var rows = libraryRows.filter(function (file) {
      var type = displayKind(file);
      return (!query || String(file.filename || '').toLowerCase().includes(query)) &&
        (!kind || kind === type || kind === 'other' && !known.includes(type));
    });
    rows.sort(function (a,b) {
      if (order === 'name') return String(a.filename).localeCompare(String(b.filename), 'zh-CN');
      if (order === 'created') return (b.created_at || 0) - (a.created_at || 0);
      return (b.last_accessed_at || b.created_at || 0) - (a.last_accessed_at || a.created_at || 0);
    });
    renderGroup(el('sessionFilesUploaded'), el('sessionFilesUploadedGroup'), rows);
    el('sessionFilesGeneratedGroup').hidden = true;
    empty.classList.remove('session-files-error');
    empty.hidden = !!rows.length;
    empty.textContent = libraryRows.length ? '没有符合筛选条件的资料。' : '还没有资料，上传后可在对话中引用。';
  }

  function renderList(payload) {
    libraryRows = (payload.uploaded || []).concat(payload.generated || []).filter(function (f) {
      return f.visibility !== 'internal';
    });
    retentionIdleDays = Math.max(0, Math.floor(Number(payload.retention_idle_days) || 0));
    var limits = payload.reference_limits;
    referenceLimits = limits && Number.isSafeInteger(limits.max_turn_files) && limits.max_turn_files > 0 &&
      Number.isSafeInteger(limits.max_reference_bytes) && limits.max_reference_bytes > 0
      ? limits : defaultReferenceLimits;
    fileCache = Object.create(null);
    libraryRows.forEach(function(file) { fileCache[file.file_id] = file; });
    el('sessionFilesStats').textContent = '共 ' + libraryRows.length + ' 份资料 · ' +
      fmtSize(libraryRows.reduce(function (n,file) { return n+(Number(file.size_bytes)||0); },0));
    renderFilteredLibrary();
    var retention = el('sessionFilesRetention');
    if (retention) retention.textContent = retentionSentence(payload.retention_idle_days);
  }

  function bindLibraryUpload() {
    var input = el('libraryUploadInput'), button = el('libraryUploadButton');
    var zone = el('libraryUploadZone'), status = el('libraryUploadStatus');
    var formats = /\.(?:csv|tsv|xlsx|xls|xlsm|xlsb|pdf|docx|jpe?g|png|webp|gif|svg|txt|md)$/i;
    button.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () {
      upload(Array.from(input.files)); input.value = '';
    });
    zone.addEventListener('dragover', function (event) {
      if (!Array.from(event.dataTransfer.types).includes('Files')) return;
      event.preventDefault(); if (!button.disabled) zone.classList.add('is-dragging');
    });
    zone.addEventListener('dragleave', function () { zone.classList.remove('is-dragging'); });
    zone.addEventListener('drop', function (event) {
      event.preventDefault(); zone.classList.remove('is-dragging');
      upload(Array.from(event.dataTransfer.files));
    });
    async function upload(files) {
      if (button.disabled || !files.length) return;
      var sid = currentSessionId(), scope = catalogScope;
      status.hidden = false;
      if (!sid) { status.textContent = '请先打开一个会话。'; return; }
      if (files.length > 8) { status.textContent = '每次最多上传 8 个文件。'; return; }
      button.disabled = true;
      var saved = 0, failures = [];
      try {
        for (var index = 0; index < files.length && scope === catalogScope; index++) {
          var file = files[index];
          status.textContent = '正在上传 ' + (index+1) + ' / ' + files.length;
          if (!formats.test(file.name) || file.size > 20*1024*1024 || !file.size) {
            failures.push(file.name + '：格式或大小不符');
            continue;
          }
          try {
            var path = '/api/v2/session/' + encodeURIComponent(sid) + '/files/upload?filename=' + encodeURIComponent(file.name);
            var res = await (window.accountFetch || fetch)(path,{method:'POST',body:file});
            if (!res.ok) {
              var body = await res.json().catch(function () { return {}; });
              failures.push(file.name + '：' + (typeof body.detail === 'string' ? body.detail : '上传失败'));
            } else saved++;
          } catch (_) { failures.push(file.name + '：上传失败'); }
        }
        if (scope === catalogScope) {
          status.textContent = (saved ? '已上传 ' + saved + ' 份。' : '') + failures.join('；');
          if (saved) await loadSessionFiles();
        }
      } finally { button.disabled = false; }
    }
  }

  function loadSessionFiles() {
    var scope = catalogScope, serial = ++catalogRequestSerial;
    function ownsList() { return scope === catalogScope && serial === catalogRequestSerial; }
    var fetchCatalog = window.accountFetch || fetch;
    var empty = el('sessionFilesEmpty');
    var hasFiles = (el('sessionFilesUploaded') && el('sessionFilesUploaded').children.length)
      || (el('sessionFilesGenerated') && el('sessionFilesGenerated').children.length);
    if (empty && !hasFiles) {
      empty.hidden = false;
      empty.classList.remove('session-files-error');
      empty.textContent = '正在加载资料库…';
    }
    var request = fetchCatalog(apiBase + '/api/v2/files',
                 { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) {
          var err = new Error('library-unavailable');
          err.status = r.status;
          throw err;
        }
        return r.json();
      })
      .then(function (d) {
        if (!ownsList()) return scope === catalogScope ? latestCatalogRequest : undefined;
        renderList(d || {});
        listLoaded = true;
        decorateTurnCards();
        var empty = el('sessionFilesEmpty');
        if (empty) empty.classList.remove('session-files-error');
        return d || {};
      })
      .catch(function (err) {
        if (!ownsList()) return scope === catalogScope ? latestCatalogRequest : undefined;
        if (err && err.name === 'AbortError') return;
        var empty = el('sessionFilesEmpty');
        if (empty) {
          empty.hidden = false;
          empty.classList.add('session-files-error');
          empty.textContent = err && err.status === 401
            ? '登录后可查看你的资料库。'
            : '资料库暂时无法加载，请稍后重试。';
        }
      });
    latestCatalogRequest = request;
    return request;
  }

  function toggleSessionFiles(show) {
    var p = el('sessionFilesPanel');
    if (!p) return;
    var want = (typeof show === 'boolean') ? show : p.hidden;
    var restoreFocus = !want && p.contains(document.activeElement);
    p.hidden = !want;
    document.body.classList.toggle('session-files-open', want);
    var toggle = el('sessionFilesRailBtn');
    if (toggle) {
      toggle.setAttribute('aria-expanded', want ? 'true' : 'false');
      toggle.classList.toggle('active', want);
    }
    if (want) {
      syncLibraryBounds(); scheduleLibraryColumns();
      var request = loadSessionFiles();
      var close = p.querySelector('.session-files-close');
      if (close && isDrawerMode()) close.focus();
      return request;
    } else {
      clearTableTiles();
      closeMoreMenus(null);
      if (toggle && restoreFocus) toggle.focus();
    }
  }

  // P1-3: 打开前记住焦点，关闭后还回去（键盘用户不会掉到页面顶部）
  var previewReturnFocus = null;
  var exportViewSerial = 0;

  function closeSessionFilePreview() {
    exportViewSerial++;
    releasePreviewResources();
    var o = el('sessionFilePreviewOverlay');
    if (!o || o.hidden) return;
    o.hidden = true;
    var stage = el('sessionFilePreviewStage');
    if (stage) { stage.innerHTML = ''; delete stage.dataset.previewKind; }
    var back = previewReturnFocus;
    previewReturnFocus = null;
    if (back && typeof back.focus === 'function' && document.contains(back)) {
      try { back.focus(); } catch (_) {}
    }
  }
  function previewStageHtml(file, meta) {
    var kind = displayKind(file);
    var url = safePreviewUrl(file.preview_url);
    var dl = safePreviewUrl(file.download_url);
    if (kind === 'svg') {
      if (!url) return unsafePreviewHtml(dl);
      // 安全边界：SVG 只以 <img> 载入（图片上下文里脚本、外链、事件处理器都不
      // 执行），绝不把源码 innerHTML 注入聊天页；要看源码请用「复制 SVG 源码」。
      return '<div class="sf-image-wrap"><img class="sf-preview-image" src="' +
             esc(url) + '" alt="' + esc(file.filename) + '"></div>' +
             '<div class="sf-preview-note" data-preview-error hidden></div>';
    }
    if (kind === 'pdf') {
      if (!url) return unsafePreviewHtml(dl);
      // 不再靠 iframe.onload 断言"已展示"：先把字节取回来、验过 PDF 魔数，再交给
      // 浏览器渲染。取不到 / 不是 PDF / 渲染器缺失时，显示可点击的兜底动作。
      return '<div class="sf-pdf-wrap" data-src="' + esc(url) + '" data-dl="' + esc(dl) + '">' +
             '<div class="sf-pdf-status">正在读取 PDF…</div>' +
             '<iframe class="sf-preview-frame sf-pdf-frame" title="PDF 预览" hidden></iframe>' +
             '<div class="sf-pdf-fallback" hidden>' +
             '<div class="sf-pdf-fallback-text"></div>' +
             '<div class="sf-pdf-fallback-actions">' +
             '<a class="api-secondary" href="' + esc(url) + '" target="_blank" rel="noopener">在新窗口打开</a>' +
             '<a class="api-secondary" href="' + esc(dl) + '" download>下载原件</a>' +
             '</div></div></div>';
    }
    if (kind === 'image') {
      if (!url) return unsafePreviewHtml(dl);
      return '<div class="sf-image-wrap"><img class="sf-preview-image" src="' +
             esc(url) + '" alt="' + esc(file.filename) + '"></div>' +
             '<div class="sf-preview-note" data-preview-error hidden></div>';
    }
    if (kind === 'html') {
      if (!url) return unsafePreviewHtml(dl);
      return '<iframe class="sf-preview-frame" sandbox="" src="' + esc(url) +
             '" title="HTML 报告预览（已隔离）"></iframe>' +
             '<div class="sf-preview-note" data-preview-error hidden></div>';
    }
    if (kind === 'text') {
      if (!url) return unsafePreviewHtml(dl);
      return '<pre class="sf-preview-text" data-src="' + esc(url) + '">加载中…</pre>';
    }
    if (kind === 'table') {
      if (!dl) return unsafePreviewHtml('');
      return '<div class="sf-preview-table" data-src="' + esc(dl) +
             '" data-name="' + esc(file.filename) + '">解析中…</div>';
    }
    if (kind === 'docx') {
      if (!dl) return unsafePreviewHtml('');
      return '<div class="sf-preview-docx">Word 文档以只读方式预览，' +
             '排版可能与原件略有差异。<br><a href="' + esc(dl) +
             '" download>下载原件查看完整格式</a></div>';
    }
    if (!dl) return unsafePreviewHtml('');
    return '<div class="sf-preview-fallback">该格式暂不支持在线预览。<br>' +
           '<a href="' + esc(dl) + '" download>下载原件</a></div>';
  }

  // Phase 5: one preview owns at most one in-flight read and its Blob URLs.
  // Closing or replacing the dialog aborts that read and revokes the bytes, so
  // a slow response cannot paint into a stale panel or leak memory.
  var PREVIEW_TIMEOUT_MS = 20000;
  var previewSerial = 0;
  var previewRequest = null;
  var previewTimer = 0;
  var _previewObjectUrls = [];

  function safePreviewUrl(value) {
    var url = String(value == null ? '' : value).trim();
    if (!url) return '';
    if (/[\\\x00-\x20]/.test(url)) return '';
    if (url.charAt(0) === '/') return url.charAt(1) === '/' ? '' : url;
    var origin = '';
    try { origin = String((window.location && window.location.origin) || ''); } catch (e) { origin = ''; }
    if (/^https?:\/\//i.test(url) && origin && url.indexOf(origin + '/') === 0) return url;
    return '';
  }

  function registeredSvgUrl(file) {
    if (!file || !isSvgFile(file) || !/^[a-f0-9]{32}$/i.test(String(file.file_id || ''))) return '';
    var expected = '/api/v2/files/' + encodeURIComponent(file.file_id) + '/download';
    return safePreviewUrl(file.download_url) === expected ? expected : '';
  }

  function unsafePreviewHtml(downloadUrl) {
    return '<div class="sf-preview-fallback">' +
      '<p class="sf-preview-warn">预览地址不是本站受保护的地址，已拒绝加载。</p>' +
      (downloadUrl ? '<a href="' + esc(downloadUrl) + '" download>下载原件</a>' : '') +
      '</div>';
  }

  function releasePreviewResources() {
    previewSerial++;
    if (previewTimer) {
      try { window.clearTimeout(previewTimer); } catch (e) { /* 忽略 */ }
      previewTimer = 0;
    }
    var request = previewRequest;
    previewRequest = null;
    if (request && request.controller && typeof request.controller.abort === 'function') {
      try { request.controller.abort(); } catch (e) { /* 已结束 */ }
    }
    while (_previewObjectUrls.length) {
      try { URL.revokeObjectURL(_previewObjectUrls.pop()); } catch (e) { /* 忽略 */ }
    }
  }

  function beginPreviewRequest() {
    releasePreviewResources();
    var request = { serial: previewSerial, controller: null, timedOut: false, signal: null };
    if (typeof window.AbortController === 'function') {
      request.controller = new window.AbortController();
      request.signal = request.controller.signal;
      previewRequest = request;
      previewTimer = window.setTimeout(function () {
        if (previewRequest !== request) return;
        request.timedOut = true;
        try { request.controller.abort(); } catch (e) { /* 已结束 */ }
      }, PREVIEW_TIMEOUT_MS);
    } else {
      previewRequest = request;
    }
    return request;
  }

  function endPreviewRequest(request) {
    if (previewRequest !== request) return;
    if (previewTimer) {
      try { window.clearTimeout(previewTimer); } catch (e) { /* 忽略 */ }
      previewTimer = 0;
    }
    previewRequest = null;
  }

  function previewStillCurrent(request, node) {
    if (!request || request.serial !== previewSerial) return false;
    var overlay = el('sessionFilePreviewOverlay');
    if (!overlay || overlay.hidden) return false;
    if (node && typeof document.contains === 'function' && !document.contains(node)) return false;
    return true;
  }

  function fetchOptions(request) {
    var options = { credentials: 'same-origin' };
    if (request && request.signal) options.signal = request.signal;
    return options;
  }

  function headerValue(response, name) {
    try {
      return response && response.headers && typeof response.headers.get === 'function'
        ? String(response.headers.get(name) || '') : '';
    } catch (e) { return ''; }
  }

  function contentTypeOf(response) {
    return headerValue(response, 'content-type').split(';')[0].trim().toLowerCase();
  }

  function responseFailure(response) {
    if (response && response.redirected) {
      var redirected = new Error('redirected');
      redirected.redirectedTo = String(response.url || '');
      return redirected;
    }
    if (response && response.status === 401) {
      var unauthorized = new Error('unauthorized');
      unauthorized.status = 401;
      return unauthorized;
    }
    if (response && response.status === 403) {
      var forbidden = new Error('forbidden');
      forbidden.status = 403;
      return forbidden;
    }
    if (response && response.status === 404) {
      var missing = new Error('not-found');
      missing.status = 404;
      return missing;
    }
    if (!response || !response.ok) {
      var status = response && response.status ? response.status : 0;
      var http = new Error(status ? 'HTTP ' + status : 'network');
      http.status = status;
      return http;
    }
    return null;
  }

  function typeMismatch(expected, actual) {
    var err = new Error('type-mismatch:' + (actual || 'unknown'));
    err.expected = expected;
    return err;
  }

  function pdfTypeFailure(response) {
    var type = contentTypeOf(response);
    if (!type) return null;
    if (type === 'application/pdf' || type === 'application/octet-stream') return null;
    if (type === 'text/html' || type === 'application/xhtml+xml') return new Error('html-instead');
    return typeMismatch('PDF', type);
  }

  function textTypeFailure(response) {
    var type = contentTypeOf(response);
    if (!type) return null;
    if (type === 'text/html' || type === 'application/xhtml+xml') return new Error('html-instead');
    if (type.indexOf('text/') === 0) return null;
    if (type === 'application/json' || type === 'application/xml' ||
        type === 'application/x-bibtex' || type === 'application/x-research-info-systems' ||
        type === 'application/yaml' || type === 'application/x-yaml' ||
        type === 'application/toml' || type === 'application/octet-stream') return null;
    return typeMismatch('text', type);
  }

  function tableTypeFailure(response, isCsv) {
    var type = contentTypeOf(response);
    if (!type) return null;
    if (type === 'text/html' || type === 'application/xhtml+xml') return new Error('html-instead');
    if (isCsv) {
      if (type === 'text/csv' || type === 'text/tab-separated-values' ||
          type === 'text/plain' || type === 'application/csv' ||
          type === 'application/vnd.ms-excel' ||
          type === 'application/octet-stream') return null;
    } else {
      if (type === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' ||
          type === 'application/vnd.ms-excel' || type === 'application/octet-stream' ||
          type === 'application/zip' || type === 'application/x-zip-compressed') return null;
    }
    return typeMismatch(isCsv ? 'CSV' : 'XLSX', type);
  }

  function asciiPrefix(buffer, count) {
    var bytes = new Uint8Array(buffer, 0, Math.min(count || 16, buffer.byteLength || 0));
    var text = '';
    for (var i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
    return text;
  }

  function looksLikeHtml(buffer) {
    var head = asciiPrefix(buffer, 256).replace(/^\uFEFF/, '').trim().toLowerCase();
    return head.indexOf('<!doctype html') === 0 || head.indexOf('<html') === 0;
  }

  function textLooksLikeHtml(text) {
    var head = String(text || '').replace(/^\uFEFF/, '').trim().toLowerCase();
    return head.indexOf('<!doctype html') === 0 || head.indexOf('<html') === 0;
  }

  function hasBinaryNul(buffer) {
    var bytes = new Uint8Array(buffer, 0, Math.min(512, buffer.byteLength || 0));
    for (var i = 0; i < bytes.length; i++) {
      if (bytes[i] === 0) return true;
    }
    return false;
  }

  function pdfMagic(buffer) {
    return asciiPrefix(buffer, 5) === '%PDF-';
  }

  function csvSignatureOk(buffer) {
    return !looksLikeHtml(buffer) && !hasBinaryNul(buffer);
  }

  function tableSignatureOk(buffer, name) {
    var bytes = new Uint8Array(buffer, 0, Math.min(8, buffer.byteLength || 0));
    if (!bytes.length) return false;
    var isXls = /\.xls$/i.test(name) && !/\.xlsx$/i.test(name);
    if (isXls) {
      return bytes.length >= 8 && bytes[0] === 0xD0 && bytes[1] === 0xCF &&
        bytes[2] === 0x11 && bytes[3] === 0xE0 && bytes[4] === 0xA1 &&
        bytes[5] === 0xB1 && bytes[6] === 0x1A && bytes[7] === 0xE1;
    }
    return bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4B &&
      (bytes[2] === 0x03 || bytes[2] === 0x05 || bytes[2] === 0x07);
  }

  function pdfFailureText(reason) {
    if (reason === 'stream-unavailable') return '浏览器无法流式预览此文件，请下载原件。';
    if (reason === 'timeout') return '读取超时，文件没有在限定时间内返回；请检查网络后重试或下载原件。';
    if (reason === 'redirected') return '预览请求被重定向到其他地址（可能是登录页或错误页），未加载内容；请重新登录或刷新后重试。';
    if (reason === 'html-instead') return '服务器返回的是网页（HTML），不是 PDF 文件，未做预览。';
    if (reason === 'not-pdf') return '服务器返回的内容不是 PDF 文件，未做预览。';
    if (reason === 'empty') return '文件内容为空（0 字节），未做预览。';
    if (reason === 'unauthorized') return '登录后才能预览这个文件。';
    if (reason === 'forbidden') return '无权访问该文件（它属于其他会话或账号），请联系文件所有者。';
    if (reason === 'not-found') return '文件不存在或已被删除，请刷新侧栏确认。';
    if (reason.indexOf('type-mismatch:') === 0) {
      return '服务器返回的类型（' + reason.slice('type-mismatch:'.length) +
        '）不是 PDF，未做预览；请下载原件。';
    }
    if (reason.indexOf('HTTP ') === 0) {
      return '服务器返回 ' + reason + '，未能读取文件；请稍后重试或下载。';
    }
    if (/Failed to fetch|NetworkError|Load failed|AbortError/i.test(reason)) {
      return '网络中断或请求已取消，文件没能下载下来；请检查网络后重试，或下载到本地再打开。';
    }
    return '读取失败（' + reason + '），请重试或下载到本地打开。';
  }

  function textFailureText(reason) {
    if (reason === 'stream-unavailable') return '浏览器无法流式预览此文件，请下载原件。';
    if (reason === 'timeout') return '读取超时，文件没有在限定时间内返回；请检查网络后重试或下载原件。';
    if (reason === 'redirected') return '预览请求被重定向到其他地址（可能是登录页或错误页），未显示内容；请重新登录或刷新后重试。';
    if (reason === 'html-instead') return '服务器返回的是网页（HTML），不是文本文件；未显示内容，请下载原件。';
    if (reason === 'binary') return '文件包含二进制内容，不是可读文本；请下载原件。';
    if (reason === 'empty') return '文件内容为空（0 字节）。';
    if (reason === 'unauthorized') return '登录后才能预览这个文件。';
    if (reason === 'forbidden') return '无权访问该文件（它属于其他会话或账号），请联系文件所有者。';
    if (reason === 'not-found') return '文件不存在或已被删除，请刷新资料库确认。';
    if (reason.indexOf('type-mismatch:') === 0) {
      return '服务器返回的类型（' + reason.slice('type-mismatch:'.length) +
        '）不是文本；请下载原件。';
    }
    if (reason.indexOf('HTTP ') === 0) {
      return '服务器返回 ' + reason + '，未能读取文件；请稍后重试或下载原件。';
    }
    return '读取失败，请下载原件查看。';
  }

  function directPreviewNodes(stage) {
    var nodes = [];
    Array.prototype.forEach.call(stage.querySelectorAll('.sf-preview-image'), function (node) {
      nodes.push(node);
    });
    Array.prototype.forEach.call(stage.querySelectorAll('.sf-preview-frame'), function (node) {
      if (node.classList && node.classList.contains('sf-pdf-frame')) return;
      nodes.push(node);
    });
    return nodes;
  }

  function bindDirectPreview(stage) {
    if (!stage || typeof stage.querySelectorAll !== 'function') return;
    var note = stage.querySelector('[data-preview-error]');
    directPreviewNodes(stage).forEach(function (node) {
      if (!safePreviewUrl(node.getAttribute('src'))) {
        node.removeAttribute('src');
        node.hidden = true;
        if (note) {
          note.hidden = false;
          note.textContent = '预览地址不是本站受保护的地址，已拒绝加载。';
        }
        return;
      }
      if (typeof node.addEventListener !== 'function') return;
      node.addEventListener('error', function () {
        if (!note) return;
        note.hidden = false;
        note.textContent = String(node.tagName).toLowerCase() === 'iframe'
          ? '报告预览加载失败：可能无权访问、文件已删除或浏览器阻止了内嵌显示。请下载原件查看。'
          : '图片加载失败：可能无权访问、文件已删除或格式不受支持。请下载原件查看。';
      });
      if (String(node.tagName).toLowerCase() !== 'iframe') {
        node.addEventListener('load', function () { if (note) note.hidden = true; });
      }
    });
  }

  function loadPdfPreview(node) {
    var src = safePreviewUrl(node.getAttribute('data-src'));
    var status = node.querySelector('.sf-pdf-status');
    var frame = node.querySelector('.sf-pdf-frame');
    var fallback = node.querySelector('.sf-pdf-fallback');
    var fbText = node.querySelector('.sf-pdf-fallback-text');

    function fail(message) {
      if (status) status.hidden = true;
      if (frame) frame.hidden = true;
      if (fallback) fallback.hidden = false;
      if (fbText) fbText.textContent = message;
    }

    if (!src) { fail('没有可用的预览地址。'); return; }
    var request = beginPreviewRequest();
    var MAX_INLINE_PDF = 40 * 1024 * 1024;
    fetch(src, fetchOptions(request))
      .then(function (r) {
        if (!previewStillCurrent(request, node)) throw new Error('stale');
        var failure = responseFailure(r);
        if (failure) throw failure;
        failure = pdfTypeFailure(r);
        if (failure) throw failure;
        return readPreviewBytes(r, MAX_INLINE_PDF, request);
      })
      .then(function (buf) {
        if (!previewStillCurrent(request, node)) throw new Error('stale');
        if (!buf || !buf.byteLength) throw new Error('empty');
        if (!pdfMagic(buf)) throw new Error('not-pdf');
        if (navigator.pdfViewerEnabled === false) {
          endPreviewRequest(request);
          fail('PDF 已读取，但此浏览器没有内嵌 PDF 阅读器。可在新窗口打开或下载原件。');
          return;
        }
        var blobUrl = URL.createObjectURL(new Blob([buf], { type: 'application/pdf' }));
        _previewObjectUrls.push(blobUrl);
        if (!previewStillCurrent(request, node)) {
          try { URL.revokeObjectURL(blobUrl); } catch (e) { /* 忽略 */ }
          return;
        }
        if (status) status.hidden = true;
        if (fallback) fallback.hidden = true;
        if (frame) { frame.src = blobUrl; frame.hidden = false; }
        endPreviewRequest(request);
      })
      .catch(function (err) {
        endPreviewRequest(request);
        if (!previewStillCurrent(request, node)) return;
        var reason = String((err && err.message) || err);
        if (reason === 'stale') return;
        if (reason === 'AbortError' && !request.timedOut) return;
        if (request.timedOut) reason = 'timeout';
        fail(reason === 'too-large' ? 'PDF 超过 40 MB 的预览上限，请下载原件。' : pdfFailureText(reason));
      });
  }

  function loadTextPreview(node) {
    var src = safePreviewUrl(node.getAttribute('data-src'));
    if (!src) { node.textContent = '没有可用的预览地址。'; return; }
    var request = beginPreviewRequest();
    fetch(src, fetchOptions(request))
      .then(function (r) {
        if (!previewStillCurrent(request, node)) throw new Error('stale');
        var failure = responseFailure(r);
        if (failure) throw failure;
        failure = textTypeFailure(r);
        if (failure) throw failure;
        return readPreviewBytes(r, 1024 * 1024, request);
      })
      .then(function (body) {
        if (!previewStillCurrent(request, node)) throw new Error('stale');
        if (looksLikeHtml(body)) throw new Error('html-instead');
        if (hasBinaryNul(body)) throw new Error('binary');
        var text = new TextDecoder('utf-8').decode(body);
        if (!text) throw new Error('empty');
        node.textContent = text.slice(0, 200000);
        endPreviewRequest(request);
      })
      .catch(function (err) {
        endPreviewRequest(request);
        if (!previewStillCurrent(request, node)) return;
        var reason = String((err && err.message) || err);
        if (reason === 'stale') return;
        if (reason === 'AbortError' && !request.timedOut) return;
        if (request.timedOut) reason = 'timeout';
        node.textContent = reason === 'too-large' ? '文本超过 1 MB 的预览上限，请下载原件。' : textFailureText(reason);
      });
  }

  // 表格预览：浏览器端解析 CSV / XLSX，不经过后端模型。
  // 上限只在浏览器端生效：超限就明确拒绝解析并要求下载原件，绝不以“截断后
  // 当作完整表格”展示。XLSX 另有 sheetRows 解析上限，超出部分会在页脚写明。
  var TABLE_PAGE_ROWS = 100;              // 每批渲染的 DOM 行数（客户端“显示更多”）
  var TABLE_MAX_RENDERED_ROWS = 2000;     // 单个预览最多留在 DOM 里的数据行数
  var CSV_MAX_BYTES = 8 * 1024 * 1024;    // CSV/TSV 下载字节上限
  var XLSX_MAX_BYTES = 16 * 1024 * 1024;  // XLSX 下载字节上限
  var XLSX_PARSE_ROW_CAP = 2000;          // SheetJS sheetRows 硬上限

  function tooLarge(bytes, limit) {
    var err = new Error('too-large');
    err.bytes = Number(bytes) || 0;
    err.limit = Number(limit) || 0;
    return err;
  }

  function mbLabel(bytes) {
    var mb = (Number(bytes) || 0) / 1048576;
    return (mb >= 10 ? mb.toFixed(0) : mb.toFixed(1)) + ' MB';
  }

  function concatChunks(chunks, total) {
    var out = new Uint8Array(total);
    var offset = 0;
    for (var i = 0; i < chunks.length; i++) {
      out.set(chunks[i], offset);
      offset += chunks[i].byteLength;
    }
    return out.buffer;
  }

  function readPreviewBytes(response, limit, request) {
    var declared = Number(headerValue(response, 'content-length')) || 0;
    if (declared > limit) {
      if (request.controller) request.controller.abort();
      throw tooLarge(declared, limit);
    }
    if (!response.body || typeof response.body.getReader !== 'function') {
      throw new Error('stream-unavailable');
    }
    var reader = response.body.getReader();
    var chunks = [];
    var total = 0;
    function stop(error) {
      reader.cancel().catch(function () {});
      throw error;
    }
    function pump() {
      if (request.serial !== previewSerial) return stop(new Error('stale'));
      return reader.read().then(function (step) {
        if (step.done) {
          if (!total) throw new Error('empty');
          return concatChunks(chunks, total);
        }
        total += step.value.byteLength;
        if (total > limit) return stop(tooLarge(total, limit));
        chunks.push(step.value);
        return pump();
      });
    }
    return pump();
  }

  function fetchTableBytes(url, maxBytes, request, isCsv) {
    return fetch(url, fetchOptions(request)).then(function (response) {
      if (request.serial !== previewSerial) throw new Error('stale');
      var failure = responseFailure(response) || tableTypeFailure(response, isCsv);
      if (failure) throw failure;
      return readPreviewBytes(response, maxBytes, request);
    });
  }

  function tableErrorHtml(src, err, isCsv) {
    var reason = String((err && err.message) || err);
    var limit = isCsv ? CSV_MAX_BYTES : XLSX_MAX_BYTES;
    var message;
    if (reason === 'too-large') {
      message = '文件为 ' + mbLabel(err && err.bytes ? err.bytes : limit) +
        '，超过浏览器端 ' + mbLabel(limit) +
        ' 的解析上限；为避免显示不完整的数据，未在页面内解析。';
    } else if (reason === 'empty') {
      message = '文件内容为空（0 字节），没有可显示的表格。';
    } else if (reason === 'timeout') {
      message = '读取超时，文件没有在限定时间内返回；请检查网络后重试或下载原件。';
    } else if (reason === 'redirected') {
      message = '预览请求被重定向到其他地址（可能是登录页或错误页），未读取内容；请重新登录或刷新资料库后重试。';
    } else if (reason === 'html-instead') {
      message = '服务器返回的是网页（HTML），不是表格数据；可能是登录页或错误页，未解析。';
    } else if (reason === 'not-table') {
      message = '服务器返回的内容与表格格式不符，未在页面内解析；请下载原件查看。';
    } else if (reason === 'unsafe-url') {
      message = '预览地址不是本站受保护的地址，已拒绝读取；请刷新资料库后重试。';
    } else if (err && err.status === 401) {
      message = '登录后才能预览这个文件。';
    } else if (err && err.status === 403) {
      message = '无权访问该文件（它属于其他会话或账号），请联系文件所有者。';
    } else if (err && err.status === 404) {
      message = '文件不存在或已被删除，请刷新资料库确认。';
    } else if (reason.indexOf('type-mismatch:') === 0) {
      message = '服务器返回的类型（' + reason.slice('type-mismatch:'.length) +
        '）与表格格式不符，未在页面内解析；请下载原件查看。';
    } else if (reason.indexOf('HTTP ') === 0) {
      message = '服务器返回 ' + reason + '，未能读取文件；请稍后重试或下载原件。';
    } else if (/Failed to fetch|NetworkError|Load failed|AbortError/i.test(reason)) {
      message = '网络中断或请求已取消，文件没能下载下来；请检查网络后重试，或下载到本地打开。';
    } else {
      message = '表格解析失败，请下载原件查看。';
    }
    return '<div class="sf-preview-note">' + esc(message) + '</div>' +
      '<div class="sf-table-footer"><a class="api-secondary" href="' + esc(src) +
      '" download>下载原件</a></div>';
  }

  function loadTablePreview(node) {
    var src = safePreviewUrl(node.getAttribute('data-src'));
    var name = node.getAttribute('data-name') || '';
    var isCsv = /\.(csv|tsv)$/i.test(name);
    if (!src) {
      node.innerHTML = tableErrorHtml('', new Error('unsafe-url'), isCsv);
      return;
    }
    var request = beginPreviewRequest();
    fetchTableBytes(src, isCsv ? CSV_MAX_BYTES : XLSX_MAX_BYTES, request, isCsv)
      .then(function (buf) {
        if (!previewStillCurrent(request, node)) throw new Error('stale');
        if (isCsv) {
          if (!csvSignatureOk(buf)) throw new Error('not-table');
          renderCsv(node, buf, name);
          endPreviewRequest(request);
          return;
        }
        if (!tableSignatureOk(buf, name)) throw new Error('not-table');
        if (window.XLSX) {
          renderXlsx(node, buf);
        } else {
          node.innerHTML = '该浏览器未加载表格解析组件。' +
            '<br><a href="' + esc(src) + '" download>下载原件</a>';
        }
        endPreviewRequest(request);
      })
      .catch(function (err) {
        endPreviewRequest(request);
        if (!previewStillCurrent(request, node)) return;
        var reason = String((err && err.message) || err);
        if (reason === 'stale') return;
        if (reason === 'AbortError' && !request.timedOut) return;
        if (request.timedOut) err = new Error('timeout');
        node.innerHTML = tableErrorHtml(src, err, isCsv);
      });
  }

  // RFC 4180 兼容：双引号包裹的字段里可以有分隔符、换行和成对双引号。
  // 字段内的换行原样保留在单元格文本里，不会被当成新行拆开。
  function parseDelimitedText(text, delimiter) {
    if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
    var rows = [];
    var row = [];
    var field = '';
    var inQuotes = false;
    var endedWithBreak = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (inQuotes) {
        if (ch === '"') {
          if (text.charAt(i + 1) === '"') { field += '"'; i++; }
          else inQuotes = false;
        } else {
          field += ch;
        }
        endedWithBreak = false;
        continue;
      }
      if (ch === '"') {
        inQuotes = true;
        endedWithBreak = false;
      } else if (ch === delimiter) {
        row.push(field); field = ''; endedWithBreak = false;
      } else if (ch === '\r' || ch === '\n') {
        if (ch === '\r' && text.charAt(i + 1) === '\n') i++;
        row.push(field); rows.push(row);
        row = []; field = ''; endedWithBreak = true;
      } else {
        field += ch;
        endedWithBreak = false;
      }
    }
    if (!endedWithBreak && (field.length || row.length)) {
      row.push(field);
      rows.push(row);
    }
    return rows;
  }

  function csvRowHtml(cells, isHeader) {
    var tag = isHeader ? 'th' : 'td';
    var html = '<tr>';
    for (var i = 0; i < cells.length; i++) {
      html += '<' + tag + '>' + esc(cells[i]) + '</' + tag + '>';
    }
    return html + '</tr>';
  }

  function renderCsv(node, buf, name) {
    var delimiter = /\.tsv$/i.test(name) ? '\t' : ',';
    var text = new TextDecoder('utf-8').decode(buf);
    var rows = parseDelimitedText(text, delimiter);
    if (!rows.length) {
      node.innerHTML = '<div class="sf-preview-note">文件没有可显示的数据行。</div>';
      return;
    }
    node.innerHTML =
      '<div class="sf-table-wrap"><table class="sf-table"><tbody></tbody></table></div>' +
      '<div class="sf-table-footer" aria-live="polite">' +
      '  <span class="sf-table-status"></span>' +
      '  <button type="button" class="sf-load-more" hidden>再显示</button>' +
      '</div>';
    var state = {
      rows: rows,
      rendered: 0,
      tbody: node.querySelector('tbody'),
      status: node.querySelector('.sf-table-status'),
      more: node.querySelector('.sf-load-more')
    };
    node.__sfCsvState = state;
    state.more.addEventListener('click', function () {
      appendCsvRows(node, TABLE_PAGE_ROWS);
    });
    appendCsvRows(node, TABLE_PAGE_ROWS);
  }

  // “显示更多”纯客户端分批：先解析出的行保存在内存里，每次只把下一批写进
  // DOM。服务器没有分页契约，这里也不声称是服务器分页。
  function appendCsvRows(node, count) {
    var state = node.__sfCsvState;
    if (!state) return;
    var moreHadFocus = state.more && document.activeElement === state.more;
    var total = state.rows.length;
    var limit = Math.min(total, state.rendered + count, TABLE_MAX_RENDERED_ROWS);
    var html = '';
    for (var i = state.rendered; i < limit; i++) html += csvRowHtml(state.rows[i], i === 0);
    if (typeof state.tbody.insertAdjacentHTML === 'function') {
      state.tbody.insertAdjacentHTML('beforeend', html);   // 追加，不重排已有行
    } else {
      state.tbody.innerHTML += html;
    }
    state.rendered = limit;
    updateCsvFooter(node);
    // The final batch hides the focused button; keep keyboard users in the
    // table instead of dropping them back to the page body.
    if (moreHadFocus && state.more.hidden) {
      var wrap = node.querySelector('.sf-table-wrap');
      if (wrap) {
        try {
          wrap.setAttribute('tabindex', '-1');
          wrap.focus({ preventScroll: true });
        } catch (err) { /* 旧浏览器没有 preventScroll */ }
      }
    }
  }

  function updateCsvFooter(node) {
    var state = node.__sfCsvState;
    var total = state.rows.length;
    var remaining = total - state.rendered;
    if (remaining > 0 && state.rendered < TABLE_MAX_RENDERED_ROWS) {
      var next = Math.min(TABLE_PAGE_ROWS, remaining, TABLE_MAX_RENDERED_ROWS - state.rendered);
      state.status.textContent = '浏览器端显示：已渲染 ' + state.rendered + ' / ' + total + ' 行。';
      state.more.hidden = false;
      state.more.textContent = '再显示 ' + next + ' 行';
    } else if (remaining > 0) {
      state.status.textContent = '浏览器端最多渲染 ' + TABLE_MAX_RENDERED_ROWS +
        ' 行，剩余 ' + remaining + ' 行未显示；完整内容请下载原件。';
      state.more.hidden = true;
    } else {
      state.status.textContent = '已渲染全部 ' + total + ' 行（浏览器端解析）。';
      state.more.hidden = true;
    }
  }

  function renderXlsx(node, buf) {
    try {
      var wb = window.XLSX.read(buf, {
        type: 'array',
        cellStyles: true,
        cellDates: true,
        cellNF: false,
        // Hard parse bound: never let one workbook freeze the page. When the
        // decoded range reaches this count the footer says so and points to
        // the original file instead of pretending the sheet is complete.
        sheetRows: XLSX_PARSE_ROW_CAP
      });
      var sheets = wb.SheetNames || [];
      var tabs = sheets.map(function (n, i) {
        return '<button type="button" class="sf-sheet-tab' + (i === 0 ? ' active' : '') +
               '" data-sheet="' + esc(n) + '">' + esc(n) + '</button>';
      }).join('');
      node.innerHTML = '<div class="sf-sheet-tabs">' + tabs +
        '</div><div class="sf-sheet-body"></div>';
      var body = node.querySelector('.sf-sheet-body');
      var state = { visibleRows: TABLE_PAGE_ROWS, sheet: '' };

      function colorOf(spec) {
        if (!spec || !spec.rgb) return '';
        var v = String(spec.rgb);
        if (v.length === 8) v = v.slice(2);
        return /^[0-9A-Fa-f]{6}$/.test(v) ? '#' + v.toLowerCase() : '';
      }

      // SheetJS 读 xlsx 时把填充摊平在 s 上（s.patternType + s.fgColor），
      // 读 xls 时则在 s.fill 里；两种形态都要认，否则标注色一块都显示不出来。
      function fillColor(st) {
        if (!st) return '';
        if (st.fill) {
          var nested = colorOf(st.fill.fgColor || st.fill.bgColor);
          if (nested) return nested;
        }
        if (st.patternType && st.patternType !== 'none') return colorOf(st.fgColor);
        return '';
      }

      function cellInlineStyle(cell) {
        var st = cell && cell.s;
        if (!st) return '';
        var css = [];
        var bg = fillColor(st);
        if (bg && bg !== '#ffffff' && bg !== '#000000') css.push('background:' + bg);
        var fg = colorOf(st.font && st.font.color);
        if (fg) css.push('color:' + fg);
        if (st.font && st.font.bold) css.push('font-weight:700');
        if (st.font && st.font.italic) css.push('font-style:italic');
        return css.join(';');
      }

      function hasBorder(cell) {
        var b = cell && cell.s && cell.s.border;
        if (!b) return false;
        return ['top', 'bottom', 'left', 'right'].some(function (side) {
          return b[side] && b[side].style;
        });
      }

      function commentOf(cell) {
        var c = cell && cell.c;
        if (!c || !c.length) return '';
        return String(c[0].t || '').trim();
      }

      var pendingFocus = null;
      var utils = window.XLSX.utils;
      var a1Range = /^\$?[A-Z]{1,3}\$?[1-9]\d{0,6}(?::\$?[A-Z]{1,3}\$?[1-9]\d{0,6})?$/i;

      function inside(point, range) {
        return point.r >= range.s.r && point.r <= range.e.r && point.c >= range.s.c && point.c <= range.e.c;
      }

      function workbookLocation(sheet, address) {
        if (!a1Range.test(String(address || '')) || sheets.indexOf(sheet) < 0) return null;
        var targetSheet = wb.Sheets[sheet];
        if (!targetSheet || !targetSheet['!ref']) return null;
        var normal = address.replace(/\$/g, '').toUpperCase();
        var location = normal.includes(':') ? utils.decode_range(normal) : {s: utils.decode_cell(normal)};
        var parsed = utils.decode_range(targetSheet['!ref']);
        var extent = utils.decode_range(targetSheet['!fullref'] || targetSheet['!ref']);
        if (!inside(location.s, extent) || location.e && (!inside(location.e, extent) ||
            location.s.r > location.e.r || location.s.c > location.e.c)) return null;
        var merge = (targetSheet['!merges'] || []).find(function (m) { return inside(location.s, m); });
        var anchor = merge ? merge.s : location.s;
        var visibleRows = anchor.r - parsed.s.r + 1;
        if (!inside(anchor, parsed) || visibleRows > TABLE_MAX_RENDERED_ROWS) return null;
        return {sheet: sheet, address: normal, ref: utils.encode_cell(anchor), visibleRows: visibleRows,
          isRange: !!location.e, merged: anchor.r !== location.s.r || anchor.c !== location.s.c};
      }

      function auditCellTarget(cell) {
        var link = cell && cell.l && cell.l.Target;
        if (typeof link !== 'string') return null;
        var match = /^#(?:'((?:[^']|'')+)'|([^!'\[\]]+))!([^!]+)$/.exec(link);
        if (!match) return null;
        var sheet = match[1] ? match[1].replace(/''/g, "'") : match[2];
        return workbookLocation(sheet, match[3]);
      }

      function cellsText(ws, row, col) {
        var cell = ws[utils.encode_cell({r: row, c: col})];
        return cell && cell.v != null ? String(cell.v).trim() : '';
      }

      function issueMetadata(ws, range) {
        // Both persisted issue tables declare their exact coordinate column.
        // Never parse a human-readable explanation into an inferred target.
        var field = function (i) { return cellsText(ws, range.s.r, range.s.c + i); };
        if (field(0) !== '序号' || field(1) !== '工作表') return null;
        var index = field(2) === '区域' ? 3 : 2;
        if (field(index) !== '单元格' || field(index + 1) !== '行' || field(index + 2) !== '列') return null;
        return {sheet: range.s.c + 1, address: range.s.c + index};
      }

      function applyPendingFocus(body) {
        if (!pendingFocus) return;
        var location = pendingFocus;
        pendingFocus = null;
        var target = body.querySelector('[data-ref="' + location.ref + '"]');
        var note = document.createElement('div');
        note.className = 'sf-preview-note';
        note.setAttribute('role', 'status');
        note.textContent = target ? '已定位到 ' + location.sheet + '!' + location.address +
          (location.isRange ? '（仅定位到范围起点）' : '') +
          (location.merged ? '（显示合并单元格 ' + location.ref + '）' : '') : '该位置无法在预览中显示，请下载原件。';
        body.appendChild(note);
        if (!target) return;
        target.classList.add('sf-cell-focus');
        target.setAttribute('tabindex', '-1');
        try {
          target.focus({preventScroll: true});
          target.scrollIntoView({block: 'center'});
        } catch (_) { /* older browser */ }
        window.setTimeout(function () { target.classList.remove('sf-cell-focus'); }, 2400);
      }

      function showSheet(name, focusMore) {
        var ws = wb.Sheets[name];
        if (!ws || !ws['!ref']) {
          body.innerHTML = '<div class="sf-preview-note">该工作表没有可显示的数据区域。</div>';
          return;
        }
        state.sheet = name;
        var range = window.XLSX.utils.decode_range(ws['!ref']);
        var totalRows = range.e.r - range.s.r + 1;
        // sheetRows already caps the parse; visibleRows only decides how much
        // of that parsed range is in the DOM right now.
        var visible = Math.min(totalRows, Math.max(1, state.visibleRows || TABLE_PAGE_ROWS),
          TABLE_MAX_RENDERED_ROWS);
        var truncatedByCap = totalRows >= XLSX_PARSE_ROW_CAP;
        // Merged ranges: the anchor cell renders with colspan/rowspan; the cells
        // it covers are skipped so the table keeps its real grid shape.
        var anchors = {};
        var covered = {};
        (ws['!merges'] || []).forEach(function (m) {
          var key = m.s.r + ':' + m.s.c;
          anchors[key] = { rows: m.e.r - m.s.r + 1, cols: m.e.c - m.s.c + 1 };
          for (var r = Math.max(m.s.r, range.s.r); r <= Math.min(m.e.r, range.s.r + visible - 1); r++) {
            for (var c = Math.max(m.s.c, range.s.c); c <= Math.min(m.e.c, range.e.c); c++) {
              if (r === m.s.r && c === m.s.c) continue;
              covered[r + ':' + c] = true;
            }
          }
        });
        var jumps = [];
        var issues = issueMetadata(ws, range);
        var html = '<table class="sf-table sf-table-styled">';
        for (var r = range.s.r; r < range.s.r + visible; r++) {
          html += '<tr>';
          for (var c = range.s.c; c <= range.e.c; c++) {
            if (covered[r + ':' + c]) continue;
            var addr = window.XLSX.utils.encode_cell({ r: r, c: c });
            var cell = ws[addr];
            var text = '';
            if (cell) {
              text = (cell.w != null && cell.w !== '') ? String(cell.w)
                   : (cell.v != null ? String(cell.v) : '');
            }
            var span = anchors[r + ':' + c];
            var attrs = '';
            if (span) {
              if (span.cols > 1) attrs += ' colspan="' + span.cols + '"';
              if (span.rows > 1) attrs += ' rowspan="' + span.rows + '"';
            }
            attrs += ' data-ref="' + addr + '" data-row="' + (r + 1) + '"';
            var style = cellInlineStyle(cell);
            var cls = 'sf-cell';
            if (hasBorder(cell) || fillColor(cell && cell.s)) cls += ' sf-cell-boxed';
            var note = commentOf(cell);
            if (note) {
              cls += ' sf-cell-noted';
              attrs += ' title="' + esc(note) + '"';
            }
            if (style) attrs += ' style="' + style + '"';
            var tag = (r === range.s.r) ? 'th' : 'td';
            var cellText = esc(text);
            var jump = auditCellTarget(cell);
            if (!jump && issues && r > range.s.r && c === issues.address) {
              jump = workbookLocation(cellsText(ws, r, issues.sheet), cellsText(ws, r, issues.address));
            }
            if (jump) {
              var locationLabel = '定位到 ' + jump.sheet + '!' + jump.address;
              cellText = '<button type="button" class="sf-audit-jump" data-jump-index="' +
                         jumps.push(jump) + '" title="' + esc(locationLabel) + '">' +
                         (cellText || esc(locationLabel)) + '</button>';
            }
            html += '<' + tag + ' class="' + cls + '"' + attrs + '>' + cellText +
                    (note ? '<span class="sf-cell-mark" aria-hidden="true">!</span>' : '') +
                    '</' + tag + '>';
          }
          html += '</tr>';
        }
        html += '</table>';
        if (visible < totalRows) {
          html += '<div class="sf-table-footer" aria-live="polite">' +
            '<span class="sf-table-status">浏览器端显示：已渲染 ' + visible + ' / ' +
            totalRows + ' 行' + (truncatedByCap ? '（已达解析上限 ' + XLSX_PARSE_ROW_CAP + ' 行）' : '') +
            '。' + (truncatedByCap ? '超出部分请下载原件查看。' : '') + '</span>' +
            '<button type="button" class="sf-load-more">再显示 ' +
            Math.min(TABLE_PAGE_ROWS, totalRows - visible) + ' 行</button></div>';
        } else if (truncatedByCap) {
          html += '<div class="sf-table-footer" aria-live="polite">' +
            '<span class="sf-table-status">浏览器端最多解析前 ' + XLSX_PARSE_ROW_CAP +
            ' 行；若表格还有更多行，请下载原件查看完整内容。</span></div>';
        } else {
          html += '<div class="sf-table-footer" aria-live="polite">' +
            '<span class="sf-table-status">已渲染全部 ' + totalRows +
            ' 行（浏览器端解析）。</span></div>';
        }
        if (visible >= totalRows && !truncatedByCap &&
            (Object.keys(anchors).length || Object.keys(covered).length)) {
          html += '<div class="sf-preview-note">已按原始合并单元格还原显示。</div>';
        }
        body.innerHTML = html;
        body.querySelectorAll('.sf-audit-jump').forEach(function (button) {
          button.addEventListener('click', function () {
            var target = jumps[Number(button.getAttribute('data-jump-index')) - 1];
            var tabs = node.querySelectorAll('.sf-sheet-tab');
            tabs.forEach(function (tab) {
              tab.classList.toggle('active', tab.getAttribute('data-sheet') === target.sheet);
            });
            pendingFocus = target;
            state.visibleRows = Math.max(TABLE_PAGE_ROWS, target.visibleRows);
            showSheet(target.sheet);
          });
        });
        applyPendingFocus(body);
        var moreBtn = body.querySelector('.sf-load-more');
        if (moreBtn) {
          moreBtn.addEventListener('click', function () {
            state.visibleRows = Math.min(totalRows,
              visible + TABLE_PAGE_ROWS, TABLE_MAX_RENDERED_ROWS);
            showSheet(name, true);
          });
          if (focusMore) {
            try { moreBtn.focus({ preventScroll: true }); } catch (err) { moreBtn.focus(); }
          }
        } else if (focusMore) {
          // The final page removed the button; keep keyboard users in the sheet.
          try {
            body.setAttribute('tabindex', '-1');
            body.focus({ preventScroll: true });
          } catch (err) { /* 旧浏览器没有 preventScroll */ }
        }
      }

      node.querySelectorAll('.sf-sheet-tab').forEach(function (btn) {
        btn.addEventListener('click', function () {
          node.querySelectorAll('.sf-sheet-tab').forEach(function (b) {
            b.classList.remove('active');
          });
          btn.classList.add('active');
          state.visibleRows = TABLE_PAGE_ROWS;
          showSheet(btn.getAttribute('data-sheet'));
        });
      });
      if (sheets.length) showSheet(sheets[0]);
    } catch (e) {
      node.innerHTML = '表格解析失败，请下载原件查看。';
    }
  }

  function openAccountFilePreview(fileOrId) {
    exportViewSerial++;
    var file = (fileOrId && typeof fileOrId === 'object')
      ? fileOrId : fileCache[String(fileOrId || '')];
    if (!file) {
      loadSessionFiles().then(function() {
        var found = fileCache[String(fileOrId || '')];
        if (found) openAccountFilePreview(found);
      });
      return;
    }
    releasePreviewResources();
    var overlay = el('sessionFilePreviewOverlay');
    previewReturnFocus = document.activeElement;
    overlay.hidden = false;
    el('sessionFilePreviewTitle').textContent = file.filename || '文件预览';
    previewFileId = String(file.file_id || '');
    var download = el('sessionFilePreviewDownload');
    var downloadUrl = safePreviewUrl(file.download_url);
    download.hidden = !downloadUrl;
    download.href = downloadUrl || '#';
    download.setAttribute('download', '');
    var copySvgBtn = el('sessionFilePreviewCopySvg');
    if (copySvgBtn) {
      copySvgBtn.hidden = !isSvgFile(file);
      copySvgBtn.setAttribute('data-file-id', previewFileId);
    }
    el('sessionFilePreviewMeta').textContent = previewMetaText(file);
    var stage = el('sessionFilePreviewStage');
    stage.dataset.previewKind = displayKind(file);
    stage.innerHTML = previewStageHtml(file);
    var pnode = stage.querySelector('.sf-pdf-wrap');
    if (pnode) loadPdfPreview(pnode);
    var tnode = stage.querySelector('.sf-preview-text');
    if (tnode) loadTextPreview(tnode);
    var gnode = stage.querySelector('.sf-preview-table');
    if (gnode) loadTablePreview(gnode);
    bindDirectPreview(stage);
    var image = stage.querySelector('.sf-preview-image');
    if (image) enableImageZoom(image, file);
    // 打开后把焦点交给关闭按钮：Tab 顺序从弹窗开始，Esc 随时可退
    var closeBtn = overlay.querySelector('.overlay-close');
    if (closeBtn && typeof closeBtn.focus === 'function') {
      try { closeBtn.focus({ preventScroll: true }); } catch (_) { closeBtn.focus(); }
    }
  }

  function openSentImagePreview(file) {
    if (!(file instanceof File) || !/\.(jpe?g|png|webp|gif)$/i.test(file.name) &&
        !/^image\/(jpeg|png|webp|gif)$/.test(file.type)) return;
    openAccountFilePreview({kind: 'image', filename: file.name});
    var stage = el('sessionFilePreviewStage');
    var url = URL.createObjectURL(file);
    _previewObjectUrls.push(url);
    var wrap = document.createElement('div');
    wrap.className = 'sf-image-wrap';
    var image = document.createElement('img');
    image.className = 'sf-preview-image';
    image.alt = file.name;
    wrap.appendChild(image);
    stage.dataset.previewKind = 'image';
    stage.replaceChildren(wrap);
    enableImageZoom(image);
    image.src = url;
    el('sessionFilePreviewDownload').href = url;
    el('sessionFilePreviewDownload').download = file.name;
    el('sessionFilePreviewDownload').hidden = false;
  }

  function enableImageZoom(image, file) {
    var wrap = image.parentElement;
    var toolbar = document.createElement('div');
    toolbar.className = 'sf-image-toolbar';
    toolbar.setAttribute('role', 'group');
    toolbar.setAttribute('aria-label', '图片缩放');
    var controls = {};
    var scale = null;
    function button(key, text, label, action) {
      var node = document.createElement('button');
      node.type = 'button';
      node.className = 'api-secondary';
      var icon = {out: 'minus', fit: 'fit', in: 'plus', edit: 'edit'}[key];
      if (icon && (key === 'in' || key === 'out')) node.innerHTML = fileIcon(icon);
      else if (icon) node.innerHTML = fileActionContent(icon, text);
      else node.textContent = text;
      node.setAttribute('aria-label', label);
      node.addEventListener('click', action);
      controls[key] = node;
      toolbar.appendChild(node);
      return node;
    }
    function fittedScale() {
      return Math.min(1, (wrap.clientWidth - 32) / image.naturalWidth,
        (wrap.clientHeight - 32) / image.naturalHeight);
    }
    function apply(next) {
      if (!image.naturalWidth || !image.naturalHeight) return;
      scale = next;
      image.style.width = scale === null ? '' : image.naturalWidth * scale + 'px';
      image.style.height = scale === null ? '' : image.naturalHeight * scale + 'px';
      wrap.classList.toggle('is-scaled', scale !== null);
      wrap.scrollLeft = (wrap.scrollWidth - wrap.clientWidth) / 2;
      wrap.scrollTop = (wrap.scrollHeight - wrap.clientHeight) / 2;
      render();
    }
    function change(factor) {
      apply(Math.min(8, Math.max(.05, (scale === null ? fittedScale() : scale) * factor)));
    }
    function render() {
      var ready = !!image.naturalWidth;
      ['out', 'in', 'fit', 'actual'].forEach(function (key) { controls[key].disabled = !ready; });
      controls.out.disabled = !ready || scale !== null && scale <= .05;
      controls.in.disabled = !ready || scale !== null && scale >= 8;
      controls.fit.setAttribute('aria-pressed', String(scale === null));
      controls.actual.setAttribute('aria-pressed', String(scale === 1));
      output.textContent = ready && scale !== null ? Math.round(scale * 100) + '%' : '适应窗口';
    }
    button('out', '−', '缩小图片', function () { change(1 / 1.25); });
    button('fit', '适应', '适应窗口', function () { apply(null); });
    button('actual', '100%', '原始大小 100%', function () { apply(1); });
    button('in', '＋', '放大图片', function () { change(1.25); });
    var output = document.createElement('output');
    output.setAttribute('aria-label', '预览缩放');
    toolbar.appendChild(output);
    if (canImportSvg(fileCache[file && file.file_id])) {
      var edit = button('edit', '尝试创建可编辑副本', '尝试创建可编辑 SVG 副本', function () { createSvgCopy(file.file_id); });
      edit.classList.add('sf-create-copy');
    }
    wrap.before(toolbar);
    image.addEventListener('load', render);
    image.addEventListener('error', render);
    render();
  }

  function queueAccountFileReference(fileId) {
    if (window.composerSupplement) { if (window.showBanner) window.showBanner('请返回新消息后添加资料库文件。'); return; }
    var file = fileCache[String(fileId || '')];
    if (!file) return;
    var refs = window._selectedAccountFileRefs || (window._selectedAccountFileRefs = []);
    if (refs.some(function(item) { return item.file_id === file.file_id; })) return;
    if (refs.length + (window._selectedAttachments || []).length >= referenceLimits.max_turn_files) {
      toast('每轮最多上传或引用 ' + referenceLimits.max_turn_files + ' 个文件。');
      return;
    }
    var refBytes = refs.reduce(function (total, item) { return total + (Number(item.size_bytes) || 0); }, 0);
    if (refBytes + (Number(file.size_bytes) || 0) > referenceLimits.max_reference_bytes) {
      toast('引用资料总大小不能超过 ' + fmtSize(referenceLimits.max_reference_bytes) + '。');
      return;
    }
    refs.push(file);
    if (window.renderAttachmentSelection) window.renderAttachmentSelection();
    if (window.showBanner) window.showBanner('已添加到当前问题。');
  }

  function deleteSessionFile(fileId) {
    if (!confirm('删除这个上传文件？此操作不可撤销。')) return Promise.resolve(false);
    var scope = catalogScope;
    return (window.accountFetch || fetch)(apiBase + '/api/v2/files/' + encodeURIComponent(fileId),
          { method: 'DELETE', credentials: 'same-origin' })
      .then(function (r) {
        if (scope !== catalogScope) return false;
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            if (scope !== catalogScope) return false;
            var detail = body && body.detail;
            toast((detail && detail.message) || (typeof detail === 'string' ? detail : '') || '删除未完成，请重试。');
            return false;
          });
        }
        // Removing a listing is not reversible by a GET begun before deletion.
        delete fileCache[fileId];
        libraryRows = libraryRows.filter(function (file) { return file.file_id !== fileId; });
        renderFilteredLibrary();
        var refs = window._selectedAccountFileRefs || [];
        var selected = refs.findIndex(function (file) { return file.file_id === fileId; });
        if (selected >= 0) {
          refs.splice(selected, 1);
          if (window.renderAttachmentSelection) window.renderAttachmentSelection();
        }
        return loadSessionFiles().then(function () { return scope === catalogScope; });
      })
      .catch(function () {
        if (scope === catalogScope) toast('删除未完成，请重试。');
        return false;
      });
  }

  function initSessionFiles() {
    panelEl = el('sessionFilesPanel');
    if (!panelEl) return;
    var filters = panelEl.querySelector('.library-filters');
    var body = el('sessionFilesBody');
    if (filters && body && filters.parentNode === body && panelEl.insertBefore) panelEl.insertBefore(filters, body);
    bindLibraryResizer();
    bindLibraryUpload();
    bindLibraryReturnRoute();
    el('librarySearch').addEventListener('input', renderFilteredLibrary);
    el('libraryKind').addEventListener('change', renderFilteredLibrary);
    el('librarySort').addEventListener('change', renderFilteredLibrary);
    bindFileActions();
    observeTurnCards();
    // 会话切换时刷新列表
    if (typeof window.addEventListener === 'function') {
      window.addEventListener('citrus:session-changed', function () {
        if (!panelEl.hidden) loadSessionFiles();
      });
      // 生成成果后刷新（发送完成事件）
      window.addEventListener('citrus:turn-finished', function () {
        if (!panelEl.hidden) loadSessionFiles();
      });
    }
    // P1-3: Esc 关闭预览（只在弹窗真的打开时响应，不干扰输入框的 Esc）
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' && e.key !== 'Esc') return;
      var o = el('sessionFilePreviewOverlay');
      if (!o || o.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      closeSessionFilePreview();
    }, true);
  }


  /* ------------------------------------------------------------------ P1-D
     资料库宽度：最小 300px，最大宽度动态计算（不设固定 800 上限）。
       最大宽度 = 视口宽 − 左侧栏（最左快捷栏 + 会话栏）实际右边界 − LIB_GAP(24px)
     左侧栏被拖宽 / 收起、窗口缩放时最大值同步变化；空间不足时优先保证聊天区
     （LIB_MIN_CHAT）。拖动只改 CSS 变量 --library-w，不触碰消息 DOM，
     所以拖动过程不会触发消息列表重渲染。 */
  var LIB_MIN_W = 300;
  var LIB_DEFAULT_W = 468;
  var LIB_GAP = 24;
  var LIB_MIN_CHAT = 320;
  var LIB_W_KEY = 'citrus_library_width';
  var libWidth = LIB_DEFAULT_W;
  var libDragging = false;

  function uiScale() {
    var scale = window.CitrusTheme && window.CitrusTheme.scale;
    return scale ? scale.effective() : 1;
  }

  function viewportWidth() {
    return Math.max(0, window.innerWidth || 0, document.documentElement.clientWidth || 0);
  }

  function isDrawerMode() { return viewportWidth() <= 1024; }

  function sidebarRightEdge() {
    var edge = 0;
    var rail = document.querySelector('.left-rail');
    if (rail) {
      var railRect = rail.getBoundingClientRect();
      if (railRect.width || railRect.height) edge = Math.max(edge, railRect.right);
    }
    var shell = el('mainContainer');
    var sidebar = el('appSidebar');
    var hidden = shell && shell.classList.contains('sidebar-hidden');
    if (sidebar && !hidden) {
      var sideRect = sidebar.getBoundingClientRect();
      if (sideRect.width) edge = Math.max(edge, sideRect.right);
    }
    if (!edge) {
      var main = document.querySelector('.main-container');
      if (main) edge = main.getBoundingClientRect().left;
    }
    return Math.max(0, edge / uiScale());
  }

  function libraryMaxWidth() {
    var vw = viewportWidth() / uiScale();
    if (!vw) return LIB_DEFAULT_W;
    var available = vw - sidebarRightEdge();
    var geometryMax = available - LIB_GAP;         // 贴到左侧栏右边界，保留 24px 安全间隙
    var chatSafeMax = available - LIB_MIN_CHAT;    // 聊天区至少 320px 才可操作
    var max = geometryMax;
    if (chatSafeMax < LIB_MIN_W) max = Math.max(240, chatSafeMax);  // 空间不足：先保聊天区
    max = Math.min(max, Math.round(vw * 0.94));
    return Math.max(240, Math.round(max));
  }

  function clampLibraryWidth(width) {
    var max = libraryMaxWidth();
    var min = Math.min(LIB_MIN_W, max);
    var value = Math.round(Number(width) || 0);
    if (!isFinite(value) || value <= 0) value = LIB_DEFAULT_W;
    return Math.max(min, Math.min(max, value));
  }

  function applyLibraryWidth(width, persist) {
    // libWidth 记的是「用户想要多宽」，实际渲染值再按当前上限夹一次：
    // 刷新瞬间左侧栏/字体还没稳定，上限会短暂偏小，不能让那一次夹紧把宽度永久改小。
    var desired = Math.round(Number(width) || 0);
    if (!isFinite(desired) || desired <= 0) desired = LIB_DEFAULT_W;
    libWidth = desired;
    var value = clampLibraryWidth(desired);
    document.documentElement.style.setProperty('--library-w', value + 'px');
    var panel = el('sessionFilesPanel');
    if (panel) panel.style.setProperty('--library-w', value + 'px');
    // R15: 宽度变了就重算列数（rAF 合并，拖动时每帧最多一次，只写一个变量）
    scheduleLibraryColumns();
    var handle = el('sessionFilesResizer');
    if (handle) {
      handle.setAttribute('aria-valuenow', String(value));
      handle.setAttribute('aria-valuemax', String(libraryMaxWidth()));
    }
    if (persist) {
      try { localStorage.setItem(LIB_W_KEY, String(value)); } catch (err) { /* 忽略 */ }
    }
    return value;
  }

  function restoreLibraryWidth() {
    var raw = null;
    try { raw = localStorage.getItem(LIB_W_KEY); } catch (err) { /* 忽略 */ }
    var saved = raw ? parseInt(raw, 10) : NaN;
    applyLibraryWidth(isFinite(saved) ? saved : LIB_DEFAULT_W, false);
  }

  // 左侧栏被拖宽/收起、窗口缩放后：最大宽度同步，必要时把超出的宽度收回来
  function syncLibraryBounds() {
    if (isDrawerMode()) return;
    applyLibraryWidth(libWidth, false);
  }

  function bindLibraryResizer() {
    var handle = el('sessionFilesResizer');
    if (!handle) return;
    var startX = 0, startW = 0;
    handle.addEventListener('pointerdown', function (event) {
      if (event.button !== 0 || isDrawerMode()) return;
      libDragging = true;
      startX = event.clientX;
      startW = el('sessionFilesPanel').clientWidth || libWidth;
      document.body.classList.add('session-files-resizing');
      try { handle.setPointerCapture(event.pointerId); } catch (err) { /* 忽略 */ }
      event.preventDefault();
    });
    handle.addEventListener('pointermove', function (event) {
      if (!libDragging) return;
      // 面板贴住右侧：指针左移（clientX 变小）→ 变宽
      applyLibraryWidth(startW + (startX - event.clientX) / uiScale(), false);
    });
    function finish(event) {
      if (!libDragging) return;
      libDragging = false;
      document.body.classList.remove('session-files-resizing');
      try { handle.releasePointerCapture(event.pointerId); } catch (err) { /* 忽略 */ }
      applyLibraryWidth(libWidth, true);      // 松开记住宽度
    }
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
    handle.addEventListener('dblclick', function () {
      applyLibraryWidth(LIB_DEFAULT_W, true); // 双击复位
    });
    handle.addEventListener('keydown', function (event) {
      if (event.key === 'Home') {
        event.preventDefault();
        applyLibraryWidth(LIB_DEFAULT_W, true);
        return;
      }
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      applyLibraryWidth(libWidth + (event.key === 'ArrowLeft' ? 16 : -16), true);
    });
    if (window.ResizeObserver) {
      var observer = new ResizeObserver(function () { syncLibraryBounds(); });
      [el('appSidebar'), document.querySelector('.left-rail')].forEach(function (node) {
        if (node) { try { observer.observe(node); } catch (err) { /* 忽略 */ } }
      });
    }
    window.addEventListener('resize', function () {
      syncLibraryBounds();     // 桌面端：夹紧宽度上限
      scheduleLibraryColumns(); // 抽屉模式（≤1024px）下也要重算列数
    });
    // 首屏布局稳定后再同步一次：刷新时先在旧上限下渲染，稳定后应该回到用户宽度
    window.setTimeout(syncLibraryBounds, 0);
    window.setTimeout(syncLibraryBounds, 350);
    window.addEventListener('load', syncLibraryBounds);
    var sidebarToggle = el('sidebarToggleBtn');
    if (sidebarToggle) {
      sidebarToggle.addEventListener('click', function () {
        window.setTimeout(syncLibraryBounds, 0);
        window.setTimeout(syncLibraryBounds, 260);
      });
    }
    restoreLibraryWidth();
  }

  /* ------------------------------------------------------------------ R15
     资料库自适应多列：列数只看「资料库面板自身宽度」，与浏览器窗口宽度
     无关（窗口 1600px 但面板拉得很窄时仍然是一列）。四档阈值与 app.css 末尾
     @container 里的查询完全一致：
       面板 < 440px → 1 列；≥440 → 2 列；≥700 → 3 列；≥980 → 4 列。

     首选纯 CSS 容器查询（app.css 给 .session-files-panel 设了
     container-type: inline-size）；这里写的 --library-cols 只用于兜底不支持
     @container 的浏览器。两条路径读的都是 panel.clientWidth（面板无 padding，
     正好等于 @container 量的内容盒宽度），所以同一宽度下算出的列数必然相同，
     不会互相打架。

     拖动过程中只在跨档时写一次样式（rAF 合并），不重排文件节点、不发请求。 */
  var LIB_COL_TIERS = [[980, 4], [700, 3], [440, 2], [0, 1]];
  var libColsRaf = 0;
  var libColsApplied = 0;

  function libraryColumnsFor(width) {
    var w = Math.round(Number(width) || 0);
    for (var i = 0; i < LIB_COL_TIERS.length; i++) {
      if (w >= LIB_COL_TIERS[i][0]) return LIB_COL_TIERS[i][1];
    }
    return 1;
  }

  function applyLibraryColumns() {
    libColsRaf = 0;
    var panel = el('sessionFilesPanel');
    if (!panel || panel.hidden) return;           // 收起时不量，打开后会重算
    var width = panel.clientWidth || 0;
    if (width <= 0) return;
    var cols = libraryColumnsFor(width);
    if (cols === libColsApplied) return;          // 未跨档：一次样式都不写
    libColsApplied = cols;
    panel.style.setProperty('--library-cols', String(cols));
    panel.setAttribute('data-library-cols', String(cols));   // 验收探针可读
  }

  function scheduleLibraryColumns() {
    if (libColsRaf) return;
    if (typeof window.requestAnimationFrame !== 'function') { applyLibraryColumns(); return; }
    libColsRaf = window.requestAnimationFrame(applyLibraryColumns);
  }

  /* ------------------------------------------------------------------ P1-E
     Agent 生成的 SVG 是科研产物：与 HTML/Excel/PDF 共用同一套资料库登记，
     这里只补四件事——预览（隔离渲染）、下载原件、复制源码、定位来源。
     归属仍用后端已有的 turn_request_id / source_session_id，前端不自己猜。 */
  var SVG_EXT_RE = /\.svg$/i;
  var listLoaded = false;
  var previewFileId = '';
  var lastCopiedSvg = '';

  function isSvgFile(file) {
    if (!file) return false;
    return file.kind === 'svg' || SVG_EXT_RE.test(String(file.filename || '')) ||
      /^image\/svg\+xml(?:\s*;|$)/i.test(String(file.mime_type || ''));
  }

  function displayKind(file) {
    return isSvgFile(file) ? 'svg' : ((file && file.kind) || 'download');
  }

  function fileDownloadUrl(fileId) {
    var cached = fileCache[String(fileId || '')];
    return (cached && cached.download_url) ||
      (apiBase + '/api/v2/files/' + encodeURIComponent(fileId) + '/download');
  }

  function resolveFile(fileId) {
    var id = String(fileId || '');
    if (fileCache[id]) return Promise.resolve(fileCache[id]);
    return loadSessionFiles().then(function () { return fileCache[id] || null; });
  }

  function legacyCopy(value) {
    var area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    area.style.left = '-1000px';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    document.body.removeChild(area);
    if (!ok) throw new Error('clipboard-blocked');
    return true;
  }

  function copyText(value) {
    var text = String(value || '');
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
      return navigator.clipboard.writeText(text)
        .catch(function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  function toast(message) {
    if (typeof window.showBanner === 'function') { window.showBanner(message); return; }
    console.info('[资料库]', message);
  }

  function fetchSvgSource(fileId) {
    return fetch(fileDownloadUrl(fileId), { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) {
          var err = new Error('HTTP ' + r.status);
          err.status = r.status;
          throw err;
        }
        return r.text();
      })
      .then(function (text) {
        var source = String(text || '');
        if (!/<svg[\s>]/i.test(source)) throw new Error('not-svg');
        return source;
      });
  }

  // 复制的是**源码文本**（从带鉴权的下载接口取回后原样写入剪贴板），
  // 不是文件名；取不到 / 不是 SVG 一律不写剪贴板。
  function copySvgSource(fileId) {
    return resolveFile(fileId)
      .then(function () { return fetchSvgSource(fileId); })
      .then(function (source) { return copyText(source).then(function () { return source; }); })
      .then(function (source) {
        lastCopiedSvg = source;
        window.lastCopiedSvgSource = source;   // 验收探针据此断言"复制到的是源码"
        toast('已复制 SVG 源码（' + source.length + ' 字符）。');
        return source;
      })
      .catch(function (err) {
        var reason = String((err && err.message) || err);
        if (reason === 'clipboard-blocked') toast('浏览器拒绝了剪贴板写入，请允许后重试。');
        else if (reason === 'not-svg') toast('这个文件的内容不是 SVG 源码，未复制。');
        else if (err && err.status === 401) toast('登录后才能复制文件内容。');
        else toast('复制 SVG 源码失败，请重试。');
        throw err;
      });
  }

  function fileInfoHtml(file) {
    var rows = [
      ['文件名', file.filename || '—'],
      ['类型', (KIND_LABEL[displayKind(file)] || '文件') +
               (file.mime_type ? '（' + file.mime_type + '）' : '')],
      ['大小', fmtSize(file.size_bytes)],
      ['创建时间', fmtTime(file.created_at) || '—'],
      ['最近访问', fmtTime(file.last_accessed_at) || '—'],
      ['保留策略', retentionIdleDays
        ? '连续 ' + retentionIdleDays + ' 天未使用时自动清理；打开、预览、下载或引用会重新计时'
        : '长期未使用时自动清理；打开、预览、下载或引用会重新计时'],
      ['来源', file.origin === 'generated' ? 'Agent 生成（科研产物）' : '用户上传'],
      ['生成工具', file.producer_tool || '—'],
      ['来源会话', file.source_session_id || '—'],
      ['来源轮次', file.turn_request_id || '—'],
      ['校验值', file.sha256 ? (String(file.sha256).slice(0, 16) + '…') : '—']
    ];
    var html = '<dl class="sf-fileinfo">';
    rows.forEach(function (row) {
      html += '<dt>' + esc(row[0]) + '</dt><dd>' + esc(row[1]) + '</dd>';
    });
    html += '</dl>';
    var actions = '<div class="sf-fileinfo-actions">' +
      '<a class="api-secondary" href="' + esc(file.download_url || '#') + '" download>下载原件</a>';
    if (isSvgFile(file)) {
      actions += '<button type="button" class="api-secondary" data-file-act="copy-svg" data-file-id="' +
                 esc(file.file_id) + '">复制 SVG 源码</button>';
    }
    if (file.turn_request_id && file.source_session_id) {
      actions += '<button type="button" class="api-secondary" data-file-act="jump" data-file-id="' +
                 esc(file.file_id) + '">定位到来源会话</button>';
    }
    actions += '</div>';
    return html + actions;
  }

  function showFileInfo(fileId) {
    exportViewSerial++;
    return resolveFile(fileId).then(function (file) {
      if (!file) { toast('文件信息暂不可用，请刷新资料库后重试。'); return; }
      releasePreviewResources();
      var overlay = el('sessionFilePreviewOverlay');
      if (!overlay) return;
      previewReturnFocus = document.activeElement;
      overlay.hidden = false;
      previewFileId = String(file.file_id || '');
      el('sessionFilePreviewTitle').textContent = '文件信息 · ' + (file.filename || '');
      var download = el('sessionFilePreviewDownload');
      if (download) {
        var url = safePreviewUrl(file.download_url);
        download.href = url || '#';
        download.hidden = !url;
      }
      var copySvgBtn = el('sessionFilePreviewCopySvg');
      if (copySvgBtn) {
        copySvgBtn.hidden = !isSvgFile(file);
        copySvgBtn.setAttribute('data-file-id', previewFileId);
      }
      el('sessionFilePreviewMeta').textContent = previewMetaText(file);
      var stage = el('sessionFilePreviewStage');
      delete stage.dataset.previewKind;
      stage.innerHTML = fileInfoHtml(file);
      var closeBtn = overlay.querySelector('.overlay-close');
      if (closeBtn && typeof closeBtn.focus === 'function') {
        try { closeBtn.focus({ preventScroll: true }); } catch (err) { closeBtn.focus(); }
      }
    });
  }

  // 面板有 transform 动画（会成为 fixed 的包含块）、面板与文件项又都是
  // overflow:hidden，菜单挂进 item 里会被裁切/错位。打开时临时挂到 body，
  // 关闭时放回原位，保证下次渲染和 DOM 结构一致。
  function closeMoreMenus(except, returnFocus) {
    document.querySelectorAll('.session-file-more').forEach(function (menu) {
      if (except && menu === except) return;
      var opener = menu.__opener;
      menu.hidden = true;
      menu.__opener = null;
      if (menu.__homeParent && menu.parentElement === document.body) {
        menu.__homeParent.appendChild(menu);
      }
      menu.__homeParent = null;
      var host = menu.parentElement;
      var trigger = host && host.querySelector('[data-file-act="more"]');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      if (returnFocus && opener && document.contains(opener) &&
          typeof opener.focus === 'function') {
        try { opener.focus({ preventScroll: true }); } catch (err) { opener.focus(); }
      }
    });
  }

  // 资料库面板与文件项都是 overflow:hidden（为了圆角和内部滚动），下拉菜单直接
  // 绝对定位会被裁掉。这里改成 position:fixed 按按钮位置摆放，并夹在视口内。
  function placeMoreMenu(button, menu) {
    var rect = button.getBoundingClientRect();
    if (menu.parentElement !== document.body) {
      menu.__homeParent = menu.parentElement;
      document.body.appendChild(menu);
    }
    menu.style.position = 'fixed';
    menu.style.zIndex = '90';
    menu.style.right = 'auto';
    menu.style.bottom = 'auto';
    var width = menu.offsetWidth || 152;
    var height = menu.offsetHeight || 150;
    var scale = uiScale();
    var left = Math.max(8, Math.min(rect.right / scale - width, window.innerWidth / scale - width - 8));
    var top = rect.bottom / scale + 4;
    if (top + height > window.innerHeight / scale - 8) top = Math.max(8, rect.top / scale - height - 4);
    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(top) + 'px';
  }

  function toggleMoreMenu(button) {
    var item = button.closest('.session-file-item');
    var menu = item && item.querySelector('.session-file-more');
    if (!menu) return;
    var willOpen = menu.hidden;
    closeMoreMenus(menu);
    menu.hidden = !willOpen;
    menu.__opener = willOpen ? button : null;
    button.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    if (willOpen) {
      placeMoreMenu(button, menu);
      var first = menu.querySelector('button');
      if (first) { try { first.focus({ preventScroll: true }); } catch (err) { /* 忽略 */ } }
    }
  }

  function triggerDownload(fileId) {
    var link = document.createElement('a');
    link.href = fileDownloadUrl(fileId);
    link.setAttribute('download', '');
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  function exportJson(response) {
    return response.json().then(function (body) {
      if (!response.ok) {
        var detail = body && body.detail;
        throw new Error((detail && detail.message) || (typeof detail === 'string' ? detail : '导出暂时不可用，请重试。'));
      }
      return body;
    });
  }

  // The same entry serves library rows and chat cards. Only the server's saved
  // formats are shown; there is no model call, guessed converter or script run.
  function openAccountFileExport(fileId) {
    var id = String(fileId || '');
    if (!/^[a-f0-9]{32}$/.test(id)) return Promise.resolve();
    var overlay = el('sessionFilePreviewOverlay');
    var stage = el('sessionFilePreviewStage');
    if (!overlay || !stage) return Promise.resolve();
    delete stage.dataset.previewKind;
    var serial = ++exportViewSerial;
    releasePreviewResources();
    previewReturnFocus = document.activeElement;
    previewFileId = id;
    overlay.hidden = false;
    el('sessionFilePreviewTitle').textContent = '导出已有成果';
    el('sessionFilePreviewMeta').textContent = '资料库文件';
    var download = el('sessionFilePreviewDownload');
    download.href = fileDownloadUrl(id);
    download.setAttribute('download', '');
    download.hidden = false;
    var copy = el('sessionFilePreviewCopySvg');
    if (copy) copy.hidden = true;
    stage.textContent = '正在核对已生成格式…';
    var close = overlay.querySelector('.overlay-close');
    if (close) close.focus();
    function active() { return serial === exportViewSerial && !overlay.hidden; }
    return fetch(apiBase + '/api/v2/files/' + encodeURIComponent(id) + '/export-options',
      { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(exportJson)
      .then(function (payload) {
        if (!active()) return;
        if (payload.file_id !== id || !/^[a-f0-9]{64}$/.test(payload.source_sha256 || '')) {
          throw new Error('无法确认成果版本，请刷新资料库。');
        }
        stage.textContent = '';
        el('sessionFilePreviewTitle').textContent = '导出 · ' + (payload.filename || '已有成果');
        var note = document.createElement('p');
        note.className = 'sf-preview-note';
        note.textContent = payload.note || '仅提供已经生成并入库的格式。';
        stage.appendChild(note);
        var options = (payload.options || []).filter(function (option) {
          return option.mode === 'existing' && /^[a-f0-9]{32}$/.test(option.file_id || '') && option.target_format;
        });
        if (!options.length) throw new Error('没有可导出的已生成格式。');
        var formats = document.createElement('div');
        formats.className = 'sf-export-formats';
        formats.setAttribute('role', 'group');
        formats.setAttribute('aria-label', '已生成格式');
        options.forEach(function (option) {
          var button = document.createElement('button');
          button.type = 'button';
          button.className = 'api-secondary';
          button.innerHTML = fileActionContent('download', String(option.target_format).toUpperCase() +
            (option.file_id === id ? '（原件）' : '（已生成）'));
          button.title = option.filename || '';
          button.onclick = function () {
            if (button.disabled) return;
            button.disabled = true;
            return fetch(apiBase + '/api/v2/files/' + encodeURIComponent(id) + '/export', {
              method: 'POST', credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
              body: JSON.stringify({ target_format: option.target_format, source_sha256: payload.source_sha256,
                target_file_id: option.file_id, target_sha256: option.sha256 || '' })
            }).then(exportJson).then(function (result) {
              if (!active()) return;
              if (result.status !== 'complete' || result.file_id !== option.file_id ||
                  result.source_sha256 !== payload.source_sha256) throw new Error('成果版本已改变，请重新打开导出。');
              triggerDownload(result.file_id);
            }).catch(function (error) {
              if (active()) toast(error.message || '导出失败，请重试。');
            }).finally(function () { button.disabled = false; });
          };
          formats.appendChild(button);
        });
        stage.appendChild(formats);
      }).catch(function (error) {
        if (active()) stage.textContent = error.message || '导出暂时不可用，请重试。';
      });
  }

  function canImportSvg(file) {
    return !!file && file.visibility !== 'internal' && registeredSvgUrl(file) &&
      (file.origin === 'uploaded' || file.origin === 'generated' && file.producer_tool === 'code_deliver' && SHA256.test(file.sha256));
  }

  function currentAccountKey() {
    var account = window.currentAccount;
    var id = account && account.user_id;
    return Number.isSafeInteger(id) && id > 0 ? String(id) : '';
  }

  function sourceMatches(file, handoff) {
    return !!file && file.visibility !== 'internal' && file.file_id === handoff.file && file.sha256 === handoff.sha &&
      typeof file.source_session_id === 'string' && file.source_session_id && file.source_session_id === handoff.session &&
      typeof file.turn_request_id === 'string' && file.turn_request_id && file.turn_request_id === handoff.turn;
  }

  function bindLibraryReturnRoute() {
    if (window.location?.pathname !== '/' || new URLSearchParams(window.location.search).get('files') !== '1') return;
    var gate = el('authGate');
    var observer;
    function enter() {
      if (!currentAccountKey() || gate && !gate.hidden) return;
      if (observer) observer.disconnect();
      var url = new URL(window.location.href);
      var source = url.searchParams.get('source') || '';
      url.searchParams.delete('files');
      url.searchParams.delete('source');
      window.history.replaceState(window.history.state, '', url);
      var request = toggleSessionFiles(true);
      if (!FILE_ID.test(source)) return;
      var handoff;
      try { handoff = JSON.parse(window.sessionStorage.getItem(FIGURE_SOURCE_KEY) || 'null'); }
      catch (_) { return; }
      if (handoff?.file !== source || handoff.account !== currentAccountKey()) return;
      var scope = catalogScope;
      Promise.resolve(request).then(function (payload) {
        if (scope !== catalogScope || handoff.account !== currentAccountKey() || !payload ||
            !sourceMatches(fileCache[source], handoff) || typeof window.jumpToFileTurn !== 'function') return;
        return Promise.resolve(window.jumpToFileTurn(handoff.session, handoff.turn)).then(function (found) {
          if (found && scope === catalogScope && handoff.account === currentAccountKey()) toggleSessionFiles(false);
          else if (!found && scope === catalogScope) toast('来源轮次不在当前可加载的历史里。');
        });
      }).catch(function () { if (scope === catalogScope) toast('定位到来源会话失败。'); });
    }
    if (gate && window.MutationObserver) {
      observer = new MutationObserver(enter);
      observer.observe(gate, {attributes: true, attributeFilter: ['hidden']});
    }
    enter();
  }

  var svgCopyPending = new Set();
  function createSvgCopy(fileId) {
    var file = fileCache[fileId];
    if (!canImportSvg(file) || svgCopyPending.has(fileId)) return;
    var scope = catalogScope;
    var account = currentAccountKey();
    svgCopyPending.add(fileId);
    toast('正在检查 SVG 是否可以安全创建样式副本…');
    var input = { file_id: fileId };
    if (SHA256.test(file.sha256)) input.expected_sha256 = file.sha256;
    return (window.accountFetch || fetch)(apiBase + '/api/v2/figure-workbench/import-svg', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(input)
    }).then(exportJson).then(function (result) {
      if (scope !== catalogScope || account !== currentAccountKey()) return;
      var original = Object.values(result.original_exports || {}).find(function (item) {
        return item && item.file_id === fileId && SHA256.test(item.sha256) && (!input.expected_sha256 || item.sha256 === input.expected_sha256);
      });
      if (!original || result.editable !== true || result.publication_state !== 'ready' ||
          result.origin !== (file.origin === 'generated' ? 'code_output' : 'uploaded') ||
          !FILE_ID.test(result.figure_id) || result.workbench_url !== '/figure-workbench?project=' + result.figure_id) {
        toast(result.edit_limitation || '此文件不可创建可编辑副本，仍可预览或下载原件。');
        return;
      }
      var handoff = { project: result.figure_id, file: fileId, sha: original.sha256, account: account,
        session: file.source_session_id, turn: file.turn_request_id };
      if (account && sourceMatches(file, handoff)) {
        try { window.sessionStorage.setItem(FIGURE_SOURCE_KEY, JSON.stringify(handoff)); } catch (_) {}
      }
      window.location.assign(result.workbench_url);
    }).catch(function (error) {
      if (scope === catalogScope) toast(error.message || '无法创建 SVG 样式副本，请重试。');
    }).finally(function () { svgCopyPending.delete(fileId); });
  }

  function onFileActionClick(event) {
    var target = event.target;
    if (!target || typeof target.closest !== 'function') return;
    var button = target.closest('[data-file-act]');
    if (!button) { closeMoreMenus(null); return; }
    var action = button.getAttribute('data-file-act');
    var fileId = String(button.getAttribute('data-file-id') || '');
    if (action === 'more') {
      event.preventDefault();
      toggleMoreMenu(button);
      return;
    }
    if (action === 'info') {
      event.preventDefault();
      showFileInfo(fileId);
      closeMoreMenus(null);
      return;
    }
    if (action === 'copy-svg') {
      event.preventDefault();
      copySvgSource(fileId);
      closeMoreMenus(null);
      return;
    }
    if (action === 'edit-svg-copy') {
      event.preventDefault();
      closeMoreMenus(null);
      createSvgCopy(fileId);
      return;
    }
    if (action === 'export') {
      event.preventDefault();
      closeMoreMenus(null);
      openAccountFileExport(fileId);
      return;
    }
    if (action === 'download') {
      event.preventDefault();
      triggerDownload(fileId);
      closeMoreMenus(null);
      return;
    }
    if (action === 'preview') {
      event.preventDefault();
      openAccountFilePreview(fileId);
      closeMoreMenus(null);
      return;
    }
    if (action === 'reference') {
      event.preventDefault();
      queueAccountFileReference(fileId);
      closeMoreMenus(null);
      return;
    }
    if (action === 'delete') {
      event.preventDefault();
      closeMoreMenus(null);
      deleteSessionFile(fileId);
      return;
    }
    if (action === 'jump') {
      event.preventDefault();
      resolveFile(fileId).then(function (file) {
        if (!file || !file.turn_request_id || !file.source_session_id) {
          toast('这个文件没有关联的会话轮次。');
          return;
        }
        var jumpFn = window.jumpToFileTurn || window.jumpFromFile;
        if (typeof jumpFn !== 'function') {
          toast('当前页面不支持定位到来源会话。');
          return;
        }
        // app.js 的 jumpToFileTurn 只在真的找到并高亮了那一轮时返回 true，不做“假跳转”。
        Promise.resolve(jumpFn(file.source_session_id, file.turn_request_id)).then(function (ok) {
          if (ok === false) toast('来源轮次不在当前可加载的历史里。');
        }, function () {
          toast('定位到来源会话失败。');
        });
      });
      closeMoreMenus(null);
    }
  }

  function bindFileActions() {
    document.addEventListener('click', onFileActionClick, false);
    document.addEventListener('keydown', function (event) {
      var target = event.target;
      var menu = (target && typeof target.closest === 'function')
        ? target.closest('.session-file-more') : null;
      if (menu && !menu.hidden) {
        var items = Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]'));
        var current = items.indexOf(document.activeElement);
        if (event.key === 'Escape') {
          event.preventDefault();
          closeMoreMenus(null, true);
          return;
        }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp' ||
            event.key === 'Home' || event.key === 'End') {
          if (!items.length) return;
          event.preventDefault();
          var next = 0;
          if (event.key === 'End') next = items.length - 1;
          else if (event.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % items.length;
          else if (event.key === 'ArrowUp') next = current <= 0 ? items.length - 1 : current - 1;
          try { items[next].focus({ preventScroll: true }); } catch (err) { items[next].focus(); }
          return;
        }
        if (event.key === 'Tab') { closeMoreMenus(null); return; }
      }
      if (event.key === 'Escape') {
        closeMoreMenus(null);
        var panel = el('sessionFilesPanel');
        if (!event.defaultPrevented && panel && !panel.hidden && panel.contains(target)) {
          event.preventDefault();
          toggleSessionFiles(false);
        }
      }
    });
    // 菜单是 position:fixed，面板滚动/窗口尺寸变化后位置会失效，直接关掉。
    var bodyEl = el('sessionFilesBody');
    if (bodyEl) bodyEl.addEventListener('scroll', function () { closeMoreMenus(null); }, { passive: true });
    window.addEventListener('resize', function () { closeMoreMenus(null); });
    window.addEventListener('citrus:scalechange', function () { closeMoreMenus(null); syncLibraryBounds(); });
    var copySvgBtn = el('sessionFilePreviewCopySvg');
    if (copySvgBtn) {
      copySvgBtn.addEventListener('click', function () {
        var id = String(copySvgBtn.getAttribute('data-file-id') || previewFileId || '');
        if (id) copySvgSource(id);
      });
    }
  }

  // 复用 app.js 的生成文件卡片及其原有链接，与资料库统一主要控件布局。
  // 保留已登记文件的复制源码、定位和信息等次要操作。
  function turnCardAction(action, fileId, icon, label) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'turn-file-act';
    button.setAttribute('data-file-act', action);
    button.setAttribute('data-file-id', fileId);
    button.title = label;
    button.setAttribute('aria-label', label);
    button.innerHTML = fileIcon(icon);
    return button;
  }

  function syncTurnSourceActions(box, file, fileId) {
    var jump = box.querySelector('[data-file-act="jump"]');
    if (file?.turn_request_id && file.source_session_id) {
      if (!jump) box.appendChild(turnCardAction('jump', fileId, 'external', '定位到生成它的会话'));
    } else if (jump) jump.remove();
    var edit = box.querySelector('[data-file-act="edit-svg-copy"]');
    if (canImportSvg(file)) {
      if (!edit) box.appendChild(turnCardAction('edit-svg-copy', fileId, 'edit', '尝试创建可编辑 SVG 副本'));
    } else if (edit) edit.remove();
  }

  function arrangeTurnPrimaryActions(card, fileId, filename) {
    if (!FILE_ID.test(fileId)) return;
    var preview = card.querySelector('button.turn-file-open');
    if (!preview) return;
    var original = card.querySelector('a.turn-file-open');
    var row = document.createElement('div');
    row.className = 'file-primary-actions';
    ['preview', 'export', 'download'].forEach(function (action) {
      // Keep existing preview listeners and SVG original links. Other files
      // use the same registered download action as library cards.
      var button = action === 'preview' ? preview : action === 'download' ? original : null;
      if (!button) {
        button = document.createElement('button');
        button.type = 'button';
        button.setAttribute('data-file-act', action);
        button.setAttribute('data-file-id', fileId);
      }
      var spec = PRIMARY_FILE_ACTIONS[action];
      button.classList.add('file-primary-action');
      button.title = spec[2] + ' ' + filename;
      button.setAttribute('aria-label', button.title);
      if (action !== 'download') button.setAttribute('aria-haspopup', 'dialog');
      button.innerHTML = fileActionContent(spec[0], spec[1]);
      row.appendChild(button);
    });
    card.appendChild(row);
  }

  function decorateTurnCards(root) {
    var scope = (root && typeof root.querySelectorAll === 'function') ? root : document;
    var cards = scope.querySelectorAll('.turn-file-card[data-file-id]');
    Array.prototype.forEach.call(cards, function (card) {
      var fileId = String(card.dataset.fileId || '');
      if (!fileId) return;
      var nameEl = card.querySelector('.turn-file-name');
      var name = nameEl ? String(nameEl.textContent || '') : '';
      var cached = fileCache[fileId];
      var existing = card.querySelector('.turn-file-extra');
      if (existing) { syncTurnSourceActions(existing, cached, fileId); return; }
      var svg = SVG_EXT_RE.test(name) || isSvgFile(cached);
      // Normalize the badge for attachment cards restored from before `kind: svg`.
      if (svg) {
        var typeEl = card.querySelector('.turn-file-type');
        if (typeEl) {
          typeEl.textContent = 'SVG';
          typeEl.setAttribute('title', 'SVG 矢量图');
        }
      }
      var box = document.createElement('div');
      box.className = 'turn-file-extra';
      box.dataset.fileId = fileId;
      if (svg) box.appendChild(turnCardAction('copy-svg', fileId, 'copy', '复制 SVG 源码'));
      arrangeTurnPrimaryActions(card, fileId, name || '生成文件');
      syncTurnSourceActions(box, cached, fileId);
      if (svg) box.appendChild(turnCardAction('info', fileId, 'info', '文件信息'));
      card.appendChild(box);
    });
    if (cards.length && !listLoaded) {
      // 元数据到位后才知道 turn_request_id / source_session_id，据此补"定位"
      loadSessionFiles().then(function () { decorateTurnCards(); });
    }
  }

  function observeTurnCards() {
    var chat = el('chat-container');
    if (!chat || !window.MutationObserver) { decorateTurnCards(); return; }
    var timer = 0;
    var observer = new MutationObserver(function () {
      if (timer) return;
      timer = window.setTimeout(function () {
        timer = 0;
        decorateTurnCards(chat);
      }, 60);
    });
    observer.observe(chat, { childList: true, subtree: true });
    decorateTurnCards(chat);
  }

  window.loadSessionFiles = loadSessionFiles;
  window.resetSessionFiles = resetSessionFiles;
  window.toggleSessionFiles = toggleSessionFiles;
  window.registeredSvgUrl = registeredSvgUrl;
  window.openSessionFilePreview = openAccountFilePreview;
  window.openAccountFilePreview = openAccountFilePreview;
  window.openSentImagePreview = openSentImagePreview;
  window.openAccountFileExport = openAccountFileExport;
  window.queueAccountFileReference = queueAccountFileReference;
  window.closeSessionFilePreview = closeSessionFilePreview;
  window.deleteSessionFile = deleteSessionFile;
  // P1-E：这两个入口也对外暴露，方便探针/其它面板复用同一套操作
  window.copySvgSource = copySvgSource;
  window.showSessionFileInfo = showFileInfo;
  window.decorateTurnCards = decorateTurnCards;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSessionFiles);
  } else {
    initSessionFiles();
  }
})();
