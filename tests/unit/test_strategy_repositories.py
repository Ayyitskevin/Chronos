from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from chronos.domain.enums import (
    BasisEntryType,
    DataQuality,
    OptionRight,
    OrderSide,
    ReconciliationStatus,
)
from chronos.domain.models import (
    BrokerExecution,
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    UnderlyingContract,
)
from chronos.persistence.database import Database
from chronos.persistence.repositories import (
    ApplicationEventRepository,
    BasisLedgerRepository,
    CandidateEvaluationRepository,
)
from chronos.persistence.schema import (
    CandidateEvaluationRow,
    CommissionRow,
    FillRow,
    RejectedCandidateReasonRow,
    WheelCycleRow,
)
from chronos.strategy.basis import BasisLedgerEntry, commission_entry, option_premium_entry
from chronos.strategy.capital import CapitalContext
from chronos.strategy.strike_resolver import (
    ResolverContext,
    ResolverPolicy,
    StrikeResolution,
    StrikeResolver,
)
from chronos.utils.identifiers import account_fingerprint

NOW = datetime(2026, 1, 15, 15, 30, tzinfo=UTC)
ACCOUNT_ID = "DU1234567"
ACCOUNT_FINGERPRINT = account_fingerprint(ACCOUNT_ID)


def _option() -> OptionContract:
    return OptionContract(
        con_id=100,
        symbol="AAPL",
        underlying_con_id=1,
        expiration=date(2026, 2, 5),
        strike=Decimal("95"),
        right=OptionRight.PUT,
        multiplier=Decimal("100"),
        trading_class="AAPL",
        local_symbol="AAPL-100",
        deliverable_shares=Decimal("100"),
        deliverable_verified=True,
    )


def _execution(
    *,
    commission: Decimal | None = None,
    commission_currency: str | None = None,
) -> BrokerExecution:
    return BrokerExecution(
        execution_id="EXEC-1",
        account_id=ACCOUNT_ID,
        broker_order_id=10,
        client_id=17,
        contract=_option(),
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("2.00"),
        timestamp=NOW,
        commission=commission,
        commission_currency=commission_currency,
    )


