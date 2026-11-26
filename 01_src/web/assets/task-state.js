(function (root) {
  'use strict';
  var terminal = ['completed', 'partial', 'failed', 'cancelled'];
  function displayStatus(data) {
    var envelope = data && (data.turn_envelope || data.terminal_snapshot);
    var status = String(envelope && envelope.display_status || data && (data.display_status || data.turn_status) || '');
    return terminal.indexOf(status) >= 0 ? status : '';
  }
  function snapshotStatus(job) {
    return displayStatus(job) || (job && terminal.indexOf(job.status) >= 0 ? job.status : '');
  }
  function list(value) { return Array.isArray(value) ? value : []; }
  function nativeProgress(data) {
    if (!data || data.origin !== 'langgraph' || data.source !== 'write_todos' || data.visibility !== 'public' ||
        data.internal === true || data.private === true || data.is_draft === true || data.draft === true ||
        typeof data.job_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(data.job_id) ||
        typeof data.note_id !== 'string' || !/^todo:[a-f0-9]{64}$/.test(data.note_id)) return false;
    if (data.status === 'inactive') return !Object.prototype.hasOwnProperty.call(data, 'text');
    return data.status === 'in_progress' && typeof data.text === 'string' && data.text.length >= 2 &&
      data.text.length <= 80 && /^[\p{L}\p{M} -]+$/u.test(data.text);
  }
  // F-14: native LangGraph stream projection. The server maps native model,
  // tool, sub-agent and interrupt facts onto the existing ``assistant_progress``
  // event with a stable ``kind`` and ``schema_version``. Anything unknown is
  // ignored, so an older or newer client never breaks on a newer kind.
  var NATIVE_SCHEMA_VERSION = 1;
  var NATIVE_SOURCE = 'native_stream';
  var NATIVE_STATUS = {model_preview: 'streaming', tool_proposed: 'proposed',
    subagent_started: 'started', subagent_completed: 'completed',
    subagent_failed: 'failed', interrupt: 'requested', resume: 'resumed'};
  var NATIVE_KEYS = {
    model_preview: ['call_id', 'delta_index', 'text'],
    tool_proposed: ['tool_call_id', 'tool_name'],
    subagent_started: ['subagent_id', 'subagent_depth'],
    subagent_completed: ['subagent_id', 'reason_code'],
    subagent_failed: ['subagent_id', 'reason_code'],
    interrupt: ['interrupt_id'],
    resume: ['reason_code']
  };
  var NATIVE_BASE_KEYS = ['origin', 'source', 'visibility', 'schema_version', 'job_id',
    'run_id', 'status', 'kind', 'at_ms', 'note_id'];
  var ID = /^[A-Za-z0-9_-]{1,128}$/;
  function isObject(value) { return !!value && typeof value === 'object' && !Array.isArray(value); }
  function nativeStreamEvent(data) {
    if (!isObject(data) || data.origin !== 'langgraph' || data.source !== NATIVE_SOURCE ||
        data.visibility !== 'public' || data.internal === true || data.private === true) return false;
    if (data.schema_version !== NATIVE_SCHEMA_VERSION) return false;
    var kind = data.kind;
    if (typeof kind !== 'string' || !Object.prototype.hasOwnProperty.call(NATIVE_STATUS, kind)) return false;
    if (data.status !== NATIVE_STATUS[kind]) return false;
    var allowed = NATIVE_BASE_KEYS.concat(NATIVE_KEYS[kind]);
    var keys = Object.keys(data).filter(function (key) { return key !== 'seq'; });
    if (keys.some(function (key) { return allowed.indexOf(key) < 0; })) return false;
    if (typeof data.job_id !== 'string' || !ID.test(data.job_id) ||
        typeof data.run_id !== 'string' || !ID.test(data.run_id)) return false;
    if (typeof data.note_id !== 'string' || !/^stream:[a-f0-9]{64}$/.test(data.note_id)) return false;
    if (!Number.isSafeInteger(data.at_ms) || data.at_ms <= 0) return false;
    if (kind === 'model_preview') {
      if (typeof data.call_id !== 'string' || !/^mdl:[a-f0-9]{16}$/.test(data.call_id)) return false;
      if (!Number.isSafeInteger(data.delta_index) || data.delta_index < 0) return false;
      if (typeof data.text !== 'string' || !data.text.trim() || data.text.length > 400) return false;
      return true;
    }
    if (kind === 'tool_proposed') {
      return typeof data.tool_call_id === 'string' && ID.test(data.tool_call_id) &&
        typeof data.tool_name === 'string' && ID.test(data.tool_name);
    }
    if (kind === 'subagent_started') {
      return typeof data.subagent_id === 'string' && /^sa:[a-f0-9]{16}$/.test(data.subagent_id) &&
        Number.isSafeInteger(data.subagent_depth) && data.subagent_depth >= 1 && data.subagent_depth <= 8;
    }
    if (kind === 'subagent_completed' || kind === 'subagent_failed') {
      return typeof data.subagent_id === 'string' && /^sa:[a-f0-9]{16}$/.test(data.subagent_id) &&
        typeof data.reason_code === 'string';
    }
    if (kind === 'interrupt') {
      return data.interrupt_id === undefined ||
        (typeof data.interrupt_id === 'string' && ID.test(data.interrupt_id));
    }
    return typeof data.reason_code === 'string';
  }
  // A pure projection: replay, live SSE and a reconnected cursor all feed the
  // same reducer, so a recovered page can never duplicate a shown operation.
  function createNativeStream(options) {
    options = options || {};
    var maxPreview = 4000, maxTools = 64, maxSubagents = 32;
    var state = {schema_version: NATIVE_SCHEMA_VERSION, job_id: '', run_id: '',
      preview: null, tools: [], subagents: [], interrupt: null, resume: null,
      closed: false, committed: false, unrecorded: 0};
    function sameJob(data) {
      if (!state.job_id && data.job_id) { state.job_id = data.job_id; state.run_id = data.run_id; }
      return !state.job_id || data.job_id === state.job_id;
    }
    function find(list, key, value) {
      for (var i = 0; i < list.length; i++) if (list[i][key] === value) return i;
      return -1;
    }
    function preview(data) {
      var current = state.preview;
      if (current && current.call_id === data.call_id && data.delta_index <= current.delta_index) return false;
      if (current && current.call_id !== data.call_id) current = null;
      var text = (current ? current.text : '') + data.text;
      if (text.length > maxPreview) text = text.slice(text.length - maxPreview);
      state.preview = {call_id: data.call_id, delta_index: data.delta_index, text: text,
        seq: Number.isSafeInteger(data.seq) ? data.seq : 0, status: 'streaming'};
      return true;
    }
    function tool(data) {
      var index = find(state.tools, 'tool_call_id', data.tool_call_id);
      if (index >= 0) {
        var previous = state.tools[index];
        if (Number.isSafeInteger(data.seq) && Number.isSafeInteger(previous.seq) && data.seq <= previous.seq) return false;
        state.tools[index] = {tool_call_id: data.tool_call_id, tool_name: data.tool_name,
          seq: data.seq, status: 'proposed'};
        return true;
      }
      state.tools.push({tool_call_id: data.tool_call_id, tool_name: data.tool_name,
        seq: Number.isSafeInteger(data.seq) ? data.seq : 0, status: 'proposed'});
      if (state.tools.length > maxTools) state.tools.splice(0, state.tools.length - maxTools);
      return true;
    }
    function subagent(data, status) {
      var index = find(state.subagents, 'subagent_id', data.subagent_id);
      if (index < 0) {
        // A terminal state for an unseen sub-agent is not a start: report the
        // settled fact without inventing a start time or a parent.
        state.subagents.push({subagent_id: data.subagent_id, depth: data.subagent_depth || 0,
          status: status, reason_code: data.reason_code || '',
          started_seq: status === 'active' ? data.seq : 0, settled_seq: status === 'active' ? 0 : data.seq});
        if (state.subagents.length > maxSubagents) state.subagents.splice(0, state.subagents.length - maxSubagents);
        return true;
      }
      var item = state.subagents[index];
      if (item.status !== 'active') return false;
      if (Number.isSafeInteger(data.seq) && Number.isSafeInteger(item.started_seq) && data.seq <= item.started_seq) return false;
      item.status = status;
      item.reason_code = data.reason_code || '';
      item.settled_seq = data.seq || 0;
      return true;
    }
    function receive(type, data) {
      if (state.closed || !nativeStreamEvent(data) || !sameJob(data)) return false;
      // Once the delivered answer owns the text surface, a late token can never
      // resurrect a provisional preview.
      if (state.committed && data.kind === 'model_preview') return false;
      var changed = false;
      switch (data.kind) {
        case 'model_preview': changed = preview(data); break;
        case 'tool_proposed': changed = tool(data); break;
        case 'subagent_started': changed = subagent(data, 'active'); break;
        case 'subagent_completed': changed = subagent(data, 'completed'); break;
        case 'subagent_failed': changed = subagent(data, 'failed'); break;
        case 'interrupt':
          state.interrupt = {interrupt_id: data.interrupt_id || '', seq: data.seq || 0, status: 'requested'};
          changed = true; break;
        case 'resume':
          state.resume = {reason_code: data.reason_code, seq: data.seq || 0};
          changed = true; break;
        default: return false;
      }
      state.unrecorded = countUnrecorded();
      return changed;
    }
    // "Unrecorded" is a claim about endings only: work that never reported a
    // terminal fact after the turn closed. Pending work during a live turn is
    // not counted, because it simply has not finished yet.
    function countUnrecorded() {
      var count = 0;
      state.subagents.forEach(function (item) { if (item.status === 'unrecorded') count++; });
      state.tools.forEach(function (item) { if (item.status === 'unrecorded') count++; });
      return count;
    }
    // The committed answer always owns the answer surface: a preview is a
    // provisional reading, never a second copy of the delivered text.
    function commit() {
      var changed = !!state.preview || !state.committed;
      state.preview = null;
      state.committed = true;
      return changed;
    }
    // A recovered turn may discard the preview without owning the answer yet.
    function discard() {
      var changed = !!state.preview;
      state.preview = null;
      return changed;
    }
    function reset() {
      state.preview = null; state.tools = []; state.subagents = []; state.interrupt = null;
      state.resume = null; state.closed = false; state.committed = false; state.unrecorded = 0;
      if (options.job_id) state.job_id = String(options.job_id);
    }
    function terminal() {
      if (state.closed) return false;
      state.closed = true;
      state.preview = null;
      state.tools.forEach(function (item) { if (item.status === 'proposed') item.status = 'unrecorded'; });
      state.subagents.forEach(function (item) { if (item.status === 'active') item.status = 'unrecorded'; });
      state.unrecorded = countUnrecorded();
      return true;
    }
    function view() {
      return {schema_version: state.schema_version, job_id: state.job_id, run_id: state.run_id,
        preview: state.preview, tools: state.tools.slice(), subagents: state.subagents.slice(),
        interrupt: state.interrupt, resume: state.resume, closed: state.closed,
        committed: state.committed, unrecorded: state.unrecorded};
    }
    return {receive: receive, view: view, commit: commit, discard: discard,
      terminal: terminal, reset: reset, state: state};
  }
  // Artifact delivery is an explicit terminal-tail fact: which verified files
  // the turn actually delivered, and how many expected files are unavailable.
  // Unknown status values stay truthful; file ids are never guessed.
  function normalizeDelivery(data) {
    if (!data || typeof data !== 'object') return null;
    var status = String(data.status || '').trim();
    if (!status) return null;
    var seen = new Set(), ids = [];
    list(data.verified_file_ids).forEach(function (id) {
      var text = String(id == null ? '' : id).trim();
      if (!text || seen.has(text)) return;
      seen.add(text); ids.push(text);
    });
    var unavailable = Number(data.unavailable_count);
    unavailable = Number.isFinite(unavailable) && unavailable > 0 ? Math.floor(unavailable) : 0;
    return {status: status, verified_file_ids: ids, unavailable_count: unavailable,
      seq: Number.isSafeInteger(data.seq) ? data.seq : 0};
  }
  // Event arrival is ordered separately from the public process projection.
  // All aliases identify one logical operation; attempts remain separate facts.
  function createProcess() {
    var entries = [], actions = [], seen = new Set(), nextId = 0, currentPlan = null, resolutions = new Map(), generation = "";
    function resolution(data) {
      var changed = false;
      list(data && data.resolutions).forEach(function (value) {
        if (!value || ['repaired', 'source_replaced', 'superseded'].indexOf(value.resolution) < 0 ||
            typeof value.tool_call_id !== 'string' || !value.tool_call_id.trim() ||
            typeof value.resolved_by_tool_call_id !== 'string' || !value.resolved_by_tool_call_id.trim() ||
            value.tool_call_id === value.resolved_by_tool_call_id) return;
        var previous = resolutions.get(value.tool_call_id);
        if (previous && Number.isSafeInteger(previous.seq) && Number.isSafeInteger(data.seq) && data.seq <= previous.seq) return;
        var record = {tool_call_id: value.tool_call_id, resolved_by_tool_call_id: value.resolved_by_tool_call_id,
          resolution: value.resolution, seq: data.seq};
        resolutions.set(value.tool_call_id, record);
        actions.forEach(function (item) { if (item.generation === generation && item.data.tool_call_id === record.tool_call_id) item.resolution = record; });
        changed = true;
      });
      return changed;
    }
    function note(data) {
      if (!nativeProgress(data) || data.status !== 'in_progress' || seen.has(data.note_id)) return null;
      seen.add(data.note_id);
      var item = {kind: 'note', id: ++nextId, text: data.text, generation: generation, note_id: data.note_id};
      entries.push(item);
      return item;
    }
    function plan(data) {
      if (!data || !data.plan_id || !Array.isArray(data.steps)) return false;
      var statuses = ['pending', 'in_progress', 'completed', 'blocked', 'cancelled'];
      var ids = new Set();
      if (data.steps.some(function (step) {
        if (!step || !step.id || statuses.indexOf(step.status) < 0 || ids.has(String(step.id))) return true;
        ids.add(String(step.id)); return false;
      })) return false;
      if (currentPlan) {
        if (String(data.plan_id) !== currentPlan.plan_id) return false;
        if (Number.isSafeInteger(data.seq) && Number.isSafeInteger(currentPlan.seq) && data.seq <= currentPlan.seq) return false;
        if (Number.isSafeInteger(data.version) && Number.isSafeInteger(currentPlan.version) && data.version < currentPlan.version) return false;
      }
      currentPlan = {plan_id: String(data.plan_id), seq: data.seq, version: data.version,
        steps: data.steps.map(function (step) { return {id: String(step.id), status: step.status}; })};
      return true;
    }
    function action(data) {
      var keys = [data.logical_key, data.operation_id, data.tool_call_id].concat(list(data.aliases)).filter(Boolean).map(String);
      var attempt = String(data.attempt || data.attempts || 1);
      var matches = actions.filter(function (item) { return item.generation === generation && item.attempt === attempt && keys.some(function (key) { return item.aliases.indexOf(key) >= 0; }); });
      var item = matches[0];
      if (!item) {
        item = {kind: 'action', id: ++nextId, generation: generation, attempt: attempt, aliases: [], data: {}};
        entries.push(item); actions.push(item);
      }
      // A later event can bridge a tool_call_id and operation_id already seen.
      matches.slice(1).forEach(function (duplicate) {
        item.data = Object.assign({}, duplicate.data, item.data);
        item.aliases = item.aliases.concat(duplicate.aliases);
        entries.splice(entries.indexOf(duplicate), 1); actions.splice(actions.indexOf(duplicate), 1);
      });
      keys.forEach(function (key) { if (item.aliases.indexOf(key) < 0) item.aliases.push(key); });
      var incoming = Object.assign({}, data);
      // An alias start arriving after a result must not erase its outcome.
      if (['running', 'resuming'].indexOf(incoming.status) >= 0 && item.data.status && ['running', 'resuming'].indexOf(item.data.status) < 0) delete incoming.status;
      item.data = Object.assign({}, item.data, incoming);
      item.resolution = resolutions.get(item.data.tool_call_id) || null;
      return item;
    }
    function begin(key) {
      if (!key || key === generation) return false;
      generation = key; currentPlan = null; resolutions.clear();
      actions.forEach(function (item) {
        if (['running', 'resuming'].indexOf(item.data.status) >= 0) item.data.status = 'interrupted';
      });
      return true;
    }
    return {begin: begin, entries: entries, note: note, action: action, plan: plan, resolution: resolution, currentPlan: function () { return currentPlan; }};
  }
  // Presentation only: queue and job state remain authoritative. Time starts at
  // the durable execution stamp, never at submission or observer attachment.
  function waitingView(state, entry, ui) {
    var status = state.status, phase = entry && entry.state;
    var terminalStatus = state.display_status || (terminal.indexOf(status) >= 0 ? status : '');
    var jobMatches = !entry || state.job_id === (entry.active_child_job_id || entry.job_id);
    if (entry && !jobMatches) terminalStatus = '';
    var start = jobMatches && state.started_at || (entry && !entry.active_child_job_id && !state.recovery_cursor ? entry.started_at : 0);
    var execution = jobMatches && (status === 'running' || status === 'detached') || phase === 'running' && Number(start) > 0;
    var result = {kind: '', start: 0, finish: 0};
    if (phase === 'queued' || phase === 'claimed' || phase === 'reconcile_required') return result;
    if (terminalStatus || phase === 'terminal') {
      result.kind = terminalStatus || (entry.clear_reason === 'cancelled' ? 'cancelled' : entry.clear_reason ? 'failed' : '');
      result.start = Number(start) || 0;
      result.finish = Number(jobMatches && state.finished_at || entry && entry.finished_at) || 0;
    } else if (ui.stopping || jobMatches && status === 'cancel_requested') result.kind = 'stopping';
    else if (phase === 'awaiting_input' || jobMatches && (status === 'awaiting_input' || state.question && state.question.status === 'pending' || list(state.approvals).some(function (item) { return item.status === 'pending'; }))) result.kind = 'awaiting_input';
    else if (phase === 'settling' || jobMatches && status === 'settling') result.kind = 'settling';
    else if (ui.disconnected) result.kind = 'disconnected';
    else if (execution && !ui.hasAnswer) {
      result.kind = ui.activeTool ? 'tool' : 'waiting';
      result.start = Number(start) || 0;
    }
    return result;
  }
  function newerQuestion(previous, incoming) {
    if (!incoming || !incoming.id) return previous;
    if (previous && previous.id === incoming.id) {
      if (Number(incoming.version || 0) < Number(previous.version || 0)) return previous;
      if (Number(incoming.version || 0) === Number(previous.version || 0) && incoming.status === 'pending' &&
          (previous.status && previous.status !== 'pending' || previous.interrupt_id && !incoming.interrupt_id)) return previous;
    }
    return Object.assign({}, incoming);
  }
  // One projection for live events, replay and persisted job snapshots. Usage
  // can attach only to the exact logical-call/attempt pair that produced it.
  function createContext(options) {
    options = options || {};
    var records = new Map(), latest = '', legacy = null, legacySeq = 0, loadStatus = 'unknown', loadSeq = 0, view = null;
    function changed() { view = null; return current(); }
    function selectLatest() {
      var admitted = new Set(), selected;
      latest = '';
      records.forEach(function (record) {
        if (record.snapshot && record.snapshot.attempt_id) admitted.add(record.snapshot.logical_call_id);
      });
      records.forEach(function (record, key) {
        var snapshot = record.snapshot;
        if (!snapshot || !snapshot.attempt_id && admitted.has(snapshot.logical_call_id)) return;
        if (!selected || record.firstSeq >= selected.firstSeq) {
          selected = record; latest = key;
        }
      });
    }
    function trim() {
      if (records.size > 64) {
        var oldest = Array.from(records.keys()).find(function (id) { return id !== latest; });
        if (oldest) records.delete(oldest);
      }
    }
    function identity(data) {
      if (!data.logical_call_id) return '';
      return data.attempt_id ? String(data.logical_call_id) + '/' + String(data.attempt_id)
        : data.snapshot_version ? String(data.logical_call_id) + '/not_admitted' : '';
    }
    function receive(type, data) {
      data = data || {};
      // Reject foreign context before caching, not only before rendering. A
      // later valid event must never reveal a previously rejected snapshot.
      if (options.request_id && data.request_id && data.request_id !== options.request_id ||
          options.session_id && data.session_id && data.session_id !== options.session_id) return current();
      if (data.history_load_status && (data.seq || 0) >= loadSeq) {
        loadStatus = data.history_load_status; loadSeq = data.seq || 0;
      }
      if (type === 'context_usage') {
        var usageKey = identity(data), record = records.get(usageKey);
        if (!usageKey || !data.attempt_id || !data.call || data.scope !== 'single_call') return current();
        // Do not select a call just because its response arrived last.
        if (!record) { record = {}; records.set(usageKey, record); }
        // A later snapshot without counters does not supersede reported usage.
        if (record.usageSeq && (!data.seq || data.seq <= record.usageSeq)) return current();
        record.usageSeq = data.seq || 0;
        record.usage = Object.assign({}, data.call, {logical_call_id: data.logical_call_id, attempt_id: data.attempt_id, scope: 'single_call'});
        if (record.snapshot) record.snapshot = Object.assign({}, record.snapshot, {provider_usage: record.usage, usage_status: 'reported'});
        trim();
        return changed();
      }
      if (['context_snapshot', 'context_budget', 'context_status'].indexOf(type) < 0) return current();
      var key = identity(data);
      if (!key) {
        // Old rows can be displayed as historical estimates, never relabelled
        // as the current complete model request or joined to unrelated usage.
        if (legacySeq && (data.seq || 0) < legacySeq) return current();
        legacySeq = data.seq || 0;
        legacy = Object.assign({}, data, {estimate_scope: 'historical_estimate',
          estimate_label: '历史估算（缺少调用身份）', provider_usage: {}, usage_status: 'unknown',
          estimated_tokens: data.estimated_input_tokens != null ? data.estimated_input_tokens : data.estimated_tokens,
          model_window: data.model_window || null, history_load_status: data.history_load_status || loadStatus});
        return changed();
      }
      var previous = records.get(key);
      if (previous && previous.snapshot && previous.snapshot.seq && (!data.seq || previous.snapshot.seq > data.seq)) {
        if (data.seq && data.seq < previous.firstSeq) {
          previous.firstSeq = data.seq; selectLatest(); return changed();
        }
        return current();
      }
      // Replayed starts cannot erase a settled snapshot hydrated from storage.
      if (previous && previous.snapshot && ['complete', 'failed', 'cancelled'].indexOf(previous.snapshot.status) >= 0 &&
          ['estimated', 'running', 'admitted'].indexOf(data.status) >= 0) return current();
      var snapshot = Object.assign({}, data, {
        estimated_tokens: data.estimated_input_tokens != null ? data.estimated_input_tokens : data.estimated_tokens,
        model_window: data.model_window || null, history_load_status: data.history_load_status || loadStatus
      });
      var usage = data.provider_usage || data.usage;
      var usageSeq = previous && previous.usageSeq || 0;
      if (data.attempt_id && usage && usage.logical_call_id === data.logical_call_id && usage.attempt_id === data.attempt_id &&
          (data.seq || 0) >= usageSeq) {
        snapshot.provider_usage = Object.assign({}, usage);
        usageSeq = data.seq || 0;
      } else {
        snapshot.provider_usage = previous && previous.usage || {};
        if (Object.keys(snapshot.provider_usage).length) snapshot.usage_status = 'reported';
      }
      var first = previous && previous.firstSeq;
      records.set(key, {snapshot: snapshot, usage: snapshot.provider_usage, usageSeq: usageSeq,
        firstSeq: first && data.seq ? Math.min(first, data.seq) : first || data.seq || 0});
      selectLatest(); trim();
      return changed();
    }
    function current() {
      if (view) return view;
      var record = records.get(latest);
      if (!record || !record.snapshot) return legacy;
      var attempts = Array.from(records.values()).filter(function (row) { return row.snapshot && row.snapshot.attempt_id; })
        .sort(function (a, b) { return a.firstSeq - b.firstSeq; }).map(function (row) {
          var item = row.snapshot;
          return {logical_call_id: item.logical_call_id, attempt_id: item.attempt_id, status: item.status,
            model: item.model, change_reason: item.change_reason, estimated_tokens: item.estimated_tokens, seq: row.firstSeq};
        });
      view = Object.assign({}, record.snapshot, {observed_attempts: attempts});
      return view;
    }
    return {receive: receive, current: current};
  }
  function create(options) {
    options = options || {};
    var state = {job_id: '', cursor: 0, task_version: 0, applied_version: 0, status: '', started_at: 0, finished_at: 0,
      display_status: '', terminal_snapshot: null, question: null, pending_updates: [], not_applied_updates: [], operations: [], approvals: [],
      artifact_delivery: null, recovery_cursor: 0, native_stream: null};
    var pending = new Map(), subscribers = [], recoveryFloor = 0, context = createContext(options);
    var nativeStream = createNativeStream(options);
    state.context_snapshot = null;
    state.native_stream = nativeStream.view();
    function notify() { subscribers.slice().forEach(function (fn) { fn(state); }); }
    // Ordered events own the newest delivery. A snapshot without a sequence
    // (terminal_snapshot / hydrated job) fills only when no ordered fact exists.
    function applyDelivery(data) {
      var next = normalizeDelivery(data), previous = state.artifact_delivery;
      if (!next) return false;
      if (previous) {
        if (next.seq && previous.seq && next.seq < previous.seq) return false;
        if (next.seq && next.seq === previous.seq) return false;
        if (!next.seq && previous.seq) return false;
        if (!next.seq && !previous.seq && next.status === previous.status &&
            next.unavailable_count === previous.unavailable_count &&
            next.verified_file_ids.join('\u0000') === previous.verified_file_ids.join('\u0000')) return false;
      }
      state.artifact_delivery = next;
      return true;
    }
    // A terminal or delivery fact must belong to the exact originating request.
    // The initial job frame may name the server-side request id; its client id
    // is authoritative when present.
    function foreignRequest(type, data) {
      if (!options.request_id || !data || (!data.request_id && !data.client_request_id)) return false;
      if (data.request_id === options.request_id) return false;
      if (data.client_request_id === options.request_id) return false;
      // A durable event naming this exact job is already bound to the turn;
      // job identity is the stronger, unambiguous origin.
      if (data.job_id && state.job_id && data.job_id === state.job_id) return false;
      if (type === 'job') return !!data.client_request_id;
      return true;
    }
    function recordTimes(data) {
      // The execution clock accepts epoch seconds. Legacy local ISO strings
      // do not establish a timezone or an execution start.
      ['started_at', 'finished_at'].forEach(function (key) {
        if (Number.isFinite(data[key]) && data[key] > 0) state[key] = data[key];
      });
    }
    function hydrate(job) {
      if (!job || job.job_id && state.job_id && job.job_id !== state.job_id) return false;
      // A durable snapshot is bound by job identity. A continuation child
      // legitimately has a client request id different from the root turn, so
      // request scoping applies to events (foreignRequest), not snapshots.
      if (Number.isSafeInteger(job.event_cursor) && job.event_cursor < state.cursor) return false;
      if (Number.isSafeInteger(job.task_version) && job.task_version < state.task_version) return false;
      if (Number.isSafeInteger(job.recovery_cursor) && job.recovery_cursor < state.recovery_cursor) return false;
      if (job.job_id) state.job_id = String(job.job_id);
      recordTimes(job);
      ['task_version', 'applied_version'].forEach(function (key) {
        if (Number.isSafeInteger(job[key]) && job[key] >= state[key]) state[key] = job[key];
      });
      ['pending_updates', 'operations', 'approvals'].forEach(function (key) {
        if (Array.isArray(job[key])) state[key] = job[key].slice();
        if (key === 'pending_updates') state.pending_updates = state.pending_updates.filter(function (item) {
          return item.version > state.applied_version && !state.not_applied_updates.some(function (update) { return update.version === item.version; });
        });
      });
      var savedContext = job.context_snapshot || (job.terminal_snapshot || {}).context_snapshot;
      if (savedContext) state.context_snapshot = context.receive('context_snapshot', savedContext);
      // The terminal snapshot is the authoritative tail when the delivery event
      // itself is no longer in the page window. Its sequence is the job cursor,
      // so a stale page can never replace a newer ordered event.
      var savedDelivery = job.artifact_delivery || (job.terminal_snapshot || {}).artifact_delivery
        || (job.turn_envelope || {}).artifact_delivery;
      if (savedDelivery) applyDelivery(Object.assign({}, savedDelivery, {
        seq: Number.isSafeInteger(job.event_cursor) ? job.event_cursor : (savedDelivery.seq || 0)}));
      if (job.question) state.question = newerQuestion(state.question, job.question);
      if (job.status && !state.display_status) state.status = String(job.status);
      var settled = snapshotStatus(job);
      if (settled) {
        state.terminal_snapshot = displayStatus(job) ? job.terminal_snapshot || null : null;
        state.display_status = settled;
        state.status = settled;
      }
      notify(); return true;
    }
    function reduce(type, data) {
      if (type === 'artifact_delivery') applyDelivery(data);
      // F-14: native stream facts share the ordered event stream, so they are
      // reduced after sequencing and before any presentation. The committed
      // answer always clears a provisional preview.
      if (type === 'assistant_progress') nativeStream.receive(type, data);
      else if (type === 'text' && data.delivery === 'committed') nativeStream.commit();
      else if (type === 'text_reset') nativeStream.discard();
      else if (type === 'done' || type === 'error') nativeStream.terminal();
      state.native_stream = nativeStream.view();
      if (type === 'artifact_delivery') applyDelivery(data);
      if (data.artifact_delivery) applyDelivery(Object.assign({}, data.artifact_delivery,
        {seq: Number.isSafeInteger(data.seq) ? data.seq : (data.artifact_delivery.seq || 0)}));
      var envelope = data.turn_envelope || data.terminal_snapshot;
      if (envelope && envelope.artifact_delivery) applyDelivery(Object.assign({}, envelope.artifact_delivery,
        {seq: Number.isSafeInteger(data.seq) ? data.seq : (envelope.artifact_delivery.seq || 0)}));
      if (['context_snapshot', 'context_status', 'context_budget', 'context_usage'].indexOf(type) >= 0) {
        state.context_snapshot = context.receive(type, data);
      }
      if (data.context_snapshot) state.context_snapshot = context.receive('context_snapshot', data.context_snapshot);
      if (data.question) state.question = newerQuestion(state.question, data.question);
      if (data.turn_envelope && data.turn_envelope.display_status === 'awaiting_input' && !state.display_status) state.status = 'awaiting_input';
      if (data.job_id) state.job_id = String(data.job_id);
      recordTimes(data);
      ['task_version', 'applied_version'].forEach(function (key) {
        if (Number.isSafeInteger(data[key])) state[key] = Math.max(state[key], data[key]);
      });
      if (type === 'job') {
        if (data.job_id && nativeStream.state.job_id && data.job_id !== nativeStream.state.job_id) {
          nativeStream.reset(); state.native_stream = nativeStream.view();
        }
        state.status = String(data.status || 'running');
      }
      if (type === 'agent_recovery' && data.status === 'admitted' && Number.isSafeInteger(data.seq)) {
        state.recovery_cursor = data.seq;
        state.status = 'running'; state.display_status = ''; state.terminal_snapshot = null;
        state.question = null; state.approvals = []; state.operations = []; state.artifact_delivery = null;
        state.started_at = 0; state.finished_at = 0; recordTimes(data);
        context = createContext(options); state.context_snapshot = null;
        nativeStream.reset(); state.native_stream = nativeStream.view();
        var reopened = Array.isArray(data.reactivated_updates) ? data.reactivated_updates.filter(function (item) {
          return Number.isSafeInteger(item.version) && item.version > state.applied_version;
        }) : [];
        state.not_applied_updates = state.not_applied_updates.filter(function (item) {
          return !reopened.some(function (update) { return update.version === item.version; });
        });
        reopened.forEach(function (item) {
          state.pending_updates = state.pending_updates.filter(function (update) { return update.version !== item.version; });
          state.pending_updates.push(Object.assign({}, item, {status: 'pending'}));
        });
      }
      if (type === 'task_update' && Number.isSafeInteger(data.version)) {
        state.task_version = Math.max(state.task_version, data.version);
        state.pending_updates = state.pending_updates.filter(function (item) { return item.version !== data.version; });
        if (data.status === 'pending' && data.version > state.applied_version && !state.not_applied_updates.some(function (item) { return item.version === data.version; })) state.pending_updates.push(data);
        if (data.status === 'not_applied') {
          state.not_applied_updates = state.not_applied_updates.filter(function (item) { return item.version !== data.version; });
          state.not_applied_updates.push(data);
        }
        if (data.status === 'applied') {
          state.applied_version = Math.max(state.applied_version, data.version);
          state.pending_updates = state.pending_updates.filter(function (item) { return item.version > state.applied_version; });
        }
      }
      if (type === 'operation' && data.operation_id) {
        var previous = state.operations.find(function (item) { return item.operation_id === data.operation_id; });
        state.operations = state.operations.filter(function (item) { return item.operation_id !== data.operation_id; });
        state.operations.push(Object.assign({}, previous || {}, data));
      }
      if (type === 'permission_request' && data.approval_id) {
        state.approvals = state.approvals.filter(function (item) { return item.approval_id !== data.approval_id; });
        state.approvals.push(Object.assign({status: 'pending'}, data));
      }
      if (type === 'approval_decided' || type === 'permission_decision') {
        state.approvals = state.approvals.map(function (item) {
          return item.approval_id === data.approval_id ? Object.assign({}, item, data) : item;
        });
      }
      if (type === 'request_user_input' || type === 'question' || type === 'question_answered' || type === 'question_cancelled') {
        state.question = newerQuestion(state.question, data.question || data);
        if (state.question && state.question.status === 'pending' && !state.display_status) state.status = 'awaiting_input';
      }
      if ((type === 'done' || type === 'error') && displayStatus(data)) {
        state.display_status = displayStatus(data);
        state.terminal_snapshot = data.turn_envelope || data.terminal_snapshot || null;
        state.status = state.display_status;
      }
    }
    function dispatch(frame) {
      if (Number.isSafeInteger(frame.data.seq) && frame.data.seq < recoveryFloor) return;
      reduce(frame.event, frame.data);
      if (options.onEvent) options.onEvent(frame.event, frame.data);
    }
    function beginReplay(firstSeq) {
      // Only a durable first page can establish a historic sequence base.
      // A later missing event must be filled before the cursor advances.
      if (state.cursor || !Number.isSafeInteger(firstSeq) || firstSeq <= 1) return;
      state.cursor = firstSeq - 1;
      pending.forEach(function (_, seq) { if (seq < firstSeq) pending.delete(seq); });
    }
    function receive(type, data, eventId) {
      data = data && typeof data === 'object' ? data : {};
      if (foreignRequest(type, data)) return false;
      if (data.job_id && state.job_id && data.job_id !== state.job_id) return false;
      var seq = data.seq;
      if (seq === undefined && eventId && /^\d+$/.test(String(eventId))) seq = Number(eventId);
      if (seq === undefined) { dispatch({event: type, data: data}); notify(); return true; }
      if (!Number.isSafeInteger(seq) || seq <= state.cursor || seq < 1) return false;
      if (eventId && /^\d+$/.test(String(eventId)) && Number(eventId) !== seq) return false;
      if (!pending.has(seq)) pending.set(seq, {event: type, data: Object.assign({}, data, {seq: seq})});
      while (pending.has(state.cursor + 1)) {
        var frame = pending.get(state.cursor + 1);
        pending.delete(++state.cursor);
        dispatch(frame);
      }
      notify(); return true;
    }
    function reset(jobId) {
      pending.clear(); recoveryFloor = 0; context = createContext(options); state.context_snapshot = null;
      nativeStream.reset(); state.native_stream = nativeStream.view();
      Object.assign(state, {job_id: jobId, cursor: 0, task_version: 0, applied_version: 0, status: '', started_at: 0, finished_at: 0,
        display_status: '', terminal_snapshot: null, question: null, approvals: [], operations: [], pending_updates: [], not_applied_updates: [],
        artifact_delivery: null, recovery_cursor: 0});
    }
    return {state: state, receive: receive, hydrate: hydrate, beginReplay: beginReplay,
      setRecoveryFloor: function (cursor) {
        if (Number.isSafeInteger(cursor) && cursor > recoveryFloor) recoveryFloor = cursor;
      },
      hasGap: function () { return pending.size > 0; },
      subscribe: function (fn) { subscribers.push(fn); return function () { subscribers = subscribers.filter(function (item) { return item !== fn; }); }; },
      resetJob: function (jobId) { reset(jobId); notify(); },
      clear: function () { subscribers.length = 0; reset(''); }};
  }
  root.CitrusTaskState = {nativeProgress: nativeProgress, nativeStreamEvent: nativeStreamEvent,
    createNativeStream: createNativeStream, create: create, createProcess: createProcess,
    createContext: createContext, waitingView: waitingView, displayStatus: displayStatus,
    snapshotStatus: snapshotStatus, isTerminal: function (state) { return terminal.indexOf(state.display_status) >= 0; },
    resumable: function (state) { return list(state.operations).filter(function (op) {
      return op.resumable === true && op.status === 'interrupted' && ['local_read', 'pure_computation'].indexOf(op.recovery_mode) >= 0;
    }); }};
}(typeof window !== 'undefined' ? window : globalThis));
