"""A1, exercised: the journal says what the order plane actually did.

Plan §6 finding 5: the supervisor read only *exceptions* out of the handoff, so
every non-exception answer journaled ``COMPLETE`` and consumed an
``orders_submitted`` activity attempt. The submission boundary **returns** its
refusals — ``READ_ONLY_LEASE``, ``MODE_FORBIDS``, ``LIVE_GATE_BLOCKED`` (which
includes a kill switch tripping inside the CAS-to-transmit window),
``RECONCILIATION_NOT_READY``, ``BROKER_REFUSED_BEFORE_SEND`` — and each of those
recorded a submitted order that never existed and spent budget on it.

The old behavior was wrong in **both** directions at once, which is why both are
pinned here:

- *permissive*: a refusal before the wire spent an activity attempt, so a
  read-only backend could exhaust an opening-order ceiling without the venue ever
  hearing from it;
- *silently reassuring*: an ambiguous send — bytes possibly gone, state unknown —
  recorded ``COMPLETE`` with no alert, when manual broker resolution of an
  ambiguous send is an owner act (VISION_COMPLETION_PLAN §11).

What is exercised, end to end through a real session, a real hash-chained
journal, and the real durable counters:

1. each of the four dispositions journals **its own** stage and refusal code;
2. ``orders_submitted`` advances **only** per the documented counting rule;
3. an ambiguous send raises a CRITICAL owner alert; a venue rejection raises a
   WARNING; a refusal before the wire raises none;
4. every member of ``SubmissionRefusalCode`` is classified at the app-plane seam,
   so a new order-plane refusal cannot default into silence;
5. ``BROKER_SUBMIT_FAILED``'s three real shapes are separated by evidence;
6. an unclassifiable handoff answer is ambiguous-and-alerted, never success.

Weighted the fail-closed way (§4d): the cases that must refuse, alert, or
withhold a count outnumber the one confirmed-send case.

The supervisor still holds no order-plane type. ``test_autonomy_contracts.py``
pins the import isolation; the last test here pins the narrower property that the
new module in particular reaches nothing that can act.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from chronos.api import autonomy_wiring
from chronos.autonomy import (
    AutonomyMandate,
    AutonomyMode,
    CapitalLimits,
    ConcentrationLimits,
    DecisionKind,
    EvidenceCitation,
    FamilyPromotion,
    InstrumentScope,
    MarketDataRequirements,
    OrderForm,
    PromotionLevel,
    ProposedDecision,
    StrategyForm,
    TradableAssetClass,
    VersionPins,
)
from chronos.domain.enums import DataQuality, OrderLifecycle
from chronos.domain.models import OrderSubmission, UnderlyingContract
from chronos.orders.submission import SubmissionOutcome, SubmissionRefusalCode
from chronos.persistence import hash_chain
from chronos.persistence.database import Database
from chronos.persistence.schema import AutonomyOwnerAlertRow, HashChainRow
from chronos.supervisor import durable, queue
from chronos.supervisor.admission import MarketDataEvidence
from chronos.supervisor.compiler import QuoteEvidence
from chronos.supervisor.handoff import (
    COUNTS_ACTIVITY_ATTEMPT,
    DEFAULT_REFUSAL_CODES,
    REQUIRES_OWNER_ALERT,
    SUBMIT_RAISED_CODE,
    UNTYPED_RESULT_CODE,
    HandoffDisposition,
    HandoffResult,
    classify,
)
from chronos.supervisor.loop import CycleFacts, CycleStage, run_cycle
from chronos.supervisor.sizing import AccountEvidence

_NOW = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
_FINGERPRINT = "a" * 64
_ACCOUNT = "DU1234567"


@pytest.fixture
def session() -> Iterator[Session]:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    # A plain session, not ``sessionmaker.begin()``: since ADR-0052 a cycle with a
    # handoff commits before the wire, and SQLAlchemy refuses to operate on a
    # transaction closed inside a context manager. Production's drain holds a
    # plain session for the same reason, so this is also the truer harness.
    db_session = database.sessions()
    try:
        yield db_session
        db_session.commit()
    finally:
        db_session.close()
        database.dispose()


# ----------------------------------------------------------------- harness


def _identity() -> queue.HarnessIdentity:
    return queue.HarnessIdentity(
        provider="anthropic",
        model_id="model-x",
        model_version="1",
        prompt_version="1",
        tool_schema_version="1",
        decision_schema_version="1",
        policy_version="1",
        evidence_bundle_id="eb-1",
        evidence_bundle_digest="b" * 64,
    )


def _mandate(**overrides: Any) -> AutonomyMandate:
    base: dict[str, Any] = {
        "mandate_id": "m-1",
        "mandate_version": 1,
        "account_fingerprint": _FINGERPRINT,
        "mode": AutonomyMode.PAPER_AUTONOMOUS,
        "promotions": (
            FamilyPromotion(
                asset_class=TradableAssetClass.EQUITY, level=PromotionLevel.PAPER_AUTONOMOUS
            ),
        ),
        "effective_from": _NOW - timedelta(hours=1),
        "expires_at": _NOW + timedelta(days=1),
        "versions": VersionPins(
            provider="anthropic",
            model_id="model-x",
            model_version="1",
            prompt_version="1",
            tool_schema_version="1",
            decision_schema_version="1",
            policy_version="1",
        ),
        "scope": InstrumentScope(
            asset_classes=(TradableAssetClass.EQUITY,),
            symbols=("SPY",),
            strategies=(StrategyForm.LONG_EQUITY,),
            order_forms=(OrderForm.LIMIT,),
        ),
        "capital": CapitalLimits(
            allocated_capital_usd=Decimal(50_000),
            max_order_notional_usd=Decimal(10_000),
            max_gross_exposure_usd=Decimal(500_000),
            max_net_exposure_usd=Decimal(500_000),
            max_position_notional_usd=Decimal(100_000),
            max_shares_per_order=100,
            min_cash_floor_usd=Decimal(1_000),
            min_buying_power_usd=Decimal(500),
        ),
        "concentration": ConcentrationLimits(max_symbol_exposure_pct=Decimal("0.50")),
        "market_data": MarketDataRequirements(
            max_quote_age_seconds=Decimal(5),
            permitted_data_qualities=(DataQuality.LIVE,),
        ),
        "owner_authorization_ref": "owner-1",
        "authored_at": _NOW,
    }
    base.update(overrides)
    return AutonomyMandate(**base)


def _facts(**overrides: Any) -> CycleFacts:
    base: dict[str, Any] = {
        "account_fingerprint": _FINGERPRINT,
        "account_id": _ACCOUNT,
        "now": _NOW,
        "process_generation": 7,
        "evidence_bundle_id": "eb-1",
        "evidence_bundle_digest": "b" * 64,
        "market_data": MarketDataEvidence(quote_age_seconds=Decimal(1), quality=DataQuality.LIVE),
        "account": AccountEvidence(
            net_liquidation_usd=Decimal(100_000),
            total_cash_usd=Decimal(60_000),
            buying_power_usd=Decimal(60_000),
            symbol_exposure_usd=Decimal(0),
            gross_exposure_usd=Decimal(0),
            net_exposure_usd=Decimal(0),
            position_notional_usd=Decimal(0),
            maintenance_margin_usd=Decimal(0),
            deployed_capital_usd=Decimal(0),
        ),
        "quote": QuoteEvidence(bid=Decimal("399.98"), ask=Decimal("400.02")),
        "contract": UnderlyingContract(con_id=111, symbol="SPY"),
        "reference_price": Decimal(400),
        "multiplier": Decimal(1),
    }
    base.update(overrides)
    return CycleFacts(**base)


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


def _activate(session: Session, mandate: AutonomyMandate) -> None:
    durable.activate(
        session,
        account_fingerprint=_FINGERPRINT,
        mandate=mandate,
        owner_event_id="owner-event-1",
        now=_NOW,
        process_generation=7,
    )


def _run(session: Session, handoff: Any, **overrides: Any) -> Any:
    """One full cycle whose handoff answers with ``handoff``."""

    mandate = overrides.pop("mandate", None) or _mandate()
    _activate(session, mandate)
    return run_cycle(
        overrides.pop("payload", None) or _proposal(),
        session=session,
        mandate=mandate,
        identity=_identity(),
        facts=overrides.pop("facts", None) or _facts(),
        submit=handoff,
        commit_before_handoff=session.commit,
        **overrides,
    )


def _counted(session: Session) -> int:
    return durable.load_counters(
        session, account_fingerprint=_FINGERPRINT, now=_NOW
    ).orders_submitted


def _alerts(session: Session) -> list[AutonomyOwnerAlertRow]:
    return list(
        session.scalars(
            select(AutonomyOwnerAlertRow).where(
                AutonomyOwnerAlertRow.account_fingerprint == _FINGERPRINT
            )
        ).all()
    )


def _journaled(session: Session) -> dict[str, Any]:
    """The last autonomy-cycle record, as the journal actually stored it."""

    rows = list(
        session.scalars(
            select(HashChainRow)
            .where(HashChainRow.stream == durable.stream_for("autonomy.cycles", _FINGERPRINT))
            .order_by(HashChainRow.sequence)
        ).all()
    )
    assert rows, "every cycle must journal, refusals included"
    payload: dict[str, Any] = dict(hash_chain.payload_of(rows[-1]))
    return payload


def _submission(lifecycle: OrderLifecycle) -> OrderSubmission:
    return OrderSubmission(
        correlation_id="corr-1",
        broker_order_id=4242,
        client_id=7,
        lifecycle=lifecycle,
        submitted_at=_NOW,
    )


# ------------------------------------- each class journals its own outcome
#
# The refusing cases come first (§4d): in a fail-closed system the spurious
# "everything is fine" is the only outcome that can hurt.


def test_a_refusal_before_the_wire_journals_handoff_and_counts_nothing(
    session: Session,
) -> None:
    """The defect, in the direction that spent budget on orders that never were.

    A read-only lease is the order plane's gate 1: it is *returned*, not raised,
    and its own detail says nothing was sent. Before A1 this journaled COMPLETE
    and incremented ``orders_submitted``.
    """

    outcome = _run(
        session,
        lambda intent: HandoffResult.refused_not_sent(
            order_plane_code="READ_ONLY_LEASE",
            detail="this backend is read-only",
        ),
    )

    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "ORDER_PLANE_REFUSED_NOT_SENT"
    assert "READ_ONLY_LEASE" in outcome.detail
    assert _counted(session) == 0, "a refusal before the wire attempted nothing"
    assert outcome.counted_activity_attempt is False
    assert _alerts(session) == [], "an ordinary refusal is journaled, not alerted"

    record = _journaled(session)
    assert record["stage"] == "HANDOFF"
    assert record["refusal"] == "ORDER_PLANE_REFUSED_NOT_SENT"
    assert record["handoff_disposition"] == "REFUSED_NOT_SENT"
    assert record["handoff_counted_attempt"] is False
    assert record["order_plane_code"] == "READ_ONLY_LEASE"


def test_an_ambiguous_send_journals_its_own_stage_counts_and_alerts_critical(
    session: Session,
) -> None:
    """Bytes may be gone. That is the one outcome the owner must be told about.

    Resolving an ambiguous send is an owner act (VISION_COMPLETION_PLAN §11), so
    the system's only correct move is to say so — loudly, durably, and while
    counting the attempt, because the venue may already hold the order.
    """

    outcome = _run(
        session,
        lambda intent: HandoffResult.sent_ambiguous(
            order_plane_code="BROKER_SUBMIT_FAILED",
            detail="broker refused or failed the submission",
        ),
    )

    assert outcome.stage is CycleStage.SENT_UNCONFIRMED
    assert outcome.refusal == "ORDER_PLANE_SEND_AMBIGUOUS"
    assert _counted(session) == 1, "an unknown wire state must be assumed spent"
    assert outcome.counted_activity_attempt is True

    raised = _alerts(session)
    assert [row.kind for row in raised] == ["autonomy.send_unconfirmed"]
    assert raised[0].severity == "CRITICAL"
    assert "reconcile" in raised[0].summary
    assert "autonomy.send_unconfirmed" in outcome.alerts_raised

    record = _journaled(session)
    assert record["stage"] == "SENT_UNCONFIRMED"
    assert record["handoff_disposition"] == "SENT_AMBIGUOUS"
    assert record["handoff_counted_attempt"] is True


def test_a_venue_rejection_after_the_send_journals_its_own_stage_and_warns(
    session: Session,
) -> None:
    """Sent, answered, not working: counts as an attempt, needs no reconciliation."""

    outcome = _run(
        session,
        lambda intent: HandoffResult.rejected_after_send(
            order_plane_code="BROKER_SUBMIT_FAILED",
            detail="broker acknowledged the send as REJECTED",
        ),
    )

    assert outcome.stage is CycleStage.REJECTED_AFTER_SEND
    assert outcome.refusal == "ORDER_PLANE_REJECTED_AFTER_SEND"
    assert _counted(session) == 1
    raised = _alerts(session)
    assert [row.kind for row in raised] == ["autonomy.rejected_after_send"]
    assert raised[0].severity == "WARNING"
    assert _journaled(session)["handoff_disposition"] == "REJECTED_AFTER_SEND"


def test_an_untyped_handoff_answer_is_ambiguous_never_complete(session: Session) -> None:
    """ "The handoff did not say" and "the order is confirmed" must never merge.

    ``run_cycle`` takes a plain callable, so an integration that has not written
    a translation can hand back anything. The old code read every such answer as
    success. This is the fail-closed reading, with its own refusal code so the
    journal does not pretend the order plane reported an ambiguity it never did.
    """

    outcome = _run(session, lambda intent: "accepted-by-order-plane")

    assert outcome.stage is CycleStage.SENT_UNCONFIRMED
    assert outcome.refusal == UNTYPED_RESULT_CODE
    assert _counted(session) == 1
    assert [row.kind for row in _alerts(session)] == ["autonomy.send_unconfirmed"]
    # The raw answer is still carried, unread.
    assert outcome.handoff == "accepted-by-order-plane"


def test_a_none_answer_is_ambiguous_and_not_read_as_a_refusal(session: Session) -> None:
    """Absent evidence is not evidence of absence.

    A duck-typed ``getattr(result, "submitted", None)`` would read ``None`` as
    falsey and record a *refusal* — silently un-counting an order that may exist.
    """

    outcome = _run(session, lambda intent: None)

    assert outcome.stage is CycleStage.SENT_UNCONFIRMED
    assert outcome.refusal == UNTYPED_RESULT_CODE
    assert _counted(session) == 1


def test_a_confirmed_send_still_journals_complete_and_counts(session: Session) -> None:
    """The one unchanged path: a confirmed order is COMPLETE and spends an attempt."""

    outcome = _run(session, lambda intent: HandoffResult.submitted(detail="working at the venue"))

    assert outcome.stage is CycleStage.COMPLETE
    assert outcome.refusal == ""
    assert _counted(session) == 1
    assert _alerts(session) == [], "a clean submission is not an alert"
    assert _journaled(session)["handoff_disposition"] == "SUBMITTED"


def test_an_exception_out_of_the_handoff_refuses_and_keeps_the_reservation(
    session: Session,
) -> None:
    """The refusal is unchanged; what it costs is not (ADR-0052).

    Before ADR-0052 a raise out of the handoff counted nothing, which was only
    safe while the raise proved the wire stayed quiet — and ``loop.py`` has always
    disclosed that it does not: a handoff that raises *after* transmitting is
    recorded here as not-sent. Now the attempt is reserved before the handoff and
    released only on positive proof of a quiet wire, so an exception keeps the
    reservation. Over-counting narrows the mandate's own authority; under-counting
    hands back budget the venue may already hold.

    The journal side of the pre-A1 path is untouched, deliberately: same stage,
    same refusal code, same rule that the exception's message is never recorded.
    """

    def _raising(intent: Any) -> Any:
        raise RuntimeError("kill switch engaged")

    outcome = _run(session, _raising)

    assert outcome.stage is CycleStage.HANDOFF
    assert outcome.refusal == "ORDER_PLANE_REFUSED"
    assert "RuntimeError" in outcome.detail
    assert "kill switch engaged" not in outcome.detail, "the message is never journaled"
    assert outcome.handoff_result is None
    assert _counted(session) == 1, "an ambiguous raise is never auto-refunded"


def test_the_activity_ceiling_survives_a_refusal_and_binds_on_a_real_attempt(
    session: Session,
) -> None:
    """The counting rule, end to end against a ceiling of one.

    A refusal before the wire must leave the budget intact — otherwise a
    read-only backend spends the day's single opening order on nothing — and the
    next real attempt must still be the one that exhausts it.
    """

    from chronos.autonomy import ActivityLimits

    mandate = _mandate(activity=ActivityLimits(max_orders_per_session=1))
    refused = _run(
        session,
        lambda intent: HandoffResult.refused_not_sent(order_plane_code="MODE_FORBIDS"),
        mandate=mandate,
    )
    assert refused.stage is CycleStage.HANDOFF
    assert _counted(session) == 0

    # Budget intact: a different trade still gets judged and reaches the plane.
    sent = run_cycle(
        _proposal(requested_quantity=Decimal(5)),
        session=session,
        mandate=mandate,
        identity=_identity(),
        facts=_facts(),
        submit=lambda intent: HandoffResult.submitted(),
        commit_before_handoff=session.commit,
    )
    assert sent.stage is CycleStage.COMPLETE
    assert _counted(session) == 1

    # And now the ceiling binds, as it always did.
    exhausted = run_cycle(
        _proposal(requested_quantity=Decimal(3)),
        session=session,
        mandate=mandate,
        identity=_identity(),
        facts=_facts(),
        submit=lambda intent: HandoffResult.submitted(),
        commit_before_handoff=session.commit,
    )
    assert exhausted.stage is CycleStage.ADMISSION
    assert exhausted.refusal == "DEGRADED_RISK_REDUCTION_ONLY"


# ------------------------------------------- the app-plane translation seam


def test_every_submission_refusal_code_is_classified() -> None:
    """A new order-plane refusal must be classified, not defaulted into silence.

    This is the guard that keeps the seam honest as ``chronos.orders`` grows: the
    order plane owns the refusal vocabulary, and an unlisted member would fall to
    the fail-closed branch and be reported as *possibly sent* forever without
    anyone deciding that.
    """

    unclassified = [
        code.value
        for code in SubmissionRefusalCode
        if code not in autonomy_wiring._PROVABLY_NOT_SENT
    ]
    assert unclassified == [], (
        "classify these refusal codes in autonomy_wiring._PROVABLY_NOT_SENT as "
        f"provably-not-sent (True) or possibly-sent (False): {unclassified}"
    )


def test_the_classification_matches_what_the_order_plane_says_it_did() -> None:
    """Guard the guard: the table's claims must match the boundary's own words.

    Every code marked provably-not-sent is returned by a gate whose detail text
    says nothing was sent, or is ``BROKER_REFUSED_BEFORE_SEND`` (ADR-0009 §6 —
    the adapter refused locally before any network send). The one code marked
    possibly-sent is the one the boundary returns from around the transmit call.
    A table that drifted into marking ``BROKER_SUBMIT_FAILED`` as not-sent would
    silently un-count real orders, so it is asserted directly.
    """

    assert autonomy_wiring._PROVABLY_NOT_SENT[SubmissionRefusalCode.BROKER_SUBMIT_FAILED] is False
    assert (
        autonomy_wiring._PROVABLY_NOT_SENT[SubmissionRefusalCode.BROKER_REFUSED_BEFORE_SEND] is True
    )
    possibly_sent = [
        code.value for code, not_sent in autonomy_wiring._PROVABLY_NOT_SENT.items() if not not_sent
    ]
    assert possibly_sent == ["BROKER_SUBMIT_FAILED"], (
        "exactly one refusal code leaves the wire state unknown; a second one "
        "means the boundary grew a new post-transmit refusal that needs its own "
        "evidence-based split in classify_submission_outcome"
    )


@pytest.mark.parametrize(
    "code",
    [
        code
        for code in SubmissionRefusalCode
        if code is not SubmissionRefusalCode.BROKER_SUBMIT_FAILED
    ],
)
def test_every_pre_wire_refusal_translates_to_refused_not_sent(
    code: SubmissionRefusalCode,
) -> None:
    """Each of the boundary's returned refusals, one by one, spends no attempt."""

    result = autonomy_wiring.classify_submission_outcome(
        SubmissionOutcome(submitted=False, refusal=code, detail="refused")
    )
    assert result.disposition is HandoffDisposition.REFUSED_NOT_SENT
    assert result.counts_activity_attempt is False
    assert result.order_plane_code == code.value


