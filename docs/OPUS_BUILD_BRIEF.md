# Chronos — Autonomous Build Brief for Opus 4.8

You are the principal engineer for **Chronos**, an AI-assisted, deterministic
algorithmic trading platform built from a corpus of quantitative-finance Pine
Scripts, targeting Interactive Brokers with an eventual ~USD 3,000 account.
**Capital preservation and correctness outrank returns and speed.** It is
acceptable — preferable — to conclude that no strategy is currently tradable
rather than invent confidence the evidence does not support.

This brief is written from a completed first build. It exists so you do not
re-learn the environment the hard way. **Read the whole thing before acting.**

---

## 0. Prime directive and how to start

**Do not assume a blank slate.** The repository already contains a working
first build (branch `claude/chronos-trading-system-rrzroq`, PR #1, CI green,
1158 tests passing). Your job is to drive it to *completion*, not to rebuild
what exists. So the very first thing you do is **orient**:

```bash
git log --oneline -20
git status
cat TASKS.md HANDOFF.md docs/GO_LIVE_CHECKLIST.md
ls src/chronos research docs specs tests
```

Then establish the baseline (see §2 for the Python-version trap):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest -q                 # expect ~1158 passed, 1 skipped
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src/chronos
```

If that baseline holds, **skip every phase already marked done in TASKS.md**
and go straight to §5 (Remaining Work). If you are ever on a fresh clone with
none of this present, execute §4 (Full Phase Plan) in order.

Work **state-aware and idempotently**: check whether a deliverable exists and
is correct before producing it. Every phase below names its output files so
you can tell.

---

## 1. Non-negotiable safety rules (these never change)

1. **No live order, ever, during development.** Live/canary modes are refused
   *in code* (`chronos.control.modes.resolve_mode_lock` returns
   `DENIED_LIVE_DISABLED`; the promotion evaluator appends a failing gate).
   Keep it that way. There is no `--force`, no env var, no config that enables
   live. Preserve this in every change.
2. **No generative model in the order path.** Strategy → portfolio → risk →
   execution → broker is deterministic, versioned, tested, replayable. AI
   (you) may only assist offline: research, classification, docs, code
   generation, analysis. Nothing you emit at runtime may submit/cancel/modify
   an order, size a position, set a stop, or move a limit.
3. **Deny by default, fail closed.** Every risk allowance defaults to
   zero/empty/false. A missing/corrupt state file reads as HALTED. Fresh
   deployments start HALTED. Any uncertainty → refuse, don't guess.
4. **Separation of authority is structural, not conventional.** Strategies
   emit `StrategyProposal` (no quantity, no account, no broker fields — the
   type physically cannot express an order). The risk engine's policy is
   frozen; it mints instance-token-bound approvals; the execution engine
   refuses any approval it didn't mint. Do not weaken these seams.
5. **No secrets in the repo.** Env vars / local secrets only. `.env.example`
   holds placeholders. Never commit credentials, account numbers, or tokens —
   check every commit (`git diff --cached`) before pushing.
6. **Broker evidence is the only truth.** Never infer an order succeeded
   because a call returned; never auto-flatten an unknown position. Acks and
   fills are authoritative; unknown/contradictory state halts and requires
   reconciliation + manual rearm.

If a change would violate any of these, stop and flag it rather than proceed.

---

## 2. Environment reality (hard-won — this is the point of the brief)

- **Python 3.12 is required; the default `python3` is 3.11 and will fail**
  install with `requires-python: >=3.12`. Always build the venv with
  `python3.12 -m venv .venv`. (Costs ~5 min if you miss it.)
- **The Pine corpus lives in Notion, not the repo.** Use the Notion MCP
  (`mcp__Notion__notion-search` / `notion-fetch`). Path: Command Center →
  Trading Library → **Pine Quant Library — Master Index**. The brief says
  "~77 scripts"; the authoritative index has **42** (catalog 00–40 + archived
  0A). Some scripts are split across inline "Part N of M" code fences on one
  page — concatenate in order, byte-exact. Record the 77-vs-42 discrepancy in
  ASSUMPTIONS.md; do not invent missing scripts. (Already done —
  `research/pine/`, hashes in `research/strategy_registry.yaml`.)
- **All finance/market-data websites are blocked (HTTP 403 through the
  proxy).** yahoo, stooq, nasdaq, tiingo, alphavantage, polygon — all dead.
  Do **not** waste time on them. Real historical data comes from the
  **Hugging Face MCP** (`mcp__Hugging_Face__hf_fs`), which works server-side.
  - Search with SHORT single-word queries ("SPY", "QQQ", "sp500", "ETF
    daily", "kaggle", "finance"). Prefer datasets with plain CSV files
    (`ls -R` to confirm; parquet-only shards are unusable without a
    binary-capable transport).
  - `hf_fs cat` is text, ~80 KB/call — read large CSVs in offset chunks and
    reassemble byte-exact. Never fabricate or interpolate a row.
  - Known-good leads already used: `mmirmomeni/spy_daily` (SPY, unadjusted,
    2000–2019-11), `Maxim37/timeseries-QQQ-1d-25yr` (QQQ, adjusted,
    1999–2024). Cross-check against a second lineage (e.g.
    `zexianli/nasdaq_data`). Neither declares a license → mark research-use-only.
  - Intraday exists (e.g. `Maxim37/timeseries-1m-QQQ-5y`) but is large; only
    sample it. Intraday strategies can't be validated here regardless (PDT +
    account size make them untradeable — A-31).
- **Use the Workflow tool for fan-out.** The corpus fetch (42 pages) and the
  forensic audit (42 scripts) were each one Workflow with ~42 parallel
  agents. Workflows **resume from cache** after interruption
  (`resumeFromRunId`) — completed agents replay instantly. This is what makes
  a long build survivable.
- **Usage limits reset at 07:00 UTC.** A big fan-out can hit the limit
  mid-run. Recovery pattern that worked: checkpoint-commit often, then use
  `mcp__claude-code-remote__send_later` to re-wake yourself after the reset
  and resume the workflow from its `runId`. Don't fight the limit; schedule
  around it.
- **CI reports via workflow runs, not the commit-status API.**
  `pull_request_read get_status` shows `pending`/`total_count: 0` even when
  green. Confirm with `actions_list list_workflow_runs` (filter by branch) and
  read `conclusion: success`. Don't panic at the misleading `pending`.
- **`actions_list` output can exceed the token cap** — it gets saved to a
  file; parse it with a `python3 -c` one-liner, or do it in a subagent.
- **The four gates that must stay green** (they are CI, `.github/workflows/ci.yml`):
  `ruff check .`, `ruff format --check .` (line length 100),
  `mypy src/chronos` (strict), `pytest -q`. Run them before every commit.

---

## 3. Operating discipline (how to run autonomously)

- **Checkpoint-commit after every meaningful unit** with descriptive messages;
  push regularly. Commits are your recovery points across usage limits and
  context resets. End commit messages with the required co-author/session
  trailers (see the repo's existing commits for the exact format).
- **Parallelize with background agents/workflows**, but give each a **single
  editing owner** per file/directory so two agents never rewrite the same
  file. In the first build: one agent owned tests, one owned docs, one owned
  data, workflows owned corpus/audit — no collisions.
- **When you delegate a search or review, keep the conclusion, not the file
  dump.** Sub-agent final reports are for you, not the user — relay only what
  matters.
- **Verify before you claim.** Never say a test passed unless you ran it.
  Distinguish passed / failed / skipped / not-runnable-without-credentials /
  not-implemented. The independent reviewers in the first build caught a
  docstring that claimed test coverage that didn't exist — don't repeat that.
- **Maintain the living docs** as you go: `TASKS.md`, `ASSUMPTIONS.md`,
  `DECISIONS.md`, `RISK_REGISTER.md`, `CHANGELOG.md`, `HANDOFF.md`. Reconcile
  stale statuses — a checklist that says a thing is both "in progress" and
  "done" is a real defect (reviewers flagged it).
- **Disclose methodology choices that affect results.** The first build's
  research widened risk caps to measure unclipped trade frequency and
  initially didn't say so; a reviewer correctly flagged it as misleading.
  Any knob that changes a reported number gets disclosed in the report.
- **After building, run an independent adversarial review** with fresh agents
  that did not author the module under review, told to *demonstrate* defects
  (repro in /tmp, file:line evidence), not summarize design. Remediate every
  CRITICAL/HIGH with a regression test; record accepted findings with a
  rationale. See `docs/INDEPENDENT_REVIEW.md` / `docs/REMEDIATION_REPORT.md`
  for the bar to clear.

---

## 4. Full phase plan (only if starting from scratch; otherwise skip to §5)

Each phase lists its outputs so you can detect what's already done. This is
the ordering that worked — corpus first, platform in parallel with research,
review last.

| Phase | Deliverable (check if it exists) | Notes |
|---|---|---|
| 1 Inventory | `research/pine/*` (42), `research/strategy_registry.yaml` + catalog CSV/JSON, `docs/STRATEGY_CATALOG.md`; `scripts/build_strategy_registry.py` | Fetch from Notion via Workflow fan-out; SHA-256 pin. |
| 2 Forensic audit | `research/pine_findings.json`, `docs/PINE_AUDIT.md`; `scripts/build_audit_docs.py` | One audit agent per script; structured schema; integrity status per script. |
| 3 Specs | `specs/*.yaml`, `chronos.specs` schema | Vendor-neutral, every Pine deviation enumerated. |
| 4 Parity | `chronos.indicators`, `chronos.strategies`, `tests/parity/`, `docs/PARITY_REPORT.md` | Spec-level only unless owner provides TradingView exports (`fixtures/tradingview/`). |
| 5 Data | `chronos.marketdata`, `research/data/raw/{*.csv,MANIFEST.json,DATA_SOURCES.md}` | HF MCP; provenance + validation + cross-check; record gaps, don't fabricate. |
| 6 Quant validation | `scripts/run_research.py`, `research/results/`, `research/selection_manifest.json`, `docs/RESEARCH_REPORT.md`, `docs/STRATEGY_SELECTION.md` | Freeze criteria **before** touching validation; keep final-test window untouched. |
| 7–9 Platform | `chronos.{portfolio,risk,execution,control}`, brokers (simulated + ibkr_paper), `chronos.execution.reconciliation` | Deny-by-default risk engine; order state machine; mode locks; reconciliation gate. |
| 10 Backtest/replay | `chronos.backtest`, `chronos.research.runner` | Closed-bar-only; next-bar fills; determinism asserted. |
| 11 Modes/gates | `chronos.control.{modes,promotion}` | RESEARCH→…→PAPER; live refused; single-step promotion. |
| 12 Kill switch | `chronos.control.halt` | Persistent, fail-closed, restart-survivable. |
| 14 Persistence/audit | `chronos.execution.{ledger,sqlite_ledger}`, `chronos.auditlog` | Append-oriented; hash-chained; owner-only file perms. |
| 15 Monitoring | **platform dashboard — NOT built yet (see §5)** | Only the wheel dashboard exists. |
| 16 Tests | `tests/{safety,platform_unit,parity,chaos}` | Safety acceptance suite is the crown jewel. |
| 17–18 Sec/deploy/CLI/docs | `chronos.cli`, full `docs/` set, ADRs 0001–0008 | Owner-only perms; localhost only; no lockfile (document it). |
| Review | `docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md` | 7 dimensions; remediate CRITICAL/HIGH. |

Acceptance for each phase = its outputs exist, are accurate, and the four
gates are green.

---

## 5. Remaining work to reach "completion" (from the current state)

The first build is a **research prototype / backtest-executed** system that
correctly concluded **zero strategies are currently tradable**. Reaching the
next real milestones requires the following, in priority order. Do them
state-aware; each has a concrete acceptance gate.

### 5.1 Broader, better research data (highest research value)
- **Why:** conclusions rest on SPY+QQQ only, mirror-sourced, SPY ends 2019-11,
  unadjusted. IWM/DIA/GLD/TLT are missing.
- **Do:** acquire dividend-adjusted daily OHLCV for the full ETF universe (HF
  MCP, or ingest an owner-provided/IBKR export if available) with the same
  provenance discipline (`MANIFEST.json`, hashes, cross-checks, validation).
  Extend `research/data/raw/`.
- **Then:** re-run `scripts/run_research.py --stage all`. The final-test
  window (2022+) is **unconsumed** — it may be touched exactly once, after
  criteria are (re)frozen. Update `docs/RESEARCH_REPORT.md` /
  `STRATEGY_SELECTION.md` with the new evidence. If a strategy now clears the
  frozen criteria, say so honestly; if not, that's still the answer.
- **Gate:** every result reproduces bit-for-bit; data provenance recorded; no
  criterion bent after seeing results; final-test window consumed at most once.

### 5.2 The long-running shadow/paper SERVICE LOOP (biggest engineering gap)
This is the single largest missing piece and it blocks two accepted review
findings (RISK_REGISTER R-22, R-23) and go-live Gates 2–3.
- **Build:** a daemon that, on a schedule/stream, (a) ingests the latest
  closed bars through a real data adapter, (b) on startup/reconnect enters a
  non-trading reconciliation state, **hydrates `ExecutionEngine._orders` from
  the ledger** (`working_intent_ids()`), calls `reconciliation.reconcile()`
  with real broker evidence, and only sets `reconciliation_passed=True` on a
  clean report, (c) runs the production decision path, (d) drives
  notifications, (e) never submits in SHADOW (NO_ORDERS) and submits in PAPER
  only under a verified paper lock.
- **Close R-22:** extend `reconcile()` to compare per-order *state* (fill qty,
  lifecycle), not just id-set membership. Add the evidence-gathering caller.
- **Close R-23:** the startup `_orders` hydration above; an in-flight order's
  post-restart event must reconcile, not just halt-and-drop.
- **Gate:** deterministic replay tests of the loop; chaos tests for
  restart-mid-order, disconnect, partial-fill-before-disconnect; the loop
  never submits in SHADOW; PAPER requires all six mode-lock conditions.

### 5.3 Platform monitoring dashboard (Phase 15)
- **Why:** only the wheel dashboard exists; the platform has none.
- **Build:** a read-only view (reuse the Streamlit stack or a small local web
  app, localhost only) showing mode, live-lock status, halt reason, broker/
  data health, reconciliation status, positions/orders/fills, realized/
  unrealized P&L, remaining loss capacity, active risk limits, code commit.
  **Paper vs live must be unmistakable and not color-only.** No trading logic
  in the UI.
- **Gate:** renders from ledger/audit/halt state without a broker call; no way
  to submit an order from the UI; localhost binding only.

### 5.4 IBKR paper smoke against a real gateway (OWNER-gated)
- The paper adapter has **never touched a real gateway** (no credentials
  here). This needs the owner running TWS/IB Gateway and
  `CHRONOS_RUN_IBKR_SMOKE=1`. You cannot complete it autonomously — surface it
  as an owner action and make sure the harness is ready
  (`scripts/smoke_test_ibkr.py`, and an equivalent for the execution-plane
  adapter). Never let a test hit a live endpoint.

### 5.5 Optional strategy expansion (only if research justifies)
- The flagship confluence AIO (script 00) and BEAR+ (02) are the remaining
  distinct executable systems. BEAR+ needs a short-selling decision
  (disabled by default for a $3k cash account — get owner approval first).
  Only translate more strategies if §5.1 gives a reason to; more code without
  edge is negative value.

### 5.6 TradingView parity (OWNER-gated)
- Upgrade parity from spec-level to TradingView-verified **only if** the owner
  provides strategy-tester exports into `fixtures/tradingview/`. Then build
  `tests/parity` fixtures comparing bar-by-bar and trade-by-trade, with
  mismatch reports (first divergent timestamp, Pine vs Python, root cause).

---

## 6. Known pitfalls (things that bit the first build)

- Python 3.11 default → install fails. Use 3.12.
- Finance sites all 403 → use HF MCP; don't burn time probing them.
- `pull_request_read get_status` lies (`pending`) → check `actions_list`.
- Huge tool outputs (actions list, agent transcripts) overflow context → parse
  saved files with `python3 -c`, or delegate to a subagent; never `cat` an
  agent's `.output` transcript.
- Deleting/weakening a safety seam to make a test pass is never the fix — the
  test is telling you something. (The paper-account-pattern check was
  correctly tightened, not loosened, when a test caught it.)
- Reviewers will (and should) attack: undisclosed methodology, doc/code drift,
  fill-translation edge cases (status vs filled-qty inconsistency), halt
  TOCTOU, file permissions, unhandled corrupt-state recovery. All were found
  and fixed once — re-check them after any change to those areas.

---

## 7. Definition of done (and honest status labels)

Never label the system "live ready." Use precise statuses:
`research prototype` · `backtest validated` · `shadow eligible` ·
`paper eligible` · `paper validated` · `canary review eligible` ·
`not eligible`. **Only evidence sets the status.**

Consider your assignment complete when:
1. §5.1–5.3 are done (data + service loop + monitoring), with gates green.
2. Full suite passes; ruff/format/mypy clean; CI green on the branch.
3. A fresh independent adversarial review found nothing CRITICAL/HIGH
   unremediated.
4. `HANDOFF.md` truthfully states the current status, what's verified, what
   remains owner-gated (§5.4, §5.6), and the recommended next milestone.
5. The safety invariants are all still true and tested: live impossible in
   code, deny-by-default, fresh deploy starts halted, six-condition paper
   lock, no strategy promoted without evidence.

If the honest answer at the end is still "no strategy has a demonstrated
edge," **that is a valid and complete outcome** — deliver it plainly with the
evidence, do not manufacture a promotion.

---

## 8. First three commands for your first session

```bash
# 1. Orient
git log --oneline -20 && cat HANDOFF.md TASKS.md docs/GO_LIVE_CHECKLIST.md
# 2. Baseline (note python3.12)
python3.12 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]' && .venv/bin/pytest -q
# 3. Decide: baseline holds → start §5.1; anything red → fix before new work
```

Then pick the highest-priority incomplete item in §5, announce your plan,
and execute it end to end — build, test, review, checkpoint-commit, push.
Proceed autonomously; only stop for the genuinely owner-gated items (§5.4,
§5.6, the BEAR+ short-selling decision) or an irreversible/financial choice.
