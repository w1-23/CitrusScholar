(function (root) {
  'use strict';
  var sceneDetails = {
    'candidate-search': ['查找候选文献', '查找与主题相关的文献供筛选。', 'search'],
    'research-question-answering': ['科研检索', '检索相关资料，基于证据回答科研问题。', 'search'],
    'reference-verification': ['文献核验', '核对参考文献，标明可确认与存疑的条目。', 'book'],
    'literature-verification': ['文献核验', '核对参考文献，标明可确认与存疑的条目。', 'book'],
    'research-integrity': ['科研诚信', '检查图表、数据与引用中的一致性问题。', 'shield'],
    'data-audit': ['数据审查', '检查数据表的缺失、重复和异常。', 'database'],
    'nature-figure': ['科研绘图', '根据数据绘制或调整科研图表。', 'chart']
  };
  // Keep native disclosure keyboard behavior. Only one anchored panel may open.
  function bindComposerMenus(menus) {
    if (!menus.length) return;
    menus.forEach(function (menu) {
      menu.addEventListener('toggle', function () {
        if (menu.open) menus.forEach(function (other) { if (other !== menu) other.open = false; });
      });
      menu.addEventListener('keydown', function (event) {
        if (!menu.open || event.key !== 'Escape') return;
        event.preventDefault();
        menu.open = false;
        menu.querySelector('summary').focus({ preventScroll: true });
      });
    });
    function closeOutside(event) {
      menus.forEach(function (menu) {
        if (menu.open && !menu.contains(event.target)) menu.open = false;
      });
    }
    document.addEventListener('pointerdown', closeOutside);
    document.addEventListener('focusin', closeOutside);
  }

  function fitScenePanel(menu) {
    if (!menu) return;
    var panel = menu.querySelector('#skill-picker-panel');
    var composer = menu.closest('.input-box'), header = document.querySelector('.app-header');
    var viewport = window.visualViewport;
    function fit() {
      if (!menu.open) return;
      var zoom = panel.currentCSSZoom || Number(getComputedStyle(panel).getPropertyValue('--ui-scale')) || 1;
      var floor = Math.max(header ? header.getBoundingClientRect().bottom : 0, viewport ? viewport.offsetTop : 0);
      var room = (panel.getBoundingClientRect().bottom - floor) / zoom - 8;
      panel.style.setProperty('--scene-panel-room', Math.max(0, Math.floor(room)) + 'px');
    }
    menu.addEventListener('toggle', function () {
      if (!menu.open) return;
      fit();
      panel.scrollTop = 0;
    });
    window.addEventListener('resize', fit);
    if (viewport) viewport.addEventListener('resize', fit);
    if (typeof ResizeObserver === 'function') {
      var observer = new ResizeObserver(fit);
      if (composer) observer.observe(composer);
      if (header) observer.observe(header);
    }
  }

  function create(options) {
    var selected = [], catalog = [], generation = 0, revision = 0, catalogKnown = false;
    var list = options.list, status = options.status, summary = options.summary;
    function setChoice(item, on) {
      var other = selected.filter(function (pick) { return pick.id !== item.id; });
      if (on && other.length >= 4) return false;
      selected = other;
      if (on) selected.push(identity(item));
      revision++;
      drawSummary();
      return true;
    }
    bindComposerMenus([options.attachmentMenu, options.menu].filter(Boolean));
    fitScenePanel(options.menu);
    function same(a, b) { return a.id === b.id && a.version === b.version && a.sha256 === b.sha256; }
    function identity(item) { return {id:item.id, version:item.version, sha256:item.sha256}; }
    function details(item) {
      return Object.prototype.hasOwnProperty.call(sceneDetails, item.id) ? sceneDetails[item.id] :
        [item.id, item.description, 'file'];
    }
    function title(item) { return details(item)[0]; }
    function pruneRetired() {
      if (!catalogKnown) return false;
      var before = selected.length;
      selected = selected.filter(function (pick) { return catalog.some(function (item) { return same(pick, item); }); });
      if (before === selected.length) return false;
      revision++; return true;
    }
    function draw() {
      list.replaceChildren();
      catalog.forEach(function (item) {
        var row = details(item);
        var label = document.createElement('label'); label.className = 'skill-choice';
        var check = document.createElement('input'); check.type = 'checkbox';
        check.setAttribute('data-skill-id', item.id);
        check.checked = selected.some(function (pick) { return same(pick, item); });
        check.addEventListener('change', function () {
          if (!options.scope()) { reset(); return; }
          if (!setChoice(item, check.checked)) { check.checked = false; status.textContent = '每轮最多选择 4 项场景。'; }
          else if (status.textContent === '每轮最多选择 4 项场景。') status.textContent = '选择适合本轮任务的场景。';
        });
        var icon = document.createElement('span'); icon.className = 'skill-choice-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.innerHTML = window.uiIcon(row[2]);
        var text = document.createElement('span'), name = document.createElement('strong'), description = document.createElement('small');
        text.className = 'skill-choice-copy';
        name.id = 'scene-name-' + item.id;
        description.id = 'scene-description-' + item.id;
        check.setAttribute('aria-labelledby', name.id);
        check.setAttribute('aria-describedby', description.id);
        name.textContent = row[0];
        description.textContent = row[1];
        text.appendChild(name); text.appendChild(description); label.appendChild(check); label.appendChild(icon); label.appendChild(text); list.appendChild(label);
      });
      drawSummary();
    }
    function drawSummary() {
      summary.textContent = selected.length ? '本轮场景：' + selected.map(title).join('、') : '';
      summary.hidden = !selected.length;
    }
    async function load() {
      var scope = options.scope();
      if (!scope) { status.textContent = '登录后可选择场景。'; return; }
      // A new refresh supersedes the old request, including across sessions.
      var ticket = ++generation; status.textContent = '正在读取场景目录…';
      try {
        var response = await options.fetch('/api/v2/skills');
        if (!response.ok) throw new Error('catalog_unavailable');
        var body = await response.json();
        if (ticket !== generation || scope !== options.scope()) return;
        if (!body || !Array.isArray(body.skills) || body.skills.length > 100) throw new Error('invalid_catalog');
        var seen = Object.create(null);
        catalog = body.skills.filter(function (item) {
          if (!item || typeof item.id !== 'string' || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(item.id) || seen[item.id] ||
              typeof item.version !== 'string' || !item.version || item.version.length > 100 ||
              typeof item.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(item.sha256) ||
              typeof item.description !== 'string' || item.description.length > 10000) return false;
          seen[item.id] = true; return true;
        });
        catalogKnown = true;
        status.textContent = pruneRetired() ? '部分场景已更新，已移除旧选择；请重新选择。' :
          catalog.length ? '选择适合本轮任务的场景。' : '当前没有可选场景；仍可直接提问。';
        draw();
      } catch (_) {
        if (ticket !== generation || scope !== options.scope()) return;
        status.textContent = '场景目录暂不可用，可重试或直接提问。';
      }
    }
    function reset() {
      generation++; revision++; selected = []; catalog = []; catalogKnown = false;
      list.replaceChildren(); status.textContent = ''; drawSummary();
      if (options.menu) options.menu.open = false;
      if (options.attachmentMenu) options.attachmentMenu.open = false;
    }
    if (options.menu) options.menu.addEventListener('toggle', function () { if (options.menu.open) load(); });
    if (options.retry) options.retry.addEventListener('click', load);
    return {load:load, reset:reset,
      restore:function (items) {
        selected = items.map(identity); revision++;
        if (pruneRetired()) status.textContent = '部分场景已更新，已移除旧选择；请重新选择。';
        draw();
      },
      capture:function () { return {items:selected.map(identity), revision:revision, scope:options.scope()}; },
      acknowledge:function (snapshot) {
        if (snapshot.scope === options.scope() && snapshot.revision === revision) {
          selected = []; revision++; draw(); if (options.menu) options.menu.open = false;
        }
      }};
  }
  root.CitrusSkillPicker = {create:create};
})(window);
