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
evidence snapshot, asks Claude for exactly one decision through a forced,
strict tool call, validates the result as hostile input, and — only if you
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
loopback enforced), `CHRONOS_WORKER_MODEL` (`claude-opus-5`),
`CHRONOS_WORKER_POLICY_FILE` (`worker/policy.md`),
`CHRONOS_WORKER_INTERVAL_SECONDS` (`300`), `CHRONOS_WORKER_LOOKBACK_DAYS`
(`30`), `CHRONOS_WORKER_FORWARD` (`false`).

The worker refuses to start on a missing key, missing token, empty watchlist,
empty kind allowlist, unreadable policy, or non-loopback backend URL — an
allow-nothing worker beats an allow-everything one.

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

`NO_EVIDENCE` (a backend read failed — the worker never thinks on partial
facts) · `NO_DECISION` (the model refused, truncated, or answered in prose) ·
`REFUSED_LOCALLY` (the decision was incoherent — the log names the rule) ·
`DRY_RUN` (translated cleanly, not sent) · `INGRESS_REFUSED` ·
`FORWARDED` (queued; "queued is received, not authorized").

## Before you enable it

Three things are yours (ADR-0027 §8): the API key and its metered cost;
`CHRONOS_WORKER_FORWARD=true` plus the allowlists and mandate that give a
forwarded proposal somewhere to be judged; and any mode beyond SHADOW, which
stays behind the ADR-0025 mechanical-readiness checklist (funding, typed loss
limits, the read-only gateway campaign, paper floor, kill drill).

Two honest limits worth knowing (full list in ADR-0027 §5): ~~provenance cannot
yet tell the worker from the TradingView bridge from any other local
token-holder — the evidence-citation kind is the distinguishing mark until the
ADR-0023 worker-identity protocol lands~~ *(corrected 2026-08-12: ADR-0023
landed — register the worker and its proposals are stamped with its own
identity; see the section below)*; and nothing pins the policy file's *content*
yet — the registration's `prompt_version` is an owner-typed label you should
bump on each policy edit, and recording edits in git is still what makes
experiments attributable.

## Registering the worker (ADR-0023)

With the backend's `AUTONOMY_PROPOSERS_FILE` unset, the worker authenticates
with the local API token and nothing here applies. Once the owner configures a
proposer registry, the proposal route refuses the general token and the worker
must present its own registered credential:

1. Mint one: `python -m chronos.cli proposer mint --proposer-id claude-worker
   --provider anthropic --model-id claude-opus-5 --expires-days 90`. The
   credential prints exactly once; the registration entry holds only its
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

## Stopping it

Stop the worker and new AI proposals stop; **Chronos does not**. The worker is
a decision source, not a control surface — standing the system down is the
kill switch, the platform halt, and mandate revocation, sequenced in
`docs/INCIDENT_RESPONSE.md`.
