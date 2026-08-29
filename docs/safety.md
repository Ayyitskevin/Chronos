# Safety model

Chronos is being built toward controlled autonomous trading (ADR-0016 / D-16). The safest
valid result is still often `NO_TRADE` or `MANUAL_REVIEW`, and under autonomy that is a
decision the deterministic kernel makes on its own authority, without asking the model.

## Authority model (ADR-0016 / D-16, 2026-07-25)

**Policy time belongs to the owner; trade time may belong to the model.** The owner defines
objectives, model and provider versions, permitted instruments and strategies, capital and
risk allocation, promotion between autonomy levels, activation and revocation of authority,
and the durable kill switch. After the owner activates an approved AutonomyMandate, an
approved model may originate trading decisions without per-order approval, inside that
mandate's bounds.

The model's authority is narrow by construction:

- It may act **only** by emitting a typed `AITradeDecision` through the single deterministic
  ModelDecisionGateway. Free-form chat, theses, summaries, and Markdown are never parsed
  into orders.
- It gets **no** IBKR client or broker object, credentials, low-level `placeOrder`/
  `cancelOrder` functions, direct submission-module imports, arbitrary SQL/shell/filesystem/
  network access, policy-writing tools, arming or mandate-writing tools, or deployment or
  code-modification tools. The model worker runs outside the broker-writing process.
- Its requested quantity is **not executable**. Deterministic code independently resolves and
  qualifies the contract, computes and clamps quantity, calculates collateral and margin,
  selects a permitted order form, validates tick/lot/multiplier/currency/exchange, checks
  data freshness and liquidity, reconciles to broker truth, enforces every limit, and mints
  execution approval.
- **The deterministic kernel has unconditional veto authority.** It may reject or reduce any
  request; the model cannot override, reinterpret, or repeatedly route around a rejection.
- **An AI failure never becomes permission to trade.** If the model, broker, market data,
  clock, database, lease, contract resolver, risk engine, or reconciliation state is
  unavailable, ambiguous, stale, or inconsistent, the system creates no new exposure, permits
  only deterministic risk-reducing behavior allowed by policy, records the denial, and alerts
  the owner.

  **Clock implementation boundary (ADR-0041):** automatic quantitative clock observation now
  feeds the display-only operational-health projection, but it is structurally excluded from
  order authority. It therefore detects and reports unavailable, stale, future, local, or
  over-bound evidence without yet enforcing the submission predicate promised above. That
  authority wiring, an owner-selected threshold, and real-host evidence remain open; the
  diagnostic must not be treated as proof that clock failure currently blocks an order.

**Status:** the stack is built and wired (M1 contracts through M7.5's app-plane wiring,
ADR-0017). A backend booted with a valid `AUTONOMY_MANDATE_FILE` auto-activates it and
drives the autonomy tick; without one, autonomy is inert and nothing is constructed. The
model worker remains a separate process calling in over the ingress (R-35) — Chronos ships
no model, no provider SDK, and no API key in the broker-holding process.

### Autonomous option selection (ADR-0030, 2026-08-01)

The first executable autonomous option scope is deliberately smaller than
ADR-0016's programme matrix: opening equity-option cash-secured puts and covered
calls only. The model still cannot name an option right or any broker identity;
the admitted strategy derives `PUT` or `CALL`, and bounded read-only evidence
must exactly qualify the underlying, complete chain, candidate contracts,
quotes/Greeks/liquidity, session, market rules, and authoritative deliverable.
Unknown volume or open interest always blocks. Missing, partial, truncated,
stale, future, duplicated, unexpected, or contradictory evidence becomes a
typed `NO_TRADE` receipt.

The selector derives the receipt-bound tick-conforming sell limit. The existing
compiler independently derives it again, and an exact contract/price mismatch
blocks before the order plane. Every receipt is committed to an account-scoped
hash chain, then the complete chain and every semantic receipt are replayed from
durable state before use and again before handoff. System/evidence refusals raise
a deduplicated owner alert. The authenticated bounded
`GET /terminal/option-selections` view is read-only and exposes replay/chain
status; SQL storage-type/length/prefix projections prevent corrupt durable text
from being materialized or echoed before validation. The outer envelope itself
must be byte-canonical, and durable timestamps are UTC-normalized before
hashing. There is no replay or promotion CLI.

