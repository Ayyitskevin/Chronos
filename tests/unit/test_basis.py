from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext

import pytest
from pydantic import ValidationError

from chronos.domain.enums import (
    BasisEntryType,
    OptionRight,
    OrderSide,
    ReconciliationStatus,
    SecurityType,
)
from chronos.domain.models import BrokerExecution, OptionContract, UnderlyingContract
from chronos.persistence.database import Database
from chronos.persistence.repositories import BasisLedgerRepository
from chronos.persistence.schema import FillRow, WheelCycleRow
from chronos.strategy.basis import (
    STRATEGY_BASIS_LABEL,
    BasisLedgerEntry,
    BasisProjectionInput,
    commission_entry,
    option_premium_entry,
    project_strategy_basis,
    stock_fill_evidence_entry,
)
from chronos.utils.identifiers import account_fingerprint

NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
CYCLE_ID = "CYCLE-AAPL-001"
ACCOUNT_ID = "DU1234567"
ACCOUNT_FINGERPRINT = account_fingerprint(ACCOUNT_ID)


def make_option(
    *,
    symbol: str = "AAPL",
    multiplier: str = "100",
    currency: str = "USD",
) -> OptionContract:
    return OptionContract(
        con_id=2001,
        symbol=symbol,
        expiration=date(2026, 2, 20),
        strike=Decimal("50"),
        right=OptionRight.PUT,
        multiplier=Decimal(multiplier),
        trading_class=symbol,
        local_symbol=f"{symbol}-260220-P-50",
        currency=currency,
    )


def make_option_execution(
    *,
    execution_id: str = "EXEC-OPEN-1",
    side: OrderSide = OrderSide.SELL,
    price: str = "2.00",
    quantity: str = "1",
    multiplier: str = "100",
    commission: str | None = "0.65",
    symbol: str = "AAPL",
    currency: str = "USD",
) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        account_id=ACCOUNT_ID,
        broker_order_id=7001,
        permanent_id=9001,
        client_id=17,
        order_ref="CHR-BASIS-TEST",
        contract=make_option(symbol=symbol, multiplier=multiplier, currency=currency),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        timestamp=NOW,
        commission=Decimal(commission) if commission is not None else None,
        commission_currency=currency if commission is not None else None,
    )


def make_stock_execution(
    *,
    execution_id: str,
    side: OrderSide,
    quantity: str = "100",
    price: str = "50",
) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        account_id=ACCOUNT_ID,
        broker_order_id=7002,
        permanent_id=9002,
        client_id=17,
        order_ref="CHR-STOCK-EVIDENCE",
        contract=UnderlyingContract(con_id=1001, symbol="AAPL", primary_exchange="NASDAQ"),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        timestamp=NOW,
    )


def projection_input(
    entries: tuple[BasisLedgerEntry, ...],
    **updates: object,
) -> BasisProjectionInput:
    values: dict[str, object] = {
        "wheel_cycle_id": CYCLE_ID,
        "symbol": "AAPL",
        "underlying_con_id": 1001,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "currency": "USD",
        "broker_average_cost": Decimal("50"),
        "eligible_shares": Decimal("100"),
        "entries": entries,
        "reconciliation_status": ReconciliationStatus.RECONCILED,
    }
    values.update(updates)
    return BasisProjectionInput.model_validate(values)


def test_option_execution_premiums_have_opening_credit_and_closing_debit_signs() -> None:
    opening_execution = make_option_execution()
    closing_execution = make_option_execution(
        execution_id="EXEC-CLOSE-1",
        side=OrderSide.BUY,
        price="0.50",
    )

    opening = option_premium_entry(opening_execution, wheel_cycle_id=CYCLE_ID, opening=True)
    closing = option_premium_entry(closing_execution, wheel_cycle_id=CYCLE_ID, opening=False)

    assert opening.entry_type is BasisEntryType.OPENING_OPTION_PREMIUM
    assert opening.amount == Decimal("-200.00")
    assert closing.entry_type is BasisEntryType.CLOSING_OPTION_PREMIUM
    assert closing.amount == Decimal("50.00")


def test_option_premium_uses_qualified_nonstandard_multiplier() -> None:
    execution = make_option_execution(price="1.25", quantity="3", multiplier="50")

    entry = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)

    assert entry.multiplier == Decimal("50")
    assert entry.amount == Decimal("-187.50")


