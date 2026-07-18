from datetime import date
from decimal import Decimal

import pytest

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
    OptionContract,
    UnderlyingContract,
)
from chronos.strategy.wheel_state import (
    WheelStateDecision,
    WheelStateInput,
    derive_wheel_state,
)

ACCOUNT_ID = "DU1234567"
SYMBOL = "TEST"
STOCK_CON_ID = 1_000


def stock_position(
    quantity: str,
    *,
    account_id: str = ACCOUNT_ID,
    symbol: str = SYMBOL,
    con_id: int = STOCK_CON_ID,
    currency: str = "USD",
) -> BrokerPosition:
    return BrokerPosition(
        account_id=account_id,
        contract=UnderlyingContract(
            con_id=con_id,
            symbol=symbol,
            currency=currency,
        ),
        quantity=Decimal(quantity),
        average_cost=Decimal("90"),
        market_price=Decimal("100"),
    )


def option_contract(
    right: OptionRight,
    *,
    multiplier: str = "100",
    symbol: str = SYMBOL,
    con_id: int | None = None,
    underlying_con_id: int | None = STOCK_CON_ID,
    currency: str = "USD",
    deliverable_shares: str | None = None,
    deliverable_verified: bool = True,
) -> OptionContract:
    contract_id = con_id or (2_001 if right is OptionRight.PUT else 2_002)
    resolved_deliverable_shares = (
        Decimal(deliverable_shares if deliverable_shares is not None else multiplier)
        if deliverable_verified
        else None
    )
    return OptionContract(
        con_id=contract_id,
        symbol=symbol,
        underlying_con_id=underlying_con_id,
        expiration=date(2026, 2, 20),
        strike=Decimal("95" if right is OptionRight.PUT else "105"),
        right=right,
        currency=currency,
        multiplier=Decimal(multiplier),
        trading_class=symbol,
        local_symbol=f"{symbol}-{right.value}-{contract_id}",
        deliverable_shares=resolved_deliverable_shares,
        deliverable_verified=deliverable_verified,
    )


def option_position(
    right: OptionRight,
    quantity: str,
    *,
    multiplier: str = "100",
    account_id: str = ACCOUNT_ID,
    symbol: str = SYMBOL,
    con_id: int | None = None,
    underlying_con_id: int | None = STOCK_CON_ID,
    currency: str = "USD",
    deliverable_shares: str | None = None,
    deliverable_verified: bool = True,
) -> BrokerPosition:
    return BrokerPosition(
        account_id=account_id,
        contract=option_contract(
            right,
            multiplier=multiplier,
            symbol=symbol,
            con_id=con_id,
            underlying_con_id=underlying_con_id,
            currency=currency,
            deliverable_shares=deliverable_shares,
            deliverable_verified=deliverable_verified,
        ),
        quantity=Decimal(quantity),
        average_cost=Decimal("2.00"),
        market_price=Decimal("1.50"),
    )


def option_order(
    right: OptionRight,
    *,
    side: OrderSide = OrderSide.SELL,
    quantity: str = "1",
    filled: str = "0",
    remaining: str = "1",
    lifecycle: OrderLifecycle = OrderLifecycle.SUBMITTED,
    multiplier: str = "100",
    account_id: str = ACCOUNT_ID,
    broker_order_id: int = 7_001,
    client_id: int = 17,
    order_ref: str | None = None,
    symbol: str = SYMBOL,
    con_id: int | None = None,
    underlying_con_id: int | None = STOCK_CON_ID,
    currency: str = "USD",
    deliverable_shares: str | None = None,
    deliverable_verified: bool = True,
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_id=client_id,
        account_id=account_id,
        order_ref=order_ref or f"CHR-{broker_order_id}",
        contract=option_contract(
            right,
            symbol=symbol,
            multiplier=multiplier,
            con_id=con_id,
            underlying_con_id=underlying_con_id,
            currency=currency,
            deliverable_shares=deliverable_shares,
            deliverable_verified=deliverable_verified,
        ),
        side=side,
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled),
        remaining_quantity=Decimal(remaining),
        limit_price=Decimal("1.25"),
        lifecycle=lifecycle,
    )