This capability is **off by default**. CANARY/LIVE additionally requires an
owner-authored resolver promotion for exactly one live mode, bound to the
canonical mandate, exact policy, versions, and material-source digest and
revalidated after acquisition and immediately before handoff. Runtime code
cannot create that artifact, and this release creates none. Both real IBKR
adapters report deliverable evidence as non-authoritative, so they intentionally
remain `NO_TRADE` until an authoritative schedule source exists.

### QQQ managed-position admission (ADR-0035/ADR-0036, 2026-08-25)

The QQQ Five-Tool management state machine and its opening-admission seam remain
off by structure: no runtime module imports admission, and neither module owns a
submission, modification, cancellation, scheduler, mandate, or activation path.
Admission accepts only a Chronos opening-order reference and an aware clock value.
It re-derives economic facts from durable local intent/risk records and two stable
read-only broker snapshots.

Registration requires a terminal PAPER QQQ `BUY OPEN`, a named passing frozen
entry-risk check, unchanged reconciliation session/generation, positive execution
identity agreeing with Chronos's broker/permanent IDs and cumulative fill, no
competing QQQ order, and exact account-level QQQ position coherence. Missing
execution evidence or a buy fill above the protected limit refuses; disappearance
from open orders never proves a fill. Repeating multi-fill VWAPs round upward at
the persistence boundary, as do derived losses, while allowed risk budgets round
downward. Schema v11 atomically binds one opening order, one deterministic managed
stream, and one broker permanent ID per account scope, and exact replay does not
re-read the broker.

This is not protection. Ongoing management observations/outcomes are not yet
authenticated by this seam; no broker-held stop/target survives disconnect or
process death; no scheduler exists; no real PAPER lifecycle has run; and PAPER
simulation would not prove LIVE equivalence. Any activation is a separate
owner-gated change.

### QQQ campaign readiness (ADR-0037, 2026-08-25)

`chronos.research.qqq_campaign_readiness` is an authentication-only report. It reads the exact
readiness/specification and referenced source bytes, composes the two existing QQQ research
compilers, and emits immutable blockers. Those inherited compilers load existing Five-Tool
market-data types and certified-reader code, but this operation does not invoke or expose a
market-data read. Direct-import AST tests and a fresh-process probe exclude registry, holdout,
broker, order, persistence, execution, supervisor, service, network, and database authority
dependencies; they do not claim that the dependency closure contains no data-related module.

The report treats the inert PAPER management and opening-admission modules as locked repository
identities only. They are not real PAPER evidence, runtime scheduling, authenticated ongoing
management events, broker-held protection, or authority. It also keeps the six-symbol QQQ release
separate from the seven-symbol base Five-Tool intake; neither catalog nor result transfers by
overlap. Compilation cannot read market data, register a trial, unlock a holdout, construct or
submit an order, or promote a strategy.

### QQQ power analysis (ADR-0038, 2026-08-26)

`chronos.research.qqq_power_analysis` is a content-addressed, pre-data calculator for the
SMA-200 primary cell. It authenticates the exact constitution, control, and Confluence
candidate bytes and recomputes a prospective one-sided mean-test requirement from explicit
design assumptions: 4% minimum annualized post-cost active return, 5% type-I error, 80%
power, 252 sessions/year, and an 8% ceiling on annualized long-run tracking error. The result
is 6,233 completed OOS daily active returns, or 24.7302289281 year-equivalents.

That result is not evidence. Long-run tracking error must include serial dependence; an IID
standard deviation is not an admissible replacement. Robustness cells cannot substitute for
the powered primary and instrument returns cannot be pooled. The independent 100-closed-
position floor must also pass; sessions and positions are never reduced to one numeric
maximum. The absolute date remains unresolved until an owner-approved clean-window start
and covered future session calendar exist. The module imports no Chronos subsystem and can
read no market data, register no trial, unlock no holdout, select no strategy, construct no
order, and grant no promotion or execution authority.

### Certified corporate-action evidence (ADR-0039, 2026-08-26)

Certification v3 commits to the distinct in-window corporate actions it judged, per symbol,
with an order-invariant semantic digest. A positive independent-sample count cannot exceed
the supplied distinct events; duplicates and an all-empty positive panel block. A genuinely
action-free short window requires a separate independent-source attestation over the exact
symbol/date windows, and supplied actions contradict it.

