/**
 * The Chronos operator terminal, browser side (M8a, ADR-0018).
 *
 * A renderer, and nothing else. The backend declares the command registry and
 * every panel's read model; this file fetches those and draws them. It knows the
 * name of exactly two mutating routes — acknowledge an alert, revoke a mandate —
 * which it reaches by POSTing to the existing endpoints, and it decides nothing
 * about either. ADR-0018 §4: a window and a set of owner buttons, never a
 * gateway. No order is constructed here, and no displayed narrative is ever read
 * back into anything.
 *
 * **No framework, no bundler, no import from anywhere.** The page is served
 * same-origin by FastAPI to a browser on loopback, beside the process that holds
 * the broker session. A CDN import would put an outbound request in that page's
 * path and break the offline requirement; an npm tree would grow R-15's supply
 * chain next to the thing that moves money. The cost is more explicit DOM code
 * than React would need, bounded by `PANELS` — the one table where a panel's
 * endpoint and its renderer meet.
 *
 * **Every server string is text.** Nothing here assigns `innerHTML`,
 * `outerHTML`, `insertAdjacentHTML` or `document.write`; text reaches the DOM
 * only via `textContent` (`el`, `setText`) or the `title` property.
 * `JournalEntryView.detail` and `QueueEntryView.refusal` carry model- and
 * worker-derived narrative (R-30, ADR-0018 residual 6). This terminal is meant
 * to be a place injected text can be *seen* and must never be a place it can
 * *act* — see the comment in `renderJournal`.
 *
 * **Honesty is a rendering rule.** The Python models return `null` for anything
 * unobserved; `null` draws as the styled word "unknown", never as `0` and never
 * as a dash that could be read as a figure. Every panel carries its own
 * freshness — the moment of its last successful load, plus an explicit STALE
 * (older data, labelled) or UNAVAILABLE (never loaded) state. A frozen number
 * that still looks live is the failure this machinery exists to prevent.
 *
 * ## Rejected
 *
 * - **Caching authority.** Kill-switch, arm and mandate state are re-read every
 *   poll and never remembered across a failure. A cached "kill switch: clear"
 *   outliving a backend outage is the most dangerous string this page could draw.
 * - **Optimistic updates.** Acknowledging does not remove the row locally; the
 *   panel re-polls. The owner is entitled to see that the write landed.
 * - **Hiding owner actions on a read-only backend.** They render disabled with
 *   the reason stated: a button that vanishes teaches nothing.
 *
 * ## Honest residuals
 *
 * 1. **The grammar is implemented twice.** `parseLine` mirrors
 *    `chronos.terminal.commands.parse` because there is no `/terminal/parse`
 *    route and a round trip per keystroke is a poor trade. Python is
 *    authoritative; if they disagree, this one is wrong. Only the *rule* is
 *    duplicated — the registry itself is never hard-coded here.
 * 2. **Polling, not streaming** (ADR-0018 residual 2): N panels are N requests
 *    per interval, disclosed in each panel header.
 * 3. **The workspace persists panels, not data.** After a reload every panel is
 *    UNAVAILABLE until its first fetch returns. Nothing in `localStorage` is
 *    evidence of anything current.
 * 4. **One panel per panel id.** Re-running a command focuses the open panel;
 *    no panel takes a symbol yet, so a second would differ only in position.
 */

// ------------------------------------------------------------------- contract

const API = {
  commands: "/terminal/commands",
  system: "/terminal/system",
  mandate: "/terminal/mandate",
  journal: "/terminal/journal",
  counters: "/terminal/counters",
  queue: "/terminal/queue",
  alerts: "/terminal/alerts",
  acknowledge: (id) => `/terminal/alerts/${encodeURIComponent(id)}/acknowledge`,
  revoke: "/terminal/mandate/revoke",
};

const POLL_MS = 5000;
const STATUS_MS = 5000;
const CLOCK_MS = 1000;
/** Multiples of a panel's own interval after which its data is stale even if no fetch failed. */
const STALE_INTERVALS = 3;
const HISTORY_MAX = 50;
const SUGGEST_MAX = 7;
const JOURNAL_LIMIT = 40;
const MAX_SYMBOL_LENGTH = 32;
const WORKSPACE_KEY = "chronos.terminal.workspace.v1";
/** Conventional ticker form, kept identical to `_SYMBOL_PATTERN` in commands.py. */
const SYMBOL_RE = /^\/?[A-Z][A-Z0-9]*(?:[./][A-Z0-9]+)*$/;

/**
 * What an absence means, stated once and in one place.
 *
 * These strings are the doctrine, not decoration: an empty journal, a missing
 * counter row and an unbound account all render as *nothing observed*, never as
 * calm. Keeping them together makes it possible to read every claim this
 * terminal makes about its own ignorance in one screen.
 */
const COPY = {
  noMandate:
    "This backend has no mandate in force, so no authority has been established. That is not the same as a mandate granting nothing — nothing has been granted or refused.",
  mandateActive:
    "active reports that an unrevoked activation covers now. It is a display, never a permission: admission re-derives every gate before anything trades.",
  noCycles:
    "This account has no cycle stream. An empty journal is not evidence of calm — on a fresh database it is evidence of nothing at all.",
  undecodable:
    "The stored payload would not decode, so everything derived from it is absent. The record is listed rather than hidden, because hiding it would hide the damage.",
  noCounters: (session) =>
    `Nothing has been observed for session ${session}. Absent counters mean no authority has been established over this session — they do not mean there have been no losses.`,
  queued: "A queued proposal has been received, which authorizes nothing.",
  noAlerts: (at) =>
    `No unacknowledged alerts as of ${at}. That is the state of the pending set at that moment, not a claim about the session.`,
  unscoped:
    "This backend is not bound to an account, so the set could not be read. Empty here means unknown, not quiet.",
  unknownFields:
    "A field shown as unknown was not observable by this backend. It is not zero, and an unknown kill switch is not a disengaged one.",
  acknowledging:
    "Acknowledging records that the owner saw the condition. It never deletes the alert and never clears what raised it.",
  readOnly:
    "this backend is read-only (it does not hold the writer lease); owner actions are refused here",
  statusUnknown:
    "the backend's status could not be read; refusing to offer an action whose effect cannot be checked",
  stale: (at) => `Everything below is from the load at ${at}, not from now.`,
  unavailable: "This panel has never loaded, so there is nothing it can honestly show.",
};

