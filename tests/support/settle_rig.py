"""A rig for the CANCEL_PENDING settle planner (design r3): a real order book, scripted evidence.

The reconciler is the REAL ``OrderRestartReconciler`` over a real (in-memory) database; only the
broker is scripted, so each design row (C1..C12, P1..P5) can be driven by exact venue facts:

* :class:`ScriptedVenue` answers ``open_orders``, ``executions`` and ``completed_orders`` from
  lists the test sets, counts every call, and can run a hook inside a read (the race window);
* :func:`make_rig` builds the database, repositories, tracker, connection manager and reconciler;
* :meth:`Rig.seed` persists one SUBMITTED stock BUY intent whose ids are the ones a real submit
  writes (broker id 9001, permId 109001, client 17); :meth:`Rig.event` writes any later event.

The intent is a 10-share BUY so the cross-session fill cases (3 earlier + 2 now) fit. Times are
fixed: ``NOW`` is 11:00 in New York on a Monday, so the session boundary ``S0`` of the design is
04:00 UTC that day; ``PRIOR`` is the previous Friday and ``EARLIER_TODAY`` is 10:00 New York.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerExecution, BrokerOrder, UnderlyingContract
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    OrderRestartReconciliationReport,
)
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderEventRecord,
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.identifiers import account_fingerprint
from tests.support.order_fakes import PAPER_ACCOUNT, FakeBroker
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

ACCOUNT = PAPER_ACCOUNT
CLIENT_ID = 17
REF = "CHR-ORD-" + "S" * 32
BROKER_ID = 9001
PERM_ID = 109001
QUANTITY = Decimal("10")
LIMIT = Decimal("150.00")
CON_ID = 265598
SYMBOL = "AAPL"

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 11:00 New York, Monday
PRIOR = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # the previous Friday: before S0
EARLIER_TODAY = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # 10:00 New York: after S0
BEFORE_OPEN = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)  # 23:00 the evening before: before S0


def contract(con_id: int = CON_ID, symbol: str = SYMBOL) -> UnderlyingContract:
    return UnderlyingContract(con_id=con_id, symbol=symbol)


def venue_order(
    lifecycle: OrderLifecycle,
    filled: str | Decimal,
    *,
    quantity: Decimal = QUANTITY,
    ref: str | None = REF,
    perm: int | None = PERM_ID,
    client: int = CLIENT_ID,
    account: str = ACCOUNT,
    broker_id: int = BROKER_ID,
    side: OrderSide = OrderSide.BUY,
    limit: Decimal = LIMIT,
    con_id: int = CON_ID,
    symbol: str = SYMBOL,
) -> BrokerOrder:
    """What the venue says about the order (an open-book row or a completed-order record)."""

    filled_quantity = Decimal(filled)
    return BrokerOrder(
        broker_order_id=broker_id,
        permanent_id=perm,
        client_id=client,
        account_id=account,
        order_ref=ref,
        contract=contract(con_id, symbol),
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=quantity - filled_quantity,
        limit_price=limit,
        lifecycle=lifecycle,
    )


def venue_execution(
    execution_id: str,
    quantity: str | Decimal,
    at: datetime,
    *,
    ref: str | None = REF,
    perm: int | None = PERM_ID,
) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        account_id=ACCOUNT,
        broker_order_id=BROKER_ID,
        permanent_id=perm,
        client_id=CLIENT_ID,
        order_ref=ref,
        contract=contract(),
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        price=LIMIT,
        timestamp=at,
    )


class ScriptedVenue(FakeBroker):
    """A broker double whose evidence is what the test says, with call counts and a read hook.

    ``completed`` of ``None`` makes ``completed_orders`` raise (the read failed);
    ``completed_error`` makes it raise that exception instead (any failure, not only a typed one);
    ``open_script`` / ``exec_script`` / ``completed_script`` (a list of answers, the last
    repeating; ``None`` in ``completed_script`` = that read fails) let the SECOND snapshot differ.
    """

    def __init__(self) -> None:
        super().__init__(account_id=ACCOUNT)
        self.open: list[BrokerOrder] = []
        self.open_script: list[tuple[BrokerOrder, ...]] | None = None
        self.execs: list[BrokerExecution] = []
        self.completed: list[BrokerOrder] | None = []
        self.exec_script: list[tuple[BrokerExecution, ...]] | None = None
        self.completed_script: list[tuple[BrokerOrder, ...] | None] | None = None
        self.completed_error: Exception | None = None
        self.open_calls = 0
        self.exec_calls = 0
        self.completed_calls = 0
        self.during_completed: Callable[[], None] | None = None

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        self.open_calls += 1
        if self.open_script is not None:
            index = min(self.open_calls - 1, len(self.open_script) - 1)
            return self.open_script[index]
        return tuple(self.open)

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        self.exec_calls += 1
        if self.exec_script is not None:
            return self.exec_script[min(self.exec_calls - 1, len(self.exec_script) - 1)]
        return tuple(self.execs)

    async def completed_orders(self) -> tuple[BrokerOrder, ...]:
        self.completed_calls += 1
        if self.during_completed is not None:
            self.during_completed()
        if self.completed_error is not None:
            raise self.completed_error
        completed: tuple[BrokerOrder, ...] | list[BrokerOrder] | None = self.completed
        if self.completed_script is not None:
            index = min(self.completed_calls - 1, len(self.completed_script) - 1)
            completed = self.completed_script[index]
        if completed is None:
            from chronos.broker.base import BrokerDataError

            raise BrokerDataError("completed orders could not be read")
        return tuple(completed)


class VenueWithoutCompletedOrders(FakeBroker):
    """A broker that does NOT implement the completed-orders capability (e.g. the sim/demo)."""

    def __init__(self) -> None:
        super().__init__(account_id=ACCOUNT)
        self.open: list[BrokerOrder] = []
        self.execs: list[BrokerExecution] = []

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(self.open)

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        return tuple(self.execs)


@dataclass
class Rig:
    db: Database
    venue: Any
    intents: OrderIntentRepository
    repo: OrderTrackerRepository
    tracker: OrderTracker
    readiness: ReconciliationReadiness
    connection: BrokerConnectionManager
    reconciler: OrderRestartReconciler
    account: str = ACCOUNT
    broker_id: int | None = BROKER_ID

    def close(self) -> None:
        self.connection.close()
        self.db.dispose()

    # --- persisted state ------------------------------------------------------------------
    def status(self, intent_id: str = "intent-1") -> OrderLifecycle:
        record = self.intents.get(intent_id, current_account_id=self.account)
        assert record is not None
        return record.status

    def events(self, intent_id: str = "intent-1") -> tuple[OrderEventRecord, ...]:
        return self.repo.events(intent_id, current_account_id=self.account)

    def trail(self, intent_id: str = "intent-1") -> list[tuple[str, OrderLifecycle]]:
        return [(e.source, e.to_status) for e in self.events(intent_id)]

    def seed(
        self,
        *,
        intent_id: str = "intent-1",
        ref: str = REF,
        quantity: Decimal = QUANTITY,
        at: datetime = PRIOR,
        permanent_id: int | None = PERM_ID,
        client_id: int | None = CLIENT_ID,
        broker_id: int | None = BROKER_ID,
        symbol: str = SYMBOL,
        con_id: int = CON_ID,
        limit: Decimal = LIMIT,
    ) -> None:
        """A SUBMITTED stock BUY with the ids a real submit persists."""

        record = OrderIntentRecord(
            intent_id=intent_id,
            idempotency_key=f"idem-{intent_id}",
            account_fingerprint=account_fingerprint(self.account, key=TEST_PERSISTENCE_KEY),
            environment="paper",
            product_family=ProductFamily.STOCK,
            wheel_cycle_id=None,
            symbol=symbol,
            con_id=con_id,
            local_symbol=None,
            action=OrderSide.BUY,
            open_close_effect="OPEN",
            quantity=quantity,
            order_type="LMT",
            limit_price=limit,
            time_in_force="DAY",
            outside_rth=False,
            quote_snapshot_id=None,
            risk_snapshot_id=None,
            preview_id=None,
            confirmation_hash=None,
            order_ref=ref,
            status=OrderLifecycle.USER_CONFIRMED,
            created_at=at,
            confirmed_at=at,
            submitted_at=None,
            expires_at=None,
        )
        self.broker_id = broker_id
        assert self.intents.create(record, current_account_id=self.account)
        assert self.repo.record_transition(
            intent_id=intent_id,
            event_key=f"{intent_id}:submit",
            source="SUBMIT",
            from_status=OrderLifecycle.USER_CONFIRMED,
            to_status=OrderLifecycle.SUBMITTED,
            current_account_id=self.account,
            broker_order_id=broker_id,
            permanent_id=permanent_id,
            client_id=client_id,
            filled_quantity=Decimal(0),
            remaining_quantity=quantity,
            occurred_at=at,
        )

    def event(
        self,
        to_status: OrderLifecycle,
        *,
        at: datetime,
        filled: str | Decimal | None = None,
        source: str = "TEST",
        intent_id: str = "intent-1",
        quantity: Decimal = QUANTITY,
        permanent_id: int | None = None,
        client_id: int | None = None,
    ) -> None:
        """Append one event as the system would have recorded it, moving the intent to it."""

        current = self.status(intent_id)
        filled_quantity = None if filled is None else Decimal(filled)
        key = f"{intent_id}:test:{next(_COUNTER)}"
        assert self.repo.record_transition(
            intent_id=intent_id,
            event_key=key,
            source=source,
            from_status=current,
            to_status=to_status,
            current_account_id=self.account,
            broker_order_id=self.broker_id,
            permanent_id=permanent_id,
            client_id=client_id,
            filled_quantity=filled_quantity,
            remaining_quantity=None if filled_quantity is None else quantity - filled_quantity,
            occurred_at=at,
        )

    def pending(self, *, at: datetime = PRIOR) -> None:
        """The cancel service's mark: SUBMITTED/PARTIALLY_FILLED -> CANCEL_PENDING."""

        self.event(OrderLifecycle.CANCEL_PENDING, at=at, source="CANCEL")

    # --- the thing under test -------------------------------------------------------------
    def reconcile(self, now: datetime = NOW) -> OrderRestartReconciliationReport:
        return self.reconciler.reconcile_report(current_account_id=self.account, now=now)


