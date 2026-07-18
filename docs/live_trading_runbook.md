# Live trading runbook

Status: the **Milestone 6 live safety layer is built and tested**. It does not
yet transmit live orders — Milestone 7 wires `transmit=True` for the LIVE branch
at the single submission boundary and validates it with a recording spy. Until
then, `ALLOW_LIVE_TRADING` and a LIVE environment are refused by configuration
validation (framed as awaiting M7, never as permanently disabled).

This runbook describes the safety controls that gate any future live order.

## The ten live gates

A live order may transmit **only** when every gate below passes
(`chronos.orders.live_gate.evaluate_live_gates`, fail-closed — any unmet gate
blocks, there is no default-allow):

1. **config** — live trading is enabled and valid (M7).
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

1. `POST /live/kill` with a clear reason (halts all future live transmission).
2. Cancel working orders through the order workspace / `POST /orders/{id}/cancel`.
3. Investigate; when clear, `POST /live/kill/disengage` with an operator note.
4. Re-arm (`POST /live/arm`) only when ready to resume.
