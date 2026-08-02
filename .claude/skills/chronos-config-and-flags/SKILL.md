---
name: chronos-config-and-flags
description: >
  The complete Chronos configuration surface. Load this whenever you ask "what does X
  control", "what is the default", "how do I configure/enable/disable this", "where is
  the kill switch file", "which env var / setting / flag does this", "what does this
  .env entry do", "what is in risk.yaml / risk.example.yaml / risk.research.yaml",
  "which client id does this process use", "is it safe to change this value", or you
  are about to edit .env, add a Settings field, touch a CLI flag, or tune any
  threshold. Covers every env var with default + file:line + safety classification,
  the exact live/paper transmission conjunctions, the inert (read-by-nothing) .env
  vars, client-id allocation, risk YAML schemas, all CLI flags, and the constants that
  act as config. NOT for what a control does at runtime (domain skills), operational
  procedures (chronos-run-and-operate), or build-time environment (chronos-build-and-env).
---

# chronos-config-and-flags

Every knob in Chronos: env vars, config files, CLI flags, and threshold constants —
each with its default, meaning, file:line, and a safety classification. Facts dated
2026-08-02; this is the most volatile skill in the library, so every section ends in
the Provenance table with a one-line re-verification command. When this file and the
code disagree, the code wins — fix this file via a documentation PR.

## 0. How configuration loads, and the four safety classes

Settings live in ONE class: `Settings` in `src/chronos/config/settings.py:32-308`
(pydantic-settings). Env var name = upper-cased field name; sources are the process
environment or `.env`. Three properties you must internalize before editing anything:

1. **`extra="ignore"` swallows typos** (settings.py:38). A misspelled env var (or a
   documented-but-inert one, §4) is silently ignored — you can believe you configured
   something and be wrong with no error. Always `grep -n "<field>" src/chronos/config/settings.py`
   to confirm the name exists before trusting a `.env` edit.
2. **Frozen + cached** (settings.py:42, 304-308): one immutable instance per process.
   A `.env` change does nothing until the process restarts.
3. **Cross-field validation refuses bad combinations at load** (settings.py:163-265),
   naming every unmet conjunct. A refusal at boot is the system working — read the
   message, do not weaken the validator.

Safety classes used in every table here and in
[references/settings-reference.md](references/settings-reference.md):

| Class | Meaning |
|---|---|
| safe-to-change | Tune freely; no coupled invariant, no trading authority |
| operational-care | Has coupled invariants or operational blast radius — read the row's note first |
| owner-gated | Changes trading capability, authority, or an evidence gate. Owner decision required (route via chronos-change-control) |
| forbidden-without-ADR | Changing it violates a stated design rule. Requires a new ADR + owner decision, never a config edit |

The master table of ALL ~75 Settings fields and direct-read env vars is in
`references/settings-reference.md`. The safety-critical subset is inline below.

## 1. THE LIVE CONJUNCTION — what must simultaneously align to transmit