const state = {
  registry: [],
  registryAt: null,
  registryError: "",
  panels: [],
  focusId: null,
  seq: 0,
  history: [],
  historyIndex: -1,
  pendingSymbol: "",
  suggestions: [],
  suggestIndex: -1,
  system: null,
  systemStatus: "loading",
  systemError: "",
  systemAt: null,
  latencyMs: null,
};

const dom = {
  form: document.getElementById("cmdform"),
  input: document.getElementById("cmd"),
  symbol: document.getElementById("cmd-symbol"),
  suggest: document.getElementById("suggest"),
  msg: document.getElementById("cmd-msg"),
  grid: document.getElementById("grid"),
  empty: document.getElementById("empty"),
  emptyKeys: document.getElementById("empty-keys"),
  panels: document.getElementById("st-panels"),
  reset: document.getElementById("st-reset"),
};

// ---------------------------------------------------------------- DOM helpers

/** Build an element. Text goes in through textContent, always. */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setText(node, text) {
  node.textContent = text === undefined || text === null ? "" : String(text);
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function append(parent, ...children) {
  for (const child of children) if (child) parent.appendChild(child);
  return parent;
}

/** One label/value pair in a definition grid. */
function kv(list, label, node) {
  append(list, el("dt", null, label));
  return append(list, append(el("dd"), typeof node === "string" ? el("span", null, node) : node));
}

/** A table with its header row, returned ready for rows. */
function table(labels) {
  const wrap = el("div", "tbl-wrap");
  const node = el("table", "tbl");
  const head = el("tr");
  for (const label of labels) head.appendChild(el("th", null, label));
  node.appendChild(el("thead")).appendChild(head);
  const rows = node.appendChild(el("tbody"));
  wrap.appendChild(node);
  return { wrap, rows };
}

function section(parent, title) {
  const wrap = append(el("div", "section"), el("div", "section-title", title));
  return parent.appendChild(wrap);
}

function stateBlock(tone, title, body) {
  return append(el("div", `state state-${tone}`), el("div", "state-title", title), el("div", "state-body", body));
}

function note(text) {
  return el("p", "note", text);
}

// --------------------------------------------------------------- honest values

/**
 * The one rendering of "we do not know". Never a zero, never a dash: an operator
 * scanning a column must not be able to mistake an unobserved value for a
 * measured one.
 */
function unknown(reason = "unknown") {
  return el("span", "unknown", reason);
}

function value(raw, { absent = "unknown", blank = "empty" } = {}) {
  if (raw === null || raw === undefined) return unknown(absent);
  const text = String(raw);
  return text === "" ? unknown(blank) : el("span", null, text);
}

function chip(label, tone = "muted", hint) {
  const node = el("span", `chip chip-${tone}`, label);
  if (hint) node.title = hint;
  return node;
}

/** A boolean that may be unknown. The unknown case is styled, never defaulted. */
function triChip(raw, onTrue, onFalse, toneTrue = "ok", toneFalse = "muted") {
  if (raw === null || raw === undefined) return chip("unknown", "unknown", "the backend could not observe this");
  return raw ? chip(onTrue, toneTrue) : chip(onFalse, toneFalse);
}

function utcStamp(date) {
  const pad = (part) => String(part).padStart(2, "0");
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`;
}

/** A server timestamp as a UTC wall clock, with the exact string it sent on hover. */
function timeNode(iso) {
  if (!iso) return unknown();
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return value(iso);
  const node = el("span", null, utcStamp(parsed));
  node.title = iso;
  return node;
}

function ageText(since) {
  if (!since) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - since) / 1000));
  if (seconds < 90) return `${seconds}s ago`;
  return seconds < 5400 ? `${Math.round(seconds / 60)}m ago` : `${Math.round(seconds / 3600)}h ago`;
}

/**
 * The account fingerprint is pseudonymous and safe to display in full
 * (`chronos.utils.identifiers`); it is truncated only because 64 hex characters
 * bury the rest of the row. The whole value sits on the element's title.
 */
function fingerprintNode(fingerprint) {
  if (!fingerprint) return unknown("this backend is not bound to an account");
  const node = el("span", "fingerprint", `${String(fingerprint).slice(0, 16)}…`);
  node.title = String(fingerprint);
  return node;
}

// ------------------------------------------------------------------ transport

async function getJSON(path) {
  const started = performance.now();
  try {
    const response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" });
    const latencyMs = Math.round(performance.now() - started);
    if (!response.ok) return { ok: false, status: response.status, error: await errorText(response), latencyMs };
    return { ok: true, status: response.status, data: await response.json(), latencyMs };
  } catch (error) {
    return { ok: false, status: 0, error: unreachable(error), latencyMs: Math.round(performance.now() - started) };
  }
}

async function postJSON(path, body) {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
    if (!response.ok) return { ok: false, status: response.status, error: await errorText(response) };
    return { ok: true, status: response.status, data: await response.json().catch(() => null) };
  } catch (error) {
    return { ok: false, status: 0, error: unreachable(error) };
  }
}

function unreachable(error) {
  return `backend unreachable (${error && error.message ? error.message : "network error"})`;
}

/** The server's own words about a failure, if it offered any, behind the status. */
async function errorText(response) {
  const label = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
  try {
    const payload = await response.json();
    if (payload && typeof payload.detail === "string" && payload.detail) return `${label} — ${payload.detail}`;
  } catch {
    /* A non-JSON error body says nothing the status did not. */
  }
  return label;
}

// ------------------------------------------------------------- panel renderers

function renderSystem(data, body) {
  const list = el("dl", "kv");
  kv(list, "backend", triChip(data.read_only, "READ-ONLY", "WRITER", "warn", "ok"));
  kv(list, "autonomy", autonomyChip(data));
  kv(list, "next tick", tickNode(data));
  kv(list, "queue depth", value(data.queue_depth));
  kv(list, "alerts", alertCountNode(data));
  kv(list, "kill switch", triChip(data.kill_switch_engaged, "ENGAGED", "clear", "danger", "ok"));
  kv(list, "live arming", triChip(data.live_armed, "ARMED", "disarmed", "warn", "ok"));
  kv(list, "mandate", triChip(data.mandate_active, "active", "inactive", "ok", "muted"));
  kv(list, "mandate id", value(data.mandate_id, { absent: "no mandate loaded" }));
  kv(list, "account", fingerprintNode(data.account_fingerprint));
  kv(list, "generated", timeNode(data.generated_at));
  append(body, list, note(COPY.unknownFields));
}

function autonomyChip(data) {
  if (!data.autonomy_configured) return chip("NOT CONFIGURED", "muted", "no autonomy runtime exists in this backend");
  if (data.autonomy_stopped === null || data.autonomy_stopped === undefined) return triChip(null);
  return data.autonomy_stopped ? chip("STOPPED", "warn") : chip("RUNNING", "ok");
}

function tickNode(data) {
  if (!data.autonomy_configured) return unknown("no runtime to schedule one");
  const seconds = data.seconds_until_next_tick;
  if (seconds === null || seconds === undefined) {
    return unknown(data.autonomy_stopped ? "runtime stopped — no tick scheduled" : "unknown");
  }
  return el("span", null, `${Number(seconds).toFixed(1)}s`);
}

function alertCountNode(data) {
  const pending = data.alerts_unacknowledged;
  if (pending === null || pending === undefined) return unknown();
  const wrap = append(el("span", "wrap"), el("span", null, `${pending} unacknowledged`));
  if (data.alerts_critical === null || data.alerts_critical === undefined) return append(wrap, unknown("critical count unknown"));
  if (data.alerts_critical > 0) append(wrap, chip(`${data.alerts_critical} CRITICAL`, "danger"));
  return wrap;
}

/** How the read model says an unset limit will be read, and how loud that should be. */
const LIMIT_EFFECTS = {
  BINDS: ["ok", "the supervisor enforces this value"],
  NO_CEILING: ["danger", "unset under model discretion: no bound at all, affordability is the limit"],
  AUTHORIZES_NOTHING: ["muted", "unset without discretion: deny-by-default sizes every order to nothing"],
  NOT_ENFORCED: ["warn", "unset: nothing will ever breach this limit"],
  NO_FLOOR: ["warn", "unset: no reserve is protected"],
};

const MANDATE_SCOPES = [
  ["symbols", "symbols"],
  ["asset classes", "asset_classes"],
  ["strategies", "strategies"],
  ["order forms", "order_forms"],
];

function renderMandate(data, body) {
  const list = el("dl", "kv");
  if (!data.mandate_known) {
    append(body, stateBlock("warn", "NO MANDATE LOADED", COPY.noMandate));
    kv(list, "account", fingerprintNode(data.account_fingerprint));
    kv(list, "generated", timeNode(data.generated_at));
    return append(body, list);
  }

  kv(list, "mandate", value(data.mandate_id));
  kv(list, "version", value(data.mandate_version));
  kv(list, "mode", value(data.mode));
  kv(list, "active", triChip(data.active, "ACTIVE", "inactive", "ok", "muted"));
  kv(list, "revoked", triChip(data.revoked, "REVOKED", "no", "danger", "muted"));
  kv(list, "discretion", triChip(data.model_discretion, "MODEL DISCRETION", "bounded", "warn", "ok"));
  kv(list, "effective", timeNode(data.effective_from));
  kv(list, "expires", timeNode(data.expires_at));
  kv(list, "activated", data.activated_at ? timeNode(data.activated_at) : unknown("no activation recorded"));
  kv(list, "owner event", value(data.owner_event_id, { absent: "no activation recorded" }));
  kv(list, "account", fingerprintNode(data.account_fingerprint));
  kv(list, "generated", timeNode(data.generated_at));
  append(body, list, note(COPY.mandateActive));

  const scope = section(body, "scope");
  const scopes = el("dl", "kv");
  for (const [label, key] of MANDATE_SCOPES) kv(scopes, label, scopeNode(data[key]));
  scope.appendChild(scopes);

  const promotions = section(body, "promotions");
  if (!data.promotions) append(promotions, unknown());
  else if (!data.promotions.length) append(promotions, note("No asset family has earned a rung."));
  else {
    const wrap = el("div", "wrap");
    for (const item of data.promotions) append(wrap, chip(`${item.asset_class} ${item.level}`, "info"));
    append(promotions, wrap);
  }

  limitsSection(body, "floors — reserves the model may not spend into", data.floors);
  limitsSection(body, "ceilings", data.ceilings);
  return append(body, revokeAction(data));
}

function scopeNode(values) {
  if (!values) return unknown();
  if (!values.length) return unknown("none declared");
  const wrap = el("div", "wrap");
  for (const item of values) append(wrap, chip(item, "info"));
  return wrap;
}

function limitsSection(body, title, limits) {
  const host = section(body, title);
  if (!limits) return append(host, unknown());
  const { wrap, rows } = table(["limit", "value", "effect"]);
  for (const limit of limits) {
    const [tone, hint] = LIMIT_EFFECTS[limit.effect] || ["muted", ""];
    const row = append(el("tr"), el("td", null, limit.name));
    append(row, append(el("td"), limit.value === null || limit.value === undefined ? unknown("unset") : el("span", null, limit.value)));
    append(rows, append(row, append(el("td"), chip(limit.effect, tone, hint))));
  }
  return append(host, wrap);
}

function renderJournal(data, body) {
  append(body, note(`${data.entries.length} record${data.entries.length === 1 ? "" : "s"}, newest first · limit ${data.limit}`));
  if (!data.stream_present) return append(body, stateBlock("warn", "NO CYCLES RECORDED", COPY.noCycles));

  for (const entry of data.entries) {
    const article = el("article", "entry");
    const head = append(el("div", "entry-head"), el("span", "entry-seq", `#${entry.sequence}`), timeNode(entry.recorded_at));
    append(head, chip(entry.stage, entry.refusal ? "warn" : "info"));
    if (entry.refusal) append(head, chip(entry.refusal, "danger"));
    if (!entry.decoded) append(head, chip("PAYLOAD UNDECODABLE", "danger", "the hash-chain record is damaged"));
    append(article, head);
    if (!entry.decoded) append(article, note(COPY.undecodable));

    if (entry.detail) {
      // Model- and worker-derived narrative (R-30, ADR-0018 residual 6). It is
      // assigned through textContent, so markup inside it is inert: the operator
      // sees the characters that were stored and the browser interprets none of
      // them. It is also never read back into an order parameter — this element
      // is the end of its journey, not a waypoint.
      append(article, el("p", "narrative", entry.detail));
      if (entry.detail_truncated) append(article, chip("detail truncated by the backend", "warn"));
    }

    const facts = entry.decision_id ? [`decision ${entry.decision_id}`] : [];
    facts.push(entry.quantity === null ? "quantity: not sized" : `quantity ${entry.quantity}`);
    facts.push(entry.limit_price === null ? "limit: not compiled" : `limit ${entry.limit_price}`);
    append(body, append(article, note(facts.join(" · "))));
  }
  return body;
}