The frozen multi-decade QQQ helper is stricter: all six action files cannot be empty, manifest
counts must equal parsed bytes, and there is no override flag. Its primary-action and
attestation identity fields also normalize and refuse the reviewed IBKR/TWS family markers,
including punctuation/case variants, Trader Workstation, IB Gateway, and `ib_async`.
Unambiguous markers of four or more characters refuse even when embedded in a longer token;
the short `TWS` acronym remains token-exact, with `TWSAPI` explicitly recognized. The guard
is deliberately not a general three-character substring ban: unrelated labels, `TWSE`, and
the token `IB` alone are not evidence of an IBKR relationship.

These checks close internal false-certification and false-independence paths. They do not
establish that an accepted label is truthful, sponsor completeness, or independent-source
truth; certify a real dataset; open a holdout; count a trial; or grant trading authority.

## Hard boundaries

Live transmission is a gated *capability*, not an impossibility (ADR-0009, Milestone 7): it
requires the full configuration conjunction plus the ten-gate live stack, and autonomous
operation additionally requires an owner-authored AutonomyMandate. Under ADR-0017 that
mandate is a **persistent file** (`AUTONOMY_MANDATE_FILE`) auto-activated on boot — the
file's owner-authored content is the grant; the variable only points at it, an invalid or
wrong-account file boots inert, and a revoked mandate stays revoked across restarts. Every
order is a positive-price limit — including the autonomy vocabulary's ADR-0017 `MARKET`
form, which compiles to a **protected** collared limit (quote±1%) and must be granted in the
mandate; a literally unbounded venue market order remains unexpressible. Uncovered short
options are not expressible in the strategy vocabulary, so no mandate can authorize one.
Automated exercise, assignment handling, and broker-wide global cancellation are not
implemented; rolling is a decision kind the gateway will compile as cancel-and-re-propose.

**How to read the rest of this document.** The sections below were written for the
Milestone 1–10 dashboard, when order transmission genuinely was hard-disabled and the UI
exposed only DEMO rehearsals. Two things have happened since, so take every "impossible",
"hard-disabled", "permanently locked", or "no submission path exists" statement below as
**historical M1–M10 posture, not current capability**:

1. Milestones 5–7C (ADR-0009, ADR-0010) delivered the real paper and live order pipeline in
   `chronos.orders`, with one transmit site behind the ten-gate stack.
2. ADR-0016 supersedes per-order confirmation inside an active AutonomyMandate.

The DEMO-rehearsal descriptions remain accurate for that demo path.

Every new short option requires a verified standard share-only deliverable tied to the exact
underlying contract ID and currency; every short call must additionally be covered by currently
held, unencumbered shares in the same pseudonymous account scope. A qualified multiplier or
matching ticker alone is not deliverable or coverage evidence. Missing, stale, delayed, crossed,
inconsistent, or unauthorized data locks opening actions. Chronos never estimates absent broker
quotes, Greeks, volume, open interest, deliverables, or positions.

The first read-only short-put service treats capital provenance as a hard prerequisite. It will
not request candidate market data unless fresh reconciliation proves the entire account has no
positions, open orders, or executions and the target symbol is uniquely flat. It never infers zero
portfolio allocation from a flat ticker, substitutes buying power for cash, or creates a fake
Wheel cycle to persist an evaluation. A ranked result is evidence for a person to inspect, not
authorization to trade; opening actions remain locked even when the resolver says `ELIGIBLE`.
Candidate work begins only after the operator presses the explicit read-only evaluation button;
page entry, ordinary reruns, and symbol changes issue no candidate request. The one optional
session-memory result is labeled historical, is cleared on symbol change or a raised refresh
error, and is never accepted as service or order evidence.

Candidate narrowing is bounded before broker fanout: settings and narrowing cap the universe at 8
expirations, 20 strikes per expiration, and an 80-contract product; qualification and quote entry
points also reject any sequence over 80 contracts. Quote evidence must match its qualified
contract exactly, not merely share a contract ID, and contracts without a verified standard
deliverable are removed before quote requests. A new broker session may improve from `UNKNOWN` to
live or frozen quality during its first request; end-of-window status and each quote must still
pass the rankable-quality and freshness gates.

