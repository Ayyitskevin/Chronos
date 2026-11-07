# CPSRECUT read-back

- base: `d2894eaa7f218a4d3aa21da8d708511447825853` (`origin/main`)
- branch: `codex/cps-identity-history-and-inflight-refusal`
- plan phase: Phase 2 — canonical broker and operations plane
- primary KPI: `broker_truth`
- gate advanced: none; code-only mitigation, no real-gateway evidence
- owner gate: required at merge; delegated build authority is `K-20261102-009`
- decision citation discrepancy: the brief names `D-62`, while live `DECISIONS.md`
  identifies D-62 as credential-epoch binding under ADR-0048. This build does not reinterpret
  that decision and changes only the two mechanically specified cancel-settlement behaviors.

files:

- `logs/CPSRECUT-READBACK.md`
- `tests/integration/test_cancel_pending_settle.py`
- `src/chronos/orders/tracker.py`
- `src/chronos/orders/reconciliation_recovery.py`
- `src/chronos/broker/callbacks.py`

contract:

1. Port `test_P1_1_a_broker_id_history_with_two_ids_never_binds_even_if_the_latest_matches`
   onto main's persistence-key-aware `Rig`. Add `OrderTracker.persisted_identities()` and make
   `_completed_record_bound` compare the completed record against the full persisted
   `broker_order_id`, `permanent_id`, and `client_id` sets. An empty, conflicting, or different
   identity set cannot bind; a completed row with broker order id zero may still bind through the
   exact permanent/client identity sets.
2. Port `test_P3_1_a_second_completed_orders_request_is_a_typed_refusal_until_reconnect`.
   `CallbackBridge.start_completed_orders()` must raise `BrokerDataError` for a second id-less
   request while the first remains in flight, and its safe message must instruct the operator to
   reconnect. `reset_for_reconnect()` must permit a fresh request.
3. Do not copy the reader branch's no-record execution-count behavior. Main's existing
   `order_ref` filtering and all other cancel-pending settlement behavior remain unchanged.
4. Prove RED at the base, commit the failing pins, apply the smallest GREEN change, kill one
   removal mutant per guard, and run the unchanged cancel-pending suites plus the required exact-
   head gates under the brief's inert environment.

stop rule:

- BLOCKED if either behavior requires weakening or changing an existing test, if the reader tree
  would need mutation, or if the requested slice expands into no-record execution semantics.
