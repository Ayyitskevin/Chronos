# ADR-0028 — The per-job evidence protocol

Status: **proposed — owner decision required.** No `DECISIONS.md` row until accepted.

Date: 2026-08-13

Closes, if accepted: the evidence half of `docs/VISION_COMPLETION_PLAN.md` §6 finding 6
(the identity half closed 2026-08-12 as D-24/ADR-0023). Named in
`docs/CONTINUATION_PLAN_2026-08-12.md` §3 as item A2.

## Context

ADR-0023 made authorship real. A registered proposer presents a proposal-only
credential, the route records which registration it verified, and the drain re-resolves
that registration into the identity the queue writer stamps. Provenance now names an
author.

It also said, in its own acceptance note, exactly what it did not fix:

> **Evidence binding is still uniform.** `ProposerRegistration` carries no
> evidence-bundle fields; every registered identity stamps the placeholder bundle
> id and an honestly-absent digest.

That is still true at `a74cd09`. Every proposal from every proposer — the model worker,
the TradingView bridge, any future one — is stamped `evidence_bundle_id="owner-workspace"`,
`evidence_bundle_digest=None` (`src/chronos/api/autonomy_wiring.py:88-100`, copied into
the registry-derived identity at `:369-370`). Evidence is the last constant in provenance.

### 1. Admission check 9 currently compares a constant against itself

This is worse than "uniform", and it is the fact that should decide how much this ADR is
worth building.

`_check_evidence_bundle` (`src/chronos/supervisor/admission.py:587-626`) compares the
decision's `provenance.evidence_bundle_id`/`_digest` against
`SupervisorState.expected_evidence_bundle_id`/`_digest`. Both sides originate in the same
place:

- the **decision** side is stamped by `queue.accept` from the `HarnessIdentity` the
  wiring supplies, whose evidence fields are `INGRESS_IDENTITY`'s
  (`autonomy_wiring.py:369-370`);
- the **expectation** side is `CycleFacts.evidence_bundle_id`/`_digest`, which the
  backend gatherer fills from `INGRESS_IDENTITY` too (`autonomy_wiring.py:490-491`),
  forwarded into `durable.build_state` at `loop.py:323-324`.

So check 9 is a tautology. It cannot refuse, in any posture, for any proposer, because
the two values it compares are two reads of one constant. The check is written correctly
— exact match, `None` included, deny-by-default when the expectation is absent — and it
is wired to a comparison that has never had two independent sides. This is the R-24..R-27
shape one level up: not a control that failed, a control whose evidence was never
gathered.

### 2. A real digest already exists, and nothing looks at it

The worker computes exactly the digest this protocol wants. `worker/evidence.py:104-112`
canonicalizes the account summary, positions, open orders and daily bars into one JSON
string, takes SHA-256 over its UTF-8 bytes, and renders **that exact string** into the
prompt; `EvidenceSnapshot.citation()` (`:65-74`) attaches it to every proposal as an
`EvidenceCitation` with `kind="worker_evidence_snapshot"`. The module's own docstring
states the property that matters: "The digest binds what the model actually saw."

That citation travels in `ProposedDecision.evidence` (`decision.py:294`), is validated as
hex, is journaled — and is read by nothing. `grep` for `.evidence` across
`src/chronos/supervisor/` returns no reader; admission looks only at `provenance`. The
bridge is in the same position: `_citation` (`bridge/translate.py:167-185`) emits
`kind="tradingview_alert"` with a digest over the exact alert text minus the secret, and
nothing downstream compares it to anything.

The repository therefore already contains, on both proposal paths, a computed digest of
the evidence and a comparable field in provenance — with no connection between them. The
work this ADR proposes is mostly connecting two things that exist.

### 3. Why the payload cannot simply carry the answer

The obvious shortcut — let the proposal state its own `evidence_bundle_id` and digest in
provenance — is already refused, correctly. `ingress.parse_proposal` rejects payloads
carrying `provenance` or `decision_id` loudly rather than stripping them
(`ingress.py:179-185`), because a hostile proposer must not write its own authorship into
a hash-chained journal. ADR-0023 solved the same problem for identity by deriving it from
*which credential authenticated*. Evidence needs the same move: the backend must hold a
record it can resolve, not a claim it must believe.

