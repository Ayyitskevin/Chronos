# ADR-0050 — The worker may think on a local model, loopback-only and with no default tag

Status: **accepted design direction, 2026-09-04 — owner review required before merge; the
owner's merge is the acceptance act (matching ADR-0048's posture). SHADOW only: this adds a
decision *source*, grants no trading capability, and `CHRONOS_WORKER_FORWARD` still defaults
false.** Index entry: DECISIONS.md D-65. Risk entry: RISK_REGISTER.md R-68.

## Context

The worker (ADR-0027 / D-23) can think through Anthropic or, since D-28, xAI. Both are
metered. The first rung the mission actually needs — a long SHADOW campaign accumulating
decision, receipt, scheduler, and alert evidence with nothing submitting — is a long run of
cheap, boring cycles. Priced providers make the cheapest rung the one with a bill attached,
and a bill attached to an evidence campaign is a standing reason to cut the campaign short.

The machines this runs on already host OpenAI-compatible inference on loopback. That is the
whole opportunity: the same forced-tool transport, pointed at `127.0.0.1`, costs nothing and
leaves the process boundary exactly where ADR-0016 §3 put it.

## Decision

`CHRONOS_WORKER_PROVIDER=local` selects `worker/model_local.py` — a raw-httpx
OpenAI-compatible Chat Completions transport that forces `propose_decision`, mirroring
`worker/model_xai.py`. Three rules distinguish it from a hosted provider, and all three exist
because a local server is *configured* where Anthropic and xAI are pinned constants:

1. **The configured base URL must name a loopback host, carry no credentials, and use
   http(s).** `CHRONOS_WORKER_LOCAL_BASE_URL` defaults to `http://127.0.0.1:11434/v1`, and
   `load_config` refuses any other host, a non-http(s) scheme, and URL userinfo — the same
   shared checker `CHRONOS_WORKER_BACKEND_URL` uses, so the backend URL gains the userinfo
   refusal too. The request body is the entire evidence snapshot: the account's cash, buying
   power, positions, and open orders. The hosted providers cannot be redirected at all,
   because their URLs are source constants; the moment a model endpoint becomes
   configuration, a typo or a copied environment line is an exfiltration path.

   **What that check does and does not establish.** It is a check on the *configured string*,
   nothing more. It does not establish what is listening on that port — an operator who runs
   a forwarding proxy on `127.0.0.1` has consented to wherever it forwards, and reaching a
   model server on another host through a local port-forward is the intended way to do it,
   so the two are indistinguishable here by design. Two things that were *outside* the
   string check and are now closed in code rather than merely disclosed: an inherited
   `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` (see decision 4), and userinfo in the URL, which
   httpx converts into an `Authorization` header and which every line that reports the
   endpoint would then print.

2. **The key is optional, and its value lives nowhere in this repository.** Ollama
   authenticates nothing, so an unset `CHRONOS_WORKER_LOCAL_API_KEY` sends no `Authorization`
   header at all rather than an empty bearer — a `Bearer ` with nothing after it is a
   credential-shaped lie in the listener's access log. A gateway that wants a key reads it
   from that variable; it is never defaulted in code and never logged.

3. **There is no default model.** `CHRONOS_WORKER_MODEL` is required for this provider and
   startup refuses without it. A local roster changes without notice, so a guessed tag is
   either absent — every cycle dies on the call — or it names a *different model than the
   operator believes is thinking*. A decision attributed to the wrong model is worse than no
   decision, and this is the one provider where the code could plausibly have guessed.

4. **The worker's environment cannot redirect it, and the server's text is not trusted.**
   Both clients in `worker/cycle.py` are built with `trust_env=False`. httpx honours the
   proxy variables by default and does *not* bypass `127.0.0.1` unless `NO_PROXY` says so, so
   without this an inherited proxy line would route the backend's API token and the whole
   evidence snapshot off-host while every string check above still passed — the copied
   environment line this decision names as its threat, arriving by a route the string check
   cannot see. The cost is real and deliberate: a hosted provider behind a corporate proxy,
   and environment-supplied TLS configuration (`SSL_CERT_FILE`) and `.netrc`, are no longer
   picked up here. Separately, `_error_summary` removes the configured key from the server's
   error text and caps its length before it reaches a log line, because a gateway that echoes
   the request's own `Authorization` header into its error body is ordinary behaviour for
   exactly this software. The residual is stated where it belongs: a listener that echoes a
   *transformed* key — base64, a hash — defeats a literal match, and the cap is what bounds
   that.

The forced `tool_choice` is a request; the deny-by-default extract is the guarantee. Only a
completed `propose_decision` call yields a candidate. Prose, a call under another tool name,
unparsable arguments, arguments that are not an object, a turn the server labelled truncated,
a non-200 whatever its body claims, a non-JSON body, a JSON body that is not an object, and
an unreachable server all yield `None`, and the cycle records `NO_DECISION`.

## Consequences

The SHADOW rung can run continuously at zero dollars on hardware already owned. Nothing else
moves: no new authority, no broker-process network channel, no live path, no change under
`src/chronos/orders` or `src/chronos/supervisor`, and forwarding still defaults off. A local
worker registered as a proposer must be minted as its own identity (`--provider local`,
`--model-id <tag>`); reusing the Claude or Grok credential would stamp one model's decisions
with another's.

Expect a materially higher refusal rate than a frontier model. Small models honour tool
forcing unevenly and some OpenAI-compatible servers ignore `tool_choice` outright. That costs
decisions, never safety — but it also means a local campaign's decision *count* is not a
quality signal, and a HOLD-heavy local log should be read as the transport working rather
than as the policy speaking.

Like xAI, this provider gets no `strict: true` equivalent. Illegal kinds and symbols still
die in `worker.propose` and again at the gateway; that gap is the same disclosed residual
ADR-0027 and D-28 already carry, not a second schema.

## Rejected alternatives

**Hoist the shared OpenAI-compatible extract into one module used by both `model_xai` and
`model_local`.** Tempting: a deny-by-default parser in two copies is two guards that can
drift apart. Rejected here because it would rewrite a *shipped* provider's live path inside a
PR whose subject is a new one, and its log lines name xAI throughout. The drift hazard is
closed instead by `test_the_two_openai_compatible_providers_refuse_the_same_bodies`, which
feeds both extracts the same canned responses and requires identical verdicts — every
extract mutation proved in this PR fails that test as well as its own. If a third
OpenAI-compatible provider arrives, hoist then, with the pin already in place to prove the
hoist changed nothing.

That choice has one visible cost, and it is recorded rather than hidden. A JSON body that
parses but is not an object (`[]`, `"str"`, `42`) has no `.get`, and raised `AttributeError`
out of `think` on **both** providers. This PR guards it in `think_local` only — deliberately
in `think`, not in `_extract_decision`, so the drift test still pins the two extracts equal.
The identical shape remains in `worker/model_xai.py`, where `run_loop` catches it and keeps
cadence, so it costs a cycle and a noisy log rather than authority. Fixing a shipped
provider's live path is not this PR's to do (R10); it is the first thing the hoist above
should carry.

**Let the local base URL name any host.** Rejected — see decision 1. The remote-gateway case
is served by a port-forward, and this fleet already reaches its loopback-bound services that
way.

**Give `local` a default model tag.** Rejected — see decision 3. Convenience here buys a
decision record that names the wrong model.

**Exempt a free provider from the daily token budget.** Rejected. `CHRONOS_WORKER_MAX_DAILY_TOKENS`
is unset by default, so charging costs a local operator nothing, and a provider exempt from
the one accounting path is a hole someone has to remember later.
