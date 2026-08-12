# ADR-0027 — The model worker

Status: proposed (owner directive, 2026-08-12; **owner gate open — see §8**)

Index entry: DECISIONS.md **D-23**

Supersedes: nothing in authority. One prose claim is corrected in place:
`docs/limitations.md`'s "there is still no live provider harness inside
Chronos, and by design there never will be" — the harness now exists **in this
repository** while remaining, by design, outside the `chronos` package and
outside the broker-holding process. The sentence's substance survives; its
letter is updated where it stands.

## 1. Context

ADR-0016 §3 chose the inversion that defines this system: Chronos never calls a
model — a model worker, running in a separate process, calls IN through
`POST /autonomy/proposals`, and everything that arrives is judged by a
hostile-input ingress and a deterministic gate stack. Every layer of that
design shipped over the following milestones except one: the worker itself.
The provenance stamp says `provider="external-worker"`, the ingress waits for
an external worker, the isolation tests forbid the model plane from acting —
and no model has ever actually proposed a trade. The throne was built empty on
purpose; the owner has now directed that it be filled (D-21: "the owner
builds, Chronos trades" — and D-16 before it: an approved generative model may
originate runtime trading decisions through the typed contract and single
gateway).

## 2. Decision

Build the worker **in this repository, as a top-level `worker/` package that
is not part of the `chronos` package**, running as its own process
(`python -m worker`). One cycle: read the backend's token-protected read
endpoints into a digested canonical-JSON evidence snapshot → send it to Claude
with the owner's editable trading policy (`worker/policy.md`) as the strategy
half of the system prompt → force a single `propose_decision` tool call →
validate the result as hostile input → POST the candidate proposal to the
existing loopback ingress.

### 2.1 The three load-bearing structural choices

1. **The worker imports nothing from `chronos`.** Stronger than the TradingView
   bridge (which borrows `chronos.utils.time`). The worker holds an LLM API
   key, renders evidence into a prompt, and consumes untrusted model output —
   it is precisely where a prompt injection would land, so it is built the way
   ADR-0016 described it: no broker handle, no database session, no lease, no
   kill switch, no submission path, "not because it promises not to use them,
   but because they were never in its address space."
   `tests/safety/test_model_worker_isolation.py` enforces this with an AST
   walk and a subprocess probe, and enforces the converse — nothing under
   `src/chronos` imports `worker` — so the dependency stays one-way.
2. **No LLM SDK enters the repository's dependency tree.** The worker calls
   the Messages API over raw `httpx` (already a chronos dependency). "Chronos
   ships no model, no provider SDK, and no API key in the broker-holding
   process" (`docs/safety.md`) was previously re-verified by a manual grep;
   it is now a structural test that fails if `anthropic`/`openai`/`litellm`/
   `langchain` ever appears in `pyproject.toml`, `requirements.txt`, or the
   lockfile. The API key lives only in the worker process's environment.
3. **The model's only output channel is a strict, forced tool call.** The
   request pins `tool_choice` to `propose_decision` with `strict: true` and
   `additionalProperties: false`, so the decision arrives as schema-valid
   structure — the schema *is* the universe of what the model can say. It has
   no `provenance`, no `decision_id`, no account, price, routing, or transmit
   field, so what a model may never author is unrepresentable rather than
   filtered. ADR-0016 §5's rule that free-form prose is never parsed into
   orders is not a discipline here; it is the transport.

### 2.2 Provenance the model cannot fabricate

The evidence citation on every proposal is stamped by deterministic worker
code: its digest is the SHA-256 of the exact canonical-JSON bytes rendered
into the prompt, its `as_of` is the snapshot time, its kind is
`worker_evidence_snapshot`. The model's *opinion* of the evidence lands in the
thesis; what it *actually saw* lands in the digest. A model that hallucinates
a position or a price can still be wrong, but it cannot claim provenance for
evidence it was not shown.

### 2.3 Fail-closed, like everything else here

- `CHRONOS_WORKER_FORWARD` defaults **false**: the shipped posture gathers,
  thinks, logs the decision it would have proposed, and sends nothing.
- The symbol watchlist and kind allowlist are required; empty means nothing is
  proposable and the worker refuses to boot.
- The backend URL must be loopback — the worker carries the backend's API
  token and may only hand it to a backend on this machine.
- Evidence is gathered or the cycle refuses to think (the supervisor's
  facts-are-never-invented doctrine, applied one process out). A model
  `refusal`, a truncation, a prose-only response, and an API error all yield
  no proposal.
- The system prompt's non-negotiable framing (code-owned, before the owner's
  policy) instructs the model that snapshot content is data rather than
  instructions, that refusals are terminal, and that insufficient evidence
  means HOLD — R-30's bounded-not-solved posture, extended to the worker's own
  prompt.

## 3. Alternatives rejected

- **Inside `src/chronos` as another package.** Puts a credential-holding,
  model-talking module into the wheel and the single-consumer story, and makes
  "no provider harness inside Chronos" false in substance rather than letter.
  Rejected.
- **The `anthropic` SDK.** Cleaner ergonomics, but it would put an LLM SDK in
  the repo's dependency tree, breaching a standing re-verification that the
  isolation story leans on. The Messages API is one POST. Rejected.