def test_actual_commission_supersedes_estimate_in_basis_projection() -> None:
    execution = make_option_execution(price="2.00", quantity="2", commission="1.30")
    premium = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
    estimate = commission_entry(
        execution,
        wheel_cycle_id=CYCLE_ID,
        amount=Decimal("1.50"),
        actual=False,
    )
    assert execution.commission is not None
    actual = commission_entry(
        execution,
        wheel_cycle_id=CYCLE_ID,
        amount=execution.commission,
        actual=True,
        occurred_at=NOW + timedelta(minutes=1),
    )
    values = projection_input(
        (premium, estimate, actual),
        eligible_shares=Decimal("200"),
    )

    result = project_strategy_basis(values)

    assert result.label == STRATEGY_BASIS_LABEL
    assert result.total_adjustment == Decimal("-398.70")
    assert result.strategy_adjusted_basis == Decimal("48.0065")
    assert result.provisional_commission is False
    assert result.reconciliation_status is ReconciliationStatus.RECONCILED
    assert result.broker_average_cost == Decimal("50")
    assert values.broker_average_cost == Decimal("50")


def test_estimated_commission_is_used_and_remains_visible_until_actual_arrives() -> None:
    execution = make_option_execution(price="2.00")
    premium = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
    estimate = commission_entry(
        execution,
        wheel_cycle_id=CYCLE_ID,
        amount=Decimal("0.75"),
        actual=False,
    )

    result = project_strategy_basis(projection_input((premium, estimate)))

    assert result.total_adjustment == Decimal("-199.25")
    assert result.strategy_adjusted_basis == Decimal("48.0075")
    assert result.provisional_commission is True
    assert "provisional" in result.reasons[0].lower()


def test_missing_commission_evidence_is_pending_instead_of_manual_review() -> None:
    premium = option_premium_entry(
        make_option_execution(commission=None),
        wheel_cycle_id=CYCLE_ID,
        opening=True,
    )

    result = project_strategy_basis(projection_input((premium,)))

    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.PENDING
    assert result.total_adjustment == Decimal("-200")
    assert any(
        "commission evidence is still pending" in reason.lower() for reason in result.reasons
    )


def test_orphan_commission_evidence_requires_manual_review() -> None:
    execution = make_option_execution()
    orphan = commission_entry(
        execution,
        wheel_cycle_id=CYCLE_ID,
        amount=Decimal("0.65"),
        actual=True,
    )

    result = project_strategy_basis(projection_input((orphan,)))

    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert any("no matching option premium" in reason.lower() for reason in result.reasons)


def test_execution_helpers_attach_only_pseudonymous_complete_provenance() -> None:
    execution = make_option_execution()
    assert isinstance(execution.contract, OptionContract)

    entry = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)

    assert entry.account_fingerprint == ACCOUNT_FINGERPRINT
    assert entry.account_fingerprint != execution.account_id
    assert entry.contract_id == execution.contract.con_id
    assert entry.security_type is SecurityType.OPTION
    assert entry.source_side is OrderSide.SELL
    assert entry.quantity == execution.quantity
    assert entry.unit_price == execution.price
    assert entry.multiplier == execution.contract.multiplier


def test_basis_arithmetic_ignores_low_ambient_decimal_precision() -> None:
    execution = make_option_execution(
        price="1.23456789",
        quantity="3",
    )

    with localcontext(Context(prec=4)):
        premium = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
        estimate = commission_entry(
            execution,
            wheel_cycle_id=CYCLE_ID,
            amount=Decimal("0.654321"),
            actual=False,
        )
        result = project_strategy_basis(
            projection_input(
                (premium, estimate),
                eligible_shares=Decimal("300"),
            )
        )

    assert premium.amount == Decimal("-370.37036700")
    assert result.total_adjustment == Decimal("-369.71604600")
    assert result.strategy_adjusted_basis == Decimal("48.76761318")


def test_execution_entry_ids_are_stable_and_distinguish_cycle_and_entry_type() -> None:
    execution = make_option_execution()

    first = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
    replay = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
    other_cycle = option_premium_entry(execution, wheel_cycle_id="CYCLE-AAPL-002", opening=True)
    actual_commission = commission_entry(
        execution,
        wheel_cycle_id=CYCLE_ID,
        amount=Decimal("0.65"),
        actual=True,
    )

    assert first == replay
    assert first.id == replay.id
    assert first.id.startswith("BASIS-")
    assert len(first.id) == 54
    assert len({first.id, other_cycle.id, actual_commission.id}) == 3


