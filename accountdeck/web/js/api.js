/* AccountDeck 2 — the local HTTP API and state polling.
 * Every request stays on this origin (CSP connect-src 'self'). POSTs carry the headers the server
 * requires (X-AccountDeck, JSON body); the session cookie is HttpOnly and sent by the browser.
 * Polling asks /api/state?rev=N, and a 304 keeps the previous state. The poll chain never dies:
 * every outcome, including a failure, schedules the next poll, and a read that stalls is given up on. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const VISIBLE_MS = 10000;
  const HIDDEN_MS = 60000;
  const FAST_MS = 2000;            // while an operation or a sign-in is pending
  const FAST_HIDDEN_MS = 10000;    // a hidden tab still learns the outcome of a switch within seconds
  const READ_TIMEOUT_MS = 20000;   // a stalled read must not hold the only polling slot for good
  // Actions that only read: they change nothing, so no state reload follows them.
  const QUERIES = new Set(['switch-plan', 'reset-plan', 'import-bundle-preview']);

  /** An API failure: `message` is the server's human text; `status` 0 means the app did not answer. */
  class ApiError extends Error {
    constructor(message, { status = 0, code = null, retryAfter = null, plan = null } = {}) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.retryAfter = retryAfter;
      this.plan = plan;
    }
  }

  async function request(method, path, body) {
    const options = { method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
    if (method === 'POST') {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-AccountDeck'] = '1';
      options.body = JSON.stringify(body || {});
    }
    // Writes are never timed out: a lost reply does not mean the action failed to start.
    const read = method === 'GET' ? new AbortController() : null;
    const limit = read ? window.setTimeout(() => read.abort(), READ_TIMEOUT_MS) : null;
    if (read) options.signal = read.signal;
    const unanswered = () => new ApiError(AD.format.STRINGS.connection.network, { status: 0, code: 'network' });
    try {
      let response;
      try {
        response = await window.fetch(path, options);
      } catch (err) {
        throw unanswered();
      }
      if (response.status === 304) return null;
      let data = null;
      try {
        data = await response.json();
      } catch (err) {
        // A body that stalled or was cut off is no answer; one that came whole but is not JSON is the server's.
        if (read && response.ok && !(err instanceof SyntaxError)) throw unanswered();
        data = null;
      }
      if (!response.ok) {
        const info = data && typeof data === 'object' ? data : {};
        const message = response.status === 401 ? AD.format.label('open_hint')
          : AD.format.errorText(info.code, info.error) || AD.format.STRINGS.connection.failed;
        throw new ApiError(message, {
          status: response.status, code: info.code || null, retryAfter: info.retry_after || null, plan: info.plan || null,
        });
      }
      return data;
    } finally {
      window.clearTimeout(limit);
    }
  }

  /** GET a same-origin path; resolves to the JSON body, or null for 304 Not Modified. */
  function get(path) {
    return request('GET', path);
  }

  /** POST a JSON object; resolves to the JSON body or rejects with an ApiError. */
  function post(path, body) {
    return request('POST', path, body);
  }

  /** Run an action: POST /api/action {action: name, ...body}. State-changing actions are
   * followed by an immediate state poll so the page never waits 10 s to show their effect. */
  async function action(name, body) {
    const result = await post('/api/action', Object.assign({}, body, { action: name }));
    if (!QUERIES.has(name)) poll();
    return result;
  }

  /** Patch server settings; resolves to {ok, settings}. */
  async function settings(patch) {
    const result = await post('/api/settings', { patch });
    poll();
    return result;
  }

  // ---- Polling --------------------------------------------------------------------------------

  const polling = {
    active: false, onState: null, onConnection: null, rev: null, timer: null,
    inFlight: null, again: false, fast: false, connection: null,
  };

  function delay() {
    if (document.hidden) return polling.fast ? FAST_HIDDEN_MS : HIDDEN_MS;
    return polling.fast ? FAST_MS : VISIBLE_MS;
  }

  function schedule() {
    window.clearTimeout(polling.timer);
    polling.timer = polling.active ? window.setTimeout(poll, delay()) : null;
  }

  function connection(status) {
    if (status === polling.connection) return;
    polling.connection = status;
    if (polling.onConnection) polling.onConnection(status);
  }

  async function load() {
    try {
      const path = polling.rev === null ? '/api/state' : `/api/state?rev=${encodeURIComponent(polling.rev)}`;
      const state = await get(path);
      if (!polling.active) return;
      connection('ok');
      if (state) {
        polling.rev = state.rev;
        polling.onState(state);
      }
    } catch (err) {
      if (!polling.active) return;
      if (err instanceof ApiError && err.status === 401) connection('signin');
      else if (err instanceof ApiError && err.status === 0) connection('offline');
      else console.error(err);
    }
  }

  /** Fetch the state now (at most one request at a time; a poll asked for meanwhile runs right after). */
  function poll() {
    if (!polling.active) return Promise.resolve();
    if (polling.inFlight) {
      polling.again = true;
      return polling.inFlight;
    }
    window.clearTimeout(polling.timer);
    polling.inFlight = load().finally(() => {
      polling.inFlight = null;
      if (polling.again && polling.active) {
        polling.again = false;
        poll();
      } else {
        schedule();
      }
    });
    return polling.inFlight;
  }

  function onVisibility() {
    if (!polling.active) return;
    if (document.hidden) schedule();
    else poll();
  }

  /** Start polling: onState(state) for every new state, onConnection('ok' | 'offline' | 'signin')
   * when reachability changes. Polls every 10 s while visible, 60 s while hidden, 2 s while
   * setFast(true), and at once when the page becomes visible. */
  function startPolling(onState, onConnection) {
    polling.onState = onState;
    polling.onConnection = onConnection || null;
    if (!polling.active) {
      polling.active = true;
      document.addEventListener('visibilitychange', onVisibility);
    }
    return poll();
  }

  function stopPolling() {
    polling.active = false;
    polling.again = false;
    window.clearTimeout(polling.timer);
    polling.timer = null;
    document.removeEventListener('visibilitychange', onVisibility);
  }

  /** Poll every 2 s (an operation or sign-in is pending) or at the normal pace. */
  function setFast(on) {
    const fast = !!on;
    if (fast === polling.fast) return;
    polling.fast = fast;
    if (polling.active && !polling.inFlight) schedule();
  }

  AD.api = { get, post, action, settings, startPolling, stopPolling, setFast, poll, ApiError };
})();
