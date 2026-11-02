"""E-0 pins for exit-risk evidence and reconciliation readiness."""

from __future__ import annotations

from decimal import Decimal

import pytest

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import (
    OptionRight,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    RiskCheckStatus,
)
from chronos.domain.models import BrokerOrder, BrokerPosition, OptionContract, UnderlyingContract
from chronos.orders.evidence import BrokerRiskEvidenceProvider
from chronos.orders.intent import build_option_intent, build_stock_intent
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from tests.support.order_fakes import (
    FIXED_NOW,
    PAPER_ACCOUNT,
    FakeBroker,
    option_contract,
    paper_settings,
    stock_contract,
)
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY


def _stock_close(quantity: int = 1):
    return build_stock_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.CLOSE_LONG_STOCK,
        contract=stock_contract(),
        quantity=quantity,
        limit_price=Decimal("100"),
    )


def _option_close(quantity: int, *, con_id: int = 222):
    return build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.CLOSE_SHORT_OPTION,
        contract=option_contract(con_id=con_id),
        quantity=quantity,
        limit_price=Decimal("0.50"),
    )


def _manual_review(readiness: ReconciliationReadiness) -> None:
    snapshot = readiness.snapshot()
    assert readiness.complete(
        expected_generation=snapshot.generation,
        status=ReconciliationStatus.MANUAL_REVIEW,
        reason="portfolio reconciliation requires manual review",
    )


def _reconciled_proven_state_detail(evidence: RiskEvidence) -> str:
    decision = OrderRiskEngine(paper_settings(), persistence_key=TEST_PERSISTENCE_KEY).evaluate(
        _stock_close(), evidence, now=FIXED_NOW
    )
    check = next(check for check in decision.checks if check.name == "reconciled_proven_state")
    assert check.status is RiskCheckStatus.FAIL
    return check.detail


def _position(contract: OptionContract | UnderlyingContract, quantity: str) -> BrokerPosition:
    return BrokerPosition(
        account_id=PAPER_ACCOUNT,
        contract=contract,
        quantity=Decimal(quantity),
        average_cost=Decimal("100"),
        market_price=Decimal("100"),
    )


def _resting_order(
    contract: OptionContract | UnderlyingContract,
    side: OrderSide,
    quantity: str,
    *,
    account_id: str = PAPER_ACCOUNT,
    broker_order_id: int = 7001,
) -> BrokerOrder:
    remaining = Decimal(quantity)
    return BrokerOrder(
        broker_order_id=broker_order_id,
        permanent_id=broker_order_id + 10000,
        client_id=17,
        account_id=account_id,
        contract=contract,
        side=side,
        quantity=remaining,
        filled_quantity=Decimal("0"),
        remaining_quantity=remaining,
        limit_price=Decimal("1"),
        lifecycle=OrderLifecycle.SUBMITTED,
        transmit=False,
    )


def _evaluate(
    intent: object,
    *,
    positions: tuple[BrokerPosition, ...] = (),
    open_orders: tuple[BrokerOrder, ...] = (),
) -> tuple[RiskEvidence, object]:
    readiness = ReconciliationReadiness()
    broker = FakeBroker(positions=positions, open_orders=open_orders)
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    try:
        snapshot = readiness.snapshot()
        assert readiness.complete(
            expected_generation=snapshot.generation,
            status=ReconciliationStatus.RECONCILED,
            reason="test broker/local parity",
            reconciled_at=FIXED_NOW,
        )
        evidence = BrokerRiskEvidenceProvider(
            connection=connection,
            settings=paper_settings(),
            reconciliation_readiness=readiness,
        ).gather(intent, now=FIXED_NOW)  # type: ignore[arg-type]
        decision = OrderRiskEngine(paper_settings(), persistence_key=TEST_PERSISTENCE_KEY).evaluate(
            intent, evidence, now=FIXED_NOW
        )  # type: ignore[arg-type]
        return evidence, decision
    finally:
        connection.close()


def _risk_check(decision: object, name: str) -> object:
    return next(check for check in decision.checks if check.name == name)  # type: ignore[attr-defined]


