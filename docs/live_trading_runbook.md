# Live trading runbook

Status: the **live execution path exists (Milestone 7, ADR-0009)** and was
validated entirely with a recording spy broker — no live order was placed
during development, and no test/CI path can transmit (only fakes and spies are
ever constructed there). `ALLOW_LIVE_TRADING=true` is honored **only** under
the full configuration conjunction (IBKR + official adapter + LIVE environment
+ `ALLOW_ORDER_TRANSMIT` + a `U`-pattern account on a non-empty
`IB_ACCOUNT_ALLOWLIST` + arming and typed-confirmation flags left on); any
other combination refuses at startup with every unmet conjunct named.

Remaining owner actions before first live use: install the official `ibapi`
package (docs/ibkr_setup.md) and perform gateway verification — the adapter's
placeOrder/cancelOrder wiring is fake-ibapi + spy validated, not yet exercised
against a running gateway.

This runbook describes the safety controls that gate every live order.

## Autonomous operation (ADR-0016 / D-16, ADR-0017 / D-17, ADR-0030 / D-34)

~~Under ADR-0016 an active owner-authored **AutonomyMandate** replaces gates 7
(session arming) and 8 (per-order confirmation) — **and only those two** — inside
its bounds.~~ **Corrected 2026-08-02 — the order plane does not implement this
substitution.** ADR-0016 §1 and ADR-0017 §1 *intend* an active owner-authored
**AutonomyMandate** to stand in for gates 7 (session arming) and 8 (per-order
confirmation), and only those two, inside its bounds. The code as of 2026-08-02
does not honour it: `chronos.orders` contains no reference to a mandate at all,
and `src/chronos/orders/submission.py:441` reads the live arming state
unconditionally on every LIVE submit. **A live autonomous order therefore still
requires a current session arm today.** This is open finding 4 in
`docs/VISION_COMPLETION_PLAN.md` §6; choosing which authority model wins — prose
or code — is an owner decision requiring a new ADR, not a documentation edit.
Re-verify with `grep -rc "mandate" src/chronos/orders/` (zero matches) and
`sed -n '441p' src/chronos/orders/submission.py`.

Every other gate in this runbook applies identically to an autonomous order, and
the kill switch takes absolute precedence over any mandate.

~~Nothing here is operable yet. Milestone 1 delivered the mandate and decision
contracts only; the deterministic gateway that admits a decision, the model
worker, and the autonomous execution path are Milestones 2 onward.~~ **Corrected
2026-08-02 — this described the Milestone 1 build.** The autonomy stack is built
and wired through M7.5/ADR-0017: contracts, gateway, admission, sizing, durable
state, compiler, decision queue, session counters, alert delivery, the tick
runtime, and the app-plane wiring all ship (README "Current status"). A backend
booted with a valid, account-matching `AUTONOMY_MANDATE_FILE` auto-activates it
and drives the autonomy tick. What does **not** exist in this repository is a
model worker: `chronos.supervisor.ingress` accepts proposals from an external
process, and no such process ships here, so an unconfigured deployment produces
no decisions. Autonomous **live** operation additionally remains blocked by the
arming contradiction above. Revoking a mandate — like engaging the kill switch —
is an immediate owner action that stops new autonomous exposure; the terminal
exposes it at `POST /terminal/mandate/revoke`, and revocation survives restart.

Opening equity-option cash-secured puts and covered calls add ADR-0030's
selection gate before sizing and compilation. `ENABLE_AUTONOMY_OPTION_SELECTION`
defaults false. A complete, bounded, exact broker evidence snapshot must produce
a canonical receipt, the receipt must commit to and semantically verify against
the account-scoped hash chain, and the existing compiler must independently
reproduce its selected contract and limit price. System/evidence failures are
typed `NO_TRADE` and alert the owner.

For `CANARY_LIVE_AUTONOMOUS` or `CAPPED_LIVE_AUTONOMOUS`, an option decision also
requires a separate owner-authored resolver promotion for exactly that one mode.
The artifact binds the canonical mandate, exact selection policy, account,
resolver versions, and material-source digest; it is checked initially, after
acquisition, and immediately before handoff. Chronos has no creator or promotion
command for it, and this release creates none. Both real IBKR adapters currently
return non-authoritative deliverable evidence, so real IBKR option selection
remains `NO_TRADE` even when evaluation is enabled. Do not create a live artifact
until an authoritative deliverable source, owner gateway verification, full
evidence review, and human sign-off exist.

## The ten live gates

A live order may transmit **only** when every gate below passes
(`chronos.orders.live_gate.evaluate_live_gates`, fail-closed — any unmet gate
blocks, there is no default-allow):

1. **config** — `settings.live_transmission_possible` (the full ADR-0009
   conjunction, re-derived at every read).
2. **connection** — the broker connection is healthy.
3. **reconciliation** — local and broker state are reconciled.
4. **data** — market-data quality is acceptable (not stale/frozen/unknown).
5. **risk** — the structured `OrderRiskDecision` is approved and unexpired.
6. **preview** — the broker what-if preview was accepted.
7. **session_arming** — live trading is armed and the arm is unexpired.
8. **per_order_confirmation** — a typed per-order confirmation matches the
   server-re-derived order/risk summary and is within its TTL.
9. **kill_switch** — the kill switch is disengaged.
10. **session_drawdown** — the intraday drawdown breaker has not tripped.

## Arming (short-lived, backend memory)

Arming authorizes nothing on its own — it is one of the ten gates. It lives in
the running backend process (a restart clears it), carries a TTL
(`LIVE_ARM_TTL_MINUTES`), and can be explicitly revoked.