There is a trap in that move which the design below is shaped around. If the stamper
stamps provenance **from the backend's record**, and the supervisor's expectation also
comes **from the same record**, check 9 stays a tautology — a per-job one instead of a
global one. A meaningful check needs two independent origins: the **backend's record** on
one side, and the **payload's own claim** on the other.

### 4. The two proposal sources are not symmetric

- **The model worker** reads evidence from Chronos over authenticated routes
  (`/account/summary`, `/account/positions`, `/orders`, `/terminal/bars`,
  `worker/evidence.py:87-99`). The backend can be a witness here: it served the bytes.
- **The TradingView bridge's evidence is the alert itself** — authored outside Chronos,
  delivered to a process that imports nothing from `chronos`, and never seen by the
  backend at all. No protocol can make the backend a witness to it. The most any protocol
  can do for the bridge is bind the proposal to a specific, non-replayable attestation
  from a specific credential.

An honest protocol must say which of those two it is providing, per source, in the record
itself.

## The decision

Every option below assumes the ADR-0023 posture rules: unset means today's behavior
verbatim, every failure refuses rather than falls back, and nothing weakens.

### Option A — Backend-issued bundles: the backend digests the bytes it served

A new route issues an evidence bundle: it composes the same facts the worker reads today
into one canonical document, takes SHA-256 over the exact bytes it serves, writes a
durable `issued` record (bundle id, proposer id, account fingerprint, digest, issued_at,
expires_at) hash-chained like every other supervisor row, and returns the document with
its bundle id and digest. The proposer cites the bundle id in an `EvidenceCitation`; the
drain resolves it against the record; provenance is stamped from the record.

- **For:** the digest is computed by the trusted process over bytes it holds, so
  "unissued", "issued to another proposer" and "expired" become facts the backend can
  *check* rather than claims it must accept. It makes `EvidenceBundle`
  (`chronos/autonomy/evidence.py:130`) the object its docstring already describes — issued,
  immutable, digest-pinned, with the supervisor comparing against "the bundle it issued".
  It also removes the worker's need to hold the backend's general API token for evidence
  (R-47(d), R-48(d)): one proposer-credentialled call replaces four token-authenticated
  reads.
- **Against:** it binds *what was served*, not *what was rendered into the prompt*. A
  worker can fetch a bundle, prompt on something else entirely, and cite the issued digest;
  nothing detects that. It is also the largest build: a composing handler, a table, a
  migration, a retention bound, and a second route the proposal-only credential opens.
- **Requires (exercised, in the house pattern — each conjunct reverted alone and watched
  fail):** a proposal citing an unissued bundle id refuses; a proposal citing a bundle
  issued to a *different* registered proposer refuses; an expired bundle refuses at the
  drain's clock, including one that expired between enqueue and drain; the digest of the
  bytes actually served verifies end-to-end through the real drain, the real hash chain
  and the real durable state; the evidence-unset posture produces byte-identical behavior
  to today; and a configured-but-broken evidence posture refuses every proposal rather
  than falling back to the placeholder.

### Option B — Accept the proposer's attested digest

The proposer registers the digest it computed over the bytes it rendered — the worker's
`worker_evidence_snapshot`, the bridge's alert digest — and the backend records it against
the presenting credential with an expiry. Admission then binds the proposal to that
record.

- **For:** it covers exactly what the model saw, which is the property the whole check
  exists for, and it is nearly free on the worker side because the digest already exists.
  It is the only shape available for the bridge at all. It converts an unchecked claim
  into a recorded, per-job, expiring, credential-bound one: distinct per job, replay-bounded,
  and non-repudiable in the journal.
- **Against:** the backend is attesting, not verifying. It can say "proposer X told me at
  time T that it saw bytes with this digest"; it cannot say those bytes were Chronos's
  facts, or that they were facts at all. A hostile proposer registers whatever digest it
  likes and then cites it truthfully. Calling that record "the evidence the supervisor
  issued" — check 9's current wording — would be a false label, so the record's kind must
  say `attested` and the ladder must know the difference.
- **Requires:** everything in A's list except the served-bytes conjunct, plus: an
  attestation is bound to the credential that registered it and refuses when cited by
  another proposer; two proposers cannot register the same bundle id; and the journal and
  any rendering distinguish `attested` from `issued` rather than showing both as
  "evidence".

