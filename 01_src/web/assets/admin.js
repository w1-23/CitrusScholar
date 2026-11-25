(function () {
  'use strict';
  // BEGIN ADMIN INVITE CONTRACT（纯函数；状态标签与请求构造，不读写 DOM）
  var CitrusAdminContract = (function () {
    var INVITE_STATES = ['issued', 'claimed', 'revoked', 'expired'];
    var INVITE_LABELS = {issued: '已签发', claimed: '已使用', revoked: '已撤销', expired: '已过期'};
    var APPLICATION_STATES = ['pending', 'approved', 'rejected'];
    var APPLICATION_LABELS = {pending: '待审核', approved: '已通过', rejected: '已拒绝'};
    function clampLimit(limit) {
      var value = Number(limit);
      if (!Number.isFinite(value)) return 50;
      value = Math.trunc(value);
      return value < 1 ? 1 : value > 100 ? 100 : value;
    }
    function pageQuery(status, limit, offset) {
      var query = '?limit=' + clampLimit(limit) + '&offset=' + Math.max(0, Math.trunc(Number(offset) || 0));
      if (status && APPLICATION_STATES.indexOf(status) >= 0) query += '&status=' + encodeURIComponent(status);
      return query;
    }
    function invitesQuery(limit, offset) {
      return '?limit=' + clampLimit(limit) + '&offset=' + Math.max(0, Math.trunc(Number(offset) || 0));
    }
    function inviteState(status) {
      var key = INVITE_STATES.indexOf(status) >= 0 ? status : 'issued';
      return {key: key, label: INVITE_LABELS[key], canRevoke: key === 'issued'};
    }
    function applicationState(status) {
      var key = APPLICATION_STATES.indexOf(status) >= 0 ? status : 'pending';
      return {key: key, label: APPLICATION_LABELS[key], canReview: key === 'pending'};
    }
    function expiresInDays(value) {
      var days = Number(value);
      if (value === '' || value == null || !Number.isFinite(days)) return undefined;
      days = Math.trunc(days);
      return days >= 1 && days <= 30 ? days : undefined;
    }
    function buildInviteBody(value) {
      var days = expiresInDays(value);
      return days === undefined ? {} : {expires_in_days: days};
    }
    function decisionBody(decision, reason) {
      if (decision !== 'approve' && decision !== 'reject') return null;
      if (decision === 'approve') return {};
      var text = String(reason == null ? '' : reason).trim();
      if (Array.from(text).length > 300) text = Array.from(text).slice(0, 300).join('');
      return text ? {reason: text} : {};
    }
    function decisionUrl(id, decision) {
      if (decision !== 'approve' && decision !== 'reject') return '';
      return '/api/v2/admin/applications/' + encodeURIComponent(String(id)) + '/' + decision;
    }
    function resetBody(currentPassword) {
      return {current_password: String(currentPassword || '')};
    }
    return {
      INVITE_STATES: INVITE_STATES,
      INVITE_LABELS: INVITE_LABELS,
      APPLICATION_STATES: APPLICATION_STATES,
      APPLICATION_LABELS: APPLICATION_LABELS,
      clampLimit: clampLimit,
      pageQuery: pageQuery,
      invitesQuery: invitesQuery,
      inviteState: inviteState,
      applicationState: applicationState,
      expiresInDays: expiresInDays,
      buildInviteBody: buildInviteBody,
      decisionBody: decisionBody,
      decisionUrl: decisionUrl,
      resetBody: resetBody
    };
  }());
  if (typeof window !== 'undefined') window.CitrusAdminContract = CitrusAdminContract;
  // END ADMIN INVITE CONTRACT
  var pageSize = 25;
  var offset = 0;
  var toastTimer = null;
  var appStatus = 'pending';
  var appOffset = 0;
  var appPageSize = 50;
  var inviteOffset = 0;
  var invitePageSize = 50;
  var oneTimeInviteCode = '';
  var oneTimeAdminSecret = '';
  var pendingResetUserId = '';
  var pendingResetName = '';
  var resetGeneration = 0;
  var panelGenerations = Object.create(null);
  var accountMutations = new Map(), applicationMutations = new Map();
  var applicationView = 0;
  var overview = {queue: null, config: null, providers: []};
  var deviceMutation = false, deviceEditGeneration = 0, deviceDirty = false, committedDevice = null;
  function nextPanel(panel) {
    panelGenerations[panel] = (panelGenerations[panel] || 0) + 1;
    return panelGenerations[panel];
  }
  async function loadPanel(panel, request, render, matches) {
    var generation = nextPanel(panel);
    var isCurrent = function () { return generation === panelGenerations[panel] && (!matches || matches()); };
    try {
      var data = await request();
      if (isCurrent()) render(data);
    } catch (error) { if (isCurrent()) throw error; }
  }
  async function loadPanels(loaders) {
    var results = await Promise.allSettled(loaders.map(function (load) { return load(); }));
    var errors = results.filter(function (result) { return result.status === 'rejected'; });
    if (errors.length) throw new Error(errors.map(function (entry) { return entry.reason.message; }).join(' · '));
  }
  function el(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]);
    });
  }
  function notify(message) {
    var box = el('toast'); if (!box) return;
    box.textContent = message; box.classList.add('visible');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { box.classList.remove('visible'); }, 3000);
  }
  async function jsonRequest(url, options) {
    var response = await fetch(url, options || {credentials:'same-origin'});
    var data = await response.json().catch(function () { return {}; });
    if (response.status === 401) { location.replace('/?next=%2Fadmin'); throw new Error('登录已失效'); }
    if (response.status === 403) throw new Error('仅管理员可访问');
    if (!response.ok) {
      var error = new Error(data.detail || '请求失败');
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function renderUsers(users) {
    var body = el('usersTable');
    if (!users.length) { body.innerHTML = '<tr><td colspan="4" class="muted">暂无账号</td></tr>'; return; }
    body.innerHTML = users.map(function (user) {
      var state = user.active ? '<span class="state active">启用</span>' : '<span class="state inactive">停用</span>';
      var actions = user.role !== 'admin'
        ? '<button class="small-button" data-action="reset" data-id="' + user.user_id + '" data-name="' + esc(user.username) + '">重置密码</button>'
        : '<span class="muted">本人改密</span>';
      if (user.role !== 'admin') {
        actions += '<button class="small-button" data-action="limit" data-id="' + user.user_id + '" data-limit="' + user.daily_limit + '">设置限额</button>';
        actions += '<button class="small-button danger" data-action="active" data-id="' + user.user_id + '" data-active="' + (!user.active) + '">' + (user.active ? '停用' : '启用') + '</button>';
      }
      return '<tr><td><strong>' + esc(user.username) + '</strong><small>' + esc(user.role) + '</small></td><td>' + state + '</td>' +
        '<td>' + esc(user.requests_today) + ' / ' + esc(user.daily_limit) + '</td><td class="actions">' + actions + '</td></tr>';
    }).join('');
  }
  function renderAudit(items) {
    var host = el('auditList');
    if (!items.length) { host.innerHTML = '<p class="muted">没有匹配的请求记录</p>'; return; }
    host.innerHTML = items.map(function (item) {
      var tools = (item.tools || []).map(function (tool) { return tool.name || tool.tool || ''; }).filter(Boolean).join('、') || '无';
      var attachments = (item.attachments || []).map(function (file) {
        var kb = Math.max(1, Math.round((file.approx_bytes || 0) / 1024));
        return esc(file.name) + '（' + kb + ' KB）';
      }).join('、') || '无';
      var when = item.created_at ? new Date(item.created_at).toLocaleString() : '时间未知';
      return '<details class="audit-item"><summary><span class="audit-user">' + esc(item.username) + '</span><span class="audit-status">' + esc(item.status) + '</span><time>' + esc(when) + '</time><span class="audit-question">' + esc(item.query) + '</span></summary>' +
        '<div class="audit-content"><p><b>问题</b><span>' + esc(item.query) + '</span></p>' +
        '<p><b>回答</b><span>' + esc(item.answer || '尚未完成') + '</span></p>' +
        '<p><b>工具</b><span>' + esc(tools) + '</span></p>' +
        '<p><b>附件</b><span>' + attachments + '</span></p>' +
        '<p><b>会话与耗时</b><span>' + esc(item.session_id) + ' · ' + esc(item.elapsed_ms == null ? '—' : item.elapsed_ms + ' ms') + '</span></p></div></details>';
    }).join('');
  }
  function renderServices() {
    var configured = overview.providers.filter(function (provider) { return provider.configured; });
    var web = overview.config && overview.config.web_search;
    var webName = web && web.provider === 'zhipu_web_search' ? '智谱 Web Search' : 'DeepSeek Search/Fetch';
    var queue = overview.queue;
    el('serviceSummary').innerHTML =
      (overview.config ? '<span class="service-chip ' + (configured.length ? 'ok' : 'warn') + '">主模型服务 ' + (configured.length ? '已配置' : '未配置') + '</span>' +
        '<span class="service-chip ' + (web && web.configured ? 'ok' : 'warn') + '">' + esc(webName) + (web && web.configured ? ' 已配置' : ' 未配置') + '</span>' : '') +
      (queue ? '<span class="service-chip neutral">处理 ' + esc(queue.active) + ' · 排队 ' + esc(queue.queued) + '</span>' : '');
  }
  function loadUsers() {
    return loadPanel('users', function () { return jsonRequest('/api/v2/admin/users'); }, function (data) {
      var users = data.users || [];
      el('userCount').textContent = users.length;
      el('requestCount').textContent = users.reduce(function (sum, user) { return sum + (Number(user.requests_today) || 0); }, 0);
      renderUsers(users);
    });
  }
  function loadQueue() {
    return loadPanel('queue', function () { return jsonRequest('/api/v2/admin/queue'); }, function (queue) {
      overview.queue = queue;
      el('activeCount').textContent = queue.active + ' / ' + queue.max_active;
      el('queuedCount').textContent = queue.queued + ' / ' + queue.max_pending;
      renderServices();
    });
  }
  function loadModel() {
    return loadPanel('model', function () {
      return Promise.all([jsonRequest('/api/v2/config'), jsonRequest('/api/v2/config/providers')]);
    }, function (data) {
      var config = overview.config = data[0], providers = overview.providers = data[1].providers || [];
      var activeProvider = providers.find(function (provider) { return provider.id === config.model.provider_id; });
      el('modelStatus').textContent = activeProvider
        ? '主模型：' + activeProvider.name + ' · ' + config.model.main + ' · ' + (config.model.image_input ? '支持图像输入' : '不支持图像输入')
        : '主模型：' + config.model.main;
      renderServices();
    });
  }
  function loadDevice() {
    if (deviceMutation) return;
    return loadPanel('device', function () { return jsonRequest('/api/v2/admin/onnx/device'); },
      function (device) { renderDevice(device, deviceDirty); });
  }
  function renderDevice(device, preserveSelection) {
    if (!device) return;
    committedDevice = device;
    if (!preserveSelection) el('onnxDeviceMode').value = device.mode || 'auto';
    var active = device.selected_providers || [];
    el('onnxDeviceStatus').textContent = '模式：' + (device.mode || 'auto') +
       ' · 当前选择：' + (active.join(' → ') || '未验证') + (device.gpu_available ? ' · 已检测到 GPU' : ' · 未检测到 GPU') +
      (device.selection_error ? ' · ' + device.selection_error : '');
    var activeModels = device.active_model_providers || {};
    el('onnxProviders').innerHTML = (device.available_providers || []).map(function (provider) {
      return '<span class="service-chip neutral">' + esc(provider) + '</span>';
    }).join('') + Object.keys(activeModels).map(function (name) {
      return '<span class="service-chip ok">' + esc(name) + '：' + esc((activeModels[name] || []).join(' → ')) + '</span>';
    }).join('');
  }
  function loadAudit() {
    var query = (el('auditQuery').value || '').trim(), page = offset;
    return loadPanel('audit', function () {
      return jsonRequest('/api/v2/admin/requests?limit=' + pageSize + '&offset=' + page + '&q=' + encodeURIComponent(query));
    }, function (data) {
      renderAudit(data.items || []);
      var total = Number(data.total) || 0;
      el('auditPageLabel').textContent = total ? (page + 1) + '–' + Math.min(page + pageSize, total) + ' / ' + total : '0 条';
      el('olderButton').disabled = page + pageSize >= total;
      el('newerButton').disabled = page <= 0;
    }, function () { return page === offset && query === (el('auditQuery').value || '').trim(); });
  }
  async function refresh() {
    el('refreshButton').disabled = true;
    var loaders = [loadUsers, loadQueue, loadModel, loadDevice, loadAudit, loadApplications, loadInvites];
    if (el('corpusList')) loaders.push(loadCorpus);
    try { await loadPanels(loaders); }
    catch (error) { notify(error.message); }
    finally { el('refreshButton').disabled = false; }
  }
  async function handleUserAction(event) {
    var button = event.target.closest('button[data-action]'); if (!button) return;
    var id = button.dataset.id, action = button.dataset.action;
    if (action === 'reset') { openResetDialog(id, button.dataset.name); return; }
    var marker = id + ':' + action;
    if (accountMutations.has(marker)) return;
    var options;
    if (action === 'active') {
      options = {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:button.dataset.active === 'true'})};
    } else if (action === 'limit') {
      var value = Number(window.prompt('每日请求上限（1–10000）', button.dataset.limit));
      if (!Number.isInteger(value) || value < 1 || value > 10000) return;
      options = {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({daily_limit:value})};
    } else return;
    var token = {};
    accountMutations.set(marker, token);
    button.disabled = true;
    nextPanel('users');
    try {
      await jsonRequest('/api/v2/admin/users/' + encodeURIComponent(id) + (action === 'active' ? '/active' : '/daily-limit'), options);
      if (accountMutations.get(marker) !== token) return;
      await loadUsers();
      if (accountMutations.get(marker) === token) notify('账号已更新');
    } catch (error) { if (accountMutations.get(marker) === token) notify(error.message); }
    finally {
      if (accountMutations.get(marker) === token) accountMutations.delete(marker);
      button.disabled = false;
    }
  }
  async function applyOnnxDevice() {
    if (deviceMutation) return;
    var button = el('onnxDeviceApply'), select = el('onnxDeviceMode'), mode = select.value;
    var edit = deviceEditGeneration;
    deviceMutation = button.disabled = true;
    var requestGeneration = nextPanel('device');
    el('onnxDeviceStatus').textContent = '正在验证所选设备上的两个推理模型…';
    try {
      var data = await jsonRequest('/api/v2/admin/onnx/device', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: mode})
      });
      if (requestGeneration !== panelGenerations.device) return;
      deviceDirty = edit !== deviceEditGeneration && select.value !== data.mode;
      renderDevice(data, deviceDirty);
      notify(data.note || '推理设备已切换');
    } catch (error) {
      if (requestGeneration !== panelGenerations.device) return;
      if (committedDevice && edit === deviceEditGeneration) {
        deviceDirty = false;
        renderDevice(committedDevice, false);
      }
      el('onnxDeviceStatus').textContent = '切换未确认：' + error.message;
      notify(error.message);
    } finally { deviceMutation = button.disabled = false; }
  }
  // ---------------------------------------------------------------- 文献候审库
  function corpusBadge(kind) {
    var map = {fetch_paper: '问答抓取', upload: '用户上传'};
    return '<span class="corpus-badge">' + esc(map[kind] || kind || '其他') + '</span>';
  }
  function corpusFilter() {
    var select = el('corpusStatus');
    return (select && select.value) || 'pending';
  }
  var corpusBefore = null;
  var corpusNext = null;
  var corpusSupplementId = null;
  var corpusLegacyMutation = false;
  var corpusOriginalRequest = null;
  function corpusCandidateUrl(id) {
    return '/api/v2/admin/corpus/candidates/' + encodeURIComponent(id);
  }
  var preparationLabels = {
    queued: '等待索引准备', queue_full: '准备队列已满，未排队', source_too_large: '已归档，超过 8 MiB 索引准备上限',
    not_queued: '已归档，未排队', prepared: '已准备', needs_review: '等待授权审核',
    awaiting_build: '等待共享索引构建', build_pending: '共享索引构建已排队', indexed_ready: '等待索引切换',
    active: '已进入共享索引', failed: '索引准备失败', expired: '索引准备已过期', revoked: '共享授权已撤销',
    scheduling_unavailable: '已归档，暂时无法排队', disabled: '共享索引未启用'
  };
  function renderCorpusCandidates(data) {
    var host = el('corpusList');
    var counts = data.counts || {};
    el('corpusSummary').textContent = '待审 ' + (counts.pending || 0) + ' 篇 · 已保留 ' +
      (counts.approved || 0) + ' 篇 · 已拒绝 ' + (counts.rejected || 0) +
      ' 篇';
    corpusLegacyMutation = data.legacy_mutation_enabled === true;
    el('corpusModeHelp').textContent = corpusLegacyMutation
      ? '查看抓取原文与共享上传资料。未取得全文的条目可在这里补录。'
      : '历史候审记录仅供查看。未取得全文的条目只显示书目信息。';
    el('corpusMissingOption').textContent = corpusLegacyMutation ? '待补全文' : '未取得全文';
    var capacity = data.shared_capacity;
    if (capacity) {
      var notices = {
        index_document_limit_near: '接近索引容量上限', index_document_limit_reached: '已达到索引容量上限',
        index_document_limit_exceeded: '授权文献超限，无法构建新索引', preparation_queue_full: '有文献因准备队列容量等待重试'
      };
      el('corpusSummary').textContent += ' · 索引授权 ' + capacity.authorized_documents + '/' + capacity.index_document_limit +
        ' · 待准备 ' + capacity.preparation_queued + '/' + capacity.preparation_limit +
        ' · 联网归档 ' + (capacity.archive_byte_limit / 1048576) + ' MiB · 索引准备 ' + (capacity.preparation_byte_limit / 1048576) + ' MiB' +
        (capacity.preparation_deferred ? ' · 未排入 ' + capacity.preparation_deferred : '') +
        capacity.alerts.map(function (alert) { return ' · ' + (notices[alert] || alert); }).join('');
    }
    corpusNext = data.next_before_id;
    el('corpusPager').hidden = corpusFilter() !== 'fulltext_missing' || (!corpusBefore && !corpusNext);
    el('corpusNewest').disabled = !corpusBefore;
    el('corpusOlder').disabled = !corpusNext;
    var items = data.candidates || [];
    if (!items.length) { host.innerHTML = '<p class="muted">当前没有符合条件的资料</p>'; return; }
    host.innerHTML = items.map(function (item) {
      var id = esc(item.id);
      var missing = item.archive_state === 'fulltext_missing';
      var rawAvailable = item.raw_status === 'available';
      var actions = rawAvailable
        ? '<button class="small-button" data-corpus="open" data-id="' + id + '">查看原件</button>' : '';
      if (corpusLegacyMutation) {
        if (missing && item.status === 'pending') {
          actions += '<button class="small-button" data-corpus="fulltext" data-id="' + id + '">补录全文</button>';
        } else if (item.status === 'pending') {
          actions += '<button class="small-button" data-corpus="approve" data-id="' + id + '">保留</button>';
        }
        if (item.status === 'pending' || item.status === 'approved') {
          actions += '<button class="small-button danger" data-corpus="reject" data-id="' + id + '">' +
            (item.status === 'approved' ? '撤下' : '拒绝') + '</button>';
        } else {
          actions += '<button class="small-button" data-corpus="reopen" data-id="' + id + '">重审</button>';
        }
      }
      var doi = item.doi ? '<span class="corpus-doi">' + esc(item.doi) + '</span>' : '<span class="muted">无 DOI</span>';
      var presence = missing ? '未取得全文 · 仅有元数据' : rawAvailable ? '已归档 · 原件可用' : '原件不可用';
      var format = presence;
      if (!missing) {
        if (item.raw_format) format += ' · ' + item.raw_format;
        if (item.conversion && item.conversion.indexOf('none') !== 0) format += ' · ' + item.conversion;
        if (item.origin === 'admin_fulltext') format += ' · 已补录全文';
      }
      var bytes = Number(item.raw_bytes || 0);
      var pmid = item.pmid ? ' · PMID ' + esc(item.pmid) : '';
      var source = missing && /^https?:\/\//i.test(item.url || '')
        ? ' · <a href="' + esc(item.url) + '" target="_blank" rel="noopener noreferrer">文献来源</a>' : '';
      if (preparationLabels[item.preparation_status]) format += ' · ' + preparationLabels[item.preparation_status];
      return '<article class="corpus-item"><div class="corpus-main"><strong>' + esc(item.title || '(无标题)') + '</strong>' +
        '<small>' + doi + pmid + source + ' · ' + corpusBadge(item.source_kind) + ' · ' + esc(format) +
        ' · ' + (bytes ? (bytes / 1024).toFixed(1) + ' KB' : '—') +
        ' · 频次 ' + esc(item.frequency) + ' · ' + esc(item.status) + '</small></div>' +
        '<div class="actions">' + actions + '</div></article>';
    }).join('');
  }
  async function loadCorpus() {
    var status = corpusFilter();
    var before = status === 'fulltext_missing' && corpusBefore;
    var query = '?status=' + encodeURIComponent(status) + '&limit=50';
    if (status === 'fulltext_missing') query += '&order=newest';
    if (before) query += '&before_id=' + encodeURIComponent(before);
    return loadPanel('corpus', function () { return jsonRequest('/api/v2/admin/corpus/candidates' + query); },
      renderCorpusCandidates, function () { return status === corpusFilter() && before === (status === 'fulltext_missing' && corpusBefore); });
  }
  function resetCorpusOriginal() {
    if (corpusOriginalRequest) corpusOriginalRequest.abort();
    corpusOriginalRequest = null;
    el('corpusOriginalText').hidden = true;
    el('corpusOriginalText').textContent = '';
    ['corpusOriginalDownload', 'corpusOriginalPdf'].forEach(function (id) {
      el(id).hidden = true;
      el(id).removeAttribute('href');
    });
  }
  async function openCorpusOriginal(button) {
    resetCorpusOriginal();
    var request = new AbortController();
    corpusOriginalRequest = request;
    var url = corpusCandidateUrl(button.dataset.id) + '/raw';
    var dialog = el('corpusOriginalDialog');
    var status = el('corpusOriginalStatus');
    el('corpusOriginalPaper').textContent = button.closest('article').querySelector('strong').textContent;
    status.textContent = '正在读取归档原件…';
    if (!dialog.open) dialog.showModal();
    try {
      var response = await fetch(url, {credentials:'same-origin', cache:'no-store', signal:request.signal});
      if (request.signal.aborted) return;
      if (response.status === 401) { location.replace('/?next=%2Fadmin'); return; }
      if (!response.ok) {
        status.textContent = response.status === 404 || response.status === 410
          ? '原件已不可用。可关闭此窗口并刷新候审列表。'
          : '无法读取归档原件，请稍后重试。';
        return;
      }
      var mime = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      var isText = ['text/plain', 'text/markdown', 'text/csv'].indexOf(mime) !== -1;
      if (response.redirected || mime === 'application/json' || mime === 'text/html' || !mime) {
        status.textContent = '未收到原件，请刷新候审列表后重试。';
        return;
      }
      if (isText) {
        var text = await response.text();
        if (request.signal.aborted) return;
        el('corpusOriginalText').textContent = text;
        el('corpusOriginalText').hidden = false;
        status.textContent = '正在显示归档文件的原始文字。';
      } else {
        if (response.body) response.body.cancel().catch(function () {});
        status.textContent = mime === 'application/pdf'
          ? '可在新标签页查看 PDF，或下载原件。' : '该格式不支持页内预览，可下载原件后打开。';
        if (mime === 'application/pdf') {
          el('corpusOriginalPdf').href = url;
          el('corpusOriginalPdf').hidden = false;
        }
      }
      el('corpusOriginalDownload').href = url + '?download=true';
      el('corpusOriginalDownload').hidden = false;
    } catch (_error) {
      if (!request.signal.aborted) status.textContent = '无法读取归档原件，请稍后重试。';
    }
  }
  async function handleCorpusAction(event) {
    var button = event.target.closest('button[data-corpus]');
    if (!button) return;
    try {
      var action = button.dataset.corpus;
      if (action === 'open') { await openCorpusOriginal(button); return; }
      if (!corpusLegacyMutation) return;
      if (action === 'fulltext') {
        corpusSupplementId = button.dataset.id;
        el('corpusFulltextForm').reset();
        el('corpusFulltextError').textContent = '';
        el('corpusFulltextPaper').textContent = button.closest('article').querySelector('strong').textContent;
        el('corpusFulltextDialog').showModal();
        return;
      }
      if (action === 'reject' && !confirm('不再保留这篇资料？之后仍可重审。')) return;
      await jsonRequest(corpusCandidateUrl(button.dataset.id) + '/' + action,
        {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({note:''})});
      notify('已更新保留状态');
      await loadCorpus();
    } catch (error) { notify(error.message); }
  }
  async function submitCorpusFulltext(event) {
    event.preventDefault();
    var button = el('corpusFulltextSubmit');
    var file = el('corpusFulltextFile').files[0];
    var errorBox = el('corpusFulltextError');
    if (!corpusLegacyMutation || !file || button.disabled) return;
    errorBox.textContent = '';
    button.disabled = true;
    el('corpusFulltextCancel').disabled = true;
    try {
      var result = await jsonRequest(corpusCandidateUrl(corpusSupplementId) + '/fulltext',
        {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:file});
      el('corpusFulltextDialog').close();
      var preparation = preparationLabels[result.candidate.preparation_status];
      notify('已保存全文' + (preparation ? '，' + preparation : ''));
      await loadCorpus();
    } catch (error) {
      if (el('corpusFulltextDialog').open) errorBox.textContent = error.message;
      else notify(error.message);
    } finally {
      button.disabled = false;
      el('corpusFulltextCancel').disabled = false;
    }
  }
  // ---------------------------------------------------------------- 注册申请
  function formatWhen(value) {
    return value ? new Date(value).toLocaleString() : '—';
  }
  function renderApplications(data) {
    var body = el('applicationsTable');
    var items = data.applications || [];
    var next = data.next_offset;
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="5" class="muted">当前筛选没有注册申请。</td></tr>';
    } else {
      body.innerHTML = items.map(function (application) {
        var meta = CitrusAdminContract.applicationState(application.status);
        var reviewed = application.reviewed_at ? formatWhen(application.reviewed_at) : '—';
        var reason = application.rejection_reason
          ? '<small>拒绝理由：' + esc(application.rejection_reason) + '</small>' : '';
        var actions = meta.canReview
          ? '<button class="small-button" data-app="approve" data-id="' + esc(application.application_id) + '">通过</button>' +
            '<button class="small-button danger" data-app="reject" data-id="' + esc(application.application_id) + '">拒绝</button>'
          : '<span class="muted">已处理</span>';
        return '<tr><td><strong>' + esc(application.username) + '</strong><small>头像 ' + esc(application.avatar_preset || '01') + '</small></td>' +
          '<td><span class="state app-' + meta.key + '">' + esc(meta.label) + '</span></td>' +
          '<td>' + esc(formatWhen(application.submitted_at)) + '</td>' +
          '<td>' + esc(reviewed) + reason + '</td>' +
          '<td class="actions">' + actions + '</td></tr>';
      }).join('');
    }
    var label = '第 ' + (Math.floor(appOffset / appPageSize) + 1) + ' 页 · 本页 ' + items.length + ' 条';
    el('applicationsPageLabel').textContent = label;
    el('applicationsNewer').disabled = appOffset <= 0;
    el('applicationsOlder').disabled = next == null;
    el('applicationsSummary').textContent = label + ' · 状态：' + CitrusAdminContract.applicationState(appStatus).label;
  }
  function loadApplications() {
    var status = appStatus, page = appOffset;
    return loadPanel('applications', function () {
      return jsonRequest('/api/v2/admin/applications' + CitrusAdminContract.pageQuery(status, appPageSize, page));
    }, renderApplications, function () { return status === appStatus && page === appOffset; });
  }
  async function handleApplicationAction(event) {
    var button = event.target.closest('button[data-app]');
    if (!button) return;
    var id = button.dataset.id;
    var decision = button.dataset.app, view = applicationView;
    var reason = '';
    if (applicationMutations.has(id)) return;
    if (decision === 'approve') {
      if (!confirm('通过该注册申请？通过后该用户即可登录。')) return;
    } else {
      var answer = window.prompt('拒绝理由（可留空，最多 300 字，只有管理员可见）：', '');
      if (answer === null) return;
      reason = answer;
      if (Array.from(reason).length > 300) { notify('拒绝理由不能超过 300 字。'); return; }
    }
    var token = {};
    applicationMutations.set(id, token);
    button.disabled = true;
    nextPanel('applications');
    function isCurrent() { return applicationView === view && applicationMutations.get(id) === token; }
    try {
      await jsonRequest(CitrusAdminContract.decisionUrl(id, decision), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(CitrusAdminContract.decisionBody(decision, reason))
      });
      if (!isCurrent()) return;
      await loadApplications();
      if (isCurrent()) notify(decision === 'approve' ? '已通过该申请' : '已拒绝该申请');
    } catch (error) {
      if (!isCurrent()) return;
      if (error.status === 409) notify('该申请已被其他管理员处理，请刷新列表。');
      else if (error.status === 404) notify('申请不存在，请刷新列表。');
      else notify(error.message);
    } finally {
      if (applicationMutations.get(id) === token) applicationMutations.delete(id);
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 邀请码
  function renderInvites(data) {
    var body = el('invitesTable');
    var items = data.invites || [];
    var next = data.next_offset;
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="5" class="muted">还没有邀请码。</td></tr>';
    } else {
      body.innerHTML = items.map(function (invite) {
        var meta = CitrusAdminContract.inviteState(invite.status);
        var actions = meta.canRevoke
          ? '<button class="small-button danger" data-invite="revoke" data-id="' + esc(invite.invite_id) + '">撤销</button>'
          : '<span class="muted">—</span>';
        var note = invite.claimed_at ? '<small>使用时间：' + esc(formatWhen(invite.claimed_at)) + '</small>'
          : invite.revoked_at ? '<small>撤销时间：' + esc(formatWhen(invite.revoked_at)) + '</small>' : '';
        return '<tr><td><code>' + esc(invite.invite_id) + '</code>' + note + '</td>' +
          '<td><span class="state invite-' + meta.key + '">' + esc(meta.label) + '</span></td>' +
          '<td>' + esc(formatWhen(invite.created_at)) + '</td>' +
          '<td>' + esc(formatWhen(invite.expires_at)) + '</td>' +
          '<td class="actions">' + actions + '</td></tr>';
      }).join('');
    }
    var label = '第 ' + (Math.floor(inviteOffset / invitePageSize) + 1) + ' 页 · 本页 ' + items.length + ' 条';
    el('invitesPageLabel').textContent = label;
    el('invitesNewer').disabled = inviteOffset <= 0;
    el('invitesOlder').disabled = next == null;
    el('invitesSummary').textContent = label + ' · 列表不显示明文邀请码。';
  }
  function loadInvites() {
    var page = inviteOffset;
    return loadPanel('invites', function () {
      return jsonRequest('/api/v2/admin/invites' + CitrusAdminContract.invitesQuery(invitePageSize, page));
    }, renderInvites, function () { return page === inviteOffset; });
  }
  function showOneTimeInvite(code) {
    oneTimeInviteCode = String(code || '');
    el('inviteCodeValue').textContent = oneTimeInviteCode;
    el('inviteOneTime').hidden = !oneTimeInviteCode;
    if (oneTimeInviteCode) el('inviteCopy').focus();
  }
  function discardOneTimeInvite() {
    oneTimeInviteCode = '';
    var value = el('inviteCodeValue'); if (value) value.textContent = '';
    var panel = el('inviteOneTime'); if (panel) panel.hidden = true;
  }
  async function createInvite() {
    var expiry = el('inviteExpiresDays').value;
    if (expiry !== '' && CitrusAdminContract.expiresInDays(expiry) === undefined) {
      notify('有效天数需为 1–30 的整数。');
      return;
    }
    var button = el('inviteCreate');
    button.disabled = true;
    try {
      var data = await jsonRequest('/api/v2/admin/invites', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(CitrusAdminContract.buildInviteBody(el('inviteExpiresDays').value)),
        cache: 'no-store'
      });
      var invite = data.invite || {};
      if (!invite.code) {
        notify('邀请码已创建，但响应未返回明文邀请码；请撤销后重新生成。');
        await loadInvites();
        return;
      }
      el('inviteDisplayHint').textContent = invite.fixed
        ? '固定邀请码每次签发可使用一次。现有邀请码有效时，重复签发不会更改有效期。'
        : '新邀请码只显示一次，请现在复制并通过安全渠道发送。';
      showOneTimeInvite(invite.code);
      notify(invite.reused ? '当前邀请码仍有效。' : '邀请码已签发。');
      await loadInvites();
    } catch (error) { notify(error.message); }
    finally { button.disabled = false; }
  }
  async function handleInviteAction(event) {
    var button = event.target.closest('button[data-invite]');
    if (!button) return;
    if (!confirm('撤销这个邀请码？撤销后无法再用于注册。')) return;
    button.disabled = true;
    try {
      await jsonRequest('/api/v2/admin/invites/' + encodeURIComponent(button.dataset.id) + '/revoke', {method: 'POST'});
      notify('邀请码已撤销');
      await loadInvites();
    } catch (error) {
      if (error.status === 409) notify('该邀请码已被使用或已过期，无法撤销。');
      else if (error.status === 404) notify('邀请码不存在，请刷新列表。');
      else notify(error.message);
    } finally { button.disabled = false; }
  }
  async function copySecret(value, successMessage) {
    if (!value) return;
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
        notify(successMessage || '已复制');
        return;
      }
    } catch (_) {}
    notify('无法自动复制，请手动选中后复制。');
  }

  // ---------------------------------------------------------------- 管理员重置密码
  function openResetDialog(id, name) {
    resetGeneration += 1;
    el('adminResetSubmit').disabled = false;
    pendingResetUserId = String(id || '');
    pendingResetName = String(name || '');
    el('adminResetHint').textContent = '为“' + pendingResetName + '”生成一次性临时密码。需要先输入你当前的管理员密码确认。';
    el('adminResetPassword').value = '';
    el('adminResetError').textContent = '';
    el('adminResetDialog').hidden = false;
    el('adminResetPassword').focus();
  }
  function closeResetDialog() {
    resetGeneration += 1;
    var dialog = el('adminResetDialog'); if (dialog) dialog.hidden = true;
    var password = el('adminResetPassword'); if (password) password.value = '';
    var error = el('adminResetError'); if (error) error.textContent = '';
    pendingResetUserId = '';
    pendingResetName = '';
  }
  function showOneTimeAdminSecret(username, secret) {
    oneTimeAdminSecret = String(secret || '');
    el('adminSecretTitle').textContent = '“' + String(username || '') + '”的一次性临时密码（关闭后无法再次查看）';
    el('adminSecretValue').textContent = oneTimeAdminSecret;
    el('adminSecretPanel').hidden = !oneTimeAdminSecret;
    if (oneTimeAdminSecret) el('adminSecretCopy').focus();
  }
  function discardAdminSecret() {
    oneTimeAdminSecret = '';
    var value = el('adminSecretValue'); if (value) value.textContent = '';
    var panel = el('adminSecretPanel'); if (panel) panel.hidden = true;
  }
  async function submitReset(event) {
    event.preventDefault();
    var targetId = pendingResetUserId, generation = resetGeneration;
    var isCurrent = function () { return generation === resetGeneration && targetId === pendingResetUserId; };
    var error = el('adminResetError');
    var button = el('adminResetSubmit');
    if (!targetId || button.disabled) return;
    var password = el('adminResetPassword').value || '';
    error.textContent = '';
    if (!password) { error.textContent = '请输入你当前的管理员密码。'; return; }
    var targetName = pendingResetName;
    button.disabled = true;
    try {
      var response = await fetch('/api/v2/admin/users/' + encodeURIComponent(targetId) + '/reset-password', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(CitrusAdminContract.resetBody(password))
      });
      var data = await response.json().catch(function () { return {}; });
      if (!isCurrent()) return;
      if (response.status === 401) { location.replace('/?next=%2Fadmin'); return; }
      if (response.status === 403) { error.textContent = '管理员密码不正确，或该账号不允许由他人重置。'; return; }
      if (response.status === 429) { error.textContent = '操作过于频繁，请稍后再试。'; return; }
      if (response.status === 404) { error.textContent = '账号不存在，请刷新列表。'; return; }
      if (!response.ok) { error.textContent = data.detail || '重置失败，请稍后重试。'; return; }
      var temporary = data.temporary_password || '';
      closeResetDialog();
      if (!temporary) {
        notify('已重置，但未收到临时密码；请让用户使用恢复码，或稍后重试。');
        await loadUsers().catch(function (e) { notify('账号已重置，但列表刷新失败：' + e.message); });
        return;
      }
      showOneTimeAdminSecret(targetName, temporary);
      notify('临时密码已生成，只显示一次，请立即复制。');
      await loadUsers().catch(function (e) { notify('密码已重置，但账号列表刷新失败：' + e.message); });
    } catch (e) { if (isCurrent()) error.textContent = '网络异常，请稍后重试。'; }
    finally { if (isCurrent()) button.disabled = false; }
  }

  async function init() {
    var me = await jsonRequest('/api/v2/auth/me');
    if (!me.user || me.user.role !== 'admin') { document.body.innerHTML = '<main class="denied"><h1>仅管理员可访问</h1><a href="/">返回 CitrusScholar</a></main>'; return; }
    el('adminName').textContent = me.user.username;
    el('refreshButton').addEventListener('click', refresh);
    el('usersTable').addEventListener('click', handleUserAction);
    el('applicationsTable').addEventListener('click', function (event) {
      handleApplicationAction(event).catch(function (e) { notify(e.message); });
    });
    el('applicationsRefresh').addEventListener('click', function () { loadApplications().catch(function (e) { notify(e.message); }); });
    el('applicationStatus').addEventListener('change', function () {
      applicationView += 1; appStatus = this.value; appOffset = 0;
      loadApplications().catch(function (e) { notify(e.message); });
    });
    el('applicationsOlder').addEventListener('click', function () {
      applicationView += 1; appOffset += appPageSize; loadApplications().catch(function (e) { notify(e.message); });
    });
    el('applicationsNewer').addEventListener('click', function () {
      applicationView += 1; appOffset = Math.max(0, appOffset - appPageSize); loadApplications().catch(function (e) { notify(e.message); });
    });
    el('invitesTable').addEventListener('click', function (event) {
      handleInviteAction(event).catch(function (e) { notify(e.message); });
    });
    el('invitesRefresh').addEventListener('click', function () { loadInvites().catch(function (e) { notify(e.message); }); });
    el('inviteCreate').addEventListener('click', function () { createInvite().catch(function (e) { notify(e.message); }); });
    el('inviteCopy').addEventListener('click', function () { copySecret(oneTimeInviteCode, '邀请码已复制'); });
    el('inviteDiscard').addEventListener('click', discardOneTimeInvite);
    el('invitesNewer').addEventListener('click', function () {
      inviteOffset = Math.max(0, inviteOffset - invitePageSize); loadInvites().catch(function (e) { notify(e.message); });
    });
    el('invitesOlder').addEventListener('click', function () {
      inviteOffset += invitePageSize; loadInvites().catch(function (e) { notify(e.message); });
    });
    el('adminSecretCopy').addEventListener('click', function () { copySecret(oneTimeAdminSecret, '临时密码已复制'); });
    el('adminSecretDiscard').addEventListener('click', discardAdminSecret);
    el('adminResetForm').addEventListener('submit', function (event) {
      submitReset(event).catch(function (e) { el('adminResetError').textContent = e.message || '重置失败。'; });
    });
    el('adminResetCancel').addEventListener('click', closeResetDialog);
    el('adminResetDialog').addEventListener('click', function (event) { if (event.target === this) closeResetDialog(); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !el('adminResetDialog').hidden) closeResetDialog();
    });
    window.addEventListener('pagehide', function () {
      closeResetDialog(); discardOneTimeInvite(); discardAdminSecret(); accountMutations.clear(); applicationMutations.clear();
      Object.keys(panelGenerations).forEach(nextPanel);
    });
    window.addEventListener('pageshow', function (event) { if (event.persisted) location.reload(); });
    el('auditSearch').addEventListener('submit', function (event) { event.preventDefault(); offset = 0; loadAudit().catch(function (e) { notify(e.message); }); });
    el('onnxDeviceApply').addEventListener('click', applyOnnxDevice);
    el('onnxDeviceMode').addEventListener('change', function () {
      deviceEditGeneration += 1;
      deviceDirty = !committedDevice || this.value !== committedDevice.mode;
    });
    el('olderButton').addEventListener('click', function () { offset += pageSize; loadAudit().catch(function (e) { notify(e.message); }); });
    el('newerButton').addEventListener('click', function () { offset = Math.max(0, offset - pageSize); loadAudit().catch(function (e) { notify(e.message); }); });
    el('logoutButton').addEventListener('click', async function () { await fetch('/api/v2/auth/logout', {method:'POST'}); location.replace('/'); });
    if (el('corpusList')) {
      el('corpusList').addEventListener('click', function (event) { handleCorpusAction(event).catch(function (e) { notify(e.message); }); });
      el('corpusNewest').addEventListener('click', function () { corpusBefore = null; loadCorpus().catch(function (e) { notify(e.message); }); });
      el('corpusOlder').addEventListener('click', function () { corpusBefore = corpusNext; loadCorpus().catch(function (e) { notify(e.message); }); });
      el('corpusOriginalClose').addEventListener('click', function () { el('corpusOriginalDialog').close(); });
      el('corpusOriginalDialog').addEventListener('close', function () { if (!this.open) resetCorpusOriginal(); });
      el('corpusFulltextForm').addEventListener('submit', submitCorpusFulltext);
      el('corpusFulltextCancel').addEventListener('click', function () { el('corpusFulltextDialog').close(); });
      el('corpusFulltextDialog').addEventListener('cancel', function (event) {
        if (el('corpusFulltextSubmit').disabled) event.preventDefault();
      });
      el('corpusRefresh').addEventListener('click', function () { loadCorpus().catch(function (e) { notify(e.message); }); });
      el('corpusStatus').addEventListener('change', function () { corpusBefore = null; loadCorpus().catch(function (e) { notify(e.message); }); });
    }
    await refresh();
  }
  document.addEventListener('DOMContentLoaded', function () { init().catch(function (e) { notify(e.message); }); });
}());
