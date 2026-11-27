// Exchange the one-use ticket in memory, never in a URL or browser storage.
(async function () {
  const status = document.getElementById('workbench-open-status');
  const params = new URLSearchParams(window.location.search);
  const sid = params.get('session_ref') || params.get('session_id') || '';
  const input = {};
  if (/^[a-zA-Z0-9_-]{1,128}$/.test(sid)) input.session_ref = sid;
  const post = (url, value) => fetch(url, {method: 'POST', credentials: 'same-origin',
    redirect: 'error', cache: 'no-store', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
  try {
    const opened = await post('/api/internal/workbench/open', input);
    if (!opened.ok) throw new Error('open');
    const data = await opened.json();
    if (data.launch_endpoint !== '/workbench/api/v1/launch' || data.workbench_url !== '/workbench/workbench/' ||
        typeof data.launch_ticket !== 'string') throw new Error('contract');
    const launched = await post(data.launch_endpoint, {ticket: data.launch_ticket});
    if (!launched.ok) throw new Error('launch');
    window.location.replace(data.workbench_url);
  } catch (_) {
    status.textContent = '\u5de5\u4f5c\u53f0\u6682\u4e0d\u53ef\u7528\u3002\u4f60\u53ef\u4ee5\u8fd4\u56de\u4f1a\u8bdd\u6216\u5237\u65b0\u91cd\u8bd5\u3002';
  }
})();