def _stock_case(
    case: str,
) -> tuple[tuple[BrokerPosition, ...], tuple[BrokerOrder, ...], int, RiskCheckStatus]:
    stock = stock_contract()
    call_222 = option_contract(right=OptionRight.CALL, con_id=222)
    call_223 = option_contract(right=OptionRight.CALL, con_id=223)
    put_333 = option_contract(right=OptionRight.PUT, con_id=333)
    own = PAPER_ACCOUNT
    none: tuple[BrokerOrder, ...] = ()
    stock_hundred = (_position(stock, "100"),)

    if case == "B1":
        return (
            stock_hundred,
            (_resting_order(stock, OrderSide.SELL, "100", account_id=own),),
            100,
            RiskCheckStatus.FAIL,
        )
    if case == "B5":
        return (
            (*stock_hundred, _position(call_222, "-1")),
            none,
            100,
            RiskCheckStatus.FAIL,
        )
    if case == "B6":
        return (
            stock_hundred,
            (_resting_order(call_222, OrderSide.SELL, "1"),),
            100,
            RiskCheckStatus.FAIL,
        )
    if case == "G1":
        return (
            stock_hundred,
            (
                _resting_order(stock, OrderSide.SELL, "60", broker_order_id=7001),
                _resting_order(stock, OrderSide.SELL, "40", broker_order_id=7002),
            ),
            1,
            RiskCheckStatus.FAIL,
        )
    if case == "G2":
        return (
            stock_hundred,
            (_resting_order(stock, OrderSide.SELL, "150", account_id="OTHER"),),
            1,
            RiskCheckStatus.FAIL,
        )
    if case == "G3":
        return (
            stock_hundred,
            (_resting_order(stock, OrderSide.BUY, "100"),),
            100,
            RiskCheckStatus.PASS,
        )
    if case == "G4":
        return (
            (_position(stock, "300"), _position(call_222, "-1"), _position(call_223, "-2")),
            none,
            1,
            RiskCheckStatus.FAIL,
        )
    if case in {"G5-100", "G5-101"}:
        return (
            (_position(stock, "300"), _position(call_222, "-2")),
            none,
            100 if case == "G5-100" else 101,
            RiskCheckStatus.PASS if case == "G5-100" else RiskCheckStatus.FAIL,
        )
    if case == "G6":
        return (
            (_position(stock, "100"), _position(call_222, "-2")),
            none,
            1,
            RiskCheckStatus.FAIL,
        )
    if case == "G9":
        other_stock = stock_contract(con_id=1000)
        return (
            stock_hundred,
            (
                _resting_order(other_stock, OrderSide.SELL, "100"),
                _resting_order(put_333, OrderSide.SELL, "1", broker_order_id=7002),
            ),
            100,
            RiskCheckStatus.PASS,
        )
    if case == "G10":
        return stock_hundred, none, 100, RiskCheckStatus.PASS
    raise AssertionError(f"unknown stock case: {case}")


