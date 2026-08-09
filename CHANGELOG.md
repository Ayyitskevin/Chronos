# CHANGELOG

## [Unreleased] — the Five-Tool research slice: fidelity and preregistration, no evidence of edge (2026-08-09)

Integration of `codex/five-tool-confluence-v36` — a deterministic research-plane
translation of the owner's `research/pine/00_five_tool_confluence_aio.pine` (SHA-256
pinned) — plus the adversarial review that conditioned the merge.

**What this is.** Implementation fidelity and experiment design. A frozen executable
input contract (`specs/five_tool_confluence_v3_6.yaml`), one causal transition kernel
shared by batch evaluation, streaming, and checkpoint replay, a strict TradingView
trace-comparison harness, and a preregistered six-hypothesis falsification contract
(`docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md`) that registers its rejection tests before any
data is read.

**What this is not.** It is not evidence that Five-Tool, or any component of it, has
edge. No hypothesis was tested, no campaign was run, no data was acquired, and the trial
ledger still ships empty. The campaign manifest
(`research/five_tool_v3_6_campaign_manifest.json`) stays
`blocked_until_identity_locks_resolve` with `performance_claims: []` and
`promotion_authority: none`, and this integration does not unblock it. Its own document
says the quiet part: "Agreement with the Pine calculation would establish implementation
fidelity, not alpha." The parity harness runs against a synthetic trace only — no real
TradingView export exists yet (A-03). QQQ 2022-01 through 2024-01 remains burned and is
named as contaminated in the manifest; the declared 2026-Q4 holdout is future,
uncollected, unopened, and forbidden to ordinary research access.

The manifest names the four capabilities that must exist before it can run: a certified
broker-owned reader, content-addressed replay artifacts, owner-frozen risk/power/benchmark
evidence, and integration with the canonical ADR-0013 registry so multiplicity is derived
rather than self-reported. Three are engineering; the owner evidence is not.

### What the adversarial review changed

The burden was on the branch, and two findings survived first contact.

- **The holdout refusal had never fired.** Deleting the entire body of
  `FiveToolTrialBroker._refuse_holdout` left the full suite green — the R-25/R-26/R-27
  defect class exactly: a protection control that is implemented, documented, and
  structurally unobserved. It was invisible because `_validate_identity` runs first and
  rejects every request the existing tests could build. The refusal is load-bearing only
  for manifests that are structurally valid yet declare a holdout the identity check
  cannot distinguish — one carved from the campaign's own `dataset_id`, or one on a custom
  partition name that manifest validation does not recognise as holdout vocabulary.
  `tests/safety/test_five_tool_holdout_refusal_exercised.py` now drives both shapes and
  asserts the outcome that had never been observed: refusal *before* the reader is called
  and *before* any ledger record exists. Each conjunct was verified by reverting it alone
  and confirming a distinct failure.
- **Unread risk, exit, and provenance fields.** `PositionPlan.initial_stop_price`,
  `.risk_distance`, `.planned_quantity`, `.unallocated_quantity`, the `QuantityPlan`
  intermediates, and the trace's lookahead-provenance identifiers
  (`primary_sequence_id`, `benchmark_source_id`, `htf_source_id`) are written and never
  read back. None can reach an order — the package is import-isolated and the campaign
  refuses all data — so these are disclosure obligations rather than live defects, and
  they are now disclosed in the module docstrings and pinned by
  `tests/safety/test_five_tool_inert_fields_disclosed.py`. A new unread field fails that
  test until it is classified or removed; a disclosed field that becomes read fails it
  too, so the disclosure cannot go stale. The provenance identifiers are the audit trail
  the preregistration's own no-lookahead test will need, so their being unread is a gap to
  close before a campaign runs, not a decoration to delete.

Verified to fire, not merely to exist: `tests/safety/test_five_tool_isolation.py` was
exercised by planting a forbidden `chronos.orders` import and, separately, a computed
`importlib.import_module` evasion — the AST walk, the dynamic-import branch, and the
subprocess `sys.modules` probe each failed with a distinct message. The blocked-manifest
refusals in `tests/unit/test_five_tool_trials.py` were exercised the same way: reverting
the public `EXECUTION_READY` rejection fails
`test_public_ready_manifest_cannot_authorize_reader_or_ledger` alone, and reverting the
reader block fails `test_direct_construction_has_no_read_authority` and
`test_checked_manifest_is_valid_but_blocks_before_reader_and_ledger` on the refusal
*reason*, not merely on the exception type.

### Drift fixed forward

The branch predated the default tip by ~37 commits. One real failure:
`test_arbitrary_serialized_chunk_boundaries_match_batch` exceeded hypothesis' 200 ms
per-example deadline. The batch reference and fixture case are now computed once instead
of per example, and the deadline — a wall-clock timer measuring machine speed, not the
parity property — is waived, following the existing precedent at
`tests/platform_unit/test_property_invariants.py:319`. No assertion was weakened and every
generated boundary set is still checked.

Suite after integration: **2745 passed, 1 skipped** (was 2543/1 at `721d7f1`); ruff check,
ruff format, and `mypy --strict src/chronos` clean over 232 source files.

### Canonical ADR-0013 registry integration — trials are counted, or they do not run (2026-08-09)

The first of the three non-owner capabilities the manifest names. ADR-0013 §5 derives the
multiple-testing N from *registered* runs and its §11 discloses the hole underneath that:
"completeness of the **trial count** ... auto-registration of the runner is a follow-on".
A registry that counts only the runs someone remembered to register reports N=3 while the
world did 40 — and the deflated Sharpe is computed against that number.

`FiveToolTrialBroker` now writes an `experiment_run` record into the canonical
hash-chained registry **before** the durable local start, and therefore before the reader
is ever called. It verifies the chain **and** the head anchor before it trusts either, the
way the holdout guardian does, and fails closed on every way a registry can be missing:
unwired, an absent registry root (a research run may not conjure the ledger that judges
it), an unreadable head record, an in-place edit, or a truncated tail. Each refuses with a
distinct reason, the reader never runs, and no durable trial is created. Sealing a campaign
additionally refuses when the registry counted fewer attempts than the campaign ledger
recorded, so a campaign run around the registry cannot produce score inputs.

Ordering is deliberately conservative. If the local start fails *after* the canonical
record lands, the registry keeps a record for an attempt that never read data: that
over-counts N, which raises the bar. Under-counting is the failure being prevented. This is
the opposite choice from `walkforward.py`, which registers *last* — it is handed an
already-read series and declines to count a cell that raised mid-statistics. The two
orderings disagree on purpose; the disagreement is recorded in ADR-0013 §11 and
`docs/limitations.md` rather than resolved by an agent, because changing which runs count
changes a frozen multiple-testing input.

`tests/safety/test_five_tool_registry_exercised.py` (22 tests) drives the whole path and
asserts the outcomes that had never been observed: the canonical record read from *inside*
the reader callback, so register-then-read is proven rather than asserted; an attempt that
dies mid-read, mid-evaluation, or on unauthorized bytes still counted; the derived count
aggregating two campaigns that keep separate trial ledgers. Every refusal conjunct was
verified by reverting it alone and confirming a distinct named failure — including the
ordering itself: moving registration after the reader fails
`test_the_canonical_record_exists_before_the_reader_sees_a_byte` specifically.

**What this is not.** The registry still ships **empty** — `research/registry/` does not
exist, no trial has been run, and every ledger in the tests lives in a temporary
directory. This is a capability, not evidence. The campaign manifest remains
`blocked_until_identity_locks_resolve`, and its `EXECUTION_READY` refusal now names the
three capabilities that are still missing — certified reader, replay artifacts, owner
evidence — and no longer the registry. That message is built from
`MISSING_CERTIFIED_RESEARCH_CAPABILITIES`, each entry backed by a test that independently
observes the absence (no certified-reader module; evidence artifacts digested but never
persisted, so no replay; the manifest's owner-frozen limits and power calculation still
unfrozen), so the list cannot go stale as those capabilities land. The reader is still an
arbitrary callback that cannot prove what data it touched, and the manifest's own blocker
list is left untouched: editing it is manifest surgery on a frozen preregistration, not an
agent's call. Nothing here imports, calls, or re-exports the holdout unlock — a subprocess
probe proves that counting trials never loads the guardian module at all.

Suite after this capability: **2767 passed, 1 skipped** (was 2745/1); ruff check, ruff
format (410 files), and `mypy --strict src/chronos` clean over 232 source files.

## [Unreleased] — M12: the handoff library, document truth, and finding 1 (2026-08-02)

Nine merged PRs across one day. The theme is not new capability — it is making the
repository's own claims true, and building the structural guards that keep them true after
the session that wrote them has ended.

### The `.claude/skills/` library (PR #48)

Sixteen repo-specific skills so a future session — human or model — can maintain and advance
Chronos without re-deriving it. Produced by a discovery → authoring → three-lens review →
fixer process: eight parallel investigations with file:line evidence for every claim, one
author per skill, then factual, doctrine, and usability review over the complete set. Zero
BLOCKING findings; eight IMPORTANT, all fixed.

It earned its place the same day it landed. `test_the_terminal_routes_do_not_import_the_order_plane`
caught the first draft of the terminal stop buttons reaching into the order plane, and the
drift checker exposed a false-positive flaw in its own design once real corrections started
landing. A library that catches its own author is doing the job.

### Document truth (PRs #49, #52)

Twenty-one documents corrected in the house style — original wording struck through in place
beside a dated correction, so history stays visible. The two that mattered:

- **`INCIDENT_RESPONSE.md`** named only the deterministic platform's halt. That halt does not
  stop the plane that can place an order. An operator following the runbook under stress
  would have engaged the wrong control. The live kill switch is now step 1, the platform halt
  an explicitly-scoped step 2, and mandate revocation step 3 — because a valid mandate file
  auto-activates on boot, so restarting is not a stop.
- **`BACKUP_AND_RECOVERY.md`** claimed "restore must never auto-resume trading, and the code
  guarantees it". True of the deterministic platform; false of the live plane, where a
  **missing** `data/live_kill_switch.json` reads DISENGAGED — the opposite default. That file
  and the mandate file were absent from the backup table entirely. A restore following the
  documented procedure could have come up with the emergency stop disarmed.

