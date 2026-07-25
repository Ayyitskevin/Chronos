"""The autonomy cycle: one proposal, all the way through, or refused (M5).

Every previous milestone built a stage and disclosed that nothing called it.
This is the caller. One function, :func:`run_cycle`, walks a proposal through
every gate in order and stops at the first refusal:

    ingress -> stamp -> admit -> size -> compile -> hand off -> record

## What this module does NOT do

**It does not submit.** It hands a compiled ``WheelOrderIntent`` to the existing
:class:`~chronos.orders.service.OrderManagementService`, which then applies every
gate it already applies to a human-proposed order — risk engine, preview,
confirmation, the ten-gate live stack, arming, kill switch, drawdown breaker,
writer lease — and owns the single ``transmit=True`` site. ADR-0016 §8 is
explicit that ``chronos.orders`` stays the single canonical execution plane, and
"the autonomy loop got its own submission path" is exactly how that guarantee
would have died.

So autonomy **adds** a gate stack in front of the existing one. It removes none.
The most this loop can do is cause a proposal that the order plane would have
accepted from a human anyway.

## Non-live by default, structurally

``run_cycle`` takes a ``submit`` callable rather than reaching for one. A caller
that does not supply it gets a full walk — admission, sizing, compilation,
recording — and **no order**, which is precisely SHADOW mode and precisely what
should happen when someone wires this up without thinking hard about the last
step. The mandate's mode is checked independently by admission, so a non-
submitting mode refuses before compilation even runs.

## Why every stage records, including the refusals

The journal is the only thing that can answer "why did it not trade" — a
question that will be asked far more often than its opposite, and one a system
that only logged its actions could never answer. Each cycle appends one
hash-chained record naming the stage that stopped it.

## Failure is a refusal, never an exception

A stage that raises would leave the cycle half-recorded: counters incremented
with no journal entry, or an intent compiled with nothing knowing about it.
Every stage is wrapped, and an unexpected error becomes a refusal with the
exception's type recorded — never its message, which could carry text a hostile
proposal chose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from chronos.autonomy import AITradeDecision, AutonomyMandate, ProposedDecision
from chronos.domain.models import Instrument
from chronos.orders.intent import WheelOrderIntent
from chronos.supervisor import alerts, durable, ingress, queue
from chronos.supervisor.admission import (
    AdmissionOutcome,
    DegradedReason,
    MarketDataEvidence,
    admit,
)
from chronos.supervisor.compiler import CompilationOutcome, QuoteEvidence, compile_order
from chronos.supervisor.sizing import AccountEvidence, SizingOutcome, size_order


class CycleStage(StrEnum):
    """Where a cycle stopped. Ordered as the pipeline runs."""

    INGRESS = "INGRESS"
    STAMP = "STAMP"
    ADMISSION = "ADMISSION"
    SIZING = "SIZING"
    COMPILATION = "COMPILATION"
    HANDOFF = "HANDOFF"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class CycleFacts:
    """Everything the supervisor gathered for this cycle. Never model-supplied.

    Assembling these is the caller's job because gathering them needs a broker,
    a clock, and a market-data feed — none of which may be reachable from the
    modules that judge. Keeping them as an explicit input is what lets the whole
    pipeline be exercised without any of that.
    """

    account_fingerprint: str
    account_id: str
    now: datetime
    process_generation: int
    #: The bundle issued to the worker for this run, and its digest.
    evidence_bundle_id: str
    evidence_bundle_digest: str
    market_data: MarketDataEvidence
    account: AccountEvidence
    quote: QuoteEvidence
    contract: Instrument | None
    reference_price: Decimal
    multiplier: Decimal = Decimal(1)
    #: Degradations the supervisor observed that the counters cannot know about
    #: — an unreachable broker, a stale resolver, a lost lease.
    degraded_reasons: tuple[DegradedReason, ...] = ()
    #: The market whose calendar day bounds this session's counters (R-34).
    #: ``None`` keeps the UTC day, which is right for a UTC-keyed audit and
    #: wrong for a market-local session — so a live deployment sets it.
    market_timezone: str | None = None


@dataclass(frozen=True, slots=True)
class InstrumentFacts:
    """The per-instrument slice of the supervisor's view (M7.5).

    ``CycleFacts`` was built when a cycle judged one proposal, so it carried one
    contract and one quote. A tick that drains a *batch* judges proposals about
    different symbols, and reusing one instrument's quote for another's order
    would price the wrong thing. Account-level truth stays on ``CycleFacts``;
    this is what varies per decision, resolved by a caller-supplied gatherer
    after the decision names its instrument.
    """

    contract: Instrument | None
    quote: QuoteEvidence | None
    reference_price: Decimal
    multiplier: Decimal = Decimal(1)


#: Resolves the instrument slice for one stamped decision, or ``None`` when it
#: cannot — which refuses that decision rather than pricing it with stale or
#: foreign facts. Lives outside the supervisor because resolution needs a
#: broker; the callable is the seam that keeps the broker out of this module.
InstrumentGatherer = Callable[[AITradeDecision], InstrumentFacts | None]


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    """What happened to one proposal, and where it stopped."""

    stage: CycleStage
    refusal: str = ""
    detail: str = ""
    decision_id: str = ""
    admission: AdmissionOutcome | None = None
    sizing: SizingOutcome | None = None
    compilation: CompilationOutcome | None = None
    intent: WheelOrderIntent | None = None
    #: Whatever the handoff returned. Deliberately untyped: this module must not
    #: import the order plane's result types, or it would start to look like it
    #: owns submission.
    handoff: Any = None
    alerts_raised: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reached_order_plane(self) -> bool:
        return self.stage in {CycleStage.HANDOFF, CycleStage.COMPLETE}


#: The handoff. Takes a compiled intent and does whatever the order plane does
#: with it. Typed as a callable so this module cannot accidentally acquire the
#: service's full surface — it can hand over an intent and learn the result, and
#: that is all.
Handoff = Callable[[WheelOrderIntent], Any]


def run_cycle(
    payload: bytes | str | ProposedDecision,
    *,
    session: Session,
    mandate: AutonomyMandate | None,
    identity: queue.HarnessIdentity,
    facts: CycleFacts,
    submit: Handoff | None = None,
    gather_instrument: InstrumentGatherer | None = None,
) -> CycleOutcome:
    """Walk one proposal through every gate. Stops at the first refusal.

    ``payload`` is bytes from an external worker in production, and may be an
    already-parsed :class:`ProposedDecision` in tests — the ingress stage is
    skipped in that case, which is safe because the type it produces is the same
    one the parser produces.

    ``submit`` is optional **on purpose**. Omitting it is SHADOW: the full walk
    runs and no order is placed. A caller that has not thought about the last
    step gets the safe behavior rather than a surprise.
    """

    # --- ingress -----------------------------------------------------------
    if isinstance(payload, ProposedDecision):
        proposal = payload
    else:
        parsed = ingress.parse_proposal(payload)
        if parsed.proposal is None:
            return _record(
                session,
                facts,
                CycleOutcome(stage=CycleStage.INGRESS, refusal=parsed.refusal),
            )
        proposal = parsed.proposal

    # --- stamp -------------------------------------------------------------
    try:
        decision = queue.accept(
            proposal,
            identity=identity,
            produced_at=facts.now,
            session=session,
            account_fingerprint=facts.account_fingerprint,
        )
    except queue.ProposalRejected as error:
        return _record(session, facts, CycleOutcome(stage=CycleStage.STAMP, refusal=str(error)))

    # --- admission ---------------------------------------------------------
    # No mandate is a refusal, and it is checked here rather than inside
    # `build_state` because assembling state for an authority that does not
    # exist is meaningless — there are no limits to read counters against.
    if mandate is None:
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.ADMISSION,
                refusal="NO_ACTIVE_MANDATE",
                detail="no AutonomyMandate is in force; the model has no trade-time authority",
                decision_id=decision.decision_id,
            ),
        )
    try:
        state = durable.build_state(
            session,
            account_fingerprint=facts.account_fingerprint,
            mandate=mandate,
            now=facts.now,
            process_generation=facts.process_generation,
            expected_evidence_bundle_id=facts.evidence_bundle_id,
            expected_evidence_bundle_digest=facts.evidence_bundle_digest,
            market_data=facts.market_data,
            extra_degraded_reasons=facts.degraded_reasons,
            market_timezone=facts.market_timezone,
        )
        admission = admit(decision, mandate, state)
    except Exception as error:  # a gate that raises must not strand the cycle
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.ADMISSION,
                refusal="ADMISSION_FAILED",
                detail=f"admission raised {type(error).__name__}",
                decision_id=decision.decision_id,
            ),
        )

    durable.record_outcome(
        session,
        account_fingerprint=facts.account_fingerprint,
        decision_id=decision.decision_id,
        outcome=admission,
        now=facts.now,
    )
    raised = alerts.alert_for_refusal(
        session,
        account_fingerprint=facts.account_fingerprint,
        decision_id=decision.decision_id,
        outcome=admission,
        now=facts.now,
    )
    alert_kinds = (raised.kind,) if raised is not None else ()

    if not admission.admitted:
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.ADMISSION,
                refusal=admission.refusal.value if admission.refusal else "REFUSED",
                detail=admission.detail,
                decision_id=decision.decision_id,
                admission=admission,
                alerts_raised=alert_kinds,
            ),
        )
    # --- instrument facts (M7.5) -------------------------------------------
    # A batch tick judges proposals about different symbols; pricing one
    # instrument's order with another's quote would trade the wrong number, so
    # the per-instrument slice is resolved per decision when a gatherer is
    # supplied. No gatherer keeps the single-instrument CycleFacts behavior.
    contract: Instrument | None = facts.contract
    quote: QuoteEvidence | None = facts.quote
    reference_price = facts.reference_price
    multiplier = facts.multiplier
    if gather_instrument is not None:
        instrument = gather_instrument(decision)
        if instrument is None:
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SIZING,
                    refusal="INSTRUMENT_FACTS_UNAVAILABLE",
                    detail=(
                        "the supervisor could not resolve a qualified contract and quote "
                        f"for {decision.symbol or decision.futures_root}; a decision is "
                        "never priced with another instrument's facts"
                    ),
                    decision_id=decision.decision_id,
                    admission=admission,
                    alerts_raised=alert_kinds,
                ),
            )
        contract = instrument.contract
        quote = instrument.quote
        reference_price = instrument.reference_price
        multiplier = instrument.multiplier

    # --- sizing ------------------------------------------------------------
    sizing = size_order(
        mandate=mandate,
        decision_kind=decision.kind,
        asset_class=decision.asset_class,
        reference_price=reference_price,
        multiplier=multiplier,
        evidence=facts.account,
        requested_quantity=decision.requested_quantity,
    )
    if sizing.quantity is None:
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.SIZING,
                refusal="NO_EXECUTABLE_SIZE",
                detail=sizing.refusal,
                decision_id=decision.decision_id,
                admission=admission,
                sizing=sizing,
                alerts_raised=alert_kinds,
            ),
        )

    # --- compilation -------------------------------------------------------
    compilation = compile_order(
        decision=decision,
        mandate=mandate,
        contract=contract,
        quantity=sizing.quantity,
        quote=quote,
        account_id=facts.account_id,
    )
    if compilation.intent is None:
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.COMPILATION,
                refusal=compilation.refusal,
                detail=compilation.detail,
                decision_id=decision.decision_id,
                admission=admission,
                sizing=sizing,
                compilation=compilation,
                alerts_raised=alert_kinds,
            ),
        )

    # --- handoff -----------------------------------------------------------
    # No submit callable is SHADOW, and it is the default. The walk completed
    # and produced an intent; nothing was sent.
    if submit is None:
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.HANDOFF,
                refusal="NO_SUBMISSION_CONFIGURED",
                detail="the cycle ran in shadow: an intent was compiled and nothing was sent",
                decision_id=decision.decision_id,
                admission=admission,
                sizing=sizing,
                compilation=compilation,
                intent=compilation.intent,
                alerts_raised=alert_kinds,
            ),
        )

    try:
        result = submit(compilation.intent)
    except Exception as error:
        # The order plane refused or failed. Its own gates are the authority on
        # why; the cycle records that it got that far and stopped.
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.HANDOFF,
                refusal="ORDER_PLANE_REFUSED",
                detail=f"the order plane raised {type(error).__name__}",
                decision_id=decision.decision_id,
                admission=admission,
                sizing=sizing,
                compilation=compilation,
                intent=compilation.intent,
                alerts_raised=alert_kinds,
            ),
        )

    # An order reached the order plane, so this session's activity counter
    # advances. Counting at handoff rather than at fill is deliberate: an
    # activity limit bounds how much the system *attempts*, and an order that
    # was sent and then rejected still consumed an attempt.
    durable.record_activity(
        session,
        account_fingerprint=facts.account_fingerprint,
        now=facts.now,
        orders_submitted=1,
        turnover_usd=_notional(sizing.quantity, reference_price, multiplier),
        market_timezone=facts.market_timezone,
    )
    return _record(
        session,
        facts,
        CycleOutcome(
            stage=CycleStage.COMPLETE,
            decision_id=decision.decision_id,
            admission=admission,
            sizing=sizing,
            compilation=compilation,
            intent=compilation.intent,
            handoff=result,
            alerts_raised=alert_kinds,
        ),
    )


def _notional(quantity: Decimal, price: Decimal, multiplier: Decimal) -> Decimal:
    try:
        return abs(quantity * price * multiplier)
    except ArithmeticError:  # pragma: no cover - guarded upstream by sizing
        return Decimal(0)


def _record(session: Session, facts: CycleFacts, outcome: CycleOutcome) -> CycleOutcome:
    """Append one hash-chained record for this cycle, whatever happened.

    Recording refusals is the point. "Why did it not trade" is asked far more
    often than its opposite, and a system that journaled only its actions could
    never answer it.
    """

    import contextlib

    from chronos.persistence import hash_chain

    # A journal failure is serious, but returning a wrong answer because the
    # journal failed would be worse: the caller still needs to know the cycle
    # refused. The chain's own verification is what surfaces the gap.
    with contextlib.suppress(Exception):
        hash_chain.append(
            session,
            stream=durable.stream_for("autonomy.cycles", facts.account_fingerprint),
            kind=outcome.stage.value,
            payload={
                "stage": outcome.stage.value,
                "refusal": outcome.refusal,
                "detail": outcome.detail,
                "decision_id": outcome.decision_id,
                "quantity": str(outcome.sizing.quantity) if outcome.sizing else None,
                "limit_price": str(outcome.intent.limit_price) if outcome.intent else None,
            },
            recorded_at=facts.now,
        )
    return outcome
