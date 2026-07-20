# Research readiness gate — evidence contracts

This document is the operator-facing evidence contract for Chronos as a
**research-first** system. It does not authorize trading. Current validation
results are **INSUFFICIENT_EVIDENCE** (or FAIL) under frozen criteria — that is
a correct scientific outcome, not a tooling defect.

Related: [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md), [RESEARCH_REPORT.md](RESEARCH_REPORT.md),
[STRATEGY_SELECTION.md](STRATEGY_SELECTION.md), ADR-0009, ADR-0013–0015.

## Planes (hard separation)

| Plane | What it may do | Order transmit |
|-------|----------------|----------------|
| **Research** | Backtest, walk-forward, campaign, SKB, registry | **Never.** Simulated broker only. |
| **Paper** | Supervised paper orders under ADR-0007 mode lock | Only when `transmission_possible` (IBKR + PAPER + transmit + account) |
| **Live** | Real capital | **LIVE TRADING BLOCKED** unless full ADR-0009 conjunction + ten-gate stack + human authorization |

Research risk policy (`config/risk.research.yaml`) governs **simulated** risk
approvals only. It cannot enable paper or live transmission.

## Verdict semantics (first-class)

Every walk-forward / campaign cell returns exactly one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | OOS trade floor met, Sharpe bootstrap CI strictly > 0, deflated Sharpe ≥ 0.95 |
| **FAIL** | Evidence supports rejection (e.g. Sharpe CI entirely ≤ 0) |
| **INSUFFICIENT_EVIDENCE** | Sample too small, CI includes 0, or DSR undefined — **blocking default** |

`INSUFFICIENT_EVIDENCE` is a **valid success of scientific honesty**. Do not
relax frozen floors, unseal holdout, or retune criteria after seeing results to
force a PASS.

## LIVE TRADING BLOCKED

The explicit outcome token is:

```text
LIVE TRADING BLOCKED
```

- Default and research-like settings always evaluate to this outcome
  (`chronos.orders.live_block.evaluate_live_trading_block`).
- The live submission branch prefixes refusals with this token when the
  ADR-0009 conjunction is unmet.
- The research readiness assessor **always** reports `live_trading_blocked=true`
  and never grants live-review eligibility from research results alone.

## Evidence required before paper trading

All of the following must hold before starting supervised paper trading of a
candidate strategy. Automation surfaces blockers via
`chronos.research.readiness.assess_campaign_readiness`; the owner still decides.

1. **At least one** strategy×symbol walk-forward cell with verdict **PASS**
   (CI > 0, deflated Sharpe ≥ 0.95, OOS trades ≥ `min_trades`, currently 20).
2. Campaign `stage_end` strictly **before** the sealed holdout wall (`2022-01-01`).
   Holdout is never consumed by research automation (C2 guardian).
3. A deterministic **research-run manifest** with `code_commit`, `policy_hash`,
   holdout-free `data_hashes`, and `config_hash`
   (`chronos.research.manifest.manifest_from_campaign`).
4. Research risk policy used only for simulation — never as a paper/live policy.
5. Frozen selection criteria (`research/selection_manifest.json`) applied as
   written. Zero selected candidates is a valid **not ready** outcome.
6. Owner review of `docs/STRATEGY_SELECTION.md` and `docs/RESEARCH_REPORT.md`.

**Current status:** not ready for paper. No cell meets the trade floor /
PASS bar; overall evidence is insufficient.

## Evidence required before any future live-trading *review*

Paper readiness is a prerequisite. Live review eligibility is **owner-mediated**
and is never granted by research automation. Required before scheduling a review:

1. All paper evidence above met, plus a documented paper soak
   (`scripts/paper_soak_report.py`) with acceptable operational outcomes.
2. Re-validation on a trusted, uniformly-adjusted feed (preferably IBKR) with a
   **fresh, untouched** holdout window reserved.
3. Shadow gate on the production decision path with `NO_ORDERS` capability.
4. Paper transmission verified under ADR-0007 with `ALLOW_LIVE_TRADING=false`.
5. Independent review of live gate stack, kill switch, arming, single-transmit-site
   invariants still green.
6. Owner-signed Gates 4–5 in [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md).

Until then, runtime remains **LIVE TRADING BLOCKED**.

## How to produce an auditable readiness snapshot

```bash
# Campaign (dev+val only; holdout sealed) — see CLI research-campaign
python -m chronos.cli research-campaign --help

# In Python (tests drive the same functions):
from chronos.research.readiness import assess_campaign_readiness
from chronos.research.manifest import write_manifest

assessment = assess_campaign_readiness(campaign_report)
write_manifest(assessment.manifest, "research/results/readiness_manifest.json")
assert assessment.live_trading_blocked  # always True from research plane
```

## What this gate does *not* do

- Does not enable live or paper order transmission.
- Does not unseal or rewrite holdout windows.
- Does not weaken risk thresholds, kill switch, or arming.
- Does not invent PASS from insufficient samples.