_COUNTER = itertools.count(1)


def make_rig(
    venue: Any | None = None,
    *,
    account: str = ACCOUNT,
    monotonic: Callable[[], float] | None = None,
    call_timeout_seconds: float = 30.0,
) -> Rig:
    """``monotonic`` drives the U-B recovery dwell's clock (default: the reconciler's own);
    ``call_timeout_seconds`` is the connection manager's call budget (D11h)."""

    venue = venue if venue is not None else ScriptedVenue()
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(
        broker_mode="ibkr", environment="paper", account_id=account, key=TEST_PERSISTENCE_KEY
    )
    intents = OrderIntentRepository(db.sessions)
    repo = OrderTrackerRepository(db.sessions)
    tracker = OrderTracker(intents, repo)
    readiness = ReconciliationReadiness()
    connection = BrokerConnectionManager(
        venue, readiness=readiness, call_timeout_seconds=call_timeout_seconds
    )
    connection.start()
    extra: dict[str, Any] = {} if monotonic is None else {"monotonic": monotonic}
    reconciler = OrderRestartReconciler(
        connection=connection,
        intents=intents,
        tracker=tracker,
        reconciliation_readiness=readiness,
        expected_broker_client_id=CLIENT_ID,
        **extra,
        persistence_key=TEST_PERSISTENCE_KEY,
    )
    return Rig(db, venue, intents, repo, tracker, readiness, connection, reconciler, account)


def issues(report: OrderRestartReconciliationReport) -> list[str]:
    """The typed reasons the report gave for intents it could not prove."""

    return [item.reason for item in report.unresolved]


def one_pass_writes(rig: Rig, before: int) -> int:
    """How many events the last pass appended to ``intent-1`` (the planner writes at most one)."""

    return len(rig.events()) - before


def sequence(values: Sequence[Any]) -> list[Any]:  # pragma: no cover - tiny helper
    return list(values)