const COUNTER_FIELDS = [
  ["realized loss", "realized_loss_usd"], ["drawdown", "drawdown_usd"], ["drawdown %", "drawdown_pct"],
  ["peak equity", "peak_equity_usd"], ["trough equity", "trough_equity_usd"], ["orders", "orders_submitted"],
  ["cancellations", "cancellations"], ["replacements", "replacements"], ["turnover", "turnover_usd"],
];

function renderCounters(data, body) {
  const list = el("dl", "kv");
  kv(list, "session", value(data.session_date));
  if (!data.counters_recorded) {
    append(body, stateBlock("warn", "NO COUNTERS RECORDED", COPY.noCounters(data.session_date)));
  } else {
    for (const [label, key] of COUNTER_FIELDS) kv(list, label, value(data[key]));
  }
  kv(list, "account", fingerprintNode(data.account_fingerprint));
  kv(list, "generated", timeNode(data.generated_at));
  return append(body, list, data.counters_recorded ? note("Figures are what the supervisor itself observed this session, in USD.") : null);
}

function renderQueue(data, body) {
  const list = el("dl", "kv");
  kv(list, "pending depth", value(data.pending_depth));
  kv(list, "account", fingerprintNode(data.account_fingerprint));
  kv(list, "generated", timeNode(data.generated_at));
  append(body, list, note(COPY.queued));

  const recent = section(body, `recent arrivals · limit ${data.limit}`);
  if (!data.recent.length) {
    return append(recent, data.account_fingerprint ? note("No proposals recorded for this account.") : unknown(COPY.unscoped));
  }
  const { wrap, rows } = table(["queued", "status", "stage", "refusal"]);
  for (const entry of data.recent) {
    const row = append(el("tr"), append(el("td"), timeNode(entry.queued_at)), el("td", null, entry.status));
    append(row, append(el("td"), entry.cycle_stage ? el("span", null, entry.cycle_stage) : unknown("not yet judged")));
    // Refusal text is Chronos- or model-authored; textContent, never markup.
    append(rows, append(row, el("td", "wrapcell", entry.refusal || "")));
  }
  return append(recent, wrap);
}