def test_a_submitted_outcome_translates_to_submitted() -> None:
    outcome = SubmissionOutcome(
        submitted=True,
        refusal=SubmissionRefusalCode.NOT_REFUSED,
        submission=_submission(OrderLifecycle.SUBMITTED),
        detail="working",
    )
    result = autonomy_wiring.classify_submission_outcome(outcome)
    assert result.disposition is HandoffDisposition.SUBMITTED
    assert result.counts_activity_attempt is True
    assert result.requires_owner_alert is False
    assert result.raw is outcome


def test_broker_submit_failed_without_a_submission_is_ambiguous() -> None:
    """Shape 1: ``BrokerError`` out of the transmit call. Bytes may have left."""

    result = autonomy_wiring.classify_submission_outcome(
        SubmissionOutcome(
            submitted=False,
            refusal=SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
            detail="broker refused or failed the submission",
        )
    )
    assert result.disposition is HandoffDisposition.SENT_AMBIGUOUS
    assert result.requires_owner_alert is True


@pytest.mark.parametrize(
    "lifecycle",
    [
        OrderLifecycle.SUBMITTED,
        OrderLifecycle.PARTIALLY_FILLED,
        OrderLifecycle.FILLED,
        OrderLifecycle.CANCEL_PENDING,
    ],
)
def test_an_unpersisted_acknowledgement_of_a_live_order_is_ambiguous(
    lifecycle: OrderLifecycle,
) -> None:
    """Shape 2: the send completed and Chronos lost the record of it.

    The broker acknowledged something that can still fill, and this process
    cannot track it. Reading that as a clean rejection is the most dangerous
    available interpretation, so it alerts as an unconfirmed send.
    """

    result = autonomy_wiring.classify_submission_outcome(
        SubmissionOutcome(
            submitted=False,
            refusal=SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
            submission=_submission(lifecycle),
            detail="broker send completed but its acknowledgement could not be persisted",
        )
    )
    assert result.disposition is HandoffDisposition.SENT_AMBIGUOUS
    assert result.requires_owner_alert is True


