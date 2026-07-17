# Risk Policy — Doctrine and Mechanics

## Doctrine

1. **Deny by default.** The `RiskPolicy` schema defaults every allowance to
   zero, empty, or false. An absent key is a denial. Unknown keys are
   rejected at load (`extra="forbid"`), so a typo cannot silently widen a
   limit. An all-default policy approves nothing (tested).
2. **Independent.** The engine is constructed by application wiring, never by
   a strategy; its policy is frozen (`model_config frozen=True`); there is no
   runtime mutation surface. Strategies cannot catch their way past it: an
   exception inside the engine yields a denial decision
   (`INTERNAL_ERROR_FAIL_CLOSED`), and the execution engine accepts only
   approval tokens minted by the wired engine instance for the exact intent.
3. **Complete explanations.** Every rejection carries all failed checks as
   machine-readable codes plus human-readable text, recorded in the ledger
   and audit log.
4. **Halts outrank everything.** A persistent halt (operator or automatic)
   denies all intents regardless of policy generosity, survives restart, and
   clears only via an explicit operator rearm with a note.

## Checks evaluated per intent (chronos.risk.engine)

Identity/permission: strategy allowlist, symbol allowlist, direction
enablement, mode capability, halt state, duplicate intent id.

Evidence: account state present, market snapshot present with positive last
price, quote age and bar age within limits (a zero limit denies), limit-price
deviation from last trade within bounds.

Financial: authorized capital nonzero, order notional, aggregate exposure,
per-symbol exposure fraction, per-trade risk from the mandatory stop
(entries without a stop are denied; stops at/above the limit are denied),
max simultaneous positions, max open orders, daily and weekly realized-loss
limits, drawdown from peak equity, consecutive-loss count.

Behavioral: pyramiding denied unless enabled, sells capped at held shares
(no shorts), no market-order type exists at all in the platform.

## Safe defaults in force

| Control | Default |
|---|---|
| Live trading | impossible in this build (mode lock hard-denies) |
| Live account allowlist | not even representable; paper allowlist empty by default |
| Live capital authorization | zero (no field exists to set it) |
| Bot capital / notional / exposure limits | 0 (deny) |
| Margin, shorts, options, market orders | disabled |
| Averaging down / martingale / pyramiding | disabled |
| Trading on stale data | denied (zero default age limit) |
| Trading with unknown account state | denied |
| Auto-resume after restart or disconnect | denied (halt + reconciliation gates) |
| Auto-promotion between modes | denied (promotion records are evidence only) |

## Loss-limit interaction with halts

The risk engine denies new *entries* once a loss limit is reached (using
broker-derived realized figures supplied by the execution wiring), while exit
intents for held shares remain validatable. A persisted halt is stricter: it
blocks **all** submissions, entries and exits alike. Closing a position while
halted is therefore a deliberate operator action taken directly at the
broker, not an automatic emergency order — Chronos never auto-flattens
(Phase 9/12 rule), because an incorrect emergency order can compound the
original problem.

## Options

The platform trades no options. Option risk in the wheel dashboard remains
decision-support only with its own Decimal scenario engine; no options
execution path exists anywhere in the repository.

## Files

- Engine: `src/chronos/risk/engine.py` · Policy schema: `src/chronos/risk/policy.py`
- Example policy: `config/risk.example.yaml` (all-deny; copy to `config/risk.yaml`)
- Tests: `tests/safety/test_safety_invariants.py`, `tests/platform_unit/`, `tests/chaos/`
