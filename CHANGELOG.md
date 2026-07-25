# CHANGELOG

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