`doc_drift_check.py` gained a `CORRECTED` verdict: its substring matching reported
correctly-repaired documents as still stale, because house-style corrections keep the
original wording on the page. It was punishing the correct fix.

### Terminal emergency stop (PR #49)

ADR-0018 §4 already granted the terminal the kill switch and disarm; what was missing was a
route the *browser* could reach, since the M8b session cookie is scoped `path=/terminal` and
structurally cannot call `/live/kill`. `POST /terminal/live/kill` and
`POST /terminal/live/disarm` close that, with typed confirmation and a required reason.

Only the authority-**removing** half is exposed. Arming and kill-disengage *grant* authority,
and whether a browser session should hold that is an owner posture question — their absence
is pinned by a test. Both routes work on a demoted, read-only backend, mirroring
`chronos.api.routes.live`'s deliberate asymmetry: the backend that lost its lease is the one
whose operator most needs the stop. Recorded as R-43.

### ADR-0020 — bounded periodic reconciliation, closing finding 1 (PRs #50, #51)

`ReconciliationReadiness` is consumed by every opening submit and was re-established by
exactly one caller: the startup call. So the first opening order of a process consumed
readiness and **nothing ever re-armed it** — every later opening order blocked until restart.

Shipped in two halves deliberately. The maximum evidence age landed first, unwired: switching
expiry on without a refresher would have tightened with nothing to re-arm it. The refresher
landed with the wiring.

Owner-frozen thresholds — 120 s positioned in RTH, 240 s flat, 1800 s closed, 300 s maximum
evidence age — set by what can change the book without an order this system placed (a fill on
a resting limit; overnight assignment), and bounded by the shared pacing budget, where
headroom is a safety property: rate limit spent watching is rate limit unavailable to cancel.

Two properties worth keeping: expiry is evaluated in `snapshot()` rather than by the loop, so
a proof stops being trusted whether or not the component guarding it is alive; and missed
cycles fail closed **by arithmetic** rather than by a failure detector. The 300 s age also
subsumes the session-open rule — a proof from before the open cannot survive it — so
overnight assignment cannot be traded against on stale evidence. That fell out rather than
being built.

This change **widens**: today's state fails closed after one order, so re-arming makes
submissions possible that are blocked now. Gated as a widening accordingly.

### Phantom configuration (PR #53)

Five `.env.example` variables were read by nothing — not a `Settings` field, not an
`os.environ` lookup, and `extra="ignore"` swallowed them silently. `PAPER_ACCOUNT_ALLOWLIST`
was the dangerous one: the allowlist it appeared to set is real and load-bearing, but is fed
from `IB_ACCOUNT_ALLOWLIST`. An operator who set the phantom name and believed they had
restricted which accounts could trade had changed nothing.

Fail-closed meant the worst case was a refusal rather than a wrong account — luck about which
direction the mistake pointed, not a property of the design. R-25 was the fail-*open* member
of that same family. `tests/safety/test_env_example_has_no_phantom_settings.py` now fails on
any advertised variable that nothing reads.

### Decision memos (PRs #52, #54)

ADR-0021, 0022, 0023 and 0024 — Phase-1 findings 7, 4, 6 and 8. All **proposed**; accepting an
ADR is an owner act. Each was frozen behind analysis nobody had done, so the analysis is done
and the owner's part is choosing rather than investigating. Three sharpened their own findings:

- **Finding 4** is not "prose versus code" — it is inconsistent *between the two gates*. Gate 8
  (typed confirmation) is already effectively replaced by the wiring calling `confirm()`
  itself; gate 7 (arming) is not.
- **Finding 6**'s two halves are one problem. The ingress correctly refuses worker-declared
  provenance, so identity can only come from which credential authenticated — and a single
  shared token cannot express that. `evidence_bundle_digest` is 64 literal zeros.
- **Finding 8**'s maximally-fail-closed option is a trap: forbidding rungs above replay blocks
  the rungs that must run to *produce* the evidence. A safety default whose exit condition is
  unreachable is a deadlock, not a default.

### `chronos mandate check` — reading the grant back to the owner (PR #59)

A mandate can be valid, activate cleanly, appear complete, and still constrain nothing the
owner thought it did. That is this repository's signature defect (R-24 … R-27) one level up
from the kernel, and until now nothing looked for it.

`python -m chronos.cli mandate check --file <path>` validates a mandate exactly as
`load_persistent_mandate` does, then reports what it *actually* authorizes: which limits are
inert, whether the account fingerprint matches this machine (a mismatch boots autonomy inert
behind one log line), whether the version pins agree with the ingress stamp (a mismatch
refuses every proposal *after* admission begins), and whether the window has closed. It exits
1 on anything blocking; `--strict` also fails on important findings. `mandate template` prints
a SHADOW skeleton to stdout and `mandate fingerprint` maps an account id to its pseudonym.

It authors nothing. There is no write path, `test_no_mandate_command_writes_anything` asserts
that over the filesystem rather than over the code, and the module holds the narrowest of the
three `_MANDATE_ONLY_MODULES` exemptions.

The ENFORCED/INERT classification moved out of `test_supervisor_gateway.py` into
`chronos.autonomy.enforcement`, so the test pin and the owner-facing report read the same map.
The pins are unchanged and still authoritative.

### Four contract claims that were wrong (PR #59)

Building the report meant checking each mandate field against the code that reads it. Four
statements did not survive, and all four are corrected in place:

- **`max_quote_age_seconds` was documented as a floor where zero is the most permissive
  value.** Admission compares directly, so zero is the *strictest* setting and refuses every
  quote. The rule requiring it positive stands; the reason was backwards.
- **`max_relative_spread` is the field with that property, and was never listed.** Admission
  skips the spread comparison entirely at zero, so this `max_` field imposes no ceiling at its
  default and nothing requires it to be set. The report says so.
- **`min_option_volume` / `min_open_interest` were called "advisory inputs to the kernel's own
  liquidity checks".** They are inputs to nothing. The strike resolver reads the same-named
  *settings*.
- **`scope.exchanges` and `scope.contract_families` were disclosed as "not enforced anywhere,
  pending contract compilation".** That compilation step landed in M4 —
  `chronos.supervisor.compiler` refuses `EXCHANGE_NOT_PERMITTED` and `FAMILY_NOT_PERMITTED`.
  The first draft of the new tool believed the stale disclosure and reported both as inert,
  which would have told an owner a binding restriction was decoration — the same defect class
  aimed the other way. Caught before merge and pinned by its own test, which scans the
  compiler as well as admission, sizing and durable.

### Repository

A remote `main` was created at the tip of `feat/wheel-dashboard-mvp`; the GitHub default branch
is unchanged and remains an owner action.

### Verification

`pytest -q` 2489 → **2543 passed, 1 skipped**; `ruff check`, `ruff format --check` (385 files)
and `mypy --strict` (221 files) clean throughout; CI green on every merge.

### What this milestone did not do

No real IBKR gateway was connected — none ever has been, and every adapter path remains
fixture-verified only. No strategy was selected; the best candidate still sits at 18 closed
trades against a frozen floor of 20. The wheel still has zero backtested evidence. Findings 3
and 5 keep their code halves open, and 4, 6, 7 and 8 await owner decisions.

## [Unreleased] — M11: the option deliverable, and the last kernel defect (2026-07-27)

Closes RISK_REGISTER **R-27**, the last of the four defects the M0 audit found. The pattern
held to the end: a control that was configured, documented, and structurally incapable of
passing.

### What was actually wrong

`standard_deliverable_verified` gates every option order on
`OptionContract.deliverable_verified`, and exactly one thing in the codebase set that flag —
`DemoBroker`, by fiat. Neither IBKR adapter populated it, so the check FAILed every option
order against a real gateway and the entire option path was unproven outside demo. A line in
`tests/unit/test_ibkr_broker.py` asserted `deliverable_verified is False` for six milestones;
it was pinning the defect.

### Why the deliverable is worth a milestone

A short put's obligation is computed as `strike × multiplier × contracts`. That is true only
for a **standard** contract. When OCC adjusts a series — a split that is not whole-share, a
spinoff, a merger, a special cash dividend — the deliverable becomes something else: 150
shares, or shares plus cash, or another issuer's stock. Sell a put on an adjusted series
while assuming 100 shares and the cash reserved is the wrong number, in the direction that
leaves the account short at assignment.

### `chronos.services.option_deliverable`

Five **necessary, conjunctive** conditions: the broker named the underlying contract; the
underlying is `STK`; the underlying symbol is the option root; the OCC root still equals the
symbol; the multiplier is 100. Any one missing or contradicted refuses, with reasons —
"your option was blocked" without a why is how a safety control ends up switched off.

Same failure mode as R-26 one layer over: `underConId`, `underSymbol` and `underSecType`
live on `ContractDetails`, not on the `Contract` inside it, so `instrument_from_contract`
had never seen them.

A contract that fails the screen is returned **unchanged** — still unverified, still refused,
exactly the pre-M11 state. Failing the screen is never worse than not having run it.

### What this is not

**A non-standard detector, not a deliverable reader.** The TWS API does not expose OCC's
deliverable schedule; no field says how many shares of what a contract delivers. The screen
infers the *absence of an adjustment* from OCC's convention that any deliverable change
produces a new root with a numeric suffix (`AAPL1`, `SPY7`). That is inference from a naming
convention, and R-27 stays MITIGATED rather than CLOSED because of it.

### One deliberate asymmetry

An unparseable local symbol is **not** held against the contract: the OSI root carries no
information the trading class does not already carry, so its absence is not evidence about
the deliverable, and refusing every option over an unverified cosmetic field would have made
this control inert in exactly the way R-25 and R-26 were. A local symbol that parses and
*contradicts* the OCC root is different — that is IBKR's own fields disagreeing, and it
refuses.

### Exercised, not just supplied

`tests/safety/test_option_deliverable.py` (30) drives `ContractDetails` through the screen,
into the qualified contract, into the risk check — and asserts the outcome that had never
happened: PASS. Each condition was then deleted in turn to confirm a distinct test fails.
The `is False` assertion in the adapter tests is now `is True`.

