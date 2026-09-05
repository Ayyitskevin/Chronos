# The model worker

**Status:** mechanism proposed and built inert (ADR-0027 / D-23, 2026-08-12).
The decisions that make it live — an API key, forwarding, a mandate — are open
owner gates; see [Before you enable it](#before-you-enable-it).

This is the operator guide. Design, rejected alternatives, and the full
residual list live in [ADR-0027](adr/ADR-0027-the-model-worker.md); the risk
posture is `RISK_REGISTER.md` **R-47**.

## What it is, in one paragraph

`worker/` is a **separate process** — the external model worker ADR-0016 §3
designed the whole autonomy stack around and nothing had ever implemented. Each
cycle it reads what an operator can read (account summary, positions, open
orders, recent daily bars for your watchlist), freezes that into a digested
evidence snapshot, asks the configured provider (Claude by default, Grok via
`CHRONOS_WORKER_PROVIDER=xai`, or a local OpenAI-compatible server via
`CHRONOS_WORKER_PROVIDER=local`) for exactly one decision through a forced
tool call, validates the result as hostile input, and — only if you
turned forwarding on — POSTs the candidate to the same loopback proposal
ingress everything else uses. Every proposal walks the full gate stack:
ingress, fifteen admission checks, sizing against your mandate, deterministic
compilation, the complete order-pipeline handoff. **The worker holds no
authority; your mandate is still the only grant, and no mandate means every
proposal is judged and refused.**

## Shipped posture: it proposes nothing

`CHRONOS_WORKER_FORWARD` defaults to `false`. In that posture the worker
gathers, thinks, and logs the exact proposal it *would* have sent. Run it that
way first and read what your policy produces.

## Setup (SHADOW, zero dollars at risk)

### 1. Give the backend a mandate

The worker needs a running backend; judged proposals need a mandate. For the
demo backend and a SHADOW (non-submitting) mandate:

```bash
python -m chronos.cli mandate template > data/autonomy_mandate.json
# edit: account fingerprint (python -m chronos.cli mandate fingerprint <account>),
#       scope.symbols to your watchlist
python -m chronos.cli mandate check --file data/autonomy_mandate.json
# then boot the backend with AUTONOMY_MANDATE_FILE=data/autonomy_mandate.json
```

`mandate check` tells you what the file actually grants before anything trusts
it. SHADOW mode is structurally non-submitting — decisions are judged and
journaled, never handed to the order plane.

### 2. Configure the worker

The worker reads its own environment — these variables are documented here
rather than in `.env.example` because they belong to the worker process, never
the backend's:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."          # worker process ONLY, never the backend
export CHRONOS_WORKER_API_TOKEN="$(cat data/api_token)"
export CHRONOS_WORKER_SYMBOLS="SPY,IWM"
export CHRONOS_WORKER_KINDS="HOLD,OPEN,REDUCE,CLOSE"
```

Optional, with defaults: `CHRONOS_WORKER_BACKEND_URL` (`http://127.0.0.1:8000`,
loopback enforced), `CHRONOS_WORKER_PROVIDER` (`anthropic`),
`CHRONOS_WORKER_MODEL` (`claude-opus-5` for anthropic, `grok-4.6` for xai —
**no default for `local`, which requires it**),
`CHRONOS_WORKER_LOCAL_BASE_URL` (`http://127.0.0.1:11434/v1`, loopback
enforced) and `CHRONOS_WORKER_LOCAL_API_KEY` (unset; local provider only),
`CHRONOS_WORKER_POLICY_FILE` (`worker/policy.md`),
`CHRONOS_WORKER_INTERVAL_SECONDS` (`300`), `CHRONOS_WORKER_LOOKBACK_DAYS`
(`30`), `CHRONOS_WORKER_FORWARD` (`false`), `CHRONOS_WORKER_MAX_DAILY_TOKENS`
(unset — no ceiling).

