# ADR-0018: The Operator Terminal — Build Fresh, Python-Served, No Second Runtime

Status: accepted (2026-07-26) — decision recorded ahead of the code it governs
Date: 2026-07-26
Index entry: DECISIONS.md **D-18**.
Implementation status: **implemented.** This ADR was written before its code, and
the note here originally warned that no terminal shipped yet; that warning has been
kept rather than deleted because the distinction it drew is the useful part. What
has since landed: **M8a** — `chronos.terminal` (command registry and panel
read-models), the `/terminal/*` routes, and the browser client — and **M8b**, the
session cookie that lets that client authenticate (see the amendment before the
residuals). Design statements below now describe code that exists; the residuals
at the end are the honest list of what still does not.
Resolves: the ADR-0016 §"Milestone sequencing" / AI_QUANT_GAME_PLAN §E2 contradiction
about whether the terminal may confirm orders (see §4).
Depends on: ADR-0016 (authority split, one gateway, no second submission path),
ADR-0017 (persistent mandate, model discretion, protected market orders),
ADR-0009 (live conjunction and the ten-gate stack), ADR-0004 §§1-4.

## Context

The owner directed that Chronos gain a Gödel-Terminal-style operator terminal
(milestone "M8"), modeled on two terminals they had already built and on the
Quant Guild reference bot analyzed in `docs/LECTURE_134_ANALYSIS.md`. Both prior
terminals were added to the working session as candidates:

- **tyche** (`/workspace/tyche`, Apache-2.0) — a multi-asset, keyboard-first
  research terminal. A pnpm/TypeScript monorepo: a Zod-typed contracts package, a
  pure DOM-free `terminal-kernel` (tolerant grammar, validated command registry,
  effect-emitting executor), a capability-typed `DataProvider` plane with a
  runtime plugin host, a Module SDK, React 18 + Vite + zustand + react-grid-layout
  over a Fastify API with SSE streaming. 65 commands, ~108 modules.
- **midas** (`/workspace/midas`, AGPL-3.0-only) — a crypto-only Bloomberg-style
  terminal. Same broad stack, but far more product surface: 234 commands, ~233
  module files, a committed amber-on-black theme, multi-workspace tabs with
  templates and share links, one multiplexed WebSocket, a "data honesty" doctrine
  labeling every value live/synthetic/unavailable, and a fail-closed
  `TradingSafetyHold` returning 503 on every order-mutating route.

The owner left the choice open: adopt one, use both, or use them as reference and
build fresh. A seven-agent reconnaissance read all three codebases end to end
before this decision was taken. Its findings, not preference, decide it.

## Decision

**Build the terminal fresh, inside Chronos, served by the existing FastAPI
backend, with no Node toolchain and no second runtime.** Take *design* from both
prior terminals — tyche's command architecture (Apache-2.0, so code too, with
attribution) and midas's operator vocabulary, supervision status bar, and
data-honesty doctrine (as patterns).

### 1. Why not adopt tyche

Adopting tyche without forking it is **structurally impossible for the surface
that matters**, and this is a fact about its architecture, not a judgment:

- **UI modules are build-time only.** A panel is a `CommandDescriptor` in
  `packages/terminal-kernel/src/commands.ts` plus a React component keyed by
  `moduleId` in `apps/web/src/modules/components.ts`. There is no runtime UI
  plugin path. Every Chronos-specific panel means editing tyche's source — a fork.
- **The sanctioned runtime plugin path cannot carry our data.** `TYCHE_PLUGINS`
  loads `ProviderPlugin`s implementing `DataProvider`, which has one method per
  *market-data* kind (`getQuote`, `getHistory`, `getOrderBook`, …). It has **no
  `portfolio` method** — positions, orders, fills, and account state do not flow
  through the provider plane at all. The single most important thing a Chronos
  terminal must show is exactly the thing the extension point cannot deliver.
- **Tyche is constitutionally not a trading terminal.** Its own `SECURITY.md`
  states it is "not a broker — no order-placement / trade-execution path," and
  `apps/api/src/routes/user.ts` carries "Tyche places no orders, period." Making
  it one is a philosophical fork, not a configuration.

### 2. Why not fork midas

- **Neither repo contains a real order-management UI to inherit.** midas's
  `POST /api/orders` and `DELETE /api/orders/:id` are hardcoded 503
  `TradingSafetyHold`; its `AGENTS.md` forbids weakening them. So the confirm /
  arm / kill / mandate surface — the actual point of a Chronos operator terminal —
  is new work under every option. What is genuinely adoptable is shell, workspace
  store, command grammar, and doctrine; not the trading surface.
- **The board catalog is its main surplus, and that surplus is deferred.** ~128
  crypto indicator boards plus a Solana suite are dead weight against IBKR
  equities and options, and at the account's verified size (~USD 110, ADR-0017
  limitation 6) rich P&L and attribution boards would render theater. Pruning ~233
  module files while keeping ~1794 tests green is real, unestimated work whose
  payoff is mostly hypothetical.
