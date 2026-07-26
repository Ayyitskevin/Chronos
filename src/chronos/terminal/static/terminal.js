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
 * - **A timeout on owner actions.** Reads abort after `FETCH_TIMEOUT_MS` so a
 *   hung backend cannot freeze a panel while it still looks live. Owner POSTs
 *   have no timeout: aborting one tells this client nothing about whether the
 *   backend applied it, and "refused" would be a claim it is not in a position
 *   to make.
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
  session: "/terminal/session",
};

const POLL_MS = 5000;
const STATUS_MS = 5000;
const CLOCK_MS = 1000;
/** Multiples of a panel's own interval after which its data is stale even if no fetch failed. */
const STALE_INTERVALS = 3;
/**
 * How long a read may hang before it counts as a failure.
 *
 * `fetch` has no default timeout. A request the backend accepts and never
 * answers suspends its caller forever, and a suspended `pollSystem` is a status
 * bar frozen on whatever it last drew while the clock beside it keeps ticking —
 * the page at its most convincing and least true. Two poll intervals is long
 * enough that a merely slow backend is not demoted on every read.
 *
 * What this timeout does NOT do, because an earlier revision of this comment
 * claimed it and a reviewer disproved it: it does not demote the bar during a
 * *sustained* hang. Polls start every `STATUS_MS` while each one takes
 * `FETCH_TIMEOUT_MS` to give up, so by the time a timed-out read resolves a
 * newer poll has already claimed `systemSeq` and the sequence guard in
 * `pollSystem` discards the older answer before it can write a status. In that
 * case the age rule in `tickClock` is the only thing that demotes.
 *
 * So the two mechanisms are complementary, not redundant: the timeout bounds a
 * single read and frees its caller, and the age rule is what the operator's
 * safety actually rests on when nothing is answering. Deleting the age rule as
 * duplicated cover would regress the hang case silently — which is why the
 * relationship is written down here instead of being left to be re-derived.
 */
const FETCH_TIMEOUT_MS = STATUS_MS * 2;
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
  gateDefault:
    "This terminal needs the local API token once, to exchange it for a session. The token is not stored by this page.",
  gateExpired:
    "The backend refused this session. It has either expired, or the backend restarted — sessions are held in memory and do not survive a restart, so a bounce signs every terminal out. Panels keep their last reading, marked stale.",
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
  noEquityFigure: "unknown — no equity was observed this session",
  noEquity:
    "No equity snapshot was taken this session, so peak, trough and both drawdowns have nothing to be derived from. They are unknown, not zero: an unobserved drawdown is not a drawdown of nothing.",
  readOnly:
    "this backend is read-only (it does not hold the writer lease); owner actions are refused here",
  statusUnknown:
    "the backend's status could not be read; refusing to offer an action whose effect cannot be checked",
  stale: (at) => `Everything below is from the load at ${at}, not from now.`,
  unavailable: "This panel has never loaded, so there is nothing it can honestly show.",
};