const SEVERITY_TONE = { CRITICAL: "danger", WARNING: "warn", INFO: "info" };

function renderAlerts(data, body) {
  if (!data.alerts.length) {
    return append(
      body,
      data.account_fingerprint
        ? stateBlock("info", "NOTHING PENDING", COPY.noAlerts(data.generated_at))
        : stateBlock("warn", "SCOPE UNKNOWN", COPY.unscoped),
    );
  }
  for (const alert of data.alerts) {
    const head = append(el("div", "entry-head"), chip(alert.severity, SEVERITY_TONE[alert.severity] || "muted"));
    append(head, el("span", null, alert.kind), timeNode(alert.raised_at));
    append(
      head,
      alert.delivered
        ? chip("delivered", "muted")
        : chip("NOT DELIVERED", "warn", "the owner was never told — an alert nobody saw is not an alert"),
    );
    if (alert.occurrences > 1) append(head, chip(`×${alert.occurrences}`, "muted", "times the condition recurred"));
    // Summaries are Chronos-authored, but they take the same path as any server
    // string: text, never markup.
    const article = append(el("article", "entry"), head, el("p", "narrative", alert.summary));
    append(body, append(article, acknowledgeAction(alert)));
  }
  return append(body, note(COPY.acknowledging));
}

function renderHelp(_data, body) {
  if (!state.registry.length) {
    return append(body, stateBlock("bad", "REGISTRY UNAVAILABLE", state.registryError || "the command registry has not loaded"));
  }
  const { wrap, rows } = table(["cmd", "aliases", "panel", "what it answers"]);
  for (const command of state.registry) {
    const row = append(el("tr"), el("td", null, command.code), el("td", null, (command.aliases || []).join(" ") || "—"));
    append(row, el("td", null, command.panel));
    const summary = append(el("td", "wrapcell"), el("div", null, command.title), el("div", "note", command.summary));
    append(rows, append(row, summary));
  }
  const at = state.registryAt ? utcStamp(new Date(state.registryAt)) : "unknown";
  return append(body, wrap, note(`Registry served by the backend at ${at}. No command exists that is not in it.`));
}

/**
 * The whole panel surface: panel id to endpoint and renderer. Adding a panel is
 * a row here plus a renderer — the command that opens it, its title and its help
 * text all arrive from the backend's registry.
 */
const PANELS = {
  system: { endpoint: () => API.system, render: renderSystem },
  mandate: { endpoint: () => API.mandate, render: renderMandate },
  journal: { endpoint: () => `${API.journal}?limit=${JOURNAL_LIMIT}`, render: renderJournal },
  counters: { endpoint: () => API.counters, render: renderCounters },
  queue: { endpoint: () => API.queue, render: renderQueue },
  alerts: { endpoint: () => API.alerts, render: renderAlerts },
  help: { local: true, render: renderHelp },
};

// -------------------------------------------------------------- owner actions

function labelledInput(form, text, options = {}) {
  const id = `f${Math.random().toString(36).slice(2, 9)}`;
  const label = el("label", null, text);
  label.htmlFor = id;
  const input = el("input");
  Object.assign(input, { id, type: "text", autocomplete: "off" }, options);
  append(form, label, input);
  return input;
}

/**
 * The typed-confirmation gate on the two mutating routes.
 *
 * The phrase is a local friction device: it is compared here and never sent,
 * stored, or logged — what crosses the wire is only the note or reason the owner
 * wrote. A misclick cannot revoke a mandate, because a click is not the last
 * thing that has to happen.
 */
function confirmForm({ heading, why, phrase, noteLabel, noteRequired, submitLabel, danger, submit, done }) {
  const form = append(el("form", danger ? "confirm confirm-danger" : "confirm"), el("div", "confirm-head", heading));
  append(form, el("p", "confirm-why", why));
  const noteField = labelledInput(form, noteLabel, { maxLength: 500 });
  const typed = labelledInput(form, `type ${phrase} to confirm`);

  const confirm = el("button", danger ? "btn btn-danger" : "btn", submitLabel);
  confirm.type = "submit";
  confirm.disabled = true;
  const cancel = el("button", "btn", "CANCEL");
  cancel.type = "button";
  append(form, append(el("div", "confirm-actions"), confirm, cancel));
  const result = el("div", "confirm-result");
  result.setAttribute("role", "status");
  append(form, result);

  const matches = () => typed.value.trim().replace(/\s+/g, " ").toUpperCase() === phrase;
  const revalidate = () => {
    confirm.disabled = !matches() || (noteRequired && !noteField.value.trim());
  };
  typed.addEventListener("input", revalidate);
  noteField.addEventListener("input", revalidate);
  cancel.addEventListener("click", () => done(false));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!matches()) return;
    confirm.disabled = true;
    cancel.disabled = true;
    result.className = "confirm-result";
    setText(result, "sending…");
    const outcome = await submit(noteField.value.trim());
    if (outcome.ok) {
      result.className = "confirm-result ok";
      setText(result, "accepted by the backend — re-reading state");
      done(true);
      return;
    }
    result.className = "confirm-result bad";
    setText(result, `refused: ${outcome.error}`);
    cancel.disabled = false;
    revalidate();
  });

  window.setTimeout(() => noteField.focus(), 0);
  return form;
}

