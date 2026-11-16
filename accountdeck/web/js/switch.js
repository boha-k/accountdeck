/* AccountDeck 2 — switching and sign-in flows in the one modal.
 * Every flow that changes an app's sign-in first asks the service for a plan (switch-plan), shows
 * it, and sends the plan id with the user's consent ("idle" or "now"); the service checks the plan
 * again before acting. Views: plan (a sheet), confirm-now and confirm-end (alerts), progress,
 * report and login. The progress, report and login views have banner stand-ins (banners.js) while
 * the modal is closed. Button order is the Mac's: the default button last (rightmost), Cancel
 * beside it, any alternative on the far left.
 * Antigravity (provider "antigravity") uses the same views: its plan lists the running agy processes
 * (`running.agy_consumers`), which AccountDeck never stops, so a switch can only wait for them and an Add cannot
 * start while one runs. A refusal with code `notice_required` shows the notice about Google's terms first
 * (antigravity.js), then asks again.
 * Using a limit reset (resets.js has its plan sheet) ends here too: its operation (kind "use-reset") shows in the
 * progress view and its report in the report view, worded by resets.js. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;

  // The action that carries out each plan purpose.
  const ACTIONS = {
    switch: 'switch', quit: 'quit-claude', add: 'add', capture: 'capture', recover: 'recover',
    'finish-login': 'finish-login', 'cancel-login': 'cancel-login',
  };
  // Warnings the "Running now" lines already say.
  const SAID = new Set(['busy', 'unknown', 'consumers']);
  const STEP_ICONS = { done: 'check', active: 'refresh', warning: 'warning', failed: 'x' };
  // An operation started here normally shows in the next state; give up waiting for it after this.
  const SHOW_UP_MS = 10000;

  let last = null;
  let watching = null;          // {op, at, seen} for the operation started from this page
  let reviewed = null;          // plan id of the needs_reconfirm prompt already shown
  let awaitingLogin = false;    // a sign-in was started here: show it once the state has it
  let stepKey = null;
  const sigs = { progress: null, report: null, login: null };

  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const spacer = () => el('span', { class: 'spacer', 'aria-hidden': 'true' });

  // ---- Building blocks ----------------------------------------------------------------------------

  function names(sessions) {
    return sessions.map((s) => `“${s.name}” (${STRINGS.session[s.status] || STRINGS.session.unknown})`).join(', ');
  }

  function sessionsText(sessions) {
    return fill(sessions.length === 1 ? STRINGS.modal.sessionsOne : STRINGS.modal.sessions,
      { n: sessions.length, names: names(sessions) });
  }

  function section(title, content) {
    return el('section', 'plan-section', el('h3', { class: 'plan-heading', text: title }), content);
  }

  function item(icon, text) {
    return el('li', 'plan-item', AD.dom.svgIcon(icon), el('span', { text: mask(text) }));
  }

  function failure(err) {
    AD.dom.toast(mask(err.message), { level: err.code === 'invalid' ? 'info' : 'critical' });
  }

  // ---- Plans ------------------------------------------------------------------------------------------

  /** The plan's running processes that count against it: Codex's for a Codex switch, agy's for an Antigravity
   * switch or Add (saving reads only, so agy may run). */
  function consumersOf(plan) {
    const running = plan.running || {};
    if (plan.provider === 'antigravity') return plan.purpose === 'capture' ? [] : running.agy_consumers || [];
    return plan.purpose === 'switch' ? running.codex_consumers || [] : [];
  }

  function planTitle(plan) {
    if (plan.provider === 'claude_cli' && plan.target) return fill(STRINGS.modal.switchTerminals, { label: mask(plan.target.label) });
    if (plan.provider === 'antigravity') {
      const G = STRINGS.antigravity;
      if (plan.purpose === 'switch' && plan.target) return fill(G.switchTitle, { label: mask(plan.target.label) });
      if (plan.purpose === 'add' && plan.target) return fill(G.againTitle, { label: mask(plan.target.label) });
      return G.purpose[plan.purpose] || STRINGS.modal.progressTitle;
    }
    if (plan.purpose === 'switch' && plan.target) {
      return fill(plan.provider === 'codex' ? STRINGS.modal.switchCodex : STRINGS.modal.switchClaude, { label: mask(plan.target.label) });
    }
    if (plan.provider === 'codex') return STRINGS.codex.purpose[plan.purpose] || STRINGS.modal.progressTitle;
    return STRINGS.modal.purpose[plan.purpose] || STRINGS.modal.progressTitle;
  }

  function targetLine(plan) {
    const target = plan.target;
    const meta = [STRINGS.apps[plan.provider], AD.format.capitalised(target.plan), target.email ? mask(target.email) : null];
    const mark = plan.provider === 'claude_cli'
      ? AD.dom.avatar(target.label, { size: 'large' })
      : AD.dom.svgIcon(plan.provider, { size: 20 });
    return el('div', 'plan-target', mark,
      el('div', 'plan-target-text', el('p', { class: 'plan-target-name', text: mask(target.label) }),
        el('p', { class: 'plan-target-meta', text: meta.filter(Boolean).join(' · ') })));
  }

  function headroomRows(plan) {
    const rows = [];
    if (plan.current && plan.headroom.now) rows.push([STRINGS.modal.rowNow, plan.current.label, plan.headroom.now]);
    if (plan.target && plan.headroom.after) rows.push([STRINGS.modal.rowAfter, plan.target.label, plan.headroom.after]);
    return el('dl', 'headroom', rows.map(([when, label, text]) => el('div', 'headroom-row',
      el('dt', { text: when }), el('dd', null, el('strong', { text: mask(label) }), el('span', { text })))));
  }

  /** A running process the plan waits for. A Codex session can be ended from here; an agy one never is. */
  function consumerRow(plan, consumer) {
    const agy = plan.provider === 'antigravity';
    const what = agy ? STRINGS.antigravity.consumerKind
      : STRINGS.modal.consumerKind[consumer.kind] || STRINGS.modal.consumerKind.other;
    const started = typeof consumer.started === 'number'
      ? fill(STRINGS.modal.consumerStarted, { clock: AD.model.clock(consumer.started, AD.app.clockNow()) }) : null;
    const parts = [what, consumer.parent ? fill(STRINGS.modal.consumerFrom, { parent: consumer.parent }) : null, started];
    return el('li', 'consumer', el('span', { text: parts.filter(Boolean).join(' · ') }),
      plan.provider === 'codex' ? AD.dom.button(STRINGS.button.endCodex, () => confirmEnd(plan, consumer), { small: true }) : null);
  }

  /** "2 Codex sessions are running…", or what agy's running sessions mean for this plan. */
  function consumersText(plan, n) {
    if (plan.provider !== 'antigravity') return fill(n === 1 ? STRINGS.modal.consumersOne : STRINGS.modal.consumers, { n });
    const G = STRINGS.antigravity;
    if (plan.purpose === 'add') return fill(n === 1 ? G.consumersAddOne : G.consumersAdd, { n });
    return fill(n === 1 ? G.consumersOne : G.consumers, { n });
  }

  function planBody(plan, options) {
    const running = plan.running || {};
    const sessions = running.sessions || [];
    const consumers = consumersOf(plan);
    const agy = plan.provider === 'antigravity';
    // Antigravity: the service's own steps, else the page's words for the purpose.
    const will = (plan.provider === 'codex' && STRINGS.codex.will[plan.purpose])
      || (agy && !(plan.will || []).length && STRINGS.antigravity.will[plan.purpose]) || plan.will || [];
    const parts = [];
    if (options.reconfirm) parts.push(el('p', 'plan-reconfirm', AD.dom.svgIcon('info'),
      el('span', { text: plan.provider === 'codex' ? STRINGS.codex.reconfirm
        : (agy ? STRINGS.antigravity.reconfirm : STRINGS.modal.reconfirm) })));
    if (options.error) parts.push(el('p', { class: 'modal-error', role: 'alert', text: mask(options.error) }));
    if (plan.target) parts.push(targetLine(plan));
    if (plan.provider === 'codex') parts.push(el('p', { class: 'plan-lead', text: STRINGS.codex.isolation }));
    if (agy) parts.push(el('p', { class: 'plan-lead', text: STRINGS.antigravity.isolation }));
    const stop = plan.provider === 'claude' ? { managed: STRINGS.modal.managed, graceful: STRINGS.modal.graceful }[plan.stop_tier] : null;
    if (stop) parts.push(el('p', { class: 'plan-lead', text: stop }));
    if (plan.saves_outgoing && plan.current) {
      parts.push(el('p', { class: 'plan-note', text: fill(STRINGS.modal.savesOutgoing, { label: mask(plan.current.label) }) }));
    }
    const now = [];
    if (sessions.length) now.push(el('p', { text: sessionsText(sessions) }));
    if (consumers.length) {
      now.push(el('p', { text: consumersText(plan, consumers.length) }));
      now.push(el('ul', 'consumer-list', consumers.map((consumer) => consumerRow(plan, consumer))));
    }
    if (plan.invisible) now.push(el('p', { class: 'plan-muted', text: plan.invisible }));
    if (plan.headroom) now.push(headroomRows(plan));
    if (now.length) parts.push(section(STRINGS.modal.running, now));
    const stays = (plan.preserved || []).map((text) => item('check', text))
      .concat((plan.account_bound || []).map((text) => item('info', text)));
    if (will.length || stays.length) {
      parts.push(el('details', { class: 'disclosure', 'data-keep': 'plan-more' }, el('summary', { text: STRINGS.modal.will }),
        will.length ? el('ol', 'plan-steps', will.map((text) => el('li', { text: mask(text) }))) : null,
        stays.length ? el('p', { class: 'plan-heading', text: STRINGS.modal.stays }) : null,
        stays.length ? el('ul', 'plan-list', stays) : null));
    }
    const heads = (plan.warnings || []).filter((w) => !SAID.has(w.code)).map((w) => item('warning', w.message));
    if (heads.length) parts.push(section(STRINGS.modal.headsUp, el('ul', 'plan-list', heads)));
    if (plan.tip) parts.push(el('p', { class: 'plan-tip', text: mask(plan.tip) }));
    if ((plan.blockers || []).length) {
      parts.push(el('div', { class: 'plan-blockers', role: 'alert' }, el('p', { class: 'plan-blocked', text: STRINGS.modal.blocked }),
        plan.blockers.map((b) => el('p', { text: b.message }))));
    } else if (plan.provider !== 'codex' && !agy && plan.remember_available && plan.purpose === 'switch') {
      parts.push(el('label', 'check', el('input', { type: 'checkbox', id: 'remember-idle' }), el('span', { text: STRINGS.modal.remember })));
    }
    return el('div', 'plan', parts);
  }

  function remembered() {
    const box = document.getElementById('remember-idle');
    return !!(box && box.checked);
  }

  function planActions(plan) {
    const cancel = AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { dataset: { choice: 'cancel' } });
    if ((plan.blockers || []).length) {
      cancel.setAttribute('data-autofocus', '');
      return [cancel];
    }
    const consumers = consumersOf(plan).length;
    const switching = plan.purpose === 'switch';
    const agy = plan.provider === 'antigravity';
    const idleOffered = plan.provider === 'claude' || (switching && consumers);
    const preferIdle = idleOffered && (plan.default_button === 'idle' || agy);
    const idle = idleOffered ? AD.dom.button(plan.provider === 'codex' ? STRINGS.button.waitCodex
      : (agy ? STRINGS.antigravity.wait : (switching ? STRINGS.button.switchIdle : STRINGS.button.continueIdle)),
    () => execute(plan, 'idle', remembered()), { kind: preferIdle ? 'primary' : undefined, dataset: { choice: 'idle' } }) : null;
    // Codex and agy sessions are never interrupted: "now" waits for none of them, so it is off while one runs (an
    // Antigravity Add too, which would sign agy out).
    const held = consumers > 0 && ((switching && plan.provider === 'codex') || (agy && (switching || plan.purpose === 'add')));
    const now = AD.dom.button(switching ? STRINGS.button.switchNow : STRINGS.button.continueNow, () => confirmNow(plan),
      { kind: preferIdle ? undefined : 'primary', disabled: held, dataset: { choice: 'now' } });
    const preferred = preferIdle ? idle : now;
    (preferred.disabled ? cancel : preferred).setAttribute('data-autofocus', '');
    // The default button goes last; the other choice sits on the far left.
    const other = preferred === idle ? now : idle;
    return [other, other ? spacer() : null, cancel, preferred];
  }

  /** Show a plan. options: reconfirm (the sessions changed), error (why the last try failed). */
  function showPlan(plan, options = {}) {
    if (!plan) return;
    AD.dom.openModal({ title: planTitle(plan), body: planBody(plan, options), actions: planActions(plan), view: 'plan' });
  }

  /** "Switch Now" while Code sessions work asks once more, naming them. */
  function confirmNow(plan) {
    const running = plan.running || {};
    if (plan.provider !== 'claude' || !((running.busy || 0) + (running.unknown || 0))) return execute(plan, 'now', remembered());
    const sessions = running.sessions || [];
    const working = sessions.filter((s) => s.status !== 'idle');
    const shown = working.length ? working : sessions;
    const switching = plan.purpose === 'switch';
    AD.dom.openModal({
      title: fill(switching ? STRINGS.modal.interrupt : STRINGS.modal.interruptOther, { names: shown.map((s) => `“${s.name}”`).join(', ') }),
      body: el('p', { text: sessionsText(sessions) }), view: 'confirm-now', size: 'alert',
      actions: [
        AD.dom.button(STRINGS.button.cancel, () => showPlan(plan), { dataset: { choice: 'cancel' }, attrs: { 'data-autofocus': '' } }),
        AD.dom.button(switching ? STRINGS.button.switchAnyway : STRINGS.button.continueAnyway, () => execute(plan, 'now'),
          { kind: 'danger', dataset: { choice: 'now' } }),
      ],
    });
    return undefined;
  }

  function confirmEnd(plan, consumer) {
    AD.dom.openModal({
      title: STRINGS.modal.endTitle, body: el('p', { text: STRINGS.modal.endBody }), view: 'confirm-end', size: 'alert',
      actions: [
        AD.dom.button(STRINGS.button.cancel, () => showPlan(plan), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(STRINGS.button.endCodex, async () => {
          try {
            await AD.api.action('stop-consumer', { provider: plan.provider, pid: consumer.pid, plan_id: plan.plan_id });
          } catch (err) {
            showPlan(plan, { error: err.message });
            return;
          }
          await replan(plan);
        }, { kind: 'danger' }),
      ],
    });
  }

  function replan(plan) {
    const agy = plan.provider === 'antigravity';
    if (plan.purpose === 'switch' && plan.target) return open(plan.target.id, agy ? { provider: plan.provider } : undefined);
    // An Antigravity Add with a target is Sign In Again for that account.
    return openPurpose(plan.provider, plan.purpose, agy && plan.target ? { id: plan.target.id } : undefined);
  }

  /** A refusal that asks for the notice about Google's terms first (Antigravity): show it, and on Continue run
   * `again`. False for any other refusal. */
  function noticeFirst(err, again) {
    if (!err || err.code !== 'notice_required' || !AD.antigravity) return false;
    AD.antigravity.notice(again);
    return true;
  }

  /** Carry out a plan with the user's consent; a changed situation shows the fresh plan instead. */
  async function execute(plan, consent, remember = false) {
    const agy = plan.provider === 'antigravity';
    const body = { plan_id: plan.plan_id, consent, provider: plan.provider };
    if (plan.purpose === 'add' && !agy) body.method = 'browser';
    if (plan.purpose === 'add' && agy && plan.target) body.id = plan.target.id;          // Sign In Again
    if (plan.purpose === 'switch') Object.assign(body, agy ? { id: plan.target.id } : { id: plan.target.id, remember_idle: remember });
    let result;
    try {
      result = await AD.api.action(ACTIONS[plan.purpose], body);
    } catch (err) {
      if (noticeFirst(err, () => replan(plan))) return;
      if ((err.code === 'needs_reconfirm' || err.code === 'confirm_required') && err.plan) {
        showPlan(err.plan, { reconfirm: err.code === 'needs_reconfirm' });
      } else if (err.code === 'stale_plan') {
        await replan(plan);
      } else {
        showPlan(plan, { error: err.message });
      }
      return;
    }
    if (plan.purpose === 'add' && !agy) awaitingLogin = true;      // Antigravity's sign-in has its own note (antigravity.js)
    follow(result);
  }

  /** Show the progress of an operation an action just started ({op}), then its report. */
  function follow(result) {
    watching = result && result.op ? { op: result.op, at: Date.now(), seen: false } : null;
    openProgress(last && last.pending);
  }

  /** Plan a switch to an account and show it; skipped when the service says no confirmation is
   * needed (nothing running and "Ask before switching when Claude is idle" off). Codex and Antigravity always
   * show it. `extra` joins the request (Antigravity names its provider). */
  async function open(accountId, extra) {
    let result;
    try {
      result = await AD.api.action('switch-plan', Object.assign({ id: accountId }, extra));
    } catch (err) {
      if (!noticeFirst(err, () => open(accountId, extra))) failure(err);
      return;
    }
    const plan = result && result.plan;
    if (plan && plan.provider !== 'codex' && plan.provider !== 'antigravity' && plan.confirmation_required === false
        && !(plan.blockers || []).length) {
      await execute(plan, 'now');
      return;
    }
    showPlan(plan);
  }

  /** Start a flow other than a switch: finish-login, cancel-login, recover, capture, add or quit. `extra` joins the
   * request (Antigravity's Sign In Again names the account: {id}). */
  async function openPurpose(provider, purpose, extra) {
    let result;
    try {
      result = await AD.api.action('switch-plan', Object.assign({ provider, purpose }, extra));
    } catch (err) {
      if (!noticeFirst(err, () => openPurpose(provider, purpose, extra))) failure(err);
      return;
    }
    showPlan(result && result.plan);
  }

  // ---- Progress ------------------------------------------------------------------------------------------

  function progressTitle(pending) {
    if (pending && pending.kind === 'switch' && pending.label) return fill(STRINGS.progress.title, { label: mask(pending.label) });
    if (pending && pending.kind === 'use-reset') {
      return pending.label ? fill(STRINGS.resets.progressOn, { label: mask(pending.label) }) : STRINGS.resets.progress;
    }
    return STRINGS.modal.progressTitle;
  }

  function stepItem(step) {
    const icon = STEP_ICONS[step.status];
    return el('li', { class: 'step', dataset: { status: step.status } },
      icon ? AD.dom.svgIcon(icon, { className: 'step-icon' }) : el('span', { class: 'step-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'step-label', text: mask(step.label) }),
      el('span', { class: 'sr-only', text: ` (${STRINGS.progress.step[step.status] || step.status})` }),
      step.detail ? el('span', { class: 'step-detail', text: mask(step.detail) }) : null);
  }

  function progressBody(pending) {
    if (!pending) return el('p', { class: 'progress-now', text: STRINGS.modal.progressTitle });
    const waiting = pending.waiting_on || [];
    const active = (pending.steps || []).find((s) => s.status === 'active');
    return el('div', 'progress',
      el('p', { class: 'progress-now', text: mask(active ? active.label : pending.message || '') }),
      waiting.length ? el('p', { class: 'progress-waiting', text: fill(STRINGS.progress.waitingOn, { names: names(waiting) }) }) : null,
      el('div', { class: 'progress-bar', 'aria-hidden': 'true' }),
      el('ol', 'steps', (pending.steps || []).map(stepItem)));
  }

  function progressActions(pending) {
    const hide = AD.dom.button(STRINGS.button.hide, () => AD.dom.closeModal(), { kind: 'primary', dataset: { choice: 'hide' } });
    if (!pending || !pending.cancellable) return [hide];
    return [AD.dom.button(STRINGS.button.cancel, () => AD.api.action('cancel', { op: pending.id }), { dataset: { choice: 'cancel' } }), hide];
  }

  function progressSig(pending) {
    return JSON.stringify([pending, AD.dom.hideEmails()]);
  }

  function openProgress(pending) {
    sigs.progress = pending ? progressSig(pending) : null;
    // Closing the progress view only hides it: the operation goes on and the banner stands in.
    AD.dom.openModal({ title: progressTitle(pending), body: progressBody(pending), actions: progressActions(pending), view: 'progress' });
  }

  function announceStep(pending) {
    const active = pending && (pending.steps || []).find((s) => s.status === 'active');
    const key = pending ? `${pending.id}|${active ? active.id : pending.phase}` : null;
    if (stepKey && key && key !== stepKey && AD.dom.modalView() === 'progress') {
      AD.dom.announce(active ? active.label : pending.message);
    }
    stepKey = key;
  }

  /** The operation ended while its progress view was open: show its report, or close the view. */
  function finishProgress(state) {
    const report = state.report;
    if (report && (!watching || report.op === watching.op)) {
      watching = null;
      openReport(report);
      return;
    }
    if (watching && !watching.seen && Date.now() - watching.at < SHOW_UP_MS) return;
    watching = null;
    AD.dom.closeModal();
  }

  /** Keep the progress view in step with state.pending; options.open opens it (the banner's button). */
  function renderPending(state, options = {}) {
    last = state;
    const pending = state && state.pending;
    if (pending && watching && pending.id === watching.op) watching.seen = true;
    announceStep(pending);
    const reconfirm = pending && pending.phase === 'needs_reconfirm' && pending.plan;
    if (options.open) {
      if (reconfirm) {
        reviewed = pending.plan.plan_id;
        showPlan(pending.plan, { reconfirm: true });
      } else if (pending) {
        openProgress(pending);
      }
      return;
    }
    if (AD.dom.modalView() !== 'progress') return;
    if (!pending) {
      finishProgress(state);
      return;
    }
    if (reconfirm && reviewed !== pending.plan.plan_id) {
      reviewed = pending.plan.plan_id;
      showPlan(pending.plan, { reconfirm: true });
      return;
    }
    const sig = progressSig(pending);
    if (sig === sigs.progress) return;
    sigs.progress = sig;
    // Using a limit reset: the view opened before the operation showed in the state says "Working…" until then.
    const title = pending.kind === 'use-reset' ? document.getElementById('modal-title') : null;
    if (title && title.textContent !== progressTitle(pending)) title.textContent = progressTitle(pending);
    AD.dom.setModalBody(progressBody(pending));
    AD.dom.setModalActions(progressActions(pending));
  }

  // ---- Report -----------------------------------------------------------------------------------------------

  function reportTitle(report) {
    if (report.kind === 'use-reset') return AD.resets.reportLook(report).title;
    if (report.outcome === 'failed') return STRINGS.report.failedTitle;
    if (report.outcome === 'cancelled') return STRINGS.report.cancelledTitle;
    if (report.kind === 'switch' && report.label) return fill(STRINGS.report.switched, { label: mask(report.label) });
    return report.summary ? mask(report.summary) : STRINGS.report.title;
  }

  function reportBody(report) {
    if (report.kind === 'use-reset') return AD.resets.reportBody(report);
    const parts = [];
    if (report.summary) parts.push(el('p', { class: 'plan-lead', text: mask(report.summary) }));
    if (report.kind === 'switch' && report.provider === 'claude' && report.outcome === 'success') {
      const verified = report.verified === 'verified';
      const wrong = report.verified === 'failed';
      parts.push(el('p', null, AD.meter.badge(verified ? 'good' : (wrong ? 'critical' : 'neutral'),
        verified ? fill(STRINGS.report.verified, { label: mask(report.label) }) : (wrong ? STRINGS.report.failed : STRINGS.report.unconfirmed),
        { icon: verified || wrong ? undefined : 'hourglass' })));
    }
    if (report.verify_reason && report.verified !== 'verified') parts.push(el('p', { class: 'plan-muted', text: mask(report.verify_reason) }));
    if (report.error && report.error.message) {
      parts.push(el('p', { class: 'modal-error', role: 'alert', text: mask(AD.format.errorText(report.error.code, report.error.message)) }));
    }
    const carried = (report.carried || []).map((c) => item(c.status === 'carried' ? 'check' : 'warning',
      c.detail ? `${c.label} — ${c.detail}` : c.label));
    if (carried.length) parts.push(section(STRINGS.report.carried, el('ul', 'plan-list', carried)));
    const bound = (report.account_bound || []).map((b) => item('info', b.detail ? `${b.label} — ${b.detail}` : b.label));
    if (bound.length) parts.push(section(STRINGS.modal.bound, el('ul', 'plan-list', bound)));
    return el('div', 'report', parts);
  }

  function reportSig(report) {
    return JSON.stringify([report, AD.dom.hideEmails()]);
  }

  function openReport(report) {
    sigs.report = reportSig(report);
    const actions = [];
    if (report.manifest) {
      actions.push(AD.dom.button(STRINGS.button.explain, () => {
        AD.dom.closeModal();
        AD.details.showManifest(report.manifest);
      }, { kind: 'ghost', dataset: { choice: 'explain' } }), spacer());
    }
    actions.push(AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(),
      { kind: 'primary', dataset: { choice: 'done' }, attrs: { 'data-autofocus': '' } }));
    AD.dom.openModal({
      title: reportTitle(report), body: reportBody(report), actions, view: 'report',
      // Seeing the report is what dismisses it, however the view is closed.
      onClose: () => {
        if (last && last.report && last.report.op === report.op) AD.api.action('dismiss-report', {}).catch(failure);
      },
    });
  }

  /** Keep the report view in step with state.report; options.open opens it (the banner's button). */
  function renderReport(state, options = {}) {
    last = state;
    const report = state && state.report;
    if (options.open) {
      if (report) openReport(report);
      return;
    }
    if (AD.dom.modalView() !== 'report') return;
    if (!report) {
      AD.dom.closeModal();
      return;
    }
    const sig = reportSig(report);
    if (sig === sigs.report) return;
    sigs.report = sig;
    AD.dom.setModalBody(reportBody(report));
  }

  // ---- Sign-in in progress ---------------------------------------------------------------------------

  function signInLink(url, small) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (err) {
      return null;
    }
    const loopback = parsed.protocol === 'http:' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
    if (parsed.protocol !== 'https:' && !loopback) return null;
    return el('a', { class: ['button', small && 'button-small'], href: parsed.href, target: '_blank',
      rel: 'noopener noreferrer', 'data-keep': 'login-open' }, STRINGS.login.open, AD.dom.svgIcon('external'));
  }

  /** The controls of a sign-in in progress (the login view and its banner): Codex opens the provider's
   * page; Claude saves or cancels through a plan, since both close Claude. options.small for a banner. */
  function loginActions(login, options = {}) {
    const small = !!options.small;
    const actions = [AD.dom.button(STRINGS.login.cancel, () => openPurpose(login.provider, 'cancel-login'),
      { small, attrs: { 'data-keep': 'login-cancel' } })];
    if (login.provider === 'codex') {
      const link = signInLink(login.url, small);
      if (link) actions.push(link);
      actions.push(AD.dom.button(STRINGS.login.saveCodex, () => openPurpose('codex', 'finish-login'), { kind: 'primary', small }));
    } else {
      actions.push(AD.dom.button(STRINGS.login.save, () => openPurpose('claude', 'finish-login'),
        { kind: 'primary', small, attrs: { 'data-keep': 'login-save' } }));
    }
    return actions;
  }

  function loginBody(login) {
    const codex = login.provider === 'codex';
    return [el('p', { text: codex ? STRINGS.login.codex : STRINGS.login.claude }),
      login.user_code ? el('p', { class: 'move-path', text: fill(STRINGS.login.deviceCode, { code: login.user_code }) }) : null,
      codex ? null : el('p', { class: 'modal-hint', text: STRINGS.login.hint })];
  }

  function openLogin(login) {
    sigs.login = JSON.stringify(login);
    AD.dom.openModal({ title: login.provider === 'codex' ? STRINGS.login.codexTitle : STRINGS.login.claudeTitle,
      body: loginBody(login), actions: loginActions(login), view: 'login' });
  }

  /** Keep the login view in step with state.login. A sign-in started here opens it as soon as the
   * state shows it and no other view is open; options.open opens it (the banner). */
  function renderLogin(state, options = {}) {
    last = state;
    const login = state && state.login;
    const view = AD.dom.modalView();
    if (login && (options.open || (awaitingLogin && !view))) {
      awaitingLogin = false;
      openLogin(login);
      return;
    }
    if (view !== 'login') return;
    if (!login) {
      AD.dom.closeModal();
      return;
    }
    const sig = JSON.stringify(login);
    if (sig === sigs.login) return;
    sigs.login = sig;
    AD.dom.setModalBody(loginBody(login));
    AD.dom.setModalActions(loginActions(login));
  }

  AD.switch = { open, openPurpose, showPlan, follow, renderPending, renderReport, renderLogin, loginActions };
})();
