# QQQ certified-data integrity evaluation — 2026-08-26

## Question

Can the public certification and frozen QQQ packet CLIs still certify legitimate inputs
while refusing the corporate-action false-positive reported in the non-author review?

## Assumptions checked before implementation

1. IBKR `TRADES` history does not provide dividend-adjusted prices. Confirmed by the
   [official historical-bars documentation](https://interactivebrokers.github.io/tws-api/historical_bars.html):
   it distinguishes split-only `TRADES` adjustment from split-and-dividend
   `ADJUSTED_LAST`.
2. An all-empty multi-decade panel for `QQQ,SPY,IWM,DIA,GLD,TLT` is not plausible under
   the frozen campaign identity. Official sponsor materials document distributions for
   [SPY](https://www.ssga.com/library-content/products/fund-data/etfs/us/distribution/SPDR_Dividend_Distribution_Schedule.pdf),
   [TLT](https://www.ishares.com/us/literature/fact-sheet/tlt-ishares-20-year-treasury-bond-etf-fund-fact-sheet-en-us.pdf),
   and [QQQ](https://www.invesco.com/content/dam/invesco/hk/en/pdf/INV-Composition-of-dividend-payments-Invesco-QQQ-ETF.pdf).
3. A claimed sample cannot exceed the distinct supplied events it purports to sample.
   This is an identity/coherence rule, not a claim that code can prove external-source
   completeness.

## Method

A disposable fixture root under `/tmp` drove the production scripts in fresh processes:

```text
.venv/bin/python scripts/certify_dataset.py certify ...
.venv/bin/python scripts/prepare_qqq_certified_data.py <subcommand> ...
```

Sixteen CLI invocations plus one cross-run digest comparison exercised synthetic bars and
actions. No repository data, owner data, credential, gateway, account, holdout, or trial was
opened. The fixtures measured gate behavior only; they do not establish provider completeness
or certify the QQQ corpus.

## Results

| Scenario | Expected | Observed |
|---|---:|---:|
| Generic positive sample count equals one supplied event | certify | certify |
| Generic positive sample count exceeds supplied events | refuse | `ATTESTATION_EXCEEDS_ACTIONS` |
| Generic positive attestation over empty panel | refuse | `EMPTY_ACTION_PANEL` |
| Reviewed no-action declaration over exact windows | certify | certify |
| Reviewed no-action declaration over different windows | refuse | `NO_ACTION_ATTESTATION_MISMATCH` |
| Reviewed no-action declaration contradicted by an event | refuse | `NO_ACTION_ATTESTATION_CONTRADICTED` |
| Duplicate generic event | refuse | `DUPLICATE_CORPORATE_ACTION` |
| Missing independent attestation | refuse | `MISSING_ATTESTATION` |
| Two events in forward order | certify | certify |
| Same two events in reverse order | certify | certify |
| Forward/reverse semantic certification digest | equal | equal |
| Frozen QQQ packet: 12 sampled of 15 supplied events | emit declaration | emitted |
| Frozen QQQ packet: 16 sampled of 15 supplied events | refuse | refused |
| Frozen QQQ packet: all six action files empty | refuse | refused |
| Frozen QQQ packet: duplicate primary event | refuse | refused |
| Frozen QQQ packet: IBKR named as action source | refuse | refused |
| Frozen QQQ packet: one action file missing | refuse | refused |

**Result: 17/17 expected checks.** The focused unit/CLI compatibility suite separately
reported 96 passed before the full repository gate.

## Residual

Counts and semantic digests make the supplied evidence internally coherent and
content-addressed. They cannot prove that a sponsor page was transcribed completely, that an
independent source itself is correct, or that a provider omitted nothing. Those remain owner
review and first-real-capture obligations recorded in ADR-0039 and R-59.