def _seed_basis_dependencies(
    database: Database,
    *,
    cycle_symbol: str = "AAPL",
    commission_amount: Decimal | None = None,
    commission_currency: str = "USD",
) -> None:
    with database.sessions.begin() as session:
        session.add(
            WheelCycleRow(
                id="CYCLE-1",
                symbol=cycle_symbol,
                status="OPEN",
                opened_at=NOW,
            )
        )
        session.flush()
        execution = _execution()
        assert isinstance(execution.contract, OptionContract)
        session.add(
            FillRow(
                execution_id=execution.execution_id,
                broker_order_id=execution.broker_order_id,
                wheel_cycle_id="CYCLE-1",
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
        session.flush()
        if commission_amount is not None:
            session.add(
                CommissionRow(
                    execution_id=execution.execution_id,
                    amount=commission_amount,
                    currency=commission_currency,
                    received_at=NOW,
                )
            )


def _candidate_evidence(
    *,
    fingerprint: str = ACCOUNT_FINGERPRINT,
    wheel_cycle_id: str = "CYCLE-1",
) -> tuple[ResolverContext, StrikeResolution]:
    underlying = UnderlyingContract(con_id=1, symbol="AAPL")
    option = _option()
    underlying_quote = MarketQuote(
        contract=underlying,
        timestamp=NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("99.90"),
        ask=Decimal("100.10"),
        last=Decimal("100"),
    )
    option_quote = MarketQuote(
        contract=option,
        timestamp=NOW,
        data_quality=DataQuality.LIVE,
        bid=Decimal("0.90"),
        ask=Decimal("1.10"),
        volume=9,
        open_interest=99,
        greeks=None,
    )
    context = ResolverContext(
        wheel_cycle_id=wheel_cycle_id,
        right=OptionRight.PUT,
        contracts=1,
        as_of=NOW,
        underlying_quote=underlying_quote,
        chain=OptionChainParameters(
            exchange="SMART",
            underlying_con_id=1,
            trading_class="AAPL",
            multiplier=Decimal("100"),
            expirations=(option.expiration,),
            strikes=(option.strike,),
        ),
        option_quotes=(option_quote,),
        reconciliation_status=ReconciliationStatus.RECONCILED,
        capital=CapitalContext(
            account_fingerprint=fingerprint,
            net_liquidation=Decimal("100000"),
            uncommitted_cash=Decimal("100000"),
            current_symbol_allocation=Decimal("0"),
            current_total_wheel_allocation=Decimal("0"),
            max_symbol_allocation_pct=Decimal("0.25"),
            max_total_wheel_allocation_pct=Decimal("0.60"),
        ),
    )
    resolution = StrikeResolver(
        ResolverPolicy(
            target_abs_delta=Decimal("0.30"),
            min_abs_delta=Decimal("0.20"),
            max_abs_delta=Decimal("0.35"),
            min_dte=7,
            target_dte=21,
            max_dte=45,
            max_expirations=6,
            min_option_volume=10,
            min_open_interest=100,
            max_relative_spread=Decimal("0.20"),
            max_quote_age_seconds=Decimal("5"),
            max_contracts_per_order=1,
            delta_weight=Decimal("1"),
            spread_weight=Decimal("0"),
            dte_weight=Decimal("0"),
            liquidity_weight=Decimal("0"),
        )
    ).resolve(context)
    return context, resolution


def test_application_event_repository_requires_bound_scope_for_writes_and_reads() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        repository = ApplicationEventRepository(database.sessions)

        with pytest.raises(RuntimeError, match="bound to a broker scope"):
            repository.append(event_type="demo_started", message="Demo started")
        with pytest.raises(RuntimeError, match="bound to a broker scope"):
            repository.recent()
    finally:
        database.dispose()


def test_application_event_repository_rejects_raw_account_identifiers() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        repository = ApplicationEventRepository(database.sessions)

        event_id = repository.append(
            event_type="demo_started",
            message="Demo started for the bound account",
            event_data={"account_fingerprint": ACCOUNT_FINGERPRINT},
        )
        with pytest.raises(ValueError, match="raw broker account IDs"):
            repository.append(
                event_type="account_message",
                message=f"Connected to {ACCOUNT_ID}",
            )
        with pytest.raises(ValueError, match="raw broker account IDs"):
            repository.append(
                event_type="advisor_message",
                message="Advisor account DF1234567 connected",
            )
        with pytest.raises(ValueError, match="raw broker account IDs"):
            repository.append(
                event_type="nested_account_data",
                message="Unsafe nested payload",
                event_data={"broker": {"accounts": ["U7654321"]}},
            )
        with pytest.raises(ValueError, match="raw broker account IDs"):
            repository.append(
                event_type="semantic_account_key",
                message="Unsafe explicitly named account field",
                event_data={"accountId": "custom-broker-identifier"},
            )

        events = repository.recent()
    finally:
        database.dispose()

    assert [event.id for event in events] == [event_id]


def test_application_event_repository_rejects_unsafe_read_limits() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        repository = ApplicationEventRepository(database.sessions)

        for invalid_limit in (0, -1, 1001):
            with pytest.raises(ValueError, match="1 through 1000"):
                repository.recent(limit=invalid_limit)
        with pytest.raises(ValueError, match="1 through 1000"):
            repository.recent(limit=True)
    finally:
        database.dispose()


def test_basis_repository_is_idempotent_and_rejects_conflicting_source() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        _seed_basis_dependencies(database)
        repository = BasisLedgerRepository(database.sessions)
        entry = option_premium_entry(_execution(), wheel_cycle_id="CYCLE-1", opening=True)

        assert repository.append(entry) is True
        assert repository.append(entry) is False
        assert repository.for_cycle("CYCLE-1") == (entry,)

        conflicting = entry.model_copy(update={"id": "BASIS-CONFLICT"})
        with pytest.raises(ValueError, match="Conflicting basis evidence"):
            repository.append(conflicting)
    finally:
        database.dispose()


def test_basis_repository_refuses_unscoped_execution_manual_and_read_access() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        _seed_basis_dependencies(database)
        repository = BasisLedgerRepository(database.sessions)
        execution_entry = option_premium_entry(
            _execution(),
            wheel_cycle_id="CYCLE-1",
            opening=True,
        )
        manual_entry = BasisLedgerEntry(
            id="BASIS-MANUAL-UNSCOPED",
            wheel_cycle_id="CYCLE-1",
            symbol="AAPL",
            entry_type=BasisEntryType.MANUAL_ADJUSTMENT,
            amount=Decimal("1"),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            source_note="Operator-approved correction",
            occurred_at=NOW,
        )

        with pytest.raises(RuntimeError, match="bound to a broker scope"):
            repository.append(execution_entry)
        with pytest.raises(RuntimeError, match="bound to a broker scope"):
            repository.append(manual_entry)
        with pytest.raises(RuntimeError, match="bound to a broker scope"):
            repository.for_cycle("CYCLE-1")
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("column", "bad_value", "mismatch_name"),
    [
        ("wheel_cycle_id", "CYCLE-2", "cycle"),
        ("symbol", "MSFT", "symbol"),
        ("contract_id", 101, "contract"),
        ("side", "BUY", "side"),
        ("quantity", "2", "quantity"),
        ("price", "2.10", "price"),
        ("multiplier", "50", "multiplier"),
        ("currency", "EUR", "currency"),
        ("security_type", "STK", "security_type"),
        ("account_fingerprint", account_fingerprint("DU7654321"), "account_fingerprint"),
    ],
)
def test_basis_repository_rejects_every_fill_provenance_mismatch(
    column: str,
    bad_value: object,
    mismatch_name: str,
) -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        _seed_basis_dependencies(database)
        if column == "wheel_cycle_id":
            with database.sessions.begin() as session:
                session.add(
                    WheelCycleRow(
                        id="CYCLE-2",
                        symbol="AAPL",
                        status="OPEN",
                        opened_at=NOW,
                    )
                )
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE fills SET {column} = :bad_value",
                {"bad_value": bad_value},
            )

        entry = option_premium_entry(_execution(), wheel_cycle_id="CYCLE-1", opening=True)
        with pytest.raises(ValueError, match=mismatch_name):
            BasisLedgerRepository(database.sessions).append(entry)
    finally:
        database.dispose()


