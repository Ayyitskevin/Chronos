---
name: chronos-autonomy-and-mandates
description: >
  Load this skill BEFORE touching anything in src/chronos/autonomy,
  src/chronos/supervisor, or src/chronos/api/autonomy_wiring.py, and whenever a
  task mentions: mandate, AutonomyMandate, AITradeDecision, ProposedDecision,
  autonomy, autonomous trading, model_discretion, gateway, ModelDecisionGateway,
  supervisor, tick, proposal ingress, promotion ladder, "add a tool", "widen a
  limit/ceiling/scope", "can the model see/do/name X", "let the model size
  itself", OrderForm.MARKET, revoke, AUTONOMY_MANDATE_FILE, or any change to what
  an AI may decide or how much authority it holds. It explains the ADR-0016/0017
  authority architecture precisely enough to EXTEND it without accidentally
  reopening a closed authority boundary — the closed-boundary table in §8 is the
  heart. Also load it when a session is tempted to "fix" the arming-vs-mandate
  contradiction, the supervisor COMPLETE-on-refusal defect, or a dead decision
  field: those are owner-gated, not judgment calls.
---

# Chronos autonomy and mandates — the authority architecture

All paths relative to the repo root. All file:line references verified against
the working tree on 2026-08-02 (branch `claude/chronos-skills-library-bfbj29`).
Jargon: a **mandate** is the owner's bounded, expiring grant of trade-time
authority; the **gateway** is the deterministic pipeline that judges every model
decision; the **supervisor** is the tick-driven runtime that drives the gateway;
the **order plane** is `chronos.orders`, the only code that can transmit.

Use this skill to understand and extend the autonomy stack. Do NOT use it for
order-pipeline mechanics, wheel strategy logic, or research evidence gates — see
"When NOT to use this skill" at the end.

Two facts frame everything: **no model runs inside this repository** (§6), and
**no real IBKR gateway has ever been connected in this project's history** —
every autonomy control on the adapter path is fixture-verified only
(see `chronos-real-gateway-campaign`). MITIGATED ≠ CLOSED.

## 1. The authority story in one page (ADR-0016 + ADR-0017)

Both ADRs are owner directives dated **2026-07-25, the same day**. Read them
together, and never quote ADR-0016 §4 or §6 without the bracketed ADR-0017
supersession notes edited into them in place (ADR-0016:142-143, 149-154,
164-169, 266-271).

**ADR-0016 — Controlled Autonomous Model Authority**
(`docs/adr/ADR-0016-controlled-autonomous-model-authority.md`, D-16, supersedes
D-11/ADR-0004 §5 ONLY; ADR-0004 §§1-4 stay load-bearing, ADR-0016:6-8). Its own
words: "this is the single largest expansion of risk in the project's history"
(ADR-0016:38-42). It splits authority by TIME: **policy time is owner-only**
(objectives, model versions, instruments, capital, promotion,
activation/revocation, kill switch); **trade time is the model's**, inside an
explicitly activated mandate (§1, :47-59). One decision type
(`AITradeDecision`), one deterministic gateway, unconditional deterministic
veto, no second submission path (§2, :61-92). Model isolation outside the
broker-writing process (§3). "Manual trading mode retains per-order typed
confirmation. Autonomous modes replace *that gate only* with the mandate. No
other gate is replaced." (:58-59 — but see §9: the code disagrees about arming.)

**ADR-0017 — Owner-Directed Maximal Autonomy**
(`docs/adr/ADR-0017-owner-directed-maximal-autonomy.md`, D-17). The owner then
directed Chronos be "as close to fully autonomous as possible." ADR-0017
supersedes, in place and by scope (ADR-0017:6-14): the 30-day live ceiling and
per-boot activation (§1: persistent `AUTONOMY_MANDATE_FILE` auto-activates on
boot, ceiling now **365 days**, `restart_behavior` defaults
`RESUME_UNTIL_EXPIRY`); deny-by-default **as applied to capital ceilings only,
only under `model_discretion`** (§2); the no-MARKET rule (§3: `OrderForm.MARKET`
compiles to a **protected collared limit**, quote ±1%, never unbounded `MKT`);
and the prefer-least-aggressive compiler rule (now prefers the MOST aggressive
*granted* form).

**The line ADR-0017 draws** (:48-72): maximal autonomy = "removing friction and
owner-optional ceilings, not removing execution-correctness mechanisms."
**Widening ceilings ≠ widening mechanisms.** What ADR-0017 explicitly did NOT
supersede (§5, :190-215), quoted:

