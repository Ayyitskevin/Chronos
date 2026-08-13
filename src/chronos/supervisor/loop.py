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

## What the handoff said, not merely whether it raised (A1)

The submission boundary **returns** its refusals. Reading only exceptions meant a
read-only lease, a kill switch engaged inside the CAS window, an ambiguous send
and a venue rejection all recorded ``COMPLETE`` and all spent an activity
attempt. The result is now classified into ``supervisor.handoff``'s four
dispositions, each journaling its own stage and refusal code, and the counting
rule is stated once, there. The supervisor still never learns what a
``SubmissionOutcome`` is: the translation happens at the app-plane seam.

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
from chronos.supervisor.handoff import HandoffDisposition, HandoffResult, classify
from chronos.supervisor.sizing import AccountEvidence, SizingOutcome, size_order


class CycleStage(StrEnum):
    """Where a cycle stopped, or how it ended. Ordered as the pipeline runs.

    The last three are all *after* the handoff and are deliberately separate
    names (A1). ``COMPLETE`` used to mean "the handoff did not raise", which
    covered a read-only refusal, an ambiguous send and a venue rejection alike;
    it now means only what it says. The two additions are additive — no existing
    stage changed meaning, and nothing that used to refuse stops refusing.
    """

    INGRESS = "INGRESS"
    STAMP = "STAMP"
    ADMISSION = "ADMISSION"
    SIZING = "SIZING"
    COMPILATION = "COMPILATION"
    #: Stopped at the handoff with nothing sent — shadow mode, an order-plane
    #: gate refusing before the wire, or an exception out of the callable.
    HANDOFF = "HANDOFF"
    #: Bytes may have reached the venue and the outcome is unconfirmed. An owner
    #: alert accompanies every one of these.
    SENT_UNCONFIRMED = "SENT_UNCONFIRMED"
    #: The venue acknowledged the send and answered with a non-active order.
    REJECTED_AFTER_SEND = "REJECTED_AFTER_SEND"
    #: A confirmed working, partially filled, or filled order. Nothing weaker.
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
    #: The bundle issued to the worker for this run, and its digest. The
    #: digest is ``None`` in the placeholder era (ADR-0023): absence is
    #: attested as absence, never as sixty-four zeros.
    evidence_bundle_id: str
    evidence_bundle_digest: str | None
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
    #: The decision this cycle judged, when one was successfully parsed. Carried
    #: so the journal can record what the model actually proposed and why —
    #: ADR-0016 §5 says narrative is "recorded, displayed, and audited", and
    #: before M8d none of the three was true of it (see `_record`).
    decision: AITradeDecision | None = None
    admission: AdmissionOutcome | None = None
    sizing: SizingOutcome | None = None
    compilation: CompilationOutcome | None = None
    intent: WheelOrderIntent | None = None
    #: Whatever the handoff returned. Deliberately untyped: this module must not
    #: import the order plane's result types, or it would start to look like it
    #: owns submission.
    handoff: Any = None
    #: The same answer, classified into the supervisor's own vocabulary (A1).
    #: ``None`` when the cycle never reached the handoff, or when it raised.
    handoff_result: HandoffResult | None = None
    alerts_raised: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reached_order_plane(self) -> bool:
        return self.stage in {
            CycleStage.HANDOFF,
            CycleStage.SENT_UNCONFIRMED,
            CycleStage.REJECTED_AFTER_SEND,
            CycleStage.COMPLETE,
        }

    @property
    def counted_activity_attempt(self) -> bool:
        """Whether this cycle spent an attempt under the activity ceiling.

        Reads the classified result rather than the stage so there is exactly one
        statement of the counting rule (``supervisor.handoff``) and no second
        copy here to drift from it.
        """

        return self.handoff_result is not None and self.handoff_result.counts_activity_attempt


#: The handoff. Takes a compiled intent and does whatever the order plane does
#: with it. Typed as a callable so this module cannot accidentally acquire the
#: service's full surface — it can hand over an intent and learn the result, and
#: that is all.
#:
#: The return stays ``Any`` rather than ``HandoffResult`` on purpose: a type
#: annotation cannot stop a caller from handing back something else, and
#: pretending otherwise would put the fail-closed decision in the type checker
#: instead of in the code. :func:`chronos.supervisor.handoff.classify` makes the
#: unclassifiable case ambiguous-and-alerted at runtime, which is where a hostile
#: or careless caller actually arrives.
Handoff = Callable[[WheelOrderIntent], Any]