/** Why an owner action cannot be offered right now, or an empty string. */
function actionBlockedReason() {
  if (state.system && state.system.read_only === true) return COPY.readOnly;
  if (state.systemStatus === "unavailable") return COPY.statusUnknown;
  return "";
}

/**
 * An owner action, with its confirmation held open against the poll.
 *
 * Opening a confirmation freezes its panel's refresh: without that, the poll
 * would rebuild the body underneath the operator and discard a half-typed
 * revocation reason, and a UI that eats the reason teaches people to type
 * something shorter. The freeze shows in the header as HELD rather than hiding,
 * because a panel that has stopped refreshing must say so even when it stopped
 * on purpose.
 */
function ownerAction(label, danger, buildForm) {
  const host = el("div", "actions");
  const button = el("button", danger ? "btn btn-danger" : "btn", label);
  button.type = "button";
  const blocked = actionBlockedReason();
  if (blocked) {
    button.disabled = true;
    button.title = blocked;
    return append(host, button, note(blocked));
  }
  button.addEventListener("click", () => {
    const owner = panelOf(host);
    if (owner) {
      owner.frozen = true;
      updateFreshness(owner);
    }
    button.hidden = true;
    append(
      host,
      buildForm((accepted) => {
        const form = host.querySelector("form");
        if (form) form.remove();
        button.hidden = false;
        if (owner) owner.frozen = false;
        if (accepted) refreshAll();
        else if (owner) updateFreshness(owner);
      }),
    );
  });
  return append(host, button);
}

/** The open panel a node belongs to, or null. */
function panelOf(node) {
  const host = node.closest ? node.closest(".panel") : null;
  return host ? state.panels.find((panel) => panel.id === host.id) || null : null;
}

function acknowledgeAction(alert) {
  return ownerAction(`ACK #${alert.id}`, false, (done) =>
    confirmForm({
      heading: `Acknowledge alert #${alert.id}`,
      why: "This records that the owner has seen the condition. It does not resolve the condition, delete the alert, or stop it recurring.",
      phrase: `ACK ${alert.id}`,
      noteLabel: "note (optional, recorded with the acknowledgement)",
      noteRequired: false,
      submitLabel: "ACKNOWLEDGE",
      danger: false,
      done,
      submit: (text) => postJSON(API.acknowledge(alert.id), { note: text }),
    }),
  );
}

function revokeAction(mandate) {
  const named = mandate.mandate_id ? ` ${mandate.mandate_id}` : "";
  return ownerAction("REVOKE MANDATE", true, (done) =>
    confirmForm({
      heading: "Revoke the active mandate",
      why: `This withdraws the owner's grant${named}. The supervisor loses its authority to act and the revocation is audited. It cannot be undone from this terminal.`,
      phrase: "REVOKE MANDATE",
      noteLabel: "reason (required, recorded)",
      noteRequired: true,
      submitLabel: "REVOKE",
      danger: true,
      done,
      submit: (reason) => postJSON(API.revoke, { reason }),
    }),
  );
}

// ------------------------------------------------------------- panel plumbing

function openPanel(command) {
  const spec = PANELS[command.panel];
  if (!spec) return message(`the backend declares panel "${command.panel}", which this client cannot draw`, "bad");
  const existing = state.panels.find((panel) => panel.panel === command.panel);
  if (existing) {
    focusPanel(existing.id);
    return refreshPanel(existing);
  }
  const panel = {
    id: `panel-${++state.seq}`,
    panel: command.panel,
    code: command.code,
    title: command.title,
    spec,
    interval: POLL_MS,
    data: null,
    status: "loading",
    error: "",
    lastOkAt: null,
    timer: 0,
    frozen: false,
  };
  state.panels.push(panel);
  mountPanel(panel);
  focusPanel(panel.id);
  refreshPanel(panel);
  if (!spec.local) panel.timer = window.setInterval(() => refreshPanel(panel), panel.interval);
  saveWorkspace();
  return syncChrome();
}

function mountPanel(panel) {
  const node = el("section", "panel");
  node.id = panel.id;
  node.tabIndex = -1;
  node.setAttribute("aria-label", `${panel.code} ${panel.title}`);

  const num = el("button", "panel-num", "?");
  num.type = "button";
  num.title = "Focus this panel (Alt+number)";
  num.addEventListener("click", () => focusPanel(panel.id));
  const close = el("button", "panel-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", `Close ${panel.code}`);
  close.addEventListener("click", () => closePanel(panel.id));
  const meta = el("span", "panel-meta");
  const freshness = el("span", "chip chip-unknown", "LOADING");
  const head = append(el("header", "panel-head"), num, el("span", "panel-code", panel.code));
  append(head, el("span", "panel-title", panel.title), meta, freshness, close);

  const body = el("div", "panel-body");
  node.addEventListener("mousedown", () => focusPanel(panel.id));
  dom.grid.appendChild(append(node, head, body));
  Object.assign(panel, { node, body, meta, freshness, numNode: num });
}

async function refreshPanel(panel) {
  if (panel.frozen) return;
  if (panel.spec.local) {
    panel.data = state.registry;
    panel.lastOkAt = state.registryAt || Date.now();
    panel.status = state.registry.length ? "ok" : "unavailable";
    panel.error = state.registryError;
    drawPanel(panel);
    return;
  }
  const outcome = await getJSON(panel.spec.endpoint());
  if (!state.panels.includes(panel) || panel.frozen) return;
  if (outcome.ok) {
    Object.assign(panel, { data: outcome.data, lastOkAt: Date.now(), status: "ok", error: "" });
  } else {
    panel.status = panel.data ? "stale" : "unavailable";
    panel.error = outcome.error;
  }
  drawPanel(panel);
}