@pytest.mark.parametrize(
    "case",
    (
        "B1",
        "B5",
        "B6",
        "G1",
        "G2",
        "G3",
        "G4",
        "G5-100",
        "G5-101",
        "G6",
        "G9",
        "G10",
    ),
)
def test_stock_close_reserves_calls_and_all_resting_stock_sells(case: str) -> None:
    positions, open_orders, quantity, expected = _stock_case(case)
    evidence, decision = _evaluate(
        _stock_close(quantity), positions=positions, open_orders=open_orders
    )
    check = _risk_check(decision, "stock_no_short")

    assert evidence.shares_reserved_by_other_orders == Decimal("0")
    assert check.status is expected
    assert (
        evidence.stock_sell_reserved_shares
        == {
            "B1": Decimal("100"),
            "B5": Decimal("0"),
            "B6": Decimal("0"),
            "G1": Decimal("100"),
            "G2": Decimal("150"),
            "G3": Decimal("0"),
            "G4": Decimal("0"),
            "G5-100": Decimal("0"),
            "G5-101": Decimal("0"),
            "G6": Decimal("0"),
            "G9": Decimal("0"),
            "G10": Decimal("0"),
        }[case]
    )
    if expected is RiskCheckStatus.FAIL:
        expected_figures = {
            "B1": ("settled=100", "covering=0", "pending_calls=0", "resting_sells=100", "bound=0"),
            "B5": ("settled=100", "covering=100", "pending_calls=0", "resting_sells=0", "bound=0"),
            "B6": ("settled=100", "covering=0", "pending_calls=100", "resting_sells=0", "bound=0"),
            "G1": ("settled=100", "covering=0", "pending_calls=0", "resting_sells=100", "bound=0"),
            "G2": ("settled=100", "covering=0", "pending_calls=0", "resting_sells=150", "bound=0"),
            "G4": ("settled=300", "covering=300", "pending_calls=0", "resting_sells=0", "bound=0"),
            "G5-101": (
                "settled=300",
                "covering=200",
                "pending_calls=0",
                "resting_sells=0",
                "bound=100",
            ),
            "G6": ("settled=100", "covering=200", "pending_calls=0", "resting_sells=0", "bound=0"),
        }
        for figure in expected_figures[case]:
            assert figure in check.detail
        assert PAPER_ACCOUNT not in check.detail
        if case == "G6":
            assert "encumbrances exceed settled long shares" in check.detail
    if case in {"B1", "B5"}:
        assert decision.overall is RiskCheckStatus.FAIL


def test_stock_close_model_constructed_negative_reservation_fails_without_raising() -> None:
    evidence, _ = _evaluate(_stock_close(), positions=(_position(stock_contract(), "1"),))
    malformed = RiskEvidence.model_construct(
        **{**evidence.__dict__, "shares_covering_short_calls": Decimal("-1")}
    )
    decision = OrderRiskEngine(paper_settings(), persistence_key=TEST_PERSISTENCE_KEY).evaluate(
        _stock_close(), malformed, now=FIXED_NOW
    )

    check = _risk_check(decision, "stock_no_short")
    assert check.status is RiskCheckStatus.FAIL


def test_stock_close_holding_bound_uses_exact_underlying_and_currency() -> None:
    target_stock = stock_contract(con_id=999)
    same_symbol_eur_stock = stock_contract(con_id=1000).model_copy(update={"currency": "EUR"})
    evidence, decision = _evaluate(
        _stock_close(150),
        positions=(
            _position(target_stock, "100"),
            _position(same_symbol_eur_stock, "100"),
        ),
    )

    check = _risk_check(decision, "stock_no_short")
    assert evidence.settled_long_shares == Decimal("100")
    assert check.status is RiskCheckStatus.FAIL
    assert "bound=100" in check.detail


def test_stock_close_refuses_unverified_call_deliverable() -> None:
    stock = stock_contract(con_id=999)
    unverified_call = option_contract(right=OptionRight.CALL, con_id=222).model_copy(
        update={"deliverable_verified": False, "deliverable_shares": None}
    )
    evidence, decision = _evaluate(
        _stock_close(50),
        positions=(_position(stock, "150"), _position(unverified_call, "-1")),
    )

    check = _risk_check(decision, "stock_no_short")
    assert check.status is RiskCheckStatus.FAIL
    assert unverified_call.symbol in check.detail
    assert unverified_call.expiration.isoformat() in check.detail
    assert str(unverified_call.strike) in check.detail
    assert PAPER_ACCOUNT not in check.detail
    assert "222" not in check.detail
    assert "999" not in check.detail

    assert evidence.shares_covering_short_calls == Decimal("0")


def test_stock_close_refuses_unverified_pending_call_deliverable() -> None:
    stock = stock_contract(con_id=999)
    unverified_call = option_contract(right=OptionRight.CALL, con_id=222).model_copy(
        update={"deliverable_verified": False, "deliverable_shares": None}
    )
    evidence, decision = _evaluate(
        _stock_close(50),
        positions=(_position(stock, "150"),),
        open_orders=(_resting_order(unverified_call, OrderSide.SELL, "1"),),
    )

    check = _risk_check(decision, "stock_no_short")
    assert check.status is RiskCheckStatus.FAIL
    assert unverified_call.symbol in check.detail
    assert unverified_call.expiration.isoformat() in check.detail
    assert str(unverified_call.strike) in check.detail
    assert PAPER_ACCOUNT not in check.detail
    assert "222" not in check.detail
    assert "999" not in check.detail

    assert evidence.shares_reserved_for_pending_calls == Decimal("0")


