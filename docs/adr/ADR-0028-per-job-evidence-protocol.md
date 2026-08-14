# ADR-0028 — The per-job evidence protocol

Status: **proposed — owner decision required.** No `DECISIONS.md` row until accepted.

Date: 2026-08-13

Closes, if accepted: `docs/VISION_COMPLETION_PLAN.md` §6's required design outcome "one
immutable per-decision evidence snapshot used by both the model and gateway", and the
evidence half that ADR-0023 explicitly left open. Unblocks ADR-0024 (§6 finding 8): a
promotion artifact needs real identity *and* real evidence, and identity is done.

Written against `a74cd09`. Every file:line below was verified at that commit.

## Context

ADR-0023 closed identity and said in its own closing section that it did not decide "the
job/evidence/response protocol's shape (plan §6's job ID, evidence digest, expiry)", and
that if Option A were taken — it was, as D-24 — "that protocol is its own design work and
should carry its own ADR". This is that ADR.

Four facts, each verified, describe what exists today.

### 1. Admission check 9 cannot fail

`_check_evidence_bundle` (`supervisor/admission.py:587-627`) compares the decision's
stamped `provenance.evidence_bundle_id` and `evidence_bundle_digest` against
`state.expected_evidence_bundle_id` / `_digest`. Both sides come from the same place: the
supervisor's expectation is built from `CycleFacts`, which `BackendGatherers.cycle_facts`
fills from the `INGRESS_IDENTITY` constants (`api/autonomy_wiring.py:93-96, 339-340`), and
the provenance is stamped from the same constants — or, with a registry configured, from
`build_identity_resolver`, which copies those same two constants onto every registration
(`autonomy_wiring.py:254-255`).

So the check reads `"owner-workspace" == "owner-workspace"` and `None == None`, on every
proposal, forever. The comparison logic is correct and carefully written — the ADR-0023
note about attesting absence as absence rather than as sixty-four zeros is exactly right —
but it is comparing a constant to itself. It is not inert in the R-25 sense (it would fire
if the values ever differed), yet nothing can make them differ. **It is a check with no
evidence to check.**

### 2. The one real digest in the system is unbound

The worker computes an honest digest: `worker/evidence.py:104-112` canonicalizes the exact
document it renders into the prompt and takes SHA-256 over those bytes, and
`EvidenceSnapshot.citation()` (`:65-74`) attaches it to every proposal as an
`EvidenceCitation` of kind `worker_evidence_snapshot`. That digest is real, it is computed
by deterministic worker code rather than by the model, and it covers precisely what the
model saw.

Nothing in Chronos ever compares it to anything. The decision contract carries the
citation tuple (`autonomy/decision.py:194-207`), the journal records it, and no check
reads it. A worker that fabricated the digest, or cited a snapshot it never read, would be
admitted identically.

### 3. `EvidenceBundle` is a type with no issuer

`autonomy/evidence.py` defines the bundle, its content-derived `digest()`, a redaction
tripwire, and `issue()` (`:170-203`) whose docstring says the caller "must record it as the
*expected* digest for this run". Grep finds no caller: nothing issues, stores, serves, or
expires a bundle. The four shipped READ tools take a bundle (`autonomy/tools.py:176-197`)
and nothing constructs one to give them.

### 4. The bridge's evidence is not a snapshot at all

The TradingView bridge (ADR-0026) does not read backend evidence. Its input is the alert
body that arrived from the public internet, and it already digests those exact bytes into
its own citation (`chronos/bridge/`). Whatever protocol is chosen must say what a bridge
does, or it will either be locked out of the ingress or be allowed to cite evidence it
never read — and the second is worse than the first.

### The consequences that make this worth building

- **No promotion artifact is issuable.** ADR-0016's promotion bindings, and the rule
  ADR-0023's recommendation stated ("no promotion artifact may be issued while
  `HarnessIdentity` is a constant"), have an evidence twin: an artifact claiming a family
  earned a rung on evidence, when the evidence field is a placeholder string, is exactly
  the class of false evidence the ladder exists to prevent. ADR-0024 stays unbuildable.
