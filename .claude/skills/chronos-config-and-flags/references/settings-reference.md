# Chronos master configuration reference — every Settings field and direct-read env var

Verified against the repo 2026-08-02. All file:line references are relative to the repo
root. Re-verify any row before relying on it in a money-adjacent change:

```bash
grep -n "<field_name>" src/chronos/config/settings.py
```

Safety classes (defined in SKILL.md §0): `safe-to-change` | `operational-care` |
`owner-gated` | `forbidden-without-ADR`.

## How Settings loads (the contract for every row below)

- Loader: `Settings` (pydantic-settings), `src/chronos/config/settings.py:32-43`.
  Sources: process env or `.env` (`env_file=".env"`), case-insensitive; env var name =
  upper-cased field name.
- `extra="ignore"` (settings.py:38): unknown/misspelled vars are SILENTLY ignored. A typo
  configures nothing and raises nothing.
- `frozen=True` (settings.py:42): process-lifetime immutable by type (ADR-0009 — branch
  selection is derived from settings, so immutability is enforced, not conventional).
- One cached instance per process: `get_settings()` `@lru_cache(maxsize=1)`
  (settings.py:304-308). Changing `.env` requires a process restart to take effect.
- Cross-field validation runs at load in `validate_safety_and_ranges`
  (settings.py:163-265). A bad combination refuses to boot, naming every unmet conjunct.

## 1. Broker / gateway connection

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| BROKER_MODE | `demo` | demo vs ibkr broker construction (`src/chronos/runtime.py:220-241`) | settings.py:45 | owner-gated (live-conjunction member) |
| BROKER_ADAPTER | `official_ibkr` | official TWS API adapter vs `ib_async`; live requires official (settings.py:174-178) | settings.py:46 | owner-gated (live-conjunction member) |
| DEMO_PROFILE | `safety_cases` | demo dataset: `safety_cases` or `empty_account` | settings.py:47 | safe-to-change |
| IB_ENVIRONMENT | `paper` | paper vs live gateway branch | settings.py:48 | owner-gated (live-conjunction member) |
| IB_HOST | `127.0.0.1` | gateway host. Keep loopback; NOT enforced in code | settings.py:49 | operational-care |
| IB_PORT | `7497` | gateway port (TWS paper 7497; IB Gateway paper 4002) | settings.py:50 | operational-care |
| IB_CLIENT_ID | `17` (ge=0) | trading-connection client id; restart reconciler pins it (`runtime.py:444`) | settings.py:51 | operational-care |
| IB_DATA_CLIENT_ID | `18` (ge=1) | read-only histdata-process client id. MUST differ from IB_CLIENT_ID (validator settings.py:260-264); ge=1 because 0 is the TWS master id (comment settings.py:52-54) | settings.py:55 | operational-care |

## 2. Options forward-capture (ADR-0012)

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| OPTION_CAPTURE_EXPIRY_HORIZON_DAYS | `120` (ge=1) | capture window; recorded in every snapshot so out-of-window is absent by policy | settings.py:58 | safe-to-change |
| OPTION_CAPTURE_STRIKE_WINDOW_PCT | `0.20` (0<x≤1) | strike band around spot | settings.py:59 | safe-to-change |

## 3. Holdout-unlock guardian (ADR-0013) — research integrity

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| HOLDOUT_UNLOCK_TTL_MINUTES | `15` (≤120) | unlock grant lifetime | settings.py:62 | owner-gated |
| HOLDOUT_SESSIONS_PER_UNLOCK | `20` (ge=1) | capture sessions accrued per unlock budget unit | settings.py:63 | owner-gated |
| HOLDOUT_MAX_OUTSTANDING_UNLOCKS | `2` (ge=0) | cap on simultaneous outstanding unlocks | settings.py:64 | owner-gated |

The unlock PHRASE is never a setting — `CHRONOS_HOLDOUT_UNLOCK_PHRASE` env var only,
read at `src/chronos/cli/main.py:810` (see §8 below).

## 4. Walk-forward evidence defaults (ADR-0014)

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| WALKFORWARD_TEST_WINDOW_BARS | `63` (ge=2) | out-of-sample window, in bars | settings.py:68 | safe-to-change (research tuning) |
| WALKFORWARD_WARMUP_BARS | `252` (ge=1) | warm-up prefix, in bars | settings.py:69 | safe-to-change (research tuning) |
| WALKFORWARD_MIN_TRADES | `20` (ge=1) | C4 floor — below it the verdict is a blocking INSUFFICIENT_EVIDENCE | settings.py:70 | owner-gated (FROZEN evidence gate) |

