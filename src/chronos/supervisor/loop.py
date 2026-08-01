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

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Text, case, cast, func, literal, select
from sqlalchemy.orm import Session

from chronos.autonomy import (
    LIVE_AUTONOMY_MODES,
    AITradeDecision,
    AutonomyMandate,
    DecisionKind,
    ProposedDecision,
    TradableAssetClass,
)
from chronos.config.limits import (
    MAX_DURABLE_HASH_TEXT_CHARS,
    MAX_DURABLE_KIND_TEXT_CHARS,
    MAX_DURABLE_SEQUENCE_TEXT_CHARS,
    MAX_DURABLE_TIMESTAMP_TEXT_CHARS,
    MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS,
    MAX_OPTION_SELECTION_RECEIPT_BYTES,
)
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
from chronos.supervisor.option_selection import (
    OPTION_SELECTION_STREAM,
    OptionSelectionReceipt,
    SelectionRejectionCode,
    SelectionStatus,
    canonical_digest,
)
from chronos.supervisor.sizing import AccountEvidence, SizingOutcome, size_order


class CycleStage(StrEnum):
    """Where a cycle stopped. Ordered as the pipeline runs."""

    INGRESS = "INGRESS"
    STAMP = "STAMP"
    ADMISSION = "ADMISSION"
    SELECTION = "SELECTION"
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
    #: Required for every autonomous opening equity-option decision. The cycle
    #: persists and verifies it before using the selected contract.
    selection_receipt: OptionSelectionReceipt | None = None
    #: App-plane read-only live-promotion recheck. It has no broker mutation
    #: surface and returns a refusal detail rather than raising authority.
    selection_activation_validator: Callable[[OptionSelectionReceipt], str] | None = None


#: Resolves the instrument slice for one stamped decision, or ``None`` when it
#: cannot — which refuses that decision rather than pricing it with stale or
#: foreign facts. Lives outside the supervisor because resolution needs a
#: broker; the callable is the seam that keeps the broker out of this module.
InstrumentGatherer = Callable[[AITradeDecision], InstrumentFacts | None]