The expiration-risk preview does not trust the candidate stored in UI session memory. A request
contains only a symbol, contract-ID selection hint, and explicit finite nonnegative total
commission estimate. The service obtains a fresh candidate evaluation internally and withholds the
preview unless that ID resolves uniquely to a newly eligible one-contract short put with coherent
contract and exact chain routing, verified deliverable, currency, quote quality, and quote age. It
also bounds candidate, underlying, reconciliation, and account timestamps against its service
clock and a hard 30-second ceiling, adds reported option age to elapsed evaluation time, requires
the underlying evidence to be an exact stock contract, and re-proves the whole account empty and
the target uniquely reconciled and flat. Finite cash must be at least the gross obligation, and the
capital totals and allocation percentages must be internally consistent. Changing the symbol,
selected contract, commission assumption, or candidate generation clears the stored risk result.
If the contract disappears during refresh, its locked reason remains visible until a control
changes. A refresh failure clears the prior candidate and payoff evidence before it can be shown as
current.

Risk output uses the fresh bid only as a labeled hypothetical credit and models expiration points
at observed spot, strike, effective entry, and zero. The commission is visibly an operator
estimate; explicit zero is visibly fees-excluded. The model is not a forecast, probability,
broker-margin estimate, order what-if, or authorization. It fixes quantity at one, persists
nothing, creates no cycle or draft, calls no broker order method, and keeps opening actions locked.
Commission input is capped at 10,000 currency units, four normalized fractional decimal places, 16
decimal digits, and 32 UI characters; non-finite chart coordinates are withheld. Per-share and
one-contract total amounts are labeled separately.

The next boundary is a deterministic DEMO what-if rehearsal, not an order authorization. It rejects
the attempt before fresh-risk or broker calls unless both settings and the concrete adapter are
DEMO. It reruns the full risk gate, rebinds resolver and capital policy to current settings, limits
input to one positive tick-aligned contract price inside the refreshed spread, and validates the
first account/exposure observation before exactly one non-transmitting demo preview. A second
observation then detects drift. Any identity, capital, exposure, clock, request-echo, commission, or
margin ambiguity withholds the result.

The successful receipt has rehearsal status `WHAT_IF_PREVIEWED`; this is not an order-lifecycle
transition. It carries no raw account ID or broker request and replaces broker messages with a
warning count. It uses the broker's deterministic commission estimate for exact-limit payoff math
while displaying the operator-assumption variance. The limit input is capped at 1,000,000 currency
units, four normalized fractional places, 16 decimal digits, and 32 UI characters. Receipt and
parent evidence live only in session memory. There is no persistence, draft, guardrail decision,
user-confirmation control, submission control, or IBKR what-if path.

Milestone 8 adds one more DEMO rehearsal, not a user-confirmation boundary. A fourth explicit action
requires the operator to type the exact canonical symbol and strict quantity one, affirm the exact
contract ID, limit, and gross assignment obligation from the current receipt, and make an explicit
risk acknowledgement. The request carries only those scalar hints. The service reruns the complete
Milestone 7 what-if boundary and compares the fresh result with every affirmation; it does not
accept the UI-held receipt as evidence.

A successful approval rehearsal ends at `APPROVAL_REHEARSED`, a rehearsal-specific status that is
never `OrderLifecycle.USER_CONFIRMED`. It grants no confirmation authority and leaves the UI at
`Progression: STOPPED` with actions `LOCKED`. It persists nothing, creates no cycle, draft,
guardrail decision, or lifecycle transition, and invokes no submit, modify, or cancel method. There
is no IBKR approval path. On success, the UI discards all M5-M7 parent evidence and typed approval
widgets, retaining only a standalone scalar receipt with no full option contract, broker
descriptive text, raw account identity, or broker-margin output. Milestone 9 wraps that receipt in
the session-only envelope below. A new ancestor attempt or workspace-symbol change terminally
supersedes its terms. Ordinary reruns perform no approval refresh or related broker call, and a
failed newer attempt retains only sanitized feedback rather than leaving older evidence looking
current.

Milestone 9 bounds how long that scalar receipt can remain visible in one process session. The
default display lease is exactly 15 minutes on a monotonic process clock; it is not evidence
freshness and never extends approval authority. Expiry, explicit operator abandonment, or a newer
ancestor evidence attempt produces `EXPIRED`, `ABANDONED`, or `SUPERSEDED`, respectively. Those are
presentation dispositions, never order-lifecycle transitions.

Every terminal transition drops the receipt itself. Its tombstone retains no business or evidence
fields beyond the canonical approval reference, symbol, monotonic timing, a bounded reason enum,
and literal false/locked safety flags—no option contract, limit, obligation, broker text, account
identity, or parent evidence. Clock regression fails closed to expiry, the first terminal
disposition cannot be reopened or changed, and an exact replay cannot extend the lease. These
transitions invoke no broker or financial service and write no persistent state. Session restart
clears all records.