> "Everything in ADR-0016 §8 stands, unweakened: one canonical transmission
> boundary and its single-transmit-site AST test; the single-writer lease and
> fencing re-check adjacent to the wire; durable idempotency and replay
> protection; reconciliation to broker truth; account and contract
> qualification; stale-data refusal (a crossed or empty book still refuses
> compilation); the durable kill switch and halt; the session and
> rolling-drawdown breakers; the cash and buying-power **floors** and the
> reserve they protect; order and cancellation rate limits; restart recovery
> and orphan handling; immutable hash-chained audit; DEMO/non-live defaults for
> anything the owner did **not** grant discretion over; and the prohibition
> against broker mutations from tests or CI."

Plus: "The **degraded-state rule stands verbatim** … **An AI failure never
becomes permission to trade**, and neither does maximal autonomy." (:202-209)
and "Deny-by-default is **not** globally repealed… never for floors, scope
tuples, order forms, asset classes, strategies, or data-quality permissions."
(:211-215). ADR-0016 §1-3, §5, §7, §8 are "preserved and load-bearing"
(ADR-0017:16-17).

## 2. The two contracts (reference)

Full field-by-field tables (enforced / advisory / DEAD, with enforcement
points): `references/contract-tables.md`. The essentials:

| | `ProposedDecision` | `AITradeDecision` |
|---|---|---|
| Defined | decision.py:231-414 | decision.py:417-438 |
| Author | the model (the ONLY thing it may author) | the deterministic queue writer `queue.accept` (queue.py:182-242) |
| Adds | — | `decision_id` + `provenance` — neither field exists on the proposal type, so a model cannot self-attribute or pick its own id |
| `decision_id` | — | UUIDv5 over **economic content** (queue.py:120-156). Narrative (thesis, rationale, confidence) excluded on purpose: rewording an explanation does not mint a new retry budget; re-proposing the same trade IS a replay |
| Order-capable? | **Structurally no.** No account/broker/order-id/client-id/routing/exchange/transmit/order-type field anywhere in the tree, and no mandate-naming field — the supervisor binds authority (decision.py:9-26). Pinned by `tests/safety/test_autonomy_contracts.py::test_decision_has_no_order_capable_field_anywhere` (:453; forbidden-name set :87-110) | same (it inherits) |
| Frozen | `AutonomyModel` base: frozen, `extra="forbid"`, `model_copy(update=...)` re-validates (base.py:31-41) | same |

**Four DEAD economic fields** — `exit_plan`, `protective_order_required`,
`max_acceptable_loss_usd`, `requested_risk_budget_usd` — affect ONLY the dedup
fingerprint; no supervisor or orders module reads them (grep-verified
2026-08-02). AGENTS.md:29-30 and Phase-1 item 7
(docs/VISION_COMPLETION_PLAN.md:160-162) require every economic-looking field to
become enforced, explicitly advisory, or forbidden. **Status: OPEN.** Route any
resolution through `chronos-change-control`; do not quietly wire or delete them.

Contrast: the MANDATE's fields DO carry a pinned ENFORCED/INERT classification
(`tests/safety/test_supervisor_gateway.py:634-751`). No equivalent pin exists
yet for the decision contract's fields — that asymmetry IS Phase-1 item 7.

## 3. The judgment pipeline, in order

There is no class named `ModelDecisionGateway`; the gateway is this pipeline,
driven per proposal by `run_cycle` (loop.py:178-453). Every stage refuses
forward — a refusal is recorded data, never an exception to catch.

| # | Stage | Where | What it enforces |
|---|---|---|---|
| 1 | **Ingress** (hostile-payload parse) | `src/chronos/supervisor/ingress.py` | 256 KiB size bound BEFORE parsing (:65); strict single-object JSON; NaN/Infinity refused (:110-131, 205-208); ≤16 nesting levels (:67-69); full `ProposedDecision` validation; writer-owned fields `provenance`/`decision_id` refused loudly (:73, 179-185); refusals NEVER echo payload content (:76-83, 188-200) |
| 2 | **Queue / stamp** | `src/chronos/supervisor/queue.py` | id derived from economic content (:120-156); provenance stamped from `HarnessIdentity` (:84-117); re-stamping refused (:211-219). Transport: `POST /autonomy/proposals` (token + loopback + writer lease, routes/autonomy.py:52-59) enqueues durably; the tick judges on its own schedule; batch bounded to `proposals_per_tick` = 10 (runtime.py:109-111) |
| 3 | **Admission** — 15 ordered checks | `admit()`, admission.py:285-432 | (1) mandate present; (2) **degraded state**; (3) activation/revocation/restart; (4) effective window; (5) account fingerprint match; (6) mode may submit; (7) replay + `MAX_RESUBMISSIONS = 3` (:110) against **durable** attempt counters (durable.py:432-450); (8) all 7 version pins; (9) evidence bundle id AND digest, unknown bundle refused; (10) HOLD recorded, never executable; (11) instrument allowlist (:616-643); (12) strategy allowlist + short-direction coherence (:646-693); (13) per-family promotion ≥ mode minimum; (14) ≥1 permitted order form; (15) market-data freshness/quality/spread. "A check whose evidence is absent is a refusal, never a pass" (:11-14); unevaluated checks record `evaluated=False, passed=False` (:142-155) |
| 4 | **Sizing** | `src/chronos/supervisor/sizing.py` | model request is one candidate among ceilings; `min()` picks the binding one (:396); floors SUBTRACTED so they genuinely reserve (:298-305); risk-reducing orders bounded by the position actually held, unknown position refuses (:236-251); a set ceiling with absent evidence refuses (:354-360); never raises (:176-184) |
| 5 | **Compilation** | `src/chronos/supervisor/compiler.py` | `_CAPABILITY_MATRIX` whitelist over (class, kind, strategy) (:118-155) + closing matrix (:159-166); unmapped combination refuses. Contract re-checked, never trusted (:416-476). Limit price derived from the supervisor's quote ONLY — the model has no price field; a `PriceTrigger` can only PREVENT compilation (:568-613). Crossed/empty book refuses; tick conformance rounds AWAY from aggression; non-positive conformed price refuses as "a market order by another name" (:534-548) |
| 6 | **Handoff** | `order_plane_handoff`, autonomy_wiring.py:185-205 | walks the FULL human pipeline: `service.propose` → risk → `service.preview` → `service.confirm` → `service.submit`. Nothing skipped; "Autonomy added a gate stack and removed none" (ADR-0017:174-181). But see §5 and §9 for two verified defects at this seam |
| 7 | **Record** | `_record`, loop.py:463-541 | every outcome, including refusals, hash-chained with the verbatim narrative |

