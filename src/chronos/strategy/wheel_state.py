"""Pure, fail-closed Wheel state derivation from reconciled broker evidence."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator

from chronos.domain.enums import (
    OptionRight,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import (
    BrokerOrder,
    BrokerOrderIdentity,
    BrokerPosition,
    ChronosModel,
    CryptoContract,
    OptionContract,
    UnderlyingContract,
)

_ACTIVE_ORDER_STATES = {
    OrderLifecycle.SUBMITTED,
    OrderLifecycle.PARTIALLY_FILLED,
}


class WheelStateInput(ChronosModel):
    """A single-symbol reconciled broker snapshot plus explicit safety evidence."""

    symbol: str
    expected_account_id: str
    positions: tuple[BrokerPosition, ...] = ()
    open_orders: tuple[BrokerOrder, ...] = ()
    matched_order_identities: frozenset[BrokerOrderIdentity]
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING
    eligible_call_multiplier: Decimal = Field(default=Decimal("100"), gt=0)
    partial_assignment: bool = False
    corporate_action_warning: bool = False
    unexplained_mismatch: bool = False

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be blank")
        return symbol

    @field_validator("expected_account_id")
    @classmethod
    def validate_expected_account_id(cls, value: str) -> str:
        account_id = value.strip()
        if not account_id:
            raise ValueError("expected_account_id must not be blank")
        return account_id


class WheelStateDecision(ChronosModel):
    """Resolved state and the exposure counts that justify it."""

    symbol: str
    stage: WheelStage
    eligible_action: OrderIntent | None
    stock_shares: Decimal
    unencumbered_shares: Decimal
    short_put_contracts: Decimal
    short_call_contracts: Decimal
    pending_put_contracts: Decimal
    pending_call_contracts: Decimal
    opening_actions_locked: bool
    manual_review_required: bool
    reasons: tuple[str, ...]


def _verified_standard_deliverable(
    option: OptionContract,
    *,
    source: str,
    manual_reasons: list[str],
) -> tuple[tuple[int, str], Decimal] | None:
    if not option.has_verified_standard_deliverable:
        manual_reasons.append(
            f"{source} lacks a verified standard share-only deliverable and exact underlying conId."
        )
        return None
    assert option.underlying_con_id is not None
    assert option.deliverable_shares is not None
    return (option.underlying_con_id, option.currency), option.deliverable_shares


def derive_wheel_state(evidence: WheelStateInput) -> WheelStateDecision:
    """Derive one conservative Wheel stage without consulting UI session state."""

    positions = tuple(
        position for position in evidence.positions if position.contract.symbol == evidence.symbol
    )
    orders = tuple(
        order for order in evidence.open_orders if order.contract.symbol == evidence.symbol
    )
    reasons: list[str] = []
    manual_reasons: list[str] = []

    position_key_counts: dict[tuple[str, int], int] = {}
    for position in positions:
        if position.account_id != evidence.expected_account_id:
            continue
        position_key = (position.account_id, position.contract.con_id)
        position_key_counts[position_key] = position_key_counts.get(position_key, 0) + 1
    duplicate_position_keys = frozenset(
        position_key for position_key, count in position_key_counts.items() if count > 1
    )
    if duplicate_position_keys:
        duplicate_con_ids = sorted(con_id for _, con_id in duplicate_position_keys)
        manual_reasons.append(
            "Broker snapshot contains duplicate position rows for the expected account and "
            f"contract conIds {duplicate_con_ids}; quantities cannot be safely aggregated."
        )

    if any(position.account_id != evidence.expected_account_id for position in positions) or any(
        order.account_id != evidence.expected_account_id for order in orders
    ):
        manual_reasons.append("Broker evidence does not belong to the expected account.")
    if any(
        identity.account_id != evidence.expected_account_id
        for identity in evidence.matched_order_identities
    ):
        manual_reasons.append(
            "Affirmative order matches include evidence from outside the expected account."
        )
    if evidence.reconciliation_status is not ReconciliationStatus.RECONCILED:
        manual_reasons.append(f"Reconciliation status is {evidence.reconciliation_status.value}.")
    if evidence.corporate_action_warning:
        manual_reasons.append("A corporate-action warning requires contract review.")
    if evidence.unexplained_mismatch:
        manual_reasons.append("Broker and local strategy evidence do not agree.")
    active_order_identities = frozenset(
        order.identity
        for order in orders
        if order.lifecycle in _ACTIVE_ORDER_STATES and order.remaining_quantity > 0
    )
    unmatched_order_identities = active_order_identities - evidence.matched_order_identities
    if unmatched_order_identities:
        unmatched_order_ids = sorted(
            identity.broker_order_id for identity in unmatched_order_identities
        )
        manual_reasons.append(
            f"Active broker orders lack an affirmative reconciliation match: {unmatched_order_ids}."
        )
    stale_matched_order_identities = evidence.matched_order_identities - active_order_identities
    if stale_matched_order_identities:
        stale_matched_order_ids = sorted(
            identity.broker_order_id for identity in stale_matched_order_identities
        )
        manual_reasons.append(
            "Matched order identities are absent from the active broker snapshot: "
            f"{stale_matched_order_ids}."
        )

    stock_shares = Decimal("0")
    stock_shares_by_instrument: dict[tuple[int, str], Decimal] = {}
    short_put_contracts = Decimal("0")
    short_call_contracts = Decimal("0")
    short_contracts_by_con_id: dict[int, Decimal] = {}
    short_right_by_con_id: dict[int, OptionRight] = {}
    option_metadata_by_con_id: dict[int, OptionContract] = {}
    call_shares_by_instrument: dict[tuple[int, str], Decimal] = {}
    call_coverage_known = True

    for position in positions:
        if (position.account_id, position.contract.con_id) in duplicate_position_keys:
            continue
        if isinstance(position.contract, UnderlyingContract):
            stock_shares += position.quantity
            stock_key = (position.contract.con_id, position.contract.currency)
            stock_shares_by_instrument[stock_key] = (
                stock_shares_by_instrument.get(stock_key, Decimal("0")) + position.quantity
            )
            continue
        if isinstance(position.contract, CryptoContract):
            # Crypto is its own product family (ADR-0010): never a Wheel
            # object and never a manual-review trigger — excluded here.
            continue
        option = position.contract
        if position.quantity > 0:
            manual_reasons.append(
                f"Long option position {option.local_symbol} is outside the MVP Wheel model."
            )
            continue
        if position.quantity == 0:
            continue
        contracts = abs(position.quantity)
        if contracts != contracts.to_integral_value():
            manual_reasons.append(
                f"Option position {option.local_symbol} has a fractional contract quantity."
            )
            continue
        existing_contract = option_metadata_by_con_id.get(option.con_id)
        if existing_contract is not None and existing_contract != option:
            manual_reasons.append(
                f"Broker evidence has conflicting metadata for option conId {option.con_id}."
            )
        option_metadata_by_con_id[option.con_id] = option
        existing_right = short_right_by_con_id.get(option.con_id)
        if existing_right is not None and existing_right is not option.right:
            manual_reasons.append(
                f"Broker positions disagree on the right for option conId {option.con_id}."
            )
        short_right_by_con_id[option.con_id] = option.right
        short_contracts_by_con_id[option.con_id] = (
            short_contracts_by_con_id.get(option.con_id, Decimal("0")) + contracts
        )
        deliverable = _verified_standard_deliverable(
            option,
            source=f"Short option position {option.local_symbol}",
            manual_reasons=manual_reasons,
        )
        if option.right is OptionRight.PUT:
            short_put_contracts += contracts
        else:
            short_call_contracts += contracts
            if deliverable is None:
                call_coverage_known = False
            else:
                stock_key, shares_per_contract = deliverable
                call_shares_by_instrument[stock_key] = (
                    call_shares_by_instrument.get(stock_key, Decimal("0"))
                    + contracts * shares_per_contract
                )

    if any(shares < 0 for shares in stock_shares_by_instrument.values()):
        manual_reasons.append("A short stock position is outside the Wheel strategy boundary.")

    pending_put_contracts = Decimal("0")
    pending_call_contracts = Decimal("0")
    pending_put_filled = Decimal("0")
    closing_in_progress = False
    closing_contracts_by_con_id: dict[int, Decimal] = {}
    opening_fills_by_con_id: dict[int, Decimal] = {}
    opening_right_by_con_id: dict[int, OptionRight] = {}
    opening_put_orders = 0
    opening_call_orders = 0

    for order in orders:
        if order.lifecycle not in _ACTIVE_ORDER_STATES or order.remaining_quantity <= 0:
            continue
        if isinstance(order.contract, UnderlyingContract):
            manual_reasons.append("An open stock order cannot be classified as a Wheel action.")
            continue
        if isinstance(order.contract, CryptoContract):
            continue  # crypto orders are outside the Wheel model (ADR-0010)
        option = order.contract
        existing_contract = option_metadata_by_con_id.get(option.con_id)
        if existing_contract is not None and existing_contract != option:
            manual_reasons.append(
                f"Order {order.broker_order_id} metadata disagrees with option conId "
                f"{option.con_id} in the broker positions."
            )
        option_metadata_by_con_id.setdefault(option.con_id, option)
        if any(
            quantity != quantity.to_integral_value()
            for quantity in (order.filled_quantity, order.remaining_quantity)
        ):
            manual_reasons.append(
                f"Open order {order.broker_order_id} has a fractional contract quantity."
            )
            continue
        order_deliverable = _verified_standard_deliverable(
            option,
            source=f"Option order {order.broker_order_id}",
            manual_reasons=manual_reasons,
        )
        if option.right is OptionRight.CALL and order_deliverable is None:
            call_coverage_known = False
        if order.side is OrderSide.BUY:
            active_contracts = short_contracts_by_con_id.get(option.con_id, Decimal("0"))
            if (
                active_contracts <= 0
                or short_right_by_con_id.get(option.con_id) is not option.right
            ):
                manual_reasons.append(
                    f"Buy order {order.broker_order_id} for conId {option.con_id} has no "
                    "matching short option with that exact contract ID to close."
                )
            else:
                closing_in_progress = True
                closing_contracts_by_con_id[option.con_id] = (
                    closing_contracts_by_con_id.get(option.con_id, Decimal("0"))
                    + order.remaining_quantity
                )
            continue
        opening_fills_by_con_id[option.con_id] = (
            opening_fills_by_con_id.get(option.con_id, Decimal("0")) + order.filled_quantity
        )
        existing_opening_right = opening_right_by_con_id.get(option.con_id)
        if existing_opening_right is not None and existing_opening_right is not option.right:
            manual_reasons.append(
                f"Opening orders disagree on the right for option conId {option.con_id}."
            )
        opening_right_by_con_id[option.con_id] = option.right
        if option.right is OptionRight.PUT:
            opening_put_orders += 1
            pending_put_contracts += order.remaining_quantity
            pending_put_filled += order.filled_quantity
        else:
            opening_call_orders += 1
            pending_call_contracts += order.remaining_quantity
            if order_deliverable is not None:
                stock_key, shares_per_contract = order_deliverable
                call_shares_by_instrument[stock_key] = (
                    call_shares_by_instrument.get(stock_key, Decimal("0"))
                    + order.remaining_quantity * shares_per_contract
                )

    if call_coverage_known:
        total_call_shares = sum(
            call_shares_by_instrument.values(),
            start=Decimal("0"),
        )
        unencumbered_shares = max(Decimal("0"), stock_shares - total_call_shares)
    else:
        unencumbered_shares = Decimal("0")

    if short_put_contracts + pending_put_contracts > 0 and stock_shares > 0:
        manual_reasons.append(
            "Stock and short-put exposure coexist for one symbol; cycle ownership is ambiguous."
        )
    if (
        short_put_contracts + pending_put_contracts > 0
        and short_call_contracts + pending_call_contracts > 0
    ):
        manual_reasons.append("Short puts and short calls coexist for one symbol.")
    for stock_key, required_shares in call_shares_by_instrument.items():
        available_shares = stock_shares_by_instrument.get(stock_key, Decimal("0"))
        if required_shares > available_shares:
            underlying_con_id, currency = stock_key
            manual_reasons.append(
                "Short or pending calls exceed matching stock shares available for full "
                f"coverage of underlying conId {underlying_con_id} in {currency}."
            )
    if opening_put_orders > 1 or opening_call_orders > 1:
        manual_reasons.append("More than one opening order is active for the same option right.")
    for con_id, filled_contracts in opening_fills_by_con_id.items():
        matching_contracts = short_contracts_by_con_id.get(con_id, Decimal("0"))
        opening_right = opening_right_by_con_id[con_id]
        matching_right = short_right_by_con_id.get(con_id)
        if matching_contracts != filled_contracts or (
            matching_contracts > 0 and matching_right is not opening_right
        ):
            right_name = "put" if opening_right is OptionRight.PUT else "call"
            manual_reasons.append(
                f"Short-{right_name} position for conId {con_id} does not match fills on "
                "the pending opening order."
            )
    if pending_put_contracts > 0 and short_put_contracts != pending_put_filled:
        manual_reasons.append(
            "Short-put position quantity does not match fills on the pending opening order."
        )
    for con_id, closing_contracts in closing_contracts_by_con_id.items():
        if closing_contracts > short_contracts_by_con_id[con_id]:
            manual_reasons.append(
                f"Closing orders for conId {con_id} exceed the matching broker short position."
            )
    if closing_in_progress and (pending_put_contracts > 0 or pending_call_contracts > 0):
        manual_reasons.append("Opening and closing option orders are active simultaneously.")
    has_option_exposure = (
        short_put_contracts + short_call_contracts + pending_put_contracts + pending_call_contracts
        > 0
    )
    positive_stock_instruments = sum(shares > 0 for shares in stock_shares_by_instrument.values())
    if (
        not has_option_exposure
        and stock_shares >= evidence.eligible_call_multiplier
        and positive_stock_instruments > 1
    ):
        manual_reasons.append(
            "Long-stock eligibility spans multiple nonfungible stock instruments."
        )
    if not has_option_exposure and Decimal("0") < stock_shares < evidence.eligible_call_multiplier:
        manual_reasons.append(
            "A positive stock holding smaller than one covered-call lot requires manual review."
        )

    if manual_reasons:
        return _decision(
            evidence=evidence,
            stage=WheelStage.MANUAL_REVIEW,
            eligible_action=None,
            stock_shares=stock_shares,
            unencumbered_shares=unencumbered_shares,
            short_put_contracts=short_put_contracts,
            short_call_contracts=short_call_contracts,
            pending_put_contracts=pending_put_contracts,
            pending_call_contracts=pending_call_contracts,
            reasons=manual_reasons,
        )

    if evidence.partial_assignment:
        return _decision(
            evidence=evidence,
            stage=WheelStage.ASSIGNMENT_RECONCILING,
            eligible_action=None,
            stock_shares=stock_shares,
            unencumbered_shares=unencumbered_shares,
            short_put_contracts=short_put_contracts,
            short_call_contracts=short_call_contracts,
            pending_put_contracts=pending_put_contracts,
            pending_call_contracts=pending_call_contracts,
            reasons=[
                "Position changes suggest assignment, exercise, or expiration activity; "
                "opening actions stay locked until reconciliation explains them.",
            ],
        )

    if closing_in_progress:
        closing_rights = {
            short_right_by_con_id.get(con_id) for con_id in closing_contracts_by_con_id
        }
        if closing_rights == {OptionRight.PUT}:
            stage = WheelStage.PUT_CLOSE_PENDING
            reasons.append("A buy-to-close order is working against the short put.")
        elif closing_rights == {OptionRight.CALL}:
            stage = WheelStage.CALL_CLOSE_PENDING
            reasons.append("A buy-to-close order is working against the short call.")
        else:
            stage = WheelStage.CLOSING
            reasons.append("A broker buy order is working against an active short option.")
    elif pending_put_contracts > 0:
        stage = WheelStage.SHORT_PUT_PENDING
        reasons.append("An opening short-put order has unfilled contracts.")
    elif pending_call_contracts > 0:
        stage = WheelStage.SHORT_CALL_PENDING
        reasons.append("An opening covered-call order has unfilled contracts.")
    elif short_put_contracts > 0:
        stage = WheelStage.SHORT_PUT_OPEN
        reasons.append("A short put is open at the broker.")
    elif short_call_contracts > 0:
        stage = WheelStage.SHORT_CALL_OPEN
        reasons.append("Every open short call is covered by broker-reported shares.")
    elif stock_shares >= evidence.eligible_call_multiplier:
        stage = WheelStage.LONG_STOCK
        reasons.append("At least one qualified covered-call lot is unencumbered.")
    else:
        stage = WheelStage.FLAT
        if stock_shares > 0:
            reasons.append("The stock holding is smaller than one covered-call lot.")
        else:
            reasons.append("No qualifying stock or short-option exposure is present.")

    eligible_action = {
        WheelStage.FLAT: OrderIntent.OPEN_SHORT_PUT,
        WheelStage.LONG_STOCK: OrderIntent.OPEN_COVERED_CALL,
    }.get(stage)
    return _decision(
        evidence=evidence,
        stage=stage,
        eligible_action=eligible_action,
        stock_shares=stock_shares,
        unencumbered_shares=unencumbered_shares,
        short_put_contracts=short_put_contracts,
        short_call_contracts=short_call_contracts,
        pending_put_contracts=pending_put_contracts,
        pending_call_contracts=pending_call_contracts,
        reasons=reasons,
    )


def _decision(
    *,
    evidence: WheelStateInput,
    stage: WheelStage,
    eligible_action: OrderIntent | None,
    stock_shares: Decimal,
    unencumbered_shares: Decimal,
    short_put_contracts: Decimal,
    short_call_contracts: Decimal,
    pending_put_contracts: Decimal,
    pending_call_contracts: Decimal,
    reasons: list[str],
) -> WheelStateDecision:
    manual_review = stage is WheelStage.MANUAL_REVIEW
    return WheelStateDecision(
        symbol=evidence.symbol,
        stage=stage,
        eligible_action=eligible_action,
        stock_shares=stock_shares,
        unencumbered_shares=unencumbered_shares,
        short_put_contracts=short_put_contracts,
        short_call_contracts=short_call_contracts,
        pending_put_contracts=pending_put_contracts,
        pending_call_contracts=pending_call_contracts,
        opening_actions_locked=stage not in {WheelStage.FLAT, WheelStage.LONG_STOCK},
        manual_review_required=manual_review,
        reasons=tuple(reasons),
    )
