# ADR-0007 — Runtime mode lock with refused live modes; halt that survives restart

Status: Accepted (2026-07-17). Index entries: DECISIONS.md D-08 and D-09.

## Context

One mistyped variable must not enable live trading, and a process restart must never clear a risk
halt. A boolean "live=true/false" flag fails both requirements: it can be typoed, defaulted, or
reset by a crash.

## Decision

**Modes as a derived capability, not a flag (D-08), in `src/chronos/control/modes.py`:**

- Modes: `RESEARCH`, `BACKTEST`, `REPLAY`, `SHADOW`, `PAPER`, `CANARY_LIVE`, `LIVE`.
- The only constructor of a `ModeLock` is `resolve_mode_lock`, which re-derives the strongest
  capability the evidence supports, denying by default:
  - `RESEARCH`, `SHADOW` → `NO_ORDERS` (shadow produces intents but cannot submit anywhere);
  - `BACKTEST`, `REPLAY` → `SIMULATED_ONLY`;
  - `CANARY_LIVE`, `LIVE` → `DENIED_LIVE_DISABLED`, unconditionally. No configuration, flag, or
    environment variable produces a live-capable lock in this build; the denial is code, and
    removing it requires a future reviewed release.
  - `PAPER` → `PAPER_SUBMISSION` only when ALL of these hold simultaneously: order transmission
    enabled; a non-empty operator-maintained paper allowlist; a broker-reported account id present,
    on that allowlist, and matching the IBKR paper pattern `D[UF]\d{4,}`; and the broker-reported
    environment verified as paper. Any missing condition degrades to `NO_ORDERS` with verbatim
    denial reasons.
- The paper execution adapter is constructible only from a `PAPER_SUBMISSION` lock
  (`src/chronos/execution/brokers/ibkr_paper.py`), so even wiring bugs cannot hand a submitting
  adapter to a non-paper context.

**Persistent halt (D-09), in `src/chronos/control/halt.py`:**

- Halt state lives in `data/platform_halt.json`. Any component may raise a halt; only an explicit
  operator rearm with a non-empty note clears it.
- Reads fail closed: a missing, unreadable, corrupt, or schema-mismatched file counts as HALTED
  (`NEVER_ARMED` / `STATE_CORRUPTION`). A brand-new deployment therefore starts halted until the
  operator arms it once — intentional.
- Writes are atomic (temp file + `os.replace`), so a crash cannot leave a torn file.
- Restart re-loads, never resets, the halt; reconnect additionally requires reconciliation to pass
  before the execution engine will submit (`ExecutionEngine.reconciliation_passed`).

Promotion between modes is evidence, not a switch: `src/chronos/control/promotion.py` writes a
versioned record with gate checks, enforces single-step promotion, and appends a failing
`live_capability_hard_disabled` gate to any promotion into `CANARY_LIVE`/`LIVE`.

## Consequences

- Live trading is impossible in this build; tests assert it
  (`tests/safety/test_safety_invariants.py::TestModeLocks::test_live_modes_are_hard_denied`).
- A live-looking account id (`U…`) cannot pass even if the operator mistakenly allowlists it.
- Operators must rearm after every halt and after first deployment; there is no quiet recovery.
  This is friction by design.
- The CLI (`python -m chronos.cli`) resolves its locks with empty broker evidence, so it can never
  hold `PAPER_SUBMISSION`: its strongest capability is `SIMULATED_ONLY` (backtest); `shadow-scan`
  resolves SHADOW/`NO_ORDERS` and never constructs a broker adapter. The CLI cannot be a broker
  order path.