All four M0 kernel defects are now mitigated. **None is closed** — each keeps a disclosed
live residual, and per-family promotion still requires owner verification against a real
gateway.

Gates: ruff clean, mypy strict clean (218 files), **2489 passed**, 1 skipped.

## [Unreleased] — M10: the daily cap, which had never refused anything (2026-07-27)

Closes RISK_REGISTER **R-25**. `max_opening_orders_per_day` shipped in Milestone 5, is
surfaced in the settings page, is documented as a control, and had never once refused an
order.

### Two defects, each sufficient on its own

`BrokerRiskEvidenceProvider.gather` never set `opening_orders_today`, so the field took its
`0` default and the check evaluated `0 + 1 <= limit` on every call, forever. That alone made
it inert. Independently, `OrderIntentRepository.count_opening_since` — the method that would
have supplied the number, which had **zero callers** — also filtered `action == SELL`.
Intersect the two predicates and the gap is exact: `OPEN ∧ SELL` is
`{OPEN_SHORT_PUT, OPEN_COVERED_CALL}`, so `OPEN_LONG_STOCK` and `OPEN_LONG_CRYPTO` were
invisible to the cap and would have stayed invisible even after it was wired.

ADR-0010 §4 asserted both halves had been fixed. Neither had. That claim is corrected in
place, at its source, rather than edited away.

### The day belongs to the market, not to UTC

22:00 in New York is already tomorrow in UTC. A UTC day boundary would split one trading
afternoon across two counters and hand out a second full allowance every evening — and
crypto trades 24/7, so "the evening" is not a corner case. The boundary is market-local
midnight, using the timezone `chronos.orders` already validates, which is the same reasoning
R-34 applied to the autonomy session counters.

### Counted at creation, not at fill

An intent that was created and then refused still consumes the allowance. The limit exists
to bound an unthrottled decision loop; a loop that could mint a thousand rejected intents
without moving the counter would be bounded by nothing at all.

### Unknown is not zero

`RiskEvidence.opening_orders_today` is now `int | None`, defaulting to `None`, and a count
that cannot be taken — no repository, or one that just died — is **UNKNOWN → blocked**
rather than a passing zero. A cap that reports full headroom precisely when it cannot see
has quietly stopped existing. The old `int = 0` default was half of why this never fired, so
it is gone; every canned test provider now has to state its count rather than inherit an
empty trading day. Closing intents remain uncapped: throttling the orders that *reduce*
exposure would be backwards, and hardest on the day the loop had been busiest.

The provider takes the repository through a structural `_OpeningCounter` protocol, so
wiring the cap added no import edge from `chronos.orders` into `chronos.persistence`.

### Verified by breaking it

`tests/safety/test_opening_cap_exercised.py` (14) drives intents through a real repository,
into the provider that computes the boundary, into the check that refuses. Each half of the
fix was then reverted in turn to confirm a distinct test fails — including the one defect
that only a real `gather` call can see: every other test in the file would still pass if
`gather` computed the count and dropped it, which is exactly what the code did for five
milestones.

Gates: ruff clean, mypy strict clean (217 files), **2459 passed**, 1 skipped.

## [Unreleased] — M9: the session gate, supplied and exercised (2026-07-26)

Closes RISK_REGISTER **R-26**, which had been open since Milestone 5 and was the sharpest
of the three kernel defects blocking per-family promotion.

### What was actually wrong

`BrokerRiskEvidenceProvider._broker_confirms_open` hard-returned `None`. The tri-state
session logic in `chronos.services.trading_hours` was complete and correct the whole time
and had no supplier, so every equity and option instant **inside** regular trading hours
resolved to `AMBIGUOUS` — which blocks. Fail-closed, so never a live-money hazard. But it
also meant **no live equity or option order could pass the risk engine at all**, and the
gate had never once been observed saying `OPEN`.

The evidence was arriving on **every qualification** and being dropped one attribute short
of the code that needed it: `liquidHours` and `timeZoneId` live on IBKR's `ContractDetails`,
not on the `Contract` inside it, so `instrument_from_contract` never saw them.

### `chronos.services.liquid_hours`

A pure parser: both IBKR format vintages, the `;`/`,` separator difference, overnight
windows that name the next date on the close, and `2400` as midnight. `UnderlyingContract`
and `OptionContract` now carry the evidence, and the provider reads it off `intent.contract`
— so the session answer costs **no broker round-trip**, because the fact was already in hand.

**The load-bearing token is `CLOSED`.** That is the venue telling you a normal-looking
Friday is not a trading day, and it is precisely the fact a weekday-and-clock calendar can
never derive. 2026-07-03 is a Friday at 11:00 New York, passes every local check, and the
exchange is shut.

### The asymmetry that shapes the tests

A spurious `True` is the only output in this chain that can open a gate which should have
held. A spurious `False` or `None` blocks an order that could have gone — visible and safe.
So every failure mode degrades toward blocking: unresolvable timezone, unparseable string,
a day the schedule never covered, a contract with no hours. The malformed-input cases
outnumber the happy path deliberately.

One asymmetry inside the parser is worth naming: a **whole** unparseable string returns
`None`, but a **single** bad segment inside an otherwise good string is skipped with a
warning. One unreadable day should not discard a week of real evidence, and the skipped day
degrades to unknown rather than to anything permissive.

### Exercised, not just supplied

`tests/safety/test_session_gate_exercised.py` drives the whole path — qualified contract →
provider → session decision — and asserts all three outcomes including the one that had
never happened: `OPEN`. It also pins that a contract *without* hours lands back on exactly
the pre-M9 behaviour, so this milestone cannot have quietly turned "unknown" into "go".

Gates: ruff clean, mypy strict clean (217 files), **2445 passed**, 1 skipped.

## [Unreleased] — M8d: the theses, and a claim that was not true (2026-07-26)

`docs/LECTURE_134_ANALYSIS.md` §4 listed five things Chronos owed the experience it is
modelled on. This closes the last substantive one: *"no view presents here is what the
system believes about each holding and why."*

### The claim that was not true

ADR-0016 §5 has said since M1 that `thesis`, `rationale`, `key_uncertainties` and
`invalidation_conditions` are **"recorded, displayed, and audited"**. Grepping `src/` for
`thesis` returns the contract that defines it and two comments explaining it is *excluded*
from the decision digest — and nothing else. The bytes survived only inside the opaque
proposal payload: unread, unindexed, and outside the hash chain that makes the rest of the
journal tamper-evident. Of the three verbs, one was arguable and two were false.

**The reason it was never true is the interesting part.**
`test_no_deterministic_module_reads_a_narrative_attribute` forbade *any* access to those
fields anywhere outside the contract. Recording requires reading, so the guard made the
ADR's own promise unimplementable. A test and a published claim had been contradicting each
other since M1 and nothing forced the question, because nothing had tried to do the thing.

### Narrowed, not weakened

The guard now exempts modules **by name**, mapped to the single function allowed to touch
narrative — and a new test, `test_a_narrative_recorder_only_copies_it`, holds them to a
stricter rule than the old one could express: the access must live in one named function
whose body contains nothing but a presence guard and a dict of the fields. No comparison, no
arithmetic, no subscript, no call other than `list()`/`str()`. Only the body is walked, so a
`dict[str, Any]` annotation does not smuggle in permission to slice the thesis.

Verified by breaking it: adding `len(decision.thesis) * 100` to the recorder — narrative
influencing a number, the exact hazard ADR-0016 §5 names — fails the new test.

### The journal records what the model said

