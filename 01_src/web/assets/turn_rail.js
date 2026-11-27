/* P2/D-A3: Codex-style turn rail (right-edge navigation for long sessions).
 *
 * Design rules:
 *  - one tick per real user turn, sourced from the server turn index
 *    (`/api/v2/session/{id}/turns`) — never from the loaded DOM, so turns that
 *    have not been paged into the chat yet still get a tick;
 *  - every turn is listed; a long session scrolls the rail instead of dropping
 *    the oldest ticks (D-A3: 全量 + 可滚动);
 *  - tool heartbeats never appear here;
 *  - ticks are buttons, so keyboard users reach them with Tab/Enter;
 *  - hovering/focusing reveals the question text, never a fake title;
 *  - clicking locates the turn, walking back through history pages when the
 *    turn is not loaded yet, and says so plainly when it cannot;
 *  - the current position is derived from the viewport, not from a guess.
 */
(function () {
  'use strict';

  var PAGE = 400;            // turns per server request
  var MAX_PAGES = 5;         // bounded fetch: up to 2000 turns listed at once
  var WALK_LIMIT = 80;       // bounded history walk when locating an old turn
  var DENSE_AT = 40;         // above this many ticks, tighten spacing
  var TAIL_DISTANCE = 36;

  var railEl = null, trackEl = null, tipEl = null, latestEl = null, statusEl = null;
  var ticks = [];
  var following = true, lastTop = 0;
  var activeLockUntil = 0;   // 跳转后短暂锁定当前刻度，避免滚动动画把高亮甩到别的轮次
  var activeMid = '';
  var buildTimer = 0, rafPending = false, busy = false;
  var turnsCache = [];       // oldest → newest, from the server index
  var hasMoreTurns = false, nextBefore = 0, loadedSession = '';
  var generation = 0, loadedEpoch = -1, indexReads = null;
  var pendingTurns = [];   // 本轮已提交、尚未拿到正式 message_id 的提问

  function chatBox() {
    if (typeof chatContainer !== 'undefined' && chatContainer) return chatContainer;
    return document.getElementById('chat-container');
  }

  function currentSession() {
    return (typeof sessionId !== 'undefined' && sessionId) ? String(sessionId) : '';
  }

  function ensureRail() {
    if (railEl && document.body.contains(railEl)) return railEl;
    var panel = chatBox().parentElement;
    railEl = document.createElement('nav');
    railEl.id = 'turnRail';
    railEl.className = 'turn-rail';
    railEl.setAttribute('aria-label', '本会话提问定位');
    trackEl = document.createElement('div');
    trackEl.className = 'turn-rail-track';
    railEl.appendChild(trackEl);
    tipEl = document.createElement('div');
    tipEl.id = 'turnRailTip';
    tipEl.className = 'turn-rail-tip';
    tipEl.setAttribute('role', 'tooltip');
    latestEl = document.createElement('button');
    latestEl.type = 'button'; latestEl.className = 'turn-return-latest'; latestEl.hidden = true;
    latestEl.textContent = '↓ 返回最新';
    latestEl.addEventListener('click', function () {
      follow(true);
      chatBox().focus({preventScroll: true});
    });
    statusEl = document.createElement('p'); statusEl.className = 'turn-rail-status'; statusEl.hidden = true;
    statusEl.setAttribute('role', 'status');
    panel.append(railEl, tipEl, statusEl);
    panel.querySelector('footer').prepend(latestEl);
    return railEl;
  }

  function shortText(t) {
    // messages sometimes carry literal backslash escapes instead of newlines
    var s = String(t || '').replace(/\\[nrt]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!s) return '（空提问）';
    return s.length > 46 ? s.slice(0, 46) + '…' : s;
  }

  function accountEpoch() { return typeof authEpoch === 'number' ? authEpoch : 0; }

  function belongs(sid, epoch, version) {
    return sid === currentSession() && epoch === accountEpoch() && version === generation;
  }

  function fetchTurns(before) {
    var sid = currentSession(), epoch = accountEpoch(), version = generation;
    if (!sid) return Promise.resolve(null);
    var url = '/api/v2/session/' + encodeURIComponent(sid) + '/turns?limit=' + PAGE;
    if (before) url += '&before=' + encodeURIComponent(before);
    var request = typeof accountFetch === 'function' ? accountFetch : fetch;
    return request(url, {signal: indexReads && indexReads.signal, cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { return belongs(sid, epoch, version) ? data : null; })
      .catch(function () { return null; });
  }

  function mergeIndex(rows) {
    var byId = new Map(turnsCache.map(function (turn) { return [turn.message_id, turn]; }));
    rows.forEach(function (turn) { byId.set(turn.message_id, turn); });
    turnsCache = Array.from(byId.values()).sort(function (a, b) { return a.message_id - b.message_id; });
  }

  /* Load the whole turn index once per session (paged, bounded). */
  function loadTurns() {
    var sid = currentSession();
    var epoch = accountEpoch();
    if (sid === loadedSession && epoch === loadedEpoch) { build(); return; }
    ensureRail();
    if (indexReads) indexReads.abort();
    indexReads = new AbortController();
    var version = ++generation;
    loadedSession = sid; loadedEpoch = epoch;
    busy = false; following = true; lastTop = 0; activeMid = ''; activeLockUntil = 0;
    latestEl.hidden = statusEl.hidden = true;
    turnsCache = [];
    pendingTurns = [];
    hasMoreTurns = false;
    nextBefore = 0;
    build();
    if (!sid) return;
    var pages = 0;
    function step() {
      return fetchTurns(nextBefore).then(function (d) {
        if (!d || !belongs(sid, epoch, version)) return;
        mergeIndex(d.turns || []);
        hasMoreTurns = !!d.has_more;
        nextBefore = Number(d.next_before || 0);
        pages += 1;
        if (hasMoreTurns && nextBefore && pages < MAX_PAGES) return step();
        build();
      });
    }
    step();
  }

  function build() {
    ensureRail();
    trackEl.innerHTML = '';
    ticks = [];
    if (!turnsCache.length && !pendingTurns.length) {
      railEl.classList.add('is-empty');
      hideTip();
      return;
    }
    railEl.classList.remove('is-empty');
    trackEl.classList.toggle('is-dense', turnsCache.length > DENSE_AT);

    if (hasMoreTurns) {
      var more = document.createElement('button');
      more.type = 'button';
      more.className = 'turn-rail-more';
      more.textContent = '⋮';
      more.title = '还有更早的提问未列出，点击继续加载';
      more.setAttribute('aria-label', '继续加载更早的提问');
      more.addEventListener('click', function () {
        var before = nextBefore;
        if (!before || more.disabled) return;
        more.disabled = true;
        fetchTurns(before).then(function (d) {
          if (!d || before !== nextBefore) return;
          mergeIndex(d.turns || []);
          hasMoreTurns = !!d.has_more;
          nextBefore = Number(d.next_before || 0);
          build();
        }).finally(function () { more.disabled = false; });
      });
      trackEl.appendChild(more);
    }

    for (var i = 0; i < turnsCache.length; i++) {
      var t = turnsCache[i];
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'turn-tick';
      b.dataset.mid = String(t.message_id || '');
      b.dataset.tip = '第 ' + (i + 1) + ' 次提问 · ' + shortText(t.text);
      b.setAttribute('aria-label', '定位到第 ' + (i + 1) + ' 次提问：' + shortText(t.text));
      b.addEventListener('click', function () { focusTurn(this.dataset.mid); });
      b.addEventListener('mouseenter', function () { showTip(this); });
      b.addEventListener('focus', function () { showTip(this); });
      b.addEventListener('mouseleave', hideTip);
      b.addEventListener('blur', hideTip);
      trackEl.appendChild(b);
      ticks.push({ mid: b.dataset.mid, el: b });
    }
    // P0: 刚提交、还没拿到正式 message_id 的提问——先给一个临时刻度，
    //     服务端确认后由 sync() 换成真实 ID；发送失败则 dropPending() 撤掉。
    for (var j = 0; j < pendingTurns.length; j++) {
      var pt = pendingTurns[j];
      var pb = document.createElement('button');
      pb.type = 'button';
      pb.className = 'turn-tick is-pending';
      pb.dataset.mid = String(pt.message_id || '');
      pb.dataset.tip = '第 ' + (turnsCache.length + j + 1) + ' 次提问（正在保存）· ' + shortText(pt.text);
      pb.setAttribute('aria-label', '第 ' + (turnsCache.length + j + 1) + ' 次提问，正在保存');
      pb.addEventListener('click', function () {
        notify('这条提问还在保存中，稍等片刻就能定位。');
      });
      trackEl.appendChild(pb);
      ticks.push({ mid: pb.dataset.mid, el: pb, pending: true });
    }
    markActive();
    syncActive();
  }

  /* P0: 提交瞬间先插一个临时刻度（不依赖刷新、不轮询）。 */
  function addPending(crid, text) {
    if (!crid) return;
    var sid = currentSession();
    if (!sid) return;
    for (var i = 0; i < pendingTurns.length; i++) {
      if (pendingTurns[i].crid === crid) return;   // 同一条提问只生成一个刻度
    }
    pendingTurns.push({ message_id: 'pending:' + crid, crid: crid, text: String(text || '') });
    build();
  }

  /* P0: 发送失败——临时刻度必须撤掉，不能永久留在正式导航里。 */
  function dropPending(crid) {
    if (!crid) return;
    var before = pendingTurns.length;
    pendingTurns = pendingTurns.filter(function (t) { return t.crid !== crid; });
    if (pendingTurns.length !== before) build();
  }

  /* P0: 服务端已落库——拉一次最新一页索引，用真实 message_id 替换临时刻度。
     只发一次 GET，不轮询、不改 SSE 协议。 */
  function sync(pendingCrid, text) {
    var sid = currentSession(), epoch = accountEpoch(), version = generation;
    if (!sid) return Promise.resolve(false);
    return fetchTurns(0).then(function (data) {
      if (!data || !belongs(sid, epoch, version)) return false;
      var known = turnsCache.length;
      mergeIndex(data.turns || []);
      if (pendingCrid && (data.turns || []).some(function (turn) { return turn.request_id === pendingCrid; })) {
        pendingTurns = pendingTurns.filter(function (turn) { return turn.crid !== pendingCrid; });
      }
      build();
      return turnsCache.length > known;
    });
  }

  function scheduleBuild() {
    if (buildTimer) clearTimeout(buildTimer);
    buildTimer = setTimeout(function () {
      buildTimer = 0;
      var sid = currentSession();
      if (sid !== loadedSession || loadedEpoch !== accountEpoch()) loadTurns();
      else onScroll();
    }, 120);
  }

  function showTip(el) {
    if (!tipEl) return;
    tipEl.textContent = el.dataset.tip || '';
    var r = el.getBoundingClientRect();
    var origin = railEl.parentElement.getBoundingClientRect().top;
    var scale = window.CitrusTheme ? window.CitrusTheme.scale.effective() : 1;
    tipEl.style.top = Math.round((r.top + r.height / 2 - origin) / scale) + 'px';
    tipEl.classList.add('is-visible');
  }

  function hideTip() {
    if (tipEl) tipEl.classList.remove('is-visible');
  }

  function markActive() {
    for (var i = 0; i < ticks.length; i++) {
      var on = ticks[i].mid === activeMid;
      ticks[i].el.classList.toggle('is-active', on);
      if (on) ticks[i].el.setAttribute('aria-current', 'location');
      else ticks[i].el.removeAttribute('aria-current');
      if (on && trackEl.scrollHeight > trackEl.clientHeight) {
        // keep the current turn visible inside a scrolling rail
        var el = ticks[i].el;
        var top = el.offsetTop, h = el.offsetHeight;
        var view = trackEl.scrollTop, box = trackEl.clientHeight;
        if (top < view + 4) trackEl.scrollTop = Math.max(0, top - 8);
        else if (top + h > view + box - 4) trackEl.scrollTop = top + h - box + 8;
      }
    }
  }

  function syncActive() {
    if (Date.now() < activeLockUntil) return;
    var box = chatBox();
    if (!box || !ticks.length) return;
    var rect = box.getBoundingClientRect();
    var center = rect.top + rect.height / 2;
    var best = '', bestD = Infinity;
    box.querySelectorAll('.message.user[data-message-id], .message.user > [data-message-id]').forEach(function (node) {
      var nr = node.getBoundingClientRect();
      var d = Math.abs((nr.top + nr.bottom) / 2 - center);
      if (d < bestD) { bestD = d; best = node.dataset.messageId; }
    });
    if (best && best !== activeMid) { activeMid = best; markActive(); }
  }

  function updateScroll() {
    var box = chatBox();
    if (!box || !latestEl) return;
    var nearTail = box.scrollHeight - box.clientHeight - box.scrollTop <= TAIL_DISTANCE;
    if (nearTail) following = true;
    else if (box.scrollTop < lastTop - 1) following = false;
    latestEl.hidden = following || nearTail;
    lastTop = box.scrollTop;
  }

  function onScroll() {
    updateScroll();
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () { rafPending = false; syncActive(); });
  }

  function follow(force) {
    var box = chatBox();
    if (!box || (!force && !following)) return;
    following = true;
    box.scrollTop = box.scrollHeight;
    lastTop = box.scrollTop;
    onScroll();
  }

  function notify(msg) {
    if (typeof showBanner === 'function') {
      try { showBanner(msg); return; } catch (e) { /* fall through */ }
    }
    if (statusEl) {
      statusEl.hidden = false; statusEl.textContent = msg;
      clearTimeout(notify.timer);
      notify.timer = setTimeout(function () { statusEl.hidden = true; }, 7000);
    }
  }

  function findInHistory(mid) {
    var msgs = (typeof _historyMsgs !== 'undefined' && Array.isArray(_historyMsgs)) ? _historyMsgs : [];
    for (var i = 0; i < msgs.length; i++) {
      if (String(msgs[i] && msgs[i].message_id || '') === String(mid)) return msgs[i];
    }
    return null;
  }

  function pinActive(mid) {
    activeMid = String(mid);
    activeLockUntil = Date.now() + 2200;
    markActive();
    // Re-evaluate after the scroll animation without mutating a newer session.
    var sid = currentSession(), epoch = accountEpoch(), version = generation;
    setTimeout(function () { if (belongs(sid, epoch, version)) onScroll(); }, 2300);
  }

  /* 目标已在视口里时滚不动是正常的，但必须给出可见反馈，不能像“没反应”。 */
  function announceIfInView(mid) {
    var box = chatBox();
    if (!box) return;
    var node = box.querySelector('[data-message-id="' + mid + '"]');
    if (!node) return;
    var br = box.getBoundingClientRect();
    var nr = node.getBoundingClientRect();
    if (!(nr.top >= br.top - 2 && nr.bottom <= br.bottom + 2)) return;
    var idx = -1;
    for (var i = 0; i < ticks.length; i++) {
      if (String(ticks[i].mid) === String(mid)) { idx = i; break; }
    }
    if (idx >= 0) notify('第 ' + (idx + 1) + ' 次提问已在当前视野内');
  }

  async function focusTurn(mid) {
    if (!mid || busy) return;
    if (typeof jumpToMessage === 'function' && jumpToMessage(mid)) {
      pinActive(mid);
      announceIfInView(mid);
      return;
    }
    busy = true;
    var sid = currentSession(), epoch = accountEpoch(), version = generation;
    try {
      var found = findInHistory(mid);
      var steps = 0;
      while (!found && steps < WALK_LIMIT) {
        if (typeof _historyHasMore === 'undefined' || !_historyHasMore) break;
        if (typeof loadEarlierHistory !== 'function') break;
        var loaded = await loadEarlierHistory();
        if (!belongs(sid, epoch, version)) return;
        if (!loaded) break;
        found = findInHistory(mid);
        steps += 1;
      }
      if (!found) {
        notify('这次提问不在已加载的历史里：请先在对话顶部点「载入更早的对话」，再点右侧定位。');
        return;
      }
      if (typeof jumpToMessage === 'function' && jumpToMessage(mid)) { pinActive(mid); return; }
      notify('这一轮已经载入，但页面没能滚动到它，请手动向上查找。');
    } finally {
      if (belongs(sid, epoch, version)) busy = false;
    }
  }

  function boot() {
    var box = chatBox();
    if (!box) return;
    ensureRail();
    if (box) {
      box.setAttribute('tabindex', '0');
      box.addEventListener('scroll', onScroll, { passive: true });
      if (typeof MutationObserver === 'function') {
        new MutationObserver(scheduleBuild).observe(box, { childList: true });
      }
    }
    window.addEventListener('resize', onScroll, { passive: true });
    window.addEventListener('citrus:scalechange', onScroll);
    window.addEventListener('citrus:session-changed', loadTurns);
    loadTurns();
    // app.js restores history asynchronously; re-check once it settles.
    setTimeout(scheduleBuild, 600);
    setTimeout(scheduleBuild, 1800);
  }

  window.turnRail = {
    refresh: scheduleBuild, focus: focusTurn, reload: loadTurns,
    addPending: addPending, dropPending: dropPending, sync: sync, follow: follow
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