def derive(
    *,
    positions: tuple[BrokerPosition, ...] = (),
    orders: tuple[BrokerOrder, ...] = (),
    expected_account_id: str = ACCOUNT_ID,
    matched_order_identities: frozenset[BrokerOrderIdentity] | None = None,
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.RECONCILED,
    eligible_call_multiplier: Decimal = Decimal("100"),
    partial_assignment: bool = False,
    corporate_action_warning: bool = False,
    unexplained_mismatch: bool = False,
) -> WheelStateDecision:
    resolved_matched_order_identities = (
        frozenset(
            order.identity
            for order in orders
            if order.lifecycle in {OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED}
            and order.remaining_quantity > 0
        )
        if matched_order_identities is None
        else matched_order_identities
    )
    return derive_wheel_state(
        WheelStateInput(
            symbol=SYMBOL,
            expected_account_id=expected_account_id,
            positions=positions,
            open_orders=orders,
            matched_order_identities=resolved_matched_order_identities,
            reconciliation_status=reconciliation_status,
            eligible_call_multiplier=eligible_call_multiplier,
            partial_assignment=partial_assignment,
            corporate_action_warning=corporate_action_warning,
            unexplained_mismatch=unexplained_mismatch,
        )
    )


def test_reconciliation_defaults_pending_and_locks_opening_actions() -> None:
    evidence = WheelStateInput(
        symbol=SYMBOL,
        expected_account_id=ACCOUNT_ID,
        matched_order_identities=frozenset(),
    )

    decision = derive_wheel_state(evidence)

    assert evidence.reconciliation_status is ReconciliationStatus.PENDING
    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.opening_actions_locked is True
    assert decision.eligible_action is None


@pytest.mark.parametrize(
    ("shares", "expected_stage"),
    [
        (None, WheelStage.FLAT),
        ("99", WheelStage.MANUAL_REVIEW),
        ("100", WheelStage.LONG_STOCK),
    ],
)
def test_stock_lot_boundaries_fail_closed_below_one_call_lot(
    shares: str | None,
    expected_stage: WheelStage,
) -> None:
    positions = () if shares is None else (stock_position(shares),)

    decision = derive(positions=positions)

    assert decision.stage is expected_stage
    expected_action = {
        WheelStage.FLAT: OrderIntent.OPEN_SHORT_PUT,
        WheelStage.LONG_STOCK: OrderIntent.OPEN_COVERED_CALL,
    }.get(expected_stage)
    assert decision.eligible_action is expected_action
    assert decision.opening_actions_locked is (expected_stage is WheelStage.MANUAL_REVIEW)


@pytest.mark.parametrize(
    ("shares", "expected_stage"),
    [("49", WheelStage.MANUAL_REVIEW), ("50", WheelStage.LONG_STOCK)],
)
def test_long_stock_threshold_uses_the_qualified_multiplier(
    shares: str,
    expected_stage: WheelStage,
) -> None:
    decision = derive(
        positions=(stock_position(shares),),
        eligible_call_multiplier=Decimal("50"),
    )

    assert decision.stage is expected_stage


