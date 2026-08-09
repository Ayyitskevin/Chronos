---
name: chronos-research-methodology
description: >-
  Load this skill BEFORE evaluating, producing, or quoting any Chronos backtest or
  research evidence. Triggers: "backtest results", "is this strategy good", "validate a
  strategy", "deflated Sharpe", "walk-forward", "bootstrap", "holdout", "trial count",
  "promotion evidence", "research campaign", "can I use this data", "overfitting",
  "sample floor", "INSUFFICIENT_EVIDENCE", "why were zero strategies selected", "re-run
  the research", "repro / replay a run", "burn a holdout", or any request to interpret
  numbers in research/results/ or docs/RESEARCH_REPORT.md. Also load it whenever a
  result LOOKS good — the whole discipline exists for exactly that moment. It defines
  the statistical evidence bar (walk-forward, DSR >= 0.95 with true trial count, block
  bootstrap CIs, frozen sample floors, burn-once holdouts, byte-identical replay) so a
  future session runs it identically and never mistakes an underpowered positive for
  validation.
---

# Chronos research methodology — the statistical evidence discipline

Verified against the repo at commit `47a8d72`, 2026-08-02. All file:line references are
to that state; re-verify volatile facts with the commands in "Provenance and
maintenance" at the end.

## 1. The evidence philosophy (binding, restated from AGENTS.md)

1. **INSUFFICIENT_EVIDENCE / NO_TRADE is a SUCCESS result.** "A correct `NO_TRADE`
   result is success when evidence is insufficient. Never weaken a gate to manufacture
   progress" (AGENTS.md:23-24). A verdict table dominated by INSUFFICIENT_EVIDENCE at
   daily-bar trade counts is the honest, expected output (ADR-0014 "Honesty bounds";
   the CLI prints this to stderr — src/chronos/cli/main.py:262-267, 322-327).
2. **Thresholds are frozen BEFORE observation.** "Freeze statistical, operational, and
   financial thresholds before observing the evidence they judge. A failed holdout
   rejects the candidate; it does not invite threshold edits" (AGENTS.md:27-28). The
   live example: selection criteria were frozen 2026-07-17T13:05Z and re-frozen
   DELIBERATELY UNCHANGED at 18:30Z before new-symbol results
   (research/selection_manifest.json:2-5).
3. **A failed holdout rejects the candidate — never invites tuning.** C6: "a final-test
   failure rejects the candidate rather than triggering re-tuning"
   (selection_manifest.json:21); Phase 3 restates it: "One untouched holdout passes
   unchanged. Failure means rejection, not tuning" (docs/VISION_COMPLETION_PLAN.md:254).
4. **Every data touch is a counted trial.** The deflated Sharpe's multiple-testing N is
   derived from a hash-chained ledger, never self-reported (src/chronos/registry/
   runs.py:120-134). Exploratory runs outside the registered paths silently understate
   N — that is multiplicity laundering, the failure this whole apparatus prevents.
5. **A good-looking result below the frozen sample floor is NOT validated.** The
   strongest cell in the repo (mean_reversion_v1 on IWM, +16.2%, PF 3.35, 12 trades) is
   explicitly documented as "the most likely to be noise" (docs/RESEARCH_REPORT.md:24-33).

## 2. The verdict machinery at a glance

```
one deterministic full-series backtest (production path, simulated broker)
  └─ OOS per-bar returns after `warmup` bars
       ├─ chopped into fixed disjoint `test_window`-bar segments ("windows")
       ├─ pooled Sharpe (per-observation, NOT annualized)
       ├─ stationary block bootstrap 95% CI on Sharpe   (§4)
       ├─ PSR, then DSR vs expected-max-of-N-trials     (§3)
       │    N = registered trials from the ledger + 1
       │    V = pvariance of per-window OOS Sharpes
       └─ VERDICT (blocking default INSUFFICIENT_EVIDENCE):
            trades < min_trades floor      → INSUFFICIENT_EVIDENCE
            CI undefined                   → INSUFFICIENT_EVIDENCE
            CI upper <= 0                  → FAIL  (positive rejection)
            CI includes 0                  → INSUFFICIENT_EVIDENCE
            DSR is None                    → INSUFFICIENT_EVIDENCE
            DSR < 0.95                     → INSUFFICIENT_EVIDENCE
            trades >= floor AND CI lower > 0 AND DSR >= 0.95 → PASS
```

Verdict ladder: src/chronos/research/walkforward.py:260-293. Low sample can NEVER pass:
`walk_forward` rejects `min_trades < 1` up front (walkforward.py:102-105) and `_verdict`
clamps the floor to >= 1 again (walkforward.py:268). All statistics are stdlib-only,
seeded, deterministic — no numpy/scipy (src/chronos/research/stats.py:1-12). Design
authority: docs/adr/ADR-0014 (status "proposed (design-review pending)" at its line 3,
but the code is merged and tested — tests/unit/test_research_stats.py,
test_walkforward.py, test_campaign.py).