const state = {
  // null until a route has answered either way. `false` raises the sign-in
  // gate; the tri-state matters because "not signed in" and "not asked yet"
  // must not look the same to the boot path.
  authenticated: null,
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
  /** Monotonic token: the answer to any status poll but the newest is discarded. */
  systemSeq: 0,
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
  gate: document.getElementById("gate"),
  gateForm: document.getElementById("gate-form"),
  gateToken: document.getElementById("gate-token"),
  gateWhy: document.getElementById("gate-why"),
  gateResult: document.getElementById("gate-result"),
  gateSubmit: document.getElementById("gate-submit"),
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

/** Every read. Bounded by `FETCH_TIMEOUT_MS`, because a read that never returns never fails. */
async function getJSON(path) {
  const started = performance.now();
  try {
    const response = await fetch(path, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    const latencyMs = Math.round(performance.now() - started);
    if (!response.ok) {
      if (response.status === 401) noteUnauthorized();
      return { ok: false, status: response.status, error: await errorText(response), latencyMs };
    }
    if (state.authenticated !== true) hideGate();
    return { ok: true, status: response.status, data: await response.json(), latencyMs };
  } catch (error) {
    return { ok: false, status: 0, error: unreachable(error), latencyMs: Math.round(performance.now() - started) };
  }
}

/**
 * Every owner action. Deliberately *not* on the read timeout.
 *
 * Aborting a POST abandons the browser's half of it and does nothing to the
 * backend, so a timed-out revocation would be reported to the owner as refused
 * while quite possibly having landed. "Refused" is a claim about the backend,
 * and this client would not be in a position to make it. A confirmation left
 * waiting is worse ergonomics and better honesty; the panel behind it re-reads
 * on the next poll and the durable state is where the answer actually is.
 */
async function postJSON(path, body) {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      // The sign-in route answers 401 for a bad token, and raising the gate on
      // top of the gate would be noise — it is already open.
      if (response.status === 401 && path !== API.session) noteUnauthorized();
      return { ok: false, status: response.status, error: await errorText(response) };
    }
    return { ok: true, status: response.status, data: await response.json().catch(() => null) };
  } catch (error) {
    return { ok: false, status: 0, error: unreachable(error) };
  }
}

// ------------------------------------------------------------------ sign-in

/**
 * The sign-in gate (M8b).
 *
 * A browser cannot put a header on a document load, so the shell arrives with no
 * credential and finds out it needs one the only way it can: by being refused.
 * Every read and every action funnels its 401 through `noteUnauthorized`, which
 * raises this gate; signing in POSTs the operator's token once and the backend
 * answers with an httpOnly cookie scoped to `/terminal`.
 *
 * What this page deliberately never does is *keep* the token. It is read out of
 * the field, sent, and the field is cleared. There is no copy in `state`, none
 * in `localStorage`, and none the page could read back out of the cookie — so a
 * script that got into this page after sign-in inherits the ability to call
 * `/terminal/*` (which the CSP exists to prevent, and which the cookie's path
 * scope keeps away from the order plane) but never learns the credential itself
 * and so cannot carry it anywhere this page cannot already reach.
 */
function showGate(why) {
  if (!dom.gate) return;
  state.authenticated = false;
  dom.gate.hidden = false;
  setText(dom.gateWhy, why || COPY.gateDefault);
  setText(dom.gateResult, "");
  if (dom.gateToken) dom.gateToken.focus();
}

function hideGate() {
  if (!dom.gate) return;
  state.authenticated = true;
  dom.gate.hidden = true;
  if (dom.gateToken) dom.gateToken.value = "";
  setText(dom.gateResult, "");
}

/**
 * Called for every 401 the backend returns.
 *
 * A session can lapse mid-use — the TTL runs out, or the backend restarts and
 * forgets every session it had — and the honest response to that is the same as
 * to never having signed in. Panels keep whatever they last drew, correctly
 * marked stale by their own freshness rule, rather than being blanked: what
 * expired is the credential, not the knowledge of what was true a moment ago.
 */
function noteUnauthorized() {
  if (state.authenticated === false) return;
  showGate(COPY.gateExpired);
}

async function signIn(token) {
  if (!token) {
    setText(dom.gateResult, "enter the token first");
    return false;
  }
  if (dom.gateSubmit) dom.gateSubmit.disabled = true;
  setText(dom.gateResult, "signing in…");
  const outcome = await postJSON(API.session, { token });
  if (dom.gateSubmit) dom.gateSubmit.disabled = false;
  if (!outcome.ok) {
    // The backend deliberately says only "invalid token" — there is one way to
    // be wrong here and elaborating would only help something that is guessing.
    setText(dom.gateResult, `refused: ${outcome.error || `HTTP ${outcome.status}`}`);
    return false;
  }
  hideGate();
  message("signed in");
  pollSystem();
  loadRegistry().then((loaded) => {
    if (loaded) restoreWorkspace();
    syncChrome();
  });
  refreshAll();
  return true;
}

