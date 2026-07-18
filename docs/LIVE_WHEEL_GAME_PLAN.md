# Chronos Live Wheel — Game Plan (start → completion)

> **North star (owner, 2026-07-17):** "my interpretation of a quantitative
> Jane-Street-caliber AI quant trading bot… my personal trading co-pilot."
> Honest translation this project builds toward: **institutional-grade
> engineering discipline** (fail-closed risk, reconciliation against broker
> truth, frozen evaluation criteria, kill switches, honest measurement)
> **wrapped around a personal co-pilot** — Chronos surfaces evidence and
> executes safely; the owner makes every trading decision. What is *not*
> promised: institutional edges (market-making infrastructure, flow) or
> autonomous alpha — the research arm exists precisely so any strategy must
> *earn* promotion under frozen criteria before it ever touches execution.

**Status:** Milestone 0 (this document). Branch: `feat/live-wheel-dashboard`.
**Audience:** the owner, and any Claude session (Opus/Sonnet) continuing this
work mid-stream. Read this before writing code. The owner's full mission
prompt (recorded 2026-07-17) governs; this plan adapts it to what already
exists in the repository.

---

## 0. Why this mission, and the strategy answer it encodes

The completed research program (docs/RESEARCH_REPORT.md, twice adversarially
reviewed) reached an honest verdict: **no autonomous strategy derived from
the Pine corpus demonstrated a defensible edge** on the available data. Zero
candidates are backtest-validated; the autonomous platform is deliberately
not live-capable.

**Given that, the best strategy this system can actually operate is the
Wheel** (cash-secured short puts → assignment → covered calls → called away),
because:

1. Its income source (the volatility risk premium on liquid, large-cap
   underlyings) is structural rather than a fitted directional signal — it
   does not depend on the kind of backtested edge the research failed to
   find, and it degrades gracefully rather than catastrophically when the
   premium thins.
2. Its dominant risks (assignment, concentration, cash security, uncovered
   calls, fat-finger orders) are exactly the risks the existing guardrail
   architecture was built to manage — deterministic checks, reconciliation,
   typed confirmation, kill switches.
3. It is human-supervised by design, which matches the honest capability of
   this system today: decision support with hard safety rails, not
   autonomous alpha.

**Role of the Pine corpus (42 scripts, mostly indicators/studies):** not
autonomous signals — *decision-support context* for Wheel timing:

- The **regime engine** (BULL+ core → `regime_trend_v1`'s Markov/EMA regime
  machinery, already implemented and tested in
  `src/chronos/strategies/regime_trend.py`) becomes a per-underlying
  **regime context panel**: label the daily regime (bullish / bearish /
  choppy, vol percentile). Heuristic use: prefer opening new short puts when
  the regime is not bearish-trending; prefer more conservative call strikes
  in strong bull regimes (upside forfeiture is expensive) and more
  aggressive premium capture in chop.
- The **RSI-2 mean-reversion core** (`mean_reversion_v1`) becomes a
  **put-entry timing hint**: short-term oversold days in a non-bearish
  regime are historically where put premium is richest for the same delta.
