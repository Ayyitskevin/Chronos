# ADR-0035 — Default-off QQQ PAPER position management

Status: **accepted design — owner-gated at merge, 2026-08-25. Proposal-only and not
runtime-wired; no PAPER or LIVE authority.** Index entry: DECISIONS.md D-49.

## Context

ADR-0034 freezes the QQQ Five-Tool candidate, including 1% native stop risk, 1R/2R
targets, breakeven after T1, a 22-session/3-ATR runner, adverse-regime exit, and outer
CVaR/session/drawdown limits. It deliberately leaves executable position management open.
A PAPER entry without a durable post-fill lifecycle would be a misleading milestone: a
signal can be correct while a partial fill, restart, ambiguous send, or stale position
causes the exit to be duplicated, missed, or sized against fiction.

The other architectural danger is authority multiplication. A position module needs to
record opening fills and later broker facts, but those facts are not an owner mandate.
Creating a second local "PAPER authority" object would let a data-recording interface be
misread as permission to trade and would compete with ADR-0016's single supervisor.

## Decision

### 1. The module owns state and proposals, never authority

`chronos.supervisor.position_management` is a deep, explicit submodule with five public
operations: build/register a plan from actual opening fills, evaluate one observation,
record one typed directive resolution, and rehydrate the position by replay. It is not
re-exported or imported by any production module.

The module defines no mandate, grant, enable flag, broker adapter, order request, or send
site. Every emitted directive wraps a normal `ProposedDecision`, declares
`execution_authority="none"`, and names the required path as the existing supervisor and
order pipeline. Recording a fill or broker outcome is truth capture, not authorization.
Any future activation must use the existing authenticated ADR-0016 mandate and retain the
single transmit boundary.

### 2. Registration binds actual fills to the exact candidate

The plan is fixed to PAPER, long QQQ, ADR-0034 candidate SHA-256
`59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054`, and management
policy SHA-256 `7a5b29eb8055b0b4cf0f80476cca200234cfe96afd5327101da7e76ac09ec188`.
It records the account fingerprint, Chronos position and opening-order references,
authoritative entry-fill identities and evidence digests, actual fill price/quantity,
signal-time risk distance, marked strategy NAV, and unit-exposure CVaR observation.

Whole shares are divided into T1, T2, and runner legs when at least three shares exist;
smaller fills use a single 2R closing leg. The stored sequence is canonical—T1, T2,
runner—so a direct model construction cannot silently change target precedence. Native
stop loss must equal quantity times the preserved stop distance. CVaR loss must equal actual
filled notional times the recorded unit-exposure loss fraction. An over-limit actual fill
is still registered as broker truth,
but immediately latches a flatten proposal; refusing to record it would hide exposure.

### 3. Management is event-sourced and semantically replayed

Each position has a dedicated stream in Chronos's existing hash-chain table. Registration,
evaluations, and directive resolutions carry timezone-aware event times. Rehydration first
verifies the hash chain, then reconstructs and recomputes every evaluation from immutable
inputs. A validly rehashed but semantically forged result still refuses.

Observation identities are one-use. Backdated or out-of-order events refuse. A prior
pending directive blocks another; an ambiguous send remains blocked until typed broker
reconciliation resolves it. Partial and complete fills update exact leg balances. T1 moves
the stop to breakeven only after its complete actual fill—not after a proposal or partial
fill. A broker execution identity can reduce the stream only once, including across
different directives and restart replay.

### 4. Fresh broker truth drives a fixed trigger order

An observation must name the same account, carry LIVE-quality evidence no more than five
seconds old, and report a broker position quantity equal to replayed managed quantity.
Chandelier inputs travel together, and the 22-session high cannot be below the current
price. The state machine tightens stops only.

The deterministic precedence is: a previously latched flatten; session loss; drawdown;
leg or runner stop; opposite confirmed regime; then T1/T2. The source-default long AVWAP,
neutral, SMA, and time exits remain off. Full-position safety exits latch through retries;
target and runner reductions affect only their remaining managed leg.

### 5. Activation remains a separate, owner-gated change

This artifact is deliberately unusable as unattended protection. Before any runtime import
or scheduled evaluation, a successor change must provide all of the following and receive
owner review:

1. an authenticated PAPER/account adapter for fills, quote evidence, reconciliation
   generation/session, positions, and outcomes;
2. a database-enforced one-opening-order-to-one-managed-stream identity;
3. a trusted management-event queue seam whose retry identity does not collide under the
   existing model-proposal economic fingerprint and does not trust model-authored nonces;
4. reviewed persistent broker protection semantics for stop/target behavior across process
   death, disconnect, gaps, partial fills, cancellation, and late events;
5. bounded scheduling plus kill, loss, drawdown, and reconciliation integration; and
6. real PAPER lifecycle evidence, including restart and ambiguous-send drills.

## Consequences

Chronos gains an executable specification for post-fill state transitions and can now test
them independently of broker authority. It does not gain a protected PAPER position, a
working order, a runnable campaign, a promotion artifact, or evidence of edge. The existing
proposal queue collision is intentionally preserved as a failing activation condition
rather than weakened to make this module appear connected.