- Arm: `POST /live/arm` with the exact typed phrase
  (`I ACCEPT LIVE TRADING RISK`). The phrase is compared in constant time and is
  **never logged, persisted, or echoed** — only the arm event (armed / expired /
  revoked) is audited to `live_arm_events`.
- Status: `GET /live/status` (arm state + kill-switch state).
- Disarm: `POST /live/disarm`.

Every mutating endpoint requires the local API token **and** the single-writer
lease (read-only backends refuse).

## Kill switch (durable, fail-closed)

A durable, atomic-write emergency stop (`LIVE_KILL_SWITCH_FILE`). A fresh deploy
is **disengaged** (trades subject to the other gates), but a corrupt/unreadable
file reads as **engaged** (fail-closed). An engaged switch survives a restart;
only an explicit operator disengage (with a note) clears it.

- Engage: `POST /live/kill` with a reason. Any component may engage it (the
  drawdown breaker engages it automatically on a breach).
- Disengage: `POST /live/kill/disengage` with a non-empty operator note.

Actions are audited to `kill_switch_events`.

## Session-drawdown circuit breaker

Establishes a per-session net-liquidation baseline (the first observation of the
trading day, persisted in `SESSION_BASELINE_FILE` so a mid-session restart keeps
the baseline). When the intraday drawdown from that baseline breaches either
`MAX_SESSION_DRAWDOWN_USD` or `MAX_SESSION_DRAWDOWN_PCT`, it **engages the kill
switch** — recovery is an explicit operator action, never a silent re-arm.

## Emergency stop procedure

1. `POST /live/kill` with a clear reason (halts all future live transmission —
   the boundary re-reads the switch as its LAST gate and once more between the
   pre-submit CAS and the transmit line, and the adapter refuses mutating calls
   while it is engaged).
2. Cancel working orders through the order workspace / `POST /orders/{id}/cancel`
   — **cancellation deliberately still works while the kill switch is engaged**
   (it is risk-reducing; an emergency stop halts new exposure AND pulls orders).
3. Investigate; when clear, `POST /live/kill/disengage` with an operator note.
4. Re-arm (`POST /live/arm`) only when ready to resume.

## Live-order operational notes (M7)

- **Re-pricing a live working order:** modify is refused on the live branch
  (the typed confirmation binds the original limit price and a modify walks
  zero gates). Cancel the order and re-propose at the new price — the full
  gate walk applies again.
- **A submit stuck in SUBMISSION_UNKNOWN** (ambiguous failure after the send
  may have started) blocks further live submissions via the reconciliation
  gate, with the stuck intent ids named in the refusal. Resolution paths, in
  order: restart reconciliation resolves it from broker evidence; otherwise
  `POST /orders/{intent_id}/resolve` with a typed operator note performs an
  **audited evidence refresh**. If the broker knows the order, the endpoint
  applies that positive truth. Snapshot absence cannot safely prove rejection,
  so it returns a conflict and leaves SUBMISSION_UNKNOWN locked for later
  positive evidence. Never edit the database directly.
- **Adapter refusals before send** (`BrokerRefusedBeforeSend`) resolve the
  intent to REJECTED automatically — nothing reached the venue, nothing
  wedges, and the refusal reason is recorded on the intent's event trail.

## Crypto family (M7C) operational notes

Crypto rides the **same** boundary, gates, arming, kill switch, and drawdown
breaker as options and stocks — it is spot only (Paxos/Zero Hash via IBKR),
long-only, limit orders only, and fractional. What differs operationally:

- **Enabling the family.** Crypto is deny-by-default: an empty `CRYPTO_ALLOWLIST`
  disables it entirely (the eligibility gate FAILs). Enable by listing symbols
  (e.g. `CRYPTO_ALLOWLIST=BTC,ETH`). A symbol on both `SYMBOL_ALLOWLIST` and
  `CRYPTO_ALLOWLIST` is an ambiguous-configuration error and is refused. Two
  per-order/portfolio caps apply: `MAX_CRYPTO_NOTIONAL_PER_ORDER_USD` and
  `MAX_CRYPTO_ALLOCATION_PCT` (of net liquidation).
- **Fractional quantities.** Crypto quantities are `Decimal` (e.g. 0.005 BTC).
  Venue min-size and size-increment come **only** from the qualified contract's
  ContractDetails; if the gateway does not return them, the venue-conformance
  check is UNKNOWN and the order fails closed — it is never assumed. There is no
  min-notional check (IBKR ContractDetails carries none); the venue's own
  minimum-order rejection plus the per-order MAX notional cap are the guards.
- **Time-in-force.** Crypto orders always accept `DAY` (the safe default);
  setting `CRYPTO_TIME_IN_FORCE=IOC` *additionally* accepts `IOC`, so enabling
  IOC never blocks a plain DAY order. Any other TIF fails the `limit_only` gate.
  Options and stocks remain DAY-only.
- **Allocation cap needs a fresh mark.** The BUY allocation cap is measured
  against a marked crypto valuation; if a held crypto position cannot be marked
  to a fresh quote, the cap is UNKNOWN and the BUY fails closed. A SELL never
  triggers the allocation cap (it reduces exposure).
- **Sessions.** Crypto is ~24/7: the family calendar defaults OPEN, but broker
  session evidence reporting the venue closed (halt/maintenance) wins and blocks
  submission.
- **Paper cannot validate crypto.** IBKR paper accounts have no crypto, so there
  is no paper dry-run for this family. Validate a change with the demo profile
  and the recording-spy pipeline suite, then perform a minimal-size live
  acceptance yourself through the finished app. Owner gateway verification
  requires TWS API ≥ 10.10 (Decimal `totalQuantity`).
