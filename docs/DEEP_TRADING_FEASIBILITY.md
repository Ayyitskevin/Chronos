# Deep-trading candidate feasibility — mapping awesome-deep-trading onto Chronos

Status: **DECISION REQUESTED (owner picks ≤ 2 candidates, or zero)**
Scope: research only. This document authorizes **no code, no data reads, no
campaign, no dependency change**. Its only output is an owner decision; the
first artifact after that decision is a preregistered hypotheses revision in
the [FIVE_TOOL_RESEARCH_HYPOTHESES.md](FIVE_TOOL_RESEARCH_HYPOTHESES.md)
pattern, and every gate that blocks the Five-Tool campaign blocks these
candidates identically.
Plan lineage: D1 of the deep-trading plan
(`shared/handoffs/2026-08-20_chronos-progress-review-claude.md`); slots under
[VISION_COMPLETION_PLAN.md](VISION_COMPLETION_PLAN.md) §8 Phase 3. PR #76
recorded this scope as deferred ("Not an LSTM/RL stack from
awesome-deep-trading"); this document replaces that deferral with an ordered
path, and the deferral stays in force until the owner picks.

## 1. What the list actually is

[`cbailes/awesome-deep-trading`](https://github.com/cbailes/awesome-deep-trading)
(© 2021, last substantive era 2016–2020) is a curated link list: papers
(CNNs, LSTMs, GANs, high-frequency, portfolio, reinforcement learning,
cryptocurrency, sentiment/behavioral), 2017–2019-era repositories, dataset
pointers, and courses. **It contains no indicator code.** "Implementing the
indicators from the starred repo" therefore means: select model *families*
from its taxonomy and build them inside Chronos's research governance —
there is nothing in the list to port.

## 2. Chronos's data reality (verified in-repo, 2026-08-21)

What `research/data/raw/` actually holds:

| File | Coverage | Bars | Fidelity | Adjustment |
|---|---|---|---|---|
| `SPY.csv` | 2000-01-03 → 2019-11-14 | daily | byte-exact | unadjusted only |
| `QQQ.csv` | 1999-11-01 → 2024-01-10 | daily | byte-exact | has `adj_close`; **2022-01 → 2024-01 is a burned holdout** |
| `GLD.csv` `IWM.csv` `TLT.csv` | 2019-01-02 → 2021-12-31 | daily, 757 rows each | markdown-transcribed, **rounded to 2 decimals**, not byte-exact | adjusted |

(Provenance: `research/data/raw/DATA_SOURCES.md` — honest about the
transport; `docs/limitations.md` — "In-repo IWM is 2019–2021 and adjusted,
not certified.")

Consequences, stated once and inherited by every row of §4:

- **No hourly bars exist anywhere.** No order book, no options surfaces, no
  point-in-time news/text corpus.
- **Adjustment policies are mixed across files** — a cross-sectional model
  trained on this corpus learns the transcription, not the market.
- The only long history is SPY/QQQ daily; the three-instrument overlap
  window is 2019–2021 only, at 2-decimal fidelity.
- QQQ's recent two years are consumed; SPY is companion-only in the SHADOW
  book (`docs/limitations.md`).

**Therefore: any DL result on the current corpus is noise by construction,
and Phase 3's certified IBKR export (6–10 liquid ETFs, daily + hourly,
2000 → present, fresh content-addressed holdout map) is a hard prerequisite
for every candidate below — it is the D2 gate, already the owner's action
item #1.** Nothing in this document weakens that ordering.

## 3. The feasibility test

A candidate family is *feasible* only if all five hold:

1. **Data:** its inputs exist in the D2 certified export (daily/hourly bars,
   exchange calendars). Anything needing order-book depth, tick data,
   point-in-time text, or options surfaces fails today.
2. **Governance fit:** it can run as trials through the brokered reader with
   multiplicity counting, preregistered thresholds, and an untouched holdout
   (Phase 3 §8 gates) — i.e., it is a supervised, replayable mapping from
   bars to signals, not an agent that owns decisions.
3. **Baseline pairing:** a deterministic counterpart exists in the same
   trial frame, so the net must beat something honest (Phase 3 requires
   comparison against deterministic baselines).
4. **Determinism:** pinned seeds, pinned versions via the hash-verified
   lockfile, content-addressed model weights (D-27's bytes-are-the-label
   applied to artifacts: a trained model's SHA-256 is its identity; an
   unpinned model cannot enter a trial).
5. **Authority:** it creates none. Signals reach autonomy only through the
   existing rails — registered proposer (D-24), evidence bundles (D-25),
   SHADOW first, mandate-gated — and only after a promotion artifact (D4,
   speculative).

## 4. Category-by-category verdict

| List category | Verdict | Why (concrete, not vibes) |
|---|---|---|
| **Deep momentum networks** (Lim/Zohren/Roberts 2019, "Enhancing Time Series Momentum Strategies Using Deep Neural Networks") | **FEASIBLE — recommended C1** | Daily bars suffice. Direct descendant of the momentum evidence Chronos already preregistered (H-5T-002 cites Moskowitz/Ooi/Pedersen; Moreira & Muir already motivates the vol-scaling arm). Natural deterministic baseline: classical TSMOM with volatility scaling. |
| **CNN/LSTM direction classifiers** (Sezer/Ozbayoglu 2018/2019; the `huseinzol05/Stock-Prediction-Models` repo as a reference zoo, **never a dependency**) | **FEASIBLE — recommended C2** | Daily (later hourly) bars suffice. Supervised classification is the easiest shape to preregister: frozen label definition, frozen features, logistic-regression twin on identical features. |
| GANs (price simulation / augmentation) | **DEFER** | Augmentation is only meaningful after a baseline model exists and its data hunger is measured. A GAN result is also not a strategy — nothing to promote. Revisit after C1/C2 verdicts. |
| Portfolio / multi-asset allocation | **DEFER** | Phase 5 discipline: one asset family, one vertical first. Cross-sectional allocation multiplies the certified-data requirement across the whole panel and adds sizing authority questions D3 deliberately avoids. |
| DeepLOB / high-frequency | **NOT FEASIBLE** | Needs historical limit-order-book depth. Chronos has none and the IBKR historical API does not supply order-book history. Also collides with the loop's design (time-driven cycles, no event path). |
| Reinforcement learning (all RL rows and repos) | **NOT FEASIBLE NOW — and autonomy-adjacent, so deliberately deferred** | Needs a trusted market simulator (fill model, costs, impact) that does not exist; the causal fill adapter is campaign infrastructure, not an RL gym. Sample-inefficient on daily bars (757–6000 rows). And an RL agent *is* a decision policy — training one blurs the research/autonomy boundary the Five-Tool plane keeps sharp. Requires its own ADR if ever revisited. |
| Cryptocurrency | **NOT FEASIBLE** | Asset family not enabled (Phase 5: equities/ETFs first; promotion never transfers across families). |
| Sentiment / behavioral / social | **NOT FEASIBLE** | No point-in-time text corpus (survivorship-safe, timestamped); scraped text is also exactly the R-30 prompt-injection surface the worker's threat model bounds. |
| Vulnerabilities (adversarial attacks on trading policies) | Out of scope as a strategy; **relevant reading** for whoever reviews D3's model-loading code. |
| Guides / courses / datasets rows | Context only. The Kaggle/AlphaVantage/Quandl dataset pointers are **not** certified sources and must not shortcut D2. |

## 5. The two recommended candidates

Recommendation: **C1 + C2**, because they share one data contract (the same
D2 export feeds both), share one trial frame, and are architecturally
disjoint enough (sequence regression vs. windowed classification) that their
results say different things. If the owner prefers a single candidate, C1
alone is the better-evidenced literature bet.

### C1 — Deep momentum network (Lim/Zohren 2019 family)

- **Shape:** small sequence model (the paper's own scale: LSTM on ~63-day
  windows of returns/vol features) emitting a position-direction score per
  instrument per day, volatility-scaled.
- **Deterministic twin (same frame, mandatory):** classical time-series
  momentum (sign of trailing 12-1 return) with the identical vol-scaling
  rule. The twin is not a formality — it is the null the paper itself had
  to beat.
- **Honest prior:** the paper's gains are modest and cost-sensitive;
  Chronos's cost model (commission, spread, slippage — Phase 3 gate) may
  erase them. A recorded "does not beat its twin after costs" is a
  successful trial outcome.

### C2 — CNN/LSTM direction classifier (Sezer/Ozbayoglu family)

- **Shape:** windowed OHLCV features → {up, down, flat} over a frozen
  horizon with a frozen dead-zone; long/flat signal only at first (no short
  authority question inside the research plane).
- **Deterministic twin:** logistic regression on the identical feature
  window, identical labels, identical splits.
- **Honest prior:** the 2018–2019 accuracy claims in this family are widely
  suspected to be adjustment/lookahead artifacts; on certified unadjusted
  data with embargoed splits, the expected result is near-chance. That is
  worth knowing and cheap to establish; it also validates the harness for
  C1.

## 6. What the owner's pick unlocks (and what it does not)

Order after the pick — nothing here starts before the thing above it:

1. **D2 (owner, the long pole):** certified IBKR export lands, quality gates
   (99.5% session coverage, classified gaps, reconciled corporate actions,
   content-addressed clean/seen/burned holdout map) pass, release digest
   frozen.
2. **Preregistration revision:** H-DT-001 (C1) / H-DT-002 (C2) written in
   the FIVE_TOOL_RESEARCH_HYPOTHESES pattern — hypothesis, falsifier,
   baseline arm, frozen thresholds, blocker codes — BLOCKED BEFORE DATA
   ACCESS until the D2 digest exists. Power calculation before any read.
3. **D3 build:** `src/chronos/research/deep/` mirroring `five_tool`'s
   proven pattern — no broker/order/mandate/service imports (extend the
   existing AST + `sys.modules` guards), trials through the append-only
   ledger, completed AND failed trials counting toward multiplicity,
   baselines first, content-addressed weights, training local (mickey/flow;
   these models are small — no cloud, per doctrine).
4. **D4 (speculative, owner-gated):** promotion wiring through existing
   rails only, and only if a promotion artifact exists.

**Dependency decision folded into the pick:** the hash-verified lock
currently carries `numpy 2.5.1` + `pandas 2.3.3` and **no DL framework**.
The twins (TSMOM, logistic regression) will be implemented numpy-only,
always. For the nets, the D3 plan will propose CPU-only PyTorch pinned into
the hash-verified lock as its own reviewed change — if that dependency is
unacceptable, C2's small MLP variant and a minimal C1 can be done numpy-only
at real implementation cost; the choice is made at D3 review, not silently.

This document does **not**: authorize any data read (the brokered reader
and blocked-campaign posture are untouched), modify any manifest, create
hypotheses (the stubs above are names, not registrations), change
dependencies, or make any performance claim. "Feasible" here means "can be
attempted under the rails" — it is not evidence of edge, and the D2 gate
means nothing is attemptable today.

## 7. Decision requested

Pick any of (or none — "not now" keeps the PR #76 deferral in force):

- [ ] **C1** — deep momentum network + TSMOM twin
- [ ] **C2** — CNN/LSTM direction classifier + logistic twin
- [ ] Neither / not now

Recorded by the owner on the PR or in DECISIONS.md; the pick (if any) makes
the preregistration revision the next research artifact after D2.