_SYSTEM_SELECTION_REJECTIONS = frozenset(
    {
        SelectionRejectionCode.UNSUPPORTED_REQUEST,
        SelectionRejectionCode.PROMOTION_REQUIRED,
        SelectionRejectionCode.PROMOTION_MISMATCH,
        SelectionRejectionCode.EVIDENCE_UNAVAILABLE,
        SelectionRejectionCode.EVIDENCE_PARTIAL,
        SelectionRejectionCode.EVIDENCE_TRUNCATED,
        SelectionRejectionCode.OBSERVATION_SET_MISMATCH,
        SelectionRejectionCode.PACING_EXHAUSTED,
        SelectionRejectionCode.DATA_PERMISSION_DENIED,
        SelectionRejectionCode.DATA_CLEANUP_FAILED,
        SelectionRejectionCode.UNDERLYING_MISSING,
        SelectionRejectionCode.UNDERLYING_TYPE_MISMATCH,
        SelectionRejectionCode.UNDERLYING_SYMBOL_MISMATCH,
        SelectionRejectionCode.UNDERLYING_CURRENCY_MISMATCH,
        SelectionRejectionCode.UNDERLYING_QUOTE_MISSING,
        SelectionRejectionCode.UNDERLYING_QUOTE_IDENTITY_MISMATCH,
        SelectionRejectionCode.UNDERLYING_QUOTE_FUTURE,
        SelectionRejectionCode.UNDERLYING_QUOTE_STALE,
        SelectionRejectionCode.UNDERLYING_DATA_QUALITY,
        SelectionRejectionCode.UNDERLYING_BOOK_INVALID,
        SelectionRejectionCode.CHAIN_MISSING,
        SelectionRejectionCode.CHAIN_AMBIGUOUS,
        SelectionRejectionCode.CHAIN_INCOMPLETE,
        SelectionRejectionCode.CHAIN_FUTURE,
        SelectionRejectionCode.CHAIN_STALE,
        SelectionRejectionCode.CHAIN_IDENTITY_MISMATCH,
        SelectionRejectionCode.CHAIN_EXCHANGE_NOT_PERMITTED,
        SelectionRejectionCode.CHAIN_FAMILY_NOT_PERMITTED,
        SelectionRejectionCode.CHAIN_MULTIPLIER_MISMATCH,
        SelectionRejectionCode.CHAIN_DUPLICATE_VALUE,
        SelectionRejectionCode.DUPLICATE_CONTRACT_ID,
        SelectionRejectionCode.CONTRACT_TYPE_MISMATCH,
        SelectionRejectionCode.CONTRACT_SYMBOL_MISMATCH,
        SelectionRejectionCode.CONTRACT_UNDERLYING_MISMATCH,
        SelectionRejectionCode.CONTRACT_RIGHT_MISMATCH,
        SelectionRejectionCode.CONTRACT_CURRENCY_MISMATCH,
        SelectionRejectionCode.CONTRACT_EXCHANGE_NOT_PERMITTED,
        SelectionRejectionCode.CONTRACT_FAMILY_NOT_PERMITTED,
        SelectionRejectionCode.CONTRACT_MULTIPLIER_MISMATCH,
        SelectionRejectionCode.CONTRACT_CHAIN_IDENTITY_MISMATCH,
        SelectionRejectionCode.CONTRACT_NOT_IN_CHAIN,
        SelectionRejectionCode.CONTRACT_DETAILS_STALE,
        SelectionRejectionCode.DELTA_MISSING,
        SelectionRejectionCode.DELTA_INVALID,
        SelectionRejectionCode.DELTA_SIGN_MISMATCH,
        SelectionRejectionCode.DELIVERABLE_MISSING,
        SelectionRejectionCode.DELIVERABLE_NOT_AUTHORITATIVE,
        SelectionRejectionCode.SESSION_EVIDENCE_MISSING,
        SelectionRejectionCode.QUOTE_MISSING,
        SelectionRejectionCode.QUOTE_IDENTITY_MISMATCH,
        SelectionRejectionCode.QUOTE_FUTURE,
        SelectionRejectionCode.QUOTE_STALE,
        SelectionRejectionCode.QUOTE_DATA_QUALITY,
        SelectionRejectionCode.QUOTE_ONE_SIDED,
        SelectionRejectionCode.QUOTE_ZERO_BID,
        SelectionRejectionCode.QUOTE_CROSSED,
        SelectionRejectionCode.VOLUME_UNKNOWN,
        SelectionRejectionCode.OPEN_INTEREST_UNKNOWN,
        SelectionRejectionCode.MARKET_RULE_MISSING,
        SelectionRejectionCode.MARKET_RULE_IDENTITY_MISMATCH,
        SelectionRejectionCode.TICK_BAND_CROSSING,
        SelectionRejectionCode.PRICE_NOT_DERIVABLE,
        SelectionRejectionCode.EVIDENCE_TIMESTAMP_CONFLICT,
        SelectionRejectionCode.ACQUISITION_PLAN_MISSING,
        SelectionRejectionCode.ACQUISITION_PLAN_CONFLICT,
        SelectionRejectionCode.CANDIDATE_SET_MISMATCH,
    }
)


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
    selection_receipt: OptionSelectionReceipt | None = None
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
    selection_receipt: OptionSelectionReceipt | None = None
    selection_activation_validator: Callable[[OptionSelectionReceipt], str] | None = None
    contract: Instrument | None = facts.contract
    quote: QuoteEvidence | None = facts.quote
    reference_price = facts.reference_price
    multiplier = facts.multiplier
    if gather_instrument is not None:
        try:
            instrument = gather_instrument(decision)
        except Exception as error:
            option_selection = _requires_option_selection(decision)
            if option_selection:
                selection_alert = _alert_for_selection_integrity_failure(
                    session,
                    account_fingerprint=facts.account_fingerprint,
                    failure="OPTION_SELECTION_FAILED",
                    now=facts.now,
                )
                alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION if option_selection else CycleStage.SIZING,
                    refusal=(
                        "OPTION_SELECTION_FAILED"
                        if option_selection
                        else "INSTRUMENT_FACTS_UNAVAILABLE"
                    ),
                    detail=f"instrument gathering raised {type(error).__name__}",
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    alerts_raised=alert_kinds,
                ),
            )
        if instrument is None:
            option_selection = _requires_option_selection(decision)
            if option_selection:
                selection_alert = _alert_for_selection_integrity_failure(
                    session,
                    account_fingerprint=facts.account_fingerprint,
                    failure="OPTION_SELECTION_EVIDENCE_UNAVAILABLE",
                    now=facts.now,
                )
                alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION if option_selection else CycleStage.SIZING,
                    refusal=(
                        "OPTION_SELECTION_EVIDENCE_UNAVAILABLE"
                        if option_selection
                        else "INSTRUMENT_FACTS_UNAVAILABLE"
                    ),
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
        selection_receipt = instrument.selection_receipt
        selection_activation_validator = instrument.selection_activation_validator

    if _requires_option_selection(decision):
        if selection_receipt is None:
            selection_alert = _alert_for_selection_integrity_failure(
                session,
                account_fingerprint=facts.account_fingerprint,
                failure="OPTION_SELECTION_RECEIPT_MISSING",
                now=facts.now,
            )
            alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION,
                    refusal="OPTION_SELECTION_RECEIPT_MISSING",
                    detail=(
                        "an opening equity-option decision may not use a contract unless "
                        "the deterministic resolver produced a receipt"
                    ),
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    alerts_raised=alert_kinds,
                ),
            )
        binding_error = _selection_binding_error(decision, mandate, selection_receipt)
        if binding_error:
            selection_alert = _alert_for_selection_integrity_failure(
                session,
                account_fingerprint=facts.account_fingerprint,
                failure="OPTION_SELECTION_RECEIPT_INVALID",
                now=facts.now,
            )
            alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION,
                    refusal="OPTION_SELECTION_RECEIPT_INVALID",
                    detail=binding_error,
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    selection_receipt=selection_receipt,
                    alerts_raised=alert_kinds,
                ),
            )
        try:
            _persist_selection_receipt(
                session,
                account_fingerprint=facts.account_fingerprint,
                receipt=selection_receipt,
            )
        except Exception as error:
            session.rollback()
            selection_alert = _alert_for_selection_integrity_failure(
                session,
                account_fingerprint=facts.account_fingerprint,
                failure="OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
                now=facts.now,
            )
            alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION,
                    refusal="OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
                    detail=f"receipt persistence raised {type(error).__name__}",
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    selection_receipt=selection_receipt,
                    alerts_raised=alert_kinds,
                ),
            )
        if selection_receipt.status is SelectionStatus.NO_TRADE:
            selection_alert = _alert_for_selection_failure(
                session,
                account_fingerprint=facts.account_fingerprint,
                receipt=selection_receipt,
            )
            if selection_alert is not None:
                alert_kinds = (*alert_kinds, selection_alert.kind)
            codes = sorted({item.code.value for item in selection_receipt.no_trade_reasons})
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION,
                    refusal="OPTION_SELECTION_NO_TRADE",
                    detail="deterministic resolver refused: " + ", ".join(codes),
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    selection_receipt=selection_receipt,
                    alerts_raised=alert_kinds,
                ),
            )
        selected = selection_receipt.selected
        assert selected is not None
        assert selected.quote.bid is not None
        assert selected.quote.ask is not None
        contract = selected.contract
        quote = QuoteEvidence(bid=selected.quote.bid, ask=selected.quote.ask)
        reference_price = selected.sizing_reference_price
        multiplier = selected.deliverable_shares

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
                selection_receipt=selection_receipt,
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
                selection_receipt=selection_receipt,
                sizing=sizing,
                compilation=compilation,
                alerts_raised=alert_kinds,
            ),
        )

    if selection_receipt is not None:
        selected = selection_receipt.selected
        assert selected is not None
        if (
            compilation.intent.contract != selected.contract
            or compilation.intent.limit_price != selected.limit_price
        ):
            selection_alert = _alert_for_selection_integrity_failure(
                session,
                account_fingerprint=facts.account_fingerprint,
                failure="OPTION_SELECTION_COMPILATION_MISMATCH",
                now=facts.now,
            )
            alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.COMPILATION,
                    refusal="OPTION_SELECTION_COMPILATION_MISMATCH",
                    detail=(
                        "the existing compiler did not reproduce the receipt-bound contract "
                        "and limit price; nothing reached the order plane"
                    ),
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    selection_receipt=selection_receipt,
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
                selection_receipt=selection_receipt,
                sizing=sizing,
                compilation=compilation,
                intent=compilation.intent,
                alerts_raised=alert_kinds,
            ),
        )

    if selection_receipt is not None:
        try:
            _verify_persisted_selection_receipt(
                session,
                account_fingerprint=facts.account_fingerprint,
                receipt=selection_receipt,
            )
        except Exception as error:
            session.rollback()
            selection_alert = _alert_for_selection_integrity_failure(
                session,
                account_fingerprint=facts.account_fingerprint,
                failure="OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
                now=facts.now,
            )
            alert_kinds = (*alert_kinds, selection_alert.kind)
            return _record(
                session,
                facts,
                CycleOutcome(
                    stage=CycleStage.SELECTION,
                    refusal="OPTION_SELECTION_RECEIPT_PERSISTENCE_FAILED",
                    detail=f"pre-handoff receipt verification raised {type(error).__name__}",
                    decision_id=decision.decision_id,
                    decision=decision,
                    admission=admission,
                    selection_receipt=selection_receipt,
                    sizing=sizing,
                    compilation=compilation,
                    intent=compilation.intent,
                    alerts_raised=alert_kinds,
                ),
            )
        if mandate.mode in LIVE_AUTONOMY_MODES:
            if selection_receipt.promotion_digest is None or selection_activation_validator is None:
                selection_alert = _alert_for_selection_integrity_failure(
                    session,
                    account_fingerprint=facts.account_fingerprint,
                    failure="OPTION_SELECTION_PROMOTION_INVALID",
                    now=facts.now,
                )
                alert_kinds = (*alert_kinds, selection_alert.kind)
                return _record(
                    session,
                    facts,
                    CycleOutcome(
                        stage=CycleStage.SELECTION,
                        refusal="OPTION_SELECTION_PROMOTION_INVALID",
                        detail=(
                            "live option selection lacks a receipt-bound promotion or "
                            "pre-handoff validator"
                        ),
                        decision_id=decision.decision_id,
                        decision=decision,
                        admission=admission,
                        selection_receipt=selection_receipt,
                        sizing=sizing,
                        compilation=compilation,
                        intent=compilation.intent,
                        alerts_raised=alert_kinds,
                    ),
                )
            promotion_error = selection_activation_validator(selection_receipt)
            if promotion_error:
                selection_alert = _alert_for_selection_integrity_failure(
                    session,
                    account_fingerprint=facts.account_fingerprint,
                    failure="OPTION_SELECTION_PROMOTION_INVALID",
                    now=facts.now,
                )
                alert_kinds = (*alert_kinds, selection_alert.kind)
                return _record(
                    session,
                    facts,
                    CycleOutcome(
                        stage=CycleStage.SELECTION,
                        refusal="OPTION_SELECTION_PROMOTION_INVALID",
                        detail=promotion_error,
                        decision_id=decision.decision_id,
                        decision=decision,
                        admission=admission,
                        selection_receipt=selection_receipt,
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
                decision=decision,
                admission=admission,
                selection_receipt=selection_receipt,
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
            decision=decision,
            admission=admission,
            selection_receipt=selection_receipt,
            sizing=sizing,
            compilation=compilation,
            intent=compilation.intent,
            handoff=result,
            alerts_raised=alert_kinds,
        ),
    )


