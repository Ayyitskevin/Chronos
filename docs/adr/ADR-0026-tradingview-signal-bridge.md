# ADR-0026 — The TradingView signal bridge

Status: proposed (owner directive, 2026-08-12; **owner gate open — see §8**)

Index entry: DECISIONS.md **D-22**

Supersedes: nothing. Explicitly does **not** supersede ADR-0016, ADR-0017,
ADR-0023, or any part of either.

## 1. Context

ADR-0016 §3 put the decision-originating worker outside the broker-writing
process and inverted the call: Chronos makes no outbound call to a model; the
worker calls *in*, over `POST /autonomy/proposals`, and
`chronos.supervisor.ingress` trusts nothing it receives.

That transport was designed for one kind of caller — an external model worker.
The owner runs strategies on TradingView, whose alerts are already the trigger
for a large part of how the owner actually trades. ADR-0025/D-21 records the
owner's direction that Chronos should implement the owner's investment
strategies; a TradingView alert is one of the concrete forms those strategies
already take, and TradingView's webhook is the only mechanism TradingView
offers for exporting one.

The question this ADR answers is therefore narrow: **may a TradingView alert
become a Chronos proposal, and if so, through what?**

## 2. Decision

Yes, through a **separate, unprivileged bridge process** — `chronos.bridge` —
that holds no Chronos capability and grants no authority. It listens for a
TradingView webhook, authenticates it against an owner-configured shared secret,
translates it into candidate proposal JSON, and POSTs that to the existing
loopback ingress with the existing local API token.

The bridge is a **client of the proposal ingress, not a new door into it**. The
route it calls, the parser that judges what it sends, and every gate downstream
are unchanged. A TradingView-sourced proposal walks the same fifteen admission
checks, the same sizing against the mandate's ceilings and floors, the same
capability-matrix compiler, and the same propose → preview → confirm → submit
handoff as anything else. It can be refused by all of them and widened by none.

### 2.1 The load-bearing structural choice

The bridge **does not import `chronos.autonomy`.**

It could have been added to the single-consumer allowlist in
`tests/safety/test_autonomy_contracts.py` beside `api/autonomy_wiring.py`. It
was not, and the reason is the whole design: a bridge that constructed a
`ProposedDecision` would be a second place where "what a valid decision is" gets
decided, and the ingress exists precisely so that there is exactly one such
place. The bridge emits a `dict`; the ingress decides whether that dict is a
proposal.

The price is duplication — `chronos.bridge.vocabulary` restates the decision
enums, and `chronos.bridge.translate` restates the contract's payload-coherence
rules. That price is paid deliberately and guarded structurally, because
undetected duplication is exactly the inert-control shape this repository was
burned by four times (R-24..R-27):

- `tests/safety/test_tradingview_bridge_isolation.py` asserts every restated set
  is **equal** to the real enum or frozenset. Adding a `DecisionKind`, renaming
  a `StrategyForm`, or reclassifying a kind as exposure-creating fails the
  safety suite until the bridge is updated with it.
- `tests/safety/test_tradingview_bridge_exercised.py` pushes the bridge's own
  output through the real `chronos.supervisor.ingress.parse_proposal` — the same
  function the backend route calls, on the same bytes. "The bridge emits
  something the ingress accepts" is therefore proven per decision kind, not
  asserted in prose.

### 2.2 What the bridge structurally cannot say

The decision contract has no account, broker order id, client id, exchange
routing, `transmit` flag, or order-type field, and it names no mandate. The
bridge inherits every one of those absences by construction: an alert that says
"buy at market" produces a *kind*, and the deterministic compiler still selects
the order form from what the mandate granted and derives the limit price from
Chronos's own quote. An alert cannot choose a price, a venue, an account, or the
authority it will be judged against.

Two further refusals are the bridge's own:

- **Equities only.** The autonomy wiring gathers instrument facts for equities
  and crypto and refuses options at that seam, so the bridge emits `EQUITY` and
  nothing else. Emitting a class the runtime cannot price would queue a proposal
  guaranteed to be refused later; an honest refusal at the edge beats a
  misleading acceptance.
- **No inert economic fields.** `exit_plan`, `protective_order_required`,
  `max_acceptable_loss_usd`, and `requested_risk_budget_usd` are the subject of
  ADR-0021 and do not mechanically affect execution. The bridge does not
  populate them. Filling an inert field is how an alert author comes to believe
  a protection exists that does not.

### 2.3 Fail-closed configuration

The bridge refuses to start rather than run with a gap: no secret (minimum 32
characters), no API token, an empty symbol allowlist, an empty kind allowlist, an
unknown kind, or a non-loopback ingress URL are each a refusal to boot.

`CHRONOS_TV_BRIDGE_FORWARD` **defaults to false**. In the shipped posture the
bridge authenticates, parses, translates, and logs — and sends nothing. That is
the same shape as `AUTONOMY_MANDATE_FILE` being unset meaning "autonomy is
inert": the grant is a separate, deliberate owner act, and the owner is expected
to run the bridge in dry run first and read what it *would* have proposed.

The ingress URL is checked to be loopback because the bridge POSTs carrying the
backend's own API token. Without that check a misconfiguration turns the bridge
into a relay that hands an owner-authenticated trading intent to whatever host an
environment variable names.

### 2.4 The refusals the transport adds

Applied in this order, before anything is forwarded: rate limit (default 10/min);
size and structure, parsed as though the sender were hostile; the shared secret,
in constant time, before any other field is judged; freshness against the alert's
own `{{timenow}}`; replay refusal on `alert_id` inside a bounded window; then
translation.