Milestone 10 treats startup identity display as a fail-closed boundary. It derives one immutable
runtime-scope view from the account and connection observations startup already makes. It fully
preflights those facts before persistent account-scope binding, so an invalid observation cannot
bind a fresh database. The bound view is materialized only after binding succeeds. The view
retains a bounded masked account ID and nonfinancial source, environment, data-quality, timing, and
literal lock fields. It excludes the raw account ID, account balances, broker status text,
connection coordinates, credentials, and service objects. The view itself is not persisted, and
rendering it issues no broker request.

Every render revalidates the exact view before exposing either interactive page. Missing or
malformed state produces only generic logged feedback, displays `UNAVAILABLE`, and withholds the
workspace. A valid view is explicitly labeled historical startup identity; it cannot substitute
for current broker health, fresh reconciliation, current market evidence, or order authority.
Mode-aware DEMO, IBKR PAPER, and IBKR LIVE labels never enable an order path, and live-money
transmission remains hard-disabled.

## Order boundary

Paper submission remains off unless all of these are true at the same decision point:

1. A fresh broker reconciliation succeeded.
2. The connected account matches the configured, masked account.
3. The environment is PAPER and the transmission setting is explicitly enabled.
4. A newly fetched quote is live or frozen, fresh, valid, and complete.
5. Duplicate, conflict, capital, concentration, and covered-call checks pass.
6. A broker what-if preview succeeded when supported.
7. The user typed the symbol, confirmed quantity, and chose the final submit action.

Any ambiguity fails closed. The lifecycle is append-only:
`DRAFT -> VALIDATED -> WHAT_IF_PREVIEWED -> USER_CONFIRMED -> SUBMITTED`, followed by a broker
outcome. The UI cannot skip states. This is the contract for a later PAPER submission path; the
current DEMO rehearsal neither enters nor advances this lifecycle.

## Kill switch (delivered in Milestone 6 — this section was stale)

The kill switch **is** wired: `chronos.orders.kill_switch.LiveKillSwitch`, a durable
atomic-write flag (`LIVE_KILL_SWITCH_FILE`) that survives restart, reads as ENGAGED when
corrupt or unreadable (fail-closed), and is cleared only by an explicit operator disengage
with a note. It is gate 9 of the ten-gate live stack, is re-read once more between the
pre-submit compare-and-swap and the transmit line, and is checked again as the adapter's last
line before a mutating call. The session-drawdown breaker engages it automatically on breach.
It blocks new orders and cancels only orders whose Chronos-owned correlation references are
known locally; it never invokes a broker-wide global cancel by default. Cancellation
deliberately still works while it is engaged, because cancelling is risk-reducing.

**Kill-switch precedence is absolute and is not superseded by any mandate** (ADR-0016 §8): an
engaged switch stops autonomous operation exactly as it stops manual operation, and the model
has no tool that can disengage it.

The paragraph that previously stood here — "no kill-switch service is wired… live-money
submission stays hard-disabled" — described the Milestone 5 build and was left stale through
M6/M7. Corrected 2026-07-25.

## Secrets and logs

Chronos never requests IBKR credentials. TWS or IB Gateway performs authentication. `.env` is
ignored; `.env.example` contains only non-secret placeholders. Account identifiers are masked in
the UI and logging filters. The ledger stores a deterministic pseudonymous fingerprint rather than
the raw account ID; it is a scope check, not encryption or anonymization. SQLite and rotating log
files are restricted to the local owner. Broker errors crossing the UI boundary are reduced to a
generic operator message and an exception-type event; their raw text is written to neither the UI
nor the application log. Reconciliation terminal events are aggregate-only: they include status,
snapshot presence, symbol-status counts, and a top-level result-reason count, never reason prose,
symbols, account data, balances, positions, contracts, or order fields. An attempted diagnostic
that fails is not retried and cannot replace the locked result. It does not trigger another read,
persist state, or unlock an action.

## Human responsibility

The operator must verify account, symbol, contract, quantity, limit price, obligation, and
coverage. Strategy-adjusted basis is an internal premium allocation and is explicitly not tax
basis. Assignment-pressure labels are heuristics, never probabilities.