def _requires_option_selection(decision: AITradeDecision) -> bool:
    return (
        decision.kind is DecisionKind.OPEN
        and decision.asset_class is TradableAssetClass.EQUITY_OPTION
    )


def _selection_binding_error(
    decision: AITradeDecision,
    mandate: AutonomyMandate,
    receipt: OptionSelectionReceipt,
) -> str:
    """Return why a receipt cannot authorize this exact decision, if anything."""

    if not receipt.verifies():
        return "the selection receipt does not reproduce its canonical output digest"
    request = receipt.request
    if request.decision_id != decision.decision_id:
        return "the selection receipt names a different decision"
    if request.symbol != decision.symbol:
        return "the selection receipt names a different symbol"
    if request.requested_contracts != decision.requested_quantity:
        return "the selection receipt carries different requested economics"
    if (
        decision.requested_strategy is not None
        and request.strategy is not decision.requested_strategy
    ):
        return "the selection receipt carries a different strategy"
    if receipt.mandate_digest != canonical_digest(mandate):
        return "the selection receipt is bound to a different owner mandate"
    policy_error = _selection_policy_error(mandate, receipt)
    if policy_error:
        return policy_error
    return ""


def _alert_for_selection_failure(
    session: Session,
    *,
    account_fingerprint: str,
    receipt: OptionSelectionReceipt,
) -> Any | None:
    codes = sorted(
        {
            item.code.value
            for item in receipt.no_trade_reasons
            if item.code in _SYSTEM_SELECTION_REJECTIONS
        }
    )
    if not codes:
        return None
    severity = (
        alerts.AlertSeverity.CRITICAL
        if any(
            code
            in {
                SelectionRejectionCode.PROMOTION_MISMATCH.value,
                SelectionRejectionCode.DATA_CLEANUP_FAILED.value,
            }
            for code in codes
        )
        else alerts.AlertSeverity.WARNING
    )
    return alerts.raise_alert(
        session,
        account_fingerprint=account_fingerprint,
        severity=severity,
        kind="option_selection.system_failure",
        summary="autonomous option selection failed closed on system evidence",
        detail={"rejection_codes": codes},
        now=receipt.evaluated_at,
    )