def test_stock_close_requires_call_currency_to_match_exactly() -> None:
    stock = stock_contract(con_id=999)
    eur_call = option_contract(right=OptionRight.CALL, con_id=222).model_copy(
        update={
            "currency": "EUR",
            "deliverable_verified": False,
            "deliverable_shares": None,
        }
    )
    _, decision = _evaluate(
        _stock_close(50),
        positions=(_position(stock, "150"), _position(eur_call, "-1")),
    )

    check = _risk_check(decision, "stock_no_short")
    assert check.status is RiskCheckStatus.FAIL
    assert eur_call.symbol in check.detail
    assert eur_call.expiration.isoformat() in check.detail
    assert str(eur_call.strike) in check.detail
    assert "222" not in check.detail
    assert "999" not in check.detail


def test_stock_close_uses_verified_adjusted_call_share_deliverable() -> None:
    stock = stock_contract(con_id=999)
    adjusted_call = option_contract(right=OptionRight.CALL, con_id=222).model_copy(
        update={"deliverable_shares": Decimal("150")}
    )
    evidence, decision = _evaluate(
        _stock_close(51),
        positions=(_position(stock, "200"), _position(adjusted_call, "-1")),
    )

    check = _risk_check(decision, "stock_no_short")
    assert evidence.shares_covering_short_calls == Decimal("150")
    assert check.status is RiskCheckStatus.FAIL
    assert "bound=50" in check.detail


def _option_case(
    case: str,
) -> tuple[
    tuple[BrokerPosition, ...],
    tuple[BrokerOrder, ...],
    int,
    RiskCheckStatus,
    int,
]:
    put_222 = option_contract(con_id=222)
    put_223 = option_contract(con_id=223)
    if case == "B3":
        return (
            (_position(put_222, "-1"),),
            (_resting_order(put_222, OrderSide.BUY, "1"),),
            1,
            RiskCheckStatus.FAIL,
            1,
        )
    if case == "G7":
        return (
            (_position(put_222, "-2"),),
            (_resting_order(put_222, OrderSide.BUY, "1"),),
            2,
            RiskCheckStatus.FAIL,
            1,
        )
    if case == "G8":
        return (
            (_position(put_222, "-2"),),
            (
                _resting_order(put_222, OrderSide.BUY, "2", broker_order_id=7001),
                _resting_order(
                    put_222, OrderSide.BUY, "1", account_id="OTHER", broker_order_id=7002
                ),
            ),
            1,
            RiskCheckStatus.FAIL,
            3,
        )
    if case == "G8-foreign":
        return (
            (_position(put_222, "-2"),),
            (
                _resting_order(put_222, OrderSide.BUY, "1", broker_order_id=7001),
                _resting_order(
                    put_222, OrderSide.BUY, "1", account_id="OTHER", broker_order_id=7002
                ),
            ),
            1,
            RiskCheckStatus.FAIL,
            2,
        )
    if case == "G7-pass-control":
        return (
            (_position(put_222, "-2"),),
            (_resting_order(put_222, OrderSide.BUY, "1"),),
            1,
            RiskCheckStatus.PASS,
            1,
        )
    if case == "G9-other-conid":
        return (
            (_position(put_222, "-1"),),
            (_resting_order(put_223, OrderSide.BUY, "1"),),
            1,
            RiskCheckStatus.PASS,
            0,
        )
    if case == "G9-resting-sell":
        return (
            (_position(put_222, "-1"),),
            (_resting_order(put_222, OrderSide.SELL, "1"),),
            1,
            RiskCheckStatus.PASS,
            0,
        )
    if case == "G10":
        return (_position(put_222, "-1"),), (), 1, RiskCheckStatus.PASS, 0
    raise AssertionError(f"unknown option case: {case}")


