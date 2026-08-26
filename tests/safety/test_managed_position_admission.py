"""Safety contract for authenticated, default-off QQQ position admission."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import inspect

from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    OrderLifecycle,
    OrderSide,
    ProductFamily,
    ReconciliationStatus,
    RiskCheckStatus,
)
from chronos.domain.models import (
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    ConnectionStatus,
    UnderlyingContract,
)
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
    RiskCheckRecord,
    RiskDecisionRecord,
    RiskDecisionRepository,
)
from chronos.persistence.schema import RiskCheckResultRow, RiskDecisionRow
from chronos.supervisor.position_admission import (
    AdmittedManagedPosition,
    ManagedPositionAdmission,
    OpeningAdmissionRefusalCode,
    RefusedManagedPositionAdmission,
)
from chronos.utils.identifiers import account_fingerprint

_T = TypeVar("_T")
_NOW = datetime(2026, 8, 25, 15, 30, tzinfo=UTC)
_ACCOUNT = "DU1234567"
_ORDER_REF = "CHR-ORD-" + "A" * 32
_CONTRACT = UnderlyingContract(
    con_id=320227571,
    symbol="QQQ",
    primary_exchange="NASDAQ",
)


class _Broker:
    def __init__(
        self,
        *,
        execution_timestamp: datetime | None = None,
        fill_quantity: Decimal = Decimal(3),
        position_quantity: Decimal | None = None,
        second_position_quantity: Decimal | None = None,
        include_execution: bool = True,
        on_second_execution_read: Any | None = None,
        open_orders: tuple[BrokerOrder, ...] = (),
        broker_order_id: int = 9001,
        permanent_id: int = 109001,
    ) -> None:
        self.position_reads = 0
        self.execution_reads = 0
        self.execution_timestamp = execution_timestamp or (_NOW - timedelta(minutes=1))
        self.fill_quantity = fill_quantity
        self.position_quantity = fill_quantity if position_quantity is None else position_quantity
        self.second_position_quantity = second_position_quantity
        self.include_execution = include_execution
        self.on_second_execution_read = on_second_execution_read
        self._open_orders = open_orders
        self.broker_order_id = broker_order_id
        self.permanent_id = permanent_id

    async def connection_status(self) -> ConnectionStatus:
        return ConnectionStatus(
            state=ConnectionState.CONNECTED,
            environment=DisplayEnvironment.PAPER,
            connected=True,
            account_id=_ACCOUNT,
            managed_accounts=(_ACCOUNT,),
            data_quality=DataQuality.LIVE,
        )

    async def server_time(self) -> datetime:
        return _NOW

    async def positions(self) -> tuple[BrokerPosition, ...]:
        self.position_reads += 1
        quantity = (
            self.second_position_quantity
            if self.position_reads == 2 and self.second_position_quantity is not None
            else self.position_quantity
        )
        return (
            BrokerPosition(
                account_id=_ACCOUNT,
                contract=_CONTRACT,
                quantity=quantity,
                average_cost=Decimal(100),
            ),
        )

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        self.execution_reads += 1
        assert since == _NOW - timedelta(hours=1)
        if self.execution_reads == 2 and self.on_second_execution_read is not None:
            self.on_second_execution_read()
        if not self.include_execution:
            return ()
        return (
            BrokerExecution(
                execution_id="0001.abcdef.01.01",
                account_id=_ACCOUNT,
                broker_order_id=self.broker_order_id,
                permanent_id=self.permanent_id,
                client_id=17,
                order_ref=_ORDER_REF,
                contract=_CONTRACT,
                side=OrderSide.BUY,
                quantity=self.fill_quantity,
                price=Decimal(100),
                timestamp=self.execution_timestamp,
            ),
        )

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        return self._open_orders


class _Runner:
    def __init__(self, broker: _Broker) -> None:
        self.broker = broker

    def run(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        timeout: float | None = None,
    ) -> _T:
        del timeout
        return asyncio.run(coroutine)


def _readiness() -> ReconciliationReadiness:
    readiness = ReconciliationReadiness(session_id="recon-7")
    generation = readiness.begin_reconciliation("test broker/local parity")
    assert readiness.complete(
        expected_generation=generation,
        status=ReconciliationStatus.RECONCILED,
        reason="stable PAPER broker/local parity",
        reconciled_at=_NOW,
    )
    return readiness


def _seed_filled_opening(
    database: Database,
    *,
    terminal_status: OrderLifecycle = OrderLifecycle.FILLED,
    local_filled_quantity: Decimal = Decimal(3),
    broker_order_id: int = 9001,
    permanent_id: int = 109001,
) -> None:
    database.bind_scope(broker_mode="ibkr", environment="paper", account_id=_ACCOUNT)
    intents = OrderIntentRepository(database.sessions)
    decisions = RiskDecisionRepository(database.sessions)
    events = OrderTrackerRepository(database.sessions)
    assert intents.create(
        OrderIntentRecord(
            intent_id="intent-qqq-1",
            idempotency_key="idem-qqq-1",
            account_fingerprint=account_fingerprint(_ACCOUNT),
            environment="paper",
            product_family=ProductFamily.STOCK,
            wheel_cycle_id=None,
            symbol="QQQ",
            con_id=_CONTRACT.con_id,
            local_symbol=None,
            action=OrderSide.BUY,
            open_close_effect="OPEN",
            quantity=Decimal(3),
            order_type="LMT",
            limit_price=Decimal(101),
            time_in_force="DAY",
            outside_rth=False,
            quote_snapshot_id=None,
            risk_snapshot_id="risk-qqq-1",
            preview_id="preview-qqq-1",
            confirmation_hash="c" * 64,
            order_ref=_ORDER_REF,
            status=OrderLifecycle.SUBMITTED,
            created_at=_NOW - timedelta(hours=1),
            confirmed_at=_NOW - timedelta(minutes=10),
            submitted_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(minutes=5),
        ),
        current_account_id=_ACCOUNT,
    )
    assert decisions.store(
        RiskDecisionRecord(
            decision_id="risk-qqq-1",
            correlation_id=_ORDER_REF,
            account_fingerprint=account_fingerprint(_ACCOUNT),
            symbol="QQQ",
            product_family=ProductFamily.STOCK,
            overall_result=RiskCheckStatus.PASS,
            decided_at=_NOW - timedelta(minutes=12),
            expires_at=_NOW - timedelta(minutes=11),
            evidence={
                "reconciliation_generation": 6,
                "reconciliation_session_id": "entry-recon-6",
                "qqq_position_management": {
                    "schema_version": "chronos-qqq-position-risk-v1",
                    "candidate_spec_sha256": (
                        "59348ca3da9e9b68ec4edd1fc54572783e9256ae9c55ac18ffe844c0b4b78054"
                    ),
                    "source_evidence_digest": "d" * 64,
                    "as_of": (_NOW - timedelta(minutes=15)).isoformat(),
                    "signal_time_entry_basis_usd": "100",
                    "signal_time_initial_stop_price_usd": "99",
                    "signal_time_risk_distance_usd": "1",
                    "marked_strategy_nav_usd": "3000",
                    "unit_exposure_cvar_loss_fraction": "0.05",
                },
            },
            checks=(
                RiskCheckRecord(
                    sequence=0,
                    check_name="qqq_position_management_risk",
                    status=RiskCheckStatus.PASS,
                    detail="frozen QQQ management envelope passed",
                    evidence={},
                ),
            ),
        ),
        current_account_id=_ACCOUNT,
    )
    assert events.record_transition(
        intent_id="intent-qqq-1",
        event_key=(
            f"intent-qqq-1:{broker_order_id}:{terminal_status.value}:{local_filled_quantity}"
        ),
        source="ORDER_STATUS",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=terminal_status,
        current_account_id=_ACCOUNT,
        broker_order_id=broker_order_id,
        filled_quantity=local_filled_quantity,
        remaining_quantity=Decimal(3) - local_filled_quantity,
        evidence={"permanent_id": permanent_id},
        occurred_at=_NOW - timedelta(minutes=1),
        enforce_from_status=True,
    )


def test_valid_fill_is_admitted_once_and_replay_is_semantically_idempotent() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    broker = _Broker()
    try:
        _seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=_Runner(broker),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        first = admission.admit_opening(_ORDER_REF, _NOW)
        second = admission.admit_opening(_ORDER_REF, _NOW + timedelta(seconds=1))

        assert isinstance(first, AdmittedManagedPosition)
        assert first.replayed is False
        assert first.state.plan.opening_order_ref == _ORDER_REF
        assert first.state.plan.entry_fill_ids == ("0001.abcdef.01.01",)
        assert first.state.plan.quantity == Decimal(3)
        assert first.state.plan.entry_price == Decimal(100)
        assert first.state.plan.initial_stop_price == Decimal(99)
        assert isinstance(second, AdmittedManagedPosition)
        assert second.replayed is True
        assert second.state.plan.position_id == first.state.plan.position_id
        assert broker.position_reads == 2
        assert broker.execution_reads == 2
    finally:
        database.dispose()


def test_execution_after_broker_capture_window_refuses() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(execution_timestamp=_NOW + timedelta(seconds=1))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNSTABLE
    finally:
        database.dispose()


def test_admission_time_before_opening_fill_refuses_without_partial_registration() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker()),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW - timedelta(minutes=2))

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.INVALID_REQUEST
        replay = admission.admit_opening(_ORDER_REF, _NOW)
        assert isinstance(replay, AdmittedManagedPosition)
        assert replay.replayed is False
    finally:
        database.dispose()


def test_fractional_qqq_fill_refuses_as_typed_evidence_failure() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(
            database,
            terminal_status=OrderLifecycle.CANCELLED,
            local_filled_quantity=Decimal("2.5"),
        )
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(fill_quantity=Decimal("2.5"))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.OPENING_FILL_UNPROVEN
    finally:
        database.dispose()


def test_broker_fill_quantity_must_match_terminal_local_lifecycle_evidence() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(fill_quantity=Decimal(2))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH
    finally:
        database.dispose()


def test_nonpositive_broker_order_identity_refuses() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database, broker_order_id=0, permanent_id=0)
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(broker_order_id=0, permanent_id=0)),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH
    finally:
        database.dispose()


def test_fill_rebased_stop_that_cannot_form_a_valid_plan_refuses_typed() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        with database.sessions.begin() as session:
            risk = session.get(RiskDecisionRow, "risk-qqq-1")
            assert risk is not None
            evidence = dict(risk.evidence)
            management = dict(evidence["qqq_position_management"])
            management.update(
                {
                    "signal_time_entry_basis_usd": "201",
                    "signal_time_initial_stop_price_usd": "1",
                    "signal_time_risk_distance_usd": "200",
                }
            )
            evidence["qqq_position_management"] = management
            risk.evidence = evidence
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker()),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.ENTRY_RISK_EVIDENCE_INVALID
    finally:
        database.dispose()


def test_terminal_partial_fill_with_cancelled_remainder_is_admitted() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(
            database,
            terminal_status=OrderLifecycle.CANCELLED,
            local_filled_quantity=Decimal(2),
        )
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(fill_quantity=Decimal(2))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, AdmittedManagedPosition)
        assert result.state.plan.quantity == Decimal(2)
        assert result.state.plan.legs[0].quantity == Decimal(2)
    finally:
        database.dispose()


def test_missing_positive_execution_refuses_without_inferring_from_absence() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(include_execution=False)),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.OPENING_FILL_UNPROVEN
    finally:
        database.dispose()


def test_changed_broker_snapshot_and_changed_reconciliation_each_refuse() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        unstable = ManagedPositionAdmission(
            connection=_Runner(_Broker(second_position_quantity=Decimal(4))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )
        first = unstable.admit_opening(_ORDER_REF, _NOW)
        assert isinstance(first, RefusedManagedPositionAdmission)
        assert first.code is OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNSTABLE

        readiness = _readiness()
        raced = ManagedPositionAdmission(
            connection=_Runner(
                _Broker(
                    on_second_execution_read=lambda: readiness.invalidate(
                        "connection changed during admission"
                    )
                )
            ),
            sessions=database.sessions,
            readiness=readiness,
            account_id=_ACCOUNT,
        )
        second = raced.admit_opening(_ORDER_REF, _NOW)
        assert isinstance(second, RefusedManagedPositionAdmission)
        assert second.code is OpeningAdmissionRefusalCode.RECONCILIATION_NOT_READY
    finally:
        database.dispose()


def test_unexplained_aggregate_qqq_position_refuses() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(position_quantity=Decimal(4))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.AGGREGATE_POSITION_CONFLICT
    finally:
        database.dispose()


def test_another_working_qqq_order_makes_aggregate_position_unsafe_to_bind() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        other_order = BrokerOrder(
            broker_order_id=9002,
            permanent_id=109002,
            client_id=17,
            account_id=_ACCOUNT,
            order_ref="CHR-ORD-" + "B" * 32,
            contract=_CONTRACT,
            side=OrderSide.BUY,
            quantity=Decimal(1),
            filled_quantity=Decimal(0),
            remaining_quantity=Decimal(1),
            limit_price=Decimal(99),
            lifecycle=OrderLifecycle.SUBMITTED,
            transmit=True,
        )
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker(open_orders=(other_order,))),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.AGGREGATE_POSITION_CONFLICT
    finally:
        database.dispose()


def test_missing_frozen_risk_refuses_without_leaving_a_partial_binding() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        with database.sessions.begin() as session:
            risk = session.get(RiskDecisionRow, "risk-qqq-1")
            assert risk is not None
            original = dict(risk.evidence)
            risk.evidence = {**original, "qqq_position_management": None}
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker()),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        refused = admission.admit_opening(_ORDER_REF, _NOW)
        assert isinstance(refused, RefusedManagedPositionAdmission)
        assert refused.code is OpeningAdmissionRefusalCode.ENTRY_RISK_EVIDENCE_INVALID

        with database.sessions.begin() as session:
            risk = session.get(RiskDecisionRow, "risk-qqq-1")
            assert risk is not None
            risk.evidence = original
        admitted = admission.admit_opening(_ORDER_REF, _NOW)
        assert isinstance(admitted, AdmittedManagedPosition)
        assert admitted.replayed is False
    finally:
        database.dispose()


def test_risk_header_cannot_replace_the_required_passing_management_check() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        _seed_filled_opening(database)
        with database.sessions.begin() as session:
            check = session.query(RiskCheckResultRow).one()
            check.status = RiskCheckStatus.FAIL.value
        admission = ManagedPositionAdmission(
            connection=_Runner(_Broker()),
            sessions=database.sessions,
            readiness=_readiness(),
            account_id=_ACCOUNT,
        )

        result = admission.admit_opening(_ORDER_REF, _NOW)

        assert isinstance(result, RefusedManagedPositionAdmission)
        assert result.code is OpeningAdmissionRefusalCode.ENTRY_RISK_EVIDENCE_INVALID
    finally:
        database.dispose()


def test_admission_has_one_public_operation_and_no_runtime_or_send_capability() -> None:
    root = Path(__file__).resolve().parents[2]
    module_path = root / "src/chronos/supervisor/position_admission.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    admission_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ManagedPositionAdmission"
    )
    public_methods = {
        node.name
        for node in admission_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    assert public_methods == {"admit_opening"}

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(
        name.startswith(
            (
                "chronos.runtime",
                "chronos.execution",
                "chronos.orders.submission",
                "chronos.broker.ibkr",
                "chronos.broker.official_ibkr",
            )
        )
        for name in imported
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {"submit_order", "modify_order", "cancel_order", "preview_order"}
    )

    for source_path in (root / "src/chronos").rglob("*.py"):
        if source_path == module_path:
            continue
        source_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(source_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or "" for node in ast.walk(source_tree) if isinstance(node, ast.ImportFrom)
        }
        assert not any(
            name == "position_admission" or name.endswith(".position_admission")
            for name in imported_modules
        ), source_path


def test_binding_schema_enforces_one_order_and_one_position_per_account() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.initialize()
    try:
        inspector = inspect(database.engine)
        unique_columns = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("managed_position_bindings")
        }
        assert ("account_fingerprint", "opening_order_ref") in unique_columns
        assert ("account_fingerprint", "position_id") in unique_columns
        columns = {
            str(column["name"]) for column in inspector.get_columns("managed_position_bindings")
        }
        assert "account_fingerprint" in columns
        assert "account_id" not in columns
    finally:
        database.dispose()
