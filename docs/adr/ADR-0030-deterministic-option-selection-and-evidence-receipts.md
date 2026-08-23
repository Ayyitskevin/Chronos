# ADR-0030: Deterministic Option Selection and Evidence Receipts

Status: accepted (2026-08-01)
Date: 2026-08-01
Index entry: DECISIONS.md **D-34**.
Extends: ADR-0016 (controlled model authority), ADR-0017 (runtime wiring and
owner-directed autonomy), ADR-0009 (the one live order boundary).
Addresses: ADR-0017 known limitation 2, for the exact v1 scope below; the
real-broker eligibility residual in "Consequences and residuals" remains.
Implementation posture: default-off; real IBKR evidence resolves to `NO_TRADE`.

## Context

The autonomy runtime can qualify equities and crypto, but deliberately refuses
an equity-option decision at its instrument seam. A model names an economic
intent, not a strike or expiry, and choosing an option without complete broker
evidence would turn missing data into trading authority.

Chronos already has a bounded read-only option-chain workflow and a deterministic
Wheel strike resolver. They are useful primitives, but their presentation models
are not an authority-bearing evidence record: they do not prove that every
broker response was complete, do not bind the applicable market-rule schedule,
and do not produce canonical replay bytes. This ADR defines that missing
boundary.

ADR-0016's broader option capability matrix remains the historical programme
direction. ADR-0017 correctly recorded that chain selection still refused. This
decision does not erase either record; it narrows the executable first release
and leaves every broader option shape for a later ADR and promotion.

## Decision

### 1. The v1 executable scope is exact

This resolver applies only to an admitted autonomous decision with all of these
properties:

- `kind == OPEN`;
- `asset_class == EQUITY_OPTION`;
- strategy is `CASH_SECURED_PUT` or `COVERED_CALL`; and
- the underlying is an owner-allowlisted US equity or ETF in the active mandate.

A cash-secured put deterministically maps to a put; a covered call maps to a
call. The resolver selects one listed equity-option contract. It does not add
long calls or puts, index options, futures options, spreads, combos, rolls,
replacements, or a new close path. Existing manual and risk-reducing paths are
unchanged. Any combination outside this list returns typed `NO_TRADE`.

### 2. Economic intent cannot name broker identity

`OptionSelectionRequest` is frozen, `extra="forbid"`, versioned, and constructed
inside the deterministic app/supervisor boundary only after admission. It is
derived from the admitted economic intent. DTE, delta, liquidity, routing,
session, multiplier, order-form, and score constraints live in the separately
owner-derived `OptionSelectionPolicy`, whose digest is bound into the receipt
and any live resolver promotion.

The model-authored decision cannot name an option `right`. The economic request
does carry `right`, but only as a deterministic derived field: a cash-secured put
maps to `PUT`, and a covered call maps to `CALL`; validation rejects any
strategy/right contradiction. Neither the model-authored decision nor the
economic request can name `conId`, `localSymbol`, `tradingClass`, exchange,
expiry, strike, multiplier, market-rule id, routing, account id, client id,
order id, or `transmit`. Raw broker account identity never enters the request;
the surrounding mandate, promotion artifact, and durable stream use the
existing pseudonymous account fingerprint.

Unknown and extra fields are rejected, never stripped. No free-form model text
is read by selection, scoring, pricing, or tie-breaking.

### 3. Evidence is read-only, complete, and time-bound

The selector consumes a frozen `OptionSelectionEvidence` assembled through a
narrow read-only broker port. It holds no preview, order, cancellation, policy
write, activation, or promotion method. Source and collection timestamps are
explicit wherever the corresponding fact is observed; quote quality,
market-rule source, deliverable source, chain completion provenance, and the canonical
aggregate evidence digest remain distinct facts rather than invented defaults.

One evaluation must contain:

1. the uniquely qualified stock underlying, including symbol, stock security
   type, currency, exchange identity, and underlying `conId`;
2. an option-chain response tied to that underlying, with `complete`,
   `truncated`, the broker's terminal completion marker, the time that marker
   was observed, and its adapter/source identity;
3. exact contract-detail qualification for every bounded candidate considered;
4. the applicable market-rule ids and complete price-increment schedules;
5. timestamped underlying and option quotes, including bid, ask, required delta,
   volume, open interest, and data quality without invented defaults; optional
   fields such as last price are retained when the broker supplies them but are
   not fabricated or made a v1 eligibility prerequisite;
6. session evidence (`liquidHours` and timezone) for the exact instrument; and
7. authoritative standard-deliverable evidence tied to the exact option and
   underlying.