@pytest.mark.parametrize(
    "lifecycle",
    [OrderLifecycle.REJECTED, OrderLifecycle.CANCELLED, OrderLifecycle.SUBMISSION_UNKNOWN],
)
def test_an_acknowledged_non_active_order_is_rejected_after_send(
    lifecycle: OrderLifecycle,
) -> None:
    """Shape 3: the venue answered, and what it holds cannot act."""

    result = autonomy_wiring.classify_submission_outcome(
        SubmissionOutcome(
            submitted=False,
            refusal=SubmissionRefusalCode.BROKER_SUBMIT_FAILED,
            submission=_submission(lifecycle),
            detail="broker acknowledged the send",
        )
    )
    assert result.disposition is HandoffDisposition.REJECTED_AFTER_SEND
    assert result.counts_activity_attempt is True
    assert lifecycle.value in result.detail


def test_a_raise_from_the_submission_call_is_ambiguous_not_a_clean_refusal() -> None:
    """A raise mid-submit does not prove the wire stayed quiet.

    Risk and confirmation refusals still raise — they happen strictly before the
    wire — but the submit call itself is the one place where an exception could
    have escaped after ``transmit=True``, so it is reported as possibly sent.
    """

    class _Raising:
        def propose(self, intent: object, *, now: datetime) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(risk=SimpleNamespace(approved=True, decision_id="r-1"))

        def preview(self, intent: object, *, now: datetime) -> object:
            return object()

        def confirm(self, intent: object, *, risk_decision_id: str, now: datetime) -> object:
            return object()

        def submit(self, intent: object, *, writer_lease_held: bool, now: datetime) -> Any:
            raise RuntimeError("the database went away mid-submit")

    from types import SimpleNamespace

    handoff = autonomy_wiring.order_plane_handoff(
        SimpleNamespace(order_management=_Raising()), is_writer=lambda: True
    )
    result = handoff(object())

    assert result.disposition is HandoffDisposition.SENT_AMBIGUOUS
    assert result.refusal_code == SUBMIT_RAISED_CODE
    assert "RuntimeError" in result.detail
    assert "the database went away" not in result.detail, "never journal the message"