**Enabling transmission is never routine. Changing any value below toward live is
owner-gated; the conjunction exists so that no single flag, ever, is enough.**
(ADR-0009; non-negotiable #1: fail-closed and deny-by-default stay the default.)

### Live transmission (all nine, simultaneously)

Derived — re-derived at every read, never cached — by
`Settings.live_transmission_possible` (`src/chronos/config/settings.py:279-301`), and
independently enforced at settings load (settings.py:165-199, which refuses boot with
`ALLOW_LIVE_TRADING=true` unless ALL of these hold):

| # | Setting | Required value | Verified at |
|---|---|---|---|
| 1 | BROKER_MODE | `ibkr` | settings.py:172-173, 291 |
| 2 | BROKER_ADAPTER | `official_ibkr` (only adapter with a validated live path) | settings.py:174-178, 292 |
| 3 | IB_ENVIRONMENT | `live` | settings.py:179-180, 293 |
| 4 | ALLOW_ORDER_TRANSMIT | `true` (master switch) | settings.py:181-182, 294 |
| 5 | ALLOW_LIVE_TRADING | `true` | settings.py:295 |
| 6 | IB_ACCOUNT_ID | matches `U\d{4,}` (`src/chronos/domain/accounts.py:19`) | settings.py:183-185, 296 |
| 7 | IB_ACCOUNT_ALLOWLIST | non-empty AND contains IB_ACCOUNT_ID | settings.py:187-190, 297-298 |
| 8 | REQUIRE_LIVE_ARMING | `true` (forbidden-without-ADR to disable) | settings.py:191-192, 299 |
| 9 | REQUIRE_TYPED_CONFIRMATION | `true` (forbidden-without-ADR to disable) | settings.py:193-194, 300 |

Configuration is only the FIRST wall. At runtime the live submission path additionally
requires, per order: `live_transmission_possible` re-checked
(`src/chronos/orders/submission.py:359`), a current live-arming session — operator
typed `REQUIRED_ARM_PHRASE` = `"I ACCEPT LIVE TRADING RISK"`
(`src/chronos/orders/arming.py:26`), checked at submission.py:441 — a fresh typed
confirmation, the kill switch DISENGAGED (checked at submission.py:453 and re-checked
inside the CAS-to-transmit window at submission.py:699-709), the writer lease, and
reconciliation. The single `transmit=True` assignment in the whole order plane is
submission.py:745. What those gates DO operationally → chronos-run-and-operate; the
architecture invariant → chronos-architecture-contract.

### Paper transmission (the smaller set)

`Settings.transmission_possible` (settings.py:267-277) — ALL of:

| # | Setting | Required value |
|---|---|---|
| 1 | BROKER_MODE | `ibkr` |
| 2 | IB_ENVIRONMENT | `paper` |
| 3 | ALLOW_ORDER_TRANSMIT | `true` |
| 4 | ALLOW_LIVE_TRADING | `false` |
| 5 | IB_ACCOUNT_ID | non-empty (validator settings.py:225-231) |

The paper submission branch checks this at submission.py:253. Live and paper are
structurally mutually exclusive — `ib_environment` is one enum field, so no Settings
instance can present both (settings.py docstring 281-288).

### The demo default (a fresh checkout)

`BROKER_MODE=demo`, no account, transmit off — both derived properties are False and
no gateway is ever contacted. `scripts/run_demo.py:12-19` additionally FORCES
`BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false` into its
child env, so the demo launcher cannot be misconfigured into transmitting.

### Refused ambiguity

`IB_ENVIRONMENT=live` + `ALLOW_ORDER_TRANSMIT=true` WITHOUT `ALLOW_LIVE_TRADING=true`
refuses at load rather than guessing intent (settings.py:215-224). As of 2026-08-02 no
real IBKR gateway (paper or live) has ever been connected in this project's history —
see chronos-real-gateway-campaign before any gateway work.

## 2. Safety-critical settings — the owner-gated / forbidden subset

Full ~75-row table: `references/settings-reference.md`. This is the subset whose
change is an owner decision (route via chronos-change-control):

| Env var | Default | One line | file:line | Class |
|---|---|---|---|---|
| BROKER_MODE / BROKER_ADAPTER / IB_ENVIRONMENT | demo / official_ibkr / paper | live-conjunction members (§1) | settings.py:45-48 | owner-gated |
| IB_ACCOUNT_ID / IB_ACCOUNT_ALLOWLIST | "" / () | the account + live allowlist | settings.py:71, 80 | owner-gated |
| ALLOW_ORDER_TRANSMIT / ALLOW_LIVE_TRADING / ALLOW_OUTSIDE_RTH | false / false / false | transmission master switch; live branch; RTH escape | settings.py:73-75 | owner-gated |
| ENABLE_PAPER_TRADING | true | paper-branch enable | settings.py:81 | owner-gated |
| REQUIRE_LIVE_ARMING / REQUIRE_TYPED_CONFIRMATION | true / true | the two MUST-STAY-TRUE gates (validator refuses false under live) | settings.py:82, 84 | forbidden-without-ADR |
| LIVE_ARM_TTL_MINUTES / ORDER_CONFIRMATION_TTL_SECONDS | 15 / 20 | arm + confirmation freshness windows | settings.py:83, 85 | owner-gated |
| MAX_OPEN_SHORT_OPTION_CONTRACTS / MAX_OPENING_ORDERS_PER_DAY | 5 / 3 | risk caps (the daily cap is the R-25 control) | settings.py:86-87 | owner-gated |
| MAX_GROSS_ASSIGNMENT_USD / MIN_CASH_BUFFER_USD / MIN_CASH_BUFFER_PCT / MIN_EXCESS_LIQUIDITY_USD | 25000 / 5000 / 0.10 / 10000 | exposure + liquidity floors (defaults assume the dead ~$3k premise — §6 flag) | settings.py:88-91 | owner-gated |
| MAX_SESSION_DRAWDOWN_USD / _PCT | 1000 / 0.02 | drawdown breaker → engages kill switch (runtime.py:298-304) | settings.py:92-93 | owner-gated |
| CRYPTO_ALLOWLIST (+ 3 crypto caps) | () = disabled | empty allowlist disables the family | settings.py:97-103 | owner-gated |
| SYMBOL_ALLOWLIST | AAPL,MSFT,SPY | tradable symbols; also gates /orders propose + reconciliation | settings.py:127 | owner-gated |
| MAX_CONTRACTS_PER_ORDER / MAX_SYMBOL_ALLOCATION_PCT / MAX_TOTAL_WHEEL_ALLOCATION_PCT | 1 / 0.25 / 0.60 | sizing caps | settings.py:143-145 | owner-gated |
| HOLDOUT_UNLOCK_TTL_MINUTES / _SESSIONS_PER_UNLOCK / _MAX_OUTSTANDING_UNLOCKS | 15 / 20 / 2 | research-integrity rationing (ADR-0013) | settings.py:62-64 | owner-gated |
| WALKFORWARD_MIN_TRADES | 20 | FROZEN evidence floor (C4) — below it: INSUFFICIENT_EVIDENCE | settings.py:70 | owner-gated |
| BACKEND_HOST | 127.0.0.1 | non-loopback REFUSED by validator (settings.py:255-259) | settings.py:106 | forbidden-without-ADR |
| LIVE_KILL_SWITCH_FILE | data/live_kill_switch.json | repointing orphans an engaged switch (§3) | settings.py:113 | forbidden-without-ADR (casual repoint) |
| AUTONOMY_MANDATE_FILE | None | the standing autonomy grant; presence auto-activates (§3) | settings.py:121 | owner-gated (highest sensitivity) |

## 3. Safety-critical file paths — read this before touching data/

| Path (default) | Set by | Missing-file semantics | Evidence |
|---|---|---|---|
| `data/live_kill_switch.json` | LIVE_KILL_SWITCH_FILE (settings.py:113) | **Missing ⇒ DISENGAGED.** Corrupt ⇒ ENGAGED (fail closed) | `src/chronos/orders/kill_switch.py:83-92` |
| `data/platform_halt.json` | CLI `--halt-file` (`src/chronos/cli/main.py:395`) | **Missing ⇒ HALTED** (NEVER_ARMED). Corrupt ⇒ HALTED (STATE_CORRUPTION) | `src/chronos/control/halt.py:102-117` |
| mandate JSON (path in AUTONOMY_MANDATE_FILE) | settings.py:121 | Unset/missing ⇒ autonomy inert. **Present + valid ⇒ AUTO-ACTIVATED on every boot** | `src/chronos/api/autonomy_wiring.py:318-350` |
| `data/chronos.db` | DATABASE_URL (settings.py:159) | fresh DB created at schema v7; existing DB fail-closed version/drift check | `src/chronos/persistence/database.py:20, 112-159` |
| `data/platform_ledger.db` | CLI/monitor flags & env | deterministic-platform execution ledger | `src/chronos/execution/sqlite_ledger.py` |
| `data/platform_audit.jsonl` | CLI `--audit-file` (cli/main.py:396) | hash-chained audit log; corrupt tail fails closed | `src/chronos/auditlog/log.py` |
| `research/registry/registry.jsonl` + `registry.head.json` | CLI `--ledger` (cli/main.py:726) | experiment registry: hash chain + out-of-band head anchor (tail-truncation detection) | `src/chronos/registry/ledger.py:8-14, 69` |
| `data/owner_alerts.jsonl` | AUTONOMY_ALERT_FILE (settings.py:122) | owner-alert JSONL sink (0600, fsync); the ONLY push channel — no network sink exists | `src/chronos/supervisor/delivery.py` |
| `data/backend_api_token` | BACKEND_TOKEN_FILE (settings.py:108) | auto-generated 0600 on first boot; 32 bytes → 64 hex | `src/chronos/api/auth.py:22-36` |

Three traps that have already misled people:

- **The two stop mechanisms have OPPOSITE missing-file defaults.** Platform halt:
  missing ⇒ HALTED. Live kill switch: missing ⇒ DISENGAGED — **deleting (or failing to
  restore) `data/live_kill_switch.json` IS disarming the stop.** Backup/restore and
  kill/halt procedures → chronos-run-and-operate.
- **`AUTONOMY_MANDATE_FILE` presence is trading authority.** A running backend + a
  valid mandate file auto-activates on boot with no per-boot human act. Revoked
  mandate versions stay revoked across restarts (autonomy_wiring.py:145-157); an
  invalid or wrong-account file boots inert with a CRITICAL alert
  (autonomy_wiring.py:335-341, 370-386). Treat the file like a credential. Mandate
  semantics → chronos-autonomy-and-mandates.
- **DATABASE_URL is not just a path.** The DB is scope-bound to (broker_mode,
  environment, account fingerprint); repointing detaches the writer lease, kill-switch
  audit, counters, activations, and attempt budgets. Rebinding a populated DB to a
  different account refuses (`database.py:161-201`).

## 4. INERT config — setting these does NOTHING (verified read-by-nothing)

`.env.example:62-69` documents five vars that **no code in src/ or scripts/ reads**
(verified 2026-08-02: `grep -rn` across the repo matches only `.env.example` itself;
`Settings` has no such fields and `extra="ignore"` swallows them silently):

| Inert var | What a reader would assume | Reality |
|---|---|---|
| PLATFORM_HALT_FILE | platform halt path | CLI takes `--halt-file`; env ignored |
| PLATFORM_AUDIT_FILE | audit log path | CLI takes `--audit-file`; env ignored |
| PLATFORM_LEDGER_DB | execution ledger path | CLI/monitor take `--ledger`; env ignored |
| PLATFORM_RISK_POLICY | risk policy path | CLI takes `--policy`; monitor reads CHRONOS_RISK_POLICY instead |
| PAPER_ACCOUNT_ALLOWLIST | paper account gate | `resolve_mode_lock` argument no production caller populates from env (`src/chronos/control/modes.py:74-81`) |

**OPEN question (do not resolve unilaterally):** whether these should be wired into
Settings or deleted from `.env.example`. Either way it changes the config contract —
route via chronos-change-control. Until then: never "configure" the platform through
these names, and never cite them as evidence a control is set.

## 5. Client-id allocation — who connects to the gateway as whom

| Client id | Default | Used by | Evidence |
|---|---|---|---|
| IB_CLIENT_ID | 17 (ge=0) | the ORDER-PLANE backend process: trading connection (`official_ibkr.py:760`, `ibkr.py:246`); the terminal chart's bar requests ride this same connection (`src/chronos/api/bars.py`); restart reconciler pins it (`runtime.py:444`) | settings.py:51 |
| IB_DATA_CLIENT_ID | 18 (ge=1) | the READ-ONLY histdata process (`python -m chronos.histdata`): bars backfill + option snapshots (`src/chronos/histdata/official_client.py:62`, `official_options_client.py:74`) | settings.py:55 |

Why the separation matters:

- TWS/Gateway rejects two live connections sharing an id (settings.py:52-54 comment),
  and id 0 is the TWS master id — hence `ge=1` on the data id and the validator
  refusing equality (settings.py:260-264).
- **Pacing budgets are per-process** (`src/chronos/marketdata/pacing.py:18-31`): the
  backend (chart bars, under IB_CLIENT_ID) and the histdata process (under
  IB_DATA_CLIENT_ID) each self-pace 6 requests/rolling minute + 15s per-key cooldown
  (pacing.py:40-42), but IBKR's real limit may be shared across both (RISK_REGISTER
  R-42). During a backfill, the chart showing stale/refused is the design working —
  see chronos-ibkr-boundary for the pacing doctrine.

