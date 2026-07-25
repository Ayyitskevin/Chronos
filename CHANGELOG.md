# CHANGELOG

## [Unreleased] — M2 review remediation: admission hardening (2026-07-25)

Remediation of the M2 five-lens adversarial review. Ten findings from the admission lens
were confirmed by an independent verification pass; all are fixed here. (Four verifier
agents were killed by a session limit mid-run; their lenses — sizing, lease, quarantine,
claims — are being re-verified and any surviving findings will follow.)

### Fixed (HIGH)
- **The strategy allowlist applied only to OPEN.** A HEDGE, INCREASE, ROLL or REPLACE
  carrying no `requested_strategy` was admitted with the check recorded as *passed*. That
  defeated the mitigation ADR-0016 §6 publishes for shorting — "omit SHORT_EQUITY from
  scope" — because a SHORT-direction HEDGE never had its strategy compared against the
  scope at all. Every exposure-creating kind must now name a permitted strategy, and a
  SHORT direction additionally requires an explicitly short-capable strategy.
- **An unevaluated evidence-bundle check was recorded as PASSED**, contradicting the
  module's own "no default-allow branch" claim, and only the bundle *id* was compared.
  Now: an unknown bundle refuses (`EVIDENCE_BUNDLE_UNKNOWN`), the **digest** is compared
  too, and `AdmissionCheck` gained an `evaluated` flag so an unevaluated check can never
  read as satisfied.
- **Four mandate limit groups were read by no code** while the mandate docstring claimed
  the supervisor re-derived "every limit". The claim is corrected, the honest
  enforced-vs-inert list now lives in one place (`admission.py`), and a test pins it so a
  mandate field cannot be added without declaring which it is.

### Added (previously missing checks)
- **Mandate activation, revocation, and restart reactivation.** Authoring a mandate is not
  enabling it; admission now requires an authenticated owner activation event, refuses a
  revoked one, and enforces `RestartBehavior.REQUIRE_REACTIVATION` against a process
  generation. Previously `restart_behavior` was inert.
- **Market-data freshness, quality, and spread**, from supervisor-gathered evidence.
  Absent evidence refuses.
- **Bounded re-submission after refusal** (R-31): a refused decision may be retried at most
  `MAX_RESUBMISSIONS` times. Replay protection previously covered only *admitted* ids.

### Documentation honesty
- `decision.py` no longer claims the provenance-stamping gateway landed in M2; the pin
  check proves agreement, not authorship, until the decision-queue writer lands (M4).
- ADR-0016's citation-binding paragraph now states what M2 actually delivered (bundle
  id+digest binding) versus what it did not (resolving individual citations).
- `docs/limitations.md` gains a full M2 section: what the gateway enforces, and the open
  gaps — loss/activity limits, `scope.exchanges`/`contract_families`, sector/family/
  correlated concentration, leverage, margin, and **the absent compilation step**, which
  the directive listed under M2 and which did not ship.

### Gates
ruff clean, ruff format clean, mypy --strict clean (193 files), pytest 1957 passed / 1
credential-gated skip.

## [Unreleased] — M2 fix: a zero ceiling authorizes nothing (2026-07-25)

**Found by self-review of the merged M2 sizing code, before any autonomous path could
consult it.** `size_order` *skipped* a mandate limit that was zero instead of binding on
it, which inverted deny-by-default exactly as the M1 review found for the floors: a
mandate whose capital ceilings were all left at their zero defaults — one that authorizes
nothing — sized to whatever cash allowed. Reproduced at **590 shares** where the correct
answer is "refuse".

Fixed at both layers:

- **Sizing:** every ceiling now binds, and zero binds at zero — per-order notional,
  per-order unit ceiling (the one that governs the asset class), allocated capital,
  per-symbol concentration headroom, and gross-exposure headroom.
- **Contract:** a submitting mandate must now state `allocated_capital_usd`,
  `max_order_notional_usd`, `max_gross_exposure_usd`, `max_symbol_exposure_pct`, and the
  unit ceiling matching its asset classes — so the failure surfaces at authoring time with
  a clear message, rather than as a silent refusal at trade time.

Regression tests cover both: the contract refuses to construct such a mandate, and sizing
refuses even when one is forced past validation via `model_construct`.

Gates: ruff clean, ruff format clean, mypy --strict clean (193 files), pytest 1944 passed
/ 1 credential-gated skip.

