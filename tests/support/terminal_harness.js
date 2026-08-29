/**
 * A driver for `chronos/terminal/static/terminal.js`, for the tests in
 * `tests/safety/test_terminal_client.py`.
 *
 * The client is a browser file with no bundler, no framework and no exports
 * (ADR-0018 §6, and the header of terminal.js on why). That leaves two ways to
 * test it: assert on its source text, or run it. Source assertions pin spelling
 * rather than behaviour — they would pass on a `noteRequired: true` that nothing
 * reads — so this harness runs it, inside a `node:vm` context holding a DOM
 * small enough to read in one sitting and a `fetch` the test drives.
 *
 * Two properties of the context are what make this work at all. Top-level
 * `const` declarations stay in the context's lexical scope, so a later
 * `runInContext` can reach `state`, `COPY` and `PANELS`; and top-level function
 * declarations land on the global object, so every renderer and every action
 * builder is callable by name.
 *
 * ## What this is not
 *
 * Not a browser. Layout, CSS, focus and real event dispatch are absent, and
 * `window.setInterval` deliberately never fires — a poll running underneath an
 * assertion would make the failures intermittent. Every timer the client sets is
 * recorded and left unfired; the tests call `pollSystem`, `tickClock` and
 * `refreshPanel` themselves, which is also the only way to control ordering
 * precisely enough to pin the request-sequencing behaviour.
 *
 * Scenarios return plain JSON so the assertions live in Python, where a failure
 * reads as a failed assertion about a value rather than as a non-zero exit code.
 *
 * Usage: `node terminal_harness.js <path-to-terminal.js> <scenario>`.
 */

"use strict";

const fs = require("node:fs");
const vm = require("node:vm");

// ------------------------------------------------------------------ fake DOM

/** The status-bar and command-bar ids `terminal.js` expects the shell to carry. */
const SHELL_IDS = [
  "cmdform", "cmd", "cmd-symbol", "suggest", "cmd-msg", "grid", "empty", "empty-keys",
  "st-panels", "st-reset", "st-conn", "st-role", "st-autonomy", "st-capability", "st-kill", "st-armed",
  "st-alerts", "st-latency", "st-clock",
];

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.id = "";
    this.title = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.scrollTop = 0;
    this.attributes = {};
    this.listeners = {};
    this.classes = new Set();
    this._text = "";
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(text) {
    this.children = [];
    this._text = text === undefined || text === null ? "" : String(text);
  }

  get firstChild() {
    return this.children.length ? this.children[0] : null;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  setAttribute(name, val) {
    this.attributes[name] = String(val);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }

  /** Not a DOM method: how a test fires a handler, and awaits it if it is async. */
  dispatch(type, event = {}) {
    const fired = { preventDefault() {}, target: this, ...event };
    return (this.listeners[type] || []).map((handler) => handler(fired));
  }

  get classList() {
    const node = this;
    return {
      toggle(name, force) {
        const on = force === undefined ? !node.classes.has(name) : Boolean(force);
        if (on) node.classes.add(name);
        else node.classes.delete(name);
      },
    };
  }

  matches(selector) {
    if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
    return this.tagName === selector.toUpperCase();
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }

  /** Every descendant matching a selector, document order. */
  queryAll(selector, into = []) {
    for (const child of this.children) {
      if (child.matches(selector)) into.push(child);
      child.queryAll(selector, into);
    }
    return into;
  }

  focus() {}
  scrollIntoView() {}
  setSelectionRange() {}
}

// ---------------------------------------------------------------- fake world

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: { 200: "OK", 401: "Unauthorized", 409: "Conflict", 422: "Unprocessable Entity" }[status] || "",
    json: async () => body,
  };
}

/**
 * A loaded client, plus the handles a scenario needs to drive it.
 *
 * The default `fetch` never settles, which is what keeps `boot()`'s own status
 * poll and registry load from resolving underneath a scenario and rewriting the
 * state it just set up. A scenario that wants answers installs its own.
 */