## 5. Trading capability flags — THE HIGH-VOLTAGE BLOCK

Every row here is a member of a transmission conjunction or a hard risk cap. See
SKILL.md §1 for the exact conjunctions.

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| IB_ACCOUNT_ID | `""` | the account. Required non-empty for paper transmit (settings.py:225-231); must match `U\d{4,}` for live (`src/chronos/domain/accounts.py:19`) | settings.py:71 | owner-gated |
| ALLOW_ORDER_TRANSMIT | `false` | transmission master switch — a conjunct of BOTH paper and live | settings.py:73 | owner-gated |
| ALLOW_LIVE_TRADING | `false` | live branch. `true` without the FULL ADR-0009 conjunction refuses at load, naming every unmet conjunct (settings.py:165-199) | settings.py:74 | owner-gated |
| ALLOW_OUTSIDE_RTH | `false` | outside regular-trading-hours flag | settings.py:75 | owner-gated |
| IB_ACCOUNT_ALLOWLIST | `()` (empty) | live account allowlist; must be non-empty AND contain IB_ACCOUNT_ID for live (settings.py:187-190) | settings.py:80 | owner-gated |
| ENABLE_PAPER_TRADING | `true` | paper-branch enable | settings.py:81 | owner-gated |
| REQUIRE_LIVE_ARMING | `true` | live arming gate. Validator refuses `false` under live (settings.py:191-192); disabling it in ANY config is forbidden-without-ADR | settings.py:82 | forbidden-without-ADR |
| LIVE_ARM_TTL_MINUTES | `15` (≤120) | arm-token lifetime | settings.py:83 | owner-gated |
| REQUIRE_TYPED_CONFIRMATION | `true` | typed-confirmation gate. Validator refuses `false` under live (settings.py:193-194); same forbidden status | settings.py:84 | forbidden-without-ADR |
| ORDER_CONFIRMATION_TTL_SECONDS | `20` (≤300) | confirmation freshness window | settings.py:85 | owner-gated |
| MAX_OPEN_SHORT_OPTION_CONTRACTS | `5` (ge=0) | open short-option cap | settings.py:86 | owner-gated |
| MAX_OPENING_ORDERS_PER_DAY | `3` (ge=0) | daily opening-order cap (the R-25 control; counted from `order_intents` rows) | settings.py:87 | owner-gated |
| MAX_GROSS_ASSIGNMENT_USD | `25000` | gross assignment-exposure cap | settings.py:88 | owner-gated |
| MIN_CASH_BUFFER_USD | `5000` | cash floor. NOTE: default assumes the old ~$3k premise; account is ≈USD 110 (live owner decision — see SKILL.md §6) | settings.py:89 | owner-gated |
| MIN_CASH_BUFFER_PCT | `0.10` (≤1) | cash floor, fraction | settings.py:90 | owner-gated |
| MIN_EXCESS_LIQUIDITY_USD | `10000` | excess-liquidity floor | settings.py:91 | owner-gated |
| MAX_SESSION_DRAWDOWN_USD | `1000` | session drawdown breaker — engages the live kill switch via SessionDrawdownBreaker (`runtime.py:298-304`) | settings.py:92 | owner-gated |
| MAX_SESSION_DRAWDOWN_PCT | `0.02` (≤1) | same, fraction | settings.py:93 | owner-gated |

## 6. Crypto family (ADR-0010; disabled by default)

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| CRYPTO_ALLOWLIST | `()` (empty) | empty = crypto family entirely disabled | settings.py:97 | owner-gated |
| MAX_CRYPTO_ALLOCATION_PCT | `0.10` (≤1) | crypto allocation cap | settings.py:98 | owner-gated |
| MAX_CRYPTO_NOTIONAL_PER_ORDER_USD | `1000` | per-order crypto cap | settings.py:99 | owner-gated |
| CRYPTO_TIME_IN_FORCE | `DAY` (`DAY`\|`IOC`) | crypto TIF; options/stocks are always DAY | settings.py:103 | owner-gated |

