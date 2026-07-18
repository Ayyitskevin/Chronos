"""Structured risk engine coverage across intents and tri-state outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tests.support.order_fakes import PAPER_ACCOUNT, option_contract, paper_settings, stock_contract

from chronos.domain.enums import (
    OrderIntent,
    ProductFamily,
    ReconciliationStatus,
    RiskCheckStatus,
)
from chronos.domain.models import AccountSummary
from chronos.orders.intent import build_option_intent, build_stock_intent
from chronos.orders.risk import OrderRiskEngine, RiskEvidence
from chronos.services.trading_hours import session_for

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _account(cash: str = "80000", nlv: str = "100000") -> AccountSummary:
    return AccountSummary(
        account_id=PAPER_ACCOUNT,
        net_liquidation=Decimal(nlv),
        total_cash=Decimal(cash),
        buying_power=Decimal("160000"),
        as_of=_NOW,
    )


def _open_session() -> object:
    return session_for(ProductFamily.OPTION, now=_NOW, broker_confirms_open=True)


def _status(decision: object, name: str) -> RiskCheckStatus:
    return next(c.status for c in decision.checks if c.name == name)  # type: ignore[attr-defined]


def test_unknown_check_makes_overall_fail() -> None:
    engine = OrderRiskEngine(paper_settings())
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "C" * 32,
    )
    # AMBIGUOUS session (no broker_confirms_open) -> market_open UNKNOWN.
    ambiguous = session_for(ProductFamily.OPTION, now=_NOW, broker_confirms_open=None)
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=ambiguous,
        ),
        now=_NOW,
    )
    assert _status(decision, "market_open") is RiskCheckStatus.UNKNOWN
    assert decision.overall is RiskCheckStatus.FAIL


def test_unreconciled_fails() -> None:
    engine = OrderRiskEngine(paper_settings())
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "D" * 32,
    )
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.MANUAL_REVIEW,
            session=_open_session(),
        ),
        now=_NOW,
    )
    assert _status(decision, "reconciled_proven_state") is RiskCheckStatus.FAIL
    assert not decision.approved


def test_covered_call_uncovered_fails() -> None:
    from chronos.domain.enums import OptionRight

    engine = OrderRiskEngine(paper_settings())
    call = option_contract(right=OptionRight.CALL, con_id=222)
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_COVERED_CALL,
        contract=call,
        quantity=1,
        limit_price=Decimal("2.00"),
        correlation_id="CHR-ORD-" + "E" * 32,
    )
    # No settled shares -> not covered.
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            settled_long_shares=Decimal("0"),
        ),
        now=_NOW,
    )
    assert _status(decision, "covered_call_coverage") is RiskCheckStatus.FAIL


def test_covered_call_fully_covered_passes() -> None:
    from chronos.domain.enums import OptionRight

    engine = OrderRiskEngine(paper_settings())
    call = option_contract(right=OptionRight.CALL, con_id=222)
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_COVERED_CALL,
        contract=call,
        quantity=1,
        limit_price=Decimal("2.00"),
        correlation_id="CHR-ORD-" + "F" * 32,
    )
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            settled_long_shares=Decimal("100"),
        ),
        now=_NOW,
    )
    assert _status(decision, "covered_call_coverage") is RiskCheckStatus.PASS


def test_buy_to_close_requires_held_short() -> None:
    engine = OrderRiskEngine(paper_settings())
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.CLOSE_SHORT_OPTION,
        contract=option_contract(),
        quantity=2,
        limit_price=Decimal("0.50"),
        correlation_id="CHR-ORD-" + "0" * 32,
    )
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            held_short_option_contracts=1,  # closing 2 but only 1 held
        ),
        now=_NOW,
    )
    assert _status(decision, "held_position_sufficiency") is RiskCheckStatus.FAIL


def test_stock_buy_within_cash_passes_and_short_sell_fails() -> None:
    engine = OrderRiskEngine(paper_settings())
    buy = build_stock_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_LONG_STOCK,
        contract=stock_contract(),
        quantity=10,
        limit_price=Decimal("100.00"),
        correlation_id="CHR-ORD-" + "1" * 32,
    )
    buy_decision = engine.evaluate(
        buy,
        RiskEvidence(
            account=_account(cash="80000"),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
        ),
        now=_NOW,
    )
    assert _status(buy_decision, "stock_cash_sufficiency") is RiskCheckStatus.PASS

    sell = build_stock_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.CLOSE_LONG_STOCK,
        contract=stock_contract(),
        quantity=10,
        limit_price=Decimal("100.00"),
        correlation_id="CHR-ORD-" + "2" * 32,
    )
    sell_decision = engine.evaluate(
        sell,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            settled_long_shares=Decimal("5"),  # only 5 held, selling 10 -> short
        ),
        now=_NOW,
    )
    assert _status(sell_decision, "stock_no_short") is RiskCheckStatus.FAIL


def test_pending_covered_call_shares_block_a_naked_second_call() -> None:
    # Fix: shares committed to a resting covered-call order are reserved, so a
    # second call on the same 100 shares is NOT covered (would be naked).
    from chronos.domain.enums import OptionRight

    engine = OrderRiskEngine(paper_settings())
    call = option_contract(right=OptionRight.CALL, con_id=222)
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_COVERED_CALL,
        contract=call,
        quantity=1,
        limit_price=Decimal("2.00"),
        correlation_id="CHR-ORD-" + "3" * 32,
    )
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            settled_long_shares=Decimal("100"),
            shares_reserved_for_pending_calls=Decimal("100"),  # already committed
        ),
        now=_NOW,
    )
    assert _status(decision, "covered_call_coverage") is RiskCheckStatus.FAIL


def test_stock_buy_blocked_by_existing_put_obligation() -> None:
    # Fix: a stock buy cannot consume cash securing an open short put.
    engine = OrderRiskEngine(paper_settings())
    buy = build_stock_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_LONG_STOCK,
        contract=stock_contract(),
        quantity=100,
        limit_price=Decimal("200.00"),  # $20,000 cost
        correlation_id="CHR-ORD-" + "4" * 32,
    )
    decision = engine.evaluate(
        buy,
        RiskEvidence(
            account=_account(cash="35000"),
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            existing_short_put_obligation=Decimal("28000"),  # secures an open CSP
        ),
        now=_NOW,
    )
    assert _status(decision, "stock_cash_sufficiency") is RiskCheckStatus.FAIL


def test_concentration_uses_existing_baseline() -> None:
    # Fix: existing wheel exposure is the concentration baseline, so a new order
    # that pushes AGGREGATE symbol allocation over the cap fails.
    engine = OrderRiskEngine(paper_settings(max_symbol_allocation_pct="0.25"))
    intent = build_option_intent(
        account_id=PAPER_ACCOUNT,
        intent=OrderIntent.OPEN_SHORT_PUT,
        contract=option_contract(),  # strike 150 -> $15,000 new obligation
        quantity=1,
        limit_price=Decimal("1.20"),
        correlation_id="CHR-ORD-" + "5" * 32,
    )
    decision = engine.evaluate(
        intent,
        RiskEvidence(
            account=_account(nlv="100000", cash="90000"),  # 25% cap = $25,000
            reconciliation_status=ReconciliationStatus.RECONCILED,
            session=_open_session(),
            current_symbol_allocation=Decimal("20000"),  # existing AAPL exposure
            current_total_wheel_allocation=Decimal("20000"),
        ),
        now=_NOW,
    )
    # 20,000 existing + 15,000 new = 35,000 > 25,000 cap.
    assert _status(decision, "concentration") is RiskCheckStatus.FAIL