@pytest.mark.parametrize(
    "case",
    (
        "B3",
        "G7",
        "G8",
        "G8-foreign",
        "G7-pass-control",
        "G9-other-conid",
        "G9-resting-sell",
        "G10",
    ),
)
def test_option_close_reserves_all_resting_buys_for_exact_contract(case: str) -> None:
    positions, open_orders, quantity, expected, expected_reserved = _option_case(case)
    evidence, decision = _evaluate(
        _option_close(quantity), positions=positions, open_orders=open_orders
    )
    check = _risk_check(decision, "held_position_sufficiency")

    assert check.status is expected
    assert evidence.option_buy_reserved_contracts == expected_reserved
    if expected is RiskCheckStatus.FAIL:
        assert "held=" in check.detail
        assert "resting_buys=" in check.detail
        assert "bound=" in check.detail
        assert PAPER_ACCOUNT not in check.detail


def test_option_close_refuses_non_integral_resting_buy_quantity() -> None:
    put = option_contract(con_id=222)
    evidence, decision = _evaluate(
        _option_close(1),
        positions=(_position(put, "-1"),),
        open_orders=(_resting_order(put, OrderSide.BUY, "0.5"),),
    )

    check = _risk_check(decision, "held_position_sufficiency")
    assert evidence.option_buy_reserved_contracts == Decimal("0.5")
    assert check.status is RiskCheckStatus.FAIL
    assert "non-integral" in check.detail
    assert "held=1" in check.detail
    assert "resting_buys=0.5" in check.detail
    assert "bound=0" in check.detail
    assert PAPER_ACCOUNT not in check.detail


def _opening_scenario(case: str):
    stock = stock_contract()
    call = option_contract(right=OptionRight.CALL, con_id=333)
    put = option_contract()
    positions = (_position(stock, "100"),)
    if case.startswith("B5"):
        positions = (
            *positions,
            _position(option_contract(right=OptionRight.CALL, con_id=222), "-1"),
        )
        open_orders: tuple[BrokerOrder, ...] = ()
    elif case.startswith("B6"):
        open_orders = (_resting_order(call, OrderSide.SELL, "1"),)
    elif case == "stock-buy-with-resting-sell" or case == "stock-sell-open-covered-call":
        open_orders = (_resting_order(stock, OrderSide.SELL, "100"),)
    else:
        open_orders = ()

    if case.endswith("open-short-put"):
        intent = build_option_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_SHORT_PUT,
            contract=put,
            quantity=1,
            limit_price=Decimal("1.20"),
        )
    elif case.endswith("open-covered-call"):
        intent = build_option_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_COVERED_CALL,
            contract=call,
            quantity=1,
            limit_price=Decimal("2.00"),
        )
    else:
        intent = build_stock_intent(
            account_id=PAPER_ACCOUNT,
            intent=OrderIntent.OPEN_LONG_STOCK,
            contract=stock,
            quantity=1,
            limit_price=Decimal("100"),
        )
    return intent, positions, open_orders