def test_basis_repository_rejects_wrong_scope_and_missing_source_fill() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        with database.sessions.begin() as session:
            session.add(
                WheelCycleRow(
                    id="CYCLE-1",
                    symbol="AAPL",
                    status="OPEN",
                    opened_at=NOW,
                )
            )
        repository = BasisLedgerRepository(database.sessions)
        entry = option_premium_entry(_execution(), wheel_cycle_id="CYCLE-1", opening=True)

        with pytest.raises(ValueError, match="no persisted source fill"):
            repository.append(entry)
        with pytest.raises(ValueError, match="different pseudonymous account scope"):
            repository.append(
                BasisLedgerEntry(
                    id="BASIS-MANUAL-WRONG-SCOPE",
                    wheel_cycle_id="CYCLE-1",
                    symbol="AAPL",
                    entry_type=BasisEntryType.MANUAL_ADJUSTMENT,
                    amount=Decimal("1"),
                    account_fingerprint=account_fingerprint("DU7654321"),
                    source_note="Wrong account must fail",
                    occurred_at=NOW,
                )
            )
    finally:
        database.dispose()


def test_basis_repository_requires_cycle_symbol_ownership() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        _seed_basis_dependencies(database, cycle_symbol="MSFT")
        entry = option_premium_entry(_execution(), wheel_cycle_id="CYCLE-1", opening=True)

        with pytest.raises(ValueError, match="symbol does not match its Wheel cycle owner"):
            BasisLedgerRepository(database.sessions).append(entry)
    finally:
        database.dispose()