def _alert_for_selection_integrity_failure(
    session: Session,
    *,
    account_fingerprint: str,
    failure: str,
    now: datetime,
) -> Any:
    """Durably distinguish a resolver/control failure from economic NO_TRADE."""

    return alerts.raise_alert(
        session,
        account_fingerprint=account_fingerprint,
        severity=alerts.AlertSeverity.CRITICAL,
        kind="option_selection.system_failure",
        summary="autonomous option selection failed closed on a control invariant",
        detail={"failure": failure},
        now=now,
    )


def _selection_policy_error(
    mandate: AutonomyMandate,
    receipt: OptionSelectionReceipt,
) -> str:
    """Ensure a receipt policy only narrows the owner grant."""

    policy = receipt.policy
    market = mandate.market_data
    scope = mandate.scope
    sessions = mandate.sessions
    if policy.max_quote_age_seconds > market.max_quote_age_seconds:
        return "the selection receipt permits quotes older than the owner mandate"
    if not set(policy.permitted_data_qualities) <= set(market.permitted_data_qualities):
        return "the selection receipt permits a data quality outside the owner mandate"
    if policy.min_option_volume < market.min_option_volume:
        return "the selection receipt weakens the owner volume floor"
    if policy.min_open_interest < market.min_open_interest:
        return "the selection receipt weakens the owner open-interest floor"
    if policy.max_relative_spread > market.max_relative_spread:
        return "the selection receipt weakens the owner spread ceiling"
    if not set(policy.permitted_exchanges) <= set(scope.exchanges):
        return "the selection receipt permits an exchange outside the owner scope"
    if not set(policy.permitted_trading_classes) <= set(scope.contract_families):
        return "the selection receipt permits a contract family outside the owner scope"
    if policy.order_form not in scope.order_forms:
        return "the selection receipt uses an order form outside the owner scope"
    if not set(policy.permitted_sessions) <= set(sessions.permitted_sessions):
        return "the selection receipt permits a session outside the owner mandate"
    if policy.required_multiplier != Decimal(100):
        return "v1 requires an exact standard 100-share option multiplier"
    return ""