No refusal message — at any layer — quotes a value the sender supplied, not even
one drawn from Chronos's own closed vocabulary. The response is written into
TradingView's alert log, outside the trust boundary. The strict invariant was
chosen over "echo only the safe subset" because the strict one is checkable in a
line and the loose one needs a judgement call at every new message. The
specifics an owner needs while debugging go to the bridge's own log instead.

## 3. Alternatives rejected

- **A route on the backend.** Exposing `/tradingview/webhook` from the process
  that holds the broker connection would put an internet-facing listener inside
  the address space the whole architecture works to keep small, and would break
  the loopback-only property `POST /autonomy/proposals` relies on for its
  transport-level assurance. Rejected.
- **Widening the ingress to accept an alert-shaped payload.** This would put
  translation inside the broker-writing process and make the ingress responsible
  for two schemas. The ingress's guarantee is valuable because it is narrow.
  Rejected.
- **Adding the bridge to the contracts allowlist and constructing a real
  `ProposedDecision`.** Cleaner-looking, and wrong: see §2.1. Rejected.
- **Trusting TradingView source IPs instead of a secret.** The owner will reach
  the bridge through a tunnel or reverse proxy in every realistic deployment, so
  the observed source address is the tunnel's, not TradingView's. An IP
  allowlist would have been assurance theatre. Rejected in favour of a mandatory
  shared secret.

## 4. What this ADR explicitly does NOT change

Everything in ADR-0016 §8 and ADR-0017 §5 stands, unweakened and untouched: one
canonical transmission boundary and its single-transmit-site test; the
single-writer lease and the fencing re-check adjacent to the wire; durable
idempotency and replay protection; reconciliation to broker truth; account and
contract qualification; stale-data refusal; the durable kill switch and halt; the
session and rolling-drawdown breakers; the cash and buying-power floors and the
reserve they protect; order and cancellation rate limits; restart recovery and
orphan handling; immutable hash-chained audit; deny-by-default for scope, order
forms, strategies, and data qualities; the degraded-state rule; and the
prohibition against broker mutations from tests or CI.

The mandate remains the only grant of trade-time authority, and the bridge cannot
read, name, write, widen, or activate one. Session arming on a LIVE-environment
backend is unaffected — the arming-versus-mandate question (plan §6 finding 4,
ADR-0022) is untouched by this ADR in either direction.

**An alert is not evidence.** Nothing here creates a promotion rung, and a
TradingView alert firing is not a research trial, a backtest, or an input to any
statistical gate. ADR-0013's registry and ADR-0024's promotion evidence are
unaffected.

## 5. Residuals, disclosed

1. **Provenance still cannot distinguish the sources.** Every proposal reaching
   the ingress is stamped with the static `INGRESS_IDENTITY`
   (`provider="external-worker"`, `model_id="ingress"`), so a TradingView-sourced
   decision's *provenance* is byte-identical to a model worker's. The bridge
   compensates where it can — it writes an evidence citation with
   `kind="tradingview_alert"` whose digest is over the exact alert text with the
   secret removed, so the audit chain records which alert produced which decision
   and the owner can recompute it — but plan §6 finding 6 and ADR-0023's
   worker-identity protocol stay **open**, and this ADR makes them more acute
   rather than less. A second proposal source is a reason to finish that work,
   not evidence that it is unnecessary.
2. **The credential is not proposal-only.** The bridge presents the same local
   API token every mutating route accepts. Narrowing it is the same open work as
   residual 1.
3. **Anyone with the URL and the secret can propose.** That is the threat model
   of a webhook rather than a defect introduced here, and it is why the secret is
   mandatory, forwarding is off by default, and both allowlists are required. The
   mandate's scope remains the real bound on what a successful forgery could
   achieve.
4. **Publishing the listener is an owner act with its own risk.** The bridge
   binds loopback. Reaching TradingView requires a tunnel or reverse proxy the
   owner runs, terminates TLS for, and is responsible for. Nothing in this
   repository manages that, and this ADR does not authorize it.
5. **The rate limit and replay cache are per process and in memory.** A restart
   forgets both. The durable defences downstream — the economic-content
   `decision_id` and `MAX_RESUBMISSIONS` — are unaffected and remain the
   authoritative replay bound.
6. **No real gateway has ever been connected.** As with every other
   adapter-adjacent control in this repository, everything downstream of the
   bridge is fixture-verified only. MITIGATED ≠ CLOSED.

## 6. Verification

```
.venv/bin/python -m pytest tests/safety/test_tradingview_bridge_isolation.py \
  tests/safety/test_tradingview_bridge_exercised.py \
  tests/unit/test_tradingview_bridge.py -q
```

The isolation suite additionally asserts that nothing under `src/chronos`
imports `chronos.bridge`: the dependency is one-way, and the direction is the
safety property.

## 7. Consequences

The owner gains a mechanical path from a TradingView alert to a Chronos
proposal, with a dry-run posture that makes the path inspectable before it is
live. Chronos gains a second proposal source and, with it, a sharper obligation
to finish worker identity (residual 1). The gate stack gains nothing to
maintain, because it was not touched.

## 8. Owner gate

Per `chronos-change-control` §1, adding a network channel is an
**owner-only + new ADR** change: an agent may build the proposal; the owner
decides. This ADR is that proposal. The code is merged in the inert posture and
grants nothing on merge.

Three decisions remain the owner's and are not taken here:

- [ ] Whether to publish the listener beyond loopback at all, and by what means.
- [ ] Whether to set `CHRONOS_TV_BRIDGE_FORWARD=true`, and with which symbol and
      kind allowlists.
- [ ] Whether TradingView alerts are an appropriate proposal source for this
      account under the ADR-0025 readiness checklist — which is a strategy
      judgement, not an engineering one.

Accepting this ADR by merging it authorizes the mechanism in its inert posture.
It does not authorize the trading.
