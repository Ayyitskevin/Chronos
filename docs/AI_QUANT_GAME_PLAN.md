# CHRONOS AI QUANT GAME PLAN

Owner directive (2026-07-18): *"Make this an AI trading bot that has knowledge of all the
Pine scripts and strategies I have a library for, as a reference. Continuously build this
project so it can be a competitive AI quant."*

This document is the umbrella roadmap for that vision. It does not replace
`docs/LIVE_WHEEL_GAME_PLAN.md` — that plan remains governing for Milestones 7, 7C, and 8,
which are absorbed here as Phase A. Everything after Phase A is new scope, planned here.

---

## 0. What "competitive AI quant" honestly means for Chronos

Chronos will not out-compete institutional market makers on speed, flow, or infrastructure,
and this plan does not pretend otherwise. A solo operator with an AI co-pilot competes on a
different stack, every layer of which is buildable here:

1. **Process discipline.** Most non-institutional accounts lose to process failure —
   overtrading, revenge trades, unmodeled costs, silent overfitting. Chronos has already
   engineered much of this away (fail-closed risk, frozen selection criteria, kill
   switches, honest measurement). Discipline is the moat; everything else stands on it.
2. **Structural premia, not predictions.** The committed strategy set *targets* documented,
   capacity-tolerant sources of return: the volatility risk premium (the Wheel) and
   regime-conditioned trend, with short-horizon mean reversion held as a re-test hypothesis
   (`docs/STRATEGY_SELECTION.md`). None require winning a race — and none is yet validated
   in-house (zero candidates selected). The research factory exists to test these premia
   under frozen criteria, not to presume them.
3. **Breadth through automation.** One human cannot continuously watch the dozens of
   strategy–symbol–regime combinations the factory may eventually validate across three
   asset families. A machine that scans, sizes, and journals everything — and only
   escalates decisions — can. (Today that breadth is prospective: the corpus holds ~4
   distinct executable systems and zero validated strategies; breadth is an *output* of
   the factory, not a present asset.)
4. **Research velocity with anti-overfitting rails.** AI makes hypothesis generation cheap.
   Cheap hypotheses are dangerous without multiple-testing controls, so the research
   factory (Phase C) pairs AI-speed iteration with tooling-enforced holdout discipline —
   the discipline is in code, not in memory, because human honesty already failed once
   (the consumed QQQ holdout, disclosed in `docs/RESEARCH_REPORT.md` §C6).
5. **Survival.** The first job of the bot is to never blow up the account. Every autonomy
   rung in this plan is gated so that the maximum cost of being wrong is bounded and known
   in advance.

**Capital reality (verified 2026-07-17):** the IBKR account holds ~USD 110 cash. Options
wheeling requires funding (a cash-secured put on even a $20 underlying reserves ~$2,000);
stock and crypto in small size are the executable families today. This plan is deliberately
**capability-first**: build the machine now so that capital, when added, drops into a
proven system rather than an experiment. Until the account is funded, any live order is a
pipeline-acceptance test, not trading — and milestones state their capital thresholds so
no session operationalizes theater at sizes where costs dominate.

**The AI boundary (REPLACED 2026-07-25 — ADR-0016 / D-16).** This plan was written under
D-11 (*no generative model output feeds any runtime decision*), and described an
advisory-only copilot plane. **That boundary is superseded.** An owner directive re-scoped
Chronos as a fully autonomous, model-driven system: an approved model may originate runtime
trading decisions, but only through a typed `AITradeDecision` and the single deterministic
ModelDecisionGateway, inside an active owner-authored AutonomyMandate. The model cannot
access IBKR directly, change its authorization, weaken policy, or bypass any deterministic
gate, and the deterministic kernel retains unconditional veto authority.