## [Unreleased] — M2: deterministic gateway, lease fencing, transmit quarantine (2026-07-25)

### Added — `chronos.supervisor` (the ModelDecisionGateway)
The first code that can turn a model decision into a *proposal*. It sits between
the model plane and `chronos.orders`, adds a gate, and removes none: an admitted,
sized decision is handed to the existing `OrderManagementService`, which applies
every gate it already applied to a human-proposed order and keeps the single
`transmit=True` site. The supervisor itself never touches a broker (asserted).

- `admission.py` — deny-by-default validation of a decision against the mandate
  in force: mandate presence, degraded state (refused *first*, so an AI/broker/
  data/lease failure never becomes permission), effective window, account
  fingerprint, submitting mode, decision replay, model/prompt/tool/schema version
  pins, evidence-bundle identity, HOLD as explicitly non-executable, asset class,
  instrument allowlist, strategy, per-family promotion, and order-form
  availability. Every check is recorded pass or fail, so a refusal is explainable.
- `sizing.py` — where "the model's requested quantity is not executable" becomes
  true in code. The request is an upper bound only; the kernel independently
  derives size from per-order notional, per-order unit ceilings, allocated
  capital, cash and buying-power **floors** (subtracted, so a floor genuinely
  reserves), per-symbol concentration headroom, and gross-exposure headroom —
  then clamps down and refuses when nothing survives. Decimal throughout;
  missing or absurd contract facts refuse rather than guess.
- 28 tests, incl. one proving the gateway re-checks promotion via
  `model_construct` rather than trusting a mandate that skipped validation.

### Fixed — R-24: the writer lease was never renewed, and was not a fencing token
`WriterLease.renew()` had **no production caller**. The lease expired after its
30-second TTL while the backend went on believing it was the writer, so a second
backend could acquire it and both would consider themselves authoritative.

- A lifespan heartbeat renews at TTL/3 and, on any failure, demotes the process
  to read-only permanently (re-acquiring would be unsafe — another writer may
  already have acted).
- New `WriterLease.holds()` re-checks ownership in the database; the submission
  boundary calls it immediately before the transmit line, beside the kill-switch
  re-read. A refusal there is provably not-sent.
- Residual, disclosed: this narrows the window, it does not close it. IBKR
  accepts an order without knowing about our lease, so broker-side fencing is
  unavailable.

### Fixed — R-28: the second transmit site is now quarantined and inventoried
`execution/brokers/ibkr_paper.py` enables transmission with an *attribute*
assignment (`order.transmit = True`) outside `chronos.orders`, which the
keyword-scoped single-transmit-site test structurally could not see.

- New `tests/safety/test_broker_mutation_inventory.py`: a repository-wide
  inventory matching **both** spellings, pinned to an explicit expected set, so a
  new transmit site anywhere fails CI; plus an AST assertion that no production
  module constructs the adapter.
- The adapter refuses construction without `quarantine_ack=True`, which nothing
  in `src/` passes — an accidental wiring fails loudly instead of quietly
  acquiring a second, ungated broker path.

### Changed
- M1's milestone guard (nothing imports the contracts) is replaced by the
  permanent, narrower invariant: **only** `chronos.supervisor` may consume an
  `AITradeDecision`, and the supervisor may not import a broker adapter or the
  submission boundary.
- `CANCEL` is no longer classified as unconditionally risk-reducing — see the
  M2a entry below.

### Gates
ruff clean, ruff format clean, mypy --strict clean (193 files), pytest 1942
passed / 1 credential-gated skip.

## [Unreleased] — M2a: contract hardening from the M1 adversarial review (2026-07-25)

Remediation of the five-lens adversarial review of M1, done before any gateway work
because M2's gateway validates mandates and cannot be built on bypassable ones. Full
finding list in ADR-0016 §"Known limitations and residuals" item 0.

### Fixed (security-relevant)
- **Authority escalation via `model_copy(update=...)`.** Pydantic does not re-run
  validators on copy, so a one-day SHADOW mandate could be copied into a ten-year
  `LIVE_AUTONOMOUS` mandate with an empty scope and a SHADOW promotion rung — every
  mandate validator skipped. New `chronos.autonomy.base.AutonomyModel` re-validates on
  copy; all autonomy contracts inherit it.
