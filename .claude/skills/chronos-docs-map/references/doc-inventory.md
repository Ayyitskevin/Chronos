# Chronos document inventory — complete, per file

Verified against the repo at HEAD `47a8d72` on 2026-08-02. Status vocabulary
(defined in ../SKILL.md): CURRENT / HISTORICAL-HONEST / STALE-UNBANNERED / MIXED.
Tier numbers are the AGENTS.md:41-54 precedence tiers (1 owner direction … 6 historical).

Re-generate the raw file list any time:

```bash
ls *.md docs/*.md docs/adr/*.md
```

## Root documents (10 .md + LICENSE + NOTICE)

| Doc | Role (one line) | Tier | Status |
|---|---|---|---|
| AGENTS.md | The repository contract: read-first list, non-negotiable build rules, the precedence ladder | Meta — defines the tiers | CURRENT |
| CLAUDE.md | 6-line entry pointer: read AGENTS.md + vision plan before any work | Meta | CURRENT |
| KIMI.md | Byte-parallel entry pointer for the Kimi agent | Meta | CURRENT |
| README.md | Front page: mission (owner directive 2026-07-25), autonomy milestone log M0-M11, safety posture with [enforced]/[contract] labels, setup | 1 (mission) / 2 (status) | CURRENT — the freshest status narrative in the repo |
| CHANGELOG.md | Reverse-chron build log with per-milestone gate counts; head entry = M11, 2026-07-27 | 2 | CURRENT — the ONLY doc with the current test count (2489 passed, 1 skipped, CHANGELOG.md:69) |
| DECISIONS.md | Decision index D-01..D-19; D-11 struck through, superseded by D-16 | 3 | CURRENT — exemplary supersession hygiene (note: rows D-17/D-18 appear after D-19 in the file) |
| RISK_REGISTER.md | Living risk register R-01..R-42 with dated statuses and disclosed residuals | 4 | MIXED — R-01's note is frozen at M1-era and contradicts R-38 (Ledger #12); trust R-24..R-42 after spot-checking code |
| ASSUMPTIONS.md | Conservative build assumptions A-01..A-42, some amended in place | 4-ish | MIXED — A-12 amended 2026-07-25; A-10/A-21/A-22 still assume ~USD 3,000 (Ledger #8) |
| TASKS.md | Legacy task board for the deterministic-platform build | 6 | HISTORICAL-HONEST — banner at :3-6 is good; counts (951, 1158) and the Open list are stale (Ledger #5) |
| HANDOFF.md | Platform handoff narrative, partially updated through M11 | 6 | MIXED — banner says "as of 2026-07-17" but body mixes 07-17, 07-25, 07-27 states and self-contradicts (Ledger #6) |
| LICENSE | MIT, (c) 2026 Kevin Lee | — | CURRENT |
| NOTICE | Attribution: terminal design adapted from Tyche (Apache-2.0), per ADR-0018 | — | CURRENT |

## docs/ — governance, safety, operations (18 files)

| Doc | Role (one line) | Tier | Status |
|---|---|---|---|
| docs/VISION_COMPLETION_PLAN.md | Canonical roadmap: two 10/10 definitions, current-truth snapshot (§2), phases 0-5, owner gates, task contract (§13) | 5 | CURRENT — "Status: canonical execution plan / Effective: 2026-08-01" (:3-5); the north star |
| docs/safety.md | Threat/authority model under ADR-0016/0017 | 4 | CURRENT |
| docs/limitations.md | "the single source of truth for limitations" (:11-12); honest cannot-do list | 4 | CURRENT |
| docs/ARCHITECTURE.md | Deterministic-platform architecture + two-subsystem overview | 3-adjacent | MIXED — platform description current; the authority-model paragraph is frozen at M1 with no banner (Ledger #3) |
| docs/architecture.md | Wheel-dashboard architecture as built through dashboard-M10 | 6 | HISTORICAL-HONEST — scope note :3-11 says to read it as M1-M10 posture, not current capability |
| docs/GO_LIVE_CHECKLIST.md | Gate-by-gate go-live checklist for the deterministic platform; reviewed-release doctrine | 6 (doctrine retained) | HISTORICAL-HONEST — two banners (:3-24) + a struck-through in-place correction (:185-189); body statuses frozen "as of 2026-07-17" (:26-27) (Ledger #4) |
| docs/INCIDENT_RESPONSE.md | Incident runbook: halt-first, playbooks | 4-ish runbook | STALE-UNBANNERED (danger) — knows only the deterministic-platform halt; zero mentions of the live order plane's kill switch (Ledger #1) |
| docs/BACKUP_AND_RECOVERY.md | Backup/restore procedures for state files | 4-ish runbook | STALE-UNBANNERED (danger) — "restore must never auto-resume trading, and the code guarantees it" is false for the orders plane; file table omits `data/live_kill_switch.json` (Ledger #1) |
| docs/live_trading_runbook.md | The ten live gates, arming, kill switch for `chronos.orders` | 4 | MIXED — gate list current; the "Autonomous operation" section (:19-29) is stale in BOTH directions (Ledger #2) |
| docs/OPERATIONS.md | Daily ops for the deterministic platform | 6-ish ops | CURRENT for its plane — `--cash 3000` example at :87 is a stale example parameter |
| docs/DEPLOYMENT.md | Local install/deploy, lockfile install | ops | MIXED — setup steps current; "Future work — shadow/paper service (NOT IMPLEMENTED)" (:133) and "`python -m chronos.service --mode shadow   # does not exist`" (:152) are false — the M2 service loop exists (Ledger #10) |
| docs/SECURITY.md | Threat model + "controls as implemented" | 4 | MIXED — self-declares current (:4-5) but at least two claims are stale: ALLOW_LIVE_TRADING and "no authentication" (Ledger #11) |
| docs/IBKR_INTEGRATION.md | Deterministic-platform paper adapter design | 6 | STALE-UNBANNERED — ":17 The ONLY code path … that can hand an equity order to IBKR" is false since M5-M7; that adapter is quarantined (R-28) (Ledger #9) |
| docs/IBKR_RUNBOOK.md | Operator procedures against IBKR | ops | STALE-UNBANNERED in part — ":8-9 no long-running shadow/paper service loop exists yet" is false (Ledger #10); `--cash 3000` examples at :175, :182 |
| docs/ibkr_setup.md | TWS/Gateway setup, official ibapi install | ops | STALE-UNBANNERED in part — ":5-6 Both are read-only until the Milestone 5-7 order service exists" — M5-M7 shipped (Ledger #10) |
| docs/histdata_runbook.md | `chronos.histdata` bars/options capture runbook (ADR-0011/0012) | ops | CURRENT |
| docs/formulas.md | Decimal financial formulas (short put, coverage, deliverable) | reference | CURRENT |
| docs/RISK_POLICY.md | Deny-by-default risk-policy doctrine for the deterministic platform | 4-ish | CURRENT for its plane |

## docs/ — research, tests, reviews (11 files)

| Doc | Role (one line) | Tier | Status |
|---|---|---|---|
| docs/RESEARCH_REPORT.md | Phase-6 research report, 5-symbol universe; "Zero candidates are selected" verdict up front | 2 | CURRENT as evidence — but its cost model is premised on USD 3,000 (:42, :176, :180, :233) — flag under Ledger #8 |
| docs/STRATEGY_SELECTION.md | Selection record: "Selected candidates: NONE" (:8) | 2 | CURRENT |
| docs/STRATEGY_CATALOG.md | Pine corpus catalog — 42 artifacts, hash-pinned, fetched 2026-07-17 | reference | CURRENT (static artifact) |
| docs/PINE_AUDIT.md | Forensic audit of all 42 Pine scripts | reference | CURRENT (static artifact) |
| docs/PARITY_REPORT.md | Pine→Python parity — "TRANSLATION VERIFIED AGAINST SPECIFICATION" (spec-level only, no TradingView exports) | 2 | CURRENT |
| docs/RESEARCH_REPRODUCIBILITY.md | produce/replay/compare manifest tooling | reference | CURRENT — dead link at :8 to RESEARCH_READINESS.md, which does not exist |
| docs/TEST_PLAN.md | Test taxonomy per layer | 6 | MIXED — taxonomy useful; ":4-5 being produced separately" is stale (results exist) and layer counts are frozen (":22 29 tests" for tests/safety/ vs "~90" per TEST_RESULTS.md:18) |
| docs/TEST_RESULTS.md | Test run evidence | 2 | STALE-UNBANNERED (danger) — the section headed "Summary (current — M2a, 2026-07-25)" reports 1901 passed; the real count is 2489 (CHANGELOG.md:69). The historical section IS labeled superseded; the "current" header simply stopped being maintained (Ledger #7) |
| docs/INDEPENDENT_REVIEW.md | Adversarial review round 1 (7 dimensions, 1 CRITICAL, 7 HIGH) | 2 | CURRENT as a record |
| docs/INDEPENDENT_REVIEW_M5.md | Adversarial review round 2 (post-M1..M4; no criticals, 2 HIGH) | 2 | CURRENT as a record |
| docs/REMEDIATION_REPORT.md | Disposition of every round-1 finding; "1158 passed" is a snapshot count, not current | 2 | CURRENT as a record |

## docs/ — historical plans and briefs (5 files)

| Doc | Role (one line) | Tier | Status |
|---|---|---|---|
| docs/AI_QUANT_GAME_PLAN.md | Umbrella AI-quant roadmap (2026-07-18 directive) | 6 | HISTORICAL-HONEST — banner :3-7 subordinates it to the vision plan; still carries the mandate-replaces-arming prose at :260-264 (Ledger #2) |
| docs/LIVE_WHEEL_GAME_PLAN.md | Live-Wheel programme plan + delivered milestone records | 6 | HISTORICAL-HONEST — double banner :3-18; internal stale line ":33 Branch: `feat/live-wheel-dashboard`" (default branch is `feat/wheel-dashboard-mvp`); mandate prose at :131-134 |
| docs/OPUS_BUILD_BRIEF.md | Continuation brief for a takeover session | 6 | HISTORICAL-HONEST — "**ARCHIVED (2026-08-01)** … do not use it as task or completion authority" (:3-7); its "1,158 tests" and "~USD 3,000" claims are quarantined by the banner |
| docs/QQQ_GOLD_SPY_CAPABILITY_BRIEF.md | 2026-07-19 standing instructions for a QQQ/GLD/SPY capability push | 6 | STALE-UNBANNERED (danger) — still says ":6-7 This brief is the standing instruction set…"; acknowledges D-16 but was never subordinated to the vision plan by any banner |
| docs/LECTURE_134_ANALYSIS.md | Analysis of the Quant Guild reference bot Chronos is modeled after | 6 context | CURRENT as analysis |

## docs/adr/ — all 19 ADRs

Status lines verified verbatim from each file's `Status:` line, 2026-08-02.

| ADR | Decides | Status line (verbatim, abridged) | Note |
|---|---|---|---|
| 0001 | Extend existing repo, don't rewrite | Accepted (2026-07-17) | |
| 0002 | IBKR via TWS API / ib_async | Accepted (2026-07-17) | |
| 0003 | Separate platform persistence | Accepted (2026-07-17) | |
| 0004 | Separation of authority; no-AI-in-runtime | "Accepted in part; §5 superseded by ADR-0016" | in-place supersession, good hygiene |
| 0005 | Closed-bar engine, next-bar fills | Accepted (2026-07-17) | |
| 0006 | Research data provenance (mirrors) | Accepted (2026-07-17) | |
| 0007 | Mode lock + persistent halt; live refused | Accepted (2026-07-17) | untouched by ADR-0016 |
| 0008 | Executable candidate scope: daily-bar long-only ETFs | Accepted (2026-07-17) | premised on "~USD 3,000" (:9, :32) — Ledger #8 |
| 0009 | The LIVE branch at the single submission boundary | accepted (design-panel remediated, 2026-07-18) | |
| 0010 | Crypto family (spot, allowlist, off by default) | accepted (design-panel remediated, 2026-07-18) | §4 carries a model in-place correction (2026-07-27, M10) |
| 0011 | Two-process historical-data plane | accepted (two-reviewer design panel) | |
| 0012 | Options forward capture (C0) | **"Status: proposed"** | STALE status line — D-14 records the decision; `python -m chronos.histdata options` is shipped (Ledger #12) |
| 0013 | Experiment registry + holdout guardian | accepted (post-merge adversarial review) | |
| 0014 | Walk-forward + statistics upgrade | **"proposed (design-review pending)"** | STALE status line — `src/chronos/research/walkforward.py`, `stats.py` exist (Ledger #12) |
| 0015 | Re-validation campaign | **"proposed (design-review pending)"** | STALE status line — `src/chronos/research/campaign.py` exists (Ledger #12) |
| 0016 | Controlled autonomous model authority (D-16) | "accepted (owner directive, 2026-07-25); §4 and §6 superseded in part by ADR-0017" | in-place supersession noted in the status line itself |
| 0017 | Owner-directed maximal autonomy, persistent mandate (D-17) | accepted (owner directive, 2026-07-25) | |
| 0018 | Operator terminal: fresh, Python-served (D-18) | accepted (2026-07-26) | tyche/midas rejected on evidence |
| 0019 | Historical bars + chart panel (D-19) | accepted (2026-07-26) | |

## The test-count lineage (why any count outside CHANGELOG head misleads)

951 (TASKS.md:13, baseline) → 1115 (GO_LIVE_CHECKLIST.md:60) → 1158
(REMEDIATION_REPORT.md, TASKS.md:40, OPUS_BUILD_BRIEF.md) → 1255 (TEST_RESULTS.md:27,
labeled historical) → 1885 (HANDOFF.md:24) → 1901 (TEST_RESULTS.md:12, labeled "current",
is not) → **2489 passed, 1 skipped** (CHANGELOG.md:69, M11 2026-07-27; re-verified green
2026-08-02).

Every count is a snapshot. Re-verify before citing:

```bash
.venv/bin/pytest -q   # count drifts with every milestone; CHANGELOG head records the last gate
```
