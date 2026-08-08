## Corpus composition and duplication analysis

The 42 scripts are **not 42 independent strategies**. The forensic audit's
`related_scripts` findings (research/pine_findings.json) show one coherent
"5T Pine Suite" family built around a shared core, with most artifacts being
satellites, variants, or measurement companions rather than distinct edges:

- **Shared math core**: script 03 (KLQuant library) supplies the Wilson
  confidence-interval, time-of-day matrix, RVOL, Markov-stay, gap-class, and
  squeeze primitives that 04, 05, 06, 07/24, 08/33, 12, and the BULL+/BEAR+
  pair all cite as their source (explicit line-referenced credit in the
  audit findings, not inferred).
- **One flagship, one mirrored pair**: script 00 (Five-Tool Confluence AIO
  v3.6) is the superset system; scripts 01 (BULL+) and 02 (BEAR+) are an
  explicit long/short mirror pair "ported from AIO v3.6" (their own headers
  say so) sharing the identical regime engine, Markov gate, and AVWAP
  machinery. Script 0A (archived Confluence Swing Strategy) is an earlier,
  simpler ancestor of the same family with its own dependency chain
  (Regime Label, RS Leader, AVWAP, R-Planner components).
- **Regime-export satellite cluster** (11 scripts): 17, 18, 19, 20, 23, 24,
  25, 26, 29, 31, 32, 33, 34, 36 all either *consume* a `REGIME_EXPORT`
  {-1,0,+1} signal produced by the flagship/BULL+/BEAR+ engines via
  `input.source`, or re-implement the identical "volatility-normalized
  window-return z-score" fallback classifier when no link is wired. These
  are measurement/readout lenses on one regime signal, not independent
  regime-detection strategies.
- **Journaling/validation-graft family** (5 scripts): 12 (Expectancy
  Journal), 27 (Profitable vs Tradable), 28 (Cost Stress), 30
  (Drawdown-Controlled Exit), 35 (Statistical Edge Validation Dashboard) all
  self-describe as the same "strategy-shell graft" pattern intended to be
  copied into a host strategy, sharing the L1P/S1F entry-ID convention, the
  Wilson-lower-bound doctrine, and a throwaway demo strategy for
  self-contained testing.
- **Mean-reversion/Kalman lineage** (4 scripts): 21 (Kalman Filter MR
  Detector) is the core; 36 (Regime-Conditioned Kalman MR) explicitly
  carries its logic "verbatim" and adds regime ledgers; 11 (MR Extremes
  Study) and 37 (Adaptive MR w/ Regime Probability) share the same
  volatility-normalized stretch/z-score family and Wilson-bound scoring.
- **Session/gap/RVOL structure family** (6 scripts): 04, 05, 06, 08, 26, 33,
  34 share day-anchored session machinery, gap-vs-ATR construction, and
  time-of-day RVOL slotting, each a different lens on the same underlying
  session-structure primitives from KLQuant.
- **Standalone/context tools** (5 scripts): 13 (SMC/ICT vocabulary study,
  explicitly thin-evidence by its own design), 14 (Fibonacci confluence),
  15 (Buffett valuation lens), 16 (Pullback-to-Value teaching combo), 22
  (Kelly sizing overlay), 39 (Monte Carlo bands), 40 (Black-Scholes gauge)
  are each genuinely distinct tools with no direct sibling, though several
  still consume the suite's `REGIME_EXPORT` convention.

**Estimated genuinely distinct strategies (executable trading systems, not
readouts): 4** — the flagship AIO (00), the BULL+/BEAR+ mirror pair (01/02,
arguably one engine with a sign flip), and the archived predecessor (0A).
Everything else in the 13 `PASS_WITH_CONSTRAINTS` scripts is a strategy-shell
graft (add-on) or a bidirectional indicator/study with entry-like logic but
no independent thesis. The remaining 28 scripts are explicitly
`NON_EXECUTABLE_INDICATOR` by design — readouts, filters, and studies that
feed the four systems above rather than trading on their own. This matches
the corpus's own stated doctrine ("one math core, written once"; "the gate
is the strategy") and is why Chronos derived only two independent executable
candidates (`regime_trend_v1`, `mean_reversion_v1`) rather than translating
all 42 files — see docs/STRATEGY_SELECTION.md for why even those two did not
clear the research bar.