`_record` now writes the symbol, kind, asset class, confidence and full narrative alongside
the outcome, verbatim rather than summarized (an audit record that paraphrases is a record
of someone's reading). It stays inert: written to an append-only chain, never parsed, and
rendered by the terminal as text and never as markup.

### `THESIS`

A panel of the latest belief per symbol, joined to what is actually held. Two rules shape it:

- **A holding with nothing on record is listed first, not omitted.** `silent_holdings`
  carries positions the model has never mentioned — the single most interesting row on a
  panel whose job is explaining the holdings, and the one a naive implementation drops.
- **Unreadable positions never render as "not held".** If the broker read fails, the panel
  says so once and every `held` reads unknown; "we could not ask" and "no" are different
  facts and only one of them is reassuring.

Positions are read in the route and passed *into* the assembler, so the read-model stays a
pure function of the database.

Also corrected: `commands.py` still claimed "every command ships `takes_symbol=False`",
falsified by `GP` in M8c.

Gates: ruff clean, mypy strict clean (216 files), **2407 passed**, 1 skipped.

## [Unreleased] — M8c / ADR-0019: the chart (2026-07-26)

ADR-0018 shipped the terminal without a chart and said why: no historical-bar route
existed and the `Broker` protocol had no bars method, so a chart would have had nothing
honest to draw. This closes that.

### Bars come from the broker, and the cheap option was rejected on the data
Serving the chart from `chronos.histdata`'s existing store would have cost no broker load
at all. It was rejected after actually reading the corpus: **SPY ends 2019-11-14**, IWM
covers 2019–2021, and R-08 records the symbols are heterogeneous — some dividend-adjusted,
some nominal, some transcribed to two decimals. That is backtest material, and a chart of
SPY ending seven years ago is not a chart. Reading it would also have dragged ADR-0013's
holdout question into a display surface, which is a question worth never having to answer.

### `Broker.historical_bars`
Closed bars only — a forming bar is a number that changes while it is being read.
`official_ibkr` implements it through the existing `RequestRegistry` (a historical response
is an append-only sequence terminated by `historicalDataEnd`, exactly the shape the
registry already models, so no new bridge state was needed). `demo` emits a deterministic
series seeded from the symbol, stamped `source="demo"` and banner-labelled by the panel.
`ib_async` refuses and points at the official adapter, the same way it already does for
crypto. An unparseable bar is **dropped with a warning, never guessed at**.

### Pacing degrades — it never blocks
The load-bearing decision. IBKR paces historical requests harder than anything else it
serves, and this process holds **one** connection shared with the order pipeline and the
autonomy tick.

`chronos.api.bars.BarProvider` caches by `(symbol, interval)` and remembers how much
history it holds, so a 30-day request is sliced from a cached 180-day series rather than
spending a paced request on a subset of what is already in memory. A paced-out request
**serves the cache labelled stale, or refuses** — it never sleeps, because sleeping in a
request handler on this event loop would put a chart in front of an order submission. It
holds no lock across a broker call. The chart panel polls at **two minutes**, not five
seconds, which is a pacing decision as much as a display one.

**A real bug, caught by its own test:** the first implementation recorded pacing budget
only after a *successful* fetch, so a symbol that always failed would retry on every poll
with nothing throttling it. Budget is now recorded before the call — the gateway sees the
request either way.

### `PacingController` moved to `chronos.marketdata`
It began in `chronos.histdata`, whose package `__init__` pulls in the whole research plane
including the holdout machinery — too much to import into the broker-holding process for a
forty-line utility. Duplicating it would have been worse: two implementations of a rate
limit is two places for it to be wrong. `chronos.marketdata` is the neutral vocabulary both
planes already share.

### The terminal
`GP` is the first command that narrows by symbol, so panel dedupe changed from "one per
panel id" to **one per (panel id, symbol)** — `SPY GP` and `AAPL GP` are different
questions. Saved workspaces gained a version and still read the old shape, so an upgrade
does not silently empty a desk. The chart is candles on a canvas with a price axis and no
dependencies; synthetic series get a banner, stale bars say when they were fetched, and a
refusal states its reason rather than drawing an empty plot that reads as "flat".

Gates: ruff clean, mypy strict clean (216 files), **2399 passed**, 1 skipped. No test sends an order.

## [Unreleased] — M8b: the terminal can log in (2026-07-26)

M8a shipped a terminal whose panels all answered `401`: a browser cannot put a header
on a document load, so the client held no credential (R-41). That was disclosed rather
than papered over, and deliberately left for its own change — inventing an
authorization surface in the commit that first exposed mandate revocation would have
been exactly the wrong place. The owner chose the session-cookie route from three
options.

### `POST /terminal/session` — the token, once, for a cookie
- Every `/terminal/*` route now accepts **either** the session cookie or the existing
  `X-Chronos-Token` header, so `curl`, scripts, and any future CLI are untouched.
- The login route is on its own router with no credential dependency — it cannot sit
  behind the credential it issues — and authenticates from its **body** instead, with
  the same constant-time comparison. Keeping it on a separate object is what makes
  that exemption visible: a route added to the main router inherits the check.
- A refusal is a flat 401 saying only `invalid token`. There is one way to be wrong
  here and elaborating would only help something that is guessing. Failures are logged
  (a *local* process probing the token is worth knowing about) and never echo any part
  of what was presented.

### The scope is the security property, not a nicety
`path=/terminal`. The browser never attaches the cookie to `/orders/*`, so the M8a
injection review's named worst case — script in this page riding an ambient credential
into order submission from the process holding the broker connection — is closed
structurally. It is **verified at the server**, not by trusting the browser to honour
the path: an order route asked with the session and no header still refuses.

What the other flags do, and what they do not: `httpOnly` stops script *reading* the
cookie but not *using* it in place, which is why the CSP (R-40) exists alongside it;
`SameSite=strict` keeps other origins from triggering it; sessions are **in memory
only**, so a restart signs every terminal out, because a credential outliving the
process it authenticates to is what this project refuses everywhere else. Ids are
stored as digests, the TTL is checked on every use rather than only on sweep, and the
32-session ceiling refuses the *new* login rather than evicting an old one — evicting
would let anyone holding the token sign the operator out.

**A session is not authority.** `require_writer` still gates every mutation
independently: a demoted backend accepts the login and still refuses the revoke.

### The client
A sign-in gate that appears only when a route answers 401 — including mid-session, when
a TTL lapses or the backend restarts. The token is read from the field, sent, and the
field cleared: no copy in `state`, none in `localStorage`, and none readable back out
of the httpOnly cookie. Panels behind the gate keep their last reading, correctly
marked stale, rather than being blanked — what expired is the credential, not the
knowledge of what was true a moment ago.

Gates: ruff clean, mypy strict clean (215 files), **2384 passed**, 1 skipped.

## [Unreleased] — M8a / ADR-0018: the operator terminal (2026-07-26)

The experience layer `docs/LECTURE_134_ANALYSIS.md` §4 said Chronos owed: a surface that
renders the decision journal, cycle outcomes, mandate state, session counters, and alerts as
a product rather than as tables nobody reads. Two existing owner-built terminals were
evaluated as candidates and both rejected on evidence (ADR-0018); the deciding fact was that
the dominant cost is Chronos-side and identical under every option, so a second runtime would
have bought shell code whose trading surface we would have written from scratch anyway.

### `chronos.terminal` — the contract lives in Python
- `commands.py` is the command registry and the tolerant grammar: the last registry-resolving
  token wins, symbol-shaped tokens before it become the symbol, and nothing resolving is an
  answer rather than an error. It never folds the operator's text — only a copy, for lookup —
  because upper-casing the line destroys IBKR option and future symbols. Registry coherence
  (duplicate codes, aliases shadowing codes, empty panels) raises at **import**: a terminal
  whose tokens mean two things is worse than one that does not start.
- `views.py` is every panel's read model. `MandateLimitView` refuses to render an unset limit
  as a number, because zero lies three different ways here — "nothing permitted" under
  ADR-0016 deny-by-default, "no ceiling at all" under ADR-0017 `model_discretion`, and "not
  enforced" where `durable.limit_breaches` reads zero as unset. Showing `0` for all three
  would tell the owner they have **no** authority at the moment they have **unlimited**.
- Data honesty is the module's organizing rule, and it is enforced one level deeper than the
  obvious: `counters_recorded` separates a missing row from an all-zero row, and
  `equity_observed` separates a measured drawdown from the `Decimal(0)` sentinel that means no
  equity snapshot was ever taken. An unobserved drawdown is not a drawdown of nothing.

### The browser client — no framework, no build step, no npm, no CDN
- Works offline; adds nothing to the dependency surface (R-15 does not grow). Served
  **same-origin** by FastAPI, which is what removes CORS from the picture entirely.
- Panels carry their own freshness and demote to STALE on age, and the supervision status bar
  now does the same. A cached `kill_switch_engaged: false` can no longer keep the badge hidden
  after a failed poll — absence is how this page says "clear", so unobserved must be loud.

### Adversarial review found three HIGH defects; all are closed and pinned
Every one was the same species — the terminal claiming a safer state than it could verify:
counters rendering sentinel zeros under the caption "what the supervisor observed"; safety
badges outliving a backend outage; and a hung `fetch` (no timeout) leaving the bar reading
"BACKEND OK" while the clock ticked. Reproduced before fixing and re-verified after against
running code, not against diffs.

The regression tests for two of them were then found to be **weaker than they looked** — they
hand-set the status and asserted what `drawStatus` drew, so a `pollSystem` that stopped
demoting on failure would have kept them green. `a_failing_poll_demotes_the_bar_without_being_told`
drives a real answer then a real rejection and names no status; breaking the demotion was
confirmed to fail it while both older tests still passed.

### Structural where a comment used to be
- The no-HTML-sinks property was only a comment, and no test read the client at all.
  `tests/safety/test_terminal_client_has_no_html_sinks.py` now scans the shipped file
  (comments stripped first, so its own honest description of what it avoids cannot satisfy it)
  and pins the no-external-origin property too.
- A **Content-Security-Policy** over `/terminal/*` makes the next `innerHTML` inert, so the
  guarantee no longer rests on ~1300 lines of hand-written DOM code never regressing.
- The autonomy contract isolation test gained a **narrower** tier rather than a wider
  exemption: modules that may name the mandate and nothing else, with the decision-type
  forbidden set derived from `chronos.autonomy.decision`'s own members so a type added later
  is covered without anyone remembering.

### Disclosed, not buried
`AUTONOMY_MANDATE_FILE`'s grant is now visible and revocable from a browser — a new
authorization surface (R-39), bounded by a required reason, writer gating, and a `mandate_id`
that binds the recorded act to the grant the owner was actually shown. The client **sends no
token**, so panels 401 until a browser-session decision is made (R-41): the terminal is not
yet usable end to end, and that choice does not belong in the commit that first exposes
revocation. No charts (no historical-bar route exists, so one would have nothing honest to
draw), no streaming (polling first — nobody has measured SSE contention against the tick).

Gates: ruff clean, mypy strict clean (214 files), **2375 passed**, 1 skipped. No test sends an order.

## [Unreleased] — M7.5 / ADR-0017: owner-directed maximal autonomy, wired (2026-07-25)

The owner directed Chronos to be as close to fully autonomous as possible, modeled on the
reference Quant-Guild bot, and answered the two scoping questions directly: **maximal**
autonomy (self-sizing, ceilings owner-optional) and a **persistent, auto-activating**
mandate. This entry is that override, recorded the way this project records overrides —
ADR-0017/D-17, dated, owner as authority, superseded text marked in place — not by quietly
deleting guarantees. "Maximal" was scoped as removing friction and owner-optional ceilings,
**not** execution-correctness mechanisms: the single transmit site, writer lease, kill
switch, reconciliation, floors/reserve, stale-data refusal, and the deterministic veto all
stand unweakened.

### The persistent mandate (`chronos.api.autonomy_wiring`)
- `AUTONOMY_MANDATE_FILE` names an owner-authored mandate JSON, validated and
  **auto-activated on every boot** — digest-stamped, so the audit trail records which text
  granted authority and an edited file writes a distinguishable activation. Supersedes
  ADR-0016 §4's "an env var alone may not activate live." A running backend plus the file
  is enough to trade; no per-boot ritual.
- What auto-activation does NOT override: **revocation survives restart** (re-granting is a
  new `mandate_version` — a fresh owner act); an invalid, unreadable, or wrong-account file
  boots **inert with a CRITICAL alert**; expiry still expires; **no file → no runtime** (a
  fresh checkout with no owner grant anywhere boots inert, kept on purpose).
- `MAX_LIVE_MANDATE_DURATION` 30d → **365d**; `restart_behavior` default →
  `RESUME_UNTIL_EXPIRY` (`REQUIRE_REACTIVATION` remains available; only the default moved).
- The wiring closes R-36's residual: `build_autonomy_runtime` assembles facts (broker
  account summary + probe quote per tick, per-decision instrument qualification), mandate,
  runtime, sinks, and the order-plane handoff; the backend lifespan drives the tick task.