function unreachable(error) {
  // A timeout is not the same failure as a refused connection: the request may
  // have been accepted and be running still. Naming it keeps the status bar's
  // hover from calling a hung backend an absent one.
  if (error && error.name === "TimeoutError") {
    return `no answer within ${Math.round(FETCH_TIMEOUT_MS / 1000)}s — the backend accepted the read and has not answered it`;
  }
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
  const list = el("dl", "kv");
  kv(list, "account", fingerprintNode(data.account_fingerprint));
  kv(list, "generated", timeNode(data.generated_at));
  append(body, list);
  append(body, note(`${data.entries.length} record${data.entries.length === 1 ? "" : "s"}, newest first · limit ${data.limit}`));
  // `journal_view` answers `stream_present: false` for two different facts: an
  // account whose cycle stream is empty, and a backend bound to no account,
  // which could not look. Only the first is "no cycles"; the second is "we did
  // not ask", and rendering it as calm is the failure the queue and alerts
  // panels already branch on `account_fingerprint` to avoid.
  if (!data.stream_present) {
    return append(
      body,
      data.account_fingerprint
        ? stateBlock("warn", "NO CYCLES RECORDED", COPY.noCycles)
        : stateBlock("warn", "SCOPE UNKNOWN", COPY.unscoped),
    );
  }

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

    // "not sized" and "not compiled" are assertions about what the cycle did.
    // An undecodable payload cannot support them: `_journal_entry` returns
    // `quantity: null` there because *nothing could be read*, not because the
    // cycle stopped before sizing, and this client cannot tell those apart from
    // the field alone. Printing the confident sentence under a record already
    // chipped PAYLOAD UNDECODABLE would have the panel contradict itself.
    const facts = el("p", "note");
    if (entry.decision_id) append(facts, el("span", null, `decision ${entry.decision_id} · `));
    if (entry.decoded) {
      const sized = entry.quantity === null ? "quantity: not sized" : `quantity ${entry.quantity}`;
      const priced = entry.limit_price === null ? "limit: not compiled" : `limit ${entry.limit_price}`;
      append(facts, el("span", null, `${sized} · ${priced}`));
    } else {
      append(facts, unknown("quantity and limit: unknown — the payload would not decode"));
    }
    append(body, append(article, facts));
  }
  return body;
}

const COUNTER_FIELDS = [
  ["realized loss", "realized_loss_usd"], ["drawdown", "drawdown_usd"], ["drawdown %", "drawdown_pct"],
  ["peak equity", "peak_equity_usd"], ["trough equity", "trough_equity_usd"], ["orders", "orders_submitted"],
  ["cancellations", "cancellations"], ["replacements", "replacements"], ["turnover", "turnover_usd"],
];

/**
 * The four figures that exist only once equity has been looked at.
 *
 * A counter row can be recorded — orders counted, turnover accumulated — with
 * no equity snapshot ever taken, and then peak, trough and both drawdowns are
 * `null`. A zero drawdown is the most reassuring number on this panel and "we
 * never looked" is the least, so `CountersView.equity_observed` turns the
 * absence into a labelled unknown rather than letting `value()` render the
 * generic word beside eight figures that *were* measured.
 */
const EQUITY_DERIVED = new Set(["drawdown_usd", "drawdown_pct", "peak_equity_usd", "trough_equity_usd"]);

