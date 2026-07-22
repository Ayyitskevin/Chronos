# ADR-0009: The LIVE branch at the single order-submission boundary (Milestone 7)

Status: accepted (design-panel remediated, 2026-07-18)
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

This ADR was adversarially design-reviewed by a three-lens panel (gate ordering/TOCTOU;
invariants/configuration attack surface; state machine/recovery) BEFORE implementation.
Every confirmed finding is folded in below; §9 records the panel's material corrections.

## Decision

### 1. One boundary, two branches, one transmit site

`PaperOrderSubmissionBoundary` is renamed `OrderSubmissionBoundary` (no compatibility
alias — every call site updates). Its `submit()` gains a LIVE branch selected purely by
configuration (`settings.ib_environment`, immutable per §2). Both branches converge on the
**same, single `transmit=True` assignment**.

The structural enforcement for this **does not exist today and is built in M7** (the
panel corrected an earlier draft that claimed to "preserve" it): a new AST test over
`src/chronos/orders/**/*.py` asserts (a) exactly one `transmit=True` keyword argument in
the package, located in `OrderSubmissionBoundary.submit`; (b) `chronos.orders` imports
nothing from `chronos.execution` or `chronos.risk`; (c) no module outside
`chronos/orders/submission.py` calls `to_order_request(transmit=True)`.

### 2. Settings: live capability is a strict conjunction, never a single flag

The hard-raise on `ALLOW_LIVE_TRADING=true` is replaced by conjunction validation. A
configuration with `allow_live_trading=True` is **valid only when ALL hold**:

- `broker_mode is IBKR` and `ib_environment is LIVE`
- `broker_adapter is OFFICIAL_IBKR` (the only adapter whose order path M7 implements;
  a live config selecting an adapter that cannot transmit must refuse at load, not
  strand intents at first submit)
- `allow_order_transmit is True` (the transmission master switch stays required)
- `ib_account_id` matches the IBKR live pattern `^U\d{4,}$`
- `ib_account_allowlist` is non-empty and contains `ib_account_id`
- `require_live_arming is True` and `require_typed_confirmation is True`

Any other combination raises at load. Additional refusals: `allow_live_trading` with
PAPER or DEMO raises; LIVE + `allow_order_transmit` **without** `allow_live_trading`
raises (ambiguous intent).

`require_live_arming` / `require_typed_confirmation` participate **only** in load-time
validation. They are never consulted at gate-evaluation time: the arming and confirmation
gates are unconditional on both branches, so no flag combination can weaken a gate.

New property `live_transmission_possible` **re-derives the entire conjunction itself**
(every bullet above, including the account pattern and allowlist membership) rather than
trusting that the validator ran — and `Settings` gains `model_config frozen=True`, making
the process-lifetime immutability of branch selection a property of the type, not a
convention. The two transmission properties are **structurally mutually exclusive**:
paper requires `PAPER and not allow_live_trading`; live requires
`LIVE and allow_live_trading`; `ib_environment` is a single enum field, so no Settings
instance can present both.

### 3. Live-mode grant lives in the orders plane; the autonomous plane is untouched

`chronos.control.modes.resolve_mode_lock` keeps hard-denying CANARY_LIVE/LIVE
**byte-identically** — ADR-0007 and the game plan's E4 doctrine forbid touching it. The
orders plane gets its own `chronos.orders.live_mode.resolve_live_submission` mirroring
the paper grant's multi-condition, deny-by-default shape:

- order transmission enabled AND live trading enabled (from settings)
- live account allowlist non-empty
- broker-reported account id present, on the allowlist, matching `^U\d{4,}$`
  (a paper `DU`/`DF` account on the live allowlist is denied by pattern)
- broker-observed environment is live (tri-state; `None`/unverified/paper all deny)

**Evidence sources are broker observations, never settings echoes** (panel finding: the
existing paper wiring feeds `settings.ib_environment` into the "broker-reported"
environment input — a config echo M7 must not copy). Concretely:

- the account id and environment evidence are read from a **fresh
  `connection_status()` / managed-accounts snapshot inside `submit()`**, not bound at
  startup;
