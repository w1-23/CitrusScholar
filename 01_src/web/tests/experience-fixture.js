// Injected only by the isolated Chromium test. No production scripts load this.
(() => {
  const sid = 'browser-fixture-session', rid = 'browser-fixture-request', job = 'browser-fixture-job';
  const otherSid = 'browser-fixture-other';
  const state = () => JSON.parse(sessionStorage.getItem('experience-test-state') || '{"phase":"queued"}');
  const save = patch => sessionStorage.setItem('experience-test-state', JSON.stringify({...state(), ...patch}));
  const has = (...phases) => phases.includes(state().phase);
  const start = () => state().start || 0;
  const samples = [['Time', 'Value', 'Notes'], [1, 42, 'north, south'], [2, 84, 'sample']];
  const csv = 'Time,Value,Notes\r\n1,42,"north, south"\r\n2,84,"two\r\nlines"\r\n';
  const file = (id, filename) => ({file_id: id, filename, kind: 'table', origin: 'uploaded', size_bytes: 128,
    download_url: '/api/v2/files/' + id + '/download', last_accessed_at: 1700000000});
  const dataFiles = [file('fixture-file-0', 'Measurements.csv'), file('workbook', 'Measurements.xlsx')];
  const svgId = 'a'.repeat(32);
  const delivered = {file_id: 'b'.repeat(32), artifact_id: 'c'.repeat(32), filename: '核验结果.csv',
    kind: 'table', origin: 'generated', source_session_id: sid, turn_request_id: rid, size_bytes: 128};
  const delivery = {status: 'verified', verified_file_ids: [delivered.file_id], unavailable_count: 0};
  window.experienceReadFailure = source => save({readFailure: source});
  window.experienceRequests = () => ({previews: state().previews || [], posts: state().posts || [], imports: state().imports || [], saves: state().saves || [],
    updates: state().updates || [], cancels: state().cancels || [], queueDeletes: state().queueDeletes || [],
    sourceReads: state().sourceReads || [], replayCursors: state().replayCursors || []});
  window.experienceActiveJob = job;
  window.experienceQueueAvailability = available => save({queueUnavailable: !available});
  window.experienceStep = phase => save({phase, start: phase === 'start' ? Date.now() / 1000 - 10 : start()});
  window.experienceScale = (turns, files) => save({turns, files});
  if (!localStorage.getItem('citrus_session_id_browser-fixture-user')) localStorage.setItem('citrus_session_id_browser-fixture-user', sid);
  const frame = (seq, event, data) => ({seq, event, data: {...data, job_id: job, request_id: rid, session_id: sid}});
  const estimate = (attempt, count, options = {}) => {
    const components = {messages: count - 1000, system: 480, tool_arguments: 70, tool_schemas: 350, images: 0, protocol: 100};
    return {snapshot_version: 1, estimate_scope: 'full_request_estimate', model: 'deepseek fixture', logical_call_id: 'call-' + attempt,
      attempt_id: 'attempt-' + attempt, estimated_input_tokens: count, components, input_limit: 86000, model_window: null,
      tool_message_count: attempt - 1, history_load_status: 'loaded', status: 'admitted', ...options};
  };
  const answer = '## 表格核对结果\n\n已读取数据。详见公开表格说明、本地表格字典和 [普通链接](https://example.org/ordinary)。\n\n字面示例 `【w1，w2】`。';
  const citations = [
    {kind: 'web', title: '公开表格说明', url: 'https://example.org/cited-table'},
    {kind: 'local', title: '本地表格字典', reader_id: 'table-dictionary'}
  ];
  const envelope = () => has('done') ? {display_status: 'completed', history_saved: true, artifact_ids: [delivered.artifact_id], artifact_delivery: delivery} : null;
  function frames() {
    if (has('queued')) return [];
    const result = [frame(1, 'job', {status: 'running'}), frame(2, 'context_snapshot', estimate(1, 9200, {change_reason: 'initial_attempt'}))];
    if (has('start')) return result;
    result.push(frame(3, 'tool_call_start', {status: 'running', display_name: '读取表格', tool_call_id: 'read-sheet'}));
    if (has('tool')) return result;
    result.push(frame(4, 'tool_result', {status: 'complete', display_name: '读取表格', tool_call_id: 'read-sheet', elapsed_ms: 1720}),
      frame(5, 'context_usage', {scope: 'single_call', logical_call_id: 'call-1', attempt_id: 'attempt-1', call: {input_tokens: 9137, output_tokens: 160, cache_read: 0, cache_uncached: 0}}));
    if (has('zero')) return result;
    result.push(frame(6, 'context_snapshot', estimate(2, 10400, {model_window: 131072, change_reason: 'tool_results_added'})),
      frame(7, 'context_usage', {scope: 'single_call', logical_call_id: 'call-2', attempt_id: 'attempt-2', call: {input_tokens: 10470, output_tokens: 900, cache_read: 2600, cache_uncached: 7800}}));
    if (has('usage')) return result;
    for (let i = 2; i <= 30; i++) {
      const tool = {display_name: '核对列 ' + i, tool_call_id: 'column-' + i};
      result.push(frame(result.length + 1, 'tool_call_start', {...tool, status: 'running'}));
      result.push(frame(result.length + 1, 'tool_result', {...tool, status: 'complete'}));
    }
    result.push(frame(result.length + 1, 'assistant_progress', {text: '核查列名', visibility: 'public', origin: 'langgraph', source: 'write_todos', status: 'in_progress', note_id: 'todo:' + 'f'.repeat(64)}));
    result.push(frame(result.length + 1, 'assistant_progress', {text: 'INTERNAL_FIXTURE_MARKER', visibility: 'private', origin: 'model', note_id: 'private-fixture'}));
    result.push(frame(result.length + 1, 'answer_start', {}));
    result.push(frame(result.length + 1, 'text', {content: '  \n', delivery: 'committed'}));
    if (has('whitespace')) return result;
    result.push(frame(result.length + 1, 'citations', {candidates: [{kind: 'web', title: '未引用的候选', url: 'https://example.org/candidate'}]}),
      frame(result.length + 2, 'citations', {display: 'bubble_only', bubbles: citations}),
      frame(result.length + 3, 'text', {content: answer, delivery: 'committed'}));
    if (has('done')) result.push(frame(result.length + 1, 'artifact_delivery', delivery));
    if (has('done')) result.push(frame(result.length + 1, 'done', {turn_envelope: envelope(), answer, turn_timings: {visible_answer_first_ms: 12100, provider_first_packet_ms: 900, queue_wait_ms: 3000}}));
    return result;
  }
  function queue() {
    return {job_id: job, root_job_id: job, session_id: sid, client_request_id: rid, active_child_job_id: '', queue_seq: 9,
      query: '请读取表格并核对列名。', state: has('queued') ? 'queued' : has('done') ? 'terminal' : 'running',
      queue_position: 3, accepted_at: (start() || Date.now() / 1000) - 30, dispatch_at: start() || null, started_at: start() || null,
      finished_at: has('done') ? start() + 31 : null};
  }
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, settings) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href), p = url.pathname;
    if (url.origin !== location.origin || !p.startsWith('/api/') && !p.startsWith('/jobs/')) return nativeFetch(input, settings);
    if (settings?.signal?.aborted || !navigator.onLine) throw new TypeError('Fixture transport offline');
    let body = {}, status = 200;
    const figureBase = '/api/v2/figure-workbench/projects/' + svgId;
    if (p.startsWith(figureBase + '/export/') || p.startsWith(figureBase + '/preview/')) return nativeFetch(input, settings);
    const original = '示例标题', title = '隔离夹具图件';
    const asSvg = value => '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="180" viewBox="0 0 360 180"><text id="fixture-title" x="20" y="40">' + value + '</text></svg>';
    const hash = async (svg) => (await import('/assets/workbench/sha256.js?v=1')).hexDigest(new TextEncoder().encode(svg));
    const rendered = async (value, revision, draftRevision = null) => {
      const renderKey = await hash(asSvg(value));
      return {figure_id: svgId, title, render_version: revision, options: {}, source_sha256: await hash(asSvg(original)),
      object_contract: {revision, draftRevision, renderKey, objects: {'fixture-title-group': {objectId: 'fixture-title-group', revision, renderKey, geometry: {svgIds: ['fixture-title']}}}},
      format_capabilities: {svg: {state: 'ready'}}, figure_size: [360, 180],
      hit_manifest: {objects: [{gid: 'fixture-title', role: 'title', bbox: {x0: 20, y0: 18, x1: 105, y1: 42}}]},
      groups: [{id: 'fixture-title-group', kind: 'title', parent: null, label: original,
        pick_ids: ['fixture-title'], properties: ['text'], values: {text: value}}]};
    };
    const saves = () => state().saves || [];
    const rendering = () => rendered(saves().at(-1)?.commands[0].value || original, 1 + saves().length);
    if (p === '/api/v2/auth/me') body = {user: {user_id: 'browser-fixture-user', username: '本地验收', role: 'user'}};
    else if (p === '/api/v2/config') body = {context: {max_tokens: 80000}};
    else if (p === '/api/v2/sessions') body = {sessions: [
      {session_id: sid, title: '表格核对', msg_count: 1, updated_at: new Date().toISOString()},
      {session_id: otherSid, title: '另一个会话', msg_count: 1, updated_at: new Date().toISOString()}]};
    else if (p === '/api/v2/skills') body = {skills: [{id: 'data-audit', version: 'fixture-v1', sha256: 'a'.repeat(64), description: '核查表格'}]};
    else if (p === '/api/v2/files') body = {retention_idle_days: 30, generated: has('done') ? [delivered, {...delivered, file_id: 'd'.repeat(32),
      artifact_id: 'e'.repeat(32), turn_request_id: 'different-turn', filename: '不属于这一轮.csv'}] : [], uploaded: Array.from({length: state().files || 0}, (_, i) =>
      i === 0 ? dataFiles[0] : file('fixture-file-' + i, '文件 ' + (i + 1) + '.csv'))};
    else if (p === dataFiles[0].download_url) return new Response(csv, {headers: {'Content-Type': 'text/csv'}});
    else if (p === dataFiles[1].download_url) {
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(samples), 'Measurements');
      return new Response(XLSX.write(workbook, {type: 'array', bookType: 'xlsx'}));
    } else if (p === '/api/v2/figure-workbench/projects') {
      const projects = [{figure_id: svgId, render_version: 1 + saves().length, title},
        ...Array.from({length: 25}, (_, i) => ({figure_id: (i + 1).toString(16).padStart(32, '0'), render_version: 1, title: '历史图件 ' + i}))];
      const offset = +(url.searchParams.get('offset') || 0), limit = +(url.searchParams.get('limit') || 12);
      body = {projects: projects.slice(offset, offset + limit), has_more: offset + limit < projects.length};
    } else if (p === figureBase) {
      body = await rendering();
    } else if (p === figureBase + '/preview' && settings?.method === 'POST') {
      const edit = JSON.parse(settings.body);
      save({previews: [...(state().previews || []), edit]});
      body = {...await rendered(edit.commands[0].value, edit.expected_revision, edit.draft_revision), draft_revision: edit.draft_revision, svg: asSvg(edit.commands[0].value)};
    } else if (p === figureBase + '/save' && settings?.method === 'POST') {
      save({saves: [...saves(), JSON.parse(settings.body)]});
      body = await rendering();
    } else if (p.endsWith('/messages')) {
      const history = state().turns == null && has('done') && p.includes('/' + sid + '/') ? [{
        turn_request_id: rid, job_id: job, job_status: 'completed', display_status: 'completed',
        terminal_snapshot: envelope(), user: {content: queue().query},
        answer: {content: answer, citation_bubbles: citations}, files: [delivered],
      }] : [];
      body = {messages: [], turns: [...history, ...Array.from({length: state().turns || 0}, (_, i) => ({
        turn_request_id: 'saved-' + i, user: {content: '历史问题 ' + i}, answer: {content: '历史回答 ' + i}, display_status: 'completed',
      }))], has_more: false};
    }
    else if (p.startsWith('/api/v2/session/' + otherSid + '/')) body = {turns: []};
    else if (p === '/api/v2/chat/jobs/' + job + '/cancel' && settings?.method === 'POST') {
      save({cancels: [...(state().cancels || []), job]});
      body = {accepted: true}; // Task observation, not this receipt, confirms terminal state.
    } else if (p.includes('/updates')) {
      save({updates: [...(state().updates || []), {path: p, method: settings?.method || 'GET'}]});
      status = 404;
    } else if (p.includes('/turn-queue/') && settings?.method === 'DELETE') {
      save({queueDeletes: [...(state().queueDeletes || []), p]});
      status = 405;
    } else if (p.endsWith('/turn-sources')) {
      const url = 'https://example.org/candidate';
      save({sourceReads: [...(state().sourceReads || []), p]});
      body = {sources: [{kind: 'web', title: '未引用的候选', url}]};
    } else if (p.endsWith('/turn-queue')) {
      if (state().queueUnavailable && settings?.method !== 'POST') return new Response('{}', {status: 503});
      let admissions = state().posts || [];
      if (settings?.method === 'POST') {
        admissions = [...admissions, JSON.parse(settings.body)];
        save({posts: admissions});
      }
      const entries = admissions.map((entry, i) => ({...entry, session_id: sid, job_id: 'new-job-' + i, queue_seq: i + 10,
        queue_position: i + 1, state: 'queued', accepted_at: Date.now() / 1000}));
      body = settings?.method === 'POST' ? entries.at(-1) : {turns: [queue(), ...entries], next_after_seq: has('done') ? 9 : null};
    }
    else if (p === '/jobs/' + job || p === '/jobs/' + job + '/events') {
      const replay = p.endsWith('/events');
      if (state().readFailure === (replay ? 'replay' : 'status')) return new Response('{}', {status: 503});
      const events = frames(), after = +(url.searchParams.get('after') || 0);
      if (replay) save({replayCursors: [...(state().replayCursors || []), after]});
      body = {job_id: job, protocol: 'citrus-task/1', status: has('done') ? 'completed' : 'running',
        terminal_snapshot: envelope(), context_snapshot: events.filter(x => x.event === 'context_snapshot').at(-1)?.data,
        event_cursor: events.length, next_cursor: events.length, has_more: false, events: events.filter(x => x.seq > after)};
      if (body.context_snapshot) body.context_snapshot = {...body.context_snapshot, seq: events.filter(x => x.event === 'context_snapshot').at(-1).seq};
    }
    return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
  };
})();