- Both are surfaced as **labeled heuristics** ("regime context — not a
  validated signal, not an assignment probability"), never as gates that
  auto-transmit and never as numbers that override the risk engine. This is
  the only honest use of the corpus the research supports, and it is
  genuinely useful in a Wheel workflow (it answers "is now a reasonable day
  to sell this put?" with evidence the operator can weigh).

This is recorded here so no future session re-litigates "which strategy":
**the product is a live-capable, human-confirmed Wheel dashboard with
Pine-derived regime context as decision support.**

---

## 1. The one big posture change (owner-authorized)

Until now every document and test asserted "live trading is impossible in
this build; no override exists." **The owner has now explicitly changed the
mission:** Chronos must be *capable* of transmitting real-money orders,
disabled by default, armed deliberately, behind eight fail-closed gates.

The safety keystone that preserves everything already built:

> **Live capability exists ONLY in the human-in-the-loop Wheel order path
> (new `chronos/api` backend → OrderService). The autonomous strategy
> platform (`chronos.service`, `chronos.execution.engine`, mode locks)
> remains structurally live-incapable — unchanged — because no autonomous
> strategy earned promotion.**

Concretely:

- `chronos.control.modes.resolve_mode_lock` keeps hard-denying
  CANARY_LIVE/LIVE for the platform. Do not touch it.
- The Wheel path gets its own, separate live gate stack (`execution/live_gate`
  etc. below) that never feeds the autonomous engine.
- Every doc/test that says "live impossible" must be consciously migrated to
  "live impossible in the autonomous platform; live gated and
  human-confirmed in the Wheel path" — as part of Milestone 6, not silently.
  Known assertion sites: `README.md`, `HANDOFF.md`, `docs/safety.md`,
  `docs/SECURITY.md`, `docs/GO_LIVE_CHECKLIST.md`, settings validators
  (`config/settings.py:91,108` currently hard-raise on
  `allow_live_trading`), wheel `IBKRBroker` order methods (raise
  `BrokerSafetyError` unconditionally), `tests/safety/` live-denial tests,
  `tests/unit` settings tests.

Non-negotiables carried forward from the owner prompt: no auto-transmit on
candidate match; no unattended trading; no market orders; no naked calls;
puts genuinely cash-secured; no automatic retry of uncertain submissions; no
order placed by any test/CI/dev workflow, ever; every live order is armed +
typed-confirmed; `transmit=True` assigned in exactly one authorized method.

---

## 2. Repository audit — what exists vs. what the spec asks for

The repo is NOT empty. Two mature subsystems (1255 tests, ruff/mypy-strict
clean, CI green). **Evolve, don't greenfield.** Map of spec-structure →
existing assets:

| Spec module | Existing asset | Gap |
|---|---|---|
| `broker/base.py` (Broker protocol) | `src/chronos/broker/base.py` — typed protocol: connect/status, account_summary, positions, open_orders, executions, qualify contracts, option chains, quotes, preview/submit/modify/cancel signatures | Extend with `server_time`, `managed_accounts`, `completed_orders`, `market_rule`, market-data-type control |
| `broker/demo.py` | `DemoBroker` with deterministic fixture cases (safety cases, empty account) | Add fixtures for the new chaos scenarios (§ Milestone 1) |
| `broker/official_ibkr.py` | **Missing** — current `broker/ibkr.py` uses `ib_async`, read-only, order methods raise | New adapter on the official TWS API; keep `ibkr.py` as the optional `ib_async` adapter behind the same interface |
| `api/` (FastAPI backend) | **Missing** — UI currently calls broker via in-process runtime | New; the single biggest structural addition |
| `domain/` | `enums.py`, `models.py` (contracts, orders, positions, quotes), `money.py` (Decimal) | Add OrderIntent/lifecycle enums for the wheel path |
| `strategy/wheel_state.py` | `strategy/wheel_state.py` exists (reconciliation-driven stages) | Extend to the full spec state machine incl. `*_PENDING`, `ASSIGNMENT_RECONCILING`, `MANUAL_REVIEW` (partially present) |
| `strategy/strike_resolver.py` | Exists — ranked candidates, hard filters, rejection reasons, NO_TRADE | Verify scoring weights vs. spec env keys; add config plumbing |
| `strategy/scenarios.py` | `services/short_put_risk_preview.py` + `short_put_demo_what_if.py` (expiration P&L, obligations, commission handling — tested) | Refactor into `strategy/scenarios.py` shape; add covered-call scenario fields |
| `execution/` (order service, live gate, confirmation, kill switch, idempotency) | **Precursors exist**: `services/short_put_demo_approval.py` is a full typed-confirmation *rehearsal* (phrase match, TTL, invalidation on quote/position change) built explicitly as the forerunner of real confirmation. The autonomous platform's `execution/intents.py` + `state_machine.py` are the proven idempotency/lifecycle patterns to imitate (not import) | New package for the wheel path implementing the spec lifecycle |
| `risk/` (pretrade, cash security, coverage, concentration, breakers) | `services/` risk preview implements cash-secured math; platform `risk/engine.py` is the pattern for structured RiskDecision with per-check explanations | New wheel-path risk package per spec check list |
| `persistence/` + Alembic | SQLAlchemy schema exists (`persistence/schema.py`: order drafts, previews, submitted orders, guardrail decisions, wheel cycles) | Add intent/confirmation/arming/kill-switch/reservation tables + Alembic migrations (currently none) |
| `ui/` | Streamlit dashboard + components + charts + rehearsal state (tested via AppTest) | Split into pages/, convert to backend `api_client` (currently in-process) |
| `cli/` | Platform CLI (`chronos.cli.main`) — status/halt/rearm/monitor/backtest | Add `chronos live arm`, kill-switch, nuclear-cancel commands (separate `live_commands.py`) |
| `utils/locking.py` (single writer) | **Missing** | New durable lease |
| Chaos tests | `tests/chaos/` harness exists (platform) | Add wheel-path chaos: disconnect-during-submission, duplicate callbacks, partial-fill reconnect, unknown submission |
| CI | `.github/workflows/ci.yml`, hash-locked installs | Keep; official IBKR API must remain un-imported unless selected |

Preserved untouched: the entire research/backtest/shadow platform, its docs,
data, and 1255-test suite. Nothing is deleted or reset.

---

## 3. Architecture decisions (final unless the owner objects)

1. **Two processes.** FastAPI backend (`chronos.api`, binds 127.0.0.1:8765)
   owns the single broker connection, all mutable trading state, order IDs,
   arming state, risk checks, persistence. Streamlit UI becomes a thin
   client through `ui/api_client.py`. During migration, demo mode may run
   the old in-process path until Milestone 4 cuts over; the cutover removes
   direct broker access from the UI.
2. **Official TWS API is the production adapter** (`broker/official_ibkr.py`)
   with a thread-safe callback→event bridge (`broker/callbacks.py`,
   `request_registry.py`, `order_ids.py`). Installed from the official
   distribution per `docs/ibkr_setup.md`; **never** added to requirements;
   imported lazily only when `BROKER_ADAPTER=official_ibkr`. The existing
   `ib_async` adapter stays as an optional secondary behind the same
   protocol. Demo mode runs with neither installed.
3. **Wheel-path OrderIntent + lifecycle** exactly per spec
   (DRAFT→…→SUBMISSION_UNKNOWN/SUBMITTED→…), persisted before `placeOrder`,
   idempotency key with DB uniqueness, submission serialized by mutex,
   SUBMISSION_UNKNOWN reconciled never retried. Modeled on (not shared with)
   the platform's tested state machine.
4. **`transmit=True` in exactly one place**: the final centralized
   submission method in `execution/order_service.py`, after all gates pass,
   re-validated server-side immediately before the broker call. Everything
   upstream constructs orders with `transmit=False` (what-ifs) or no order
   at all.
5. **Live arming is backend-memory only**, scoped to one account, TTL'd,
   revoked on disconnect/account-change/restart/kill-switch/critical error.
   Typed per-order confirmation with the spec's phrase format, TTL,
   invalidation set, and hash-only storage — extending the proven rehearsal
   machinery in `short_put_demo_approval.py`.
6. **Single-writer lease** in SQLite (`utils/locking.py`): heartbeat row +
   process token; second instance starts read-only with a banner.
7. **Kill switch** disarms + blocks new/modified orders + cancels
   Chronos-owned orders individually (no account-wide cancel); nuclear
   cancel is a separate, deliberately hard CLI path.
8. **Regime context (Pine value-add)** is a read-only decision-support
   service reusing `strategies/regime_trend.py` / `mean_reversion.py` over
   the owner's daily bars; displayed with explicit "heuristic, not a
   validated signal" labels; never an order gate. Ships in Milestone 4.
9. **Decimal everywhere** on the money path (already the wheel convention);
   minimum-tick from market rules, never naive rounding.
10. **The autonomous platform keeps its live hard-denial.** Its tests are
    untouched. New wheel-path tests assert the *gated* model.

---

## 4. Milestones (each ends with: gates green + milestone report + pause)

Quality gates for every milestone: `pytest` (full suite), `ruff check .`,
`ruff format --check .`, `mypy src/chronos` — all clean, no skips beyond the
existing credential-gated smoke. No test may transmit any order to any
broker environment, ever.

### Milestone 0 — Audit, architecture, game plan *(this document — DONE)*
Deliverables: repo audit (§2), architecture decisions (§3), this plan,
branch `feat/live-wheel-dashboard`. No code changes.

### Milestone 1 — Backend scaffold + config + single-writer (demo-only)
- `chronos/api/`: FastAPI app factory, `/health`, `/account`, `/positions`,
  `/candidates`, read endpoints backed by DemoBroker through the existing
  runtime; localhost bind; local API token (`api/auth.py`, random,
  file-permission-restricted, never logged).
- `config/settings.py`: add the spec's new keys (BROKER_ADAPTER,
  IB_ACCOUNT_ALLOWLIST, ENABLE_/REQUIRE_ flags, LIVE_ARM_TTL_MINUTES,
  confirmation TTLs, risk limits, resolver weights, BACKEND_HOST/PORT) with
  validation; **replace** the current "live ⇒ hard-raise" validators with
  "live ⇒ every live limit present and finite, else refuse startup"
  (documented as the posture change, tests updated in the same commit).
- `utils/locking.py` single-writer lease + read-only fallback mode.
- `persistence`: Alembic init + first migration capturing current schema +
  new tables (order_intents, confirmations, live_arm_events,
  kill_switch_events, cash_reservations, share_reservations,
  reconciliation_runs, event_log append-only).
- `scripts/run_backend.py`, `scripts/run_ui.py`, Makefile targets.
- Tests: API demo round-trips, settings validation matrix, lease contention,
  migration up/down.

### Milestone 2 — Official IBKR read-only adapter
- `broker/official_ibkr.py` + `connection.py` upgrades + `callbacks.py`
  (thread-safe queue bridge), `request_registry.py`, `order_ids.py`
  (central, monotonic, crash-safe), `market_data.py` (type classification
  LIVE/FROZEN/DELAYED/STALE…, pacing, bounded subscriptions, backoff).
- Read paths: server_time, managed_accounts (exact allowlist match),
  account_summary, positions, executions, commissions, open/completed
  orders, qualify underlying/options, chain params, market rules, quotes.
- Read-only smoke test extended (`scripts/smoke_test_ibkr.py`) — opt-in,
  never transmits. CI never imports the official package (lazy import +
  adapter-selection test with the package absent).
- Exit: demo unaffected; smoke test documented for the owner to run.

### Milestone 3 — Wheel engine + resolver + scenarios (backend services)
- `strategy/wheel_state.py` → full spec state machine, reconciliation-driven
  (startup/reconnect/event/periodic), MANUAL_REVIEW semantics.
- `strategy/strike_resolver.py` → confirm spec filters/scoring, wire the new
  config weights, paced chain workflow, typed NO_TRADE.
- `strategy/scenarios.py` + `assignment_pressure.py` + `capital.py` +
  `basis.py` (strategy-adjusted basis ledger vs broker average cost — never
  overwrite broker cost; MANUAL_REVIEW on unexplained mismatch).
- Reservations: conservative cash/share reservation engine (existing +
  pending + proposed + buffer).
- Tests: the full resolver/scenario/risk matrices from the owner prompt's
  testing section (most already exist for puts; add calls + reservations).

### Milestone 4 — Dashboard on the backend (UI cutover) + regime context
- `ui/pages/{portfolio,symbol_detail,order_workspace,activity,settings}.py`
  driven by `ui/api_client.py` (polling first; SSE later if needed).
- Portfolio cards/table, Near-Term Focus, symbol detail with Plotly strike
  ladder (shape+label differentiation, hover details), lock reasons always
  displayed with explanations.
- **Regime context panel** (§0): daily regime label + RSI-2 stretch for each
  allowlisted symbol, marked "heuristic context — not a validated signal".
- Cutover: UI loses all direct broker imports (enforced by an import test
  like the monitoring plane's `sys.modules` probe).

### Milestone 5 — Paper order management (end-to-end, still no live)
- `execution/`: `order_builder` (limit-only, MID/NATURAL/CUSTOM with market
  rules), `order_preview` (what-if), `order_service` (single submission
  boundary; paper only at this milestone), `order_tracker` (spec lifecycle +
  duplicate-callback idempotency), `idempotency`, `modification`,
  `confirmation` (real typed confirmations grown from the rehearsal code),
  cancellation with CANCEL_PENDING semantics.
- `risk/`: structured RiskDecision with per-check pass/fail/unknown
  (unknown⇒fail), the full spec check list, decision expiry.
- Buy-to-close, partial fills, restart reconciliation, SUBMISSION_UNKNOWN
  recovery (reconcile via orderRef/permId/executions; never auto-retry).
- Paper validation session per spec + `scripts/paper_soak_report.py`.
- Exit: owner runs the documented paper flows against their paper account.

### Milestone 6 — Live safety layer
- `execution/live_gate.py`: the eight-gate stack (config, connection,
  reconciliation, data, risk, preview, session-arming, per-order
  confirmation), all fail-closed, each gate a typed check with explanation.
- Live arming (backend memory, TTL, revocation set), `cli/live_commands.py`
  (`chronos live arm` with typed phrase, masked logging).
- Session-drawdown circuit breaker (persisted session NLV baseline).
- `execution/kill_switch.py` (+ optional nuclear-cancel CLI, separately
  confirmed, documented as emergency-only).
- **Posture migration:** every "live impossible" claim updated to the split
  model (§1 list); safety tests updated in the same commits; new tests
  assert each gate independently blocks.
- Docs: `live_trading_runbook.md`, `incident_response.md` updates,
  `order_lifecycle.md`, `paper_validation.md`.

### Milestone 7 — Live execution capability (validated without trading)
- `transmit=True` at the single authorized boundary; live order object built
  from the qualified contract, valid tick, confirmed account, DAY limit,
  `outsideRth=false`.
- Validation with a **recording spy broker**: full gate walk, wrong-account
  rejection, arming expiry, confirmation mismatch, final server-side
  re-validation, kill-switch interruption — proving the live path emits a
  correct order object **without any order reaching a broker**.
- No live trade is placed during development; the owner performs any
  eventual live acceptance manually through the finished app.

### Milestone 8 — Hardening, chaos, CI, docs, PR
- Wheel-path chaos tests: disconnect during submission, duplicate callbacks,
  partial-fill reconnect, unknown submission state, backend restart with
  working orders.
- Alembic verification in CI; README rewrite per spec (risk disclosures,
  setup for demo/paper/live, runbooks); `docs/limitations.md`.
- Full-suite soak, adversarial self-review pass (reuse the M5 seven-dimension
  method), draft PR into `feat/wheel-dashboard-mvp`.

---

## 5. Definition of done

The owner prompt's 25-point Definition of Done applies verbatim. Additions
from this plan: (26) the autonomous platform's live hard-denial is
unchanged and still tested; (27) the regime-context panel is present,
labeled heuristic, and has no pathway into order transmission; (28) every
milestone shipped with the standard gates green and a milestone report.

## 6. Working agreements for continuing sessions

- Follow the milestone order; do not start milestone N+1 before N's gates
  are green and reported. Small commits, descriptive messages, this branch
  only, never push to the default branch.
- Never claim an untested result; paste actual gate output in milestone
  reports. Never place any order from tests/CI/dev. Never commit secrets or
  account IDs (mask suffixes). `.env` never committed.
- When the official IBKR package is unavailable in the environment (it is
  not on PyPI), develop the adapter against the recorded-interface fakes in
  tests and mark gateway verification as an owner action — exactly as the
  existing `ibkr_paper` adapter did.
- If the repo state diverges from this plan (e.g. the owner merged other
  work), re-audit before coding; update §2 rather than assuming.

## 6b. Scope expansion (owner-directed, 2026-07-17): stocks and crypto

The owner has extended the mission: Chronos must also trade **stocks** and
**crypto**, through the *same* human-confirmed, gated, single-writer order
pipeline. This section is the authoritative adaptation; the owner's original
prompt listed crypto as rejected-in-live — **the owner's later message
explicitly overrides that** for a phased, gated implementation.

**Product-family model.** `domain/enums.py` gains `ProductFamily`
(`OPTION | STOCK | CRYPTO`) in Milestone 3. Every order intent carries its
family; eligibility, risk checks, sessions, and quantity semantics dispatch
on it. One pipeline, three families — no family-specific submission paths.

**Stocks (folds into the existing milestones).** Buy/sell **limit DAY**
orders for allowlisted US-listed equities/ETFs, whole shares only in the
MVP. This is a natural Wheel companion (exiting assigned stock, rounding
lots, deliberate entries that seed covered-call positions). Same gates,
same typed confirmation, same risk engine (symbol/product allowlist,
concentration, cash sufficiency for buys, held-share sufficiency for sells
— **no short selling**, no margin buys). Lands in Milestone 5 (paper)
alongside options; live in Milestone 7. Stock paper validation works
normally (IBKR paper supports stocks).

**Crypto (new Milestone 7C — after live options/stocks are validated).**
Honest constraints that shape the design, stated up front:

- IBKR crypto is **spot only** (Paxos/Zero Hash venue). There are **no
  crypto options at IBKR**, therefore no crypto wheel — crypto support means
  human-confirmed spot buy/sell with guardrails plus the regime-context
  panel (the regime/mean-reversion cores run on daily crypto bars exactly as
  on equities, same "heuristic, not a validated signal" labeling).
- **Fractional Decimal quantities** (e.g. 0.005 BTC) — quantity moves from
  int to Decimal for this family only, with venue min-size/notional
  validation from qualified contract details, never assumed.
- **~24/7 sessions** — the trading-hours module gets family-aware calendars;
  the "market open" risk check consults the family calendar.
- Limit orders only (as everywhere); crypto exchange routing (not SMART);
  **no shorting, no margin, no staking/transfer features** — trade only.
- **IBKR paper accounts do not support crypto.** The paper-validation path
  is impossible for this family. Validation is therefore: deterministic demo
  fixtures + the recording-spy live-path walk (as Milestone 7) + an
  owner-performed minimal-size live acceptance. This limitation is disclosed
  in README/limitations rather than papered over.
- Region/eligibility for IBKR crypto varies by jurisdiction — verifying the
  owner's account eligibility is an owner action at 7C start.
- New settings: `CRYPTO_ALLOWLIST` (default empty ⇒ family disabled),
  `MAX_CRYPTO_ALLOCATION_PCT`, `MAX_CRYPTO_NOTIONAL_PER_ORDER_USD`. Empty
  allowlist keeps crypto fully off — deny-by-default like everything else.

**Milestone deltas:** M1 adds the new settings keys (safe defaults, family
off). M3 adds `ProductFamily`, family eligibility, family-aware trading
hours. M4 dashboard shows family badges; crypto symbols appear only when
allowlisted. M5 implements stock orders end-to-end in paper. M7 live-enables
options+stocks. **M7C** implements crypto qualification, fractional
quantities, session calendar, risk checks, demo fixtures, spy validation.
M8 hardening covers all three families.

**Sequencing amendment:** Milestone 1 *adds* the new configuration keys;
the `allow_live_trading` validator keeps refusing **only because the live
submission path is not built yet** — a flag must not pretend to enable code
that does not exist. Milestone 6 replaces that refusal with the real gate
stack, and Milestone 7 assigns `transmit=True` at the authorized boundary.

**LIVE TRADING IS A COMMITTED DELIVERABLE (owner directive, restated
2026-07-17: "non-negotiable").** No milestone may introduce language,
config, or tests that frame live execution as permanently disabled; interim
refusals must state they are awaiting the M6-7 implementation, nothing more.
The MVP live model remains owner-armed + per-order-confirmed exactly per the
owner's spec (`ALLOW_AUTOMATED_TRANSMISSION=false`); fully unattended
autonomy is the post-M7 extension seam reserved in that spec.

## 7. Open owner decisions (blocking only where marked)

1. **Confirm the split posture** (§1): platform stays live-incapable; only
   the wheel path becomes gated-live. *(Assumed yes; cheap to reverse.)*
2. `IB_ACCOUNT_ALLOWLIST` values and the live account suffix — needed at
   Milestone 6, never committed.
3. Symbol allowlist for live MVP (default AAPL,MSFT,SPY per spec).
4. Whether the `ib_async` wheel adapter should be retired after the official
   adapter lands, or kept as the documented optional secondary. *(Plan
   assumes: kept, optional.)*
5. GTC orders, margin-secured puts, automatic rolling: all out of scope for
   MVP per spec; revisit only by explicit owner request.
6. Crypto (owner-directed, §6b): confirm IBKR account crypto eligibility for
   the owner's jurisdiction before Milestone 7C; provide `CRYPTO_ALLOWLIST`
   symbols (suggested start: BTC, ETH); accept that crypto validation cannot
   use paper and requires an owner-performed minimal-size live acceptance.
7. Stock trading (owner-directed, §6b): whole shares, limit DAY, long-only
   in the MVP — confirm this matches intent. *(Assumed yes.)*

## STATUS NOTE (2026-07-17)

Milestones 0-4 are COMPLETE. Work is consolidated on the designated branch
`claude/chronos-trading-system-rrzroq` (a clean fast-forward of the prior
`feat/live-wheel-dashboard` line; no history was rewritten and no commits
were lost). All future milestones push there.

**Milestone 4 delivered** the dashboard cutover onto the loopback backend:
the strategy endpoints (GET /strategy/reconciliation, POST
/strategy/candidates/{symbol}, GET /strategy/regime/{symbol}, POST
/strategy/risk-preview, POST /strategy/demo-what-if, POST
/strategy/demo-approval — all allowlist-gated), the regime-context service
(`services/regime_context.py`, a Pine-derived EMA/RSI-2/vol-percentile
heuristic that is labeled "not a validated signal" and is never an order
gate), the thin `ui/api_client.py`, the page renderers
(`ui/pages/{portfolio,symbol_detail,order_workspace,activity,settings_page}`),
and the new default entrypoint `ui/backend_app.py`. Two structural guarantees
are enforced by tests: the UI reaches no broker module (AST walk + subprocess
sys.modules probe) and no page exposes a submit/transmit control. Gates green:
full pytest suite passing, mypy --strict clean, ruff clean.

**Milestone 5 delivered** the human-in-the-loop paper order-management
pipeline in a NEW, isolated `chronos.orders` package (it imports nothing from
the autonomous `chronos.execution`/`chronos.risk`, which stay live-incapable):
`WheelOrderIntent` + deterministic idempotency key; a structured tri-state risk
engine (`OrderRiskDecision`, per-check PASS/FAIL/UNKNOWN, unknown⇒fail, decision
expiry, the full check list incl. cash-secured-put at gross, covered-call
coverage, concentration, session-open, allowlist, caps, stock whole-share/
no-short); what-if preview; the SINGLE paper submission boundary
(`PaperOrderSubmissionBoundary.submit` — the only `transmit=True` in the
codebase, behind a fail-closed gate chain: lease → transmission_possible →
mode-lock PAPER_SUBMISSION → account match → risk approved+unexpired → typed
confirmation hash-match+TTL → USER_CONFIRMED idempotency); the order tracker
(lifecycle + duplicate-callback idempotency + partial-fill monotonicity);
buy-to-close, limit-only modify, cancel through CANCEL_PENDING; restart
reconciliation + SUBMISSION_UNKNOWN recovery (matches by `order_ref`, resolves
not-found to REJECTED, NEVER auto-retries); persistence v4 (`order_events`,
`risk_decisions`, `risk_check_results` + migration 0003); the `/orders/*` API
(token + writer-lease + allowlist gated); and `scripts/paper_soak_report.py`.
Stocks fold into the same pipeline (limit DAY, whole shares, long-only).

New `OrderLifecycle` states: `SUBMISSION_UNKNOWN`, `CANCEL_PENDING`. New
`RiskCheckStatus` enum. New stock `OrderIntent` members.

**M5 verification:** full pytest suite green (1419 passed), mypy --strict
clean, ruff clean. The pipeline is proven against a recording `FakeBroker` (the
happy path transmits exactly once with `transmit=True` to a paper account;
every refusal path leaves `submit_calls == 0`). `DemoBroker.submit_order` still
raises; `settings.transmission_possible` is False in every non-paper config, so
no order is placed by any test/CI/dev path.

**M5 owner-verified boundary (honest limitation):** the `OfficialIBKRBroker`
order methods still refuse — the official `ibapi` package is not installable in
this environment, so the adapter's `placeOrder`/`cancelOrder` wiring is
recording-spy validated in Milestone 7 (a correct order object emitted without
reaching a venue) and gateway-verified by the owner against a running paper
gateway, per §6's working agreement. The complete pipeline drives any `Broker`
implementation, so this is the one remaining integration seam.

The IBKR MCP connector is authorized and verified (account: USD 110 cash, no
positions — options wheeling requires further funding; stock/crypto families
are the executable ones at this size).

**Milestone 6 delivered** the live safety layer (built and tested, in the
isolated `chronos.orders` package; live transmission is NOT enabled — that is
M7): the fail-closed **ten-gate live stack** (`live_gate.py`: config,
connection, reconciliation, data, risk, preview, session-arming, per-order
confirmation, plus the kill-switch and session-drawdown breakers), each gate a
typed check asserted to block independently; **live arming** (`arming.py`,
backend memory, TTL, revocation, constant-time phrase compare that is never
logged/persisted/echoed, audited to `live_arm_events`); the **durable kill
switch** (`kill_switch.py`, atomic write, DISENGAGED default but fail-closed on
a corrupt file, audited to `kill_switch_events`); the **session-drawdown
breaker** (`session_drawdown.py`, persisted per-session NLV baseline, breach
engages the kill switch); the `/live/*` API (arm/disarm/status/kill, token +
writer-lease gated) wired into the runtime; and the **posture migration** —
settings validators, the UI settings page, and the game plan now frame live as
awaiting the M7 transmit wiring (never "permanently disabled"), with the
settings tests updated to match. Docs: `docs/live_trading_runbook.md`.

**M6 verification:** full pytest suite green, mypy --strict clean, ruff clean;
30 new safety tests (each of the ten gates blocks independently; arming
TTL/revocation; kill-switch fail-closed-on-corrupt + restart persistence;
drawdown breach engages the kill switch; the `/live` endpoints). No live order
is transmitted anywhere; `ALLOW_LIVE_TRADING` and LIVE+transmit remain refused
by configuration until M7.

**Next: Milestone 7** — live execution capability (validated without trading):
wire `transmit=True` for the LIVE branch at the single submission boundary,
building the live order object from the qualified contract/valid tick/confirmed
account/DAY limit; validate the full gate walk with a **recording spy broker**
(correct order object emitted, no order reaches a venue); then Milestone 7C for
crypto (fractional Decimal quantities, family session calendar).
