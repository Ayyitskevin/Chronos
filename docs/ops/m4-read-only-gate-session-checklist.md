# M4 read-only gateway gate — owner session checklist

**Status: no real IBKR gateway — paper or live — has ever been connected to Chronos.** Every
adapter path is verified against fixtures only (`docs/limitations.md`, `docs/IBKR_RUNBOOK.md`
"Reality check"). **This checklist has not been rehearsed against a real gateway.** It is a
transcription of what the repository's own documents and scripts say the owner does and
records; where it names an expected observation, that is a thing to write down, not a fact.

Audience: the owner. Every act below that touches IBKR — the API install, the paper account,
API permissions, market-data subscriptions, 2FA, gateway restarts, and every `.env` flag — is
the owner's (`docs/VISION_COMPLETION_PLAN.md` §11: "Broker credentials, 2FA, account
configuration, API permissions, and gateway access" are owner-only; "No test result,
backtest, backup, or agent recommendation substitutes for an owner gate"). An agent session
can prepare and verify offline; it cannot run a session for you.

Lines marked `[GAP]` name a mechanism the repository does not have. There the owner records
by hand; each is an M4 prerequisite or follow-on, not something this checklist pretends exists.

## 1. The gate, verbatim (`docs/VISION_COMPLETION_PLAN.md` §7 "Real-gateway read-only gate")

> The owner installs and pins the official IB API, supplies a paper account and market-data
> permissions, enables read-only mode, and keeps every transmit/live flag false.
>
> For at least five sessions, including a gateway restart/reset, capture sanitized evidence
> for exact account scope, server time, account summary, positions, executions, open and
> completed orders, contract qualification, option chains, market rules/minimum ticks,
> trading sessions, quote permissions, pacing, callbacks, and subscription cancellation.
>
> **EXIT:** no mutation call; no leaked subscription, account drift, unexplained callback, or
> pacing failure; captured fixtures replay offline exactly.

Definitions, first use: **gateway** = the IBKR TWS or IB Gateway application that owns the
login and the API socket; Chronos only connects to its local port. **Session** = one connect →
capture → disconnect against a running gateway. **Fixture** = one captured, sanitized session
directory that the offline check can replay. **Sanitized** = raw account ids replaced with
stable pseudonyms before anything is stored in the repository.

## 2. Flags that must hold for every session

The checks in §3 refuse to run otherwise; the values are also the shipped defaults, so a
fresh `.env` that does not set them is already correct.

| Setting (`.env` / environment) | Required for the gate | Shipped default (`src/chronos/config/settings.py`) |
|---|---|---|
| `ALLOW_ORDER_TRANSMIT` | `false` | `false` |
| `ALLOW_LIVE_TRADING` | `false` | `false` |
| `AUTONOMY_MANDATE_FILE` | unset | unset |
| `BROKER_MODE` | `ibkr` for a gateway session (`demo` only for the offline rehearsal) | `demo` |

`ALLOW_LIVE_TRADING=true` by itself — with the other campaign settings at their defaults — is
refused at settings validation (`Settings()` raises). With the full live conjunction
(`BROKER_MODE=ibkr`, `BROKER_ADAPTER=official_ibkr`, `IB_ENVIRONMENT=live`,
`ALLOW_ORDER_TRANSMIT=true`, a live `U…` account on `IB_ACCOUNT_ALLOWLIST`, arming and typed
confirmation required) settings validation accepts the configuration and
`live_transmission_possible` is true (`validate_safety_and_ranges` in
`src/chronos/config/settings.py`; pinned by `tests/unit/test_settings.py`). That is a statement
about the configuration and says nothing about whether a process starts: `build_runtime` can
still fail afterwards (database initialization, adapter construction — where
`verify_environment_port` in `src/chronos/broker/official_ibkr.py` refuses an environment/port
mismatch — broker connection, account summary and scope). It is exactly why every flag in the
table stays at its default for this gate. (`docs/IBKR_RUNBOOK.md` §4 is corrected the same way
on its own branch, DOC-4; that runbook is not edited by this checklist.) A present, valid
`AUTONOMY_MANDATE_FILE` auto-activates autonomy on every backend boot (ADR-0017), so it stays
unset for the whole campaign. The IBKR-side "Read-Only API" option (§3.1) is defense in depth
on top of these flags, not a substitute for them.