**Degraded-state rule, exactly** (`_check_degraded`, admission.py:435-485): any
degraded reason with `blocks_risk_reduction=True` (position truth unknown)
refuses EVERYTHING — "not even a close, which could open the opposite position."
Otherwise the cycle narrows to `RISK_REDUCING_DECISION_KINDS` = {HOLD, REDUCE,
CLOSE} (enums.py:231-233). **The exception's own exception:** CANCEL is
deliberately NOT risk-reducing by kind — cancelling a closing/protective order
increases net risk, so it is `CONTEXT_DEPENDENT` (enums.py:223-237) and excluded
from the degraded carve-out (admission.py:467-477). Loss/activity-limit breaches
arrive as non-blocking degraded reasons via durable counters, so a breach stops
new exposure while the position stays closable (admission.py:29-39).

**Sizing under `model_discretion` — the EXACT semantics** (verified
sizing.py:253-341; mandate.py:259-266, 475-481; ADR-0017 §2):

- Applies ONLY when the mandate states `capital.model_discretion: true`
  (default False). The flag is the owner writing the inversion down.
- Under the grant, an UNSET (zero) `max_*` capital/exposure ceiling neither
  binds nor demands evidence — the bound becomes **affordability**: cash and
  buying power **net of the floors** (sizing.py:298-305), which always applies.
- Every ceiling the owner DID set (positive) still binds exactly as before AND
  still refuses when its evidence is absent (sizing.py:337-344, 354-360).
- Floors are ALWAYS required, discretion included — the validator checks floors
  BEFORE the discretion waiver returns (mandate.py:465-481). "Discretion over
  size is not discretion over the reserve." (ADR-0017:139-140)
- Nothing else reads the flag: scope tuples, order forms, strategies, data
  qualities, loss/activity/concentration limits keep deny-by-default.

**`OrderForm.MARKET` — protected, never unbounded** (compiler.py:98-102,
498-550; enums.py:88-109): must be granted in `scope.order_forms`
(`_select_order_form`, :479-495, preferring MARKET > MARKETABLE_LIMIT > LIMIT);
compiles to buy at `ask × 1.01` / sell at `bid × 0.99`
(`MARKET_PROTECTION_COLLAR = 0.01`), tick-conformed. Every compiled intent is a
positive-price limit; a literally unbounded venue `MKT` is UNEXPRESSIBLE and
would need its own future ADR (ADR-0017:238-244). Pinned by
`tests/safety/test_supervisor_compiler.py::test_a_market_form_compiles_to_a_collared_limit`
(:480-505). The 1% collar is a disclosed judgment, not a derived number.

## 4. AutonomyMandate lifecycle

Schema essentials in `references/contract-tables.md` §4. Lifecycle:

1. **Authoring is owner-only.** The mandate is an owner-written JSON file.
   Nothing in the model plane can write one (no write tools exist, §6), and
   `durable.activate` is called only by the wiring/owner path
   (durable.py:205-247).

   **Read it back before trusting it:** `python -m chronos.cli mandate check
   --file <path>` (`cli/mandate_check.py`) validates the file exactly as
   `load_persistent_mandate` does and then reports what it *actually*
   authorizes — which limits are INERT, whether the account fingerprint matches
   this machine, whether the version pins agree with the ingress stamp, and
   whether `max_relative_spread` was left at its no-ceiling default. It exits 1
   on anything BLOCKING and `--strict` also fails on IMPORTANT. It writes
   nothing and grants nothing; `mandate template` prints a SHADOW skeleton to
   stdout and `mandate fingerprint` maps an account id to its pseudonym. Adding
   a write path to that module would breach
   `test_no_mandate_command_writes_anything` and the `_MANDATE_ONLY_MODULES`
   exemption in `test_autonomy_contracts.py`.