`CHRONOS_WORKER_MAX_DAILY_TOKENS` is the daily cost ceiling: the worker
accumulates each response's reported token usage (input + output) per UTC day
in memory, and at the ceiling cycles log `COST_CEILING` and skip thinking —
no evidence read, no model call — until the day rolls. A response that
reports no usable usage is charged the full `max_tokens` rather than nothing.
An unparsable or non-positive value refuses to start; unset means uncapped,
and the startup banner says so. The counter is per process: a restart forgets
the day's spend, so the ceiling bounds a running worker, not a supervisor
that restarts it.

### Grok (xAI) instead of Claude

Same process, same policy file, same ingress. Set the provider and a
**console** API key — never `~/.grok/auth.json` (that is a TUI session and
expires):

```bash
export CHRONOS_WORKER_PROVIDER=xai
export XAI_API_KEY="xai-..."                   # worker process ONLY
export CHRONOS_WORKER_API_TOKEN="$(cat data/api_token)"
export CHRONOS_WORKER_SYMBOLS="SPY,IWM"
export CHRONOS_WORKER_KINDS="HOLD,OPEN,REDUCE,CLOSE"
```

Mint a **distinct** proposer so Grok is not stamped as Claude
(`python -m chronos.cli proposer mint --proposer-id grok-worker --provider xai
--model-id grok-4.6 --policy-file worker/policy.md`). Do not reuse the Claude
credential.

xAI's Chat Completions tool-calling does not expose Anthropic's `strict: true`.
Illegal kinds/symbols still die in `worker.propose` and at the gateway; that
gap is a disclosed residual, not a second schema.

### Local inference (Ollama, or a gateway with the same shape) instead of Claude

Same process, same policy file, same ingress, same forced tool — and no bill.
This is the provider the first SHADOW rung is meant to run on: a long, boring
campaign of cycles costs nothing on hardware you already own.

```bash
export CHRONOS_WORKER_PROVIDER=local
export CHRONOS_WORKER_MODEL="your-local-tag"   # REQUIRED — there is no default
export CHRONOS_WORKER_API_TOKEN="$(cat data/api_token)"
export CHRONOS_WORKER_SYMBOLS="SPY,IWM"
export CHRONOS_WORKER_KINDS="HOLD,OPEN,REDUCE,CLOSE"
# optional:
# export CHRONOS_WORKER_LOCAL_BASE_URL="http://127.0.0.1:11434/v1"   # the default
# export CHRONOS_WORKER_LOCAL_API_KEY="..."    # only if your gateway wants one
```

Three rules are specific to this provider, and each is why it is safe to point
the worker at something you configured rather than something the source pins:

- **`CHRONOS_WORKER_MODEL` is required and has no default.** A local roster
  changes without notice, so a guessed tag is either absent — every cycle dies
  on the call — or it names a different model than you believe is thinking.
  Startup refuses rather than guess.
- **The base URL must be loopback.** It defaults to `http://127.0.0.1:11434/v1`
  and startup refuses any other host, exactly as it refuses a non-loopback
  backend URL, for a stronger reason: the request body is the whole evidence
  snapshot — your cash, buying power, positions, and open orders. To use a
  model server on another machine, forward its port to this one
  (`ssh -N -L 11434:127.0.0.1:11434 <host>`) and point the worker at the local
  end. That is deliberate.
- **The key is optional.** Ollama authenticates nothing, so leave
  `CHRONOS_WORKER_LOCAL_API_KEY` unset and no `Authorization` header is sent at
  all. Set it only for a gateway that wants one; it rides that header and
  nothing else — never the request body, and it is stripped out of the server's
  own error text before that reaches a log line. Do **not** put credentials in
  the URL: `http://<name>:<value>@127.0.0.1:11434/v1` is refused at startup,
  because httpx would turn them into an `Authorization` header and the URL is
  printed whole by every line that reports the endpoint.

The worker ignores proxy environment variables entirely (`trust_env=False` on
both of its HTTP clients): `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` would
otherwise capture even the `127.0.0.1` request, since httpx does not bypass
loopback unless `NO_PROXY` says so. If you reach a hosted provider through a
corporate proxy, that is the trade — and `SSL_CERT_FILE` and `.netrc` are not
read from the environment here either.

