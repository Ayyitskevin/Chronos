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

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from chronos.api.auth import require_token
from chronos.api.dependencies import BackendState, get_state, require_writer
from chronos.broker.base import BrokerError
from chronos.domain.enums import OptionRight, OrderIntent, ProductFamily
from chronos.domain.models import ChronosModel, OptionContractSpec
from chronos.orders.intent import (
    WheelOrderIntent,
    build_option_intent,
    build_stock_intent,
    new_correlation_id,
)
from chronos.orders.mutations import OrderMutationError
from chronos.orders.service import OrderManagementService, OrderPipelineError
from chronos.orders.submission import SubmissionOutcome
from chronos.persistence.order_repositories import OrderIntentRecord

router = APIRouter(dependencies=[Depends(require_token)])

StateDep = Annotated[BackendState, Depends(get_state)]
WriterDep = Annotated[BackendState, Depends(require_writer)]


class OrderProposeRequest(ChronosModel):
    symbol: str
    product_family: ProductFamily
    intent: OrderIntent
    quantity: int
    limit_price: Decimal
    # Option-only fields (ignored for STOCK).
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
        quantity=str(record.quantity),
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


@router.get("/orders", response_model=list[OrderView])
def list_orders(state: StateDep) -> list[OrderView]:
    return [_view(record) for record in _service(state).list_orders()]


@router.get("/orders/{intent_id}", response_model=OrderView)
def get_order(intent_id: str, state: StateDep) -> OrderView:
    return _view(_require_record(state, intent_id))


@router.post("/orders/propose", response_model=ProposeResponse)
def propose(request: OrderProposeRequest, state: WriterDep) -> ProposeResponse:
    _require_allowlisted(request.symbol, state)
    intent = _build_intent(request, state)
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
    """Audited operator resolution of a broker-absent SUBMISSION_UNKNOWN intent.

    Drives the intent to REJECTED only when a fresh broker snapshot taken in
    this call shows no matching order and no executions (ADR-0009 §6); if the
    broker DOES know the order, its true state is applied instead and this
    returns 409 so the operator sees the evidence-based resolution.
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