## 3. Once, before session 1 (owner prerequisites + offline preflight)

### 3.1 Owner prerequisites (each is a §11 owner act)

1. Install and pin the official TWS API into the Chronos venv (`docs/ibkr_setup.md`
   "Installing the official TWS API"): download from interactivebrokers.github.io, then
   `cd IBJts/source/pythonclient && /path/to/Chronos/.venv/bin/pip install .`. Verify with
   `.venv/bin/python -c "import ibapi; print(ibapi.__file__)"`. **Record the installed
   version string** — there is no lockfile entry; your note is the pin.
2. Log in to TWS or IB Gateway with the **paper** credentials. Paper account ids match
   `D[UF]\d{4,}` (`docs/IBKR_RUNBOOK.md` §1). If the banner does not say paper/simulated, stop.
3. Configure → API → Settings: enable socket clients, keep "Allow connections from localhost
   only", check the **Read-Only API** option, note the socket port (`docs/IBKR_RUNBOOK.md` §2;
   paper ports 7497 TWS / 4002 Gateway; live 7496 / 4001 — if a live port is listening you are
   in a live session; fix the session, do not rely on the adapter's refusal).
4. Enable market data for at least the first symbol in `SYMBOL_ALLOWLIST` — the smoke test
   qualifies only the first symbol and fails on an `UNKNOWN`/empty quote by design
   (`docs/ibkr_setup.md`). Missing permissions surface as explicit missing-data states.
5. Port check (`docs/IBKR_RUNBOOK.md` §3): `ss -tlnp | grep -E '7497|4002'`.

### 3.2 `.env` (never committed; `docs/IBKR_RUNBOOK.md` §4, `docs/ibkr_setup.md`)

```dotenv
BROKER_MODE=ibkr
BROKER_ADAPTER=official_ibkr
IB_ENVIRONMENT=paper
IB_HOST=127.0.0.1
IB_PORT=7497            # or 4002 for IB Gateway — the port the gateway actually shows
IB_CLIENT_ID=17
IB_ACCOUNT_ID=DU1234567 # your real paper id: the official adapter refuses a blank one
ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false
SYMBOL_ALLOWLIST=AAPL,MSFT,SPY
# AUTONOMY_MANDATE_FILE stays unset
```

Never put an IBKR username or password in any Chronos file.

### 3.3 Offline preflight (no gateway needed; an agent may run these)

1. `make gates` green at the commit you will run sessions from. Record the measured counts.
2. Config audit — print the derived conjunctions and require both `False`:
   ```bash
   .venv/bin/python -c "
   from chronos.config.settings import Settings
   s = Settings()
   print(s.broker_mode.value, s.broker_adapter.value, s.ib_environment.value, s.ib_port)
   print('transmit', s.allow_order_transmit, 'live', s.allow_live_trading)
   print('mandate', s.autonomy_mandate_file)
   print('transmission_possible', s.transmission_possible)
   print('live_transmission_possible', s.live_transmission_possible)"
   ```
3. Safety-state inventory (read-only; record the output in the session-1 evidence doc):
   `python -m chronos.cli status` (platform halt + audit chain; CWD-relative default paths
   `data/platform_halt.json`, `data/platform_audit.jsonl`) and
   `ls data/live_kill_switch.json` (a MISSING kill-switch file means DISENGAGED — the
   opposite of the platform halt, whose missing file reads HALTED).