- `OfficialIBKRBroker` derives an `observed_environment` from gateway evidence: every
  `managedAccounts` entry matching `^U\d{4,}$` ⇒ live; any `D[UF]\d+` entry ⇒ paper;
  anything else/mixed/absent ⇒ unknown (denies). It is forbidden to derive this field
  from `Settings.ib_environment`;
- a spy test asserts: settings say LIVE but the broker reports a paper account ⇒ the
  grant denies on the environment/pattern reasons, and nothing transmits.

### 4. Gate ordering in `submit()` — evidence order, evaluation order, and the CAS

LIVE branch order of operations:

1. writer lease held (shared with paper)
2. `settings.live_transmission_possible` (config gate)
3. fresh broker evidence snapshot: connection status, managed accounts / observed
   environment, then `resolve_live_submission` grant
4. account match: intent account == freshly-observed connected account == grant account
5. **all remaining broker/DB I/O evidence, gathered up front**: reconciliation query
   (§5), fresh quote for the order's contract, NLV read for the drawdown check
6. **fresh `utc_now()` taken after all I/O**, then the TTL-sensitive checks evaluate
   against it: risk decision APPROVED and unexpired; typed confirmation present, fresh,
   hash-matched; arming unexpired. (Panel finding: a single entry timestamp would let a
   confirmation or arm expire mid-walk yet still pass — with a 20s confirmation TTL the
   staleness could exceed the TTL itself.)
7. intent exactly USER_CONFIRMED; preview evidence per §5
8. the ten-gate `evaluate_live_gates` walk assembled from the evidence above — with the
   **kill-switch file read as the LAST piece of evidence** (it is local-disk, cheap, and
   reading it after the slow broker I/O minimizes the engage-to-transmit window)
9. **a true CAS pre-submit transition**: `record_transition` gains
   `enforce_from_status=True` — inside the same transaction it verifies the intent row's
   current status equals USER_CONFIRMED and refuses otherwise. (Panel finding: the
   existing call is only an event-key one-shot latch; it never compares current status,
   and the LIVE branch stretches the read-to-write window across seconds of broker I/O.
   The event-key stays as the duplicate-submit latch; the status predicate is the state
   guard.) The paper branch adopts the same CAS — strictly stronger, no behavior change
   for legitimate flows.
10. **final kill-switch re-read** (local file) after the CAS win. If engaged: refuse,
    and because no broker call was made, the boundary itself resolves the intent
    SUBMISSION_UNKNOWN → REJECTED synchronously (provably nothing at the venue — §6).
11. the single `transmit=True` line; broker call; a post-send failure leaves
    SUBMISSION_UNKNOWN for reconciliation (never auto-retried).

**Why the gate walk sits before the CAS:** a refusal after the CAS would otherwise strand
SUBMISSION_UNKNOWN (the reconciler deliberately leaves broker-absent SUBMISSION_UNKNOWN
unresolved — M5 remediation), and the reconciliation gate itself would self-block if
evaluated after the marker was written. Steps 10-11 are safe post-CAS precisely because
their refusal legs are provably-not-sent and self-resolving (§6).

**Drawdown evidence semantics** (panel finding — do not conflate "cannot evaluate" with
"breached"): if the NLV read fails, the gate fails via `drawdown_breached=True` in
`LiveGateInputs` **directly**, WITHOUT calling `SessionDrawdownBreaker.check()`, without
touching the baseline file, and without engaging the durable kill switch. The kill-switch
side effect is reserved for a real computed breach or a corrupt in-session baseline
(existing breaker behavior). `_write_baseline`/`check()` additionally refuse to establish
a non-positive baseline. The breaker's engage path is idempotent (`is_engaged()` guard),
so repeated submits during a breach are safe.

The live dependencies (arming service, kill switch, drawdown breaker, market data) are
constructor-injected. A boundary constructed without them **refuses the LIVE branch
entirely** (missing machinery = fail closed) — and `build_runtime` must (a) construct the
live safety services BEFORE the order-management wiring, (b) pass the **same instances**
into the boundary that `AppRuntime`/the `/live` API hold (`LiveArmingService` state is
process-memory: a fresh instance would make `/live/arm` invisible to the boundary and
live trading permanently un-armed while every unit test passes), and (c) fail loudly at
startup if a live-valid configuration is missing any live dependency. An integration test
arms through `runtime.live_arming` (the API path) and asserts the boundary sees it.