Qualification and quotes must match the requested set exactly. Missing,
duplicate, unexpected, ambiguous, conflicting, stale, future-dated, partial, or
pacing-truncated evidence returns a stable typed `NO_TRADE`; a non-empty partial
answer is not success. Unknown volume or open interest remains `null`, never
zero, and always blocks a candidate in v1 even when the configured numeric floor
is zero. Missing evidence cannot become a liquidity score.

Raw market-rule, deliverable, and quote observations are retained separately
from their candidate projection. A complete candidate set requires one raw
observation of each kind per candidate, with exact value and timestamp equality
to the projection. Missing groups, duplicate or unexpected identities, and
same-identity content conflicts therefore remain visible as
`OBSERVATION_SET_MISMATCH`; they are never collapsed by a dictionary join or
represented as a fabricated qualified candidate.

The evidence assembler applies explicit structural bounds. Raw chain metadata is
rejected above 32 rows, 512 expirations per row, or 4,096 strikes per row. The
deterministic acquisition plan then records the policy bounds, eligible,
selected, and excluded expirations, selected and excluded strikes, and every
requested contract specification. Its hard ceilings are 8 expirations, 20
strikes per expiration, and 80 requested contracts. Exact-set reads reject
missing, duplicate, or unexpected results and retain already observed facts,
including successful earlier qualification batches, in a typed partial-evidence
receipt. Cached contracts are not relabelled as observations from a failed
fresh read. Each market-rule schedule is limited to 256 price increments;
over-limit input is rejected while a deterministic bounded schedule is retained
as failure evidence. Broker codes are limited to 32 characters, local symbols
and timezones to 128, session strings to 4,096, and deliverable asset evidence
to 32 entries of at most 128 characters. A recursive 4,096-character backstop
applies before any evidence canonicalization. The assembler never silently
truncates a broker response
or substitutes cached evidence whose age or completion proof cannot be
established.

### 4. Contract eligibility is conjunctive

A candidate is eligible only when every hard gate passes for the same evidence
snapshot:

- exact underlying, option security type, symbol, currency, exchange, trading
  class, contract family, and broker identity; account scope is bound outside
  the candidate by the mandate, promotion artifact, and account-scoped stream;
- deterministic right, owner-policy DTE window, OTM/moneyness rule, and delta
  window;
- positive non-crossed market, permitted quote quality and age, relative-spread
  ceiling, and configured liquidity floors;
- multiplier and authoritative standard deliverable;
- permitted and open trading session; and
- a complete, non-conflicting market-rule schedule with an applicable positive
  price increment.

Every considered candidate becomes a frozen `CandidateEvaluation`, eligible or
rejected, with normalized facts and stable reason codes. A global evidence
failure also appears explicitly in the receipt. No gate defaults to passing.

### 5. Ranking and price conformance are deterministic

After hard filtering, v1 uses a versioned weighted score over normalized absolute
delta distance, relative spread, DTE distance, and a bounded liquidity bonus.
The receipt records every component, weight, intermediate normalized value, and
final score.

Ascending rank uses this total order:

1. score;
2. absolute delta distance;
3. relative spread;
4. DTE distance;
5. descending open interest;
6. descending volume;
7. expiry;
8. strike; and
9. `conId`.

All candidate, callback, and broker-row inputs are normalized before filtering,
so input order cannot change evaluations, selection, receipt bytes, or digest.
An empty eligible set is `NO_TRADE`, never a fallback to the least-bad contract.

The selector owns the receipt-bound limit-price derivation. For the v1 opening
short-option shapes it derives the sell price from the owner-granted order form,
selects the increment applicable to that price from the complete market-rule
schedule, rounds away from aggression, and records both tick and limit in every
eligible evaluation and the selected result. A missing rule, unmatched price
band, contradictory increment, band-crossing round, or rounding ambiguity is a
typed refusal. A legacy or guessed penny tick is not evidence.

The existing compiler remains an independent execution gate: it receives the
selected quote and the receipt-bound tick on the qualified contract, derives the
price again without trusting the receipt's number, and the supervisor requires
exact contract and limit-price equality. A mismatch stops before the order
plane.

### 6. The receipt is the replay boundary

Every evaluation emits one frozen, versioned `OptionSelectionReceipt`, including
at least:

- request and owner-policy digests plus the exact canonical digest of the full
  owner mandate;
- request, receipt, resolver, scoring, canonicalization, broker-evidence-adapter,
  and policy versions;
