(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CitrusContextView = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const CELL_COUNT = 100;
  function count(value) {
    return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
  }
  function project(snapshot) {
    const data = snapshot || {};
    const used = count(data.estimated_input_tokens ?? data.estimated_tokens);
    const full = data.estimate_scope === 'full_request_estimate';
    const capacity = full && count(data.model_window) || null;
    const ratio = used != null && capacity != null ? used / capacity : null;
    const state = ratio == null ? 'unknown' : ratio >= .93 ? 'critical' : ratio >= .75 ? 'warning' : 'healthy';
    return { used, capacity, ratio, state, historical: !full && used != null };
  }
  function short(n) {
    if (n == null) return '—';
    if (n < 1000) return n.toLocaleString();
    if (n < 1_000_000) return Number((n / 1000).toFixed(n < 100_000 ? 1 : 0)) + 'K';
    return Number((n / 1_000_000).toFixed(2)) + 'M';
  }
  function percent(ratio) {
    if (ratio == null) return '—';
    return ratio > 0 && ratio < .01 ? '<1%' : Math.round(ratio * 100) + '%';
  }
  function create(doc) {
    const el = id => doc.getElementById(id);
    const drawer = el('ctxDrawer'), backdrop = el('ctxBackdrop'), trigger = el('ctxCircleBtn');
    const close = el('ctxClose'), ringMode = el('ctxViewRing'), gridMode = el('ctxViewGrid');
    const ring = el('ctxRing'), grid = el('ctxGrid'), headerArc = el('ctxBtnFg'), panelArc = el('ctxPanelFg');
    let current = project(null);
    const cells = Array.from({length: CELL_COUNT}, () => doc.createElement('i'));
    grid.replaceChildren(...cells);
    function arc(node, ratio) {
      const used = ratio == null ? 0 : Math.min(1, ratio);
      node.setAttribute('stroke-dasharray', `${used * 100} 100`);
    }
    function render() {
      const {used, capacity, ratio, state, historical} = current;
      const gridOn = gridMode.checked && ratio != null;
      const percentage = percent(ratio);
      drawer.dataset.level = trigger.dataset.level = state;
      ring.hidden = gridOn;
      grid.hidden = !gridOn;
      gridMode.disabled = ratio == null;
      if (ratio == null) ringMode.checked = true;
      const visibleCells = ratio == null ? 0 : Math.round(Math.min(ratio, 1) * CELL_COUNT);
      cells.forEach((cell, index) => cell.classList.toggle('is-used', index < visibleCells));
      arc(headerArc, ratio); arc(panelArc, ratio);
      el('ctxRingPct').textContent = percentage;
      el('ctxUsed').textContent = short(used) + (capacity == null ? '' : ' / ' + short(capacity));
      el('ctxUsed').title = used == null ? '' : `${used.toLocaleString()}${capacity == null ? '' : ' / ' + capacity.toLocaleString()} tokens`;
      el('ctxStatus').textContent = {healthy:'健康', warning:'接近容量', critical:'占用较高', unknown:''}[state];
      el('ctxStatus').hidden = state === 'unknown';
      el('ctxValueNote').textContent = used == null ? '占用量暂不可用' : historical ? '历史估算 · 容量未知'
        : capacity == null ? '已用上下文（估算） · 容量未知' : '已用上下文 / 窗口容量（估算）';
      el('ctxGridPct').textContent = gridOn ? '占用 ' + percentage : '';
      el('ctxGridPct').hidden = !gridOn;
      trigger.setAttribute('aria-label', used == null ? '上下文用量' : `上下文已用 ${short(used)}${ratio == null ? '，容量未知' : `，占用 ${percentage}`}`);
      trigger.title = trigger.getAttribute('aria-label');
    }
    function setOpen(open, focus = true) {
      drawer.classList.toggle('open', open);
      backdrop.classList.toggle('open', open);
      trigger.setAttribute('aria-expanded', String(open));
      drawer.inert = !open;
      if (focus) (open ? close : trigger).focus({preventScroll: true});
      if (open) render();
    }
    ringMode.addEventListener('change', render);
    gridMode.addEventListener('change', render);
    doc.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.defaultPrevented || drawer.inert) return;
      if (doc.activeElement && !drawer.contains(doc.activeElement) && doc.activeElement !== trigger) return;
      event.preventDefault(); setOpen(false);
    });
    render();
    return { update(snapshot) { current = project(snapshot); render(); }, close() { setOpen(false, false); },
      toggle() { setOpen(drawer.inert); } };
  }
  return { project, short, percent, create };
});