- **The audit trail cannot answer "what did it see".** The journal records what the model
  said and what the kernel decided. It does not record the facts the model reasoned from
  in any form anyone can verify afterwards, which is the one thing that would let an owner
  reconstruct a bad decision.
- **Two proposals from the same proposer are indistinguishable in their evidence.** There
  is no job identity, so "this decision came from that read" is unexpressible.

### The design constraints that shape the fix

These are not preferences; each is a closed boundary or an established doctrine, and a
protocol that violates one is not on the table.

1. **The gateway must not price from the model's bundle.** Plan §6 asks for "one immutable
   per-decision evidence snapshot used by both the model and gateway", and the naive
   reading — the supervisor sizes and compiles from the bundle the model was shown — would
   invert the fact-gathering rule that `CycleFacts` exists to enforce ("Never
   model-supplied", `supervisor/loop.py:112-143`). **Used by both** must mean: the model
   reasons from it, and the gateway *verifies against* it — the supervisor keeps gathering
   its own facts at judge time and pricing exclusively from those.
2. **No self-attributed provenance.** ADR-0016 §2 and the ingress's refusal of
   writer-owned fields (`supervisor/ingress.py:179-186`) mean the bundle claim cannot be a
   payload field the proposer authors. It has to arrive the way `proposer_id` does: as a
   route-level fact the transport verified.
3. **A check whose evidence is absent is a refusal** (`admission.py:11-13`). Every failure
   mode below must land on refusal, not on a pass.
4. **The registry-off posture must keep working.** ADR-0023's shape — unset means the old
   posture verbatim — is the precedent, and an evidence protocol that only functions with
   a registry configured would make the unconfigured posture *less* honest than it is now.
5. **Issuance is a write.** A read-only backend must not issue bundles; the writer lease
   already gates the proposal route, and issuance belongs behind the same gate.

## The decision

Five sub-decisions. Each is presented with its options because each can be got wrong
independently, and taking a different option on any one of them yields a coherent (if
weaker) protocol.

### Decision 1 — Who computes the digest

**Option 1A — The backend digests the exact bytes it serves.** A new proposer-facing read
endpoint returns the evidence document *and* records the digest of the response body it
just produced. The proposer renders those bytes and cites the returned bundle id.

- **For:** the digest covers bytes Chronos itself produced, so "the evidence on record"
  and "the evidence served" cannot diverge — there is no canonicalization the proposer
  could get wrong, differently, or dishonestly. The comparison at admission becomes a real
  comparison against a value the backend independently holds.
- **Against:** the worker must stop assembling its snapshot from four separate reads
  (`/account/summary`, `/account/positions`, `/orders`, `/terminal/bars`) and take the
  served document instead — a real change to `worker/evidence.py`, and one that moves the
  choice of *what is in* the snapshot from the worker's config into the backend.
- **Requires:** exercised tests that the served bytes and the recorded digest match
  byte-for-byte; that a proposal citing a digest that is not the recorded one refuses; and
  that the endpoint refuses under a read-only lease.

**Option 1B — The backend accepts a worker-computed digest of the bytes it was served.**
The worker keeps assembling and canonicalizing; it registers its digest with the backend,
which stores it as the expected value.

- **For:** a much smaller change; the worker's existing digest becomes load-bearing
  as-is.
- **Against:** the backend cannot verify that the digest covers what it served. The
  protocol would then attest "the worker told us what it hashed", which is the same class
  of claim as self-attributed provenance — a claim by the party the check exists to bind.
  It would also re-introduce a canonicalization contract between two processes: a
  key-ordering change on either side silently breaks every proposal.
- **Requires:** the same tests, minus the one that matters.

**Recommendation: 1A.** The whole value of the check is that it is not the proposer's
word.

### Decision 2 — Where issued bundles persist

**Option 2A — A durable table plus a hash-chain record.** A new
`autonomy_evidence_bundle` table (migration `0008`, head today is `0007_proposal_proposer.py`)
holding `bundle_id`, `account_fingerprint`, `proposer_id`, `kind`, `digest`, `issued_at`,
`expires_at`, and the served content itself; issuance also appends to the existing
`autonomy.authority`-style chain via `durable.stream_for` so an issuance cannot be quietly
removed after the fact.

- **For:** matches every comparable durable act in this system (activations, revocations,
  counters, alerts) and survives restart, which an in-memory map does not. Storing the
  content — not only the digest — is what makes the audit trail able to answer "what did
  it see", and is what a future promotion artifact would have to cite.
- **Against:** disk growth. Daily bars for a small watchlist are on the order of tens of
  kilobytes per issuance; at a worker cadence of minutes this is megabytes per day, so the
  ADR must decide retention, not leave it implicit (proposal: keep content for a bounded
  window, keep the digest and metadata row forever — the row is the evidence that a
  bundle existed; the content is the convenience).
- **Requires:** migration completeness tests (`tests/integration/test_migrations.py`
  already pins v2/v3/v4→head and no-untracked-tables), and a test that an issuance
  survives a restart.

**Option 2B — In-memory only, per process generation.** The runtime holds issued bundles
in a dict keyed by id.

- **For:** no migration, no growth, trivially fast.
- **Against:** a restart invalidates every outstanding bundle (proposals in flight refuse),
  nothing is auditable afterwards, and the "evidence on record" is not on any record. It
  also cannot support a promotion artifact, which is the reason this ADR exists.

**Recommendation: 2A**, with content retention bounded and the metadata row permanent.

### Decision 3 — How a proposal claims its bundle

**Option 3A — A request header, recorded on the queue row.** `POST /autonomy/proposals`
gains `X-Chronos-Evidence-Bundle: <bundle_id>`; the route records it on the queue row
beside `proposer_id`; at drain the resolver looks the bundle up, requires it to have been
issued to *that* proposer for *this* account, and stamps provenance with **the recorded
digest**, never with anything the caller supplied.

- **For:** exactly the shape ADR-0023 established for identity — a route-level fact,
  verified at the transport, re-resolved at the moment authority is exercised. The digest
  in provenance is then a backend value by construction, so constraint 2 holds without
  anyone having to remember it.
- **Against:** one more migration column and one more header both the worker and the
  bridge must send.
- **Requires:** exercised tests that a missing header refuses; that a bundle issued to
  proposer A cannot be cited by proposer B; and that the stamped digest is the recorded
  one even when the caller supplies a contradicting citation in the payload.

**Option 3B — Read it out of the decision's `evidence` citations.** The citation tuple
already carries a digest; admission could require one citation to match the issued bundle.

- **For:** no transport change; uses a field that already exists.
- **Against:** the citations are payload content. Even though deterministic worker code
  writes them today, the protocol would be reading a binding claim out of the untrusted
  body, which is the boundary ADR-0016 §2 draws. It also cannot express "which bundle",
  only "a digest that happens to match".

**Recommendation: 3A.** Keep the citations as the model-facing narrative record; keep the
binding at the transport.

### Decision 4 — Expiry

The bound has to be longer than the real latency between reading evidence and being
judged, or every proposal refuses; and short enough that a decision is not admitted on a
picture of a market that has moved. Measured components at `a74cd09`: the worker's model
call may take up to **600 s** (`worker/model.py:56`, read timeout), the runtime's idle tick
is **60 s** (`supervisor/runtime.py:115`), and a batch drains ten proposals per tick.

**Recommendation: a configurable `AUTONOMY_EVIDENCE_TTL_SECONDS`, defaulting to 900 s,
with a hard ceiling of 3600 s and fail-closed parsing** (an unparsable or over-ceiling
value refuses to start, the posture `chronos-config-and-flags` already applies to
safety-relevant settings). 900 s = the 600 s thinking ceiling + one idle tick + margin.
The ceiling exists because evidence older than an hour is not evidence about a current
market, and no configuration should be able to say otherwise.

Expiry is evaluated **at judge time against the drain's clock**, not at enqueue — the same
rule ADR-0023 applies to registration currency, and for the same reason: authority is
exercised when the cycle runs.

### Decision 5 — What the bridge does

**Option 5A — Its own bundle kind, issued from submitted content.** The bridge POSTs the
alert bytes it received to the issuing endpoint with `kind=ALERT_PAYLOAD`; the backend
stores those bytes, digests them, returns a bundle id, and the bridge proposes citing it.

- **For:** honest about what the bridge's evidence *is*, and the audit trail ends up
  holding the exact alert that caused the proposal — which is more than it holds today.
- **Against:** a submitted-content bundle attests only "these are the bytes the proposer
  says it acted on". It is a **weaker claim** than a served-snapshot bundle, which attests
  "these are the bytes Chronos produced and served". If the two kinds are stored in one
  table and read by one check, a future promotion artifact could treat them as equivalent.
  **Mitigation, and it is not optional: `kind` is recorded on the row, the distinction is
  documented at the point of use, and any evidence-bound promotion (ADR-0024) must state
  which kinds it will accept.**
- **Requires:** an exercised test that an `ALERT_PAYLOAD` bundle is distinguishable in the
  journal, and that the weaker kind cannot be presented as a served snapshot.

**Option 5B — The bridge fetches a snapshot like the worker.** It would cite evidence it
did not use to decide anything.

- **Against:** false. The bridge's decision comes from the alert; citing an account
  snapshot it ignored would put a true-looking, meaningless binding into the record.

**Option 5C — The bridge is locked out until it has a snapshot protocol.** Fail-closed,
and it stops a working, owner-approved path (ADR-0026/D-22) for no safety gain — the
bridge's proposals already face every gate.

**Recommendation: 5A**, with the kind distinction load-bearing rather than cosmetic.

## Recommendation, in one paragraph

Take 1A + 2A + 3A + 4 (900 s default, 3600 s ceiling, fail-closed) + 5A. Concretely: a new
proposer-facing `GET /autonomy/evidence` issues and serves one document, recording it
durably with a TTL; `POST /autonomy/proposals` carries the bundle id in a header, recorded
on the queue row; at drain, provenance is stamped with the *recorded* digest after checking
that the bundle was issued to this proposer for this account and has not expired; admission
check 9 stops comparing a constant to itself and starts comparing a decision against an
issuance, refusing unissued, foreign, and expired evidence with distinct codes. The
registry-off posture keeps a placeholder-free path: with no registry, bundles are issued to
the local token holder and the same checks apply, so the unconfigured posture gets *more*
honest rather than staying frozen.

**Sequencing note.** This is an admission-semantics change on the path that can reach a
broker, so it should land in one reviewed PR after this ADR is accepted, and not be mixed
with A3 (live proposer revocation) or anything else. The build is L-sized: migration,
route, worker change, bridge change, resolver, admission, plus exercised tests for each
refusal direction.

## What this ADR does not decide

- **What is *in* a snapshot.** The document served is today's four reads; whether it should
  carry option chains, news, or filings is a separate question that touches
  `TextualEvidence` and R-30, and it does not block this protocol.
- **Promotion.** ADR-0024 decides what an artifact binds and which bundle kinds it will
  accept. This ADR makes such an artifact *possible*; it does not issue one, and nothing
  here should be read as a rung earned.
- **The tool surface.** The four shipped READ tools (`autonomy/tools.py`) still have no
  caller. Wiring them to issued bundles is future work; this protocol does not depend on
  it.
- **Retention length for stored content.** Recommended bounded, but the number is an owner
  choice about disk and audit depth, and should be set when the build lands.

## Residuals this protocol will still carry, if built exactly as recommended

1. **It binds what was *served*, not what was *reasoned from*.** A worker that fetches a
   bundle and then prompts its model with something else is caught only if it also fails to
   cite the served digest — which it controls. The protocol closes fabrication-at-a-distance
   (citing a bundle that was never issued, or one issued to someone else, or a stale one),
   not a worker lying about its own prompt. Closing that would need attestation the
   architecture cannot provide from outside the worker process, and pretending otherwise
   would be the same overclaim this ADR exists to remove.
2. **A submitted-content bundle is a weaker attestation than a served one**, permanently.
3. **The evidence a bundle contains is only as good as the backend's own reads**, which
   remain fixture-verified: no real IBKR gateway has ever been connected, so every fact in
   every bundle is, today, demo data.
4. **Bundle issuance is a new authenticated surface.** It is read-only in effect but writes
   a row, so it inherits R-48's care: proposal-only credential, writer lease, no content
   echoed in refusals.