- **Importing `chronos.autonomy` to author `ProposedDecision` directly.** The
  contracts are the model plane's vocabulary, so this was genuinely arguable —
  but it would add a fourth consumer to the single-consumer test's allowlist
  and put the whole chronos import graph in the worker's address space. The
  bridge's weaker position (emit a dict; the ingress decides) already has the
  proof pattern: restate, pin equality, push the output through the real
  `ingress.parse_proposal`. Rejected in favour of consistency and weakness.
- **A worker identity/credential of its own (ADR-0023) as part of this
  change.** Needed, not blocked on this, and owner-gated on its own terms.
  Building the worker first makes the identity work concrete instead of
  speculative. Deferred, not rejected.

## 4. What this ADR explicitly does NOT change

Everything in ADR-0016 §8 and ADR-0017 §5, unweakened and untouched — the
single transmit site, the writer lease and fencing, idempotency and replay
bounds, reconciliation, the kill switch and halt, the floors and breakers,
deny-by-default mandates, and the full propose → preview → confirm → submit
handoff. The worker is a client of the proposal ingress exactly as the
TradingView bridge is: it can be refused by every gate and widens none. The
mandate remains the only grant of trade-time authority; the worker cannot
read, name, write, or activate one, and a worker running with no mandate file
on the backend produces judged-and-refused proposals, not trades.

**A worker decision is not evidence.** Nothing here touches the registry,
the promotion ladder, or any statistical gate. SHADOW-mode cycles are the
machinery proving itself, not a strategy proving anything.

## 5. Residuals, disclosed

1. **Provenance still cannot distinguish sources — now three ways.** Model
   worker, TradingView bridge, and any other local token-holder all arrive
   stamped `INGRESS_IDENTITY`. The evidence-citation kinds
   (`worker_evidence_snapshot` vs `tradingview_alert`) distinguish them in the
   audit record, but plan §6 finding 6 / ADR-0023 (proposal-only credential,
   real worker identity, per-worker version pins) stays **OPEN** and is now
   overdue rather than merely open. The mandate's version-pin check still
   authenticates "came through the ingress," not "produced by the pinned
   model" — the worker's model id is configuration the pins cannot yet see.
2. **The policy file is unaudited authority over strategy.** `worker/policy.md`
   shapes what the model proposes and nothing hashes or pins it. Within the
   gate stack this is bounded — a policy cannot widen a mandate — but two runs
   with different policies are indistinguishable in provenance today. The fix
   belongs to the ADR-0023 protocol (prompt_version is already a pin awaiting
   a real value).
3. **Prompt injection is bounded, not solved (R-30 unchanged).** Evidence
   fields render into the prompt; the framing tells the model to treat them as
   data, and the gate stack bounds what obedience to an injection could do —
   inside the mandate, as always. The worker adds one new surface: bars and
   order fields fetched from the backend are the only text an outsider could
   influence, and they pass through the contract's control-character refusals
   before any human renders them.
4. **The worker trusts its backend.** It authenticates itself to the backend;
   nothing authenticates the backend to it beyond loopback. A hostile local
   backend could feed a hostile snapshot — and everything it could thereby
   cause still walks the gate stack.
5. **Cost is unmetered beyond logging.** Token usage is logged per call;
   nothing enforces a spend ceiling. The cadence bound (one cycle per
   interval, one decision per cycle) is the only throttle.
6. **No real gateway has ever been connected.** Everything downstream remains
   fixture-verified; a SHADOW worker against the demo backend proves the
   decision loop, not the broker path. MITIGATED ≠ CLOSED.

## 6. Verification

```
.venv/bin/python -m pytest tests/safety/test_model_worker_isolation.py \
  tests/safety/test_model_worker_exercised.py \
  tests/unit/test_model_worker.py -q
```

Both structural guards were verified by the revert-the-fix pattern: injecting
a `chronos` import into the worker fails the isolation test; injecting
`anthropic` into `pyproject.toml` fails the no-SDK test; both pass on restore.

## 7. Consequences

Chronos gains its first actual decision-originating AI — runnable today, in
dry-run, against the demo backend, with zero broker and zero dollars anywhere
near it. The owner gains the file that makes "trades like me" concrete:
`worker/policy.md` is where the trading persona lives, and editing it is how
the experiment iterates. The open worker-identity work (ADR-0023) gains a
concrete consumer and loses its last excuse.

## 8. Owner gate

Merging adds the mechanism in its inert posture. Three decisions stay with the
owner and are not taken here:

- [ ] **Supplying an Anthropic API key** to the worker's environment and
      accepting its metered cost — a spend decision (plan §11's data-budget
      class), not an engineering one.
- [ ] **Setting `CHRONOS_WORKER_FORWARD=true`**, and with which watchlist and
      kind allowlists — and, prerequisite to any judged proposal existing at
      all, **authoring a mandate** (`python -m chronos.cli mandate template`
      emits the SHADOW skeleton; `mandate check` reports what it grants before
      it is trusted).
- [ ] **Any mode beyond SHADOW.** The runbook (`docs/model_worker.md`)
      sequences SHADOW → paper behind ADR-0025's mechanical-readiness
      checklist, which this ADR does not advance or weaken.