### Model self-sizing (`CapitalLimits.model_discretion`)
- A new owner-written flag. When granted, unset capital **ceilings** stop meaning
  "authorizes nothing" — affordability (cash/buying power **net of the floors**) becomes
  the bound. Any ceiling the owner DID set still binds, and still refuses on absent
  evidence. The **floors are still required in every mode** — discretion over size is not
  discretion over the reserve. Defaults False; every existing mandate keeps ADR-0016
  semantics exactly.

### Protected market orders (`OrderForm.MARKET`)
- The enum grew — this is the "instrument-specific ADR, tests, and mandate permission"
  ADR-0016 §6 required. It must be granted in the mandate's `order_forms`, and it compiles
  to a **protected marketable limit** at quote±1% (`MARKET_PROTECTION_COLLAR`): market-order
  fill behavior in any sane book, a price ceiling on the broken print. Every compiled
  intent is still a positive-price limit; the reference project's unbounded `MKT` was
  deliberately NOT copied, and going literally unbounded is flagged in ADR-0017 as a
  separate, un-taken decision.
- `_select_order_form` now prefers the **most aggressive granted** form: listing an
  aggressive form in the mandate IS the explicit grant, and quietly preferring LIMIT anyway
  would second-guess a written authorization. Owners wanting passive fills grant only LIMIT.

### The handoff is the existing pipeline
- `order_plane_handoff` walks propose → risk → preview → confirm → submit — the same stack
  a human proposal walks, nothing skipped. The supervisor-consumer isolation test permits
  the app-plane wiring **by explicit module name** rather than by weakening the check.

### Tests
- `tests/safety/test_autonomy_wiring.py` (18): digest loading, invalid/missing/wrong-account
  files inert + alerting, idempotent auto-activation, **revocation survives restart** (unit
  and end-to-end), handoff order + risk-refusal short-circuit, fact-helper edge cases.
- Gateway discretion suite (5): unset ceiling → affordability bound; set ceiling still
  binds; floors never waived; discretionary submitting mandate validates without ceilings.
- Compiler: most-aggressive-form preference; MARKET compiles to a bounded, collared,
  tick-conformed limit. Contracts: the order-form vocabulary is pinned to exactly the three
  protected forms.



M3 made alerts durable. M5 disclosed the gap plainly: durable is not delivered, the channel
was **pull**, and an owner who was not looking was not told. R-32 was the last blocking
promotion criterion; this closes most of it and states exactly what remains.

### Alert delivery (`chronos.supervisor.delivery`)
- Unacknowledged alerts are pushed to sinks. **`delivered_at` records that the owner was
  *told*, which is a different fact from *acknowledged*** — and the difference matters most in
  the case where something went wrong and nobody noticed. Attempts are counted durably, so a
  persistently failing sink is *visible* rather than silently retried forever.
- An alert counts as delivered when **at least one** sink accepts. A stricter rule would let a
  single misconfigured file path suppress the log sink forever, which is the opposite of what
  an alerting system should do when partly broken. Every sink is attempted even after one
  succeeds, so delivery does not depend on registration order.
- A sink that raises is caught: an alerting system that crashes the process it is alerting
  about has made things worse.

### The decision: local sinks only
Not timidity — the actual threat model. A networked sender needs **credentials** beside a
process that moves money (the reference-project pattern the directive forbids), an **egress
path** a compromised component could ride in the other direction, and its failure mode is
**silence** — which converts "no alerts" from *unknown* into *all clear*.