### Option C — Issue *and* attest, and require them to agree (recommended)

Option A's issuance, plus Option B's attestation, plus one rule: for a proposer whose
evidence the backend served, the digest it cites must equal the digest the backend
recorded. The worker gets that for free by rendering the served canonical bytes verbatim
into the prompt, which is what it already does with its own canonicalization. The
bridge, whose evidence Chronos never served, uses the attested kind alone.

Concretely, the check splits across two planes that already exist:

- **At STAMP (authority half).** The drain resolves the cited bundle id against the
  durable record, exactly where and how it already re-resolves the proposer registration
  against the drain's clock (`runtime.py:350-357`, `build_identity_resolver`). No record,
  wrong proposer, or expired against the drain's `now` → a STAMP-stage refusal alongside
  `PROPOSER_UNRESOLVED`; the proposal is never judged. Provenance is stamped from the
  **record**.
- **At admission check 9 (agreement half).** The check keeps its exact-match semantics
  and gains the second, independent side it has never had: the decision must carry an
  `EvidenceCitation` whose `evidence_id` is the expected bundle id, and that citation's
  digest must equal the expected digest. The payload's claim finally faces the backend's
  record inside the pure kernel, where every refusal is reproducible from its inputs.

- **For:** it is the only option under which check 9 stops being a tautology, and it puts
  the load-bearing comparison in the pure, exhaustively-tested function rather than in the
  wiring. Equality catches the realistic failure — an honest worker whose rendering drifts
  from what it fetched (truncation, reordering, a key-order change, a partial fetch) — which
  is exactly the class that produced R-24..R-27. It composes into ADR-0024 without rework.
- **Against:** it costs A plus B, and it does **not** catch a *dishonest* proposer: one
  that prompts on other bytes and cites the served digest is indistinguishable from an
  honest one. Requiring equality also couples the backend's serialization to the worker's
  rendering — a change to either breaks every forward until both move, which is the
  fail-closed direction but is real operational coupling that must be disclosed and
  version-pinned (`bundle_version` already exists on the type,
  `chronos/autonomy/evidence.py:139`).
- **Requires:** the union of A's and B's lists, plus: a proposal whose cited digest
  disagrees with the issued record refuses at admission with a distinct code from the
  unissued case; a proposal carrying *no* citation for the expected bundle refuses; and a
  bridge-sourced (attested-kind) proposal is never accepted under the served-kind rule, or
  vice versa — kinds do not substitute for one another.

### Option D — Keep the placeholder, and forbid what depends on it

Change nothing mechanical. Record the rule that ADR-0023 already implies: no promotion
artifact, and no rung above `shadow`, while evidence binding is a constant.

- **For:** free, and honest. Today's realistic exposure is a loopback-bound worker and
  bridge the owner runs, no family has cleared any rung, and no real gateway has ever been
  connected — so nothing is currently harmed by the constant.
- **Against:** it leaves a check in the codebase that reads as a control and cannot act,
  which this repository has been burned by four times, and it leaves ADR-0016's promotion
  bindings and ADR-0024's Option B unbuildable. It also defers the work to the moment real
  evidence exists — which ADR-0024 argues is precisely the wrong moment, because then the
  question becomes whether to honor evidence that predates the mechanism.

## Recommendation

**Option C, with the bridge on the attested kind, and evidence binding off by default.**

Three reasons, in order of weight.

1. **Only C gives check 9 two independent sides.** A and B each move the constant to a
   per-job value without ever making the comparison meaningful — the stamper and the
   expectation still read one record. If the owner accepts anything here, it should be the
   version where the check can refuse.
2. **The expensive half is already built.** The worker canonicalizes and digests the
   exact prompt bytes; the bridge digests the exact alert; the durable-row-plus-hash-chain
   pattern is `supervisor/durable.py`; the resolve-at-drain-against-the-drain's-clock
   pattern is `build_identity_resolver`. What is missing is a composing route, a table, and
   the second half of one check.
3. **It removes a credential from the worker.** Under C the worker's evidence path stops
   needing the backend's general API token — a real reduction in what a compromised worker
   holds, and one that R-47(d)/R-48(d) currently disclose as a residual. Making
   `CHRONOS_WORKER_API_TOKEN` optional is worth doing in the same build; this ADR does not
   require it.