# ------------------------------------------------- the rule, pinned in one place


def test_the_counting_rule_is_exactly_the_documented_one() -> None:
    """One statement of the rule, asserted rather than described.

    If someone widens this set, this test is where the argument has to be made —
    which is the point: the counting rule is a safety mechanism, and
    ``REFUSED_NOT_SENT`` joining it would silently restore the old defect.
    """

    assert set(COUNTS_ACTIVITY_ATTEMPT) == {
        HandoffDisposition.SUBMITTED,
        HandoffDisposition.SENT_AMBIGUOUS,
        HandoffDisposition.REJECTED_AFTER_SEND,
    }
    assert HandoffDisposition.REFUSED_NOT_SENT not in COUNTS_ACTIVITY_ATTEMPT
    assert set(REQUIRES_OWNER_ALERT) == {
        HandoffDisposition.SENT_AMBIGUOUS,
        HandoffDisposition.REJECTED_AFTER_SEND,
    }


def test_every_disposition_has_a_stage_a_refusal_code_and_a_counting_verdict() -> None:
    """No disposition may be added without deciding all three of its consequences."""

    from chronos.supervisor.loop import _HANDOFF_STAGE

    for disposition in HandoffDisposition:
        assert disposition in _HANDOFF_STAGE, f"{disposition} journals no stage"
        assert disposition in DEFAULT_REFUSAL_CODES, f"{disposition} has no refusal code"
    # Distinct stages per class: a shared stage would merge two answers in the
    # journal, which is the defect this work exists to remove.
    assert len(set(_HANDOFF_STAGE.values())) == len(HandoffDisposition)
    # Only the confirmed class is allowed an empty refusal code.
    empty = {d for d, code in DEFAULT_REFUSAL_CODES.items() if not code}
    assert empty == {HandoffDisposition.SUBMITTED}