Shipped: a log sink (always present, cannot fail environmentally) and an optional JSONL file
sink (0600, fsync'd, `O_NOFOLLOW` per R-21) that composes with whatever the operator already
runs — `tail -f`, an editor watch, a systemd path unit, a desktop notifier. **A structural test
fails if the module ever gains a network import**, so adding one is the deliberate, ADR-bearing
act it should be.

**Residual, stated rather than buried:** a local file does not follow you off the machine.
Unattended operation *away from the host* still needs a networked channel and its own ADR.

### The ingress transport (`POST /autonomy/proposals`)
M5 said authenticating *which* worker is calling belongs to the transport. This answers it by
**reusing what exists** rather than inventing a weaker scheme: loopback-only binding, the same
local API token every mutating endpoint requires, and the single-writer lease. Nothing here is
weaker than the surface it sits beside.

The route parses with the ingress (bypassing FastAPI's model binding on purpose — two parsers
would be two things to reason about) and returns 202, not 201: the proposal was *received and
judged*, and no resource the caller owns was created. A 201 would imply an order exists.

### Schema v6 (migration 0005)
Adds `delivered_at` and `delivery_attempts`. The column add is **idempotent**, and that is
worth explaining: revision 0004 builds its tables from *current* metadata rather than frozen
DDL, so a database upgraded today already has these columns while one upgraded earlier does
not. Adding blindly fails on the first path. This is the cost of the metadata-derived approach
showing up for the first time.

### Also
- R-32 → MITIGATED (local channels only). New: **R-36** — no scheduled runner drives the cycle.
  The proposal route validates and reports that no autonomy runtime is wired; the autonomy path
  is **reachable but inert**, which is the safe state.
- Fixed a false positive in M4's "no deterministic module reads untrusted text" guard: it
  matched `request.body()`, an HTTP body. A method *call* is not a field read, and
  `TextualEvidence.body` is a `str` that is never called — so excluding call targets separates
  the two without weakening the guard on what it actually protects.

### Gates
ruff clean, ruff format clean, mypy --strict clean (206 files), pytest 2175 passed / 1
credential-gated skip. No test sends an order.

## [Unreleased] — M5: the cycle runs, and the model worker is a separate process (2026-07-25)

Every milestone before this built a stage and disclosed that nothing called it. M5 is the
caller, and it closes the process-isolation gap by **inverting** the relationship.

### The autonomy cycle (`chronos.supervisor.loop`)
- `run_cycle` walks one proposal through ingress → stamp → admit → size → compile → hand off →
  record, stopping at the first refusal.
- **It does not submit.** It hands a compiled `WheelOrderIntent` to a callable; the existing
  `OrderManagementService` applies every gate it already applies to a human-proposed order and
  owns the single `transmit=True` site. `chronos.orders` stays the single canonical execution
  plane — "the autonomy loop got its own submission path" is exactly how that guarantee would
  have died. Autonomy **adds** a gate stack and removes none.
- **Non-live by default, structurally.** The handoff is optional; omitting it runs the full
  walk and places no order. A caller who has not thought about the last step gets SHADOW.
- **Every cycle is journalled, especially the refusals.** "Why did it not trade" is asked far
  more often than its opposite, and a system that logged only its actions could never answer it.
- **M3's session counters are finally fed.** Counting happens at *handoff*, not at fill,
  because an activity limit bounds what the system **attempts** — an order that was sent and
  then rejected still consumed one, and counting at fill would let a system being rejected by
  the venue retry without limit.

### The proposal ingress (`chronos.supervisor.ingress`) — R-35 closed
**Chronos does not call a model. A model worker calls in.** That inversion is the fix:
- No provider SDK, no API key, and no egress path in the broker-holding process. A worker that
  dies, hangs, or is never started produces no decisions, which is the correct failure mode.
- The worker holds no Chronos capability — no broker handle, no session, no lease, no kill
  switch, no submission path — not by promise but because none was ever in its address space.
- **Every payload is treated as hostile**, because a separate process is exactly where an
  attacker who compromised the worker would be standing: bounded size *before* parsing, strict
  single-object JSON, NaN/Infinity refused (`NaN > limit` is False, so a naive ceiling check
  would pass one), bounded nesting, full contract validation, and writer-owned fields
  (`provenance`, `decision_id`) refused **loudly rather than stripped** — a sender who tried is
  a sender worth knowing about.
- Refusals never echo payload content, so a hostile worker cannot write chosen text into an
  operator's terminal or logs.

### Session boundaries (R-34)
- `session_key` accepts an explicit `market_timezone`, so counters roll where the market's day
  does rather than at UTC midnight — 22:00 in New York is already the next UTC day, so a single
  trading afternoon straddled two counters. Optional and explicit rather than defaulting to a
  guess, and an unknown zone **raises rather than falling back to UTC**, because a silent
  fallback is wrong in exactly the way nobody notices.

### Also
- R-34 and R-35 → MITIGATED, both with residuals stated. Remaining blockers for unattended
  `LIVE_AUTONOMOUS`: **R-32** (no out-of-band alert delivery).
- Disclosed: the ingress does not authenticate *which* worker is calling (transport's job — a
  Unix socket's permissions or loopback plus the API token; a second, weaker scheme here would
  give false assurance), Chronos ships no process supervisor that starts the worker, and
  nothing calls `run_cycle` on a timer.

### Gates
ruff clean, ruff format clean, mypy --strict clean (203 files), pytest 2159 passed / 1
credential-gated skip. No test sends an order.

## [Unreleased] — M4: the gate finally has something routed through it (2026-07-25)

Two milestones' worth of disclosed bounds close here: the gateway stops being "a gate with
nothing routed through it", and the version-pin check stops being *agreement* and becomes
*authorship*.

### Deterministic compilation (`chronos.supervisor.compiler`)
- An admitted, sized decision becomes a `WheelOrderIntent`. The **capability matrix is a
  whitelist** over `(asset class, kind, strategy)`, and a test enumerates every combination —
  which is how you prove a whitelist is one rather than trusting that it is. Adding an enum
  member cannot silently become a tradable capability. Naked shorts, futures, futures options
  and multi-leg spreads map to nothing, and each absence is a safety property: constructing a
  spread one leg at a time is exactly the temporary naked exposure ADR-0016 §6 forbids.
- **`scope.exchanges` and `scope.contract_families` bind.** M2 disclosed them as unenforceable
  because they need a *qualified* contract; one now exists. The contract is an input rather
  than a lookup — resolution needs a broker, and a module that reached a broker would be one
  the model could reach through — and it is re-checked rather than trusted.
- **The limit price is entirely deterministic.** I corrected my own first draft here: it read
  `PriceTrigger.value` as a limit price and clamped it, which was a semantic stretch dressed
  up as a safety feature. A `PriceTrigger` is a *condition*. Reading the contract correctly
  gives the stronger guarantee — there is no model input to clamp. The trigger can only
  PREVENT compilation, and one the supervisor cannot evaluate refuses rather than being
  ignored, because silently dropping it would let a model attach conditions that look
  protective and do nothing.
- The passive order form is preferred when both are permitted (paying the spread should be an
  explicit grant); tick conformance rounds *away* from aggression; a crossed book refuses.

### Provenance is authenticated (`chronos.supervisor.queue`)
- The decision contract is split, and **the split is the control**. A model may author only a
  `ProposedDecision`, which has no provenance field and no id — there is nothing to forge.
  The writer, outside the model process, stamps both from harness-held configuration.
- Re-stamping an already-attributed decision is refused, or the boundary itself would become
  the forgery tool.
- **The id is a UUIDv5 over economic content**, which closes R-31's dedup residual. A
  model-chosen id would have escaped the re-submission bound by being fresh each time.
  Narrative is excluded, so rewording a thesis does not hand back the retry budget.

### The model's world (`chronos.autonomy.evidence`, `chronos.autonomy.tools`)
- **EvidenceBundles** are immutable, versioned, digest-pinned, and redacted *by shape*: no
  field exists for an account number or credential, plus a tripwire at issue time.
- **The tool surface has no write kind.** `ToolKind` is `{READ, DECISION}`, so the reference
  project's defect — one registry mixing read tools with direct broker write functions — has
  no vocabulary here. Handlers receive a bundle and nothing else (the signature is the
  sandbox), the registry freezes at startup, and unknown names refuse rather than near-matching.
- **R-30 (prompt injection) is bounded and tested, not solved.** Chronos claims only that an
  injection cannot become a trade the mandate would not otherwise have permitted. Injected
  narrative is proven to change no compiled order parameter byte-for-byte, not to change the
  decision id, and an injected size is still clamped. External text is carried verbatim and
  marked untrusted rather than sanitized — stripping it would destroy the record of what the
  model was actually shown.

### Also
- R-31 **closed**. R-30 → MITIGATED (bounded). New: **R-35** — model isolation is a code
  boundary, not a process boundary, which blocks unattended `LIVE_AUTONOMOUS` alongside R-32.
- Disclosed plainly: **no live provider harness exists**, so nothing in Chronos calls a model,
  and no runtime loop yet wires admission → sizing → compilation → `OrderManagementService`.

### Gates
ruff clean, ruff format clean, mypy --strict clean (201 files), pytest 2122 passed / 1
credential-gated skip. No test sends an order.

## [Unreleased] — M3: the supervisor gets a memory (2026-07-25)

M2 shipped a gateway with no state, which left three guarantees unenforceable. All three
are closed, and two persistence prerequisites landed first because durable state is only
as trustworthy as the store beneath it.

### Persistence prerequisites
- **The main `chronos.db` now uses WAL, `synchronous=FULL`, and a `busy_timeout`** — and
  each is *verified* on every connection, not merely issued. It ran on SQLite's defaults,
  so a committed transaction could be lost on power loss. A risk counter that silently
  rolls back is worse than one that does not exist, because the system would trust it.
  (The order ledger's own store has had WAL + `synchronous=FULL` since Phase 9; the main
  database had neither.)
- **Fixed a latent ordering bug in the R-21 symlink guard** that enabling WAL exposed. The
  guard ran only after the first connection, and switching journal modes makes SQLite
  unlink a stale sidecar — so the symlink it was meant to reject had already been removed,
  and the check passed on the real file SQLite had just created. Nothing was ever written
  through the link, but a guard that silently stops firing is a guard that is no longer
  there. It now runs before any connection is opened.
- **Append-only tables are hash-chained** (`chronos.persistence.hash_chain`). They were
  append-only *by convention* — a statement about the code, not about the data. Per-stream
  chaining detects edits, deletions and reordering. Honest bound stated in the module and
  as R-33: tamper-evident, not tamper-proof.
- **Schema v5** (migration 0004). Creates tables and deliberately never backfills: an
  absent counter must read as "no authority established", never as "no losses yet".

### The supervisor's memory (`chronos.supervisor.durable`)
- **`LossLimits` and `ActivityLimits` are enforced**, not contract-only. Every field of
  both is backed by durable per-session counters. A breach becomes a `DegradedReason` with
  `blocks_risk_reduction=False`, so it stops new exposure while leaving the position
  closable — being at a loss limit is exactly when closing must remain possible. Routing
  through the existing degraded lever rather than new refusal codes keeps that rule with
  one implementation instead of two chances to get it wrong.
- **Mandate activation and revocation are durable owner events**, so
  `RestartBehavior.REQUIRE_REACTIVATION` finally means something — an in-memory activation
  vanished on restart and could not tell "reactivated" from "never activated". Revocation
  is marked in place, never deleted: an audit trail that forgets a revocation cannot answer
  the first question an incident review asks.
- **R-31's re-submission counters are durable.** They lived in memory, so a model that
  wanted to route around a refusal only had to wait for a restart. Patience no longer
  defeats the bound.
- **Counters may only increase.** A caller that could decrement one could restore headroom
  under a ceiling, making the ceiling advisory.

### Owner alerting (`chronos.supervisor.alerts`)
ADR-0016 §8's fourth clause — *alert the owner* — was the one M2 left unbuilt.
- Alerts are durable, hash-chained, and acknowledged rather than deleted: an alert that can
  vanish cannot prove the owner was told, which is the only thing an alert is for.
- Recurrences fold into one row. A degraded loop would otherwise bury every other alert
  under identical entries, and an alert list nobody can read is an alert nobody receives.
  The occurrence count is itself information: one is an event, four thousand is a stuck
  system.
- Severity escalates but never downgrades while unacknowledged.
- Alerting is deliberately **narrow** — degraded state, exhausted re-submission, revoked or
  unactivated authority. Not ordinary scope/promotion/version-pin refusals: a gateway that
  alerts on every disagreement trains the owner to ignore alerts, and then the one that
  matters is ignored too.
- **Disclosed gap (R-32): there is no out-of-band delivery.** No email, SMS, push, or
  webhook. The channel is pull, so an owner who is not looking is not told. That is
  deliberate (no outbound network, no credential store beside a trading system, and a
  silently-broken push channel would turn "no alerts" from *unknown* into *all clear*) and
  it **blocks unattended `LIVE_AUTONOMOUS` promotion**. A structural test fails if an egress
  dependency is added quietly.

### Also
- The M2 enforcement-classification pin now scans `durable.py`. It passed without the
  change — while certifying `LossLimits` as inert — which is exactly the stale-claim failure
  that pin exists to catch.
- New risks: R-32 (no alert delivery), R-33 (tamper-evident bound), R-34 (session
  boundaries are calendar dates, not market sessions).

### Gates
ruff clean, ruff format clean, mypy --strict clean (197 files), pytest 2045 passed / 1
credential-gated skip. No test sends an order.

## [Unreleased] — M2 review remediation: complete (2026-07-25)

Remediation of the M2 five-lens adversarial review, now finished across all five lenses.
The verification pass confirmed 43 findings and refuted 10. Every confirmed finding is
either fixed below or, where it names work that belongs to a later milestone, disclosed
in `docs/limitations.md` and this file rather than left implied.

### Fixed (CRITICAL) — sizing ignored four of the seven capital ceilings
`size_order` published "never larger than any mandate ceiling" while
`max_position_notional_usd`, `max_net_exposure_usd`, `max_leverage` and
`max_margin_utilization_pct` were read by no code, so a mandate that set them tightly
sized as though they were unlimited. The same deny-by-default inversion the zero-ceiling
fix corrected, reached by a different route.

- All seven ceilings bind. A ceiling whose evidence the supervisor did not gather is a
  **refusal**, not an ignored limit, so the published claim is true rather than
  aspirational.
- `AccountEvidence` gained net/gross/symbol/position exposure, maintenance margin,
  deployed capital and position quantity. Optional fields are `None` when unknown rather
  than `0`, because zero is the *most permissive* value for a headroom calculation.
- `allocated_capital_usd` caps the mandate, not one order: deployed capital is subtracted.
  Without that, a $50k allocation authorized a $50k order on every pass while each
  individual order looked compliant.
- `size_order` never raises; adversarial `Decimal` input yields a refusal, because a
  raising sizer inside the supervisor loop would strand a decision.

### Fixed (HIGH) — the degraded-state rule had only one of its two halves
ADR-0016 §8 says *create no new exposure, permit deterministic risk-reducing behavior*.
Any degraded reason refused every decision kind, so a degraded system could not be unwound
through the gateway at all — a stale quote feed trapped the position at exactly the moment
the owner most wanted out.

- `DegradedReason` is typed and declares whether risk reduction may proceed, defaulting to
  **blocking** so an unconsidered reason gets the strictest behavior. One blocking reason
  overrides every permissive one: the question is "do we know what we hold", not "how many
  subsystems are up".
- `size_order` takes the `DecisionKind`. Exposure-headroom ceilings bind only on decisions
  that create exposure — applied to a CLOSE they give negative headroom, so an account
  over its ceiling was refused the one order that brings it back under. Risk-reducing
  orders are bounded by the position actually held, and refused when it is unknown.
- CANCEL stays refused while degraded: it is `CONTEXT_DEPENDENT`, because cancelling a
  *closing* order raises net risk and admission cannot see which kind of order is targeted.

### Fixed (HIGH) — the writer-lease heartbeat could lock the operator out of the kill switch
Before the M2 heartbeat, read-only was a *startup* condition, so the operator of a
lease-holding process could always halt trading. A running backend can now demote itself
mid-session, and uniform `require_writer` would have refused the emergency stop at the
worst possible moment.

- Engaging the kill switch and disarming are no longer writer-gated. Both only ever remove
  authority and both write lock-protected state, so serving them from a non-lease-holder
  is fail-safe: the worst case is that trading stops when it need not have. Arming and
  kill-switch disengagement stay writer-gated.
- The pre-transmit lease check — the only database call inside the CAS-to-transmit window
  — is wrapped so any failure fails closed instead of stranding the intent in
  `SUBMISSION_UNKNOWN` with nothing sent and no refusal recorded.
- The bound verifier checks `read_only` as well as the database lease, so a submission
  already in flight cannot transmit after self-demotion.
- `_RENEWALS_PER_TTL` is documented as an interval divisor, not the retry budget its
  comment implied: one failed renewal demotes immediately.

### Fixed (HIGH) — safety tests that did not test what their names claimed
- The file named *broker mutation inventory* pinned transmit **flags** and no broker
  mutation. The complete `placeOrder`/`cancelOrder` inventory is pinned now, plus an
  assertion that no `exerciseOptions`/`reqGlobalCancel` capability exists.
- The transmit inventory matched only a literal `True` and scanned only `src/chronos`,
  while claiming completeness "whichever syntax it uses". It now scans `scripts/` too and
  treats anything that is not a literal `False` as a site. That surfaced five propagation
  sites, which are declared and held separate from the two that *originate* transmit
  authority.
- R-24's pre-transmit gate had only structural guards, which catch deletion but survive an
  inverted condition that would transmit *only* when the lease was lost. Lost, unverifiable
  and held leases are now each exercised through a real submission.
- Every mandate limit is classified ENFORCED or INERT against the models themselves, so a
  new field cannot arrive undisclosed and an INERT field the kernel starts reading fails.
- `decision.py` promised a data-flow test asserting no deterministic module parses model
  narrative into an order parameter, and cited an M1 guard that M2 had retired. The test
  exists now; the stale citation is gone.
- `VersionPins.policy_version` had no `DecisionProvenance` counterpart, so it was compared
  against nothing — a control that could not fail. It has one, it is compared, and a test
  fails if any future pin lacks a counterpart.

### Fixed — overstated claims
- `chronos/supervisor/__init__.py` described the handoff to `OrderManagementService` in the
  present tense. Nothing converts an admitted, sized decision into an order intent;
  compilation is M4, and until it lands the gateway is a gate nothing flows through.
- ADR-0016 labelled R-24 "CLOSED in M2" while RISK_REGISTER recorded MITIGATED with a live
  residual. A risk with a live residual is not closed; the ADR now carries the weaker claim.
- R-31 still read "enforcement lands with the gateway in M2" after the gateway shipped.
- The quarantined paper adapter still opened by calling itself "the ONLY code path in the
  platform that can hand an equity order to Interactive Brokers".
- README's degraded-state bullet was marked `[M2+] not built`; it is built and marked
  `[enforced]`, with a new `[M2+]` bullet for the compilation step that genuinely is not.
- `docs/limitations.md` still published R-24 as unfixed and the second submission path as
  un-quarantined, contradicting RISK_REGISTER and ADR-0016 at the same commit.
- A CHANGELOG line gave a specific gateway test count that was wrong when written.

---

## [Earlier] — M2 review remediation: admission hardening (2026-07-25)

The first batch, covering the admission lens.

### Fixed (HIGH)
- **The strategy allowlist applied only to OPEN.** A HEDGE, INCREASE, ROLL or REPLACE
  carrying no `requested_strategy` was admitted with the check recorded as *passed*. That
  defeated the mitigation ADR-0016 §6 publishes for shorting — "omit SHORT_EQUITY from
  scope" — because a SHORT-direction HEDGE never had its strategy compared against the
  scope at all. Every exposure-creating kind must now name a permitted strategy, and a
  SHORT direction additionally requires an explicitly short-capable strategy.
- **An unevaluated evidence-bundle check was recorded as PASSED**, contradicting the
  module's own "no default-allow branch" claim, and only the bundle *id* was compared.
  Now: an unknown bundle refuses (`EVIDENCE_BUNDLE_UNKNOWN`), the **digest** is compared
  too, and `AdmissionCheck` gained an `evaluated` flag so an unevaluated check can never
  read as satisfied.
- **Four mandate limit groups were read by no code** while the mandate docstring claimed
  the supervisor re-derived "every limit". The claim is corrected, the honest
  enforced-vs-inert list now lives in one place (`admission.py`), and a test pins it so a
  mandate field cannot be added without declaring which it is.

### Added (previously missing checks)
- **Mandate activation, revocation, and restart reactivation.** Authoring a mandate is not
  enabling it; admission now requires an authenticated owner activation event, refuses a
  revoked one, and enforces `RestartBehavior.REQUIRE_REACTIVATION` against a process
  generation. Previously `restart_behavior` was inert.
- **Market-data freshness, quality, and spread**, from supervisor-gathered evidence.
  Absent evidence refuses.
- **Bounded re-submission after refusal** (R-31): a refused decision may be retried at most
  `MAX_RESUBMISSIONS` times. Replay protection previously covered only *admitted* ids.

### Documentation honesty
- `decision.py` no longer claims the provenance-stamping gateway landed in M2; the pin
  check proves agreement, not authorship, until the decision-queue writer lands (M4).
- ADR-0016's citation-binding paragraph now states what M2 actually delivered (bundle
  id+digest binding) versus what it did not (resolving individual citations).