### 5. Evidence the gates actually have (panel-corrected)

- **Preview gate:** no persisted artifact proves a preview happened today, and the
  lifecycle advances to WHAT_IF_PREVIEWED even when the broker *declined* the what-if.
  M7 fixes both: `preview()` persists the preview identity on the intent record and
  advances the lifecycle **only when `accepted is True`** (a declined preview leaves the
  intent VALIDATED with a recorded refusal event). The boundary's preview gate checks the
  re-fetched intent record's persisted preview evidence — not a caller-supplied boolean.
- **Reconciliation gate:** new repository query `ids_in_status(SUBMISSION_UNKNOWN)`
  (the existing `active()` cannot filter per-status). The gate blocks while any exist and
  **names the stuck intent ids in the refusal detail** so the operator knows exactly what
  to resolve.
- **Data gate:** a fresh quote (age ≤ `max_quote_age_seconds`) for the order's contract
  AND the limit price conforms to the qualified contract's `min_tick` (Decimal modulo).

### 6. No dead ends: refused-before-send vs failed-after-send, and the operator exit

The panel's most severe finding: the reconciliation gate plus the M5 leave-unresolved
doctrine would otherwise let the FIRST adapter-refused live submit wedge all live trading
forever, with database surgery as the only exit. M7 designs the exits:

- **Refused-before-send:** the adapter distinguishes local refusals (its own
  re-validation raising before any network send — account/port/order-shape/kill-switch
  checks) with a dedicated `BrokerRefusedBeforeSend` error type. For these, the venue
  provably never saw the order, so the boundary synchronously resolves
  SUBMISSION_UNKNOWN → REJECTED with the refusal recorded. Same rule for the post-CAS
  kill-switch re-read (§4 step 10).
- **Failed-after-send** (timeout/disconnect once bytes may have left): stays
  SUBMISSION_UNKNOWN for reconciliation — unchanged doctrine, never auto-retried.
- **Audited operator refresh:** the writer-lease-gated
  `POST /orders/{intent_id}/resolve` endpoint applies matching positive broker evidence.
  Snapshot absence never proves rejection because the broker read and local lifecycle write
  cannot be atomic; the endpoint returns a conflict and leaves the intent
  SUBMISSION_UNKNOWN. The typed note makes the privileged recovery attempt explicit.

### 7. Mutations on the LIVE branch (panel finding: modify was an un-gated transmit path)

- **Modify is REFUSED for live-environment intents in M7.** `modify()` re-prices a
  working order at the venue through zero of the ten gates — on the live branch that
  violates "kill switch engaged ⇒ all live trading is halted" and the per-order
  confirmation invariant (the confirmation hash binds the *original* limit price).
  The M7 operator workflow for re-pricing a live order is cancel + re-propose + full
  gate walk. Gated live modification (fresh typed confirmation binding the new price) is
  deferred scope, recorded in the game plan.
- **Cancel stays un-gated by arming/kill-switch, deliberately.** Cancellation is
  risk-reducing and must work precisely when the kill switch is engaged (emergency
  stop = halt new exposure AND be able to pull working orders). This asymmetry is
  intentional and documented; cancel still requires the writer lease and account scope.

### 8. `OfficialIBKRBroker` order methods

`preview_order`, `submit_order`, `modify_order`, `cancel_order` are implemented against
the official `ibapi` interface shape:

- per-call re-verification: connected session manages the configured account;
  environment/port re-checked; LMT/DAY/RTH-only; refuses `transmit=False` submits
  defensively (previews go through `whatIf=True`, which never transmits by construction)
- **last-line kill-switch check** (panel finding): the adapter takes an optional
  `LiveKillSwitch` and `submit_order`/`modify_order` refuse (refused-before-send) when
  it is engaged — a final local guard that also covers any hypothetical non-boundary
  caller holding the broker object
- local refusals raise `BrokerRefusedBeforeSend` (§6); order ids from the existing
  strictly-monotonic `OrderIdAllocator` seeded by `nextValidId`; submission awaits the
  correlated `openOrder`/`orderStatus` ack through the request registry
- `connection_status()`/managed-accounts expose the observed-environment evidence (§3)

