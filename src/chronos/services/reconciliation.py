"""Read-only, fail-closed reconciliation of broker and local Wheel evidence."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite
from time import monotonic as system_monotonic
from typing import Any, Literal, Protocol, TypeVar

from pydantic import AwareDatetime, Field

from chronos.broker.base import Broker
from chronos.domain.enums import (
    ConnectionState,
    DataQuality,
    DisplayEnvironment,
    OptionRight,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
    WheelStage,
)
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerOrderIdentity,
    BrokerPosition,
    ChronosModel,
    ConnectionStatus,
    Instrument,
    LocalReconciliationEvidence,
    OptionContract,
)
from chronos.strategy.wheel_state import WheelStateInput, derive_wheel_state
from chronos.utils.logging import mask_account_id

_T = TypeVar("_T")
_LOGGER = logging.getLogger("chronos.services.reconciliation")


class ConnectionRunner(Protocol):
    """The narrow connection-manager surface needed by reconciliation."""

    broker: Broker

    def run(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        timeout: float | None = None,
    ) -> _T: ...


class LocalReconciliationReader(Protocol):
    """Read account-scoped local evidence in one database transaction."""

    def read(self, current_account_id: str) -> LocalReconciliationEvidence: ...


class ReconciliationAccountView(ChronosModel):
    """Presentation-safe account values with no raw broker account identifier."""

    masked_account_id: str
    net_liquidation: Decimal = Field(ge=0)
    total_cash: Decimal
    buying_power: Decimal = Field(ge=0)
    currency: str
    as_of: AwareDatetime


class ReconciliationPositionView(ChronosModel):
    """Broker position fields safe to render or serialize locally."""

    contract: Instrument
    quantity: Decimal
    average_cost: Decimal
    market_price: Decimal | None = Field(default=None, ge=0)
    unrealized_pnl: Decimal | None = None


class ReconciliationOrderView(ChronosModel):
    """Open-order fields safe to render without account or order-reference data."""

    broker_order_id: int
    permanent_id: int | None
    client_id: int = Field(ge=0)
    contract: Instrument
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    filled_quantity: Decimal = Field(ge=0)
    remaining_quantity: Decimal = Field(ge=0)
    limit_price: Decimal = Field(gt=0)
    lifecycle: OrderLifecycle
    transmit: bool
    outside_rth: bool


class ReconciliationSnapshotView(ChronosModel):
    """Stable broker observation with account identifiers removed."""

    environment: DisplayEnvironment
    data_quality: DataQuality
    account: ReconciliationAccountView
    positions: tuple[ReconciliationPositionView, ...]
    open_orders: tuple[ReconciliationOrderView, ...]
    execution_count: int = Field(ge=0)
    server_time_start: AwareDatetime | None
    server_time_end: AwareDatetime | None
    captured_at: AwareDatetime
    window_seconds: float = Field(ge=0)
    server_window_seconds: float = Field(ge=0)


class SymbolReconciliation(ChronosModel):
    """Presentation-safe state decision for one symbol."""

    symbol: str
    status: ReconciliationStatus
    stage: WheelStage
    stock_shares: Decimal
    unencumbered_shares: Decimal
    short_put_contracts: Decimal
    short_call_contracts: Decimal
    pending_put_contracts: Decimal
    pending_call_contracts: Decimal
    opening_actions_locked: Literal[True] = True
    manual_review_required: bool
    reasons: tuple[str, ...]


class ReconciliationResult(ChronosModel):
    """Presentation-safe portfolio result; this milestone never unlocks an action."""

    status: ReconciliationStatus
    snapshot: ReconciliationSnapshotView | None
    symbols: tuple[SymbolReconciliation, ...]
    opening_actions_locked: Literal[True] = True
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _BrokerObservation:
    status_start: ConnectionStatus
    server_time_start: datetime
    account: AccountSummary
    positions_first: tuple[BrokerPosition, ...]
    orders_first: tuple[BrokerOrder, ...]
    executions_first: tuple[BrokerExecution, ...]
    positions_second: tuple[BrokerPosition, ...]
    orders_second: tuple[BrokerOrder, ...]
    executions_second: tuple[BrokerExecution, ...]
    server_time_end: datetime
    status_end: ConnectionStatus


class ReconciliationCoordinator:
    """Capture one bounded broker window and derive conservative Wheel state."""

    def __init__(
        self,
        connection: ConnectionRunner,
        local_reader: LocalReconciliationReader,
        allowlisted_symbols: Iterable[str],
        *,
        max_observation_seconds: float = 30.0,
        monotonic: Callable[[], float] = system_monotonic,
    ) -> None:
        symbols = frozenset(symbol.strip().upper() for symbol in allowlisted_symbols)
        if not symbols or any(not symbol for symbol in symbols):
            raise ValueError("Reconciliation requires at least one nonblank allowlisted symbol")
        if not isfinite(max_observation_seconds) or max_observation_seconds <= 0:
            raise ValueError("max_observation_seconds must be finite and positive")
        self._connection = connection
        self._broker = connection.broker
        self._local_reader = local_reader
        self._allowlisted_symbols = symbols
        self._max_observation_window = timedelta(seconds=max_observation_seconds)
        self._max_observation_seconds = max_observation_seconds
        self._monotonic = monotonic

    def reconcile(self) -> ReconciliationResult:
        """Read broker and local evidence without writing state or invoking an order method."""

        try:
            started_at = self._monotonic()
            observation = self._connection.run(self._observe_broker())
            elapsed_seconds = self._monotonic() - started_at
        except Exception:
            _LOGGER.warning(
                "Broker reconciliation observation failed; evidence remains locked",
                extra={"event": "reconciliation_broker_observation_failed"},
            )
            return self._pending_without_snapshot(
                "Broker evidence could not be captured; reconciliation remains locked."
            )

        broker_reasons = _observation_reasons(
            observation,
            self._max_observation_window,
            elapsed_seconds=elapsed_seconds,
            max_elapsed_seconds=self._max_observation_seconds,
        )
        if broker_reasons:
            _LOGGER.warning(
                "Broker reconciliation evidence was rejected; evidence remains locked",
                extra={"event": "reconciliation_broker_evidence_rejected"},
            )
            return self._pending_without_snapshot(*broker_reasons)

        local_evidence: LocalReconciliationEvidence | None = None
        local_read_failed = False
        account_id = observation.account.account_id
        if _is_well_formed_account_id(account_id):
            try:
                local_evidence = self._local_reader.read(account_id)
            except Exception:
                local_read_failed = True
                _LOGGER.warning(
                    "Local reconciliation evidence read failed; evidence remains locked",
                    extra={"event": "reconciliation_local_evidence_read_failed"},
                )
        else:
            local_read_failed = True
        return self._derive_result(
            observation,
            elapsed_seconds=elapsed_seconds,
            local_evidence=local_evidence,
            local_read_failed=local_read_failed,
        )

    async def _observe_broker(self) -> _BrokerObservation:
        status_start = await self._broker.connection_status()
        server_time_start = await self._broker.server_time()
        account = await self._broker.account_summary()
        positions_first = await self._broker.positions()
        orders_first = await self._broker.open_orders()
        executions_first = await self._broker.executions()
        positions_second = await self._broker.positions()
        orders_second = await self._broker.open_orders()
        executions_second = await self._broker.executions()
        server_time_end = await self._broker.server_time()
        status_end = await self._broker.connection_status()
        return _BrokerObservation(
            status_start=status_start,
            server_time_start=server_time_start,
            account=account,
            positions_first=positions_first,
            orders_first=orders_first,
            executions_first=executions_first,
            positions_second=positions_second,
            orders_second=orders_second,
            executions_second=executions_second,
            server_time_end=server_time_end,
            status_end=status_end,
        )

    def _derive_result(
        self,
        observation: _BrokerObservation,
        *,
        elapsed_seconds: float,
        local_evidence: LocalReconciliationEvidence | None,
        local_read_failed: bool,
    ) -> ReconciliationResult:
        global_reasons: list[str] = []
        identities = (
            local_evidence.active_order_identities if local_evidence is not None else frozenset()
        )
        if local_read_failed:
            global_reasons.append(
                "Local strategy evidence could not be read; reconciliation remains locked."
            )
        elif local_evidence is None or not local_evidence.complete:
            global_reasons.append(
                "Local strategy evidence is incomplete; reconciliation remains locked."
            )
        else:
            global_reasons.extend(_identity_reasons(identities, observation.account.account_id))

        globally_pending = bool(global_reasons)
        unresolved_symbols = (
            local_evidence.unresolved_symbols if local_evidence is not None else frozenset()
        )
        symbols = self._all_symbols(observation, identities, unresolved_symbols)
        symbol_results = tuple(
            self._reconcile_symbol(
                symbol,
                observation,
                identities=identities,
                unresolved_symbols=unresolved_symbols,
                globally_pending=globally_pending,
            )
            for symbol in symbols
        )
        if globally_pending:
            status = ReconciliationStatus.PENDING
        elif any(result.status is ReconciliationStatus.MANUAL_REVIEW for result in symbol_results):
            status = ReconciliationStatus.MANUAL_REVIEW
            global_reasons.append("One or more symbols require manual reconciliation review.")
        else:
            status = ReconciliationStatus.RECONCILED

        return ReconciliationResult(
            status=status,
            snapshot=_snapshot_view(observation, elapsed_seconds=elapsed_seconds),
            symbols=symbol_results,
            reasons=tuple(_unique(global_reasons)),
        )

    def _pending_without_snapshot(self, *reasons: str) -> ReconciliationResult:
        reason_values = tuple(_unique(reasons))
        symbols = tuple(
            SymbolReconciliation(
                symbol=symbol,
                status=ReconciliationStatus.PENDING,
                stage=WheelStage.MANUAL_REVIEW,
                stock_shares=Decimal("0"),
                unencumbered_shares=Decimal("0"),
                short_put_contracts=Decimal("0"),
                short_call_contracts=Decimal("0"),
                pending_put_contracts=Decimal("0"),
                pending_call_contracts=Decimal("0"),
                manual_review_required=True,
                reasons=reason_values,
            )
            for symbol in sorted(self._allowlisted_symbols)
        )
        return ReconciliationResult(
            status=ReconciliationStatus.PENDING,
            snapshot=None,
            symbols=symbols,
            reasons=reason_values,
        )

    def _all_symbols(
        self,
        observation: _BrokerObservation,
        identities: frozenset[BrokerOrderIdentity],
        unresolved_symbols: frozenset[str],
    ) -> tuple[str, ...]:
        observed = (
            {evidence.contract.symbol for evidence in observation.positions_second}
            | {evidence.contract.symbol for evidence in observation.orders_second}
            | {evidence.contract.symbol for evidence in observation.executions_second}
        )
        return tuple(
            sorted(
                self._allowlisted_symbols
                | observed
                | {identity.symbol for identity in identities}
                | unresolved_symbols
            )
        )

    def _reconcile_symbol(
        self,
        symbol: str,
        observation: _BrokerObservation,
        *,
        identities: frozenset[BrokerOrderIdentity],
        unresolved_symbols: frozenset[str],
        globally_pending: bool,
    ) -> SymbolReconciliation:
        positions = tuple(
            item for item in observation.positions_second if item.contract.symbol == symbol
        )
        orders = tuple(item for item in observation.orders_second if item.contract.symbol == symbol)
        executions = tuple(
            item for item in observation.executions_second if item.contract.symbol == symbol
        )
        matched_identities = frozenset(item for item in identities if item.symbol == symbol)

        if globally_pending:
            requested_status = ReconciliationStatus.PENDING
            provenance_reasons: list[str] = []
        else:
            requested_status, provenance_reasons = self._symbol_status(
                symbol,
                positions=positions,
                orders=orders,
                executions=executions,
                matched_identities=matched_identities,
                unresolved_symbols=unresolved_symbols,
            )

        decision = derive_wheel_state(
            WheelStateInput(
                symbol=symbol,
                expected_account_id=observation.account.account_id,
                positions=positions,
                open_orders=orders,
                matched_order_identities=matched_identities,
                reconciliation_status=requested_status,
                unexplained_mismatch=symbol not in self._allowlisted_symbols,
            )
        )
        if requested_status is ReconciliationStatus.PENDING:
            final_status = ReconciliationStatus.PENDING
        elif decision.manual_review_required:
            final_status = ReconciliationStatus.MANUAL_REVIEW
        else:
            final_status = requested_status
        return SymbolReconciliation(
            symbol=symbol,
            status=final_status,
            stage=decision.stage,
            stock_shares=decision.stock_shares,
            unencumbered_shares=decision.unencumbered_shares,
            short_put_contracts=decision.short_put_contracts,
            short_call_contracts=decision.short_call_contracts,
            pending_put_contracts=decision.pending_put_contracts,
            pending_call_contracts=decision.pending_call_contracts,
            manual_review_required=(
                decision.manual_review_required
                or final_status is not ReconciliationStatus.RECONCILED
            ),
            reasons=tuple(_unique([*provenance_reasons, *decision.reasons])),
        )

    def _symbol_status(
        self,
        symbol: str,
        *,
        positions: tuple[BrokerPosition, ...],
        orders: tuple[BrokerOrder, ...],
        executions: tuple[BrokerExecution, ...],
        matched_identities: frozenset[BrokerOrderIdentity],
        unresolved_symbols: frozenset[str],
    ) -> tuple[ReconciliationStatus, list[str]]:
        reasons: list[str] = []
        if symbol not in self._allowlisted_symbols:
            reasons.append("Broker or local evidence exists outside the configured allowlist.")
        if symbol in unresolved_symbols:
            reasons.append("Local strategy evidence for this symbol is not fully resolved.")
        if any(position.quantity != 0 for position in positions):
            reasons.append(
                "A nonzero broker position lacks complete cycle and fill-allocation provenance."
            )
        if executions:
            reasons.append(
                "Broker executions require complete local fill allocation before reconciliation."
            )

        exact_owned_put = _is_exact_owned_zero_fill_put(orders, matched_identities)
        if orders and not exact_owned_put:
            reasons.append(
                "Open-order evidence is not one exact, zero-fill, owned short-put submission."
            )
        if not orders and matched_identities:
            reasons.append("Local active-order ownership is absent from the broker snapshot.")
        if reasons:
            return ReconciliationStatus.MANUAL_REVIEW, reasons
        return ReconciliationStatus.RECONCILED, reasons


def _observation_reasons(
    observation: _BrokerObservation,
    max_window: timedelta,
    *,
    elapsed_seconds: float,
    max_elapsed_seconds: float,
) -> list[str]:
    reasons: list[str] = []
    expected_account = observation.account.account_id
    if (
        observation.status_start.state is not ConnectionState.CONNECTED
        or observation.status_end.state is not ConnectionState.CONNECTED
        or not observation.status_start.connected
        or not observation.status_end.connected
    ):
        reasons.append("The broker was not continuously connected during reconciliation.")
    if (
        observation.status_start.environment is not observation.status_end.environment
        or observation.status_start.data_quality is not observation.status_end.data_quality
    ):
        reasons.append("Broker environment or data quality changed during reconciliation.")
    if not _is_well_formed_account_id(expected_account) or any(
        not _is_well_formed_account_id(value) or value != expected_account
        for value in (
            observation.status_start.account_id,
            observation.status_end.account_id,
        )
    ):
        reasons.append("Broker account scope did not remain consistent during reconciliation.")

    if any(
        item.account_id != expected_account
        for items in (
            observation.positions_first,
            observation.positions_second,
            observation.orders_first,
            observation.orders_second,
            observation.executions_first,
            observation.executions_second,
        )
        for item in items
    ):
        reasons.append("Broker evidence does not belong to one consistent account scope.")

    if not _is_aware(observation.server_time_start) or not _is_aware(observation.server_time_end):
        reasons.append("Broker server time was not timezone-aware.")
    else:
        elapsed = observation.server_time_end - observation.server_time_start
        if elapsed < timedelta(0) or elapsed > max_window:
            reasons.append("Broker observation exceeded its bounded server-time window.")
    if (
        not isfinite(elapsed_seconds)
        or elapsed_seconds < 0
        or elapsed_seconds > max_elapsed_seconds
    ):
        reasons.append("Broker observation exceeded its bounded real-time window.")

    if (
        _position_signatures(observation.positions_first)
        != _position_signatures(observation.positions_second)
        or _order_signatures(observation.orders_first)
        != _order_signatures(observation.orders_second)
        or _execution_signatures(observation.executions_first)
        != _execution_signatures(observation.executions_second)
    ):
        reasons.append("Broker evidence changed during the reconciliation observation window.")

    for positions, orders, executions in (
        (
            observation.positions_first,
            observation.orders_first,
            observation.executions_first,
        ),
        (
            observation.positions_second,
            observation.orders_second,
            observation.executions_second,
        ),
    ):
        if _has_duplicate_evidence(positions, orders, executions):
            reasons.append("Broker snapshot contains duplicate state-critical evidence.")
        if _has_conflicting_evidence(positions, orders, executions):
            reasons.append("Broker snapshot contains conflicting contract or order evidence.")
    return _unique(reasons)


def _identity_reasons(
    identities: frozenset[BrokerOrderIdentity],
    expected_account_id: str,
) -> list[str]:
    reasons: list[str] = []
    if any(identity.account_id != expected_account_id for identity in identities):
        reasons.append("Local order ownership does not belong to the broker account scope.")
    broker_keys = [
        (identity.account_id, identity.client_id, identity.broker_order_id)
        for identity in identities
    ]
    permanent_keys = [
        (identity.account_id, identity.permanent_id)
        for identity in identities
        if identity.permanent_id is not None
    ]
    if len(set(broker_keys)) != len(broker_keys) or len(set(permanent_keys)) != len(permanent_keys):
        reasons.append("Local order ownership contains conflicting broker identities.")
    return reasons


def _is_exact_owned_zero_fill_put(
    orders: tuple[BrokerOrder, ...],
    identities: frozenset[BrokerOrderIdentity],
) -> bool:
    if len(orders) != 1:
        return False
    order = orders[0]
    return (
        isinstance(order.contract, OptionContract)
        and order.contract.right is OptionRight.PUT
        and order.side is OrderSide.SELL
        and order.lifecycle is OrderLifecycle.SUBMITTED
        and order.filled_quantity == 0
        and order.remaining_quantity == order.quantity
        and identities == frozenset({order.identity})
    )


def _has_duplicate_evidence(
    positions: tuple[BrokerPosition, ...],
    orders: tuple[BrokerOrder, ...],
    executions: tuple[BrokerExecution, ...],
) -> bool:
    position_keys = [(item.account_id, item.contract.con_id) for item in positions]
    order_keys = [(item.account_id, item.client_id, item.broker_order_id) for item in orders]
    execution_keys = [(item.account_id, item.execution_id) for item in executions]
    permanent_keys = [
        (item.account_id, item.permanent_id) for item in orders if item.permanent_id is not None
    ]
    return any(
        len(set(keys)) != len(keys)
        for keys in (position_keys, order_keys, execution_keys, permanent_keys)
    )


def _has_conflicting_evidence(
    positions: tuple[BrokerPosition, ...],
    orders: tuple[BrokerOrder, ...],
    executions: tuple[BrokerExecution, ...],
) -> bool:
    contracts: dict[int, str] = {}
    evidence: tuple[BrokerPosition | BrokerOrder | BrokerExecution, ...] = (
        *positions,
        *orders,
        *executions,
    )
    for item in evidence:
        contract_json = item.contract.model_dump_json()
        existing = contracts.setdefault(item.contract.con_id, contract_json)
        if existing != contract_json:
            return True

    orders_by_key = {
        (item.account_id, item.client_id, item.broker_order_id): item for item in orders
    }
    for execution in executions:
        order = orders_by_key.get(
            (execution.account_id, execution.client_id, execution.broker_order_id)
        )
        if order is None:
            continue
        if (
            order.contract != execution.contract
            or order.side is not execution.side
            or (
                order.permanent_id is not None
                and execution.permanent_id is not None
                and order.permanent_id != execution.permanent_id
            )
            or (
                order.order_ref is not None
                and execution.order_ref is not None
                and order.order_ref != execution.order_ref
            )
        ):
            return True
    return False


def _position_signatures(
    positions: tuple[BrokerPosition, ...],
) -> frozenset[tuple[str, str, Decimal, Decimal]]:
    return frozenset(
        (
            item.account_id,
            item.contract.model_dump_json(),
            item.quantity,
            item.average_cost,
        )
        for item in positions
    )


def _order_signatures(orders: tuple[BrokerOrder, ...]) -> frozenset[str]:
    return frozenset(item.model_dump_json() for item in orders)


def _execution_signatures(executions: tuple[BrokerExecution, ...]) -> frozenset[str]:
    return frozenset(item.model_dump_json() for item in executions)


def _snapshot_view(
    observation: _BrokerObservation,
    *,
    elapsed_seconds: float,
) -> ReconciliationSnapshotView:
    server_elapsed = max(
        0.0,
        (observation.server_time_end - observation.server_time_start).total_seconds(),
    )
    captured_at = (
        observation.server_time_end
        if _is_aware(observation.server_time_end)
        else observation.account.as_of
    )
    account = observation.account
    return ReconciliationSnapshotView(
        environment=observation.status_end.environment,
        data_quality=observation.status_end.data_quality,
        account=ReconciliationAccountView(
            masked_account_id=mask_account_id(account.account_id),
            net_liquidation=account.net_liquidation,
            total_cash=account.total_cash,
            buying_power=account.buying_power,
            currency=account.currency,
            as_of=account.as_of,
        ),
        positions=tuple(
            ReconciliationPositionView(
                contract=item.contract,
                quantity=item.quantity,
                average_cost=item.average_cost,
                market_price=item.market_price,
                unrealized_pnl=item.unrealized_pnl,
            )
            for item in observation.positions_second
        ),
        open_orders=tuple(
            ReconciliationOrderView(
                broker_order_id=item.broker_order_id,
                permanent_id=item.permanent_id,
                client_id=item.client_id,
                contract=item.contract,
                side=item.side,
                quantity=item.quantity,
                filled_quantity=item.filled_quantity,
                remaining_quantity=item.remaining_quantity,
                limit_price=item.limit_price,
                lifecycle=item.lifecycle,
                transmit=item.transmit,
                outside_rth=item.outside_rth,
            )
            for item in observation.orders_second
        ),
        execution_count=len(observation.executions_second),
        server_time_start=(
            observation.server_time_start if _is_aware(observation.server_time_start) else None
        ),
        server_time_end=(
            observation.server_time_end if _is_aware(observation.server_time_end) else None
        ),
        captured_at=captured_at,
        window_seconds=elapsed_seconds,
        server_window_seconds=server_elapsed,
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _is_well_formed_account_id(value: str | None) -> bool:
    return value is not None and bool(value) and value == value.strip()


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