- `docs/limitations.md` gains a full M2 section: what the gateway enforces, and the open
  gaps — loss/activity limits, `scope.exchanges`/`contract_families`, sector/family/
  correlated concentration, leverage, margin, and **the absent compilation step**, which
  the directive listed under M2 and which did not ship.

### Gates
ruff clean, ruff format clean, mypy --strict clean (193 files), pytest 1957 passed / 1
credential-gated skip.

## [Unreleased] — M2 fix: a zero ceiling authorizes nothing (2026-07-25)

**Found by self-review of the merged M2 sizing code, before any autonomous path could
consult it.** `size_order` *skipped* a mandate limit that was zero instead of binding on
it, which inverted deny-by-default exactly as the M1 review found for the floors: a
mandate whose capital ceilings were all left at their zero defaults — one that authorizes
nothing — sized to whatever cash allowed. Reproduced at **590 shares** where the correct
answer is "refuse".

Fixed at both layers:

- **Sizing:** every ceiling now binds, and zero binds at zero — per-order notional,
  per-order unit ceiling (the one that governs the asset class), allocated capital,
  per-symbol concentration headroom, and gross-exposure headroom.
- **Contract:** a submitting mandate must now state `allocated_capital_usd`,
  `max_order_notional_usd`, `max_gross_exposure_usd`, `max_symbol_exposure_pct`, and the
  unit ceiling matching its asset classes — so the failure surfaces at authoring time with
  a clear message, rather than as a silent refusal at trade time.

Regression tests cover both: the contract refuses to construct such a mandate, and sizing
refuses even when one is forced past validation via `model_construct`.

Gates: ruff clean, ruff format clean, mypy --strict clean (193 files), pytest 1944 passed
/ 1 credential-gated skip.

## [Unreleased] — M2: deterministic gateway, lease fencing, transmit quarantine (2026-07-25)

### Added — `chronos.supervisor` (the ModelDecisionGateway)
The first code that can turn a model decision into a *proposal*. It sits between
the model plane and `chronos.orders`, adds a gate, and removes none: an admitted,
sized decision is handed to the existing `OrderManagementService`, which applies
every gate it already applied to a human-proposed order and keeps the single
`transmit=True` site. The supervisor itself never touches a broker (asserted).

- `admission.py` — deny-by-default validation of a decision against the mandate
  in force: mandate presence, degraded state (refused *first*, so an AI/broker/
  data/lease failure never becomes permission), effective window, account
  fingerprint, submitting mode, decision replay, model/prompt/tool/schema version
  pins, evidence-bundle identity, HOLD as explicitly non-executable, asset class,
  instrument allowlist, strategy, per-family promotion, and order-form
  availability. Every check is recorded pass or fail, so a refusal is explainable.
- `sizing.py` — where "the model's requested quantity is not executable" becomes
  true in code. The request is an upper bound only; the kernel independently
  derives size from per-order notional, per-order unit ceilings, allocated
  capital, cash and buying-power **floors** (subtracted, so a floor genuinely
  reserves), per-symbol concentration headroom, and gross-exposure headroom —
  then clamps down and refuses when nothing survives. Decimal throughout;
  missing or absurd contract facts refuse rather than guess.
- A dedicated `tests/safety/test_supervisor_gateway.py` suite, incl. one test
  proving the gateway re-checks promotion via `model_construct` rather than
  trusting a mandate that skipped validation. (An earlier revision of this line
  gave a specific test count that was wrong when written and went stale
  immediately; the file is the count.)

### Fixed — R-24: the writer lease was never renewed, and was not a fencing token
`WriterLease.renew()` had **no production caller**. The lease expired after its
30-second TTL while the backend went on believing it was the writer, so a second
backend could acquire it and both would consider themselves authoritative.

- A lifespan heartbeat renews at TTL/3 and, on any failure, demotes the process
  to read-only permanently (re-acquiring would be unsafe — another writer may
  already have acted).
- New `WriterLease.holds()` re-checks ownership in the database; the submission
  boundary calls it immediately before the transmit line, beside the kill-switch
  re-read. A refusal there is provably not-sent.
- Residual, disclosed: this narrows the window, it does not close it. IBKR
  accepts an order without knowing about our lease, so broker-side fencing is
  unavailable.

### Fixed — R-28: the second transmit site is now quarantined and inventoried
`execution/brokers/ibkr_paper.py` enables transmission with an *attribute*
assignment (`order.transmit = True`) outside `chronos.orders`, which the
keyword-scoped single-transmit-site test structurally could not see.

- New `tests/safety/test_broker_mutation_inventory.py`: a repository-wide
  inventory matching **both** spellings, pinned to an explicit expected set, so a
  new transmit site anywhere fails CI; plus an AST assertion that no production
  module constructs the adapter.