def test_duplicate_stock_position_rows_require_manual_review_without_double_counting() -> None:
    decision = derive(
        positions=(
            stock_position("40"),
            stock_position("60"),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.stock_shares == Decimal("0")
    assert any("duplicate position rows" in reason for reason in decision.reasons)


def test_duplicate_option_position_rows_require_manual_review_without_double_counting() -> None:
    decision = derive(
        positions=(
            option_position(OptionRight.PUT, "-1"),
            option_position(OptionRight.PUT, "-1"),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.short_put_contracts == Decimal("0")
    assert any("duplicate position rows" in reason for reason in decision.reasons)


def test_multiple_stock_instruments_cannot_create_long_stock() -> None:
    decision = derive(
        positions=(
            stock_position("50"),
            stock_position("50", con_id=STOCK_CON_ID + 1),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.stock_shares == Decimal("100")
    assert decision.opening_actions_locked is True
    assert any("multiple nonfungible" in reason for reason in decision.reasons)


def test_unfilled_opening_put_is_pending() -> None:
    decision = derive(orders=(option_order(OptionRight.PUT),))

    assert decision.stage is WheelStage.SHORT_PUT_PENDING
    assert decision.pending_put_contracts == Decimal("1")
    assert decision.short_put_contracts == Decimal("0")
    assert decision.opening_actions_locked is True


def test_active_order_without_affirmative_reconciliation_match_is_locked() -> None:
    decision = derive(
        orders=(option_order(OptionRight.PUT),),
        matched_order_identities=frozenset(),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.opening_actions_locked is True
    assert any("affirmative reconciliation match" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "identity_change",
    [
        {"client_id": 18},
        {"order_ref": "CHR-DIFFERENT-OWNER"},
    ],
)
def test_same_bare_order_id_with_different_ownership_identity_is_not_matched(
    identity_change: dict[str, object],
) -> None:
    order = option_order(OptionRight.PUT)
    different_identity = order.identity.model_copy(update=identity_change)

    decision = derive(
        orders=(order,),
        matched_order_identities=frozenset({different_identity}),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("affirmative reconciliation match" in reason for reason in decision.reasons)
    assert any("absent from the active broker snapshot" in reason for reason in decision.reasons)


def test_stale_matched_order_identity_requires_manual_review() -> None:
    stale_identity = option_order(OptionRight.PUT, broker_order_id=7_099).identity
    decision = derive(matched_order_identities=frozenset({stale_identity}))

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("absent from the active broker snapshot" in reason for reason in decision.reasons)


def test_coherent_partial_put_fill_remains_pending() -> None:
    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1"),),
        orders=(
            option_order(
                OptionRight.PUT,
                quantity="2",
                filled="1",
                remaining="1",
                lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            ),
        ),
    )

    assert decision.stage is WheelStage.SHORT_PUT_PENDING
    assert decision.short_put_contracts == Decimal("1")
    assert decision.pending_put_contracts == Decimal("1")


def test_short_put_without_unfilled_order_is_open() -> None:
    decision = derive(positions=(option_position(OptionRight.PUT, "-1"),))

    assert decision.stage is WheelStage.SHORT_PUT_OPEN
    assert decision.short_put_contracts == Decimal("1")
    assert decision.eligible_action is None


def test_unfilled_opening_call_is_pending_when_fully_covered() -> None:
    decision = derive(
        positions=(stock_position("100"),),
        orders=(option_order(OptionRight.CALL),),
    )

    assert decision.stage is WheelStage.SHORT_CALL_PENDING
    assert decision.pending_call_contracts == Decimal("1")
    assert decision.unencumbered_shares == Decimal("0")


def test_coherent_partial_call_fill_counts_filled_and_pending_coverage() -> None:
    decision = derive(
        positions=(
            stock_position("200"),
            option_position(OptionRight.CALL, "-1"),
        ),
        orders=(
            option_order(
                OptionRight.CALL,
                quantity="2",
                filled="1",
                remaining="1",
                lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            ),
        ),
    )

    assert decision.stage is WheelStage.SHORT_CALL_PENDING
    assert decision.short_call_contracts == Decimal("1")
    assert decision.pending_call_contracts == Decimal("1")
    assert decision.unencumbered_shares == Decimal("0")


def test_coherent_additional_fully_covered_call_remains_pending() -> None:
    pending_con_id = 2_003
    decision = derive(
        positions=(
            stock_position("300"),
            option_position(OptionRight.CALL, "-1", con_id=2_002),
            option_position(OptionRight.CALL, "-1", con_id=pending_con_id),
        ),
        orders=(
            option_order(
                OptionRight.CALL,
                con_id=pending_con_id,
                quantity="2",
                filled="1",
                remaining="1",
                lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            ),
        ),
    )

    assert decision.stage is WheelStage.SHORT_CALL_PENDING
    assert decision.short_call_contracts == Decimal("2")
    assert decision.pending_call_contracts == Decimal("1")
    assert decision.unencumbered_shares == Decimal("0")


def test_partial_fill_cannot_match_a_different_option_contract() -> None:
    pending_con_id = 2_099
    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1", con_id=2_001),),
        orders=(
            option_order(
                OptionRight.PUT,
                con_id=pending_con_id,
                quantity="2",
                filled="1",
                remaining="1",
                lifecycle=OrderLifecycle.PARTIALLY_FILLED,
            ),
        ),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any(f"conId {pending_con_id}" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    ("shares", "calls", "expected_unencumbered"),
    [("100", "-1", "0"), ("250", "-2", "50")],
)
def test_open_short_calls_report_exact_unencumbered_shares(
    shares: str,
    calls: str,
    expected_unencumbered: str,
) -> None:
    decision = derive(
        positions=(
            stock_position(shares),
            option_position(OptionRight.CALL, calls),
        )
    )

    assert decision.stage is WheelStage.SHORT_CALL_OPEN
    assert decision.unencumbered_shares == Decimal(expected_unencumbered)


def test_nonstandard_deliverable_is_not_eligible_even_when_share_coverage_exists() -> None:
    decision = derive(
        positions=(
            stock_position("50"),
            option_position(
                OptionRight.CALL,
                "-1",
                multiplier="100",
                deliverable_shares="50",
            ),
        ),
        eligible_call_multiplier=Decimal("50"),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.unencumbered_shares == Decimal("0")
    assert any("verified standard" in reason for reason in decision.reasons)


@pytest.mark.parametrize("right", [OptionRight.PUT, OptionRight.CALL])
@pytest.mark.parametrize("pending", [False, True])
def test_short_options_require_verified_standard_deliverable_evidence(
    right: OptionRight,
    pending: bool,
) -> None:
    positions: tuple[BrokerPosition, ...]
    orders: tuple[BrokerOrder, ...]
    if pending:
        positions = (stock_position("100"),) if right is OptionRight.CALL else ()
        orders = (option_order(right, deliverable_verified=False),)
    else:
        stock = (stock_position("100"),) if right is OptionRight.CALL else ()
        positions = (*stock, option_position(right, "-1", deliverable_verified=False))
        orders = ()

    decision = derive(positions=positions, orders=orders)

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.unencumbered_shares == Decimal("0")
    assert any("verified standard" in reason for reason in decision.reasons)


def test_call_coverage_requires_the_exact_underlying_contract_id() -> None:
    decision = derive(
        positions=(
            stock_position("100"),
            option_position(
                OptionRight.CALL,
                "-1",
                underlying_con_id=STOCK_CON_ID + 1,
            ),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("full coverage" in reason for reason in decision.reasons)


def test_call_coverage_requires_the_exact_underlying_currency() -> None:
    decision = derive(
        positions=(
            stock_position("100", currency="CAD"),
            option_position(OptionRight.CALL, "-1", currency="USD"),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("in USD" in reason for reason in decision.reasons)


def test_call_coverage_never_pools_nonfungible_stock() -> None:
    decision = derive(
        positions=(
            stock_position("50"),
            stock_position("50", con_id=STOCK_CON_ID + 1),
            option_position(OptionRight.CALL, "-1"),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.stock_shares == Decimal("100")
    assert decision.unencumbered_shares == Decimal("0")
    assert any("full coverage" in reason for reason in decision.reasons)


def test_call_coverage_rejects_duplicate_rows_of_the_same_stock_instrument() -> None:
    decision = derive(
        positions=(
            stock_position("40"),
            stock_position("60"),
            option_position(OptionRight.CALL, "-1"),
        )
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.unencumbered_shares == Decimal("0")
    assert any("duplicate position rows" in reason for reason in decision.reasons)


def test_instrument_and_account_codes_normalize_before_reconciliation() -> None:
    stock = stock_position(
        "100",
        account_id=f"  {ACCOUNT_ID}  ",
        symbol=" test ",
        currency=" usd ",
    )
    call = option_position(
        OptionRight.CALL,
        "-1",
        account_id=f"  {ACCOUNT_ID}  ",
        symbol=" test ",
        currency=" usd ",
    )

    decision = derive(
        positions=(stock, call),
        expected_account_id=f"  {ACCOUNT_ID}  ",
    )

    assert stock.account_id == ACCOUNT_ID
    assert stock.contract.symbol == SYMBOL
    assert stock.contract.currency == "USD"
    assert call.account_id == ACCOUNT_ID
    assert call.contract.symbol == SYMBOL
    assert call.contract.currency == "USD"
    assert call.contract.trading_class == SYMBOL
    assert decision.stage is WheelStage.SHORT_CALL_OPEN


def test_broker_position_rejects_a_blank_account_id() -> None:
    with pytest.raises(ValueError, match="account_id must not be blank"):
        stock_position("100", account_id="   ")


def test_buy_to_close_against_a_short_put_is_put_close_pending() -> None:
    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1"),),
        orders=(option_order(OptionRight.PUT, side=OrderSide.BUY),),
    )

    assert decision.stage is WheelStage.PUT_CLOSE_PENDING
    assert decision.opening_actions_locked is True


def test_buy_to_close_against_a_short_call_is_call_close_pending() -> None:
    decision = derive(
        positions=(
            stock_position("100"),
            option_position(OptionRight.CALL, "-1"),
        ),
        orders=(option_order(OptionRight.CALL, side=OrderSide.BUY),),
    )

    assert decision.stage is WheelStage.CALL_CLOSE_PENDING
    assert decision.opening_actions_locked is True


def test_partial_assignment_is_assignment_reconciling_not_manual_review() -> None:
    decision = derive(partial_assignment=True)

    assert decision.stage is WheelStage.ASSIGNMENT_RECONCILING
    assert decision.opening_actions_locked is True
    assert decision.manual_review_required is False
    assert any("assignment" in reason.lower() for reason in decision.reasons)


def test_buy_to_close_cannot_match_a_different_option_contract() -> None:
    closing_con_id = 2_099
    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1", con_id=2_001),),
        orders=(
            option_order(
                OptionRight.PUT,
                side=OrderSide.BUY,
                con_id=closing_con_id,
            ),
        ),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any(f"conId {closing_con_id}" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    ("positions", "expected_stage"),
    [
        ((), WheelStage.FLAT),
        ((stock_position("100"),), WheelStage.LONG_STOCK),
    ],
)
def test_cancelled_unfilled_opening_order_no_longer_affects_state(
    positions: tuple[BrokerPosition, ...],
    expected_stage: WheelStage,
) -> None:
    right = OptionRight.PUT if expected_stage is WheelStage.FLAT else OptionRight.CALL
    decision = derive(
        positions=positions,
        orders=(
            option_order(
                right,
                lifecycle=OrderLifecycle.CANCELLED,
            ),
        ),
    )

    assert decision.stage is expected_stage


def test_cancelled_remainder_after_partial_fill_leaves_open_option() -> None:
    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1"),),
        orders=(
            option_order(
                OptionRight.PUT,
                quantity="2",
                filled="1",
                remaining="1",
                lifecycle=OrderLifecycle.CANCELLED,
            ),
        ),
    )

    assert decision.stage is WheelStage.SHORT_PUT_OPEN


def test_fully_reconciled_assignment_outcomes_follow_broker_positions() -> None:
    put_assignment = derive(positions=(stock_position("100"),))
    called_away = derive()

    assert put_assignment.stage is WheelStage.LONG_STOCK
    assert called_away.stage is WheelStage.FLAT


@pytest.mark.parametrize(
    (
        "partial_assignment",
        "corporate_action_warning",
        "unexplained_mismatch",
        "reconciliation_status",
    ),
    [
        (False, True, False, ReconciliationStatus.RECONCILED),
        (False, False, True, ReconciliationStatus.RECONCILED),
        (False, False, False, ReconciliationStatus.PENDING),
        (False, False, False, ReconciliationStatus.MANUAL_REVIEW),
    ],
)
def test_explicit_unsafe_evidence_requires_manual_review(
    partial_assignment: bool,
    corporate_action_warning: bool,
    unexplained_mismatch: bool,
    reconciliation_status: ReconciliationStatus,
) -> None:
    decision = derive(
        partial_assignment=partial_assignment,
        corporate_action_warning=corporate_action_warning,
        unexplained_mismatch=unexplained_mismatch,
        reconciliation_status=reconciliation_status,
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.manual_review_required is True
    assert decision.opening_actions_locked is True
    assert decision.eligible_action is None
    assert decision.reasons


@pytest.mark.parametrize(
    "positions",
    [
        (stock_position("99"), option_position(OptionRight.CALL, "-1")),
        (stock_position("199"), option_position(OptionRight.CALL, "-2")),
    ],
)
def test_uncovered_short_calls_require_manual_review(
    positions: tuple[BrokerPosition, ...],
) -> None:
    decision = derive(positions=positions)

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.unencumbered_shares == Decimal("0")
    assert any("full coverage" in reason for reason in decision.reasons)


def test_uncovered_pending_call_requires_manual_review() -> None:
    decision = derive(
        positions=(stock_position("99"),),
        orders=(option_order(OptionRight.CALL),),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.pending_call_contracts == Decimal("1")


@pytest.mark.parametrize(
    "positions",
    [
        (stock_position("100"), option_position(OptionRight.PUT, "-1")),
        (
            option_position(OptionRight.PUT, "-1"),
            option_position(OptionRight.CALL, "-1", con_id=2_003),
        ),
    ],
)
def test_conflicting_wheel_exposures_require_manual_review(
    positions: tuple[BrokerPosition, ...],
) -> None:
    decision = derive(positions=positions)

    assert decision.stage is WheelStage.MANUAL_REVIEW


def test_evidence_spanning_expected_and_wrong_accounts_requires_manual_review() -> None:
    decision = derive(
        positions=(stock_position("100"),),
        orders=(option_order(OptionRight.CALL, account_id="DU7654321"),),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("expected account" in reason for reason in decision.reasons)


def test_homogeneous_wrong_account_position_evidence_requires_manual_review() -> None:
    decision = derive(
        positions=(stock_position("100", account_id="DU7654321"),),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.eligible_action is None
    assert any("expected account" in reason for reason in decision.reasons)


def test_homogeneous_wrong_account_order_evidence_requires_manual_review() -> None:
    decision = derive(
        orders=(option_order(OptionRight.PUT, account_id="DU7654321"),),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert decision.eligible_action is None
    assert any("expected account" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "positions",
    [
        (stock_position("-100"),),
        (option_position(OptionRight.PUT, "1"),),
        (option_position(OptionRight.PUT, "-0.5"),),
    ],
)
def test_positions_outside_the_mvp_wheel_model_require_manual_review(
    positions: tuple[BrokerPosition, ...],
) -> None:
    assert derive(positions=positions).stage is WheelStage.MANUAL_REVIEW


def test_buy_order_without_a_matching_short_option_requires_manual_review() -> None:
    decision = derive(orders=(option_order(OptionRight.PUT, side=OrderSide.BUY),))

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("no matching short option" in reason for reason in decision.reasons)


def test_oversized_buy_to_close_order_requires_manual_review() -> None:
    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1"),),
        orders=(
            option_order(
                OptionRight.PUT,
                side=OrderSide.BUY,
                quantity="2",
                remaining="2",
            ),
        ),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW


def test_other_symbols_do_not_contaminate_the_decision() -> None:
    decision = derive(
        positions=(stock_position("100", symbol="OTHER"),),
        orders=(option_order(OptionRight.PUT, symbol="OTHER"),),
        matched_order_identities=frozenset(),
    )

    assert decision.stage is WheelStage.FLAT
    assert decision.stock_shares == Decimal("0")


def test_same_contract_id_with_conflicting_metadata_requires_manual_review() -> None:
    order = option_order(OptionRight.PUT, side=OrderSide.BUY)
    assert isinstance(order.contract, OptionContract)
    conflicting_order = order.model_copy(
        update={
            "contract": order.contract.model_copy(
                update={"strike": order.contract.strike - Decimal("1")}
            )
        }
    )

    decision = derive(
        positions=(option_position(OptionRight.PUT, "-1"),),
        orders=(conflicting_order,),
    )

    assert decision.stage is WheelStage.MANUAL_REVIEW
    assert any("metadata disagrees" in reason for reason in decision.reasons)
