"""Turning validated tool output into candidate proposal JSON (ADR-0027).

The model's tool input is schema-valid by the time it reaches this module —
``strict: true`` guarantees the shape — but shape is not coherence, and this
worker treats its own model as untrusted. Every rule below mirrors one the
decision contract enforces, restated for the same reason the TradingView
bridge restates them: a refusal that names the real problem ("a CLOSE must
name the order it closes") is worth more than a generic contract error two
processes away, and the drift risk is pinned by the isolation suite's
equality tests.

What the model can never do here, by construction rather than by check:

- **Author provenance.** ``provenance`` and ``decision_id`` are not fields of
  the tool schema, are never copied from model output, and would be refused
  loudly by the ingress if they somehow appeared.
- **Fabricate evidence.** The single citation on every proposal is built by
  :meth:`worker.evidence.EvidenceSnapshot.citation` from the exact bytes the
  model was shown. The model's opinion of what it saw is recorded in the
  thesis; what it actually saw is recorded in the digest.
- **Reach beyond the allowlists.** Symbol and kind must be in the worker's
  own configuration — defence in depth in front of the mandate's scope, which
  still binds regardless.
"""

from __future__ import annotations

import re
from typing import Any

from worker.config import WorkerConfig
from worker.evidence import EvidenceSnapshot
from worker.vocabulary import (
    CHRONOS_REFERENCE_PATTERN,
    DECISION_KINDS,
    DIRECTIONS,
    EXPOSURE_CREATING_KINDS,
    NO_ENTRY_KINDS,
    SIZELESS_KINDS,
    SUPPORTED_ASSET_CLASS,
    TARGETED_KINDS,
)

_REFERENCE = re.compile(CHRONOS_REFERENCE_PATTERN)

#: Contract bounds, restated. The contract re-checks all of them downstream.
_MAX_NARRATIVE = 4000
_MAX_ENTRY = 500
_MAX_ENTRIES = 32


class ProposalRefused(ValueError):
    """The model's decision is shaped right but may not become a proposal."""


def build_proposal(
    decision: dict[str, Any], snapshot: EvidenceSnapshot, config: WorkerConfig
) -> dict[str, Any]:
    """Build candidate proposal JSON, or raise :class:`ProposalRefused`."""

    kind = _required_str(decision, "kind")
    if kind not in DECISION_KINDS:
        raise ProposalRefused(f"{kind!r} is not a decision kind")
    if kind not in config.kinds:
        raise ProposalRefused(
            f"decision kind {kind} is not in this worker's CHRONOS_WORKER_KINDS allowlist"
        )

    symbol = _required_str(decision, "symbol").upper()
    if symbol not in config.symbols:
        raise ProposalRefused(
            f"symbol {symbol} is not on this worker's watchlist; the model may only "
            "propose on symbols it was shown evidence for"
        )

    direction = _required_str(decision, "direction").upper()
    if direction not in DIRECTIONS:
        raise ProposalRefused(f"{direction!r} is not a direction")

    thesis = _required_str(decision, "thesis")
    if len(thesis) > _MAX_NARRATIVE:
        raise ProposalRefused(f"thesis is longer than the contract's {_MAX_NARRATIVE} characters")

    invalidation = [
        entry.strip()
        for entry in (decision.get("invalidation") or [])
        if isinstance(entry, str) and entry.strip()
    ]
    if len(invalidation) > _MAX_ENTRIES:
        raise ProposalRefused(f"more than {_MAX_ENTRIES} invalidation entries")
    if any(len(entry) > _MAX_ENTRY for entry in invalidation):
        raise ProposalRefused(f"an invalidation entry exceeds {_MAX_ENTRY} characters")

    target = _optional_str(decision, "target_reference")
    quantity = _optional_str(decision, "quantity")
    strategy = _optional_str(decision, "strategy")

    _check_coherence(
        kind,
        direction=direction,
        quantity=quantity,
        strategy=strategy,
        target=target,
        invalidation=invalidation,
    )

    proposal: dict[str, Any] = {
        "kind": kind,
        "asset_class": SUPPORTED_ASSET_CLASS,
        "symbol": symbol,
        "direction": direction,
        "thesis": thesis,
        "evidence": [snapshot.citation()],
    }
    rationale = _optional_str(decision, "rationale")
    if rationale:
        if len(rationale) > _MAX_NARRATIVE:
            raise ProposalRefused(
                f"rationale is longer than the contract's {_MAX_NARRATIVE} characters"
            )
        proposal["rationale"] = rationale
    if quantity:
        proposal["requested_quantity"] = quantity
    if strategy:
        proposal["requested_strategy"] = strategy
    horizon = _optional_str(decision, "time_horizon")
    if horizon:
        proposal["time_horizon"] = horizon
    if target:
        proposal["target_client_reference"] = target
    confidence = _optional_str(decision, "confidence")
    if confidence:
        proposal["confidence"] = confidence
    if invalidation:
        proposal["invalidation_conditions"] = invalidation
    return proposal


def _check_coherence(
    kind: str,
    *,
    direction: str,
    quantity: str | None,
    strategy: str | None,
    target: str | None,
    invalidation: list[str],
) -> None:
    """Every rule here mirrors one the decision contract enforces."""

    if kind in EXPOSURE_CREATING_KINDS and not invalidation:
        raise ProposalRefused(
            f"a {kind} decision must state its invalidation conditions: exposure is never "
            "created on an unsupported assertion"
        )
    if kind in TARGETED_KINDS and not target:
        raise ProposalRefused(
            f"a {kind} decision must name the Chronos reference it acts on in target_reference"
        )
    if target and not _REFERENCE.match(target.upper()):
        raise ProposalRefused(
            "target_reference must be a Chronos-owned CHR-<PREFIX>-<32 hex> reference from "
            "the evidence snapshot; a decision may never name a broker order id"
        )
    if kind == "OPEN" and target:
        raise ProposalRefused("an OPEN decision may not name an existing order or position")
    if kind in SIZELESS_KINDS and quantity:
        raise ProposalRefused(f"a {kind} decision may not request a size")
    if kind in NO_ENTRY_KINDS and strategy:
        raise ProposalRefused(f"a {kind} decision may not request a strategy")
    if kind == "HOLD" and direction != "NEUTRAL":
        raise ProposalRefused("a HOLD decision may not express a direction")


def _required_str(decision: dict[str, Any], name: str) -> str:
    value = decision.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProposalRefused(f"{name} is required and must be a non-empty string")
    return value.strip()


def _optional_str(decision: dict[str, Any], name: str) -> str | None:
    value = decision.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProposalRefused(f"{name} must be a string when present")
    stripped = value.strip()
    return stripped or None
