(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CitrusContextView = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const COMPONENTS = [
    ['system', '系统指令', '系统与开发者消息中的文本。'],
    ['messages', '消息内容', '消息正文，包括当前请求、带入历史和工具结果。现有估算不分别统计这些内容。'],
    ['tool_arguments', '工具调用参数', '请求中保留的工具调用及其结构化参数。'],
    ['tool_schemas', '工具定义', '本次请求发送的工具名称、说明和参数定义。'],
    ['images', '图片', '基于请求中的图片数量保守估算，不是服务商实测。'],
    ['protocol', '协议开销', '消息边界、命名字段与部分请求选项的估算。']
  ];
  const CELL_COUNT = 100;
  const LIMIT_LABEL = { window: '模型上下文窗口', input: '应用单次输入上限' };
  const HISTORY_LABEL = { loaded: '历史已加载', success: '历史已加载', empty: '暂无历史', degraded: '历史加载受限', failed: '历史加载失败' };
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;

  function providerUsage(data) {
    const usage = data.provider_usage;
    if (!usage || usage.scope !== 'single_call' || !data.logical_call_id || !data.attempt_id ||
        usage.logical_call_id !== data.logical_call_id || usage.attempt_id !== data.attempt_id) return null;
    const input = count(usage.input_tokens), output = count(usage.output_tokens);
    const read = count(usage.cache_read), uncached = count(usage.cache_uncached), write = count(usage.cache_write);
    if ([input, output, read, uncached, write].every(value => value == null)) return null;
    // Only show coverage when explicit provider read and uncached counters account
    // for the full reported input. A cache write is an independent observation.
    const coverage = input > 0 && read != null && uncached != null && read + uncached === input ? read / input : null;
    return {input, output, read, uncached, write, coverage};
  }

  function project(snapshot) {
    const data = snapshot || {};
    const used = count(data.estimated_input_tokens ?? data.estimated_tokens);
    const full = data.estimate_scope === 'full_request_estimate';
    const modelWindow = full && count(data.model_window) || null;
    const inputLimit = full && count(data.input_limit) || null;
    const capacity = modelWindow ?? inputLimit;
    const limitKind = modelWindow != null ? 'window' : inputLimit != null ? 'input' : null;
    const ratio = used != null && capacity != null ? used / capacity : null;
    const ringLimit = full && count(data.application_input_limit) || inputLimit;
    const ringRatio = used != null && ringLimit != null ? used / ringLimit : null;
    const state = ratio == null ? 'unknown' : ratio >= .93 ? 'critical' : ratio >= .75 ? 'warning' : 'healthy';
    const rows = full && data.components && typeof data.components === 'object' && used != null
      ? COMPONENTS.map(([key, label, detail], index) => ({key, label, detail, slot: index + 1, value: count(data.components[key])})) : [];
    const known = rows.filter(row => row.value != null);
    const knownTotal = known.reduce((sum, row) => sum + row.value, 0);
    const breakdown = known.length && knownTotal > used ? 'inconsistent'
      : !known.length ? 'unavailable' : known.length < rows.length || knownTotal < used ? 'partial' : 'complete';
    if (full && used != null && breakdown !== 'inconsistent' && used > knownTotal) rows.push({
      key: 'other', label: '未分类输入', detail: '有输入总量，尚未记录这部分的分类。', slot: 0, value: used - knownTotal
    });
    const free = ratio != null ? Math.max(0, capacity - used) : null;
    if (free != null) rows.push({key: 'free', label: '未用额度', slot: -1, value: free,
      detail: limitKind === 'input' ? '按应用单次输入上限扣除估算输入。' : '窗口容量扣除估算输入，未扣除为生成预留的令牌。'});
    return {used, full, capacity, inputLimit, ringLimit, ringRatio, modelWindow, limitKind, ratio, state, rows, breakdown, free,
      chartTotal: full && used != null && breakdown !== 'inconsistent' ? Math.max(used, capacity ?? 0) : 0,
      historical: !full && used != null, estimateMethod: data.estimate_method, model: full ? data.model : '',
      outputReserve: full ? count(data.output_tokens) : null, usage: full ? providerUsage(data) : null,
      scope: data.view_scope, history: full ? HISTORY_LABEL[data.history_load_status] : '',
      session: data.session_id || '', request: data.request_id || ''};
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

  function gridSlots(rows, used, total) {
    if (!total) return [];
    const active = Math.round(used / total * CELL_COUNT);
    const counts = rows.filter(row => row.key !== 'free' && row.value > 0).map(row => {
      const size = row.value / used * active;
      return {key: row.key, whole: Math.floor(size), fraction: size % 1};
    });
    const remainder = active - counts.reduce((sum, row) => sum + row.whole, 0);
    counts.slice().sort((a, b) => b.fraction - a.fraction).slice(0, remainder).forEach(row => row.whole++);
    return [...counts.flatMap(row => Array(row.whole).fill(row.key)), ...Array(CELL_COUNT - active).fill('free')];
  }

  // Binary treemap with proportional areas and reasonably compact cells.
  function treemap(rows) {
    const positive = rows.filter(row => row.value > 0).sort((a, b) => b.value - a.value);
    function layout(level, x, y, w, h) {
      if (level.length === 1) return [{row: level[0], x, y, w, h}];
      const total = level.reduce((sum, row) => sum + row.value, 0);
      let split = 1, first = level[0].value;
      while (split < level.length - 1 && Math.abs(first + level[split].value - total / 2) < Math.abs(first - total / 2)) {
        first += level[split++].value;
      }
      const left = level.slice(0, split), right = level.slice(split), ratio = first / total;
      return w * 2.4 >= h
        ? [...layout(left, x, y, w * ratio, h), ...layout(right, x + w * ratio, y, w * (1 - ratio), h)]
        : [...layout(left, x, y, w, h * ratio), ...layout(right, x, y + h * ratio, w, h * (1 - ratio))];
    }
    const free = positive.find(row => row.key === 'free');
    if (!free || positive.length === 1) return positive.length ? layout(positive, 0, 0, 1, 1) : [];
    const usedRows = positive.filter(row => row !== free);
    const total = positive.reduce((sum, row) => sum + row.value, 0);
    const usedWidth = (total - free.value) / total;
    return [...layout(usedRows, 0, 0, usedWidth, 1), {row:free, x:usedWidth, y:0, w:1 - usedWidth, h:1}];
  }

  function create(doc) {
    const el = id => doc.getElementById(id);
    const drawer = el('ctxDrawer'), backdrop = el('ctxBackdrop'), trigger = el('ctxCircleBtn');
    const close = el('ctxClose'), treeMode = el('ctxViewTreemap'), gridMode = el('ctxViewGrid');
    const tree = el('ctxTreemap'), grid = el('ctxGrid'), headerArc = el('ctxBtnFg');
    const legend = el('ctxLegend'), description = el('ctxCategoryDetail'), metadata = el('ctxMetadata');
    const usageBlock = el('ctxUsage'), usageRows = el('ctxUsageRows');
    const cells = Array.from({length: CELL_COUNT}, () => doc.createElement('i'));
    grid.replaceChildren(...cells);
    let current = project(null), selected = '', drawn = '', legendRows = new Map(), tiles = [];

    function showCategory(key) {
      selected = selected === key ? '' : key;
      renderCategory();
    }

    function renderCategory() {
      const row = current.rows.find(entry => entry.key === selected);
      const amount = row && (row.value == null ? '未报告' : row.value.toLocaleString() + ' 令牌');
      description.textContent = row ? `${row.label}（${amount}） · ${row.detail}` : '';
      description.hidden = !row;
      legendRows.forEach(({button}, key) => button.setAttribute('aria-expanded', String(key === selected)));
      tiles.forEach(tile => tile.setAttribute('aria-expanded', String(tile.dataset.category === selected)));
    }

    function addCategory(row) {
      const button = doc.createElement('button'), swatch = doc.createElement('i');
      const label = doc.createElement('span'), value = doc.createElement('span'), share = doc.createElement('span');
      button.type = 'button'; button.className = 'ctx-item context-legend-row';
      button.dataset.category = row.key;
      button.setAttribute('aria-controls', description.id);
      swatch.className = 'ctx-swatch ctx-slot-' + row.slot;
      swatch.setAttribute('aria-hidden', 'true');
      label.className = 'ctx-item-name'; label.append(swatch, row.label);
      value.className = 'ctx-item-value'; share.className = 'ctx-item-percent';
      button.append(label, value, share);
      button.addEventListener('click', () => showCategory(row.key));
      legendRows.set(row.key, {button, value, share});
      legend.append(button);
    }

    function renderCharts() {
      const {rows, used, chartTotal} = current;
      const stamp = JSON.stringify([used, chartTotal, rows.map(({key, value}) => [key, value])]);
      if (drawn === stamp) return;
      drawn = stamp;
      const slots = gridSlots(rows, used, chartTotal), itemByKey = new Map(rows.map(row => [row.key, row]));
      cells.forEach((cell, index) => {
        const key = slots[index], row = itemByKey.get(key);
        cell.className = row ? 'ctx-cell ctx-slot-' + row.slot + (key === 'free' ? '' : ' is-used') : '';
      });
      const focusedCategory = tree.contains(doc.activeElement) ? doc.activeElement.dataset.category : '';
      tiles = (chartTotal > 0 ? treemap(rows) : []).map(({row, x, y, w, h}) => {
        const tile = doc.createElement('button');
        tile.type = 'button'; tile.className = 'context-tree-tile ctx-slot-' + row.slot;
        tile.dataset.category = row.key;
        tile.setAttribute('aria-label', row.label + ' ' + short(row.value));
        tile.setAttribute('aria-controls', description.id);
        tile.title = row.label + ' ' + row.value.toLocaleString();
        Object.assign(tile.style, {left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%`});
        if (w >= .23 && h >= .26) {
          const label = doc.createElement('span'); label.textContent = row.label;
          tile.append(label);
        }
        tile.addEventListener('click', () => showCategory(row.key));
        return tile;
      });
      tree.replaceChildren(...tiles);
      if (focusedCategory) (tiles.find(tile => tile.dataset.category === focusedCategory) || treeMode).focus({preventScroll:true});
    }

    function renderLegend() {
      const {rows, capacity, used, breakdown} = current;
      const keys = new Set(rows.map(row => row.key));
      legendRows.forEach(({button}, key) => { if (!keys.has(key)) { button.remove(); legendRows.delete(key); } });
      rows.forEach((row, index) => {
        if (!legendRows.has(row.key)) addCategory(row);
        const {button, value, share} = legendRows.get(row.key);
        if (legend.children[index] !== button) legend.insertBefore(button, legend.children[index] || null);
        value.textContent = short(row.value);
        value.title = row.value == null ? '未报告' : row.value.toLocaleString();
        const basis = capacity ?? used;
        share.textContent = basis > 0 && row.value != null && (breakdown !== 'inconsistent' || row.key === 'free')
          ? percent(row.value / basis) : '—';
      });
      legend.hidden = !rows.length;
      if (!keys.has(selected)) selected = '';
      renderCategory();
    }

    function renderUsage(usage) {
      usageBlock.hidden = !usage;
      if (!usage) { usageRows.replaceChildren(); return; }
      const reported = [['输入', usage.input], ['输出', usage.output], ['缓存读取', usage.read],
        ['未命中输入', usage.uncached], ['缓存写入', usage.write], ['缓存覆盖率', usage.coverage, true]];
      usageRows.replaceChildren(...reported.flatMap(([label, value, proportion]) => {
        if (value == null) return [];
        const term = doc.createElement('dt'), amount = doc.createElement('dd');
        term.textContent = label;
        amount.textContent = proportion ? percent(value) : value.toLocaleString();
        return [term, amount];
      }));
    }

    function render() {
      const {used, capacity, ratio, state, historical, limitKind, chartTotal, inputLimit, modelWindow, breakdown, ringRatio} = current;
      const chartAvailable = chartTotal > 0;
      const percentage = percent(ratio);
      const basis = LIMIT_LABEL[limitKind] || '';
      drawer.dataset.level = state;
      trigger.dataset.level = ringRatio == null ? 'unknown' : ringRatio >= .93 ? 'critical' : ringRatio >= .75 ? 'warning' : 'healthy';
      tree.hidden = !chartAvailable || !treeMode.checked;
      grid.hidden = !chartAvailable || !gridMode.checked;
      gridMode.disabled = treeMode.disabled = !chartAvailable;
      headerArc.setAttribute('stroke-dasharray', `${Math.min(ringRatio ?? 0, 1) * 100} 100`);
      el('ctxTriggerPct').textContent = ringRatio == null ? short(used) : percent(ringRatio);
      el('ctxUsed').textContent = short(used) + (used == null || capacity == null ? '' : ' / ' + short(capacity));
      el('ctxUsed').title = used == null ? '' : `${used.toLocaleString()}${capacity == null ? '' : ' / ' + capacity.toLocaleString()} tokens`;
      el('ctxStatus').textContent = ratio == null ? '' : percentage;
      el('ctxStatus').hidden = ratio == null;
      el('ctxValueNote').textContent = used == null ? '占用量暂不可用' : historical ? '历史估算 · 容量未知'
        : '估算输入 · ' + (basis || '模型窗口与应用上限均未知');
      const notes = [];
      if (chartAvailable && ratio == null) notes.push('仅显示已估算输入的构成，不表示窗口占用率。');
      if (breakdown === 'partial') notes.push('分类统计不完整。');
      if (breakdown === 'unavailable' && chartAvailable && used > 0) notes.push('本次未记录分类。');
      if (breakdown === 'inconsistent') notes.push('分类合计与输入总数不一致，未绘制比例。');
      if (ratio > 1) notes.push('估算输入超过显示上限，图示按估算输入构成填满。');
      el('ctxChartNote').textContent = notes.join(' ');
      el('ctxChartNote').hidden = !notes.length;
      const extra = [current.model, current.scope === 'previous' ? '上一调用' : current.scope === 'active' ? '当前调用' : '',
        current.history, current.estimateMethod === 'character_heuristic' ? '字符估算' : '',
        limitKind === 'input' ? '模型窗口未知' : modelWindow != null && inputLimit != null ? '应用单次输入上限 ' + short(inputLimit) : '',
        current.outputReserve != null ? '输出预留 ' + short(current.outputReserve) : ''].filter(Boolean);
      metadata.textContent = extra.join(' · ');
      metadata.hidden = !extra.length;
      trigger.setAttribute('aria-label', used == null ? '上下文用量' : `估算输入 ${short(used)}${ringRatio == null ? '，应用上限比例未知' : `，占应用单次输入上限 ${percent(ringRatio)}`}`);
      trigger.title = trigger.getAttribute('aria-label');
      renderCharts();
      renderLegend();
      renderUsage(current.usage);
    }

    function setOpen(open, focus = true) {
      drawer.classList.toggle('open', open);
      backdrop.classList.toggle('open', open);
      trigger.setAttribute('aria-expanded', String(open));
      drawer.inert = !open;
      if (focus) (open ? close : trigger).focus({preventScroll: true});
    }

    treeMode.addEventListener('change', render);
    gridMode.addEventListener('change', render);
    doc.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.defaultPrevented || drawer.inert) return;
      if (doc.activeElement && !drawer.contains(doc.activeElement) && doc.activeElement !== trigger) return;
      event.preventDefault(); setOpen(false);
    });
    render();
    return {update(snapshot) {
      const next = project(snapshot);
      if (!snapshot || next.session !== current.session || next.request !== current.request) {
        selected = ''; usageBlock.open = false;
      }
      current = next;
      render();
    }, close() { setOpen(false, false); }, toggle() { setOpen(drawer.inert); }};
  }
  return { project, short, percent, gridSlots, treemap, create };
});