## 3. Deflated Sharpe Ratio (Bailey & López de Prado), as implemented

Source: "The Deflated Sharpe Ratio" (Bailey & López de Prado), linked from
docs/VISION_COMPLETION_PLAN.md:373. Implementation in src/chronos/research/stats.py:

| Piece | Exact implementation | Where |
|---|---|---|
| Moments | population mean/std/skew/non-excess kurtosis; None if n < 2 | stats.py:33-46 |
| Sharpe | per-observation mean/std, **no annualization** | stats.py:49-55 |
| PSR | `Φ((SR − SR*)·√(n−1) / √(1 − γ3·SR + ((γ4−1)/4)·SR²))`, Φ via `math.erf` | stats.py:130-146 |
| E[max SR of N] | `√V · [(1−γ)·Z⁻¹(1−1/N) + γ·Z⁻¹(1−1/(N·e))]`, γ = 0.5772156649015329 | stats.py:149-158, :21 |
| Z⁻¹ | Acklam's rational approximation, relative error < 1.2e-9 (disclosed) | stats.py:71-127 |
| DSR | PSR with benchmark = E[max SR of N trials] | stats.py:161-175 |
| Pass bar | `_DSR_PASS_THRESHOLD = 0.95` | walkforward.py:41, :288 |

**The two inputs that make it honest:**

- **N (trial count)** = `trial_count(ledger, strategy_id=strategy_id) + 1`
  (walkforward.py:134) — cumulative per-strategy data-touching runs derived from the
  hash-chained registry ledger, plus the current run. `trial_count` counts recorded
  `experiment_run` records with `touched_data=True` (runs.py:120-134). Self-reported
  counts are refused by construction: N is "a property of the ledger, never a human
  claim" (ADR-0013 §5), and `register_run` fails closed on null provenance — an
  empty/"unknown" commit, empty criteria_ref, or empty strategy_id raises
  (runs.py:101-106).
- **V (cross-trial Sharpe variance)** = `statistics.pvariance(window_sharpes)`, the
  population variance of the per-window OOS Sharpes, requiring >= 2 defined window
  Sharpes (walkforward.py:135-139).

**Honesty rule** (`_deflated_or_none`, walkforward.py:187-224): if N <= 1, DSR = PSR
against a 0 benchmark (no deflation needed). If N > 1 and V is unavailable or <= 0,
DSR = **None** — never an undeflated PSR wearing the DSR name — and None routes to a
blocking INSUFFICIENT_EVIDENCE. Disclosed limit: cross-session trials contribute to N
but not to V (ADR-0014 "Honesty bounds").

## 4. Stationary block bootstrap

`block_bootstrap_ci(returns, statistic, *, block_size, n_resamples, seed, alpha=0.05)`
at stats.py:178-216, applied to the pooled OOS **bar-return** series:

- **Stationary blocks**: geometric block lengths with mean `block_size` — continuation
  ends with probability `1/block_size` per step (stats.py:197, :206-207), circular wrap
  via `returns[index % n]` (stats.py:204). Deliberately NOT IID trade-level resampling —
  it preserves autocorrelation (stats.py:10-12).
- **Seeded and deterministic**: `random.Random(seed)` (stats.py:196).
- **Output**: a percentile CI (stats.py:213-216), by default the 95% CI on the pooled
  OOS Sharpe.
- **Defaults used in anger**: `block_size=20`, `n_resamples=1000`, `seed=0`,
  `alpha=0.05` (walkforward.py:95-96; cli/main.py:483-485, :505-507).

**How the CI gates** (walkforward.py:274-282): CI undefined → INSUFFICIENT_EVIDENCE;
CI upper <= 0 → FAIL (the only positive rejection); CI straddles 0 →
INSUFFICIENT_EVIDENCE; PASS requires CI lower > 0 (plus the floor and DSR >= 0.95).

## 5. Walk-forward: scheme, defaults, invocation

**Scheme** (src/chronos/research/walkforward.py:78-184): one deterministic full-series
backtest through the production path (portfolio sizer → risk engine → execution engine →
simulated broker, walkforward.py:107-113); OOS = per-bar equity returns after `warmup`
bars (:116-118); OOS chopped into fixed disjoint `test_window`-bar segments defined up
front (`_windows`, :233-257); pooled trades = closed trades whose entry_date falls in
the OOS span (:121-125). For the current fixed-rule strategies a "fold" is a causal
replay — nothing is re-fit, so purged CV does not bind (`requires_purging` returns False
for fixed-rule replays, src/chronos/research/purged_cv.py:44-47; the report records
`purged_cv: "not applicable (fixed-rule replay)"`, walkforward.py:181). `purged_kfold`
exists and is tested for future fitted workflows (purged_cv.py:25-41).

