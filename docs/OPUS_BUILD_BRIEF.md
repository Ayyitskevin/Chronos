# Chronos — Autonomous Continuation Brief (for Claude Opus 4.8)

> **ARCHIVED (2026-08-01).** This brief contains point-in-time branch, test-count, and
> capability claims that are no longer current. Agents must start with `../AGENTS.md` and
> [VISION_COMPLETION_PLAN.md](VISION_COMPLETION_PLAN.md), then verify live repository state.
> Preserve this file for historical rationale; do not use it as task or completion
> authority.

You are taking over **Chronos**: a deterministic, safety-first trading
research platform for Interactive Brokers, built from the owner's Pine
Script corpus, targeting an eventual ~USD 3,000 account. A complete first
build already exists on this branch — CI green, 1,158 tests passing, seven
independent adversarial reviews performed and remediated. **Your job is to
carry it to completion, not to rebuild it.**

This document was written by the session that built the system. It encodes
what that build learned so you spend your effort on the frontier, not on
rediscovery. It supersedes any earlier draft of this file. Read it fully
before your first tool call.

---

## 1. The one paragraph that governs everything

Optimize in this order: capital preservation → correctness → risk
containment → reproducibility → parity → resilience → auditability →
statistical robustness → maintainability → performance → aesthetics. The
first build's research concluded, honestly, that **zero strategies currently
have a demonstrated edge** — and that conclusion survived adversarial
review. "Still zero, with better evidence" is a valid final answer for your
run too. You are never being asked to make the numbers look better; you are
being asked to make the evidence stronger and the platform complete.

---

## 2. Orient before acting (every session, ~3 minutes)

```bash
git log --oneline -15 && git status
cat HANDOFF.md TASKS.md                       # current truth
python3.12 -m venv .venv 2>/dev/null; .venv/bin/python -m pip install -q -e '.[dev]'
.venv/bin/pytest -q                            # baseline: ~1158 passed, 1 skipped
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/chronos
```

**`python3.12` explicitly** — the bare `python3` here is 3.11 and the
install fails on `requires-python >= 3.12`.

If the baseline is green: skip to §6 (the work plan) and take the highest
incomplete milestone. If anything is red: fixing it *is* your first task —
never stack new work on a broken baseline. If you're somehow on a clone
missing the first build entirely, stop and re-read `TASKS.md` +
`docs/GO_LIVE_CHECKLIST.md` before assuming anything needs rebuilding.

Key map (all exists, all tested):

| Concern | Where |
|---|---|
| Pine corpus (42 scripts, SHA-256 pinned) | `research/pine/`, `research/strategy_registry.yaml` |
| Forensic audit | `research/pine_findings.json`, `docs/PINE_AUDIT.md` |
| Strategy specs + implementations | `specs/*.yaml`, `src/chronos/strategies/` |
| Backtest/replay engine | `src/chronos/backtest/`, `src/chronos/research/runner.py` |
| Risk engine (deny-by-default) | `src/chronos/risk/` |
| Execution: intents, state machine, ledgers, sim broker, IBKR paper adapter, reconciliation | `src/chronos/execution/` |
| Control: mode locks, persistent halt, promotion gates | `src/chronos/control/` |
| Research harness + frozen criteria | `scripts/run_research.py`, `research/selection_manifest.json` |
| Safety acceptance tests | `tests/safety/` (31), plus `tests/{platform_unit,parity,chaos}` |
| CLI | `python -m chronos.cli` (status, backtest, shadow-scan, halt, rearm, verify-*) |
| Wheel dashboard (older subsystem — leave intact) | `src/chronos/{broker,services,strategy,ui}` |

---

## 3. Invariants you must never break — and how to prove you didn't

These are load-bearing. After **any** change to `risk/`, `execution/`,
`control/`, or `cli/`, run `pytest tests/safety tests/chaos -q` at minimum;
full suite before every commit.

1. **Live trading is impossible in code.** `resolve_mode_lock` hard-denies
   CANARY_LIVE/LIVE and denies unrecognized modes by default; promotion to a
   live mode always appends a failing gate. Enforced by
   `tests/safety/test_safety_invariants.py::TestModeLocks` and
   `test_promotion.py`. There is no `--force` anywhere; do not add one.
2. **Paper submission requires six simultaneous conditions** (transmission
   flag, non-empty allowlist, broker-reported id on the allowlist, id
   matches `D[UF]\d{4,}`, broker-verified paper environment, PAPER mode).
   One mistyped env var must never be sufficient.
3. **Deny-by-default risk.** An all-zero `RiskPolicy` approves nothing;
   unknown YAML keys are rejected; the policy object is frozen; an internal
   engine exception becomes a denial. Strategies hold no engine reference
   and emit `StrategyProposal` objects that *cannot express an order* (no
   quantity/account/broker fields) — keep the type that way.