## 6. Risk YAML policies (`config/`)

Schema: `RiskPolicy` in `src/chronos/risk/policy.py:21-63` — frozen,
**`extra="forbid"`** (a typo'd key is FATAL here, the opposite of Settings), every
default denies. `config_hash` = first 16 hex of SHA-256 over the sorted dump
(policy.py:60-63). A risk policy governs ONLY what the deterministic-platform risk
engine approves in backtest/shadow — **it cannot enable transmission** (that is §1's
conjunction; stated in `config/risk.research.yaml:7-10`).

Field-by-field (schema default = deny; both shipped files verified 2026-08-02):

| Field | Schema default | risk.example.yaml (`example-1`) | risk.research.yaml (`research-1`) |
|---|---|---|---|
| allowed_symbols | () | [] | SPY,QQQ,IWM,DIA,GLD,TLT |
| allowed_strategy_ids | () | [] | regime_trend_v1, mean_reversion_v1, baseline_buy_hold, baseline_sma_trend, baseline_random_entries |
| allow_long_entries / allow_short_entries | false / false | false / false | true / false (short unsupported) |
| max_bot_capital_usd / max_position_notional_usd / max_aggregate_exposure_usd | 0 | 0 | 10,000,000 each (research latitude) |
| max_symbol_exposure_fraction / max_risk_per_trade_fraction | 0 | 0 | 1.0 / 0.50 |
| max_simultaneous_positions / max_open_orders | 0 | 0 | 1 / 1 |
| max_daily_loss_usd / max_weekly_loss_usd | 0 | 0 | 3000 / 3000 |
| max_drawdown_fraction / max_consecutive_losses | 0 | 0 | 0.95 / 1000 |
| max_quote_age_seconds / max_bar_age_seconds | 0 | 0 | 518400 (6 days — weekend gaps not stale) |
| max_price_deviation_fraction | 0 | 0 | 0.05 |
| max_order_rejections_per_day / cooldown_bars_after_loss_halt | 0 | 0 | 100 / 0 |
| allow_market_orders / allow_margin / allow_averaging_down / allow_pyramiding / allow_options | false | false | false |
| allow_overnight_positions | false | false | true (daily-bar strategies need it) |

**Zero means DENY here, not "no limit"** (risk.example.yaml:33-36) — the inverse of
mandate ceilings, where an unset sizing ceiling under model_discretion renders as
NO_CEILING (`src/chronos/terminal/views.py:147-169`; see chronos-autonomy-and-mandates).

Which engine reads which file when (all defaults verified in argparse/env):

- `risk.example.yaml` (deny-all): default for `chronos.cli` `risk-show`, `shadow-scan`,
  `monitor`, `backtest`, `research walk-forward` (cli/main.py:412, 428, 436, 451, 474),
  `python -m chronos.service --policy` (`src/chronos/service/__main__.py`), and the
  Streamlit monitor via CHRONOS_RISK_POLICY (`streamlit_app.py:32, 53`).
- `risk.research.yaml` (vetted `research-1`): default for `research campaign` and
  `research repro produce` (cli/main.py:497, 527) — it must permit trades to be
  non-vacuous; still zero order capability.
- `config/risk.yaml`: gitignored (.gitignore:18) local copy the operator edits
  deliberately. **It does not exist until the owner authors it.** TASKS.md:61-62
  (owner task, verbatim): if a strategy ever clears the frozen research criteria,
  author a reviewed `config/risk.yaml` AND a promotion record **before any shadow
  run** of a selected strategy. Do not fabricate one to make a command run.

**LIVE OWNER DECISION — flag, never resolve:** `MIN_CASH_BUFFER_USD=5000`
(settings.py:89), the example file's "~USD 3,000 account" comment
(risk.example.yaml:6), and the CLI's `--cash 3000` defaults all assume the dead ~$3k
premise, while the last account snapshot is ≈USD 110
(docs/VISION_COMPLETION_PLAN.md §2). Which number governs is unresolved — surface it,
work against neither silently (see chronos-priorities-and-roadmap owner-decision queue).

## 7. CLI flags — every subcommand, one line each

### `python -m chronos.cli` (prog `chronos-platform`; parser cli/main.py:390-460)

The console script `chronos` is the Streamlit dashboard, NOT this CLI
(pyproject.toml). No subcommand can arm, transmit, or touch the mandate; there is no
`--force` flag anywhere (cli/main.py:1-10 docstring).

| Command | Flags (default) |
|---|---|
| (global) | `--halt-file` (data/platform_halt.json), `--audit-file` (data/platform_audit.jsonl) — :395-396 |
| `status` | none — mode banner + halt + audit-chain verify |
| `halt` | `--reason` (required) |
| `rearm` | `--note` (required) |
| `risk-show` | `--policy` (config/risk.example.yaml) |
| `verify-corpus` | `--registry` (research/strategy_registry.yaml) |
| `verify-audit-log` | none — exit 1 on chain failure |
| `shadow-scan` | `--strategies` (regime_trend_v1,mean_reversion_v1), `--symbols` (SPY,QQQ,IWM,DIA,GLD,TLT), `--data-dir` (research/data/raw), `--policy` (example), `--equity` (3000.0) |
| `monitor` | `--mode` (shadow), `--policy` (example), `--data-dir`, `--symbols` (SPY,QQQ), `--ledger` (None) |
| `backtest` | `--strategy`*, `--symbol`*, `--data-dir`, `--policy` (example), `--cash` (3000.0), `--slippage-bps` (2.0) |
| `skb query` | `--disposition --reason --family --direction --classification --executable --tradable --ported/--not-ported --format` (table) — :702-720 |
| `skb stats` | none |
| `registry stats` / `registry verify` | `--ledger` (research/registry/registry.jsonl) — :843-852 |
| `holdout status` | `--ledger`, `--history-root` (research/data/history) |
| `holdout unlock` | `--window`*, `--reason`*, `--ledger`, `--history-root`; phrase via CHRONOS_HOLDOUT_UNLOCK_PHRASE env, never a flag (:797-816) |
| `research walk-forward` | `--strategy`*, `--symbol`*, `--data-dir`, `--policy` (example), `--ledger`, `--criteria-ref`, `--cash` (3000.0), `--slippage-bps` (2.0), `--test-window`/`--warmup`/`--min-trades` (None ⇒ Settings), `--block-size` (20), `--n-resamples` (1000), `--seed` (0) — :467-486 |
| `research campaign` | `--strategies`, `--symbols`, `--data-dir`, `--policy` (**research**), `--ledger`, `--stage-end` (2021-12-31; holdout is 2022+), `--cash`, `--slippage-bps`, `--warmup` (252), `--test-window` (252), `--min-trades` (20), `--block-size` (20), `--n-resamples` (1000), `--seed` (0) — :488-508 |
| `research repro produce` | `--run-dir`*, `--strategy`*, `--symbol`*, `--data-dir`, `--policy` (**research**), `--cash`, `--slippage-bps`, `--date-start/--date-end`, `--seed` (0), `--timezone` (UTC), `--from-existing` — :519-543 |
| `research repro replay` | `--manifest`*, `--run-dir`*, `--data-dir`/`--policy` (None ⇒ manifest values) — :545-563 |
| `research repro compare` | `--expected`*, `--actual`*, `--require-same-commit` — :565-576 |

(* = required.)

### `python -m chronos.service` (prog `chronos-service`; `src/chronos/service/__main__.py`)

Shadow/paper service loop. `--mode` (shadow|paper, default shadow — live/canary not
selectable; "There is no flag that enables live trading", module docstring),
`--symbols` (SPY,QQQ), `--strategies` (regime_trend_v1,mean_reversion_v1), `--equity`
(3000.0), `--data-dir` (research/data/raw), `--policy` (config/risk.example.yaml),
`--halt-file`, `--audit-file`, `--watch` (loop), `--interval` (3600.0 s).

### `python -m chronos.histdata` (`src/chronos/histdata/__main__.py:44-72`)

Read-only data plane under IB_DATA_CLIENT_ID. `bars`: `--symbols`* (comma list),
`--end-date` (today UTC), `--duration-days` (365), `--history-root`
(research/data/history), `--exchange` (SMART). `options`: `--symbols`*, `--session`
(today UTC), `--horizon-days` / `--strike-window-pct` (None ⇒ the two
OPTION_CAPTURE_* settings), `--history-root`, `--allow-correction`.

### `scripts/*.py`

| Script | Flags |
|---|---|
| initialize_database.py | `--url` (override DATABASE_URL) — :13 |
| paper_soak_report.py | `--database` (None ⇒ DATABASE_URL) — :119-123 |
| run_research.py | `--stage` {dev,val,final,all} (all) — :226 |
| build_skb.py | `--check` (fail non-zero if committed artifacts differ) — :42-46 |
| smoke_test_ibkr.py | positional adapter (official_ibkr\|ib_async); sets CHRONOS_RUN_IBKR_SMOKE=1 and forces transmit off — :15-25 |
| run_backend.py / run_ui.py / run_demo.py / build_strategy_registry.py / build_audit_docs.py | no flags |

## 8. Constants that act as config (change = code change; some are FROZEN)

**Rule: statistical/promotion thresholds are FROZEN-before-observation.** Rows marked
FROZEN may not be tuned to fit an observed result — a change is an owner gate
(chronos-change-control) and the methodology home is chronos-research-methodology.

| Constant | Value | file:line | Role |
|---|---|---|---|
| _DSR_PASS_THRESHOLD | 0.95 | src/chronos/research/walkforward.py:41 | deflated-Sharpe pass floor — FROZEN |
| walk-forward `--min-trades` fallback | 20 | settings.py:70; cli/main.py:504 | C4 sample floor — FROZEN |
| `--block-size` default | 20 | cli/main.py:483, 505 | block-bootstrap block length — FROZEN |
| `--n-resamples` default | 1000 | cli/main.py:484, 506 | bootstrap resamples — FROZEN |
| MAX_RESUBMISSIONS | 3 | src/chronos/supervisor/admission.py:110 | resubmissions of one economic decision before the supervisor stops considering it (ADR-0016/R-31) |
| MAX_LIVE_MANDATE_DURATION | 365 days | src/chronos/autonomy/mandate.py:69 | live/canary mandate ceiling (ADR-0017 raised 30d→365d); renewal is a fresh owner act |
| MARKET_PROTECTION_COLLAR | 0.01 (1%) | src/chronos/supervisor/compiler.py:102 | OrderForm.MARKET compiles to a protected limit at touch±1%, never a venue market order |
| LIVE_PERMITTED_DATA_QUALITIES | {LIVE, DELAYED} | mandate.py:78-80 | qualities a live mandate may permit; must stay ⊆ the kernel's own set |
| PacingController defaults | 6/rolling 1 min + 15 s per-key cooldown | src/chronos/marketdata/pacing.py:40-42 | historical-request budget, per process (§5) |
| BarProvider DEFAULT_TTL / MAX_CACHED_SERIES / MAX_LOOKBACK_DAYS | 15 min / 64 / 3650 | src/chronos/api/bars.py:74, 79, 84 | chart cache TTL / LRU bound / lookback ceiling |
| POLL_MS / CHART_POLL_MS / STALE_INTERVALS | 5000 / 120000 / 3 | src/chronos/terminal/static/terminal.js:86, 123, 90 | panel vs chart poll cadence; staleness marker |
| SESSION_TTL / MAX_SESSIONS | 12 h / 32 | src/chronos/api/terminal_session.py:75, 81 | terminal cookie lifetime; live-session ceiling (refuses new logins, never evicts) |
| _TOKEN_BYTES | 32 (→ 64 hex) | src/chronos/api/auth.py:22 | API-token entropy |
| REQUIRED_ARM_PHRASE | "I ACCEPT LIVE TRADING RISK" | src/chronos/orders/arming.py:26 | typed arm phrase — a constant so it is never serialized/logged |
| WriterLease DEFAULT_TTL | 30 s | src/chronos/utils/locking.py:27 | single-writer lease TTL |
| _RENEWALS_PER_TTL | 3 (⇒ 10 s heartbeat) | src/chronos/api/main.py:117 | one failed renewal demotes the backend to read-only until restart |
| _SQLITE_BUSY_TIMEOUT_MS | 5000 | src/chronos/persistence/database.py:33 | writer-handover wait |
| SCHEMA_VERSION | 7 | database.py:20 | supported main-DB schema (alembic head `0006`) |
| MAX_CANDIDATE_EXPIRATIONS / STRIKES / CONTRACTS | 8 / 20 / 80 | src/chronos/config/limits.py:3-5 | hard caps bounding the env-tunable candidate settings |
| MAX_PAYLOAD_BYTES | 256 KiB | src/chronos/supervisor/ingress.py:65 (used api/routes/autonomy.py:64) | proposal-ingress size cap before parse |
| Panel bounds (journal 50/500, queue 25/200, alerts 50/200; detail 2000 ch) | — | src/chronos/terminal/views.py:117-129 | terminal disclosure bounds |
| MAX_NARRATIVE_CHARS / theses scan | 1200; 400/2000 | views.py:995-1000 | thesis panel bounds |
| MAX_SYMBOL_LENGTH | 32 | src/chronos/terminal/commands.py:108 | terminal symbol-token bound |
| Live / paper account patterns | `U\d{4,}` / `D[UF]\d{4,}` | src/chronos/domain/accounts.py:19-20 | account-id shape checks |

## 9. How to add a config axis (checklist)

1. **One home per value.** If the value already exists as a constant or another
   setting, do NOT add a second source of truth — move it or reference it. Grep first:
   `grep -rn "<value_or_name>" src/chronos/`.
2. Add the field to `Settings` (`src/chronos/config/settings.py`) with a
   **fail-closed default**: off, zero, empty, deny. A fresh checkout with an empty
   `.env` must stay demo-mode, non-transmitting, autonomy-inert.
3. If it interacts with another field, extend `validate_safety_and_ranges`
   (settings.py:163-265) so the bad combination refuses at load with a named reason.
4. Add the entry to `.env.example` with a comment (safe placeholder value only —
   never a real account/path/secret).
5. Add a test in `tests/unit/test_settings.py` proving (a) the default is the safe
   value and (b) the validator refuses the dangerous combination.
6. Classify it (§0 classes) and add the row to
   `references/settings-reference.md` — and to §2 above if owner-gated or forbidden.
7. If it widens trading capability or autonomy authority in ANY way, stop: that is a
   new ADR + owner decision BEFORE the code exists (chronos-change-control).
8. Remember `extra="ignore"`: a test that sets the env var and observes behavior is
   the only proof the name is actually wired. (The §4 inert vars are the cautionary
   tale — documented, believed, read by nothing.)

## 10. When NOT to use this skill

- What a control DOES at runtime → chronos-architecture-contract (invariants),
  chronos-autonomy-and-mandates (mandates/gateway), chronos-wheel-and-options (wheel
  domain), chronos-ibkr-boundary (adapters/pacing).
- HOW to run/stop/arm/revoke/back up → chronos-run-and-operate.
- Build-time environment (venv, lockfile, ibapi install, container traps) →
  chronos-build-and-env.
- Whether you MAY change a value → chronos-change-control (this skill only tells you
  the safety class; that skill owns the gate procedure).
- Statistical thresholds' meaning/derivation → chronos-research-methodology.
- Doc-vs-code contradictions about config (e.g. SECURITY.md staleness) → chronos-docs-map.

## Provenance and maintenance

All facts verified 2026-08-02 against the working tree (branch
`claude/chronos-skills-library-bfbj29`). This skill is volatile by design — before
relying on any section after code changes, run its one-liner:

| Section | Re-verify with |
|---|---|
| §0 loader semantics | `sed -n '32,43p;304,308p' src/chronos/config/settings.py` |
| §1 live conjunction | `sed -n '163,301p' src/chronos/config/settings.py` |
| §1 runtime gates + transmit site | `grep -n "transmission_possible\|is_armed\|is_engaged\|transmit=True" src/chronos/orders/submission.py` |
| §1 arm phrase | `sed -n '24,26p' src/chronos/orders/arming.py` |
| §2 safety-critical rows | `sed -n '45,161p' src/chronos/config/settings.py` (defaults) |
| §3 kill-switch default | `sed -n '83,92p' src/chronos/orders/kill_switch.py` |
| §3 halt default | `sed -n '102,117p' src/chronos/control/halt.py` |
| §3 mandate auto-activation | `sed -n '318,350p' src/chronos/api/autonomy_wiring.py` and `sed -n '126,175p'` (revocation) |
| §3 DB scope binding | `sed -n '161,201p' src/chronos/persistence/database.py` |
| §4 inert vars | `grep -rn "PLATFORM_HALT_FILE\|PLATFORM_AUDIT_FILE\|PLATFORM_LEDGER_DB\|PLATFORM_RISK_POLICY\|PAPER_ACCOUNT_ALLOWLIST" --include="*.py" src/ scripts/` (expect ZERO matches) |
| §5 client ids | `grep -n "ib_client_id\|ib_data_client_id" src/chronos/broker/official_ibkr.py src/chronos/histdata/official_client.py src/chronos/config/settings.py` |
| §5 pacing budget | `sed -n '40,42p' src/chronos/marketdata/pacing.py` |
| §6 risk schema | `sed -n '21,63p' src/chronos/risk/policy.py`; `cat config/risk.example.yaml config/risk.research.yaml` |
| §6 risk.yaml owner note | `grep -n "risk.yaml" TASKS.md .gitignore` |
| §6 capital contradiction | `grep -n "110" docs/VISION_COMPLETION_PLAN.md`; `grep -n "3,000" config/risk.example.yaml` |
| §7 platform CLI flags | `sed -n '390,460p' src/chronos/cli/main.py` (+ :463-576 research, :690-723 skb, :843-867 registry/holdout) |
| §7 service flags | `sed -n '38,60p' src/chronos/service/__main__.py` |
| §7 histdata flags | `sed -n '44,72p' src/chronos/histdata/__main__.py` |
| §7 script flags | `grep -n "add_argument" scripts/*.py` |
| §8 frozen stats thresholds | `grep -n "_DSR_PASS_THRESHOLD" src/chronos/research/walkforward.py`; `grep -n "block-size\|n-resamples\|min-trades" src/chronos/cli/main.py` |
| §8 order-plane constants | `grep -n "MAX_RESUBMISSIONS" src/chronos/supervisor/admission.py`; `grep -n "MAX_LIVE_MANDATE_DURATION" src/chronos/autonomy/mandate.py`; `grep -n "MARKET_PROTECTION_COLLAR" src/chronos/supervisor/compiler.py` |
| §8 infra constants | `grep -n "DEFAULT_TTL" src/chronos/utils/locking.py src/chronos/api/bars.py`; `grep -n "SESSION_TTL\|MAX_SESSIONS" src/chronos/api/terminal_session.py`; `grep -n "SCHEMA_VERSION" src/chronos/persistence/database.py` |
| master table | `references/settings-reference.md` § "Re-verification" |

If any one-liner's output disagrees with this file, the code wins — update this skill
in the same PR that changed the code, per the task contract (chronos-change-control).