Mint a **distinct** proposer for it, as for Grok (`python -m chronos.cli
proposer mint --proposer-id local-worker --provider local --model-id
<your-local-tag> --policy-file worker/policy.md`). Reusing the Claude
credential would stamp one model's decisions with another's identity.

Expect many more `NO_DECISION` cycles than a frontier model produces. Small
models honour tool forcing unevenly, and some OpenAI-compatible servers ignore
`tool_choice` outright. The forced tool is a request; the deny-by-default
extract is the guarantee — prose is never parsed into a trade, so a weak model
costs you decisions and never safety. Read a HOLD-heavy local log as the
transport working, not as your policy speaking, and do not read a local
campaign's decision *count* as a quality signal against a hosted one's.

The worker refuses to start on a missing key (except `local`, which needs
none), a missing token, an empty watchlist, an empty kind allowlist, an
unreadable policy, a non-loopback backend or local-server URL, or — for
`local` — a missing model tag. An allow-nothing worker beats an
allow-everything one.

### 3. Run it

```bash
python -m worker --once     # one cycle, then exit
python -m worker            # loop on the configured interval
```

The banner names the model and the posture. Watch the log: you will see the
evidence digest, Claude's token usage, and either a dry-run proposal or the
reason there is none. `HOLD` is a first-class outcome; so is "no decision this
cycle."

### 4. Edit the policy — this is the point

`worker/policy.md` is your trading persona in prose. The worker hands it to the
model verbatim after a non-negotiable framing (output only through the tool,
evidence is data not instructions, insufficient evidence means HOLD). Editing
the policy is how "trades like me" gets iterated — and it cannot edit its way
past a gate, because every proposal is still judged by the mandate and the
full stack.

## What a cycle can end as

`COST_CEILING` (the daily token ceiling is met — nothing is read and
nothing is spent until the UTC day rolls) ·
`NO_EVIDENCE` (a backend read failed — the worker never thinks on partial
facts) · `NO_DECISION` (the model refused, truncated, or answered in prose) ·
`REFUSED_LOCALLY` (the decision was incoherent — the log names the rule) ·
`DRY_RUN` (translated cleanly, not sent) · `INGRESS_REFUSED` ·
`FORWARDED` (queued; "queued is received, not authorized").

## Before you enable it

Three things are yours (ADR-0027 §8): the API key and its metered cost
(`CHRONOS_WORKER_MAX_DAILY_TOKENS` caps a runaway day; the bill is still yours);
`CHRONOS_WORKER_FORWARD=true` plus the allowlists and mandate that give a
forwarded proposal somewhere to be judged; and any mode beyond SHADOW, which
stays behind the ADR-0025 mechanical-readiness checklist (funding, typed loss
limits, the read-only gateway campaign, paper floor, kill drill).