4. The smoke test must SKIP without its opt-in flag:
   `.venv/bin/pytest tests/integration/test_ibkr_smoke.py -ra` → one test skipped, reason
   names `CHRONOS_RUN_IBKR_SMOKE`. If it runs or errors, stop — your environment already
   opts in, or collection is broken.
5. Rehearse the capture harness against the demo broker — proves the harness end to end and
   proves its refusals. Demo output is stamped `gateway_evidence: false` and can never count:
   ```bash
   BROKER_MODE=demo .venv/bin/python \
     .claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py \
     --out /tmp/chronos-rehearsal --label rehearsal --allow-demo
   .venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py \
     --allow-demo /tmp/chronos-rehearsal
   ```
   Expected: the capture writes `capture.json`, `derived_liquid_hours.json`, `manifest.json`;
   the replay prints `[PASS]`. Without `--allow-demo` both refuse — verify that refusal too.

## 4. Every session (sessions 1 … N, N ≥ 5)

Do these in order; write the evidence doc (§4.5) before the next session.

### 4.1 Connect and capture

1. Gateway running and authenticated (2FA is yours). Config audit (§3.3 step 2) — expected
   identical to session 1; any difference is explained before connecting.
2. Session 1 only: the read-only smoke test, `.venv/bin/python scripts/smoke_test_ibkr.py`.
   The launcher force-sets `CHRONOS_RUN_IBKR_SMOKE=1`, `BROKER_MODE=ibkr`,
   `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false` and runs
   `tests/integration/test_ibkr_smoke.py` (connect → server time → account summary → qualify
   first symbol → option-chain metadata → one bounded quote → cancel that quote →
   disconnect; it never calls `preview_order`, `submit_order`, `modify_order`,
   `cancel_order` — `docs/ibkr_setup.md` "Run the smoke test"). A pass is the first gateway
   contact in the project's history: record the timestamp and the gateway version. On any
   message mentioning pacing: stop the session, record the exact code and text, wait for the
   window (`docs/IBKR_RUNBOOK.md` §7 "Pacing violations"); do not loop retries.
3. The capture run (the record of the session):
   ```bash
   .venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py \
     --out ~/chronos-gateway-evidence/$(date +%F)-session-<N> --label session-<N>
   ```
   The harness refuses to start unless `ALLOW_ORDER_TRANSMIT` and `ALLOW_LIVE_TRADING` are
   false and `AUTONOMY_MANDATE_FILE` is unset; it never calls an order method; a step that
   fails is recorded as `{"error": …}` and the capture continues — absence of evidence is
   evidence.

### 4.2 What is captured, per §7 category

The harness writes one `capture.json` (every observation keyed by step name),
`derived_liquid_hours.json`, and `manifest.json` (sha256 per file + session metadata).

