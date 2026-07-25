# QQQ / GOLD / SPY Capability Brief — instructions for Opus-class build sessions

Owner directive (2026-07-19): reviewed `ZiadFrancis/GPT_5.6_Sol_Trading_Agent` for
inspiration; make Chronos "just as capable," exclusively trading QQQ, Gold, and SPY.

This brief is the standing instruction set for an Opus-class (or stronger) session
executing that directive. It EXTENDS the adopted roadmaps — `docs/AI_QUANT_GAME_PLAN.md`
(phases B–E) and `docs/LIVE_WHEEL_GAME_PLAN.md` — it does not fork them. Every locked
invariant applies verbatim (AI_QUANT_GAME_PLAN §4) — **except** that ADR-0004 §5 / D-11
("no generative model output feeds any runtime decision") was **superseded on 2026-07-25
by ADR-0016 / D-16**. The governing rule is now: an approved model may originate runtime
trading decisions only through a typed `AITradeDecision` and the single deterministic
ModelDecisionGateway, under an owner AutonomyMandate, and it cannot access IBKR directly,
change its authorization, weaken policy, or bypass any deterministic gate. The research
harness described below stays strictly research-side regardless (see §3.1).

## 1. What the reviewed repo actually is (verified from source, not its README)

A **backtest-only research harness** in which an LLM makes bar-by-bar trading
decisions over 1-hour candles through a ReAct tool loop (get_price / get_indicators /
check_position → one `place_order` call: buy|sell|close|hold, with ATR-multiple
stops/targets and a size fraction). It has **no live execution of any kind**. Its
genuinely good ideas:

1. **Five explicit anti-lookahead guards** (visible-slice indicators, completed-day
   resampling, next-bar-open fills with pessimistic gap-through-stop handling, fees +
   slippage modeled, decision-time-only feature computation). Chronos's engine
   already matches this discipline (ADR-0005/0006) — parity, not a gap.
2. **Anonymization mode** — prices rebased to ~100, dates stripped, ticker hidden —
   controlling for the one confound unique to LLM strategies: the model may have
   MEMORIZED the actual price history it is being "tested" on. This is a real
   methodological contribution and the single most important thing to copy.
3. **Deterministic LLM caching** — each decision keyed by hash(model, prompt, feature
   window, position state, equity); identical inputs replay from disk. Reproducible,
   cheap, and exactly aligned with our C2 experiment-registry doctrine.
4. **Baseline control agents** — a deterministic EMA-crossover MockAgent and a seeded
   RandomAgent run through the identical harness. (Chronos already mandates three
   baselines including a random-entry twin — parity.)
5. **Scale-free features** (returns %, RSI, EMA distances %, ATR %, Donchian
   position) and per-run artifacts (decisions.jsonl with reasoning + token usage,
   trades.csv, equity-vs-buy-and-hold, HTML report).

What it does NOT have — and Chronos already does: any execution capability, risk
engine, kill switch/arming/drawdown stack, reconciliation against broker truth,
persistence/audit, frozen-criteria promotion, or multi-family support. "As capable"
therefore means **adding its research capability to our execution platform**, not
copying its architecture.

## 2. The universe decision

- **SPY, QQQ** — already first-class (stock family; SPY is on the default allowlist;
  both are the re-test hypotheses universe from `docs/RESEARCH_REPORT.md`).
- **"Gold" = GLD** (SPDR Gold Shares ETF), traded through the existing STOCK family.
  Spot XAU/USD and gold futures are explicitly out of scope: Chronos has no futures
  or CFD support, and GLD keeps gold inside the validated stock pipeline. If the
  owner later wants futures, that is a new game-plan milestone, not a symbol tweak.
- Research allowlist for this program: `("SPY", "QQQ", "GLD")`. Live symbol
  allowlist remains an owner decision at deploy time.

## 3. What to build (in order) — each item maps into the adopted plan

### 3.1 `llm_lab`: an LLM-decision research harness inside the research plane (Phase C/D)

A new `chronos.research.llm_lab` package, patterned on the reviewed agent but built
on OUR engine and doctrine:

- Drives the EXISTING deterministic backtest path (proposal → sizer → risk →
  simulated broker, next-bar fills) — the LLM plays the role of a strategy,
  emitting the same `StrategyProposal` vocabulary deterministic strategies emit.
  One decision per completed bar window; ATR-multiple stops map to the existing
  protective-stop model.
- **Anonymization mode is mandatory for any evaluative run**: rebase prices to 100,
  strip dates/ticker (the memorization control). Runs without anonymization are
  labeled `exploratory`, never eligible for selection.
- **Deterministic decision cache** keyed exactly as the reviewed repo does; cache
  files and the run manifest (model id, prompt hash, feature-window hash, token
  spend) are experiment-registry artifacts (C2), and every LLM decision counts in
  trial accounting.