**Posture.** A new setting (`AUTONOMY_EVIDENCE_BUNDLES`, unset by default) is the switch,
following `AUTONOMY_MANDATE_FILE` and `AUTONOMY_PROPOSERS_FILE`.

- **Unset — today's posture, verbatim.** `INGRESS_IDENTITY`'s `owner-workspace` /`None`
  on both sides, `CycleFacts` carrying the expectation, check 9 passing tautologically.
  Not "approximately today": the acceptance criterion is that the unset path produces
  byte-identical journal rows to `a74cd09`, proven by test, because a posture switch that
  quietly changes the default posture is the failure this repository fixes rather than
  ships.
- **Set — evidence binding in force.** Check 9's expectation no longer comes from
  `CycleFacts` at all; it comes from the per-proposal record the drain resolved. A missing
  record never reaches check 9 (STAMP refused it); an unresolvable one reaching check 9
  anyway keeps the existing `EVIDENCE_BUNDLE_UNKNOWN` refusal with `evaluated=False`. The
  digest comparison keeps its tri-state discipline exactly: `None` is never a pass, and
  under this posture a `None` digest in provenance means the stamper had no record — which
  is a defect, and must refuse rather than read as attested absence. Two additive refusal
  codes (`EVIDENCE_BUNDLE_EXPIRED`, `EVIDENCE_BUNDLE_UNCITED`) keep stale, forged and
  absent distinguishable in the journal; nothing existing weakens, and
  `EVIDENCE_BUNDLE_MISMATCH` keeps its current meaning.
- **Set without a proposer registry — refuses, never falls back.** A bundle is issued *to*
  a credential; with no registry there is no author to issue to. The combination logs an
  error, raises a CRITICAL owner alert, and refuses every proposal at the route, the same
  shape a broken registry already has (`auth.py:132-139`). It does not boot the backend
  down: the process that can still close positions never dies because a grant was
  malformed.

**Expiry: the backend's clock, judged at the drain, default 300 seconds.** The clock
question has one defensible answer — the drain's `now`, the same clock that judges
registration currency, so a bundle that expires between enqueue and drain refuses at the
moment authority is exercised rather than the moment bytes arrived. The proposer's `as_of`
is data in the record, never the judge. The 300 s number is a disclosed judgment, not a
derived one (the `MARKET_PROTECTION_COLLAR` precedent): it must exceed worst-case
gather → model call → POST → queue wait → drain latency, and a TTL set below a worker's
think time refuses everything, which is the safe direction and a visible failure rather
than a silent one. It should be settable with a hard ceiling, and unparsable refuses to
start.

**Multi-citation within the window, bounded by what already bounds it.** One issued
bundle may back more than one proposal until it expires: a single evidence read
legitimately supports several decisions in one thinking pass, and single-use would collide
with `MAX_RESUBMISSIONS`'s retry budget in ways that punish honest retries. Replay
protection stays where it is — economic-content `decision_id` and the durable attempt
counters — and every citation is journaled.

**The bridge gets its own kind, and it is not a substitute.** `alert_attested` records
what the bridge can honestly claim: this credential asserted at time T that it received an
alert with this digest. The record must never be rendered, journaled, or read as "evidence
the backend issued", and the recommended rule for ADR-0024 to adopt is blunt: **an
attested bundle may back a proposal; it may not back a promotion rung.** A source whose
evidence originates outside Chronos cannot produce evidence Chronos witnessed, and calling
it otherwise would be exactly the false-evidence class the ladder exists to prevent.

**One authorization-surface change, stated loudly.** Issuance is a *write* reachable by a
proposal-only credential, and it makes that credential open a second route. R-48's
enumeration test — every mutating route, every way a confused process could present the
credential, all 401 — must grow this route as a deliberate, named exception rather than
absorb it. Two bounds come with it: a per-proposer issuance cap in the shape of
`proposals.MAX_PENDING` (a proposer that could mint unbounded rows is a disk-filling
denial of service against the process holding the broker connection), and a retention rule
for expired rows that keeps the hash chain intact. This should be the last route that
credential opens without a further ADR.