Because `ibapi` is not installable in this environment, the adapter methods are validated
against a fake ibapi application object (existing test pattern) plus the boundary-level
recording spy; the owner performs gateway verification per the game plan's working
agreement. This limitation is disclosed in the milestone report and docs.

New refusal codes so tests assert exact refusals, not string details:
`LIVE_GRANT_DENIED`, `LIVE_GATE_BLOCKED`, `LIVE_DEPENDENCIES_MISSING`, plus the
`source="OPERATOR"` resolution event of §6.

### 9. What the panel changed (record)

CRITICAL: reconciliation-gate dead end → §6 (refused-before-send + operator resolution);
modify as un-gated live transmission → §7 (live modify refused; cancel exempt, documented).
HIGH: pre-submit "CAS" was an event-key latch, not a status CAS → §4 step 9; broker
environment evidence was a settings echo → §3; the structural transmit-site test did not
exist → §1 (built in M7); preview evidence did not exist and accepted=False still advanced
→ §5; ib_async adapter in a live config would dead-end → §2 (adapter conjunct).
MEDIUM/LOW: kill-switch read last + post-CAS re-read → §4; fresh `now` after I/O for TTL
gates → §4 step 6; unreadable NLV must not engage the kill switch or touch the baseline →
§4; `live_transmission_possible` re-derives the full conjunction and Settings freezes →
§2; adapter last-line kill-switch → §8; runtime construction order + shared arming
instance → §4; `ids_in_status` query + stuck-id surfacing → §5; reconciliation_recovery
docstring corrected to match code (absence = unresolved); test-time truth: the no-transmit
invariant in tests rests on broker construction (fakes only), so all test Settings use
`_env_file=None` and a conftest guard fails the run if `get_settings()` ever reports
`live_transmission_possible=True` under pytest.

### 10. What proves it (M7e)

- Happy-path spy test: full live walk emits **exactly one** correct order object
  (LMT, DAY, `outside_rth=False`, tick-valid price, qualified con_id, verified live
  account, `transmit=True`); `submit_calls == 1`; the spy is the only "venue".
- Adversarial spy tests, each leaving `submit_calls == 0`: config not live-capable; grant
  denied (paper account on live allowlist, empty allowlist, unverified/paper observed
  environment with LIVE settings); account mismatch; stale/unapproved risk;
  missing/expired/mismatched confirmation; confirmation expiring during the walk (fresh
  `now` discipline); wrong lifecycle; status flipped out of USER_CONFIRMED after evidence
  gathering but before the CAS (true-CAS proof); un-reconciled SUBMISSION_UNKNOWN backlog
  (with stuck ids in the refusal); stale quote; tick-invalid price; not armed / arm
  expired; kill switch engaged (before the walk AND between CAS and transmit); NLV read
  failure (refuses without engaging the kill switch); drawdown breached; corrupt
  baseline; CAS loser on concurrent submit; live deps absent; live modify refused; cancel
  allowed with kill switch engaged.
- Settings permutation tests: `live_transmission_possible` false unless the full
  conjunction holds; never true simultaneously with `transmission_possible`; all
  demo/test/CI profiles false; frozen Settings rejects mutation.
- Structural AST tests per §1; `chronos.control.modes` untouched (its tests unchanged);
  UI/broker isolation unchanged.
- Integration: arm via the runtime/API instance, boundary sees it; operator refresh
  applies positive broker truth while broker absence leaves SUBMISSION_UNKNOWN locked.

## Consequences

- Live capability exists but is inert everywhere except a deliberately-configured live
  deployment: real IBKR live account on an operator allowlist, official adapter, arming
  phrase typed within TTL, per-order confirmation typed within TTL, kill switch
  disengaged, drawdown intact, fresh data, reconciled state.
- In tests/CI/dev the no-transmit invariant rests on broker construction (only fakes and
  spies are ever constructed) — stated honestly, and double-guarded by `_env_file=None`
  discipline plus the pytest-time conjunction tripwire.
- The owner's eventual live acceptance is a manual action through the finished app
  (game plan §6); nothing in this milestone places a live order.
- The autonomous plane's live posture is unchanged: `resolve_mode_lock` still hard-denies
  live modes, `promotion.py` still refuses CANARY_LIVE/LIVE.