4. **Approvals are unforgeable in practice**: instance-token-bound, checked
   by identity in `ExecutionEngine.submit_approved`, which also re-reads the
   halt immediately before `broker.submit` (a reviewer proved the TOCTOU;
   don't reintroduce it).
5. **Fail closed, always.** Fresh deployments start HALTED
   (`NEVER_ARMED`); corrupt/missing halt or audit state reads as
   halted/raises `AuditLogCorruptionError`; ledger write failure halts
   before submission; a broker `submit()` exception → order `UNKNOWN` →
   `RECONCILIATION_REQUIRED` + halt. Restart never clears a halt; rearm
   requires an operator note.
6. **Broker evidence is the only truth.** Never infer success from a call
   returning; unknown orders/events halt; **never auto-flatten** an
   unexplained position — block and demand review instead.
7. ~~**No generative model in the runtime order path.**~~ **Superseded 2026-07-25 by
   ADR-0016 / D-16.** A model may originate runtime trading decisions, but only as a
   typed `AITradeDecision` admitted by the single deterministic ModelDecisionGateway
   under an active owner AutonomyMandate; it holds no broker object, no credentials,
   and no low-level order functions, and runs outside the broker-writing process. What
   still holds verbatim: every runtime *gate* is deterministic, versioned, and
   replayable, and the deterministic kernel keeps unconditional veto authority.
8. **No secrets in the repo, ever.** Placeholders only in `.env.example`;
   check `git diff --cached` before each push. TWS/Gateway auth belongs to
   the owner; never automate login or 2FA.
9. **Research integrity.** Selection criteria are frozen *before* results
   are computed (`research/selection_manifest.json`); the final-test window
   (2022+) is consumed **at most once**, only after criteria are frozen;
   criteria are never bent after seeing results — the first build refused to
   bend a 2-trade miss, and that refusal survived review. Any knob that
   changes a reported number gets disclosed in the report (a reviewer caught
   an undisclosed cap-widening once; that class of omission is a HIGH).
10. **Docs must match code.** Reviewers diff every claim against source.
    When behavior changes, update `docs/`, `TASKS.md`, `CHANGELOG.md`,
    `RISK_REGISTER.md`, and `docs/GO_LIVE_CHECKLIST.md` in the same commit.
    Two documents contradicting each other is a real defect here.

---

## 4. Environment playbook (earned facts — trust these first)

- **Corpus**: lives in Notion (Command Center → Trading Library → *Pine
  Quant Library — Master Index*), fetched via the Notion MCP. It contains
  **42** artifacts, not the "~77" the original brief claimed (A-01). Large
  scripts are split across inline "Part N of M" code fences on one page —
  concatenate byte-exact. Verify local integrity anytime with
  `python -m chronos.cli verify-corpus`.
- **Market data**: every finance website is 403-blocked by the egress proxy
  (yahoo, stooq, tiingo, alphavantage, polygon, nasdaq — all of them; don't
  re-probe). Real data comes from the **Hugging Face MCP** (`hf_fs`),
  server-side. Search with *short* queries ("SPY", "sp500", "ETF daily",
  "kaggle"). Prefer plain-CSV datasets; parquet shards are unreadable
  through the text-only `cat` (~80 KB/call — chunk by offset and reassemble
  byte-exact; never fabricate a row). Known-good: `mmirmomeni/spy_daily`,
  `Maxim37/timeseries-QQQ-1d-25yr`; cross-check lineage:
  `zexianli/nasdaq_data`. Everything gets a `MANIFEST.json` entry: source
  URI, sha256, row count, date range, adjusted status, validation results.
- **An `Interactive_Brokers_IBKR` MCP connector exists but is
  unauthenticated** in this environment. If the owner authorizes it (their
  claude.ai connector settings), prefer it as the historical-data source
  (read-only use only) — that directly retires the mirror-data caveat
  (RISK_REGISTER R-08). Until then it is unavailable; don't ask for tokens.
- **Fan-out**: use the Workflow tool for per-script/per-page parallel work
  (the corpus fetch and audit were each one workflow of ~42 agents).
  Workflows **resume from cache** via `resumeFromRunId` — completed agents
  replay instantly. This is what makes long autonomous runs survivable.
- **Usage limits**: a big fan-out can exhaust the session budget mid-run
  (resets 07:00 UTC). Recovery pattern that worked: checkpoint-commit
  constantly, schedule a self-wakeup (`send_later`), resume the workflow
  from its run id. Plan around the limit; don't fight it.
- **CI reads misleading**: `pull_request_read get_status` reports `pending,
  total_count: 0` even when green. The truth is in
  `actions_list list_workflow_runs` (filter by branch, read `conclusion`).
  That listing can exceed the token cap — parse the saved file with a
  `python3 -c` one-liner or inside a subagent.
- **Gates** (mirror CI exactly): `ruff check .`, `ruff format --check .`
  (line length 100), `mypy src/chronos` (strict), `pytest -q`. Run locally
  before every commit; CI should never surprise you.
- **Parallel agents**: one editing owner per file/directory, always. Never
  `cat` a subagent's `.output` transcript into context.
- **Research results reproduce bit-for-bit.** If re-running
  `scripts/run_research.py` changes previously-committed numbers without a
  data/code change, that is a determinism defect to investigate — never
  silently overwrite.

---

## 5. Explicit anti-goals (do none of these)

- No live or canary order path, no market orders, no shorts, no margin, no
  options execution, no averaging down, no pyramiding.
- No auto-promotion between modes; promotion records are evidence, not
  switches.
- No unfreezing or reinterpreting selection criteria after results exist; no
  second consumption of a final-test window.
- No weakening a safety seam to make a test pass — the test is the message.
- No synthetic data presented as market data; gaps are recorded, not filled.
- No claiming untested things are tested (a reviewer caught exactly one such
  docstring; it was treated as a CRITICAL).
- No touching the wheel dashboard's milestones/invariants except to keep its
  tests green.
- No re-fetching or re-auditing the corpus unless hashes fail verification.

---

## 6. The work plan to completion (priority order)

Work milestone by milestone. For each: announce the plan, execute end to
end, run gates, checkpoint-commit, push, update the tracking docs. Statuses
live in `TASKS.md` §Open and `docs/GO_LIVE_CHECKLIST.md` — keep both
truthful as you go.

### M1 — Broaden the research data and re-run research
*The highest-value item: all conclusions currently rest on SPY+QQQ, mirror-
sourced, SPY truncated at 2019-11, unadjusted.*

1. Acquire dividend-adjusted daily OHLCV for IWM, DIA, GLD, TLT and a
   longer SPY (HF MCP per §4; or the IBKR connector if the owner authorizes
   it). Same provenance discipline as the existing files; update
   `research/data/raw/MANIFEST.json` + `DATA_SOURCES.md`; record what you
   could not get.
2. Re-freeze selection criteria **before** touching new validation windows:
   update `research/selection_manifest.json` (new `frozen_at`, what data is
   now available, unchanged or deliberately amended criteria — amendments
   must be justified by data availability, never by prior results), commit
   it, *then* run.
3. `scripts/run_research.py --stage dev`, then `val`; only if a candidate
   clears C1–C5 run `--stage final` — once.
4. Rewrite `docs/RESEARCH_REPORT.md` + `docs/STRATEGY_SELECTION.md` from the
   new evidence, disclosures included (cap policy, cold-start warmup,
   adjusted-vs-raw series choice per symbol).

**Done when:** every number in the reports reproduces from committed
inputs; selection outcome (zero or more candidates) stated plainly with
per-symbol evidence; final window consumed ≤ once and that fact provable
from git history.

### M2 — The long-running shadow service loop
*The largest engineering gap. Closes accepted review findings R-22 and
R-23 (see `RISK_REGISTER.md`) and unlocks go-live Gate 2.*

Build `chronos/service/` (new package) + a `python -m chronos.service`
entry point, deliberately named differently from the CLI:

1. **Startup sequence (order matters):** load config → resolve mode lock →
   read halt (stay halted if halted) → open ledger/audit → **hydrate
   `ExecutionEngine._orders` from `SqliteLedger.working_intent_ids()`**
   (closes R-23: today a restart drops in-flight orders' events into an
   UNKNOWN_ORDER halt with no evidence trail) → gather broker evidence →
   run reconciliation → only a clean report sets
   `reconciliation_passed=True`.
2. **Close R-22:** extend `execution/reconciliation.py` beyond id-set
   presence to per-order *state* comparison (broker status + filled qty vs
   ledger's latest transition + fills). Contradictions → discrepancy →
   blocked, halted, human review. Keep the function pure; the service owns
   evidence-gathering.
3. **Main loop:** on a schedule (daily bars: after close), ingest latest
   closed bars through a data adapter with the existing quality gate, run
   strategy → portfolio → risk through the production path, append every
   decision to the audit log, emit notifications via the existing fanout.
   In SHADOW the mode lock is `NO_ORDERS` — structurally no submission. In
   PAPER (only under the six-condition lock) submissions flow through
   `submit_approved` exactly as the backtest path does.
4. **Resilience:** clean shutdown on signals; on any unexpected exception →
   persist halt, exit nonzero; restart re-enters reconciliation, never
   resumes trading automatically.
5. **Tests:** deterministic replay of the loop over recorded bars (twice →
   identical audit trails); chaos: restart-mid-order (hydration must
   reconcile, not halt-and-lose-evidence), disconnect during the window,
   contradictory reconciliation evidence, halt raised mid-cycle. Extend
   `tests/safety` with: service cannot submit in SHADOW; service never sets
   `reconciliation_passed` without a clean report.

**Done when:** R-22/R-23 rows in `RISK_REGISTER.md` flip to MITIGATED with
test references; GO_LIVE Gate 2 items about the loop flip to DONE; all
gates green.

### M3 — Platform monitoring (Phase 15; only the wheel dashboard exists)

Read-only, localhost-only view over ledger + audit + halt + latest service
state: mode and live-lock status (paper/live distinction **not by color
alone**), halt reason, reconciliation status, market-data age, positions,
open orders, recent fills, realized/unrealized P&L, remaining loss
capacity, active limits, code commit. Reuse the Streamlit stack for
consistency. **No trading logic, no submit path, no broker calls from the
UI** — render persisted state only. Test: renders from fixture state files;
grep-level test that the UI package imports no broker adapter.

### M4 — Hardening and coverage residue

- Tests for `chronos/cli/main.py` (currently untested — a reviewer flagged
  it), `research/runner.py`, `research/shadow.py`.
- Dependency lockfile (uv or pip-tools) + CI install from it;
  `docs/SECURITY.md` currently documents its absence — update it.
- Property-based tests (hypothesis) for the invariants that deserve them:
  intent-id uniqueness, state-machine legality, sizer bounds, risk-engine
  deny-monotonicity (a stricter policy never approves what a looser one
  denied).
- Optional, only if M1 justified more strategies: translate BEAR+ (02)
  **only after** the owner approves short selling — otherwise skip; and
  treat the AIO (00) as REQUIRES_REWRITE-scale work needing its own plan.

### M5 — Fresh independent adversarial review + remediation

Seven fresh reviewer agents (quant methodology, risk architecture,
brokerage integration, security, failure recovery, test quality, doc
accuracy) that did **not** author the code under review, mandated to
*demonstrate* defects with file:line evidence and `/tmp` repros — not to
summarize design. Mutation-test spot checks (restore sources afterward and
prove it with `git diff`). Remediate every CRITICAL/HIGH with a regression
test; record accepted findings with rationale. Refresh
`docs/INDEPENDENT_REVIEW.md` + `docs/REMEDIATION_REPORT.md`. The first
round's reports set the bar — read them first.

### M6 — Truthful final handoff

Refresh `HANDOFF.md`, `TASKS.md`, `CHANGELOG.md`, `docs/GO_LIVE_CHECKLIST.md`,
`docs/TEST_RESULTS.md` (exact counts from your own runs). Update the PR
body. State the precise status label — `research prototype`,
`backtest validated`, `shadow eligible`, `paper eligible`, or
`not eligible` — **only as the evidence supports**, and never "live ready."

### Owner-gated (surface, prepare, do not force)

- Real-gateway smoke test: needs the owner running TWS/Gateway
  (`CHRONOS_RUN_IBKR_SMOKE=1`, `scripts/smoke_test_ibkr.py`); the
  execution-plane paper adapter has **never** touched a real gateway.
- TradingView parity fixtures: needs owner exports into
  `fixtures/tradingview/`; until then parity remains spec-level
  (`docs/PARITY_REPORT.md`) — label it accordingly, never as
  "verified against TradingView."
- IBKR MCP connector authorization (see §4).
- Short-selling approval before any BEAR+ work; live financial limits are
  the owner's to set and default to zero until then.

---

## 7. Autonomy protocol

- **Cadence per milestone:** plan (2–3 sentences in your log) → execute →
  gates → checkpoint-commit → push → update tracking docs → move on.
  Commits are your recovery points across limits and context resets; make
  them small and truthful. Use the repo's existing commit-trailer format.
- **Delegate leaf work** (per-script analysis, test authoring, doc sweeps,
  data chunk-fetching, reviews) to parallel agents/workflows with disjoint
  file ownership. Keep architecture and safety-critical code in your own
  hands.
- **Stop and ask only when genuinely owner-gated** (§6 list), when a choice
  changes financial risk, or when something is irreversible. Everything
  else: make the conservative call, record it in `ASSUMPTIONS.md`, keep
  moving.
- **Report faithfully.** Failed test → say so with output. Skipped step →
  say so. If the honest conclusion at the end is "still zero tradable
  strategies, platform complete," deliver exactly that with the evidence.
  That sentence, fully backed, is a successful completion of this brief.

---

## 8. First actions, verbatim

```bash
# 1. Orient
git log --oneline -15 && cat HANDOFF.md TASKS.md
# 2. Baseline (python3.12, not python3)
python3.12 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]' && .venv/bin/pytest -q
# 3. Verify corpus integrity
.venv/bin/python -m chronos.cli verify-corpus
```

Green baseline → begin **M1** and proceed through **M6** without waiting
for permission between milestones.