Option D is rejected on ADR-0024's own ground rather than on cost: deferring until real
evidence exists means deciding, later, whether to honor evidence that predates the
mechanism. Option B alone is rejected as a *general* answer because a digest the backend
cannot recompute is a record, not a check — but it is the honest fallback if the owner
judges the composing route too large a build, and it is strictly better than today.

## Consequences if accepted as recommended

- A new durable table and migration (head is `0007_proposal_proposer.py`), hash-chained on
  the existing per-account stream, written transactionally with the issuance that
  describes it.
- A new issuance route requiring the proposer credential; the worker's evidence path moves
  onto it and off the four token-authenticated reads.
- `CycleFacts.evidence_bundle_id`/`_digest` stop being the expectation source under the
  configured posture; the per-proposal resolution joins identity resolution at the drain.
  `CycleFacts` keeps both fields for the unset posture, unchanged.
- Admission check 9 gains a payload-side half and two additive refusal codes. `evidence.py`'s
  docstring claim — "the supervisor compares the decision's cited digest against the bundle
  it issued" — becomes true for the first time; today it describes an intent.
- ADR-0023's gating rule becomes satisfiable in both halves: identity stopped being a
  constant on 2026-08-12, evidence stops being one here. ADR-0024's Option B gains
  something that resolves.
- `mandate check` should surface the evidence posture the way it was made registry-aware,
  so a mandate authored against the wrong posture is BLOCKING at authoring time rather
  than a run of silent refusals at trade time.
- A RISK_REGISTER row with the residuals below, on acceptance and with the build — not
  with this draft.

## Bounds — what this does not achieve

Stated plainly, because a protocol named "evidence binding" invites more belief than it
earns.

**A hostile proposer can still:** fetch a bundle, reason on entirely different text, and
cite the issued digest — undetectable, because the backend cannot observe a prompt in
another process; choose *when* within the TTL to fetch, selecting a favorable moment
inside stale-but-unexpired evidence (bounded only by the TTL and, separately, by
admission's own market-data freshness check, which reads the supervisor's quote and not
the bundle); and back several proposals with one bundle inside the window. What it
**cannot** do: author provenance, cite a bundle issued to another proposer, cite an
unissued or expired one, or have a proposal admitted whose citation disagrees with the
record.

**Equality catches accident, not malice.** This is the honest description of Option C's
central rule and should be repeated wherever the feature is described.

**Attested is not witnessed.** For the bridge — and for any future proposer whose evidence
originates outside Chronos — the record binds a claim to a credential and a time. It is
non-repudiation, not verification.

**A bundle binds which facts were served, never that they were true.** Every fact in it
comes from a fixture-verified adapter path: no real IBKR gateway has ever been connected in
this project's history, so a bundle proves the backend served these numbers, not that the
broker held these positions. Nothing here leaves MITIGATED. The §7 read-only campaign
remains the only thing that changes that, and it remains owner-gated.

**The chain is tamper-evident, not tamper-proof.** `persistence/hash_chain.py` says so
about itself; an attacker who can write the database can recompute a consistent tail. This
protocol inherits that bound and does not narrow it.

**None of this makes a worker's decision evidence.** No registry write, no rung, no
statistical claim — the rule R-45 and R-47 both state, restated because a per-job evidence
record is exactly the artifact someone will later be tempted to read as research evidence.

## What this ADR does not decide

- **The promotion artifact.** Whether and how a bundle reference appears in a rung is
  ADR-0024's decision; this ADR only makes one resolvable, and proposes the
  attested-may-not-promote rule for ADR-0024 to adopt or reject.
- **The research evidence plane.** Campaign manifests, registry runs and holdouts are a
  different kind of evidence with its own gates (ADR-0013/0014/0015); nothing here touches
  them, and the two must not be conflated because they share the word.
- **Policy-content pinning.** Binding `prompt_version` to the policy file's bytes is item
  A4 and R-47(b); an evidence digest says what the model *saw*, never which policy it ran
  under.
- **Live proposer revocation** (item A3, R-48(c)). The registry stays a boot-time snapshot
  here; issuance inherits that posture and does not improve it.
- **Whether the worker keeps the general API token.** Recommended to drop from the
  evidence path, but the change is a separate, small decision.
- **Anything about arming, the four inert economic fields, or the supervisor's plane
  isolation.** Untouched, deliberately.