## 7. Backend service

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| BACKEND_HOST | `127.0.0.1` | backend bind address. Validator REFUSES non-loopback (settings.py:255-259) | settings.py:106 | forbidden-without-ADR (loopback is a design rule) |
| BACKEND_PORT | `8765` (1-65535) | backend port | settings.py:107 | safe-to-change |
| BACKEND_TOKEN_FILE | `data/backend_api_token` | API-token path (auto-generated, 0600, `src/chronos/api/auth.py:25-36`) | settings.py:108 | operational-care |

## 8. Safety-critical file paths (full semantics in SKILL.md §3)

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| LIVE_KILL_SWITCH_FILE | `data/live_kill_switch.json` | durable live kill switch. Missing file = DISENGAGED (`src/chronos/orders/kill_switch.py:83-85`); corrupt = ENGAGED (:86-92) | settings.py:113 | forbidden-without-ADR to repoint casually — a path change silently orphans an engaged switch |
| SESSION_BASELINE_FILE | `data/session_baseline.json` | session-drawdown baseline path | settings.py:114 | operational-care (same orphaning hazard) |
| AUTONOMY_MANDATE_FILE | `None` (unset) | the owner's standing autonomy grant. Unset = no autonomy runtime; present+valid = AUTO-ACTIVATED every boot (ADR-0017; `src/chronos/api/autonomy_wiring.py:318-350`) | settings.py:121 | owner-gated — highest sensitivity; the file content IS the authority |
| AUTONOMY_ALERT_FILE | `data/owner_alerts.jsonl` | file alert sink (JSONL, 0600, fsync — `src/chronos/supervisor/delivery.py`) | settings.py:122 | safe-to-change |
| DATABASE_URL | `sqlite:///data/chronos.db` | main DB (schema v7). `file:` URIs refused; DB is scope-bound to one (mode, environment, account) — repointing detaches ALL durable safety state (`src/chronos/persistence/database.py:161-201`) | settings.py:159; alembic override `src/chronos/persistence/migrations/env.py:19-21` | operational-care (heavy — read SKILL.md §3 first) |
| LOG_FILE | `logs/chronos.log` | rotating structured log (5 MiB × 5, 0600, account-masked) | settings.py:161 | safe-to-change |

## 9. Autonomy runtime cadence

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| AUTONOMY_IDLE_INTERVAL_SECONDS | `60.0` (>0) | tick idle cadence | settings.py:123 | operational-care |
| AUTONOMY_MIN_INTERVAL_SECONDS | `5.0` (>0) | tick floor; event hints coalesce, never go below | settings.py:124 | operational-care |
| AUTONOMY_MARKET_TIMEZONE | `America/New_York` | session-counter day boundary. NOT validated at load — an unusable value surfaces as `/terminal/counters` 503 (`src/chronos/api/routes/terminal.py:592-599`) | settings.py:125 | operational-care |

## 10. Wheel strategy tuning