What survives from the old framing: deterministic, tested code still evaluates every gate
and still owns the single transmit site, and the system stays auditable because every
decision is typed, provenance-stamped, and hash-chained. What changes: the model is now a
decision *originator*, not merely an advisor. Read the P5/P6 pillars and the Phase D/E
milestones below with that correction in mind — where they say "advisory," "never
transmits," or "human-confirmed," ADR-0016 governs. The rungs, gates, and frozen-criteria
discipline they describe are retained.

---

## 1. Where we are (evidence, not aspiration)

Verified by repo survey 2026-07-18:

**Assets in hand**
- 42-script Pine corpus, SHA-256-pinned, byte-exact from Notion, forensically audited
  per-script (`docs/PINE_AUDIT.md`, 1,792 lines; `research/pine_findings.json`). Verdicts:
  28 non-executable indicators (by design), 13 pass-with-constraints, 1 requires-rewrite.
  Family analysis: one "5T Pine Suite" with ~4 genuinely distinct executable systems.
- Two canonical strategy ports with spec-level parity tests (`regime_trend_v1`,
  `mean_reversion_v1`), both honestly `research_prototype` — zero strategies passed the
  frozen selection criteria (binding failure: C4's ≥20-closed-trade floor; max 18).
- A live-capable, human-confirmed order pipeline (`chronos.orders`) through M6: single
  transmit boundary, seven submission gates, ten-gate live stack, arming, durable kill
  switch, session-drawdown breaker — all fail-closed, all adversarially reviewed.
- A deterministic backtest engine that routes through the production decision path, with
  chaos/fault injection and reproducibility manifests.
- Institutional-grade process artifacts: frozen criteria, provenance manifests, two
  adversarial review rounds, hash-chained audit log.

**Load-bearing gaps (each maps to a phase below)**
- **No strategy knowledge base.** Corpus metadata is scattered across five artifacts
  (registry YAML, findings JSON, specs, selection manifest, results JSONs) with free-text
  values; a machine cannot answer "all daily-bar long regime strategies validated on ETFs."
- **No historical market-data pipeline.** The IBKR adapter has no historical-bars
  capability; research runs on a frozen, heterogeneous 5-ETF CSV corpus (SPY ends 2019).
  `docs/RESEARCH_REPORT.md` itself names IBKR historical data as the single
  highest-value research input.
- **No walk-forward loop, no statistical multiple-testing controls in code, no experiment
  registry.** Freeze discipline currently depends on git ordering and human honesty.
- **The Wheel is unbacktestable.** The engine models no options mechanics (no chains, no
  assignment, no expiration) — the flagship forward strategy has only point-in-time
  scenario math.
- **No bridge between planes.** Strategies emit into the dormant autonomous plane;
  the live-capable pipeline has no strategy feed. No portfolio allocator, no P&L
  attribution, no tracking-error measurement, log-only notifications.
- **Live transmission not yet wired** (M7), crypto family disabled (M7C), hardening/docs
  drift outstanding (M8).

---

## 2. Target architecture — six pillars

```
              ┌────────────────────────────────────────────────┐
              │  P5 AI DECISION PLANE (reads evidence; emits    │
              │  typed AITradeDecision into a durable queue —   │
              │  never transmits, holds no broker object; also  │
              │  briefs/theses/attribution; ADR-0016 boundary)  │
              └────────┬───────────────────────────┬───────────┘
                       │ reads                     │ reads
   ┌───────────────────▼────────┐      ┌───────────▼───────────────────┐
   │ P1 STRATEGY KNOWLEDGE BASE │      │ P4 OPERATIONS LEDGER          │
   │ 42 scripts + specs + audit │      │ orders, fills, P&L attribution,│
   │ + validation history, one  │      │ tracking error, strategy health│
   │ queryable store            │      └───────────▲───────────────────┘
   └───────────▲────────────────┘                  │ writes
               │ informs                ┌──────────┴────────────────────┐
   ┌───────────┴────────────────┐      │ P3 EXECUTION PLANE (built/M7) │
   │ P2 RESEARCH FACTORY        │      │ single transmit site, gates,  │
   │ data plane (IBKR history), │      │ arming, kill switch, breaker  │
   │ walk-forward, purged CV,   │      └──────────▲────────────────────┘
   │ experiment registry, wheel │                 │ compiled intents (all gates;
   │ simulator                  │                 │ authorized by AutonomyMandate)
   │                            │      ┌──────────┴────────────────────┐
   └───────────┬────────────────┘      │ P6 AUTONOMY LADDER (capstone) │
               │ promotes (frozen      │ scheduler → decision → gateway│
               └──── criteria only) ──▶│ graduated shadow/paper/canary │
                                       └───────────────────────────────┘
```

- **P1 — Strategy Knowledge Base (SKB).** The Pine library becomes first-class,
  machine-queryable knowledge: one schema-validated store joining source hash, audit
  verdict, structured tags (family, direction, timeframe, asset class, regime), spec
  linkage, validation history, and machine-readable disposition
  (ported / deferred / blocked-on-X / rejected). This is what "the AI knows my whole
  library" concretely means.
- **P2 — Research factory.** Trusted data (IBKR historical, uniformly adjusted, fresh
  embargoed holdouts), walk-forward evaluation, purged CV, bootstrap confidence intervals,
  deflated-Sharpe/multiple-testing controls, an experiment registry that mediates holdout
  access in tooling, and — the hard new capability — a Wheel/options simulator with
  disclosed model risk.
- **P3 — Execution plane.** Already built through M6; Phase A finishes live capability
  (M7/M7C/M8). Afterward: execution-quality measurement (fill vs mid, slippage journal).
- **P4 — Operations ledger.** Per-strategy/per-family P&L attribution, live-vs-backtest
  tracking error, strategy health states, and real notifications (push/email on halt,
  kill-switch, drawdown, fills).
- **P5 — AI decision plane** (was "AI copilot — advisory"; re-scoped by ADR-0016). A
  separate plane that reads the SKB, EvidenceBundles, and the operations ledger and writes:
  typed `AITradeDecision` records into a durable decision queue, plus theses, morning
  briefs, post-trade attribution narratives, and anomaly explanations. It remains
  structurally incapable of *transmitting* — it holds no broker object, no credentials, and
  no low-level order functions, and runs outside the broker-writing process, enforced by the
  same AST/import-isolation test pattern that guards the UI (`tests/safety/test_autonomy_contracts.py`).
  Its decisions reach a broker only by surviving the deterministic gateway and the existing
  `chronos.orders` gate chain.
- **P6 — Autonomy ladder.** Now governed by ADR-0016 §7: per-asset-family promotion along
  BACKTEST → REPLAY → SHADOW → PAPER_AUTONOMOUS → CANARY_LIVE_AUTONOMOUS →
  CAPPED_LIVE_AUTONOMOUS, each rung a separate owner decision, each requiring
  frozen-before-evaluation criteria, each capped by the existing kill-switch/drawdown
  machinery. A stock promotion authorizes neither futures nor options. No family skips
  rungs, and a material change to model, prompt, tool schema, decision schema, contract
  resolver, risk policy, or order compiler returns that configuration to SHADOW or PAPER.
  The standing-authorization model E3a reserved is delivered as the AutonomyMandate;
  unattended operation is never a configuration change.

---

## 3. Phases and milestones

Sizes: S ≈ one session, M ≈ one-to-two sessions, L ≈ multi-session. Every milestone ends
with the standard report and an explicit owner go/no-go before the next begins.

### Phase A — Finish committed live capability (governing doc: LIVE_WHEEL_GAME_PLAN)

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| A1 | **M7 — Live execution capability, validated without trading** | L | `transmit=True` for the LIVE branch at the single submission boundary only; live order object from qualified contract / valid tick / confirmed account / DAY limit; full ten-gate walk validated with a recording spy broker (wrong-account, arming-expiry, confirmation-mismatch, kill-switch-interruption cases); **no order reaches any venue in dev/test/CI** |
| A2 | **M7C — Crypto family** | M | Spot-only, fractional Decimal quantities, venue min-size validation, family session calendar, `CRYPTO_ALLOWLIST` default-empty; demo fixtures + spy validation (IBKR paper does not support crypto — disclosed) |
| A3 | **M8 — Hardening, chaos, docs, PR** | M | Wheel-path chaos tests; README rewrite (fixes the stale "live hard-disabled" posture text); `docs/limitations.md`; retire the legacy in-process Streamlit app (superseded by the thin-client UI, slated for removal since M5); full-suite soak; adversarial self-review; PR |

### Phase B — Strategy Knowledge Base (the library becomes queryable)

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| B1 | **SKB schema + compiler** | M | Pydantic schema (controlled vocabularies for family/direction/timeframe/asset-class/regime-tag/disposition); compiler joins `strategy_registry.yaml` + `pine_findings.json` + `specs/*.yaml` + selection manifest + results JSONs into one validated store (`research/skb/`); fails closed on unjoinable records; regenerated deterministically, hash-pinned to corpus |
| B2 | **SKB query surface + backfill** | M | Query API + CLI (`chronos skb query ...`) answering structured questions ("executable daily long strategies not yet validated", "everything blocked on intraday data"); per-script disposition backfilled for all 42 (ported / deferred / blocked-on / rejected, each with a machine-readable reason); generated human docs; tests |

### Phase C — Data plane + research factory

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| C0 | **Options chain/IV forward capture — deploy ASAP** | S | Scheduled snapshot capture of option chains/IV/greeks for allowlisted underlyings into the local store with provenance manifests. Deployed as early as the two-process topology (C1) allows — IBKR provides **no historical data for expired options**, so every week of delay is unrecoverable history. $0-tier capture is delayed/EOD-snapshot quality (real-time OPRA is a paid subscription IBKR has historically gated on account minimums); staleness is recorded in the manifests, not hidden |
| C1 | **IBKR historical bar pipeline** | L | `reqHistoricalData` runs in a **separate read-only data process** with its own gateway client id — it never holds the writer lease and never imports `chronos.orders` (enforced by the same AST + subprocess import-isolation tests that guard the UI); pacing-compliant backfill coordinated with the trading backend; the store keeps **unadjusted bars plus a corporate-action/dividend event stream**, deriving adjusted/total-return views at read time — never incrementally appending to an adjusted series (retroactive re-adjustment would silently break hash-pinned provenance); data-quality gate reuse; **fresh holdout windows declared and embargoed in tooling before any strategy sees the data** |
| C2 | **Experiment registry + holdout guardian** | M | Every research run recorded (config hash, data hashes, criteria version, stage, git commit); the multiple-testing trial count is **derived automatically from the registry** — every walk-forward inner-loop parameter configuration and every AI-drafted proposal that touched data counts as a trial (self-reported N is theater); holdout reads mediated by the registry — consuming a holdout requires an explicit, logged, once-only unlock **typed by the owner; no scheduled job, proposal-execution path, or copilot artifact can invoke it**; a holdout budget policy rations unlocks against newly accrued data; the M5 review's "burned holdout" failure class becomes structurally impossible |
| C3 | **Walk-forward + statistics upgrade** | L | Rolling-window walk-forward loop in code with fixed reporting rules: what may be labeled out-of-sample is defined up front, and per-window re-optimization counts each configuration as a registry trial. Statistics scoped to what the samples support: bar-level stationary/block bootstrap CIs (not IID trade-level resampling on autocorrelated series); deflated Sharpe with registry-derived trial counts; purged/embargoed CV applied **only** to fitted-model/parameter-search workflows (fixed-rule replays have nothing to purge); low-sample verdicts stay blocking. Stated plainly: at current trade frequencies this upgrade **formalizes rejection rather than enabling validation** — the binding fix is longer uniform history (C1) and/or higher-frequency strategy families |
| C4 | **Re-validation campaign** | M | Preceded by **pre-registered power arithmetic** (expected trades per candidate per clean window; earliest arithmetically-possible pass date) and a **contamination map** of seen/burned/clean symbol-windows — QQQ is burned through 2024-01 and 2018–2021 is seen on all five symbols; the pooling question (whether the ≥20-trade floor may aggregate across the uniform panel) is decided at criteria re-freeze time, before results exist. If the arithmetic shows a pass is impossible in the available clean windows, C4 re-scopes to contamination mapping and **spends no holdout unlocks**. Otherwise: the two reserved hypotheses re-run on trusted uniform data under re-frozen criteria with fresh holdouts; next-tier SKB candidates triaged; outcome recorded honestly — zero-selected remains an acceptable answer |
| C5 | **Wheel/options research capability** | L | Wheel lifecycle simulator (CSP → assignment → covered call → called away) on underlying bars plus captured (C0) and estimated premium surfaces, with estimate-vs-capture model risk quantified and disclosed; cost/assignment stress grids. Honesty bound stated in the deliverable itself: with no expired-options backfill available, captured surfaces accrue at calendar speed and will span few volatility regimes for years — C5 therefore delivers **stress-grid analysis and model-risk measurement, not frozen-criteria Wheel validation**; validation requires either the paid-data decision (N2) or an explicitly accepted multi-year capture horizon |

### Phase D — Operations ledger + AI copilot

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| D1 | **P&L attribution + tracking error** | M | Per-strategy/per-family realized+unrealized P&L pipeline from broker truth; live/paper vs backtest tracking-error monitor; strategy health states (healthy / degraded / halted) persisted and displayed |
| D2 | **Notifications** | S | Push/email on kill-switch engagement, drawdown trip, halt, fill, reconciliation anomaly (existing notifier protocol, real channels) |
| D3 | **AI copilot v1 (advisory plane)** | L | Morning brief (positions, risk state, regime context, calendar); trade theses citing SKB entries + current risk evidence; post-trade attribution narratives; anomaly explanations. Structural guarantees, both required: (a) the copilot package cannot import order/submission/broker-write modules (AST + subprocess tests, same pattern as UI isolation); (b) **data-flow isolation** — copilot output is written only to a designated advisory store, and a test asserts no runtime module (`chronos.orders`, the allocator, the scheduler, strategy-health logic) reads from that store. **Superseded framing:** under ADR-0016 this plane also emits typed `AITradeDecision` records into the durable decision queue; the import- and data-flow isolation above is retained, and the decision queue is the *only* channel into runtime — narrative output stays advisory and is never parsed into an order |
| D4 | **AI research assistant** | M | Copilot drafts research proposals (hypothesis, SKB lineage, test spec) as structured documents; the experiment registry executes a proposal **only after explicit owner review**, treats it identically to a human-authored one, and counts it in trial accounting; proposals can never trigger a holdout unlock and never shortcut the frozen-criteria pipeline |

### Phase E — Portfolio layer + autonomy ladder (each rung an explicit owner decision)

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| E1 | **Portfolio allocator** | L | Cross-strategy/cross-family capital allocation with per-symbol/per-family caps, position netting, conflict resolution; outputs *suggested* intents into the existing human-confirmed pipeline; account equity/positions from broker truth. Capital threshold: designed now, **dormant below ~$10k deployed** — below that, multi-sleeve splitting multiplies IBKR's $1/order minimum into a prohibitive per-sleeve cost floor, so the allocator's useful outputs are netting and conflict checks only |
| E2 | **Scheduler + proposed-intent queue (semi-auto)** | M | Scheduled evaluation on fresh data; qualifying strategies enqueue fully-built proposed intents; the owner reviews/confirms in the dashboard — the confirmation and transmit path is byte-identical to manual flow |
| E3a | **Standing-authorization redesign (safety-critical scope)** — **DELIVERED as ADR-0016 / the AutonomyMandate (M1, 2026-07-25)** | M | The reserved envelope is now specified and typed: `chronos.autonomy.AutonomyMandate` — owner-authored, versioned, expiring (live ≤ 30 days), revocable, deny-by-default, scoped by account fingerprint, mode, promotion level, instruments, strategies, order forms, capital, exposure, loss, concentration, activity, sessions, and market-data floors; kill-switch precedence absolute. Replaces per-order confirmation and session arming **only** inside its bounds. No unattended order exists until the M2 gateway ships and the per-family promotion gates pass |
| E3 | **Bounded paper autonomy** | L | New reviewed release per `docs/GO_LIVE_CHECKLIST.md` doctrine, implementing E3a's authorization model: unattended paper-only operation for promoted strategies inside the pre-authorized envelope; every other safety layer active and unchanged; soak period with published tracking-error results |
| E4 | **Live canary autonomy** | L | **Its own future reviewed release with a fresh independent adversarial review** (Gate-4 doctrine, `docs/GO_LIVE_CHECKLIST.md`). Only for strategies that cleared frozen criteria AND the E3 paper soak; minimum-size live operation under graduated caps, running through the `chronos.orders` plane — the autonomous plane's CANARY_LIVE/LIVE hard denial (`control/modes.py`, `control/promotion.py`, ADR-0007) **stays untouched**; equivalent promotion machinery (versioned promotion records, single-step rung progression, gates written before the run) is built for the orders plane as part of this milestone; kill-switch/drawdown/halt machinery unchanged; size expansion is a per-strategy owner decision informed by D1 evidence |

Phases B and C can interleave with Phase A if the owner prefers research progress while
M7's owner-gated items (ibapi install, gateway verification) are pending — B1/B2 touch no
execution code, and C0 should deploy as early as the C1 two-process topology allows,
because captured options history is unrecoverable. Default order is as listed.

---

## 4. Invariants that survive every phase (verbatim commitments)

1. No order placed by any test/CI/dev workflow, ever.
2. Exactly one reachable `transmit=True` site (`chronos.orders.submission`); no other
   module may assign or override the final transmit state — including everything built by
   this plan.
3. Market orders are impossible; puts are cash-secured; naked calls can never be enabled
   through configuration. No phase of this plan changes this; any future revisit requires
   an explicit owner directive plus a reviewed release — this document grants neither.
4. ~~No generative model output feeds any runtime **decision** (ADR-0004 / D-11, verbatim).~~
   **REPLACED by ADR-0016 / D-16.** An approved model may originate runtime trading
   decisions **only** through a typed `AITradeDecision` and the single deterministic
   ModelDecisionGateway, inside an active owner-authored AutonomyMandate. The model gets no
   IBKR client, no credentials, no low-level order functions, no direct submission-module
   imports, and no policy-, arming-, or mandate-writing tools; it runs outside the
   broker-writing process. Free-form chat, theses, summaries, and Markdown are never parsed
   into orders. The deterministic kernel keeps unconditional veto authority, and an AI
   failure never becomes permission to trade.
5. All safety machinery (mode lock, arming, per-order confirmation, kill switch,
   drawdown breaker, writer lease, halt) applies unchanged **except** that an active
   AutonomyMandate replaces per-order human confirmation and session arming inside its
   bounds — the substitution ADR-0016 §1 makes, and the concrete form of the envelope E3a
   reserved. **That is the only gate autonomy replaces.** The kill switch, drawdown
   breaker, mode lock, writer lease, halt, single transmit boundary, idempotency,
   reconciliation-to-broker-truth, contract qualification, and stale-data rejection apply
   identically at every rung and are enumerated in ADR-0016 §8 as explicitly
   not-superseded. An AI failure never becomes permission to trade.
6. Frozen-criteria promotion: no strategy reaches paper autonomy, let alone live, without
   passing criteria that were frozen before its results existed. "Zero selected, with
   better evidence" remains a valid outcome of any research phase.
7. Never log: IBKR credentials, 2FA codes, full account identifiers, raw live-arm phrases,
   raw confirmation phrases. Localhost-only backend. No secrets or account numbers in the
   repo.
8. No unofficial `ibapi` package in requirements; owner installs the official TWS API.
9. Honest reporting: every milestone report states what was NOT done and what cannot be
   verified from this environment.

---

## 5. Open owner decisions (carried + new)

Carried from LIVE_WHEEL_GAME_PLAN §7 (still open): IB_ACCOUNT_ALLOWLIST values; live symbol
allowlist (default AAPL/MSFT/SPY); crypto eligibility + CRYPTO_ALLOWLIST (suggested
BTC/ETH) + acceptance that crypto cannot be paper-validated; ib_async adapter keep/retire;
stocks whole-share/limit-DAY/long-only confirmation; TradingView parity exports (or
explicit acceptance of spec-level parity); ibapi install + gateway verification for M7.

New for this plan:
- **N1. Funding intent.** The plan is capability-first regardless, but knowing the target
  account size (and when) lets C4/C5 prioritize the strategy families you'll actually run.
- **N2. Data budget.** IBKR historical *bar* data is free within pacing limits for held
  account types, but IBKR provides **no historical data for expired options** — there is
  no backfill path at any spend level through IBKR. The plan assumes $0 data spend and
  builds forward capture (C0) instead, accepting that $0-tier capture is delayed/EOD
  quality (real-time OPRA is a paid subscription IBKR has historically gated on account
  minimums). Consequence stated plainly: meaningful frozen-criteria Wheel validation
  requires either a paid options-data vendor (explicit future owner decision) or an
  accepted multi-year capture horizon.
- **N3. Copilot model + spend.** D3/D4 run an LLM on schedule; owner picks the
  model/frequency/budget envelope. All copilot output is stored locally.
- **N4. Autonomy appetite.** E2 (semi-auto proposals) vs E3/E4 (unattended) are separate
  opt-ins; nothing in Phases A-D commits you to any of them.

---

## 6. Working protocol (unchanged)

Milestone-by-milestone: build → gates (ruff, mypy --strict, pytest) → adversarial review
for safety-relevant milestones — explicitly: **A1, A2, C2, D3, E1, E2, E3a, E3, E4**
(anything that constructs intents, mediates holdouts, authors operator-facing trade
rationale, or changes authorization; the implementing session may add to this list but
never remove from it) → report (Completed / Files changed / Commands+results / Known
limitations / Safety status / Proposed next) → **explicit owner go-ahead** → next.
All work on the designated branch; PR per milestone into `feat/wheel-dashboard-mvp`.

---

## 7. Review record

2026-07-18: this plan was adversarially reviewed before adoption by two independent
reviewers (quantitative-research honesty; safety-invariant consistency — a third,
facts/sequencing, was verified inline after an infrastructure failure). Confirmed
findings, all remediated in this text: the original invariant 5 was logically impossible
for unattended rungs (fixed via E3a); E4 lacked the reviewed-release doctrine; the AI
boundary had been silently narrowed from D-11's "any runtime decision" (restored, with
data-flow isolation added); an "in the MVP" qualifier weakened the naked-call invariant
(struck); C4's power arithmetic and contamination map were missing; C0 was split out of
C5 because expired-options history is unrecoverable; C3's statistics were rescoped to
what n<30 trade samples support. Known soft spots a future reviewer should re-attack:
the E3a authorization design (deliberately unspecified here), the C5 premium-estimation
model risk, and any drift between this plan and LIVE_WHEEL_GAME_PLAN §7 owner decisions.