- Baselines run in the same harness: the existing SMA/random baselines plus an
  EMA20/50 mock agent for direct comparability with the reviewed repo.
- Features: start with their scale-free set (returns, RSI(14), EMA20/50/200
  distance %, ATR%, Donchian-20 position, daily context) computed by
  `chronos.indicators` — spec'd, not ad-hoc.
- Artifacts per run: decisions.jsonl (reasoning + usage), trades/equity CSVs,
  equity-vs-buy-and-hold, and a report page — stored under `research/results/` with
  the standard reproducibility manifest.

**The boundary line, restated for this harness (updated 2026-07-25, ADR-0016/D-16):**
the LLM *in this research harness* is a RESEARCH subject — its outputs are backtest
artifacts and nothing it emits here reaches the runtime order path. That separation
is unchanged and still worth keeping: a backtest harness is not an execution path.

What has changed is the last sentence of the original text. "Runtime LLM
auto-transmission is not on any roadmap and requires an explicit owner directive
plus a reviewed release to even discuss" — **the owner gave that directive on
2026-07-25.** Runtime model-originated decisions are now the mission, via a typed
`AITradeDecision` through the single deterministic ModelDecisionGateway under an
AutonomyMandate (ADR-0016). That path is deliberately **separate from this harness**:
a promotable LLM-derived strategy still travels the frozen-criteria pipeline, and
what reaches live operation is either a distilled deterministic rule set (spec'd in
`specs/`, parity-tested) or decisions admitted through the gateway — never raw
harness output.

### 3.2 Hourly bars for three symbols (extends C1)

The reviewed agent's 1H cadence is part of its capability. Chronos's validated
interval is daily-only. Extend the C1 data plane (IBKR historical bars) to fetch
and store **1-hour bars for SPY/QQQ/GLD only**, with the same provenance manifests,
quality gates, and holdout embargo. Intraday validation of the bar vocabulary
(`BarInterval` 1h is declared but unvalidated — ASSUMPTIONS A-31) becomes a named
deliverable with its own tests. Daily remains the default for everything else.

### 3.3 Focused research campaign (C4, re-scoped to this universe)

Run the C4 re-validation on SPY/QQQ/GLD once C1 data lands: regime_trend_v1 and
mean_reversion_v1 on daily bars; the llm_lab agents on 1H; all under re-frozen
criteria with the power arithmetic and contamination map C4 already mandates.
"Zero selected, with better evidence" remains a valid outcome.

### 3.4 Live focus (no new code)

Live trading of this universe is ALREADY delivered for the stock family (M7):
SPY/QQQ/GLD are ordinary stock-family symbols behind the full gate stack. The only
change at deploy time is the owner setting `SYMBOL_ALLOWLIST=SPY,QQQ,GLD`.

## 4. Standing instructions for the executing session

1. Read `docs/AI_QUANT_GAME_PLAN.md` §4 (invariants) and §6 (protocol) first; this
   brief never overrides them. Milestone protocol applies: report + explicit owner
   go-ahead between milestones (unless the owner grants an autonomous mandate).
2. Design-panel before code for 3.1 and 3.2 (the ADR-0009/0010 pattern: a short ADR,
   three adversarial judges, remediate, then build). Adversarial review before any
   PR leaves draft.
3. Gates at every commit: ruff, ruff format, mypy --strict, full pytest.
4. No new third-party dependencies without an explicit owner decision (the LLM
   client for llm_lab is the Anthropic SDK, already the platform convention; its
   API key is an owner-supplied secret, never committed, never logged).
5. Honesty rules: llm_lab results are research evidence, never trading signals;
   every run manifest discloses anonymization state, cache hit rate, and token
   spend; no performance claim without the baseline comparison attached.
6. Token/cost discipline: cached replays are free; fresh evaluative runs state
   their expected LLM spend in the milestone report before running.

## 5. Recommendations (owner asked)

1. **Adopt 3.1 + 3.2 as the next research milestones after M7C/M8 close** — they
   slot cleanly into Phase C and directly serve the "AI quant on QQQ/Gold/SPY"
   goal. Doing them before M8 hardening is possible but I recommend finishing the
   committed live-capability arc first.
2. **Steal the anonymization idea wholesale** — it is the one methodological piece
   we did not already have, and it matters for ANY future LLM-signal research.
3. **Do not copy the 95%-equity-per-trade sizing or the LLM-in-the-loop execution**
   — the first is account-destroying sizing for a real account, the second violates
   the platform's core safety boundary. Their harness is a good lab instrument;
   ours must stay a lab instrument attached to a hardened executor.
4. **Gold = GLD unless you explicitly want futures** — say the word and futures
   support gets scoped as its own milestone (it is substantial: new family,
   margin model, session calendar, data).
5. When llm_lab exists, point the D3/D4 copilot at its artifacts: "why did the
   agent trade here" narratives over decisions.jsonl are cheap and useful.