function renderCounters(data, body) {
  const list = el("dl", "kv");
  kv(list, "session", value(data.session_date));
  const noEquity = data.equity_observed === false;
  if (!data.counters_recorded) {
    append(body, stateBlock("warn", "NO COUNTERS RECORDED", COPY.noCounters(data.session_date)));
  } else {
    for (const [label, key] of COUNTER_FIELDS) {
      const absent = noEquity && EQUITY_DERIVED.has(key) ? COPY.noEquityFigure : "unknown";
      kv(list, label, value(data[key], { absent }));
    }
  }
  kv(list, "account", fingerprintNode(data.account_fingerprint));
  kv(list, "generated", timeNode(data.generated_at));
  append(body, list);
  if (!data.counters_recorded) return body;
  return append(body, note("Figures are what the supervisor itself observed this session, in USD."), noEquity ? note(COPY.noEquity) : null);
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
      // The route's own words, carried out verbatim. `RevokeResult.detail` says
      // which cycle is still allowed to finish and `AcknowledgeResult.detail`
      // says what an acknowledgement did *not* do; paraphrasing either here
      // would make this file a second declaration of what the backend decided.
      const detail = outcome.data && typeof outcome.data.detail === "string" ? outcome.data.detail : "";
      setText(result, `accepted by the backend — re-reading state${detail ? `. ${detail}` : ""}`);
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

/**
 * Why an owner action cannot be offered right now, or an empty string.
 *
 * Authority must be *positively observed* before a button is live, not merely
 * un-refuted. `boot` fires the status poll and the registry load concurrently
 * and `/terminal/commands` touches no database while `/terminal/system` opens a
 * session, so the registry routinely wins and `restoreWorkspace` mounts the
 * mandate panel while `state.system` is still null — an enabled REVOKE MANDATE
 * on a backend this client has not yet learned is read-only. A stale status is
 * the same problem with a longer fuse: `read_only` from minutes ago describes a
 * lease this process may since have lost.
 *
 * The button still renders, disabled, with the reason stated. See the header:
 * a button that vanishes teaches nothing.
 */
function actionBlockedReason() {
  if (state.systemStatus !== "ok" || !state.system) return COPY.statusUnknown;
  if (state.system.read_only === true) return COPY.readOnly;
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
      // `chronos.supervisor.alerts.acknowledge` raises on an empty note and the
      // route turns that into a 422. Labelling the field optional here would be
      // this client declaring the rule a second time and declaring it wrong —
      // the operator would leave it blank as invited and read "refused: HTTP
      // 422" for doing what the label said.
      noteLabel: "note (required, recorded with the acknowledgement)",
      noteRequired: true,
      submitLabel: "ACKNOWLEDGE",
      danger: false,
      done,
      submit: (text) => postJSON(API.acknowledge(alert.id), { note: text }),
    }),
  );
}

/**
 * The revocation, named and then bound to the name.
 *
 * The confirmation used to quote `mandate.mandate_id` from the panel's last
 * load while the request carried no id at all, leaving the route to re-derive
 * whatever was in force when it arrived. A backend can restart under an edited
 * grant and auto-activate it (ADR-0017) while the owner is still typing their
 * reason, and the owner would then confirm a form naming M-1 while the chain
 * recorded the revocation of M-2. The typed-confirmation gate exists precisely
 * so the stated intent and the recorded act are the same act, so the id is sent
 * and the backend refuses with a 409 if a different grant is in force by then —
 * whose detail reaches the operator through the ordinary refusal path.
 */
function revokeAction(mandate) {
  const named = mandate.mandate_id
    ? `This withdraws the owner's grant ${mandate.mandate_id}, and that id travels with the request: if a different grant is in force by the time it lands, the backend refuses rather than revoking one the owner did not name.`
    : "This withdraws whatever grant this backend has in force. The panel could not name one, so this terminal cannot tell you which it will be.";
  return ownerAction("REVOKE MANDATE", true, (done) =>
    confirmForm({
      heading: "Revoke the active mandate",
      why: `${named} The supervisor loses its authority to act and the revocation is audited. It cannot be undone from this terminal.`,
      phrase: "REVOKE MANDATE",
      noteLabel: "reason (required, recorded)",
      noteRequired: true,
      submitLabel: "REVOKE",
      danger: true,
      done,
      submit: async (reason) => {
        const outcome = await postJSON(API.revoke, { reason, mandate_id: mandate.mandate_id || null });
        // `revoked: false` is a 200 by the route's deliberate choice — "an
        // answer, not an error". It is not a *success* for the owner, who asked
        // for authority to be gone and had nothing withdrawn, so it is carried
        // out in the refusal style with the route's own detail as the text.
        // Keying the outcome on HTTP status alone would report the one case the
        // route designed a distinct answer for as if it had worked.
        if (outcome.ok && outcome.data && outcome.data.revoked === false) {
          const detail = outcome.data.detail || "nothing was in force here, so nothing was revoked";
          return { ok: false, status: outcome.status, error: detail };
        }
        return outcome;
      },
    }),
  );
}