function drawPanel(panel) {
  // Redrawing wholesale is the simplest correct thing, but it throws away where
  // the operator had scrolled to. Restoring the offset is the difference between
  // a journal you can read and one that jumps every five seconds.
  const offset = panel.body.scrollTop;
  clear(panel.body);
  if (panel.status === "unavailable") {
    append(panel.body, stateBlock("bad", "DATA UNAVAILABLE", `${panel.error || "the panel could not be loaded"}. ${COPY.unavailable}`));
    return updateFreshness(panel);
  }
  if (panel.status === "stale") {
    const at = panel.lastOkAt ? utcStamp(new Date(panel.lastOkAt)) : "an earlier time";
    append(panel.body, stateBlock("warn", "STALE", `${panel.error || "the last refresh did not succeed"}. ${COPY.stale(at)}`));
  }
  if (panel.data) {
    try {
      panel.spec.render(panel.data, panel.body);
    } catch (error) {
      const why = error && error.message ? error.message : "unknown error";
      append(panel.body, stateBlock("bad", "RENDER FAILED", `${why}. The backend answered but this client could not draw the answer, so nothing below it is being shown.`));
    }
  }
  panel.body.scrollTop = offset;
  return updateFreshness(panel);
}

const FRESH_TONE = { ok: "chip chip-ok", stale: "chip chip-warn", unavailable: "chip chip-danger" };
const FRESH_LABEL = { ok: "LIVE", stale: "STALE", unavailable: "UNAVAIL" };

/**
 * The freshness contract, re-evaluated every second.
 *
 * A panel that has not loaded within `STALE_INTERVALS` of its own period is
 * demoted to STALE even though no fetch failed: a throttled tab, a suspended
 * laptop and a wedged event loop all produce numbers that are silently old, and
 * silently old is the one thing a supervision panel may never be.
 */
function updateFreshness(panel) {
  if (!panel.node) return;
  const overdue = panel.status === "ok" && !panel.spec.local && panel.lastOkAt && Date.now() - panel.lastOkAt > panel.interval * STALE_INTERVALS;
  if (overdue && !panel.frozen) {
    panel.status = "stale";
    panel.error = `no successful load in ${Math.round((Date.now() - panel.lastOkAt) / 1000)}s`;
    drawPanel(panel);
    return;
  }
  panel.freshness.className = panel.frozen ? "chip chip-warn" : FRESH_TONE[panel.status] || "chip chip-unknown";
  setText(panel.freshness, panel.frozen ? "HELD" : FRESH_LABEL[panel.status] || "LOADING");
  panel.freshness.title = panel.frozen
    ? "refresh paused while a confirmation is open — these values are from the last load"
    : panel.error || "";
  const cadence = panel.frozen ? "paused" : panel.spec.local ? "static" : `every ${Math.round(panel.interval / 1000)}s`;
  const last = panel.lastOkAt ? `${utcStamp(new Date(panel.lastOkAt))} (${ageText(panel.lastOkAt)})` : "never";
  setText(panel.meta, `${cadence} · last ok ${last}`);
}

function closePanel(id) {
  const index = state.panels.findIndex((panel) => panel.id === id);
  if (index < 0) return;
  const [panel] = state.panels.splice(index, 1);
  if (panel.timer) window.clearInterval(panel.timer);
  if (panel.node) panel.node.remove();
  if (state.focusId === id) state.focusId = state.panels.length ? state.panels[Math.max(0, index - 1)].id : null;
  saveWorkspace();
  syncChrome();
}

function focusPanel(id) {
  state.focusId = id;
  for (const panel of state.panels) if (panel.node) panel.node.classList.toggle("focused", panel.id === id);
  const target = state.panels.find((panel) => panel.id === id);
  if (target && target.node) target.node.scrollIntoView({ block: "nearest" });
}

function refreshAll() {
  for (const panel of state.panels) refreshPanel(panel);
  pollSystem();
}

/** Panel numbering and the empty state are derived, never stored. */
function syncChrome() {
  state.panels.forEach((panel, index) => {
    if (panel.numNode) setText(panel.numNode, index < 9 ? String(index + 1) : "·");
  });
  dom.empty.hidden = state.panels.length > 0;
  setText(dom.panels, `${state.panels.length} panel${state.panels.length === 1 ? "" : "s"}`);
}

// ---------------------------------------------------------------- persistence

function saveWorkspace() {
  try {
    window.localStorage.setItem(WORKSPACE_KEY, JSON.stringify({ version: 1, panels: state.panels.map((panel) => panel.panel) }));
  } catch {
    /* Storage can be absent or full. A workspace that cannot be saved is not a
       reason to stop supervising. */
  }
}

function restoreWorkspace() {
  let stored = null;
  try {
    stored = JSON.parse(window.localStorage.getItem(WORKSPACE_KEY) || "null");
  } catch {
    stored = null;
  }
  if (!stored || !Array.isArray(stored.panels)) return;
  for (const id of stored.panels) {
    const command = state.registry.find((entry) => entry.panel === id);
    if (command) openPanel(command);
  }
}

// ------------------------------------------------------------------ status bar

async function pollSystem() {
  const outcome = await getJSON(API.system);
  state.latencyMs = outcome.latencyMs;
  if (outcome.ok) {
    Object.assign(state, { system: outcome.data, systemStatus: "ok", systemError: "", systemAt: Date.now() });
  } else {
    state.systemStatus = state.system ? "stale" : "unavailable";
    state.systemError = outcome.error;
  }
  drawStatus();
  if (!state.registry.length) loadRegistry();
}

function badge(id, text, className, { hidden = false, title = "" } = {}) {
  const node = document.getElementById(id);
  node.hidden = hidden;
  node.className = className;
  node.title = title;
  setText(node, text);
}