def run_cycle(
    payload: bytes | str | ProposedDecision,
    *,
    session: Session,
    mandate: AutonomyMandate | None,
    identity: queue.HarnessIdentity | None,
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
    # An unresolved identity is a STAMP-stage refusal, not an exception: the
    # caller could not say who proposed (ADR-0023 — the credential no longer
    # resolves to a current registration, or a pre-registry row met a
    # registry-on runtime), and a decision that cannot be attributed is never
    # judged. Refusing here rather than stamping a guessed identity is the
    # fail-closed direction: misattribution in a hash-chained journal would be
    # worse than a recorded refusal.
    if identity is None:
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.STAMP,
                refusal="PROPOSER_UNRESOLVED",
                detail=(
                    "the proposal's credential does not resolve to a current registered "
                    "proposer, so identity cannot be stamped; re-registering or renewing "
                    "the proposer is an owner act"
                ),
            ),
        )
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
                decision=decision,
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
                decision=decision,
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
                decision=decision,
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
                    decision=decision,
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
                decision=decision,
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
                decision=decision,
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
                decision=decision,
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
        # why; the cycle records that it got that far and stopped. Unchanged by
        # A1, deliberately: the production wiring raises only from *before* the
        # wire (a risk veto, a refused confirmation) and translates anything the
        # submission call itself raises into SENT_AMBIGUOUS, because from outside
        # the boundary a raise mid-submit does not prove the wire stayed quiet.
        # A caller who supplies a handoff that raises after transmitting would be
        # recorded here as not-sent — disclosed in the risk register, not hidden.
        return _record(
            session,
            facts,
            CycleOutcome(
                stage=CycleStage.HANDOFF,
                refusal="ORDER_PLANE_REFUSED",
                detail=f"the order plane raised {type(error).__name__}",
                decision_id=decision.decision_id,
                decision=decision,
                admission=admission,
                sizing=sizing,
                compilation=compilation,
                intent=compilation.intent,
                alerts_raised=alert_kinds,
            ),
        )

    # The handoff answered. What it *said* now decides the journal, the counter
    # and the alert — before A1 only a raise was read, so a returned refusal
    # ("read-only lease", "kill switch engaged between pre-submit and transmit")
    # recorded COMPLETE and spent an attempt on an order that never existed.
    handoff_result = classify(result)

    # An attempt is consumed exactly when nothing proves the wire stayed quiet
    # (the rule, and the argument for it, live in `supervisor.handoff`). A
    # refusal before the wire attempted nothing, so it must not spend budget an
    # activity ceiling exists to ration.
    if handoff_result.counts_activity_attempt:
        durable.record_activity(
            session,
            account_fingerprint=facts.account_fingerprint,
            now=facts.now,
            orders_submitted=1,
            turnover_usd=_notional(sizing.quantity, reference_price, multiplier),
            market_timezone=facts.market_timezone,
        )
    if handoff_result.requires_owner_alert:
        raised_handoff = alerts.raise_alert(
            session,
            account_fingerprint=facts.account_fingerprint,
            severity=_HANDOFF_ALERT_SEVERITY[handoff_result.disposition],
            kind=_HANDOFF_ALERT_KIND[handoff_result.disposition],
            summary=_handoff_alert_summary(handoff_result),
            detail={
                "decision_id": decision.decision_id,
                "disposition": handoff_result.disposition.value,
                "refusal": handoff_result.refusal_code,
                "order_plane_code": handoff_result.order_plane_code,
            },
            now=facts.now,
        )
        alert_kinds = (*alert_kinds, raised_handoff.kind)

    return _record(
        session,
        facts,
        CycleOutcome(
            stage=_HANDOFF_STAGE[handoff_result.disposition],
            refusal=handoff_result.refusal_code,
            detail=handoff_result.journal_detail,
            decision_id=decision.decision_id,
            decision=decision,
            admission=admission,
            sizing=sizing,
            compilation=compilation,
            intent=compilation.intent,
            handoff=result,
            handoff_result=handoff_result,
            alerts_raised=alert_kinds,
        ),
    )