2. **Validation on every boot.** `AutonomyMandate.model_validate_json` runs the
   full validator stack (mandate.py:397-535): expiry after start; live window ≤
   `MAX_LIVE_MANDATE_DURATION` = **365 days** (:63-69, 402-407); explicit scope,
   per-family promotions, and positive floors for every submitting mode;
   live modes restricted to LIVE/DELAYED data.
3. **Auto-activation** (`build_autonomy_runtime`, autonomy_wiring.py:318-367):
   `settings.autonomy_mandate_file` (env `AUTONOMY_MANDATE_FILE`,
   settings.py:121; see `chronos-config-and-flags`) unset → return None — **no
   file, no runtime**, "the one remaining non-maximal default, kept on purpose"
   (ADR-0017:117-119). `load_persistent_mandate` (:105-123) validates raw bytes
   and takes their SHA-256; the activation row's owner-event id is
   `persistent-mandate:<digest[:16]>` (:163), so the audit trail records WHICH
   text granted authority and an edited file writes a distinguishable row.
4. **Account matching:** `loaded.mandate.account_fingerprint` must equal the
   fingerprint of the connected account; mismatch → log error + CRITICAL alert
   `autonomy.mandate_invalid` + no runtime (:339-342, 370-386). Invalid or
   unreadable file → same CRITICAL alert, backend boots, autonomy stays inert —
   a broken grant never takes down the process that can still close positions.
5. **Expiry:** admission re-checks the window per decision (admission.py:329-335);
   renewal is a fresh owner act.
6. **Revocation is durable and terminal per version.** `durable.revoke` marks
   the activation row in place + hash-chains `mandate_revoked`
   (durable.py:250-287); admission independently refuses revoked activations
   (admission.py:500-506); on the next boot `ensure_activation` REFUSES to
   re-arm a revoked mandate and raises a WARNING alert (autonomy_wiring.py:145-157).
   Re-granting requires a new `mandate_version` in the file — a fresh owner act.
   The operational route is `POST /terminal/mandate/revoke` (writer-gated,
   non-empty reason required, bound to the mandate in force) — procedures live
   in `chronos-run-and-operate`.
7. **"Trading off" requires a durable revoke or file removal — NOT a restart.**
   `restart_behavior` defaults `RESUME_UNTIL_EXPIRY` (mandate.py:374-378), so a
   restart RE-ARMS a valid file. This is RISK_REGISTER **R-38**'s disclosed
   residual and the deliberate inversion the owner chose (ADR-0017:231-234).
   Operators carrying ADR-0016-era intuition ("restart to disarm") are wrong by
   owner decision. `REQUIRE_REACTIVATION` remains available per mandate.

## 5. Supervisor runtime — and the verified open defect