function drawStatus() {
  const system = state.system;
  const status = state.systemStatus;
  const seen = state.systemAt ? utcStamp(new Date(state.systemAt)) : "never";
  const conn = { ok: ["● BACKEND OK", "st st-ok"], stale: ["● BACKEND STALE", "st st-warn"] }[status] || [
    "● BACKEND UNREACHABLE",
    "st st-bad",
  ];
  badge("st-conn", conn[0], conn[1], { title: `${state.systemError ? `${state.systemError} · ` : ""}last read ${seen}` });

  const readOnly = system ? system.read_only : null;
  const role = readOnly === null || readOnly === undefined ? ["ROLE UNKNOWN", "st st-warn"] : readOnly ? ["READ-ONLY", "st st-warn"] : ["WRITER", "st st-ok"];
  badge("st-role", role[0], role[1], { title: "whether this backend holds the single-writer lease" });

  let autonomy = ["AUTONOMY UNKNOWN", "st st-warn"];
  if (system && !system.autonomy_configured) autonomy = ["AUTONOMY NOT CONFIGURED", "st st-dim"];
  else if (system && system.autonomy_stopped === true) autonomy = ["AUTONOMY STOPPED", "st st-warn"];
  else if (system && system.autonomy_stopped === false) autonomy = ["AUTONOMY RUNNING", "st st-ok"];
  badge("st-autonomy", autonomy[0], autonomy[1]);

  // Engaged is loud and unknown is nearly as loud; only a positively observed
  // "disengaged" is allowed to be silent.
  const kill = system ? system.kill_switch_engaged : null;
  badge("st-kill", kill ? "KILL SWITCH ENGAGED" : "KILL SWITCH UNKNOWN", kill ? "st badge-kill" : "st badge-unknown", {
    hidden: kill === false,
    title: "the durable kill switch",
  });

  const armed = system ? system.live_armed : null;
  badge("st-armed", armed ? "LIVE ARMED" : "ARM STATE UNKNOWN", armed ? "st badge-armed" : "st badge-unknown", {
    hidden: armed === false,
    title: "whether the live session is armed",
  });

  const pending = system ? system.alerts_unacknowledged : null;
  const critical = system ? system.alerts_critical : null;
  const alertsText = pending === null || pending === undefined ? "ALERTS UNKNOWN" : `${pending} ALERT${pending === 1 ? "" : "S"}${critical ? ` · ${critical} CRITICAL` : ""}`;
  const alertsTone = critical ? "st-bad" : pending ? "st-warn" : pending === 0 ? "st-dim" : "st-warn";
  badge("st-alerts", alertsText, `st ${alertsTone}`, { title: "unacknowledged owner alerts" });

  const slow = state.latencyMs !== null && state.latencyMs > 500;
  badge("st-latency", state.latencyMs === null ? "— ms" : `${state.latencyMs} ms`, `st ${slow ? "st-warn" : "st-dim"}`, {
    title: "round trip of the status poll",
  });
}

function tickClock() {
  const now = new Date();
  const node = document.getElementById("st-clock");
  setText(node, utcStamp(now));
  node.title = now.toISOString();
  for (const panel of state.panels) updateFreshness(panel);
}

// --------------------------------------------------------------------- grammar

/**
 * Resolve one token against the served registry: codes first, then aliases — the
 * precedence `chronos.terminal.commands._index_registry` builds, so an alias can
 * never shadow a code.
 */
function resolveToken(token) {
  const key = token.trim().toUpperCase();
  if (!key) return null;
  return (
    state.registry.find((command) => command.code.toUpperCase() === key) ||
    state.registry.find((command) => (command.aliases || []).some((alias) => alias.toUpperCase() === key)) ||
    null
  );
}

function looksLikeSymbol(token) {
  return token.length <= MAX_SYMBOL_LENGTH && SYMBOL_RE.test(token);
}

/** The tolerant grammar, mirrored from commands.py. See residual 1 in the header. */
function parseLine(line) {
  const raw = line.trim();
  const tokens = raw ? raw.split(/\s+/) : [];
  let command = null;
  let commandIndex = -1;
  tokens.forEach((token, index) => {
    const found = resolveToken(token);
    if (found) {
      command = found;
      commandIndex = index;
    }
  });
  const head = commandIndex < 0 ? tokens : tokens.slice(0, commandIndex);
  let symbol = "";
  let symbolIndex = -1;
  for (let index = 0; index < head.length; index += 1) {
    if (!resolveToken(head[index]) && looksLikeSymbol(head[index])) {
      symbol = head[index];
      symbolIndex = index;
      break;
    }
  }
  const anchor = commandIndex >= 0 ? commandIndex : symbolIndex;
  return { command, symbol, args: tokens.slice(anchor + 1), raw };
}

/** True when `needle` is a subsequence of `hay` (both upper-cased by the caller). */
function isSubsequence(needle, hay) {
  let index = 0;
  for (const character of hay) {
    if (character === needle[index]) index += 1;
    if (index === needle.length) return true;
  }
  return needle.length === 0;
}

/**
 * Ranked completions, after tyche's `suggest.ts` (Apache-2.0, attribution in
 * NOTICE): prefix on the code wins, then prefix on an alias, then a fuzzy
 * subsequence of the code, then a substring of the title. Everything before the
 * token being typed is preserved, so `SPY JR` completes to `SPY JRNL`.
 */
function buildSuggestions(raw) {
  if (!raw.trim() || /\s$/.test(raw)) return [];
  const parts = raw.trim().split(/\s+/);
  const last = (parts[parts.length - 1] || "").toUpperCase();
  const prefix = parts.slice(0, -1).join(" ");
  if (!last) return [];
  const ranked = [];
  for (const command of state.registry) {
    const code = command.code.toUpperCase();
    const aliases = (command.aliases || []).map((alias) => alias.toUpperCase());
    let rank = null;
    if (code.startsWith(last)) rank = 0;
    else if (aliases.some((alias) => alias.startsWith(last))) rank = 1;
    else if (last.length >= 2 && isSubsequence(last, code)) rank = 2;
    else if (last.length >= 3 && command.title.toUpperCase().includes(last)) rank = 3;
    if (rank === null) continue;
    ranked.push({ rank, line: prefix ? `${prefix} ${command.code}` : command.code, hint: command.title });
  }
  ranked.sort((a, b) => a.rank - b.rank || a.line.localeCompare(b.line));
  return ranked.slice(0, SUGGEST_MAX);
}

function drawSuggestions() {
  clear(dom.suggest);
  const open = state.suggestions.length > 0;
  dom.suggest.hidden = !open;
  dom.input.setAttribute("aria-expanded", String(open));
  state.suggestions.forEach((suggestion, index) => {
    const item = el("li", "suggest-item");
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(index === state.suggestIndex));
    item.id = `suggest-${index}`;
    append(item, el("span", "suggest-line", suggestion.line), el("span", "suggest-hint", suggestion.hint));
    item.addEventListener("mousedown", (event) => {
      event.preventDefault();
      runLine(suggestion.line);
    });
    dom.suggest.appendChild(item);
  });
  if (state.suggestIndex >= 0) dom.input.setAttribute("aria-activedescendant", `suggest-${state.suggestIndex}`);
  else dom.input.removeAttribute("aria-activedescendant");
}

