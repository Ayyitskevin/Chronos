"""S-12 RED: a blank-reference observation never settles a terminal state (LEGACY-1, LEGACY-2).

Packet ``legacy-resolver-scope/PACKET.md`` §1 FM-1/FM-2 (root cause RC-A), §3 (the S-12 test
design) and §6 Q2 (the adopted answer: **never**, progress only). ``_is_owned`` lets a listed
row or an execution that carries NO ``orderRef`` stand for the intent when its permId equals the
persisted one (BP-2 R-f). That fallback exists so a venue that reports an order back without
its reference still resolves to NON-terminal progress; on main today it can also drive a
TERMINAL write, which clears the opening-order lock on a permId-only identity the closed settle
design (r3 §2.1-2.3) refuses to trust.

Everything runs through the REAL ``OrderRestartReconciler.reconcile_report`` over a real
in-memory order book (the local builder below; main has no settle rig); only the broker is a
scripted ``FakeBroker``. The RED pins were strict xfails (LEGACY-1, LEGACY-2) until S-12's
implementation, which removed the decorators as they flipped; they are now ordinary pins, beside
the positive controls that must keep passing (the exact-reference settles, and blank-reference
PROGRESS, which BP-2 needs).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.support.order_fakes import PAPER_ACCOUNT, FakeBroker
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.domain.models import BrokerExecution, BrokerOrder, UnderlyingContract
from chronos.orders.reconciliation_readiness import ReconciliationReadiness
from chronos.orders.reconciliation_recovery import (
    OrderRestartReconciler,
    OrderRestartReconciliationReport,
)
from chronos.orders.state_machine import TERMINAL_ORDER_LIFECYCLES
from chronos.orders.tracker import OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
)
from chronos.utils.identifiers import account_fingerprint

SUBMITTED = OrderLifecycle.SUBMITTED
PF = OrderLifecycle.PARTIALLY_FILLED
CP = OrderLifecycle.CANCEL_PENDING
FILLED = OrderLifecycle.FILLED
CANCELLED = OrderLifecycle.CANCELLED
REJECTED = OrderLifecycle.REJECTED

ACCOUNT = PAPER_ACCOUNT
CLIENT_ID = 17
REF = "CHR-ORD-" + "B" * 32
BROKER_ID = 9001
PERM_ID = 109001
QUANTITY = Decimal("10")
LIMIT = Decimal("150.00")
CON_ID = 265598
SYMBOL = "AAPL"
SUBMITTED_AT = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
TODAY = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
_KEYS = itertools.count(1)


def _row(
    lifecycle: OrderLifecycle, filled: str, *, ref: str | None, perm: int | None = PERM_ID
) -> BrokerOrder:
    filled_quantity = Decimal(filled)
    return BrokerOrder(
        broker_order_id=BROKER_ID,
        permanent_id=perm,
        client_id=CLIENT_ID,
        account_id=ACCOUNT,
        order_ref=ref,
        contract=UnderlyingContract(con_id=CON_ID, symbol=SYMBOL),
        side=OrderSide.BUY,
        quantity=QUANTITY,
        filled_quantity=filled_quantity,
        remaining_quantity=QUANTITY - filled_quantity,
        limit_price=LIMIT,
        lifecycle=lifecycle,
    )


def _execution(execution_id: str, quantity: str, *, ref: str | None) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        account_id=ACCOUNT,
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        order_ref=ref,
        contract=UnderlyingContract(con_id=CON_ID, symbol=SYMBOL),
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        price=LIMIT,
        timestamp=TODAY,
    )


@dataclass
class Book:
    """A real in-memory order book, one SUBMITTED stock BUY with the ids a real submit writes."""

    db: Database
    broker: FakeBroker
    intents: OrderIntentRepository
    repo: OrderTrackerRepository
    readiness: ReconciliationReadiness
    connection: BrokerConnectionManager
    reconciler: OrderRestartReconciler

    def status(self) -> OrderLifecycle:
        record = self.intents.get("intent-1", current_account_id=ACCOUNT)
        assert record is not None
        return record.status

    def move_to(self, to_status: OrderLifecycle, *, filled: str | None = None) -> None:
        """Append one event as the system would have recorded it (a fill callback, the cancel
        service's mark), moving the intent to ``to_status``."""

        filled_quantity = None if filled is None else Decimal(filled)
        assert self.repo.record_transition(
            intent_id="intent-1",
            event_key=f"intent-1:test:{next(_KEYS)}",
            source="TEST",
            from_status=self.status(),
            to_status=to_status,
            current_account_id=ACCOUNT,
            broker_order_id=BROKER_ID,
            filled_quantity=filled_quantity,
            remaining_quantity=None if filled_quantity is None else QUANTITY - filled_quantity,
            occurred_at=SUBMITTED_AT,
        )

    def venue(
        self,
        *,
        open_orders: tuple[BrokerOrder, ...] = (),
        executions: tuple[BrokerExecution, ...] = (),
    ) -> None:
        self.broker._open_orders = open_orders
        self.broker._executions = executions

    def reconcile(self) -> OrderRestartReconciliationReport:
        return self.reconciler.reconcile_report(current_account_id=ACCOUNT, now=NOW)


@pytest.fixture
def book() -> Iterator[Book]:
    db = Database("sqlite:///:memory:")
    db.initialize()
    db.bind_scope(
        broker_mode="ibkr", environment="paper", account_id=ACCOUNT, key=TEST_PERSISTENCE_KEY
    )
    intents = OrderIntentRepository(db.sessions)
    repo = OrderTrackerRepository(db.sessions)
    tracker = OrderTracker(intents, repo)
    readiness = ReconciliationReadiness()
    broker = FakeBroker(account_id=ACCOUNT)
    connection = BrokerConnectionManager(broker, readiness=readiness)
    connection.start()
    reconciler = OrderRestartReconciler(
        connection=connection,
        intents=intents,
        tracker=tracker,
        reconciliation_readiness=readiness,
        expected_broker_client_id=CLIENT_ID,
        persistence_key=TEST_PERSISTENCE_KEY,
    )
    record = OrderIntentRecord(
        intent_id="intent-1",
        idempotency_key="idem-intent-1",
        account_fingerprint=account_fingerprint(ACCOUNT, key=TEST_PERSISTENCE_KEY),
        environment="paper",
        product_family=ProductFamily.STOCK,
        wheel_cycle_id=None,
        symbol=SYMBOL,
        con_id=CON_ID,
        local_symbol=None,
        action=OrderSide.BUY,
        open_close_effect="OPEN",
        quantity=QUANTITY,
        order_type="LMT",
        limit_price=LIMIT,
        time_in_force="DAY",
        outside_rth=False,
        quote_snapshot_id=None,
        risk_snapshot_id=None,
        preview_id=None,
        confirmation_hash=None,
        order_ref=REF,
        status=OrderLifecycle.USER_CONFIRMED,
        created_at=SUBMITTED_AT,
        confirmed_at=SUBMITTED_AT,
        submitted_at=None,
        expires_at=None,
    )
    assert intents.create(record, current_account_id=ACCOUNT)
    assert repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:submit",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=SUBMITTED,
        current_account_id=ACCOUNT,
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        filled_quantity=Decimal(0),
        remaining_quantity=QUANTITY,
        occurred_at=SUBMITTED_AT,
    )
    try:
        yield Book(db, broker, intents, repo, readiness, connection, reconciler)
    finally:
        connection.close()
        db.dispose()