def _persist_selection_receipt(
    session: Session,
    *,
    account_fingerprint: str,
    receipt: OptionSelectionReceipt,
) -> None:
    """Commit and independently verify the exact receipt before it can authorize use."""

    stream = durable.stream_for(OPTION_SELECTION_STREAM, account_fingerprint)
    matches = _verified_selection_rows(
        session,
        stream,
        decision_id=receipt.request.decision_id,
    )
    expected_payload = _selection_receipt_payload(receipt)
    if matches:
        row, _, payload = matches[0]
        if (
            row.kind != receipt.status.value
            or row.recorded_at != receipt.evaluated_at
            or payload != expected_payload
        ):
            from chronos.persistence.hash_chain import HashChainError

            raise HashChainError("an option decision already has a conflicting receipt")
    else:
        from chronos.persistence import hash_chain

        with session.begin_nested():
            hash_chain.append(
                session,
                stream=stream,
                kind=receipt.status.value,
                payload=expected_payload,
                recorded_at=receipt.evaluated_at,
            )

    try:
        session.commit()
    except BaseException:
        session.rollback()
        raise

    _verify_persisted_selection_receipt(
        session,
        account_fingerprint=account_fingerprint,
        receipt=receipt,
    )


def _selection_receipt_payload(receipt: OptionSelectionReceipt) -> dict[str, str]:
    canonical_receipt = receipt.canonical_bytes()
    if len(canonical_receipt) > MAX_OPTION_SELECTION_RECEIPT_BYTES:
        raise ValueError("option-selection receipt exceeds the durable size limit")
    return {
        "decision_id": receipt.request.decision_id,
        "status": receipt.status.value,
        "output_digest": receipt.output_digest,
        "canonical_receipt": canonical_receipt.decode("utf-8"),
    }


