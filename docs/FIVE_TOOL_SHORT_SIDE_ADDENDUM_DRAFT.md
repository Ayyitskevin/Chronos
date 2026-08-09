# Five-Tool short-side attribution addendum — DRAFT

Status: **DRAFT — proposed, owner decision required. NOT registered, NOT part of
any campaign identity.**
Scope: research design only; no data access, no strategy change, no performance
claim, no promotion or activation of anything.
Relationship to the registered document: `docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md`
is a frozen preregistration surface and is not modified by this file. Adopting
this addendum means folding it into the **next owner-blessed manifest revision**,
which changes the campaign's manifest digest and therefore starts a new campaign
identity (the same revision can carry the two stale-prose corrections already
queued: the manifest's `blocked_before_first_data_read` list and
`FIVE_TOOL_RESEARCH_HYPOTHESES.md:23`, both of which still name capabilities
that landed in Tracks B.1–B.3). Hypothesis numbering below is provisional until
registration.

## Why the short side is its own preregistration, not a mirror

The registered hypotheses H-5T-001…006 isolate components on a fixed
information set that is dominated by long-side economics: U.S. equity ETFs with
positive drift. A short position in those instruments fights the drift, pays
carry, and takes its worst losses during rallies *inside* bear markets — the
sharpest rallies in the sample. The momentum-crash literature (Daniel &
Moskowitz, "Momentum Crashes," *JFE* 2016) documents exactly this asymmetry:
short legs of momentum strategies are destroyed in rebounds, not in trends.
Mirroring a long signal and calling it a short strategy is therefore an
untested claim, and this addendum registers it as one.

