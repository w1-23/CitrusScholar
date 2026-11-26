(function (root) {
  'use strict';
  function parser(deliver) {
    var buffer = '', type = 'message', id = '', lines = [];
    function line(text) {
      if (!text) {
        if (lines.length) {
          var data;
          try { data = JSON.parse(lines.join('\n')); } catch (_) { data = null; }
          if (data) deliver(type, data, id);
        }
        type = 'message'; id = ''; lines = []; return;
      }
      if (text[0] === ':') return;
      var colon = text.indexOf(':'), key = colon < 0 ? text : text.slice(0, colon);
      var value = colon < 0 ? '' : text.slice(colon + 1).replace(/^ /, '');
      if (key === 'event') type = value;
      else if (key === 'id') id = value;
      else if (key === 'data') lines.push(value);
    }
    return {push: function (text, final) {
      buffer += text;
      var offset;
      while ((offset = buffer.indexOf('\n')) >= 0) {
        line(buffer.slice(0, offset).replace(/\r$/, '')); buffer = buffer.slice(offset + 1);
      }
      if (final) { if (buffer) line(buffer.replace(/\r$/, '')); buffer = ''; line(''); }
    }};
  }
  function create(options) {
    var store = options.store, stopped = false, replaying = null, timer = null, followVersion = 0;
    var reads = new AbortController();
    function owns() { return !stopped && options.owns(); }
    async function json(url, settings) {
      if (!owns()) return null;
      var scope = reads;
      var response = await options.fetch(url, settings || {signal: scope.signal, cache: 'no-store'});
      if (!owns() || scope !== reads) return null;
      var data = await response.json();
      if (!owns() || scope !== reads) return null;
      if (!response.ok) {
        var error = new Error(response.status === 409 ? '任务已更新，请核对最新状态后重试。' : '请求未完成，请稍后重试。');
        error.status = response.status; error.detail = data.detail; throw error;
      }
      return data;
    }
    function snapshot(data) {
      if (!owns() || !data) return;
      if (!store.hydrate(data)) return;
      if (options.onSnapshot) options.onSnapshot(data);
    }
    async function replay(jobId) {
      if (!jobId || !owns()) return;
      if (replaying) return replaying;
      var scope = reads;
      var flight = replaying = (async function () {
        var more = true;
        while (more && owns()) {
          if (scope !== reads) return;
          var before = store.state.cursor;
          var page = await json('/jobs/' + encodeURIComponent(jobId) + '/events?after=' + before + '&limit=200');
          if (!owns() || !page) return;
          if (page.protocol !== 'citrus-task/1' || page.job_id && page.job_id !== jobId) throw new Error('任务事件格式不匹配。');
          if (store.setRecoveryFloor) store.setRecoveryFloor(page.recovery_cursor);
          (page.events || []).forEach(function (frame) {
            if (owns()) store.receive(frame.event, Object.assign({}, frame.data, {seq: frame.seq}), String(frame.seq));
          });
          // A cursor is a watermark: a page may repeat consumed events, never
          // move it backwards. The terminal snapshot is hydrated only after the
          // page (including the artifact-delivery tail before ``done``) is applied.
          if (Number.isSafeInteger(page.next_cursor) && page.next_cursor < store.state.cursor) {
            throw new Error('任务事件游标回退。');
          }
          if (store.hasGap() || Number.isSafeInteger(page.next_cursor) && store.state.cursor < page.next_cursor) {
            throw new Error('任务状态暂未同步，正在补读事件。');
          }
          more = !!page.has_more;
          if (more && store.state.cursor <= before) throw new Error('任务事件游标未前进。');
          if (!more) snapshot(Object.assign({}, page, {event_cursor: page.next_cursor}));
        }
      }());
      try { return await flight; } finally { if (replaying === flight) replaying = null; }
    }
    async function refresh(jobId, withEvents) {
      var job = await json('/jobs/' + encodeURIComponent(jobId));
      if (!job || !owns()) return null;
      if (job.job_id !== jobId) throw new Error('任务状态标识不匹配。');
      var taskState = root.CitrusTaskState;
      // A late status response must not move the consumed watermark backwards.
      if (Number.isSafeInteger(job.event_cursor) && job.event_cursor < store.state.cursor) return job;
      // The status read may see recovery admitted after replay returned a terminal event.
      if ((withEvents !== false || taskState.snapshotStatus(job) || taskState.isTerminal(store.state)) &&
          job.event_cursor > store.state.cursor) {
        await replay(jobId);
        if (owns() && job.event_cursor > store.state.cursor) throw new Error('任务状态暂未同步，正在补读事件。');
      }
      if (owns()) snapshot(job);
      return owns() ? job : null;
    }
    function follow(jobId) {
      if (timer) clearTimeout(timer);
      var version = ++followVersion;
      async function poll() {
        if (!owns() || version !== followVersion) return;
        try {
          await replay(jobId);
          if (owns() && version === followVersion) await refresh(jobId, false);
          if (root.CitrusTaskState.isTerminal(store.state)) return;
        } catch (error) { if (owns() && version === followVersion && options.onError) options.onError(error); }
        if (owns() && version === followVersion) timer = setTimeout(poll, 2000);
      }
      return poll();
    }
    function followContinuation(jobId) {
      if (store.state.job_id !== jobId) {
        reads.abort();
        reads = new AbortController();
        replaying = null;
        store.resetJob(jobId);
      }
      return follow(jobId);
    }
    function followRequest(sessionId, requestId, onFound, onUnavailable) {
      if (timer) clearTimeout(timer);
      var version = ++followVersion, attempts = 0;
      var url = '/api/v2/session/' + encodeURIComponent(sessionId) + '/task-by-request/' + encodeURIComponent(requestId);
      async function lookup() {
        if (!owns() || version !== followVersion) return;
        attempts++;
        var claim;
        try { claim = await json(url); }
        catch (error) {
          if (!owns() || version !== followVersion) return;
          if (error.status === 401 || error.status === 403 || attempts >= 8) {
            onUnavailable(error);
            return;
          }
          timer = setTimeout(lookup, 2000);
          return;
        }
        if (!owns() || version !== followVersion) return;
        if (!claim || typeof claim.job_id !== 'string' || !claim.job_id) {
          onUnavailable(new Error('任务标识暂时无法确认。'));
          return;
        }
        onFound(claim.job_id);
        return follow(claim.job_id);
      }
      return lookup();
    }
    async function stream(response, signal) {
      var reader = response.body.getReader(), decoder = new TextDecoder();
      var frames = [];
      var input = parser(function (type, data, id) { frames.push([type, data, id]); });
      async function flush() {
        while (frames.length && owns() && !signal.aborted) {
          var frame = frames.shift();
          store.receive(frame[0], frame[1], frame[2]);
          if (store.hasGap() && store.state.job_id) await replay(store.state.job_id);
        }
      }
      try {
        while (owns() && !signal.aborted) {
          var chunk = await reader.read();
          if (!owns() || signal.aborted) return;
          input.push(chunk.done ? decoder.decode() : decoder.decode(chunk.value, {stream: true}), !!chunk.done);
          await flush();
          if (chunk.done) return;
        }
      } finally {
        if (!owns() || signal.aborted) await reader.cancel().catch(function () {});
      }
    }
    return {stream: stream, replay: replay, refresh: refresh, follow: follow, followContinuation: followContinuation, followRequest: followRequest,
      answer: function (jobId, questionId, payload) { return json('/api/tasks/' + encodeURIComponent(jobId) + '/questions/' + encodeURIComponent(questionId) + '/answer', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      }); },
      post: function (jobId, path, payload) { return json('/api/v2/chat/jobs/' + encodeURIComponent(jobId) + '/' + path, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      }); },
      lookupUpdate: function (jobId, requestId) {
        return json('/api/v2/chat/jobs/' + encodeURIComponent(jobId) + '/updates/' + encodeURIComponent(requestId));
      },
      stop: function () { stopped = true; reads.abort(); if (timer) clearTimeout(timer); timer = null; }};
  }
  function currentQueueEntry(previous, entry) {
    if (!entry || !entry.client_request_id) return false;
    if (!previous) return true;
    var generation = Number.isSafeInteger(entry.generation) ? entry.generation : 0;
    var last = Number.isSafeInteger(previous.generation) ? previous.generation : 0;
    return generation >= last && (generation > last || previous.state !== 'terminal' || entry.state === 'terminal');
  }

  // One fetch lane and timer for the mounted session, independent of how many
  // turns observe their own job events. A watched terminal need not be within
  // the page that advanced after_seq. Missing/failed reads never erase a row.
  function createSessionObserver(options) {
    var observers = new Map(), known = new Map(), cursor = null, timer, flight, stopped = false;
    var reads = new AbortController();
    var owns = function () { return !stopped && options.owns(); };
    function schedule() {
      if (timer != null || !owns() || !observers.size) return;
      timer = setTimeout(function () { timer = null; if (observers.size) void read(); }, options.interval || 2000);
    }
    function read() {
      if (!owns()) return Promise.resolve(null);
      if (flight) return flight;
      if (timer != null) { clearTimeout(timer); timer = null; }
      var watches = Array.from(observers.keys()).slice(0, 32);
      // The unqualified first window returns the latest history. A numeric
      // zero would instead page from the beginning of an old session.
      var url = options.url + '?limit=100' + (cursor == null ? '' : '&after_seq=' + cursor)
        + watches.map(function (id) { return '&watch=' + encodeURIComponent(id); }).join('');
      flight = Promise.resolve().then(async function () {
        if (!owns()) return null;
        var response = await options.fetch(url, {signal: reads.signal, cache: 'no-store'});
        if (!response.ok) throw new Error('queue_unavailable');
        var page = await response.json();
        if (!owns()) return null;
        if (!Array.isArray(page.turns) || (page.next_after_seq != null &&
            (!Number.isSafeInteger(page.next_after_seq) || page.next_after_seq < (cursor ?? 0)))) throw new Error('queue_protocol');
        var returned = new Map();
        page.turns.forEach(function (entry) {
          if (!entry || (entry.session_id && entry.session_id !== options.sessionId)) return;
          var id = entry.client_request_id;
          if (currentQueueEntry(known.get(id), entry)) { known.set(id, entry); returned.set(id, entry); }
        });
        if (page.next_after_seq != null) cursor = page.next_after_seq;
        if (known.size > 132) {
          for (var pair of known) {
            if (known.size <= 132) break;
            if (pair[1].state === 'terminal' && !observers.has(pair[0])) known.delete(pair[0]);
          }
        }
        if (options.onAvailability) options.onAvailability(watches.every(function (id) { return returned.has(id); }));
        returned.forEach(function (entry, id) {
          var listeners = observers.get(id);
          if (listeners && owns()) Array.from(listeners).forEach(function (adopt) { adopt(entry); });
        });
        return Array.from(known.values());
      }).catch(function () {
        if (owns() && options.onAvailability) options.onAvailability(false);
        return null;
      }).finally(function () { flight = null; schedule(); });
      return flight;
    }
    return {snapshot: read, watch: function (id, adopt) {
      if (!owns()) return function () {};
      var listeners = observers.get(id);
      if (!listeners) observers.set(id, listeners = new Set());
      listeners.add(adopt); schedule();
      return function () {
        listeners.delete(adopt);
        if (!listeners.size) observers.delete(id);
        if (!observers.size && timer != null) { clearTimeout(timer); timer = null; }
      };
    }, stop: function () {
      stopped = true; reads.abort(); observers.clear(); known.clear();
      if (timer != null) clearTimeout(timer);
      timer = null;
    }};
  }
  root.CitrusTaskTransport = {parser: parser, create: create, createSessionObserver: createSessionObserver,
    currentQueueEntry: currentQueueEntry};
}(typeof window !== 'undefined' ? window : globalThis));