def _verify_persisted_selection_receipt(
    session: Session,
    *,
    account_fingerprint: str,
    receipt: OptionSelectionReceipt,
) -> None:
    """Read the committed stream afresh and prove this one exact envelope exists."""

    from chronos.persistence.hash_chain import HashChainError

    stream = durable.stream_for(OPTION_SELECTION_STREAM, account_fingerprint)
    try:
        matches = _verified_selection_rows(
            session,
            stream,
            decision_id=receipt.request.decision_id,
        )
        expected_payload = _selection_receipt_payload(receipt)
        if len(matches) != 1:
            raise HashChainError("the committed option decision does not have exactly one receipt")
        row, _, payload = matches[0]
        if (
            row.kind != receipt.status.value
            or row.recorded_at != receipt.evaluated_at
            or payload != expected_payload
        ):
            raise HashChainError("the committed option receipt envelope conflicts")
    finally:
        # End the verification read transaction before a broker handoff. This
        # avoids carrying a stale SQLite snapshot across an external effect.
        session.rollback()


@dataclass(frozen=True, slots=True)
class _VerifiedSelectionRow:
    """The bounded, normalized durable fields needed after stream verification."""

    sequence: int
    kind: str
    recorded_at: datetime
    previous_hash: str
    record_hash: str


def _verified_selection_rows(
    session: Session,
    stream: str,
    *,
    decision_id: str,
) -> list[tuple[_VerifiedSelectionRow, OptionSelectionReceipt, dict[str, Any]]]:
    """Verify one bounded stream snapshot and retain only the requested receipt.

    Durable text is untrusted at this boundary.  Projecting ``payload_json``
    directly would let a corrupt multi-gigabyte value exhaust the process in
    the database driver before Python could apply a size check.  SQL length and
    substring projections ensure at most ``MAX + 1`` characters cross that
    boundary.  Cryptographic and semantic verification share this same stream,
    so a concurrent append cannot split the two checks across snapshots.
    """

    from chronos.persistence import hash_chain
    from chronos.persistence.schema import HashChainRow

    is_sqlite = session.get_bind().dialect.name == "sqlite"

    def _bounded_durable_text(
        column: Any,
        *,
        maximum: int,
        expected_storage_type: str,
    ) -> tuple[Any, Any, Any]:
        text_value = cast(column, Text)
        if not is_sqlite:
            return (
                literal(expected_storage_type),
                func.length(text_value),
                func.substr(text_value, 1, maximum + 1),
            )
        storage_type = func.typeof(column)
        expected = storage_type == expected_storage_type
        return (
            storage_type,
            case((expected, func.length(text_value)), else_=literal(None)),
            case(
                (expected, func.substr(text_value, 1, maximum + 1)),
                else_=literal(None),
            ),
        )

    sequence_type, sequence_length, sequence_value = _bounded_durable_text(
        HashChainRow.sequence,
        maximum=MAX_DURABLE_SEQUENCE_TEXT_CHARS,
        expected_storage_type="integer",
    )
    kind_type, kind_length, kind_value = _bounded_durable_text(
        HashChainRow.kind,
        maximum=MAX_DURABLE_KIND_TEXT_CHARS,
        expected_storage_type="text",
    )
    payload_type, payload_length, payload_text_value = _bounded_durable_text(
        HashChainRow.payload_json,
        maximum=MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS,
        expected_storage_type="text",
    )
    recorded_at_type, recorded_at_length, recorded_at_value = _bounded_durable_text(
        HashChainRow.recorded_at,
        maximum=MAX_DURABLE_TIMESTAMP_TEXT_CHARS,
        expected_storage_type="text",
    )
    previous_hash_type, previous_hash_length, previous_hash_value = _bounded_durable_text(
        HashChainRow.previous_hash,
        maximum=MAX_DURABLE_HASH_TEXT_CHARS,
        expected_storage_type="text",
    )
    record_hash_type, record_hash_length, record_hash_value = _bounded_durable_text(
        HashChainRow.record_hash,
        maximum=MAX_DURABLE_HASH_TEXT_CHARS,
        expected_storage_type="text",
    )
    statement = (
        select(
            sequence_type.label("sequence_storage_type"),
            sequence_length.label("sequence_length"),
            sequence_value.label("sequence_text"),
            kind_type.label("kind_storage_type"),
            kind_length.label("kind_length"),
            kind_value.label("kind"),
            payload_type.label("payload_storage_type"),
            payload_length.label("payload_length"),
            payload_text_value.label("payload_json"),
            recorded_at_type.label("recorded_at_storage_type"),
            recorded_at_length.label("recorded_at_length"),
            recorded_at_value.label("recorded_at_text"),
            previous_hash_type.label("previous_hash_storage_type"),
            previous_hash_length.label("previous_hash_length"),
            previous_hash_value.label("previous_hash"),
            record_hash_type.label("record_hash_storage_type"),
            record_hash_length.label("record_hash_length"),
            record_hash_value.label("record_hash"),
        )
        .where(HashChainRow.stream == stream)
        .order_by(HashChainRow.sequence.asc())
        .execution_options(yield_per=1)
    )
    matches: list[tuple[_VerifiedSelectionRow, OptionSelectionReceipt, dict[str, Any]]] = []
    decision_ids: set[str] = set()
    expected_previous = hash_chain.GENESIS_HASH
    for expected_sequence, row in enumerate(session.execute(statement), start=1):
        try:
            if row.sequence_storage_type != "integer":
                raise ValueError("option-selection sequence has an invalid storage type")
            if row.kind_storage_type != "text":
                raise ValueError("option-selection receipt kind has an invalid storage type")
            if row.payload_storage_type != "text":
                raise ValueError("option-selection receipt payload has an invalid storage type")
            if row.recorded_at_storage_type != "text":
                raise ValueError("option-selection receipt timestamp has an invalid storage type")
            if row.previous_hash_storage_type != "text" or row.record_hash_storage_type != "text":
                raise ValueError("option-selection receipt hash field has an invalid storage type")
            if row.sequence_length > MAX_DURABLE_SEQUENCE_TEXT_CHARS:
                raise ValueError("option-selection sequence exceeds its durable bound")
            sequence_text_value = row.sequence_text
            if (
                not sequence_text_value.isascii()
                or not sequence_text_value.isdecimal()
                or str(int(sequence_text_value)) != sequence_text_value
            ):
                raise ValueError("option-selection sequence is not a canonical integer")
            sequence = int(sequence_text_value)
            if sequence != expected_sequence:
                raise ValueError("option-selection receipt chain has a sequence gap")
            if row.kind_length > MAX_DURABLE_KIND_TEXT_CHARS:
                raise ValueError("option-selection receipt kind exceeds its durable bound")
            if row.payload_length > MAX_OPTION_SELECTION_PAYLOAD_JSON_CHARS:
                raise ValueError("option-selection receipt payload exceeds its durable bound")
            if row.recorded_at_length > MAX_DURABLE_TIMESTAMP_TEXT_CHARS:
                raise ValueError("option-selection receipt timestamp exceeds its durable bound")
            try:
                recorded_at = datetime.fromisoformat(row.recorded_at_text)
            except (TypeError, ValueError) as error:
                raise ValueError("option-selection receipt timestamp is invalid") from error
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            else:
                recorded_at = recorded_at.astimezone(UTC)
            if (
                row.previous_hash_length != MAX_DURABLE_HASH_TEXT_CHARS
                or row.record_hash_length != MAX_DURABLE_HASH_TEXT_CHARS
            ):
                raise ValueError("option-selection receipt hash field has an invalid length")
            if row.previous_hash != expected_previous:
                raise ValueError("option-selection receipt does not link to its predecessor")
            recomputed = hash_chain.compute_hash(
                stream=stream,
                sequence=sequence,
                recorded_at=recorded_at,
                payload_json=row.payload_json,
                previous_hash=row.previous_hash,
            )
            if recomputed != row.record_hash:
                raise ValueError("option-selection receipt contents do not match its digest")
            decoded_payload: object = json.loads(row.payload_json)
            if not isinstance(decoded_payload, dict):
                raise TypeError("option receipt envelope is not an object")
            payload: dict[str, Any] = decoded_payload
            if row.payload_json != hash_chain.canonical_payload(payload):
                raise ValueError("option receipt envelope is not canonical JSON")
            if set(payload) != {
                "decision_id",
                "status",
                "output_digest",
                "canonical_receipt",
            }:
                raise ValueError("unexpected option receipt envelope fields")
            canonical = payload["canonical_receipt"]
            if not isinstance(canonical, str):
                raise TypeError("canonical receipt is not text")
            if (
                len(canonical) > MAX_OPTION_SELECTION_RECEIPT_BYTES
                or len(canonical.encode("utf-8")) > MAX_OPTION_SELECTION_RECEIPT_BYTES
            ):
                raise ValueError("canonical receipt exceeds its durable bound")
            receipt = OptionSelectionReceipt.model_validate_json(canonical)
            if (
                not receipt.verifies()
                or canonical != receipt.canonical_bytes().decode("utf-8")
                or payload != _selection_receipt_payload(receipt)
                or row.kind != receipt.status.value
                or recorded_at != receipt.evaluated_at
                or receipt.request.decision_id in decision_ids
            ):
                raise ValueError("option receipt envelope does not replay exactly")
        except Exception as error:
            raise hash_chain.HashChainError(
                "option-selection receipt stream contains an invalid semantic envelope"
            ) from error
        expected_previous = row.record_hash
        decision_ids.add(receipt.request.decision_id)
        if receipt.request.decision_id == decision_id:
            matches.append(
                (
                    _VerifiedSelectionRow(
                        sequence=sequence,
                        kind=row.kind,
                        recorded_at=recorded_at,
                        previous_hash=row.previous_hash,
                        record_hash=row.record_hash,
                    ),
                    receipt,
                    payload,
                )
            )
    return matches


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
                "option_selection_status": (
                    outcome.selection_receipt.status.value
                    if outcome.selection_receipt is not None
                    else None
                ),
                "option_selection_digest": (
                    outcome.selection_receipt.output_digest
                    if outcome.selection_receipt is not None
                    else None
                ),
                "quantity": str(outcome.sizing.quantity) if outcome.sizing else None,
                "limit_price": str(outcome.intent.limit_price) if outcome.intent else None,
                **_narrative_of(outcome.decision),
            },
            recorded_at=facts.now,
        )
    return outcome


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
