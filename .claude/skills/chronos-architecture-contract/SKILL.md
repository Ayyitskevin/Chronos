---
name: chronos-architecture-contract
description: >
  Load BEFORE touching any cross-cutting Chronos code. Answers: "how does this fit
  together", "where does X live", "can module A import module B", "system overview",
  "what are the invariants", "why is there a second adapter", "why are there two
  subsystems / two order planes / two kill switches", "architecture", "is this safe to
  refactor". The map of load-bearing design decisions, the invariants that must hold
  (each with its enforcing test), the ADR digest, and the openly-weak points. Use it
  before any refactor that crosses a package boundary, before adding an import between
  chronos packages, and whenever a doc and the code seem to disagree about authority.
---

# Chronos architecture contract

Facts dated 2026-08-02, verified against the live repo at `/home/user/Chronos`
(branch tip 47a8d72). Re-verify volatile facts with the commands in "Provenance
and maintenance" before relying on them.

Chronos is ONE repository holding TWO trading subsystems that must never merge, plus an
autonomy stack and an operator terminal. Confusing the two subsystems is the #1 hazard
in this codebase. Non-negotiables that bind every change here: fail-closed and
deny-by-default stay the default posture everywhere; never weaken a safety mechanism or
widen autonomous authority without a NEW ADR and an explicit owner decision; never claim
anything "done/working/validated" without naming the exact evidence. MITIGATED ≠ CLOSED:
every broker-adapter control is fixture-verified only — **no real IBKR gateway (paper or
live) has ever been connected in this project's history** (docs/limitations.md:22-23;
see chronos-real-gateway-campaign).

## 1. The system map

### The four zones