function load(sourcePath) {
  const nodes = new Map();
  for (const id of SHELL_IDS) {
    const node = new FakeNode("div");
    node.id = id;
    nodes.set(id, node);
  }

  const calls = [];
  let responder = () => new Promise(() => {});

  const store = new Map();
  const timers = [];
  const sandbox = {
    console,
    AbortSignal,
    performance: { now: () => Date.now() },
    fetch: (url, init) => {
      calls.push({ url, init: init || {}, body: init && init.body ? JSON.parse(init.body) : null });
      return responder(url, init || {});
    },
    document: {
      visibilityState: "visible",
      createElement: (tag) => new FakeNode(tag),
      getElementById: (id) => {
        if (!nodes.has(id)) {
          const node = new FakeNode("div");
          node.id = id;
          nodes.set(id, node);
        }
        return nodes.get(id);
      },
      addEventListener: () => {},
    },
    window: {
      // Recorded and never fired: a poll landing mid-assertion is how a suite
      // like this becomes intermittent.
      setTimeout: (fn, ms) => timers.push({ kind: "timeout", fn, ms }),
      setInterval: (fn, ms) => timers.push({ kind: "interval", fn, ms }),
      clearInterval: () => {},
      addEventListener: () => {},
      localStorage: {
        getItem: (key) => (store.has(key) ? store.get(key) : null),
        setItem: (key, val) => store.set(key, String(val)),
      },
    },
  };
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(sourcePath, "utf8"), ctx, { filename: "terminal.js" });

  return {
    ctx,
    calls,
    timers,
    node: (id) => sandbox.document.getElementById(id),
    run: (expression) => vm.runInContext(expression, ctx),
    /** Hand the client a value it can read without a cross-realm surprise. */
    give: (name, value) => {
      sandbox[name] = value;
    },
    respond: (fn) => {
      responder = fn;
    },
  };
}