def _local(book: Book, local: OrderLifecycle) -> None:
    """Put the seeded SUBMITTED intent in the local state under test."""

    if local is PF:
        book.move_to(PF, filled="3")
    elif local is CP:
        book.move_to(CP)
    else:
        assert local is SUBMITTED


def _assert_no_terminal_write(book: Book, report: OrderRestartReconciliationReport) -> None:
    """The adopted rule's observable form: the intent did not reach a terminal state, it is still
    remaining-active, and the restart report does not permit a new opening order."""

    assert book.status() not in TERMINAL_ORDER_LIFECYCLES, book.status()
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]
    assert report.permits_new_opening_order is False


_LOCAL_STATES = [SUBMITTED, PF, CP]
_BLANK = [None, "   "]  # missing, and blank (BrokerOrder/BrokerExecution normalise it to None)


# --- LEGACY-1: a blank-reference LISTED row never settles a terminal state ---------------------


@pytest.mark.parametrize("ref", _BLANK, ids=["missing_ref", "blank_ref"])
@pytest.mark.parametrize(
    ("row_lifecycle", "filled"),
    [(FILLED, "10"), (SUBMITTED, "10"), (REJECTED, "0"), (CANCELLED, "0")],
    ids=["typed_filled", "working_filled_by_quantity", "rejected", "cancelled"],
)
@pytest.mark.parametrize("local", _LOCAL_STATES, ids=lambda s: s.value)
def test_legacy_1_a_blank_reference_listed_row_never_settles_a_terminal_state(
    book: Book,
    local: OrderLifecycle,
    row_lifecycle: OrderLifecycle,
    filled: str,
    ref: str | None,
) -> None:
    _local(book, local)
    book.venue(open_orders=(_row(row_lifecycle, filled, ref=ref),))
    _assert_no_terminal_write(book, book.reconcile())