| Zone | Packages | Live-capable? |
|---|---|---|
| 1. Live Wheel order plane | chronos.orders / broker / api / services / strategy / ui / domain / persistence / config / notifications / utils + runtime.py, app.py | YES — gated LIVE branch (ADR-0009) |
| 2. Deterministic strategy platform | chronos.marketdata / strategies / indicators / backtest / research / risk / execution / portfolio / control / registry / histdata / skb / specs / auditlog / monitoring / service / cli | NO — live modes hard-denied in code (control/modes.py:86-95, ADR-0007) |
| 3. Autonomy stack | chronos.autonomy / supervisor + api/autonomy_wiring.py | Proposes into zone 1's full pipeline; transmits nothing itself |
| 4. Operator terminal | chronos.terminal (served by zone 1's backend) | Read-models + two mutating routes (ack, revoke) |

### One-line role per package

| Package | Role |
|---|---|
| chronos.orders | Order lifecycle propose→preview→confirm→submit; ALL live gates; the single `transmit=True` site (submission.py:745) |
| chronos.broker | IBKR adapters: `official_ibkr` (canonical, only validated order path), `ibkr` (ib_async, read-only, refuses orders), `demo` (deterministic fake); serialized connection manager |
| chronos.api | FastAPI backend: loopback-only, token auth, writer-lease heartbeat, order/live/autonomy/terminal routes, `autonomy_wiring`, BarProvider |
| chronos.services | Read-only evidence services: trading hours, liquid-hours parser (R-26), option-deliverable screen (R-27), reconciliation, short-put demo ladder |
| chronos.strategy | Wheel strategy engines: wheel_state, strike_resolver, eligibility, assignment_pressure (ORPHANED — see §5) |
| chronos.ui | Legacy Streamlit dashboard (M1–M10 era decision-support; demo rehearsals) |
| chronos.domain | Broker-neutral domain vocabulary (models, enums) |
| chronos.persistence | Main DB (SQLite, schema v7), repositories, hash chains, alembic migrations, account-scope binding |
| chronos.config | Frozen, validated `Settings` (env/.env); the live conjunction properties |
| chronos.notifications | Notifier port; the only transport is the local logger |
| chronos.utils | WriterLease (locking.py), account_fingerprint (identifiers.py), masked logging, secure files |
| chronos.runtime (module) | `build_runtime()` — wires kill switch FIRST, broker, DB scope, order pipeline |
| chronos.app (module) | Streamlit wheel-dashboard entry point (the `chronos` console script — NOT the platform CLI) |
| chronos.marketdata | Platform bar vocabulary, provider ports, data-quality checks, PacingController (shared with zone 1 by ADR-0019) |
| chronos.strategies | Deterministic strategy framework — proposals only, no broker/sizing access |
| chronos.indicators | Pine v5/v6-parity indicator library (ADR-0005) |
| chronos.backtest | Deterministic, byte-identical backtest/replay engine |
| chronos.research | Walk-forward / campaign / repro harness (see chronos-research-methodology) |
| chronos.risk | Platform deny-by-default risk engine; frozen YAML policy; zero means deny |
| chronos.execution | Platform execution engine + the QUARANTINED `brokers/ibkr_paper.py` (R-28) |
| chronos.portfolio | Converts strategy proposals into sized order intents |
| chronos.control | Mode lock (live hard-denied), persistent HaltStore, promotion gates |
| chronos.registry | Experiment-registry hash-chain ledger + holdout guardian (ADR-0013) |
| chronos.histdata | Separate-process historical-data store (unadjusted bars); never holds the writer lease |
| chronos.skb | Strategy Knowledge Base — hash-pinned join of corpus/specs/results |
| chronos.specs | Canonical vendor-neutral strategy specifications |
| chronos.auditlog | Append-only hash-chained JSONL audit log |
| chronos.monitoring | Read-only platform monitor; imports no broker adapter |
| chronos.service | Platform SHADOW/PAPER supervised loop (NO_ORDERS in shadow) — NOT chronos.services |
| chronos.cli | Platform CLI (`python -m chronos.cli`): status/halt/rearm/backtest/registry/holdout/research |
| chronos.autonomy | Model-facing contracts ONLY: ProposedDecision, AutonomyMandate, read/decision tools. Imports no trading module |
| chronos.supervisor | Deterministic gateway: ingress → queue → admission → sizing → compiler → loop; durable memory; owner alerts |
| chronos.api.autonomy_wiring | The ONLY non-supervisor consumer of the contracts; boot auto-activation (ADR-0017); order-plane handoff |
| chronos.terminal | Command registry, panel read-models (views.py), build-free ES-module client |

### Import-direction rules (who may touch whom) and their enforcing tests

| Rule | Enforced by |
|---|---|
| Exactly one `transmit=True` in chronos.orders (submission.py:745); no new transmit site anywhere in the repo, either spelling | tests/safety/test_single_transmit_site.py; tests/safety/test_broker_mutation_inventory.py (repo-wide, both `transmit=True` and `order.transmit = True`) |
| chronos.orders imports nothing from chronos.execution — the two order planes never cross | grep-verified 2026-08-02 (zero imports); submission.py:1-9 docstring discloses the dormant execution-plane site |
| chronos.autonomy imports NONE of orders/broker/execution/risk/api/persistence/services/control, ibapi, ib_async, sqlalchemy | tests/safety/test_autonomy_contracts.py:69-81 (`_FORBIDDEN`), :295-301 AST walk, :304-318 subprocess sys.modules probe |
| Autonomy contracts consumed only by chronos.supervisor + `api/autonomy_wiring.py` by explicit name; terminal holds a narrower mandate-only exemption | tests/safety/test_autonomy_contracts.py:321-358, :361-373 |
| The automated tree (incl. autonomy, supervisor) may not import chronos.registry or call the holdout unlock | tests/safety/test_registry_no_automated_unlock.py:37-51 |
| The supervisor never imports order-plane result types — the handoff result is deliberately untyped | src/chronos/supervisor/loop.py:161-168 (design note; see weak point 5) |
| chronos.histdata never reaches the trading plane (orders/broker/persistence/lease/DB) | tests/safety/test_histdata_isolation.py (AST + subprocess) |
| chronos.registry never reaches the trading plane | tests/safety/test_registry_isolation.py |
| Research code never reaches the REAL order/broker path (simulated execution is legitimate) | tests/safety/test_research_isolation.py |
| chronos.monitoring imports no broker adapter | src/chronos/monitoring/__init__.py:1-12 (structural, mirrored on tests/unit/test_ui_no_broker_imports.py) |
| Deterministic platform live modes CANARY_LIVE/LIVE are hard-denied; no configuration can enable them | src/chronos/control/modes.py:86-95; tests/safety/test_safety_invariants.py:123 |
| Allowed cross-zone exception: PacingController lives in chronos.marketdata and is imported by both api.bars and histdata (deliberate, ADR-0019 §3) | src/chronos/marketdata/pacing.py |

Adding ANY import that crosses these lines is an architecture change: it needs an ADR,
and most of the tests above will fail first. That is them working. Do not widen a
forbidden-list or add a module-name exemption to make a feature fit.

## 2. The invariants table

Every row: what must hold, why, where the code enforces it, the test that pins it, and
what breaks if it is violated. All file paths relative to repo root, `src/chronos/`
abbreviated to `…`.

| # | Invariant | Why | Enforced at | Enforcing test | If violated |
|---|---|---|---|---|---|
| 1 | Single transmit site: the only `transmit=True` assignment in chronos.orders is `…orders/submission.py:745`, inside `_cas_and_transmit_claimed`, after every gate of the selected branch | One auditable choke point for "bytes leave for the broker"; every gate is provably upstream of it | submission.py:744-745; adapter refuses requests without `transmit=True`/send_guard (…broker/official_ibkr.py:1375-1383) and never assigns a literal True (:1288) | tests/safety/test_single_transmit_site.py (AST, chronos.orders); tests/safety/test_broker_mutation_inventory.py (repo-wide, both spellings, pinned expected set) | A second transmit path exists that skips arming/kill/risk/reconciliation — the exact R-28 hazard. Any new site fails CI |
| 2 | The R-28 adapter stays quarantined: `…execution/brokers/ibkr_paper.py` (hardcoded `order.transmit = True` at :160) refuses construction without `quarantine_ack=True`, which nothing in src/ passes | It is a working placeOrder path with NONE of the ADR-0009 gates; kept for history, not use | ibkr_paper.py:103-113 (constructor refusal) | tests/safety/test_broker_mutation_inventory.py (site inventory + no-production-construction assertion); RISK_REGISTER.md:36 (R-28) | One wiring mistake creates a second, ungated broker plane. NEVER construct this class; never pass the ack outside its own tests |
| 3 | Writer lease: one writer per DB (30s TTL, …utils/locking.py:27); heartbeat renews at TTL/3 and ONE failed renewal demotes the backend to read-only permanently (…api/main.py:117-153); the boundary re-checks `holds()` in the DB inside the CAS-to-transmit window (submission.py:713-742), bound via `bind_lease_verifier` (:193-205, double-bind refused; wired at main.py:188-201) | R-24: before this, `renew()` had zero callers — two backends could both believe they were the writer | locking.py:143 (`holds()`); main.py:188-200 (`_still_the_writer` = not read_only AND holds()) | tests/safety/test_writer_lease_fencing.py | Split-brain: two processes submit against one account. DISCLOSED RESIDUAL (RISK_REGISTER R-24): IBKR knows nothing of the lease — no broker-side fencing exists; the window is narrowed, not closed |
| 4 | Model plane cannot reach the broker: chronos.autonomy imports no trading module, ships no write tools (`ToolKind = {READ, DECISION}`, …autonomy/tools.py:63-73), and Chronos calls no model — the external worker POSTs in | ADR-0016 §3: the single largest risk expansion in project history is kept narrow by structural incapacity, not policy | autonomy package imports (restated constants, not imports: decision.py:67-72) | tests/safety/test_autonomy_contracts.py:295-318 (AST walk + subprocess probe); tests/safety/test_model_tool_surface.py | A model-plane import of orders/broker is a straight path from prompt injection to order transmission |
| 5 | A decision cannot express an order: `ProposedDecision` has no account/broker/routing/transmit/order-type/price fields, cannot author its own `decision_id`/`provenance` (stamped by the queue writer, …supervisor/queue.py:211-219), and every model must inherit `AutonomyModel` (validators re-run on `model_copy`, …autonomy/base.py:31-41) | The kernel computes and clamps; the model only requests. Closes the M1 `model_copy` escalation | …autonomy/decision.py (contract); …supervisor/ingress.py:73 (writer-owned fields refused) | tests/safety/test_autonomy_contracts.py:87-110 (forbidden field-name set) | A decision that names an account, price, or transmit flag is an order by another name — the gateway's veto becomes decorative |
| 6 | Mandate is frozen, expiring, owner-authored: every mandate model frozen `extra="forbid"` (…autonomy/mandate.py:12); live ceiling `MAX_LIVE_MANDATE_DURATION` = 365d (mandate.py:69, enforced :402-407); authority IS the owner-authored JSON file named by `AUTONOMY_MANDATE_FILE`, digest-stamped at boot (…api/autonomy_wiring.py:318-367); revocation survives restart (:145-157); wrong-account/invalid file boots inert with a CRITICAL alert | Standing authority must be bounded in time, unforgeable in content, and revocable durably (ADR-0016 §4 as amended by ADR-0017 §1) | mandate.py validators (:399-473); autonomy_wiring.py:105-174 | tests/safety/test_autonomy_contracts.py (duration/immutability); tests/safety/test_supervisor_gateway.py:634-750 (ENFORCED/INERT pin per mandate field) | Perpetual or self-modifying authority. Note the inversion: `model_discretion` waives CAPITAL CEILINGS ONLY — floors, scopes, order forms, strategies, data qualities stay deny-by-default (ADR-0017 §2/§5; see chronos-autonomy-and-mandates) |
| 7 | Deny-by-default risk everywhere: order-plane `OrderRiskEngine` passes only if EVERY check is PASS; UNKNOWN blocks (…orders/risk.py:165-169; `opening_orders_today: int \| None = None` at risk.py:107 — an uncountable day blocks); platform `chronos.risk` all-zero policy rejects everything; admission refuses any check whose evidence is absent (…supervisor/admission.py:11-14) | R-25 taught this: an `int = 0` default made the daily cap pass forever. Absent evidence must refuse, never default | risk.py:140-178; …risk/ (platform); admission.py:285-432 | tests/safety/test_opening_cap_exercised.py; tests/safety/test_safety_invariants.py:235 (all-zero policy); tests/safety/test_supervisor_gateway.py | Restoring a "safe-looking" default (0, False, empty-passes) silently disables a control — the signature Chronos defect class |
| 8 | TWO SEPARATE STOP MECHANISMS with OPPOSITE missing-file defaults — see the boxed section below | — | …control/halt.py:102-117 vs …orders/kill_switch.py:83-92 | tests/safety/test_safety_invariants.py:198 (missing halt ⇒ HALTED); tests/unit/test_live_safety_layer.py:90 (`test_kill_switch_fresh_is_disengaged`) | Operator stops the wrong plane during an incident; a restore silently boots the live plane stoppable-by-nothing |
| 9 | Mode lock: the deterministic platform refuses CANARY_LIVE/LIVE unconditionally — ADR-0007 untouched by ADR-0016/0017 | Zone 2's promotion vocabulary must never become a live path; autonomy's ladder is a DIFFERENT vocabulary on purpose | …control/modes.py:86-95 (`DENIED_LIVE_DISABLED`, "no configuration can enable them") | tests/safety/test_safety_invariants.py:123-134 | The quarantined R-28 adapter becomes reachable from a "promoted" platform mode |
| 10 | Tamper-EVIDENT records, honestly bounded: hash-chained JSONL audit log (…auditlog/log.py), per-stream DB hash chains (…persistence/hash_chain.py), registry ledger + out-of-band head anchor `registry.head.json` (…registry/ledger.py:7-20) | Detect targeted edits, deletions, reordering, truncation (incl. un-burning a holdout) | append paths fsync + verify-before-trust | tests/unit/test_database.py, tests/safety/test_supervisor_durable_state.py; `python -m chronos.cli verify-audit-log`, `registry verify` | NOT tamper-PROOF (R-33, RISK_REGISTER.md:42): a writer who rewrites the whole chain (or ledger+anchor together) wins; no external/off-host anchor exists. Never claim otherwise |
| 11 | Account-fingerprint DB binding: the DB is scope-bound to (broker_mode, environment, SHA-256 account pseudonym) — raw account ids never persisted (…utils/identifiers.py:17-24); rebinding a populated DB to a different scope refuses | Durable safety state (lease, kill events, counters, activations) must not silently apply to the wrong account | …persistence/database.py:161-201 (`bind_scope`) | tests/unit/test_database.py:190+ | Orders and safety history from account A silently govern account B; also: repointing `DATABASE_URL` detaches ALL durable safety state |
| 12 | Owner alerts are structurally network-free: log sink + local JSONL file sink only; a network import in the delivery module fails a test | A networked sender = credentials beside a money-moving process + an egress path + silent failure mode (R-32) | …supervisor/delivery.py:9-29 | tests/safety/test_alert_delivery.py | An outbound channel appears in the broker-holding process. Adding one requires an ADR and belongs OUTSIDE this process (Phase 2 sidecar) |
| 13 | Terminal read/write split: all /terminal data routes take token OR session cookie; the cookie is `path=/terminal` so the browser never sends it to /orders or /live; only two mutating routes (acknowledge, revoke), both writer-gated; client has zero HTML sinks (textContent only) | The always-open operator surface must be unable to move money even if fully compromised | …api/terminal_session.py:26-42 (defense stack); …api/routes/terminal.py:175 (router credential), :642, :703 (WriterDep) | tests/safety/test_terminal_client_has_no_html_sinks.py; tests/safety/test_terminal_client.py; tests/integration/test_terminal_api.py | Widening the cookie path or adding innerHTML turns an XSS into order authority. `path=/terminal` is THE load-bearing property |
| 14 | UNKNOWN renders as unknown, never zero: terminal read-models use `None` = unknown; a failed positions read is `positions_observed=false`, not an empty portfolio | Zero reads as "all clear"; the panels exist to distinguish "nothing" from "cannot see" | …terminal/views.py:9-47 (the one rule reviews keep re-checking) | tests/safety/test_terminal_views.py | An operator trusts a dead panel; same doctrine as invariant 7's None-not-zero |
| 15 | Hostile-payload ingress: `POST /autonomy/proposals` bounds size to 256 KiB BEFORE parsing (…supervisor/ingress.py:65), refuses >16 nesting levels (:69), NaN/Infinity, non-single-object JSON, and any payload carrying writer-owned `provenance`/`decision_id` (:73); refusals never echo payload content | The worker is untrusted input into the broker-holding process (R-30 bounded, not solved) | ingress.py:65-73, 101, 153, 173 | tests/safety/test_autonomy_cycle.py:475+ ("the hostile ingress") | Parser resource exhaustion or forged provenance in the money process. KNOWN GAP: the credential is the shared local token, not proposal-only (weak point 6) |
| 16 | Single brokered research reader + holdout guardian: held-out data unmasks ONLY via `unlocked=True` passed inside …registry/holdout_guardian.py; the guardian requires an owner-typed, single-use, file-locked unlock and verifies the ledger before trusting it | A burned holdout reused as "fresh" already happened once (M5 review); trial counts and burns must be ledger-derived, not self-reported | registry/holdout_guardian.py; ledger verify-before-trust (ledger.py:7-20) | tests/safety/test_single_unmask_site.py (AST: `unlocked=True` nowhere else, and the guardian IS a site); tests/safety/test_registry_no_automated_unlock.py | Research evidence silently loses its out-of-sample meaning; the DSR/trial-count math (chronos-research-methodology) becomes fiction |

## 3. TWO SEPARATE STOP MECHANISMS — read this twice

This asymmetry is deliberate, disclosed, and the single most operator-hostile fact in
the system. Procedures live in chronos-run-and-operate; the contract is:

| | Platform halt (zone 2) | Live kill switch (zone 1) |
|---|---|---|
| File | `data/platform_halt.json` | `data/live_kill_switch.json` |
| Code | …control/halt.py (HaltStore) | …orders/kill_switch.py (LiveKillSwitch) |
| Engage | `python -m chronos.cli halt --reason …` | `POST /live/kill` (deliberately NOT writer-gated, routes/live.py:105-121) |
| Stops | ONLY the deterministic platform | ONLY the chronos.orders live plane |
| **Missing file** | **⇒ HALTED** (NEVER_ARMED, halt.py:102-109) | **⇒ DISENGAGED** (kill_switch.py:83-85) |
| Corrupt file | ⇒ HALTED (STATE_CORRUPTION, :110-117) | ⇒ ENGAGED (fail closed, :86-92) |

Consequences you must internalize: `chronos.cli halt` does NOT stop live trading.
docs/INCIDENT_RESPONSE.md only knows the platform halt (Phase-1 finding 2). A restore
that omits `data/live_kill_switch.json` boots the live plane kill-DISENGAGED — the
backup doc does not even list the file (Phase-1 finding 3). Both defaults are pinned by
tests (test_safety_invariants.py:198; test_live_safety_layer.py:90), so "fixing" the
asymmetry is an owner-gated design change, not a bug fix.

## 4. Architecture decisions digest (ADR-0001..0019)

A MAP, not the territory — details, context, and residuals live in `docs/adr/`. Read
the ADR before relying on any row. In-place supersessions are part of the files: never
quote ADR-0016 §4/§6 or ADR-0004 §5 without reading the bracketed correction notes.

| ADR | Decision (one line) | Why (one line) | Status (2026-08-02) |
|---|---|---|---|
| 0001 | Extend the existing repo, don't rewrite | Working wheel code + history beats a green field | Accepted (D-01) |
| 0002 | IBKR via TWS API with ib_async | Official socket API, maintained async wrapper | Accepted (D-02) — but the live-Wheel PRODUCTION adapter is now `official_ibkr` (ADR-0009 era; D-19); ib_async is the read-only secondary (R-12) |
| 0003 | Platform persists to its own files, separate from the wheel ledger | Two subsystems must not share durable state | Accepted (D-03) |
| 0004 | Structural separation of authority; no generative AI in runtime | Determinism and reviewability | §5 (AI prohibition) SUPERSEDED by ADR-0016; §§1-4 preserved and load-bearing |
| 0005 | Closed-bar deterministic engine; next-bar fills | Byte-identical replay; no intra-bar fantasy | Accepted (D-05/D-06) |
| 0006 | Research data from public mirrors with per-file provenance | Auditability of every research input | Accepted (D-07) |
| 0007 | Mode lock refuses live modes; halt survives restart | Platform must be structurally live-incapable | Accepted (D-08/D-09) — UNTOUCHED by 0016/0017 (invariant 9) |
| 0008 | Executable candidates: daily-bar, long-only ETF | One tractable, honest vertical first | Accepted (D-12) |
| 0009 | The LIVE branch at the single submission boundary | Live capability only as a gated branch of the ONE boundary | Accepted (design-panel remediated) |
| 0010 | The CRYPTO product family | Owner-requested family behind its own caps/allowlist | Accepted (§4's opening-cap claim was FALSE until M10 — corrected in place) |
| 0011 | Two-process historical-data plane | Pacing + isolation: data ingest must never touch the trading plane | Accepted |
| 0012 | Options chain/IV/greeks forward capture | Options backtests need data that must be captured forward | **Status: proposed** (capture store ships EMPTY) |
| 0013 | Experiment registry + holdout guardian | Trial counts and holdout burns enforced in code, not honor | Accepted (post-merge review remediated) |
| 0014 | Walk-forward + sample-honest statistics | Default verdict INSUFFICIENT_EVIDENCE; DSR/bootstrap gates | **Status: proposed (design-review pending)** yet implemented and binding in code |
| 0015 | Re-validation campaign | One reproducible grid over the frozen gates | **Status: proposed (design-review pending)** |
| 0016 | Controlled autonomous model authority: one typed decision, one gateway, owner mandate, deterministic veto | Owner directive 2026-07-25 ended the AI prohibition; risk kept narrow by structure | Accepted; **§4 and §6 superseded IN PART by ADR-0017** (365d ceiling, persistent mandate, protected MARKET); §8's not-superseded list is closed |
| 0017 | Owner-directed maximal autonomy: persistent auto-activating mandate, model_discretion over capital ceilings ONLY, MARKET = collared limit | Owner directive same day: remove friction ceilings, never execution-correctness mechanisms | Accepted; its §5 "not superseded" list restates ADR-0016 §8 verbatim |
| 0018 | Operator terminal: fresh, Python-served, no second runtime | tyche/midas rejected on evidence; no Node toolchain beside the money process | Accepted, implemented |
| 0019 | Chart bars from the broker adapter, not the research corpus; BarProvider never sleeps | Terminal charts must not contend with order submission (R-42) | Accepted |

DECISIONS.md is the index: D-11 (no-AI rule) is struck through, SUPERSEDED by D-16
(DECISIONS.md:18, :23); note the file's row order puts D-17/D-18 after D-19.

## 5. Known-weak points — all OPEN, stated plainly

These are disclosed, current defects/gaps. Do not soften them, do not silently "fix"
the owner-gated ones, and do not build on top of them as if they were sound.

The 8 Phase-1 findings (docs/VISION_COMPLETION_PLAN.md:143-165, observed 2026-08-01,
re-verified current 2026-08-02):

1. **Reconciliation readiness is consumed by ONE opening submission** and nothing
   re-arms it — no supervised callback consumer, no bounded periodic convergence loop
   (…orders/reconciliation_readiness.py:129-158). Each subsequent opening needs a fresh
   reconcile. Do not "fix" this by weakening the latch.
2. **The incident runbook invokes the wrong stop** — docs/INCIDENT_RESPONSE.md drives
   the platform halt only; it never mentions `POST /live/kill` (§3 above).
3. **Restore guidance overstates safety** — a by-the-book restore omits
   `data/live_kill_switch.json` and boots the live plane kill-DISENGAGED (§3 above).
4. **Prose says the mandate replaces arming; the code says it does not.** Every LIVE
   submit still requires a current in-memory arm (…orders/submission.py:441;
   live_gate.py gate 7); the mandate replaces NOTHING in chronos.orders, and the wiring
   auto-mints the gate-8 confirmation itself (autonomy_wiring.py:201-202). Three docs
   tell three different stories. Choosing the authority model is an OWNER GATE.
5. **Supervisor COMPLETE ≠ submitted.** Any non-exception handoff return — including
   `SubmissionOutcome(submitted=False)` refusals, venue rejections, ambiguous sends —
   journals as `CycleStage.COMPLETE` (…supervisor/loop.py:405-453). Read
   `handoff.submitted` before believing a cycle traded. The fix must respect the
   untyped-handoff isolation (loop.py:161-168), not delete it.
6. **External-worker provenance is static and its credential is not proposal-only.**
   Every proposal is stamped with the constant `INGRESS_IDENTITY`
   (autonomy_wiring.py:84-94); the ingress token also opens every other mutating route.
7. **Economic-looking decision fields are inert**: `exit_plan`,
   `protective_order_required`, `max_acceptable_loss_usd`, `requested_risk_budget_usd`
   affect nothing but the dedup fingerprint. A model-requested protective stop is
   placed by NOTHING. This violates AGENTS.md's "enforced, advisory, or forbidden" rule
   and is a disclosed release blocker.
8. **Promotion is not evidence-bound.** `FamilyPromotion` rungs are self-declared JSON
   in the owner's mandate file; no code grants, records, demotes, or binds a rung to
   the replay/shadow/paper evidence ADR-0016 §7 requires. Writing
   `CAPPED_LIVE_AUTONOMOUS` into the file satisfies every check.

Beyond Phase 1:

- **Pacing budgets are per-process fiction** (R-42, RISK_REGISTER.md:47): backend and
  histdata each self-pace 6 req/min under different client ids; whether IBKR's real
  limit is shared across client ids is unknowable without a live gateway.
- **R-18 clock drift OPEN** (RISK_REGISTER.md:29): every TTL/staleness gate trusts the
  host clock; automated NTP verification is not implemented.
- **R-33 no external anchor** (RISK_REGISTER.md:42): all tamper evidence is on-host;
  a full-rewrite attacker wins (invariant 10).
- **No options simulator exists.** The wheel (an options strategy) has zero backtest
  evidence; ADR-0012's forward-capture store ships empty. See chronos-wheel-and-options.
- **assignment_pressure is ORPHANED**: `…strategy/assignment_pressure.py` has zero
  production callers (grep-verified 2026-08-02; only its own unit test imports it) — an
  economic-looking module wired to nothing, same smell as finding 7.
- **The capital question is LIVE**: last account snapshot ≈ USD 110 vs the ~USD 3,000
  premise baked into defaults like `MIN_CASH_BUFFER_USD=5000` (R-10 predates the
  revision). An unresolved OWNER decision — flag it, never assume either number.
- **Everything broker-adjacent is fixture-verified only** — no real gateway ever
  (§ intro). The first read-only real-gateway session is the Phase-2 exit gate:
  chronos-real-gateway-campaign.

## 6. Churn and stability (visible window only)

CAVEAT: this clone is SHALLOW — 150 visible commits, grafted at a65c7b3 (2026-07-16).
History before the graft (the original M1–M10 wheel build, ADR-0001..0008 adoption) is
NOT excavatable locally (`git rev-parse --is-shallow-repository` ⇒ true). Churn claims
below describe 2026-07-16 → 2026-08-01 only.

- Hottest files (commits touching them): RISK_REGISTER.md 23, CHANGELOG.md 21,
  docs/limitations.md 20, README.md 20 — the top churners are TRUTH-MAINTENANCE
  documents; expect to update them with any safety-relevant change. Hot code:
  config/settings.py 12, api/main.py 12, broker/official_ibkr.py 11,
  tests/safety/test_autonomy_contracts.py 11, runtime.py 10, orders/submission.py 9 —
  exactly where the kernel defects lived. Touching these means touching the contract:
  re-read the relevant invariant row first.
- Stable interfaces (1-2 visible commits, settled since the pre-graft era or first
  landing): …auditlog/log.py (1), …control/halt.py (2), …registry/ledger.py (2),
  …supervisor/loop.py (3, post-M7), broker/base.py Protocol (5). Changing any of these
  is rare and consequential — expect an ADR, not a patch.
- Zero file deletions in the visible window; the repo quarantines instead of deleting
  (R-28). Follow that norm.

## 7. When NOT to use this skill

- Extending autonomy, mandates, admission/sizing/compiler details → chronos-autonomy-and-mandates
- Wheel state machine, deliverable checks, options gating specifics → chronos-wheel-and-options
- IBKR field semantics, Contract-vs-ContractDetails map, pacing, inert-control prevention → chronos-ibkr-boundary
- Day-to-day running, kill/halt/arm/revoke PROCEDURES, backup/restore → chronos-run-and-operate
- What counts as evidence, test-suite map, proof patterns → chronos-validation-and-qa
- Statistical gates and research discipline → chronos-research-methodology
- Which document to trust when prose conflicts → chronos-docs-map (precedence rule:
  AGENTS.md:42-54 — current executable facts outrank every document)

## Provenance and maintenance

All facts verified 2026-08-02 against branch tip 47a8d72 by direct read at the cited
file:line, or by the read-only commands below. If a re-verification command disagrees
with this skill, the repo wins — update this file. Run everything from the repo root.

| Volatile fact | Re-verify with (read-only) |
|---|---|
| Single transmit site at submission.py:745 | `grep -n "transmit=True" src/chronos/orders/submission.py` |
| All invariant-pinning tests still pass | `.venv/bin/python -m pytest tests/safety -q` |
| Quarantine + dormant site lines | `grep -n "quarantine_ack\|order.transmit = True" src/chronos/execution/brokers/ibkr_paper.py` |
| Stop-mechanism defaults (halt vs kill switch) | `grep -n -A3 "except FileNotFoundError" src/chronos/control/halt.py src/chronos/orders/kill_switch.py` |
| Mode lock still hard-denies live | `grep -n -A6 "_LIVE_MODES:" src/chronos/control/modes.py` |
| Phase-1 findings still open / unchanged | `sed -n '138,166p' docs/VISION_COMPLETION_PLAN.md` (then reverify each against code, as the doc itself orders) |
| Risk-register rows cited (R-18/24/28/32/33/42) | `grep -n "R-18\|R-24\|R-28\|R-32\|R-33\|R-42" RISK_REGISTER.md` |
| assignment_pressure still orphaned | `grep -rln "assignment_pressure" src/chronos tests` (only the module + its unit test ⇒ still orphaned) |
| Mandate-replaces-arming contradiction unresolved | `grep -n "is_armed" src/chronos/orders/submission.py` (still gate-fed ⇒ unresolved) |
| Churn / stability counts | `git log --format= --name-only \| sort \| uniq -c \| sort -rn \| head -20` |
| Shallow-clone boundary | `git rev-parse --is-shallow-repository && cat .git/shallow` |
| ADR statuses (incl. 0012/0014/0015 "proposed") | `grep -n "^Status:" docs/adr/ADR-*.md` |
| No real gateway ever (still true?) | `sed -n '20,25p' docs/limitations.md`; absence of capture artifacts under `fixtures/` |

Maintenance rules: (1) any change to a file cited in the invariants table requires
re-reading that row and re-running its enforcing test before claiming anything; (2) a
new import between chronos packages requires checking §1's rules table and, if it
crosses a line, an ADR + owner decision; (3) never edit this skill to say a control is
CLOSED unless RISK_REGISTER.md says CLOSED and names real-gateway evidence.
