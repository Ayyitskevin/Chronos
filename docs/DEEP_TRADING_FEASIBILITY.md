# Deep-trading candidate feasibility — mapping awesome-deep-trading onto Chronos

Status: **DECIDED 2026-08-21 — the owner selected C1 + C2** (in-session
direction to Claude Code; recorded as D-29 in DECISIONS.md and on PR #79)
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

[`cbailes/awesome-deep-trading`](https://github.com/cbailes/awesome-deep-trading),
whose latest commit is
[`91eee43`, 2021-01-01](https://github.com/cbailes/awesome-deep-trading/commit/91eee433ec7791915ad20b15330f0eee04798f07),
is a curated link list of papers
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
| **CNN/LSTM direction classifiers** (separate Sezer/Ozbayoglu CNN and Fischer/Krauss LSTM lines; the `huseinzol05/Stock-Prediction-Models` repo as a reference zoo, **never a dependency**) | **FEASIBLE — recommended C2** | Daily (later hourly) bars suffice. C2 is a new Chronos experiment inspired by separate sources, not a replication of a published CNN/LSTM hybrid. Each architecture is a separately preregistered and multiplicity-counted arm with frozen labels/features and a logistic-regression twin on identical inputs. |
| GANs (price simulation / augmentation) | **DEFER** | Augmentation is only meaningful after a baseline model exists and its data hunger is measured. A GAN result is also not a strategy — nothing to promote. Revisit after C1/C2 verdicts. |
| Portfolio / multi-asset allocation | **DEFER** | Phase 5 discipline: one asset family, one vertical first. Cross-sectional allocation multiplies the certified-data requirement across the whole panel and adds sizing authority questions D3 deliberately avoids. |
| DeepLOB / high-frequency | **NOT FEASIBLE** | Needs historical limit-order-book depth. Chronos has none and the IBKR historical API does not supply order-book history. Also collides with the loop's design (time-driven cycles, no event path). |
| Reinforcement learning (all RL rows and repos) | **NOT FEASIBLE NOW — and autonomy-adjacent, so deliberately deferred** | Historical-replay RL can run without a complete simulator, but only under restrictive environment assumptions such as no market impact. Chronos has no approved RL environment/accounting contract, and an RL agent *is* a decision policy — training one blurs the research/autonomy boundary the Five-Tool plane keeps sharp. Requires its own ADR if ever revisited. |
| Cryptocurrency | **NOT FEASIBLE** | Asset family not enabled (Phase 5: equities/ETFs first; promotion never transfers across families). |
| Sentiment / behavioral / social | **NOT FEASIBLE** | No point-in-time text corpus (survivorship-safe, timestamped); scraped text is also exactly the R-30 prompt-injection surface the worker's threat model bounds. |
| Vulnerabilities (adversarial attacks on trading policies) | Out of scope as a strategy; **adopt as future test-method input** for stale/malformed observations and model integrity after a candidate exists. It supplies no Chronos pass threshold and is not evidence of edge. |
| Guides / courses / datasets rows | Context only. The Kaggle/AlphaVantage/Quandl dataset pointers are **not** certified sources and must not shortcut D2. |

## 5. The two recommended candidates

Recommendation: **C1 + C2**, because they share one data contract (the same
D2 export feeds both), share one trial frame, and are architecturally
disjoint enough (sequence regression vs. windowed classification) that their
results say different things. If the owner prefers a single candidate, C1
alone is the better-evidenced literature bet.

### C1 — Deep momentum network (Lim/Zohren 2019 family)

- **Shape:** small sequence model (the paper uses a 63-step LSTM trajectory;
  input features include horizons through one year) emitting a
  position-direction score per instrument per day, volatility-scaled.
- **Deterministic twin (same frame, mandatory):** classical time-series
  momentum (sign of trailing 12-1 return) with the identical vol-scaling
  rule. The twin is not a formality — it is the null the paper itself had
  to beat.
- **Evidence boundary:** the paper explicitly incorporates turnover into
  cost-adjusted training/evaluation. Chronos must use its own certified cost
  model and cannot transfer the paper's costs or results. A recorded "does
  not beat its twin after costs" remains a successful trial outcome.

### C2 — separately counted CNN and LSTM direction classifiers

- **Shape:** windowed OHLCV features → {up, down, flat} over a frozen
  horizon with a frozen dead-zone; long/flat signal only at first (no short
  authority question inside the research plane).
- **Deterministic twin:** logistic regression on the identical feature
  window, identical labels, identical splits.
- **Evidence boundary:** this shape is Chronos-designed. The cited Sezer
  work proposes CNN image representations, while the LSTM evidence is a
  separate classifier line; neither source supports treating "CNN/LSTM" as
  one uncounted hybrid. The sources do not establish Chronos-grade leakage,
  corporate-action, or tuning-isolation controls, so no expected accuracy or
  profitability is inferred from them.

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

## 7. Decision

Decided 2026-08-21 (owner, in-session direction; D-29):

- [x] **C1** — deep momentum network + TSMOM twin
- [x] **C2** — CNN/LSTM direction classifier + logistic twin
- [ ] Neither / not now

The pick makes the preregistration revision the next research artifact
after D2. Nothing else is unlocked: D2 remains the gate, and the §6
ordering stands.

## 8. Primary-source mining pass (2026-08-29)

The catalog was used only as an index. Its latest commit is dated
2021-01-01 (§1), so it is stale evidence for the current state of libraries,
data access, and deployment practice. The papers and first-party repositories
below supply hypotheses, data-contract checks, and falsification methods —
**not broker evidence, a validated edge, or proof of autonomous trading**.
Synthetic data cannot satisfy D2, a trial gate, a prospective holdout, or a
broker/paper/live promotion gate.

### Finding ledger

| Claim | Primary source | Confidence and one-line reason | Chronos disposition |
|---|---|---|---|
| C1 directly combines volatility-scaled time-series momentum with sequence models and cost/turnover-aware objectives; its 63-day value is the LSTM trajectory length, while features extend through one year. | [Lim, Zohren & Roberts (2019)](https://arxiv.org/abs/1904.04912) | **High on method; Medium on transfer** — the design is explicit, but its futures corpus is not a Chronos ETF replication set and no official code was located. | **Adopt after D2** as H-DT-001, inseparable from its deterministic TSMOM/MACD twin and Chronos-native costs. |
| The Sezer/Ozbayoglu 2018 source is a CNN over a 15×15 technical-indicator image; it does not publish a CNN/LSTM hybrid. | [Sezer & Ozbayoglu (2018)](https://doi.org/10.1016/j.asoc.2018.04.024) | **High** — the architecture and representation are the paper's stated method. | **Adopt after D2** only as inspiration for a separately registered CNN arm; do not call C2 a replication. |
| The related CNN-BI proof of concept renders 30-day bar-chart images, derives labels from future-price slopes, and describes tuning by observing experiment results without a Chronos-grade isolated-validation contract. | [Sezer & Ozbayoglu (2019)](https://arxiv.org/abs/1903.04610) | **High on the documented protocol** — labels and tuning are described directly; this is a control gap, not a claim that leakage occurred. | **Adopt now as test design:** training-only label calibration, full-horizon purge/embargo, duplicate-window checks, and a frozen tuning budget. |
| Published LSTM direction classification is a separate model line with logistic regression among its comparators, not evidence for merging CNN and LSTM into one arm. | [Fischer & Krauss (2018)](https://doi.org/10.1016/j.ejor.2017.11.054) | **High on comparison design; Medium on transfer** — the publisher source identifies the models, but its equity universe and period do not establish Chronos results. | **Adopt after D2** as its own multiplicity-counted arm with the same labels, splits, features, and logistic twin. |
| The archived `Stock-Prediction-Models` project is a notebook/reference zoo rather than a governed reproducibility base. | [First-party repository](https://github.com/huseinzol05/Stock-Prediction-Models) | **High** — GitHub marks it archived and its README presents many example models/agents. | **Reject** as a dependency, benchmark, or evidence source; retain only as an idea index. |
| Observation-channel attacks on DQN trading policies include one-step delays and bounded, financially coherent perturbations. | [Faghan et al. (2020)](https://arxiv.org/abs/2010.11388) | **Medium for test transfer; Low for quantitative generalization** — the threat shapes are concrete, but the paper studies DQN policies in limited environments, not C1/C2. | **Defer** execution until a model exists; adopt the threat shapes now as non-numeric test cases with no imported pass threshold. |
| Stock-GAN requires message-level orders, cancellations, quantities, and order-book state; its real-market demonstration is intentionally narrow. | [Stock-GAN (AAAI 2020)](https://ojs.aaai.org/index.php/AAAI/article/view/5415) | **High on the data contract; Low on broad generalization** — required inputs are explicit and the paper frames the market experiment as an initial step. | **Reject for the current wedge**: Chronos has no LOB/tick corpus or HFT event path. Synthetic order flow would not validate broker fills. |
| Equity-options simulation consumes full strike/maturity surfaces and uses a discrete-local-volatility representation to preserve static no-arbitrage structure. | [Wiese et al. (2019)](https://arxiv.org/abs/1911.01700) | **High on required data; Medium-Low on temporal generalization** — the representation is explicit, while the evaluation uses a random rather than prospective temporal split. | **Defer** to the options/Phase 5 data program; bars-only D2 cannot support it, and synthetic surfaces cannot replace lifecycle or fill evidence. |
| DeepLOB consumes raw limit-order-book price/size tensors and combines convolutional and recurrent components. | [Zhang, Zohren & Roberts (2018)](https://arxiv.org/abs/1808.03668), [authors' repository](https://github.com/zcakhaa/DeepLOB-Deep-Convolutional-Neural-Networks-for-Limit-Order-Books) | **High** — the paper and first-party repository expose the LOB input contract. | **Reject for current Chronos**: OHLCV cannot reconstruct queue state, fills, latency, or cross-venue semantics. |
| Replay-based RL is technically possible without a complete simulator, but published examples assume away market impact and depend on a chosen environment/reward contract. | [Théate & Ernst (2020)](https://arxiv.org/abs/2004.06627), [authors' code](https://github.com/ThibautTheate/An-Application-of-Deep-Reinforcement-Learning-to-Algorithmic-Trading) | **High on the limitation** — the paper states the no-impact assumption; that makes replay possible but not broker-realistic. | **Defer**, not categorically reject: revisit only via a separate ADR covering deterministic accounting, costs/slippage, terminal liquidation, seeds/trial counts, reward mismatch, and zero order authority. |

### Action map to exact Chronos gaps

| Timing | What is mined | Gap it addresses or gate it waits on |
|---|---|---|
| **Adopt now (research specification only)** | Deterministic twins; training-only transforms/label calibration; chronological walk-forward splits; full-label-horizon purge/embargo; duplicate-window assertions; trial counting across architecture/loss/seed/tuning choices; adversarial input-shape checklist; simulator-fidelity checklist. | Tightens the future preregistration and leakage controls without reading data, running a trial, adding a framework, or advancing a gate. |
| **Adopt after D2** | C1 plus TSMOM/MACD twins; separately preregistered C2 CNN and LSTM arms plus logistic twin; all using identical certified releases, split manifests, sizing, and Chronos costs. | Waits on the missing certified daily/hourly ETF release, corporate-action reconciliation, session coverage, clean/seen/burned map, and frozen digest. |
| **Defer** | Adversarial execution tests; options-surface simulation; all RL; GAN augmentation; multi-asset allocation. | Each waits respectively on a trained candidate; real option surfaces/lifecycle capture; a separate environment/authority ADR; demonstrated baseline data need; or Phase 5 cross-asset and sizing governance. |
| **Reject for the current wedge** | DeepLOB/HFT; notebook-zoo dependency; synthetic streams/surfaces as trial, broker, or promotion evidence. | Chronos lacks message-level LOB/tick state and an HFT event runtime; examples lack Chronos governance; synthetic fidelity cannot prove real costs, fills, edge, or autonomy safety. |

### Concrete experiment and test extractions

- **Cost and turnover:** preregister separate C1 objectives only if selected;
  charge every arm through the same Chronos commission/spread/slippage model,
  report turnover alongside post-cost output, and import no paper cost number.
- **Deterministic baselines:** run TSMOM/MACD before C1 and logistic regression
  before each C2 architecture using identical release bytes, timestamps,
  labels, split manifests, sizing, and costs. A neural-only result is invalid.
- **Leakage and multiplicity:** fit scalers, volatility estimates, feature
  transforms, and label thresholds inside each training fold; purge/embargo at
  least the complete frozen label horizon; reject duplicated dates/windows
  across partitions; count every architecture, loss, seed, feature set, and
  tuning choice in the append-only registry. The preregistration, not this
  document, sets numeric thresholds and budgets.
- **Adversarial stress:** after a candidate exists, replay one-cycle stale,
  missing, duplicated, reordered, and corrected observations; add bounded
  OHLC-consistent feature perturbations and model-hash mismatch. Assert
  deterministic refusal or bounded signal behavior and zero order-authority
  escalation; freeze pass criteria before execution.
- **Simulator limits:** any future LOB simulator must reproduce exchange
  ordering, queue priority, cancels, partial fills, intensity, and spread
  behavior on untouched regimes. Any future options simulator must enforce
  calendar/butterfly/spread invariants and pass held-out density, tail,
  cross-correlation, and autocorrelation diagnostics. Passing those checks is
  simulator validation only — never strategy edge, broker evidence, or
  autonomous proof.
