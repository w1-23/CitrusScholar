var API_URL = '/api/v2/chat';
var currentAccount = null;
// An account lifetime owns requests, file readers and all asynchronous UI work.
var authEpoch = 0;
var authAbortController = new AbortController();
var accountFileReaders = new Set();
var authInFlight = false;
var accountDocumentRetired = false;
// BEGIN INVITE AUTH CONTRACT（纯函数；注册、登录与找回密码共用，不读写 DOM）
var CitrusAuthContract = (function () {
  function clean(value) { return String(value == null ? '' : value).trim(); }
  function buildRegisterPayload(input) {
    return {
      username: clean(input.username),
      password: String(input.password || ''),
      avatar_preset: input.avatar_preset || '01',
      invite_code: clean(input.invite_code)
    };
  }
  function buildLoginPayload(input) {
    return {username: clean(input.username), password: String(input.password || '')};
  }
  function buildRecoverPayload(input) {
    return {
      username: clean(input.username),
      recovery_code: clean(input.recovery_code),
      new_password: String(input.new_password || '')
    };
  }
  function buildPasswordChangePayload(input) {
    return {
      current_password: String(input.current_password || ''),
      new_password: String(input.new_password || '')
    };
  }
  function classifyAuthResponse(mode, status, data) {
    data = data || {};
    if (mode === 'register' && status === 202) {
      return {kind: 'pending', applicationId: data.application_id || '', message: '申请已提交，等待管理员审核。'};
    }
    if (status >= 200 && status < 300) {
      if (!data.user) return {kind: 'error', message: '服务器响应不完整，请稍后重试。'};
      return {kind: 'success', user: data.user};
    }
    if (status === 401) return {kind: 'error', message: '用户名或密码不正确；若刚提交申请，请等待管理员审核后再登录。'};
    if (status === 403) return {kind: 'error', message: '当前登录已失效，请重新登录。'};
    if (status === 409) return {kind: 'error', message: mode === 'register' ? '用户名已被使用，请更换。' : '请求与当前账号状态冲突，请刷新后重试。'};
    if (status === 400) return {kind: 'error', message: mode === 'register' ? '邀请码无效或注册信息不完整，请检查后重试。' : '请求未通过校验，请检查填写内容。'};
    if (status === 422) return {kind: 'error', message: '请检查填写内容后重试。'};
    if (status === 429) return {kind: 'error', message: '操作过于频繁，请稍后再试。'};
    return {kind: 'error', message: '请求失败，请稍后重试。'};
  }
  function validatePasswordChange(input) {
    var current = String(input.current_password || '');
    var next = String(input.new_password || '');
    var confirm = String(input.confirm_password == null ? next : input.confirm_password);
    if (!current) return '请输入当前密码。';
    if (Array.from(next).length < 10) return '新密码至少 10 位。';
    if (next !== confirm) return '两次输入的新密码不一致。';
    return '';
  }
  function validateRecovery(input) {
    if (!clean(input.username)) return '请输入用户名。';
    if (!clean(input.recovery_code)) return '请输入恢复码。';
    var next = String(input.new_password || '');
    var confirm = String(input.confirm_password == null ? next : input.confirm_password);
    if (Array.from(next).length < 10) return '新密码至少 10 位。';
    if (next !== confirm) return '两次输入的新密码不一致。';
    return '';
  }
  function normalizeRecoveryCodes(data) {
    var codes = data && Array.isArray(data.codes) ? data.codes : [];
    return codes.filter(function (code) { return typeof code === 'string' && code; }).slice(0, 8);
  }
  return {
    buildRegisterPayload: buildRegisterPayload,
    buildLoginPayload: buildLoginPayload,
    buildRecoverPayload: buildRecoverPayload,
    buildPasswordChangePayload: buildPasswordChangePayload,
    classifyAuthResponse: classifyAuthResponse,
    validatePasswordChange: validatePasswordChange,
    validateRecovery: validateRecovery,
    normalizeRecoveryCodes: normalizeRecoveryCodes
  };
}());
if (typeof window !== 'undefined') window.CitrusAuthContract = CitrusAuthContract;
// END INVITE AUTH CONTRACT

function staleAccountError() { return new DOMException('Account changed', 'AbortError'); }
function accountFetch(url, options) {
  var epoch = authEpoch;
  var opts = Object.assign({}, options || {});
  var lifetimeSignal = authAbortController.signal;
  if (opts.signal && opts.signal !== lifetimeSignal && typeof AbortSignal.any === 'function') {
    opts.signal = AbortSignal.any([opts.signal, lifetimeSignal]);
  } else if (opts.signal && opts.signal !== lifetimeSignal) {
    var combined = new AbortController();
    var abort = function() { combined.abort(); };
    if (opts.signal.aborted || lifetimeSignal.aborted) abort();
    else {
      opts.signal.addEventListener('abort', abort, {once:true});
      lifetimeSignal.addEventListener('abort', abort, {once:true});
    }
    opts.signal = combined.signal;
  } else opts.signal = lifetimeSignal;
  return fetch(url, opts).then(function(response) {
    if (epoch !== authEpoch) throw staleAccountError();
    return response;
  });
}
var skillPicker = window.CitrusSkillPicker ? window.CitrusSkillPicker.create({
  attachmentMenu: document.getElementById('input-tools-menu'),
  menu: document.getElementById('skill-picker-menu'), list: document.getElementById('skill-picker-list'),
  status: document.getElementById('skill-picker-status'), summary: document.getElementById('selected-skills'),
  retry: document.getElementById('skill-picker-refresh'), fetch: accountFetch,
  scope: function () { return currentAccount && sessionId && !accountDocumentRetired ? authEpoch + ':' + sessionId : ''; }
}) : null;
function advanceAuthEpoch() {
  if (skillPicker) skillPicker.reset();
  authEpoch += 1;
  authAbortController.abort();
  authAbortController = new AbortController();
  accountFileReaders.forEach(function(reader) { reader.abort(); });
  accountFileReaders.clear();
}
function resetAccountState() {
  if (currentAccount) accountDocumentRetired = true;
  advanceAuthEpoch();
  disposeTaskView();
  if (currentAbortController) currentAbortController.abort();
  currentAbortController = null; activeTurnCancel = null;
  window._activeTurnStatusUpdater = null;
  currentJobId = ''; window._currentJobId = '';
  if (currentAccount) localStorage.removeItem(sessionStorageKey());
  for (var i = localStorage.length - 1; i >= 0; i--) {
    var key = localStorage.key(i);
    if (key && (key.indexOf('citrus_ctx_label:') === 0 || key === 'citrus_session_groups_open')) localStorage.removeItem(key);
  }
  localStorage.removeItem('citrus_session_id');
  sessionStorage.removeItem('citrus_session_id');
  clearComposerDrafts();
  currentAccount = null; sessionId = null;
  _selectedAttachments.length = 0;
  window._selectedAccountFileRefs.length = 0;
  _attachmentPreviewUrls.forEach(function(url) { URL.revokeObjectURL(url); });
  _attachmentPreviewUrls = [];
  clearSentAttachmentPreviewUrls();
  _historyMsgs = []; _historyTurns = null; _historyHasMore = false;
  roundHistory = []; _restoredGroups = null; _restoredRefPool = null;
  resetSessionList(); _citationsRetry = 0;
  _ctxData = null; _ctxSelection = null;
  if (window.resetSessionFiles) window.resetSessionFiles();
  currentRoundId = 0; currentQuery = ''; totalTokens = 0;
  _agentTokenBuffer = ''; _agentTokenName = '';
  window._fullTextFromDone = {}; window._remapAppliedRounds = {};
  resetPermissionCards(); _runtimeConfig = null; _providerModelLists = {};
  _providerCatalog = JSON.parse(JSON.stringify(_defaultProviderCatalog));
  signupAvatarDataUrl = ''; settingsAvatarDataUrl = '';
  signupAvatarPreset = '01'; settingsAvatarPreset = '01';
  onboardingSaveInFlight = false; reviewConsentEnabled = false;
  if (window.CitrusGuide) window.CitrusGuide.close(false);
  window.lastCopiedSvgSource = '';
  if (chatContainer) chatContainer.replaceChildren();
  if (userInput) { userInput.value = ''; userInput.placeholder = '有什么想做的？'; }
  refreshTaskComposer();
  ['attachment-list', 'session-list', 'sidebar-content', 'workspace-list',

    'accountMemoryContent', 'accountMemoryStatus', 'accountPreferenceStatus',
    'settingsAvatarError', 'settingsAvatarLabel', 'authAvatarError', 'authAvatarLabel',
    'authNotice', 'authError', 'recoverError', 'recoverNotice', 'passwordChangeStatus', 'recoveryStatus', 'recoveryCodesList',
    'accountMenuName', 'accountMenuRole', 'sessionFilesUploaded', 'sessionFilesGenerated',
    'sessionFilePreviewStage', 'sessionFilePreviewTitle', 'sessionFilePreviewMeta',
    'configuredProvidersList'].forEach(function(id) {
    var node = document.getElementById(id); if (node) node.replaceChildren();
  });
  ['attachment-input', 'image-attachment-input', 'authUsername', 'authPassword', 'authInviteCode', 'authAvatarFile', 'settingsAvatarFile',
    'recoverUsername', 'recoverCode', 'recoverNewPassword', 'recoverConfirmPassword',
    'passwordCurrent', 'passwordNew', 'passwordConfirm', 'recoveryCurrent',
    'apiKeyInput', 'webSearchApiKeyInput', 'webFetchApiKeyInput', 'mainModelSelect', 'mainApiBaseUrlInput'].forEach(function(id) {
    var node = document.getElementById(id); if (node) node.value = '';
  });
  ['avatarSettingsOverlay', 'memorySettingsOverlay', 'securitySettingsOverlay', 'onboardingOverlay',
    'sessionFilePreviewOverlay', 'sessionFilesPanel'].forEach(function(id) {
    var node = document.getElementById(id); if (node) node.hidden = true;
  });
  ['settingsAvatarPreview', 'authAvatarPreview'].forEach(function(id) {
    var node = document.getElementById(id); if (node) node.removeAttribute('src');
  });
  var download = document.getElementById('sessionFilePreviewDownload');
  if (download) download.removeAttribute('href');
  var permission = document.getElementById('permission-card'); if (permission) permission.remove();
  var banner = document.getElementById('degraded-banner'); if (banner) banner.remove();
  closeSessionMenu(); closeSettingsModal(); setPreferenceFormValues({});
  ['ctxDrawer', 'ctxBackdrop'].forEach(function(id) {
    var node = document.getElementById(id); if (node) { node.classList.remove('open'); if (id === 'ctxDrawer') node.inert = true; }
  });
  var ctxTrigger = document.getElementById('ctxCircleBtn');
  if (ctxTrigger) ctxTrigger.setAttribute('aria-expanded', 'false');
  if (_contextView) _contextView.update(null);
  if (citationCount) citationCount.textContent = '0';
  var count = document.getElementById('session-count'); if (count) count.textContent = '0';
  renderAttachmentSelection(); renderReviewConsent(); setSendDisabled(false);
  clearRecoveryCodes(false);
  ['passwordChangeSubmit', 'recoveryCodesButton'].forEach(function(id) {
    var button = document.getElementById(id); if (button) button.disabled = false;
  });
  var recoverCard = document.getElementById('authRecoverCard'); if (recoverCard) recoverCard.hidden = true;
  var loginCard = document.getElementById('authLoginCard'); if (loginCard) loginCard.hidden = false;
  var noticeCard = document.getElementById('authNotice'); if (noticeCard) noticeCard.hidden = true;
  showAuthGate(true);
}
// Drawing is available through natural-language requests; no composer opt-in.
var visualizationEnabled = true;
var citrusAvatarPresets = Array.from({length: 20}, function(_, index) { return String(index + 1).padStart(2, '0'); });
var signupAvatarPreset = '01';
var signupAvatarDataUrl = '';
var settingsAvatarPreset = '01';
var settingsAvatarDataUrl = '';
var onboardingSaveInFlight = false;
function sessionStorageKey() { return 'citrus_session_id_' + (currentAccount ? currentAccount.user_id : ''); }
function avatarSource(account) {
  if (!account) return '/assets/avatars/01.webp';
  if (account.avatar_custom) return '/api/v2/auth/avatar?v=' + encodeURIComponent(account.avatar_updated_at || 'current');
  var preset = /^\d{2}$/.test(account.avatar_preset || '') ? account.avatar_preset : '01';
  return '/assets/avatars/' + preset + '.webp';
}
function renderAvatarImage(container, account, className, altText) {
  var epoch = authEpoch;
  if (!container) return;
  container.replaceChildren();
  var image = document.createElement('img');
  image.className = className || '';
  image.src = avatarSource(account);
  image.alt = altText || '用户头像';
  image.decoding = 'async';
  image.onerror = function() {
    if (epoch !== authEpoch || image.parentNode !== container) return;
    container.replaceChildren();
    container.textContent = account && account.username
      ? Array.from(account.username.trim())[0].toLocaleUpperCase() : 'C';
  };
  container.appendChild(image);
}
function renderAgentAvatar(container) {
  if (!container) return;
  var image = document.createElement('img');
  image.className = 'agent-avatar-image';
  image.src = '/assets/citrusscholar-mascot.png';
  image.alt = 'CitrusScholar 助手';
  image.width = 36;
  image.height = 36;
  image.decoding = 'async';
  container.replaceChildren(image);
}
function refreshCurrentUserAvatars() {
  renderAvatarImage(document.getElementById('accountAvatar'), currentAccount, 'account-avatar-image', '账户头像');
  document.querySelectorAll('.message.user .avatar').forEach(function(node) {
    renderAvatarImage(node, currentAccount, 'message-avatar-image', '我的头像');
  });
}
function showAuthGate(show) {
  var gate = document.getElementById('authGate');
  if (gate) gate.hidden = !show;
  var app = document.getElementById('mainContainer');
  if (app) app.style.visibility = show ? 'hidden' : '';
  var account = document.getElementById('sidebarAccount');
  if (account) account.hidden = !currentAccount;
  var settings = document.getElementById('modelSettingsBtn');
  if (settings) settings.hidden = !currentAccount || currentAccount.role !== 'admin';
  var who = document.getElementById('accountLabel');
  if (who) who.textContent = currentAccount ? currentAccount.username : '';
  refreshCurrentUserAvatars();
  var role = document.getElementById('accountRole');
  if (role) role.textContent = currentAccount && currentAccount.role === 'admin' ? '管理员' : '账户';
  var admin = document.getElementById('adminUsersBtn');
  if (admin) admin.hidden = !currentAccount || currentAccount.role !== 'admin';
  var logout = document.getElementById('logoutBtn');
  if (logout) logout.hidden = !currentAccount;
  if (show) setAccountMenuOpen(false);
}
let reviewConsentEnabled = false;

async function refreshReviewConsent() {
  var epoch = authEpoch;
  try {
    const res = await accountFetch(apiBase() + "/api/v2/review-consent", {credentials: "include"});
    if (!res.ok) return;
    const data = await res.json();
    if (epoch !== authEpoch) return;
    reviewConsentEnabled = !!data.enabled;
  } catch (err) { if (epoch !== authEpoch) return;
    return;
  }
  renderReviewConsent();
}

function renderReviewConsent() {
  const btn = document.getElementById("reviewConsentBtn");
  if (!btn) return;
  btn.textContent = "\u21ea 文献共享授权：" + (reviewConsentEnabled ? "已授权" : "未授权");
  btn.setAttribute("aria-pressed", reviewConsentEnabled ? "true" : "false");
}

async function toggleReviewConsent() {
  var epoch = authEpoch;
  const next = !reviewConsentEnabled;
  try {
    const res = await accountFetch(apiBase() + "/api/v2/review-consent", {
      method: "PUT",
      credentials: "include",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled: next})
    });
    if (epoch !== authEpoch) return;
    if (!res.ok) {
      showToast && showToast("\u4fdd\u5b58\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5");
      return;
    }
    if (epoch !== authEpoch) return;
    reviewConsentEnabled = next;
    renderReviewConsent();
    showToast && showToast(next
      ? "\u5df2\u5f00\u542f\uff1a\u4f60\u4e0a\u4f20\u7684\u6587\u732e\u5c06\u8fdb\u5165\u7ba1\u7406\u5458\u5019\u5ba1\u5e93"
      : "\u5df2\u5173\u95ed\uff1a\u4e0a\u4f20\u6587\u4ef6\u4e0d\u518d\u5171\u4eab");
  } catch (err) { if (epoch !== authEpoch) return;
    showToast && showToast("\u7f51\u7edc\u5f02\u5e38\uff0c\u672a\u4fdd\u5b58");
  }
}

function setAccountMenuOpen(open) {
  var menu = document.getElementById('accountMenu');
  var trigger = document.getElementById('accountTrigger');
  if (menu) menu.hidden = !open;
  if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) {
    try { refreshReviewConsent(); } catch (err) {}
    // P1-4: 菜单里的账号信息与左下触发器保持一致
    var nameEl = document.getElementById('accountMenuName');
    var roleEl = document.getElementById('accountMenuRole');
    var triggerName = document.querySelector('.account-trigger-name');
    var triggerRole = document.querySelector('.account-trigger-role');
    if (nameEl && triggerName) nameEl.textContent = triggerName.textContent || '';
    if (roleEl && triggerRole) roleEl.textContent = triggerRole.textContent || '';
  }
}
function toggleAccountMenu() {
  var menu = document.getElementById('accountMenu');
  setAccountMenuOpen(!menu || menu.hidden);
}
document.addEventListener('click', function(event) {
  var area = document.getElementById('sidebarAccount');
  if (area && !area.contains(event.target)) setAccountMenuOpen(false);
});
// 本轮：宠物与主题拆成两个独立弹层——主题只管配色/背景，宠物只管吉祥物本体
function _railMenuEls() {
  return {
    pet: document.getElementById('petMenu'),
    petBtn: document.getElementById('petRailBtn'),
    theme: document.getElementById('themeMenu'),
    themeBtn: document.getElementById('themeRailBtn')
  };
}
function setRailMenuOpen(which, open) {
  var els = _railMenuEls();
  var menu = which === 'pet' ? els.pet : els.theme;
  var btn = which === 'pet' ? els.petBtn : els.themeBtn;
  if (!menu) return;
  if (open) {
    var other = which === 'pet' ? els.theme : els.pet;
    var otherBtn = which === 'pet' ? els.themeBtn : els.petBtn;
    if (other) other.hidden = true;
    if (otherBtn) { otherBtn.setAttribute('aria-expanded', 'false'); otherBtn.classList.remove('is-active'); }
    setAccountMenuOpen(false);
    if (which === 'pet') syncPetPanel();
  }
  menu.hidden = !open;
  if (btn) {
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.classList.toggle('is-active', !!open);
  }
}
function togglePetMenu() {
  var menu = document.getElementById('petMenu');
  setRailMenuOpen('pet', !!menu && menu.hidden);
}
function toggleThemeMenu() {
  var menu = document.getElementById('themeMenu');
  setRailMenuOpen('theme', !!menu && menu.hidden);
}
document.addEventListener('click', function(event) {
  var els = _railMenuEls();
  var pairs = [['pet', els.pet, els.petBtn], ['theme', els.theme, els.themeBtn]];
  for (var i = 0; i < pairs.length; i++) {
    var menu = pairs[i][1]; var btn = pairs[i][2];
    if (!menu || menu.hidden) continue;
    if (menu.contains(event.target) || (btn && btn.contains(event.target))) continue;
    setRailMenuOpen(pairs[i][0], false);
  }
});
document.addEventListener('keydown', function(event) {
  if (event.key !== 'Escape') return;
  var els = _railMenuEls();
  if (els.pet && !els.pet.hidden) setRailMenuOpen('pet', false);
  if (els.theme && !els.theme.hidden) setRailMenuOpen('theme', false);
});
// 宠物面板与吉祥物自身控件共享同一份尺寸/显隐状态（citrus_mascot_scale / citrus_mascot_hidden）
function _mascotScalePct() {
  var scale = 100;
  if (window.CitrusMascot && typeof window.CitrusMascot.getScale === 'function') {
    scale = Number(window.CitrusMascot.getScale());
  } else {
    try { scale = Number(localStorage.getItem('citrus_mascot_scale')); } catch (err) {}
  }
  if (!isFinite(scale) || scale <= 0) return 100;
  if (scale <= 1.8) scale = scale * 100;   // 兼容 0.8–1.8 与 80–180 两种存法
  return Math.max(80, Math.min(180, Math.round(scale)));
}
function syncPetPanel() {
  var hidden = false;
  try { hidden = localStorage.getItem('citrus_mascot_hidden') === '1'; } catch (err) {}
  var text = document.getElementById('petToggleText');
  if (text) text.textContent = hidden ? '显示宠物' : '隐藏宠物';
  var pct = _mascotScalePct();
  var range = document.getElementById('petScaleRange');
  var value = document.getElementById('petScaleValue');
  if (range && Number(range.value) !== pct) range.value = String(pct);
  if (value) value.textContent = pct + '%';
  var motion = null;
  try { motion = localStorage.getItem('citrus_pet_motion'); } catch (err) {}
  var box = document.getElementById('petMotionToggle');
  if (box) box.checked = motion !== '0';
  syncPetActivity();
  syncMascotRailButton();
}
function onPetScaleInput(value) {
  var pct = Math.max(80, Math.min(180, Math.round(Number(value) || 100)));
  if (window.CitrusMascot && typeof window.CitrusMascot.setScale === 'function') {
    window.CitrusMascot.setScale(pct, true);
  } else {
    try { localStorage.setItem('citrus_mascot_scale', String(pct)); } catch (err) {}
  }
  var range = document.getElementById('petScaleRange');
  var label = document.getElementById('petScaleValue');
  if (range && Number(range.value) !== pct) range.value = String(pct);
  if (label) label.textContent = pct + '%';
}
function resetPetScale() {
  onPetScaleInput(100);
  if (window.CitrusMascot && typeof window.CitrusMascot.setScale === 'function') {
    window.CitrusMascot.setScale(100, true);
  }
}
function resetPetPosition() {
  if (window.CitrusMascot && typeof window.CitrusMascot.resetPosition === 'function') {
    window.CitrusMascot.resetPosition();
    return;
  }
  try { localStorage.removeItem('citrus_mascot_pos'); } catch (err) {}
  if (window.CitrusMascot && typeof window.CitrusMascot.setPosition === 'function') {
    window.CitrusMascot.setPosition(null);
  }
}
function setPetMotion(on) {
  try { localStorage.setItem('citrus_pet_motion', on ? '1' : '0'); } catch (err) {}
  if (window.CitrusMascot && typeof window.CitrusMascot.setMotion === 'function') {
    window.CitrusMascot.setMotion(!!on);
  }
}
function syncMascotRailButton() {
  var btn = document.getElementById('petRailBtn');
  if (!btn) return;
  var hidden = false;
  try { hidden = localStorage.getItem('citrus_mascot_hidden') === '1'; } catch (err) {}
  btn.setAttribute('aria-pressed', hidden ? 'false' : 'true');
  btn.classList.toggle('is-off', hidden);
  var text = document.getElementById('petToggleText');
  if (text) text.textContent = hidden ? '显示宠物' : '隐藏宠物';
}
function setPetActivity(level) {
  if (level !== 'quiet' && level !== 'natural' && level !== 'lively') return;
  try { localStorage.setItem('citrus_mascot_activity', level); } catch (err) {}
  if (window.CitrusMascot && typeof window.CitrusMascot.setActivityLevel === 'function') {
    window.CitrusMascot.setActivityLevel(level);
  }
  syncPetActivity();
}
function syncPetActivity() {
  var level = 'natural';
  try { level = localStorage.getItem('citrus_mascot_activity') || 'natural'; } catch (err) {}
  if (level !== 'quiet' && level !== 'natural' && level !== 'lively') level = 'natural';
  if (window.CitrusMascot && typeof window.CitrusMascot.getActivityLevel === 'function') {
    var live = window.CitrusMascot.getActivityLevel();
    if (live) level = live;
  }
  var btns = document.querySelectorAll('[data-pet-activity]');
  for (var i = 0; i < btns.length; i++) {
    var on = btns[i].getAttribute('data-pet-activity') === level;
    btns[i].classList.toggle('is-on', on);
    btns[i].setAttribute('aria-checked', on ? 'true' : 'false');
  }
}
function toggleMascotVisibility() {
  if (window.CitrusMascot) window.CitrusMascot.toggle();
  syncMascotRailButton();
}
// P5: 等待态「绿色小芽」节点从模板克隆（模板在 index.html，避免重复 DOM）
function _hideLoadSprout(el) {
  if (!el || el.hidden) return;
  el.classList.add('is-hiding');
  setTimeout(function () { el.hidden = true; el.classList.remove('is-hiding'); }, 260);
}
function _createLoadSprout() {
  var tpl = document.getElementById('loadSproutTpl');
  if (!tpl || !tpl.content) return null;
  var node = tpl.content.firstElementChild;
  if (!node) return null;
  var clone = node.cloneNode(true);
  clone.hidden = false;
  return clone;
}
// P1-4: 减少动画——只影响过渡与装帧动画，不动吉祥物状态机
function setReduceMotion(on) {
  document.documentElement.setAttribute('data-reduce-motion', on ? '1' : '0');
  var box = document.getElementById('reduceMotionToggle');
  if (box) box.checked = !!on;
  try { localStorage.setItem('citrus_reduce_motion', on ? '1' : '0'); } catch (err) {}
}
// Force motion ignores the OS 'reduce motion' preference, for users who want the
// loading sprout and mascot micro-motion even when Windows animations are off.
function setForceMotion(on) {
  document.documentElement.setAttribute('data-force-motion', on ? '1' : '0');
  var box = document.getElementById('forceMotionToggle');
  if (box) box.checked = !!on;
  try { localStorage.setItem('citrus_force_motion', on ? '1' : '0'); } catch (err) {}
}
(function restoreForceMotion() {
  var saved = null;
  try { saved = localStorage.getItem('citrus_force_motion'); } catch (err) {}
  setForceMotion(saved === '1');
})();
(function restoreReduceMotion() {
  var saved = null;
  try { saved = localStorage.getItem('citrus_reduce_motion'); } catch (err) {}
  setReduceMotion(saved === '1');
  syncMascotRailButton();
})();
(function bindPetPanel() {
  var range = document.getElementById('petScaleRange');
  if (range) range.addEventListener('input', function () { onPetScaleInput(range.value); });
  syncPetPanel();
})();
function renderAvatarGrid(host, selected, onSelect) {
  if (!host) return;
  host.replaceChildren();
  citrusAvatarPresets.forEach(function(preset) {
    var option = document.createElement('button');
    option.type = 'button'; option.className = 'avatar-option' + (selected === preset ? ' selected' : '');
    option.setAttribute('role', 'option'); option.setAttribute('aria-selected', selected === preset ? 'true' : 'false');
    option.setAttribute('aria-label', '头像 ' + preset); option.title = '头像 ' + preset;
    var img = document.createElement('img'); img.src = '/assets/avatars/' + preset + '.webp'; img.alt = '';
    var label = document.createElement('span'); label.textContent = preset;
    option.appendChild(img); option.appendChild(label);
    option.addEventListener('click', function() { onSelect(preset); });
    host.appendChild(option);
  });
}
function setSignupAvatar(preset) {
  signupAvatarPreset = preset; signupAvatarDataUrl = '';
  var file = document.getElementById('authAvatarFile'); if (file) file.value = '';
  var error = document.getElementById('authAvatarError'); if (error) error.textContent = '';
  var preview = document.getElementById('authAvatarPreview'); if (preview) preview.src = '/assets/avatars/' + preset + '.webp';
  var label = document.getElementById('authAvatarLabel'); if (label) label.textContent = '已选 ' + preset;
  renderAvatarGrid(document.getElementById('authAvatarGrid'), preset, setSignupAvatar);
}
function syncAuthAvatarPicker() {
  var picker = document.getElementById('authAvatarPicker');
  var mode = document.getElementById('authMode');
  var invite = document.getElementById('authInviteCode');
  if (!picker || !mode) return;
  var registering = mode.value === 'register';
  picker.hidden = !registering;
  if (invite) invite.hidden = !registering;
  if (registering && !document.getElementById('authAvatarGrid').childElementCount) setSignupAvatar('01');
}
function previewSignupAvatar(input) {
  if (input) input.value = '';
  signupAvatarDataUrl = '';
  var error = document.getElementById('authAvatarError');
  if (error) error.textContent = '注册阶段使用预设头像；审核通过后可在账户菜单更换自定义头像。';
}
function openAvatarSettings() {
  var overlay = document.getElementById('avatarSettingsOverlay'); if (!overlay) return;
  settingsAvatarPreset = currentAccount && currentAccount.avatar_preset || '01';
  settingsAvatarDataUrl = '';
  var input = document.getElementById('settingsAvatarFile'); if (input) input.value = '';
  var error = document.getElementById('settingsAvatarError'); if (error) error.textContent = '';
  var preview = document.getElementById('settingsAvatarPreview');
  if (preview) preview.src = avatarSource(currentAccount);
  var label = document.getElementById('settingsAvatarLabel');
  if (label) label.textContent = currentAccount && currentAccount.avatar_custom ? '当前自定义头像' : '预设头像 ' + settingsAvatarPreset;
  renderAvatarGrid(document.getElementById('settingsAvatarGrid'),
    currentAccount && currentAccount.avatar_custom ? '' : settingsAvatarPreset, selectSettingsAvatar);
  overlay.hidden = false;
  var first = overlay.querySelector('.avatar-option'); if (first) first.focus();
}
function closeAvatarSettings() {
  var overlay = document.getElementById('avatarSettingsOverlay'); if (overlay) overlay.hidden = true;
  var trigger = document.getElementById('accountTrigger'); if (trigger) trigger.focus();
}
function selectSettingsAvatar(preset) {
  settingsAvatarPreset = preset; settingsAvatarDataUrl = '';
  var input = document.getElementById('settingsAvatarFile'); if (input) input.value = '';
  var preview = document.getElementById('settingsAvatarPreview'); if (preview) preview.src = '/assets/avatars/' + preset + '.webp';
  var label = document.getElementById('settingsAvatarLabel'); if (label) label.textContent = '预设头像 ' + preset;
  var error = document.getElementById('settingsAvatarError'); if (error) error.textContent = '';
  renderAvatarGrid(document.getElementById('settingsAvatarGrid'), preset, selectSettingsAvatar);
}
function previewSettingsAvatar(input) {
  var epoch = authEpoch;
  var file = input && input.files && input.files[0];
  var error = document.getElementById('settingsAvatarError'); if (error) error.textContent = '';
  if (!file) return;
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) {
    if (error) error.textContent = file.size > 5 * 1024 * 1024 ? '图片不能超过 5 MB。' : '请选择 JPG、PNG 或 WebP 图片。';
    input.value = ''; return;
  }
  fileAsDataUrl(file).then(function(dataUrl) {
    if (epoch !== authEpoch) return;
    settingsAvatarDataUrl = dataUrl;
    var preview = document.getElementById('settingsAvatarPreview'); if (preview) preview.src = settingsAvatarDataUrl;
    var label = document.getElementById('settingsAvatarLabel'); if (label) label.textContent = '自定义头像';
    renderAvatarGrid(document.getElementById('settingsAvatarGrid'), '', selectSettingsAvatar);
  }).catch(function() { if (epoch === authEpoch && error) error.textContent = '读取图片失败，请重试。'; });
}
async function saveAccountAvatar() {
  var epoch = authEpoch;
  var button = document.getElementById('saveAvatarButton');
  var error = document.getElementById('settingsAvatarError');
  if (button) button.disabled = true;
  if (error) error.textContent = '';
  try {
    var endpoint = settingsAvatarDataUrl ? '/api/v2/auth/avatar/upload' : '/api/v2/auth/avatar/preset';
    var body = settingsAvatarDataUrl ? {data_url: settingsAvatarDataUrl} : {preset: settingsAvatarPreset};
    var response = await accountFetch(endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    var result = await response.json(); if (epoch !== authEpoch) return;
    if (!response.ok) throw new Error(result.detail || '头像保存失败。');
    currentAccount = result.user;
    showAuthGate(false);
    closeAvatarSettings();
  } catch (e) { if (epoch !== authEpoch) return; if (error) error.textContent = e.message || '头像保存失败。'; }
  finally { if (epoch !== authEpoch) return; if (button) button.disabled = false; }
}
function openOnboardingGuide(startPage) {
  if (window.CitrusGuide) window.CitrusGuide.open(startPage || 'research', {onClose: completeOnboarding});
}
function setPreferenceFormValues(data) {
  var preferences = (data && data.preferences) || {};
  var fields = document.querySelectorAll('#preferenceForm [data-preference-field]');
  for (var i = 0; i < fields.length; i++) {
    var field = fields[i].getAttribute('data-preference-field');
    fields[i].value = preferences[field] == null ? '' : String(preferences[field]);
  }
  updateCustomNoteCount();
}

function collectPreferenceFormValues() {
  var payload = {};
  var fields = document.querySelectorAll('#preferenceForm [data-preference-field]');
  for (var i = 0; i < fields.length; i++) {
    payload[fields[i].getAttribute('data-preference-field')] = fields[i].value || '';
  }
  return payload;
}

function updateCustomNoteCount() {
  var note = document.querySelector('#preferenceForm [data-preference-field="custom_note"]');
  var count = document.getElementById('customNoteCount');
  if (note && count) count.textContent = String(Array.from(note.value || '').length);
}

async function loadAccountPreferences() {
  var epoch = authEpoch;
  var status = document.getElementById('accountPreferenceStatus');
  if (status) status.textContent = '正在读取…';
  try {
    var response = await accountFetch('/api/v2/auth/preferences', {credentials:'same-origin'});
    var data = await response.json(); if (epoch !== authEpoch) return;
    if (!response.ok) throw new Error(data.detail || '读取个人偏好失败。');
    setPreferenceFormValues(data);
    if (status) status.textContent = '';
  } catch (error) { if (epoch !== authEpoch) return;
    if (status) status.textContent = error.message || '读取个人偏好失败。';
  }
}

async function saveAccountPreferences() {
  var epoch = authEpoch;
  var status = document.getElementById('accountPreferenceStatus');
  if (status) status.textContent = '正在保存…';
  try {
    var response = await accountFetch('/api/v2/auth/preferences', {
      method:'PUT',
      credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(collectPreferenceFormValues())
    });
    var data = await response.json(); if (epoch !== authEpoch) return;
    if (!response.ok) throw new Error(data.detail || '保存个人偏好失败。');
    setPreferenceFormValues(data);
    if (status) status.textContent = '已保存，下一轮回答生效。';
  } catch (error) { if (epoch !== authEpoch) return;
    if (status) status.textContent = error.message || '保存个人偏好失败。';
  }
}

async function resetAccountPreferences() {
  var epoch = authEpoch;
  if (!window.confirm('将个人偏好恢复为系统默认值？记录和会话不会删除。')) return;
  var status = document.getElementById('accountPreferenceStatus');
  if (status) status.textContent = '正在恢复…';
  try {
    var response = await accountFetch('/api/v2/auth/preferences', {
      method:'DELETE', credentials:'same-origin'
    });
    var data = await response.json(); if (epoch !== authEpoch) return;
    if (!response.ok) throw new Error(data.detail || '恢复默认值失败。');
    setPreferenceFormValues(data);
    if (status) status.textContent = '已恢复默认值。';
  } catch (error) { if (epoch !== authEpoch) return;
    if (status) status.textContent = error.message || '恢复默认值失败。';
  }
}

async function openMemorySettings() {
  var epoch = authEpoch;
  var overlay = document.getElementById('memorySettingsOverlay');
  if (!overlay) return;
  overlay.hidden = false;
  await loadAccountPreferences();
  if (epoch !== authEpoch || overlay.hidden) return;
  var first = document.querySelector('#preferenceForm select');
  if (first) first.focus();
}
function closeMemorySettings() {
  var overlay = document.getElementById('memorySettingsOverlay'); if (overlay) overlay.hidden = true;
  var trigger = document.getElementById('accountTrigger'); if (trigger) trigger.focus();
}
document.addEventListener('input', function (event) {
  if (event.target && event.target.matches &&
      event.target.matches('#preferenceForm [data-preference-field="custom_note"]')) {
    updateCustomNoteCount();
  }
});
document.addEventListener('submit', function (event) {
  if (!event.target || event.target.id !== 'preferenceForm') return;
  event.preventDefault();
  saveAccountPreferences();
});
document.addEventListener('keydown', function (event) {
  if (event.key !== 'Escape') return;
  var security = document.getElementById('securitySettingsOverlay');
  if (security && !security.hidden) { event.preventDefault(); closeSecuritySettings(); return; }
  var overlay = document.getElementById('memorySettingsOverlay');
  if (!overlay || overlay.hidden) return;
  event.preventDefault();
  closeMemorySettings();
});
async function completeOnboarding() {
  var epoch = authEpoch;
  if (window.CitrusGuide) window.CitrusGuide.close(false);
  if (!currentAccount || currentAccount.onboarding_completed || onboardingSaveInFlight) return;
  onboardingSaveInFlight = true;
  try {
    var response = await accountFetch('/api/v2/auth/onboarding/complete', {method:'POST'});
    if (!response.ok) throw new Error('guide state not saved');
    var data = await response.json(); if (epoch !== authEpoch) return; currentAccount = data.user;
  } catch (_) { if (epoch !== authEpoch) return;
    // Keep the guide dismissible during network failures; the account will retry
    // onboarding on its next sign-in, without overwriting account/theme storage.
    updateStatus('warn', '指南阅读状态未保存；可随时从使用指南重新打开。');
  } finally { if (epoch !== authEpoch) return; onboardingSaveInFlight = false; }
}
async function authenticate(mode) {
  if (authInFlight) return;
  var username = (document.getElementById('authUsername').value || '').trim();
  var password = document.getElementById('authPassword').value || '';
  var inviteInput = document.getElementById('authInviteCode');
  var inviteCode = inviteInput ? (inviteInput.value || '').trim() : '';
  var error = document.getElementById('authError');
  if (error) error.textContent = '';
  setAuthNotice('');
  if (mode === 'register' && !inviteCode) {
    if (error) error.textContent = '请输入邀请码。邀请码由管理员发放。';
    return;
  }
  advanceAuthEpoch();
  authInFlight = true;
  var epoch = authEpoch;
  try {
    var payload = mode === 'register'
      ? CitrusAuthContract.buildRegisterPayload({username: username, password: password, avatar_preset: signupAvatarPreset, invite_code: inviteCode})
      : CitrusAuthContract.buildLoginPayload({username: username, password: password});
    var response = await accountFetch('/api/v2/auth/' + mode, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
      credentials: 'same-origin',
      cache: 'no-store'
    });
    var data = await response.json().catch(function () { return {}; });
    if (epoch !== authEpoch) return;
    var outcome = CitrusAuthContract.classifyAuthResponse(mode, response.status, data);
    if (outcome.kind === 'pending') {
      // 202 只表示申请已受理：仍停留在登录界面，清除秘密输入，绝不进入登录成功路径。
      clearAuthSecrets();
      setAuthMode('login');
      setAuthNotice(outcome.message + '审核通过后，请使用同一账号登录。');
      return;
    }
    if (outcome.kind !== 'success') throw new Error(outcome.message);
    // Other page modules own private caches. A retired document may never host
    // another account: navigation releases those closures and their File refs.
    if (accountDocumentRetired) { location.replace('/'); return; }
    currentAccount = outcome.user;
    clearAuthSecrets();
    localStorage.removeItem('citrus_session_id');
    var next = new URLSearchParams(location.search).get('next');
    if (next === '/admin') {
      if (currentAccount.role !== 'admin') throw new Error('此页面仅管理员可访问。');
      location.replace('/admin'); return;
    }
    if (/^\/(?:figures(?:\/legacy)?|figure-workbench)(?:\?|$)/.test(next || '')) {
      location.replace(next); return;
    }
    await startAuthenticatedApp();
  } catch (e) { if (epoch === authEpoch) { if (error) error.textContent = e.message || '请求失败'; } }
  finally { if (epoch === authEpoch) authInFlight = false; }
}
async function logoutAccount() {
  if (accountDocumentRetired) return;
  accountDocumentRetired = true;
  resetAccountState();
  authInFlight = true;
  var epoch = authEpoch;
  try {
    var response = await accountFetch('/api/v2/auth/logout', {method:'POST'});
    if (epoch !== authEpoch) return;
    if (!response.ok) throw new Error('退出未完成，请重试登录。');
    location.replace('/');
  } catch (error) {
    if (epoch !== authEpoch) return;
    document.getElementById('authError').textContent = error.message || '退出未完成，请重试登录。';
  } finally { if (epoch === authEpoch) authInFlight = false; }
}
function setAuthNotice(message, kind, id) {
  var box = document.getElementById(id || 'authNotice');
  if (!box) return;
  box.textContent = message || '';
  box.hidden = !message;
  if (kind) box.setAttribute('data-kind', kind); else box.removeAttribute('data-kind');
}
function setAuthMode(mode) {
  var select = document.getElementById('authMode');
  if (select) select.value = mode === 'register' ? 'register' : 'login';
  syncAuthAvatarPicker();
}
function clearAuthSecrets() {
  ['authPassword', 'authInviteCode', 'authAvatarFile'].forEach(function (id) {
    var node = document.getElementById(id); if (node) node.value = '';
  });
  signupAvatarDataUrl = '';
  var avatarError = document.getElementById('authAvatarError'); if (avatarError) avatarError.textContent = '';
}
function openRecoverPane() {
  var login = document.getElementById('authLoginCard');
  var recover = document.getElementById('authRecoverCard');
  clearAuthSecrets();
  clearRecoveryInputs();
  if (login) login.hidden = true;
  if (recover) recover.hidden = false;
  var first = document.getElementById('recoverUsername'); if (first) first.focus();
}
function closeRecoverPane() {
  var login = document.getElementById('authLoginCard');
  var recover = document.getElementById('authRecoverCard');
  if (recover) recover.hidden = true;
  if (login) login.hidden = false;
  clearRecoveryInputs();
  var first = document.getElementById('authUsername'); if (first) first.focus();
}
function clearRecoveryInputs() {
  ['recoverUsername', 'recoverCode', 'recoverNewPassword', 'recoverConfirmPassword'].forEach(function (id) {
    var node = document.getElementById(id); if (node) node.value = '';
  });
  var error = document.getElementById('recoverError'); if (error) error.textContent = '';
  setAuthNotice('', '', 'recoverNotice');
}
async function submitPasswordRecovery() {
  if (authInFlight) return;
  var username = (document.getElementById('recoverUsername').value || '').trim();
  var code = (document.getElementById('recoverCode').value || '').trim();
  var next = document.getElementById('recoverNewPassword').value || '';
  var confirmValue = document.getElementById('recoverConfirmPassword').value || '';
  var error = document.getElementById('recoverError');
  if (error) error.textContent = '';
  var invalid = CitrusAuthContract.validateRecovery({
    username: username, recovery_code: code, new_password: next, confirm_password: confirmValue
  });
  if (invalid) { if (error) error.textContent = invalid; return; }
  authInFlight = true;
  var epoch = authEpoch;
  try {
    var response = await accountFetch('/api/v2/auth/password/recover', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(CitrusAuthContract.buildRecoverPayload({username: username, recovery_code: code, new_password: next})),
      credentials: 'same-origin',
      cache: 'no-store'
    });
    if (epoch !== authEpoch) return;
    if (response.status === 429) { if (error) error.textContent = '尝试过于频繁，请稍后再试。'; return; }
    if (!response.ok) { if (error) error.textContent = '用户名或恢复码无效，请检查后重试。'; return; }
    clearRecoveryInputs();
    closeRecoverPane();
    setAuthMode('login');
    setAuthNotice('密码已重置，请使用新密码登录。');
  } catch (e) { if (epoch === authEpoch && error) error.textContent = '网络异常，请稍后重试。'; }
  finally { if (epoch === authEpoch) authInFlight = false; }
}
var _recoveryCodes = [];
var recoveryDisplayEpoch = 0;
var securityCurrentTab = 'password';
function setSecurityStatus(id, text) {
  var node = document.getElementById(id); if (node) node.textContent = text || '';
}
function openSecuritySettings(tab) {
  var overlay = document.getElementById('securitySettingsOverlay'); if (!overlay) return;
  overlay.hidden = false;
  switchSecurityTab(tab || 'password');
  clearSecurityInputs();
  clearRecoveryCodes(false);
  var first = overlay.querySelector(securityCurrentTab === 'recovery' ? '#recoveryCurrent' : '#passwordCurrent');
  if (first) first.focus();
}
function closeSecuritySettings() {
  var overlay = document.getElementById('securitySettingsOverlay'); if (overlay) overlay.hidden = true;
  clearSecurityInputs();
  clearRecoveryCodes(false);
  var trigger = document.getElementById('accountTrigger'); if (trigger) trigger.focus();
}
function switchSecurityTab(tab) {
  var selected = tab === 'recovery' ? 'recovery' : 'password';
  if (selected !== 'recovery') clearRecoveryCodes(false);
  securityCurrentTab = selected;
  var passwordPane = document.getElementById('passwordPane');
  var recoveryPane = document.getElementById('recoveryPane');
  var passwordTab = document.getElementById('passwordTab');
  var recoveryTab = document.getElementById('recoveryTab');
  if (passwordPane) passwordPane.hidden = selected !== 'password';
  if (recoveryPane) recoveryPane.hidden = selected !== 'recovery';
  if (passwordTab) {
    passwordTab.classList.toggle('is-active', selected === 'password');
    passwordTab.setAttribute('aria-selected', selected === 'password' ? 'true' : 'false');
  }
  if (recoveryTab) {
    recoveryTab.classList.toggle('is-active', selected === 'recovery');
    recoveryTab.setAttribute('aria-selected', selected === 'recovery' ? 'true' : 'false');
  }
}
function clearSecurityInputs() {
  ['passwordCurrent', 'passwordNew', 'passwordConfirm', 'recoveryCurrent'].forEach(function (id) {
    var node = document.getElementById(id); if (node) node.value = '';
  });
  setSecurityStatus('passwordChangeStatus', '');
  setSecurityStatus('recoveryStatus', '');
}
function clearRecoveryCodes(announce) {
  recoveryDisplayEpoch += 1;
  _recoveryCodes = [];
  var list = document.getElementById('recoveryCodesList'); if (list) list.textContent = '';
  var result = document.getElementById('recoveryCodesResult'); if (result) result.hidden = true;
  if (announce) setSecurityStatus('recoveryStatus', '恢复码已从页面清除，请确认已妥善保存。');
}
async function submitPasswordChange() {
  var epoch = authEpoch;
  var button = document.getElementById('passwordChangeSubmit');
  var current = (document.getElementById('passwordCurrent') || {}).value || '';
  var next = (document.getElementById('passwordNew') || {}).value || '';
  var confirmValue = (document.getElementById('passwordConfirm') || {}).value || '';
  var invalid = CitrusAuthContract.validatePasswordChange({
    current_password: current, new_password: next, confirm_password: confirmValue
  });
  if (invalid) { setSecurityStatus('passwordChangeStatus', invalid); return; }
  if (button) button.disabled = true;
  setSecurityStatus('passwordChangeStatus', '正在修改…');
  try {
    var response = await accountFetch('/api/v2/auth/password/change', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(CitrusAuthContract.buildPasswordChangePayload({current_password: current, new_password: next})),
      credentials: 'same-origin',
      cache: 'no-store'
    });
    var data = await response.json().catch(function () { return {}; });
    if (epoch !== authEpoch) return;
    if (response.status === 403) {
      setSecurityStatus('passwordChangeStatus', '当前密码不正确。');
      var currentInput = document.getElementById('passwordCurrent'); if (currentInput) currentInput.value = '';
      return;
    }
    if (response.status === 429) { setSecurityStatus('passwordChangeStatus', '操作过于频繁，请稍后再试。'); return; }
    if (response.status === 401) {
      resetAccountState();
      setAuthMode('login');
      setAuthNotice('登录已失效，请重新登录。');
      return;
    }
    if (!response.ok) { setSecurityStatus('passwordChangeStatus', data.detail || '密码修改失败，请稍后重试。'); return; }
    clearSecurityInputs();
    closeSecuritySettings();
    // 服务端已撤销全部会话；本地同样回到登录界面，不保留任何账号缓存。
    resetAccountState();
    setAuthMode('login');
    setAuthNotice('密码已修改，所有设备已退出登录，请使用新密码重新登录。');
  } catch (e) { if (epoch === authEpoch) setSecurityStatus('passwordChangeStatus', '网络异常，请稍后重试。'); }
  finally { if (epoch === authEpoch && button) button.disabled = false; }
}
async function submitRecoveryCodes() {
  var epoch = authEpoch;
  var button = document.getElementById('recoveryCodesButton');
  var current = (document.getElementById('recoveryCurrent') || {}).value || '';
  if (!current) { setSecurityStatus('recoveryStatus', '请输入当前密码。'); return; }
  clearRecoveryCodes(false);
  var displayEpoch = recoveryDisplayEpoch;
  if (button) button.disabled = true;
  setSecurityStatus('recoveryStatus', '正在生成…');
  try {
    var response = await accountFetch('/api/v2/auth/password/recovery-codes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({current_password: current}),
      credentials: 'same-origin',
      cache: 'no-store'
    });
    var data = await response.json().catch(function () { return {}; });
    if (epoch !== authEpoch || displayEpoch !== recoveryDisplayEpoch) return;
    if (response.status === 403) {
      setSecurityStatus('recoveryStatus', '当前密码不正确。');
      var currentInput = document.getElementById('recoveryCurrent'); if (currentInput) currentInput.value = '';
      return;
    }
    if (response.status === 429) { setSecurityStatus('recoveryStatus', '操作过于频繁，请稍后再试。'); return; }
    if (response.status === 401) { resetAccountState(); setAuthMode('login'); setAuthNotice('登录已失效，请重新登录。'); return; }
    if (!response.ok) { setSecurityStatus('recoveryStatus', data.detail || '生成恢复码失败，请稍后重试。'); return; }
    var codes = CitrusAuthContract.normalizeRecoveryCodes(data);
    if (!codes.length) { setSecurityStatus('recoveryStatus', '未收到恢复码，请重试。'); return; }
    _recoveryCodes = codes;
    var list = document.getElementById('recoveryCodesList');
    if (list) list.textContent = codes.join('\n');
    var result = document.getElementById('recoveryCodesResult'); if (result) result.hidden = false;
    var recoveryInput = document.getElementById('recoveryCurrent'); if (recoveryInput) recoveryInput.value = '';
    setSecurityStatus('recoveryStatus', '已生成 8 个恢复码，旧恢复码已失效。请立即保存。');
    if (list) list.focus();
  } catch (e) { if (epoch === authEpoch && displayEpoch === recoveryDisplayEpoch) setSecurityStatus('recoveryStatus', '网络异常，请稍后重试。'); }
  finally { if (epoch === authEpoch && button) button.disabled = false; }
}
window.addEventListener('pagehide', function () {
  // 一次性秘密不留在可恢复的页面快照里。
  clearSecurityInputs();
  clearRecoveryCodes(false);
  clearAuthSecrets();
});
async function copyRecoveryCodes() {
  if (!_recoveryCodes.length) return;
  var textToCopy = _recoveryCodes.join('\n');
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(textToCopy);
      setSecurityStatus('recoveryStatus', '恢复码已复制，请粘贴到安全位置。');
      return;
    }
  } catch (_) {}
  var list = document.getElementById('recoveryCodesList');
  if (list) {
    var range = document.createRange(); range.selectNodeContents(list);
    var selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
  }
  setSecurityStatus('recoveryStatus', '已选中恢复码，请按 Ctrl+C 或 ⌘C 复制。');
}

var sharedMaintenanceTimer = null;
var sharedMaintenanceActive = false;

function displaySharedMaintenance(message) {
  sharedMaintenanceActive = Boolean(message);
  var notice = document.getElementById('shared-maintenance-notice');
  if (notice) {
    if (notice.textContent !== message) notice.textContent = message;
    notice.hidden = !message;
  }
}

async function refreshSharedMaintenance(epoch) {
  if (epoch !== authEpoch || !currentAccount || !document.getElementById('shared-maintenance-notice')) return;
  var delay = 5000;
  try {
    var response = await accountFetch('/api/v2/shared-corpus/maintenance', {cache:'no-store'});
    if (epoch !== authEpoch) return;
    if ([401, 404].includes(response.status)) { displaySharedMaintenance(''); return; }
    if (!response.ok) throw new Error('maintenance status unavailable');
    var status = await response.json();
    if (epoch !== authEpoch) return;
    displaySharedMaintenance(status.state === 'maintenance' ? status.message || '' : '');
    if (status.state === 'disabled') return;
    delay = status.state === 'idle' || status.state === 'active' ? 8000 : 2000;
  } catch (_) {
    if (epoch !== authEpoch) return;
  }
  sharedMaintenanceTimer = setTimeout(function () { refreshSharedMaintenance(epoch); }, delay);
}

async function startAuthenticatedApp() {
  var epoch = authEpoch;
  sessionId = localStorage.getItem(sessionStorageKey());
  var returnUrl = new URL(location.href);
  var returnSession = returnUrl.searchParams.get('figure_session');
  if (returnSession !== null) {
    returnUrl.searchParams.delete('figure_session');
    history.replaceState(history.state, '', returnUrl);
    if (returnSession !== sessionId && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(returnSession)) {
      // Verify ownership and existence before replacing the remembered chat.
      var origin = await accountFetch('/api/v2/session/' + returnSession + '/turns?limit=1', {cache:'no-store'})
        .catch(function () { return null; });
      if (epoch !== authEpoch) return;
      if (origin && origin.ok) {
        sessionId = returnSession;
        localStorage.setItem(sessionStorageKey(), sessionId);
        window.dispatchEvent(new CustomEvent('citrus:session-changed', {detail:{session_id:sessionId}}));
      }
    }
  }
  showAuthGate(false);
  restoreComposerSession();
  var configReady;
  if (currentAccount && currentAccount.role === 'admin') configReady = loadRuntimeConfig();
  else configReady = accountFetch('/api/v2/config').then(function(r) { if (epoch !== authEpoch) return; return r.json(); }).then(function(cfg) { if (epoch !== authEpoch) return;
    _runtimeConfig = cfg; applyAppVersion();
  }).catch(function(){ if (epoch !== authEpoch) return;});
  await ensureSession(); if (epoch !== authEpoch) return;
  clearTimeout(sharedMaintenanceTimer);
  displaySharedMaintenance('');
  void refreshSharedMaintenance(epoch);
  if (currentAccount && currentAccount.role === 'admin' &&
      new URLSearchParams(location.search).get('adminSettings') === '1') {
    if (configReady) await configReady;
    if (epoch !== authEpoch) return;
    history.replaceState(null, '', '/');
    openSettingsModal();
  }
  if (currentAccount && currentAccount.onboarding_required) {
    window.setTimeout(function() { if (epoch === authEpoch) openOnboardingGuide(); }, 300);
  }
}
async function bootstrapAccount() {
  var epoch = authEpoch;
  try {
    var response = await accountFetch('/api/v2/auth/me');
    if (!response.ok) throw new Error('not logged in');
    var data = await response.json(); if (epoch !== authEpoch) return; currentAccount = data.user;
    await startAuthenticatedApp();
  } catch (_) { if (epoch !== authEpoch) return; showAuthGate(true); }
}
window.addEventListener('DOMContentLoaded', bootstrapAccount);
var sessionId = null;
var sessionNavigation = 0;
var chatContainer = document.getElementById('chat-container');
var userInput = document.getElementById('user-input');
var sendBtn = document.getElementById('send-btn');
var sidebarContent = document.getElementById('sidebar-content');
var citationCount = document.getElementById('citation-count');
var _selectedAttachments = [];
window._selectedAccountFileRefs = window._selectedAccountFileRefs || [];
var _attachmentPreviewUrls = [];
var _sentAttachmentPreviewUrls = [];
var _composerNotice = '';
var roundHistory = [];
var _citationsRetry = 0;   // v8.10q: citations 空结果延迟重试计数（防 spawn 时序缺口）
var currentRoundId = 0;
var currentQuery = '';
var totalTokens = 0;
var _agentTokenBuffer = '';
var _agentTokenName = '';

if (typeof marked === 'undefined') window.marked = { parse: function(t) { return t; } };

var composerDraftPrefix = 'citrus.composer.';
var composerDrafts = new Map();
var composerSelections = new Map();
var composerAdmissions = new Map();
var queueSyncUnavailable = false;

function composerDraftKey(jobId) {
  return currentAccount && sessionId && !accountDocumentRetired
    ? composerDraftPrefix + JSON.stringify([currentAccount.user_id, sessionId, jobId || '']) : '';
}

function composerDraft(key) {
  if (!composerDrafts.has(key)) {
    var text = '', pending = null, received = {};
    try {
      if (key) {
        text = sessionStorage.getItem(key) || '';
        pending = JSON.parse(sessionStorage.getItem(key + '.update') || 'null');
        received = JSON.parse(sessionStorage.getItem(key + '.received') || 'null') || {};
      }
    } catch (_) {}
    composerDrafts.set(key, {text: text, revision: 0, pending: pending, received: received});
  }
  return composerDrafts.get(key);
}

function writeComposerDraft(key, text) {
  if (!key) return;
  var draft = composerDraft(key);
  if (draft.text === text) return;
  draft.text = text;
  draft.revision++;
  if (draft.pending && !draft.pending.edited) {
    draft.pending.edited = true;
    savePendingUpdate(key, draft.pending);
  }
  try {
    if (text) sessionStorage.setItem(key, text);
    else sessionStorage.removeItem(key);
  } catch (_) {}
}

function savePendingUpdate(key, pending) {
  composerDraft(key).pending = pending;
  try {
    if (pending) sessionStorage.setItem(key + '.update', JSON.stringify(pending));
    else sessionStorage.removeItem(key + '.update');
  } catch (_) {}
}

function saveReceivedUpdates(key, received) {
  try {
    if (Object.keys(received).length) sessionStorage.setItem(key + '.received', JSON.stringify(received));
    else sessionStorage.removeItem(key + '.received');
  } catch (_) {}
}

function saveComposerInput() {
  writeComposerDraft(composerDraftKey(), userInput.value);
}

function clearComposerDrafts() {
  composerDrafts.clear();
  composerSelections.clear();
  composerAdmissions.clear();
  turnAdmissionChains.clear();
  var recovery = document.getElementById('composer-admission-recovery');
  if (recovery) { recovery.replaceChildren(); recovery.hidden = true; }
  queueSyncUnavailable = false;
  try {
    for (var index = sessionStorage.length - 1; index >= 0; index--) {
      var key = sessionStorage.key(index);
      if (key && (key.indexOf(composerDraftPrefix) === 0 || key.indexOf('citrus.task-draft.') === 0)) sessionStorage.removeItem(key);
    }
  } catch (_) {}
}

function rememberComposerSession() {
  var key = composerDraftKey();
  if (!key) return;
  saveComposerInput();
  composerSelections.set(key, {files: _selectedAttachments, refs: window._selectedAccountFileRefs,
    skills: skillPicker ? skillPicker.capture() : null});
}

function restoreComposerSession(preserve) {
  composerContinuation = null;
  var key = composerDraftKey();
  var saved = composerSelections.get(key);
  _composerNotice = '';
  queueSyncUnavailable = false;
  if (preserve) saveComposerInput();
  else {
    userInput.value = composerDraft(key).text;
    _selectedAttachments = saved ? saved.files : [];
    window._selectedAccountFileRefs = saved ? saved.refs : [];
    if (skillPicker) {
      skillPicker.reset();
      if (saved && saved.skills) skillPicker.restore(saved.skills.items);
    }
  }
  renderAttachmentSelection();
  renderAdmissionRecovery(key);
  if (key) Object.values(readComposerAdmissions(key)).forEach(function (entry) {
    if (entry.state === 'uncertain') lookupDetachedAdmission(key, entry);
  });
}

// Retain the original request identity. File bytes stay in the File selection,
// never in sessionStorage; an unknown outcome is reconciled with a GET only.
function readComposerAdmissions(key) {
  if (!composerAdmissions.has(key)) {
    var pending = Object.create(null);
    try {
      var stored = JSON.parse(sessionStorage.getItem(key + '.admissions') || '[]');
      if (Array.isArray(stored)) stored.forEach(function (entry) {
        if (!entry || !/^crid-[a-z0-9-]+$/.test(entry.id) || typeof entry.text !== 'string') return;
        if (entry.state === 'staged') entry.state = 'rejected';
        if (entry.state === 'posting') entry.state = 'uncertain';
        if (entry.state === 'uncertain' || entry.state === 'rejected') pending[entry.id] = entry;
      });
    } catch (_) {}
    composerAdmissions.set(key, pending);
  }
  return composerAdmissions.get(key);
}
function setComposerAdmission(key, id, entry, refresh) {
  var pending = readComposerAdmissions(key);
  if (entry) pending[id] = entry;
  else delete pending[id];
  try {
    var records = Object.values(pending).map(function (item) { return {id:item.id, text:item.text, state:item.state}; });
    if (records.length) sessionStorage.setItem(key + '.admissions', JSON.stringify(records));
    else sessionStorage.removeItem(key + '.admissions');
  } catch (_) {}
  if (refresh !== false) renderAdmissionRecovery(key);
}
function renderAdmissionRecovery(key) {
  var host = document.getElementById('composer-admission-recovery');
  if (!host || !key || key !== composerDraftKey()) return;
  host.replaceChildren();
  Object.values(readComposerAdmissions(key)).forEach(function (entry) {
    if (entry.state !== 'rejected' && entry.state !== 'uncertain') return;
    var action = document.createElement('button'); action.type = 'button'; action.className = 'history-retry';
    action.textContent = entry.state === 'rejected' ? '恢复未发送的草稿' : '重新确认原消息状态';
    if (entry.text) action.title = entry.text.slice(0, 160);
    action.onclick = function () {
      if (key !== composerDraftKey()) return;
      if (entry.state === 'uncertain') { lookupDetachedAdmission(key, entry); return; }
      userInput.value = [userInput.value, entry.text].filter(Boolean).join('\n\n');
      saveComposerInput(); setComposerAdmission(key, entry.id, null);
      refreshTaskComposer(); userInput.focus();
    };
    host.appendChild(action);
  });
  host.hidden = !host.firstChild;
}
function lookupDetachedAdmission(key, entry) {
  if (key !== composerDraftKey()) return;
  if (entry.lookupPending) return;
  entry.lookupPending = true;
  var epoch = authEpoch, sid = sessionId;
  readTurnQueue(sid, function (error, result) {
    delete entry.lookupPending;
    if (epoch !== authEpoch || accountDocumentRetired || readComposerAdmissions(key)[entry.id] !== entry) return;
    if (!error && result && result.ok && result.body && result.body.client_request_id === entry.id) {
      setComposerAdmission(key, entry.id, null);
      if (key === composerDraftKey()) restoreSessionHistory();
    }
  }, entry.id);
}

function readSupplementDraft(state) {
  var key = composerDraftKey(state.job_id);
  if (!key || !state.job_id) return null;
  var draft = composerDraft(key), received = draft.received;
  var unapplied = [], pruned = false;
  var terminal = CitrusTaskState.isTerminal(state);
  Object.keys(received).forEach(function (version) {
    var notApplied = state.not_applied_updates.some(function (item) { return item.version === Number(version); });
    if (!notApplied && Number(version) <= state.applied_version) {
      delete received[version];
      pruned = true;
    } else if (terminal) {
      unapplied.push('第 ' + version + ' 版 · ' + received[version]);
    }
  });
  if (pruned) saveReceivedUpdates(key, received);
  if (!terminal) return null;
  return {text: [draft.text].concat(unapplied).filter(Boolean).join('\n\n'), pending: draft.pending};
}

function reconcileSupplement(controller) {
  var jobId = controller.store.state.job_id, key = composerDraftKey(jobId);
  if (key && composerDraft(key).pending) void lookupLegacyComposerUpdate({controller: controller, jobId: jobId, key: key, epoch: authEpoch});
}

async function lookupLegacyComposerUpdate(target) {
  var controller = target.controller;
  var draft = composerDraft(target.key), pending = draft.pending;
  if (!pending || controller.isBusy() || controller.store.state.job_id !== target.jobId) return;
  var outcome = await controller.submitUpdate(pending.payload, 'lookup', function () { return false; });
  if (outcome === 'pending' || target.epoch !== authEpoch || draft.pending !== pending) return;
  if (outcome.version && outcome.status !== 'applied' && (outcome.status !== 'not_applied' || draft.text !== pending.text)) {
    draft.received[outcome.version] = pending.text;
    saveReceivedUpdates(target.key, draft.received);
  }
  savePendingUpdate(target.key, null);
  if (outcome.version && outcome.status !== 'not_applied' && !pending.edited && draft.text === pending.text) {
    writeComposerDraft(target.key, '');
  }
  if (controller.owns()) controller.render(controller.store.state);
  refreshTaskComposer();
}

// Dialogs, paste and drag/drop share this size and format gate.
function handleAttachmentSelection(fileList, picker) {
  if (!currentAccount || accountDocumentRetired) return;
  var dropped = Array.from(fileList || []);
  if (picker) picker.value = '';
  var allowed = dropped.filter(function(file) {
    var name = String(file.name || '').toLowerCase();
    return /^image\/(jpeg|png|webp|gif)$/.test(file.type || '') || /\.(jpe?g|png|webp|gif|pdf|docx|csv|tsv|xlsx|xls|xlsm|xlsb)$/.test(name);
  });
  if (allowed.length !== dropped.length) {
    var rejected = dropped.filter(function(file) { return allowed.indexOf(file) < 0; });
    alert('暂不支持这些文件：' + rejected.map(function(file) { return file.name; }).join('、'));
  }
  var added = allowed;
  if (!added.length) return;
  var total = _selectedAttachments.reduce(function(n, f) { return n + f.size; }, 0) +
    added.reduce(function(n, f) { return n + f.size; }, 0);
  if (_selectedAttachments.length + window._selectedAccountFileRefs.length + added.length > 8 || total > 20 * 1024 * 1024) {
    alert('每轮最多 8 个附件，合计不超过 20 MB。');
    return;
  }
  _selectedAttachments = _selectedAttachments.concat(added);
  renderAttachmentSelection();
  if (userInput) { try { userInput.focus({preventScroll: true}); } catch (_) { userInput.focus(); } }
}

function handleAttachmentPaste(event) {
  if (!currentAccount || accountDocumentRetired || event.defaultPrevented) return;
  var clipboard = event.clipboardData;
  if (!clipboard) return;
  // files and items usually describe the same payload. Read only one of them.
  var files = Array.from(clipboard.files || []);
  if (!files.length) {
    Array.from(clipboard.items || []).forEach(function(item) {
      if (item.kind !== 'file' || typeof item.getAsFile !== 'function') return;
      var file = item.getAsFile();
      if (file) files.push(file);
    });
  }
  if (!files.length) return;
  var imageExtensions = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'};
  files = files.map(function(file, index) {
    var extension = imageExtensions[file.type];
    if (!extension || /\.(jpe?g|png|webp|gif)$/i.test(file.name || '')) return file;
    // Clipboard blobs may have no filename, but the existing upload contract
    // requires a supported suffix. Keep ordinary file names unchanged.
    return new File([file], 'clipboard-' + Date.now() + '-' + (index + 1) + extension,
      {type: file.type, lastModified: file.lastModified});
  });
  handleAttachmentSelection(files);
  // Native text insertion preserves selection, caret and undo for mixed paste.
  if (!clipboard.getData('text/plain')) event.preventDefault();
}
if (userInput) userInput.addEventListener('paste', handleAttachmentPaste);

(function wireAttachmentOptions() {
  var panel = document.getElementById('attachment-options');
  if (!panel) return;
  panel.addEventListener('click', function (event) {
    var button = event.target.closest('[data-composer-source]');
    if (!button) return;
    var source = button.getAttribute('data-composer-source');
    var disclosure = document.getElementById('input-tools-menu');
    disclosure.open = false;
    disclosure.querySelector('summary').focus();
    if (source === 'library') {
      if (typeof window.toggleSessionFiles === 'function') window.toggleSessionFiles(true);
      return;
    }
    document.getElementById(source === 'image' ? 'image-attachment-input' : 'attachment-input').click();
  });
})();

// One composer key handler. Enter sends and suppresses its native newline;
// Shift+Enter keeps the native newline, IME (composition / keyCode 229) and
// auto-repeat keydowns never send. The old inline handler is removed from HTML.
function handleComposerKeydown(event) {
  if (!event || event.key !== 'Enter') return;
  if (event.shiftKey) return;
  if (event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (event.repeat) return;
  _composerNotice = '';
  sendMsg();
}
if (userInput) {
  userInput.addEventListener('keydown', handleComposerKeydown);
  userInput.addEventListener('input', function () {
    if (String(userInput.value || '').trim()) _composerNotice = '';
    saveComposerInput();
    refreshTaskComposer();
  });
}

function hasDraggedFiles(event) {
  var types = event.dataTransfer && event.dataTransfer.types;
  return !!types && Array.from(types).indexOf('Files') >= 0;
}

(function enableFileDrop() {
  var panel = document.querySelector('.chat-panel');
  if (!panel) return;
  var dragDepth = 0;
  panel.addEventListener('dragenter', function(event) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    panel.classList.add('file-drag-over');
  });
  panel.addEventListener('dragover', function(event) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  });
  panel.addEventListener('dragleave', function(event) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) panel.classList.remove('file-drag-over');
  });
  panel.addEventListener('drop', function(event) {
    if (!hasDraggedFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    panel.classList.remove('file-drag-over');
    handleAttachmentSelection(event.dataTransfer.files);
  });
})();

function renderAttachmentSelection() {
  var host = document.getElementById('attachment-list');
  _attachmentPreviewUrls.forEach(function(url) { URL.revokeObjectURL(url); });
  _attachmentPreviewUrls = [];
  if (!host) return;
  host.textContent = '';
  host.style.display = (_selectedAttachments.length || window._selectedAccountFileRefs.length) ? 'flex' : 'none';
  _selectedAttachments.forEach(function(file, index) {
    var card = document.createElement('div');
    card.className = 'attachment-preview-card';
    card.title = file.name;
    if (/^image\/(jpeg|png|webp|gif)$/.test(file.type || '') || /\.(jpe?g|png|webp|gif)$/i.test(file.name || '')) {
      var previewUrl = URL.createObjectURL(file);
      _attachmentPreviewUrls.push(previewUrl);
      var image = document.createElement('img');
      image.className = 'attachment-preview-image';
      image.src = previewUrl;
      image.alt = file.name;
      card.appendChild(image);
    } else {
      var icon = document.createElement('span');
      icon.className = 'attachment-preview-icon';
      icon.textContent = /\.pdf$/i.test(file.name || '') ? 'PDF' :
        /\.xlsx$/i.test(file.name || '') ? 'XLSX' : /\.xlsb$/i.test(file.name || '') ? 'XLSB' : /\.xlsm?$/i.test(file.name || '') ? 'XLS' : /\.tsv$/i.test(file.name || '') ? 'TSV' : /\.csv$/i.test(file.name || '') ? 'CSV' : 'DOC';
      card.appendChild(icon);
    }
    var label = document.createElement('span');
    label.className = 'attachment-preview-name';
    label.textContent = file.name;
    card.appendChild(label);
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'attachment-preview-remove';
    remove.setAttribute('aria-label', '移除 ' + file.name);
    remove.title = '移除附件';
    remove.textContent = '×';
    remove.onclick = function() { _selectedAttachments.splice(index, 1); renderAttachmentSelection(); if (userInput) { try { userInput.focus({preventScroll: true}); } catch (_) { userInput.focus(); } } };
    card.appendChild(remove);
    host.appendChild(card);
  });
  window._selectedAccountFileRefs.forEach(function(file, index) {
    var card = document.createElement('div');
    card.className = 'attachment-preview-card library-reference-chip';
    card.title = file.filename || '资料库文件';
    var icon = document.createElement('span');
    icon.className = 'attachment-preview-icon';
    icon.textContent = String(file.filename || '').split('.').pop().toUpperCase().slice(0, 5) || 'FILE';
    card.appendChild(icon);
    var label = document.createElement('span');
    label.className = 'attachment-preview-name';
    label.textContent = file.filename || '资料库文件';
    card.appendChild(label);
    var remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'attachment-preview-remove';
    remove.setAttribute('aria-label', '移除 ' + (file.filename || '资料库文件'));
    remove.textContent = '×';
    remove.onclick = function() { window._selectedAccountFileRefs.splice(index, 1); renderAttachmentSelection(); if (userInput) { try { userInput.focus({preventScroll: true}); } catch (_) { userInput.focus(); } } };
    card.appendChild(remove); host.appendChild(card);
  });
  refreshTaskComposer();
}

function appendSentLibraryReferences(contentEl, files) {
  if (!contentEl || !files || !files.length) return;
  var gallery = document.createElement('div'); gallery.className = 'sent-attachment-gallery sent-library-gallery';
  files.forEach(function(file) {
    var card = document.createElement('button'); card.type = 'button';
    card.className = 'sent-attachment-card sent-library-card';
    card.title = file.filename || '资料库文件';
    var icon = document.createElement('span'); icon.className = 'sent-attachment-icon';
    icon.textContent = String(file.filename || '').split('.').pop().toUpperCase().slice(0, 5) || 'FILE';
    var label = document.createElement('span'); label.className = 'sent-attachment-name';
    label.textContent = file.filename || '资料库文件';
    card.appendChild(icon); card.appendChild(label);
    card.onclick = function() { if (window.openAccountFilePreview) window.openAccountFilePreview(file); };
    gallery.appendChild(card);
  });
  contentEl.appendChild(gallery);
}

function appendRestoredAttachmentCards(contentEl, files) {
  if (!contentEl || !Array.isArray(files) || !files.length) return;
  var gallery = document.createElement('div'); gallery.className = 'sent-attachment-gallery restored-attachment-gallery';
  files.forEach(function(file) {
    var card = document.createElement('button'); card.type = 'button';
    card.className = 'sent-attachment-card sent-library-card';
    card.title = file.filename || '附件';
    if ((file.kind === 'image' || /^image\//.test(file.mime_type || '')) && file.preview_url) {
      var img = document.createElement('img'); img.className = 'sent-attachment-image';
      img.src = file.preview_url; img.alt = file.filename || '上传图片'; card.appendChild(img);
    } else {
      var icon = document.createElement('span'); icon.className = 'sent-attachment-icon';
      icon.textContent = String(file.filename || '').split('.').pop().toUpperCase().slice(0, 5) || 'FILE';
      card.appendChild(icon);
    }
    var label = document.createElement('span'); label.className = 'sent-attachment-name';
    label.textContent = file.filename || '附件'; card.appendChild(label);
    card.onclick = function() { if (window.openAccountFilePreview) window.openAccountFilePreview(file); };
    gallery.appendChild(card);
  });
  contentEl.appendChild(gallery);
}

function appendSentAttachmentPreviews(contentEl, files) {
  if (!contentEl || !files || !files.length) return;
  var gallery = document.createElement('div');
  gallery.className = 'sent-attachment-gallery';
  files.forEach(function(file) {
    var card = document.createElement('div');
    card.className = 'sent-attachment-card';
    card.title = file.name;
    if (/^image\/(jpeg|png|webp|gif)$/.test(file.type || '') || /\.(jpe?g|png|webp|gif)$/i.test(file.name || '')) {
      var previewUrl = URL.createObjectURL(file);
      _sentAttachmentPreviewUrls.push(previewUrl);
      var image = document.createElement('img');
      image.className = 'sent-attachment-image';
      image.src = previewUrl;
      image.alt = file.name;
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'sent-attachment-open-image';
      button.setAttribute('aria-label', '预览图片 ' + file.name);
      button.onclick = function () {
        if (window.openSentImagePreview) window.openSentImagePreview(file);
      };
      button.appendChild(image);
      card.appendChild(button);
    } else {
      var icon = document.createElement('span');
      icon.className = 'sent-attachment-icon';
      icon.textContent = /\.pdf$/i.test(file.name || '') ? 'PDF' :
        /\.xlsx$/i.test(file.name || '') ? 'XLSX' : /\.xlsb$/i.test(file.name || '') ? 'XLSB' : /\.xlsm?$/i.test(file.name || '') ? 'XLS' : /\.tsv$/i.test(file.name || '') ? 'TSV' : /\.csv$/i.test(file.name || '') ? 'CSV' : 'DOC';
      card.appendChild(icon);
    }
    var label = document.createElement('span');
    label.className = 'sent-attachment-name';
    label.textContent = file.name;
    card.appendChild(label);
    if (/\.pdf$/i.test(file.name || '')) {
      var review = document.createElement('button');
      review.type = 'button'; review.className = 'paper-review-btn'; review.textContent = '引用标记';
      review.onclick = function() { reviewUploadedPdf(file, card); };
      card.appendChild(review);
    }
    gallery.appendChild(card);
  });
  contentEl.appendChild(gallery);
}

var paperReviewSessionVersion = 0;
window.addEventListener('citrus:session-changed', function() { paperReviewSessionVersion += 1; });
async function reviewUploadedPdf(file, card) {
  var epoch = authEpoch, sid = sessionId, sessionVersion = paperReviewSessionVersion;
  function ownsReview() {
    return epoch === authEpoch && sid === sessionId && sessionVersion === paperReviewSessionVersion &&
      card.isConnected !== false && card.querySelector('.paper-review-result') === panel;
  }
  if (!sid || !currentAccount) return;
  var old = card.querySelector('.paper-review-result'); if (old) old.remove();
  var panel = document.createElement('div'); panel.className = 'paper-review-result'; panel.textContent = '正在处理 PDF 引用标记…'; card.appendChild(panel);
  try {
    var payload = {name:file.name, data:await fileAsDataUrl(file)};
    if (!ownsReview()) return;
    var response = await accountFetch('/api/v2/session/' + encodeURIComponent(sid) + '/paper-review', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    var result = await response.json(); if (!ownsReview()) return; if (!response.ok) throw new Error(result.detail || '解析失败');
    panel.textContent = '';
    var external = result.verification_mode === 'external';
    var status = document.createElement('div');
    status.textContent = external || result.status === 'incomplete' ? result.reason :
      ((result.references || []).length + ' 条参考文献；' + (result.reason || '正文编号引用已标亮。'));
    panel.appendChild(status);
    var highlightedUrl = safeProductUrl(result.highlighted_pdf_url, true);
    if (highlightedUrl) {
      var artifactId = highlightedUrl.split('/').pop();
      var link = document.createElement('a'); link.href = '/api/v2/session/' + encodeURIComponent(sid) + '/paper-preview/' + encodeURIComponent(artifactId); link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = '预览高亮 PDF'; panel.appendChild(link);
      var download = document.createElement('a'); download.href = highlightedUrl; download.textContent = ' · 下载副本'; panel.appendChild(download);
    }
    if (external) {
      var verify = document.createElement('button');
      verify.type = 'button'; verify.className = 'paper-review-btn'; verify.textContent = '核验文献';
      var notice = document.createElement('div'); notice.setAttribute('role', 'status');
      verify.onclick = async function () {
        if (!ownsReview() || verify.disabled) return;
        verify.disabled = true;
        notice.textContent = '';
        try {
          var catalog = await accountFetch('/api/v2/skills');
          if (!catalog.ok) throw new Error('读取文献核验场景失败，请稍后重试。');
          var body = await catalog.json();
          if (!ownsReview()) return;
          var skill = body && Array.isArray(body.skills) && body.skills.find(function (row) {
            return row && row.id === 'literature-verification' && typeof row.version === 'string' &&
              row.version.length > 0 && /^[a-f0-9]{64}$/.test(row.sha256);
          });
          if (!skill) throw new Error('当前没有可用的文献核验场景。');
          if (!await sendMsg({file: file, skill: {id: skill.id, version: skill.version, sha256: skill.sha256}}) && ownsReview()) {
            notice.textContent = '当前无法提交核验，请稍后重试。';
          }
        } catch (error) {
          if (ownsReview()) notice.textContent = error.message || '无法打开文献核验场景，请稍后重试。';
        } finally {
          if (ownsReview()) verify.disabled = false;
        }
      };
      panel.appendChild(verify); panel.appendChild(notice);
      return;
    }
    var auditBase = result.audit_id ? '/api/v2/session/' + encodeURIComponent(sid) + '/reference-audits/' + encodeURIComponent(result.audit_id) : '';
    var rowNodes = {}, rowIndex = {};
    var summary = document.createElement('div'); summary.className = 'paper-audit-summary';
    var controls = document.createElement('div'); controls.className = 'paper-audit-controls';
    function auditButton(label, handler) {
      var button = document.createElement('button'); button.type = 'button'; button.textContent = label;
      button.onclick = handler; controls.appendChild(button); return button;
    }
    function auditLink(label, format) {
      var a = document.createElement('a'); a.href = auditBase + '/export?format=' + format;
      a.textContent = label; a.setAttribute('download', ''); controls.appendChild(a); return a;
    }
    function auditStatusText(row) {
      var verdict = row && row.verdict;
      if (verdict) return ({verified:'已核实', correction_required:'建议更正', not_confirmed:'尚未确认'})[verdict.decision] || '尚未确认';
      var state = (row && row.result && row.result.status) || (row && row.status) || 'not_checked';
      return ({metadata_matched:'已匹配', metadata_mismatch:'字段冲突', metadata_uncertain:'字段待确认',
        title_candidate:'标题候选待确认', web_candidate:'网页候选待确认',
        not_found_in_checked_registries:'登记库未查到', network_error:'联网失败，可重试',
        not_checked:'未核验'})[state] || state;
    }
    function paintFields(target, row) {
      var fields = row && row.result && row.result.fields || {};
      var labels = {doi:'DOI', title:'标题', authors:'作者', year:'年份', journal:'期刊', volume:'卷', issue:'期', pages:'页码', article_number:'文章号'};
      var states = {conflicting:'冲突', missing:'登记字段缺失', uncertain:'待确认'};
      target.replaceChildren();
      var problems = Object.keys(labels).filter(function(key) { return fields[key] && states[fields[key].status]; });
      target.hidden = !problems.length;
      if (!problems.length) return;
      var heading = document.createElement('summary');
      heading.textContent = problems.map(function(key) { return labels[key] + '：' + states[fields[key].status]; }).join('；');
      target.appendChild(heading);
      problems.forEach(function(key) {
        var check = fields[key], detail = document.createElement('div');
        detail.textContent = labels[key] + '（' + states[check.status] + '）：提交 ' + String(check.submitted || '—').slice(0, 500) +
          '；登记 ' + String(check.registered || '—').slice(0, 500) + (check.reason ? '；' + String(check.reason).slice(0, 200) : '');
        target.appendChild(detail);
      });
    }
    function paintProviders(target, row) {
      target.replaceChildren();
      var result = row && row.result || {}, facts = result.facts || {};
      var providers = facts.providers || {}, errors = facts.errors || result.errors || {};
      var evidence = facts.http_evidence || result.evidence || [];
      var names = Object.keys(providers);
      target.hidden = !names.length && !Object.keys(errors).length && !evidence.length;
      if (target.hidden) return;
      var heading = document.createElement('summary'); heading.textContent = '登记库查询与证据'; target.appendChild(heading);
      names.forEach(function(name) {
        var state = {ok:'已返回', not_found:'本次未查到', error:'查询失败'}[providers[name]] || String(providers[name]);
        var text = document.createElement('div'); text.textContent = name + '：' + state +
          (errors[name] ? '；' + String(errors[name]).slice(0, 300) : ''); target.appendChild(text);
      });
      Object.keys(errors).filter(function(name) { return names.indexOf(name) < 0; }).forEach(function(name) {
        var text = document.createElement('div'); text.textContent = name + '：' + String(errors[name]).slice(0, 300); target.appendChild(text);
      });
      var count = document.createElement('div'); count.textContent = '已存查询证据 ' + (Array.isArray(evidence) ? evidence.length : 0) + ' 项；完整记录与原始坐标见 JSON / XLSX。'; target.appendChild(count);
      (Array.isArray(result.provider_conflicts) ? result.provider_conflicts : []).forEach(function(conflict) {
        if (!conflict || typeof conflict !== 'object') return;
        var text = document.createElement('div'); text.textContent = '登记库之间的字段差异：' + String(conflict.provider || '') + ' · ' +
          (Array.isArray(conflict.fields) ? conflict.fields.join('、') : '待复核'); target.appendChild(text);
      });
    }
    var resumeButton = null, exportJson = null, exportXlsx = null, cancelButton = null, verifyAll = null;
    var audit = null, running = false, cancelling = false, stopRequested = false, auditRevision = 0;
    function paintAudit() {
      if (!ownsReview()) return;
      var view = audit.view || {}, coverage = view.coverage || {};
      var reasons = {deadline:'本批时间已到，可继续核验', budget_exhausted:'请求预算已用完',
        network_disabled:'联网已关闭', already_running:'已有核验正在运行，请稍后继续',
        cancelled:'已取消，已完成的记录保留', cancelled_use_resume:'已取消，可继续核验',
        lease_lost:'运行状态已改变，请继续核验'};
      var verdicts = view.verdict_counts;
      summary.textContent = '已处理 ' + (coverage.processed || 0) + '/' + (coverage.total || 0) +
        (verdicts ? '；已核实 ' + verdicts.verified + '，建议更正 ' + verdicts.correction_required + '，尚未确认 ' + verdicts.not_confirmed : '') +
        (coverage.pending ? '，待核验 ' + coverage.pending : '') +
        (coverage.retryable ? '，可重试 ' + coverage.retryable : '') +
        (view.extraction && view.extraction.status !== 'complete' ? '；条目提取不完整，仅核验已提取部分' : '') +
        (audit.note ? '；' + audit.note : reasons[audit.reason] ? '；' + reasons[audit.reason] : '');
      Object.keys(rowNodes).forEach(function(key) {
        var row = rowIndex[key], nodes = rowNodes[key];
        nodes.badge.textContent = auditStatusText(row);
        paintFields(nodes.fields, row);
        paintProviders(nodes.providers, row);
      });
      var remaining = coverage.pending > 0 || coverage.retryable > 0;
      verifyAll.disabled = running || cancelling || !remaining;
      resumeButton.hidden = !remaining;
      resumeButton.disabled = running || cancelling || !remaining;
      cancelButton.disabled = cancelling || (!running && !remaining);
      if (exportJson) exportJson.hidden = !(coverage.processed > 0);
      if (exportXlsx) exportXlsx.hidden = exportJson.hidden;
    }
    function mergeAudit(view) {
      audit.view = view;
      (view.rows || []).forEach(function(row) { rowIndex[row.record_index] = row; });
    }
    async function refreshAudit(revision) {
      if (!ownsReview() || revision !== auditRevision) return null;
      var response = await accountFetch(auditBase + '?limit=5000', {headers:{'Accept':'application/json'}});
      var detail = await response.json();
      if (!ownsReview() || revision !== auditRevision) return null;
      if (!response.ok) throw new Error(detail.detail || '无法读取核验进度');
      rowIndex = {}; mergeAudit(detail); paintAudit(); return detail;
    }
    if (auditBase) {
      audit = {id: result.audit_id, view: result.audit || {}, reason:'', note:''};
      verifyAll = auditButton('核验全部', function() { return runBatches(false); });
      resumeButton = auditButton('继续核验', function() { return runBatches(true); });
      cancelButton = auditButton('取消', async function() {
        if (!ownsReview() || cancelling) return;
        // Fence both an outstanding run response and the next automatic batch.
        stopRequested = true; cancelling = true;
        var revision = ++auditRevision;
        audit.note = '正在取消…'; paintAudit();
        try {
          var response = await accountFetch(auditBase + '/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
          var cancelled = await response.json();
          if (!ownsReview() || revision !== auditRevision) return;
          if (!response.ok) throw new Error(cancelled.detail || '请稍后重试');
          mergeAudit(cancelled); audit.reason = 'cancelled'; audit.note = '';
          await refreshAudit(revision);
        } catch (error) {
          if (ownsReview() && revision === auditRevision) audit.note = '取消失败：' + (error.message || '请稍后重试');
        } finally {
          if (ownsReview() && revision === auditRevision) { cancelling = false; paintAudit(); }
        }
      });
      exportJson = auditLink('导出 JSON', 'json');
      exportXlsx = auditLink('导出 XLSX', 'xlsx');
      panel.appendChild(summary); panel.appendChild(controls);
      async function runBatches(resume) {
        if (!ownsReview() || running || cancelling) return;
        var batches = 0, revision = ++auditRevision;
        running = true; stopRequested = false; audit.note = ''; audit.reason = ''; paintAudit();
        try {
          while (ownsReview() && !stopRequested && batches < 30) {
            batches += 1;
            var response = await accountFetch(auditBase + '/run', {method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({max_rows:20, time_budget:45, resume:resume})});
            var run = await response.json();
            if (!ownsReview() || stopRequested || revision !== auditRevision) return;
            if (run.coverage) { mergeAudit(run); audit.reason = run.run_reason || ''; paintAudit(); }
            if (!response.ok) throw new Error(run.detail || (response.status === 409 ? '运行状态冲突，请稍后继续核验' : '核验失败'));
            resume = false;
            // Run responses contain only the first 100 records. Read the full
            // bounded inventory so later record_index badges update each batch.
            await refreshAudit(revision);
            if (!ownsReview() || stopRequested || revision !== auditRevision) return;
            var coverage = audit.view.coverage || {};
            if (!coverage.pending || (run.run_reason && run.run_reason !== 'batch_finished')) break;
          }
          if (batches === 30 && (audit.view.coverage || {}).pending) audit.note = '本次批次已结束，可继续核验';
        } catch (error) {
          if (ownsReview() && !stopRequested && revision === auditRevision) audit.note = '核验中断：' + (error.message || '请稍后重试');
        } finally {
          if (ownsReview()) { running = false; paintAudit(); }
        }
      }
      refreshAudit(auditRevision).catch(function(error) {
        if (ownsReview() && auditRevision === 0) { audit.note = '进度读取失败：' + (error.message || '请稍后重试'); paintAudit(); }
      });
    }
    (result.references || []).forEach(function(ref) {
      var row = document.createElement('div'); row.className = 'paper-reference-row';
      var text = document.createElement('span'); text.textContent = '[' + ref.number + '] ' + ref.text; row.appendChild(text);
      if (auditBase && typeof ref.record_index === 'number') {
        var badge = document.createElement('span'); badge.className = 'paper-audit-state'; badge.textContent = '未核验';
        var fields = document.createElement('details'); fields.className = 'paper-audit-fields'; fields.hidden = true;
        var providers = document.createElement('details'); providers.className = 'paper-audit-providers'; providers.hidden = true;
        rowNodes[ref.record_index] = {badge:badge, fields:fields, providers:providers}; row.appendChild(badge); row.appendChild(fields); row.appendChild(providers);
      }
      if (ref.doi) {
        var get = document.createElement('button'); get.textContent = '找开放 PDF';
        get.onclick = async function() {
          if (!ownsReview()) return;
          get.disabled = true;
          try {
            var r = await accountFetch('/api/v2/session/' + encodeURIComponent(sid) + '/citation-open-pdf', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({doi:ref.doi})});
            var d = await r.json(); if (!ownsReview()) return;
            var pdfUrl = r.ok && d && safeProductUrl(d.download_url, true);
            if (pdfUrl) {
              get.textContent = '下载 PDF';
              get.onclick = function() { if (ownsReview()) window.open(pdfUrl, '_blank', 'noopener,noreferrer'); };
            } else get.textContent = d && (d.message || d.status) || '暂无可用下载';
          } catch (_) { if (ownsReview()) get.textContent = '获取失败，可重试'; }
          finally { if (ownsReview()) get.disabled = false; }
        };
        row.appendChild(get);
      }
      panel.appendChild(row);
    });
    if (audit) { mergeAudit(audit.view); paintAudit(); }
  } catch (e) { if (!ownsReview()) return; panel.textContent = '解析失败：' + (e.message || '请稍后重试'); }
}

// API URLs are untrusted display data. Preserve authorized local downloads and
// ordinary web citations without allowing executable or protocol-relative links.
function safeProductUrl(value, localOnly) {
  if (typeof value !== 'string' || /[\s\\<>"'\x00-\x1f]/.test(value)) return '';
  if (/^\/api\/v2\/[A-Za-z0-9_./?=&%+-]+$/.test(value) && !/%(?:2f|5c|2e)/i.test(value) && !value.includes('/../')) return value;
  return !localOnly && /^https?:\/\/[^/]+(?:\/|$)/i.test(value) ? value : '';
}
function sourceEvidenceText(item) {
  item = item || {};
  var provenance = item.provenance && typeof item.provenance === 'object' ? item.provenance : {};
  var level = item.evidence_level || provenance.evidence_level;
  var labels = {metadata:'元数据', snippet:'检索片段', summary:'摘要', abstract:'摘要',
    passage:'原文片段', full_text:'全文', fulltext:'全文', fulltext_confirmed:'已获取全文'};
  var origin = item.origin || provenance.origin;
  var ref = item.source_ref || provenance.source_ref || provenance.ref;
  var parts = ['证据层级：' + (labels[level] || (typeof level === 'string' ? level : '未标注'))];
  if (origin) parts.push('来源：' + String(origin));
  if (ref) parts.push('证据记录：' + String(ref));
  var locator = item.locator || provenance.locator;
  if (locator && typeof locator === 'object') {
    ['page', 'page_number', 'section', 'chunk_id', 'chunk_index'].forEach(function(key) {
      if (locator[key] != null) parts.push(key + '：' + String(locator[key]));
    });
  }
  return parts.join(' · ');
}

function clearSentAttachmentPreviewUrls() {
  _sentAttachmentPreviewUrls.forEach(function(url) { URL.revokeObjectURL(url); });
  _sentAttachmentPreviewUrls = [];
}

function fileAsDataUrl(file) {
  var epoch = authEpoch;
  return new Promise(function(resolve, reject) {
    var reader = new FileReader();
    accountFileReaders.add(reader);
    function finish(error) {
      accountFileReaders.delete(reader);
      reader.onload = reader.onerror = reader.onabort = null;
      if (epoch !== authEpoch) error = staleAccountError();
      if (error) reject(error); else resolve(String(reader.result || ''));
    }
    reader.onload = function() { finish(); };
    reader.onerror = function() { finish(new Error('无法读取附件：' + file.name)); };
    reader.onabort = function() { finish(staleAccountError()); };
    try { reader.readAsDataURL(file); } catch (error) { finish(error); }
  });
}
if (typeof marked.setOptions === 'function') {
  marked.setOptions({ breaks: true });
}

/* ─── XSS 防护: 所有动态文本入 innerHTML 前转义 (v8.3.3) ─── */
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
/* v8.4.3: 代码块内容只转义 & < >（引号安全——处于 <pre><code> 标签内部，
   全量转义会把代码里的 ' 显示成 &#39;，如 5'上游调控区） */
function displayToolName(name) {
  var names = {
    deepseek_search: '网页搜索', deepseek_fetch: '读取网页',
    deepseek_fetch_paper: '获取论文全文',
    local_hybrid_search: '本地检索', local_bm25_search: '本地检索',
    local_dense_search: '本地检索', citrus_rag_search: '本地检索',
    read_account_file: '查阅资料', read_tool_result: '读取工具结果', update_task_plan: '更新工作计划', write_todos: '更新工作计划',
    draw_svg: '绘图',
    inspect_uploaded_table: '读取表格', audit_uploaded_table: '数据审查',
    audit_uploaded_data_bundle: '数据审查', audit_uploaded_data: '数据审查',
    verify_uploaded_references: '文献核验', fetch_and_audit_open_supplements: '获取补充数据',
    export_literature_evidence: '导出文献证据', export_paper_analysis: '导出论文解读',
    export_experiment_design: '导出实验方案', save_research_note: '保存课题记录',
    recall_research_notes: '查阅课题记录', export_research_notes: '导出课题记录',
    export_corpus_gap_report: '导出语料清单', '读取历史结果': '读取工具结果'
  };
  var value = String(name || '');
  if (names[value]) return names[value];
  if (/^[\u3400-\u9fff·\s]{2,16}$/.test(value)) return value;
  return '科研工具';
}
function toggleToolTrace(id, trigger) {
  var panel = document.getElementById(String(id || ''));
  if (!panel) return;
  var open = panel.style.display === 'none' || !panel.style.display;
  panel.style.display = open ? 'block' : 'none';
  if (trigger) trigger.setAttribute('aria-expanded', String(open));
}
function escapeHtmlCode(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function renderSvgBlock(source) {
  var raw = String(source || '').trim();
  if (!raw || raw.length > 100000 || /<!DOCTYPE|<!ENTITY/i.test(raw)) {
    return '<div class="svg-error">SVG 图形无效或尺寸超过限制。</div>';
  }
  try {
    var doc = new DOMParser().parseFromString(raw, 'image/svg+xml');
    var root = doc.documentElement;
    var ns = 'http://www.w3.org/2000/svg';
    if (!root || root.localName.toLowerCase() !== 'svg' ||
        (root.namespaceURI && root.namespaceURI !== ns) ||
        doc.getElementsByTagName('parsererror').length) throw new Error('invalid SVG');
    var allowedTags = new Set(['svg','g','path','rect','circle','ellipse','line','polyline','polygon',
      'text','tspan','title','desc','defs','lineargradient','radialgradient','stop','clippath','marker',
      'use','symbol','filter','fegaussianblur','feoffset','feflood','fecomposite','femerge',
      'femergenode','fecolormatrix','feblend','fecomponenttransfer','fefunca','fefuncr','fefuncg','fefuncb']);
    var attrNames = {
      'viewbox':'viewBox','preserveaspectratio':'preserveAspectRatio','stroke-width':'stroke-width',
      'stroke-linecap':'stroke-linecap','stroke-linejoin':'stroke-linejoin','stroke-dasharray':'stroke-dasharray',
      'stroke-dashoffset':'stroke-dashoffset','fill-opacity':'fill-opacity','stroke-opacity':'stroke-opacity',
      'font-family':'font-family','font-size':'font-size','font-weight':'font-weight','font-style':'font-style','text-anchor':'text-anchor',
      'dominant-baseline':'dominant-baseline','gradientunits':'gradientUnits','gradienttransform':'gradientTransform',
      'stop-color':'stop-color','stop-opacity':'stop-opacity','clippathunits':'clipPathUnits',
      'markerwidth':'markerWidth','markerheight':'markerHeight','markerunits':'markerUnits',
      'marker-start':'marker-start','marker-mid':'marker-mid','marker-end':'marker-end','aria-label':'aria-label',
      'clip-path':'clip-path','role':'role','id':'id','x':'x','y':'y','x1':'x1','y1':'y1','x2':'x2','y2':'y2','cx':'cx','cy':'cy',
      'r':'r','rx':'rx','ry':'ry','width':'width','height':'height','d':'d','points':'points',
      'fill':'fill','stroke':'stroke','opacity':'opacity','transform':'transform','offset':'offset',
      'style':'style','class':'class','href':'href','xlink:href':'href','filter':'filter'
    };
    var safeStyleProps = new Set(['fill','stroke','stroke-width','stroke-linecap','stroke-linejoin',
      'stroke-dasharray','stroke-dashoffset','fill-opacity','stroke-opacity','opacity','font-family',
      'font-size','font-weight','font-style','text-anchor','dominant-baseline','stop-color','stop-opacity',
      'clip-path','filter','marker-start','marker-mid','marker-end']);
    function safeStyle(value) {
      var kept = [];
      String(value).split(';').forEach(function(part) {
        var colon = part.indexOf(':');
        if (colon < 1) return;
        var name = part.slice(0, colon).trim().toLowerCase();
        var val = part.slice(colon + 1).trim();
        if (!safeStyleProps.has(name) || !val || /[<>\\{}]/.test(val) ||
            /expression|javascript:|data:|@import/i.test(val)) return;
        if (/url\s*\(/i.test(val) && !/^url\(\s*#[A-Za-z][\w:.-]{0,100}\s*\)$/i.test(val)) return;
        kept.push(name + ':' + val);
      });
      return kept.join(';');
    }
    var count = 0;
    function copyNode(node) {
      if (++count > 5000) throw new Error('SVG complexity limit');
      if (node.nodeType === 3 || node.nodeType === 4) return document.createTextNode(node.nodeValue || '');
      if (node.nodeType !== 1 || (node.namespaceURI && node.namespaceURI !== ns) ||
          !allowedTags.has(node.localName.toLowerCase())) return null;
      var out = document.createElementNS(ns, node.localName);
      for (var i = 0; i < node.attributes.length; i++) {
        var attr = node.attributes[i], key = attr.name.toLowerCase(), canonical = attrNames[key];
        if (!canonical) continue;
        var value = attr.value;
        if (value.length > 4000 || /[<>\u0000-\u001f]/.test(value)) continue;
        if (canonical === 'style') { value = safeStyle(value); if (!value) continue; }
        if (canonical === 'class' && !/^[A-Za-z0-9 _-]{1,200}$/.test(value)) continue;
        if (canonical === 'href' && !/^#[A-Za-z][\w:.-]{0,100}$/.test(value)) continue;
        if (canonical === 'id' && !/^[A-Za-z][\w:.-]{0,100}$/.test(value)) continue;
        if (/url\s*\(/i.test(value) && !/^url\(\s*#[A-Za-z][\w:.-]{0,100}\s*\)$/i.test(value)) continue;
        if (/javascript:|data:/i.test(value)) continue;
        out.setAttribute(canonical, value);
      }
      for (var child = node.firstChild; child; child = child.nextSibling) {
        var safeChild = copyNode(child);
        if (safeChild) out.appendChild(safeChild);
      }
      return out;
    }
    var safe = copyNode(root);
    if (!safe.getAttribute('viewBox') && !safe.getAttribute('width')) {
      throw new Error('SVG needs a viewBox or width');
    }
    return '<figure class="svg-figure" role="group">' + new XMLSerializer().serializeToString(safe) + '</figure>';
  } catch (_) {
    return '<div class="svg-error">SVG 图形无法安全显示，请检查 SVG 代码。</div>';
  }
}

function citedHrefKey(url) {
  try { var parsed = new URL(url, location.origin); parsed.hash = ''; return parsed.href; }
  catch (_) { return ''; }
}

function isPrivateCitationRoute(href) {
  return /^\/api\/v2\/(?:corpus\/source\b|shared-corpus\/documents\b)/.test(href);
}

function renderCitationBubbles(container, bubbles, requestId, sid, producerJobId) {
  container.replaceChildren();
  container.classList.add('turn-citation-bubbles');
  container.setAttribute('role', 'group'); container.setAttribute('aria-label', '本轮引证');
  var seen = new Set(), links = [];
  (Array.isArray(bubbles) ? bubbles : []).forEach(function (source) {
    var title = String(source.title || '来源');
    if (source.kind === 'web' && safeProductUrl(source.url, false) && /^https?:/i.test(source.url)) {
      var webKey = citedHrefKey(source.url);
      if (!webKey || seen.has(webKey)) return;
      seen.add(webKey);
      var link = document.createElement('a'); link.className = 'turn-citation-bubble';
      link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = '联网：' + title; links.push(link);
    } else if (source.kind === 'local' || source.kind === 'shared') {
      var id = String(source.reader_id || source.record_key || source.ref_id || '');
      if (!id || seen.has(id) || !requestId || !sid) return;
      seen.add(id);
      var button = document.createElement('button'); button.type = 'button';
      button.className = 'turn-citation-bubble'; button.textContent = '本地：' + title;
      button.addEventListener('click', function () {
        if (window.CitrusCorpusReader && typeof window.CitrusCorpusReader.openCitation === 'function') {
          window.CitrusCorpusReader.openCitation(source, {sessionId: sid, requestId: requestId, producerJobId: producerJobId || ""});
        }
      });
      links.push(button);
    }
  });
  // Preserve every verified citation and its readback scope without a wall of links.
  var initial = 5;
  links.slice(0, initial).forEach(function (link) { container.appendChild(link); });
  if (links.length > initial) {
    var more = document.createElement('details'); more.className = 'turn-citation-more';
    var summary = document.createElement('summary');
    summary.textContent = '另外 ' + (links.length - initial) + ' 条引证';
    more.appendChild(summary);
    var rest = document.createElement('div'); rest.className = 'turn-citation-rest';
    links.slice(initial).forEach(function (link) { rest.appendChild(link); });
    more.appendChild(rest); container.appendChild(more);
  }
  container.hidden = !links.length;
}

// Markdown is untrusted prose. Only these authenticated preview routes may
// auto-fetch. Everything else is either a user-activated web link or alt text.
function isTrustedProductImage(url) {
  if (url.origin !== location.origin || url.hash) return false;
  if (/^\/api\/v2\/(?:session\/[A-Za-z0-9_-]{8,128}\/)?files\/[a-f0-9]{32}\/preview$/.test(url.pathname)) {
    return !url.search || url.search === '?library_tile=1';
  }
  if (/^\/api\/v2\/figures\/projects\/[a-f0-9]{32}\/preview\.(?:svg|png)$/.test(url.pathname)) {
    return !url.search || /^\?v=\d+$/.test(url.search);
  }
  return /^\/api\/v2\/figure-workbench\/projects\/[a-f0-9]{32}\/preview\/(?:svg|png)$/.test(url.pathname) &&
    /^\?revision=\d+$/.test(url.search);
}

function renderMarkdownImage(token, title, text) {
  var raw = typeof token === 'object' && token ? token.href : token;
  var label = escapeHtml((typeof token === 'object' && token ? token.text : text) || '图片');
  if (typeof raw !== 'string' || !/^(?:https?:\/\/|\/)/i.test(raw) || /[\s\\<>"'\x00-\x1f]/.test(raw)) return label;
  try {
    var url = new URL(raw, location.origin);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password) return label;
    if (isTrustedProductImage(url)) {
      return '<img src="' + escapeHtml(url.pathname + url.search) + '" alt="' + label +
        '" loading="lazy" decoding="async">';
    }
    if (url.origin !== location.origin) {
      return '<a href="' + escapeHtml(url.href) + '" target="_blank" rel="noopener noreferrer" ' +
        'referrerpolicy="no-referrer">查看外部图片：' + label + '</a>';
    }
  } catch (_) { /* malformed URL */ }
  return label;
}

function renderMarkdownSafe(text) {
  // Accept bare SVG responses as well as fenced ```svg blocks.
  var svgBlocks = [];
  var src = String(text || '');
  var svgPrefix = "CITRUS_SAFE_SVG_";
  while (src.includes(svgPrefix)) svgPrefix += "_";
  function svgToken(svg) {
    var token = svgPrefix + svgBlocks.length + '_TOKEN';
    svgBlocks.push({ token: token, html: renderSvgBlock(svg) });
    return token;
  }
  // Let the Markdown parser handle SVG code fences. Standalone raw SVG passes
  // through the same sanitizer, but text/attribute positions never receive markup.
  src = src.split(/(^[ \t]{0,3}(?:```+|~~~+)[^\n]*\n[\s\S]*?^[ \t]{0,3}(?:```+|~~~+)[ \t]*$)/gm)
    .map(function(part, i) {
      return i % 2 ? part : part.replace(/^[ \t]{0,3}(<svg\b[\s\S]*?<\/svg\s*>)[ \t]*$/gmi,
        function(_, svg) { return '\n' + svgToken(svg) + '\n'; });
    }).join('');
  var renderer = new marked.Renderer();
  renderer.code = function(code, lang) {
    // v8.4.8: 兼容 marked>=5 的 token 对象签名（code 可能为 {text:...}），
    // 否则代码块会渲染成 "[object Object]"
    if (code && typeof code === 'object') { lang = code.lang || ''; code = code.text || ''; }
    if (String(lang || '').trim().toLowerCase() === 'svg') return renderSvgBlock(code);
    var cls = lang ? ' class="lang-' + escapeHtml(lang) + '"' : '';
    return '<pre><code' + cls + '>' + escapeHtmlCode(code) + '</code></pre>';
  };
  renderer.codespan = function(code) {
    if (code && typeof code === 'object') { code = code.text || ''; }
    return '<code>' + escapeHtmlCode(code) + '</code>';
  };
  // Raw HTML never bypasses the SVG sanitizer or level-specific renderers.
  renderer.html = function(html) { return escapeHtml(html); };
  renderer.image = renderMarkdownImage;
  var originalLink = renderer.link;
  renderer.link = function(token, title, text) {
    var href = typeof token === 'object' && token ? token.href : token;
    var label = typeof token === 'object' && token ? token.text : text;
    if (!safeProductUrl(href, false) || isPrivateCitationRoute(href)) {
      return escapeHtml(label || '');
    }
    return originalLink.apply(this, arguments);
  };
  renderer.del = function (token) {
    return typeof token === 'object' ? this.parser.parseInline(token.tokens) : token;
  };
  var rendered = marked.parse(src, { renderer: renderer, breaks: true });
  svgBlocks.forEach(function(block) {
    rendered = rendered.split('<p>' + block.token + '</p>').join(block.html);
  });
  return rendered;
}

/* ─── v8.4.3: 运行时配置（上下文预算/权限模式单一来源，删本地硬编码）─── */
var _runtimeConfig = null;
var _pendingPermission = null;
var _permissionQueue = [];
var _decidedPermissionIds = new Set();
var _permWaitSec = 90;   /* v8.4.12: 审批卡片超时提示（config 下发，缺省 90s） */
/* v8.5.0 开源版：未配置 API Key 时显示引导遮罩 */
function maybeShowApiSetup(cfg) {
  if (!currentAccount || currentAccount.role !== 'admin') return;
  var overlay = document.getElementById('apiSetupOverlay');
  if (!overlay) return;
  fillModelConfigFields(cfg);
  var closeBtn = document.getElementById('settingsCloseBtn');
  if (cfg && (cfg.any_model_provider_configured || cfg.has_api_key)) {
    if (closeBtn) closeBtn.style.display = 'inline-block';
  } else {
    overlay.style.display = 'flex';
    if (closeBtn) closeBtn.style.display = 'none';
    var input = document.getElementById('apiKeyInput');
    if (input) input.focus();
  }
}
var _providerModelLists = {};
var _providerCatalog = {
  deepseek: { name: 'DeepSeek', base_url: 'https://api.deepseek.com', default_model: 'deepseek-v4-flash' },
  opencode_go: { name: 'OpenCode Go', base_url: 'https://opencode.ai/inference/go/openai/v1', default_model: 'deepseek-v4.1-flash' },
  zhipu: { name: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', default_model: 'glm-4.5-flash' },
  aliyun_bailian: { name: '阿里云百炼（通义千问）', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', default_model: 'qwen-plus' },
  custom_openai: { name: '其他 OpenAI 兼容服务', base_url: 'https://api.example.com/v1', default_model: '' }
};
var _defaultProviderCatalog = JSON.parse(JSON.stringify(_providerCatalog));
function loadProviderCatalog() {
  var epoch = authEpoch;
  accountFetch('/api/v2/config/providers').then(function(r) { if (epoch !== authEpoch) return; return r.json(); }).then(function(data) { if (epoch !== authEpoch) return;
    var select = document.getElementById('mainProviderSelect');
    if (!select || !Array.isArray(data.providers)) return;
    var selected = select.value;
    _providerCatalog = {};
    select.innerHTML = '';
    data.providers.forEach(function(provider) {
      _providerCatalog[provider.id] = provider;
      var option = document.createElement('option');
      option.value = provider.id;
      option.textContent = provider.name + (provider.configured ? ' · 已配置' : '');
      select.appendChild(option);
    });
    if (_providerCatalog[selected]) select.value = selected;
    else if (_runtimeConfig && _runtimeConfig.model && _providerCatalog[_runtimeConfig.model.provider_id])
      select.value = _runtimeConfig.model.provider_id;
    renderConfiguredProviders();
    onProviderChange(true);
  }).catch(function() { if (epoch !== authEpoch) return;});
}
function hasConfiguredProvider(providerId) {
  if ((_providerCatalog[providerId] || {}).configured) return true;
  var ids = (_runtimeConfig && _runtimeConfig.configured_model_provider_ids) || [];
  return ids.indexOf(providerId) >= 0 || !!(_runtimeConfig && _runtimeConfig.has_api_key &&
    _runtimeConfig.model && _runtimeConfig.model.provider_id === providerId);
}
function renderConfiguredProviders() {
  var box = document.getElementById('configuredProvidersList');
  if (!box) return;
  var configured = Object.keys(_providerCatalog).map(function(id) {
    return _providerCatalog[id];
  }).filter(function(provider) { return provider.configured; });
  box.innerHTML = '';
  box.hidden = configured.length === 0;
  if (!configured.length) return;
  var title = document.createElement('div');
  title.className = 'configured-providers-title';
  title.textContent = '已配置服务商';
  box.appendChild(title);
  configured.forEach(function(provider) {
    var row = document.createElement('div');
    row.className = 'configured-provider-row';
    var name = document.createElement('span');
    name.className = 'configured-provider-name';
    name.textContent = provider.name;
    var status = document.createElement('span');
    status.className = 'configured-provider-state';
    status.textContent = provider.id === ((_runtimeConfig || {}).model || {}).provider_id ? '● 当前使用' : '● 已保存';
    var edit = document.createElement('button');
    edit.type = 'button'; edit.className = 'configured-provider-edit'; edit.textContent = '编辑';
    edit.onclick = function() {
      var select = document.getElementById('mainProviderSelect');
      if (select) { select.value = provider.id; onProviderChange(true); }
    };
    row.appendChild(name); row.appendChild(status); row.appendChild(edit);
    box.appendChild(row);
  });
}
function onProviderChange(preserveConfiguredModel) {
  var id = document.getElementById('mainProviderSelect').value;
  var info = _providerCatalog[id] || _providerCatalog.deepseek;
  var hint = document.getElementById('providerEndpointHint');
  var baseInput = document.getElementById('mainApiBaseUrlInput');
  var models = document.getElementById('mainModelSelect');
  var priorProvider = document.getElementById('mainProviderSelect').dataset.lastProvider;
  if (!preserveConfiguredModel && priorProvider && priorProvider !== id) {
    var apiKeyField = document.getElementById('apiKeyInput');
    if (apiKeyField) apiKeyField.value = '';
    var apiKeyError = document.getElementById('apiKeyError');
    if (apiKeyError) apiKeyError.textContent = '';
  }
  document.getElementById('mainProviderSelect').dataset.lastProvider = id;
  if (hint) hint.textContent = 'API 地址：' + info.base_url;
  var keyInput = document.getElementById('apiKeyInput');
  if (keyInput) keyInput.placeholder = hasConfiguredProvider(id)
    ? '已保存；留空继续使用' : '输入服务商 API Key';
  var current = ((_runtimeConfig || {}).model || {});
  if (baseInput) baseInput.value = preserveConfiguredModel && current.provider_id === id && current.main_base_url
    ? current.main_base_url : info.base_url;
  if (baseInput) baseInput.readOnly = !info.custom_base_url;
  if (models) {
    var showCurrent = preserveConfiguredModel && current.provider_id === id && current.main;
    models.innerHTML = '';
    var option = document.createElement('option');
    if (showCurrent) {
      option.value = current.main;
      var freeModels = (info.free_models || []).map(function(x) { return String(x).toLowerCase(); });
      option.textContent = current.main + (freeModels.indexOf(String(current.main).toLowerCase()) >= 0 ? ' · 免费' : '') + '（当前配置）';
      models.appendChild(option);
      models.value = current.main;
    } else {
      option.value = '';
      option.textContent = hasConfiguredProvider(id) ? '点击“获取模型列表”选择模型' : '先填写 Key 并获取模型列表';
      models.appendChild(option);
    }
    models.disabled = true;
  }
  if (!preserveConfiguredModel) _providerModelLists[id] = null;
}
function fetchProviderModels() {
  var epoch = authEpoch;
  var providerId = document.getElementById('mainProviderSelect').value;
  var keyInput = document.getElementById('apiKeyInput');
  var key = (keyInput && keyInput.value || '').trim();
  var err = document.getElementById('apiKeyError');
  var btn = document.getElementById('providerModelsBtn');
  if (!key && !hasConfiguredProvider(providerId)) {
    if (err) err.textContent = '请填写所选服务商对应的 API Key'; return;
  }
  if (btn) btn.disabled = true;
  if (err) { err.className = 'api-error'; err.textContent = '正在向 ' + ((_providerCatalog[providerId] || {}).name || providerId) + ' 获取模型列表…'; }
  accountFetch('/api/v2/config/provider-models', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider_id: providerId, api_key: key,
      api_base_url: (document.getElementById('mainApiBaseUrlInput').value || '').trim() })
  }).then(function(r) { if (epoch !== authEpoch) return; return r.json().then(function(d) { if (epoch !== authEpoch) return; return { ok: r.ok, d: d }; }); })
    .then(function(res) { if (epoch !== authEpoch) return;
      if (!res.ok) throw new Error((res.d && res.d.detail) || '获取模型列表失败');
      var ids = res.d.models || [];
      _providerModelLists[providerId] = ids;
      var select = document.getElementById('mainModelSelect');
      if (select) {
        select.innerHTML = '';
        ids.forEach(function(id) {
          var opt = document.createElement('option'); opt.value = id;
          var freeModels = ((_providerCatalog[providerId] || {}).free_models || []).map(function(x) { return String(x).toLowerCase(); });
          opt.textContent = id + (freeModels.indexOf(String(id).toLowerCase()) >= 0 ? ' · 免费' : '');
          select.appendChild(opt);
        });
        select.disabled = ids.length === 0;
        var activeModel = ((_runtimeConfig || {}).model || {});
        var preferred = activeModel.provider_id === providerId ? activeModel.main : '';
        if (ids.indexOf(preferred) >= 0) select.value = preferred;
        else if (ids.indexOf((_providerCatalog[providerId] || {}).default_model) >= 0) select.value = _providerCatalog[providerId].default_model;
      }
      if (err) { err.className = 'api-warn'; err.textContent = '已取得 ' + ids.length + ' 个可用模型，请选择后保存。'; }
    }).catch(function(e) { if (epoch !== authEpoch) return;
      if (err) { err.className = 'api-error'; err.textContent = e.message; }
    }).finally(function() { if (epoch !== authEpoch) return; if (btn) btn.disabled = false; });
}
function saveProviderConfig() {
  var epoch = authEpoch;
  var err = document.getElementById('modelConfigError');
  var btn = document.getElementById('modelConfigSave');
  var providerId = document.getElementById('mainProviderSelect').value;
  var key = (document.getElementById('apiKeyInput').value || '').trim();
  var model = document.getElementById('mainModelSelect').value;
  var contextTokens = parseInt(document.getElementById('modelContextWindowInput').value, 10) || 0;
  if (!key && !hasConfiguredProvider(providerId)) {
    if (err) err.textContent = '请填写 Provider API Key'; return;
  }
  if (!_providerModelLists[providerId] || _providerModelLists[providerId].indexOf(model) < 0) {
    if (err) err.textContent = '请先获取模型列表并选择其中一个模型'; return;
  }
  if (btn) btn.disabled = true;
  if (err) { err.className = 'api-error'; err.textContent = '正在验证并保存…'; }
  accountFetch('/api/v2/config/provider', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider_id: providerId, api_key: key, main_model: model,
      api_base_url: (document.getElementById('mainApiBaseUrlInput').value || '').trim(),
      context_window_tokens: contextTokens })
  }).then(function(r) { if (epoch !== authEpoch) return; return r.json().then(function(d) { if (epoch !== authEpoch) return; return { ok: r.ok, d: d }; }); })
    .then(function(res) { if (epoch !== authEpoch) return;
      if (!res.ok) throw new Error((res.d && res.d.detail) || '保存失败');
      if (err) { err.className = 'api-warn'; err.textContent = '配置已保存：' + ((_providerCatalog[providerId] || {}).name) + ' / ' + model; }
      document.getElementById('apiKeyInput').value = '';
      loadRuntimeConfig();
    }).catch(function(e) { if (epoch !== authEpoch) return;
      if (err) { err.className = 'api-error'; err.textContent = e.message; }
    }).finally(function() { if (epoch !== authEpoch) return; if (btn) btn.disabled = false; });
}
function fillModelConfigFields(cfg) {
  var m = (cfg && cfg.model) || {};
  function setv(id, v) { var el = document.getElementById(id); if (el) el.value = v || ''; }
  setv('mainProviderSelect', m.provider_id || 'deepseek');
  var keyInput = document.getElementById('apiKeyInput');
  if (keyInput) keyInput.placeholder = hasConfiguredProvider(m.provider_id || 'deepseek')
    ? '已保存；留空继续使用' : '输入服务商 API Key';
  setv('modelContextWindowInput', m.context_window_tokens);
  onProviderChange(true);
  var web = (cfg && cfg.web_search) || {};
  var webProvider = document.getElementById('webSearchProviderSelect');
  if (webProvider) webProvider.value = web.provider || 'deepseek_search_fetch';
  onWebSearchProviderChange();
}
function onWebSearchProviderChange() {
  var provider = document.getElementById('webSearchProviderSelect');
  var key = document.getElementById('webSearchApiKeyInput');
  var fetchKey = document.getElementById('webFetchApiKeyInput');
  var fetchKeyLabel = document.getElementById('webFetchApiKeyLabel');
  var hint = document.getElementById('webSearchKeyHint');
  var web = ((_runtimeConfig || {}).web_search || {});
  var selected = provider ? provider.value : 'deepseek_search_fetch';
  if (key) key.placeholder = web.configured && web.provider === selected
    ? '已配置；留空继续使用' : '输入对应服务商的 Key';
  if (fetchKey) fetchKey.hidden = selected !== 'zhipu_web_search';
  if (fetchKeyLabel) fetchKeyLabel.hidden = selected !== 'zhipu_web_search';
  if (hint) hint.textContent = selected === 'zhipu_web_search'
    ? (web.deepseek_fetch_configured ? 'Web Fetch 已配置 DeepSeek Key。' : '搜索使用智谱；如需网页全文读取，可另填 DeepSeek Fetch Key。')
    : 'Search 与 Fetch 均使用 DeepSeek Search/Fetch Key。';
}
function saveWebSearchConfig() {
  var epoch = authEpoch;
  var provider = document.getElementById('webSearchProviderSelect').value;
  var input = document.getElementById('webSearchApiKeyInput');
  var key = (input && input.value || '').trim();
  var fetchKey = (document.getElementById('webFetchApiKeyInput').value || '').trim();
  var status = document.getElementById('webSearchConfigError');
  var btn = document.getElementById('webSearchConfigSave');
  var existing = ((_runtimeConfig || {}).web_search || {});
  if (!key && !(existing.configured && existing.provider === provider)) {
    if (status) status.textContent = '请填写所选服务商的搜索 Key';
    return;
  }
  if (btn) btn.disabled = true;
  if (status) { status.className = 'api-warn'; status.textContent = '正在保存…'; }
  accountFetch('/api/v2/config/web-apikey', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider_id: provider, api_key: key, fetch_api_key: fetchKey })
  }).then(function(r) { if (epoch !== authEpoch) return; return r.json().then(function(d) { if (epoch !== authEpoch) return; return { ok: r.ok, d: d }; }); })
    .then(function(res) { if (epoch !== authEpoch) return;
      if (!res.ok) throw new Error((res.d && res.d.detail) || '联网搜索配置保存失败');
      if (input) input.value = '';
      var fetchInput = document.getElementById('webFetchApiKeyInput');
      if (fetchInput) fetchInput.value = '';
      if (status) { status.className = 'api-warn'; status.textContent = '已保存'; }
      return accountFetch('/api/v2/config').then(function(r) { if (epoch !== authEpoch) return; return r.json(); });
    }).then(function(cfg) { if (epoch !== authEpoch) return;
      _runtimeConfig = cfg;
      onWebSearchProviderChange();
    }).catch(function(e) { if (epoch !== authEpoch) return;
      if (status) { status.className = 'api-error'; status.textContent = e.message; }
    }).finally(function() { if (epoch !== authEpoch) return; if (btn) btn.disabled = false; });
}
function openSettingsModal() {
  var overlay = document.getElementById('apiSetupOverlay');
  if (!overlay) return;
  overlay.style.display = 'flex';
  fillModelConfigFields(_runtimeConfig);
  var closeBtn = document.getElementById('settingsCloseBtn');
  if (closeBtn) closeBtn.style.display = (_runtimeConfig &&
    (_runtimeConfig.any_model_provider_configured || _runtimeConfig.has_api_key)) ? 'inline-block' : 'none';
  var keyInput = document.getElementById('apiKeyInput');
  if (!(_runtimeConfig && (_runtimeConfig.any_model_provider_configured || _runtimeConfig.has_api_key)) && keyInput) keyInput.focus();
}
function closeSettingsModal() {
  var overlay = document.getElementById('apiSetupOverlay');
  if (overlay) overlay.style.display = 'none';
}
function loadRuntimeConfig() {
  var epoch = authEpoch;
  loadProviderCatalog();
  return accountFetch('/api/v2/config').then(function(r) { if (epoch !== authEpoch) return; return r.json(); })
    .then(function(cfg) { if (epoch !== authEpoch) return;
      _runtimeConfig = cfg;
      if (cfg.permission_wait_sec) _permWaitSec = cfg.permission_wait_sec;
      applyAppVersion();
      maybeShowApiSetup(cfg);
    })
    .catch(function() { if (epoch !== authEpoch) return; applyAppVersion(); });
}
/* v8.4.5: 版本单源——后端 /api/v2/config.version 下发，静态文案仅为离线回退 */
function _appVersion() {
  return (_runtimeConfig && _runtimeConfig.version) || '1.0';
}
function applyAppVersion() {
  var v = _appVersion();
  var badge = document.getElementById('versionBadge');
  if (badge) badge.textContent = 'v' + v;
  var w = document.getElementById('welcomeVersion');
  if (w) w.textContent = 'CitrusScholar v' + v;
  if (_ctxData) renderContextPanel(_ctxData);
}

/* ─── v8.4.3/8.4.12: 权限审批卡片（授权动作不写入对话历史）───
   v8.4.12: 全 CSS 变量驱动（浅/深色模式一致），会话窗口上方居中弹出 */
function resetPermissionCards() {
  _pendingPermission = null;
  _permissionQueue.length = 0;
  _decidedPermissionIds.clear();
  document.getElementById('permission-card')?.remove();
}
function advancePermissionCard() {
  if (!_pendingPermission && _permissionQueue.length) showPermissionCard(_permissionQueue.shift());
}
function settlePermissionCard(id) {
  if (id) {
    _decidedPermissionIds.add(id);
    if (_decidedPermissionIds.size > 128) _decidedPermissionIds.delete(_decidedPermissionIds.values().next().value);
    _permissionQueue = _permissionQueue.filter(function (item) { return item.approval_id !== id; });
  }
  if (_pendingPermission && _pendingPermission.approval_id === id) {
    document.getElementById('permission-card')?.remove();
    _pendingPermission = null;
  }
  advancePermissionCard();
}
function syncPermissionCards(jobId, approvals) {
  if (!jobId || !Array.isArray(approvals)) return;
  var pending = approvals.filter(function (item) { return item.status === 'pending'; });
  var ids = new Set(pending.map(function (item) { return item.approval_id; }));
  _permissionQueue = _permissionQueue.filter(function (item) { return item.job_id !== jobId || ids.has(item.approval_id); });
  if (_pendingPermission && _pendingPermission.job_id === jobId && !ids.has(_pendingPermission.approval_id)) {
    settlePermissionCard(_pendingPermission.approval_id);
  }
  pending.forEach(function (item) { showPermissionCard(Object.assign({}, item, {job_id: jobId})); });
}
function showPermissionCard(data) {
  var id = data.approval_id;
  if (id && (_decidedPermissionIds.has(id) || _pendingPermission?.approval_id === id)) return;
  if (_pendingPermission && id) {
    if (!_permissionQueue.some(function (item) { return item.approval_id === id; })) _permissionQueue.push(data);
    return;
  }
  _pendingPermission = data;
  var existing = document.getElementById('permission-card');
  if (existing) existing.remove();
  var card = document.createElement('div');
  card.id = 'permission-card'; card.className = 'perm-card';
  card.setAttribute('role', 'region'); card.setAttribute('aria-label', '操作权限确认');
  var tool = data.tool_name || '';
  card.innerHTML = '<div class="perm-title">权限确认</div>' +
    '<div class="perm-tool">此操作需要你的确认：' + escapeHtml(displayToolName(tool)) + '</div>' +
    '<div class="perm-actions"><button class="primary" data-scope="once">允许本次</button>' +
    (data.approval_id ? '' : '<button data-scope="session">本会话允许</button><button data-scope="workspace">始终允许工作区</button>') +
    '<button data-decision="rejected">拒绝</button></div>' +
    '<div class="perm-hint" role="status">你可以明确允许或拒绝；未响应将在 ' + (_permWaitSec || 90) + ' 秒后过期。</div>';
  card.querySelectorAll('button').forEach(function(btn) {
    btn.onclick = function() { decidePermission(tool, btn.getAttribute('data-decision') || 'granted', btn.getAttribute('data-scope') || 'once'); };
  });
  document.body.appendChild(card);
}
function decidePermission(toolName, decision, scope) {
  var epoch = authEpoch, sid = sessionId, pending = _pendingPermission;
  var card = document.getElementById('permission-card');
  var canonical = pending && pending.job_id && pending.approval_id;
  var url = canonical ? '/api/v2/chat/jobs/' + encodeURIComponent(pending.job_id) + '/approvals/' + encodeURIComponent(pending.approval_id)
    : '/api/v2/permission/' + (decision === 'rejected' ? 'reject' : 'grant');
  var body = canonical ? {decision: decision} : {session_id: sid, tool_name: toolName, scope: scope};
  if (card) card.querySelectorAll('button').forEach(function(button) { button.disabled = true; });
  return accountFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
    .then(async function(response) {
      var data = await response.json();
      if (epoch !== authEpoch || sid !== sessionId || _pendingPermission !== pending) return;
      if (!response.ok) throw new Error(data.detail && data.detail.message || '权限决定未保存，请重试。');
      settlePermissionCard(pending?.approval_id);
      // Approval continues the original execution; never replay the chat POST.
    }).catch(function(error) {
      if (epoch !== authEpoch || sid !== sessionId || _pendingPermission !== pending) return;
      if (card) {
        var hint = card.querySelector('.perm-hint'); if (hint) hint.textContent = error.message;
        card.querySelectorAll('button').forEach(function(button) { button.disabled = false; });
      }
    });
}
function grantPermission(toolName, scope) { return decidePermission(toolName, 'granted', scope); }
function rejectPermission(toolName) { return decidePermission(toolName, 'rejected', 'once'); }

/* Theme changes restyle the mounted page without replacing conversation state. */
function mascotSay(name) {
  try { if (window.CitrusMascot) window.CitrusMascot.setState(name); } catch (_) {}
}

/* ─── AbortController for SSE (cancel inflight on new send) ─── */
var currentAbortController = null;
var activeTurnCancel = null;
var activeTurnComposer = null;
var composerRunning = false;
var historyTaskDisposers = [];
var historyRestoreVersion = 0;
var historyRequest = null, historyPageRequest = null;
var turnQueueBindings = [];
var turnAdmissionChains = new Map();
var TURN_POST_DEADLINE_MS = 15000;
var turnQueueReads = [], turnQueueObserver = null, composerContinuation = null;

function sessionQueueObserver(sid) {
  var observer = turnQueueObserver;
  if (observer && observer.sid === sid) return observer;
  if (observer) observer.stop();
  var epoch = authEpoch;
  observer = CitrusTaskTransport.createSessionObserver({url: turnQueueURL(sid), fetch: accountFetch, sessionId: sid,
    owns: function () { return turnQueueObserver === observer && epoch === authEpoch && sid === sessionId; },
    onAvailability: function (available) { queueSyncUnavailable = !available; refreshTaskComposer(); }});
  observer.sid = sid;
  turnQueueObserver = observer;
  return observer;
}

function watchTurnQueue(sid, requestId, adopt) {
  return sessionQueueObserver(sid).watch(requestId, adopt);
}

function abortHistoryRequests() {
  if (historyRequest) historyRequest.abort();
  if (historyPageRequest) historyPageRequest.abort();
  historyRequest = null; historyPageRequest = null;
}

function enqueueTurnAdmission(scope, work) {
  var previous = turnAdmissionChains.get(scope) || Promise.resolve();
  var run = previous.then(work);
  var lane = run.catch(function () {});
  turnAdmissionChains.set(scope, lane);
  lane.then(function () { if (turnAdmissionChains.get(scope) === lane) turnAdmissionChains.delete(scope); });
  return run;
}

async function submitTurnBeforeDeadline(url, payload, controller) {
  var timer;
  try {
    return await Promise.race([
      (async function () {
        var response = await accountFetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
          signal:controller.signal, body:JSON.stringify(payload)});
        var body = null;
        try { body = await response.json(); } catch (_) {}
        return {response:response, body:body};
      })(),
      new Promise(function (_, reject) { timer = setTimeout(function () {
        controller.abort(); reject(new Error('turn_post_timeout'));
      }, TURN_POST_DEADLINE_MS); })
    ]);
  } finally { clearTimeout(timer); }
}

function turnQueueURL(sid) {
  return '/api/v2/session/' + encodeURIComponent(sid) + '/turn-queue';
}

function abortTurnQueueReads() {
  turnQueueReads.splice(0).forEach(function (controller) {
    try { controller.abort(); } catch (_) {}
  });
}

function readTurnQueue(sid, callback, requestId) {
  if (!sid) { callback(new Error('session_missing')); return null; }
  var controller = new AbortController();
  turnQueueReads.push(controller);
  function finish() {
    var index = turnQueueReads.indexOf(controller);
    if (index >= 0) turnQueueReads.splice(index, 1);
  }
  var url = '/api/v2/session/' + encodeURIComponent(sid) + '/task-by-request/' + encodeURIComponent(requestId);
  accountFetch(url, {signal: controller.signal, cache: 'no-store'})
    .then(function (response) {
      return response.json().then(function (body) {
        return {ok: response.ok, status: response.status, body: body};
      }, function () { return {ok: response.ok, status: response.status, body: null}; });
    })
    .then(function (result) {
      finish();
      callback(controller.signal.aborted ? new Error('queue_read_aborted') : null, result);
    }, function (error) {
      finish();
      callback(error);
    });
  return controller;
}

// Retire observers and view controls only. Navigation never sends cancellation.
function disposeTaskView(preserveQueue) {
  historyRestoreVersion += 1;
  abortHistoryRequests();
  abortTurnQueueReads();
  if (!preserveQueue && turnQueueObserver) { turnQueueObserver.stop(); turnQueueObserver = null; }
  if (activeTurnCancel) activeTurnCancel('switch');
  else if (activeTurnComposer) activeTurnComposer.cancel('switch');
  turnQueueBindings.slice().forEach(function (binding) {
    try { if (binding && binding.dispose) binding.dispose('switch'); } catch (_) {}
  });
  turnQueueBindings.length = 0;
  historyTaskDisposers.splice(0).forEach(function(dispose) { dispose(); });
  activeTurnCancel = null; activeTurnComposer = null;
  currentJobId = ''; window._currentJobId = '';
  composerRunning = false;
  window._activeTurnStatusUpdater = null;
  if (currentAbortController) { currentAbortController.abort(); currentAbortController = null; }
  resetPermissionCards();
  refreshTaskComposer();
}

function turnQueueIsExecuting(state) {
  if (!state || !state.job_id || CitrusTaskState.isTerminal(state)) return false;
  if (state.question && state.question.status === 'pending') return false;
  return ['running', 'detached', 'cancel_requested'].indexOf(state.status) >= 0;
}

function appendPublicationReconciliation(node, sid, cid) {
  var url = turnQueueURL(sid) + '/' + encodeURIComponent(cid) + '/reconcile/publications';
  var panel = node._publicationPanel;
  if (panel && panel._publicationURL === url) { node.appendChild(panel); return; }
  panel = document.createElement('details');
  panel._publicationURL = url;
  node._publicationPanel = panel;
  var summary = document.createElement('summary');
  summary.textContent = '查看逐文件发布状态';
  panel.appendChild(summary);
  var list = document.createElement('ul');
  panel.appendChild(list);
  var status = document.createElement('span');
  status.setAttribute('role', 'status');
  panel.appendChild(status);
  panel.addEventListener('toggle', async function () {
    if (!panel.open || panel._publicationLoaded) return;
    panel._publicationLoaded = true;
    try {
      var response = await accountFetch(url);
      if (!response.ok) throw new Error('publication_status');
      var payload = await response.json();
      if (!payload.files.length) status.textContent = '这轮没有可核验的文件回执。';
      payload.files.forEach(function (file) {
        var li = document.createElement('li');
        var label = document.createElement('span');
        function show(stage) {
          var state = {started: '已开始，缺少持久化证据', committed: '已保存，待确认登记', confirmed: '已确认'}[stage];
          label.textContent = (file.filename || ('文件 ' + file.file_index)) + ' · ' + state + ' · 操作 ' + file.operation_id;
        }
        function link(href) {
          if (!href || !href.startsWith('/api/v2/session/' + encodeURIComponent(sid) + '/artifacts/')) return;
          var download = document.createElement('a');
          download.href = href;
          download.textContent = '下载';
          li.appendChild(download);
        }
        show(file.stage);
        li.appendChild(label);
        link(file.url);
        list.appendChild(li);
        if (!file.can_restore) return;
        var restore = document.createElement('button');
        restore.type = 'button';
        restore.textContent = '补登记此文件';
        restore.onclick = async function () {
          restore.disabled = true;
          try {
            var route = url + '/' + encodeURIComponent(file.operation_id) + '/' + encodeURIComponent(file.file_index);
            var restored = await accountFetch(route, {method: 'POST'});
            if (!restored.ok) throw new Error('publication_unconfirmed');
            var outcome = await restored.json();
            show(outcome.stage);
            link(outcome.url);
            restore.remove();
            status.textContent = '文件已确认，未重新执行计算。';
          } catch (_) {
            status.textContent = '文件仍待确认。请稍后重试。';
            restore.disabled = false;
          }
        };
        li.appendChild(restore);
      });
    } catch (_) {
      panel._publicationLoaded = false;
      status.textContent = '暂时无法读取发布状态。';
    }
  });
  node.appendChild(panel);
}

function queueStateLabel(entry) {
  if (!entry) return '';
  var names = {queued: '待发送', claimed: '准备执行', running: '执行中',
    awaiting_input: '等待你的选择', settling: '正在保存结果', terminal: '已结束',
    reconcile_required: '状态待核对'};
  return names[entry.state] || '等待确认';
}

function markQueuedBubble(userContent, assistantContent, entry) {
  var queued = !!(entry && ['queued', 'claimed'].indexOf(entry.state) >= 0);
  var row = assistantContent && assistantContent.closest('.message');
  if (row) row.classList.toggle('turn-awaiting-dispatch', queued);
  if (!userContent) return;
  var badge = userContent.querySelector('.turn-user-queue-state');
  if (!queued) { if (badge) badge.remove(); return; }
  if (!badge) {
    badge = document.createElement('span'); badge.className = 'turn-user-queue-state';
    badge.setAttribute('role', 'status'); userContent.appendChild(badge);
  }
  badge.textContent = '待发送';
}

// Only one turn can drive the composer: the executing job first, otherwise the
// earliest still-pending admission. Queued turns keep their own observers.
function selectActiveTurn() {
  var chosen = null, pending = null, binding;
  for (var i = 0; i < turnQueueBindings.length; i++) {
    binding = turnQueueBindings[i];
    if (!binding || !binding.owns || !binding.owns()) continue;
    if (turnQueueIsExecuting(binding.store.state)) { chosen = binding; break; }
    if (!pending && !CitrusTaskState.isTerminal(binding.store.state)) pending = binding;
  }
  if (!chosen) chosen = pending;
  activeTurnComposer = chosen;
  activeTurnCancel = chosen && chosen.cancel ? chosen.cancel : null;
  currentJobId = chosen && chosen.store.state.job_id ? chosen.store.state.job_id : '';
  window._currentJobId = currentJobId;
  selectTurnContext(chosen);
  refreshTaskComposer();
  return chosen;
}

function registerTurnBinding(binding) {
  if (!binding || turnQueueBindings.indexOf(binding) >= 0) return binding;
  turnQueueBindings.push(binding);
  selectActiveTurn();
  return binding;
}

function unregisterTurnBinding(binding) {
  var index = turnQueueBindings.indexOf(binding);
  if (index >= 0) turnQueueBindings.splice(index, 1);
  if (activeTurnComposer === binding) activeTurnComposer = null;
  selectActiveTurn();
}

function refreshTaskComposer() {
  var binding = activeTurnComposer && activeTurnComposer.owns() ? activeTurnComposer : null;
  var state = binding && binding.store.state;
  var terminal = state && CitrusTaskState.isTerminal(state);
  var stopping = binding && (binding.stopping || state.status === 'cancel_requested');
  var executing = !!(binding && (turnQueueIsExecuting(state) || (stopping && state.job_id && !terminal)));
  var button = document.getElementById('send-btn');
  var hasText = !!String((userInput && userInput.value) || '').trim();
  var hasAttachments = !!((_selectedAttachments && _selectedAttachments.length) ||
    (window._selectedAccountFileRefs && window._selectedAccountFileRefs.length));
  if (button) {
    var label = executing ? (stopping ? '正在停止当前任务' : '停止当前任务') : '发送消息';
    button.disabled = !!historyRequest || !!(executing && stopping);
    button.title = executing ? label + '。按 Enter 可将新消息加入队列' : label + ' (Enter)';
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-disabled', String(button.disabled || (!executing && !hasText)));
    button.classList.toggle('is-stop-mode', executing);
    button.classList.toggle('is-stopping', !!(executing && stopping));
    var icon = button.querySelector('use');
    if (icon) icon.setAttribute('href', executing ? '/assets/citrus-icons.svg#ui-stop' : '/assets/citrus-icons.svg#ui-send');
  }
  var continuation = document.getElementById('composer-continuation');
  if (continuation) continuation.hidden = !(composerContinuation && composerContinuation.session === sessionId);
  var hint = document.getElementById('composer-task-status');
  if (hint) {
    hint.textContent = queueSyncUnavailable ? '消息状态待同步，可刷新重试。'
      : _composerNotice && (hasText || hasAttachments) ? _composerNotice
      : !hasText && hasAttachments ? '已选择附件，请填写正文后再发送。'
      : stopping ? '停止请求待确认，已完成的结果会保留。' : '';
    hint.hidden = !hint.textContent;
    hint.title = hint.textContent;
  }
}

function setComposerContinuation(requestId) {
  composerContinuation = requestId ? {session: sessionId, requestId: requestId} : null;
  refreshTaskComposer();
}
var clearContinuation = document.getElementById('clear-composer-continuation');
if (clearContinuation) clearContinuation.addEventListener('click', function () { setComposerContinuation(null); });

function activateComposer() {
  var binding = activeTurnComposer;
  if (binding && binding.owns() && turnQueueIsExecuting(binding.store.state)) {
    return stopCurrentTask();
  }
  return sendMsg();
}

/* ─── Plan Mode state (synced from server flags) ─── */
var planModeEnabled = true;

function setSendDisabled(val) {
  // The durable queue keeps the composer available; val is retained so existing
  // call sites continue to mark admission completion without hiding send.
  refreshTaskComposer();
}

// S1: 本轮任务 id（后端首帧 `job` 事件给出）。停止时用它精确取消这一个任务，
// 而不是把会话里所有任务一起停掉。
var currentJobId = '';

// Cancel only the job bound to the current composer.
function stopCurrentTask() {
  var binding = activeTurnComposer && activeTurnComposer.owns() ? activeTurnComposer : null;
  if (!binding || binding.stopping || !turnQueueIsExecuting(binding.store.state)) return;
  var cancelJobId = binding.store.state.job_id;
  if (!cancelJobId) return;
  try { if (window.CitrusMascot) CitrusMascot.setState('idle', { force: true }); } catch (_) {}
  binding.cancel('user');
  accountFetch('/api/v2/chat/jobs/' + encodeURIComponent(cancelJobId) + '/cancel', {method: 'POST'})
    .then(function (response) {
      if (!response.ok) throw new Error('cancel_failed');
      return response.json();
    }).then(function (data) {
      if (data.accepted === false && data.note !== 'already_finished') throw new Error('cancel_unconfirmed');
    }).catch(function () {
      if (activeTurnComposer !== binding || !binding.owns() || CitrusTaskState.isTerminal(binding.store.state)) return;
      binding.stopping = false;
      showBanner('停止请求未获确认，可以再次尝试。');
      refreshTaskComposer();
    });
}

/* ─── Sidebar ─── */
var _sidebarMap = { session: 'session-list' };
var _sidebarSections = ['session'];

function isSessionDrawer() { return window.innerWidth <= 1024; }
function setSidebarVisible(visible) {
  var shell = document.getElementById('mainContainer');
  var button = document.getElementById('sidebarToggleBtn');
  var sidebar = document.getElementById('appSidebar');
  if (!shell || !button || !sidebar) return;
  shell.classList.toggle('sidebar-hidden', !visible);
  sidebar.inert = !visible;
  button.setAttribute('aria-expanded', String(visible));
  button.setAttribute('aria-label', visible ? '隐藏会话侧栏' : '显示会话侧栏');
  button.title = visible ? '隐藏会话侧栏' : '显示会话侧栏';
  if (!isSessionDrawer()) {
    try { localStorage.setItem('citrus_sidebar_visible', visible ? '1' : '0'); } catch (_) {}
  }
}

function toggleSidebar() {
  var shell = document.getElementById('mainContainer');
  if (!shell) return;
  var opening = shell.classList.contains('sidebar-hidden');
  setSidebarVisible(opening);
  if (opening && isSessionDrawer()) document.getElementById('session-section-toggle').focus();
}

(function setupSessionDrawer() {
  var narrow = isSessionDrawer();
  function restore() {
    var saved;
    try { saved = localStorage.getItem('citrus_sidebar_visible'); } catch (_) {}
    setSidebarVisible(!narrow && saved !== '0');
  }
  restore();
  window.addEventListener('resize', function () {
    if (isSessionDrawer() === narrow) return;
    narrow = isSessionDrawer();
    restore();
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || event.defaultPrevented || !isSessionDrawer()) return;
    var rail = document.getElementById('sidebarToggleBtn');
    var sidebar = document.getElementById('appSidebar');
    if (rail.getAttribute('aria-expanded') === 'true' && (event.target === rail || sidebar.contains(event.target))) {
      event.preventDefault(); setSidebarVisible(false); rail.focus();
    }
  });
  document.getElementById('session-list').addEventListener('click', function (event) {
    if (isSessionDrawer() && event.target.closest('.session-title')) {
      setSidebarVisible(false);
      document.getElementById('sidebarToggleBtn').focus();
    }
  });
})();

var SIDEBAR_MIN = 218, SIDEBAR_MAX = 480, SIDEBAR_DEFAULT = 280;
function applySidebarWidth(width, persist) {
  var shell = document.getElementById('mainContainer');
  var value = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(width)));
  if (shell) shell.style.setProperty('--sidebar-w', value + 'px');
  var handle = document.getElementById('sidebarResizer');
  if (handle) handle.setAttribute('aria-valuenow', String(value));
  if (persist) { try { localStorage.setItem('citrus_sidebar_width', String(value)); } catch (err) {} }
  return value;
}
(function restoreSidebarWidth() {
  var raw = null;
  try { raw = localStorage.getItem('citrus_sidebar_width'); } catch (err) {}
  var n = raw ? parseInt(raw, 10) : NaN;
  applySidebarWidth(isFinite(n) ? n : SIDEBAR_DEFAULT, false);
})();
(function bindSidebarResizer() {
  var handle = document.getElementById('sidebarResizer');
  var shell = document.getElementById('mainContainer');
  var sidebar = document.getElementById('appSidebar');
  if (!handle || !shell || !sidebar) return;
  var dragging = false, startX = 0, startW = 0;
  function interfaceScale() { return window.CitrusTheme ? window.CitrusTheme.scale.effective() : 1; }
  handle.addEventListener('pointerdown', function(event) {
    if (event.button !== 0) return;
    dragging = true; startX = event.clientX;
    startW = sidebar.getBoundingClientRect().width / interfaceScale();
    shell.classList.add('sidebar-resizing');
    try { handle.setPointerCapture(event.pointerId); } catch (err) {}
    event.preventDefault();
  });
  handle.addEventListener('pointermove', function(event) {
    if (!dragging) return;
    applySidebarWidth(startW + (event.clientX - startX) / interfaceScale(), false);
  });
  function finish(event) {
    if (!dragging) return;
    dragging = false;
    shell.classList.remove('sidebar-resizing');
    try { handle.releasePointerCapture(event.pointerId); } catch (err) {}
    applySidebarWidth(sidebar.getBoundingClientRect().width / interfaceScale(), true);
    setSidebarVisible(true);
  }
  handle.addEventListener('pointerup', finish);
  handle.addEventListener('pointercancel', finish);
  handle.addEventListener('dblclick', function() { applySidebarWidth(SIDEBAR_DEFAULT, true); });
  handle.addEventListener('keydown', function(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
    event.preventDefault();
    var current = sidebar.getBoundingClientRect().width / interfaceScale() || SIDEBAR_DEFAULT;
    applySidebarWidth(event.key === 'Home' ? SIDEBAR_DEFAULT : current + (event.key === 'ArrowRight' ? 16 : -16), true);
  });
})();

function applySidebar(active) {
  for (var i = 0; i < _sidebarSections.length; i++) {
    var s = _sidebarSections[i];
    var el = document.getElementById(_sidebarMap[s]);
    var icon = document.getElementById(s + '-toggle');
    var sec = document.getElementById(s + '-section');
    if (!el || !icon || !sec) continue;
    var isActive = (active === s);
    el.classList.toggle('collapsed', !isActive);
    icon.classList.toggle('collapsed', !isActive);
    sec.classList.toggle('expanded', isActive);
    var disclosure = document.getElementById(s + '-section-toggle');
    if (disclosure) disclosure.setAttribute('aria-expanded', String(isActive));
  }
  localStorage.setItem('citrus_sidebar_active', active || '');
}

function toggleSection(section) {
  var el = document.getElementById(_sidebarMap[section]);
  if (!el) return;
  var isCollapsed = el.classList.contains('collapsed');
  applySidebar(isCollapsed ? section : null);
}

/* 会话列表默认展开。 */
applySidebar('session');

/* ─── v8.9 会话管理（列表/切换/新建/重命名/软删除）+ 工作区文件 ─── */
var _sessions = [];
var _sessionsBefore = null, _sessionsRead = null, _sessionsError = '', _sessionPageSize = 20;

function resetSessionList() {
  if (_sessionsRead) _sessionsRead.abort();
  _sessions = []; _sessionsBefore = null; _sessionsRead = null; _sessionsError = '';
}

async function loadSessions(append) {
  var epoch = authEpoch, before = append ? _sessionsBefore : null;
  if (append && (!before || _sessionsRead)) return false;
  if (_sessionsRead) _sessionsRead.abort();
  var read = new AbortController();
  _sessionsRead = read; _sessionsError = '';
  renderSessions();
  function owns() { return _sessionsRead === read && epoch === authEpoch && !accountDocumentRetired; }
  try {
    var response = await accountFetch('/api/v2/sessions?limit=' + _sessionPageSize +
      (before ? '&before=' + encodeURIComponent(before) : ''), {signal: read.signal});
    if (!owns()) return false;
    if (!response.ok) throw new Error('sessions_unavailable');
    var data = await response.json();
    if (!owns()) return false;
    if (!data || !Array.isArray(data.sessions)) throw new Error('invalid_sessions');
    var byId = new Map((append ? _sessions : []).map(function (item) { return [item.session_id, item]; }));
    data.sessions.forEach(function (item) {
      if (item && typeof item.session_id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(item.session_id)) byId.set(item.session_id, item);
    });
    _sessions = Array.from(byId.values());
    _sessionsBefore = typeof data.next_before === 'string' && data.next_before !== before ? data.next_before : null;
    return true;
  } catch (_) {
    if (owns()) _sessionsError = append ? 'append' : 'initial';
    return false;
  } finally {
    if (owns()) { _sessionsRead = null; renderSessions(); }
  }
}

function fmtSessionTime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  var now = new Date();
  var hh = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  if (d.toDateString() === now.toDateString()) return '今天 ' + hh;
  var yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return '昨天 ' + hh;
  var mm = String(d.getMonth() + 1).padStart(2, '0');
  var dd = String(d.getDate()).padStart(2, '0');
  return mm + '-' + dd + ' ' + hh;
}

function renderSessions() {
  var box = document.getElementById('session-list');
  var count = document.getElementById('session-count');
  if (!box) return;
  // v8.9: 过滤空会话（无消息的测试/残留不显示）；当前活动会话总是显示
  var visible = [];
  for (var i = 0; i < _sessions.length; i++) {
    var s = _sessions[i];
    if (s.msg_count > 0 || s.has_tasks || s.session_id === sessionId) visible.push(s);
  }
  if (count) count.textContent = visible.length;
  var buckets = [
    {key:'today',label:'今天',items:[]},
    {key:'yesterday',label:'昨天',items:[]},
    {key:'week',label:'最近 7 天',items:[]},
    {key:'older',label:'更早',items:[]}
  ];
  visible.forEach(function(s) {
    var key = sessionTimeBucket(s.updated_at);
    var bucket = buckets.find(function(b) { return b.key === key; }) || buckets[3];
    bucket.items.push(s);
  });
  function sessionItemHtml(s) {
    var active = s.session_id === sessionId ? ' active' : '';
    var title = escapeHtml(s.title || '新会话');
    return '<div class="session-item' + active + '">' +
      '<button type="button" class="session-title" onclick="switchSession(\'' + s.session_id + '\')" title="' + title + '">' + title + '</button>' +
      '<span class="session-time">' + fmtSessionTime(s.updated_at) + '</span>' +
      '<div class="session-ops">' +
      '<button class="session-op" onclick="sessionMenu(event,\'' + s.session_id + '\')" title="更多">⋯</button>' +
      '</div></div>';
  }
  var savedOpenGroups = ['today'];
  try {
    var parsedOpenGroups = JSON.parse(localStorage.getItem('citrus_session_groups_open') || 'null');
    if (Array.isArray(parsedOpenGroups)) savedOpenGroups = parsedOpenGroups;
  } catch (_) {}
  var html = '';
  if (!visible.length && !_sessionsRead && !_sessionsError) {
    html = '<div class="empty-sidebar">' + (_sessionsBefore ? '此页暂无会话，可继续加载。' : '暂无会话，点击左侧 ＋ 新建') + '</div>';
  }
  buckets.forEach(function(bucket) {
    if (!bucket.items.length) return;
    var containsActive = bucket.items.some(function(s) { return s.session_id === sessionId; });
    var open = containsActive || savedOpenGroups.indexOf(bucket.key) >= 0;
    html += '<details class="session-group" data-session-group="' + bucket.key + '"' + (open ? ' open' : '') + '>' +
      '<summary>' + bucket.label + '<span class="session-group-count">' + bucket.items.length + '</span></summary>' +
      '<div class="session-group-content">' + bucket.items.map(sessionItemHtml).join('') + '</div></details>';
  });
  if (_sessionsRead) html += '<div class="empty-sidebar" role="status">正在读取会话…</div>';
  else if (_sessionsError) html += '<button class="session-more" type="button" onclick="retrySessionsList()">会话读取失败，点击重试</button>';
  else if (_sessionsBefore) html += '<button class="session-more" type="button" onclick="loadMoreSessions()">加载更多会话</button>';
  box.innerHTML = html;
  box.querySelectorAll('.session-group').forEach(function(group) {
    group.addEventListener('toggle', function() {
      var openKeys = Array.from(box.querySelectorAll('.session-group[open]')).map(function(node) {
        return node.getAttribute('data-session-group');
      });
      localStorage.setItem('citrus_session_groups_open', JSON.stringify(openKeys));
    });
  });
}

function sessionTimeBucket(iso) {
  var d = new Date(iso || 0);
  if (isNaN(d.getTime())) return 'older';
  var now = new Date();
  var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  var date = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  var days = Math.floor((today - date) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return 'week';
  return 'older';
}

function loadMoreSessions() { return loadSessions(true); }
function retrySessionsList() { return loadSessions(_sessionsError === 'append'); }

function closeSessionMenu() {
  var m = document.querySelector('.session-menu');
  if (m) m.remove();
}

function sessionMenu(ev, sid) {
  ev.stopPropagation();
  closeSessionMenu();
  var item = ev.currentTarget.closest('.session-item');
  var menu = document.createElement('div');
  menu.className = 'session-menu';
  menu.innerHTML = '<div class="session-menu-item" onclick="sessionMenuAction(\'rename\',\'' + sid + '\')">✎ 重命名</div>' +
                   '<div class="session-menu-item danger" onclick="sessionMenuAction(\'delete\',\'' + sid + '\')">🗑 删除</div>';
  var rect = item ? item.getBoundingClientRect() : { bottom: 80, right: 160 };
  menu.style.top = (rect.bottom + 4) + 'px';
  menu.style.left = Math.max(8, rect.right - 130) + 'px';
  document.body.appendChild(menu);
  setTimeout(function () {
    document.addEventListener('click', function once() {
      closeSessionMenu();
      document.removeEventListener('click', once);
    });
  }, 0);
}

function sessionMenuAction(action, sid) {
  closeSessionMenu();
  if (action === 'rename') renameSession(sid);
  else if (action === 'delete') deleteSession(sid);
}

function switchSession(sid, skipHistory) {
  if (!sid || sid === sessionId) return;
  sessionNavigation++;
  disposeTaskView();
  setSendDisabled(false);
  rememberComposerSession();
  sessionId = sid;
  restoreComposerSession();
  localStorage.setItem(sessionStorageKey(), sid);
  window.dispatchEvent(new CustomEvent('citrus:session-changed', {detail:{session_id:sid}}));
  clearSentAttachmentPreviewUrls();
  chatContainer.innerHTML = '';
  _historyMsgs = [];
  _historyTurns = null;
  _historyHasMore = false;
  roundHistory = [];
  _restoredGroups = null;   // v9.2: 切换会话清空恢复视图，等下一次 restore
  renderContextPanel(null);
  sidebarContent.innerHTML = '<div class="empty-sidebar">提交问题后文献将在此展示</div>';
  citationCount.textContent = '0';
  renderSessions();
  if (skipHistory) renderSessionWelcome();
  else restoreSessionHistory();
  if (window.turnRail && window.turnRail.reload) window.turnRail.reload();
  userInput.focus();
}

function renameSession(sid) {
  var epoch = authEpoch;
  var cur = null;
  for (var i = 0; i < _sessions.length; i++) if (_sessions[i].session_id === sid) cur = _sessions[i];
  var title = prompt('重命名会话：', cur ? (cur.title && cur.title !== '新会话' ? cur.title : '') : '');
  if (title === null || !title.trim()) return;
  accountFetch('/api/v2/session/' + encodeURIComponent(sid) + '/rename', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title.trim() })
  }).then(function (r) { if (epoch !== authEpoch) return; return r.json(); }).then(function () { if (epoch !== authEpoch) return; loadSessions(); })
    .catch(function(error) { if (epoch === authEpoch) alert('重命名失败: ' + error.message); });
}

function captureSessionView() {
  var epoch = authEpoch, sid = sessionId, navigation = sessionNavigation;
  return {id: sid, owns: function () {
    return epoch === authEpoch && sid === sessionId && navigation === sessionNavigation && !accountDocumentRetired;
  }};
}

function notifySessionMutationFailure(response, action) {
  if (response && response.status === 409) {
    alert('会话还有待处理的消息。请等待任务完成后再' + action + '，当前会话未更改。');
  } else {
    alert(action + '失败，当前会话未更改。请稍后重试。');
  }
}

async function deleteSession(sid) {
  if (!sid) return false;
  var view = captureSessionView();
  var isCurrent = sid === view.id;
  if (!confirm('删除会话？\n' + (isCurrent ? '（当前会话）' : '') + '\n仅从列表隐藏，历史数据保留（可追溯）。')) return false;
  try {
    var response = await accountFetch('/api/v2/session/' + encodeURIComponent(sid), {method: 'DELETE'});
    if (!view.owns()) return false;
    if (!response.ok) { notifySessionMutationFailure(response, '删除'); return false; }
    if (isCurrent) await newSession();
    else await loadSessions();
    return true;
  } catch (_) {
    if (view.owns()) notifySessionMutationFailure(null, '删除');
    return false;
  }
}

function loadWorkspaceFiles() {
  var epoch = authEpoch;
  var box = document.getElementById('workspace-list');
  if (!box) return;
  var reqSid = sessionId;   // v8.10l: 竞态防护——快速切换会话时丢弃过期响应
  box.innerHTML = '<div class="empty-sidebar">加载中...</div>';
  accountFetch('/api/v2/session/' + encodeURIComponent(sessionId) + '/workspace-files')
    .then(function (r) { if (epoch !== authEpoch) return; return r.json(); }).then(function (d) { if (epoch !== authEpoch) return;
      if (reqSid !== sessionId) return;
      var files = (d && d.files) || [];
      if (files.length === 0) {
        box.innerHTML = '<div class="empty-sidebar">本会话暂无写作成果</div>';
        return;
      }
      var html = '';
      for (var i = 0; i < files.length; i++) {
        var f = files[i];
        // v8.10i: 路径分段编码（整体 encodeURIComponent 会把 / 编成 %2F 导致 404）
        var enc = f.path.split('/').map(encodeURIComponent).join('/');
        html += '<a class="workspace-file" href="/api/v2/session/' + encodeURIComponent(reqSid) + '/workspace-file?path=' + enc +
          '" target="_blank" title="' + escapeHtml(f.path) + '">📄 ' + escapeHtml(f.name) + '</a>';
      }
      box.innerHTML = html;
    }).catch(function () { if (epoch !== authEpoch) return;
      // v8.10k: 失败可点击重试（不再静默）
      box.innerHTML = '<div class="empty-sidebar" style="cursor:pointer" onclick="loadWorkspaceFiles()">加载失败，点击重试</div>';
    });
}

/* v9.2: 会话历史文献引用恢复——按来源 4 组合并（RAG/UCR/Web/历史），
   与 live 侧栏同构（替代 v8.10k 每轮列表 R{turn}-{i}：编号体系与正文不同源、
   点击不可追踪）。后端 /citations 返回 groups，恢复后按 4 组手风琴渲染；
   新提问触发的实时轮次（roundHistory）到达后优先显示实时轮次结构。 */
var _restoredGroups = null;   // {rag:[], ucr:[], web:[], historical:[]}（刷新恢复视图）
var _restoredRefPool = null;  // v9.4.3: 恢复接口版本标记（自证编号池版本）

function restoreSessionCitations() {
  var epoch = authEpoch;
  var box = document.getElementById('sidebar-content');
  if (!box || !sessionId) return;
  var reqSid = sessionId;   // v8.10l: 竞态防护——快速切换会话时丢弃过期响应
  accountFetch('/api/v2/session/' + encodeURIComponent(sessionId) + '/citations')
    .then(function (r) { if (epoch !== authEpoch) return; return r.json(); }).then(function (d) { if (epoch !== authEpoch) return;
      if (reqSid !== sessionId) return;
      var groups = d && d.groups;
      var gTotal = 0;
      if (groups) {
        for (var _gk in groups) {
          if (Object.prototype.hasOwnProperty.call(groups, _gk)) {
            gTotal += (groups[_gk] || []).length;
          }
        }
      }
      if (!groups || gTotal === 0) {
        if (roundHistory.length === 0) {
          // v8.10q: save_evidence 是后台 spawn 异步落库——切换会话时最新一轮
          // 证据可能尚未落库（空结果）。仅重试 1 次覆盖时序缺口（避免新会话无限轮询）
          _citationsRetry = (_citationsRetry || 0) + 1;
          box.innerHTML = '<div class="empty-sidebar">提交问题后文献将在此展示</div>';
          citationCount.textContent = '0';
          if (_citationsRetry <= 1) {
            setTimeout(function () {
              if (epoch === authEpoch && reqSid === sessionId) restoreSessionCitations();
            }, 800);
          } else {
            _citationsRetry = 0;
          }
        } else {
          _citationsRetry = 0;
        }
        return;
      }
      _citationsRetry = 0;
      _restoredGroups = groups;
      _restoredRefPool = (d && d.ref_pool) || null;
      renderRoundSidebar();
    }).catch(function () { if (epoch !== authEpoch) return; /* 保持现状，不打扰 */ });
}

/* ─── Context Drawer ─── */
var _ctxData = null, _ctxSelection = null;
function selectTurnContext(active) {
  // A queued admission has no model call yet. Keep the last measured call in
  // view until the running turn actually emits its own snapshot.
  var source = active && active.store.state.context_snapshot;
  if (!source) {
    for (var i = turnQueueBindings.length - 1; i >= 0; i--) {
      var candidate = turnQueueBindings[i];
      if (candidate.owns() && candidate.store.state.context_snapshot) {
        source = candidate.store.state.context_snapshot; break;
      }
    }
  }
  if (!source) return;
  var scope = active && active.store.state.context_snapshot === source && !CitrusTaskState.isTerminal(active.store.state) ? 'active' : 'previous';
  if (_ctxSelection && _ctxSelection.source === source && _ctxSelection.scope === scope && _ctxSelection.epoch === authEpoch && _ctxSelection.sid === sessionId) return;
  _ctxSelection = {source: source, scope: scope, epoch: authEpoch, sid: sessionId};
  renderContextPanel(Object.assign({}, source, {view_scope: scope}));
}
var _contextView;
function contextView() {
  if (!_contextView) _contextView = CitrusContextView.create(document);
  return _contextView;
}
function toggleCtxDrawer() { contextView().toggle(); }
function renderContextPanel(data) {
  _ctxData = data || null;
  var limit = _runtimeConfig && _runtimeConfig.context && _runtimeConfig.context.max_tokens;
  if (typeof CitrusContextView !== 'undefined') contextView().update(_ctxData && Object.assign({}, _ctxData, {application_input_limit: limit}));
}

function publicTaskStage(value) {
  var text = String(value == null ? '' : value).trim();
  if (/已加载.*(?:历史|\d+\s*条)|loaded\s+(?:history|\d+)|history\s+loaded|(?:准备|注入|读取|加载|恢复|压缩).{0,12}(?:会话|上下文|历史记录)|正在理解.{0,8}(?:请求|要求|需求)|(?:load|inject|prepare|compress|restore).{0,25}(?:session|context|history)/i.test(text)) return '';
  if (/```|<script\b|<think\b|private[_ ]reasoning|chain.of.thought|(?:^|\n)\s*(?:import \w|from \w+ import|def \w+\(|function\s*\w*\(|const \w+\s*=|Traceback)/i.test(text)) return '';
  return text.replace(/\s+/g, ' ').slice(0, 600);
}

/* F-14: provisional model text is opt-in. The default surface shows only
   lifecycle rows (tool proposals, sub-agent start/end, interrupt, resume) so a
   streamed fragment is never mistaken for the delivered answer. Enable with
   ?native_preview=1 or window.CITRUS_NATIVE_PREVIEW = true (or by setting this
   binding before the first frame is rendered). */
var NATIVE_STREAM_PREVIEW = (function () {
  try {
    if (typeof location !== 'undefined' && location.search &&
        /(?:^\?|&)native_preview=1(?:&|$)/.test(location.search)) return true;
    return typeof window !== 'undefined' && window.CITRUS_NATIVE_PREVIEW === true;
  } catch (_) { return false; }
}());

function createResearchProcessPanel(goal) {
  var epoch = authEpoch, sid = sessionId, knownJobId = '';
  var settled = false, activeMilestone = '', progressSeq = 0;
  var panel = document.createElement('div'); panel.className = 'research-process';
  var body = document.createElement('div'); body.className = 'research-process-body'; panel.appendChild(body);
  var label = document.createElement('span'); label.className = 'research-task-status';
  label.setAttribute('role', 'status'); label.setAttribute('aria-live', 'polite');
  var line = document.createElement('div'); line.className = 'research-task-line';
  var orbit = document.createElement('span'); orbit.className = 'research-task-orbit'; orbit.setAttribute('aria-hidden', 'true');
  for (var i = 0; i < 3; i++) orbit.appendChild(document.createElement('i'));
  var elapsed = document.createElement('span'); elapsed.className = 'research-task-elapsed'; elapsed.setAttribute('aria-live', 'off');
  line.appendChild(orbit); line.appendChild(label); line.appendChild(elapsed);
  panel.insertBefore(line, body);
  var observation = document.createElement('div');
  observation.className = 'research-task-observation'; observation.setAttribute('role', 'status');
  observation.hidden = true; panel.insertBefore(observation, body);
  var readFailures = new Set();
  function readState(source, failed) {
    if (failed) readFailures.add(source); else readFailures.delete(source);
    var messages = [];
    if (readFailures.has('status')) messages.push('暂时无法读取任务状态');
    if (readFailures.has('replay')) messages.push('暂时无法补读任务事件');
    observation.hidden = settled || !messages.length;
    observation.textContent = messages.length ? messages.join('；') + '，正在重试。' : '';
  }
  var timer = null, lastLabel = '', presentation = null;
  function stopClock() { if (timer !== null) { clearInterval(timer); timer = null; } }
  function timeText(seconds) {
    return seconds < 60 ? seconds + ' 秒' : Math.floor(seconds / 60) + ' 分 ' + seconds % 60 + ' 秒';
  }
  function tick() {
    if (!ownsDetails()) { stopClock(); return; }
    if (!document.hidden && presentation && presentation.start > 0) {
      var text = '· 已运行 ' + timeText(Math.max(0, Math.floor(Date.now() / 1000 - presentation.start)));
      if (elapsed.textContent !== text) elapsed.textContent = text;
    }
  }
  function activeLabel() {
    var tool = view.activeTool();
    return tool ? '正在执行 · ' + tool : activeMilestone;
  }
  function refreshActiveLabel() {
    if (!settled && presentation && ['waiting', 'tool'].includes(presentation.kind)) {
      var text = activeLabel();
      if (label.textContent !== text) label.textContent = text;
      lastLabel = text;
    }
  }
  function syncTask(state, entry, ui) {
    var next = CitrusTaskState.waitingView(state, entry, Object.assign({activeTool: view.activeTool()}, ui));
    var wasActive = presentation && (presentation.kind === 'waiting' || presentation.kind === 'tool');
    view.pause(['stopping', 'disconnected', 'awaiting_input', 'settling'].indexOf(next.kind) >= 0 ||
      !!(entry && ['queued', 'claimed', 'reconcile_required'].includes(entry.state)));
    if (next.kind === 'waiting' && view.activeTool()) next.kind = 'tool';
    var same = presentation && presentation.kind === next.kind && presentation.start === next.start && presentation.finish === next.finish;
    presentation = next;
    line.dataset.state = next.kind;
    var labels = {awaiting_input: '等待你的选择',
      settling: '正在保存', stopping: '已请求停止；等待服务端确认',
      disconnected: '连接中断，任务可能仍在后台运行', completed: '已完成',
      partial: '部分完成', failed: '未完成', cancelled: '已停止'};
    var active = next.kind === 'waiting' || next.kind === 'tool';
    var text = active ? activeLabel() : labels[next.kind] || '';
    if ((text || label.textContent === lastLabel || wasActive) && label.textContent !== text) label.textContent = text;
    lastLabel = text;
    if (same) return;
    if (active && next.start > 0) {
      tick(); if (timer === null) timer = setInterval(tick, 1000);
    } else {
      stopClock();
      elapsed.textContent = active ? '· 等待开始时间' : next.start > 0 && next.finish >= next.start
        ? '· 运行用时 ' + timeText(Math.floor(next.finish - next.start)) : '';
    }
  }
  function ownsDetails() { return epoch === authEpoch && sid === sessionId && chatContainer.contains(panel); }
  var view = CitrusTaskCards.createProcessView({container: body, clean: publicTaskStage,
    toolName: displayToolName, statusText: toolStatusText, owns: ownsDetails,
    loadToolResult: async function (callId) {
      if (!ownsDetails()) return null;
      var response = await accountFetch('/api/v2/session/' + encodeURIComponent(sid) + '/tool-result/' + encodeURIComponent(callId), {credentials: 'same-origin'});
      if (!ownsDetails()) return null;
      if (!response.ok) { var error = new Error('工具记录读取失败'); error.status = response.status; throw error; }
      var result = await response.json();
      if (!ownsDetails()) return null;
      if (!result || (result.tool_call_id !== callId && result.provider_call_id !== callId)) throw new Error('工具记录身份不匹配');
      return result;
    },
    elapsed: function (ms) { return (Math.max(0, Number(ms) || 0) / 1000).toFixed(1) + ' 秒'; }});
  // F-14: native LangGraph stream (model/tool/sub-agent/interrupt lifecycle).
  // Frames are already ordered and de-duplicated by the durable event cursor,
  // so this layer only redraws the current projection — it never appends twice
  // and never invents an ending the server did not report. Both globals are
  // feature-detected so a mixed-cache client keeps its previous behaviour.
  var nativeStream = typeof CitrusTaskState.createNativeStream === 'function'
    ? CitrusTaskState.createNativeStream({}) : null;
  var nativeHost = document.createElement('div');
  nativeHost.className = 'task-native-host';
  body.appendChild(nativeHost);
  function renderNativeStreamPanel() {
    if (!nativeStream || typeof CitrusTaskCards.renderNativeStream !== 'function') return;
    if (!ownsDetails()) return;
    CitrusTaskCards.renderNativeStream(nativeHost, CitrusTaskCards.nativeStreamModel(
      nativeStream.view(), {toolName: displayToolName, clean: publicTaskStage,
        preview: NATIVE_STREAM_PREVIEW}));
  }
  function receiveNativeStreamEvent(type, data) {
    if (!nativeStream) return;
    if (['assistant_progress', 'tool_result', 'operation'].indexOf(type) >= 0) {
      if (type === 'assistant_progress' && data.kind === 'model_preview' && !NATIVE_STREAM_PREVIEW) return;
      if (!nativeStream.receive(type, data)) return;
    } else if (type === 'text' && data && data.delivery === 'committed') {
      if (!nativeStream.commit()) return;
    } else if (type === 'text_reset') {
      if (!nativeStream.discard()) return;
    } else if ((type === 'done' || type === 'error') && CitrusTaskState.displayStatus(data)) {
      if (!nativeStream.terminal()) return;
    } else return;
    renderNativeStreamPanel();
  }
  function resetNativeStreamPanel() {
    if (!nativeStream) return;
    nativeStream.reset();
    renderNativeStreamPanel();
  }
  function progress(data) {
    if (!CitrusTaskState.nativeProgress(data) || knownJobId && data.job_id !== knownJobId) return;
    var item = data.status === 'in_progress' ? view.note(data) : null;
    var seq = Number.isSafeInteger(data.seq) ? data.seq : 0;
    if (settled || seq && seq <= progressSeq || !item && data.status !== 'inactive') return;
    if (seq) progressSeq = seq;
    activeMilestone = item ? item.text : '';
    refreshActiveLabel();
  }
  function answerVisible() { /* Public paragraphs remain visible beside the answer. */ }
  function receive(type, data) {
    data = data || {};
    if (data.internal === true || data.private === true || data.is_draft === true || data.draft === true || ['private', 'internal'].indexOf(data.visibility) >= 0) return;
    receiveNativeStreamEvent(type, data);
    var savedResolutions = (data.turn_envelope || data.terminal_snapshot || data).tool_resolutions;
    if (Array.isArray(savedResolutions)) view.resolution({resolutions: savedResolutions});
    if (type === 'job') {
      var nextJobId = String(data.job_id || knownJobId);
      if (nextJobId && nextJobId !== knownJobId) {
        activeMilestone = ''; progressSeq = 0; settled = false;
        view.begin(nextJobId); nativeStream && nativeStream.reset(nextJobId);
        view.terminal(false); renderNativeStreamPanel(); refreshActiveLabel();
      }
      knownJobId = nextJobId;
    }
    else if (type === 'task_plan' || type === 'status' && data.stage === 'task_plan') {
      if (!knownJobId || String(data.plan_id) === knownJobId) view.plan(data);
    }
    else if (type === 'agent_recovery' && data.status === 'admitted') {
      view.begin(String(data.job_id || knownJobId) + '/' + String(data.seq || data.recovery_cursor || 'recovery'));
      settled = false; activeMilestone = ''; progressSeq = Number.isSafeInteger(data.seq) ? data.seq : progressSeq;
      readFailures.clear(); readState('', false);
      resetNativeStreamPanel();
      view.terminal(false); label.textContent = '正在恢复任务'; panel.classList.remove('has-failure');
    }
    else if (type === 'tool_resolution') view.resolution(data);
    else if (type === 'answer_start') {
      activeMilestone = '';
      if (Number.isSafeInteger(data.seq)) progressSeq = Math.max(progressSeq, data.seq);
      refreshActiveLabel();
    }
    else if (type === 'request_user_input' || type === 'question') label.textContent = '等待你的选择';
    else if (type === 'assistant_progress') progress(data);
    else if (type === 'status' && !settled && data.summary && ['preparing_attachments', 'starting', 'waiting_code'].includes(data.stage)) {
      activeMilestone = publicTaskStage(data.summary); refreshActiveLabel();
    }
    else if (type === 'tool_progress') view.progress(data);
    else if (['tool_call_start', 'tool_executing', 'tool_result', 'operation'].indexOf(type) >= 0 &&
        (data.tool_call_id || data.operation_id || data.logical_key || Array.isArray(data.aliases) && data.aliases.length) &&
        data.internal !== true && ['private', 'internal'].indexOf(data.visibility) < 0) {
      var actionStatus = data.status || (type === 'tool_result' ? (data.is_error ? 'failed' : 'complete') : 'running');
      view.action(Object.assign({}, data.outcome || {}, data, {status: actionStatus}));
      refreshActiveLabel();
    }
    // status/plan/thinking/section/context events are never promoted to prose.
  }
  function terminal(status) {
    var names = {completed: '已完成', partial: '部分完成', cancelled: '已停止', failed: '未完成'};
    if (!Object.prototype.hasOwnProperty.call(names, status)) return false;
    settled = true; activeMilestone = ''; readFailures.clear(); readState('', false);
    stopClock(); line.dataset.state = status;
    label.textContent = names[status]; view.terminal();
    panel.classList.toggle('has-failure', status === 'failed');
    return true;
  }
  return {element: panel, label: label, elapsed: elapsed, body: body,
    receive: receive, terminal: terminal, answerVisible: answerVisible, projection: view.projection,
    syncTask: syncTask, stopClock: stopClock, readState: readState};
}





/* ─── Chat ─── */
/* v8.10: 反馈栏默认不随气泡创建（回答生成完成后由 appendFeedback 追加，主流时序）；
   历史恢复的 bot 消息同样不带反馈 */
function followChatTail(force) {
  if (window.turnRail && window.turnRail.follow) window.turnRail.follow(force);
  else chatContainer.scrollTop = chatContainer.scrollHeight;
}
function addMessage(role, html, before, fresh) {
  var div = document.createElement('div');
  div.className = 'message ' + role + (fresh ? ' message-enter' : '');
  div.dataset.messageRole = role;
  div.innerHTML = '<div class="avatar"></div><div class="content">' + html + '</div>';
  if (role === 'user') renderAvatarImage(div.querySelector('.avatar'), currentAccount, 'message-avatar-image', '我的头像');
  else renderAgentAvatar(div.querySelector('.avatar'));
  chatContainer.insertBefore(div, before || null);
  if (fresh && !before) followChatTail(role === 'user');
  return div.querySelector('.content');
}

function uiIcon(name) {
  return '<svg class="ui-icon" aria-hidden="true"><use href="/assets/citrus-icons.svg#ui-' + name + '"></use></svg>';
}

function appendMessageActions(content, role, text) {
  if (!content) return;
  var message = content.closest ? content.closest('.message') : content.parentNode;
  var assistant = role === 'assistant';
  if (assistant && !String(text || '').trim()) return;
  var actionHost = message || content;
  if (actionHost.querySelector('.message-actions')) return;
  var actions = document.createElement('div');
  actions.className = 'message-actions' + (assistant ? ' assistant-actions' : '');
  var copy = document.createElement('button');
  copy.type = 'button'; copy.className = 'message-action'; copy.title = '复制内容';
  copy.setAttribute('aria-label', '复制内容'); copy.innerHTML = uiIcon('copy') + '<span>复制</span>';
  copy.addEventListener('click', function() { copyMessageText(String(text || ''), copy); });
  actions.appendChild(copy);
  if (role === 'user') {
    var edit = document.createElement('button');
    edit.type = 'button'; edit.className = 'message-action';
    var hasAttachments = !!content.querySelector('.sent-attachment-gallery');
    edit.title = hasAttachments ? '修改问题后重新发送；附件需重新添加' : '将问题放回输入框修改后重新发送';
    edit.setAttribute('aria-label', '编辑问题');
    edit.textContent = '✎ 编辑';
    edit.addEventListener('click', function() {
      userInput.value = String(text || '');
      setComposerContinuation(null);
      userInput.focus();
      refreshTaskComposer();
      userInput.setSelectionRange(userInput.value.length, userInput.value.length);
      userInput.scrollIntoView({behavior:'smooth', block:'nearest'});
    });
    actions.appendChild(edit);
  }
  if (assistant && content.dataset.requestId && content.dataset.sessionId === sessionId && (text || '').trim()) {
    var prior = content.dataset.requestId, sid = sessionId;
    var next = document.createElement('button');
    next.type = 'button'; next.className = 'message-action turn-explicit-continuation';
    next.textContent = '沿用本轮附件';
    next.title = '明确继承此轮保存的附件';
    next.addEventListener('click', function () {
      if (sid !== sessionId || !chatContainer.contains(content)) return;
      setComposerContinuation(prior); userInput.focus();
    });
    actions.appendChild(next);
  }
  actionHost.appendChild(actions);
  if (assistant) appendFeedback(actions);
}

function copyMessageText(text, button) {
  function showCopied() {
    if (!button) return;
    var label = button.querySelector('span') || button;
    var old = label.textContent; label.textContent = '已复制';
    setTimeout(function() { label.textContent = old; }, 1200);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(showCopied).catch(function() { copyMessageFallback(text, showCopied); });
  } else copyMessageFallback(text, showCopied);
}

function copyMessageFallback(text, done) {
  var node = document.createElement('textarea');
  node.value = text; node.style.position = 'fixed'; node.style.opacity = '0';
  document.body.appendChild(node); node.select();
  try { document.execCommand('copy'); if (done) done(); } catch (e) {}
  node.remove();
}

function isPublicTurnFile(file) {
  return file && file.internal !== true && file.is_intermediate !== true && file.formal_attachment !== false
    && ['internal', 'private', 'intermediate'].indexOf(String(file.visibility || '').toLowerCase()) < 0
    && ['internal', 'intermediate', 'working'].indexOf(String(file.purpose || file.role || '').toLowerCase()) < 0;
}

function filesAttributedToTurn(files, sid, rid, artifactIds, verifiedFileIds) {
  return (files || []).filter(function(file) {
    if (!isPublicTurnFile(file) || (file.source_session_id && String(file.source_session_id) !== String(sid))) return false;
    if (file.turn_request_id && String(file.turn_request_id) !== String(rid)) return false;
    if (Array.isArray(verifiedFileIds) && verifiedFileIds.length) return verifiedFileIds.includes(String(file.file_id || ''));
    if (Array.isArray(artifactIds)) return artifactIds.includes(String(file.artifact_id || '')) || artifactIds.includes(String(file.file_id || ''));
    return String(file.source_session_id || '') === String(sid) && String(file.turn_request_id || '') === String(rid);
  });
}

function turnFileScopes(content) {
  return new Set([content.dataset.requestId || ''].concat(content._turnFileProducerIds || []));
}

function recordTurnFileProducer(content, producerRequestId) {
  if (!producerRequestId) return;
  var ids = content._turnFileProducerIds || (content._turnFileProducerIds = []);
  if (!ids.includes(producerRequestId)) ids.push(producerRequestId);
}

function continuationProducer(entry, observedJobId) {
  return entry && entry.active_child_job_id === observedJobId ? observedJobId : '';
}

async function fetchTurnGeneratedFiles(sid, requestId, producerJobId, artifactIds, verifiedFileIds) {
  if (!producerJobId) {
    if (!window.loadSessionFiles) return null;
    var library = await window.loadSessionFiles();
    return library ? {requestId: requestId, files: filesAttributedToTurn(library.generated, sid, requestId, artifactIds, verifiedFileIds)} : null;
  }
  var query = new URLSearchParams({request_id: requestId, producer_job_id: producerJobId});
  var response = await accountFetch('/api/v2/session/' + encodeURIComponent(sid) + '/turn-artifacts?' + query);
  if (!response.ok) return null;
  var payload = await response.json();
  if (payload.request_id !== requestId || payload.producer_job_id !== producerJobId ||
      typeof payload.producer_request_id !== 'string' || !payload.producer_request_id || !Array.isArray(payload.files)) return null;
  return {requestId: payload.producer_request_id,
    files: filesAttributedToTurn(payload.files, sid, payload.producer_request_id, artifactIds, verifiedFileIds)};
}

function restoreTurnGeneratedFiles(content, sid, requestId, producerJobId, data, delivery, owns) {
  if (!owns()) return;
  data = data || {};
  var envelope = data.turn_envelope || data.terminal_snapshot || data;
  var artifactIds = Array.isArray(data.artifact_ids) ? data.artifact_ids : envelope.artifact_ids;
  if (Array.isArray(artifactIds)) artifactIds = artifactIds.map(String);
  var verifiedFileIds = delivery && delivery.verified_file_ids;
  var key = JSON.stringify([producerJobId, artifactIds, verifiedFileIds]);
  if (content._turnFileRead === key) return;
  content._turnFileRead = key;
  async function load(attempt) {
    if (!owns() || content._turnFileRead !== key) return;
    var result;
    try { result = await fetchTurnGeneratedFiles(sid, requestId, producerJobId, artifactIds, verifiedFileIds); }
    catch (_) { /* An unavailable read leaves confirmed delivery intact. */ }
    if (!owns() || content._turnFileRead !== key) return;
    if (result) {
      recordTurnFileProducer(content, result.requestId);
      appendTurnGeneratedFiles(content, result.files);
    }
    var expected = verifiedFileIds && verifiedFileIds.length || artifactIds && artifactIds.length || 0;
    if (expected && (!result || result.files.length < expected) && attempt < 2) {
      setTimeout(function () { void load(attempt + 1); }, 500 * (attempt + 1));
    }
  }
  void load(0);
}

function syncHistoryTurnFiles() {
  if (!window.loadSessionFiles || !sessionId) return;
  var view = captureSessionView(), sid = view.id;
  window.loadSessionFiles().then(function(payload) {
    if (!view.owns() || !payload) return;
    chatContainer.querySelectorAll('.content').forEach(function(content) {
      if (!content.closest('.bot') || content.dataset.sessionId !== sid || !content.dataset.requestId) return;
      turnFileScopes(content).forEach(function (producer) {
        appendTurnGeneratedFiles(content, filesAttributedToTurn(payload.generated, sid, producer));
      });
    });
  }).catch(function() {});
}

function appendTurnGeneratedFiles(content, files, headingText) {
  if (!content || !files || !files.length) return;
  var host = content.closest('.message') || content;
  var ownerSession = content.dataset.sessionId || sessionId;
  var producerRequests = turnFileScopes(content);
  // A canonical turn's file references are authoritative. Conflicting metadata
  // is never allowed to move a file into another session or turn.
  files = files.filter(function(file) {
    return isPublicTurnFile(file) && file.file_id && (!file.source_session_id || String(file.source_session_id) === ownerSession)
      && (!file.turn_request_id || producerRequests.has(String(file.turn_request_id)));
  });
  if (!files.length) return;
  // The section is a sibling immediately after the answer bubble, owned by
  // its message wrapper. Library refreshes append idempotently to that section.
  var section = host.querySelector('.turn-files');
  var list = section ? section.querySelector('.turn-file-list') : null;
  if (!section) {
    section = document.createElement('section'); section.className = 'turn-files';
    var heading = document.createElement('div'); heading.className = 'turn-files-heading';
    heading.textContent = headingText || '本轮生成文件'; section.appendChild(heading);
    list = document.createElement('div'); list.className = 'turn-file-list';
    section.appendChild(list);
  } else if (!list) {
    list = document.createElement('div'); list.className = 'turn-file-list';
    section.appendChild(list);
  }
  var added = 0;
  var seen = {};
  host.querySelectorAll('.turn-file-card').forEach(function(existing) {
    seen[String(existing.dataset.fileId || '')] = true;
  });
  files.forEach(function(file) {
    var fid = String((file && file.file_id) || '');
    if (fid && seen[fid]) return;          // same file must not appear twice in one bubble
    if (fid) seen[fid] = true;
    var card = document.createElement('div'); card.className = 'turn-file-card';
    if (fid) card.dataset.fileId = fid;
    var type = document.createElement('span'); type.className = 'turn-file-type';
    type.textContent = String(file.kind || file.filename || 'FILE').toUpperCase().replace(/[^A-Z]/g, '').slice(0, 5) || 'FILE';
    var info = document.createElement('div'); info.className = 'turn-file-info';
    var name = document.createElement('span'); name.className = 'turn-file-name'; name.textContent = file.filename || '生成文件';
    name.title = file.filename || '';
    var meta = document.createElement('span'); meta.className = 'turn-file-meta';
    meta.textContent = file.size_bytes ? (Math.max(1, Math.round(Number(file.size_bytes) / 1024)) + ' KB') : '可预览与下载';
    info.appendChild(name); info.appendChild(meta);
    var preview = document.createElement('button'); preview.type = 'button'; preview.className = 'turn-file-open';
    preview.textContent = '预览'; preview.setAttribute('aria-label', '预览 ' + (file.filename || '生成文件'));
    preview.addEventListener('click', function() {
      if (window.openSessionFilePreview) window.openSessionFilePreview(file.file_id);
    });
    var svgUrl = window.registeredSvgUrl && window.registeredSvgUrl(file);
    if (svgUrl) {
      var image = document.createElement('img');
      image.className = 'turn-file-svg';
      image.alt = file.filename || '已登记图件';
      image.loading = 'lazy';
      image.width = 64;
      image.height = 48;
      image.addEventListener('error', function () { image.hidden = true; type.hidden = false; });
      type.hidden = true;
      image.src = svgUrl;
      card.appendChild(image);
    }
    card.appendChild(type); card.appendChild(info); card.appendChild(preview);
    if (svgUrl) {
      var download = document.createElement('a');
      download.className = 'turn-file-open';
      download.href = svgUrl;
      download.download = '';
      download.textContent = '下载原件';
      card.appendChild(download);
    }
    list.appendChild(card);
    added++;
  });
  if (!added) return;                     // no new file: keep the DOM as it was
  if (section.parentNode) return;
  if (host !== content) host.insertBefore(section, content.nextSibling);
  else content.appendChild(section);
}

/* v8.6 用户反馈（书 §4.6 反馈循环）: 助手回答下方追加 👍/👎 记录条（纯记录，不影响回答）
   v8.10: 回答完整生成后才调用（done 事件），不再随气泡创建即显示 */
function appendFeedback(msgEl) {
  if (!msgEl || msgEl.querySelector('.msg-feedback')) return;
  var fb = document.createElement('div');
  fb.className = 'msg-feedback';
  fb.innerHTML = '<button class="fb-btn" data-rating="1" title="回答有帮助">' + uiIcon('like') + '<span>有用</span></button>' +
                 '<button class="fb-btn" data-rating="-1" title="回答有问题">' + uiIcon('dislike') + '<span>有问题</span></button>' +
                 '<span class="fb-msg"></span>';
  msgEl.appendChild(fb);
}

/* v8.6/v8.7 反馈提交：点击 👍/👎 展开"哪里好/哪里不好"填写行，提交时带 comment；
   内容哈希作 message_id（幂等去重），仅记录不阻断 */
function submitFeedback(fb, mid, rating, comment) {
  var epoch = authEpoch;
  var fbMsg = fb.querySelector('.fb-msg');
  accountFetch('/api/v2/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId || '', message_id: mid, rating: rating, comment: comment || '' })
  }).then(function (r) { if (epoch !== authEpoch) return; return r.json(); }).then(function (d) { if (epoch !== authEpoch) return;
    var row = fb.querySelector('.fb-row');
    if (row) row.remove();
    if (d && d.status === 'ok' && fbMsg) { fbMsg.textContent = '已记录'; }
  }).catch(function () { if (epoch !== authEpoch) return;
    var row = fb.querySelector('.fb-row');
    if (row) row.remove();
    var allBtns = fb.querySelectorAll('.fb-btn');
    for (var i = 0; i < allBtns.length; i++) allBtns[i].disabled = false;
  });
}

document.addEventListener('click', function (e) {
  var btn = e.target.closest ? e.target.closest('.fb-btn') : null;
  if (!btn) return;
  var rating = parseInt(btn.getAttribute('data-rating'), 10);
  var fb = btn.closest('.msg-feedback');
  if (!fb || fb.querySelector('.fb-input')) return;   // 已展开填写行
  var msgEl = btn.closest('.message');
  var contentEl = msgEl ? msgEl.querySelector('.content') : null;
  var text = (contentEl ? contentEl.textContent || '' : '').substring(0, 2000);
  var hash = 0;
  for (var i = 0; i < text.length; i++) { hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0; }
  var mid = 'h' + (hash >>> 0).toString(16);

  var row = document.createElement('div');
  row.className = 'fb-row';
  row.innerHTML = '<input class="fb-input" placeholder="哪里好 / 哪里不好（可留空直接提交）" maxlength="500">' +
                  '<button class="fb-submit">提交</button>' +
                  '<button class="fb-cancel">取消</button>';
  fb.appendChild(row);
  var inp = row.querySelector('.fb-input');
  inp.focus();
  var allBtns = fb.querySelectorAll('.fb-btn');
  for (var i = 0; i < allBtns.length; i++) allBtns[i].disabled = true;
  row.querySelector('.fb-submit').addEventListener('click', function () {
    submitFeedback(fb, mid, rating, inp.value);
  });
  row.querySelector('.fb-cancel').addEventListener('click', function () {
    row.remove();
    for (var i = 0; i < allBtns.length; i++) allBtns[i].disabled = false;
  });
});

function renderSessionWelcome() {
  chatContainer.innerHTML = '<div class="message bot"><div class="avatar"><img class="agent-avatar-image" src="/assets/citrusscholar-mascot.png" alt="CitrusScholar 助手" width="36" height="36"></div><div class="content">您好！有什么想了解的？</div></div>';
}

async function clearSession(silent) {
  var view = captureSessionView();
  if (!view.id || (!silent && !confirm('确定清空当前会话？'))) return false;
  try {
    var response = await accountFetch('/api/v1/session/' + encodeURIComponent(view.id) + '/clear', {method: 'POST'});
    if (!view.owns()) return false;
    if (!response.ok) { notifySessionMutationFailure(response, '清空'); return false; }
    sessionNavigation++;
    disposeTaskView();
    clearSentAttachmentPreviewUrls();
    renderSessionWelcome();
    sidebarContent.innerHTML = '<div class="empty-sidebar">提交问题后文献将在此展示</div>';
    citationCount.textContent = '0';
    _historyMsgs = []; _historyTurns = null; _historyHasMore = false;
    roundHistory = []; _restoredGroups = null; _restoredRefPool = null;
    renderContextPanel(null);
    if (window.turnRail && window.turnRail.reload) window.turnRail.reload();
    void loadSessions();
    return true;
  } catch (_) {
    if (view.owns()) notifySessionMutationFailure(null, '清空');
    return false;
  }
}

async function newSession() {
  var view = captureSessionView();
  try {
    var response = await accountFetch('/api/v1/session/new', {method: 'POST'});
    if (!view.owns()) return false;
    var data = response.ok ? await response.json() : null;
    if (!view.owns()) return false;
    if (!data || typeof data.session_id !== 'string' || !data.session_id) throw new Error('session_create_failed');
    switchSession(data.session_id, true);
    applySidebar('session');
    void loadSessions();
    return true;
  } catch (_) {
    if (view.owns()) notifySessionMutationFailure(null, '创建新会话');
    return false;
  }
}

function toggleRound(id) {
  var body = document.getElementById(id);
  var icon = document.getElementById(id + '-icon');
  body.style.display = body.style.display === 'none' ? 'block' : 'none';
  icon.classList.toggle('collapsed');
}
/* v8.15.2: 证据来源组手风琴（全局版本——内联 onclick 需要全局解析；
   此前定义在 streamOnce 内导致 RAG/UCR 组无法展开收缩） */
function toggleSrcGroup(id) {
  var g = document.getElementById('srcgroup-' + id);
  if (!g) return;
  var willOpen = g.style.display === 'none';
  var box = g.parentNode;
  // 收起同 Round 内的其他来源组（手风琴语义）
  var siblings = box.querySelectorAll('.src-group-body');
  for (var i = 0; i < siblings.length; i++) {
    siblings[i].style.display = 'none';
    var sibIcon = siblings[i].previousElementSibling &&
      siblings[i].previousElementSibling.querySelector('.toggle-icon');
    if (sibIcon) sibIcon.classList.add('collapsed');
  }
  if (willOpen) {
    g.style.display = 'block';
    var myIcon = g.previousElementSibling &&
      g.previousElementSibling.querySelector('.toggle-icon');
    if (myIcon) myIcon.classList.remove('collapsed');
  }
}
/* v8.15.2: 引用编号自然排序（[1]..[13] 升序；[W1] 组在数字之后） */
function _refIdKey(id) {
  var s = String(id == null ? '' : id);
  var m = s.match(/^([WH])?(\d+)$/i);
  if (!m) return 'ZZ' + s;
  return (m[1] ? m[1].toUpperCase() : 'N') + ('0000' + m[2]).slice(-4);
}
function _refIdSort(items) {
  return (items || []).slice().sort(function (a, b) {
    var ka = _refIdKey(a && a.ref_id), kb = _refIdKey(b && b.ref_id);
    return ka < kb ? -1 : (ka > kb ? 1 : 0);
  });
}
  // v10.13（Task E，用户指示）：联网组内按**来源分级**排序 T1 → T2 → T3 → TU。
  //   用户原话："每一个 url 分开，T1T2T3 优先排列"。
  //   同 tier 内回退到 ref_id 升序（保持可追溯、稳定）；无 tier 的排最后。
  // v10.13（改动 3.2）：联网组内按 `Wn` 数字升序（组内已同 tier）。
  function _webRefSort(items) {
    return (items || []).slice().sort(function (a, b) {
      var na = parseInt(String((a && a.ref_id) || '').replace(/\D/g, ''), 10);
      var nb = parseInt(String((b && b.ref_id) || '').replace(/\D/g, ''), 10);
      if (isNaN(na)) na = 1e9;
      if (isNaN(nb)) nb = 1e9;
      return na - nb;
    });
  }
  function _tierSort(items) {
    var ORD = { 'T1': 0, 'T2': 1, 'T3': 2, 'TU': 3 };
    return (items || []).slice().sort(function (a, b) {
      var ta = String((a && a.tier) || '').toUpperCase();
      var tb = String((b && b.tier) || '').toUpperCase();
      var oa = (ORD[ta] != null) ? ORD[ta] : 9;
      var ob = (ORD[tb] != null) ? ORD[tb] : 9;
      if (oa !== ob) return oa - ob;
      var ka = _refIdKey(a && a.ref_id), kb = _refIdKey(b && b.ref_id);
      return ka < kb ? -1 : (ka > kb ? 1 : 0);
    });
  }

// Hoisted to file scope: renderHistoryMessages() also renders tool status text,
// and the previous nested declarations were invisible to it (refresh lost all
// messages after the first tool row with a ReferenceError).
function toolActivityLabel(name) {
  var labels = {
    '网页搜索': '正在搜索网页…', '读取网页': '正在读取网页资料…',
    '获取论文全文': '正在查找论文全文…', '本地检索': '正在检索本地资料…',
    '数据审查': '正在检查数据…', '读取表格': '正在读取表格结构…',
    '文献核验': '正在核对文献信息…', '读取 PDF': '正在读取 PDF…',
    '绘图': '正在生成图示…', '统计分析': '正在分析数据…',
    '实验设计': '正在整理实验方案…', '导出论文解读': '正在整理论文解读…',
    '导出实验方案': '正在整理实验方案…', '写作': '正在整理内容…',
    '分析': '正在分析资料…'
  };
  return labels[name] || ('正在处理：' + (name || '科研任务') + '…');
}
function toolStatusText(status, reason) {
  var reasons = {
    quota: '服务额度暂不可用', auth: '服务认证未通过', timeout: '处理超时',
    network: '网络连接失败', dependency: '所需组件不可用',
    configuration_missing: '服务尚未配置', no_matching_corpus: '没有匹配的本地资料类别',
    translation_failed: '查询语言转换未完成', no_selected_attachment: '未找到可处理的附件',
    no_reference_identifiers: '没有提取到可核验的文献信息',
    no_open_archive: '没有找到可获取的开放材料', blocked_destination: '目标暂不可访问',
    duplicate_request: '重复操作已跳过', tool_batch_limit: '本轮操作数量已达上限',
    execution_record_unavailable: '执行记录不可用',
    status_unavailable: '结果状态暂不可读',
    source_unavailable: '来源暂时无法访问',
    some_records_unverified: '部分条目未能核实', coverage_incomplete: '部分内容未完成检查',
    some_material_unreviewed: '部分材料未能检查',
    fields_unavailable: '读取字段不存在；请使用 JSON Pointer 路径（如 /rows）',
    result_fields_unavailable: '所选字段不可读，可改用原文分页', result_fields_invalid: '请选择更少的字段',
    result_fields_missing: '已返回可用字段，部分字段不存在', result_json_invalid: '回执格式不支持字段读取，可按原文查看',
    result_read_range: '请调整起始位置或缩小单页范围', result_page_too_large: '请缩小原文页或字段范围',
    result_access_denied: '回执读取权限未通过', result_unavailable: '当前会话没有可读取的回执原文',
    result_integrity_failed: '回执原文未通过完整性校验', result_storage_unavailable: '暂时无法读取回执存储',
    result_retention_truncated: '只保留了原文的开头和结尾', budget_blocked: '上下文预算不足，请缩小读取范围',
    no_new_evidence: '未获得新的证据或覆盖', finalization_budget: '工作预算已用尽，保留已有结果',
    context_budget_exhausted: '上下文超出工作输入限额', history_load_failed: '会话历史加载失败',
    model_timeout: '模型响应超时', model_network: '模型连接失败',
    model_rate_limit: '模型请求频率受限', model_quota: '模型服务额度不足',
    model_auth: '模型服务认证失败', model_unavailable: '模型服务暂不可用',
    range_exhausted: '选择范围已处理完，没有新条目', output_missing: '没有生成所需产物',
    service_restart: '服务进程中断，已保留已有记录',
    browse_host_not_allowed: '受限浏览器未获准访问该来源，可使用已获准的来源',
    browse_url_denied: '该网址超出受限浏览器的访问范围',
    browse_dns_denied: '该来源不符合受限浏览器的地址访问范围',
    browse_peer_mismatch: '该来源未通过受限浏览器的地址核验',
    browse_not_authorized: '本轮没有受限浏览器访问权限',
    browse_method_denied: '该操作超出受限浏览器的只读访问范围',
    browse_network_error: '浏览器访问来源时网络失败',
    browse_network_timeout: '浏览器访问来源超时'
  };
  var reasonText = reason ? ' · ' + (reasons[reason] || publicTaskStage(reason)) : '';
  if (status === 'complete' || status === 'completed') return '已完成';
  if (status === 'partial') {
    if (reason === 'source_unavailable') return '部分完成 · 来源暂时无法访问，已用已有资料';
    if (reason === 'timeout') return '部分完成 · 时间到，已保留已有结果';
    return '部分完成' + reasonText;
  }
  if (status === 'no_results') return '已完成，未找到结果';
  if (status === 'unavailable') return '执行记录不可用';
  if (status === 'cancelled') return '已停止';
  if (status === 'interrupted') return '上次执行未记录结束状态';
  if (status === 'skipped') return '已跳过' + reasonText;
  if (status === 'not_run') return '未执行' + reasonText;
  return '未完成' + reasonText;
}

async function sendMsg(paperVerification, continueFrom) {
  if (!currentAccount || !sessionId || accountDocumentRetired || historyRequest) return false;
  var epoch = authEpoch, requestSessionId = sessionId;
  function ownsTurn() { return epoch === authEpoch && requestSessionId === sessionId && (!botContent || chatContainer.contains(botContent)); }
  function ownsControls() {
    return ownsTurn() && !!activeTurnComposer && activeTurnComposer.requestId === clientRequestId;
  }
  var sendBtnEl = document.getElementById('send-btn');
  if (sendBtnEl && sendBtnEl.disabled && !sendBtnEl.classList.contains('is-stop-mode')) return false;
  var rawDraft = String(userInput.value || '');
  var query = paperVerification ? '请核验所附原始 PDF 的参考文献，区分已核实、建议更正和尚未确认，并提供结果及证据。' : rawDraft.trim();
  if (!query) {
    if (_selectedAttachments.length || window._selectedAccountFileRefs.length) {
      _composerNotice = '已选择附件；请填写正文后再发送，附件会保留。';
      refreshTaskComposer();
      try { userInput.focus({preventScroll: true}); } catch (_) { userInput.focus(); }
    }
    return;
  }
  if (!paperVerification) {
    _composerNotice = '';
    if (!continueFrom && composerContinuation && composerContinuation.session === requestSessionId) continueFrom = composerContinuation.requestId;
    setComposerContinuation(null);
  }
  mascotSay('planning');
  var visualizeForRequest = !paperVerification && visualizationEnabled;
  var draftKey = composerDraftKey();
  // The PDF action sends one original File in its own turn, without consuming
  // unrelated composer inputs or replacing the regular selection snapshot.
  var filesForRequest = paperVerification ? [paperVerification.file] : _selectedAttachments.slice();
  var fileRefsForRequest = paperVerification ? [] : window._selectedAccountFileRefs.slice();
  var capturedSkills = paperVerification ? {items: [paperVerification.skill]} :
    skillPicker ? skillPicker.capture() : {items: []};
  var skillsForRequest = (capturedSkills.items || []).map(function (item) { return Object.assign({}, item); });

  // v8.3.7 M1: 每次点击生成稳定幂等 ID，重试复用（防重发重复写历史）
  var clientRequestId = 'crid-' + Date.now().toString(36) + '-' +
    Math.random().toString(36).slice(2, 10);

  if (historyPageRequest) { historyPageRequest.abort(); historyPageRequest = null; }
  currentRoundId++;
  var requestRoundId = currentRoundId;
  currentQuery = query;

  // v8.4.11: 新发送清理停止提示，恢复输入框占位
  var stopMsg = document.getElementById('stop-msg');
  if (stopMsg) stopMsg.style.display = 'none';
  if (!paperVerification) userInput.placeholder = '有什么想做的？';
  var userMessageContent = addMessage('user', escapeHtml(query) +
    (filesForRequest.length + fileRefsForRequest.length ? '<div class="sent-attachment-count">已附加 ' + (filesForRequest.length + fileRefsForRequest.length) + ' 个文件</div>' : ''), null, true);
  var _sentPreviewStart = _sentAttachmentPreviewUrls.length;
  appendSentAttachmentPreviews(userMessageContent, filesForRequest);
  // Only URLs created for this optimistic row are revoked if admission fails.
  var _optimisticSentPreviewUrls = _sentAttachmentPreviewUrls.slice(_sentPreviewStart);
  appendSentLibraryReferences(userMessageContent, fileRefsForRequest);
  appendMessageActions(userMessageContent, 'user', query);
  // P0: 提交瞬间先给导航一个临时刻度，服务端确认后换成正式 message_id
  if (window.turnRail && window.turnRail.addPending) window.turnRail.addPending(clientRequestId, query);
  if (!paperVerification) {
    saveComposerInput();
    userInput.value = '';
    writeComposerDraft(draftKey, '');
  }
  var sentDraftRevision = composerDraft(draftKey).revision;
  var admission = {id:clientRequestId, text:paperVerification ? '' : rawDraft, state:'staged'};
  setComposerAdmission(draftKey, clientRequestId, admission, false);

  var botContent = addMessage('bot', '', null, true);
  // P0-A: 实时气泡带与历史恢复相同的轮次锚点（同一套归属规则，文件/跳转可定位）
  if (botContent) { botContent.dataset.requestId = clientRequestId; botContent.dataset.sessionId = requestSessionId; }
  var botMessage = botContent && botContent.closest ? botContent.closest('.message') : botContent.parentNode;
  var optimisticUserMessage = userMessageContent && userMessageContent.closest ? userMessageContent.closest('.message') : null;
  var optimisticRemoved = false;
  function removeOptimisticTurn() {
    if (optimisticRemoved) return;
    optimisticRemoved = true;
    _optimisticSentPreviewUrls.forEach(function (url) { try { URL.revokeObjectURL(url); } catch (_) {} });
    _sentAttachmentPreviewUrls = _sentAttachmentPreviewUrls.filter(function (url) {
      return _optimisticSentPreviewUrls.indexOf(url) < 0;
    });
    if (optimisticUserMessage && optimisticUserMessage.parentNode) optimisticUserMessage.remove();
    if (botMessage && botMessage.parentNode) botMessage.remove();
    if (window.turnRail && window.turnRail.dropPending) window.turnRail.dropPending(clientRequestId);
  }
  var isCancelled = false;
  var _turnDisconnected = false;  // 断线 ≠ 取消：后台可能仍在运行
  var _turnDisplayStatus = '';    // 服务端 TurnEnvelope 的唯一终态
  var _turnEnvelope = null;
  var spinnerTimeout = null;
  var requestAbortController = null;
  var requestSubmitted = false, statusRetry = null;

  function followSubmittedRequest() {
    if (!ownsTurn()) return;
    var jobId = taskStore.state.job_id;
    if (jobId) { taskTransport.follow(jobId); return; }
    if (!requestSubmitted) return;
    statusLabel.textContent = '连接中断，正在确认原任务';
    taskTransport.followRequest(requestSessionId, clientRequestId, function (foundJobId) {
      taskStore.hydrate({job_id: foundJobId});
      botContent.dataset.jobId = foundJobId;
      if (statusRetry) { statusRetry.remove(); statusRetry = null; }
      if (ownsControls()) refreshTaskComposer();
    }, function () {
      statusLabel.textContent = '任务状态暂未同步';
      if (ownsControls()) setSendDisabled(false);
      if (statusRetry) return;
      statusRetry = document.createElement('button');
      statusRetry.type = 'button'; statusRetry.className = 'history-retry';
      statusRetry.textContent = '重新查看原任务';
      statusRetry.onclick = function () {
        statusRetry.remove(); statusRetry = null;
        followSubmittedRequest();
      };
      statusDiv.appendChild(statusRetry);
    });
  }

  // Keep the answer and completed rows. Aborting receipt or requesting cancel
  // never supplies a business terminal state; replay confirms that state.
  function stopCurrentTurn(reason) {
    if (reason === 'account' || reason === 'switch') {
      if (activeTurnComposer === myBinding) { activeTurnComposer = null; composerRunning = false; }
      isCancelled = true;
      clearQueuePoll();
      if (unsubscribeBinding) { unsubscribeBinding(); unsubscribeBinding = null; }
      if (taskTransport) taskTransport.stop();
      if (taskControls) taskControls.dispose();
      if (taskStore) taskStore.clear();
      clearTimeout(spinnerTimeout); processPanel.stopClock();
      if (reason === 'account' && requestAbortController) requestAbortController.abort();
      unregisterTurnBinding(myBinding);
      return;
    }
    if (!ownsTurn() || (isCancelled && !existingJobNotice)) return;
    if (!ownsControls()) {
      _turnDisconnected = true;
      if (!hasTerminalEvent) { appendNoticeRow('连接中断，任务状态待确认。'); finishStatus(); }
      followSubmittedRequest();
      return;
    }
    if (reason === 'disconnect') {
      // Transport ended, business state did not. Keep the stop button wired so
      // the user can still cancel, and query the authoritative job state once.
      if (hasTerminalEvent || _turnDisconnected) return;
      if (taskStore.state.question && taskStore.state.question.status === 'pending') {
        _turnDisconnected = true; clearTimeout(spinnerTimeout);
        syncWaiting();
        if (ownsControls()) setSendDisabled(false);
        if (taskStore.state.job_id) taskTransport.follow(taskStore.state.job_id);
        return;
      }
      _turnDisconnected = true;
      if (spinnerTimeout) { clearTimeout(spinnerTimeout); spinnerTimeout = null; }
      var discAbort = requestAbortController;
      requestAbortController = null;
      if (discAbort) {
        try { discAbort.abort(); } catch (e) {}
        if (currentAbortController === discAbort) currentAbortController = null;
      }
      appendNoticeRow('连接中断 · 任务可能仍在后台运行', 'is-running');
      finishStatus();
      setSendDisabled(requestSubmitted);
      followSubmittedRequest();
      return;
    }
    isCancelled = true;
    _turnDisconnected = true;
    if (activeTurnComposer && activeTurnComposer.cancel === stopCurrentTurn) activeTurnComposer.stopping = true;
    clearTimeout(spinnerTimeout); processPanel.stopClock();
    var ac = requestAbortController; requestAbortController = null;
    if (ac) {
      ac.abort(); if (currentAbortController === ac) currentAbortController = null;
    }
    appendNoticeRow('已请求停止；等待服务端确认。', 'is-skipped');
    finishStatus(); setSendDisabled(false);
    var stoppingJobId = taskStore.state.job_id || String(currentJobId || window._currentJobId || '');
    if (stoppingJobId) taskTransport.follow(stoppingJobId);
    if (activeTurnCancel === stopCurrentTurn) activeTurnCancel = null;
    try { userInput.focus(); } catch (_) {}
    refreshTaskComposer();
  }

  // Every turn owns its panel, including plain answers and unsuccessful turns.
  var processPanel = createResearchProcessPanel(query);
  var statusDiv = processPanel.element;
  var statusLabel = processPanel.label;
  var statusElapsed = processPanel.elapsed;
  botContent.appendChild(statusDiv);

  // The dispatch spinner starts when the queue reports this turn is running,
  // so a long wait behind other queued messages is not treated as a hang.
  var answerDiv = document.createElement('div'); answerDiv.className = 'turn-answer';
  answerDiv.style.display = 'none';

  var citationsDiv = document.createElement('div');
  citationsDiv.hidden = true;

  var deliveryDiv = document.createElement('div');
  deliveryDiv.className = 'turn-delivery';
  deliveryDiv.setAttribute('role', 'status');
  deliveryDiv.hidden = true;

  botContent.appendChild(answerDiv);
  botContent.appendChild(deliveryDiv);
  botContent.appendChild(citationsDiv);

  var fullText = '';
  var isAnswerStarted = false;
  var isFinalAnswerArmed = false;
  var renderPending = false;
  var hasTerminalEvent = false;
  var snapshotOnly = false;
  var existingJobNotice = false;
  var taskStore = CitrusTaskState.create({onEvent: handleEvent, request_id: clientRequestId, session_id: requestSessionId});
  var taskTransport = CitrusTaskTransport.create({store: taskStore, fetch: accountFetch, owns: ownsTurn,
    onSnapshot: reconcileTaskSnapshot,
    onTerminalDelivery: function (data) {
      if (ownsTurn() && (!hasTerminalEvent || snapshotOnly)) handleEvent('done', data);
    },
    onReadState: function (source, failed) {
      if (!ownsTurn()) return;
      processPanel.readState(source, failed);
      if (failed && navigator.onLine === false) { _turnDisconnected = true; syncWaiting(); }
    },
    onStreamInterrupted: function () { if (ownsTurn()) { _turnDisconnected = true; syncWaiting(); } }
  });
  var taskControls = CitrusTaskControls.create({container: botContent, store: taskStore, transport: taskTransport, owns: ownsTurn,
    readDraft: readSupplementDraft, onReconcile: reconcileSupplement,
    onChange: function () { if (ownsControls()) refreshTaskComposer(); }});
  var myBinding = {
    requestId: clientRequestId, sessionId: requestSessionId, owns: ownsTurn, query: query,
    store: taskStore, controls: taskControls, cancel: stopCurrentTurn,
    stopping: false, following: false, queueEntry: null,
    dispose: function (reason) { stopCurrentTurn(reason || 'switch'); }
  };
  function syncWaiting() {
    processPanel.syncTask(taskStore.state, queueEntry, {hasAnswer: !!fullText.trim(),
      disconnected: _turnDisconnected, stopping: myBinding.stopping});
  }
  registerTurnBinding(myBinding);
  var unsubscribeBinding = taskStore.subscribe(function () {
    if (!ownsTurn()) return;
    renderArtifactDelivery(botContent, taskStore.state.artifact_delivery);
    myBinding.following = !!queueFollowedJobId;
    selectActiveTurn();
    renderQueueStatus();
    syncWaiting();
  });
  refreshTaskComposer();
  var questionCard = CitrusTaskControls.createQuestion({container: botContent, store: taskStore, transport: taskTransport, owns: ownsTurn,
    onContinue: function (result) {
      if (result.resume_job_id === taskStore.state.job_id) taskTransport.follow(result.resume_job_id);
      else if (!queueEntry) refreshQuestionContinuation(result, ownsTurn, questionCard.element);
    }});

  function reconcileTaskSnapshot(job) {
    if (!ownsTurn()) return;
    _turnDisconnected = false;
    processPanel.receive('snapshot', job);
    renderTurnTimings(botContent, job);
    renderArtifactDelivery(botContent, taskStore.state.artifact_delivery);
    syncWaiting();
    if (taskStore.state.question && taskStore.state.question.status === 'pending' && !CitrusTaskState.displayStatus(job)) {
      statusLabel.textContent = '等待你的选择';
      if (ownsControls()) { clearTimeout(spinnerTimeout); setSendDisabled(false); }
    }
    if (!ownsControls()) {
      if (CitrusTaskState.displayStatus(job)) {
        applyTurnEnvelope({turn_envelope: job.terminal_snapshot}); hasTerminalEvent = true; snapshotOnly = true; finishStatus();
        loadTurnGeneratedFiles(job.terminal_snapshot);
      }
      return;
    }
    syncPermissionCards(taskStore.state.job_id, job.approvals);
    if (!job.terminal_snapshot || !CitrusTaskState.displayStatus(job) || hasTerminalEvent) return;
    applyTurnEnvelope({turn_envelope: job.terminal_snapshot});
    hasTerminalEvent = true; snapshotOnly = true; _turnDisconnected = false;
    clearTimeout(spinnerTimeout); clearStageLine(); finishStatus(); setSendDisabled(false);
    loadTurnGeneratedFiles(job.terminal_snapshot);
    if (activeTurnCancel === stopCurrentTurn) activeTurnCancel = null;
    if (!fullText && !existingJobNotice) {
      answerDiv.style.display = '';
      answerDiv.textContent = '任务已结束；可从会话记录查看已保存的回答。';
    }
  }

  function applyTurnEnvelope(data) {
    var env = data && data.turn_envelope;
    var status = CitrusTaskState.displayStatus(data);
    _turnEnvelope = env || null;
    _turnDisplayStatus = status;
    answerDiv.dataset.displayStatus = status;
    return status;
  }

  function appendNoticeRow(text) {
    // Transport/control facts are a concise notice, never model commentary.
    statusLabel.textContent = publicTaskStage(text);
  }

  function clearStageLine() {
    // Public process entries remain available when a completed panel reopens.
  }

  function clearStatus() {
    processPanel.stopClock();
    // Admission errors have no business envelope. Keep an honest, expandable
    // panel rather than making this turn's process UI disappear.
    statusDiv.classList.remove('is-running'); statusDiv.classList.add('is-done');
    statusLabel.textContent = '未开始执行';
  }

  function finishStatus() {
    if (!ownsTurn()) return;
    processPanel.stopClock();
    // Receipt failure/cancel requests are not a business terminal state.
    if (!hasTerminalEvent) {
      syncWaiting();
      return;
    }
    processPanel.terminal(_turnDisplayStatus);
    syncWaiting();
  }

  function loadTurnGeneratedFiles(data) {
    var jobId = taskStore.state.job_id, recovery = taskStore.state.recovery_cursor;
    restoreTurnGeneratedFiles(botContent, requestSessionId, clientRequestId,
      continuationProducer(queueEntry, jobId), data, taskStore.state.artifact_delivery, function () {
        return ownsTurn() && taskStore.state.job_id === jobId && taskStore.state.recovery_cursor === recovery;
      });
  }

  function renderAnswer() {
    if (!renderPending) {
      renderPending = true;
      requestAnimationFrame(function() {
        if (!ownsTurn()) return;
        var s = fullText.replace(/__NEED_COMPLEX__/g, '');
        var frx = /---\s*\*\*.. \u6df1\u5165\u63a2\u7d22.*?\*\*([\s\S]*?)$/;
        var m = s.match(frx);
        var main = s;
        var qs = [];
        if (m) { main = s.replace(frx, '').trim();
          var qb = m[1]; var ql = qb.split('\n');
          for (var qi = 0; qi < ql.length; qi++) {
            var q = ql[qi].trim().replace(/^\d+\.\s*/, '');
            if (q) qs.push(q);
          }
        }
        answerDiv.innerHTML = renderMarkdownSafe(main);
        if (qs.length > 0) {
          var cc = document.createElement('div'); cc.className = 'follow-up-container';
          var cl = document.createElement('div'); cl.className = 'follow-up-label'; cl.textContent = '深入探索（点击追问）'; cc.appendChild(cl);
          var cd = document.createElement('div'); cd.className = 'follow-up-chips';
          for (var qi = 0; qi < qs.length; qi++) { (function(qt){var c=document.createElement('button');c.className='follow-up-chip';c.textContent=qt;c.onclick=function(){handleSendFollowUp(qt)};cd.appendChild(c)})(qs[qi]); }
          cc.appendChild(cd); answerDiv.appendChild(cc);
        }
        if (ownsControls()) followChatTail(false);
        renderPending = false;
      });
    }
  }

  function handleSendFollowUp(q) {
    userInput.value = q;
    sendMsg(null, clientRequestId);
  }

  function showBanner(msg) {
    if (!ownsControls()) return;
    var b = document.getElementById('degraded-banner');
    if (!b) {
      b = document.createElement('div');
      b.id = 'degraded-banner';
      b.style.cssText = 'margin:8px auto;max-width:760px;padding:8px 12px;border-radius:10px;' +
        'background:var(--bg-surface);border:1px solid var(--border-hover);color:var(--text-primary);font-size:13px';
      var sc = document.getElementById('chatContainer') || chatContainer;
      sc.parentNode.insertBefore(b, sc);
    }
    b.textContent = msg;
    b.style.display = '';
    clearTimeout(showBanner._t);
    showBanner._t = setTimeout(function () { b.style.display = 'none'; }, 8000);
  }


  function handleEvent(type, data) {
    if (!ownsTurn()) return;
    processPanel.receive(type, data);
    renderArtifactDelivery(botContent, taskStore.state.artifact_delivery);
    if (type === 'agent_recovery' && data.status === 'admitted') {
      isCancelled = false; hasTerminalEvent = false; snapshotOnly = false; fullText = ''; botContent._turnFileRead = null;
      answerDiv.replaceChildren(); answerDiv.style.display = 'none';
      renderCitationBubbles(citationsDiv, [], clientRequestId, requestSessionId);
      var timing = botContent.querySelector('.turn-timings'); if (timing) timing.remove();
      isFinalAnswerArmed = false; isAnswerStarted = false;
    }
    if (type === 'answer_start' || type === 'text' || type === 'text_reset') {
      if ((hasTerminalEvent && !snapshotOnly) || !isPublicAnswerEvent(data)) return;
      if (type === 'answer_start') isFinalAnswerArmed = true;
      if (type === 'text_reset') {
        isFinalAnswerArmed = false; isAnswerStarted = false; fullText = '';
        answerDiv.replaceChildren(); answerDiv.style.display = 'none';
      }
      if (type === 'text' && isFinalAnswerArmed && data.delivery === 'committed' && typeof data.content === 'string') {
        if (!isAnswerStarted && data.content.trim()) {
          isAnswerStarted = true; clearTimeout(spinnerTimeout);
          if (ownsControls()) mascotSay('generating');
          answerDiv.style.display = '';
        }
        fullText += data.content;
        renderAnswer();
      }
      return;
    }
    if (type === 'citations') {
      if (data.display === 'bubble_only') {
        renderCitationBubbles(citationsDiv, data.bubbles, clientRequestId, requestSessionId,
          continuationProducer(queueEntry, taskStore.state.job_id));
      }
      return;
    }
    if ((type === 'done' || type === 'error') && CitrusTaskState.displayStatus(data)) renderTurnTimings(botContent, data);
    // Retired rounds may finish replaying into their own DOM. They cannot
    // release the composer, alter context usage, permissions or the sidebar.
    if (!ownsControls()) {
      if ((type === 'done' || type === 'error') && CitrusTaskState.displayStatus(data)) {
        applyTurnEnvelope(data); hasTerminalEvent = true; clearTimeout(spinnerTimeout); finishStatus();
        if (data.answer) {
          fullText = data.answer; answerDiv.style.display = ''; answerDiv.innerHTML = renderMarkdownSafe(fullText);
          if (String(data.answer).trim()) processPanel.answerVisible();
        }
        if (fullText) appendMessageActions(botContent, 'assistant', fullText);
        loadTurnGeneratedFiles(data);
      }
      return;
    }
    if ((type === 'done' || type === 'error') && !CitrusTaskState.displayStatus(data)) {
      if (data.question || taskStore.state.question && taskStore.state.question.status === 'pending' || data.status === 'awaiting_input' || (data.turn_envelope || {}).display_status === 'awaiting_input') {
        statusLabel.textContent = '等待你的选择'; clearTimeout(spinnerTimeout); setSendDisabled(false);
        if (data.answer) { fullText = data.answer; answerDiv.style.display = ''; answerDiv.innerHTML = renderMarkdownSafe(fullText); }
      } else { appendNoticeRow('任务状态待确认，正在等待执行记录。'); }
      return;
    }
    if ((type === 'done' || type === 'error') && hasTerminalEvent && !snapshotOnly) return;
    switch (type) {
      case 'reasoning': break; // Private model reasoning is never shown to users.
      case 'plan': break;
      case 'thinking': mascotSay('planning'); break;
      case 'tool_call_start':
      case 'tool_executing': mascotSay('retrieving'); break;
      case 'browse_phase':
        if (!hasTerminalEvent && data.phase === 'organizing') mascotSay('generating');
        break;
      case 'tool_progress':
      case 'tool_result':
      case 'agent_switch':
      case 'assistant_progress': break; // Public content is projected once above.
      case 'draft':
        // v8.17.15: 草稿全链删除——后端不再推送 draft 事件，忽略（防御残留）
        break;
      case 'context_snapshot':
      case 'context_status':
      case 'context_budget':
      case 'context_usage':
        if (data.request_id && data.request_id !== clientRequestId) break;
        if (data.session_id && data.session_id !== requestSessionId) break;
        break; // The selected task store owns the context drawer.
      case 'status':
        if (data.stage === 'awaiting_input' || data.status === 'awaiting_input') {
          statusLabel.textContent = '等待你的选择'; clearTimeout(spinnerTimeout); setSendDisabled(false);
        }
        break;
      case 'retrieval_cache':
        break;
      case 'context_degraded':
        showBanner(String(data.reason || data.message || data.summary
          || '上下文压缩未完成；已保留关键证据继续回答。'));
        break;
      case 'permission_request':
        showPermissionCard(data);
        break;
      case 'permission_decision':
        if (data.approval_id) settlePermissionCard(data.approval_id);
        break;
      case 'task_update':
        break; // Version/pending state is reduced by CitrusTaskState.
      case 'artifact_delivery':
        break; // Reduced by CitrusTaskState and rendered above; text only, no links.
      case 'operation':
        break; // Every operation uses the shared public process projection.
      case 'request_user_input':
      case 'question':
        if (taskStore.state.question && taskStore.state.question.status === 'pending') {
          statusLabel.textContent = '等待你的选择';
          clearTimeout(spinnerTimeout); setSendDisabled(false);
        }
        break;

      case 'plan_ready':
      case 'section_start':
      case 'section_done': break;
      case 'job':
        if (data.job_id) botContent.dataset.jobId = String(data.job_id);
        // S1: 记下本轮 job_id——点停止时据此精确取消这一个任务。
        currentJobId = String(data.job_id || '');
        window._currentJobId = currentJobId;
        taskTransport.refresh(currentJobId, false).catch(function() {});
        break;
      case 'memory_saved':
        var _memoryCount = Number(data.count || 0);
        if (_memoryCount > 0) {
          showBanner('已保存 ' + _memoryCount + ' 条账号记忆。');
          var _memoryOverlay = document.getElementById('memorySettingsOverlay');
          if (_memoryOverlay && !_memoryOverlay.hidden) openMemorySettings();
        }
        break;
      case 'memory_save_failed':
        showBanner('账号记忆暂时未能保存，下次打开记忆面板会自动重试。');
        break;
      case 'done':
        snapshotOnly = false;
        _turnDisconnected = false;
        clearStageLine();
        // One authoritative reducer: the server TurnEnvelope owns the terminal
        // state. Local tool rows never promote a partial turn to completed.
        var _terminal = applyTurnEnvelope(data);
        hasTerminalEvent = !!_terminal;
        // The mascot has no "partial" state; only a fully completed turn gets
        // the success animation, everything else stays neutral or fails.
        mascotSay(_terminal === 'completed' ? 'completed'
          : _terminal === 'failed' ? 'failed' : 'idle');
        if (data.warning) showBanner(String(data.warning));
        if (_turnEnvelope && _turnEnvelope.history_saved === false) {
          showBanner('本轮历史未确认写入；刷新后可能无法恢复。');
        }
        if (_turnEnvelope && _turnEnvelope.notices && _turnEnvelope.notices.length) {
          showBanner(_turnEnvelope.notices.join('；'));
        }
        // P0: 本轮已落库——拉一次最新索引，把临时刻度换成真实 ID
        if (window.turnRail && window.turnRail.sync) window.turnRail.sync(clientRequestId, query);
        clearTimeout(spinnerTimeout); isCancelled = hasTerminalEvent; answerDiv.style.display=''; finishStatus();
        window._activeTurnStatusUpdater = null;
        if (activeTurnCancel === stopCurrentTurn) activeTurnCancel = null;
        // v8.3.7 M3: 引用无检索支撑 → 轻提示（不阻断回答）
        if (data.citation_unsupported) {
          showBanner('回答中的部分引用尚未核实，请引用前检查原文。');
        } else if (data.citation_mismatch) {
          showBanner('部分引用与本轮资料未能对应，请核对原文。');
        }
        // v10.0（卡点③）: 修复循环的修正答案不流式上屏，须以 done.answer 覆盖草稿
        var _repaired = !!(data.audit && data.audit.repaired);
        // v10.5（F6，用户口径）: **判官看的是 done.answer，人看的也必须是 done.answer**
        //   ——即"重编号后的最终答案"。后端已在出口保证 done.answer 为终稿编号
        //   （expert_graph.py 的 F6 块），因此这里一律以它为准覆盖草稿。
        //   此前只在 audit.repaired 为真时覆盖，导致两类错位：
        //     ① 强制收尾题（预算/熔断/跑满轮次）用户看到的是"历轮草稿拼接"，
        //        判官评的却是终稿 —— final84 实测 38/84 题落此坑；
        //     ② 被改写但未置 repaired 标志的题同理。
        //   例外：done.answer 为空或为错误串时保留草稿（错误事件另有提示）。
        var _fromDone = (window._fullTextFromDone = window._fullTextFromDone || {});
        var _ansOk = !!data.answer && data.answer.indexOf('An error occurred') !== 0;
        if (_ansOk) {
          fullText = data.answer;
          _fromDone[clientRequestId] = 1;
          if (_repaired) {
            showBanner('回答已完成一致性检查。');
          }
        } else if (!fullText && data.answer) {
          fullText = data.answer;
          _fromDone[clientRequestId] = 1;
        }
        // A cancelled or empty turn has no answer, but it still has a real
        // terminal text (stop marker / honest notice). Show it instead of an
        // empty bubble; never fabricate answer prose.
        if (!fullText && data.stop_notice) {
          fullText = String(data.stop_notice);
          _fromDone[clientRequestId] = 1;
        }
        if (!fullText && data.notice && _terminal !== 'completed') {
          fullText = String(data.notice);
          _fromDone[clientRequestId] = 1;
        }
        // v10.0（卡点③）: 审计结果轻提示（critical / 需求未满足）
        try {
          var _au = data.audit || {};
          var _sa = (_au.stage_a || {}).severity || 'ok';
          var _sb = (_au.stage_b || {}).severity || '';
          if (_sa === 'critical' || _sb === 'critical') {
            showBanner('回答仍有待核实之处，请结合原文判断。');
          } else if (_au.stage_b && _au.stage_b.requirement_met === false) {
            showBanner('回答可能未覆盖问题的全部要求，请检查后续内容。');
          }
        } catch (_ae) {}
        if (fullText) {
          answerDiv.innerHTML=renderMarkdownSafe(fullText.replace(/__NEED_COMPLEX__/g,''));
        }
        if (fullText.indexOf('__NEED_COMPLEX__')>=0) {
          var uc=document.createElement('div');uc.style.cssText='margin-top:12px;text-align:center';
          var ub=document.createElement('button');ub.className='need-complex-btn';ub.textContent='继续深入检索';
          ub.onclick=function(){handleSendFollowUp(query)};
          uc.appendChild(ub);botContent.appendChild(uc);
        }
        if (data.session_id) sessionId=data.session_id;if(sessionId)localStorage.setItem(sessionStorageKey(),sessionId);
        window.dispatchEvent(new Event('citrus:turn-finished'));
        if (CitrusTaskState.isTerminal({display_status: _terminal})) {
          appendMessageActions(botContent, 'assistant', fullText.replace(/__NEED_COMPLEX__/g, ''));
        }
        loadTurnGeneratedFiles(data);
        setSendDisabled(false);userInput.focus();break;
      case 'error':
        snapshotOnly = false;
        hasTerminalEvent = true;
        // A server error frame is a terminal frame too: consume the same
        // envelope reducer instead of hard-coding a local failure label.
        applyTurnEnvelope(data);
        window._activeTurnStatusUpdater = null;
        mascotSay('failed');
        // P0: 这一轮没有落库成功，撤掉临时刻度
        if (window.turnRail && window.turnRail.dropPending) window.turnRail.dropPending(clientRequestId);
        clearTimeout(spinnerTimeout);isCancelled=true;setSendDisabled(false);userInput.focus();
        if (activeTurnCancel === stopCurrentTurn) activeTurnCancel = null;
        if(!fullText){finishStatus();answerDiv.style.display='';answerDiv.textContent=data.code === 'model_credential_unconfigured'
          ? '主模型尚未配置，请联系管理员配置模型后重试。附件仍可在资料库中查看。'
          : '这次没有完成，请稍后重试。';}
        else { finishStatus(); showBanner('回答提前中断，当前内容可能不完整。'); }
        loadTurnGeneratedFiles(data);
        break;
      case 'heartbeat': break; // Execution time is derived from the server start stamp.
      default: break;
    }
  }

  function showExistingJob(data) {
    if (!ownsTurn()) return;
    existingJobNotice = true;
    clearTimeout(spinnerTimeout);
    isCancelled = true; // This POST has finished; the original job has not been cancelled.
    if (activeTurnCancel === stopCurrentTurn) activeTurnCancel = null;
    window._activeTurnStatusUpdater = null;
    currentJobId = String(data.job_id || '');
    window._currentJobId = currentJobId;
    taskStore.hydrate(data);
    renderArtifactDelivery(botContent, taskStore.state.artifact_delivery);
    answerDiv.style.display = '';
    answerDiv.replaceChildren();
    var note = document.createElement('p');
    function describe(job) {
      var names = {running:'处理中', detached:'后台处理中', cancel_requested:'正在停止',
        completed:'已完成', failed:'未完成', cancelled:'已停止', interrupted:'已中断'};
      var snapshot = job.terminal_snapshot || {};
      var status = (job.status === 'interrupted' || job.current_step === 'interrupted') ? names.interrupted
        : snapshot.display_status === 'partial' ? '部分完成' : (names[job.status] || '状态待确认');
      note.textContent = '这条请求已受理，未重复执行。原任务：' + status + '。';
      statusLabel.textContent = '' + status;
      processPanel.terminal(CitrusTaskState.snapshotStatus(job));
    }
    describe(data);
    answerDiv.appendChild(note);
    var check = document.createElement('button');
    check.type = 'button'; check.textContent = '查看任务状态';
    check.onclick = async function () {
      if (!ownsTurn()) return;
      check.disabled = true;
      try {
        var job = await taskTransport.refresh(currentJobId);
        if (ownsTurn() && job) describe(job);
      } catch (_) {
        if (ownsTurn()) note.textContent = '暂时无法读取原任务状态；请求没有重复执行，请稍后再查看。';
      } finally { if (ownsTurn()) check.disabled = false; }
    };
    answerDiv.appendChild(check);
    if (data.session_id && data.current_step !== 'admission_failed') {
      var history = document.createElement('button');
      history.type = 'button'; history.textContent = '查看原会话';
      history.onclick = function () {
        if (!ownsTurn()) return;
        if (sessionId !== data.session_id) switchSession(data.session_id);
        else restoreSessionHistory();
      };
      answerDiv.appendChild(history);
    }
    setSendDisabled(false);
    userInput.focus();
  }

  var queueEntry = null, unwatchQueue = null, queueFollowedJobId = '', queueStatusEl = null;
  var queueUncertainAttempts = 0;

  function queueStatusNode() {
    if (queueStatusEl || !botContent) return queueStatusEl;
    queueStatusEl = document.createElement('div');
    queueStatusEl.className = 'turn-queue-state';
    queueStatusEl.setAttribute('role', 'status');
    queueStatusEl.setAttribute('aria-live', 'polite');
    botContent.appendChild(queueStatusEl);
    return queueStatusEl;
  }

  function renderQueueStatus() {
    if (!ownsTurn() || !queueEntry) return;
    var node = queueStatusNode();
    if (!node) return;
    var entry = queueEntry;
    markQueuedBubble(userMessageContent, botContent, entry);
    if (entry.state !== 'reconcile_required') {
      node.replaceChildren();
      if (entry.state !== 'terminal' || !entry.clear_reason) { node.hidden = true; return; }
      node.hidden = false;
      var settled = document.createElement('span');
      settled.className = 'turn-queue-status';
      var reconciled = entry.clear_reason === 'turn_reconciled_by_user';
      settled.textContent = reconciled ? '轮次已对账' : (entry.clear_reason === 'cancelled' ? '已取消' : '未开始执行');
      node.appendChild(settled);
      if (reconciled) appendPublicationReconciliation(node, requestSessionId, clientRequestId);
      return;
    }
    node.hidden = false;
    node.replaceChildren();
    var text = document.createElement('span');
    text.className = 'turn-queue-status';
    text.textContent = queueStateLabel(entry);
    node.appendChild(text);
    if (entry.state === 'reconcile_required') {
      var settle = document.createElement('button');
      settle.type = 'button'; settle.className = 'turn-queue-cancel turn-queue-reconcile';
      settle.textContent = '确认并继续';
      settle.setAttribute('aria-label', '确认上一轮结果无法核对并继续本会话');
      settle.onclick = function () { reconcileQueuedEntry(); };
      appendPublicationReconciliation(node, requestSessionId, clientRequestId);
      node.appendChild(settle);
    }
  }

  function clearQueuePoll() { if (unwatchQueue) { unwatchQueue(); unwatchQueue = null; } }

  function scheduleQueuePoll() {
    if (unwatchQueue || !ownsTurn() || !queueEntry || queueEntry.state === 'terminal') return;
    unwatchQueue = watchTurnQueue(requestSessionId, clientRequestId, function (entry) { adoptTurnEntry(entry, true); });
  }

  function maybeFollowObserver() {
    var observer = queueEntry && observeQueuedJob(queueEntry);
    if (!observer || queueFollowedJobId === observer) return;
    if (queueFollowedJobId) {
      fullText = ''; isAnswerStarted = false; isFinalAnswerArmed = false;
      answerDiv.replaceChildren(); answerDiv.style.display = 'none';
      renderCitationBubbles(citationsDiv, [], clientRequestId, requestSessionId);
      hasTerminalEvent = false; snapshotOnly = false;
    }
    processPanel.receive('job', {job_id: observer});
    queueFollowedJobId = observer;
    myBinding.following = true;
    clearTimeout(spinnerTimeout);
    spinnerTimeout = setTimeout(function () {
      if (ownsTurn() && !isCancelled && !hasTerminalEvent) {
        showBanner('本轮长时间无响应；正在查询后台任务状态。');
        followSubmittedRequest();
      }
    }, 300000);
    taskTransport.followContinuation(observer);
    selectActiveTurn();
  }

  function onQueueTerminal(entry) {
    clearQueuePoll();
    renderQueueStatus();
    if (!ownsTurn() || hasTerminalEvent) return;
    if (entry.clear_reason === 'cancelled') {
      isCancelled = true; hasTerminalEvent = true; _turnDisplayStatus = 'cancelled';
      clearTimeout(spinnerTimeout); processPanel.stopClock();
      processPanel.terminal('cancelled');
      answerDiv.style.display = ''; answerDiv.textContent = '已取消，这条消息不会执行。';
      statusLabel.textContent = '已取消';
      setSendDisabled(false);
      selectActiveTurn();
    } else if (entry.clear_reason && !queueFollowedJobId) {
      // Rejected before dispatch: no job stream will follow this turn.
      clearTimeout(spinnerTimeout); clearStatus();
      isCancelled = true; hasTerminalEvent = true; _turnDisplayStatus = 'failed';
      processPanel.terminal('failed');
      answerDiv.style.display = ''; answerDiv.textContent = '这条消息未能开始执行，请重新发送。';
      statusLabel.textContent = '未开始执行';
      setSendDisabled(false);
      selectActiveTurn();
    }
  }

  function adoptTurnEntry(entry, silent) {
    if (!ownsTurn() || !entry || entry.client_request_id !== clientRequestId) return false;
    if (!CitrusTaskTransport.currentQueueEntry(queueEntry, entry)) return false;
    queueEntry = entry;
    queueSyncUnavailable = false;
    myBinding.queueEntry = entry;
    if (entry.job_id) botContent.dataset.jobId = String(entry.active_child_job_id || entry.job_id);
    if (!silent) {
      if (!paperVerification && skillPicker) skillPicker.acknowledge(capturedSkills);
      if (!paperVerification && (filesForRequest.length || fileRefsForRequest.length)) {
        window.dispatchEvent(new Event('citrus-turn-finished'));
        _selectedAttachments = _selectedAttachments.filter(function (f) { return filesForRequest.indexOf(f) < 0; });
        window._selectedAccountFileRefs = window._selectedAccountFileRefs.filter(function (f) { return fileRefsForRequest.indexOf(f) < 0; });
        renderAttachmentSelection();
      }
      if (window.turnRail && window.turnRail.addPending && entry.job_id) window.turnRail.addPending(clientRequestId, query);
    }
    refreshTaskComposer();
    renderQueueStatus();
    maybeFollowObserver();
    syncWaiting();
    if (entry.state === 'terminal') onQueueTerminal(entry);
    else scheduleQueuePoll();
    return true;
  }

  function restoreNewDraft() {
    if (paperVerification) return true;
    var current = composerDraftKey() === draftKey;
    if (current && userInput.value) saveComposerInput();
    if (composerDraft(draftKey).revision !== sentDraftRevision) return false;
    writeComposerDraft(draftKey, rawDraft);
    if (current) {
      userInput.value = rawDraft;
      if (continueFrom) setComposerContinuation(continueFrom);
      refreshTaskComposer();
    }
    return true;
  }

  function recordKnownNonAdmission() {
    admission.state = 'rejected';
    setComposerAdmission(draftKey, clientRequestId, restoreNewDraft() ? null : admission);
  }

  function recordUnknownAdmission() {
    admission.state = 'uncertain';
    setComposerAdmission(draftKey, clientRequestId, admission, !ownsTurn());
    if (ownsTurn()) reconcileQueueAdmission();
    else if (composerDraftKey() === draftKey) lookupDetachedAdmission(draftKey, admission);
  }

  function restoreDraft(message) {
    clearTimeout(spinnerTimeout); clearStatus();
    isCancelled = true; hasTerminalEvent = true;
    answerDiv.style.display = ''; answerDiv.textContent = '';
    showBanner(message);
    setSendDisabled(false);
    if (requestRoundId === currentRoundId) userInput.focus();
  }

  // A timeout or server-side error cannot prove the POST did not commit,
  // even when the response has a JSON error body. Reconcile the original ID.
  function admissionRejectionConfirmed(status, code) {
    // This code is emitted before the durable queue write. Generic 503 is uncertain.
    return (status === 503 && code === 'turn_queue_unavailable') ||
      (status >= 400 && status < 500 && status !== 408 && status !== 499);
  }

  function handleAdmissionError(status, body) {
    var detail = body && body.detail && typeof body.detail === 'object' ? body.detail : {};
    var code = detail.code || (body && body.code);
    if (!admissionRejectionConfirmed(status, code)) { recordUnknownAdmission(); return; }
    requestSubmitted = false;
    recordKnownNonAdmission();
    if (!ownsTurn()) return;
    var message = status === 401 ? '请先登录后继续。'
      : status === 413 ? '附件过大或数量超限，请调整后重试。'
      : status === 429 || code === 'turn_queue_full' ? '待处理消息已达上限，请稍后再试。'
      : code === 'turn_payload_conflict' ? '相同请求标识已用于不同内容；请重新发送为新请求。'
      : code === 'skill_selection_changed' ? '所选技能已变更或不可用，请打开“场景”刷新目录后重新选择。'
      : code === 'account_closed' ? '账户已停用，不能接受新请求。'
      : code === 'turn_queue_unavailable' ? '消息队列暂不可用，内容已保留，请稍后重试。'
      : detail.message ? String(detail.message)
      : '这次没有完成，请稍后重试。';
    restoreDraft(message);
    stopCurrentTurn('switch');
    removeOptimisticTurn();
  }

  function showQueueRetry() {
    if (statusRetry) return;
    statusRetry = document.createElement('button');
    statusRetry.type = 'button'; statusRetry.className = 'history-retry';
    statusRetry.textContent = '重新确认消息状态';
    statusRetry.onclick = function () {
      if (statusRetry) { statusRetry.remove(); statusRetry = null; }
      reconcileQueueAdmission();
    };
    statusDiv.appendChild(statusRetry);
  }

  function reconcileQueueAdmission() {
    if (!ownsTurn()) return;
    statusLabel.textContent = '网络中断，正在确认消息是否已进入队列';
    readTurnQueue(requestSessionId, function (error, result) {
      if (!ownsTurn()) return;
      if (!error && result && result.ok && result.body && result.body.client_request_id === clientRequestId) {
        queueUncertainAttempts = 0;
        setComposerAdmission(draftKey, clientRequestId, null);
        adoptTurnEntry(result.body, !requestSubmitted);
        return;
      }
      if (!requestSubmitted) { recordKnownNonAdmission(); restoreDraft('发送未完成，内容已保留，请重试。'); return; }
      if (++queueUncertainAttempts < 8) { setTimeout(reconcileQueueAdmission, 1500); return; }
      statusLabel.textContent = '消息状态暂未同步';
      renderAdmissionRecovery(draftKey);
      showQueueRetry();
    }, clientRequestId);
  }

  function reconcileQueuedEntry() {
    if (!queueEntry || queueEntry.state !== 'reconcile_required') return;
    if (!window.confirm('上一轮的执行结果无法核对。确认后不会重新执行它，只解除本会话的阻塞，以便继续提问。')) return;
    var node = queueStatusEl;
    var button = node && node.querySelector ? node.querySelector('.turn-queue-reconcile') : null;
    if (button) button.disabled = true;
    accountFetch(turnQueueURL(requestSessionId) + '/' + encodeURIComponent(clientRequestId) + '/reconcile', {method: 'POST'})
      .then(function (response) {
        return response.json().then(function (body) {
          return {ok: response.ok, status: response.status, body: body};
        }, function () { return {ok: response.ok, status: response.status, body: null}; });
      })
      .then(function (outcome) {
        if (!ownsTurn()) return;
        if (outcome.ok && outcome.body) adoptTurnEntry(outcome.body, true);
        else {
          if (button) button.disabled = false;
          showBanner((outcome.body && outcome.body.detail && outcome.body.detail.message) || '状态核对未完成，请刷新后重试。');
        }
      })
      .catch(function () {
        if (!ownsTurn()) return;
        if (button) button.disabled = false;
        showBanner('解除状态核对的请求暂未确认，请稍后重试。');
      });
  }

  function streamOnce() {
    if (!ownsTurn()) return;
    fullText = ''; isAnswerStarted = false; isFinalAnswerArmed = false;
    renderPending = false; _turnDisconnected = false; _turnDisplayStatus = ''; _turnEnvelope = null;
    answerDiv.style.display = ''; answerDiv.innerHTML = '';
    deliveryDiv.hidden = true; deliveryDiv.replaceChildren();
    statusLabel.textContent = ''; statusElapsed.textContent = '';
    statusDiv.className = 'research-process'; hasTerminalEvent = false;

    var streamAbortController = new AbortController();
    requestAbortController = streamAbortController;

    function ownsAdmission() { return epoch === authEpoch && !accountDocumentRetired; }
    // An unrelated session never waits for this POST, while messages in one
    // owner/session preserve their original submission order.
    enqueueTurnAdmission(epoch + ':' + draftKey, async function () {
      if (!ownsAdmission() || streamAbortController.signal.aborted) return;
      try {
        var attachments = await Promise.all(filesForRequest.map(async function (file) {
          return {name: file.name, type: file.type, data: await fileAsDataUrl(file)};
        }));
        if (!ownsAdmission() || streamAbortController.signal.aborted) return;
        var payload = {
          session_id: requestSessionId, query: query, client_request_id: clientRequestId,
          visualization_enabled: visualizeForRequest,
          selected_skills: skillsForRequest, attachments: attachments,
          file_refs: fileRefsForRequest.map(function (file) { return file.file_id; })
        };
        if (!paperVerification && typeof continueFrom === 'string' && continueFrom) payload.continue_from = continueFrom;
        requestSubmitted = true;
        admission.state = 'posting'; setComposerAdmission(draftKey, clientRequestId, admission, false);
        var outcome = await submitTurnBeforeDeadline(turnQueueURL(requestSessionId), payload, streamAbortController);
        if (!ownsAdmission()) return;
        if (!outcome.response.ok) { handleAdmissionError(outcome.response.status, outcome.body); return; }
        if (!outcome.body || outcome.body.client_request_id !== clientRequestId) { recordUnknownAdmission(); return; }
        setComposerAdmission(draftKey, clientRequestId, null);
        if (ownsTurn()) adoptTurnEntry(outcome.body);
        else {
          var saved = composerSelections.get(draftKey);
          if (saved && !paperVerification) {
            saved.files = saved.files.filter(function (file) { return filesForRequest.indexOf(file) < 0; });
            saved.refs = saved.refs.filter(function (file) { return fileRefsForRequest.indexOf(file) < 0; });
            if (saved.skills && saved.skills.scope === capturedSkills.scope && saved.skills.revision === capturedSkills.revision) saved.skills = null;
          }
          if (composerDraftKey() === draftKey) {
            _selectedAttachments = _selectedAttachments.filter(function (file) { return filesForRequest.indexOf(file) < 0; });
            window._selectedAccountFileRefs = window._selectedAccountFileRefs.filter(function (file) { return fileRefsForRequest.indexOf(file) < 0; });
            renderAttachmentSelection();
            restoreSessionHistory();
          }
        }
      } catch (_) {
        if (!ownsAdmission()) return;
        if (!requestSubmitted) {
          recordKnownNonAdmission();
          if (ownsTurn()) {
            restoreDraft('发送未完成，内容已保留，请重试。');
            stopCurrentTurn('switch'); removeOptimisticTurn(); setSendDisabled(false);
          }
        } else recordUnknownAdmission();
      } finally {
        if (requestAbortController === streamAbortController) requestAbortController = null;
      }
    });
  }

  streamOnce();
  return true;
}

  // Shared by live citations and session restoration before any new chat.
  // v8.15: 证据来源展示元数据（来源分组手风琴 + 条目徽标；新增来源只加一行）
  // v9.2 (P0#4): 硬编码仅限 4 个内置来源的名称/顺序；未知来源（如 pipeline1
  // --source-type 新标注）动态生成同名分组 —— 见 srcKey()，分组本身代码驱动。
  var SOURCE_META = {
    rag:  { tag: 'RAG',  name: '本地文献库', cls: 'rag' },
    ucr:  { tag: 'UCR',  name: 'UCR品种库',  cls: 'ucr' },
    web:  { tag: 'Web',  name: '联网搜索',   cls: 'web' },
    historical: { tag: '历史', name: '历史证据', cls: 'historical' },
    // v10.13（改动 3.2）：联网按来源分级拆成三组（用户口径：T1 优先排列）
    web_t1: { tag: 'T1', name: '联网 · T1 同行评议 / 官方 / 学术数据库', cls: 'web' },
    web_t2: { tag: 'T2', name: '联网 · T2 权威与行业媒体 / 机构新闻稿', cls: 'web' },
    web_t3: { tag: 'T3', name: '联网 · T3 自媒体 / 论坛 / 来源不明',     cls: 'web' }
  };
  function srcMeta(item) {
    var s = (item && (item.source || item._src)) || '';
    var sm = SOURCE_META[s];
    if (sm) return sm;
    if (!s) return SOURCE_META['rag'];
    var key = _normSourceKey(s);
    return { tag: key, name: key, cls: 'custom' };
  }
  // v9.4 (P0#4 增强): 来源分组键规范化——非内置来源去尾部数字
  // （paper1→paper、Citrus varieties1→Citrus varieties），同名文件夹在前端
  // 经代码提取自动收纳为同类型分组；内置 4 组（rag/ucr/web/historical）原样。
  function _normSourceKey(s) {
    s = String(s || '').trim();
    if (s === 'rag' || s === 'ucr' || s === 'web' || s === 'historical') return s;
    var norm = s.replace(/\d+$/, '').trim();
    return norm || s;
  }
  // v9.2 (P0#4): 来源分组键——内置 4 组原样；未知来源（pipeline1 source_type）
  // 经 _normSourceKey 归组，使新来源（paper1..8 → paper、Citrus varieties1..3 →
  // Citrus varieties）自动生成侧栏手风琴组（不再并入 RAG）。
  // ── v10.13（改动 3.2/3.4）：侧栏按 **4 组固定顺序**归组 ──────────────────
  //   引用
  //   ├─ 本地文献                        （rag / ucr / historical 全归此组）
  //   ├─ 联网 · T1 同行评议 / 官方 / 学术数据库
  //   ├─ 联网 · T2 权威与行业媒体 / 机构新闻稿
  //   └─ 联网 · T3 自媒体 / 论坛 / 来源不明
  //   说明：历史组（`historical`）已按 3.4 归入「本地文献」——`Hn` 已退役，
  //   历史不再是独立引用通道（见 reports/38 §3）。
  function srcKey(item) {
    var s = (item && (item.source || item._src)) || '';
    var t = String((item && item.type) || '').toLowerCase();
    // 联网条目：按来源分级拆组
    if (s === 'web' || t === 'web') {
      var tier = String((item && item.tier) || '').toUpperCase();
      if (tier === 'T1') return 'web_t1';
      if (tier === 'T2') return 'web_t2';
      return 'web_t3';                       // T3 / TU / 未标注 一律归 T3 组
    }
    // 本地文献（rag / ucr / historical / 其它）统一归「本地文献」
    return 'rag';
  }
  // v8.15: 条目渲染——加来源徽标 + UCR 专属字段（无 DOI 时显示登记号/品种名）
  function renderCitationItem(item) {
    var sm = srcMeta(item);
    var isUcr = sm.cls === 'ucr';
    var du = item.doi && item.doi !== 'N/A' ? 'https://doi.org/' + encodeURIComponent(item.doi) : '#';
    // Candidate/uncited entries never claim to be cited.  The role is set by
    // the backend from the actual answer markers, so this is a display-only
    // decision, not a new inference.
    var _isCandidate = item.role === 'candidate' || item.type === 'candidate'
      || item.type === 'uncited';
    var b = (!_isCandidate && item.type === 'main') ? 'citation-badge' : 'citation-badge uncited';
    var sl = _isCandidate ? '候选（正文未引用）'
      : (item.type === 'dynamic' ? 'Agent 发现'
      : (item.type === 'web' ? 'Web'
      : (item.type === 'historical' ? '历史证据'
      : (typeof item.score === 'number' && Number.isFinite(item.score) ? 'CE=' + item.score.toFixed(3) : ''))));
    var h = '<div class="citation-item" onclick="this.classList.toggle(\'expanded\')">';
    // ── v10.15（C.1 + C.2）：侧栏渲染为「**标题 + 🔗**」 ─────────────────────
    //   用户口径（报告 45 问题 2）：侧栏此前显示 `[W2] Web:` —— **标题行根本不存在**
    //   （`renderCitationItem` 整段没有一处读 `item.title`）。
    //   现改为：**不显示 `[Wn]` 编号、不显示 `Web:` 前缀**，只显示
    //   `标题` + 紧随其后的 `🔗`（点击跳转）。分组（本地/T1/T2/T3）保持不变。
    var _title = item.title || item.name || item.host || '未命名';
    var _turl = safeProductUrl(item.url || (item.doi && item.doi !== 'N/A'
      ? 'https://doi.org/' + encodeURIComponent(item.doi) : ''), false);
    h += '<div class="cite-row">';
    h += '<span class="cite-title">' + escapeHtml(_title) + '</span>';
    if (_turl) {
      h += '<a href="' + escapeHtml(_turl) + '" target="_blank" class="cite-link"'
        + ' onclick="event.stopPropagation()" title="' + escapeHtml(_turl) + '">🔗</a>';
    }
    h += '</div>';
    h += '<div class="citation-text citation-evidence">' + escapeHtml(sourceEvidenceText(item)) + '</div>';
    // 折叠区（点击条目展开）：保留原有元数据，不再占据首行
    h += '<div class="citation-header citation-meta">';
    h += '<span class="src-badge ' + sm.cls + '">' + escapeHtml(sm.tag) + '</span>';
    h += '<span class="citation-score">' + escapeHtml(sl) + '</span></div>';
    // ── v10.13（改动 3.3）：联网条目显示为「标题 🔗」（🔗 点击跳转）────────
    //   用户口径：正文里的"标题 + 明文 URL 列表"全部移到侧栏。
    // ── v10.15（C.2）：标题 + 🔗 已上移到首行（`.cite-row`）⇒ 此处只保留
    //   **非联网**条目的标识信息（UCR 登记号 / DOI），避免重复渲染。
    if (isUcr && item.registry_id) {
      h += '<div class="citation-doi" style="color:var(--accent)">登记号: ' + escapeHtml(item.registry_id) + '</div>';
    } else if ((item.type === 'main' || item.type === 'dynamic')
               && item.doi && item.doi !== 'N/A') {
      h += '<a href="' + du + '" target="_blank" class="citation-doi" onclick="event.stopPropagation()">' + escapeHtml(item.doi) + '</a>';
    }
    // ── v10.16 实测修复：**侧栏 T1/T2/T3 与标题重复** ────────────────────────
    //   现象（用户真机截图）：侧栏把来源分级**重复列出** ——
    //     组标题已经写着「联网 · T1 同行评议 / 官方 / 学术数据库」，
    //     条目里**又**渲染一行「来源分级: T1（…）」；
    //     且标题已在首行 `.cite-title` 显示，折叠区**又**渲染「Title: …」。
    //   修法：**折叠态只留「标题 + 🔗」**（用户口径），其余元数据一律收进
    //   `.citation-detail`（点击才展开）。**不删数据**，只是不再常显。
    h += '<div class="citation-detail">';
    if (isUcr && item.variety_name) {
      h += '<div class="citation-text"><strong>品种:</strong> ' + escapeHtml(item.variety_name) + '</div>';
    }
    if (item.section_name) h += '<div class="citation-text"><strong>Section:</strong> ' + escapeHtml(item.section_name) + '</div>';
    // v10.5（§6.4 可溯源）: 联网条目展示来源分级与六层管线的判定痕迹
    //   tier/tier_reason 来自确定性分级（source_tiers）；filter_stage/content_chars/
    //   relevance_score/rerank_score 来自六层管线（只记录不裁剪时也有值）。
    // v10.16: 此处**只在折叠区**展示（组标题已表达 tier，常显会重复列出 T1/T2/T3）。
    if (item.source_tier) {
      h += '<div class="citation-text"><strong>来源分级:</strong> ' + escapeHtml(item.source_tier)
        + (item.tier_reason ? '（' + escapeHtml(item.tier_reason) + '）' : '') + '</div>';
    }
    if (item.filter_stage) {
      h += '<div class="citation-text"><strong>过滤:</strong> ' + escapeHtml(item.filter_stage)
        + (item.content_chars != null ? ' · 正文 ' + escapeHtml(String(item.content_chars)) + ' 字' : '')
        + (item.relevance_score != null ? ' · 相关性 ' + escapeHtml(String(item.relevance_score)) : '')
        + (item.rerank_score != null ? ' · 精排 ' + escapeHtml(String(item.rerank_score)) : '')
        + '</div>';
    }
    // v9.2: 恢复视图条目携带来源轮次元数据（live 条目无 round_seq 不显示）
    if (item.round_seq) h += '<div class="citation-text"><strong>Round:</strong> 第 ' + escapeHtml(String(item.round_seq)) + ' 轮</div>';
    if (item.text_preview) h += '<div class="citation-text">' + escapeHtml(String(item.text_preview).substring(0, 250)) + '</div>';
    // v10.16: `Title:` 不再重复渲染 —— 标题已在首行 `.cite-title` 显示。
    //   UCR 条目的首行也是标题，同样无需重复。
    if (item.authors) h += '<div class="citation-text"><strong>Authors:</strong> ' + escapeHtml(item.authors) + '</div>';
    if (item.year) h += '<div class="citation-text"><strong>Year:</strong> ' + escapeHtml(item.year) + '</div>';
    if (item.chunk_id) h += '<div class="citation-text"><strong>Chunk:</strong> ' + escapeHtml(item.chunk_id) + '</div>';
    h += '</div></div>';
    return h;
  }
  // v8.15: 来源分组手风琴（同一 Round 内同时只展开一组）
  // v8.15.2: toggleSrcGroup 已提升为全局函数（见 toggleRound 下方）——
  // 此前的内部定义导致内联 onclick 全局解析失败，RAG/UCR 来源组无法展开收缩。
  /* v9.2: 刷新恢复视图——按来源 4 组手风琴（与 live 侧栏同构，共用
     src-group 结构与 renderCitationItem；条目携带来源轮次元数据） */
  function renderRestoredGroups() {
    if (!_restoredGroups) return false;
    // v9.4: 恢复视图同样支持动态来源组（与 live 侧栏同口径，未知组排在内置 4 组之后）
    var dyn = [];
    var gkeys = Object.keys(_restoredGroups || {});
    var _FIXED_RESTORE = ['rag', 'ucr', 'web', 'historical',
                           'web_t1', 'web_t2', 'web_t3'];
    for (var gk = 0; gk < gkeys.length; gk++) {
      if (_FIXED_RESTORE.indexOf(gkeys[gk]) === -1) dyn.push(gkeys[gk]);
    }
    // Keep the live order: local first, then web T1/T2/T3.
    var order = ['rag', 'web_t1', 'web_t2', 'web_t3', 'ucr', 'web', 'historical']
      .concat(dyn);
    var total = 0;
    for (var g = 0; g < order.length; g++) total += (_restoredGroups[order[g]] || []).length;
    citationCount.textContent = String(total) + (_restoredRefPool ? ' · ' + _restoredRefPool : '');
    var html = '';
    var firstOpen = true;
    for (var gi = 0; gi < order.length; gi++) {
      var gk = order[gi];
      var items = _restoredGroups[gk] || [];
      if (items.length === 0) continue;
      var sm2 = SOURCE_META[gk] || srcMeta({ source: gk });
      var gid = 'restore-' + gi;
      var gOpen = firstOpen;
      firstOpen = false;
      html += '<div class="src-group">';
      html += '<div class="src-group-header" onclick="toggleSrcGroup(\'' + gid + '\')">';
      html += '<span class="toggle-icon' + (gOpen ? '' : ' collapsed') + '">▶</span>';
      html += '<span class="src-badge ' + sm2.cls + '">' + escapeHtml(sm2.tag) + ':</span>';
      html += '<span class="round-title" style="font-size:11px">' + escapeHtml(sm2.name) + '</span>';
      html += '<span class="round-count">' + items.length + '</span></div>';
      html += '<div class="src-group-body" id="srcgroup-' + gid + '"' + (gOpen ? '' : ' style="display:none"') + '>';
      // v8.15.2: 组内按 ref_id 升序（重排后数字/W/H 均连续，_refIdSort 保序）
      // v10.13（Task E，用户指示）：**web 组内改按来源分级排序** T1 → T2 → T3 → TU，
      //   同 tier 内再按 ref_id。用户要求"每一个 url 分开，T1T2T3 优先排列"。
      //   其它组（本地文献库 / UCR / 历史）保持原 ref_id 升序，不受影响。
      // v10.13：web 组已按 tier 拆组，组内改用 `_webRefSort`（按 Wn 数字升序）；
      // 本地文献组用 `_refIdSort`（[n] 升序）。
      var gItems = (gk.indexOf('web_') === 0) ? _webRefSort(items) : _refIdSort(items);
      for (var m2 = 0; m2 < gItems.length; m2++) {
        html += renderCitationItem(gItems[m2]);
      }
      html += '</div></div>';
    }
    if (!html) return false;
    sidebarContent.innerHTML = html;
    return true;
  }

  function renderRoundSidebar() {
    if (roundHistory.length === 0) {
      // v9.2: 无实时轮次时优先显示刷新恢复的 4 组合并视图
      if (renderRestoredGroups()) return;
      sidebarContent.innerHTML = '<div class="empty-sidebar">提交问题后文献将在此展示</div>';
      citationCount.textContent = '0';
      return;
    }
    var tC = 0, tU = 0, tW = 0, tH = 0;
    for (var i = 0; i < roundHistory.length; i++) {
      tC += roundHistory[i].cited.length; tU += roundHistory[i].uncited.length;
      tW += (roundHistory[i].web || []).length;
      tH += (roundHistory[i].historical || []).length;
    }
    citationCount.textContent = (tC + tU + tW + tH);
    var html = '';
    for (var i = roundHistory.length - 1; i >= 0; i--) {
      var round = roundHistory[i];
      // v8.15: 不再过滤 web 条目——按来源分组展示（本地文献库/UCR品种库/联网搜索/历史）
      var all = [];
      for (var k = 0; k < round.cited.length; k++) all.push(round.cited[k]);
      for (var k = 0; k < round.uncited.length; k++) all.push(round.uncited[k]);
      for (var k = 0; k < (round.historical || []).length; k++) all.push(round.historical[k]);
      // v10.14（阶段 3）：并入联网条目（`web_separate` ⇒ `round.web`）。
      //   这是「侧栏不显示联网条目」的修复点：此前该数组根本没进 `all`。
      for (var k = 0; k < (round.web || []).length; k++) all.push(round.web[k]);
      if (all.length === 0) continue;
      var qp = round.query.length > 25 ? round.query.substring(0, 25) + '...' : round.query;
      var open = i === roundHistory.length - 1;
      html += '<div class="round-group">';
      html += '<div class="round-header" onclick="toggleRound(\'round-' + round.roundId + '\')">';
      html += '<span class="toggle-icon" id="round-' + round.roundId + '-icon">▶</span>';
      html += '<span class="round-title">Round ' + escapeHtml(String(round.roundId)) + ': ' + escapeHtml(qp) + '</span>';
      html += '<span class="round-count">' + all.length + '</span></div>';
      html += '<div class="round-body" id="round-' + round.roundId + '"' + (open ? '' : ' style="display:none"') + '>';
      // v8.15: 按来源分组（固定顺序：本地文献库→UCR品种库→联网搜索→历史证据）
      // v9.2 (P0#4): 未知来源（非 4 内置组）动态归组，排在固定 4 组之后
      var groups = {};
      for (var m = 0; m < all.length; m++) {
        var key = srcKey(all[m]);
        if (!groups[key]) groups[key] = [];
        groups[key].push(all[m]);
      }
      // v10.13（改动 3.2）：固定 4 组顺序 —— 本地文献 → 联网 T1 → T2 → T3
      // ── v10.19 修复：**分组标题重复列出**（用户截图：T1 出现两次、T3 出现两次）──
      //   缺陷：动态段只排除了 `rag/ucr/web/historical`，**没排除 `web_t1/t2/t3`**——
      //   而这三个键在前半段已列过 ⇒ `groupOrder` 里各出现**两次** ⇒ 同组渲染两遍。
      //   实测吻合：T1 两次、T3 两次，T2 因空被 `continue` 跳过（故只两次两组）。
      //   修法：把 `web_t1/web_t2/web_t3` 一并纳入排除集（固定段用固定顺序，动态段只收未知组）。
      var _FIXED_KEYS = ['rag', 'ucr', 'web', 'historical',
                         'web_t1', 'web_t2', 'web_t3'];
      var groupOrder = ['rag', 'web_t1', 'web_t2', 'web_t3']
        .concat(Object.keys(groups).filter(function (k) {
          return _FIXED_KEYS.indexOf(k) === -1;
        }));
      var firstOpen = true;
      for (var gi = 0; gi < groupOrder.length; gi++) {
        var gk = groupOrder[gi];
        if (!groups[gk] || groups[gk].length === 0) continue;
        var sm2 = SOURCE_META[gk] || srcMeta({ source: gk });
        var gid = round.roundId + '-' + gk;
        var gOpen = open && firstOpen;
        firstOpen = false;
        html += '<div class="src-group">';
        html += '<div class="src-group-header" onclick="toggleSrcGroup(\'' + gid + '\')">';
        html += '<span class="toggle-icon' + (gOpen ? '' : ' collapsed') + '">▶</span>';
        html += '<span class="src-badge ' + sm2.cls + '">' + escapeHtml(sm2.tag) + ':</span>';
        html += '<span class="round-title" style="font-size:11px">' + escapeHtml(sm2.name) + '</span>';
        html += '<span class="round-count">' + groups[gk].length + '</span></div>';
        html += '<div class="src-group-body" id="srcgroup-' + gid + '"' + (gOpen ? '' : ' style="display:none"') + '>';
        // v8.15.2: 组内按 ref_id 升序（回答乱序引用时侧栏保持编号顺序整齐）
        var gItems = _refIdSort(groups[gk]);
        for (var m2 = 0; m2 < gItems.length; m2++) {
          html += renderCitationItem(gItems[m2]);
        }
        html += '</div></div>';
      }
      html += '</div></div>';
    }
    sidebarContent.innerHTML = html || '<div class="empty-sidebar">本轮未检索到相关文献</div>';
  }

/* v8.4.9 会话持久化：刷新/关闭重开后恢复历史对话渲染（数据在 sessions.db，只缺读取通道）
   v8.10l: 恢复增强——工具链折叠块（tool 消息）+ 深度思考折叠块（reasoning 持久化） */
// S3: history paging + file→turn jump. The server returns the newest page and
// a ``before`` cursor; older pages are prepended in memory and re-rendered, so a
// long session can be walked backwards without losing the latest turns.
var _historyMsgs = [];
var _historyTurns = null;    // canonical /messages.turns; null = legacy fallback
var _historyHasMore = false;
var HISTORY_PAGE = 60;

async function fetchHistoryPage(limit, before, signal) {
  var epoch = authEpoch, sid = sessionId, version = historyRestoreVersion;
  function owns() { return !signal.aborted && epoch === authEpoch && sid === sessionId && version === historyRestoreVersion; }
  var url = '/api/v2/session/' + encodeURIComponent(sid) + '/messages?limit='
    + encodeURIComponent(limit) + (before ? '&before=' + encodeURIComponent(before) : '');
  var resp = await accountFetch(url, {signal: signal, cache: 'no-store'});
  if (!owns()) return null;
  if (resp.status === 404) {
    await recoverMissingSession(epoch, sid);
    return null;
  }
  var data = await resp.json();
  if (!owns()) return null;
  if (!resp.ok) {
    var error = new Error('history_unavailable');
    if (resp.status === 503 && data.code === 'history_unavailable' && data.session_id === sid) error.turns = data.turns;
    throw error;
  }
  return data;
}

function buildEarlierHistoryButton() {
  var wrap = document.createElement('div');
  wrap.className = 'history-earlier';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'history-earlier-btn';
  btn.textContent = '载入更早的对话';
  btn.onclick = function () { loadEarlierHistory(); };
  wrap.appendChild(btn);
  return wrap;
}

function syncEarlierHistoryButton() {
  var existing = chatContainer.querySelector('.history-earlier');
  if (_historyHasMore && !existing) {
    chatContainer.insertBefore(buildEarlierHistoryButton(), chatContainer.firstChild);
  } else if (!_historyHasMore && existing) {
    existing.remove();
  }
}

async function loadEarlierHistory() {
  if (!sessionId || historyRequest || historyPageRequest || !_historyHasMore) return false;
  var oldest = Number((_historyMsgs.find(function (message) { return Number(message.message_id) > 0; }) || {}).message_id);
  if (!oldest) return false;
  var page = new AbortController(), round = currentRoundId;
  historyPageRequest = page;
  try {
    var data = await fetchHistoryPage(HISTORY_PAGE, oldest, page.signal);
    if (!data || historyPageRequest !== page || round !== currentRoundId) return false;
    var seen = new Set(_historyMsgs.map(function (message) { return String(message.message_id); }));
    var fresh = (data.messages || []).filter(function (message) {
      var id = String(message.message_id);
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    });
    _historyHasMore = !!data.has_more && fresh.length > 0;
    if (!fresh.length) { syncEarlierHistoryButton(); return false; }
    var earlierTurns = normalizeHistoryTurns(data.turns);
    var knownTurns = _historyTurns || groupHistoryTurns(_historyMsgs);
    var keys = new Set(knownTurns.map(function (turn) { return turn.turnRequestId; }).filter(Boolean));
    var insertTurns = (earlierTurns.length ? earlierTurns : groupHistoryTurns(fresh)).filter(function (turn) { return !keys.has(turn.turnRequestId); });
    _historyMsgs = fresh.concat(_historyMsgs);
    _historyTurns = mergeHistoryTurns(earlierTurns.length ? earlierTurns : groupHistoryTurns(fresh), knownTurns);
    renderHistoryMessages(fresh, insertTurns, {prepend: true});
    jumpToMessage(oldest, {silent: true});
    return true;
  } catch (_) {
    if (historyPageRequest === page) showBanner('更早的对话暂时无法读取，请重试。');
    return false;
  } finally {
    if (historyPageRequest === page) historyPageRequest = null;
  }
}

function jumpToMessage(messageId, opts) {
  opts = opts || {};
  var mid = String(messageId || '');
  if (!mid) return false;
  var node = chatContainer.querySelector('[data-message-id="' + mid + '"]');
  return highlightTurnNode(node, opts);
}

function highlightTurnNode(node, opts) {
  if (!node) return false;
  // Center the whole bubble, not just its text box; the id can live on .content.
  var host = node.closest('.message') || node;
  try { host.scrollIntoView({block: 'center', behavior: opts.silent ? 'auto' : 'smooth'}); }
  catch (e) { host.scrollIntoView(); }
  if (!opts.silent) {
    var lit = node.classList.contains('message') ? node : (host.classList.contains('message') ? host : node);
    lit.classList.remove('is-jump-target');
    void lit.offsetWidth;
    lit.classList.add('is-jump-target');
    setTimeout(function () { lit.classList.remove('is-jump-target'); }, 2400);
  }
  return true;
}

// Called from the file library: open the session that owns the file, then scroll
// to the turn that uploaded/produced it. Returns true only when the anchor was
// actually found and highlighted (no silent "jumped" claims).
window.jumpToFileTurn = async function (targetSessionId, requestId) {
  if (!targetSessionId || !requestId) return false;
  if (targetSessionId !== sessionId) switchSession(targetSessionId);
  var view = captureSessionView();
  if (!_historyMsgs.length) await restoreSessionHistory();
  if (!view.owns()) return false;
  var version = historyRestoreVersion;
  function mountedProducer() {
    return Array.prototype.find.call(chatContainer.querySelectorAll('.content'), function (content) {
      return content.dataset.sessionId === targetSessionId && turnFileScopes(content).has(String(requestId));
    });
  }
  if (highlightTurnNode(mountedProducer(), {})) return true;
  var target = null;
  for (var i = 0; i < _historyMsgs.length; i++) {
    if (String(_historyMsgs[i].request_id || '') === String(requestId)) { target = _historyMsgs[i]; break; }
  }
  if (!target) {
    // The turn may sit in an older page: walk back a bounded number of pages.
    for (var guard = 0; guard < 8 && _historyHasMore && !target; guard++) {
      if (!await loadEarlierHistory() || !view.owns() || version !== historyRestoreVersion) return false;
      if (highlightTurnNode(mountedProducer(), {})) return true;
      for (var j = 0; j < _historyMsgs.length; j++) {
        if (String(_historyMsgs[j].request_id || '') === String(requestId)) { target = _historyMsgs[j]; break; }
      }
    }
  }
  if (!target) return false;
  return jumpToMessage(target.message_id);
};

async function restoreSessionHistory() {
  if (!sessionId) return false;
  abortHistoryRequests();
  var epoch = authEpoch, sid = sessionId, round = currentRoundId;
  var version = ++historyRestoreVersion, read = new AbortController();
  historyRequest = read;
  refreshTaskComposer();
  function ownsRestore() { return epoch === authEpoch && sid === sessionId && round === currentRoundId && version === historyRestoreVersion; }
  try {
    var data = await fetchHistoryPage(HISTORY_PAGE, 0, read.signal);
    if (!ownsRestore()) return false;
    if (!data) throw new Error('history_unavailable');
    var restoredContext = CitrusTaskState.createContext({session_id: sid});
    var savedContext = data.context_snapshot || data.context;
    if (savedContext) renderContextPanel(restoredContext.receive('context_snapshot', savedContext));
    _historyMsgs = data.messages || [];
    var queueTurns = await loadQueueTurns(sid);
    if (!ownsRestore()) return false;
    queueSyncUnavailable = queueTurns === null;
    if (queueSyncUnavailable && turnQueueBindings.length) return false;
    var savedTurns = normalizeHistoryTurns(data.turns);
    _historyTurns = mergeQueuedHistory(savedTurns.length ? savedTurns : groupHistoryTurns(_historyMsgs), queueTurns || []);
    if (!_historyTurns.length) _historyTurns = null;
    _historyHasMore = !!data.has_more;
    renderHistoryMessages(_historyMsgs, _historyTurns);
    return true;
  } catch (error) {
    if (!ownsRestore()) return false;
    var known = new Set(Array.from(chatContainer.querySelectorAll('[data-job-id]'), function (node) { return node.dataset.jobId; }));
    var discovered = normalizeHistoryTurns(error.turns).filter(function (turn) { return turn.jobId && !known.has(turn.jobId); });
    if (discovered.length) renderHistoryMessages([], discovered, {append: true});
    var existing = chatContainer.querySelector('.history-retry'); if (existing) existing.remove();
    var retry = document.createElement('button'); retry.type = 'button'; retry.className = 'history-retry';
    retry.textContent = '历史暂时无法读取，点击重试';
    retry.onclick = function() { if (ownsRestore()) { retry.remove(); restoreSessionHistory(); } };
    chatContainer.appendChild(retry);
    return false;
  } finally {
    if (historyRequest === read) { historyRequest = null; refreshTaskComposer(); }
  }
}
window.addEventListener('pageshow', function(event) {
  if (!event.persisted || !currentAccount || accountDocumentRetired || !sessionId) return;
  disposeTaskView();
  restoreSessionHistory();
  loadSessions();
});

/* ─── P0-A: 逻辑轮次归并（历史恢复与实时流式共用同一套归属规则） ───
   一轮 = 用户问题 + 公开工作说明 + 全部工具调用/结果（含失败与重试）
        + 最终回答 + 该轮生成的文件。
   归并键是 turn_request_id（绝不用"文本相同"判重，因此同一轮里两次相同的
   检索都会保留）；幂等键是 message_id / tool_call_id / file_id。
   规则与 src/api/main.py 的 build_history_turns() 逐条对应。 */
function newHistoryTurn(key) {
  return {turnRequestId: key, user: null, answer: null, trace: [], files: [], messageIds: []};
}

function findTraceCall(turn, toolCallId, attempt) {
  if (!toolCallId) return null;
  for (var i = 0; i < turn.trace.length; i++) {
    var e = turn.trace[i];
    if ((e.kind === 'call' || e.kind === 'tool') && e.tool_call_id === toolCallId && String(e.attempt || 1) === String(attempt || 1)) return e;
  }
  return null;
}

function pushTurnFile(turn, file) {
  var fid = String((file && file.file_id) || '');
  if (!fid) return;
  for (var i = 0; i < turn.files.length; i++) {
    if (String((turn.files[i] || {}).file_id || '') === fid) return;
  }
  turn.files.push(file);
}

function groupHistoryTurns(msgs) {
  var turns = [];
  var current = null;
  // 一条持久化的助手行会产出两个 item（tool_calls + 公开工作说明），
  // 所以身份是 (message_id, role)，不是 message_id 单键。
  var seenItems = {};
  var list = msgs || [];
  for (var i = 0; i < list.length; i++) {
    var m = list[i];
    if (!m || typeof m !== 'object') continue;
    var mid = String(m.message_id || '');
    var role = String(m.role || '');
    if (role === 'assistant' && m.progress) continue;
    if (mid) {
      var itemKey = mid + '|' + role;
      if (seenItems[itemKey]) continue;         // same page merged twice
      seenItems[itemKey] = true;
    }
    var key = String(m.turn_request_id || m.request_id || '');
    if (role === 'user') {
      current = newHistoryTurn(key);
      turns.push(current);
      if (mid) current.messageIds.push(mid);
      var attachments = m.attachments || [];
      var uploaded = [], generated = [];
      for (var a = 0; a < attachments.length; a++) {
        var f = attachments[a];
        if (!f) continue;
        if (String(f.origin || '') === 'generated') generated.push(f); else uploaded.push(f);
      }
      current.user = {content: m.content || '', message_id: mid, request_id: key,
                      attachments: uploaded};
      for (var g = 0; g < generated.length; g++) pushTurnFile(current, generated[g]);
      continue;
    }
    if (!current || (key && key !== current.turnRequestId)) {
      // 分页窗口可能从一轮中间开始：这些行归到自己的轮次键下，
      // 不能挂到下一个问题上（否则刷新后同一轮会被拆成两个气泡）。
      current = newHistoryTurn(key);
      turns.push(current);
    }
    if (mid && current.messageIds.indexOf(mid) < 0) current.messageIds.push(mid);
    if (role === 'tool_calls') {
      var calls = m.calls || [];
      for (var c = 0; c < calls.length; c++) {
        var call = calls[c] || {};
        var cid = String(call.id || '');
        if (cid && findTraceCall(current, cid, call.attempt)) continue;
        current.trace.push(Object.assign({}, call, {kind: 'call', tool_call_id: cid,
                            name: String(call.name || 'tool'),
                            summary: String(call.summary || ''), message_id: mid}));
      }
    } else if (role === 'tool') {
      var tcid = String(m.tool_call_id || '');
      var entry = findTraceCall(current, tcid, m.attempt);
      if (!entry) {
        // 调用行缺失（旧数据/截断历史）时，结果本身仍是真实步骤：保留记录。
        entry = {kind: 'tool', tool_call_id: tcid, name: String(m.name || 'tool'), summary: ''};
        current.trace.push(entry);
      }
      ['logical_key', 'operation_id', 'attempt', 'aliases', 'facts', 'outcome', 'result_refs', 'result_id'].forEach(function (field) { if (m[field] != null) entry[field] = m[field]; });
      entry.status = m.status || m.content || '';
      entry.reason_code = m.reason_code || '';
      entry.action_hint = m.action_hint || '';
      entry.result_summary = m.summary || '';
      entry.completed_at_ms = Number(m.completed_at_ms) || 0;
      entry.elapsed_ms = Number(m.elapsed_ms) || 0;
      entry.execution_order_available = !!m.execution_order_available;
      entry.truncated = !!m.truncated;
      // 结果行单独记 id，不覆盖调用行的 message_id（工作说明要据此找到
      // 它下面那批调用并插到其上方）。
      entry.toolMessageId = mid || entry.toolMessageId || '';
    } else if (role === 'assistant') {
      current.trace.push({kind: 'note', content: String(m.content || ''), message_id: mid,
                          gen_time_ms: Number(m.gen_time_ms) || 0, answerCandidate: true});
    }
  }
  // 整页最后一条不是助手消息 => 最后一轮还在进行中（分页窗口截断在轮中），
  // 不把旁白提升成回答，避免刷新时把工作说明当成最终答案。
  var last = list.length ? (list[list.length - 1] || {}) : {};
  var lastIsAnswer = String(last.role || '') === 'assistant' && !last.progress;
  for (var ti = 0; ti < turns.length; ti++) {
    if (ti === turns.length - 1 && !lastIsAnswer) { turns[ti].partial = true; continue; }
    promoteLastAnswer(turns[ti]);
  }
  return turns;
}

function promoteLastAnswer(turn) {
  for (var pos = turn.trace.length - 1; pos >= 0; pos--) {
    var entry = turn.trace[pos];
    if (entry.answerCandidate) {
      turn.trace.splice(pos, 1);
      turn.answer = {content: entry.content || '', message_id: entry.message_id || '',
                     gen_time_ms: entry.gen_time_ms || 0};
      return;
    }
  }
}

function historyTraceStatus(entry) {
  return String(entry.status || (entry.content ? 'complete' : 'unavailable'));
}

/* 轨迹行的显示顺序：并行工具按真实完成时间（实时流即按到达顺序），
   没有时间戳的旧数据保持库内顺序；工作说明跟随它前面那一步，不被挪走。 */
function orderHistoryTrace(trace) {
  var out = [], anchor = Number.MAX_SAFE_INTEGER;
  for (var i = 0; i < trace.length; i++) {
    var e = trace[i];
    var ts = Number(e.completed_at_ms) || 0;
    if (ts > 0) anchor = ts;
    out.push({entry: e, sortKey: ts > 0 ? ts : anchor, index: i});
  }
  out.sort(function(a, b) {
    return a.sortKey === b.sortKey ? a.index - b.index : a.sortKey - b.sortKey;
  });
  return out.map(function(x) { return x.entry; });
}

/* Artifact delivery is an explicit server fact carried by the ordered event
   tail and the terminal snapshot. Render it as text only: the client never
   guesses download URLs and never promotes an unverified file. */
function artifactDeliveryText(delivery) {
  if (!delivery || !delivery.status || ['none', 'not_checked'].includes(delivery.status)) return '';
  var statuses = {available: '文件已交付', delivered: '文件已交付', verified: '文件已核实', partial: '部分文件已交付',
    unavailable: '文件暂不可用', failed: '文件交付未完成'};
  var ids = Array.isArray(delivery.verified_file_ids) ? delivery.verified_file_ids : [];
  var unavailable = Number(delivery.unavailable_count);
  var parts = [statuses[delivery.status] || ('文件交付状态：' + delivery.status)];
  if (ids.length) parts.push('已核实 ' + ids.length + ' 个文件');
  if (Number.isFinite(unavailable) && unavailable > 0) parts.push(Math.floor(unavailable) + ' 个文件暂不可用');
  return parts.join(' · ');
}
function renderArtifactDelivery(container, delivery) {
  if (!container) return null;
  var node = container.querySelector('.turn-delivery');
  if (!node) {
    node = document.createElement('div'); node.className = 'turn-delivery';
    node.setAttribute('role', 'status');
    container.appendChild(node);
  }
  var text = artifactDeliveryText(delivery);
  node.hidden = !text;
  if (!text) { node.replaceChildren(); return node; }
  var ids = Array.isArray(delivery.verified_file_ids) ? delivery.verified_file_ids : [];
  var unavailable = Number(delivery.unavailable_count);
  node.dataset.status = String(delivery.status);
  node.dataset.verifiedCount = String(ids.length);
  node.dataset.unavailableCount = String(Number.isFinite(unavailable) && unavailable > 0 ? Math.floor(unavailable) : 0);
  if (node.textContent !== text) node.textContent = text;
  return node;
}

/* Canonical turn adapter: the server's ``/messages.turns`` uses snake_case
   for a few keys; normalise them once so rendering never cares where the
   turn came from. A legacy session without ``turns`` still goes through
   groupHistoryTurns() below. */
function normalizeHistoryTurn(raw) {
  var t = raw || {};
  var trace = [];
  var rawTrace = Array.isArray(t.trace) ? t.trace : [];
  for (var i = 0; i < rawTrace.length; i++) {
    var e = rawTrace[i] || {};
    var entry = Object.assign({}, e, {
      kind: String(e.kind || 'call'),
      tool_call_id: String(e.tool_call_id || ''),
      name: String(e.name || ''),
      summary: String(e.summary || ''),
      content: String(e.content || ''),
      message_id: (e.message_id != null ? e.message_id : 0),
      gen_time_ms: Number(e.gen_time_ms) || 0
    });
    if (e.status != null) entry.status = e.status;
    if (e.reason_code != null) entry.reason_code = e.reason_code;
    if (e.action_hint != null) entry.action_hint = e.action_hint;
    if (e.result_summary != null) entry.result_summary = e.result_summary;
    if (e.completed_at_ms != null) entry.completed_at_ms = Number(e.completed_at_ms) || 0;
    if (e.elapsed_ms != null) entry.elapsed_ms = Number(e.elapsed_ms) || 0;
    if (e.execution_order_available != null) entry.execution_order_available = !!e.execution_order_available;
    if (e.truncated != null) entry.truncated = !!e.truncated;
    var toolMsgId = (e.tool_message_id != null ? e.tool_message_id : e.toolMessageId);
    if (toolMsgId != null) entry.toolMessageId = toolMsgId;
    if (e.answer_candidate || e.answerCandidate) entry.answerCandidate = true;
    trace.push(entry);
  }
  var files = [];
  var rawFiles = Array.isArray(t.files) ? t.files : [];
  for (var f = 0; f < rawFiles.length; f++) { if (rawFiles[f]) files.push(rawFiles[f]); }
  var ids = Array.isArray(t.message_ids) ? t.message_ids
    : (Array.isArray(t.messageIds) ? t.messageIds : []);
  return {
    turnRequestId: String(t.turn_request_id || t.turnRequestId || ''),
    user: t.user || null,
    answer: t.answer || null,
    trace: trace,
    files: files,
    messageIds: ids.slice(),
    partial: !!t.partial,
    intentSummary: publicTaskStage(t.intent_summary || t.task_summary),
    displayStatus: CitrusTaskState.displayStatus(t),
    stageLabel: publicTaskStage(t.stage_label),
    jobId: String(t.job_id || ''),
    jobStatus: String(t.job_status || ''),
    terminalSnapshot: t.terminal_snapshot || null,
    contextSnapshot: t.context_snapshot || (t.terminal_snapshot || {}).context_snapshot || null,
    artifactDelivery: t.artifact_delivery || (t.terminal_snapshot || {}).artifact_delivery || null,
    replayCursor: Number.isSafeInteger(t.replay_cursor) ? t.replay_cursor : 0,
    question: t.question || null,
    parentJobId: String(t.parent_job_id || ''),
    parentQuestionId: String(t.parent_question_id || '')
  };
}

function normalizeHistoryTurns(list) {
  var out = [];
  if (!Array.isArray(list)) return out;
  for (var i = 0; i < list.length; i++) {
    if (list[i] && typeof list[i] === 'object') out.push(normalizeHistoryTurn(list[i]));
  }
  return out;
}

/* Paging merges two server windows that can overlap on the boundary turn.
   The same turn key is one logical turn: join it instead of rendering the
   question twice. Entries are matched by tool_call_id / (message_id, text). */
function mergeHistoryTurns(earlierTurns, laterTurns) {
  var out = [];
  var byKey = Object.create(null);
  function mergeEntry(base, incoming) {
    for (var k = 0; k < base.trace.length; k++) {
      var existing = base.trace[k];
      var same = false;
      if (Number.isSafeInteger(incoming.seq) && Number.isSafeInteger(existing.seq)) same = incoming.seq === existing.seq;
      else if (incoming.tool_call_id || existing.tool_call_id) {
        same = incoming.tool_call_id && existing.tool_call_id === incoming.tool_call_id
          && existing.kind === incoming.kind && String(existing.attempt || 1) === String(incoming.attempt || 1);
      } else {
        same = incoming.message_id && existing.kind === incoming.kind
          && String(existing.message_id || '') === String(incoming.message_id);
      }
      if (!same) continue;
      Object.keys(incoming).forEach(function (key) {
        if (incoming[key] != null && incoming[key] !== '') existing[key] = incoming[key];
      });
      if (!existing.toolMessageId && incoming.toolMessageId) existing.toolMessageId = incoming.toolMessageId;
      return;
    }
    base.trace.push(incoming);
  }
  function adopt(turn) {
    var key = String(turn.turnRequestId || '');
    if (key && byKey[key]) {
      var base = byKey[key];
      if (!base.user && turn.user) base.user = turn.user;
      if (turn.answer) base.answer = turn.answer;
      if (turn.intentSummary) base.intentSummary = turn.intentSummary;
      if (turn.displayStatus) base.displayStatus = turn.displayStatus;
      if (turn.stageLabel) base.stageLabel = turn.stageLabel;
      ['jobId', 'jobStatus', 'terminalSnapshot', 'contextSnapshot', 'artifactDelivery', 'question', 'parentJobId', 'parentQuestionId'].forEach(function (key) { if (turn[key]) base[key] = turn[key]; });
      // Merging history windows never advertises events as consumed.
      base.replayCursor = 0;
      for (var i = 0; i < turn.trace.length; i++) mergeEntry(base, turn.trace[i]);
      for (var f = 0; f < turn.files.length; f++) pushTurnFile(base, turn.files[f]);
      for (var m = 0; m < turn.messageIds.length; m++) {
        if (base.messageIds.indexOf(turn.messageIds[m]) < 0) base.messageIds.push(turn.messageIds[m]);
      }
      // A boundary turn is partial only until the page that contains its
      // answer arrives; a merged turn with an answer is complete again.
      base.partial = !!(turn.partial && !base.answer);
      return;
    }
    if (key) byKey[key] = turn;
    out.push(turn);
  }
  for (var a = 0; a < (earlierTurns || []).length; a++) adopt(earlierTurns[a]);
  for (var b = 0; b < (laterTurns || []).length; b++) adopt(laterTurns[b]);
  return out;
}

function showQuestionContinuation(container, childJobId, owns) {
  if (!container || !childJobId || container.querySelector('.task-question-continue')) return;
  var link = document.createElement('button'); link.type = 'button'; link.className = 'task-question-continue';
  link.textContent = '查看继续任务';
  link.addEventListener('click', function () { if (owns()) refreshQuestionContinuation({child_job_id: childJobId}, owns, container); });
  container.appendChild(link);
}

async function refreshQuestionContinuation(result, owns, container) {
  if (owns() && !await restoreSessionHistory() && owns()) showQuestionContinuation(container, result.child_job_id, owns);
}

function observeQueuedJob(turn) {
  if (['running', 'awaiting_input', 'settling', 'terminal'].indexOf(turn.state) < 0) return '';
  if (turn.state === 'terminal' && !turn.dispatch_at) return '';
  return String(turn.active_child_job_id || turn.job_id || '');
}

function loadQueueTurns(sid) {
  if (!sid || sid !== sessionId) return Promise.resolve(null);
  return sessionQueueObserver(sid).snapshot();
}

function mergeQueuedHistory(history, entries) {
  var byRequest = new Map(), byJob = new Map(), hidden = new Set(), missing = [];
  history.forEach(function (turn) {
    if (turn.turnRequestId) byRequest.set(turn.turnRequestId, turn);
    if (turn.jobId) byJob.set(turn.jobId, turn);
  });
  entries.forEach(function (entry) {
    var root = byRequest.get(entry.client_request_id);
    var observed = observeQueuedJob(entry);
    var segment = observed && byJob.get(observed);
    if (!root) {
      if (entry.state === 'terminal' && !segment && (!entry.clear_reason || entry.dispatch_at)) return;
      root = normalizeHistoryTurn({turn_request_id: entry.client_request_id});
      missing.push(root);
    }
    if (!root.user) root.user = {content: entry.query || '', request_id: entry.client_request_id, attachments: entry.attachments || []};
    if (segment && segment !== root) {
      if (segment.answer) {
        root.answer = segment.answer;
        root.answerProducerJobId = segment.jobId;
      }
      root.trace = segment.trace;
      root.messageIds = root.messageIds.concat(segment.messageIds);
    }
    if (observed && observed !== root.jobId) {
      root.jobId = observed;
      root.jobStatus = segment ? segment.jobStatus : '';
      root.question = segment ? segment.question : null;
      root.terminalSnapshot = segment ? segment.terminalSnapshot : null;
      root.contextSnapshot = segment ? segment.contextSnapshot : null;
      root.displayStatus = segment ? segment.displayStatus : '';
    }
    root.queueEntry = entry;
    var relative = segment;
    var producerRequests = [], files = new Map(root.files.map(function (file) { return [file.file_id, file]; }));
    while (relative && relative !== root && !hidden.has(relative)) {
      hidden.add(relative);
      if (relative.turnRequestId) producerRequests.push(relative.turnRequestId);
      relative.files.forEach(function (file) { if (!files.has(file.file_id)) files.set(file.file_id, file); });
      relative = byJob.get(relative.parentJobId);
    }
    root.fileProducerRequestIds = producerRequests;
    root.files = Array.from(files.values());
  });
  return history.filter(function (turn) { return !hidden.has(turn); }).concat(missing);
}

function projectHistoryTrace(process, trace) {
  // Server trace order preserves public notes interleaved with action groups.
  // Never sort notes by a neighbouring tool's completion timestamp.
  var entries = (trace || []).slice(), seenSeq = new Set();
  if (entries.length && entries.every(function (entry) { return Number.isSafeInteger(entry.seq); })) entries.sort(function (a, b) { return a.seq - b.seq; });
  entries.forEach(function (entry) {
    if (Number.isSafeInteger(entry.seq)) { if (seenSeq.has(entry.seq)) return; seenSeq.add(entry.seq); }
    if (entry.event) { process.receive(entry.event, Object.assign({}, entry.data || {}, entry.seq != null ? {seq: entry.seq} : {})); return; }
    if (entry.kind === 'call' || entry.kind === 'tool') process.receive('tool_result', Object.assign({}, entry, {
      tool_name: entry.tool_name || entry.name, display_name: entry.display_name || displayToolName(entry.name),
      summary: entry.result_summary || entry.summary, status: entry.status || 'unavailable'
    }));
  });
}

function isPublicAnswerEvent(data) {
  return data.private !== true && data.internal !== true && ['private', 'internal'].indexOf(data.visibility) < 0;
}

function bindHistoryTask(turn, content, answer, process, ownsContext, useComposer, userContent) {
  var epoch = authEpoch, sid = sessionId, round = currentRoundId, armed = false, streamedAnswer = '', closed = false;
  var binding = null, entry = turn.queueEntry, unwatchQueue = null, observed = '', queueStatus, disconnected = false;
  var hasSavedAnswer = !!String(turn.answer && turn.answer.content || '').trim(), finalAnswerReceived = false;
  var citationContainer = content.querySelector('.turn-citation-bubbles');
  function syncWaiting() {
    process.syncTask(store.state, entry, {hasAnswer: hasSavedAnswer || !!streamedAnswer.trim(),
      disconnected: disconnected, stopping: !!(binding && binding.stopping)});
  }
  function owns() { return !closed && epoch === authEpoch && sid === sessionId && chatContainer.contains(content); }
  function ownsComposer() { return owns() && activeTurnComposer === binding; }
  function projectContext() {
    if (!binding && owns() && ownsContext && round === currentRoundId && store.state.context_snapshot) {
      renderContextPanel(Object.assign({}, store.state.context_snapshot, {view_scope: 'previous'}));
    }
  }
  function questionLabel() {
    if (!store.state.question || CitrusTaskState.isTerminal(store.state)) return;
    var labels = {pending: '等待你的选择', answered: '已答复并继续', cancelled: '选择已取消', expired: '选择已过期'};
    var label = labels[store.state.question.status];
    if (label) process.label.textContent = label;
  }
  function reconcileControls() {
    if (binding && owns()) selectActiveTurn();
    if (!ownsComposer()) return;
    var state = store.state, terminal = CitrusTaskState.isTerminal(state);
    syncPermissionCards(state.job_id, terminal ? [] : state.approvals);
  }
  function snapshot(job) {
    if (!owns()) return;
    disconnected = false;
    process.receive('snapshot', job);
    renderTurnTimings(content, job);
    renderArtifactDelivery(content, store.state.artifact_delivery);
    answer.dataset.displayStatus = store.state.display_status;
    if (!process.terminal(store.state.display_status)) {
      var labels = {detached: '任务在后台执行'};
      if (labels[store.state.status]) process.label.textContent = labels[store.state.status];
    }
    projectContext(); questionLabel(); syncWaiting(); reconcileControls();
    if (CitrusTaskState.isTerminal(store.state)) loadGeneratedFiles(job);
  }
  function loadGeneratedFiles(data) {
    var jobId = store.state.job_id, recovery = store.state.recovery_cursor;
    if (!jobId) return;
    restoreTurnGeneratedFiles(content, sid, turn.turnRequestId, continuationProducer(entry, jobId),
      data, store.state.artifact_delivery, function () {
        return owns() && store.state.job_id === jobId && store.state.recovery_cursor === recovery;
      });
  }
  function dispose() {
    if (closed) return;
    closed = true; process.stopClock(); if (unwatchQueue) unwatchQueue(); transport.stop(); controls.dispose(); questionCard.dispose(); store.clear();
    composerRunning = false;
    unregisterTurnBinding(binding);
  }
  function cancel(reason) {
    if (reason === 'account' || reason === 'switch') { dispose(); return; }
    if (!ownsComposer() || CitrusTaskState.isTerminal(store.state)) return;
    binding.stopping = true;
    syncWaiting();
    refreshTaskComposer();
    // stopCurrentTask posts once to this exact job; observation confirms stop.
    if (store.state.job_id) transport.follow(store.state.job_id);
  }
  var store = CitrusTaskState.create({request_id: turn.turnRequestId, session_id: sid, onEvent: function (type, data) {
    if (!owns()) return;
    process.receive(type, data);
    renderArtifactDelivery(content, store.state.artifact_delivery);
    if (type === 'agent_recovery' && data.status === 'admitted') {
      answer.dataset.displayStatus = '';
      finalAnswerReceived = false; armed = false; streamedAnswer = ''; content._turnFileRead = null;
      hasSavedAnswer = false; answer.replaceChildren();
      if (citationContainer) renderCitationBubbles(citationContainer, [], turn.turnRequestId, sid);
      var timing = content.querySelector('.turn-timings'); if (timing) timing.remove();
    }
    if (type.indexOf('context_') === 0 || data.context_snapshot) projectContext();
    if (type === 'citations' && data.display === 'bubble_only') {
      if (!citationContainer) {
        citationContainer = document.createElement('div'); content.appendChild(citationContainer);
      }
      renderCitationBubbles(citationContainer, data.bubbles, turn.turnRequestId, sid,
        continuationProducer(entry, store.state.job_id));
    }
    if (!finalAnswerReceived && isPublicAnswerEvent(data)) {
      if (type === 'answer_start') armed = true;
      if (type === 'text' && armed && data.delivery === 'committed' && typeof data.content === 'string') {
        streamedAnswer += data.content;
        if (!hasSavedAnswer) answer.innerHTML = renderMarkdownSafe(streamedAnswer);
      }
      if (type === 'text_reset') {
        armed = false; streamedAnswer = '';
        if (!hasSavedAnswer) answer.replaceChildren();
      }
    }
    if ((type === 'done' || type === 'error') && data.answer) {
      answer.innerHTML = renderMarkdownSafe(data.answer);
      if (String(data.answer).trim()) process.answerVisible();
    }
    if ((type === 'done' || type === 'error') && CitrusTaskState.displayStatus(data)) {
      renderTurnTimings(content, data);
      finalAnswerReceived = true;
      if (CitrusTaskState.isTerminal(store.state)) {
        appendMessageActions(content, 'assistant', data.answer || streamedAnswer || (turn.answer && turn.answer.content));
      }
      process.terminal(CitrusTaskState.displayStatus(data));
      answer.dataset.displayStatus = CitrusTaskState.displayStatus(data);
      loadGeneratedFiles(data);
    }
    if (type === 'request_user_input' || type === 'question' || type === 'done') questionLabel();
    syncWaiting();
    reconcileControls();
  }});
  var transport = CitrusTaskTransport.create({store: store, fetch: accountFetch, owns: owns, onSnapshot: snapshot,
    onTerminalDelivery: function (data) {
      if (!owns() || finalAnswerReceived) return;
      process.receive('done', data);
      if (data.answer) { answer.innerHTML = renderMarkdownSafe(data.answer); process.answerVisible(); }
      finalAnswerReceived = true;
      appendMessageActions(content, 'assistant', data.answer || streamedAnswer || (turn.answer && turn.answer.content));
    },
    onReadState: function (source, failed) {
      if (!owns()) return;
      process.readState(source, failed);
      if (failed && navigator.onLine === false) { disconnected = true; syncWaiting(); }
    },
    onStreamInterrupted: function () { if (owns()) { disconnected = true; syncWaiting(); } }});
  var controls = CitrusTaskControls.create({container: content, store: store, transport: transport, owns: owns,
    readDraft: readSupplementDraft, onReconcile: reconcileSupplement,
    onChange: function() {
      if (binding) reconcileControls();
    }});
  var questionCard = CitrusTaskControls.createQuestion({container: content, store: store, transport: transport, owns: owns,
    onContinue: function (result) {
      if (result.resume_job_id === store.state.job_id) transport.follow(result.resume_job_id);
      else if (!entry) refreshQuestionContinuation(result, owns, questionCard.element);
    }});
  store.hydrate({job_id: turn.jobId, status: turn.jobStatus, terminal_snapshot: turn.terminalSnapshot,
    artifact_delivery: turn.artifactDelivery, question: turn.question, context_snapshot: turn.contextSnapshot});
  if (useComposer || entry && entry.state !== 'terminal') {
    binding = {owns: owns, store: store, controls: controls, cancel: cancel, stopping: false,
      query: turn.user && turn.user.content,
      queueEntry: entry, following: false, dispose: dispose};
    registerTurnBinding(binding);
  }
  snapshot({status: turn.jobStatus, terminal_snapshot: turn.terminalSnapshot});
  if (!entry && turn.question && turn.question.child_job_id) showQuestionContinuation(questionCard.element, turn.question.child_job_id, owns);
  historyTaskDisposers.push(dispose);

  function adopt(entryNow) {
    if (!owns() || !CitrusTaskTransport.currentQueueEntry(entry, entryNow)) return;
    if (entryNow.state === 'terminal' && unwatchQueue) { unwatchQueue(); unwatchQueue = null; }
    entry = entryNow;
    if (binding) binding.queueEntry = entry;
    markQueuedBubble(userContent, content, entry);
    var reconciled = entry.state === 'terminal' && entry.clear_reason === 'turn_reconciled_by_user';
    queueStatus.hidden = entry.state !== 'reconcile_required' && !reconciled;
    queueStatus.replaceChildren();
    var label = document.createElement('span');
    label.textContent = entry.state === 'terminal' && entry.clear_reason === 'cancelled' ? '已取消' : queueStateLabel(entry);
    queueStatus.appendChild(label);
    if (reconciled) {
      label.textContent = '轮次已对账';
      appendPublicationReconciliation(queueStatus, sid, entry.client_request_id);
    }
    if (entry.state === 'terminal' && !entry.dispatch_at && entry.clear_reason && !hasSavedAnswer) {
      var cancelled = entry.clear_reason === 'cancelled';
      answer.dataset.displayStatus = cancelled ? 'cancelled' : 'failed';
      answer.textContent = cancelled ? '已取消，这条消息不会执行。' : '这条消息未能开始执行，请重新发送。';
    }
    var job = observeQueuedJob(entry);
    if (entry.state === 'reconcile_required') {
      var settle = document.createElement('button');
      settle.type = 'button'; settle.className = 'turn-queue-cancel turn-queue-reconcile';
      settle.textContent = '确认并继续';
      settle.setAttribute('aria-label', '确认上一轮结果无法核对并继续本会话');
      settle.onclick = async function () {
        if (!window.confirm('上一轮的执行结果无法核对。确认后不会重新执行它，只解除本会话的阻塞，以便继续提问。')) return;
        settle.disabled = true;
        try {
          var response = await accountFetch(turnQueueURL(sid) + '/' + encodeURIComponent(entry.client_request_id) + '/reconcile', {method: 'POST'});
          var result = await response.json();
          if (response.ok && owns()) adopt(result);
          else if (owns()) showBanner('状态核对未完成，请刷新后重试。');
        } catch (_) { if (owns()) showBanner('解除状态核对的请求暂未确认，请稍后重试。'); }
        finally { settle.disabled = false; }
      };
      appendPublicationReconciliation(queueStatus, sid, entry.client_request_id);
      queueStatus.appendChild(settle);
    }

    if (job && job !== observed) {
      if (observed && observed !== job) {
        hasSavedAnswer = false; answer.replaceChildren(); answer.dataset.displayStatus = '';
      }
      process.receive('job', {job_id: job});
      observed = job;
      streamedAnswer = ''; armed = false; finalAnswerReceived = false;
      content.dataset.jobId = job;
      transport.followContinuation(job);
    }
    syncWaiting();
    selectActiveTurn();
  }
  if (entry) {
    queueStatus = document.createElement('div'); queueStatus.className = 'turn-queue-state';
    queueStatus.setAttribute('role', 'status');
    content.appendChild(queueStatus);
    adopt(entry);
    if (entry.state !== 'terminal') unwatchQueue = watchTurnQueue(sid, entry.client_request_id, adopt);
  } else if (turn.jobId) transport.follow(turn.jobId);
}

function bindSettledHistoryDetails(turn, content, answer, process) {
  // Persistent preview is sufficient for the initial history viewport.
  // Fetch full public events only if this specific turn is expanded.
  var sid = sessionId, epoch = authEpoch, controller = null, done = false;
  var recoveryAnswer = !String(turn.answer && turn.answer.content || "").trim();
  var button = document.createElement('button');
  button.type = 'button'; button.className = 'research-tool-toggle';
  button.textContent = '查看完整过程';
  process.element.appendChild(button);
  function owns() { return epoch === authEpoch && sid === sessionId && chatContainer.contains(content); }
  historyTaskDisposers.push(function () { if (controller) controller.abort(); });
  button.onclick = async function () {
    if (controller || done || !owns()) return;
    controller = new AbortController(); button.disabled = true;
    var jobId = (turn.queueEntry || {}).active_child_job_id || turn.jobId;
    try {
      var cursor = 0, previous, page;
      do {
        previous = cursor;
        var response = await accountFetch('/jobs/' + encodeURIComponent(jobId) + '/events?after=' + cursor + '&limit=200',
                                          {signal: controller.signal});
        if (!response.ok) throw new Error('history_events_unavailable');
        page = await response.json();
        if (!owns()) return;
        if (page.protocol !== 'citrus-task/1' || page.job_id !== jobId || !Array.isArray(page.events)) throw new Error('history_event_identity_mismatch');
        page.events.forEach(function (event) {
          if (!Number.isSafeInteger(event.seq) || event.seq <= cursor) return;
          cursor = event.seq;
          var type = event.event, data = Object.assign({}, event.data || {}, {seq: event.seq});
          process.receive(type, data);
          if ((type === 'done' || type === 'error') && event.seq >= (page.recovery_cursor || 0)) renderTurnTimings(content, data);
          if (recoveryAnswer && event.seq >= (page.recovery_cursor || 0) && (type === 'done' || type === 'error') && typeof data.answer === 'string' &&
              data.answer.trim() && isPublicAnswerEvent(data)) {
            answer.innerHTML = renderMarkdownSafe(data.answer);
            appendMessageActions(content, 'assistant', data.answer);
          }
        });
        if (page.next_cursor !== cursor || page.has_more && cursor <= previous) throw new Error('history_event_cursor_invalid');
      } while (page.has_more && owns());
      if (!owns()) return;
      process.terminal(turn.displayStatus);
      done = true;
      button.textContent = cursor ? '完整过程已加载' : '没有更多过程记录';
    } catch (_) {
      if (owns()) button.textContent = '过程记录暂不可用，点击重试';
    } finally {
      controller = null;
      if (owns()) button.disabled = done;
    }
  };
}

function renderHistoryMessages(msgs, serverTurns, placement) {
  if (!placement) {
    disposeTaskView(true);
    clearSentAttachmentPreviewUrls();
    chatContainer.innerHTML = '';
  }
  var before = placement && placement.prepend ? chatContainer.querySelector('.message') : null;
  var turns = placement ? serverTurns : (serverTurns && serverTurns.length) ? serverTurns : groupHistoryTurns(msgs);
  var composerIndex = -1, lastJobIndex = -1;
  turns.forEach(function(turn, index) {
    if (!turn.jobId) return;
    lastJobIndex = index;
    if (!CitrusTaskState.displayStatus({terminal_snapshot: turn.terminalSnapshot}) &&
        (!turn.jobStatus || ['queued', 'running', 'detached', 'cancel_requested'].indexOf(turn.jobStatus) >= 0)) composerIndex = index;
  });
  if (composerIndex < 0) composerIndex = lastJobIndex;
  if (placement && (placement.prepend || activeTurnComposer)) composerIndex = -1;
  for (var ti = 0; ti < turns.length; ti++) {
    var turn = turns[ti], userContent = null;
    if (turn.user) {
      userContent = addMessage('user', escapeHtml(turn.user.content), before);
      // S3: stable anchors so a file can jump back to the exact turn.
      if (turn.user.message_id) userContent.dataset.messageId = String(turn.user.message_id);
      if (turn.user.request_id) userContent.dataset.requestId = String(turn.user.request_id);
      // P0-1: an upload belongs to the user bubble; a generated file belongs to
      // the assistant bubble of the same turn (placed further below).
      appendRestoredAttachmentCards(userContent, turn.user.attachments || []);
      appendMessageActions(userContent, 'user', turn.user.content || '');
    }
    var answer = turn.answer || {content: '', message_id: '', gen_time_ms: 0};
    if (!turn.user && !answer.content && !turn.trace.length && !turn.files.length && !turn.jobId) continue;
    // P0-A: 每轮只渲染一个助手气泡——与实时流式的形状完全一致
    // （一个气泡、一份执行轨迹、一个最终回答、一次生成文件）。
    var contentEl = addMessage('bot', '', before);
    if (turn.jobId) contentEl.dataset.jobId = turn.jobId;
    if (answer.message_id) contentEl.dataset.messageId = String(answer.message_id);
    if (turn.turnRequestId) contentEl.dataset.requestId = String(turn.turnRequestId);
    (turn.fileProducerRequestIds || []).forEach(function (rid) { recordTurnFileProducer(contentEl, rid); });
    contentEl.dataset.sessionId = sessionId;
    var answerBody = document.createElement('div'); answerBody.className = 'turn-answer';
    answerBody.dataset.displayStatus = turn.displayStatus;
    answerBody.innerHTML = renderMarkdownSafe(answer.content); contentEl.appendChild(answerBody);
    if (answer.citation_bubbles && answer.citation_bubbles.length) {
      var citationRow = document.createElement('div');
      contentEl.appendChild(citationRow);
      renderCitationBubbles(citationRow, answer.citation_bubbles, turn.turnRequestId, sessionId, turn.answerProducerJobId);
    }
    if (turn.artifactDelivery) renderArtifactDelivery(contentEl, turn.artifactDelivery);
    renderTurnTimings(contentEl, turn.terminalSnapshot);
    var process = createResearchProcessPanel(turn.intentSummary || (turn.user && turn.user.content));
    contentEl.insertBefore(process.element, contentEl.firstChild);
    process.receive('snapshot', {terminal_snapshot: turn.terminalSnapshot});
    if (String(answer.content || '').trim()) process.answerVisible(true);
    if (!process.terminal(turn.displayStatus) && turn.jobId && !turn.queueEntry) process.label.textContent = '状态未确认';
    if (turn.parentJobId || turn.parentQuestionId) {
      var relation = document.createElement('div'); relation.className = 'turn-continuation'; relation.textContent = '根据上一轮选择继续';
      contentEl.insertBefore(relation, process.element);
    }
    var savedSettlement = !!(turn.jobId && turn.displayStatus &&
      (!turn.queueEntry || turn.queueEntry.state === 'terminal') &&
      !(turn.question && turn.question.status === 'pending'));
    if (!turn.jobId || savedSettlement) projectHistoryTrace(process, turn.trace);
    var ownsContext = (!placement || placement.append) && ti === (composerIndex >= 0 ? composerIndex : turns.length - 1);
    if (savedSettlement) {
      bindSettledHistoryDetails(turn, contentEl, answerBody, process);
      if (ownsContext && turn.contextSnapshot) renderContextPanel(turn.contextSnapshot);
    } else if (turn.jobId || turn.question || turn.queueEntry) bindHistoryTask(turn, contentEl, answerBody, process, ownsContext, ti === composerIndex, userContent);
    else if (ownsContext) {
      var context = CitrusTaskState.createContext({session_id: sessionId, request_id: turn.turnRequestId});
      if (turn.contextSnapshot) context.receive('context_snapshot', turn.contextSnapshot);
      (turn.trace || []).forEach(function (entry) {
        if (entry.event && entry.event.indexOf('context_') === 0) context.receive(entry.event, Object.assign({}, entry.data || {}, {seq: entry.seq}));
      });
      if (context.current()) renderContextPanel(context.current());
    }
    if (!turn.jobId || process.terminal(turn.displayStatus)) {
      appendMessageActions(contentEl, 'assistant', answer.content || '');
    }
    // P0-A: 该轮生成文件只在这一处、按 file_id 幂等追加一次。
    if (turn.files.length) appendTurnGeneratedFiles(contentEl, turn.files, '本轮生成文件');
  }
  syncEarlierHistoryButton();
  syncHistoryTurnFiles();
  // Reading scrollHeight for every restored message forces a layout per row.
  if (!placement || placement.append) followChatTail(!placement);
}

/* v8.10: 无会话时自动创建默认会话（主流行为）——打开即有一个会话，历史回答有处可存，
   不再依赖用户手动点 ＋ 新建 */
function renderTurnTimings(container, source) {
  var envelope = source && (source.turn_envelope || source.terminal_snapshot);
  var timing = source && (source.turn_timings || envelope && envelope.turn_timings);
  if (!timing) return;
  var labels = {visible_answer_first_ms: '提交到首段可见答复', provider_first_packet_ms: '模型调用到服务商首包',
    queue_wait_ms: '队列等待', tool_elapsed_ms: '工具累计用时', context_maintenance_ms: '上下文维护用时'};
  var rows = Object.keys(labels).filter(function (key) { return Number.isFinite(timing[key]) && timing[key] >= 0; })
    .map(function (key) { return labels[key] + ' ' + (timing[key] / 1000).toFixed(1) + ' 秒'; });
  if (!rows.length) return;
  var badge = container.querySelector('.turn-timings');
  if (!badge) {
    badge = document.createElement('div'); badge.className = 'turn-timings';
    badge.style.cssText = 'margin-top:8px;font-size:11px;color:var(--text-muted)';
    badge.title = '服务端记录的各阶段用时，统计起点不同，不应直接相加。';
    container.appendChild(badge);
  }
  badge.textContent = rows.join(' · ');
}

var _sessionCreation = null;
async function createDefaultSession(epoch, preserveComposer) {
  if (epoch !== authEpoch || sessionId) return;
  if (_sessionCreation && _sessionCreation.epoch === epoch) return _sessionCreation.promise;
  var creation = {epoch: epoch};
  _sessionCreation = creation;
  creation.promise = (async function() {
    try {
      var resp = await accountFetch('/api/v1/session/new', {method: 'POST'});
      if (!resp.ok) return;
      var data = await resp.json();
      if (epoch !== authEpoch || sessionId || !data || typeof data.session_id !== 'string' || !data.session_id) return;
      sessionNavigation++;
      sessionId = data.session_id;
      restoreComposerSession(preserveComposer);
      localStorage.setItem(sessionStorageKey(), sessionId);
      window.dispatchEvent(new CustomEvent('citrus:session-changed', {detail:{session_id:sessionId}}));
    } catch (e) { /* Leave creation retryable on the next explicit initialization. */ }
    finally { if (_sessionCreation === creation) _sessionCreation = null; }
  })();
  return creation.promise;
}

async function recoverMissingSession(epoch, sid) {
  if (epoch !== authEpoch || !sid || sid !== sessionId) return;
  // Clear only the invalid session's rendered state. Composer draft, selected
  // local Files, preview URLs and account file references belong to the user.
  disposeTaskView();
  setSendDisabled(false);
  sessionNavigation++;
  sessionId = null;
  localStorage.removeItem(sessionStorageKey());
  clearSentAttachmentPreviewUrls();
  chatContainer.replaceChildren();
  _historyMsgs = []; _historyTurns = null; _historyHasMore = false;
  roundHistory = []; _restoredGroups = null; _restoredRefPool = null;
  _ctxData = null;
  sidebarContent.innerHTML = '<div class="empty-sidebar">提交问题后文献将在此展示</div>';
  citationCount.textContent = '0';
  renderContextPanel(null);
  // One creation attempt; do not recursively reload a replacement's history.
  await createDefaultSession(epoch, true);
  if (epoch !== authEpoch) return;
  loadSessions();
  if (window.turnRail && window.turnRail.reload) window.turnRail.reload();
}

async function ensureSession() {
  var epoch = authEpoch;
  if (!sessionId) await createDefaultSession(epoch);
  if (epoch !== authEpoch) return;
  if (sessionId) {
    userInput.focus();
    await restoreSessionHistory();
  }
  if (epoch === authEpoch) loadSessions();
}