# --- LEGACY-2: blank-reference EXECUTIONS never settle FILLED ----------------------------------


@pytest.mark.parametrize(
    "executions",
    [
        (("e1", "6", None), ("e2", "4", None)),  # blank-ref executions alone reach Q
        (("e1", "10", "   "),),  # one blank (normalised) execution of the whole order
        (("e1", "6", REF), ("e2", "4", None)),  # exact 6 topped up by a blank 4
    ],
    ids=["blank_alone", "blank_single", "exact_plus_blank"],
)
@pytest.mark.parametrize("local", _LOCAL_STATES, ids=lambda s: s.value)
def test_legacy_2_blank_reference_executions_alone_never_settle_filled(
    book: Book, local: OrderLifecycle, executions: tuple[tuple[str, str, str | None], ...]
) -> None:
    _local(book, local)
    book.venue(executions=tuple(_execution(i, q, ref=r) for i, q, r in executions))
    _assert_no_terminal_write(book, book.reconcile())


# --- positive controls: they pass today and must keep passing --------------------------------


@pytest.mark.parametrize("local", _LOCAL_STATES, ids=lambda s: s.value)
def test_control_an_exact_reference_filled_row_settles_filled(
    book: Book, local: OrderLifecycle
) -> None:
    _local(book, local)
    book.venue(open_orders=(_row(FILLED, "10", ref=REF),))
    report = book.reconcile()
    assert book.status() is FILLED
    assert report.remaining_active == () and report.permits_new_opening_order is True


# CANCEL_PENDING is the U-A settle planner's once U-A lands: its executions-only settle (C10a) also
# needs the completed-orders capability, which this rig's FakeBroker does not have (fail closed:
# "no terminal record" cannot be established). Its counterparts are pinned in
# tests/integration/test_cancel_pending_recovery.py (test_r3b_on_caf70a5_*).
@pytest.mark.parametrize(
    "local", [state for state in _LOCAL_STATES if state is not CP], ids=lambda s: s.value
)
def test_control_exact_reference_executions_reaching_q_settle_filled(
    book: Book, local: OrderLifecycle
) -> None:
    _local(book, local)
    book.venue(executions=(_execution("e1", "6", ref=REF), _execution("e2", "4", ref=REF)))
    report = book.reconcile()
    assert book.status() is FILLED and report.permits_new_opening_order is True


@pytest.mark.parametrize("ref", _BLANK, ids=["missing_ref", "blank_ref"])
def test_control_a_blank_reference_working_row_still_records_partial_progress(
    book: Book, ref: str | None
) -> None:
    """BP-2 R-f's purpose, kept: permId stands in for a missing reference for PROGRESS."""

    book.venue(open_orders=(_row(SUBMITTED, "4", ref=ref),))
    report = book.reconcile()
    assert book.status() is PF
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]


def test_control_blank_reference_executions_below_q_still_record_partial_progress(
    book: Book,
) -> None:
    book.venue(executions=(_execution("e1", "4", ref=None),))
    book.reconcile()
    assert book.status() is PF


def test_control_a_blank_reference_working_row_with_no_fills_keeps_the_intent_submitted(
    book: Book,
) -> None:
    book.venue(open_orders=(_row(SUBMITTED, "0", ref=None),))
    report = book.reconcile()
    assert book.status() is SUBMITTED
    assert [item.intent_id for item in report.remaining_active] == ["intent-1"]


def test_control_a_blank_reference_row_with_another_permid_is_not_ours(book: Book) -> None:
    """The fallback's own boundary (BP-2 R-f): without the persisted permId, a blank-reference
    row is not this intent's at all, so nothing is written either today or after S-12."""

    book.venue(open_orders=(_row(FILLED, "10", ref=None, perm=PERM_ID + 1),))
    report = book.reconcile()
    assert book.status() is SUBMITTED
    assert report.permits_new_opening_order is False