def test_basis_repository_requires_exact_persisted_actual_commission() -> None:
    execution = _execution(
        commission=Decimal("1.25"),
        commission_currency="USD",
    )
    entry = commission_entry(
        execution,
        wheel_cycle_id="CYCLE-1",
        amount=Decimal("1.25"),
        actual=True,
    )

    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        _seed_basis_dependencies(database, commission_amount=Decimal("1.25"))
        repository = BasisLedgerRepository(database.sessions)

        assert repository.append(entry) is True
        assert repository.append(entry) is False
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("persisted_amount", "persisted_currency", "message"),
    [
        (None, "USD", "no persisted commission report"),
        (Decimal("1.24"), "USD", "amount and currency"),
        (Decimal("1.25"), "EUR", "amount and currency"),
    ],
)
def test_basis_repository_rejects_missing_or_conflicting_commission_report(
    persisted_amount: Decimal | None,
    persisted_currency: str,
    message: str,
) -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        _seed_basis_dependencies(
            database,
            commission_amount=persisted_amount,
            commission_currency=persisted_currency,
        )
        execution = _execution(
            commission=Decimal("1.25"),
            commission_currency="USD",
        )
        entry = commission_entry(
            execution,
            wheel_cycle_id="CYCLE-1",
            amount=Decimal("1.25"),
            actual=True,
        )

        with pytest.raises(ValueError, match=message):
            BasisLedgerRepository(database.sessions).append(entry)
    finally:
        database.dispose()


def test_candidate_repository_persists_rankings_and_rejection_reasons_atomically() -> None:
    context, resolution = _candidate_evidence()

    unscoped = Database("sqlite+pysqlite:///:memory:")
    try:
        unscoped.initialize()
        with pytest.raises(RuntimeError, match="bound to a broker scope"):
            CandidateEvaluationRepository(unscoped.sessions).record(
                evaluation_id="EVAL-UNSCOPED",
                context=context,
                resolution=resolution,
            )
    finally:
        unscoped.dispose()

    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        with database.sessions.begin() as session:
            session.add(
                WheelCycleRow(
                    id=context.wheel_cycle_id,
                    symbol="AAPL",
                    status="OPEN",
                    opened_at=NOW,
                )
            )
        repository = CandidateEvaluationRepository(database.sessions)
        assert (
            repository.record(
                evaluation_id="EVAL-1",
                context=context,
                resolution=resolution,
            )
            is True
        )
        assert (
            repository.record(
                evaluation_id="EVAL-1",
                context=context,
                resolution=resolution,
            )
            is False
        )
        changed_resolution = resolution.model_copy(
            update={"no_trade_reasons": ("Different no-trade explanation.",)}
        )
        with pytest.raises(ValueError, match="different evidence"):
            repository.record(
                evaluation_id="EVAL-1",
                context=context,
                resolution=changed_resolution,
            )
        changed_policy_resolution = resolution.model_copy(
            update={
                "policy": resolution.policy.model_copy(update={"target_abs_delta": Decimal("0.31")})
            }
        )
        with pytest.raises(ValueError, match="different evidence"):
            repository.record(
                evaluation_id="EVAL-1",
                context=context,
                resolution=changed_policy_resolution,
            )
        with pytest.raises(ValueError, match="different evidence"):
            repository.record(
                evaluation_id="EVAL-1",
                context=context.model_copy(update={"conflicting_position_or_order": True}),
                resolution=resolution,
            )
        with pytest.raises(ValueError, match="cycle argument"):
            repository.record(
                evaluation_id="EVAL-1",
                context=context,
                resolution=resolution,
                wheel_cycle_id="DIFFERENT-CYCLE",
            )
        with database.sessions() as session:
            row = session.get(CandidateEvaluationRow, "EVAL-1")
            reason_count = session.scalar(
                select(func.count()).select_from(RejectedCandidateReasonRow)
            )
    finally:
        database.dispose()

    assert row is not None
    assert row.outcome == "NO_TRADE"
    assert row.wheel_cycle_id == context.wheel_cycle_id
    assert row.selected_contract_id is None
    assert row.ranking_snapshot["algorithm_version"] == resolution.algorithm_version
    assert row.ranking_snapshot["resolution"] == resolution.model_dump(mode="json")
    expected_reason_count = sum(len(rejected.rejection_reasons) for rejected in resolution.rejected)
    assert reason_count == expected_reason_count


