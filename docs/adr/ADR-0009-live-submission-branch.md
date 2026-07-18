# ADR-0009: The LIVE branch at the single order-submission boundary (Milestone 7)

Status: proposed (M7 in progress)
Date: 2026-07-18

## Context

Milestones 5-6 delivered a paper-only, human-in-the-loop order pipeline
(`chronos.orders`) with exactly one reachable `transmit=True` assignment
(`chronos/orders/submission.py`), a seven-gate fail-closed submission chain, and a
ten-gate live safety stack (`chronos/orders/live_gate.py`) that is built and tested but
never invoked in production. Settings hard-raise on `ALLOW_LIVE_TRADING=true`. Every
production broker adapter's order methods raise `BrokerSafetyError`.

Milestone 7 (game plan A1) must make live execution *capable* — validated entirely with a
recording spy broker, with no order reaching any venue in development, tests, or CI — while
preserving every locked invariant.

## Decision

### 1. One boundary, two branches, one transmit site

`PaperOrderSubmissionBoundary` is renamed `OrderSubmissionBoundary` (no compatibility
alias — every call site updates). Its `submit()` gains a LIVE branch selected purely by
configuration (`settings.ib_environment`). Both branches converge on the **same, single
`transmit=True` assignment** — the line count of reachable transmit sites in
`chronos.orders` remains exactly one, and the grep/AST structural tests keep enforcing it.
No other module gains any transmit authority.

### 2. Settings: live capability is a strict conjunction, never a single flag

The hard-raise on `ALLOW_LIVE_TRADING=true` is replaced by conjunction validation. A
configuration with `allow_live_trading=True` is **valid only when ALL hold**:

- `broker_mode is IBKR` and `ib_environment is LIVE`
- `allow_order_transmit is True` (the transmission master switch stays required)
- `ib_account_id` matches the IBKR live pattern `^U\d{4,}$`
- `ib_account_allowlist` is non-empty and contains `ib_account_id`
- `require_live_arming is True` and `require_typed_confirmation is True`
  (the MVP live model is owner-armed + per-order-confirmed; disabling either while
  live-enabled refuses — the unattended seam is Phase E scope, not this ADR)

Any other combination raises at load. Additional refusals: `allow_live_trading` with
PAPER or DEMO raises; LIVE + `allow_order_transmit` **without** `allow_live_trading`
raises (ambiguous intent).

New property `live_transmission_possible` mirrors `transmission_possible` for the live
branch. The two are **structurally mutually exclusive**: paper requires
`PAPER and not allow_live_trading`; live requires `LIVE and allow_live_trading`. No
configuration can arm both. Every demo/test/CI configuration yields both `False`.

### 3. Live-mode grant lives in the orders plane; the autonomous plane is untouched

