---
name: chronos-failure-archaeology
description: >
  The chronicle of every major Chronos defect, dead end, rejected alternative, and pivot —
  symptom, root cause, evidence (commit hash or file:line), and current status. Load this
  when you ask "why is it like this", "has this happened before", "what's the history of X",
  "why was X rejected", "was there a previous bug here", "what's the lesson", "who decided
  this", "did we try that already", or any repo-archaeology question; ALWAYS load it before
  re-attempting an approach that might have been rejected (a different terminal shell, the
  Client Portal API, serving the chart from research data, removing arming, "just delete the
  quarantined adapter", re-testing on QQQ 2022-2024). Owns the full inert-control narrative
  (R-24..R-27) and the pivot record (D-11 to D-16/D-17, tyche/midas, USD 3k to USD 110). NOT
  for current-code prevention checklists (chronos-ibkr-boundary), current doc contradictions
  (chronos-docs-map), or deciding what to do next (chronos-priorities-and-roadmap).
---

# Chronos failure archaeology — settled battles, and how to stop re-fighting them

Verified against the live repo on 2026-08-02 (HEAD `47a8d72`). Every entry cites a commit
hash or `file:line`. Read this before concluding a control is broken, before proposing an
alternative that may already be a rejected corpse, and before trusting any historical claim
about this repo — the clone is shallow (§6) and three milestone-numbering schemes overlap.

Status vocabulary used throughout (matches RISK_REGISTER.md:4):

| Status | Meaning here |
|---|---|
| MITIGATED | Fixed in code with tests, but a disclosed residual remains. NOT the same as CLOSED. |
| CLOSED | No live residual. Rare in this repo. |
| ACCEPTED | Risk consciously kept, by owner directive or documented trade-off. |
| SETTLED (rejections) | Decided with evidence and an ADR. Reopening requires new evidence + a new ADR (see chronos-change-control). |
| RESULT (not failure) | An honest negative outcome the system was designed to produce. Do not "fix" these. |

---

## 1. The four inert kernel defects (R-24..R-27) — the signature failure class

**"Inert control"** (define once, use everywhere): a safety control that is fully wired,
documented, surfaced in the UI, and covered by passing tests, yet is *structurally unable to
ever fire* — its evidence is never supplied, its counter has no callers, or its flag has no
real setter. Chronos shipped four of them simultaneously in its safety kernel.

All four were filed together as OPEN risks by the **M0 adversarial audit** at the start of
the autonomy governance reset — commit `6feaea9` (2026-07-25, "M1: governance reset",
adopts ADR-0016). ADR-0016 records it directly: "The M0 audit found four that unattended
operation makes strictly more dangerous, recorded as RISK_REGISTER R-24 … R-27"
(docs/adr/ADR-0016-controlled-autonomous-model-authority.md:376-378). The M0 audit has no
standalone document; its findings live as the `-orig` rows in RISK_REGISTER.md (lines 32, 37).

### 1.1 R-24 — Writer lease: advisory lock wearing a fencing-token costume

- **Symptom:** none. That is the point — nothing ever failed visibly.
- **Root cause:** `WriterLease.renew()` had **zero production callers** ("`grep '\.renew('`
  over `src/` is empty" — RISK_REGISTER.md:32, the R-24-orig row). The 30-second lease
  silently expired mid-session while `BackendState.writer` stayed `True`; a second backend
  could acquire the lease while the first still believed it held it. The token was never
  checked by DB writes or the broker send — an advisory lock, not a fencing token.
- **Fix:** M2, commit `f8d4150` (2026-07-25). Backend lifespan heartbeat renews at TTL/3
  (the call: `lease.renew` at src/chronos/api/main.py:135); **any single renewal failure
  permanently demotes the process to read-only** (re-acquiring would be unsafe — another
  writer may already have acted); `holds()` re-checks ownership in the database immediately
  before the transmit line. Tests: `tests/safety/test_writer_lease_fencing.py` (7).
- **Status:** MITIGATED (M2, 2026-07-25), RISK_REGISTER.md:31. **Residual:** IBKR does not
  know about the lease, so true broker-side fencing is impossible — the check-to-wire window
  is narrowed, not closed. R-25's fix leans on this residual (see 1.2).

### 1.2 R-25 — Daily opening cap: never refused anything (the only FAIL-OPEN one)

- **Symptom:** none, again — but inverted. `max_opening_orders_per_day` shipped in (wheel)
  Milestone 5 (`22e2b7c`, 2026-07-18), was surfaced in the settings page, was documented as
  a control, and **never once refused an order**.
- **Root cause — two independent defects, each sufficient** (commit `654f842` message):
  1. `BrokerRiskEvidenceProvider.gather` **never set `opening_orders_today`**, so the field
     took its `0` default and the check evaluated `0 + 1 <= limit` on every call, forever.
  2. Independently, `count_opening_since` — **which had zero callers** — also filtered
     `action == SELL`. `OPEN AND SELL` is `{OPEN_SHORT_PUT, OPEN_COVERED_CALL}`, so
     `OPEN_LONG_STOCK` and `OPEN_LONG_CRYPTO` would have stayed invisible to the cap even
     once it was wired.
- **Aggravating factor:** ADR-0010 §4 had **falsely claimed both halves were fixed**. The
  claim was corrected at its source, left standing and struck rather than edited away:
  "Neither half of that sentence was true when it was written, and the paragraph stayed
  wrong for five milestones" (docs/adr/ADR-0010-crypto-family.md:118-125). A doc asserted a
  fix that never happened — treat every "this is wired" sentence as a claim, not a fact.
- **Fix:** M10, commit `654f842` (2026-07-27). Market-local day boundary, not UTC (22:00 in
  New York is already tomorrow in UTC — a UTC boundary hands out a second full allowance
  every evening, and crypto trades 24/7); counted at intent **creation**, not fill (the cap
  exists to bound an unthrottled decision loop); `RiskEvidence.opening_orders_today` became
  `int | None` defaulting to `None` — **an uncountable day is UNKNOWN ⇒ blocked**, "a cap
  that reports full headroom precisely when it cannot see has stopped existing". Closing
  intents stay uncapped. Tests: `tests/safety/test_opening_cap_exercised.py` (14), each half
  of the fix reverted in turn to confirm a distinct test fails.
- **Status:** MITIGATED (M10, 2026-07-27), RISK_REGISTER.md:33. **Residual:** the count is a
  per-process read, not transactionally fenced against the write — two racing backends could
  each see the pre-write count. R-24's writer lease is what makes that a single-writer
  question, and R-24 has its own live residual.
- **Why it matters most:** R-26 and R-27 failed closed (blocked everything — safe but
  paralyzing). R-25 failed **open**: it passed everything. It is the one of the four that
  could have cost money.

### 1.3 R-26 — Market-session gate: permanently AMBIGUOUS

- **Symptom:** with fail-closed logic, **no live equity or option order could EVER have
  passed the risk engine** — and no test had ever seen the gate say OPEN. Invisible because
  nothing live ever ran.
- **Root cause:** `BrokerRiskEvidenceProvider._broker_confirms_open` **hard-returned `None`**
  from (wheel) Milestone 5 onward. The tri-state session logic in
  `chronos.services.trading_hours` was complete and correct the whole time — it simply had
  no evidence supplier, so every in-hours instant resolved to AMBIGUOUS ⇒ blocked. The
  wrong-object read, verbatim from commit `701ebf4`: "**`liquidHours` and `timeZoneId` live
  on IBKR's `ContractDetails`, not on the `Contract` inside it, so
  `instrument_from_contract` never saw them**" — the evidence "was arriving on EVERY
  qualification and being dropped one attribute short of the code that needed it."
- **Fix:** M9, commit `701ebf4` (2026-07-27). New pure parser
  `chronos.services.liquid_hours` (both IBKR format vintages, `;`/`,` separators, overnight
  windows, `2400` = midnight); contracts now carry the evidence, so the answer costs zero
  broker round-trips. The load-bearing token is `CLOSED` — the venue saying a normal-looking
  Friday (e.g. 2026-07-03, 11:00 New York) is not a trading day, which no weekday-and-clock
  calendar can derive. Every failure mode degrades toward blocking; malformed-input tests
  outnumber happy-path 13 to 5. Tests: `tests/safety/test_liquid_hours.py` (29),
  `tests/safety/test_session_gate_exercised.py` (9) — the latter asserts all three outcomes
  "including the one that had never happened" (OPEN).
- **Status:** MITIGATED (M9), RISK_REGISTER.md:34. **Residual:** parser verified against
  fixtures, never a live gateway.

### 1.4 R-27 — Option-deliverable verification: only the demo broker ever set it

- **Symptom:** `standard_deliverable_verified` FAILed every option order against a real
  gateway; the entire option path was unproven outside demo. Fail-closed, so invisible.
- **Root cause:** exactly one thing in the codebase set `deliverable_verified=True` —
  `DemoBroker`, **by fiat**. Neither IBKR adapter populated it. Same wrong-object read as
  R-26, one layer over (commit `c72a8e5`): "**`underConId`, `underSymbol` and `underSecType`
  live on `ContractDetails`, not on the `Contract` inside it, so `instrument_from_contract`
  had never seen them.**"
- **Most damning detail:** a unit test **pinned the defect for six milestones** — a line in
  `tests/unit/test_ibkr_broker.py` asserted `deliverable_verified is False`; it now asserts
  `is True` (tests/unit/test_ibkr_broker.py:602). A passing test can be a monument to a bug.
- **The stake:** assignment math. A short put's obligation is `strike × multiplier ×
  contracts` — true only for a *standard* contract. An OCC-adjusted series (non-whole split,
  spinoff, merger, special dividend) delivers 150 shares, shares plus cash, or another
  issuer's stock; sizing it as 100 "reserves the wrong number, in the direction that leaves
  the account short at assignment."
- **Fix:** M11, commit `c72a8e5` (2026-07-27). `chronos.services.option_deliverable` screens
  each qualified option on **five necessary, conjunctive conditions** (underlying named;
  underlying is STK; underlying symbol == option root; OCC root still equals the symbol;
  multiplier is 100). A failing contract is returned **unchanged** (the pre-M11 state), "so
  failing is never worse than not having run it." One deliberate asymmetry: an unparseable
  local symbol is *not* held against the contract — "refusing over an unverified cosmetic
  field would make this control inert the way R-25 and R-26 were" — but a parsed local
  symbol that *contradicts* the OCC root refuses. Tests:
  `tests/safety/test_option_deliverable.py` (30), each condition deleted in turn.
- **Honest scoping — why MITIGATED, never CLOSED:** this is a non-standard **detector**, not
  a deliverable **reader**. The TWS API does not expose OCC's deliverable schedule; the
  screen infers *absence of adjustment* from OCC's suffixed-root naming convention
  (RISK_REGISTER.md:35).

### 1.5 Common anatomy — the lesson the repo itself drew

1. **Wired + documented + tested ≠ exercised.** All four had implementations, docs, and
   passing suites; none had ever produced its intended outcome (a renewal, a refusal, OPEN,
   PASS) end to end. Each fix added a `tests/safety/*_exercised.py` test that drives the
   full path and asserts the previously-never-seen outcome, then reverts/deletes the fix
   piecewise to prove a distinct test catches each half. Those proof patterns are owned by
   **chronos-validation-and-qa** — use them for any new safety control.
2. **The wrong-nested-object read is the signature mechanism** (R-26 and R-27 verbatim;
   R-25 was the sibling "ungathered evidence + orphaned counter" variant; R-24 the
   "no caller" variant). The current-code prevention checklist and the full
   Contract-vs-ContractDetails field map are owned by **chronos-ibkr-boundary** — load it
   before touching any IBKR qualification or adapter code, so this class does not recur a
   fifth time.
3. **Neither failure direction produced a test failure.** Fail-closed hid two (everything
   blocked, nothing live running to notice); fail-open hid one (everything passed, nothing
   refused to notice). Production never caught any of them **because there has been no
   production** — no real IBKR gateway (paper or live) has ever been connected in this
   project's history (docs/limitations.md:22-23).
4. **Docs and tests can both lie in the comfortable direction.** ADR-0010 §4 claimed a fix
   that didn't exist (1.2); a unit test pinned R-27's defect for six milestones (1.4).
5. **All four are MITIGATED, none is CLOSED** — "each keeps a disclosed residual, and
   per-family live promotion still needs owner verification against a real gateway"
   (README.md:102-103; final paragraph of `c72a8e5`). Every fix is fixture-verified only.
   Do not describe any of them as "closed" or "gateway-proven."

---

## 2. Earlier adversarial-review catches (wheel platform, pre-shallow-boundary)

`docs/INDEPENDENT_REVIEW.md` (round 1, 7 reviewer agents) and `docs/REMEDIATION_REPORT.md`
predate the visible git history and caught a *different* bug set. "Caught by adversarial
review" is the process constant; the specific pass for R-24..R-27 was the later M0 audit.

| Finding | What it was | Disposition & current status |
|---|---|---|
| C1 (CRITICAL) | `ibkr_paper.py` docstring + TEST_RESULTS claimed unit-test coverage that did not exist (INDEPENDENT_REVIEW.md:28) | Fixed: 18 tests written, claims corrected. The adapter itself was later quarantined entirely (R-28, §3) |
| H1 — halt TOCTOU | `submit_approved` read the halt once, then did ledger fsync I/O before `broker.submit`; a halt landing in the window was missed, order still reached the broker (reproduced) (INDEPENDENT_REVIEW.md:32) | Fixed: halt re-read immediately before submission. Now **R-20, MITIGATED** with regression test (RISK_REGISTER.md:22) |
| H2/H3 — fill translation | IB status text trusted over fill quantity: FILLED emitted with `filled < total` (remainder silently lost); a full fill under an ACKNOWLEDGED status dropped as a no-op (INDEPENDENT_REVIEW.md:33-34) | Fixed: fill quantity authoritative for every fill-relevant kind, tested (REMEDIATION_REPORT.md:14-15). Lives in the now-quarantined `ibkr_paper` adapter |
| H4 — audit-log corruption | `AuditLog.__init__` crashed uncleanly on a corrupt last line (INDEPENDENT_REVIEW.md:35) | Fixed fail-closed: specific `AuditLogCorruptionError`, CLI halts with `AUDIT_LOG_FAILURE`. Now **R-14, MITIGATED** (RISK_REGISTER.md:21) |
| H5 — undisclosed cap-widening | Research caps raised to USD 10M in the same commit that froze criteria; under the original USD 3,000 caps the flagship makes **7 trades, not 18** — the "missed by 2" narrative was cap-dependent (INDEPENDENT_REVIEW.md:36) | Fixed: disclosure added, near-miss language removed. Pass/fail outcome unchanged (both fail the 20-trade floor either way) |
| M4/M5 — accepted MEDIUMs | Presence-only reconciliation; no restart hydration of in-flight orders (INDEPENDENT_REVIEW.md:44-45) | Accepted-with-documentation then, later MITIGATED as **R-22/R-23** by the deterministic platform's M2 service loop (RISK_REGISTER.md:24-25) |

**Round 2** (`docs/INDEPENDENT_REVIEW_M5.md`, from `1663e96`, 2026-07-17): no criticals; two
HIGHs — the burned-holdout disclosure (INDEPENDENT_REVIEW_M5.md:28; full story §3 and §4)
and a vacuous deny-monotonicity property test ("deleting e.g. the aggregate-exposure check
would pass the whole suite"), closed with 12 per-limit breach tests.

---

## 3. Other settled defects, each with its commit

- **Zero mandate ceiling authorized everything, not nothing** — `4b6bc9e` (2026-07-25,
  "M2 fix: a zero mandate ceiling must authorize nothing, not everything"). `size_order()`
  **skipped** a mandate limit that was zero instead of binding on it: a mandate whose
  capital ceilings were all zero — a mandate that authorizes *nothing* — sized to whatever
  cash allowed, reproduced at 590 shares where the correct answer is "refuse". "The
  docstring claimed 'zero authorizes nothing'; the arithmetic did the opposite." Found by
  self-review before any autonomous path consulted it. Fixed at both layers (every ceiling
  binds, zero binds at zero; the contract refuses to construct such a mandate). **The
  general class:** a zero/`None` default silently read as "no limit" instead of "no
  authority" — the same deny-by-default inversion as R-25's `0` default. Any `if limit:`
  guard on a Decimal/int ceiling is a suspect.
- **An ADR claim contradicted by the repo's own safety test for four milestones** —
  `3199a17` (2026-07-26, "M8d: the theses, and a claim that was not true"). ADR-0016 §5 had
  said since M1 that thesis/rationale/uncertainties/invalidation were "recorded, displayed,
  and audited". Grep showed nothing read them; the bytes survived only in an opaque queue
  payload outside the hash chain. Root cause preserved in the message: the safety test
  `test_no_deterministic_module_reads_a_narrative_attribute` forbade ANY access to those
  fields, "so the guard made the ADR's own promise unimplementable: a test and a published
  claim had been contradicting each other for four milestones, and nothing forced the
  question because nothing had tried to do the thing." Fix **narrowed, not weakened**: a
  named-module exemption held to a stricter copy-only rule, verified by breaking it.
- **Chart pacing budget recorded after the call** — the first implementation of the M8c
  chart panel (ADR-0019, introduced by `b7b0cd5`) recorded the pacing budget only on success, so
  "a failure that consumed nothing would let a bad symbol retry every poll unthrottled — a
  real defect in the first implementation, **caught by its own test**" (RISK_REGISTER.md:47,
  R-42; same rule recorded in DECISIONS.md D-19: budget is recorded **before** the call).
- **Order submission not bound to fresh reconciliation** — fixed by `49fdc81` (2026-07-21,
  "fix: bind order submission to fresh reconciliation", PR #30, 40 files): submission now
  requires fresh reconciliation evidence via a generation-bound latch. Known residual: the
  latch is consumed by one opening submission and nothing re-arms it automatically
  (docs/VISION_COMPLETION_PLAN.md:143-145) — that is a missing periodic loop, not a bug in
  the latch; do not weaken the latch to "fix" a blocked second order.
- **The M5-review burned-holdout failure → the registry + guardian** — the round-2 review
  found the research report claiming its final window was "pristine" when the M1 re-run's
  `--stage all` had computed and committed final-window results, consuming QQQ's one-shot
  holdout (INDEPENDENT_REVIEW_M5.md:28). Process fixes: `--stage all` no longer includes
  `final`; ADR-0013/D-15 built the hash-chained trial registry + holdout guardian so a
  burned window is *detected and refused*, not merely remembered. **The residual trap,
  in one line (2026-08-02): the registry ledger ships EMPTY while the QQQ
  2022-01-03..2024-01-10 holdout IS burned — never infer holdout cleanliness from the
  empty ledger.** Full current state (CLI outputs, the documentary burn records) and the
  holdout discipline: **chronos-research-methodology §7**.

---

## 4. Honest outcomes that are RESULTS, not failures

Do not "fix", re-run-until-green, or soften any of these. They are the evidence discipline
working as designed (`AGENTS.md:23-24`: "A correct NO_TRADE result is success").

| Outcome | Evidence | What it is NOT |
|---|---|---|
| **Zero strategies selected — twice.** Original two-symbol run, then the broadened 5-symbol re-run with criteria re-frozen unchanged *before* results (`e693980`), "verdict unchanged: zero selected" (`e4523c6`, 2026-07-17). "Selected candidates: NONE" (docs/STRATEGY_SELECTION.md:8) | Broadening 2→5 symbols confirmed low trade frequency is structural, not a QQQ artifact (`e4523c6` message) | Not a platform bug; not an invitation to lower the floor |
| **Best cell 18 < 20.** C4's frozen ≥20-closed-trades floor is never met; max is 18 (regime_trend_v1 on QQQ) (docs/RESEARCH_REPORT.md:143-145) | Under the original USD 3,000 caps it would be 7, not 18 (INDEPENDENT_REVIEW.md:36, H5) — the "near miss" was cap-dependent | Not "so close we should pass it" — the floor exists for exactly this case |
| **Intraday corpus classified research-only.** No trustworthy intraday data obtainable here; scripts marked research-only "regardless of their code quality" (ASSUMPTIONS.md A-31, line 66; RISK_REGISTER R-11) | A data-availability classification | Not a merit rejection of those scripts |
| **"Brief said ~77, index contains 42."** The build brief described ~77 Pine scripts; the authoritative Notion Master Index catalogs 42; the discrepancy was recorded rather than scripts invented (ASSUMPTIONS.md A-01, line 10) | Honest inventory | Not missing data to go hunt for |

---

## 5. Pivots and rejections — settled decisions; do not relitigate

Reopening any of these requires new evidence and a new ADR (route: **chronos-change-control**).

### 5.1 The 2026-07-25 autonomy pivot: D-11 → D-16/D-17

The founding rule "no generative model output feeds any runtime decision" (D-11) is **struck
through in DECISIONS.md:18**, superseded by D-16 on an explicit owner directive re-scoping
Chronos as an autonomous model-driven system. ADR-0016 (controlled model authority, commit
`6feaea9`) and ADR-0017 (owner-directed maximal autonomy, commit `257d583`) were adopted the
**same day**, 2026-07-25. Two scoping facts future sessions must not blur:

- **"Maximal" was scoped as ceilings-not-mechanisms** (DECISIONS.md:26-27, D-17): removing
  friction and owner-optional ceilings, never execution-correctness mechanisms. The "Not
  superseded" list — floors/reserve, single transmit site, writer lease, kill switch,
  reconciliation, stale-data refusal, the full propose→preview→confirm→submit handoff,
  deny-by-default elsewhere — is load-bearing.
- **The literal-unbounded-market-order interpretation was explicitly NOT taken**:
  `OrderForm.MARKET` compiles to a protected collared limit (quote ±1%), never a venue
  `MKT`, "with the literal-unbounded interpretation flagged as a separate un-taken decision"
  (DECISIONS.md:26, D-17). Anyone proposing true market orders is reopening a decision the
  owner already declined, not filling a gap.

Full authority architecture: **chronos-autonomy-and-mandates**.

### 5.2 Rejected alternatives (with the evidence that killed them)

| Rejected | Decision | Why (evidence) |
|---|---|---|
| Adopting **tyche** as the operator terminal | D-18 / ADR-0018 (DECISIONS.md:25) | Structurally unforkable for this use: UI modules are build-time only; its `DataProvider` plugin plane has no `portfolio` method, so positions/orders/account cannot ride the sanctioned extension point; declares itself "not a broker, no order-placement path". Seven-agent reconnaissance read the codebase before the call |
| Forking **midas** | D-18 (same row) | AGPL-3.0-only license; ~128-board crypto catalog is dead weight for IBKR equities at a ~USD 110 account; parser mangles IBKR option/future symbols; order routes are a hardcoded 503 — the trading surface would still be written from scratch |
| Any external terminal shell at all | D-18 | The decisive fact: **the dominant cost was Chronos-side and identical under every option** (no routes existed for tick health, queue depth, journal, counters, mandate state). A TS shell removed none of that and added a second runtime beside the process that moves money. Terminal built fresh in Python; tyche design attributed in NOTICE (Apache-2.0) |
| IBKR **Client Portal Web API** | D-02 / ADR-0002 (DECISIONS.md:9) | Rejected for session-keepalive complexity and weaker order-event semantics. Chronos stays on the TWS API. (Era note: D-02 chose `ib_async`; the production live adapter is now `official_ibkr` and `ib_async` is a read-only secondary — the Client Portal rejection is the part that still binds) |
| Serving the terminal **chart from the histdata/research store** | D-19 / ADR-0019 (DECISIONS.md:24) | Rejected on the data itself: SPY ends **2019-11-14**, IWM covers 2019-2021, corpus heterogeneous (some adjusted, some nominal, some 2-decimal transcriptions) — "backtest material and useless for supervising a live trader" — and it would drag ADR-0013's holdout question into a display surface. Chart reads bars from the broker |

### 5.3 The capital revision: ~USD 3,000 → ~USD 110

The funded-account premise was ~USD 3,000 (ADR-0008:9; ASSUMPTIONS.md A-10). The reality —
**~USD 110 cash** — was verified as early as 2026-07-17 ("Capital reality (verified
2026-07-17): the IBKR account holds ~USD 110 cash", docs/AI_QUANT_GAME_PLAN.md:50) and is
canonical in docs/VISION_COMPLETION_PLAN.md:68. **This is a LIVE, UNRESOLVED owner
decision** — whether to fund toward the $3k premise or re-plan around $110. Never quietly
assume either number. Multiple docs still carry the unamended $3k premise (ASSUMPTIONS.md
A-10/A-21/A-22, RISK_REGISTER R-10, HANDOFF.md:121, GO_LIVE_CHECKLIST.md:179, among
others); the per-line ledger of which docs are stale is owned by **chronos-docs-map**.

---

## 6. Archaeology limits and traps — read before citing history

1. **This is a SHALLOW clone.** `git rev-parse --is-shallow-repository` → `true`;
   only **~150 commits** were visible at authoring (2026-08-02; the count grows with
   skill-library checkpoint commits — re-verify with `git log --oneline | wc -l`); the
   apparent first commit `a65c7b3` (2026-07-16) is a
   **graft point** that "adds" the entire pre-existing tree, not the real repo root. The
   original wheel-dashboard M1-M10 build history is **unexcavatable locally** — README.md
   and docs/architecture.md prose (banner: "read it as the historical M1-M10 posture",
   architecture.md:3-11) are the only records. Label any claim sourced from them as
   doc-claimed, not commit-verified.
2. **Three overlapping milestone-numbering schemes.** A bare "M5" citation is ambiguous
   without its scheme — always name which one:
   - *Platform-hardening M1-M6* (2026-07-17): M3 monitoring plane `60e8c00`, M5 =
     adversarial review round 2 `1663e96`, M6 handoff refresh `dc8aba2`.
   - *Wheel/live-plan "Milestone 2-8" + M7/M7C/M8a-e* (07-17→07-19): Milestone 2 = IBKR
     adapter `a555a89`; **Milestone 5 = the paper order pipeline `22e2b7c` where the inert
     cap and the never-supplied session gate were born**; M7 = ADR-0009 live branch.
   - *Autonomy-governance M1-M11* (07-25→07-27, ADR-0016's own sequence): M1 reset
     `6feaea9` … M9 `701ebf4`, M10 `654f842`, M11 `c72a8e5`.
3. **Zero deletions in the visible history.** `git log --diff-filter=D --summary` returns
   empty. The dangerous dormant transmit path was **quarantined, not deleted** (R-28,
   RISK_REGISTER.md:36-37: `IBKRPaperExecutionAdapter` refuses construction without
   `quarantine_ack=True`, which nothing in src/ passes, pinned by
   `tests/safety/test_broker_mutation_inventory.py`). The house culture is: keep history and
   tests, correct false claims **in place and visibly struck** (ADR-0010's correction block;
   GO_LIVE_CHECKLIST.md:185-189's struck-through sentence) rather than rewrite. Follow it —
   do not delete quarantined code or edit old claims away.

---

## 7. When NOT to use this skill

| You actually want | Go to |
|---|---|
| The current-code prevention checklist for the wrong-nested-object class; the ContractDetails field map | **chronos-ibkr-boundary** |
| Which document to trust today; the stale/contradiction ledger (incl. every $3k reference) | **chronos-docs-map** |
| Deciding what to work on next; current status | **chronos-priorities-and-roadmap** |
| The `*_exercised` / revert-the-fix proof patterns for a NEW control | **chronos-validation-and-qa** |
| Holdout mechanics, trial counting, the statistical gates | **chronos-research-methodology** |
| Whether you may reopen a settled decision; how to write the superseding ADR | **chronos-change-control** |

---

## Provenance and maintenance

Compiled 2026-08-02 at HEAD `47a8d72` from read-only `git log`/`git show` and the cited
files. Commit messages quoted here are immutable; the **statuses** are volatile — re-verify
before repeating them:

| Volatile fact | Re-verify (read-only) |
|---|---|
| R-24..R-27 all MITIGATED, none CLOSED | `grep -n "R-2[4-7]" RISK_REGISTER.md` and `grep -n "none is closed" README.md` |
| No real gateway ever connected | `grep -n "never been exercised" docs/limitations.md` |
| Registry still empty / burn still undocumented in-ledger | `ls research/registry/ 2>&1; cat research/data/history/HOLDOUTS.json` |
| QQQ 2022-2024 burn record | `grep -n "consumed" docs/VISION_COMPLETION_PLAN.md docs/RESEARCH_REPORT.md` |
| Capital still unresolved ($110 vs $3k) | `grep -n "USD 110" docs/VISION_COMPLETION_PLAN.md; grep -n "USD 3,000" ASSUMPTIONS.md` |
| Zero strategies selected | `sed -n '1,15p' docs/STRATEGY_SELECTION.md` |
| Clone still shallow / deletions still zero | `git rev-parse --is-shallow-repository; git log --diff-filter=D --summary \| head` |
| Any commit quote | `git show -s <hash>` (e.g. `701ebf4`, `654f842`, `c72a8e5`, `6feaea9`, `4b6bc9e`, `3199a17`) |

Maintenance rule: when a residual here is genuinely closed (e.g. first real-gateway
verification lands — see **chronos-real-gateway-campaign**), update the status line in the
affected entry and cite the new evidence; never delete the entry. This file records battles
so they stay fought once.