def test_classify_passes_a_typed_result_through_unchanged() -> None:
    """Non-vacuity: the classifier is not simply answering "ambiguous" always."""

    typed = HandoffResult.refused_not_sent(order_plane_code="MODE_FORBIDS")
    assert classify(typed) is typed
    assert classify(object()).disposition is HandoffDisposition.SENT_AMBIGUOUS


def test_journal_refusal_codes_fit_the_columns_that_store_them() -> None:
    """The queue row stores stage in 32 chars and refusal in 64 (schema.py).

    A longer name would be truncated into a different word, and the truncated
    form is what an operator would then search for and never find.
    """

    for stage in CycleStage:
        assert len(stage.value) <= 32, stage
    for code in DEFAULT_REFUSAL_CODES.values():
        assert len(code) <= 64, code
    for code in (UNTYPED_RESULT_CODE, SUBMIT_RAISED_CODE):
        assert len(code) <= 64, code


def test_the_handoff_module_imports_nothing_that_can_act() -> None:
    """Structural: the supervisor's result type must not reach the order plane.

    ``CycleOutcome.handoff`` is untyped precisely so the supervisor cannot import
    ``SubmissionOutcome``; giving the supervisor a *typed* result would be a
    hollow win if the type were obtained by importing the plane it exists to stay
    out of. The broader ban is pinned by ``test_autonomy_contracts.py``; this
    asserts it for the new module specifically.
    """

    from chronos.supervisor import handoff as handoff_module

    tree = ast.parse(inspect.getsource(handoff_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = [
        name
        for name in imported
        if name.startswith(
            ("chronos.orders", "chronos.execution", "chronos.api", "ib_async", "ibapi")
        )
    ]
    assert forbidden == [], f"the supervisor's handoff vocabulary must own itself: {forbidden}"