`chronos.control.modes.resolve_mode_lock` (the autonomous plane's lock) keeps hard-denying
CANARY_LIVE/LIVE **byte-identically** — ADR-0007 and the game plan's E4 doctrine forbid
touching it. The orders plane gets its own `chronos.orders.live_mode.resolve_live_submission`
mirroring the paper grant's multi-condition, deny-by-default shape:

- order transmission enabled AND live trading enabled (from settings)
- live account allowlist non-empty
- broker-reported account id present, on the allowlist, and matching `^U\d{4,}$`
  (a paper `DU`/`DF` account on the live allowlist is denied by pattern)
- broker-reported environment verified as **live** (tri-state input: `None`/unverified
  denies; `True`-is-paper denies)

Result is an immutable grant (`may_submit_live`, `live_account_id`, `denial_reasons`)
with deny-by-default on any unmet condition.

### 4. Gate ordering in `submit()` — and why

LIVE branch order of operations:

1. writer lease held (shared with paper)
2. `settings.live_transmission_possible` (config gate)
3. `resolve_live_submission` grant (broker-evidence gate)
4. account match: intent account == connected account == grant account
5. risk decision APPROVED and unexpired (shared)
6. typed confirmation present, fresh, hash-matched (shared)
7. intent exactly USER_CONFIRMED (shared)
8. **the full ten-gate `evaluate_live_gates` walk, evaluated at submit time** with
   evidence gathered *inside* the boundary immediately before use (no caller-supplied
   stale booleans):
   - `config` — `live_transmission_possible`
   - `connection` — broker connection state re-read now
   - `reconciliation` — zero intents currently in SUBMISSION_UNKNOWN (query, not cache)
   - `data` — a fresh quote (age ≤ `max_quote_age_seconds`) for the order's contract AND
     the limit price conforms to the qualified contract's `min_tick`
     (Decimal modulo — tick validity is a data-quality fact)
   - `risk` — same decision as gate 5 (approved, unexpired)
   - `preview` — the stored lifecycle passed WHAT_IF_PREVIEWED (service-enforced ordering,
     re-derived from the persisted intent, not trusted from the caller)
   - `session_arming` — `LiveArmingService.is_armed(now)` (memory, TTL)
   - `per_order_confirmation` — same evidence as gate 6
   - `kill_switch` — `LiveKillSwitch.is_engaged()` read from disk **now**
   - `session_drawdown` — `SessionDrawdownBreaker.check(NLV, now)` with NLV read from the
     broker **now**; an unreadable NLV or corrupt baseline arrives as breached
     (fail-closed), and a breach engages the kill switch as a side effect
9. CAS pre-submit transition (USER_CONFIRMED → SUBMISSION_UNKNOWN); the loser of a
   concurrent submit refuses (no double transmit)
10. the single `transmit=True` line; broker call; failure leaves SUBMISSION_UNKNOWN for
    reconciliation (never auto-retried)

**Why the gate walk sits before the CAS, not after:** a refusal after the CAS write would
strand the intent in SUBMISSION_UNKNOWN with nothing at the broker — the reconciler treats
broker-absence for SUBMISSION_UNKNOWN as *unresolved* (M5 remediation), so a deliberate
late refusal would require manual cleanup. Placing the walk immediately before the CAS
keeps the CAS→transmit window free of new I/O (identical to the paper branch today) while
still evaluating every breaker at submit time — the "kill-switch interruption" scenario
(engaged at any point between confirmation and submit) is caught, because evidence is
gathered inside `submit()`, never earlier.

The live dependencies (arming service, kill switch, drawdown breaker, market data) are
constructor-injected and default to `None`; a boundary constructed without them **refuses
the LIVE branch entirely** (missing machinery = fail closed). The paper branch never
consults them, and paper behavior is byte-compatible with M5/M6.

### 5. Live order object

The live branch transmits only an order that is: LMT (the intent type system permits
nothing else), TIF DAY, `outside_rth=False`, quantity > 0, tick-valid limit price,
qualified contract (`con_id` present), account equal to the verified live account. These
are re-validated at the boundary and again inside the adapter.

### 6. `OfficialIBKRBroker` order methods

`preview_order`, `submit_order`, `modify_order`, `cancel_order` are implemented against
the official `ibapi` interface shape:

- per-call re-verification: connected session manages the configured account; environment
  port re-checked; LMT/DAY/RTH-only; refuses `transmit=False` requests defensively
  (nothing but the boundary builds transmit=True, and previews go through `whatIf`)
- order ids from the existing strictly-monotonic `OrderIdAllocator` seeded by
  `nextValidId`
- `preview_order` uses `whatIf=True` (never transmits by construction)
- submission awaits the correlated `openOrder`/`orderStatus` ack through the existing
  request registry and returns a typed `OrderSubmission`

Because `ibapi` is not installable in this environment, the adapter methods are validated
against a fake ibapi application object (existing test pattern) plus the boundary-level
recording spy; the owner performs gateway verification per the game plan's working
agreement. This limitation is disclosed in the milestone report and docs.

### 7. What proves it (M7e)

- Happy-path spy test: full live walk emits **exactly one** correct order object;
  `submit_calls == 1`; nothing reaches any venue (the spy is the venue).
- Adversarial spy tests, each leaving `submit_calls == 0`: config not live-capable; grant
  denied (paper account, missing allowlist, unverified environment); account mismatch;
  stale/unapproved risk; missing/expired/mismatched confirmation; wrong lifecycle;
  un-reconciled SUBMISSION_UNKNOWN backlog; stale quote; tick-invalid price; not armed /
  arm expired; kill switch engaged (including engaged after confirmation); drawdown
  breached and corrupt-baseline; CAS loser on concurrent submit; live deps absent.
- Settings permutation tests: `live_transmission_possible` false unless the full
  conjunction holds; never true simultaneously with `transmission_possible`; all
  demo/test/CI profiles false.
- Structural tests: still exactly one reachable `transmit=True` in `chronos.orders`;
  `chronos.control.modes` untouched (its tests unchanged); UI/broker isolation unchanged.

## Consequences

- Live capability exists but is inert everywhere except a deliberately-configured live
  deployment: real IBKR live account on an operator allowlist, arming phrase typed within
  TTL, per-order confirmation typed within TTL, kill switch disengaged, drawdown intact,
  fresh data, reconciled state.
- No test, CI job, or dev workflow can transmit: they cannot satisfy the settings
  conjunction, and the only broker they construct is a fake/spy.
- The owner's eventual live acceptance is a manual action through the finished app
  (game plan §6); nothing in this milestone places a live order.
- The autonomous plane's live posture is unchanged: `resolve_mode_lock` still hard-denies
  live modes, `promotion.py` still refuses CANARY_LIVE/LIVE.