| Env var | Default | What it controls | Read at | Safety class |
|---|---|---|---|---|
| SYMBOL_ALLOWLIST | `AAPL,MSFT,SPY` | tradable symbols (comma list; alnum, no dups, non-empty — settings.py:200-205); also gates `/orders` propose and reconciliation expectations | settings.py:127 | owner-gated |
| TARGET_ABS_DELTA / MIN_ABS_DELTA / MAX_ABS_DELTA | 0.30 / 0.20 / 0.35 | delta band (ordering validated settings.py:206-207) | settings.py:128-130 | safe-to-change (strategy tuning) |
| MIN_DTE / TARGET_DTE / MAX_DTE | 7 / 21 / 45 | days-to-expiry band (ordering validated :208-209) | settings.py:131-133 | safe-to-change |
| MAX_EXPIRATIONS | `6` (≤8) | candidate-request expirations cap; product with strikes ≤ 80 (settings.py:210-214, `src/chronos/config/limits.py:3-5`) | settings.py:134 | operational-care |
| MAX_STRIKES_PER_EXPIRATION | `12` (≤20) | candidate-request strikes cap | settings.py:135-137 | operational-care |
| MIN_OPTION_VOLUME | `10` | liquidity floor | settings.py:138 | safe-to-change |
| MIN_OPEN_INTEREST | `100` | liquidity floor | settings.py:139 | safe-to-change |
| MAX_RELATIVE_SPREAD | `0.20` (>0) | spread cap | settings.py:140 | safe-to-change |
| MAX_QUOTE_AGE_SECONDS | `5` | quote-staleness cap (MarketDataManager, `runtime.py:242-246`) | settings.py:141 | operational-care |
| MARKET_TIMEZONE | `America/New_York` | market timezone; must be installed IANA (validator :243-246). Separate from the hard-coded `utils/time.py:6` constant | settings.py:142 | operational-care |
| MAX_CONTRACTS_PER_ORDER | `1` | per-order size cap | settings.py:143 | owner-gated |
| MAX_SYMBOL_ALLOCATION_PCT | `0.25` (≤1) | per-symbol allocation cap | settings.py:144 | owner-gated |
| MAX_TOTAL_WHEEL_ALLOCATION_PCT | `0.60` (≤1) | total wheel allocation cap | settings.py:145 | owner-gated |
| DELTA_WEIGHT / SPREAD_WEIGHT / DTE_WEIGHT / LIQUIDITY_WEIGHT | 0.45 / 0.30 / 0.15 / 0.10 | candidate resolver weights (sum must be > 0, :232-236) | settings.py:147-150 | safe-to-change |
| ASSIGNMENT_NEAR_ZERO_EXTRINSIC / MEANINGFUL_EXTRINSIC | 0.05 / 0.10 | assignment-pressure heuristic (NEAR_ZERO ≤ MEANINGFUL, :237-240) | settings.py:152-153 | safe-to-change |
| ASSIGNMENT_ELEVATED_ABS_DELTA | `0.50` | assignment heuristic | settings.py:154 | safe-to-change |
| ASSIGNMENT_HIGH_DTE / ELEVATED_DTE | 3 / 5 | assignment heuristic (HIGH ≤ ELEVATED, :241-242) | settings.py:155-156 | safe-to-change |
| ASSIGNMENT_EX_DIVIDEND_WINDOW_DAYS | `5` | ex-dividend window | settings.py:157 | safe-to-change |
| LOG_LEVEL | `INFO` | log level (DEBUG..CRITICAL) | settings.py:160 | safe-to-change |

## 11. Derived properties (never set these — they are computed)

- `settings.transmission_possible` (settings.py:267-277) — the PAPER conjunction,
  re-derived at every read.
- `settings.live_transmission_possible` (settings.py:279-301) — the FULL ADR-0009 live
  conjunction, re-derived at every read to defeat validation bypass
  (`model_copy(update=...)`). Structurally mutually exclusive with paper:
  `ib_environment` is one enum field.

## 12. Env vars read directly, OUTSIDE Settings

| Var | Default | Where read | Purpose |
|---|---|---|---|
| DATABASE_URL | (as above) | `src/chronos/persistence/migrations/env.py:19-21` | alembic URL override |
| CHRONOS_MONITOR_MODE | `shadow` | `src/chronos/monitoring/streamlit_app.py:38` | monitor page mode banner |
| CHRONOS_MONITOR_SYMBOLS | `SPY,QQQ` | streamlit_app.py:43-47 | monitor freshness symbols |
| CHRONOS_HALT_FILE | `data/platform_halt.json` | streamlit_app.py:51 | monitor halt file |
| CHRONOS_AUDIT_FILE | `data/platform_audit.jsonl` | streamlit_app.py:52 | monitor audit file |
| CHRONOS_RISK_POLICY | `config/risk.example.yaml` | streamlit_app.py:53 | monitor risk policy |
| CHRONOS_DATA_DIR | `research/data/raw` | streamlit_app.py:54 | monitor data dir |
| CHRONOS_LEDGER_FILE | unset | streamlit_app.py:48 | optional execution-ledger view |
| CHRONOS_HOLDOUT_UNLOCK_PHRASE | unset | `src/chronos/cli/main.py:810` | holdout unlock phrase — env only, never a flag, never stored/echoed |
| CHRONOS_RUN_IBKR_SMOKE | unset (`1` opts in) | `tests/integration/test_ibkr_smoke.py`; set by `scripts/smoke_test_ibkr.py` | opt-in read-only gateway smoke test |
| CI | set by CI | `tests/safety/test_terminal_client.py` | CI-only test behavior |

`scripts/run_demo.py:12-19` FORCES `BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`,
`ALLOW_LIVE_TRADING=false` into its child environment — the demo launcher cannot be
misconfigured into a transmitting mode.

## Re-verification (whole file)

```bash
# The Settings surface (field names, defaults, validators):
sed -n '1,310p' src/chronos/config/settings.py
# Direct-read env vars:
grep -rn "os.environ" src/chronos/ scripts/ --include="*.py" | grep -v test
```
