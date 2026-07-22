"""Order-management endpoints (Milestone 5): propose -> preview -> confirm ->
submit, plus modify/cancel/list/get.

Every mutating endpoint requires the local API token (router-level) AND the
single-writer lease (``require_writer`` -> 409 in read-only mode) AND, where a
symbol is supplied, allowlist membership (403 otherwise). The backend re-derives
every stage from fresh evidence and re-addresses the in-flight proposal by id;
the client never carries a contract or an authorization token across the
round-trips. ``POST /orders/{id}/submit`` is the single transmit path for BOTH
branches (ADR-0009): paper behind the seven-gate chain, live behind the
ten-gate walk — the boundary hard-refuses unless the selected branch's entire
chain passes.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, get_state, require_writer
from chronos.broker.base import BrokerError
from chronos.domain.enums import OptionRight, OrderIntent, ProductFamily
from chronos.domain.models import ChronosModel, OptionContractSpec
from chronos.orders.intent import (
    WheelOrderIntent,
    build_crypto_intent,
    build_option_intent,
    build_stock_intent,
    canonical_quantity,
    new_correlation_id,
)
from chronos.orders.mutations import OrderMutationError
from chronos.orders.service import OrderManagementService, OrderPipelineError
from chronos.orders.submission import SubmissionOutcome
from chronos.persistence.order_repositories import OrderIntentRecord

_logger = logging.getLogger("chronos.api.orders")


router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]
WriterDep = Annotated[BackendState, Depends(require_writer)]


class OrderProposeRequest(ChronosModel):
    symbol: str
    product_family: ProductFamily
    intent: OrderIntent
    # Decimal so a crypto order can be fractional (ADR-0010 §1). OPTION/STOCK
    # whole-unit enforcement is unchanged — the intent validator re-asserts it
    # and the stock risk check re-asserts it again (defense in depth).
    quantity: Decimal
    limit_price: Decimal
    # Family-conditional TIF (ADR-0010 §3): ignored for OPTION/STOCK (always
    # DAY); a crypto order may pass IOC when the owner setting permits it.
    time_in_force: Literal["DAY", "IOC"] = "DAY"
    # Option-only fields (ignored for STOCK/CRYPTO).
    expiration: date | None = None
    strike: Decimal | None = None
    right: OptionRight | None = None


class OrderView(ChronosModel):
    intent_id: str
    order_ref: str | None
    symbol: str
    product_family: ProductFamily
    intent: str
    status: str
    quantity: str
    limit_price: str | None
    risk_snapshot_id: str | None


class ProposeResponse(ChronosModel):
    order: OrderView
    risk_decision_id: str
    risk_approved: bool


class ConfirmRequest(ChronosModel):
    risk_decision_id: str
    ui_session_id: str | None = None


class ModifyRequest(ChronosModel):
    new_limit_price: Decimal


class ResolveRequest(ChronosModel):
    operator_note: str


class ReconciliationResponse(ChronosModel):
    status: str
    generation: int
    applied_count: int
    proven_count: int
    unresolved_count: int
    remaining_active_count: int


def _service(state: BackendState) -> OrderManagementService:
    return state.runtime.order_management


def _require_allowlisted(symbol: str, state: BackendState) -> str:
    normalized = symbol.strip().upper()
    settings = state.runtime.settings
    allowed = set(settings.symbol_allowlist) | set(settings.crypto_allowlist)
    if normalized not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{normalized!r} is not on any product allowlist",
        )
    return normalized


def _view(record: OrderIntentRecord) -> OrderView:
    return OrderView(
        intent_id=record.intent_id,
        order_ref=record.order_ref,
        symbol=record.symbol,
        product_family=record.product_family,
        intent=record.open_close_effect,
        status=record.status.value,
        quantity=canonical_quantity(record.quantity),
        limit_price=(format(record.limit_price, "f") if record.limit_price is not None else None),
        risk_snapshot_id=record.risk_snapshot_id,
    )


def _require_record(state: BackendState, intent_id: str) -> OrderIntentRecord:
    record = _service(state).get(intent_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown order")
    return record


def _build_intent(request: OrderProposeRequest, state: BackendState) -> WheelOrderIntent:
    account_id = _service(state).account_id
    correlation_id = new_correlation_id()
    if request.product_family is not ProductFamily.CRYPTO and request.time_in_force != "DAY":
        # TIF is family-conditional (ADR-0010 §3): only CRYPTO may be non-DAY.
        # Reject explicitly rather than silently coercing to DAY, so the client
        # never believes it sent an IOC option/stock order that we downgraded.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="only CRYPTO orders may set a non-DAY time_in_force",
        )
    if request.product_family is ProductFamily.STOCK:
        underlying = state.runtime.connection.run(
            state.runtime.broker.qualify_underlying(request.symbol)
        )
        return build_stock_intent(
            account_id=account_id,
            intent=request.intent,
            contract=underlying,
            quantity=request.quantity,
            limit_price=request.limit_price,
            correlation_id=correlation_id,
        )
    if request.product_family is ProductFamily.CRYPTO:
        crypto = state.runtime.connection.run(state.runtime.broker.qualify_crypto(request.symbol))
        return build_crypto_intent(
            account_id=account_id,
            intent=request.intent,
            contract=crypto,
            quantity=request.quantity,
            limit_price=request.limit_price,
            time_in_force=request.time_in_force,
            correlation_id=correlation_id,
        )
    if request.expiration is None or request.strike is None or request.right is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="option orders require expiration, strike, and right",
        )
    spec = OptionContractSpec(
        symbol=request.symbol,
        expiration=request.expiration,
        strike=request.strike,
        right=request.right,
        multiplier=Decimal("100"),
        trading_class=request.symbol,
    )
    qualified = state.runtime.connection.run(state.runtime.broker.qualify_option_contracts((spec,)))
    if not qualified:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="broker could not qualify the requested option contract",
        )
    return build_option_intent(
        account_id=account_id,
        intent=request.intent,
        contract=qualified[0],
        quantity=request.quantity,
        limit_price=request.limit_price,
        correlation_id=correlation_id,
    )


@router.post("/orders/reconcile", response_model=ReconciliationResponse)
def reconcile_orders(
    state: WriterDep,
) -> ReconciliationResponse:
    """Rebuild submission readiness from fresh broker and local evidence."""

    try:
        report = state.runtime.reconcile_submission_readiness()
    except Exception as error:
        _logger.error(
            "Operator submission reconciliation failed; submission remains locked",
            extra={
                "event": "submission_reconciliation_failed",
                "operation": "operator_reconcile",
                "error_type": type(error).__name__,
                "outcome": "locked",
                "passed": False,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "reconciliation failed; order submission remains locked while "
                "inspection, cancellation, and recovery remain available"
            ),
        ) from error

    restart = report.restart
    readiness = report.readiness
    log = _logger.info if readiness.ready else _logger.warning
    log(
        "Operator submission reconciliation finished %s "
        "(applied=%d unresolved=%d remaining_active=%d)",
        readiness.status.value,
        len(restart.applied_updates),
        len(restart.unresolved),
        len(restart.remaining_active),
        extra={
            "event": "submission_reconciliation_finished",
            "operation": "operator_reconcile",
            "reconciliation_status": readiness.status.value,
            "applied_count": len(restart.applied_updates),
            "outcome": "ready" if readiness.ready else "locked",
            "passed": readiness.ready,
            "proven_count": len(restart.proven),
            "unresolved_count": len(restart.unresolved),
            "remaining_active_count": len(restart.remaining_active),
        },
    )
    return ReconciliationResponse(
        status=readiness.status.value,
        generation=readiness.generation,
        applied_count=len(restart.applied_updates),
        proven_count=len(restart.proven),
        unresolved_count=len(restart.unresolved),
        remaining_active_count=len(restart.remaining_active),
    )


@router.get("/orders", response_model=list[OrderView])
def list_orders(state: StateDep) -> list[OrderView]:
    return [_view(record) for record in _service(state).list_orders()]


@router.get("/orders/{intent_id}", response_model=OrderView)
def get_order(intent_id: str, state: StateDep) -> OrderView:
    return _view(_require_record(state, intent_id))


@router.post("/orders/propose", response_model=ProposeResponse)
def propose(request: OrderProposeRequest, state: WriterDep) -> ProposeResponse:
    _require_allowlisted(request.symbol, state)
    try:
        intent = _build_intent(request, state)
    except ValueError as error:
        # The intent value-object validator rejects economically invalid
        # requests (e.g. a fractional OPTION/STOCK quantity, or an intent that
        # does not match the product family). Surface as 422, not 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    except BrokerError as error:
        # Contract qualification hit the broker (e.g. a symbol the gateway can't
        # resolve, or an adapter that refuses the family). Surface as 502, not a
        # bare 500 — the request was well-formed; the broker could not fulfil it.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    result = _service(state).propose(intent)
    return ProposeResponse(
        order=_view(result.intent),
        risk_decision_id=result.risk.decision_id,
        risk_approved=result.risk.approved,
    )


@router.post("/orders/{intent_id}/preview", response_model=OrderView)
def preview(intent_id: str, state: WriterDep) -> OrderView:
    try:
        _service(state).preview_by_id(intent_id)
    except OrderPipelineError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _view(_require_record(state, intent_id))


@router.post("/orders/{intent_id}/confirm", response_model=OrderView)
def confirm(intent_id: str, request: ConfirmRequest, state: WriterDep) -> OrderView:
    try:
        _service(state).confirm_by_id(
            intent_id,
            risk_decision_id=request.risk_decision_id,
            ui_session_id=request.ui_session_id,
        )
    except OrderPipelineError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _view(_require_record(state, intent_id))


@router.post("/orders/{intent_id}/submit", response_model=SubmissionOutcome)
def submit(intent_id: str, state: WriterDep) -> SubmissionOutcome:
    try:
        return _service(state).submit_by_id(intent_id, writer_lease_held=state.writer)
    except OrderPipelineError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/orders/{intent_id}/modify", response_model=OrderView)
def modify(intent_id: str, request: ModifyRequest, state: WriterDep) -> OrderView:
    try:
        _service(state).modify(intent_id, request.new_limit_price)
    except (OrderPipelineError, OrderMutationError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _view(_require_record(state, intent_id))


@router.post("/orders/{intent_id}/cancel", response_model=OrderView)
def cancel(intent_id: str, state: WriterDep) -> OrderView:
    try:
        _service(state).cancel(intent_id)
    except (OrderPipelineError, OrderMutationError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _view(_require_record(state, intent_id))


@router.post("/orders/{intent_id}/resolve", response_model=OrderView)
def resolve(intent_id: str, request: ResolveRequest, state: WriterDep) -> OrderView:
    """Operator-triggered bounded refresh of a SUBMISSION_UNKNOWN intent.

    Positive broker evidence is applied through reconciliation. Snapshot
    absence cannot safely prove rejection, so it returns 409 and the intent
    remains locked instead of writing an unsafe terminal lifecycle.
    """

    try:
        resolved = _service(state).resolve_submission_unknown(
            intent_id, operator_note=request.operator_note
        )
    except (OrderPipelineError, ValueError, BrokerError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "broker evidence resolved this intent (or its status changed); "
                "operator rejection was not applied — re-check the order state"
            ),
        )
    return _view(_require_record(state, intent_id))