**Defaults** when CLI flags are omitted (src/chronos/config/settings.py:68-70):
`walkforward_test_window_bars=63`, `walkforward_warmup_bars=252`,
`walkforward_min_trades=20`. Note the campaign uses `test_window=252`, not 63 (§6).

**Invocation** (parser at cli/main.py:467-486; run from the repo root):

```bash
.venv/bin/python -m chronos.cli research walk-forward \
  --strategy regime_trend_v1 --symbol QQQ \
  --data-dir research/data/raw \
  --policy config/risk.research.yaml \
  --ledger research/registry/registry.jsonl \
  [--test-window 63 --warmup 252 --min-trades 20 \
   --block-size 20 --n-resamples 1000 --seed 0 \
   --cash 3000.0 --slippage-bps 2.0]
```

- **TRAP — the default policy is deny-all.** `--policy` defaults to
  `config/risk.example.yaml` (cli/main.py:474), which approves nothing → 0 trades → a
  vacuous INSUFFICIENT_EVIDENCE. That is a config fact, not a research result
  (ADR-0015). Pass `--policy config/risk.research.yaml` (the vetted `research-1`
  profile) for a non-vacuous run.
- **Every invocation appends one VALIDATION trial to the shared ledger** and therefore
  deflates every future DSR for that strategy. Run it for selection-relevant work, not
  as a smoke test — the machinery's smoke test is
  `pytest tests/unit/test_walkforward.py`. NEVER point `--ledger` at a throwaway path
  to dodge the count: that is undercounting (§8, trap 2).
- **Outputs**: a JSON `WalkForwardReport` to stdout (windows, pooled bars/trades,
  sharpe, CI, PSR, DSR, trial_count, verdict, reason — walkforward.py:60-75) plus a
  stderr banner stating INSUFFICIENT_EVIDENCE is expected (cli/main.py:262-267). A
  simulated halt file is written to cwd-relative
  `data/walkforward_halt_<strategy>_<SYMBOL>.json` (cli/main.py:239-241) — it is
  gitignored run debris, not the platform halt.
- Trial registration ordering: the trial is registered LAST, after every statistic and
  the verdict succeed, so a mid-statistics exception leaves no orphan trial inflating a
  sibling's N; numerically identical to register-then-read (walkforward.py:130-134,
  :157-168). See §7 for the FUTURE Phase-3 regime that flips this.

## 6. Campaign (ADR-0015): the (strategy × symbol) grid

`run_campaign` (src/chronos/research/campaign.py:148-303) runs the walk-forward over a
grid on the dev+val span only:

- **Holdout wall**: `FINAL_START = date(2022, 1, 1)` (campaign.py:47);
  `DEFAULT_STAGE_END = "2021-12-31"` (:48). A non-ISO or holdout-reaching `stage_end`
  is refused up front (:169-174); each series is sliced to the cutoff before any use
  (:205) and the per-cell provenance fingerprint hashes only the sliced bars
  (:107-116) — no holdout byte enters a statistic or a recorded hash.
- **Fail-closed provenance**: requires a resolvable git commit; otherwise refuses
  (:185-190).
- **No silent skips**: symbols too short for `warmup + 2*test_window` bars are excluded
  with a recorded reason (:193-215); a cell that raises is recorded in `errored` and
  registers NO trial (:261-265).
- **Deterministic order, cumulative N**: fixed strategy-major, symbol-minor sorted
  iteration so each cell's cumulative trial count is deterministic; each cell's DSR is
  deflated against the running cumulative N; **re-running the campaign deflates
  further, by design** (:217-226).
- **Defaults**: warmup 252, test_window 252, min_trades 20, seed 0, block_size 20,
  n_resamples 1000 (campaign.py:159-164; cli/main.py:502-507).

```bash
.venv/bin/python -m chronos.cli research campaign \
  [--strategies regime_trend_v1,mean_reversion_v1] \
  [--symbols SPY,QQQ,IWM,DIA,GLD,TLT] \
  [--policy config/risk.research.yaml] \  # campaign default IS the research policy
  [--stage-end 2021-12-31] [--ledger research/registry/registry.jsonl]
```

