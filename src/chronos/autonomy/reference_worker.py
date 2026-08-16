"""Deterministic reference worker. Not a live model and not a provider SDK.

Emits one ``ProposedDecision`` from an issued bundle: HOLD unless exactly one
Five-Tool signal is ``enter_long`` / ``enter_short`` and the matching pairing
veto is ``allow``. The payload never includes ``provenance`` or ``decision_id``.

A real LLM worker is an external process the owner runs. This function is the
pinned, testable stand-in so shadow journaling can train on Chronos's own
closed-bar facts without loosening a gate.
"""

from __future__ import annotations

import json

from chronos.autonomy.book import DEFAULT_HOLD_SYMBOL, is_tradable
from chronos.autonomy.decision import EvidenceCitation, ProposedDecision
from chronos.autonomy.enums import DecisionDirection, DecisionKind, StrategyForm, TradableAssetClass
from chronos.autonomy.evidence import AdvisoryFiveToolFact, AdvisoryVetoFact, EvidenceBundle

_ENTER_LONG = "enter_long"
_ENTER_SHORT = "enter_short"
_ALLOW = "allow"


def _actionable(
    bundle: EvidenceBundle,
) -> tuple[AdvisoryFiveToolFact, AdvisoryVetoFact] | None:
    if len(bundle.five_tool_signals) != 1 or len(bundle.pairing_vetoes) != 1:
        return None
    signal = bundle.five_tool_signals[0]
    veto = bundle.pairing_vetoes[0]
    if signal.intent not in {_ENTER_LONG, _ENTER_SHORT}:
        return None
    if veto.status != _ALLOW:
        return None
    if veto.original_intent != signal.intent:
        return None
    if veto.filtered_intent != signal.intent:
        return None
    if not is_tradable(signal.symbol):
        return None
    return signal, veto


def _hold_symbol(bundle: EvidenceBundle) -> str:
    if bundle.five_tool_signals and is_tradable(bundle.five_tool_signals[0].symbol):
        return bundle.five_tool_signals[0].symbol
    return DEFAULT_HOLD_SYMBOL


def propose(bundle: EvidenceBundle) -> ProposedDecision:
    """HOLD, or OPEN when ENTER and pairing ALLOW agree on one book name."""

    actionable = _actionable(bundle)
    if actionable is None:
        symbol = _hold_symbol(bundle)
        return ProposedDecision(
            kind=DecisionKind.HOLD,
            asset_class=TradableAssetClass.EQUITY,
            symbol=symbol,
            direction=DecisionDirection.NEUTRAL,
            thesis="reference worker: HOLD unless Five-Tool ENTER and pairing ALLOW",
        )
    signal, _veto = actionable
    long_side = signal.intent == _ENTER_LONG
    citation = EvidenceCitation(
        evidence_id=bundle.bundle_id,
        kind="advisory_five_tool",
        as_of=bundle.issued_at,
        digest=bundle.digest(),
    )
    return ProposedDecision(
        kind=DecisionKind.OPEN,
        asset_class=TradableAssetClass.EQUITY,
        symbol=signal.symbol,
        direction=DecisionDirection.LONG if long_side else DecisionDirection.SHORT,
        requested_strategy=StrategyForm.LONG_EQUITY if long_side else StrategyForm.SHORT_EQUITY,
        evidence=(citation,),
        invalidation_conditions=(
            "pairing veto status is not allow",
            "five-tool intent is no longer enter",
        ),
        thesis="reference worker: Five-Tool ENTER and pairing ALLOW",
        rationale="deterministic stub; not a live model",
    )


def propose_payload(bundle: EvidenceBundle) -> bytes:
    """JSON a hostile ingress can parse. Writer-owned fields are absent."""

    proposal = propose(bundle)
    dumped = proposal.model_dump(mode="json")
    dumped.pop("provenance", None)
    dumped.pop("decision_id", None)
    return json.dumps(dumped, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
