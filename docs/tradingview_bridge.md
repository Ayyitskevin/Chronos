# The TradingView signal bridge

**Status:** mechanism proposed and built inert (ADR-0026 / D-22, 2026-08-12).
Three decisions remain open owner gates — see [Before you enable it](#before-you-enable-it).

This is the operator guide. The design, the alternatives rejected, and the full
residual list live in [ADR-0026](adr/ADR-0026-tradingview-signal-bridge.md); the
risk posture is `RISK_REGISTER.md` **R-45**.

## What it does, in one paragraph

`chronos.bridge` is a **separate process** that listens for a TradingView webhook,
authenticates it against a shared secret, translates it into candidate proposal
JSON, and POSTs that to the backend's existing `POST /autonomy/proposals` on
loopback. It holds no trading authority. Everything downstream is unchanged: a
TradingView-sourced proposal walks the same admission checks, the same sizing
against your mandate's ceilings and floors, the same compiler, and the same
propose → preview → confirm → submit handoff as anything else. **The mandate is
still the only thing that grants authority, and the bridge cannot read, name,
write, or activate one.**

An alert is not evidence. Nothing here creates a promotion rung or a research
trial.

## Shipped posture: it sends nothing

`CHRONOS_TV_BRIDGE_FORWARD` defaults to `false`. In that posture the bridge
authenticates, parses, translates, logs what it *would* have proposed, and
forwards nothing. Run it that way first and read the log.

## Setup

### 1. Configure it

All variables are documented in `.env.example` under
`--- TradingView signal bridge (ADR-0026) ---`. They configure the **bridge
process**, not the backend. The bridge refuses to start without a secret of at
least 32 characters, the backend's API token, a non-empty symbol allowlist, and a
non-empty kind allowlist — an empty allowlist means *nothing* is translatable,
never everything.

```bash
export CHRONOS_TV_BRIDGE_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export CHRONOS_TV_BRIDGE_API_TOKEN="$(cat data/api_token)"   # wherever yours lives
export CHRONOS_TV_BRIDGE_SYMBOLS="SPY,IWM"
export CHRONOS_TV_BRIDGE_KINDS="OPEN,CLOSE,HOLD"
```

### 2. Run it

```bash
python -m chronos.bridge
```

It prints its posture on startup — where it binds, where proposals go, which
symbols and kinds are allowed, and whether forwarding is on. It prints no secret
and no token.

### 3. Write the TradingView alert message

In TradingView's alert dialog, set the webhook URL to your bridge and paste this
as the **message body**. It must be JSON — TradingView cannot set request
headers, which is why the secret travels in the body.

```json
{
  "secret": "<CHRONOS_TV_BRIDGE_SECRET>",
  "alert_id": "spy-breakout-{{timenow}}",
  "sent_at": "{{timenow}}",
  "action": "OPEN",
  "symbol": "{{ticker}}",
  "direction": "LONG",
  "quantity": "10",
  "strategy": "LONG_EQUITY",
  "thesis": "20-day breakout with expanding range",
  "invalidation": ["close below the 20-day moving average"]
}
```

### Field reference

| Field | Required | Notes |
|---|---|---|
| `secret` | always | Compared in constant time. Stripped before the alert is digested, logged, or translated — it never reaches the proposal. |
| `alert_id` | always | How a duplicate delivery is recognized and how the decision is traced back. Letters, digits, `.`, `_`, `:`, `-`; max 80 chars. Make it unique per firing (`{{timenow}}` works). |
| `sent_at` | always | ISO-8601 with an offset. Use `{{timenow}}`. Alerts older than `CHRONOS_TV_BRIDGE_MAX_ALERT_AGE_SECONDS` (default 120) are refused. |
| `action` | always | A decision kind, and it must also be in your `CHRONOS_TV_BRIDGE_KINDS` allowlist. |
| `symbol` | always | Must be in `CHRONOS_TV_BRIDGE_SYMBOLS` **and** in your mandate's scope. |
| `direction` | no | `LONG` / `SHORT` / `NEUTRAL`, default `NEUTRAL`. A `HOLD` may not express one. |
| `quantity` | no | A *request*, not an executable size — the kernel computes and clamps it against the mandate. `HOLD` and `CANCEL` may not carry one. |
| `strategy` | no | A `StrategyForm`. Not permitted on `HOLD`/`REDUCE`/`CLOSE`/`CANCEL`. |
| `time_horizon` | no | `INTRADAY` / `SWING` / `POSITION` / `LONG_TERM`. |
| `target_reference` | for targeted kinds | `INCREASE`, `REDUCE`, `CLOSE`, `ROLL`, `CANCEL`, `REPLACE` act on something existing and must name a Chronos `CHR-<PREFIX>-<32 hex>` reference. **Never a broker order id** — the contract refuses one. |
| `thesis`, `rationale` | no | Recorded, displayed, audited. Copied verbatim and never parsed into an order parameter. |
| `confidence` | no | 0 to 1. |
| `invalidation` | for exposure-creating kinds | `OPEN`, `INCREASE`, `HEDGE`, `ROLL`, `REPLACE` must state what would prove them wrong. Exposure is never created on an unsupported assertion. |

Unknown fields are **refused, not ignored** — a field the bridge silently dropped
is a field you believe is doing something.

## What it will refuse, and why

Refusals are applied in this order, and nothing is forwarded until all of them
pass: rate limit (default 10/min) → size and structure → the shared secret →
freshness → replay on `alert_id` → translation (allowlists, then the contract's
own coherence rules).

The response TradingView receives names a stage and a refusal code and
**deliberately quotes nothing you sent** — that response lands in TradingView's
log, outside Chronos's trust boundary. When you need to know *which* symbol or
*which* alert was refused, read the bridge's own log, where the specifics go.

A refusal is the system working. Do not widen an allowlist to make one go away
without deciding you meant to.

## Before you enable it

Three things are yours to decide, and merging ADR-0026 authorized the mechanism
in its inert posture only — not the trading:

1. **Whether to publish the listener beyond loopback at all, and how.** The
   bridge binds `127.0.0.1`. Reaching TradingView needs a tunnel or reverse proxy
   that you run, terminate TLS for, and own the risk of. Nothing in this
   repository manages that.
2. **Whether to set `CHRONOS_TV_BRIDGE_FORWARD=true`,** and with which symbol
   and kind allowlists.
3. **Whether TradingView alerts are an appropriate proposal source for this
   account** under the ADR-0025 readiness checklist. That is a strategy
   judgement, not an engineering one.

Two honest limits worth knowing before you decide:

- ~~**Provenance cannot tell the sources apart.**~~ *(corrected 2026-08-12:
  ADR-0023 landed — plan §6 finding 6 is addressed.)* Under the default,
  registry-unset posture this limit still reads true: every proposal carries
  the static ingress identity and the `tradingview_alert` evidence citation is
  the distinguishing mark. Once the owner configures `AUTONOMY_PROPOSERS_FILE`,
  the bridge must present its own registered credential — mint one
  (`python -m chronos.cli proposer mint --proposer-id tradingview-bridge
  --provider tradingview --model-id signal-bridge ...`), paste the registration
  into the registry, set `CHRONOS_TV_BRIDGE_PROPOSER_TOKEN` — and every
  bridge-sourced decision is stamped with the bridge's own identity, journaled
  with its proposer_id, distinguishable from the worker's. Note the pins
  consequence: a mandate whose `versions` block names the *worker's*
  registration refuses bridge proposals at admission — that is the owner
  choosing which author a grant trusts, and `mandate check` reports which
  registration a mandate authorizes.
- **Anyone with the URL and the secret can propose.** That is what a webhook is.
  Your mandate's scope remains the real bound on what a successful forgery could
  achieve, which is another reason to keep both allowlists tight.

## Stopping it

Stopping the bridge stops new TradingView proposals; it does **not** stop
Chronos. The bridge is a signal source, not a control surface. To stand the
trading system down, use the mechanisms that actually hold authority — the live
kill switch, the platform halt, and mandate revocation — sequenced in
`docs/INCIDENT_RESPONSE.md`. Killing the bridge and assuming you have stopped
trading would be a serious mistake.