The v3.6 program already encodes the asymmetry doctrine ("bull regimes are not
age-gated the way bear regimes are") and already carries the candidate
short-side machinery — the bear-Markov gates with a selectable Wilson
lower-bound stay-probability estimator, the SHORT+ dwell-percentile youth gate
(default OFF), and setup-coded short entry IDs for attribution. The owner's
corpus separately holds three complementary tools this addendum nominates as
candidate short-side components: `02_markov_regime_bear_plus.pine`,
`09_breadth_internals_proxy.pine`, `07_squeeze_exhaustion_sentinel.pine`, and
`08_gap_overnight_risk_classifier.pine`.

## Evidence the cited sources do and do not provide

| Source | Supports | Does **not** support |
|---|---|---|
| Daniel & Moskowitz, "Momentum Crashes," *JFE* (2016) | Short legs suffer crash risk in rebounds; option-like payoff of shorts in bear-market rallies; motivation for regime/vol conditioning of short exposure | The Pine bear-Markov classifier, Wilson-bound choice, any threshold, or this panel |
| Moreira & Muir, "Volatility-Managed Portfolios," NBER 22208 | Scaling exposure by observed variance in their settings | Short-side entry timing; the ATR/vol-percentile implementation |
| Breadth/participation literature (practitioner-grade; no single canonical citation is claimed here) | Motivation to *test* breadth deterioration as a top/regime confirmation input | Any claim that the owner's breadth proxy predicts returns; this is the weakest-sourced component and is registered as such |
| TradingView execution-model / repainting docs | Confirmed-bar semantics for any short-side signal | Any economic claim |
| `research/pine/00_five_tool_confluence_aio.pine` @ SHA-256 `e51d5a40…48e45f` and the corpus scripts named above | Exact implementation authority for what is being tested | Independent evidence that any component works |

## Registered hypotheses (draft)

Every hypothesis below inherits the common campaign tests, cost stress, plateau
requirement, and sample floors of the registered document unchanged. **No
threshold in this addendum may be set or revised after any cell is observed.**

### H-5T-007-SHORT-REGIME — bear-regime gate quality

**Claim under test.** On confirmed bars, short exposure gated by the bear-Markov
regime state (using the **Wilson lower-bound** stay-probability estimator)
produces post-cost out-of-sample expectancy and benchmark-relative alpha whose
95% lower bounds are above zero, against a duration- and exposure-matched
baseline that shorts at random times within price-defined downtrends.

**Isolation.** Regime gate and direction rule only; momentum, divergence, RS,
AVWAP, and vol scaling disabled as alpha inputs. The Wilson-vs-raw estimator
choice is itself a preregistered paired comparison inside this cell — not a
post-hoc pick.

**Falsification.** The common floors, plus: reject if the result depends on the
single best bear episode in the accessible partitions (episode-removal test),
or if the baseline comparison loses significance under doubled borrow-cost
assumptions (borrow modeled explicitly; see execution reality below).

### H-5T-008-SHORT-YOUTH — dwell-age youth gate increment

**Claim under test.** Conditional on H-5T-007's gate, restricting short entries
to *young* bear regimes (the v3.6 dwell-percentile youth gate) adds positive
post-cost incremental expectancy versus the same cell with the youth gate off.

**Isolation.** Paired cells identical except the youth gate. The dwell
percentile threshold is frozen at the v3.6 shipped default before any cell is
observed; no threshold sweep is registered.

**Falsification.** Common floors on the paired increment; additionally reject
if the gate's trade-count reduction pushes the cell below the frozen sample
floor — an underpowered improvement is INSUFFICIENT_EVIDENCE, not a pass.

### H-5T-009-SHORT-BREADTH — breadth-deterioration confirmation increment

**Claim under test.** Requiring breadth-proxy deterioration (the owner's
`09_breadth_internals_proxy` construction, exact parameters frozen at its
corpus SHA) as a confirmation adds positive post-cost incremental expectancy to
the H-5T-007 cell versus breadth-off.

**Isolation.** Paired cells; breadth is confirmation-only (may veto, never
initiate). **Source honesty:** this is the weakest-sourced hypothesis in the
addendum; a rejection here is the expected base case and must be reported as
informative, not buried.

**Falsification.** Common floors on the paired increment; reject additionally
if the increment is confined to one instrument or one bear episode.

### H-5T-010-SHORT-TAIL — overnight/gap risk exclusion (a risk hypothesis)

**Claim under test.** Excluding short holds flagged high-risk by the gap/
overnight classifier (`08_gap_overnight_risk_classifier`, parameters frozen at
corpus SHA) improves the short book's post-cost tail profile — CVaR(95) of
per-trade returns and worst-single-gap loss — without reducing expectancy's
95% lower bound below zero.

**Isolation.** Paired cells identical except the exclusion rule. This is a
**risk** hypothesis: its acceptance criterion is tail improvement at
non-negative expectancy, not expectancy improvement.

**Falsification.** Reject if tail metrics do not improve under both base and
stressed costs, or if the exclusion removes so many holds the cell falls below
the frozen floor. Best-trade/best-month removal applies to the tail metrics'
stability, not only the mean.

## Data hazards this addendum must not paper over

1. **Bear regimes are rare and the sample floors do not bend.** The likely
   honest outcome of every hypothesis above, for years, is
   INSUFFICIENT_EVIDENCE. That outcome is acceptable and expected; the floors
   are not editable to escape it.
2. **The burned window contains the most recent bear market.** QQQ
   2022-01→2024-01 is consumed and not clean. Because the panel's instruments
   are highly correlated, a correlated-window rule must be frozen at
   registration: **the 2022 bear episode is treated as contaminated across the
   entire panel**, not only for QQQ. Accessible bear episodes are whatever the
   certified dataset's clean partitions actually contain (candidates: 2011,
   2015-16, 2018Q4, 2020; their availability is a fact of the certified
   dataset, not an assumption of this document). The declared 2026-Q4 holdout
   remains future, forbidden, and unopened.
3. **Cross-hypothesis multiplicity.** These four cells (plus paired variants)
   join the campaign's global trial count in the canonical ADR-0013 registry
   like every other attempt. The FWER/FDR budget is campaign-wide; adding
   hypotheses spends it, and that cost is accepted at registration, not
   discovered after.

## Execution reality (binding disclosure)

Research validity is not tradability. D-12 scoped executable candidates to
long-only for account-size, PDT, margin, and cost reasons, and nothing here
changes that. Short-selling ETFs in the current account is likely
unexecutable (margin/borrow/PDT); modeling must therefore include explicit
borrow-cost assumptions, and any future executable short authority would
require its own mandate scope, promotion evidence per family, and owner
decisions — none of which this addendum requests or implies. A validated short
component whose only honest disposition is "informs the long side's regime
exits" is a legitimate and likely end state.

## What this addendum does not do

No data is accessed, no cell is run, no component is added to or changed in
the v3.6 program, no manifest field is edited, and no capability is unblocked
by this document. It exists so that, when the owner freezes Phase-0 evidence
and certifies a dataset, the short side enters the campaign as a registered
question with its multiplicity paid — instead of as a debate.