- The adapter refuses construction without `quarantine_ack=True`, which nothing
  in `src/` passes — an accidental wiring fails loudly instead of quietly
  acquiring a second, ungated broker path.

### Changed
- M1's milestone guard (nothing imports the contracts) is replaced by the
  permanent, narrower invariant: **only** `chronos.supervisor` may consume an
  `AITradeDecision`, and the supervisor may not import a broker adapter or the
  submission boundary.
- `CANCEL` is no longer classified as unconditionally risk-reducing — see the
  M2a entry below.

### Gates
ruff clean, ruff format clean, mypy --strict clean (193 files), pytest 1942
passed / 1 credential-gated skip.

## [Unreleased] — M2a: contract hardening from the M1 adversarial review (2026-07-25)

Remediation of the five-lens adversarial review of M1, done before any gateway work
because M2's gateway validates mandates and cannot be built on bypassable ones. Full
finding list in ADR-0016 §"Known limitations and residuals" item 0.

### Fixed (security-relevant)
- **Authority escalation via `model_copy(update=...)`.** Pydantic does not re-run
  validators on copy, so a one-day SHADOW mandate could be copied into a ten-year
  `LIVE_AUTONOMOUS` mandate with an empty scope and a SHADOW promotion rung — every
  mandate validator skipped. New `chronos.autonomy.base.AutonomyModel` re-validates on
  copy; all autonomy contracts inherit it.
- **Per-family promotion.** A single scalar `promotion_level` let one asset family's
  evidence license another's live trading, contradicting ADR-0016 §7. Replaced with
  `promotions: tuple[FamilyPromotion, ...]`, required for every permitted asset class.
- **Kind/payload coherence.** A CLOSE, REDUCE or CANCEL could carry a strategy, entry
  plan, risk budget, size and direction — an opening request wearing a risk-reducing
  label. Now refused per kind.
- **Floors are not deny-by-default.** `min_cash_floor_usd`, `min_buying_power_usd` and
  `max_quote_age_seconds` default to zero, which is the *most* permissive value, not the
  most restrictive. Submitting mandates must now set them explicitly; the docstrings that
  claimed otherwise are corrected.
- **Live data quality.** A live mandate could license FROZEN/DELAYED_FROZEN/DEMO data the
  deterministic live gate already refuses. Now restricted to LIVE/DELAYED.
- **AST import matchers were blind to `from chronos import <subpackage>`**, silently
  defeating the autonomy isolation test, the M1 milestone guard, and the ADR-0013 holdout
  bar. All three fixed, each with a guard-the-guard test.
- **Naked-short guarantee was a substring scan** that a member named `SHORT_CALL` would
  have passed. `StrategyForm` is now pinned to its exact member set.
- Bounded `target_client_reference` to the exact `CHR-<PREFIX>-<32 hex>` shape, bounded
  the evidence tuple and all monetary/trigger amounts, restricted symbol and futures-root
  alphabets, cross-validated scope strategies against permitted asset classes, and made
  the decision plane refuse `FUTURE_OPTION` explicitly.

### Documentation honesty
- README safety bullets now carry `[enforced]` / `[contract]` / `[M2+]` markers; several
  described machinery that does not exist yet as though it were live.
- Corrected stale claims the review surfaced outside the M1 diff: `DECISIONS.md` D-08 and
  D-15, `docs/ARCHITECTURE.md` item 1, `docs/safety.md`'s staleness scope,
  `docs/GO_LIVE_CHECKLIST.md`'s closing sentence, `docs/DEPLOYMENT.md`'s env-var rows,
  `docs/adr/ADR-0013`, and `src/chronos/__init__.py`'s description.
- `AITradeDecision` no longer claims a data-flow test that does not exist; the claim is
  scoped to the milestone guard that does, with the permanent test owed by M2.
- `DecisionProvenance` now documents that it is stamped by the deterministic queue writer,
  not self-reported by the model — otherwise the version-pin check is a self-attestation.

### Gates
ruff clean, ruff format clean, mypy --strict clean (190 files), pytest 1901 passed /
1 credential-gated skip. Still no broker behavior: nothing outside `chronos.autonomy`
imports the contracts.

## [Unreleased] — controlled autonomous model authority (M1, 2026-07-25)

### Governance
- **ADR-0016 — Controlled Autonomous Model Authority** added. Supersedes **ADR-0004 §5 only**
  (the generative-AI prohibition); ADR-0004 §§1-4 (D-04, structural separation of authority)
  are preserved and load-bearing.
- **DECISIONS.md D-11 marked superseded in place** (kept for history) and replaced by **D-16**:
  an approved generative model may originate runtime trading decisions only through a typed
  `AITradeDecision` and the single deterministic ModelDecisionGateway; it cannot access IBKR
  directly, change its authorization, weaken policy, or bypass any deterministic gate.
- D-15's prospective copilot bar retargeted to the real `chronos.autonomy` plane (unchanged,
  not relaxed).
- Migrated: README, `docs/ARCHITECTURE.md`, `docs/architecture.md`, `docs/safety.md`,
  `docs/limitations.md`, `docs/AI_QUANT_GAME_PLAN.md`, `docs/LIVE_WHEEL_GAME_PLAN.md`,
  `docs/GO_LIVE_CHECKLIST.md`, `docs/live_trading_runbook.md`, `docs/TEST_PLAN.md`,
  `ASSUMPTIONS.md`, `TASKS.md`, `RISK_REGISTER.md`.

### Added
- `chronos.autonomy` — **contracts only, wired into nothing**: `AITradeDecision` (typed,
  frozen, `extra="forbid"`, structurally unable to express a broker order) and
  `AutonomyMandate` (owner-authored, versioned, expiring, revocable, deny-by-default), plus
  the autonomy vocabulary (modes, promotion ladder, asset classes, strategy and order forms).
- `tests/safety/test_autonomy_contracts.py` — 24 structural tests enforcing D-16, including
  model-plane import isolation (AST + subprocess probe) and a milestone guard asserting M1
  added no broker behavior.

### Risk register
- R-01 restated (the blanket "no live-capable code path" claim retired as stale post-M7).
- R-24…R-27 opened for kernel defects the M0 audit found and autonomy makes more dangerous
  (unrenewed writer lease with no fencing token; inert `max_opening_orders_per_day`;
  permanently ambiguous `market_open`; demo-only option deliverable verification).
- R-28 (dormant second submission path), R-29 (autonomy risk expansion, accepted by owner
  directive), R-30 (prompt injection), R-31 (refusal re-submission loops) opened.

### Safety posture
- No broker behavior added. Nothing in `chronos.autonomy` is imported by any runtime path.
- Every deterministic guarantee in ADR-0016 §8 is unweakened: one transmit site, single-writer
  lease, idempotency, reconciliation to broker truth, contract qualification, stale-data
  rejection, durable kill switch and halt, drawdown breakers, capital/concentration/margin/
  leverage limits, restart recovery, immutable audit trails, DEMO defaults, and the
  prohibition on broker mutations from tests or CI.

## [Unreleased] — deterministic strategy platform

### Added
- Pine corpus ingestion: 42 scripts fetched byte-exact from the Notion
  "Pine Quant Library — Master Index" into `research/pine/`, SHA-256 pinned in
  `research/strategy_registry.yaml` (+ CSV/JSON catalogs) via
  `scripts/build_strategy_registry.py`.
- Platform packages under `src/chronos/`: `marketdata`, `indicators`, `specs`,
  `strategies`, `portfolio`, `risk`, `execution` (engine, state machine,
  ledgers, reconciliation, simulated broker, IBKR paper adapter), `control`
  (modes, halt, promotion), `auditlog`, `notifications`, `backtest`,
  `research`, `cli`.
- Derived strategy implementations with canonical YAML specs:
  `regime_trend_v1` (core of Pine 01 BULL+ v1.1), `mean_reversion_v1`
  (executable derivation of Pine 11 MR Extremes Study v1.1); baselines
  (buy-and-hold, SMA 50/200, deterministic random entries).
- Safety acceptance test suite (`tests/safety/`) covering mode locks, halt
  persistence, deny-by-default risk, execution gating, and strategy isolation.
- Deny-by-default risk policy schema + `config/risk.example.yaml`.
- Complete documentation set: `docs/ARCHITECTURE.md`, `docs/RISK_POLICY.md`,
  `docs/STRATEGY_CATALOG.md`, `docs/PINE_AUDIT.md`, `docs/PARITY_REPORT.md`,
  `docs/RESEARCH_REPORT.md`, `docs/STRATEGY_SELECTION.md`, `docs/TEST_PLAN.md`,
  `docs/TEST_RESULTS.md`, `docs/SECURITY.md`, `docs/DEPLOYMENT.md`,
  `docs/OPERATIONS.md`, `docs/BACKUP_AND_RECOVERY.md`,
  `docs/INCIDENT_RESPONSE.md`, `docs/IBKR_INTEGRATION.md`,
  `docs/IBKR_RUNBOOK.md`, `docs/GO_LIVE_CHECKLIST.md`, ADRs 0001–0008,
  `docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md`; plus
  `ASSUMPTIONS.md`, `DECISIONS.md`, `RISK_REGISTER.md`, `TASKS.md`, `HANDOFF.md`.
- Independent adversarial review across seven dimensions with all
  CRITICAL/HIGH findings remediated (see REMEDIATION_REPORT).
- Owner-only (0600) permissions on platform ledger/halt/audit files;
  halt-write fsync durability; collision-resistant order-intent ids;
  deny-by-default for unrecognized trading modes.
- Dependencies: `pyyaml` (+ `types-PyYAML` dev).

### Changed
- `.gitignore`: runtime state files under `data/` (json/jsonl/tmp) ignored.
- `pyproject.toml`: dependency additions only; existing wheel-dashboard code
  untouched.

### Safety posture (unchanged and extended)
- Wheel dashboard: live-money transmission remains hard-disabled; IBKR order
  methods still raise unconditionally.
- Platform: live-capable modes resolve to a hard-denied capability; paper
  submission requires six simultaneous independently-verified conditions; a
  new deployment starts halted until first operator rearm.