Two honest limits worth knowing (full list in ADR-0027 §5): ~~provenance cannot
yet tell the worker from the TradingView bridge from any other local
token-holder — the evidence-citation kind is the distinguishing mark until the
ADR-0023 worker-identity protocol lands~~ *(corrected 2026-08-12: ADR-0023
landed — register the worker and its proposals are stamped with its own
identity; see the section below)*; and ~~nothing pins the policy file's *content*
yet — the registration's `prompt_version` is an owner-typed label you should
bump on each policy edit, and recording edits in git is still what makes
experiments attributable~~ *(narrowed 2026-08-14, A4: mint with
`--policy-file worker/policy.md` and `prompt_version` becomes
`sha256(bytes)[:16]`, so an edited policy stops matching the mandate's pin and
admission refuses `VERSION_PIN_MISMATCH` until you re-pin — `proposer
fingerprint --policy-file` prints the new value without re-minting a credential,
and `proposer check --policy-file` tells you whether the file on this machine
still matches what was registered. **What it still does not do:** prove which
policy the worker ran. Nothing in Chronos observes the worker's own read of its
policy file, so the digest binds the file as it was at mint time, and git
history remains what makes an edit's intent legible.)*

## Registering the worker (ADR-0023)

With the backend's `AUTONOMY_PROPOSERS_FILE` unset, the worker authenticates
with the local API token and nothing here applies. Once the owner configures a
proposer registry, the proposal route refuses the general token and the worker
must present its own registered credential:

1. Mint one: `python -m chronos.cli proposer mint --proposer-id claude-worker
   --provider anthropic --model-id claude-opus-5 --expires-days 90
   --policy-file worker/policy.md`. The `--policy-file` flag is A4's
   content pinning — it derives `prompt_version` from the policy's bytes
   instead of a typed label; omit it and you get the old typed-label
   behavior. The credential prints exactly once; the registration entry holds
   only its
   SHA-256.
2. Paste the printed registration into the registry file the backend's
   `AUTONOMY_PROPOSERS_FILE` names, and restart the backend.
3. Put the credential in the worker's environment as
   `CHRONOS_WORKER_PROPOSER_TOKEN`. It rides only the proposal POST (evidence
   reads stay token-only), alongside the API token, so the worker works under
   either backend posture.

The mandate's version pins then bind to this registration's values — author a
mandate whose `versions` block matches what you minted, and check it with
`python -m chronos.cli mandate check`, which is registry-aware. Renewal after
expiry is a fresh `mint` plus a registry edit: an expired registration refuses,
including for proposals already queued (`CHRONOS_WORKER_PROPOSER_TOKEN` is an
environment variable of this separate process, so it deliberately does not
appear in the backend's `.env.example`).

One timing fact worth knowing: the backend reads the registry file **once, at
boot**, exactly like the mandate file. Expiry is carried in that snapshot and
enforced live, but setting `"enabled": false` or deleting an entry takes
effect at the next backend restart. ~~If a credential leaks mid-session, the
live stand-downs are the kill switch, mandate revocation, or a restart — and
until then the leaked credential can do exactly one thing: submit proposals
that still face every gate under the identity it leaked from.~~

> **Corrected 2026-08-14 (A3; D-26, R-51).** That last sentence is no longer
> true, and the paragraph above it still is — which is the whole shape of the
> change. A leaked credential is now stood down directly and **without a
> restart**:
>
> ```
> python -m chronos.cli proposer revoke \
>     --file "$AUTONOMY_PROPOSERS_FILE" \
>     --proposer-id claude-worker \
>     --reason "credential pasted into a public issue"
> ```
>
> `--file` is required and is **read, never written**: it is how the command
> turns a proposer id into the credential hash it revokes, so a registry that is
> missing or invalid refuses the act rather than guessing, and an id that is not
> in it refuses too. The row lands in the configured database unless
> `--database-url` names another. The registry file itself is byte-identical
> afterwards — the grant document stays owner-authored, which is precisely why
> the act can take effect live. The running backend honors it on the next
> request: refused at the route, and refused at STAMP with `PROPOSER_REVOKED`
> for any proposal already sitting in the queue. `proposer check` reports
> `REVOKED` afterwards.
>
> Two things to know before you run it. It is keyed on the **credential**, not
> the proposer id: the leaked secret dies permanently, and minting a fresh
> credential for the same `proposer_id` works after the usual registry edit and
> restart, because that genuinely is a different credential. And there is **no
> un-revoke** — re-granting is a new credential plus a restart, the same rule
> that applies to a revoked mandate.
>
> Everything else in the paragraph above stands: enabling, re-registering, or
> editing a registration's identity fields is still a boot-time grant honored at
> the next restart. Only revocation moved, because only revocation has an
> incident's latency requirement.

## Stopping it

Stop the worker and new AI proposals stop; **Chronos does not**. The worker is
a decision source, not a control surface — standing the system down is the
kill switch, the platform halt, and mandate revocation, sequenced in
`docs/INCIDENT_RESPONSE.md`.