- **Per-family promotion.** A single scalar `promotion_level` let one asset family's
  evidence license another's live trading, contradicting ADR-0016 §7. Replaced with
  `promotions: tuple[FamilyPromotion, ...]`, required for every permitted asset class.
- **Kind/payload coherence.** A CLOSE, REDUCE or CANCEL could carry a strategy, entry
  plan, risk budget, size and direction — an opening request wearing a risk-reducing
  label. Now refused per kind.
- **Floors are not deny-by-default.** `min_cash_floor_usd`, `min_buying_power_usd` and
  `max_quote_age_seconds` default to zero, which is the *most* permissive value, not the
  most restrictive. Submitting mandates must now set them explicitly; the docstrings that
  claimed otherwise are corrected.
- **Live data quality.** A live mandate could license FROZEN/DELAYED_FROZEN/DEMO data the
  deterministic live gate already refuses. Now restricted to LIVE/DELAYED.
- **AST import matchers were blind to `from chronos import <subpackage>`**, silently
  defeating the autonomy isolation test, the M1 milestone guard, and the ADR-0013 holdout
  bar. All three fixed, each with a guard-the-guard test.
- **Naked-short guarantee was a substring scan** that a member named `SHORT_CALL` would
  have passed. `StrategyForm` is now pinned to its exact member set.
- Bounded `target_client_reference` to the exact `CHR-<PREFIX>-<32 hex>` shape, bounded
  the evidence tuple and all monetary/trigger amounts, restricted symbol and futures-root
  alphabets, cross-validated scope strategies against permitted asset classes, and made
  the decision plane refuse `FUTURE_OPTION` explicitly.

### Documentation honesty
- README safety bullets now carry `[enforced]` / `[contract]` / `[M2+]` markers; several
  described machinery that does not exist yet as though it were live.
- Corrected stale claims the review surfaced outside the M1 diff: `DECISIONS.md` D-08 and
  D-15, `docs/ARCHITECTURE.md` item 1, `docs/safety.md`'s staleness scope,
  `docs/GO_LIVE_CHECKLIST.md`'s closing sentence, `docs/DEPLOYMENT.md`'s env-var rows,
  `docs/adr/ADR-0013`, and `src/chronos/__init__.py`'s description.
- `AITradeDecision` no longer claims a data-flow test that does not exist; the claim is
  scoped to the milestone guard that does, with the permanent test owed by M2.
- `DecisionProvenance` now documents that it is stamped by the deterministic queue writer,
  not self-reported by the model — otherwise the version-pin check is a self-attestation.

### Gates
ruff clean, ruff format clean, mypy --strict clean (190 files), pytest 1901 passed /
1 credential-gated skip. Still no broker behavior: nothing outside `chronos.autonomy`
imports the contracts.

## [Unreleased] — controlled autonomous model authority (M1, 2026-07-25)

### Governance
- **ADR-0016 — Controlled Autonomous Model Authority** added. Supersedes **ADR-0004 §5 only**
  (the generative-AI prohibition); ADR-0004 §§1-4 (D-04, structural separation of authority)
  are preserved and load-bearing.
- **DECISIONS.md D-11 marked superseded in place** (kept for history) and replaced by **D-16**:
  an approved generative model may originate runtime trading decisions only through a typed
  `AITradeDecision` and the single deterministic ModelDecisionGateway; it cannot access IBKR
  directly, change its authorization, weaken policy, or bypass any deterministic gate.
- D-15's prospective copilot bar retargeted to the real `chronos.autonomy` plane (unchanged,
  not relaxed).
- Migrated: README, `docs/ARCHITECTURE.md`, `docs/architecture.md`, `docs/safety.md`,
  `docs/limitations.md`, `docs/AI_QUANT_GAME_PLAN.md`, `docs/LIVE_WHEEL_GAME_PLAN.md`,
  `docs/GO_LIVE_CHECKLIST.md`, `docs/live_trading_runbook.md`, `docs/TEST_PLAN.md`,
  `ASSUMPTIONS.md`, `TASKS.md`, `RISK_REGISTER.md`.

### Added
- `chronos.autonomy` — **contracts only, wired into nothing**: `AITradeDecision` (typed,
  frozen, `extra="forbid"`, structurally unable to express a broker order) and
  `AutonomyMandate` (owner-authored, versioned, expiring, revocable, deny-by-default), plus
  the autonomy vocabulary (modes, promotion ladder, asset classes, strategy and order forms).
