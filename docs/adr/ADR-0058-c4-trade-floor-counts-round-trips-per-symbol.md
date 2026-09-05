# ADR-0058 — C4's trade floor counts round trips, per candidate per symbol

Status: **accepted interpretation — ruled by the lead on 2026-09-05 under the owner's delegated
steering; relaxable only by the owner, in writing.** Index entry: DECISIONS.md D-74. Risk entry:
RISK_REGISTER.md R-76.

## Context

`research/selection_manifest.json` was frozen on 2026-07-17, before validation results existed, and
re-frozen unchanged before the three added symbols were computed. Its fourth criterion reads:

> C4: profit factor >= 1.1 with **>= 20 closed trades on the validation window**, AND remains
> net-positive under 2x commission stress and >= 10 bps slippage stress

Two phrases in that sentence are not self-defining, and both have now been reached by real work
rather than by speculation:

- **"closed trades."** Four of the corpus's five standalone strategies hold one position while
  splitting the entry into three same-bar legs. When such a position closes, three legs close and
  one round trip closes. The criterion does not say which it counts.
- **"on the validation window."** The window is one date range shared by five symbols. A count
  taken "on the validation window" could be per symbol, or across all symbols at once.

Neither ambiguity mattered while the only candidates were single-entry strategies evaluated one
symbol at a time. Both matter now: a port of `16_pullback_to_value_playbook` was designed against
the first, and a pooled variant of its probe cleared a bar that no per-symbol variant cleared.

## The two readings, and what each is worth

**The leg reading is worth 3× on four of five strategies.** `16_pullback_to_value_playbook` gates
both entries on `strategy.position_size == 0` (L310/L311) while `pyramiding = 3` (L32) funds three
same-bar legs (L371/L374/L377). opus-3's S0 probe (2026-09-05) measured the best per-symbol cell the
existing corpus offers it — QQQ with the relative-strength gate off, AVWAP-only — at **15** spaced
setups, an upper bound on round trips. Fifteen round trips fails a floor of twenty. The same fifteen
positions, counted by leg, are forty-five closed trades and pass. Identical data, identical
strategy, identical corpus; the verdict turns entirely on which noun "closed trades" attaches to.

**Pooling lowers the bar twice.** It raises the count, and — because the floor is a fixed *count*
rather than a rate — it simultaneously lowers the rate that count implies. Measured two ways on
2026-09-05:

| evidence | per symbol | pooled |
|---|---|---|
| validation-window closed trades, base configuration (corpus arithmetic) | `regime_trend_v1` max **18**, `mean_reversion_v1` max **15** — both fail | **53** and **56** — both pass |
| S0 setup-rate probe for #16, RS off, AVWAP-only | QQQ alone: 15 setups over 1008 bars, floor **19.84** per 1000 — fails | four symbols: 42 over 2979 bars, floor **6.71** per 1000 — passes |

The second row is the one to keep. The required rate fell by a factor of three because symbols were
added to the denominator, not because any strategy improved. A candidate that cleared C4 that way
would have cleared a criterion nobody wrote.

The frozen manifest already leans against pooling without quite closing it. C1 is scoped "on at
least one symbol", and the re-freeze's multiple-testing guard states that the interpretation must
discount, not credit, breadth:

> a candidate is not treated as validated on the strength of one symbol among many ... the new
> symbols' short 2019-2021 windows make a >= 20-trade sample unlikely, so they function primarily as
> corroborating out-of-sample robustness checks, not as fresh chances to manufacture a pass.

## Decision

**I-1.** "Closed trades" counts **round trips**. A position opened and subsequently closed is one
closed trade, however many legs, fills or tranches its entry or its exit is split into. A leg
closing inside a still-open position is not a closed trade.

**I-2.** C4 is evaluated **per candidate per symbol** on the validation window. A (family, symbol)
cell either clears twenty closed trades or does not. Counts are never pooled across symbols, and no
candidate is credited with a C4 pass it did not earn on a single symbol.

Both readings are **conservative by direction on the clause they were ruled about** — the trade
count. Round trips are never more numerous than legs, and a per-symbol count is never larger than a
pooled one, so under both readings the ">= 20 closed trades" test is harder to satisfy and never
easier. That much is proved.

**It is not proved for C4 as a whole, and this ADR does not claim it.** C4 is a conjunction, and its
other half — profit factor >= 1.1 — is computed over whatever "closed trades" denotes, so changing
the denotation changes the profit factor too, and not monotonically. One counterexample settles it: a
candidate whose trades are one round trip of legs (+60, -20, -20) and one single-leg round trip of
(-15) has, counted as round trips, gross profit 20 against gross loss 15 — **PF 1.33, which passes**;
counted as legs, gross profit 60 against gross loss 55 — **PF 1.09, which fails**. On that clause the
round-trip reading is the *more permissive* one. Neither reading dominates the other on the
conjunction, so the set of candidates passing C4 is not a subset under either, and the unqualified
claim this ADR first made was too strong.

What survives is the claim worth making, and it is the one the ruling rests on: each reading tightens
the clause it was ruled about, neither was chosen because it let something through, and the
profit-factor clause's direction is recorded as unproven in R-76 rather than assumed away. Neither
reading may be loosened by implication, by a refactor, or by a later document; only by the owner, in
writing.

## Consequences

**Nothing measured changes, and that is checkable.** Both readings describe what the code already
does:

- `backtest/engine.py`'s fill loop appends exactly one `ClosedTrade` when a sell reaches
  `BrokerEventKind.FILLED`, then resets the position, so a subsequent exit fill finds no open entry
  and appends nothing; sell-side `PARTIAL_FILL` never appends; buy-side fills only accumulate
  `position.quantity`. Three entry legs therefore already produce one `ClosedTrade`.
- `research/campaign.py` iterates strategy-by-symbol and emits one verdict row per cell. No pooling
  path exists to disable.

That both readings were already true is the argument for writing them down rather than against it.
Until this ADR they were **emergent properties of a fill loop and a nested loop** — unstated, unnamed
by any test, and changeable by a refactor that looked like a tidy-up. This converts two accidents
into two contracts.

**It answers an open implementation question.** opus-3's port design for #16 raised exactly this as
its D-2 — *"does C4's '>= 20 closed trades' count round trips or legs? Up to 3× on this strategy.
Criteria question; RED here; needs whoever owns the frozen criteria."* I-1 is that answer.

**It creates an obligation for legged ports, not a permission.** Any future strategy that scales into
or out of a position must still contribute one closed trade per round trip. Meeting that is the
porting work's burden; C4 does not move to accommodate it.

## Scope

No number, window, partition, threshold or criteria file changes. `research/selection_manifest.json`
is untouched, and must stay untouched: this ADR interprets the frozen text, it does not amend it.
No code changes. No schema, no migration, `SCHEMA_VERSION` untouched.

Out of scope, recorded so it is not lost: `backtest/engine.py` overwrites `position.entry_price` on
each buy fill rather than maintaining a quantity-weighted average while `position.quantity`
accumulates. Unreachable for both ported strategies, which take a single entry; immediate for any
legged port, which would price the whole position at its final leg. I-1 makes legged ports thinkable,
so this becomes reachable the moment one is attempted. It is filed as its own issue and should block
that port, not this ADR.