- **Its parser mis-parses our symbols.** midas's 73-line parser upper-cases the
  whole line and treats the first token as a slash-pair symbol; IBKR option and
  future symbols (`ESZ5`, occ-style option symbols with spaces) do not survive it.
- **License, recorded and then set aside.** midas is AGPL-3.0-only. The GitHub
  collaborator list shows a single collaborator (the owner), so relicensing is the
  owner's to grant and the practical risk is low — but build-fresh means the
  question never has to be answered, and Chronos's license story stays simple.

### 3. Why not any Node shell: the cost is on our side anyway

The reconnaissance's central finding is that **the dominant cost of an operator
terminal is Chronos-side and identical under every option.** Verified today:
`src/chronos/api/routes/` contains only `account`, `autonomy`, `health`, `live`,
`orders`, `strategy`; there are **no** routes for tick health, proposal-queue
depth, the cycle journal, session counters, or mandate state; there is **no**
WebSocket, SSE, or streaming response anywhere in `src/`; there is no CORS
middleware and no static serving; `openapi_url=None`.

Every candidate — adopt, fork, or build — must first add those routes, a
streaming transport, and a token-safe serving architecture. Choosing a
TypeScript shell does not reduce that work by a line. What it *adds* is a second
language toolchain, a `node_modules` tree, a second test framework, and a second
supervised process running beside the one that holds the broker connection and
moves money. That trade is not worth shell code whose trading surface we would
have to build from scratch regardless.

### 4. The execution posture, resolved

The reconnaissance surfaced a genuine contradiction between two texts in this
repository, and it must be resolved rather than straddled:

- ADR-0016's milestone sequencing describes "M5 the terminal and scheduler **with
  execution still disabled**."
- `docs/AI_QUANT_GAME_PLAN.md` §E2 specifies that "the owner reviews/confirms in
  the dashboard — the confirmation and transmit path is **byte-identical to manual
  flow**."

**Resolution: the terminal is display plus owner-actions, and it reaches
execution only through the existing, unchanged confirm path.** The reasoning:

- ADR-0016's clause is *sequencing*, written at M1 when nothing in
  `chronos.autonomy` was wired to any runtime path; "execution still disabled"
  described the state of the programme at that milestone, not a permanent property
  of the terminal. ADR-0017 wired execution. The clause is spent.
- The shipped thin-client Streamlit UI (`src/chronos/ui/pages/order_workspace.py`)
  **already** drives propose → preview → confirm. The terminal supersedes that
  client; denying it the confirm flow would be a functional regression dressed as
  a safety gain, while the risk it purports to address is unchanged.
- E2's constraint is the one that actually binds, and it is a *correctness*
  constraint rather than a permission: byte-identical. The terminal calls the same
  routes with the same payloads and gets the same gates.

Stated as a rule:

**The terminal MAY DO** — arm and disarm the live session; engage and disengage
the durable kill switch; acknowledge owner alerts; revoke a mandate; and drive
propose → preview → confirm → submit through the **existing** order routes.

**The terminal MAY NOT** — construct or transmit an order by any other path;
parse model narrative (thesis, rationale) into any order parameter; hold a broker
handle, a session, or a lease; or become a second place where authority is
decided. It is a window and a set of owner buttons, never a gateway.

### 5. What we take from each

From **tyche** (Apache-2.0; attribution recorded in `NOTICE`):

- The tolerant command grammar `<symbol?> <key>* <command?> <args…>`, where the
  last registry-resolving token wins, a bare symbol defaults to a description
  panel, and unrecognized text degrades to search rather than erroring.
- **A validated command registry as the single source of truth**, from which the
  panel surface, the help text, and completions are all derived.
- **Capability gating**: a panel declares what data it needs, and an unavailable
  capability renders a named, honest empty state instead of a crash or a blank.

From **midas** (patterns, not code):

- The operator command vocabulary — positions, orders, fills, balances, system
  status, alerts, journal — which is very nearly the exact list Chronos needs.
- The **supervision status bar**: stream state, data source, latency, clock, and a
  prominent live-trading badge, always visible.
- The **data-honesty doctrine**: every displayed value is labeled live, stale, or
  unavailable, and the terminal never renders a number whose provenance it cannot
  state. This matches Chronos's existing posture that missing broker data is
  represented as missing and never fabricated.

### 6. The inversion: the terminal's contract lives in Python

The one genuinely novel decision here, and the reason build-fresh is *better*
rather than merely cheaper:

**The command registry and every panel read-model are Python**, in
`chronos.terminal`, and are served to the browser as JSON. The browser client is
a thin, build-free ES-module renderer that fetches the registry and draws what the
backend describes.

Consequences, all of them the point:

- The terminal's contract is typed and tested by the gates that already protect
  this repository — `mypy --strict`, `ruff`, and `pytest` — rather than by a
  second toolchain with its own conventions.
- There is no build step, no `node_modules`, and no lockfile to audit beside a
  process that moves money (R-15's supply-chain surface does not grow).
- Serving the client **same-origin from FastAPI** dissolves the browser-token
  problem the reconnaissance flagged: no CORS, no bearer token in client-visible
  JavaScript, no local BFF holding a secret. The loopback binding and the existing
  `X-Chronos-Token` posture are unchanged.
- The panel surface cannot drift from what the backend can actually answer,
  because the backend is what declares it.

## Consequences

- Chronos gains a first-party operator terminal with no new language, runtime,
  package manager, or supervised process. The dependency surface is unchanged.
- The terminal's panel surface is bounded by what `chronos.terminal` declares,
  which is a feature: a panel that no route can feed cannot be declared.
- We give up the prior terminals' charting (lightweight-charts in midas, a
  hand-rolled canvas renderer in tyche) and their board catalogs. Charting is
  deferred deliberately; at present Chronos has no historical-bar route and no
  `Broker` method to serve one, so a chart would have nothing honest to draw.
- Cost accepted: writing a renderer without a framework means more explicit DOM
  code than React would need. Bounded by keeping panels small and declarative, and
  by the registry living server-side.

## Amendment (M8b, 2026-07-26): how the browser authenticates

§6 above put the client behind same-origin serving and left *how it presents a
credential* unstated, which M8a then discovered the hard way: a browser cannot
put a header on a document load, so the shipped client held nothing and every
panel answered `401` (R-41). The owner chose the session-cookie route from three
options (pasted token in `sessionStorage`, session cookie, exempting loopback
reads).

`POST /terminal/session` exchanges the local API token for an httpOnly cookie,
and every `/terminal/*` route accepts **either** that cookie or the existing
`X-Chronos-Token` header — so every non-browser caller is unchanged.

**The cookie is scoped to `path=/terminal`, and that scope is the whole reason a
cookie is acceptable in this process.** An ambient credential here is a genuine
hazard: the M8a injection review observed that the moment this page holds one,
same-origin script execution becomes able to act with it, in the process that
holds the broker connection. A `path=/` cookie would let injected script reach
`/orders/*` with the browser authenticating for it. The narrow scope makes that
structurally impossible, and it is verified server-side rather than by trusting
the browser to honour the path — an order route asked with the session and no
header still refuses.

What the other properties do, and honestly what they do not:

- `httpOnly` stops script *reading* the cookie, so it cannot be exfiltrated. It
  does **not** stop script *using* it in place on `/terminal/*`; the CSP (R-40)
  is the layer that addresses that, which is why both exist.
- `SameSite=strict` keeps another origin from causing the browser to send it.
- Sessions live **in memory only**, so a restart signs every terminal out. A
  durable session store would be a credential outliving the process it
  authenticates to.
- A session is **not authority**. It proves the caller held the token and grants
  nothing further: `require_writer` still gates every mutation on its own, and
  signing in on a demoted backend buys inspection and nothing else.

## Known limitations and residuals

1. **No charts in this milestone.** There is no historical-bar route, and the
   `Broker` protocol has no bars method. A chart panel requires that backend work
   plus an IBKR pacing budget, and is deliberately out of scope rather than faked.
2. **Polling before streaming.** The first cut refreshes panels on an interval.
   A streaming transport (SSE) is the natural follow-on, but nobody has measured
   its contention against the autonomy tick, the writer-lease heartbeat, and IBKR
   pacing in the process that holds the broker connection. Adding it before that
   measurement would be putting load on the wrong process on a guess.
3. **`GET /autonomy/alerts` requires the writer lease**, so a terminal pointed at
   a demoted or secondary backend loses its alert feed. Read routes added here are
   deliberately *not* writer-gated so the terminal degrades to read-only rather
   than going dark — but the pre-existing alerts route keeps its gate until that
   is revisited on purpose.
4. **Mandate revocation becomes reachable over HTTP for the first time.** It has
   existed only as `chronos.supervisor.durable.revoke()` with no route and no CLI.
   Exposing it is an authorization-surface change: it is owner-only, token-gated,
   requires a typed reason, and is audited — and it is called out here so the
   adversarial review the working protocol requires has a named target.
5. **The account is ~USD 110.** Position and P&L panels will look sparse, and
   that is honest. The terminal's value at this size is supervision — mandate
   state, tick health, refusals, alerts — not portfolio analytics.
6. **Prompt-injection surface (R-30) now has an owner-facing renderer.** Model
   narrative (thesis, rationale) derived from external text is displayed to the
   owner. It is rendered as **text, never as markup**, and no displayed narrative
   is ever parsed into an order parameter (ADR-0016 §5). The terminal is a place
   injected text can be *seen*, which is intended; it must never be a place
   injected text can *act*.
