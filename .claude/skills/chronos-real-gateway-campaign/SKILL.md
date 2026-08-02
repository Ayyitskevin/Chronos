---
name: chronos-real-gateway-campaign
description: >
  The executable campaign for Chronos's hardest live problem: producing the FIRST-EVER
  real-IBKR-gateway evidence, read-only, against the VISION_COMPLETION_PLAN.md §7 gate.
  Load this skill whenever a task involves "connect to IBKR", "paper account", "gateway",
  "IB Gateway", "TWS", "smoke test", "first connection", "real data", "capture evidence",
  "read-only gate", "soak", "paper soak", "gateway evidence", "fixtures from the gateway",
  "market data permissions", or closing the real-gateway evidence gap. It is a numbered,
  decision-gated runbook: owner prerequisites, local preflight, first contact, a
  five-session capture soak including a gateway restart, fixture conversion with offline
  replay, and the measured §7 EXIT — without ever mutating broker state. NOT for routine
  operations (chronos-run-and-operate), IBKR object semantics (chronos-ibkr-boundary), or
  what to do after the gate passes (chronos-priorities-and-roadmap).
---

# The real-gateway campaign — first-ever read-only IBKR evidence

Facts and commands verified against the repo on 2026-08-02. Run everything from the repo
root with the project venv (`.venv/bin/python`, per README Setup).

## 0. Why this campaign exists