`AutonomyRuntime` (runtime.py) is **tick-driven, single-threaded by design**
(two overlapping ticks would double-spend activity counters, runtime.py:147-155).
Events are HINTS: they coalesce to "wake early once", floored at
`minimum_interval_seconds` (default 5.0s; a zero floor is rejected as "the
unbounded shape this design rejects", runtime.py:104-127) — events never trigger
cycles directly. Facts are gathered per tick by `BackendGatherers`
(autonomy_wiring.py:208-293); anything ungatherable returns None and the runtime
refuses to run that tick with a WARNING alert (runtime.py:275-287) — facts are
never invented. Options refuse at the instrument seam (`instrument_facts`
returns None for anything but EQUITY/CRYPTO, :272-284). A failing tick alerts
and keeps cadence; `max_consecutive_failures` (default 5) stops the runtime with
a CRITICAL alert (runtime.py:114-115, 334-377); restart is an operator act
(:379-391).

**VERIFIED OPEN DEFECT — COMPLETE-on-refusal (Phase-1 item 5).** The handoff
result is inspected for exceptions only (loop.py:405-425):

```python
    try:
        result = submit(compilation.intent)
    except Exception as error:
        # The order plane refused or failed. ...
        return _record(... CycleOutcome(stage=CycleStage.HANDOFF,
                refusal="ORDER_PLANE_REFUSED", ...))
```

Any non-exception return falls through to `durable.record_activity(...,
orders_submitted=1, ...)` and `CycleOutcome(stage=CycleStage.COMPLETE, ...,
handoff=result)` (loop.py:427-453). But the handoff ends with
`return service.submit(...)`, and the boundary RETURNS refusals —
`SubmissionOutcome(submitted=False, refusal=...)` (submission.py:114-122) — for
READ_ONLY_LEASE, LIVE_GATE_BLOCKED (including "not armed"), MODE_FORBIDS,
RECONCILIATION_NOT_READY, BROKER_REFUSED_BEFORE_SEND, and the ambiguous
BROKER_SUBMIT_FAILED/SUBMISSION_UNKNOWN case (:766-772). So a refusal, an
ambiguous send, and a venue rejection ALL journal as `stage=COMPLETE` and
increment `orders_handed_off` (runtime.py:331-332). **Never** build monitoring,
promotion evidence, or reconciliation logic on COMPLETE without reading
`handoff.submitted`/`handoff.refusal` and the order plane's own lifecycle
records. Status: OPEN (VISION_COMPLETION_PLAN.md:154-156; the fix needs the
typed-outcome vocabulary of :174-175). Constraint on any fix:
`CycleOutcome.handoff` is DELIBERATELY untyped so the supervisor cannot import
order-plane result types (loop.py:160-163) — respect that isolation, don't
delete it. Route through `chronos-change-control`.

**Two more verified seams at the handoff** (autonomy_wiring.py:195-203):
the wiring auto-fabricates the "typed confirmation" (`service.confirm(...)` at
:202 — gate 8 becomes a hash-freshness gate, not proof a human typed anything),
and passes `writer_lease_held=True` as a **literal** (:203), bypassing the
top-of-submit flag check (submission.py:222-226). The real lease protection is
the boundary's late database re-check inside the CAS-to-transmit window
(submission.py:713-742), which production wiring binds via `bind_lease_verifier`
(:193-205). Both are OPEN parts of the Phase-1 item 4 authority-model decision
(§9) — a boundary constructed without a verifier leaves the autonomy path with
effectively no lease gate.

## 6. Isolation layers (each with its pinned test)

| Layer | Mechanism | Pinned by |
|---|---|---|
| Import isolation | `chronos.autonomy` may not import orders/broker/execution/risk/api/persistence/services/control, ib_async, ibapi, sqlalchemy — AST walk over every module + alias-aware matcher + subprocess `sys.modules` probe | tests/safety/test_autonomy_contracts.py:69-81, 295-318 |
| Single consumer | only `chronos/supervisor/` plus `api/autonomy_wiring.py` **by explicit module name** may import the contracts; terminal modules hold a narrower mandate-only exemption (display, revoke) | test_autonomy_contracts.py:321-398 (:340 names the wiring) |
| Registry / holdout bar | `autonomy` and `supervisor` are in the automated tree scanned for `chronos.registry` imports and `request_unlock`/`mediated_holdout_read` calls — D-15 "retargeted, not relaxed" | tests/safety/test_registry_no_automated_unlock.py:37-51 |
| Tool surface | `ToolKind = {READ, DECISION}` — "There is no write kind, by construction" (tools.py:63-73); handlers receive `(EvidenceBundle, args)` and nothing else (:76-78); registry frozen at startup; unknown names refuse, no fallback (:107-167); shipped surface = exactly four READ tools: get_quote, list_positions, get_account, list_texts (:200-240) | tests/safety/test_model_tool_surface.py (:143, :155, :212) |
| Transmit inventory | one `transmit=True` in `chronos.orders` (submission.py:744-745); repo-wide inventory catches both spellings incl. attribute assignment (guards the quarantined R-28 path) | tests/safety/test_single_transmit_site.py:32-43; tests/safety/test_broker_mutation_inventory.py |

Details of the transmit site, writer lease, and kill switch live in
`chronos-architecture-contract` — do not restate them here beyond the table.

**Provenance reality (Phase-1 item 6, OPEN).** No model runs in-repo: no LLM SDK
in `pyproject.toml`/`requirements.txt` (grep-verified), no prompt files, no
worker implementation; "Chronos ships no model, no provider SDK, and no API key
in the broker-holding process" (docs/safety.md) and "there is still no live
provider harness inside Chronos, and by design there never will be"
(docs/limitations.md:281-283). The worker calls IN over
`POST /autonomy/proposals`. Every arriving proposal is stamped with the static
`INGRESS_IDENTITY` constant (provider="external-worker", model_id="ingress",
autonomy_wiring.py:84-94) — so the version-pin check currently authenticates
"came through the ingress", NOT "produced by the pinned model". The credential
is the same local API token every mutating route accepts, not a proposal-only
one (routes/autonomy.py:52-59; limitations.md:340-341). The narrow
job/evidence/worker-identity protocol is specified at
VISION_COMPLETION_PLAN.md:157-159 and does not exist yet.

## 7. Promotion: the reality vs the ladder

**What exists (verified):** the vocabulary (`PromotionLevel`,
`PROMOTION_LADDER`, `MINIMUM_PROMOTION_FOR_MODE`, enums.py:36-48, 196-216), the
carrier (`FamilyPromotion(asset_class, level)` tuples inside the owner-authored
mandate file, mandate.py:175-185, 371), and two consistency checks (mandate
validator :440-456; admission check 13 :396-407) plus a terminal read-model.

**What does NOT exist (verified by exhaustive grep):** no promotion-evidence
store, no signed/expiring promotion artifact, no code that grants, records, or
demotes a rung. `FamilyPromotion` appears ONLY in its definition and the mandate
field. **Promotion is self-declared owner configuration; the checks verify
internal consistency of the declaration, not evidence.** Writing
`CAPPED_LIVE_AUTONOMOUS` into the JSON satisfies every check — deliberately an
owner act, but no future "promotion service" may read those fields as proof of
anything. This is Phase-1 item 8, **OPEN**
(VISION_COMPLETION_PLAN.md:163-164: "Replace self-declared family levels with
signed, expiring evidence artifacts"). The documented rung table (replay →
shadow → supervised paper → autonomous paper → live canary → capped live, with
minimum evidence per rung) lives ONLY in VISION_COMPLETION_PLAN.md §9
(:263-270). Evidence-gate discipline for earning a rung:
`chronos-research-methodology`. Do not confuse this ladder with
`src/chronos/control/promotion.py` — that is the deterministic platform's
separate record system, whose plane remains live-incapable (ADR-0007).

## 8. THE CLOSED-BOUNDARY TABLE

Each row is a decided, closed authority boundary. **Reopening ANY of them
requires a new ADR and an explicit owner decision, recorded the D-11→D-16→D-17
way (dated ADR, superseded text marked in place) — never a session judgment
call, never a code-only change.** "The owner liked another bot's design" is
license to widen owner-set LIMITS only, never mechanisms, and only through a new
ADR. If a task would touch a row, stop and route through
`chronos-change-control`.

| Closed boundary | Closed by | Enforced at |
|---|---|---|
| One transmit site; no second AI submission path | ADR-0016 §2/§8; ADR-0009 | submission.py:744-745; test_single_transmit_site.py; test_broker_mutation_inventory.py |
| Adding a tool kind beyond READ/DECISION (any write tool) | ADR-0016 §3 (tools.py:63-68: "a deliberate, reviewable act with an ADR attached") | test_model_tool_surface.py:143-176 |
| Model plane importing orders/broker/execution/risk/api/persistence | ADR-0016 §3 | test_autonomy_contracts.py:69-81, 295-318 |
| Model plane reaching registry/holdout unlock | ADR-0013 §7 / D-15, retargeted by D-16 | test_registry_no_automated_unlock.py:37-51 |
| A decision expressing an order (account/broker/routing/transmit fields) | ADR-0016 §2 | decision.py:9-26; test_autonomy_contracts.py:87-110, 453 |
| The model seeing, naming, or selecting its mandate; self-authored id/provenance | ADR-0016 §2; M4 split | decision.py:16-18, 231-258; ingress.py:179-185; queue.py:211-219 |
| Mandate self-authorization (model writing/arming a mandate) | ADR-0016 §4 ("It cannot arm itself or change its limits", :135-138) | no write tools (tools.py); isolation tests; durable.activate is owner-path only |
| Routing around a refusal (unbounded retries, fresh-id replays) | ADR-0016 §2 / R-31 | MAX_RESUBMISSIONS=3 admission.py:110, 525-545; economic-content ids queue.py:120-156; durable counters durable.py:432-450 |
| Deny-by-default for scope, order forms, strategies, data qualities — NOT inverted by model_discretion | ADR-0017 §5 | mandate.py:423-438; sizing reads the flag for capital ceilings only |
| Weakening floors (cash/BP reserve, quote-age) in any mode | ADR-0017 §2 ("not discretion over the reserve") | mandate.py:458-481; sizing.py:298-305 |
| model_discretion inverting anything beyond capital ceilings | ADR-0017 §2 | mandate.py:266, 475-481; sizing.py:259-341 |
| Unbounded venue MKT order | ADR-0017 §3 + residual 1 (:238-244 — its own future ADR) | compiler.py:498-548; test_supervisor_compiler.py:480-505 |
| Uncovered/naked short options — unexpressible | ADR-0016 §6 (UNCHANGED by ADR-0017) | enums.py:68-85 (no naked member, set pinned); compiler.py:110-117 |
| FUTURE_OPTION recognized-and-refused; futures untradable | ADR-0016 §6 | mandate.py:224-230; decision.py:379-384; compiler matrix |
| Kill-switch precedence absolute over any mandate | ADR-0016 §8 | docs/safety.md ("not superseded by any mandate"); submission.py:698-711 |
| Revocation survives restart; re-grant = new mandate_version | ADR-0017 §1 | autonomy_wiring.py:145-157; durable.py:250-287 |
| Invalid/wrong-account mandate file boots inert + CRITICAL alert | ADR-0017 §1 | autonomy_wiring.py:333-342, 370-386 |
| Expiry required; live ceiling 365d; renewal a fresh owner act | ADR-0017 §1 | mandate.py:69, 399-407 |
| Degraded state: no new exposure; risk-reduction only with position truth intact; AI failure ≠ permission | ADR-0016 §8; ADR-0017 §5 verbatim | admission.py:435-485 |
| Handoff walks the FULL order pipeline; adds gates, removes none | ADR-0017 §4 | autonomy_wiring.py:185-205 |
| Network alert channel (alerts leave the machine) | R-32 — local sinks only was a deliberate decision; "adding a networked channel needs an ADR" (the test's own message) | tests/safety/test_alert_delivery.py::test_the_delivery_module_has_no_network_capability (:82-116) |
| Registry access from ANY automated-tree module | ADR-0013 §7 | test_registry_no_automated_unlock.py (dirs list :37-47) |
| `chronos.control.modes` plane stays live-incapable and model-free | ADR-0007, untouched by 0016/0017 | enums.py:1-8; control/modes.py |
| Owner gates: capital, holdout unlock, mandates, canary, promotion, cap increases | AGENTS.md:33-34; VISION_COMPLETION_PLAN §11 | doc-level contract — see `chronos-change-control` |

## 9. Arming vs mandate — a LIVE contradiction, owner-gated

Three documents tell three different stories about what a mandate replaces:

- `docs/live_trading_runbook.md:21-24`: the mandate "replaces gates 7 (session
  arming) and 8 (per-order confirmation) — and only those two."
- `docs/AI_QUANT_GAME_PLAN.md:260-264`: the mandate "replaces per-order human
  confirmation and session arming inside its bounds — … That is the only gate
  autonomy replaces" (names two, says "only gate").
- ADR-0017:83-84: "A running backend plus a valid mandate file is now
  sufficient to trade; there is no per-boot ritual."

**The code says otherwise (verified):** `chronos.orders` has zero mandate
awareness. Every LIVE submit requires a current, unexpired in-memory arm —
`armed = self._live_arming.is_armed(now=fresh_now)` (submission.py:441) feeds
gate 7 `session_arming` unconditionally (live_gate.py:23-34); arming is
process-memory with a TTL, cleared by restart, set only by `POST /live/arm`.
The autonomy wiring never arms; it DOES auto-mint the gate-8 confirmation
(§5). Net effect: on a PAPER-environment backend the autonomy path can submit
end-to-end (the paper branch consults neither arming nor the kill switch,
submission.py:241-330); on a LIVE-environment backend it blocks at
`session_arming` unless a human armed within the TTL — and per §5 that refusal
currently journals as COMPLETE. **Resolving this is an OWNER authority-model
choice** (VISION_COMPLETION_PLAN.md:151-153, Phase-1 item 4: "Choose and
implement one reviewed authority model"; AGENTS.md:33-34), not a code cleanup.
Do not "fix" either side quietly, in either direction.

## 10. How to extend CORRECTLY (worked patterns)

Doctrine for all four: a control is real only when a test EXERCISES it
(fires it and observes the refusal), not when the code is merely present —
the exercised-not-present proof patterns live in `chronos-validation-and-qa`.

**A. Add a READ tool.** Allowed without an ADR (the surface stays READ-only);
a new tool KIND is a closed boundary (§8). Add a handler taking exactly
`(EvidenceBundle, args)`, register a `ToolSpec(kind=ToolKind.READ)` in
`default_registry()` (tools.py:200-240) before `freeze()`. Tests that must
exist/pass: `test_every_shipped_tool_is_read_only`,
`test_a_handler_receives_the_bundle_and_nothing_else`,
`test_the_tool_module_imports_nothing_that_can_act`
(test_model_tool_surface.py) and the autonomy import-isolation suite — the
handler may read the bundle and NOTHING else (no session, broker, settings,
filesystem, network).

**B. Add an instrument to a mandate.** This is an owner EDIT of the JSON file,
not code: add the symbol to `scope.symbols` (normalized/uppercased,
routing-syntax refused, mandate.py:199-202, and pinned by
`test_mandate_scope_rejects_routing_syntax_in_symbols`). The edit changes the
file digest, so the next boot writes a NEW digest-stamped activation row.
Validation happens on boot; admission check 11 then allowlists it per decision.
Remember the probe-quote trap: cycle-level market evidence quotes the FIRST
scoped symbol (autonomy_wiring.py:216-218), so symbol ordering matters
operationally. No test change needed; verify with the boot log line
`Persistent mandate ... auto-activated` and `GET /terminal/mandate`.

**C. Add a gateway check.** TIGHTENING (a new refusal reason) is allowed with
tests: add it as a pure, ordered check in `admit()` returning a new
`AdmissionRefusal` member; record `evaluated=False, passed=False` when its
evidence is absent; add exercised tests proving it FIRES (a decision that hits
it is refused) and that absent evidence refuses. If it reads a NEW mandate
field, you must classify the field in `_LIMIT_ENFORCEMENT`
(test_supervisor_gateway.py:647-700) — the suite fails until you do, and the
guard-the-guard test (:724-751) fails if the classification lies. LOOSENING or
removing any check is a §8 event: new ADR + owner decision.

**D. Add evidence to the fact gatherers.** Extend `CycleFacts`/`InstrumentFacts`
(loop.py) and `BackendGatherers` (autonomy_wiring.py:208-293), keeping both
rules: facts are caller-gathered, "Never model-supplied"; and anything
ungatherable returns None → the runtime refuses to run rather than inventing a
number (runtime.py:275-287). The wiring module is the ONE place allowed to hold
both supervisor and broker imports — do not gather facts anywhere else, or the
single-consumer test fails (correctly). Tests: extend
`tests/safety/test_autonomy_wiring.py` / `test_autonomy_cycle.py` with an
absent-evidence-refuses case for each new fact.

Never as part of any extension: connect to IBKR, place orders, or weaken a gate
"to verify". Verification is read-only (see below).

## When NOT to use this skill

- Order-pipeline mechanics (gates, CAS window, reconciliation, kill switch,
  lease): `chronos-architecture-contract` for invariants,
  `chronos-run-and-operate` for procedures (arm/revoke/kill/halt).
- Wheel-strategy logic, options gating, deliverable checks:
  `chronos-wheel-and-options`.
- Research evidence gates (walk-forward, DSR, holdouts) and what earns a
  promotion rung: `chronos-research-methodology`.
- Broker adapters and the ContractDetails bug class: `chronos-ibkr-boundary`.
- ADR mechanics, owner-gate process, document precedence:
  `chronos-change-control`. History of how these boundaries got here:
  `chronos-failure-archaeology`.

## Provenance and maintenance

Written 2026-08-02 against branch `claude/chronos-skills-library-bfbj29`.
Volatile facts and their read-only re-verification commands (run from the repo
root with the project venv per README Setup, `.venv/bin/python`):

| Volatile fact | Re-verify with |
|---|---|
| All pinned boundary tests still pass (179 tests as of 2026-08-02) | `.venv/bin/python -m pytest tests/safety/test_autonomy_contracts.py tests/safety/test_model_tool_surface.py tests/safety/test_registry_no_automated_unlock.py tests/safety/test_single_transmit_site.py tests/safety/test_broker_mutation_inventory.py tests/safety/test_supervisor_gateway.py tests/safety/test_supervisor_compiler.py -q` |
| Live mandate ceiling still 365d | `grep -n "MAX_LIVE_MANDATE_DURATION" src/chronos/autonomy/mandate.py` |
| MAX_RESUBMISSIONS still 3 | `grep -n "MAX_RESUBMISSIONS" src/chronos/supervisor/admission.py` |
| Collar still 1% | `grep -n "MARKET_PROTECTION_COLLAR" src/chronos/supervisor/compiler.py` |
| COMPLETE-on-refusal defect still open (item 5) | `sed -n '405,453p' src/chronos/supervisor/loop.py` — fixed only if a non-exception refused outcome no longer records COMPLETE |
| Arming still required on LIVE (item 4) | `grep -n "is_armed" src/chronos/orders/submission.py` and `grep -n "session_arming" src/chronos/orders/live_gate.py` |
| writer_lease_held literal + auto-confirm still present | `sed -n '195,205p' src/chronos/api/autonomy_wiring.py` |
| Four decision fields still dead (item 7) | `grep -rn "exit_plan\|protective_order_required\|max_acceptable_loss_usd\|requested_risk_budget_usd" src/chronos --include="*.py" \| grep -v "autonomy/decision.py" \| grep -v "supervisor/queue.py"` — empty output = still dead |
| Promotion still self-declared (item 8) | `grep -rn "FamilyPromotion" src/chronos --include="*.py"` — only the definition + mandate field = still no grant/demote code |
| Provenance still static (item 6) | `sed -n '84,94p' src/chronos/api/autonomy_wiring.py` |
| No LLM SDK in-repo | `grep -n "anthropic\|openai\|litellm\|langchain" pyproject.toml requirements.txt` — no hits |
| Phase-1 findings list unchanged | `sed -n '140,164p' docs/VISION_COMPLETION_PLAN.md` |

If any re-verification diverges, the CODE is the truth (AGENTS.md precedence:
current executable facts outrank every document, this skill included) — update
this skill, and treat a silently-moved boundary as an incident to surface, not
absorb.