- evaluation time and every evidence timestamp/digest;
- normalized evaluation for every considered candidate;
- hard-gate facts and reason codes;
- score components and the complete tie-break tuple;
- the exact selected contract, or typed `NO_TRADE`; and
- an output digest.

The accepted bindings are `option-selection-request-v1`,
`option-selection-policy-v1`, `option-selection-evidence-v1`,
`option-selection-receipt-v1`, `deterministic-option-selector-v1`,
`option-score-v1`, `chronos-canonical-json-v1`,
`option-evidence-port-v1`, and `option-resolver-promotion-v1`.

Canonical serialization uses sorted object keys, compact UTF-8 JSON, one UTC
timestamp spelling, normalized exponent-free finite Decimal strings, enum
values, explicit domain ordering for every collection, and no floats, naive
times, non-finite values, sets, secrets, credentials, or raw account ids. The
SHA-256 output digest covers a version-tagged receipt body that excludes only
the digest field itself. The full canonical receipt stores that digest. Every
request, policy, acquisition, and raw-evidence Decimal has a structural
32-significant-digit and exponent `[-18, 18]` bound. Recursively validated
derived receipt values have a 64-significant-digit and exponent `[-128, 128]`
bound, which accommodates the selector's 64-digit arithmetic context without
allowing a persisted finite value with pathological fixed-point rendering to
exhaust memory or make even the size-guard refusal itself non-durable.

Replay takes the frozen request, policy, evidence, mandate digest, compatibility
digest, and optional promotion digest only. It opens no broker connection and
reads no wall clock. `OptionSelectionReceipt.verifies()` rebuilds the outcome
and requires byte equality, not merely a matching stored digest. Identical inputs
must produce byte-identical receipt bodies, full receipts, and digests. Any
material change to a request, receipt, canonicalization, evidence-adapter,
filter, score, tie-break, market-rule, or resolver version changes the version
binding and invalidates promotion. The compatibility digest conservatively
hashes every shipped runtime Python source under `src/chronos`; source discovery
is mechanical so a newly added authority module cannot be omitted from a
hand-maintained manifest.

Before a selection can authorize downstream use, the canonical receipt is
bounded to 1,000,000 bytes, appended to the account-scoped
`autonomy.option-selections` hash chain, committed as a durability barrier, and
read afresh. Verification covers the complete cryptographic chain and every
stored semantic envelope: the outer envelope's exact canonical JSON bytes,
canonical receipt bytes, deterministic replay, status, digest, decision id,
record kind/time, and decision-id uniqueness. Hash-chain timestamps are
normalized to UTC before both hashing and storage. Durable verification streams
one row at a time and asks SQLite for storage type, length, and only a bounded
prefix of every dynamically typed field; neither the supervisor nor terminal
loads an unbounded corrupt row through the database driver. Appends likewise
read only a bounded sequence/hash head link, never the prior payload. The exact
committed receipt and full stream are verified again immediately before an
order-plane handoff.

### 7. Integration adds no execution authority

On selection, the exact qualified contract, quote evidence, multiplier, market
rule, and receipt digest enter the existing supervisor facts. The existing
compiler, order-plane risk engine, reconciliation, writer lease, idempotency,
preview/confirmation flow, kill switch, live gates, and single guarded submit
site remain unchanged. The selector cannot submit, preview, modify, cancel,
exercise, route, transmit, or mint an execution approval.

There is no AI-specific order path and no fallback to direct broker access. An
exception or unavailable selector result is a recorded `NO_TRADE`, not
permission to compile. System/evidence refusals raise a deduplicated
`option_selection.system_failure` owner alert. Missing, conflicting, unknown,
identity-invalid, stale/future, or source-quality evidence is a system refusal;
ordinary numeric candidate misses such as DTE, moneyness, delta range, spread,
volume, or open-interest thresholds do not manufacture an operational alert.

### 8. Activation and promotion fail closed

The feature is off by default. Offline replay and explicitly enabled inspection
confer no trading authority. Enabling evaluation does not itself enable an
autonomous handoff.

Existing asset-family promotion is necessary and not sufficient for autonomous
option trading. Each `CANARY_LIVE_AUTONOMOUS` or `LIVE_AUTONOMOUS` mode
additionally requires a separate owner-created promotion artifact naming
**exactly one** of those modes. It is bound to the pseudonymous account, exact
canonical mandate digest, policy digest, request/evidence/receipt schemas,
resolver, scoring, canonicalization, evidence-adapter version, and a digest of
every material implementation source. The artifact does not pre-authorize a
future per-evaluation request or receipt digest; those remain facts recorded and
checked for that evaluation. Missing, expired, malformed, differently scoped,
or mismatched artifacts block the handoff.