def test_repository_exact_replay_is_idempotent_and_conflicting_replay_is_rejected() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
    execution = make_option_execution()
    assert isinstance(execution.contract, OptionContract)
    entry = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
    with database.sessions.begin() as session:
        cycle = WheelCycleRow(
            id=CYCLE_ID,
            symbol="AAPL",
            status="OPEN",
            opened_at=NOW,
        )
        session.add(cycle)
        session.flush()
        session.add(
            FillRow(
                execution_id=execution.execution_id,
                broker_order_id=execution.broker_order_id,
                wheel_cycle_id=CYCLE_ID,
                symbol="AAPL",
                contract_id=execution.contract.con_id,
                security_type=execution.contract.security_type.value,
                side=execution.side.value,
                quantity=execution.quantity,
                price=execution.price,
                multiplier=execution.contract.multiplier,
                currency=execution.contract.currency,
                account_fingerprint=ACCOUNT_FINGERPRINT,
                occurred_at=execution.timestamp,
            )
        )
    repository = BasisLedgerRepository(database.sessions)
    conflicting_execution = execution.model_copy(update={"price": Decimal("2.10")})
    conflicting = option_premium_entry(
        conflicting_execution,
        wheel_cycle_id=CYCLE_ID,
        opening=True,
    )

    try:
        assert repository.append(entry) is True
        assert repository.append(entry) is False
        assert repository.for_cycle(CYCLE_ID) == (entry,)
        assert conflicting.id == entry.id
        with pytest.raises(ValueError, match="persisted fill provenance: price"):
            repository.append(conflicting)
    finally:
        database.dispose()


def test_stock_assignment_evidence_requires_reconciled_share_allocation() -> None:
    assignment = stock_fill_evidence_entry(
        make_stock_execution(execution_id="EXEC-ASSIGN-1", side=OrderSide.BUY),
        wheel_cycle_id=CYCLE_ID,
        assignment=True,
    )
    called_away = stock_fill_evidence_entry(
        make_stock_execution(execution_id="EXEC-CALL-AWAY-1", side=OrderSide.SELL),
        wheel_cycle_id=CYCLE_ID,
        assignment=False,
    )

    result = project_strategy_basis(projection_input((assignment, called_away)))

    assert assignment.amount == called_away.amount == Decimal("0")
    assert result.total_adjustment == Decimal("0")
    assert result.broker_average_cost == Decimal("50")
    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert any("share allocation" in reason.lower() for reason in result.reasons)


@pytest.mark.parametrize(
    ("flag", "reason_fragment"),
    [
        ("partial_assignment", "Partial assignment"),
        ("stock_split_warning", "stock split"),
        ("symbol_change_warning", "symbol change"),
        ("manual_trade_warning", "manual trade"),
        ("unexplained_mismatch", "do not reconcile"),
    ],
)
def test_unsafe_basis_conditions_require_manual_review(flag: str, reason_fragment: str) -> None:
    result = project_strategy_basis(projection_input((), **{flag: True}))

    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert any(reason_fragment.lower() in reason.lower() for reason in result.reasons)


def test_unreconciled_or_zero_share_projection_requires_manual_review() -> None:
    result = project_strategy_basis(
        projection_input(
            (),
            eligible_shares=Decimal("0"),
            reconciliation_status=ReconciliationStatus.PENDING,
        )
    )

    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert any("PENDING" in reason for reason in result.reasons)
    assert any("positive reconciled share allocation" in reason for reason in result.reasons)


