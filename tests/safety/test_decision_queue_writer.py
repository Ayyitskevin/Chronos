"""A model cannot say who it is, or what its decision is called (ADR-0016 §5, M4).

M2 and M3 both disclosed the same honest bound: the version-pin check proved a
decision *agreed* with the mandate's pins, not that an approved model produced
it. A model asked to state its own identity could simply claim a pinned one.
This module pins the upgrade.

Two properties, and each is structural rather than a policy check:

- **Provenance is stamped, never authored.** `ProposedDecision` has no
  provenance field at all, so there is nothing for a model to forge. The writer
  attaches it from configuration the model process never receives.
- **The id is derived from economic content.** If a model chose its own id it
  would escape R-31's re-submission bound by picking a fresh one each time --
  the counters are keyed by id, so a new id is a new budget. Re-proposing the
  same trade now yields the same id and is caught as a replay. Rewording the
  thesis does not, because a model that changes its explanation has not changed
  its trade.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from chronos.autonomy import (
    AITradeDecision,
    DecisionKind,
    DecisionProvenance,
    EntryPlan,
    EvidenceCitation,
    PriceReference,
    PriceTrigger,
    ProposedDecision,
    StrategyForm,
    TradableAssetClass,
    TriggerComparator,
)
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.supervisor import queue

_NOW = datetime(2026, 7, 25, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        with database.sessions.begin() as db_session:
            yield db_session
    finally:
        database.dispose()


def _identity(**overrides: Any) -> queue.HarnessIdentity:
    base: dict[str, Any] = {
        "provider": "anthropic",
        "model_id": "model-x",
        "model_version": "1",
        "prompt_version": "1",
        "tool_schema_version": "1",
        "decision_schema_version": "1",
        "policy_version": "1",
        "evidence_bundle_id": "eb-1",
        "evidence_bundle_digest": "b" * 64,
    }
    base.update(overrides)
    return queue.HarnessIdentity(**base)


def _proposal(**overrides: Any) -> ProposedDecision:
    base: dict[str, Any] = {
        "kind": DecisionKind.OPEN,
        "asset_class": TradableAssetClass.EQUITY,
        "symbol": "SPY",
        "requested_strategy": StrategyForm.LONG_EQUITY,
        "requested_quantity": Decimal(10),
        "evidence": (
            EvidenceCitation(evidence_id="ev-1", kind="quote", as_of=_NOW, digest="c" * 64),
        ),
        "invalidation_conditions": ("closes below 400",),
    }
    base.update(overrides)
    return ProposedDecision(**base)


# ------------------------------------------------------------ no self-report


def test_a_proposal_has_no_provenance_field_to_forge() -> None:
    """The structural half. There is nothing for a model to lie about."""

    assert "provenance" not in ProposedDecision.model_fields
    assert "decision_id" not in ProposedDecision.model_fields


def test_a_proposal_refuses_a_smuggled_provenance() -> None:
    """Extra fields are forbidden, so it cannot be attached on the way in."""

    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        ProposedDecision(
            kind=DecisionKind.HOLD,
            asset_class=TradableAssetClass.EQUITY,
            symbol="SPY",
            provenance=DecisionProvenance(
                provider="attacker",
                model_id="claimed",
                model_version="1",
                prompt_version="1",
                tool_schema_version="1",
                decision_schema_version="1",
                policy_version="1",
                evidence_bundle_id="eb-1",
                evidence_bundle_digest="b" * 64,
                produced_at=_NOW,
            ),
        )


def test_the_writer_stamps_identity_the_proposal_never_carried() -> None:
    decision = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW)
    assert isinstance(decision, AITradeDecision)
    assert decision.provenance.provider == "anthropic"
    assert decision.provenance.model_id == "model-x"
    assert decision.provenance.produced_at == _NOW


def test_the_stamped_identity_comes_from_the_harness_not_the_proposal() -> None:
    """Two different harnesses stamp two different attributions on one proposal."""

    proposal = _proposal()
    first = queue.accept(proposal, identity=_identity(model_id="model-x"), produced_at=_NOW)
    second = queue.accept(proposal, identity=_identity(model_id="model-y"), produced_at=_NOW)
    assert first.provenance.model_id == "model-x"
    assert second.provenance.model_id == "model-y"
    # ...and the proposal itself was never mutated by either.
    assert not hasattr(proposal, "provenance")


def test_re_stamping_an_already_attributed_decision_is_refused() -> None:
    """Otherwise the boundary itself becomes the forgery tool.

    A caller holding a stamped decision could pass it back through `accept` with
    a different identity and overwrite the attribution -- through the very
    function that exists to prevent that.
    """

    decision = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW)
    with pytest.raises(queue.ProposalRejected, match="already carries provenance"):
        queue.accept(decision, identity=_identity(model_id="other"), produced_at=_NOW)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(queue.ProposalRejected, match="timezone-aware"):
        queue.accept(
            _proposal(),
            identity=_identity(),
            produced_at=datetime(2026, 7, 25, 14, 0),  # noqa: DTZ001 - the point
        )


def test_every_proposal_field_survives_stamping() -> None:
    """Stamping adds; it must never quietly drop what the model proposed."""

    proposal = _proposal(
        thesis="mean reversion",
        rationale="oversold",
        confidence=Decimal("0.6"),
        key_uncertainties=("earnings in 3 days",),
    )
    decision = queue.accept(proposal, identity=_identity(), produced_at=_NOW)
    for name in ProposedDecision.model_fields:
        assert getattr(decision, name) == getattr(proposal, name), name


# --------------------------------------------------- economic identity (R-31)


def test_the_same_trade_proposed_twice_gets_the_same_id() -> None:
    """The retry loophole, closed: a fresh id is no longer a fresh budget."""

    first = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW)
    later = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW + timedelta(hours=1))
    assert first.decision_id == later.decision_id


def test_rewording_the_thesis_does_not_mint_a_new_id() -> None:
    """A model that changes its explanation has not changed its trade.

    If narrative were in the digest, rewording would hand back the retry budget
    the digest exists to take away.
    """

    plain = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW)
    reworded = queue.accept(
        _proposal(
            thesis="an entirely different story",
            rationale="different reasoning",
            confidence=Decimal("0.9"),
            key_uncertainties=("something else",),
        ),
        identity=_identity(),
        produced_at=_NOW,
    )
    assert plain.decision_id == reworded.decision_id


@pytest.mark.parametrize(
    "override",
    [
        {"requested_quantity": Decimal(11)},
        {"symbol": "QQQ"},
        {"direction": "SHORT"},
        {"requested_risk_budget_usd": Decimal(500)},
        {"max_acceptable_loss_usd": Decimal(250)},
        {"protective_order_required": True},
        {
            "entry_plan": EntryPlan(
                trigger=PriceTrigger(
                    comparator=TriggerComparator.AT_OR_BELOW,
                    reference=PriceReference.ASK,
                    value=Decimal(390),
                )
            )
        },
    ],
)
def test_a_genuinely_different_trade_gets_a_different_id(override: dict[str, Any]) -> None:
    """Economic terms must separate. Collapsing them would block real trades."""

    baseline = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW)
    altered = queue.accept(_proposal(**override), identity=_identity(), produced_at=_NOW)
    assert baseline.decision_id != altered.decision_id


def test_the_id_does_not_depend_on_who_produced_it() -> None:
    """Identity is about the trade. Two models proposing the same trade collide,
    which is correct: it is the same trade, and the second is a replay."""

    first = queue.accept(_proposal(), identity=_identity(model_id="a"), produced_at=_NOW)
    second = queue.accept(_proposal(), identity=_identity(model_id="b"), produced_at=_NOW)
    assert first.decision_id == second.decision_id


def test_the_fingerprint_is_stable_across_processes() -> None:
    """A UUIDv5 over content, not a random or hash-seeded value.

    A per-process value would make the id useless for replay detection after a
    restart -- the exact failure R-31's in-memory counters had.
    """

    proposal = _proposal()
    assert queue.economic_fingerprint(proposal) == queue.economic_fingerprint(proposal)
    assert queue.economic_fingerprint(proposal) == queue.economic_fingerprint(_proposal())


# ------------------------------------------------------------------ the chain


def test_an_accepted_proposal_is_recorded_in_a_chained_stream(session: Session) -> None:
    queue.accept(
        _proposal(),
        identity=_identity(),
        produced_at=_NOW,
        session=session,
        account_fingerprint=_FINGERPRINT,
    )
    verification = hash_chain.verify(session, f"{queue.PROPOSAL_STREAM}:{_FINGERPRINT}")
    assert verification.ok is True
    assert verification.records == 1


def test_the_writer_works_without_a_database(session: Session) -> None:
    """Deliberate: the boundary must be testable, and auditable, without one."""

    del session
    decision = queue.accept(_proposal(), identity=_identity(), produced_at=_NOW)
    assert decision.decision_id


def test_the_recorded_payload_carries_no_narrative(session: Session) -> None:
    """The proposal stream records what was traded, not the model's prose.

    Narrative belongs in the decision journal, which the owner reads
    deliberately. Duplicating it here would spread model-authored text into a
    second store for no added accountability.
    """

    queue.accept(
        _proposal(thesis="secret sauce", rationale="because"),
        identity=_identity(),
        produced_at=_NOW,
        session=session,
        account_fingerprint=_FINGERPRINT,
    )
    rows = hash_chain.verify(session, f"{queue.PROPOSAL_STREAM}:{_FINGERPRINT}")
    assert rows.ok
    from chronos.persistence.schema import HashChainRow

    row = session.query(HashChainRow).one()
    assert "secret sauce" not in row.payload_json
    assert "because" not in row.payload_json
