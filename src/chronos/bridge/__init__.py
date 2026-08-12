"""The signal bridge: an untrusted process that turns an external alert into a proposal.

ADR-0026. Chronos already has exactly one way for something outside the
broker-writing process to suggest a trade: ``POST /autonomy/proposals``, parsed
by :mod:`chronos.supervisor.ingress`, which "trusts nothing it receives". That
endpoint was built for an external *model* worker. This package adds a second
kind of caller to that same door — a TradingView alert — and the design rule is
that it must arrive as one more untrusted stranger, not as a privileged one.

## What this package is, structurally

A separate process. It holds no Chronos capability at all:

- it imports no order, broker, execution, risk, persistence, or supervisor
  module, and — unusually for this repository — **it does not import
  ``chronos.autonomy`` either**;
- it opens no database, holds no writer lease, and touches no kill switch;
- its only outbound action is one HTTP POST to the loopback ingress, carrying
  the same local API token any other local caller must present.

The refusal to import the decision contract is deliberate and is the load-bearing
choice in this package. ``tests/safety/test_autonomy_contracts.py``'s
single-consumer guard says only ``chronos.supervisor`` (plus the named app-plane
wiring) may consume model decisions. The bridge could have been added to that
allowlist. It was not, because the weaker position is the correct one: **the
bridge emits candidate JSON, and the existing ingress decides whether that JSON
is a proposal.** A bridge that constructed a ``ProposedDecision`` would be a
second place where "what a valid decision is" is decided, and the whole point of
the ingress is that there is exactly one such place.

The cost of that choice is duplication: :mod:`chronos.bridge.vocabulary` restates
the decision vocabulary and :mod:`chronos.bridge.translate` restates the
contract's payload-coherence rules. The cost is paid deliberately and is
guarded — ``tests/safety/test_tradingview_bridge_isolation.py`` fails if the
restatement drifts from the real enums, and
``tests/safety/test_tradingview_bridge_exercised.py`` pushes the bridge's own
output through the real :func:`chronos.supervisor.ingress.parse_proposal` so
"the bridge emits something the ingress accepts" is proven rather than assumed.

## What this package is NOT

It grants nothing. Every gate that judged a proposal before this package existed
judges it identically now: admission's fifteen ordered checks, sizing against the
mandate's ceilings and floors, the capability-matrix compiler, and the full
propose → preview → confirm → submit handoff. A TradingView alert cannot reach
a broker except by satisfying all of them, and the mandate's instrument,
strategy, order-form, and capital scope binds it exactly as it binds anything
else. The bridge cannot widen any of that; it can only be refused by it.

Two honest consequences are recorded rather than smoothed over:

1. **Provenance still cannot tell the sources apart.** Every proposal reaching
   the ingress is stamped with the static ``INGRESS_IDENTITY``
   (provider ``external-worker``, model ``ingress``), so the *provenance* on a
   TradingView-sourced decision is byte-identical to one from a model worker.
   The bridge compensates where it can — it writes an evidence citation naming
   the alert and digesting its exact bytes, so the audit record distinguishes
   them — but plan §6 finding 6 (a proposal-only credential and a real worker
   identity protocol) stays open, and this package makes it more acute rather
   than less.
2. **Anyone who learns the webhook URL and secret can propose.** That is the
   threat model of a webhook, not a defect introduced here, and it is why the
   secret is mandatory, why forwarding is off until the owner turns it on, and
   why the symbol and kind allowlists are required rather than defaulted.
"""

from __future__ import annotations

from chronos.bridge.alert import AlertRejected, AlertUnauthorized, TradingViewAlert, parse_alert
from chronos.bridge.config import BridgeConfig, BridgeConfigError, load_config
from chronos.bridge.translate import TranslationRefused, build_proposal

__all__ = [
    "AlertRejected",
    "AlertUnauthorized",
    "BridgeConfig",
    "BridgeConfigError",
    "TradingViewAlert",
    "TranslationRefused",
    "build_proposal",
    "load_config",
    "parse_alert",
]