/** Let every pending microtask chain settle before asserting on what won. */
async function settle(rounds = 4) {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const SYSTEM_OK = {
  read_only: false,
  autonomy_configured: true,
  autonomy_stopped: false,
  seconds_until_next_tick: 2.5,
  queue_depth: 0,
  alerts_unacknowledged: 0,
  alerts_critical: 0,
  kill_switch_engaged: false,
  live_armed: false,
  mandate_active: true,
  mandate_id: "M-1",
  account_fingerprint: "f".repeat(64),
  operational_health: {
    service_readiness: { state: "READY", reasons: [] },
    trading_capability: {
      paper_new_exposure: { state: "BLOCKED", reasons: ["lane_not_configured"] },
      live_new_exposure: { state: "BLOCKED", reasons: ["lane_not_configured"] },
      autonomous_new_exposure: { state: "BLOCKED", reasons: ["mandate_absent"] },
    },
  },
  generated_at: "2026-07-26T12:00:00+00:00",
};

/** A status bar reduced to what a test asserts about it. */
function statusBar(client) {
  const read = (id) => {
    const node = client.node(id);
    return { text: node.textContent, className: node.className, hidden: node.hidden };
  };
  return {
    conn: read("st-conn"),
    role: read("st-role"),
    autonomy: read("st-autonomy"),
    capability: read("st-capability"),
    kill: read("st-kill"),
    armed: read("st-armed"),
    alerts: read("st-alerts"),
  };
}

/** Open an owner-action confirmation and return its parts. */
function openConfirm(client, host) {
  host.children[0].dispatch("click");
  const form = host.querySelector("form");
  const inputs = form.queryAll("input");
  const buttons = form.queryAll("button");
  return {
    form,
    why: form.queryAll(".confirm-why")[0],
    labels: form.queryAll("label").map((label) => label.textContent),
    note: inputs[0],
    typed: inputs[1],
    confirm: buttons[0],
    result: form.queryAll(".confirm-result")[0],
  };
}

// ---------------------------------------------------------------- scenarios

const scenarios = {
  // FINDING 1 — cached safety badges must not outlive a backend outage.
  async cached_badges_do_not_outlive_an_outage(client) {
    client.give("__system", SYSTEM_OK);
    const state = client.run("state");
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
    client.run("drawStatus()");
    const live = statusBar(client);

    // The poll now fails. `pollSystem` leaves `state.system` in place.
    state.systemStatus = "stale";
    state.systemError = "backend unreachable (network error)";
    client.run("drawStatus()");
    const afterFailure = statusBar(client);

    state.systemStatus = "unavailable";
    client.run("drawStatus()");
    return { live, afterFailure, afterUnavailable: statusBar(client) };
  },

  // FINDING 2 — the bar ages out on the panels' own rule.
  async the_status_bar_ages_out(client) {
    client.give("__system", SYSTEM_OK);
    const state = client.run("state");
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
    client.run("tickClock()");
    const fresh = { bar: statusBar(client), status: state.systemStatus };

    const bound = client.run("STATUS_MS * STALE_INTERVALS");
    state.systemAt = Date.now() - bound - 1000;
    client.run("tickClock()");
    return { fresh, aged: { bar: statusBar(client), status: state.systemStatus, error: state.systemError } };
  },

  // FINDINGS 1 AND 2 AGAIN — this time the *coupling* the two scenarios above
  // leave untested. Both of those assign `state.systemStatus` by hand and assert
  // what `drawStatus` draws when given it. Neither would notice a `pollSystem`
  // that stopped demoting on failure: the badges would go on reading "clear"
  // from cache and every assertion above would still pass, which is the shape of
  // a regression test that outlives the behaviour it was written for.
  //
  // So this one names no status. It drives the real poll against a real answer,
  // then against a real rejection, and reads the bar. The path from "a read
  // failed" to "the page stops claiming the kill switch is clear" is the thing
  // under test, and nothing here reaches past the client's own front door to
  // arrange the result.
  async a_failing_poll_demotes_the_bar_without_being_told(client) {
    client.respond(() => Promise.resolve(jsonResponse(200, SYSTEM_OK)));
    client.run("pollSystem()");
    await settle();
    const live = { bar: statusBar(client), status: client.run("state.systemStatus") };

    client.respond(() => Promise.reject(new TypeError("Failed to fetch")));
    client.run("pollSystem()");
    await settle();
    const state = client.run("state");
    return {
      live,
      afterFailure: { bar: statusBar(client), status: state.systemStatus },
      // The cache is deliberately still there. That is the point: keeping it is
      // what lets a recovered backend redraw instantly, and the fix is that
      // keeping it no longer lets a stale `false` render as "clear".
      cacheRetained: Boolean(state.system) && state.system.kill_switch_engaged === false,
    };
  },

  // FINDING 2 — a read that is never answered must resolve as a failure.
  async reads_are_bounded_by_a_timeout(client) {
    client.respond(() => new Promise(() => {}));
    client.run("pollSystem()");
    await settle();
    const read = client.calls[client.calls.length - 1];

    // A POST is deliberately not bounded — aborting one says nothing about
    // whether the backend applied it.
    client.run("postJSON('/terminal/mandate/revoke', {reason: 'x'})");
    await settle();
    const written = client.calls[client.calls.length - 1];
    return {
      timeoutMs: client.run("FETCH_TIMEOUT_MS"),
      readHasSignal: Boolean(read.init.signal),
      readSignalAborts: Boolean(read.init.signal && typeof read.init.signal.addEventListener === "function"),
      writeHasSignal: Boolean(written.init.signal),
    };
  },

  // FINDING 3 — a slow answer must not overwrite a newer one.
  async a_slow_status_answer_is_discarded(client) {
    const pending = [];
    client.respond(() => new Promise((resolve) => pending.push(resolve)));
    client.run("pollSystem()");
    client.run("pollSystem()");
    await settle();
    const requests = pending.length;

    client.give("__newer", { ...SYSTEM_OK, mandate_id: "NEWER" });
    client.give("__older", { ...SYSTEM_OK, mandate_id: "OLDER" });
    pending[1](jsonResponse(200, client.run("__newer")));
    await settle();
    const afterNewer = client.run("state").system.mandate_id;
    pending[0](jsonResponse(200, client.run("__older")));
    await settle();
    return { requests, afterNewer, afterOlder: client.run("state").system.mandate_id };
  },

  // FINDING 3 — the same rule, per panel, including the freshness stamp.
  async a_slow_panel_answer_is_discarded(client) {
    client.give("__cmd", { panel: "system", code: "SYS", title: "system", aliases: [] });
    client.run("openPanel(__cmd)");
    const panel = client.run("state.panels[0]");

    const pending = [];
    client.respond(() => new Promise((resolve) => pending.push(resolve)));
    client.run("refreshPanel(state.panels[0])");
    client.run("refreshPanel(state.panels[0])");
    await settle();
    const requests = pending.length;

    client.give("__newer", { ...SYSTEM_OK, mandate_id: "NEWER" });
    client.give("__older", { ...SYSTEM_OK, mandate_id: "OLDER" });
    pending[1](jsonResponse(200, client.run("__newer")));
    await settle();
    const afterNewer = panel.data.mandate_id;
    const stampedAt = panel.lastOkAt;
    pending[0](jsonResponse(200, client.run("__older")));
    await settle();
    return { requests, afterNewer, afterOlder: panel.data.mandate_id, stampMoved: panel.lastOkAt !== stampedAt };
  },

  // FINDING 4 — the client must not invite a note the backend will refuse.
  async acknowledging_requires_a_note(client) {
    const state = client.run("state");
    client.give("__system", SYSTEM_OK);
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
    client.give("__alert", { id: 5, severity: "CRITICAL", kind: "k", summary: "s", occurrences: 1, raised_at: "2026-07-26T12:00:00+00:00", delivered: true, acknowledged: false });
    const host = client.run("acknowledgeAction(__alert)");
    const parts = openConfirm(client, host);

    parts.typed.value = "ACK 5";
    parts.typed.dispatch("input");
    const withoutNote = parts.confirm.disabled;
    parts.note.value = "seen it";
    parts.note.dispatch("input");
    return { labels: parts.labels, withoutNote, withNote: parts.confirm.disabled };
  },

  // FINDING 5 / 8 — `revoked: false` is a 200 and is not a success; the id travels.
  async nothing_in_force_is_not_a_success(client) {
    const state = client.run("state");
    client.give("__system", SYSTEM_OK);
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
    client.give("__mandate", { mandate_known: true, mandate_id: "M-1" });
    const detail = "no unrevoked activation of this mandate existed, so nothing changed";
    client.respond(() => Promise.resolve(jsonResponse(200, { revoked: false, mandate_id: "M-1", detail })));

    const host = client.run("revokeAction(__mandate)");
    const parts = openConfirm(client, host);
    parts.note.value = "standing down";
    parts.typed.value = "REVOKE MANDATE";
    parts.typed.dispatch("input");
    await Promise.all(parts.form.dispatch("submit"));
    await settle();
    return {
      why: parts.why.textContent,
      body: client.calls[client.calls.length - 1].body,
      resultClass: parts.result.className,
      resultText: parts.result.textContent,
    };
  },

  // FINDING 5 — a real success carries the route's own words, not this file's.
  async an_accepted_action_carries_the_route_detail(client) {
    const state = client.run("state");
    client.give("__system", SYSTEM_OK);
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
    const revokeDetail = "authority is withdrawn; a cycle already in flight finishes";
    const ackDetail = "the condition that raised the alert is unchanged and may raise it again";

    client.give("__mandate", { mandate_known: true, mandate_id: "M-1" });
    client.respond(() => Promise.resolve(jsonResponse(200, { revoked: true, mandate_id: "M-1", detail: revokeDetail })));
    const revoke = openConfirm(client, client.run("revokeAction(__mandate)"));
    revoke.note.value = "standing down";
    revoke.typed.value = "REVOKE MANDATE";
    revoke.typed.dispatch("input");
    await Promise.all(revoke.form.dispatch("submit"));
    await settle();

    client.give("__alert", { id: 7, severity: "WARNING", kind: "k", summary: "s", occurrences: 1, raised_at: "2026-07-26T12:00:00+00:00", delivered: true, acknowledged: false });
    client.respond(() => Promise.resolve(jsonResponse(200, { acknowledged: true, alert_id: 7, detail: ackDetail })));
    const ack = openConfirm(client, client.run("acknowledgeAction(__alert)"));
    ack.note.value = "seen it";
    ack.note.dispatch("input");
    ack.typed.value = "ACK 7";
    ack.typed.dispatch("input");
    await Promise.all(ack.form.dispatch("submit"));
    await settle();

    return {
      revoke: { className: revoke.result.className, text: revoke.result.textContent },
      acknowledge: { className: ack.result.className, text: ack.result.textContent },
    };
  },

  // FINDING 8 — a 409 from the backend must reach the operator intact.
  async a_conflicting_grant_is_reported(client) {
    const state = client.run("state");
    client.give("__system", SYSTEM_OK);
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
    client.give("__mandate", { mandate_known: true, mandate_id: "M-1" });
    const detail = "the grant in force is M-2, not the M-1 this request named; nothing was revoked";
    client.respond(() => Promise.resolve(jsonResponse(409, { detail })));

    const parts = openConfirm(client, client.run("revokeAction(__mandate)"));
    parts.note.value = "standing down";
    parts.typed.value = "REVOKE MANDATE";
    parts.typed.dispatch("input");
    await Promise.all(parts.form.dispatch("submit"));
    await settle();
    return { className: parts.result.className, text: parts.result.textContent };
  },

  // FINDING 6 — an undecodable payload supports no claim about sizing.
  async an_undecodable_entry_claims_nothing(client) {
    const entry = (over) => ({
      sequence: 1, recorded_at: "2026-07-26T12:00:00+00:00", stage: "REFUSED", refusal: "",
      detail: "", detail_truncated: false, decision_id: "", quantity: null, limit_price: null,
      decoded: true, ...over,
    });
    client.give("__damaged", {
      entries: [entry({ sequence: 9, decoded: false })],
      stream_present: true, limit: 40, account_fingerprint: "a".repeat(64),
      generated_at: "2026-07-26T12:00:00+00:00",
    });
    client.give("__unsized", {
      entries: [entry({ sequence: 8, decision_id: "d-1" })],
      stream_present: true, limit: 40, account_fingerprint: "a".repeat(64),
      generated_at: "2026-07-26T12:00:00+00:00",
    });
    const draw = (name) => {
      client.run(`(() => { __out = document.createElement("div"); renderJournal(${name}, __out); })()`);
      const out = client.run("__out");
      return { text: out.textContent, unknowns: out.queryAll(".unknown").map((node) => node.textContent) };
    };
    return { damaged: draw("__damaged"), unsized: draw("__unsized") };
  },

  // FINDING 7 — no owner action before authority is positively observed.
  async actions_wait_for_observed_authority(client) {
    const state = client.run("state");
    client.give("__system", SYSTEM_OK);
    client.give("__readOnly", { ...SYSTEM_OK, read_only: true });
    const reason = () => client.run("actionBlockedReason()");

    Object.assign(state, { system: null, systemStatus: "loading" });
    const booting = reason();
    Object.assign(state, { system: client.run("__system"), systemStatus: "stale" });
    const stale = reason();
    Object.assign(state, { system: client.run("__readOnly"), systemStatus: "ok" });
    const readOnly = reason();
    Object.assign(state, { system: client.run("__system"), systemStatus: "ok" });
    const writer = reason();

    // And the button that reason produces must be disabled, not absent.
    Object.assign(state, { system: null, systemStatus: "loading" });
    client.give("__mandate", { mandate_known: true, mandate_id: "M-1" });
    const host = client.run("revokeAction(__mandate)");
    return {
      booting, stale, readOnly, writer,
      copy: { statusUnknown: client.run("COPY.statusUnknown"), readOnly: client.run("COPY.readOnly") },
      button: { text: host.children[0].textContent, disabled: host.children[0].disabled, title: host.children[0].title },
      hostText: host.textContent,
    };
  },

  // FINDING 9 — a server-supplied panel name may not reach Object.prototype.
  async an_inherited_panel_name_is_refused(client) {
    // Null-prototype, because `out.__proto__ = …` on an ordinary object sets the
    // prototype instead of a key — the same footgun the client is being tested for.
    const out = Object.create(null);
    for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      client.give("__cmd", { panel: name, code: "X", title: "t", aliases: [] });
      client.run("openPanel(__cmd)");
      out[name] = { panels: client.run("state.panels.length"), message: client.node("cmd-msg").textContent };
    }
    client.give("__cmd", { panel: "system", code: "SYS", title: "system", aliases: [] });
    client.run("openPanel(__cmd)");
    out.real = { panels: client.run("state.panels.length") };
    return out;
  },

  // FINDING 10 — a null critical count is not a measured zero.
  async the_alert_badge_separates_unknown_from_zero(client) {
    const state = client.run("state");
    const draw = (pending, critical) => {
      client.give("__system", { ...SYSTEM_OK, alerts_unacknowledged: pending, alerts_critical: critical });
      Object.assign(state, { system: client.run("__system"), systemStatus: "ok", systemAt: Date.now() });
      client.run("drawStatus()");
      const node = client.node("st-alerts");
      return { text: node.textContent, className: node.className };
    };
    return {
      quiet: draw(0, 0),
      pendingOnly: draw(3, 0),
      critical: draw(2, 1),
      criticalUnknown: draw(3, null),
      quietButUnknown: draw(0, null),
      allUnknown: draw(null, null),
    };
  },

  // FINDING 11 — "no cycles" is a claim the unscoped backend cannot make.
  async an_unscoped_journal_says_scope_unknown(client) {
    const draw = (fingerprint) => {
      client.give("__journal", {
        entries: [], stream_present: false, limit: 40,
        account_fingerprint: fingerprint, generated_at: "2026-07-26T12:00:00+00:00",
      });
      client.run('(() => { __out = document.createElement("div"); renderJournal(__journal, __out); })()');
      const out = client.run("__out");
      return { text: out.textContent, titles: out.queryAll(".state-title").map((node) => node.textContent) };
    };
    return { unscoped: draw(null), scoped: draw("b".repeat(64)) };
  },

  // The CountersView contract change: unobserved equity is not a zero drawdown.
  async equity_that_was_never_observed_is_unknown(client) {
    const base = {
      session_date: "2026-07-26", counters_recorded: true, realized_loss_usd: "0",
      orders_submitted: 4, cancellations: 0, replacements: 0, turnover_usd: "1200",
      account_fingerprint: "c".repeat(64), generated_at: "2026-07-26T12:00:00+00:00",
    };
    const draw = (over) => {
      client.give("__counters", { ...base, ...over });
      client.run('(() => { __out = document.createElement("div"); renderCounters(__counters, __out); })()');
      const out = client.run("__out");
      return { text: out.textContent, unknowns: out.queryAll(".unknown").map((node) => node.textContent) };
    };
    return {
      unobserved: draw({
        equity_observed: false, drawdown_usd: null, drawdown_pct: null,
        peak_equity_usd: null, trough_equity_usd: null,
      }),
      observed: draw({
        equity_observed: true, drawdown_usd: "10", drawdown_pct: "0.5",
        peak_equity_usd: "1000", trough_equity_usd: "990",
      }),
    };
  },
};

// ---------------------------------------------------------------------- main

async function main() {
  const [sourcePath, scenario] = process.argv.slice(2);
  if (!Object.prototype.hasOwnProperty.call(scenarios, scenario)) {
    throw new Error(`no such scenario: ${scenario}`);
  }
  const client = load(sourcePath);
  process.stdout.write(JSON.stringify(await scenarios[scenario](client)));
}

main().then(
  () => process.exit(0),
  (error) => {
    process.stderr.write(String((error && error.stack) || error));
    process.exit(1);
  },
);