function updateSuggestions() {
  state.suggestions = buildSuggestions(dom.input.value);
  state.suggestIndex = state.suggestions.length ? 0 : -1;
  drawSuggestions();
}

function closeSuggestions() {
  state.suggestions = [];
  state.suggestIndex = -1;
  drawSuggestions();
}

// ------------------------------------------------------------------ command bar

function message(text, tone = "") {
  dom.msg.className = `cmd-msg ${tone}`;
  setText(dom.msg, text);
  dom.msg.title = text || "";
}

function setPendingSymbol(symbol) {
  state.pendingSymbol = symbol;
  dom.symbol.hidden = !symbol;
  setText(dom.symbol, symbol);
}

function pushHistory(line) {
  if (state.history[state.history.length - 1] !== line) state.history.push(line);
  while (state.history.length > HISTORY_MAX) state.history.shift();
  state.historyIndex = -1;
}

function walkHistory(delta) {
  if (!state.history.length) return;
  // Forward from "the present" is nowhere: only ArrowUp enters the history.
  if (state.historyIndex < 0 && delta > 0) return;
  const next = state.historyIndex < 0 ? state.history.length - 1 : state.historyIndex + delta;
  if (next >= state.history.length) {
    state.historyIndex = -1;
    dom.input.value = "";
    return;
  }
  state.historyIndex = Math.max(0, next);
  dom.input.value = state.history[state.historyIndex];
  dom.input.setSelectionRange(dom.input.value.length, dom.input.value.length);
}

function runLine(line) {
  const raw = line.trim();
  if (!raw) return;
  if (!state.registry.length) {
    message(state.registryError || "the command registry has not loaded — no command can be resolved", "bad");
    return;
  }
  pushHistory(raw);
  const parsed = parseLine(raw);
  dom.input.value = "";
  closeSuggestions();
  if (parsed.symbol) setPendingSymbol(parsed.symbol);
  if (!parsed.command) {
    const why = parsed.symbol ? `no command in "${raw}" — ${parsed.symbol} is held` : `unknown command: ${raw}`;
    message(`${why}; type HELP for the registry`, "warn");
    return;
  }
  openPanel(parsed.command);
  message(
    parsed.symbol && !parsed.command.takes_symbol
      ? `${parsed.command.code} opened · ${parsed.symbol} held, but no panel filters by symbol yet`
      : `${parsed.command.code} opened`,
  );
}

function completeToSelection() {
  const suggestion = state.suggestions[state.suggestIndex >= 0 ? state.suggestIndex : 0];
  if (!suggestion) return;
  dom.input.value = suggestion.line;
  dom.input.setSelectionRange(dom.input.value.length, dom.input.value.length);
  updateSuggestions();
}

function onInputKeydown(event) {
  const open = state.suggestions.length > 0;
  const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
  if (step) {
    event.preventDefault();
    if (open) {
      state.suggestIndex = (state.suggestIndex + step + state.suggestions.length) % state.suggestions.length;
      drawSuggestions();
    } else if (!dom.input.value || state.historyIndex >= 0) {
      walkHistory(step);
    }
    return;
  }
  if (event.key === "Tab" && open) {
    event.preventDefault();
    completeToSelection();
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    if (open) closeSuggestions();
    else if (dom.input.value) dom.input.value = "";
    else if (state.pendingSymbol) setPendingSymbol("");
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    const suggestion = state.suggestions[state.suggestIndex];
    runLine(suggestion && state.suggestIndex >= 0 ? suggestion.line : dom.input.value);
  }
}

/**
 * Type-anywhere focus, after midas's CommandBar. Guarded on INPUT, TEXTAREA and
 * contenteditable so typing a revocation reason never steals its own keystrokes
 * into the command bar.
 */
function onGlobalKeydown(event) {
  const target = event.target;
  const typingElsewhere =
    target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable === true);
  if (event.ctrlKey || event.metaKey) return;
  if (event.altKey) {
    const index = Number.parseInt(event.key, 10);
    const panel = Number.isInteger(index) ? state.panels[index - 1] : null;
    if (panel && index >= 1 && index <= 9) {
      event.preventDefault();
      focusPanel(panel.id);
      panel.node.focus();
    }
    return;
  }
  if (typingElsewhere) return;
  if (event.key.length === 1 && event.key !== " ") {
    event.preventDefault();
    dom.input.focus();
    dom.input.value += event.key;
    updateSuggestions();
  }
}

// ------------------------------------------------------------------------ boot

async function loadRegistry() {
  const outcome = await getJSON(API.commands);
  if (!outcome.ok) {
    state.registryError = `command registry unavailable — ${outcome.error}`;
    message(state.registryError, "bad");
    return false;
  }
  const commands = outcome.data && Array.isArray(outcome.data.commands) ? outcome.data.commands : [];
  state.registry = commands;
  state.registryAt = Date.now();
  state.registryError = commands.length ? "" : "the backend served an empty command registry";
  drawStarters();
  for (const panel of state.panels) if (panel.spec.local) refreshPanel(panel);
  return commands.length > 0;
}

function drawStarters() {
  clear(dom.emptyKeys);
  for (const command of state.registry.slice(0, 8)) {
    const button = el("button", "btn");
    button.type = "button";
    button.title = command.summary;
    append(button, el("span", "suggest-line", command.code), el("span", "suggest-hint", ` ${command.title}`));
    button.addEventListener("click", () => runLine(command.code));
    dom.emptyKeys.appendChild(button);
  }
}

function boot() {
  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    runLine(dom.input.value);
  });
  dom.input.addEventListener("input", updateSuggestions);
  dom.input.addEventListener("keydown", onInputKeydown);
  dom.input.addEventListener("blur", () => window.setTimeout(closeSuggestions, 120));
  window.addEventListener("keydown", onGlobalKeydown);
  dom.reset.addEventListener("click", () => {
    for (const panel of [...state.panels]) closePanel(panel.id);
    message("workspace cleared");
  });
  // A backgrounded tab has its timers throttled, so its panels are older than
  // their headers would otherwise admit. Re-read on return rather than let the
  // operator glance at a number the browser froze.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshAll();
  });

  syncChrome();
  drawStatus();
  tickClock();
  window.setInterval(tickClock, CLOCK_MS);
  window.setInterval(pollSystem, STATUS_MS);
  pollSystem();
  loadRegistry().then((loaded) => {
    if (loaded) restoreWorkspace();
    syncChrome();
  });
  dom.input.focus();
}

boot();