def test_candidate_repository_requires_bound_account_and_cycle_symbol_scope() -> None:
    context, resolution = _candidate_evidence()
    wrong_scope_context, wrong_scope_resolution = _candidate_evidence(
        fingerprint=account_fingerprint("DU7654321")
    )
    missing_cycle_context, missing_cycle_resolution = _candidate_evidence(
        wheel_cycle_id="CYCLE-MISSING"
    )
    wrong_symbol_context, wrong_symbol_resolution = _candidate_evidence(wheel_cycle_id="CYCLE-MSFT")
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        with database.sessions.begin() as session:
            session.add_all(
                (
                    WheelCycleRow(
                        id=context.wheel_cycle_id,
                        symbol="AAPL",
                        status="OPEN",
                        opened_at=NOW,
                    ),
                    WheelCycleRow(
                        id="CYCLE-MSFT",
                        symbol="MSFT",
                        status="OPEN",
                        opened_at=NOW,
                    ),
                )
            )
        repository = CandidateEvaluationRepository(database.sessions)

        with pytest.raises(ValueError, match="different pseudonymous account scope"):
            repository.record(
                evaluation_id="EVAL-WRONG-SCOPE",
                context=wrong_scope_context,
                resolution=wrong_scope_resolution,
            )
        with pytest.raises(ValueError, match="no persisted Wheel cycle"):
            repository.record(
                evaluation_id="EVAL-MISSING-CYCLE",
                context=missing_cycle_context,
                resolution=missing_cycle_resolution,
            )
        with pytest.raises(ValueError, match="symbol does not match its Wheel cycle owner"):
            repository.record(
                evaluation_id="EVAL-WRONG-SYMBOL",
                context=wrong_symbol_context,
                resolution=wrong_symbol_resolution,
            )
        with pytest.raises(ValueError, match="cycle argument"):
            repository.record(
                evaluation_id="EVAL-CYCLE-ARGUMENT",
                context=context,
                resolution=resolution,
                wheel_cycle_id="CYCLE-MSFT",
            )
    finally:
        database.dispose()


def test_candidate_repository_replay_requires_exact_rejection_reason_multiset() -> None:
    context, resolution = _candidate_evidence()
    database = Database("sqlite+pysqlite:///:memory:")
    try:
        database.initialize()
        database.bind_scope(broker_mode="demo", environment="DEMO", account_id=ACCOUNT_ID)
        with database.sessions.begin() as session:
            session.add(
                WheelCycleRow(
                    id=context.wheel_cycle_id,
                    symbol="AAPL",
                    status="OPEN",
                    opened_at=NOW,
                )
            )
        repository = CandidateEvaluationRepository(database.sessions)

        assert repository.record(
            evaluation_id="EVAL-MISSING-REASON",
            context=context,
            resolution=resolution,
        )
        with database.sessions.begin() as session:
            missing_reason = session.scalar(
                select(RejectedCandidateReasonRow)
                .where(RejectedCandidateReasonRow.evaluation_id == "EVAL-MISSING-REASON")
                .limit(1)
            )
            assert missing_reason is not None
            session.delete(missing_reason)
        with pytest.raises(ValueError, match="different evidence"):
            repository.record(
                evaluation_id="EVAL-MISSING-REASON",
                context=context,
                resolution=resolution,
            )

        assert repository.record(
            evaluation_id="EVAL-DUPLICATE-REASON",
            context=context,
            resolution=resolution,
        )
        with database.sessions.begin() as session:
            persisted_reason = session.scalar(
                select(RejectedCandidateReasonRow)
                .where(RejectedCandidateReasonRow.evaluation_id == "EVAL-DUPLICATE-REASON")
                .limit(1)
            )
            assert persisted_reason is not None
            session.add(
                RejectedCandidateReasonRow(
                    evaluation_id=persisted_reason.evaluation_id,
                    contract_key=persisted_reason.contract_key,
                    reason_code=persisted_reason.reason_code,
                    explanation=persisted_reason.explanation,
                )
            )
        with pytest.raises(ValueError, match="different evidence"):
            repository.record(
                evaluation_id="EVAL-DUPLICATE-REASON",
                context=context,
                resolution=resolution,
            )
    finally:
        database.dispose()
