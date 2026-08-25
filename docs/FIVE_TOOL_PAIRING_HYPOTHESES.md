# Five-Tool pairing hypotheses (v1)

Status: **PREREGISTERED / BLOCKED BEFORE DATA ACCESS**

Manifest: `research/five_tool_pairing_v1_campaign_manifest.json`

Host: Five-Tool Confluence AIO v3.6 at Pine SHA-256
`e51d5a40d2e933bf86847c7432364ba8934fd2de653d6aec3d7205639248e45f`.
This campaign is a sidecar. It does not reopen or mutate
`five-tool-v3.6-preregistered-002`.

No campaign result exists. Public validation authorizes zero executable trials.

## Intraday companion screen (2026-08-24)

The orthogonal market-state/session/liquidity screen selected **no companion**:
`INSUFFICIENT_EVIDENCE`. Contemporaneous relative quoted spread plus quote
freshness (`C-1`) is the highest-priority future cell, but it remains contingent
on certified identity-bound quotes/trades, an exact admission clock, frozen
costs and thresholds, multiplicity treatment, and an untouched owner holdout.

The bounded arrival-quote slice is measurement infrastructure only: it can
causally align a quote and fail closed, but it cannot open data, apply a veto,
register a trial, mutate Five-Tool/Pine identity, or create selection, paper,
live, risk, or promotion authority. See
[FIVE_TOOL_INTRADAY_COMPANION_SOURCES.md](FIVE_TOOL_INTRADAY_COMPANION_SOURCES.md)
and `research/five_tool_intraday_quote_evidence_v1_manifest.json`.

## Isolation

Each cell holds the Five-Tool signal stream, fill policy, and risk budget fixed.
Treatment applies one named veto; control leaves that veto off. Incremental
expectancy is the object of test. Neighbor axes are single-input and monotone.

Common falsification follows [FIVE_TOOL_RESEARCH_HYPOTHESES.md](FIVE_TOOL_RESEARCH_HYPOTHESES.md):
post-cost incremental expectancy 95% lower bound above zero; three instruments
and two regimes; stressed costs; 67% neighbor plateau; best-trade and best-month
removal. Those statistical gates are unimplemented here and therefore blocking.

## H-PAIR-TAIL

**Claim.** A `FAT_TAILED` veto on confirmed-bar tail moments improves post-cost
expectancy or tail risk versus the same Five-Tool entries with the veto off.

**Neighbor axis.** `tail_kurtosis_fat`.

**Source boundary.** Mandelbrot-style tail facts motivate the test. They do not
validate the window, kurtosis cut, or this ETF sample.

## H-PAIR-RVOL

**Claim.** Requiring daily In-Play (`volume / SMA(volume, 20) >= 1.5` and the
frozen dollar-volume floor) as an entry veto adds positive post-cost incremental
expectancy versus the unfiltered Five-Tool stream.

**Neighbor axis.** `rvol_min_ratio`.

**Source boundary.** Zarattini / Barbon / Aziz (2024) motivate RVOL as a
selection statistic. Time-of-day RVOL is inert on daily bars and is not part of
this cell.

## H-PAIR-VIX

**Claim.** Blocking new entries when index vol is `STRESS`, or `ELEVATED` with
VIX/VIX3M backwardation, improves post-cost expectancy or drawdown versus the
same stream with the veto off.

**Neighbor axis.** `iv_cut_stress`.

**Source boundary.** This is S&P index-vol weather, not symbol IV. Missing VIX
fails closed.

## H-PAIR-BREADTH

**Claim.** Blocking entries when ETF-ratio ALIGN is `-1` (RSP/SPY and QQQ/SPY
slopes disagree with the Five-Tool regime) adds positive post-cost incremental
expectancy.

**Neighbor axis.** `breadth_slope_lookback`.

**Source boundary.** TICK/ADD/VOLD are optional and unused for the daily veto.
Missing RSP, SPY, or QQQ fails closed.

## Invalidation

A change to the host Pine SHA, feature-policy digest, companion-catalog digest,
certified-intake identity, fill policy, or hypothesis topology ends this
campaign identity. Failure on a later untouched holdout is rejection, not a
threshold edit. The consumed QQQ 2022-01 through 2024-01-10 window is not a
clean holdout.

GLD does not use `H-PAIR-VIX` or `H-PAIR-BREADTH`. Gold cells are on
`five-tool-pairing-gld-v1-preregistered-001`. See
[FIVE_TOOL_GOLD_STRATEGY.md](FIVE_TOOL_GOLD_STRATEGY.md).