// ------------------------------------------------------------- panel plumbing

/**
 * The renderer the backend named, or null.
 *
 * Own-property lookup rather than `PANELS[name]`: the panel id is a
 * server-supplied string, and `constructor`, `toString` or `__proto__` would
 * answer with an inherited `Object.prototype` member that passes a truthiness
 * guard and then mounts a panel whose `endpoint` is undefined — a permanent
 * LOADING tile throwing an unhandled rejection every five seconds. Registry
 * text is allowed to be wrong; it is not allowed to reach through this table.
 */
function panelSpec(name) {
  return Object.prototype.hasOwnProperty.call(PANELS, name) ? PANELS[name] : null;
}

function openPanel(command) {
  const spec = panelSpec(command.panel);
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
    /** Monotonic token: the answer to any read but this panel's newest is discarded. */
    reqSeq: 0,
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
  // Reads are never serialized: the interval fires without waiting for the last
  // one, `refreshAll` fires again on every return to the tab and after every
  // accepted owner action, and each read may take up to `FETCH_TIMEOUT_MS`. So
  // several are routinely in flight, and without a token the *slower* of two
  // wins — older data applied last and stamped `lastOkAt: Date.now()`, which is
  // a stale panel wearing a LIVE badge. The token makes the answer to anything
  // but the newest request unusable rather than merely unlikely to matter.
  const token = (panel.reqSeq += 1);
  const outcome = await getJSON(panel.spec.endpoint());
  if (!state.panels.includes(panel) || panel.frozen || token !== panel.reqSeq) return;
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
  // Sequenced for the same reason `refreshPanel` is: the interval does not wait,
  // `refreshAll` fires on top of it, and a slow answer applied after a fast one
  // would restamp `systemAt` and make older authority look newly read.
  const token = (state.systemSeq += 1);
  const outcome = await getJSON(API.system);
  if (token !== state.systemSeq) return;
  state.latencyMs = outcome.latencyMs;
  if (outcome.ok) {
    Object.assign(state, { system: outcome.data, systemStatus: "ok", systemError: "", systemAt: Date.now() });
  } else {
    // The last answer is kept only to tell STALE from UNAVAILABLE. Nothing reads
    // it for a fact while the status is not "ok" — see `drawStatus`.
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

  // `state.system` is the last answer, not the current one. Anything but a
  // successful poll therefore makes every fact below *unobserved* rather than
  // cached — because the disengaged and disarmed renderings are hidden badges,
  // and an absent badge is how this page says "clear". Reading a cached `false`
  // would erase the kill-switch badge for as long as the backend stayed
  // unreachable, which is the header's first rejected design reached from the
  // other side: a cached "kill switch: clear" outliving an outage. A null
  // renders the loud dashed badge-unknown and is never hidden.
  const observed = status === "ok" ? system : null;

  const readOnly = observed ? observed.read_only : null;
  const role = readOnly === null || readOnly === undefined ? ["ROLE UNKNOWN", "st st-warn"] : readOnly ? ["READ-ONLY", "st st-warn"] : ["WRITER", "st st-ok"];
  badge("st-role", role[0], role[1], { title: "whether this backend holds the single-writer lease" });

  let autonomy = ["AUTONOMY UNKNOWN", "st st-warn"];
  if (observed && !observed.autonomy_configured) autonomy = ["AUTONOMY NOT CONFIGURED", "st st-dim"];
  else if (observed && observed.autonomy_stopped === true) autonomy = ["AUTONOMY STOPPED", "st st-warn"];
  else if (observed && observed.autonomy_stopped === false) autonomy = ["AUTONOMY RUNNING", "st st-ok"];
  badge("st-autonomy", autonomy[0], autonomy[1]);

  // Engaged is loud and unknown is nearly as loud; only a positively observed
  // "disengaged" is allowed to be silent.
  const kill = observed ? observed.kill_switch_engaged : null;
  badge("st-kill", kill ? "KILL SWITCH ENGAGED" : "KILL SWITCH UNKNOWN", kill ? "st badge-kill" : "st badge-unknown", {
    hidden: kill === false,
    title: "the durable kill switch",
  });

  const armed = observed ? observed.live_armed : null;
  badge("st-armed", armed ? "LIVE ARMED" : "ARM STATE UNKNOWN", armed ? "st badge-armed" : "st badge-unknown", {
    hidden: armed === false,
    title: "whether the live session is armed",
  });

  // A null critical count is not a measured zero: `_alert_counts` answers null
  // for an unscoped backend that could not ask. `alertCountNode` says so in the
  // panel, and a bar that silently dropped the suffix would leave the two
  // surfaces disagreeing about the same field with the quieter one wrong.
  const pending = observed ? observed.alerts_unacknowledged : null;
  const critical = observed ? observed.alerts_critical : null;
  const criticalKnown = critical !== null && critical !== undefined;
  const criticalText = criticalKnown ? (critical > 0 ? ` · ${critical} CRITICAL` : "") : " · CRITICAL UNKNOWN";
  const alertsText = pending === null || pending === undefined ? "ALERTS UNKNOWN" : `${pending} ALERT${pending === 1 ? "" : "S"}${criticalText}`;
  let alertsTone = "st-warn";
  if (criticalKnown && critical > 0) alertsTone = "st-bad";
  else if (criticalKnown && pending === 0) alertsTone = "st-dim";
  badge("st-alerts", alertsText, `st ${alertsTone}`, { title: "unacknowledged owner alerts" });

  const slow = state.latencyMs !== null && state.latencyMs > 500;
  badge("st-latency", state.latencyMs === null ? "— ms" : `${state.latencyMs} ms`, `st ${slow ? "st-warn" : "st-dim"}`, {
    title: "round trip of the status poll",
  });
}

/**
 * The clock, and the ageing rule that keeps the bar beside it honest.
 *
 * The status bar gets the demotion `updateFreshness` already applies to panels,
 * and for the same reasons: a throttled tab, a suspended laptop and a poll the
 * backend accepted but never answered all leave `state.systemAt` behind without
 * any fetch having failed. Without this the bar would keep drawing the role,
 * the autonomy state and the authority badges from whenever the last answer
 * arrived while the clock next to them ticked on — the page at its most
 * convincing and least true.
 */
function tickClock() {
  const now = new Date();
  const node = document.getElementById("st-clock");
  setText(node, utcStamp(now));
  node.title = now.toISOString();
  // Only a bar that still believes it is current gets demoted here. One that a
  // failed poll already demoted keeps that poll's own words — "HTTP 401" says
  // more about what to do next than the age does.
  const aged = state.systemAt && Date.now() - state.systemAt > STATUS_MS * STALE_INTERVALS;
  if (aged && state.systemStatus === "ok") {
    state.systemStatus = state.system ? "stale" : "unavailable";
    state.systemError = `no successful status read in ${Math.round((Date.now() - state.systemAt) / 1000)}s`;
  }
  drawStatus();
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
  if (dom.gateForm) {
    dom.gateForm.addEventListener("submit", (event) => {
      event.preventDefault();
      signIn(dom.gateToken ? dom.gateToken.value.trim() : "");
    });
  }

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