**No real IBKR gateway — paper or live — has ever been connected in this project's
history.** Evidence: docs/limitations.md:15-27 ("Live trading has never been exercised
from this codebase"; the order path is "validated against fake-ibapi objects and a
recording spy, not a live gateway"); docs/GO_LIVE_CHECKLIST.md:130-131 lists the read-only
smoke test as an `[OWNER]` TODO — "first proof this code has ever touched a real gateway";
`fixtures/` contains only `tradingview/`; the one gateway test in the suite is skipped by
default (`tests/integration/test_ibkr_smoke.py:15-23`).

Every adapter behavior beyond the fakes — real `liquidHours` strings, `timeZoneId` names,
`underSecType` values, pacing behavior, callback ordering, ack sequences — is
**fixture-verified conjecture**. The project has already been burned by exactly this gap:
the R-26/R-27 inert controls were "fully wired, documented, tested" against fixtures for
months while structurally unable to fire against a real gateway (RISK_REGISTER.md:34-35;
see chronos-failure-archaeology for the narrative, chronos-ibkr-boundary for prevention).

**Target gate** — docs/VISION_COMPLETION_PLAN.md §7 "Real-gateway read-only gate"
(VISION_COMPLETION_PLAN.md:200-211): at least five sessions including a gateway
restart/reset; sanitized capture of account scope, server time, account summary,
positions, executions, open and completed orders, contract qualification, option chains,
market rules/minimum ticks, trading sessions, quote permissions, pacing, callbacks, and
subscription cancellation. **EXIT: no mutation call; no leaked subscription, account
drift, unexplained callback, or pacing failure; captured fixtures replay offline exactly.**

Definitions (first use): **gateway** = the IBKR-supplied TWS or IB Gateway application
that owns authentication and the API socket; Chronos only connects to its local port.
**Paper account** = an IBKR simulated-money account (id `DU…`/`DF…`). **Session** = one
connect → capture → disconnect cycle against a running gateway. **Fixture** = a captured,
sanitized session directory that offline checks can replay. **Sanitized** = raw account
ids (and any order ids) replaced with stable pseudonyms before anything enters the repo.

**Decoupled owner question:** the account-capital decision (~USD 110 today vs the ~USD
3,000 historical premise — VISION_COMPLETION_PLAN.md:68-70, RISK_REGISTER.md R-10) is a
LIVE, parallel owner item. **The read-only gate does not need it.** Do not couple them,
and do not let waiting on the capital decision delay this campaign.

## 1. Standing rules — never break these during the campaign

| Rule | Why (evidence) |
|---|---|
| `ALLOW_ORDER_TRANSMIT=false` and `ALLOW_LIVE_TRADING=false` for every session | §7's own precondition ("keeps every transmit/live flag false", VISION_COMPLETION_PLAN.md:202-203); the smoke launcher force-sets both (scripts/smoke_test_ibkr.py:20-29) |
| `AUTONOMY_MANDATE_FILE` stays UNSET | A present, valid mandate file AUTO-ACTIVATES autonomy on every backend boot (ADR-0017; src/chronos/api/autonomy_wiring.py:318-386; .env.example documents this). The campaign must not create standing trading authority |
| Never call `preview_order` / `submit_order` / `modify_order` / `cancel_order` | The gate is read-only by definition; the capture harness never calls them and the replay check scans for them |
| Capture sessions are NOT trading experiments | One session = the §7 capture list, bounded requests, disconnect. No candidate evaluation, no order rehearsal, no "while we're connected…" |
| Demo/fixture success is NOT gateway evidence | The R-26/R-27 lesson: fixtures hid inert controls for months (RISK_REGISTER.md:34-35). The capture manifest stamps `gateway_evidence: false` on demo runs and the replay check refuses them for the gate |
| A surprising gateway response is a DELIVERABLE, not a bug to hide | If reality diverges from a fixture assumption, record the divergence verbatim first. Never edit a fixture or a captured file to match expectations — the sha256 manifest makes that detectable |
| Owner gates bind | Credentials, 2FA, account config, API permissions, market-data subscriptions are owner-supplied only (VISION_COMPLETION_PLAN.md §11). No agent works around a missing owner input |

## 2. Campaign map

| Phase | What | Exit condition |
|---|---|---|
| 0 | Owner prerequisites (install/pin ibapi, paper account, permissions, gateway config) | `import ibapi` succeeds in `.venv`; gateway runs read-only on a paper port |
| 1 | Local preflight (no gateway needed) | Gates green; config audit prints the exact read-only conjunction; demo rehearsal of the harness passes |
| 2 | First contact (session 1): smoke test + first capture + first-contact ledger | Smoke test passes; session-1 directory + evidence doc exist |
| 3 | Five-session soak incl. one gateway restart | 5 session directories + evidence docs; leak/mutation checks clean or explained |
| 4 | Fixture conversion + offline replay | `replay_check.py` passes for all sessions; fixtures land in `fixtures/ibkr/` via change control |
| 5 | EXIT measurement + promotion + follow-on queue | Every §7 EXIT row measured; evidence doc + doc updates merged via chronos-change-control with owner approval |

---

## Phase 0 — Owner prerequisites (owner-gated)

Everything here is owner-supplied (VISION_COMPLETION_PLAN.md §11). An agent session can
prepare and verify, never substitute.

**0.1 Install and pin the official IB API.** `ibapi` is deliberately NOT on PyPI and not
in the lockfile (requirements-dev.lock has no ibapi entry; the adapter raises install
guidance when absent — src/chronos/broker/official_ibkr.py:203-207). Per
docs/ibkr_setup.md:9-28:

```bash
# 1. Download "TWS API" (latest stable) from https://interactivebrokers.github.io
# 2. Unzip; the Python client is at IBJts/source/pythonclient
cd IBJts/source/pythonclient
/path/to/Chronos/.venv/bin/pip install .
/path/to/Chronos/.venv/bin/python -c "import ibapi; print(ibapi.__file__)"
```

- EXPECTED: the import prints a path inside `.venv`. **Record the installed version
  string** (`pip show ibapi` or the download page version) in the campaign evidence doc —
  this is the PIN. There is no lockfile entry to pin it for you.
- If instead `pip install ibapi` was run from PyPI → uninstall it. Whatever PyPI serves
  under that name is not the official client (docs/ibkr_setup.md:10-11; the repo warns
  against exactly this — see chronos-build-and-env).
- CI never installs or imports ibapi (docs/ibkr_setup.md:28); nothing about this step
  touches CI.

**0.2 Paper account + market-data permissions.** Owner supplies a paper account (`DU…`)
and enables market data for at least the first `SYMBOL_ALLOWLIST` symbol (the smoke test
qualifies only the first symbol — docs/ibkr_setup.md:86-87). Missing permissions surface
as explicit missing-data states, not fabricated values (docs/ibkr_setup.md:61-63) — the
smoke test will fail on an `UNKNOWN`/empty quote by design.

**0.3 Gateway configured read-only.** Per docs/ibkr_setup.md:42-58: authenticate in
TWS/Gateway (Chronos never handles credentials), enable socket clients, bind to loopback,
**select the IBKR-side API read-only option**, and verify the actual socket port:

| Application | Paper | Live |
|---|---:|---:|
| TWS | 7497 | 7496 |
| IB Gateway | 4002 | 4001 |

The adapter refuses an environment/port mismatch at construction
(`verify_environment_port`, official_ibkr.py:690-700; paper ports {7497, 4002}). Use the
paper environment. The IBKR-side read-only toggle is defense in depth ON TOP of the
Chronos flags, not a substitute for them.

**0.4 Known staleness in docs/ibkr_setup.md — do not repeat it.** Lines 5-6 claim "Both
[adapters] are read-only until the Milestone 5-7 order service exists" and lines 30-37
describe the M2/M7 era. STALE: since M7 `official_ibkr` has a gated order path
(official_ibkr.py:1375-1383; README M7). The campaign's read-only property comes from the
Standing rules table, not from that sentence. The doc's gateway-setup and troubleshooting
sections (lines 42-159) remain accurate and are what Phase 0/2 rely on. Record this
discrepancy in the evidence doc; propose the doc fix through chronos-change-control (do
not silently edit mid-campaign).

## Phase 1 — Local preflight (no gateway needed)

**1.1 Suite green.**

```bash
make gates    # = ruff check, ruff format --check, mypy src/chronos, pytest -q
```

- EXPECTED (measured 2026-08-02): all four pass; pytest `2489 passed, 1 skipped` — the
  one skip IS the gateway smoke test. Counts drift; re-measure, don't quote.
- If the whole suite fails with "SAFETY TRIPWIRE: ambient settings are live-capable" →
  your repo-root `.env` sets a live-capable combination; the suite refuses by design
  (tests/conftest.py:17-52). Fix `.env` (see 1.2). Other failures → stop the campaign;
  route via chronos-debugging-playbook / chronos-build-and-env.

**1.2 Config audit — the exact read-only conjunction.** Set `.env` (start from
`.env.example`, per docs/ibkr_setup.md:71-87):

```dotenv
BROKER_MODE=ibkr
BROKER_ADAPTER=official_ibkr
IB_ENVIRONMENT=paper
IB_HOST=127.0.0.1
IB_PORT=7497            # or 4002 for IB Gateway — the port the gateway actually shows
IB_CLIENT_ID=17
IB_ACCOUNT_ID=DU1234567 # the REAL paper id: the official adapter refuses a blank one
ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false
SYMBOL_ALLOWLIST=AAPL,MSFT,SPY
# AUTONOMY_MANDATE_FILE stays unset/commented
```

`IB_ACCOUNT_ID` is required: `OfficialIBKRBroker.__init__` raises `BrokerSafetyError`
without it (official_ibkr.py:723-727). Never commit `.env`. Then audit:

```bash
.venv/bin/python -c "
from chronos.config.settings import Settings
s = Settings()
print('broker_mode =', s.broker_mode.value, '| adapter =', s.broker_adapter.value)
print('environment =', s.ib_environment.value, '| port =', s.ib_port)
print('transmit =', s.allow_order_transmit, '| live =', s.allow_live_trading)
print('mandate_file =', s.autonomy_mandate_file)
print('transmission_possible =', s.transmission_possible)
print('live_transmission_possible =', s.live_transmission_possible)"
```

- EXPECTED: `ibkr / official_ibkr / paper`, both transmit flags `False`, `mandate_file =
  None`, and **both `*_transmission_possible = False`** (the derived conjunctions,
  settings.py:267-301). Anything else → fix `.env` before touching a gateway.
- If `Settings()` itself raises → the validators refused your combination (e.g.
  `ALLOW_LIVE_TRADING=true` without the full ADR-0009 conjunction). That refusal is
  correct; see chronos-config-and-flags.

**1.3 Kill/halt/mandate state inventory (record, read-only).** The campaign never arms
anything, but record the safety state you started from (interpretation and drift scripts:
chronos-diagnostics; procedures: chronos-run-and-operate):

```bash
ls data/live_kill_switch.json 2>/dev/null || echo "no kill-switch file (= DISENGAGED by default)"
.venv/bin/python -m chronos.cli status   # platform halt + audit chain (CWD-relative paths)
```

Note the trap: a MISSING live kill-switch file means DISENGAGED
(src/chronos/orders/kill_switch.py:83-85) — opposite of the platform halt's missing ⇒
HALTED. Neither gates this campaign (no backend order path runs), but the inventory
belongs in every session evidence doc.

**1.4 Dry expectation of the smoke test.** Without the opt-in flag it must SKIP:

```bash
.venv/bin/pytest tests/integration/test_ibkr_smoke.py -ra
```

- EXPECTED: `1 skipped` with reason "set CHRONOS_RUN_IBKR_SMOKE=1 …". If it runs or
  errors instead, stop: your environment already sets the flag or collection is broken.

**1.5 Rehearse the capture harness offline (demo).** Proves the harness end to end
without a gateway. Demo output is stamped `gateway_evidence: false` and can never count
toward the gate:

```bash
BROKER_MODE=demo .venv/bin/python \
  .claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py \
  --out /tmp/chronos-rehearsal --label rehearsal --allow-demo
.venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py \
  --allow-demo /tmp/chronos-rehearsal
```

- EXPECTED: capture writes ~23+ steps; a few recorded `Demo contract is not qualified`
  errors are normal (the demo broker qualifies only its seeded contracts); replay prints
  `[PASS]`. Without `--allow-demo` both refuse — that refusal is a feature, verify it.

## Phase 2 — First contact (Session 1)

**2.1 The smoke test.** With the gateway running and authenticated:

```bash
.venv/bin/python scripts/smoke_test_ibkr.py            # official_ibkr (default)
```

The launcher force-sets `CHRONOS_RUN_IBKR_SMOKE=1`, `BROKER_MODE=ibkr`,
`ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false` and runs
`pytest -m ibkr tests/integration/test_ibkr_smoke.py --maxfail=1 -ra`
(scripts/smoke_test_ibkr.py:19-39). The test performs exactly: connect → connection
status → tz-aware server time → account summary (must match `IB_ACCOUNT_ID`) → qualify
first allowlisted underlying → non-empty option-chain metadata → one bounded underlying
quote with real price data (quality LIVE/FROZEN/DELAYED) → disconnect, always, via
`finally` (tests/integration/test_ibkr_smoke.py:31-107). It never calls any order method.

| Observation | Meaning → branch |
|---|---|
| `1 passed` | **First gateway contact in project history.** Record timestamp + gateway version in the evidence doc; proceed to 2.2 |
| `1 skipped` | Flag not set — you ran pytest directly; use the launcher |
| `BrokerError` with ibapi install guidance | Phase 0.1 not done → back to 0.1 |
| `BrokerSafetyError: IB_ACCOUNT_ID is required` | Set the paper id (1.2) |
| `BrokerSafetyError: IB_ENVIRONMENT=paper requires a paper port` | Port/environment mismatch → 0.3 table; check what the gateway shows |
| `TimeoutError` / connection refused on connect | Gateway not running, API not enabled, or wrong host/port → docs/ibkr_setup.md:143-159 troubleshooting |
| `AssertionError: Connected account does not match configured IB_ACCOUNT_ID` | Wrong/multi-account session → set `IB_ACCOUNT_ID` to an account visible in the gateway |
| Quote assertion fails (`UNKNOWN`/empty) | Market-data permissions missing for symbol 1 → owner gate 0.2; pick a permissioned first symbol |
| Errors mentioning pacing | STOP the session. Record the exact message + code. Wait for the pacing window (docs/IBKR_RUNBOOK.md §7 "Pacing violations"); do not loop retries |

A passing smoke test proves the read path works — nothing more (docs/ibkr_setup.md:127-129).

**2.2 The capture run.** Same shell/env discipline:

```bash
.venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/capture_readonly.py \
  --out ~/chronos-gateway-evidence/$(date +%F)-session-1 --label session-1
```

The harness refuses to start unless both transmit flags are false AND
`AUTONOMY_MANDATE_FILE` is unset. It captures into sanitized canonical JSON
(`capture.json`, `derived_liquid_hours.json`, `manifest.json` with sha256 per file). Map
to the §7 capture list:

| §7 item | Captured as | Notes / known gaps (record, don't hide) |
|---|---|---|
| Account scope, server time, account summary | `connection_status`, `server_time` (+ local-clock bracket), `account_summary` | Offset is a BASELINE to record — no expected value exists for a first contact |
| Positions, executions, open orders | `positions`, `executions`, `open_orders` | Fresh paper account ⇒ likely empty tuples; emptiness is evidence too |
| Completed orders | `completed_orders` gap marker | The `Broker` protocol has no completed-orders read (src/chronos/broker/base.py:82-86 has open_orders/executions only). GAP: record it; feeds VCP §7 Phase-2 work |
| Contract qualification (the R-26/R-27 fields) | `symbol:<S>:qualify_underlying`, `…:qualify_option_contracts` | REAL `liquid_hours`, `time_zone_id`, `min_tick`, `underlying_con_id`, `deliverable_*` vs fixture assumptions is a first-class deliverable → 2.3 |
| Option chains | `…:option_chain_parameters`, `…:option_specs` | Bounded: 1 expiration × 2 strikes, PUT (hard caps: chronos-ibkr-boundary) |
| Market rules / minimum ticks | `min_tick` on qualified contracts | GAP: `marketRuleIds` on ContractDetails is read nowhere in src (grep-verified 2026-08-02); market-rule callbacks exist but the id→contract linkage is unbuilt — record as gateway-unobservable today |
| Trading sessions | `derived_liquid_hours.json` | parse + `confirms_open` at the captured probe instant, replayable offline |
| Quote permissions | `…:underlying_quote` → `data_quality` | Record which quality tier the account actually gets (LIVE vs DELAYED vs FROZEN) |
| Pacing | `…:historical_bars_1d_30d` result/error + any error text | BASELINE: record codes/messages verbatim. The official adapter classifies NO pacing codes (callbacks.py has only benign/connection-uncertain sets); the ib_async adapter assumes {100, 420} (src/chronos/broker/ibkr.py:86). Which codes a real gateway sends is exactly the unknown being measured |
| Callbacks | `callback_notices` (the bridge's benign-notice log) + step errors | Classify each code against callbacks.py:35-38 sets; anything unclassifiable = "unexplained callback" until explained |
| Subscription cancellation | `active_subscription_count_before_disconnect` | EXPECTED `0` (the manager cancels quote subscriptions per operation). Nonzero = leak → EXIT blocker until explained |

Failed steps are recorded as `{"error": …}` and the capture continues — absence of
evidence is evidence. Classify every recorded error in the evidence doc.

**2.3 The first-contact ledger (the point of the campaign).** For each qualified
contract, compare reality against the fixture-era assumptions and record verbatim:

- `liquid_hours`: which format vintage (legacy `20090507:0700-1830` vs current
  `20180323:0400-20180323:2000`; separators `,` vs `;`; `CLOSED` days; `2400` closes —
  src/chronos/services/liquid_hours.py:26-45). Did `parse_liquid_hours` parse it?
  `parsed: null` in `derived_liquid_hours.json` = the parser met a real string it cannot
  read → fail-closed AMBIGUOUS downstream — a REAL R-26-class finding. Record the string.
- `time_zone_id`: is the real value in the `_ZONE_ALIASES` map or IANA
  (liquid_hours.py:59-73)? An unmapped zone = permanent AMBIGUOUS for that contract.
- Options: did `deliverable_verified` come back `True` with `deliverable_shares ==
  multiplier`? A standard-looking contract failing the screen = record the raw fields.
- **Recording the divergence IS the deliverable.** A fixture-derived expectation being
  wrong is the campaign succeeding, not failing. File each divergence in the evidence doc
  with the captured string; fixes to parsers/fixtures happen AFTER the campaign, through
  chronos-change-control, each with a new test carrying the real captured string.

**2.4 Sanitization check before anything leaves the machine.** The harness replaces every
observed account id with `ACCT-<sha256-fingerprint[:16]>` (the repo's pseudonym scheme,
src/chronos/utils/identifiers.py:17-24). Verify by hand:

```bash
grep -RniE "DU[0-9]|DF[0-9]|U[0-9]{6}" ~/chronos-gateway-evidence/  && echo LEAK || echo clean
```

If `executions`/`open_orders` are non-empty (pre-existing manual history), broker order
ids/exec ids/permIds are present: either replace them with stable placeholders (keep the
mapping OFF-repo) or keep those files off-repo and record counts only. Nothing
unsanitized enters git — ever.

**2.5 Session evidence doc.** One markdown doc per session (goes to
`docs/evidence/real_gateway/<date>-session-<N>.md` in the gate-passage PR — a new
directory this campaign introduces via change control). Required fields: date/time (UTC
+ gateway-local), gateway app + version, ibapi pin, account fingerprint (never the raw
id), config audit output (1.2), smoke result, capture dir + manifest sha256s, step-error
classification, first-contact ledger entries (2.3), leak-check values, pacing
observations, kill/halt inventory (1.3), operator initials.

## Phase 3 — Five-session soak (including a gateway restart)

**Definition:** ≥5 sessions per §7, each a full Phase-2 capture (smoke test optional
after session 1; the capture harness is the record). Recommended (not §7-required):
distinct calendar days, so the nightly IBKR reset window (~23:45–00:45 ET) and daily
gateway auto-restart behavior get sampled (docs/IBKR_RUNBOOK.md §5).

Per-session checklist (every session, no exceptions):

1. Config audit (1.2) — EXPECTED identical to session 1; any diff → explain before connecting.
2. Capture run → `…-session-<N>`.
3. Leak check: `active_subscription_count_before_disconnect == 0`; `disconnect: ok`;
   `final_connection_status.connected == false`. Nonzero/failed → record; investigate
   before the next session (a leaked subscription is an EXIT blocker).
4. Mutation check: `.venv/bin/python scripts/paper_soak_report.py` (reads the order DB;
   places no orders — its docstring). EXPECTED: `order intents: 0` and zero counts
   throughout, every session, if the backend order plane never ran (fresh DB) — record
   the unchanged output as no-mutation evidence. CAUTION verified 2026-08-02: the script
   calls `Database.initialize()`, which CREATES a fresh schema-v7 DB file at a
   nonexistent URL — run it against your real `DATABASE_URL` (default
   `sqlite:///data/chronos.db`), not a typo'd path, and note that "no DB existed before
   the first run" is itself the strongest no-mutation evidence.
5. Gateway-side check (owner): TWS/Gateway order log shows zero orders from the Chronos
   client id. Record "checked, none".
6. Account-drift check (session ≥2): diff `account_summary` and `positions` against the
   previous session. EXPECTED: identical on an untouched paper account, except values
   IBKR itself moves (paper resets can adjust balances). Every diff gets an explanation
   in the evidence doc; an unexplainable diff is an EXIT blocker.
7. Callback check: every `callback_notices` code classified (benign set
   callbacks.py:35, connection-uncertain set callbacks.py:38, or explained in prose).
8. Evidence doc (2.5) written before the next session.

**The restart session (one of sessions 2-5, do it deliberately):**

1. Run a capture → `…-session-<N>`.
2. Restart the gateway (or use its daily auto-restart; re-authenticate — 2FA is
   owner-held).
3. Run a second capture in the same sitting → `…-session-<N>-post-restart`.
4. Record: reconnect behavior with the SAME `IB_CLIENT_ID` (clean reconnect vs "client id
   in use" — docs/ibkr_setup.md:148), all connection-uncertain callback codes observed
   (1100/1102/1300/2110 are the fixture-era expectations, callbacks.py:38 — record what
   ACTUALLY arrives), server-time continuity, and whether account/positions survive
   identically. These are BASELINES; no expected values exist yet.
5. Both captures + the restart narrative count as the §7 "including a gateway
   restart/reset" evidence.

## Phase 4 — Fixture conversion + offline replay

**4.1 Offline replay (no gateway, no network):**

```bash
.venv/bin/python .claude/skills/chronos-real-gateway-campaign/scripts/replay_check.py \
  ~/chronos-gateway-evidence/*session*
```

Measures, per session directory: (a) **byte integrity** — every file re-hashes to its
manifest sha256; (b) **derivation replay** — `derived_liquid_hours.json` is recomputed
from the RAW captured strings through the repo's own `parse_liquid_hours` +
`confirms_open` at the captured probe instant and must be **byte-identical**; (c)
**mutation scan** — no order-mutation step name appears; (d) demo directories are
refused. EXPECTED: `[PASS]` per directory, exit 0.

- sha256 drift → the fixture was edited after capture. Do NOT "fix" the hash; recover
  the original from the capture machine and record what happened.
- Derivation replay FAIL → either repo parser code changed since capture (re-run after
  checking out the capture-time commit to confirm) or the file was hand-edited. If a
  parser CHANGE is the cause, that is a real finding: the fixture now pins real-gateway
  behavior the code must keep handling — add the captured string to the unit tests via
  change control.

Honest scope: this measures replay-exactness for the session-evidence slice (the R-26
surface) plus integrity of every captured file. Full adapter-level callback replay
(feeding captured wire callbacks back through `CallbackBridge`) does not exist yet —
record as follow-on work; do not claim it.

**4.2 Fixture landing.** Copy sanitized session dirs into the repo as
`fixtures/ibkr/<YYYY-MM-DD>-session-<N>/` (sibling of the existing
`fixtures/tradingview/`), in the SAME PR as the evidence docs (Phase 5). Re-run
`replay_check.py` against the in-repo copies — byte-identical means it still passes.

## Phase 5 — EXIT measurement, promotion, and what it unlocks

**5.1 The §7 EXIT checklist — every row measured, none eyeballed:**

| §7 EXIT criterion | Measurement | Pass looks like |
|---|---|---|
| ≥5 sessions incl. a gateway restart | Count session dirs; restart narrative present | ≥5 dirs + `-post-restart` pair + evidence docs |
| No mutation call | replay mutation scan; per-session `paper_soak_report` zeros/unchanged; owner gateway-log check | All three recorded, all clean |
| No leaked subscription | `active_subscription_count_before_disconnect` across all sessions | `0` in every capture (or explained + re-run) |
| No account drift | Session-over-session diffs (3.6) | Every diff explained; none unexplained |
| No unexplained callback | Callback classification (3.7) | Every observed code classified or explained |
| No pacing failure | Pacing observations per session | No pacing error, or each one recorded + session stopped per the rule (a recorded, understood pacing message with a stopped session is an OBSERVATION; an ignored one is a FAILURE) |
| Fixtures replay offline exactly | `replay_check.py` exit 0 over all in-repo fixtures | `[PASS]` × N, exit 0 — paste the output |

**5.2 Promotion — through chronos-change-control, never silent edits.** Owner approvals:

- BEFORE: owner supplied Phase 0 (credentials, account, permissions, install) and
  approves starting the campaign (it uses their brokerage account).
- DURING: owner performs restarts/2FA and the gateway-side no-orders checks; owner
  resolves anything a session cannot explain (VISION_COMPLETION_PLAN.md §11: manual
  broker resolution is owner-only).
- AFTER: owner reviews the evidence set and approves the gate-passage PR.

The gate-passage PR (one reviewable unit) contains:

1. `docs/evidence/real_gateway/` — per-session evidence docs + a campaign summary with
   the 5.1 table filled in and the first-contact ledger.
2. `fixtures/ibkr/…` — the sanitized session fixtures (post 2.4 re-check).
3. RISK_REGISTER.md updates for each adapter-path residual whose "fixture-verified only /
   gateway-unverified" status CHANGES: R-26 and R-27 residuals currently say verified
   "against fixtures, not a live gateway" (RISK_REGISTER.md:34-35) → propose
   MITIGATED-with-gateway-evidence wording naming the exact fixture paths. R-42 (pacing)
   gains its first real observations. Residuals that did NOT gain evidence keep their
   label — crypto venue-metadata field names (official_ibkr.py:1034-1039) stay
   gateway-unverified (this campaign captures no crypto), as does the order/ack path
   (read-only campaign — order-path claims remain fixture-only by design).
4. docs/limitations.md broker-integration bullets and docs/GO_LIVE_CHECKLIST.md:130-131
   `[OWNER]` smoke item updated to point at the evidence, plus the ibkr_setup.md
   staleness fix (0.4).
5. The agent task contract block (VISION_COMPLETION_PLAN.md §13) with
   `plan_phase: 2`, `gate_advanced: real-gateway read-only gate`, rerunnable
   verification (`replay_check.py` command + output).

Claim discipline: after merge you may say "the read paths listed in the capture map have
real-gateway evidence at these paths". You may NOT say the adapter is "gateway-proven"
generally — order submission, cancellation, fills, and the ack path have zero gateway
evidence and keep MITIGATED ≠ CLOSED status (see chronos-validation-and-qa for the
claim-evidence ladder).

**5.3 What passing unlocks (route via chronos-priorities-and-roadmap):**

- **Forward option capture can START** (`python -m chronos.histdata options …`, dedicated
  `IB_DATA_CLIENT_ID`): calendar-bound — "Begin forward option capture immediately;
  missed days cannot be recreated from IBKR" (VISION_COMPLETION_PLAN.md:215). Every day
  between gate-passage and starting capture is unrecoverable.
- VCP §7 Phase-2 broker-truth work (idempotent broker-fact persistence, periodic
  reconciliation, completed-orders read — the gaps this campaign recorded).
- Re-basing adapter unit fixtures on captured real strings (each via change control).

## Wrong paths — fenced off

| Temptation | Why it is wrong (evidence) |
|---|---|
| "Enable `ALLOW_ORDER_TRANSMIT=true` so we can test properly" | The campaign is read-only BY DEFINITION; §7 requires every transmit/live flag false (VISION_COMPLETION_PLAN.md:202-203). The launcher and harness force/verify the flags off. Order-path evidence is a LATER, separately owner-gated campaign |
| "Configure `AUTONOMY_MANDATE_FILE` while we're setting things up" | It AUTO-ACTIVATES on every backend boot — file present + backend running = standing trading authority (autonomy_wiring.py:318-386, ADR-0017). Verified: the harness refuses to run with it set. Zero reason to touch it here |
| "The demo/fixture run passed, so the gateway behavior is covered" | The R-26/R-27 lesson: fixture-verified controls were inert against reality for months (RISK_REGISTER.md:34-35). `gateway_evidence: false` artifacts never count toward the gate |
| "Skip sanitization, it's only a paper account id" | Raw account ids never enter the repo — the persistence layer itself refuses to store them (identifiers.py:17-24 pseudonym scheme). §7 says *sanitized* capture. Paper ids map to a real owner identity |
| "While connected, let's also evaluate candidates / rehearse an order" | A capture session that doubles as a trading experiment destroys the evidence claim (which calls produced which state?) and invites pacing incidents. One session = the capture list, nothing else |
| "The gateway returned something weird — just fix the fixture so tests pass" | Recording the divergence IS the deliverable. Editing captured files breaks the sha256 manifest (detected by replay_check) and repeats the exact epistemic failure this campaign exists to end |
| "Reuse a capture session to also 'verify' the kill switch / arming" | Those need the backend order plane and belong to different runbooks (chronos-run-and-operate). Mixing authority experiments into read-only evidence contaminates both |

## When NOT to use this skill

- Routine start/stop/operate questions → **chronos-run-and-operate**.
- IBKR object semantics, ContractDetails field mapping, pacing design, the inert-control
  prevention checklist → **chronos-ibkr-boundary**.
- Environment/install problems (venv, lockfile, ibapi import) → **chronos-build-and-env**.
- What a flag means / safe-to-change → **chronos-config-and-flags**.
- After the gate passes, what to do next → **chronos-priorities-and-roadmap**.
- Whether a change/claim is allowed and how to route the PR → **chronos-change-control**.

## Provenance and maintenance

Compiled 2026-08-02 from the live repo (branch `claude/chronos-skills-library-bfbj29`).
The demo rehearsal, refusal guards, sanitization, tamper detection, and replay checks of
the shipped scripts were executed and verified in this environment on 2026-08-02. No
gateway was contacted (none exists here — that is the campaign's job).

Volatile facts → re-verify before trusting (all read-only):

| Fact | Re-verify with |
|---|---|
| No gateway evidence exists yet / fixtures absent | `ls fixtures/` (only `tradingview/` ⇒ campaign not run) and `ls docs/evidence/ 2>/dev/null` |
| Smoke test still opt-in, read-only, 8 steps | `sed -n 1,45p tests/integration/test_ibkr_smoke.py`; `sed -n 14,42p scripts/smoke_test_ibkr.py` |
| §7 gate text unchanged | `grep -n "Real-gateway read-only gate" -A 12 docs/VISION_COMPLETION_PLAN.md` |
| Suite green + only skip is the smoke test | `make gates` (re-measure counts) |
| Paper ports {7497,4002}; account-id required | `grep -n "_PAPER_PORTS\|IB_ACCOUNT_ID is required" src/chronos/broker/official_ibkr.py` |
| Missing kill-switch file ⇒ DISENGAGED | `sed -n 80,95p src/chronos/orders/kill_switch.py` |
| Mandate auto-activation on boot | `grep -n "AUTONOMY_MANDATE_FILE" .env.example src/chronos/config/settings.py` |
| `marketRuleIds` still unread (capture-map gap) | `grep -rn "marketRuleIds" src/ \|\| echo "still unread"` |
| Completed-orders read still absent | `grep -n "completed" src/chronos/broker/base.py \|\| echo "still absent"` |
| R-26/R-27 residuals still fixture-only | `grep -n "R-26\|R-27" RISK_REGISTER.md` |
| ibkr_setup.md staleness (0.4) still present | `sed -n 1,8p docs/ibkr_setup.md` |
| paper_soak_report reads-only-but-initializes | `sed -n 117,133p scripts/paper_soak_report.py` |

Update triggers: any change to VISION_COMPLETION_PLAN.md §7, the smoke test, the Broker
protocol's read surface, `parse_liquid_hours` formats, or the first landing of
`fixtures/ibkr/` — after which Phase state in §2's map must be re-assessed and this
skill's "never connected" premise re-verified (it should then be FALSE; update §0).
