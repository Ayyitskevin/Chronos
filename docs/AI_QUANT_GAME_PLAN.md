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
2. **Structural premia, not predictions.** The committed strategy set harvests documented,
   capacity-tolerant sources of return: the volatility risk premium (the Wheel),
   regime-conditioned trend, and short-horizon mean reversion. None require winning a race.
3. **Breadth through automation.** One human cannot watch 40 strategies across three asset
   families. A machine that scans, sizes, and journals everything — and only escalates
   decisions — can.
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
proven system rather than an experiment.

**The AI boundary (unchanged, load-bearing):** ADR-0004 / DECISIONS.md D-11 — *no
generative model output feeds any runtime order decision.* The AI layer reads state and
knowledge, writes analysis, proposals, and explanations; deterministic, tested code
evaluates every gate and owns the single transmit site. "AI quant" here means
AI-accelerated research and AI-explained operations around a deterministic executor —
which is also how it stays auditable.

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
              │  P5 AI COPILOT (advisory plane — reads, never   │
              │  transmits: briefs, theses, attribution,        │
              │  research proposals; ADR-0004 boundary)         │
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
   │ experiment registry, wheel │                 │ human-confirmed intents
   │ simulator                  │      ┌──────────┴────────────────────┐
   └───────────┬────────────────┘      │ P6 AUTONOMY LADDER (capstone) │
               │ promotes (frozen      │ scheduler → proposed intents →│
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
- **P5 — AI copilot.** A separate advisory plane that reads the SKB and operations ledger
  and writes: morning briefs, trade theses that cite SKB entries and risk state, post-trade
  attribution narratives, anomaly explanations, and research proposals for the factory.
  Structurally incapable of transmitting: enforced by the same AST/import-isolation test
  pattern that already guards the UI.
- **P6 — Autonomy ladder.** The post-M7 seam reserved in the live plan, made explicit:
  scheduler-driven strategy evaluation → auto-*proposed* intents awaiting human
  confirmation → bounded unattended paper autonomy → tiny-size live canary — each rung a
  separate owner decision, each requiring frozen-criteria validation, each capped by the
  existing kill-switch/drawdown machinery. No strategy skips rungs.

---

## 3. Phases and milestones

Sizes: S ≈ one session, M ≈ one-to-two sessions, L ≈ multi-session. Every milestone ends
with the standard report and an explicit owner go/no-go before the next begins.

### Phase A — Finish committed live capability (governing doc: LIVE_WHEEL_GAME_PLAN)

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| A1 | **M7 — Live execution capability, validated without trading** | L | `transmit=True` for the LIVE branch at the single submission boundary only; live order object from qualified contract / valid tick / confirmed account / DAY limit; full ten-gate walk validated with a recording spy broker (wrong-account, arming-expiry, confirmation-mismatch, kill-switch-interruption cases); **no order reaches any venue in dev/test/CI** |
| A2 | **M7C — Crypto family** | M | Spot-only, fractional Decimal quantities, venue min-size validation, family session calendar, `CRYPTO_ALLOWLIST` default-empty; demo fixtures + spy validation (IBKR paper does not support crypto — disclosed) |
| A3 | **M8 — Hardening, chaos, docs, PR** | M | Wheel-path chaos tests; README rewrite (fixes the stale "live hard-disabled" posture text); `docs/limitations.md`; full-suite soak; adversarial self-review; PR |

### Phase B — Strategy Knowledge Base (the library becomes queryable)

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| B1 | **SKB schema + compiler** | M | Pydantic schema (controlled vocabularies for family/direction/timeframe/asset-class/regime-tag/disposition); compiler joins `strategy_registry.yaml` + `pine_findings.json` + `specs/*.yaml` + selection manifest + results JSONs into one validated store (`research/skb/`); fails closed on unjoinable records; regenerated deterministically, hash-pinned to corpus |
| B2 | **SKB query surface + backfill** | M | Query API + CLI (`chronos skb query ...`) answering structured questions ("executable daily long strategies not yet validated", "everything blocked on intraday data"); per-script disposition backfilled for all 42 (ported / deferred / blocked-on / rejected, each with a machine-readable reason); generated human docs; tests |

### Phase C — Data plane + research factory

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| C1 | **IBKR historical data pipeline** | L | `reqHistoricalData` in the read-only adapter path (no order surface touched); incremental local bar store with provenance manifests; uniform adjustment handling (total-return aware); pacing-compliant backfill; data-quality gate reuse; **fresh holdout windows declared and embargoed in tooling before any strategy sees the data** |
| C2 | **Experiment registry + holdout guardian** | M | Every research run recorded (config hash, data hashes, criteria version, stage, git commit); holdout reads mediated by the registry — consuming a holdout requires an explicit, logged, once-only unlock; the M5 review's "burned holdout" failure class becomes structurally impossible |
| C3 | **Walk-forward + statistics upgrade** | L | Rolling-window walk-forward loop in code; purged/embargoed CV; bootstrap CIs on all headline metrics; deflated Sharpe / multiple-testing adjustment reported alongside every result; low-sample verdicts stay blocking |
| C4 | **Re-validation campaign** | M | The two reserved hypotheses (regime_trend_v1 on liquid indices; mean_reversion_v1 on small-caps) re-run on trusted uniform data under re-frozen criteria with fresh holdouts; next-tier corpus candidates (from SKB) triaged; outcome recorded honestly — zero-selected remains an acceptable answer |
| C5 | **Wheel/options research capability** | L | Forward-building options dataset (scheduled chain/IV snapshot capture into the local store — history accrues from day one); Wheel lifecycle simulator (CSP → assignment → covered call → called away) on underlying bars + captured/estimated premium surfaces, with model risk quantified and disclosed; cost/assignment stress grids |

### Phase D — Operations ledger + AI copilot

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| D1 | **P&L attribution + tracking error** | M | Per-strategy/per-family realized+unrealized P&L pipeline from broker truth; live/paper vs backtest tracking-error monitor; strategy health states (healthy / degraded / halted) persisted and displayed |
| D2 | **Notifications** | S | Push/email on kill-switch engagement, drawdown trip, halt, fill, reconciliation anomaly (existing notifier protocol, real channels) |
| D3 | **AI copilot v1 (advisory plane)** | L | Morning brief (positions, risk state, regime context, calendar); trade theses citing SKB entries + current risk evidence; post-trade attribution narratives; anomaly explanations. Structural guarantees: copilot package cannot import order/submission/broker-write modules (AST + subprocess tests, same pattern as UI isolation); all output labeled advisory; **zero generative output parsed into any runtime decision** (ADR-0004 restated) |
| D4 | **AI research assistant** | M | Copilot drafts research proposals (hypothesis, SKB lineage, test spec) as structured documents the experiment registry can execute; proposals are inputs to the frozen-criteria pipeline, never shortcuts around it |

### Phase E — Portfolio layer + autonomy ladder (each rung an explicit owner decision)

| # | Milestone | Size | Definition of done |
|---|-----------|------|--------------------|
| E1 | **Portfolio allocator** | L | Cross-strategy/cross-family capital allocation with per-symbol/per-family caps, position netting, conflict resolution; outputs *suggested* intents into the existing human-confirmed pipeline; account equity/positions from broker truth |
| E2 | **Scheduler + proposed-intent queue (semi-auto)** | M | Scheduled evaluation on fresh data; qualifying strategies enqueue fully-built proposed intents; the owner reviews/confirms in the dashboard — the confirmation and transmit path is byte-identical to manual flow |
| E3 | **Bounded paper autonomy** | L | New reviewed release per `docs/GO_LIVE_CHECKLIST.md` doctrine: unattended paper-only operation for promoted strategies inside hard caps (orders/day, notional, loss); every safety layer active; soak period with published tracking-error results |
| E4 | **Live canary autonomy** | L | Only for strategies that cleared frozen criteria AND the paper soak; minimum-size live operation under graduated caps; kill-switch/drawdown/halt machinery unchanged; expansion of size is an owner decision per strategy, informed by D1 evidence |

Phases B and C can interleave with Phase A if the owner prefers research progress while
M7's owner-gated items (ibapi install, gateway verification) are pending — B1/B2 touch no
execution code. Default order is as listed.

---

## 4. Invariants that survive every phase (verbatim commitments)

1. No order placed by any test/CI/dev workflow, ever.
2. Exactly one reachable `transmit=True` site (`chronos.orders.submission`); no other
   module may assign or override the final transmit state — including everything built by
   this plan.
3. Market orders are impossible; puts are cash-secured; naked calls can never be enabled
   through configuration in the MVP.
4. No generative model output feeds any runtime order decision (ADR-0004). The copilot
   plane is read-and-advise only, enforced structurally by import-isolation tests.
5. All safety machinery (mode lock, arming, per-order confirmation, kill switch,
   drawdown breaker, writer lease, halt) applies to autonomous rungs exactly as to manual
   flow — autonomy changes who *proposes*, never what *gates*.
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
- **N2. Data budget.** IBKR historical data is free within pacing limits for held account
  types; if deeper options history is ever wanted, paid sources (e.g. an options-data
  vendor) are an explicit future decision — the plan assumes $0 data spend and builds
  forward-capture instead.
- **N3. Copilot model + spend.** D3/D4 run an LLM on schedule; owner picks the
  model/frequency/budget envelope. All copilot output is stored locally.
- **N4. Autonomy appetite.** E2 (semi-auto proposals) vs E3/E4 (unattended) are separate
  opt-ins; nothing in Phases A-D commits you to any of them.

---

## 6. Working protocol (unchanged)

Milestone-by-milestone: build → gates (ruff, mypy --strict, pytest) → adversarial review
for safety-relevant milestones → report (Completed / Files changed / Commands+results /
Known limitations / Safety status / Proposed next) → **explicit owner go-ahead** → next.
All work on the designated branch; PR per milestone into `feat/wheel-dashboard-mvp`.