| §7 category | Captured as (step names in `capture.json`) | Expected observation to record — not a fact until seen |
|---|---|---|
| exact account scope | `connection_status`, `account_summary` (the summary must match `IB_ACCOUNT_ID`; ids are pseudonymised, §5) | one account, the configured one |
| server time | `server_time` + `server_time_offset` (local-clock bracket) | the offset is a baseline; no expected value exists for a first contact |
| account summary | `account_summary` | paper balances; values IBKR itself moves between resets are explained, not alarming |
| positions | `positions` | likely empty on an untouched paper account — emptiness is evidence |
| executions | `executions` | likely empty; if not, see §5 (order/exec ids) |
| open and completed orders | `open_orders`; `completed_orders` is written as a `not_captured` marker | `[GAP]` no `def completed_orders` read exists on the `Broker` protocol (`src/chronos/broker/base.py` exposes `open_orders` and `executions` only). Record the marker; completed orders are checked by the owner in the gateway's Trade Log (§4.3 step 3) |
| contract qualification | `symbol:<S>:qualify_underlying`, `symbol:<S>:qualify_option_contracts`, `symbol:<S>:qualify_option:<expiry>:<strike>:<right>` | the REAL `liquid_hours`, `time_zone_id`, `min_tick`, deliverable fields versus the fixture-era assumptions — record every divergence verbatim (§4.4) |
| option chains | `symbol:<S>:option_chain_parameters`, `symbol:<S>:option_specs` | bounded narrowing; a `no populated chain` / `no usable spot price` error is a recorded observation |
| market rules/minimum ticks | `symbol:<S>:option_market_rules` plus `min_tick` on each qualified contract | one entry per qualified option with an increment schedule; `not_captured` means no option qualified |
| trading sessions | `derived_liquid_hours.json` — the raw `liquid_hours` strings parsed by `src/chronos/services/liquid_hours.py` and `confirms_open` at the captured instant | `parsed: null` means the parser met a real string it cannot read → a real finding, record the string |
| quote permissions | `symbol:<S>:underlying_quote` → `data_quality` | which tier the account actually gets (LIVE / FROZEN / DELAYED); `UNKNOWN` = permissions missing |
| pacing | `symbol:<S>:historical_bars_1d_30d` result or error, plus any error text anywhere in the capture | codes the gateway actually sends; the official adapter classifies `{100, 420}` as pacing (`src/chronos/broker/callbacks.py`) — record what arrives, classified or not |
| callbacks | `callback_notices` (the bridge's notice log) + every step error | each code classified against the benign set, the connection-uncertain set `{1100, 1101, 1102, 1300, 2110}`, or the pacing/permission sets in `src/chronos/broker/callbacks.py`; anything else is "unexplained" until you explain it in prose |
| subscription cancellation | `active_subscription_count_before_disconnect`, `disconnect`, `final_connection_status` | `0`, `{"ok": true}`, `connected: false`. Nonzero = a leaked subscription = EXIT blocker until explained |

### 4.3 Per-session checks (write each result down; "checked, none" counts)

1. Leak check: `active_subscription_count_before_disconnect == 0`, `disconnect: ok`,
   `final_connection_status.connected == false`.
2. Mutation check, Chronos side — a genuinely read-only inspection of the order database
   (default `DATABASE_URL` = `sqlite:///data/chronos.db`). Snapshot first, then read the copy:
   ```bash
   S=~/chronos-gateway-evidence/$(date +%F)-session-<N>
   [ -e data/chronos.db ] && sqlite3 -readonly data/chronos.db ".backup '$S/chronos.db.snapshot'" \
     || echo "no order database exists — record that; it is the strongest no-mutation evidence"
   sqlite3 -readonly "$S/chronos.db.snapshot" "SELECT count(*) FROM order_intents;"
   ```
   Expected: `0` every session if the backend order plane never ran, or a count unchanged
   session over session. `-readonly` refuses a missing file instead of creating one, and
   refuses any write; the snapshot's sha256 goes in the evidence doc.
   Do NOT use `scripts/paper_soak_report.py` for this against the real database: it calls
   `database.initialize()`, which initializes a schema on an empty target — it creates the
   SQLite file, every table and a `schema_version` row (`src/chronos/persistence/database.py`)
   — so its exit 0 proves nothing about mutation.
3. Mutation check, gateway side (owner): the TWS/Gateway order log shows zero orders from the
   Chronos client id. Record "checked, none".
4. Account-drift check (session ≥ 2): `[GAP]` no `session_drift_report` tool exists — diff
   `account_summary` and `positions` against the previous session by hand (`diff` / `jq`).
   Expected identical on an untouched paper account except values IBKR moves at its own
   resets; every difference gets an explanation; an unexplainable one is an EXIT blocker.
5. Callback check: `[GAP]` no `classify_callbacks` tool exists — classify every code in
   `callback_notices` by hand against the sets in `src/chronos/broker/callbacks.py`.
6. Pacing check: any pacing code or message → session stopped per §4.1 step 2, recorded
   verbatim. A recorded, understood pacing message with a stopped session is an observation;
   an ignored one is a failure.
7. Sanitization check (§5) before the directory leaves the machine.

### 4.4 The first-contact ledger (why the campaign exists)

For each qualified contract, compare reality with the fixture-era assumptions and record
verbatim — a fixture-derived expectation being wrong is the campaign succeeding:

- `liquid_hours`: format vintage (legacy `20090507:0700-1830` vs current
  `20180323:0400-20180323:2000`; separators `,` vs `;`; `CLOSED` days; `2400` closes). Did
  `parse_liquid_hours` parse it?
- `time_zone_id`: is the real value one `src/chronos/services/liquid_hours.py` maps?
- options: did `deliverable_verified` come back `True` with `deliverable_shares ==
  multiplier`? A standard-looking contract failing the screen = record the raw fields.
- market rules: one `option_market_rules` entry per qualified option, same `con_id` and
  exchange, positive `market_rule_id`, schedule beginning at zero — or the error, verbatim.

Fixes to parsers or fixtures happen AFTER the campaign, each with a new test carrying the
real captured string; never edit a captured file to match an expectation (the manifest makes
that detectable).

### 4.5 The session evidence doc

One markdown file per session, off-repo until the gate-passage PR (§6). Required fields:
date/time (UTC + gateway-local), gateway app + version, `ibapi` version string (the pin),
account fingerprint (never the raw id), config-audit output (§3.3 step 2), smoke result
(session 1), capture directory + the `manifest.json` sha256s, every step error classified,
first-contact ledger entries (§4.4), leak/mutation/drift/callback/pacing check results
(§4.3), safety-state inventory (§3.3 step 3), your initials.

## 5. The restart session (one of sessions 2 … N, done deliberately)

1. Run a capture → `…-session-<N>`.
2. Restart the gateway (or use its daily auto-restart — `docs/IBKR_RUNBOOK.md` §5 — and
   re-authenticate; 2FA is yours).
3. Run a second capture in the same sitting → `…-session-<N>-post-restart`.
4. Record: reconnect behaviour with the SAME `IB_CLIENT_ID` (clean reconnect vs "client id in
   use" — `docs/ibkr_setup.md` troubleshooting); every connection-uncertain callback code that
   actually arrived (the fixture-era expectation is `{1100, 1101, 1102, 1300, 2110}`; 1101 and
   1102 are also in the benign set, so an overlap is classified, not unexplained); server-time
   continuity; whether account summary and positions survived identically. These are
   baselines — no expected values exist yet.
5. Both captures plus the restart narrative are the §7 "including a gateway restart/reset"
   evidence. The reconnect discipline of `docs/IBKR_RUNBOOK.md` §6 (status → halt persists →
   reconciliation before any submission → `rearm` with a note) is not exercised by a
   read-only session — no order plane runs — but note the halt state you observed.

## 6. Sanitization — what happens before anything is stored

- The harness replaces every observed account id (the configured `IB_ACCOUNT_ID` and any the
  gateway reports) with `ACCT-<sha256 fingerprint[:16]>` in every file it writes — the repo's
  own pseudonym scheme, `account_fingerprint` in `src/chronos/utils/identifiers.py`
  (`sha256("chronos-account:" + id)`) — the same pseudonym the order persistence stores in
  place of the raw id (`src/chronos/persistence/database.py`). Raw account ids never enter git.
- Verify by hand before the directory leaves the machine:
  `grep -RniE "DU[0-9]|DF[0-9]|U[0-9]{6}" ~/chronos-gateway-evidence/ && echo LEAK || echo clean`.
- `[GAP]` no `sanitize_order_ids` tool exists: if `executions` or `open_orders` are
  non-empty (pre-existing manual history), broker order ids, exec ids and permIds are present
  in the capture. Either replace them with stable placeholders by hand (mapping kept
  OFF-repo) or keep those files off-repo and record counts only. Nothing unsanitized enters
  git — ever.
- Names: the harness records no owner name; do not add one to the evidence doc beyond
  initials.

## 7. Where fixtures and evidence go

- Working captures: `~/chronos-gateway-evidence/<YYYY-MM-DD>-session-<N>/` (off-repo).
- Repository landing, in ONE gate-passage PR through change control after §8 is measured:
  sanitized session directories → `fixtures/ibkr/<YYYY-MM-DD>-session-<N>/` (sibling of the
  existing `fixtures/tradingview/`), and per-session evidence docs + a campaign summary →
  `docs/evidence/real_gateway/`. Neither directory exists today — `[GAP]` `fixtures/ibkr`
  and `[GAP]` `docs/evidence` are created by the gate-passage PR.
- Re-run the replay check (§8) against the in-repo copies: byte-identical means it still
  passes.

## 8. EXIT — measured, none eyeballed

| §7 EXIT criterion | Measurement | Pass looks like |
|---|---|---|
| ≥ 5 sessions incl. a gateway restart | count session directories; the `-post-restart` pair and its narrative exist | ≥ 5 dirs + the pair + evidence docs |
| no mutation call | (a) `replay_check.py`'s mutation scan (no `submit_order` / `preview_order` / `modify_order` / `cancel_order` step name in any capture); (b) the `-readonly` `order_intents` count on the session snapshot (§4.3 step 2) zero or unchanged, every session; (c) the owner's gateway order-log check | all three recorded, all clean |
| no leaked subscription | `active_subscription_count_before_disconnect` in every capture | `0` everywhere (or explained and re-run) |
| no account drift | session-over-session diffs (§4.3 step 4) | every difference explained; none unexplained |
| no unexplained callback | callback classification (§4.3 step 5) | every observed code classified or explained |
| no pacing failure | pacing observations per session (§4.3 step 6) | none, or each recorded with the session stopped |
| captured fixtures replay offline exactly | `.venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py fixtures/ibkr/*` — byte integrity against each `manifest.json`, `derived_liquid_hours.json` re-derived through `parse_liquid_hours` + `confirms_open` and byte-compared, mutation scan, demo directories refused | `[PASS]` × N, exit 0 — paste the output |

Honest scope of the last row: it measures replay-exactness for the derived-session-evidence
slice plus byte integrity of every captured file. `[GAP]` no `replay_callbacks` mechanism
exists (full adapter-level replay — feeding captured wire callbacks back through the callback
bridge); record it as follow-on work and do not claim it.

After every row is measured: the gate-passage PR (fixtures, evidence docs, the §7 EXIT table
filled in, `RISK_REGISTER.md` residuals that gained gateway evidence, the `docs/limitations.md`
and `docs/GO_LIVE_CHECKLIST.md` `[OWNER]` smoke items updated to point at the evidence) is the
owner's to approve. Claim discipline afterwards: the read paths in §4.2 have real-gateway
evidence at those paths; the adapter is not "gateway-proven" generally — order submission,
cancellation, fills and the ack path keep zero gateway evidence by this campaign's design.

## 9. Sources

`docs/VISION_COMPLETION_PLAN.md` §7 (the gate, quoted in §1) and §11 (owner gates) ·
`docs/IBKR_RUNBOOK.md` §1–§8 · `docs/ibkr_setup.md` · `docs/IBKR_INTEGRATION.md` ·
`docs/INCIDENT_RESPONSE.md` "Evidence capture" (copy, don't move; timestamp everything) ·
`scripts/smoke_test_ibkr.py` · `scripts/paper_soak_report.py` (cited only for what it does NOT prove) ·
`.claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py` and
`replay_check.py` (the harness and the offline check) · `src/chronos/config/settings.py` ·
`src/chronos/broker/base.py` · `src/chronos/broker/callbacks.py` ·
`src/chronos/utils/identifiers.py` · `src/chronos/services/liquid_hours.py` ·
`tests/integration/test_ibkr_smoke.py`. Contract test:
`tests/unit/test_m4_session_checklist_contract.py`.