_OPENING_CHECK_SIGNATURES = {
    "B5-open-short-put": {
        ("cash_secured_put", "PASS"),
        ("concentration", "PASS"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_gross_assignment_usd", "PASS"),
        ("max_open_short_option_contracts", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "B5-open-covered-call": {
        ("concentration", "PASS"),
        ("covered_call_coverage", "FAIL"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "B6-open-short-put": {
        ("cash_secured_put", "PASS"),
        ("concentration", "PASS"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_gross_assignment_usd", "PASS"),
        ("max_open_short_option_contracts", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "B6-open-covered-call": {
        ("concentration", "PASS"),
        ("covered_call_coverage", "FAIL"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "clean-open-short-put": {
        ("cash_secured_put", "PASS"),
        ("concentration", "PASS"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_gross_assignment_usd", "PASS"),
        ("max_open_short_option_contracts", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "clean-open-covered-call": {
        ("concentration", "PASS"),
        ("covered_call_coverage", "PASS"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "stock-sell-open-covered-call": {
        ("concentration", "PASS"),
        ("covered_call_coverage", "PASS"),
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("standard_deliverable_verified", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
    "stock-buy-with-resting-sell": {
        ("limit_only", "PASS"),
        ("market_open", "UNKNOWN"),
        ("max_contracts_per_order", "PASS"),
        ("max_opening_orders_per_day", "UNKNOWN"),
        ("reconciled_proven_state", "PASS"),
        ("stock_cash_sufficiency", "PASS"),
        ("stock_whole_shares", "PASS"),
        ("symbol_family_eligibility", "PASS"),
    },
}


@pytest.mark.parametrize("case", tuple(_OPENING_CHECK_SIGNATURES))
def test_e0_preserves_every_open_check_name_and_status(case: str) -> None:
    intent, positions, open_orders = _opening_scenario(case)
    _, decision = _evaluate(intent, positions=positions, open_orders=open_orders)
    signature = {(check.name, check.status.value) for check in decision.checks}

    assert signature == _OPENING_CHECK_SIGNATURES[case]


def test_manual_review_survives_a_consistent_gather_bracket() -> None:
    readiness = ReconciliationReadiness()
    broker = FakeBroker()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    try:
        _manual_review(readiness)
        evidence = BrokerRiskEvidenceProvider(
            connection=connection,
            settings=paper_settings(),
            reconciliation_readiness=readiness,
        ).gather(_stock_close(), now=FIXED_NOW)

        assert evidence.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
        assert evidence.reconciliation_generation is None
        assert evidence.reconciliation_session_id is None
        assert (
            _reconciled_proven_state_detail(evidence)
            == "reconciliation is MANUAL_REVIEW, not RECONCILED"
        )
    finally:
        connection.close()


def test_manual_review_republished_in_a_new_generation_is_not_bracketed() -> None:
    readiness = ReconciliationReadiness()
    broker = FakeBroker()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    try:
        _manual_review(readiness)
        original_positions = broker.positions

        async def positions_after_a_new_manual_review_generation() -> tuple[BrokerPosition, ...]:
            generation = readiness.begin_reconciliation("connection changed during gather")
            assert readiness.complete(
                expected_generation=generation,
                status=ReconciliationStatus.MANUAL_REVIEW,
                reason="portfolio reconciliation requires manual review",
            )
            return await original_positions()

        broker.positions = positions_after_a_new_manual_review_generation  # type: ignore[method-assign]
        evidence = BrokerRiskEvidenceProvider(
            connection=connection,
            settings=paper_settings(),
            reconciliation_readiness=readiness,
        ).gather(_stock_close(), now=FIXED_NOW)

        assert readiness.snapshot().status is ReconciliationStatus.MANUAL_REVIEW
        assert evidence.reconciliation_status is ReconciliationStatus.PENDING
        assert evidence.reconciliation_generation is None
        assert evidence.reconciliation_session_id is None
    finally:
        connection.close()


def test_consumed_reconciliation_during_positions_read_is_not_bracketed() -> None:
    readiness = ReconciliationReadiness()
    broker = FakeBroker()
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    try:
        generation = readiness.begin_reconciliation("publish test readiness")
        assert readiness.complete(
            expected_generation=generation,
            status=ReconciliationStatus.RECONCILED,
            reason="full broker/local parity proven",
            reconciled_at=FIXED_NOW,
            published_at=FIXED_NOW,
        )
        original_positions = broker.positions

        async def positions_after_consuming_readiness() -> tuple[BrokerPosition, ...]:
            with readiness.submission_guard(expected_generation=generation) as acquired:
                assert acquired
                return await original_positions()

        broker.positions = positions_after_consuming_readiness  # type: ignore[method-assign]
        evidence = BrokerRiskEvidenceProvider(
            connection=connection,
            settings=paper_settings(),
            reconciliation_readiness=readiness,
        ).gather(_stock_close(), now=FIXED_NOW)

        assert evidence.reconciliation_status is ReconciliationStatus.PENDING
        assert evidence.reconciliation_generation is None
        assert evidence.reconciliation_session_id is None
        _reconciled_proven_state_detail(evidence)
    finally:
        connection.close()
