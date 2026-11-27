/* Owned citation bubbles open verified full Markdown or a clearly labeled fragment.
 * Older exact-reference and candidate readers retain their own read paths. */
(function () {
  'use strict';

  var MASK_ID = 'corpusReaderMask';
  var state = { key: '', data: null, view: 'rendered', request: 0 };
  var PAGE_CHARS = 12000;
  var CITATION_PAGE_CHARS = 60000;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function humanNum(n) {
    var v = Number(n) || 0;
    return v.toLocaleString('en-US');
  }

  /* 把纯文本里的 DOI 变成可点链接；已经是链接的段落不重复处理 */
  function linkifyDoi(text) {
    return String(text || '').replace(
      /(^|[\s（(【，,;；:：])(10\.\d{4,9}\/[^\s<>"'）)】\]]+)/g,
      function (m, lead, doi) {
        var clean = doi.replace(/[.,;]+$/, '');
        return lead + '<a class="cr-doi" href="https://doi.org/' + esc(clean) +
          '" target="_blank" rel="noopener noreferrer">' + esc(clean) + '</a>';
      }
    );
  }

  function readerIcon(name) {
    return window.AppIcon ? window.AppIcon.html(name, {size: 16}) : '';
  }

  function readerLabel(id) {
    var button = document.getElementById(id);
    return button.querySelector('[data-reader-label]') || button;
  }

  function ensureMask() {
    var mask = document.getElementById(MASK_ID);
    if (mask) return mask;
    mask = document.createElement('div');
    mask.id = MASK_ID;
    mask.className = 'corpus-reader-mask';
    mask.setAttribute('hidden', '');
    mask.innerHTML =
      '<div class="corpus-reader" role="dialog" aria-modal="true" aria-label="文献阅读">' +
        '<header class="cr-heading" id="crHeading" hidden></header>' +
        '<div class="cr-body" id="crBody" tabindex="0"></div>' +
        '<footer class="cr-foot" id="crFoot">' +
          '<div class="cr-actions">' +
            '<button type="button" class="cr-btn" id="crPreviousBtn" hidden>' + readerIcon('chevron-left') + '<span>上一页</span></button>' +
            '<button type="button" class="cr-btn" id="crNextBtn" hidden><span>下一页</span>' + readerIcon('chevron-right') + '</button>' +
            '<button type="button" class="cr-btn" id="crViewBtn" title="切换渲染/原始 Markdown">' + readerIcon('code') + '<span data-reader-label>原始 Markdown</span></button>' +
            '<button type="button" class="cr-btn" id="crCopyBtn" title="复制原文">' + readerIcon('copy') + '<span data-reader-label>复制原文</span></button>' +
            '<button type="button" class="cr-close" id="crCloseBtn" aria-label="关闭">' + readerIcon('close') + '</button>' +
          '</div>' +
          '<div class="cr-foot-note" id="crFootNote"></div>' +
        '</footer>' +
      '</div>';
    document.body.appendChild(mask);

    mask.addEventListener('click', function (ev) {
      if (ev.target === mask) closeReader();
    });
    mask.querySelector('#crCloseBtn').addEventListener('click', closeReader);
    mask.querySelector('#crViewBtn').addEventListener('click', function () {
      state.view = state.view === 'rendered' ? 'raw' : 'rendered';
      paint();
    });
    mask.querySelector('#crCopyBtn').addEventListener('click', copySource);
    mask.querySelector('#crPreviousBtn').addEventListener('click', function () {
      if (state.citation && state.data && state.data.previous_start != null) loadCitationPage(state.data.previous_start);
      else if (state.shared && state.data) loadSharedPage(state.shared, Math.max(0, state.data.start - PAGE_CHARS));
    });
    mask.querySelector('#crNextBtn').addEventListener('click', function () {
      if (!state.data || state.data.next_start == null) return;
      if (state.citation) loadCitationPage(state.data.next_start);
      else if (state.shared) loadSharedPage(state.shared, state.data.next_start);
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !mask.hasAttribute('hidden')) closeReader();
    });
    return mask;
  }

  function setStatus(text, kind) {
    var body = document.getElementById('crBody');
    if (!body) return;
    body.innerHTML = '<div class="cr-note cr-note-' + (kind || 'info') + '">' + esc(text) + '</div>';
  }

  function closeReader() {
    state.request += 1;
    state.shared = null;
    state.citation = null;
    state.data = null;
    var mask = document.getElementById(MASK_ID);
    if (mask) mask.setAttribute('hidden', '');
    document.body.classList.remove('corpus-reader-open');
  }

  function isOpen() {
    var mask = document.getElementById(MASK_ID);
    return !!mask && !mask.hasAttribute('hidden');
  }

  function paint() {
    var d = state.data;
    if (!d) return;
    var body = document.getElementById('crBody');
    if (state.citation) { paintCitation(d, body); return; }
    var viewBtn = readerLabel('crViewBtn');

    if (state.view === 'raw') {
      body.innerHTML = '<pre class="cr-raw">' + esc(d.markdown || '') + '</pre>';
      viewBtn.textContent = '渲染视图';
    } else {
      var html = '';
      try {
        html = (typeof window.renderMarkdownSafe === 'function')
          ? window.renderMarkdownSafe(d.markdown || '')
          : '<pre class="cr-raw">' + esc(d.markdown || '') + '</pre>';
      } catch (e) {
        html = '<pre class="cr-raw">' + esc(d.markdown || '') + '</pre>';
      }
      body.innerHTML = linkifyDoi(html);
      viewBtn.textContent = '原始 Markdown';
    }
    body.scrollTop = 0;
    if (state.view !== 'raw' && state.focus) markFocus(state.focus);
    var footNote = document.getElementById('crFootNote');
    if (footNote) {
      footNote.textContent = d.kind === 'stored_markdown'
        ? (d.exact_span ? '已保存 Markdown 引用片段 · ' : '已保存 Markdown 全文 · ') + (d.title || '共享文献') + ' · 字符 ' + (d.start + 1) + '–' + d.end + ' / ' + d.total_chars
        : (d.kind === 'stored_evidence' ? '已保存的引用片段 · ' : '基于分块重建 · ') + (d.corpus || '');
      document.getElementById('crPreviousBtn').hidden = !(state.shared && !d.exact_span && d.start > 0);
      document.getElementById('crNextBtn').hidden = !(state.shared && d.next_start != null);
    }
  }

  function citationRange(data) {
    var text = data.markdown;
    var start = data.hit_start - data.start, end = data.hit_end - data.start;
    if (data.hit_start >= data.end || data.hit_end <= data.start) return null;
    // Server offsets are Python Unicode code points, not JavaScript UTF-16 offsets.
    var points = 0, units = 0, from = -1;
    for (var character of text) {
      if (points === start) from = units;
      if (points === end) break;
      points++;
      units += character.length;
    }
    return from >= 0 && points === end ? [from, units] : null;
  }

  function renderCitationFragment(text) {
    if (!text) return '';
    try {
      var parser = window.marked;
      var renderer = new parser.Renderer();
      renderer.html = esc;
      renderer.image = typeof window.renderMarkdownImage === 'function'
        ? window.renderMarkdownImage : function (_, title, label) { return esc(label || title || '图片'); };
      renderer.link = function (href, _, label) {
        var url = /^https?:\/\//i.test(String(href || '')) ? safeWebUrl(href) : '';
        return url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>' : label;
      };
      return parser.parse(text, { renderer: renderer, mangle: false, headerIds: false });
    } catch (_) {
      return '<pre class="cr-raw">' + esc(text) + '</pre>';
    }
  }

  function paintCitation(data, body) {
    var text = data.markdown;
    var range = citationRange(data);
    var raw = state.view === 'raw';
    var heading = document.getElementById('crHeading');
    heading.textContent = data.title || '文献阅读';
    heading.hidden = false;
    readerLabel('crViewBtn').textContent = raw ? '渲染视图' : '原始 Markdown';
    if (raw) {
      body.innerHTML = '<pre class="cr-raw">' + (range
        ? esc(text.slice(0, range[0])) + '<mark class="cr-hit cr-citation-focus">' +
          esc(text.slice(range[0], range[1])) + '</mark>' + esc(text.slice(range[1]))
        : esc(text)) + '</pre>';
    } else {
      body.innerHTML = range
        ? renderCitationFragment(text.slice(0, range[0])) +
          '<div class="cr-hit-block cr-citation-focus" aria-label="命中分块">' +
          renderCitationFragment(text.slice(range[0], range[1])) + '</div>' +
          renderCitationFragment(text.slice(range[1]))
        : renderCitationFragment(text);
    }
    var label = data.kind === 'stored_markdown' ? '已保存的 Markdown 全文'
      : data.kind === 'reconstructed_markdown' ? '由完整索引分块链重建，非原始文件'
      : data.coverage === 'chunk' ? '仅保存的命中分块，全文不可用' : '仅保存的引用片段，全文不可用';
    document.getElementById('crFootNote').textContent = label + ' · 字符 ' +
      humanNum(data.start + 1) + '–' + humanNum(data.end) + ' / ' + humanNum(data.total_chars);
    document.getElementById('crPreviousBtn').hidden = data.previous_start == null;
    document.getElementById('crNextBtn').hidden = data.next_start == null;
    readerLabel('crCopyBtn').textContent = data.chars < data.total_chars ? '复制本页' :
      (data.kind === 'stored_evidence' ? '复制片段' : '复制 Markdown');
    body.scrollTop = 0;
    var hit = body.querySelector('.cr-citation-focus');
    if (hit) hit.scrollIntoView({block: 'center', inline: 'nearest'});
  }

  // 命中片段高亮：把工具结果里的证据片段在正文中定位出来。
  // 跨行/多空格用 \s+ 宽松匹配；找不到就静默返回（预览本身不受影响）。
  function markFocus(focus) {
    var body = document.getElementById('crBody');
    var needle = String(focus || '').replace(/\s+/g, ' ').trim();
    if (!body || needle.length < 16) return false;
    var words = needle.slice(0, 90).split(' ').filter(function (w) { return w; });
    if (!words.length) return false;
    var pattern = words.map(function (w) {
      return w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('\\s+');
    var re;
    try { re = new RegExp(pattern, 'i'); } catch (e) { return false; }
    var walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      var value = node.nodeValue || '';
      if (value.length < 16) continue;
      var m = re.exec(value);
      if (!m) continue;
      var range = document.createRange();
      range.setStart(node, m.index);
      range.setEnd(node, m.index + m[0].length);
      var mark = document.createElement('mark');
      mark.className = 'cr-hit';
      try { range.surroundContents(mark); } catch (e) { return false; }
      var banner = document.createElement('div');
      banner.className = 'cr-hit-banner';
      banner.textContent = '已定位到命中片段';
      body.insertBefore(banner, body.firstChild);
      return true;
    }
    return false;
  }

  function copySource() {
    var d = state.data;
    if (!d) return;
    var text = d.markdown || '';
    var done = function (ok) {
      var btn = readerLabel('crCopyBtn');
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = ok ? '已复制' : '复制失败';
      setTimeout(function () { btn.textContent = old; }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); },
        function () { done(fallbackCopy(text)); });
    } else {
      done(fallbackCopy(text));
    }
  }

  function fallbackCopy(text) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '-1000px';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  function reasonText(payload, status) {
    if (status === 401) return '登录状态已失效，请刷新页面后重试。';
    if (status === 403) {
      return (payload && payload.message) || '该来源需要更高权限，当前账号无法查看。';
    }
    if (payload && payload.detail && payload.detail.code === 'knowledge_base_updating') return payload.detail.message;
    if (payload && payload.message) return payload.message;
    if (status === 404) return '本地原文不可用。';
    return '读取原文失败（HTTP ' + status + '），请稍后重试。';
  }

  function openReader(key, focus) {
    var mask = ensureMask();
    mask.removeAttribute('hidden');
    document.body.classList.add('corpus-reader-open');

    state.key = key;
    state.data = null;
    state.view = 'rendered';
    state.focus = focus;
    document.getElementById('crFootNote').textContent = '';
    document.getElementById('crHeading').hidden = true;
    readerLabel('crCopyBtn').textContent = '复制原文';
    readerLabel('crViewBtn').textContent = '原始 Markdown';
    document.getElementById('crPreviousBtn').hidden = true;
    document.getElementById('crNextBtn').hidden = true;
  }

  function openCorpusSource(recordKey, opts) {
    var key = String(recordKey || '').trim();
    if (!key) return;
    state.shared = null;
    state.citation = null;
    openReader(key, (opts && opts.focus) ? String(opts.focus) : '');
    loadSource('/api/v2/corpus/source?record=' + encodeURIComponent(key), 'local');
  }

  function loadSharedPage(ref, start) {
    var params = new URLSearchParams({version_id: ref.version_id, index_generation: ref.index_generation,
                                      md_sha256: ref.md_sha256, start: start, length: PAGE_CHARS});
    if (ref.chunk_id) {
      params.set('chunk_id', ref.chunk_id);
      params.set('start', ref.start);
      params.set('length', ref.end - ref.start);
    }
    loadSource('/api/v2/shared-corpus/documents/' + encodeURIComponent(ref.document_id) + '?' + params, 'shared');
  }

  function openSharedSource(ref) {
    state.citation = null;
    state.shared = ref;
    openReader(ref.document_id, '');
    loadSharedPage(ref, ref.start);
  }

  function openCitation(source, scope) {
    if (!source || !scope) return;
    var id = String(source.reader_id || source.record_key || '');
    if (!/^(cite|shared):[a-f0-9]{64}$/.test(id) || !scope.sessionId || !scope.requestId) return;
    state.shared = null;
    state.citation = {id: id, sessionId: scope.sessionId, requestId: scope.requestId,
      producerJobId: scope.producerJobId || "", pin: null};
    openReader(id, '');
    loadCitationPage(null);
  }

  function loadCitationPage(offset) {
    var citation = state.citation;
    var params = new URLSearchParams({request_id: citation.requestId, ref_id: citation.id,
                                      mode: 'full', length: CITATION_PAGE_CHARS});
    if (citation.producerJobId) params.set('producer_job_id', citation.producerJobId);
    if (offset != null) params.set('offset', offset);
    loadSource('/api/v2/session/' + encodeURIComponent(citation.sessionId) + '/citation-source?' + params, 'citation');
  }

  function validCitationPage(page) {
    if (!page || !['stored_markdown', 'reconstructed_markdown', 'stored_evidence'].includes(page.kind)
        || page.offset_unit !== 'unicode_code_point' || typeof page.markdown !== 'string'
        || !/^[a-f0-9]{64}$/.test(page.md_sha256 || '')) return false;
    if (![page.start, page.end, page.total_chars, page.hit_start, page.hit_end].every(Number.isSafeInteger)
        || page.start < 0 || page.end <= page.start || page.end > page.total_chars
        || page.hit_start < 0 || page.hit_end <= page.hit_start || page.hit_end > page.total_chars
        || Array.from(page.markdown).length !== page.end - page.start) return false;
    if ((page.previous_start != null && (!Number.isSafeInteger(page.previous_start) ||
         page.previous_start < 0 || page.previous_start >= page.start)) ||
        (page.next_start != null && (page.next_start !== page.end || page.next_start >= page.total_chars))) return false;
    if ((page.hit_start < page.end && page.hit_end > page.start) && !citationRange(page)) return false;
    var pin = JSON.stringify([page.kind, page.coverage, page.index_generation, page.version_id, page.md_sha256,
                              page.total_chars, page.hit_start, page.hit_end]);
    if (state.citation.pin && state.citation.pin !== pin) return false;
    state.citation.pin = pin;
    return true;
  }

  function loadSource(url, kind) {
    var request = ++state.request;
    state.data = null;
    document.getElementById('crFootNote').textContent = '';
    document.getElementById('crPreviousBtn').hidden = true;
    document.getElementById('crNextBtn').hidden = true;
    setStatus('正在读取本地原文…', 'info');
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        return r.json().catch(function () { return {}; })
          .then(function (payload) { return { ok: r.ok, status: r.status, payload: payload }; });
      })
      .then(function (res) {
        if (request !== state.request) return;
        if (!res.ok || !res.payload || (kind === 'shared' ? res.payload.kind !== 'stored_markdown' : res.payload.ok !== true)) {
          setStatus(reasonText(res.payload, res.status), 'warn');
          return;
        }
        if (kind === 'citation' && !validCitationPage(res.payload)) {
          setStatus('该文献版本已变化或不可核验，请重新打开引用。', 'warn');
          return;
        }
        state.data = res.payload;
        paint();
      })
      .catch(function () {
        if (request === state.request) setStatus('读取原文失败：网络或服务异常，请稍后重试。', 'warn');
      });
  }

  // ── 本轮本地来源：按 request_id 回查工具结果，渲染可点开的来源列表 ──────
  // 回答正文里的内部键已被后端清洗，溯源只从 tool_results 回查；
  // 键只作为请求参数与 data 属性存在，标签一律用可读题名/年份/期刊。
  var SOURCE_RETRY_DELAYS = [0, 500, 1200, 2500, 5000];
  var SOURCE_FINAL_RECHECK_MS = 10000;
  var SOURCE_EMPTY_RECHECK_MS = 30000;

  function sourceStateAllowsRetry(state, rid) {
    if (!state || state.indexOf('empty:' + rid + ':') !== 0) return true;
    var parts = state.split(':');
    var checkedAt = Number(parts[parts.length - 2] || 0);
    var checks = Number(parts[parts.length - 1] || 0);
    if (!checkedAt || Date.now() - checkedAt < SOURCE_EMPTY_RECHECK_MS) return false;
    return checks < 3;
  }

  function markSourcesEmpty(bubble, rid, checks) {
    bubble.setAttribute(
      'data-cr-sources',
      'empty:' + rid + ':' + Date.now() + ':' + String(checks || 0)
    );
  }

  function completedSourceChecks(checks) {
    return Math.min(3, Math.max(1, Number(checks || 0) + 1));
  }

  function attachTurnSources(bubble, requestId) {
    if (!bubble || !requestId) return;
    var sid = window.sessionId || '';
    if (!sid) return;                                   // 没登录/没会话：下一轮再试
    var rid = String(requestId);
    var state = bubble.getAttribute('data-cr-sources');
    if (state === 'loaded:' + rid || state === 'loading:' + rid) return;
    if (!sourceStateAllowsRetry(state, rid)) return;
    bubble.setAttribute('data-cr-sources', 'loading:' + rid);
    var url = '/api/v2/session/' + encodeURIComponent(sid) +
              '/turn-sources?request_id=' + encodeURIComponent(rid);

    function ownsSources() {
      return window.sessionId === sid && bubble.isConnected !== false &&
        bubble.getAttribute('data-cr-sources') === 'loading:' + rid;
    }
    function attempt(index, checks, finalRecheck) {
      if (!ownsSources()) return;
      fetch(url, { credentials: 'same-origin' })
        .then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (payload) { return { ok: r.ok, status: r.status, payload: payload }; });
        })
        .then(function (res) {
          if (!ownsSources()) return;
          var list = (res.ok && res.payload && res.payload.sources) || [];
          if (Array.isArray(list) && list.length && renderTurnSources(bubble, list)) {
            bubble.setAttribute('data-cr-sources', 'loaded:' + rid);
            return;
          }
          if (res.status === 401 || res.status === 403) {
            markSourcesEmpty(bubble, rid, 3);
            return;
          }
          var noneIsTerminal = !!(res.ok && res.payload &&
                                  res.payload.status === 'none');
          if (noneIsTerminal && !finalRecheck) {
            bubble.setAttribute('data-cr-sources', 'loading:' + rid);
            setTimeout(function () { attempt(0, checks, true); }, SOURCE_FINAL_RECHECK_MS);
            return;
          }
          if (noneIsTerminal) {
            markSourcesEmpty(bubble, rid, completedSourceChecks(checks));
            return;
          }
          if (index + 1 < SOURCE_RETRY_DELAYS.length) {
            bubble.setAttribute('data-cr-sources', 'loading:' + rid);
            setTimeout(function () { attempt(index + 1, checks, finalRecheck); }, SOURCE_RETRY_DELAYS[index + 1]);
          } else if (!finalRecheck) {
            // A finished turn can briefly race the ledger commit. Recheck once
            // after the stream is fully closed before declaring a terminal none.
            bubble.setAttribute('data-cr-sources', 'loading:' + rid);
            setTimeout(function () { attempt(0, checks, true); }, SOURCE_FINAL_RECHECK_MS);
          } else {
            markSourcesEmpty(bubble, rid, completedSourceChecks(checks));
          }
        })
        .catch(function () {
          if (!ownsSources()) return;
          if (index + 1 < SOURCE_RETRY_DELAYS.length) {
            bubble.setAttribute('data-cr-sources', 'loading:' + rid);
            setTimeout(function () { attempt(index + 1, checks, finalRecheck); }, SOURCE_RETRY_DELAYS[index + 1]);
          } else if (!finalRecheck) {
            bubble.setAttribute('data-cr-sources', 'loading:' + rid);
            setTimeout(function () { attempt(0, checks, true); }, SOURCE_FINAL_RECHECK_MS);
          } else {
            markSourcesEmpty(bubble, rid, completedSourceChecks(checks));
          }
        });
    }
    var priorChecks = state && state.indexOf('empty:' + rid + ':') === 0
      ? Number(state.split(':').pop() || 0) : 0;
    attempt(0, priorChecks, false);
  }

  function safeWebUrl(value) {
    try {
      var parsed = new URL(String(value || ''));
      return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : '';
    } catch (e) {
      return '';
    }
  }

  function sourceIdentity(item) {
    var keys = [];
    if (item.kind === 'shared' && item.read_ref && item.read_ref.document_id) {
      var ref = item.read_ref;
      return ['shared:' + JSON.stringify([ref.document_id, ref.version_id, ref.index_generation])];
    }
    var url = safeWebUrl(item.url);
    if (item.kind === 'local' && item.record_key) keys.push('local:' + item.record_key);
    else if (item.kind !== 'web' || !url) return [];
    if (url) keys.push('url:' + url.split('#')[0]);
    var doi = String(item.doi || '').trim().toLowerCase()
      .replace(/^https?:\/\/(?:dx\.)?doi\.org\//, '').replace(/^doi:\s*/, '');
    if (!doi && url && /^https?:\/\/(?:dx\.)?doi\.org\//i.test(url)) {
      doi = new URL(url).pathname.slice(1).toLowerCase();
    }
    if (/^10\.\d+\//.test(doi)) keys.push('doi:' + doi);
    return keys;
  }

  function renderTurnSources(bubble, list) {
    var old = bubble.querySelector('.cr-turn-sources');
    var shared = [], currentLocals = [], currentWebs = [], historyLocals = [], historyWebs = [];
    var seen = new Set(), count = 0;
    var candidates = list.filter(Boolean);
    var ordered = candidates.filter(function (item) { return item.origin !== 'history'; })
      .concat(candidates.filter(function (item) { return item.origin === 'history'; }));
    ordered.forEach(function (item) {
      var keys = sourceIdentity(item);
      if (!keys.length || keys.some(function (key) { return seen.has(key); })) return;
      keys.forEach(function (key) { seen.add(key); });
      count++;
      var historical = item.origin === 'history';
      if (item.kind === 'shared') shared.push(item);
      else if (item.kind === 'local') (historical ? historyLocals : currentLocals).push(item);
      else (historical ? historyWebs : currentWebs).push(item);
    });
    if (!count) return false;
    if (old) old.remove();

    var row = document.createElement('details');
    row.className = 'cr-turn-sources';
    row.open = !!(old && old.open);
    var summary = document.createElement('summary');
    summary.textContent = '候选来源（' + count + '）';
    row.appendChild(summary);
    var chips = document.createElement('div');
    chips.className = 'cr-ts-items';
    row.appendChild(chips);

    function group(title, items, makeChip) {
      if (!items.length) return;
      var label = document.createElement('span');
      label.className = 'cr-ts-label';
      label.textContent = title;
      chips.appendChild(label);
      for (var k = 0; k < items.length; k++) chips.appendChild(makeChip(items[k]));
    }

    function localChip(item) {
      var a = document.createElement('a');
      a.className = 'cr-ts-chip';
      a.href = '#';
      a.setAttribute('data-corpus-key', item.record_key);
      if (item.snippet) a.setAttribute('data-focus', item.snippet);
      var label = item.title || '本地文献';
      a.textContent = label;
      var tip = [label];
      if (item.journal) tip.push(item.journal);
      if (item.year) tip.push(String(item.year));
      if (item.registry) tip.push(item.registry);
      a.title = tip.join(' · ') + ' — 点击查看原文并定位命中片段';
      return a;
    }

    function webChip(item) {
      var a = document.createElement('a');
      a.className = 'cr-ts-chip cr-ts-web';
      a.href = safeWebUrl(item.url);
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      var label = item.title || item.url;
      a.textContent = label;
      a.title = (item.snippet ? item.snippet.slice(0, 120) + ' — ' : '') + item.url;
      return a;
    }

    group('共享文献', shared, function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'cr-ts-chip';
      button.textContent = item.title || '共享文献';
      button.addEventListener('click', function () { openSharedSource(item.read_ref); });
      return button;
    });
    group('本轮本地来源', currentLocals, localChip);
    group('本轮网络来源', currentWebs, webChip);
    group('历史复用来源 · 本地', historyLocals, localChip);
    group('历史复用来源 · 网络', historyWebs, webChip);

    var content = null;
    var actions = null;
    for (var n = 0; n < bubble.children.length; n++) {
      var child = bubble.children[n];
      if (child.classList && child.classList.contains('content') && !content) {
        content = child;
      }
      if (child.classList && child.classList.contains('message-actions') && !actions) {
        actions = child;
      }
    }
    if (content && actions && content.nextSibling === actions) bubble.insertBefore(row, actions);
    else if (content && content.nextSibling) bubble.insertBefore(row, content.nextSibling);
    else if (content) bubble.appendChild(row);
    else bubble.appendChild(row);
    return true;
  }

  function attachHistorySources(root) {
    var scope = root || document;
    var bubbles = scope.querySelectorAll('.message.bot');
    for (var i = 0; i < bubbles.length; i++) {
      var b = bubbles[i];
      // request id 挂在气泡内的 .content 上（历史与流式同一契约）
      var holder = b.querySelector('[data-request-id]');
      if (!holder) continue;
      var rid = holder.getAttribute('data-request-id');
      if (!rid) continue;
      var trace = b.querySelector('.tool-trace');
      if (trace && !trace.classList.contains('is-done')) continue;  // 流式未结束
      attachTurnSources(b, rid);
    }
  }

  // chips 自己的点击代理：不依赖 app.js 里针对 a.corpus-key 的代理
  document.addEventListener('click', function (ev) {
    var el = ev.target;
    if (!el || !el.closest) return;
    var chip = el.closest('.cr-ts-chip');
    if (!chip) return;
    if (chip.classList.contains('cr-ts-web')) return;   // 保留浏览器默认的新标签页跳转
    ev.preventDefault();
    var key = chip.getAttribute('data-corpus-key');
    var focus = chip.getAttribute('data-focus') || '';
    if (key) {
      if (typeof openCorpusSource === 'function') openCorpusSource(key, { focus: focus });
      else window.openCorpusSource(key, { focus: focus });
    }
  }, true);

  window.openCorpusSource = openCorpusSource;
  window.CitrusCorpusReader = {
    open: openCorpusSource,
    openShared: openSharedSource,
    openCitation: openCitation,
    close: closeReader,
    isOpen: isOpen,
    getState: function () { return { key: state.key, view: state.view, chars: (state.data && state.data.chars) || 0 }; },
    attachTurnSources: attachTurnSources,
    attachHistorySources: attachHistorySources
  };
})();