def test_currency_cycle_symbol_and_duplicate_evidence_fail_closed() -> None:
    base = option_premium_entry(make_option_execution(), wheel_cycle_id=CYCLE_ID, opening=True)
    duplicate_source = base.model_copy(update={"id": "BASIS-DUPLICATE-SOURCE"})
    wrong_owner = option_premium_entry(
        make_option_execution(
            execution_id="EXEC-OTHER-1",
            symbol="MSFT",
            currency="EUR",
        ),
        wheel_cycle_id="CYCLE-MSFT-001",
        opening=True,
    )
    unreconciled = BasisLedgerEntry(
        id="BASIS-MANUAL-PENDING",
        wheel_cycle_id=CYCLE_ID,
        symbol="AAPL",
        entry_type=BasisEntryType.MANUAL_ADJUSTMENT,
        amount=Decimal("5"),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        source_note="Pending operator allocation",
        occurred_at=NOW,
        reconciliation_status=ReconciliationStatus.PENDING,
    )

    result = project_strategy_basis(
        projection_input((base, duplicate_source, wrong_owner, unreconciled))
    )

    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert any("different cycle or symbol" in reason for reason in result.reasons)
    assert any("no implicit FX conversion" in reason for reason in result.reasons)
    assert any("same execution and basis entry type" in reason for reason in result.reasons)
    assert any("pending reconciliation" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "updates",
    [
        {"id": " "},
        {"wheel_cycle_id": ""},
        {"symbol": " "},
        {"currency": " "},
        {"account_fingerprint": "DU1234567"},
        {"contract_id": None},
        {"security_type": None},
        {"source_side": None},
        {"quantity": None},
        {"unit_price": None},
        {"multiplier": None},
        {"source_execution_id": None},
        {"source_execution_id": " "},
        {"amount": Decimal("-199.99")},
        {"occurred_at": datetime(2026, 1, 15, 15, 30)},  # noqa: DTZ001
    ],
)
def test_option_premium_entry_model_rejects_incomplete_or_inconsistent_evidence(
    updates: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "id": "BASIS-VALID",
        "wheel_cycle_id": CYCLE_ID,
        "symbol": "aapl",
        "entry_type": BasisEntryType.OPENING_OPTION_PREMIUM,
        "amount": Decimal("-200"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "contract_id": 2001,
        "security_type": SecurityType.OPTION,
        "source_side": OrderSide.SELL,
        "quantity": Decimal("1"),
        "unit_price": Decimal("2"),
        "multiplier": Decimal("100"),
        "currency": "usd",
        "source_execution_id": "EXEC-OPEN-1",
        "occurred_at": NOW,
    }
    values.update(updates)

    with pytest.raises(ValidationError):
        BasisLedgerEntry.model_validate(values)


@pytest.mark.parametrize(
    "entry_type,amount,provisional",
    [
        (BasisEntryType.COMMISSION_ESTIMATE, Decimal("0.65"), False),
        (BasisEntryType.COMMISSION_ACTUAL, Decimal("0.65"), True),
        (BasisEntryType.COMMISSION_ACTUAL, Decimal("-0.01"), False),
    ],
)
def test_commission_entry_model_enforces_actual_and_provisional_semantics(
    entry_type: BasisEntryType,
    amount: Decimal,
    provisional: bool,
) -> None:
    with pytest.raises(ValidationError):
        BasisLedgerEntry(
            id="BASIS-COMMISSION",
            wheel_cycle_id=CYCLE_ID,
            symbol="AAPL",
            entry_type=entry_type,
            amount=amount,
            account_fingerprint=ACCOUNT_FINGERPRINT,
            contract_id=2001,
            security_type=SecurityType.OPTION,
            source_side=OrderSide.SELL,
            quantity=Decimal("1"),
            unit_price=Decimal("2"),
            multiplier=Decimal("100"),
            provisional=provisional,
            source_execution_id="EXEC-OPEN-1",
            occurred_at=NOW,
        )


def test_manual_adjustment_requires_note_and_cannot_claim_broker_execution() -> None:
    common = {
        "id": "BASIS-MANUAL",
        "wheel_cycle_id": CYCLE_ID,
        "symbol": "AAPL",
        "entry_type": BasisEntryType.MANUAL_ADJUSTMENT,
        "amount": Decimal("10"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "occurred_at": NOW,
    }

    with pytest.raises(ValidationError, match="Manual adjustments require a note"):
        BasisLedgerEntry.model_validate(common)
    with pytest.raises(ValidationError, match="Manual adjustments require a note"):
        BasisLedgerEntry.model_validate(
            common
            | {
                "source_note": "Operator-approved allocation",
                "source_execution_id": "EXEC-1",
            }
        )

    valid = BasisLedgerEntry.model_validate(
        common | {"source_note": "Operator-approved allocation"}
    )
    assert valid.symbol == "AAPL"
    assert valid.currency == "USD"


def test_projection_input_rejects_negative_cost_or_share_allocation() -> None:
    with pytest.raises(ValidationError):
        projection_input((), broker_average_cost=Decimal("-0.01"))
    with pytest.raises(ValidationError):
        projection_input((), eligible_shares=Decimal("-1"))


@pytest.mark.parametrize(
    "updates",
    [
        {"wheel_cycle_id": " "},
        {"symbol": ""},
        {"currency": "  "},
    ],
)
def test_projection_input_rejects_blank_ownership_codes(updates: dict[str, object]) -> None:
    values: dict[str, object] = {
        "wheel_cycle_id": CYCLE_ID,
        "symbol": "AAPL",
        "underlying_con_id": 1001,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "currency": "USD",
        "broker_average_cost": Decimal("50"),
        "eligible_shares": Decimal("100"),
        "entries": (),
    }
    values.update(updates)
    with pytest.raises(ValidationError):
        BasisProjectionInput.model_validate(values)


def test_projection_input_defaults_to_pending_until_reconciliation_is_explicit() -> None:
    values = BasisProjectionInput(
        wheel_cycle_id=CYCLE_ID,
        symbol="AAPL",
        underlying_con_id=1001,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        broker_average_cost=Decimal("50"),
        eligible_shares=Decimal("100"),
        entries=(),
    )

    result = project_strategy_basis(values)

    assert result.strategy_adjusted_basis is None
    assert result.reconciliation_status is ReconciliationStatus.PENDING


def test_execution_helpers_reject_wrong_instrument_or_side() -> None:
    sell_option = make_option_execution(side=OrderSide.SELL)
    buy_option = make_option_execution(side=OrderSide.BUY)
    buy_stock = make_stock_execution(execution_id="EXEC-STOCK-BUY", side=OrderSide.BUY)
    sell_stock = make_stock_execution(execution_id="EXEC-STOCK-SELL", side=OrderSide.SELL)

    with pytest.raises(ValueError, match="opening/closing premium semantics"):
        option_premium_entry(sell_option, wheel_cycle_id=CYCLE_ID, opening=False)
    with pytest.raises(ValueError, match="opening/closing premium semantics"):
        option_premium_entry(buy_option, wheel_cycle_id=CYCLE_ID, opening=True)
    with pytest.raises(ValueError, match="option execution"):
        option_premium_entry(buy_stock, wheel_cycle_id=CYCLE_ID, opening=False)
    with pytest.raises(ValueError, match="assignment semantics"):
        stock_fill_evidence_entry(buy_stock, wheel_cycle_id=CYCLE_ID, assignment=False)
    with pytest.raises(ValueError, match="assignment semantics"):
        stock_fill_evidence_entry(sell_stock, wheel_cycle_id=CYCLE_ID, assignment=True)
    with pytest.raises(ValueError, match="stock execution"):
        stock_fill_evidence_entry(sell_option, wheel_cycle_id=CYCLE_ID, assignment=False)
    with pytest.raises(ValueError, match="limited to option executions"):
        commission_entry(
            buy_stock,
            wheel_cycle_id=CYCLE_ID,
            amount=Decimal("0.65"),
            actual=True,
        )


def test_actual_commission_requires_exact_broker_amount_and_currency() -> None:
    missing = make_option_execution(commission=None)
    with pytest.raises(ValueError, match="broker commission report"):
        commission_entry(
            missing,
            wheel_cycle_id=CYCLE_ID,
            amount=Decimal("0.65"),
            actual=True,
        )

    execution = make_option_execution(commission="0.65")
    with pytest.raises(ValueError, match="amount must match"):
        commission_entry(
            execution,
            wheel_cycle_id=CYCLE_ID,
            amount=Decimal("0.66"),
            actual=True,
        )

    wrong_currency = execution.model_copy(update={"commission_currency": "EUR"})
    with pytest.raises(ValueError, match="currency must match"):
        commission_entry(
            wrong_currency,
            wheel_cycle_id=CYCLE_ID,
            amount=Decimal("0.65"),
            actual=True,
        )


def test_one_execution_cannot_be_both_opening_and_closing_premium() -> None:
    opening_execution = make_option_execution(side=OrderSide.SELL)
    closing_execution = opening_execution.model_copy(update={"side": OrderSide.BUY})
    opening = option_premium_entry(
        opening_execution,
        wheel_cycle_id=CYCLE_ID,
        opening=True,
    )
    closing = option_premium_entry(
        closing_execution,
        wheel_cycle_id=CYCLE_ID,
        opening=False,
    )
    estimate = commission_entry(
        opening_execution,
        wheel_cycle_id=CYCLE_ID,
        amount=Decimal("0.65"),
        actual=False,
    )

    result = project_strategy_basis(projection_input((opening, closing, estimate)))

    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert result.strategy_adjusted_basis is None
    assert any("both opening and closing" in reason.lower() for reason in result.reasons)


def test_projection_rejects_single_wrong_account_and_conflicting_fill_provenance() -> None:
    execution = make_option_execution()
    premium = option_premium_entry(execution, wheel_cycle_id=CYCLE_ID, opening=True)
    conflicting = commission_entry(
        execution.model_copy(update={"price": Decimal("2.01")}),
        wheel_cycle_id=CYCLE_ID,
        amount=Decimal("0.65"),
        actual=False,
    )
    wrong_account = premium.model_copy(
        update={
            "id": "BASIS-WRONG-ACCOUNT",
            "account_fingerprint": account_fingerprint("DUOTHER"),
        }
    )

    result = project_strategy_basis(projection_input((wrong_account, conflicting)))

    assert result.reconciliation_status is ReconciliationStatus.MANUAL_REVIEW
    assert any("different pseudonymous account" in reason.lower() for reason in result.reasons)
    assert any(
        "conflicting immutable fill provenance" in reason.lower() for reason in result.reasons
    )