The live gate is checked when resolution starts, then the artifact, effective
window, policy, and material-source digest are re-read after acquisition. The
exact receipt-bound artifact and source digest are checked a third time
immediately before order-plane handoff. Replacement, deletion, expiry, or code
change during either interval fails closed.

The runtime contains a loader and verifier only. It has no endpoint, CLI command,
startup behavior, migration, or background job that creates, edits, upgrades, or
promotes this artifact. Authoring it is an explicit owner action after review and
evidence collection.

### 9. Inspection and replay are read-only

Operators inspect stored receipts and verification status through the
authenticated, bounded `GET /terminal/option-selections` view. It verifies the
account-scoped hash chain, every envelope in the full stream, decision-ID
uniqueness, and each returned receipt's canonical semantic replay. Oversized
invalid receipt or envelope text, malformed/deep JSON, and corrupted sequence,
timestamp, kind, payload, or hash storage are reported as invalid but are not
echoed through the API. The semantic scan streams the full history one row at a
time while retaining only the bounded newest page plus decision identifiers;
page size does not weaken whole-stream verification or make receipt-model memory
grow with history. Exact duplicate detection necessarily retains one bounded
decision identifier per historical receipt, so that identifier set grows
linearly with stream history even though receipt bodies do not.
Programmatic/local replay uses the frozen `OptionSelectionReceipt` model's
`verifies()` method; v1 does **not** promise a separate replay CLI. These
surfaces expose no approve, arm, promote, submit, cancel, modify, exercise, or
replay-with-live-data action. Unknown facts render as unknown, not zero, and
receipt views expose no raw account or credential material.

## Verification requirements

The implementation is not complete without:

- unit and property tests for every filter boundary, normalization rule, score
  component, tie-break, market-rule band, and side-aware tick rounding;
- a golden frozen replay fixture pinning exact canonical bytes and digest;
- candidate, callback, and broker-row permutation tests;
- adversarial fixtures for duplicate or mismatched contracts, stale/future or
  conflicting timestamps, crossed/zero markets, non-standard deliverables,
  mixed account scope, partial or pacing-truncated chains, and bad market rules;
- a mutation-style matrix proving every gate changes a valid baseline into the
  expected typed `NO_TRADE`, with exact coverage of the rejection-code enum;
- structural proof that model-authored schemas cannot express contract identity,
  the selector has only read dependencies, and the runtime cannot create a
  promotion artifact;
- unchanged repository-wide broker-mutation and single-transmit inventories;
- integration proof through the existing compiler/risk/reconciliation/handoff
  using fakes, with every refusal leaving submit calls empty; and
- the full offline CI gates: pytest, strict mypy, Ruff check, and Ruff format.

Tests and CI use no network, credentials, live brokerage endpoint, or real order.

## Consequences and residuals

- Autonomous v1 option intent becomes deterministic and replayable without
  granting the model broker identity or execution authority.
- The receipt is evidence of what the resolver saw and decided, not an approval
  to trade. Downstream gates independently re-derive their own truths.
- **Real IBKR selection is currently `NO_TRADE`.** TWS API contract details do
  not expose OCC's authoritative deliverable schedule. Chronos's existing
  root/multiplier screen is a useful non-standard-contract detector but is not an
  authoritative statement of what the contract delivers, so it cannot satisfy
  this ADR's deliverable gate. Demo and offline fixtures must be labelled as
  synthetic. A future authoritative source, adapter implementation, owner
  read-only gateway verification, and fresh promotion artifact are required
  before real autonomous options can pass.
- No live resolver-promotion artifact is created or shipped by this change.
  `ENABLE_AUTONOMY_OPTION_SELECTION` remains false by default; setting it is not
  live authority.
- Real market-rule ids, increment schedules, chain completion behavior, quote
  permissions, volume/open-interest availability, pacing, and session strings
  remain owner read-only gateway-verification items. Failure in any item remains
  `NO_TRADE`; no live access is attempted silently.
- If an id-less ib_async error races a completed multi-contract qualification,
  the failure evidence retains the returned contract IDs but does not promote or
  persist the raw, pre-ContractDetails spec-to-result association as candidate
  authority. The outcome remains `NO_TRADE`; richer non-authoritative raw
  qualification receipts are a future explainability refinement.
- This ADR does not promote any asset family and does not assert profitability,
  execution quality, or readiness for live money.
