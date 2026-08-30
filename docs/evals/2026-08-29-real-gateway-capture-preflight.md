# Real-gateway capture preflight — market-rule evidence

Date: 2026-08-29

## Task contract

```yaml
plan_phase: 2
primary_kpi: broker_truth
gate_advanced: none
files: read-only capture helper, campaign procedure, focused regression tests, this evaluation
verification: exact-main preflight, red-green focused tests, demo capture/replay, full repository gates, independent non-author review
evidence_artifact: this evaluation; demo output is disposable and is not gateway evidence
owner_gate: real IBKR installation, account configuration, gateway contact, and every later order-capable phase remain owner-gated
open: five real PAPER gateway sessions, completed-orders read, real pacing/permission/session observations, D2 owner data packet
```

## Outcome

The campaign helper now captures the bounded market-rule schedule for every option
contract it successfully qualifies. If a batch contains one bad contract, the helper
retains successful one-by-one recoveries for this read rather than losing all market-rule
evidence. If no contract qualifies, it emits an explicit `not_captured` marker. Adapter or
gateway failures remain captured as errors; the helper never substitutes `min_tick` or a
guessed schedule.

This corrects a stale campaign claim. Both broker adapters and the market-data manager
already implemented `option_market_rules`; the helper simply did not call the existing
read-only path. Completed-orders history remains a real protocol gap and is still recorded
as such.

## Safety and measurement boundary

No real gateway, broker account, credential, market-data byte, holdout, registry trial,
order, funding, or promotion artifact was accessed. The host had no repository `.env`, the
official `ibapi` package was unavailable, the account and authority variables were unset,
and the standard PAPER/LIVE ports were closed. Runtime defaults remained:

```text
BROKER_MODE=demo
BROKER_ADAPTER=official_ibkr
IB_ENVIRONMENT=paper
ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false
AUTONOMY_MANDATE_FILE=unset
transmission_possible=false
live_transmission_possible=false
```

`chronos.cli status` reported RESEARCH / NO_ORDERS, NEVER_ARMED, and LIVE hard-disabled.
The capture helper additionally refuses to start if transmit, live-trading, or mandate
authority is present. This slice adds one observation call only and contains no preview,
submit, modify, or cancel call.

## Red-green evidence

Before the implementation, the demo capture produced 27 steps and zero market-rule steps.
The focused regression then failed on the missing
`symbol:AAPL:option_market_rules` result and on the stale campaign wording:

```text
.venv/bin/pytest -q tests/unit/test_real_gateway_campaign.py
# 2 failed
```

After the implementation:

```text
.venv/bin/pytest -q tests/unit/test_real_gateway_campaign.py
# 2 passed
```

The successful AAPL recovery binds qualified contract `con_id=2002`, exchange `SMART`,
market-rule id `26`, and the complete demo increment schedule `0 -> 0.01`, `3 -> 0.05`.
The MSFT path has no matching PUT fixture and records `not_captured` rather than fabricating
evidence.

## Offline rehearsal

The post-change demo capture produced 29 steps. It ended with zero active subscriptions, a
successful disconnect, and a final DISCONNECTED state. Its manifest says
`gateway_evidence=false`. Offline replay accepts it only with the explicit demo flag:

```text
BROKER_MODE=demo .venv/bin/python \
  .claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py \
  --out <temporary>/session --label preflight-market-rules \
  --symbols AAPL MSFT --max-symbols 2 --allow-demo
# capture complete; 29 steps; gateway_evidence=false

.venv/bin/python \
  .claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py \
  --allow-demo <temporary>/session
# PASS

.venv/bin/python \
  .claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py \
  <temporary>/session
# FAIL: demo evidence refused (exit 1)
```

The temporary rehearsal is deleted after verification; it is deliberately not filed under
real gateway evidence.

## D2 downstream audit

D2 remains correctly blocked before the first market-data read:

```text
python -m chronos.cli registry verify
# canonical registry ledger OK

python -m chronos.cli registry stats
# records=0, trials=0, chain_ok=true

python -m chronos.cli holdout status
# declared_windows=[], burned_windows=[], accrued_sessions=0,
# available_unlock_budget=0, chain_ok=true
```

The registry diagnostics created only an empty ignored lock directory as a process-locking
side effect. It contained no ledger record or trial and was removed immediately; the final
gate verifies that `research/registry/` does not ship.

The authenticated QQQ readiness report returned
`blocked_before_first_data_read`, `data_read_permitted=false`, and
`registered_trials=0`. The reviewed packet helper and wizard already exist, but code cannot
supply the missing external truth: a real six-symbol IBKR export, complete primary
corporate-action streams, an independent action sample, and owner approval of the holdout,
benchmark/cash leg, long-side costs, and TradingView traces.

No catalog or release digest exists, so no deep-trading hypothesis is unlocked and no edge
claim is made.

## Result

**Pragmatic partial.** The future five-session real-gateway campaign will no longer omit a
required market-rule observation, and its demo/non-demo evidence boundary still refuses
false proof. Broker truth and D2 themselves do not advance until the owner supplies their
real external prerequisites.