Writes `research/results/campaign_<stage-end>.json` (a serialized `CampaignReport`:
policy version/hash, stage_end, seed, code_commit, warmup/test_window/min_trades,
per-cell reports, verdict table, excluded/errored with reasons — campaign.py:75-89,
cli/main.py:299-302) plus a human verdict table to stdout. As of 2026-08-02 **no
campaign_*.json exists in research/results/** and the ledger has 0 records — the
ADR-0015 machinery is built and tested, but no registered campaign run has been
committed (`ls research/results/` → research_all.json, research_dev.json,
research_val.json only).

## 7. Trial registry + holdout guardian (ADR-0013) — and the one trap that bites hardest

### Mechanics (all verified in code)

- **Hash-chained ledger + out-of-band head anchor**
  (src/chronos/registry/ledger.py). Record kinds: `experiment_run`, `holdout_unlock`,
  `holdout_consume` (:37-39). Built on the fsync'd hash-chained `AuditLog`
  (record hash = sha256("{seq}|{at}|{kind}|{payload_json}|{prev}"), ADR-0013 §1). Every
  append also writes `registry.head.json` {count, last_hash}, owner-only perms
  (:72-84). `verify()` requires chain intact AND anchor match — detects tail truncation
  (deleting the trailing consume line would "un-burn" a window), whole-file deletion,
  and rollback (:111-133). Honest residual: an actor rewriting BOTH ledger and anchor
  consistently resets state undetected — tamper-EVIDENT, not tamper-proof
  (ledger.py:13-17; docs/limitations.md:131-138).
- **Concurrency**: exclusive `fcntl.flock` around read-verify-append critical sections
  (ledger.py:42-54) — two processes cannot both consume one grant.
- **Counting**: completed data-touching runs count via `trial_count` (runs.py:120-134);
  a walk-forward cell that errors registers nothing (§5). NOTE the two regimes:
  - **CURRENT (shipped)**: register-LAST-on-success, deliberately, so failed cells
    don't inflate a sibling's N (walkforward.py:157-168; ADR-0015).
  - **FUTURE (Phase 3, NOT yet implemented)**: one brokered research reader where
    "every data touch writes `trial_started` before bytes are returned; completed and
    failed trials both count", plus **order-invariant campaign scoring** using one
    final global trial count (docs/VISION_COMPLETION_PLAN.md:222-226). ~~No
    `trial_started` token exists in src/ today.~~ When that lands, both completed AND
    failed trials must count — do NOT port the current register-on-success semantics
    into the brokered reader, and do not claim order-invariance for today's campaign
    (today's per-cell N is running-cumulative and order-fixed instead).

    > **Correction (2026-08-09).** The struck sentence was already false when the
    > Five-Tool slice merged and is now triply so. `trial_started` exists
    > (`src/chronos/research/five_tool_trials.py`, `KIND_TRIAL_STARTED`), the Five-Tool
    > path **registers in the canonical ADR-0013 registry before it reads** and counts
    > completed *and* failed attempts, its reader can be a **certified** one —
    > `chronos.research.five_tool.certified_reader.CertifiedDatasetReader`, digest-locked
    > to a certification manifest, whose read is re-checked against its pre-read
    > attestation so `data_hashes.certified_reader` is a proven claim — and every attempt
    > that opens data now persists a **content-addressed replay artifact**
    > (`chronos.research.five_tool.replay`) written before its terminal record and named
    > by it, so a completed trial that cannot be re-executed byte-for-byte cannot exist;
    > `replay_trial` refuses on any byte divergence and names which axis moved, and
    > registers no trial while doing so (same stance as `repro produce/replay`, §9).
    > Exercised: `tests/safety/test_five_tool_registry_exercised.py`,
    > `tests/safety/test_five_tool_certified_reader_exercised.py`,
    > `tests/safety/test_five_tool_replay_exercised.py`. **Still true and still
    > load-bearing:** this is the Five-Tool path only — `walkforward.py`/`campaign.py`
    > keep register-last semantics and running-cumulative, order-fixed N; the registry
    > ships **empty**; **no dataset has been certified** (no `CERTIFICATION.json` under
    > `research/`); **no replay artifact exists** under `research/`; and the campaign
    > manifest stays blocked on ~~replay artifacts and~~ **owner evidence alone** — the
    > Phase-0 freezes only the owner can make. So Phase-3 multiplicity and order-invariant
    > scoring are still not available — the plumbing arrived, the evidence did not.
- **Holdout guardian** (src/chronos/registry/holdout_guardian.py): unlock requires the
  owner-typed module-constant phrase `REQUIRED_HOLDOUT_UNLOCK_PHRASE = "I ACCEPT
  BURNING THIS HOLDOUT"` (:40), compared with `hmac.compare_digest` (:54-57), never
  stored/logged/echoed; the CLI reads it only from env
  `CHRONOS_HOLDOUT_UNLOCK_PHRASE`, never a flag (cli/main.py:809-816).
  `request_unlock` (:119-175) verifies the ledger FIRST (fails closed on a broken
  chain), requires the window declared in HOLDOUTS.json, not already burned, no
  outstanding grant, and budget > 0; grants a single-use `unlock_id` with TTL expiry
  (default 15 min — settings.py:62). `mediated_holdout_read` (:178-215) is the ONLY
  sanctioned unmasking path and **appends the `holdout_consume` (burn) BEFORE returning
  any data** (:209-213) — the fail-safe direction. Budget: 1 credit per 20
  option-capture snapshot dates, max 2 outstanding (budget.py:24-67;
  settings.py:63-64). No shipped automated path can invoke the unlock
  (tests/safety/test_registry_no_automated_unlock.py, test_single_unmask_site.py).

### CURRENT STATE 2026-08-02 — read this twice

```
$ .venv/bin/python -m chronos.cli registry stats
{"records": 0, "trials": 0, "burned_windows": [], "chain_ok": true, "chain_detail": "empty ledger"}
$ .venv/bin/python -m chronos.cli holdout status
{"declared_windows": [], "burned_windows": [], "accrued_sessions": 0,
 "available_unlock_budget": 0, "chain_ok": true, "chain_detail": "empty ledger"}
```

**The ledger ships EMPTY** (`research/registry/` does not even exist on disk;
`research/data/history/HOLDOUTS.json` declares zero windows) — **yet the QQQ
2022-01-03..2024-01-10 holdout WAS burned.** The burn predates the registry, so the
ledger does not know about it. The ONLY records of the burn are documentary:
docs/RESEARCH_REPORT.md:184-214 (corrected disclosure with the final-window numbers),
docs/VISION_COMPLETION_PLAN.md:61-62 ("one QQQ holdout was consumed and must not be
treated as clean"), ADR-0015, and the 5 `tag=="final"` runs in
research/results/research_all.json (QQQ only; regime_trend_v1: 3 trades, +16.0%,
PF 6.56, low_sample=true).

**A session that trusts the empty ledger will re-treat QQQ 2022+ as clean data. Do
not.** Any QQQ re-test must treat 2022-2024 as seen and reserve a NEW window
(post-2024-01 data or fresh IBKR history). How the burn happened: the harness's
then-default `--stage all` implied the final stage; today `--stage all` runs dev+val
only and the final stage requires an explicit `--stage final`
(scripts/run_research.py:21-23, :222-227, :259-260), and the campaign refuses
`stage_end >= 2022-01-01` (campaign.py:169-174). Do not "fix" or bypass either guard.
Also: the 170 committed Phase 6 runs are not in the ledger either — trial counts start
honest only from the first registered run. **Whether/how to backfill the registry with
the historical burn and the Phase 6 trials is OPEN work** — route it through
chronos-change-control (owner decision + ADR territory); never hand-edit
`research/registry/registry.jsonl` (the head anchor makes truncation detectable, and a
hand-built history would be a fabricated evidence record).

## 8. The TWO sample floors — do not confuse them (a known confusion)

| | Frozen selection floor (CURRENT) | Phase-3 promotion gate (FUTURE, stricter) |
|---|---|---|
| Where frozen | research/selection_manifest.json:19 (C4), frozen 2026-07-17 before validation results | docs/VISION_COMPLETION_PLAN.md:240-254, to be frozen in Phase 0 before observation |
| Sample floor | **>= 20 closed trades** on the validation window (with PF >= 1.1, plus 2x commission and >= 10 bps slippage stress) | **max(power-required N, 100 OOS closed trades)** (:242) |
| Statistical bar | C1-C6 conjunction; walk-forward machinery adds Sharpe-CI lower > 0 and DSR >= 0.95 | DSR >= 0.95; **FWER or FDR q <= 0.05; PBO <= 10%** (:245-247); net-expectancy & benchmark-alpha 95% lower bounds > 0 after ALL costs (:243-244) |
| Robustness | C5 sensitivity majority net-positive | >= 3 instruments AND 2 materially different regimes (:248); positive after removing best trade AND best month, under doubled commissions + stressed slippage (:248-249); parameter plateau, not isolated optimum (:250-251); concentration bounds (:252-253); one untouched holdout passes unchanged (:254) |
| Applies when | Judging the completed Phase 6 campaign and today's walk-forward/campaign runs (min_trades default 20 — settings.py:70) | Any FUTURE promotion of a strategy toward shadow/paper/live |
| Status today | Best cell regime_trend_v1/QQQ = **18 closed trades → correctly UNSELECTED** (docs/STRATEGY_SELECTION.md:20-24; docs/RESEARCH_REPORT.md:8-11, :143-148) | Not yet exercised; power analysis may RAISE a sample requirement, never lower it after results (VISION_COMPLETION_PLAN.md:260-263) |

They do not conflict: no candidate clears even the 20-trade floor today (max 18 on QQQ,
12 on the new symbols — verified against research/results/research_val.json). Quoting
the 20-trade floor as "the promotion bar" overstates readiness; quoting 18/20 as
"nearly validated" is exactly the selection bias the freeze exists to prevent. Zero
selected candidates is the current, correct answer (STRATEGY_SELECTION.md:8-28).

## 9. Campaign reproducibility (repro produce / replay / compare)

Doc: docs/RESEARCH_REPRODUCIBILITY.md. Code: src/chronos/research/repro.py. Verified
LIVE this session (2026-08-02, commit 47a8d72): produce → replay gave **byte-identical
manifest_fingerprint AND output_fingerprint**, and compare returned
`{"status": "pass", "ok": true, "reasons": ["match"]}` with exit 0.

```bash
# produce: run a deterministic named-backtest slice + write a manifest
.venv/bin/python -m chronos.cli research repro produce --run-dir /tmp/run-a \
  --strategy baseline_buy_hold --symbol SPY --data-dir research/data/raw \
  --policy config/risk.research.yaml --seed 0 --slippage-bps 0 \
  --date-start 2010-01-04 --date-end 2015-12-31
# replay: recompute from the manifest into a new bundle
.venv/bin/python -m chronos.cli research repro replay \
  --manifest /tmp/run-a/manifest.json --run-dir /tmp/run-b
# compare: precise pass/fail reasons; exit 0 on match
.venv/bin/python -m chronos.cli research repro compare \
  --expected /tmp/run-a/manifest.json --actual /tmp/run-b/manifest.json
```

- **Manifest** (`research-run-manifest-v1`, repro.py:25, required fields :43-58):
  code_commit, timezone (UTC-only; local zones rejected — :131-147), seed, strategy
  id/version, config + config_hash, policy version/hash, datasets with content sha256,
  date_window, outputs, output_fingerprint, manifest_fingerprint. Secret-like keys are
  redacted (`_SECRET_KEY_RE` → `[REDACTED]`, :34-40, :112-128). Run bundle on disk:
  config.json, output.json, manifest.json, halt.json.
- **Fail-closed**: produce from a non-git cwd returns
  `{"ok": false, "reason": "incomplete_manifest", "detail": "...code_commit must be a
  resolvable git SHA; run from a git checkout"}` (verified live).
- **Compare reason codes** (repro.py:68-84): match, incomplete_manifest,
  unsupported_legacy, missing_data, checksum_drift, config_drift, seed_drift,
  timezone_drift, date_window_drift, strategy_drift, commit_drift, nondeterminism,
  output_drift, policy_drift.
- **Scope limit**: this replays the named-backtest slice
  (`run_named_backtest`) only — full campaign JSONs are not auto-replayed by this CLI.
  Full-campaign byte-identical replay from one manifest is a Phase-3 deliverable, and
  under it **criteria/data/code/model changes invalidate the campaign**
  (VISION_COMPLETION_PLAN.md:227-228). Apply that rule today by hand: if the criteria
  file, the data files' hashes, or the code commit changed, prior campaign numbers are
  context, not current evidence.
- repro produce/replay registers NO trial (it is a reproducibility probe, not a
  selection run) — do not use it to sneak selection-relevant sweeps past the ledger.
- **A second, separate replay path exists as of 2026-08-09**, and it is per-*trial* rather
  than per-run: `chronos.research.five_tool_trials.replay_trial` re-executes one recorded
  Five-Tool attempt from the content-addressed artifact that attempt persisted
  (`chronos.research.five_tool.replay`), refusing on any byte divergence and naming which
  axis moved — inputs, configuration, certification, outcome, or outputs. Its reason codes
  are deliberately shaped like `CompareReason` above; the two are **separate code** and must
  stay so (this one is import-isolated from the strategy platform). It also registers no
  trial. It does **not** replace the scope limit in this section: full-campaign
  byte-identical replay from one manifest is still a Phase-3 deliverable, and no Five-Tool
  campaign has run, so no artifact exists under `research/`.

## 10. Running research end-to-end TODAY

Setup per README: Python 3.12+, `python3 -m venv .venv`,
`.venv/bin/pip install -e '.[dev]'`. Every research CLI prints a mode banner (MODE:
BACKTEST, CAPABILITY: SIMULATED_ONLY, TRADING HALTED reason NEVER_ARMED) — research is
structurally read-only w.r.t. trading (walkforward imports no order/broker module;
tests/safety/test_research_isolation.py).

| Task | Command | Artifacts | Counts a trial? |
|---|---|---|---|
| Historical Phase 6 harness | `.venv/bin/python scripts/run_research.py [--stage dev\|val\|final\|all]` ("all" = dev+val ONLY) | `research/results/research_<stage>.json` (run_research.py:344-346) | **NO — predates the registry** |
| Registered walk-forward | §5 command | report to stdout; `data/walkforward_halt_*.json` | YES (one per run) |
| Registered campaign | §6 command | `research/results/campaign_<stage-end>.json` | YES (one per cell) |
| Repro produce/replay/compare | §9 commands | run bundle in `--run-dir` | no |
| Five-Tool `replay_trial` (§9, added 2026-08-09) | Python API only — no CLI, because public Five-Tool execution is still blocked | reads an existing replay artifact; writes nothing | no |
| Inspect registry/holdout | `... -m chronos.cli registry stats` / `registry verify` / `holdout status` | JSON to stdout | no (read-only) |
| Corpus integrity | `... -m chronos.cli verify-corpus` (→ "verified 42 scripts, 0 failures") | stdout | no |
| Holdout unlock (owner-only) | `CHRONOS_HOLDOUT_UNLOCK_PHRASE='…' ... -m chronos.cli holdout unlock --window W --reason R` | ledger records | burn event |

Phase 6 harness details: hardcoded partitions dev ..2017-12-31, val
2018-01-01..2021-12-31, final 2022-01-01.. (run_research.py:62-64); base costs USD
3,000 cash + 2 bps/side slippage (:97) with engine commission defaults 0.005 USD/share
min 1.00 USD (src/chronos/backtest/engine.py:69-70); slippage stress 5/10/25 bps, 2x
commission stress, frozen SENSITIVITY grids (10 variants regime_trend, 8
mean_reversion — :179-202). Today the holdout unlock MUST fail closed regardless of
phrase: zero declared windows and zero budget. All runs are against the fixture CSVs in
`research/data/raw/` (§11) — **fixture-data conclusions are research-grade, not
production evidence**.

Metrics note: every metrics dict carries `low_sample: bool`, true when closed trades
< 30 (src/chronos/backtest/metrics.py:43, :137). Never quote PF/Sharpe from a
low_sample cell without the flag.

## 11. Data honesty — what the corpus can and cannot support

The research corpus is **heterogeneous** (RISK_REGISTER.md R-08, OPEN/ACCEPTED for
research; provenance in research/data/raw/MANIFEST.json, verified):

| File | Rows | Range | Adjustment / fidelity |
|---|---|---|---|
| SPY.csv | 5000 | 2000-01-03..**2019-11-14** | UNADJUSTED, byte-exact |
| QQQ.csv | 6087 | 1999-11-01..**2024-01-10** | unadjusted OHLC + adj_close, byte-exact |
| IWM.csv | 757 | 2019-01-02..2021-12-31 | dividend-ADJUSTED, transcribed, 2-decimal |
| GLD.csv | 757 | 2019-01-02..2021-12-31 | effectively nominal, transcribed, 2-decimal |
| TLT.csv | 757 | 2019-01-02..2021-12-31 | dividend-ADJUSTED (heavily), transcribed, 2-decimal |
| DIA | — | — | NOT acquired — excluded with a recorded reason, not fabricated |

Consequences (selection_manifest.json:33-35):
- **Within-symbol judgments only.** Adjusted vs unadjusted series differ in level and
  total return; cross-symbol absolute-return comparisons are NOT apples-to-apples. Each
  symbol is judged against its OWN baselines on its OWN series.
- IWM/GLD/TLT cover 2019-2021 only → **validation-window-only**; they can never reach a
  final test. SPY ends 2019-11-14 → dev + partial validation only.
- The go-forward histdata store (`research/data/history/`) and the options snapshot
  store ship **EMPTY** (only HOLDOUTS.json with zero windows + README). A re-run
  against production-source (IBKR) data before promotion is owner-gated future work
  (R-08) — no real gateway has ever been connected in this project's history.
- The holdout embargo is a default-masked accessor, not a filesystem wall: direct reads
  of `research/data/raw/*.csv` bypass it and QQQ.csv contains post-2022 bytes
  (docs/limitations.md:103-105). Discipline, not the OS, is the wall.

## 12. Traps, ranked (each with its guard)

1. **Treating QQQ 2022-2024 as a clean holdout because the ledger is empty.** §7. The
   documentary record is the only guard; reserve a NEW window for any QQQ re-test.
2. **Undercounting trials.** Anything selection-relevant run via `chronos backtest`,
   `run_named_backtest`, repro produce, or scripts/run_research.py registers NOTHING.
   Run selection-relevant work ONLY through `research walk-forward` / `research
   campaign` with the shared ledger; never hand-edit registry.jsonl; never point
   `--ledger` at a scratch path to keep N small.
3. **Quoting an underpowered positive as validated.** mean_reversion_v1/IWM (+16.2%,
   PF 3.35, 12 trades) is best-of-ten-cells noise-candidate by the frozen
   multiple-testing guard (RESEARCH_REPORT.md:24-33, :163-171); regime_trend_v1's
   final-window PF 6.56 sits on 3 trades. `low_sample` flags < 30 trades; the verdict
   machinery blocks; a PASS at daily frequency "would itself warrant scrutiny"
   (ADR-0015).
4. **Editing a frozen threshold after seeing results.** The 18-vs-20 near-miss is
   exactly the case the floor exists for (STRATEGY_SELECTION.md:20-24). Thresholds may
   be raised by power analysis, never lowered after results
   (VISION_COMPLETION_PLAN.md:260-263).
5. **Running the walk-forward under the default deny-all policy and misreading the
   zero-trade INSUFFICIENT_EVIDENCE as a research result.** Pass
   `--policy config/risk.research.yaml` (§5).
6. **Treating fixture-data conclusions as production evidence.** §11: heterogeneous
   public-mirror CSVs, R-08 caveat, owner-gated IBKR re-run before promotion.
7. **Cross-symbol return comparisons** on mixed adjusted/unadjusted series (§11).
8. **Trade counts are cap-dependent** (disclosed): wide research caps give QQQ
   regime_trend 18 trades; a tight USD 3,000/0.25 cap gives 7 — C4 fails either way
   (RESEARCH_REPORT.md:173-182). Quote counts with their policy.

## 13. When NOT to use this skill

| Question | Go to |
|---|---|
| Backtest ENGINE semantics (fills, ADR-0005 closed-bar rules, stop model) | chronos-architecture-contract, or the code (src/chronos/backtest/engine.py) |
| Wheel/options evidence (there is none) and options gating | chronos-wheel-and-options |
| Claim/evidence discipline in general; test-suite map; proof patterns | chronos-validation-and-qa |
| What to research next; execution order; owner-decision queue | chronos-priorities-and-roadmap |
| Owner gates, ADR discipline, document precedence | chronos-change-control |
| Env vars / config surface (settings knobs live there) | chronos-config-and-flags |

## Provenance and maintenance

Written 2026-08-02 against commit `47a8d72` on branch
`claude/chronos-skills-library-bfbj29`. Volatile facts and their one-line re-checks
(all read-only):

| Volatile fact | Re-verify with |
|---|---|
| DSR threshold 0.95; min_trades floor semantics | `grep -n "_DSR_PASS_THRESHOLD\|min_trades" src/chronos/research/walkforward.py` |
| Walk-forward defaults 63/252/20 | `grep -n "walkforward_" src/chronos/config/settings.py` |
| Bootstrap defaults 20/1000/0 | `grep -n "block_size\|n_resamples\|seed" src/chronos/cli/main.py \| sed -n '1,8p'` |
| Holdout wall 2022-01-01; campaign defaults | `sed -n '43,48p;158,165p' src/chronos/research/campaign.py` |
| Ledger still empty / burned windows | `.venv/bin/python -m chronos.cli registry stats && .venv/bin/python -m chronos.cli holdout status` |
| Zero declared holdout windows | `cat research/data/history/HOLDOUTS.json` |
| No campaign output committed yet | `ls research/results/` |
| C4 floor 20 + freeze timestamps | `sed -n '2,5p;19p' research/selection_manifest.json` |
| Phase-3 gates (100-trade floor, FWER/FDR, PBO) | `grep -n -A8 "declared holdouts that are inaccessible" docs/VISION_COMPLETION_PLAN.md` |
| QQQ burn disclosure unchanged | `sed -n '184,214p' docs/RESEARCH_REPORT.md` |
| `--stage all` still excludes final | `sed -n '19,23p;259,260p' scripts/run_research.py` |
| Unlock phrase mechanics | `grep -n "REQUIRED_HOLDOUT_UNLOCK_PHRASE\|compare_digest" src/chronos/registry/holdout_guardian.py` |
| ~~trial_started still unimplemented (Phase 3)~~ **corrected 2026-08-09: it exists on the Five-Tool path only** (§7 correction note) | `grep -rn "trial_started" src/ docs/VISION_COMPLETION_PLAN.md` |
| No dataset is certified; the certified reader exists but certifies nothing | `ls src/chronos/research/five_tool/certified_reader.py`; `find research -name CERTIFICATION.json` (→ empty) |
| Repro round-trip still byte-identical | §9 produce/replay/compare against a /tmp run-dir; expect `"reasons": ["match"]` |
| Data corpus unchanged | `python3 -c "import json;m=json.load(open('research/data/raw/MANIFEST.json'));print({k:v['row_count'] for k,v in m['files'].items()})"` |

If any re-check disagrees with this skill, the repo wins — update this file and note the
drift, per AGENTS.md document precedence (current executable facts outrank this skill).