- `tests/safety/test_autonomy_contracts.py` — 24 structural tests enforcing D-16, including
  model-plane import isolation (AST + subprocess probe) and a milestone guard asserting M1
  added no broker behavior.

### Risk register
- R-01 restated (the blanket "no live-capable code path" claim retired as stale post-M7).
- R-24…R-27 opened for kernel defects the M0 audit found and autonomy makes more dangerous
  (unrenewed writer lease with no fencing token; inert `max_opening_orders_per_day`;
  permanently ambiguous `market_open`; demo-only option deliverable verification).
- R-28 (dormant second submission path), R-29 (autonomy risk expansion, accepted by owner
  directive), R-30 (prompt injection), R-31 (refusal re-submission loops) opened.

### Safety posture
- No broker behavior added. Nothing in `chronos.autonomy` is imported by any runtime path.
- Every deterministic guarantee in ADR-0016 §8 is unweakened: one transmit site, single-writer
  lease, idempotency, reconciliation to broker truth, contract qualification, stale-data
  rejection, durable kill switch and halt, drawdown breakers, capital/concentration/margin/
  leverage limits, restart recovery, immutable audit trails, DEMO defaults, and the
  prohibition on broker mutations from tests or CI.

## [Unreleased] — deterministic strategy platform

### Added
- Pine corpus ingestion: 42 scripts fetched byte-exact from the Notion
  "Pine Quant Library — Master Index" into `research/pine/`, SHA-256 pinned in
  `research/strategy_registry.yaml` (+ CSV/JSON catalogs) via
  `scripts/build_strategy_registry.py`.
- Platform packages under `src/chronos/`: `marketdata`, `indicators`, `specs`,
  `strategies`, `portfolio`, `risk`, `execution` (engine, state machine,
  ledgers, reconciliation, simulated broker, IBKR paper adapter), `control`
  (modes, halt, promotion), `auditlog`, `notifications`, `backtest`,
  `research`, `cli`.
- Derived strategy implementations with canonical YAML specs:
  `regime_trend_v1` (core of Pine 01 BULL+ v1.1), `mean_reversion_v1`
  (executable derivation of Pine 11 MR Extremes Study v1.1); baselines
  (buy-and-hold, SMA 50/200, deterministic random entries).
- Safety acceptance test suite (`tests/safety/`) covering mode locks, halt
  persistence, deny-by-default risk, execution gating, and strategy isolation.
- Deny-by-default risk policy schema + `config/risk.example.yaml`.
- Complete documentation set: `docs/ARCHITECTURE.md`, `docs/RISK_POLICY.md`,
  `docs/STRATEGY_CATALOG.md`, `docs/PINE_AUDIT.md`, `docs/PARITY_REPORT.md`,
  `docs/RESEARCH_REPORT.md`, `docs/STRATEGY_SELECTION.md`, `docs/TEST_PLAN.md`,
  `docs/TEST_RESULTS.md`, `docs/SECURITY.md`, `docs/DEPLOYMENT.md`,
  `docs/OPERATIONS.md`, `docs/BACKUP_AND_RECOVERY.md`,
  `docs/INCIDENT_RESPONSE.md`, `docs/IBKR_INTEGRATION.md`,
  `docs/IBKR_RUNBOOK.md`, `docs/GO_LIVE_CHECKLIST.md`, ADRs 0001–0008,
  `docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md`; plus
  `ASSUMPTIONS.md`, `DECISIONS.md`, `RISK_REGISTER.md`, `TASKS.md`, `HANDOFF.md`.
- Independent adversarial review across seven dimensions with all
  CRITICAL/HIGH findings remediated (see REMEDIATION_REPORT).
- Owner-only (0600) permissions on platform ledger/halt/audit files;
  halt-write fsync durability; collision-resistant order-intent ids;
  deny-by-default for unrecognized trading modes.
- Dependencies: `pyyaml` (+ `types-PyYAML` dev).

### Changed
- `.gitignore`: runtime state files under `data/` (json/jsonl/tmp) ignored.
- `pyproject.toml`: dependency additions only; existing wheel-dashboard code
  untouched.

### Safety posture (unchanged and extended)
- Wheel dashboard: live-money transmission remains hard-disabled; IBKR order
  methods still raise unconditionally.
- Platform: live-capable modes resolve to a hard-denied capability; paper
  submission requires six simultaneous independently-verified conditions; a
  new deployment starts halted until first operator rearm.