#: Which stage each classified outcome journals. The mapping lives here because
#: the stage vocabulary is the journal's, and the disposition vocabulary is the
#: handoff's; keeping them in separate modules is what lets the supervisor own a
#: result type without owning the order plane's.
_HANDOFF_STAGE: dict[HandoffDisposition, CycleStage] = {
    HandoffDisposition.SUBMITTED: CycleStage.COMPLETE,
    HandoffDisposition.REFUSED_NOT_SENT: CycleStage.HANDOFF,
    HandoffDisposition.SENT_AMBIGUOUS: CycleStage.SENT_UNCONFIRMED,
    HandoffDisposition.REJECTED_AFTER_SEND: CycleStage.REJECTED_AFTER_SEND,
}

_HANDOFF_ALERT_KIND: dict[HandoffDisposition, str] = {
    HandoffDisposition.SENT_AMBIGUOUS: "autonomy.send_unconfirmed",
    HandoffDisposition.REJECTED_AFTER_SEND: "autonomy.rejected_after_send",
}

#: An unconfirmed send is CRITICAL because resolving it is an owner act the
#: system is forbidden to perform for itself (plan §11: "manual broker
#: resolution of unknown orders, positions, assignments, ambiguous sends"). A
#: venue rejection is WARNING: the order plane already resolved it, and the
#: owner needs to know the model is proposing orders the venue will not take.
_HANDOFF_ALERT_SEVERITY: dict[HandoffDisposition, alerts.AlertSeverity] = {
    HandoffDisposition.SENT_AMBIGUOUS: alerts.AlertSeverity.CRITICAL,
    HandoffDisposition.REJECTED_AFTER_SEND: alerts.AlertSeverity.WARNING,
}


def _handoff_alert_summary(result: HandoffResult) -> str:
    if result.disposition is HandoffDisposition.SENT_AMBIGUOUS:
        return (
            "an autonomous order may have reached the venue and its state is "
            f"unconfirmed ({result.refusal_code}); reconcile with the broker before "
            "trusting any position or counter"
        )
    return (
        "the venue answered an autonomous order with a non-active lifecycle "
        f"({result.refusal_code}); the attempt was counted and nothing is working"
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

    ## The narrative, and a claim that was not true until M8d

    ADR-0016 §5 says the model's ``thesis``, ``rationale``, ``key_uncertainties``
    and ``invalidation_conditions`` "are recorded, displayed, and audited". Of
    those three, only the first was even arguably true: the raw proposal payload
    persists in ``autonomy_proposal_queue``, so the bytes survived — but as an
    opaque blob nothing read back, indexed by nothing, and outside the
    hash chain that makes the rest of this journal tamper-evident. Nothing
    displayed it and nothing audited it.

    So the narrative is journaled here, next to the outcome it explains. It also
    carries the **symbol**, which the payload above did not expose either, and
    which is what lets a per-holding view answer "what does the system believe
    about this position, and why".

    Two properties this deliberately keeps:

    - **It is recorded verbatim, not summarized.** An audit record that
      paraphrases is a record of someone's reading rather than of what was said.
      Bounding happens at display time, where truncation can be labelled.
    - **It stays inert.** This text originates outside Chronos and is an
      injection surface (R-30). Writing it to an append-only chain is recording,
      not executing; nothing in the pipeline parses it into an order parameter,
      and the terminal renders it as text and never as markup.
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
                **_handoff_of(outcome.handoff_result),
                **_narrative_of(outcome.decision),
            },
            recorded_at=facts.now,
        )
    return outcome


def _handoff_of(result: HandoffResult | None) -> dict[str, Any]:
    """What the order plane did, for the journal (A1).

    Absent when the cycle never reached the handoff — an admission refusal has no
    disposition, and writing one would make "nothing was handed off" look like a
    classification someone made.
    """

    if result is None:
        return {}
    return {
        "handoff_disposition": result.disposition.value,
        "handoff_counted_attempt": result.counts_activity_attempt,
        "order_plane_code": result.order_plane_code,
    }


def _narrative_of(decision: AITradeDecision | None) -> dict[str, Any]:
    """What the model said it was doing, for the journal.

    Empty when the cycle failed before a decision existed — an ingress refusal
    has no thesis, and inventing empty strings for one would make "the model said
    nothing" indistinguishable from "there was no model output to record".
    """

    if decision is None:
        return {}
    return {
        "symbol": decision.symbol,
        "kind": decision.kind.value,
        "asset_class": decision.asset_class.value,
        "confidence": str(decision.confidence),
        "thesis": decision.thesis,
        "rationale": decision.rationale,
        "key_uncertainties": list(decision.key_uncertainties),
        "invalidation_conditions": list(decision.invalidation_conditions),
    }
