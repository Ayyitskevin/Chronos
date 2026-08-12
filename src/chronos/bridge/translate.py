"""Turning an authenticated alert into candidate proposal JSON (ADR-0026).

The output of this module is a plain ``dict`` that will be serialized and POSTed
to ``/autonomy/proposals``. It is deliberately **not** a ``ProposedDecision``:
constructing one here would make this a second place where "what a valid
decision is" gets decided, and the supervisor's ingress exists precisely so that
there is only one such place. What this module produces is a candidate; the
ingress decides whether it is a proposal.

## Why the contract's rules are restated here anyway

Because a refusal that names the real problem is worth more than a generic one.
An alert asking to CLOSE while carrying a strategy is incoherent, and the owner
debugging their TradingView alert template deserves to be told that, rather than
receiving ``rejected field(s): (root)`` from a parser two processes away. Every
rule below mirrors one in :mod:`chronos.autonomy.decision`, and
``tests/safety/test_tradingview_bridge_exercised.py`` proves the mirror is
faithful by pushing this module's output through the real ingress.

These messages reach the caller, so none of them quotes a value the caller sent —
not even one drawn from a closed vocabulary. "The response echoes nothing the
sender supplied" is an invariant a test can check in one line; "the response
echoes only the safe subset" is one that needs a judgement call at every new
message, and this package has too many messages for that to hold. The specifics
an owner needs while debugging are written to the bridge's own log instead, where
the reader is already inside the trust boundary.

## What is deliberately not translatable

- **Any asset class other than equities.** The autonomy wiring can only gather
  instrument facts for equities and crypto; an option decision refuses at that
  seam because chain resolution is not owned there. Emitting one would queue a
  proposal guaranteed to be refused later, which is worse than refusing now.
- **Order type, price, routing, account, or transmit.** None of these exist on
  the decision contract. The bridge cannot express them because the contract
  cannot, which is the structural guarantee ADR-0016 §2 rests on — a
  TradingView alert saying "buy at market" gets a *kind*, and the deterministic
  compiler still chooses the form and derives the price from Chronos's own quote.
- **A risk budget, a protective-stop requirement, or a maximum acceptable loss.**
  Those four fields on the contract are the subject of ADR-0021 and are inert;
  the bridge does not set fields that do not mechanically do anything, because
  populating an inert field is how an alert author comes to believe a protection
  exists that does not.
"""

from __future__ import annotations

import re
from typing import Any, Final

from chronos.bridge.alert import TradingViewAlert
from chronos.bridge.config import BridgeConfig
from chronos.bridge.vocabulary import (
    CHRONOS_REFERENCE_PATTERN,
    EXPOSURE_CREATING_KINDS,
    NO_ENTRY_KINDS,
    SIZELESS_KINDS,
    SUPPORTED_ASSET_CLASS,
    TARGETED_KINDS,
)

_REFERENCE = re.compile(CHRONOS_REFERENCE_PATTERN)

#: The citation kind written into every proposal this bridge emits. Chosen so a
#: reader of the audit chain can tell a TradingView-sourced decision from a
#: model-worker one, which the static ingress provenance cannot do (plan §6
#: finding 6 — see this package's ``__init__``).
EVIDENCE_KIND: Final[str] = "tradingview_alert"


class TranslationRefused(ValueError):
    """The alert is well-formed but may not become a proposal."""


def build_proposal(alert: TradingViewAlert, config: BridgeConfig) -> dict[str, Any]:
    """Build candidate proposal JSON, or raise :class:`TranslationRefused`."""

    if alert.symbol not in config.symbols:
        raise TranslationRefused(
            "the symbol this alert names is not in the bridge's allowlist; add it to "
            "CHRONOS_TV_BRIDGE_SYMBOLS if the mandate also scopes it"
        )
    if alert.action not in config.kinds:
        raise TranslationRefused(
            "the decision kind this alert asks for is not in the bridge's allowlist; add "
            "it to CHRONOS_TV_BRIDGE_KINDS to permit it"
        )

    _check_coherence(alert)

    proposal: dict[str, Any] = {
        "kind": alert.action,
        "asset_class": SUPPORTED_ASSET_CLASS,
        "symbol": alert.symbol,
        "direction": alert.direction,
        "evidence": [_citation(alert)],
    }
    proposal.update(_narrative_of(alert, _default_thesis(alert)))
    if alert.quantity is not None:
        proposal["requested_quantity"] = str(alert.quantity)
    if alert.strategy:
        proposal["requested_strategy"] = alert.strategy
    if alert.time_horizon:
        proposal["time_horizon"] = alert.time_horizon
    if alert.target_reference:
        proposal["target_client_reference"] = alert.target_reference
    if alert.confidence is not None:
        proposal["confidence"] = str(alert.confidence)
    if alert.invalidation:
        proposal["invalidation_conditions"] = list(alert.invalidation)
    return proposal


