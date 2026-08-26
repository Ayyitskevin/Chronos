"""Authenticated, default-off admission of filled QQQ PAPER openings.

The sole operation accepts only Chronos's opening-order reference and an aware
clock value.  Everything economic is re-derived from the persisted order/risk
records and two stable reads of the canonical broker port.  A successful write
atomically binds one opening order to one deterministic managed-position
identity and appends the existing hash-chained position registration.

This module is intentionally absent from production/runtime imports.  It reads
broker state but owns no order construction, submission, scheduling, mandate,
or activation capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from chronos.domain.enums import (
    ConnectionState,
    DisplayEnvironment,
    OrderLifecycle,
    OrderSide,
    ProductFamily,
    RiskCheckStatus,
)
from chronos.domain.models import BrokerExecution, BrokerOrder, BrokerPosition, ConnectionStatus
from chronos.orders.reconciliation_readiness import (
    ReconciliationReadiness,
    ReconciliationReadinessSnapshot,
)
from chronos.orders.risk import QQQPositionManagementRiskEvidence
from chronos.persistence.repositories import _require_matching_account_scope
from chronos.persistence.schema import (
    ManagedPositionBindingRow,
    OrderEventRow,
    OrderIntentRow,
    RiskCheckResultRow,
    RiskDecisionRow,
)
from chronos.supervisor.position_management import (
    QQQ_FIVE_TOOL_CANDIDATE_SHA256,
    QQQ_FIVE_TOOL_PAPER_POLICY_SHA256,
    PositionManagementError,
    PositionManagementState,
    build_qqq_five_tool_paper_plan,
    register_position,
    rehydrate_position,
)
from chronos.utils.identifiers import account_fingerprint

_T = TypeVar("_T")
_ORDER_REF = re.compile(r"^CHR-ORD-[0-9A-F]{32}$")
_TERMINAL_FILLED_STATUSES = frozenset({OrderLifecycle.FILLED, OrderLifecycle.CANCELLED})
_ENTRY_PRICE_QUANTUM = Decimal("0.00000001")


class ReadOnlyBrokerEvidence(Protocol):
    """The exact non-mutating broker facts admission may observe."""

    async def connection_status(self) -> ConnectionStatus: ...

    async def server_time(self) -> datetime: ...

    async def positions(self) -> tuple[BrokerPosition, ...]: ...

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]: ...

    async def open_orders(self) -> tuple[BrokerOrder, ...]: ...


class ConnectionRunner(Protocol):
    broker: ReadOnlyBrokerEvidence

    def run(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        timeout: float | None = None,
    ) -> _T: ...


class OpeningAdmissionRefusalCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ALREADY_BOUND_CONFLICT = "ALREADY_BOUND_CONFLICT"
    LOCAL_ORDER_NOT_FOUND = "LOCAL_ORDER_NOT_FOUND"
    LOCAL_ORDER_INELIGIBLE = "LOCAL_ORDER_INELIGIBLE"
    ENTRY_RISK_EVIDENCE_INVALID = "ENTRY_RISK_EVIDENCE_INVALID"
    RECONCILIATION_NOT_READY = "RECONCILIATION_NOT_READY"
    BROKER_EVIDENCE_UNAVAILABLE = "BROKER_EVIDENCE_UNAVAILABLE"
    BROKER_EVIDENCE_UNSTABLE = "BROKER_EVIDENCE_UNSTABLE"
    BROKER_IDENTITY_MISMATCH = "BROKER_IDENTITY_MISMATCH"
    OPENING_FILL_UNPROVEN = "OPENING_FILL_UNPROVEN"
    AGGREGATE_POSITION_CONFLICT = "AGGREGATE_POSITION_CONFLICT"
    DURABLE_STATE_CONFLICT = "DURABLE_STATE_CONFLICT"


@dataclass(frozen=True, slots=True)
class AdmittedManagedPosition:
    state: PositionManagementState
    replayed: bool


@dataclass(frozen=True, slots=True)
class RefusedManagedPositionAdmission:
    code: OpeningAdmissionRefusalCode
    detail: str


type ManagedPositionAdmissionResult = AdmittedManagedPosition | RefusedManagedPositionAdmission


@dataclass(frozen=True, slots=True)
class _BrokerObservation:
    status_start: ConnectionStatus
    server_time_start: datetime
    positions_first: tuple[BrokerPosition, ...]
    executions_first: tuple[BrokerExecution, ...]
    orders_first: tuple[BrokerOrder, ...]
    positions_second: tuple[BrokerPosition, ...]
    executions_second: tuple[BrokerExecution, ...]
    orders_second: tuple[BrokerOrder, ...]
    server_time_end: datetime
    status_end: ConnectionStatus


class ManagedPositionAdmission:
    """Bind proven PAPER fills to the inert QQQ management state machine."""

    def __init__(
        self,
        *,
        connection: ConnectionRunner,
        sessions: sessionmaker[Session],
        readiness: ReconciliationReadiness,
        account_id: str,
    ) -> None:
        normalized_account = account_id.strip()
        if not normalized_account:
            raise ValueError("managed-position admission requires a bound account")
        self._connection = connection
        self._broker = connection.broker
        self._sessions = sessions
        self._readiness = readiness
        self._account_id = normalized_account
        self._account_fingerprint = account_fingerprint(normalized_account)

    def admit_opening(
        self,
        opening_order_ref: str,
        now: datetime,
    ) -> ManagedPositionAdmissionResult:
        """Admit one terminal opening fill, or return an explicit refusal."""

        order_ref = opening_order_ref.strip().upper()
        if _ORDER_REF.fullmatch(order_ref) is None:
            return _refuse(
                OpeningAdmissionRefusalCode.INVALID_REQUEST,
                "opening_order_ref must be CHR-ORD-<32 hex>",
            )
        if now.tzinfo is None or now.utcoffset() is None:
            return _refuse(
                OpeningAdmissionRefusalCode.INVALID_REQUEST,
                "admission time must be timezone-aware",
            )

        replay = self._existing_binding(order_ref)
        if replay is not None:
            return replay

        with self._sessions() as session:
            _require_matching_account_scope(session, self._account_id)
            intent = session.scalar(
                select(OrderIntentRow).where(OrderIntentRow.order_ref == order_ref)
            )
            if intent is None:
                return _refuse(
                    OpeningAdmissionRefusalCode.LOCAL_ORDER_NOT_FOUND,
                    "no bound local order has that opening reference",
                )
            execution_since = intent.created_at

        readiness_before = self._readiness.snapshot()
        if not readiness_before.ready:
            return _refuse(
                OpeningAdmissionRefusalCode.RECONCILIATION_NOT_READY,
                "fresh broker/local reconciliation is not ready",
            )
        try:
            observation = self._connection.run(self._observe_broker(execution_since))
        except Exception:
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNAVAILABLE,
                "broker evidence could not be captured",
            )
        readiness_after = self._readiness.snapshot()
        if not _same_ready_generation(readiness_before, readiness_after):
            return _refuse(
                OpeningAdmissionRefusalCode.RECONCILIATION_NOT_READY,
                "reconciliation changed while opening evidence was captured",
            )
        observation_refusal = self._validate_observation(observation, order_ref=order_ref)
        if observation_refusal is not None:
            return observation_refusal

        matching_executions = tuple(
            sorted(
                (item for item in observation.executions_second if item.order_ref == order_ref),
                key=lambda item: (item.timestamp, item.execution_id),
            )
        )
        try:
            return self._bind(
                order_ref=order_ref,
                now=now,
                readiness=readiness_after,
                observation=observation,
                executions=matching_executions,
            )
        except IntegrityError:
            replay = self._existing_binding(order_ref)
            if replay is not None:
                return replay
            return _refuse(
                OpeningAdmissionRefusalCode.DURABLE_STATE_CONFLICT,
                "a concurrent admission created conflicting durable state",
            )
        except PositionManagementError:
            return _refuse(
                OpeningAdmissionRefusalCode.DURABLE_STATE_CONFLICT,
                "managed-position registration conflicts with durable state",
            )

    async def _observe_broker(self, since: datetime) -> _BrokerObservation:
        status_start = await self._broker.connection_status()
        server_time_start = await self._broker.server_time()
        positions_first = await self._broker.positions()
        executions_first = await self._broker.executions(since=since)
        orders_first = await self._broker.open_orders()
        positions_second = await self._broker.positions()
        executions_second = await self._broker.executions(since=since)
        orders_second = await self._broker.open_orders()
        server_time_end = await self._broker.server_time()
        status_end = await self._broker.connection_status()
        return _BrokerObservation(
            status_start=status_start,
            server_time_start=server_time_start,
            positions_first=positions_first,
            executions_first=executions_first,
            orders_first=orders_first,
            positions_second=positions_second,
            executions_second=executions_second,
            orders_second=orders_second,
            server_time_end=server_time_end,
            status_end=status_end,
        )

    def _validate_observation(
        self,
        observation: _BrokerObservation,
        *,
        order_ref: str,
    ) -> RefusedManagedPositionAdmission | None:
        if (
            observation.server_time_start.tzinfo is None
            or observation.server_time_end.tzinfo is None
            or observation.server_time_end < observation.server_time_start
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNSTABLE,
                "broker time evidence is invalid or moved backwards",
            )
        if not _status_matches_account(observation.status_start, self._account_id) or not (
            _status_matches_account(observation.status_end, self._account_id)
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "broker session is not the bound PAPER account",
            )
        first = _stable_payload(
            observation.positions_first,
            observation.executions_first,
            observation.orders_first,
        )
        second = _stable_payload(
            observation.positions_second,
            observation.executions_second,
            observation.orders_second,
        )
        if first != second or _status_identity(observation.status_start) != _status_identity(
            observation.status_end
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNSTABLE,
                "broker position, execution, order, or session evidence changed between reads",
            )
        if any(
            execution.timestamp > observation.server_time_end
            for execution in observation.executions_second
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_EVIDENCE_UNSTABLE,
                "broker execution evidence is timestamped after its capture window",
            )
        matching_orders = tuple(
            order for order in observation.orders_second if order.order_ref == order_ref
        )
        if matching_orders:
            return _refuse(
                OpeningAdmissionRefusalCode.OPENING_FILL_UNPROVEN,
                "the opening order still appears active at the broker",
            )
        if not any(execution.order_ref == order_ref for execution in observation.executions_second):
            return _refuse(
                OpeningAdmissionRefusalCode.OPENING_FILL_UNPROVEN,
                "no positive broker execution proves this opening fill",
            )
        return None

    def _bind(
        self,
        *,
        order_ref: str,
        now: datetime,
        readiness: ReconciliationReadinessSnapshot,
        observation: _BrokerObservation,
        executions: tuple[BrokerExecution, ...],
    ) -> ManagedPositionAdmissionResult:
        with self._sessions.begin() as session:
            _require_matching_account_scope(session, self._account_id)
            existing = _binding_for_order(session, self._account_fingerprint, order_ref)
            if existing is not None:
                return self._rehydrate_binding(session, existing, replayed=True)

            intent = session.scalar(
                select(OrderIntentRow).where(OrderIntentRow.order_ref == order_ref)
            )
            intent_refusal = self._validate_intent(session, intent, executions)
            if intent_refusal is not None:
                return intent_refusal
            assert intent is not None and intent.risk_snapshot_id is not None
            risk = session.get(RiskDecisionRow, intent.risk_snapshot_id)
            try:
                risk_evidence = self._risk_evidence(session, risk, intent=intent)
            except (TypeError, ValueError, ValidationError):
                return _refuse(
                    OpeningAdmissionRefusalCode.ENTRY_RISK_EVIDENCE_INVALID,
                    "the authorizing risk decision lacks valid frozen QQQ management evidence",
                )
            assert risk is not None

            fill_quantity = sum((execution.quantity for execution in executions), Decimal(0))
            if fill_quantity != fill_quantity.to_integral_value():
                return _refuse(
                    OpeningAdmissionRefusalCode.OPENING_FILL_UNPROVEN,
                    "QQQ opening executions do not sum to whole shares",
                )
            fill_notional = sum(
                (execution.quantity * execution.price for execution in executions), Decimal(0)
            )
            # ADR-0035 persists decimal geometry at 1e-8.  A multi-fill VWAP can
            # repeat forever, so round a long entry upward: the bounded delta is
            # conservative for basis, gross exposure, CVaR, and all targets.
            entry_price = (fill_notional / fill_quantity).quantize(
                _ENTRY_PRICE_QUANTUM,
                rounding=ROUND_CEILING,
            )
            opened_at = max(execution.timestamp for execution in executions)
            if now < opened_at:
                return _refuse(
                    OpeningAdmissionRefusalCode.INVALID_REQUEST,
                    "admission time predates the proven opening fill",
                )
            fill_digest = _digest([execution.model_dump(mode="json") for execution in executions])
            risk_digest = _digest(
                {
                    "decision_id": risk.decision_id,
                    "decided_at": risk.decided_at.isoformat(),
                    "evidence": risk_evidence.model_dump(mode="json"),
                }
            )
            position_id = _position_id(self._account_fingerprint, order_ref)
            try:
                plan = build_qqq_five_tool_paper_plan(
                    position_id=position_id,
                    opening_order_ref=order_ref,
                    account_fingerprint=self._account_fingerprint,
                    entry_fill_ids=tuple(execution.execution_id for execution in executions),
                    opening_fill_evidence_digest=fill_digest,
                    entry_risk_evidence_digest=risk_digest,
                    opened_at=opened_at,
                    quantity=fill_quantity,
                    entry_price=entry_price,
                    initial_stop_price=(entry_price - risk_evidence.signal_time_risk_distance_usd),
                    signal_time_risk_distance_usd=risk_evidence.signal_time_risk_distance_usd,
                    strategy_nav_usd=risk_evidence.marked_strategy_nav_usd,
                    unit_exposure_cvar_loss_fraction=(
                        risk_evidence.unit_exposure_cvar_loss_fraction
                    ),
                )
            except (ValueError, ValidationError):
                return _refuse(
                    OpeningAdmissionRefusalCode.ENTRY_RISK_EVIDENCE_INVALID,
                    "frozen signal risk cannot form a valid plan at the proven fill",
                )
            position_refusal = self._validate_aggregate_position(
                session,
                observation.positions_second,
                observation.orders_second,
                intent=intent,
                candidate_quantity=fill_quantity,
            )
            if position_refusal is not None:
                return position_refusal

            broker_order_id = executions[0].broker_order_id
            permanent_id = executions[0].permanent_id
            assert permanent_id is not None
            session.add(
                ManagedPositionBindingRow(
                    account_fingerprint=self._account_fingerprint,
                    opening_order_ref=order_ref,
                    position_id=position_id,
                    risk_decision_id=risk.decision_id,
                    broker_order_id=broker_order_id,
                    permanent_id=permanent_id,
                    opening_fill_evidence_digest=fill_digest,
                    entry_risk_evidence_digest=risk_digest,
                    candidate_spec_sha256=QQQ_FIVE_TOOL_CANDIDATE_SHA256,
                    management_policy_sha256=QQQ_FIVE_TOOL_PAPER_POLICY_SHA256,
                    reconciliation_session_id=readiness.session_id,
                    reconciliation_generation=readiness.generation,
                    admitted_at=now,
                )
            )
            session.flush()
            state = register_position(session, plan=plan, recorded_at=now)
            return AdmittedManagedPosition(state=state, replayed=False)

    def _validate_intent(
        self,
        session: Session,
        intent: OrderIntentRow | None,
        executions: tuple[BrokerExecution, ...],
    ) -> RefusedManagedPositionAdmission | None:
        if intent is None:
            return _refuse(
                OpeningAdmissionRefusalCode.LOCAL_ORDER_NOT_FOUND,
                "the local opening order disappeared during evidence capture",
            )
        if (
            intent.account_fingerprint != self._account_fingerprint
            or intent.environment != "paper"
            or intent.product_family != ProductFamily.STOCK.value
            or intent.symbol != "QQQ"
            or intent.action != OrderSide.BUY.value
            or intent.open_close_effect != "OPEN"
            or OrderLifecycle(intent.status) not in _TERMINAL_FILLED_STATUSES
            or intent.risk_snapshot_id is None
            or intent.con_id is None
            or intent.submitted_at is None
            or intent.limit_price is None
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.LOCAL_ORDER_INELIGIBLE,
                "local order is not a terminal, risk-bound PAPER QQQ BUY opening",
            )
        if not executions or len({execution.execution_id for execution in executions}) != len(
            executions
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.OPENING_FILL_UNPROVEN,
                "opening execution identities are absent or duplicated",
            )
        broker_order_ids = {execution.broker_order_id for execution in executions}
        permanent_ids = {execution.permanent_id for execution in executions}
        if (
            len(broker_order_ids) != 1
            or len(permanent_ids) != 1
            or any(order_id <= 0 for order_id in broker_order_ids)
            or any(permanent_id is None or permanent_id <= 0 for permanent_id in permanent_ids)
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "opening executions do not share one positive broker and permanent identity",
            )
        events = tuple(
            session.scalars(
                select(OrderEventRow)
                .where(OrderEventRow.intent_id == intent.intent_id)
                .order_by(OrderEventRow.sequence)
            )
        )
        tracked_order_ids = {
            event.broker_order_id for event in events if event.broker_order_id is not None
        }
        if tracked_order_ids != broker_order_ids:
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "broker execution identity disagrees with the tracked local order",
            )
        tracked_permanent_ids = {
            permanent_id
            for event in events
            for permanent_id in (event.evidence.get("permanent_id"),)
            if isinstance(permanent_id, int)
        }
        if tracked_permanent_ids and tracked_permanent_ids != permanent_ids:
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "broker permanent identity disagrees with local lifecycle evidence",
            )
        local_fill_values = tuple(
            event.filled_quantity for event in events if event.filled_quantity is not None
        )
        broker_fill_quantity = sum((execution.quantity for execution in executions), Decimal(0))
        if not local_fill_values or max(local_fill_values) != broker_fill_quantity:
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "broker fill quantity disagrees with terminal local lifecycle evidence",
            )
        if (
            OrderLifecycle(intent.status) is OrderLifecycle.FILLED
            and broker_fill_quantity != intent.quantity
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "a locally FILLED order does not reconcile to its intended quantity",
            )
        if any(
            execution.account_id != self._account_id
            or execution.order_ref != intent.order_ref
            or execution.side is not OrderSide.BUY
            or execution.contract.symbol != "QQQ"
            or execution.contract.con_id != intent.con_id
            or execution.price > intent.limit_price
            or execution.timestamp < intent.created_at
            for execution in executions
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.BROKER_IDENTITY_MISMATCH,
                "opening executions disagree with account, order, side, contract, limit, or time",
            )
        return None

    def _risk_evidence(
        self,
        session: Session,
        risk: RiskDecisionRow | None,
        *,
        intent: OrderIntentRow,
    ) -> QQQPositionManagementRiskEvidence:
        if intent.submitted_at is None:
            raise ValueError("opening order has no submission time")
        if intent.limit_price is None:
            raise ValueError("opening order has no protected limit price")
        if (
            risk is None
            or risk.account_fingerprint != self._account_fingerprint
            or risk.correlation_id != intent.order_ref
            or risk.symbol != "QQQ"
            or risk.product_family != ProductFamily.STOCK.value
            or risk.overall_result != RiskCheckStatus.PASS.value
            or risk.decided_at > intent.submitted_at
        ):
            raise ValueError("risk decision identity is invalid")
        checks = tuple(
            session.scalars(
                select(RiskCheckResultRow)
                .where(RiskCheckResultRow.decision_id == risk.decision_id)
                .where(RiskCheckResultRow.check_name == "qqq_position_management_risk")
            )
        )
        if len(checks) != 1 or checks[0].status != RiskCheckStatus.PASS.value:
            raise ValueError("required QQQ management risk check did not pass")
        evidence = QQQPositionManagementRiskEvidence.model_validate(
            risk.evidence.get("qqq_position_management")
        )
        if (
            evidence.candidate_spec_sha256 != QQQ_FIVE_TOOL_CANDIDATE_SHA256
            or evidence.as_of > risk.decided_at
        ):
            raise ValueError("risk evidence provenance is invalid")
        generation = risk.evidence.get("reconciliation_generation")
        session_id = risk.evidence.get("reconciliation_session_id")
        if (
            type(generation) is not int
            or generation < 0
            or not isinstance(session_id, str)
            or not (session_id.strip())
        ):
            raise ValueError("entry reconciliation provenance is invalid")
        projection = evidence.project(
            quantity=intent.quantity,
            protected_entry_price=intent.limit_price,
        )
        if not projection.passed:
            raise ValueError("persisted QQQ entry risk exceeds its frozen envelope")
        return evidence

    def _validate_aggregate_position(
        self,
        session: Session,
        positions: tuple[BrokerPosition, ...],
        open_orders: tuple[BrokerOrder, ...],
        *,
        intent: OrderIntentRow,
        candidate_quantity: Decimal,
    ) -> RefusedManagedPositionAdmission | None:
        if any(
            order.account_id == self._account_id
            and order.contract.symbol == "QQQ"
            and order.contract.con_id == intent.con_id
            for order in open_orders
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.AGGREGATE_POSITION_CONFLICT,
                "another working QQQ order can change the aggregate position",
            )
        matches = tuple(
            position
            for position in positions
            if position.account_id == self._account_id
            and position.contract.symbol == "QQQ"
            and position.contract.con_id == intent.con_id
        )
        if len(matches) != 1:
            return _refuse(
                OpeningAdmissionRefusalCode.AGGREGATE_POSITION_CONFLICT,
                "broker QQQ position identity is missing or ambiguous",
            )
        existing_quantity = Decimal(0)
        rows = session.scalars(
            select(ManagedPositionBindingRow).where(
                ManagedPositionBindingRow.account_fingerprint == self._account_fingerprint
            )
        )
        try:
            for row in rows:
                state = rehydrate_position(
                    session,
                    account_fingerprint=self._account_fingerprint,
                    position_id=row.position_id,
                )
                existing_quantity += state.remaining_quantity
        except PositionManagementError:
            return _refuse(
                OpeningAdmissionRefusalCode.DURABLE_STATE_CONFLICT,
                "an existing managed-position binding cannot be rehydrated",
            )
        if matches[0].quantity != existing_quantity + candidate_quantity:
            return _refuse(
                OpeningAdmissionRefusalCode.AGGREGATE_POSITION_CONFLICT,
                "broker aggregate QQQ quantity includes missing or unexplained exposure",
            )
        return None

    def _existing_binding(self, order_ref: str) -> ManagedPositionAdmissionResult | None:
        with self._sessions() as session:
            _require_matching_account_scope(session, self._account_id)
            row = _binding_for_order(session, self._account_fingerprint, order_ref)
            if row is None:
                return None
            return self._rehydrate_binding(session, row, replayed=True)

    def _rehydrate_binding(
        self,
        session: Session,
        row: ManagedPositionBindingRow,
        *,
        replayed: bool,
    ) -> ManagedPositionAdmissionResult:
        try:
            state = rehydrate_position(
                session,
                account_fingerprint=self._account_fingerprint,
                position_id=row.position_id,
            )
        except PositionManagementError:
            return _refuse(
                OpeningAdmissionRefusalCode.ALREADY_BOUND_CONFLICT,
                "the durable opening binding has no valid managed-position stream",
            )
        plan = state.plan
        if (
            plan.opening_order_ref != row.opening_order_ref
            or plan.position_id != row.position_id
            or plan.opening_fill_evidence_digest != row.opening_fill_evidence_digest
            or plan.entry_risk_evidence_digest != row.entry_risk_evidence_digest
            or plan.candidate_spec_sha256 != row.candidate_spec_sha256
            or plan.management_policy_sha256 != row.management_policy_sha256
        ):
            return _refuse(
                OpeningAdmissionRefusalCode.ALREADY_BOUND_CONFLICT,
                "the durable opening binding contradicts its managed-position plan",
            )
        return AdmittedManagedPosition(state=state, replayed=replayed)


def _binding_for_order(
    session: Session,
    fingerprint: str,
    order_ref: str,
) -> ManagedPositionBindingRow | None:
    return session.scalar(
        select(ManagedPositionBindingRow)
        .where(ManagedPositionBindingRow.account_fingerprint == fingerprint)
        .where(ManagedPositionBindingRow.opening_order_ref == order_ref)
    )


def _same_ready_generation(
    before: ReconciliationReadinessSnapshot,
    after: ReconciliationReadinessSnapshot,
) -> bool:
    return (
        before.ready
        and after.ready
        and before.session_id == after.session_id
        and before.generation == after.generation
        and before.reconciled_at == after.reconciled_at
    )


def _status_matches_account(status: ConnectionStatus, account_id: str) -> bool:
    return (
        status.connected
        and status.state is ConnectionState.CONNECTED
        and status.environment is DisplayEnvironment.PAPER
        and status.account_id == account_id
        and account_id in status.managed_accounts
    )


def _status_identity(status: ConnectionStatus) -> tuple[object, ...]:
    """Stable authorization fields; diagnostic timestamps/messages may move."""

    return (
        status.state,
        status.environment,
        status.connected,
        status.account_id,
        status.managed_accounts,
        status.data_quality,
    )


def _stable_payload(
    positions: tuple[BrokerPosition, ...],
    executions: tuple[BrokerExecution, ...],
    orders: tuple[BrokerOrder, ...],
) -> str:
    return json.dumps(
        {
            "positions": sorted(
                (
                    {
                        "account_id": item.account_id,
                        "contract": item.contract.model_dump(mode="json"),
                        "quantity": str(item.quantity),
                        "average_cost": str(item.average_cost),
                    }
                    for item in positions
                ),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
            "executions": sorted(
                (
                    {
                        key: value
                        for key, value in item.model_dump(mode="json").items()
                        if key not in {"commission", "commission_currency"}
                    }
                    for item in executions
                ),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
            "orders": sorted(
                (item.model_dump(mode="json") for item in orders),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _position_id(fingerprint: str, order_ref: str) -> str:
    material = f"chronos-managed-position-v1\x1f{fingerprint}\x1f{order_ref}"
    return "CHR-POS-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32].upper()


def _refuse(
    code: OpeningAdmissionRefusalCode,
    detail: str,
) -> RefusedManagedPositionAdmission:
    return RefusedManagedPositionAdmission(code=code, detail=detail)


__all__ = [
    "AdmittedManagedPosition",
    "ManagedPositionAdmission",
    "ManagedPositionAdmissionResult",
    "OpeningAdmissionRefusalCode",
    "RefusedManagedPositionAdmission",
]
