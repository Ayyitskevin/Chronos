# Chronos System Architecture

Chronos now contains two cooperating systems in one repository:

1. **Wheel execution plane** (`chronos.orders`, `chronos.api`, `chronos.broker`,
   `chronos.services`, `chronos.strategy`, `chronos.ui`, `chronos.persistence`)
   — the options/stock/crypto order pipeline and its Streamlit front end. Order
   transmission is a **gated, fail-closed capability** (ADR-0009, Milestones 5–7),
   not hard-disabled: one transmit site behind the ten-gate live stack, arming,
   a durable kill switch, a drawdown breaker, and the writer lease. See
   [live_trading_runbook.md](live_trading_runbook.md) for the current posture;
   [architecture.md](architecture.md) records the M1–M10 dashboard history, when
   transmission genuinely was hard-disabled.
2. **Deterministic strategy platform** (this document) — the research,
   backtest, replay, shadow, and paper execution platform built from the Pine
   Quant Library corpus.

Both share the safety doctrine: deny-by-default, fail closed, broker evidence
over local belief, and an unconditional deterministic veto over every order.

The repository-scoped capability matrix and default posture are generated from
executable sources in [generated/CURRENT_STATE.md](generated/CURRENT_STATE.md).
That page reports code paths, not deployment authority or operational evidence.

**Authority model (ADR-0016 / D-16, 2026-07-25).** The former "no generative AI in
any runtime decision path" clause (ADR-0004 §5 / D-11) is superseded. An approved
model may originate runtime trading decisions, but only as a typed
`AITradeDecision` passing through the single deterministic ModelDecisionGateway,
inside an active owner-authored AutonomyMandate. The model cannot access IBKR
directly, change its authorization, weaken policy, or bypass any deterministic
gate. ADR-0004 §§1-4 — the structural separation of authority below — are
**preserved** and are what make that safe. ~~The gateway is Milestone 2; as of
Milestone 1 the contracts (`chronos.autonomy`) exist and are wired into nothing.~~

*(Corrected 2026-08-02 — the paragraph above was frozen at Milestone 1.)* The stack is
built and wired through M7.5/ADR-0017: the deterministic gateway, admission and sizing
(M2), durable supervisor state (M3), the order compiler and decision queue (M4),
market-local session counters (M5), owner-alert delivery (M6), the time-driven tick
runtime (M7), and the app-plane wiring that auto-activates a valid
`AUTONOMY_MANDATE_FILE` on boot (M7.5). See README "Current status" and
`chronos.api.autonomy_wiring`. Two honest qualifications: **no model worker ships in
this repository** — `chronos.supervisor.ingress` accepts proposals from a separate
process, so an unconfigured deployment produces no decisions — and autonomous **live**
submission is still blocked by the unresolved arming contradiction (open finding 4,
`docs/VISION_COMPLETION_PLAN.md` §6; details in `docs/live_trading_runbook.md`).

## Three planes (ADR-0004)

### Research plane — `chronos.marketdata`, `chronos.indicators`, `chronos.specs`, `chronos.strategies`, `chronos.backtest`, `chronos.research`

Pine corpus ingestion and audit tooling (`scripts/build_strategy_registry.py`,
`research/`), CSV historical data with provenance manifests, the
Pine-semantics indicator library, canonical strategy specifications, derived
strategy implementations, the deterministic backtest engine, and the research
runner that stamps every result with code commit, data hashes, and policy
hash. **No module in this plane can reach a broker adapter.**

### Control plane — `chronos.control`, `chronos.risk.policy`, `chronos.cli`

Operating modes and the multi-condition mode lock, the persistent halt store,
promotion-gate records, risk-policy loading, and the operator CLI. The mode
lock is re-derived from configuration plus runtime broker evidence at every
resolution; `CANARY_LIVE` and `LIVE` resolve to a hard-denied capability in
this build — there is no configuration, flag, or environment variable that
produces a live-capable lock.

### Execution plane — `chronos.portfolio`, `chronos.risk.engine`, `chronos.execution`

The event path is exactly:

```
closed bar
  -> Strategy.on_bar (deterministic, sees bars + own position only)
  -> StrategyProposal (no quantity, no account, no broker fields)
  -> portfolio.convert_proposal (whole-share sizing, stop attach)
  -> OrderIntent (deterministic UUIDv5 identity)
  -> RiskEngine.validate (deny-by-default; mints RiskApproval on pass)
  -> ExecutionEngine.submit_approved (halt, mode, reconciliation,
     duplicate-ledger, and approval-identity gates)
  -> ExecutionBrokerPort.submit (simulated | IBKR paper)
  -> BrokerEvent stream (acks, fills, cancels, rejects — the only truth)
  -> OrderStateMachine (illegal transitions halt the system)
  -> SqliteLedger / MemoryLedger + hash-chained audit log
```

Separation of authority is structural, not conventional:

- Strategies emit proposals that cannot express an order (no quantity or
  account fields exist on the type).
- The risk engine's policy object is frozen; there are no setters; strategies
  never hold an engine reference; an internal engine exception becomes a
  denial, never an approval.
- The execution engine refuses any approval not minted by the exact engine
  instance it was wired with (object-identity token), refuses duplicates via
  the durable ledger, and refuses everything while halted, unreconciled, or
  in a non-submitting mode.
- Broker adapters never see strategy logic; the paper adapter re-verifies the
  gateway's managed accounts against the verified paper account before every
  submission.

## Determinism and Pine parity (ADR-0005)

- Bars are processed only when closed; there is no intrabar path, which
  removes the `calc_on_every_tick` repainting class by construction.
- Indicator math is IEEE-754 float64, matching Pine's numeric model; money and
  order prices cross into `Decimal` at the intent boundary and round to tick.
- Fills happen strictly on the bar after the decision (marketable limit at
  min/max(open, limit)); there are no same-bar entries/exits.
- Backtest = replay: repeated runs over identical inputs produce identical
  equity curves and trade lists (asserted in tests).

## Persistence (ADR-0003)

The platform persists to its own files, separate from the wheel dashboard's
account-bound SQLite schema: `data/platform_ledger.db` (order intents,
transitions, fills — append-oriented, WAL, synchronous=FULL),
`data/platform_halt.json` (halt state, atomic replace, fail-closed reads), and
`data/platform_audit.jsonl` (hash-chained audit records). A missing or corrupt
halt file reads as HALTED. A new deployment therefore starts halted until the
operator arms it once.

## Modes and promotion (ADR-0007)

`RESEARCH → BACKTEST → REPLAY → SHADOW → PAPER → (CANARY_LIVE → LIVE)`.
Promotion is evidence, not a switch: `chronos.control.promotion` writes a
versioned record with gate checks; the operator then reconfigures the mode,
which the lock re-derives from live evidence. Single-step promotion only.
The last two modes are recognized vocabulary that this build refuses to arm:
`resolve_mode_lock` returns `DENIED_LIVE_DISABLED` unconditionally, and the
promotion evaluator appends a failing hard-disabled gate check.

PAPER capability additionally requires, simultaneously: transmission enabled,
a non-empty operator-maintained paper allowlist, a broker-reported account id
on that allowlist, the id matching the IBKR paper pattern `D[UF]\d{4,}`, and
broker-reported paper environment. One mistyped environment variable cannot
produce a submitting system.

## Failure containment

Any of the following blocks new orders (and most persist a halt that survives
restart): halt raised (any source), ledger write failure, audit-log failure,
unknown broker event, illegal order-state transition, reconciliation
discrepancy (unknown broker order, missing broker order, unexplained
position — never auto-flattened), stale market data, missing account state,
risk-engine internal error, data-quality blocking issue. Rearm always
requires an explicit operator note (`python -m chronos.cli rearm --note ...`).

## Operational-health projection (ADR-0040)

`chronos.operations.health` converts one immutable fact snapshot into three distinct
diagnostic answers: request liveness, readiness to serve operator inspection, and
lane-specific new-exposure capability. `chronos.api.operational_health` is the bounded
FastAPI adapter: it reads the local store, retained startup/task observations,
reconciliation evidence, and a sanitized connection cache. It does not call the broker.

The projection is display-only. Order, supervisor, risk, broker, and runtime authority code
cannot import it and continues to enforce the actual lease, mandate, reconciliation, arm,
kill-switch, evidence, and deterministic gates. A read-only backend can therefore remain
service-ready for inspection while all trading lanes are blocked.

`chronos.operations.clock` (ADR-0041) optionally samples one fixed local chrony query into a
thread-safe cache for both backend roles. The projection compares chrony's quantitative
maximum-error bound with an explicitly configured threshold and applies freshness separately;
health requests never execute the query. Disabled or uncertain observation remains `UNKNOWN`.
The same structural boundary excludes this observer from authority modules, so it describes
clock evidence but does not itself enforce an order predicate.

## What is intentionally not built

- No live or canary-live order path **in this deterministic platform** (refused in
  code, not just config — ADR-0007 is untouched by ADR-0016). Live capability
  lives only in the `chronos.orders` plane.
- No market orders, shorts, margin, options execution, averaging down, or
  pyramiding anywhere in the platform.
- ~~No AI/LLM component in any runtime path (D-11).~~ **Superseded by ADR-0016 /
  D-16.** A model may originate decisions through the typed `AITradeDecision` +
  ModelDecisionGateway path, under an owner mandate. It still gets no broker
  object, no credentials, no low-level order functions, and no policy, arming, or
  mandate-writing tools; the model worker runs outside the broker-writing process.
  This deterministic platform remains model-free.
- No web-exposed control surface; the CLI is local, and the wheel dashboard
  binds locally.

Architecture decision records: [docs/adr/](adr/).