def _check_coherence(alert: TradingViewAlert) -> None:
    """Every rule here mirrors one the decision contract enforces."""

    kind = alert.action

    if kind in EXPOSURE_CREATING_KINDS and not alert.invalidation:
        raise TranslationRefused(
            f"a {kind} alert must state its invalidation conditions: exposure may not be "
            "created on an unsupported assertion. Add an 'invalidation' field to the "
            "TradingView alert message"
        )
    if kind in TARGETED_KINDS and not alert.target_reference:
        raise TranslationRefused(
            f"a {kind} alert must name the Chronos reference it acts on, in "
            "'target_reference'; it acts on an existing order or position"
        )
    if alert.target_reference and not _REFERENCE.match(alert.target_reference):
        raise TranslationRefused(
            "target_reference must be a Chronos-owned CHR-<PREFIX>-<32 hex> reference; a "
            "decision may never name a broker order id"
        )
    if kind == "OPEN" and alert.target_reference:
        raise TranslationRefused("an OPEN alert may not name an existing order or position")
    if kind in SIZELESS_KINDS and alert.quantity is not None:
        raise TranslationRefused(f"a {kind} alert may not request a size")
    if kind in NO_ENTRY_KINDS and alert.strategy:
        raise TranslationRefused(f"a {kind} alert may not request a strategy")
    if kind == "HOLD" and alert.direction != "NEUTRAL":
        raise TranslationRefused("a HOLD alert may not express a direction")


def _narrative_of(alert: TradingViewAlert, fallback: str) -> dict[str, str]:
    """The alert's own words, copied into the proposal and never inspected.

    Every narrative read in this module happens here, and this function does
    nothing but copy — pinned by ``test_a_narrative_recorder_only_copies_it``,
    which walks this body and fails on a comparison, a subscript, arithmetic, or
    any call outside ``list``/``str``.

    That is a stricter rule than the module would otherwise be held to, and it is
    taken on deliberately. ADR-0016 §5's hazard is free-form prose reaching an
    order parameter, and a translator standing between an alert author and a
    trading decision is exactly where ``if "aggressive" in alert.thesis:`` would
    one day look reasonable. The empty-thesis branch below is a guard on
    *presence*, the same shape as the sanctioned ``if decision is None``; nothing
    here reads what the text says.
    """

    if alert.thesis:
        return {"thesis": alert.thesis, "rationale": alert.rationale}
    return {"thesis": fallback, "rationale": alert.rationale}


def _citation(alert: TradingViewAlert) -> dict[str, Any]:
    """The alert itself, cited as the evidence it is.

    The digest is over the exact alert text with the secret removed, so the
    audit chain records *which* alert produced the decision and the owner can
    recompute it from what TradingView sent.
    """

    return {
        "evidence_id": f"tradingview-alert:{alert.alert_id}",
        "kind": EVIDENCE_KIND,
        "as_of": alert.sent_at.isoformat(),
        "digest": alert.digest,
        "excerpt": (
            f"TradingView alert {alert.alert_id} for {alert.symbol}: "
            f"{alert.action} {alert.direction}"
        ),
    }


def _default_thesis(alert: TradingViewAlert) -> str:
    """What the decision says about itself when the alert supplied no thesis.

    Stated as the mechanical fact it is. An alert that carries no reasoning
    should produce a decision that says so, rather than one that looks reasoned.
    """

    return (
        f"TradingView alert {alert.alert_id} fired for {alert.symbol}. "
        "No thesis was supplied by the alert; this decision rests on the alert "
        "condition alone."
    )
