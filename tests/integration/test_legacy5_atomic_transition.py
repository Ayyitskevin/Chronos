"""LEGACY-5 r2 (Daybreak r1 delta P2-1): one transition is ONE database transaction.

``OrderTrackerRepository.record_transition`` inserted the event inside a savepoint and assigned
the intent status after it. pysqlite opens no outer transaction before the first statement, so
that savepoint was the outermost transaction and its RELEASE committed the event alone; the status
followed in a later transaction. A second writer in between could durably record a fill that the
first writer's status then hid (``CANCELLED`` with 5 filled on record), or change the status a CAS
had already read.

These run over a REAL file-backed database with TWO session factories on one engine (two writers,
two connections) and pause the first writer with a SQLAlchemy session hook at the exact boundary:
right after its savepoint ends (the old split) or right as it opens (after its CAS read). The
second writer runs synchronously inside the hook, so the interleaving is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from chronos.domain.enums import OrderLifecycle, OrderSide, ProductFamily
from chronos.orders.tracker import OrderStatusUpdate, OrderTracker
from chronos.persistence.database import Database
from chronos.persistence.order_repositories import (
    OrderIntentRecord,
    OrderIntentRepository,
    OrderTrackerRepository,
    RecordedFillsAdvanced,
)
from chronos.utils.identifiers import account_fingerprint

ACCOUNT = "DU1234567"
BROKER_ID = 9001
PERM_ID = 109001
CLIENT_ID = 17
QUANTITY = Decimal("10")
PRIOR = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


@dataclass
class _Writer:
    intents: OrderIntentRepository
    repo: OrderTrackerRepository
    tracker: OrderTracker


@dataclass
class _Book:
    db: Database
    a_sessions: sessionmaker[Session]
    a: _Writer  # the writer the hooks pause
    b: _Writer  # the other writer, on its own session factory and connection

    def status(self, intent_id: str = "intent-1") -> OrderLifecycle:
        record = self.b.intents.get(intent_id, current_account_id=ACCOUNT)
        assert record is not None
        return record.status

    def trail(self, intent_id: str = "intent-1") -> list[tuple[str, Decimal | None]]:
        events = self.b.repo.events(intent_id, current_account_id=ACCOUNT)
        return [(event.to_status.value, event.filled_quantity) for event in events]

    def max_recorded(self, intent_id: str = "intent-1") -> Decimal:
        return self.b.tracker.recorded_filled_quantity(intent_id, current_account_id=ACCOUNT)

    def seed(self, local: OrderLifecycle, *, intent_id: str = "intent-1") -> None:
        record = OrderIntentRecord(
            intent_id=intent_id,
            idempotency_key=f"idem-{intent_id}",
            account_fingerprint=account_fingerprint(ACCOUNT),
            environment="paper",
            product_family=ProductFamily.STOCK,
            wheel_cycle_id=None,
            symbol="AAPL",
            con_id=265598,
            local_symbol=None,
            action=OrderSide.BUY,
            open_close_effect="OPEN",
            quantity=QUANTITY,
            order_type="LMT",
            limit_price=Decimal("150.00"),
            time_in_force="DAY",
            outside_rth=False,
            quote_snapshot_id=None,
            risk_snapshot_id=None,
            preview_id=None,
            confirmation_hash=None,
            order_ref="CHR-ORD-" + intent_id[-1] * 32,
            status=OrderLifecycle.USER_CONFIRMED,
            created_at=PRIOR,
            confirmed_at=PRIOR,
            submitted_at=None,
            expires_at=None,
        )
        assert self.b.intents.create(record, current_account_id=ACCOUNT)
        paths: dict[OrderLifecycle, list[tuple[OrderLifecycle, Decimal | None]]] = {
            OrderLifecycle.USER_CONFIRMED: [],
            OrderLifecycle.SUBMISSION_UNKNOWN: [(OrderLifecycle.SUBMISSION_UNKNOWN, None)],
            OrderLifecycle.PARTIALLY_FILLED: [
                (OrderLifecycle.SUBMITTED, Decimal(0)),
                (OrderLifecycle.PARTIALLY_FILLED, Decimal(3)),
            ],
        }
        path = paths[local]
        status = OrderLifecycle.USER_CONFIRMED
        for to_status, filled in path:
            assert self.b.repo.record_transition(
                intent_id=intent_id,
                event_key=f"{intent_id}:seed:{to_status.value}",
                source="SEED",
                from_status=status,
                to_status=to_status,
                current_account_id=ACCOUNT,
                broker_order_id=None if filled is None else BROKER_ID,
                filled_quantity=filled,
                occurred_at=PRIOR,
                enforce_from_status=True,
            )
            status = to_status
        assert self.status(intent_id) is local


def _writer(sessions: sessionmaker[Session]) -> _Writer:
    intents = OrderIntentRepository(sessions)
    repo = OrderTrackerRepository(sessions)
    return _Writer(intents, repo, OrderTracker(intents, repo))


@pytest.fixture
def book(tmp_path: Path) -> Iterator[_Book]:
    db = Database(f"sqlite:///{tmp_path / 'chronos.db'}")
    db.initialize()
    db.bind_scope(broker_mode="ibkr", environment="paper", account_id=ACCOUNT)
    a_sessions = sessionmaker(bind=db.engine, expire_on_commit=False, class_=Session)
    b_sessions = sessionmaker(bind=db.engine, expire_on_commit=False, class_=Session)
    try:
        yield _Book(db, a_sessions, _writer(a_sessions), _writer(b_sessions))
    finally:
        db.dispose()


def _once_at(
    sessions: sessionmaker[Session], hook: str, action: Callable[[], object]
) -> list[bool]:
    """Run ``action`` once, when a NESTED transaction of ``sessions`` hits ``hook``."""

    fired: list[bool] = []

    def listener(session: Session, transaction: SessionTransaction, *_: Any) -> None:
        if transaction.nested and not fired:
            fired.append(True)
            action()

    event.listen(sessions, hook, listener)
    return fired


def _update(lifecycle: OrderLifecycle, filled: str) -> OrderStatusUpdate:
    return OrderStatusUpdate(
        intent_id="intent-1",
        broker_order_id=BROKER_ID,
        permanent_id=PERM_ID,
        client_id=CLIENT_ID,
        lifecycle=lifecycle,
        filled_quantity=Decimal(filled),
        remaining_quantity=QUANTITY - Decimal(filled),
        occurred_at=NOW,
    )


# --- the split itself, for every record_transition caller --------------------------------------


@pytest.mark.parametrize("enforce", [True, False], ids=["cas", "plain"])
def test_r2_the_status_is_durable_when_the_event_is(book: _Book, enforce: bool) -> None:
    """At the moment the event's savepoint ends, another connection must already read the new
    status (and submitted_at): the event and the status are one commit, for every caller."""

    book.seed(OrderLifecycle.USER_CONFIRMED)
    seen: list[tuple[OrderLifecycle, datetime | None]] = []

    def observe() -> None:
        record = book.b.intents.get("intent-1", current_account_id=ACCOUNT)
        assert record is not None
        seen.append((record.status, record.submitted_at))

    fired = _once_at(book.a_sessions, "after_transaction_end", observe)
    assert book.a.repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:submit",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=ACCOUNT,
        broker_order_id=BROKER_ID,
        filled_quantity=Decimal(0),
        occurred_at=NOW,
        enforce_from_status=enforce,
    )

    assert fired == [True]
    assert [status for status, _ in seen] == [OrderLifecycle.SUBMITTED]
    assert seen[0][1] is not None  # submitted_at landed in the same commit


def test_r2_a_refused_write_leaves_neither_the_event_nor_the_status(book: _Book) -> None:
    book.seed(OrderLifecycle.PARTIALLY_FILLED)  # 0, then 3 recorded
    trail = book.trail()

    with pytest.raises(RecordedFillsAdvanced):
        book.a.tracker.ingest(
            _update(OrderLifecycle.CANCELLED, "0"), current_account_id=ACCOUNT, fills_floor=True
        )

    assert book.trail() == trail
    assert book.status() is OrderLifecycle.PARTIALLY_FILLED


# --- Daybreak's two-writer interleaving ---------------------------------------------------------


def test_r2_a_fill_landing_at_the_old_split_cannot_coexist_with_terminal_zero(
    book: _Book,
) -> None:
    """SUBMISSION_UNKNOWN, nothing recorded. Writer A ingests guarded CANCELLED/0; the moment its
    savepoint ends, writer B ingests the late PARTIALLY_FILLED/5 on its own connection; then A
    finishes. At b4afdd9 the result was CANCELLED with 5 filled on record (the lock released over
    a durable fill). Now A's event and status committed together before B ran, so B meets a
    terminal intent and records nothing: the durable state never says terminal-at-0 beside 5."""

    book.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    late: list[object] = []
    fired = _once_at(
        book.a_sessions,
        "after_transaction_end",
        lambda: late.append(
            book.b.tracker.ingest(
                _update(OrderLifecycle.PARTIALLY_FILLED, "5"), current_account_id=ACCOUNT
            )
        ),
    )

    outcome = book.a.tracker.ingest(
        _update(OrderLifecycle.CANCELLED, "0"), current_account_id=ACCOUNT, fills_floor=True
    )

    assert fired == [True] and outcome.lifecycle_changed
    status, recorded = book.status(), book.max_recorded()
    # the invariant Daybreak named: a terminal status never sits beside a larger recorded fill
    assert not (status is OrderLifecycle.CANCELLED and recorded > 0), (status, book.trail())
    assert status is OrderLifecycle.CANCELLED and recorded == Decimal(0)
    (b_outcome,) = late
    assert b_outcome.recorded is False  # type: ignore[attr-defined]


def test_r2_a_fill_landing_before_the_terminal_savepoint_is_a_typed_refusal(book: _Book) -> None:
    """The other order: B's PARTIALLY_FILLED/5 commits after A read the intent but before A's
    savepoint opens. A's guarded terminal write is refused (typed), PARTIALLY_FILLED/5 is kept and
    the intent stays active (the lock held)."""

    book.seed(OrderLifecycle.SUBMISSION_UNKNOWN)
    fired = _once_at(
        book.a_sessions,
        "after_transaction_create",
        lambda: book.b.tracker.ingest(
            _update(OrderLifecycle.PARTIALLY_FILLED, "5"), current_account_id=ACCOUNT
        ),
    )

    with pytest.raises(RecordedFillsAdvanced, match="5 filled on record"):
        book.a.tracker.ingest(
            _update(OrderLifecycle.CANCELLED, "0"), current_account_id=ACCOUNT, fills_floor=True
        )

    assert fired == [True]
    assert book.status() is OrderLifecycle.PARTIALLY_FILLED
    assert book.trail()[-1] == ("PARTIALLY_FILLED", Decimal(5))
    active = book.b.intents.active(current_account_id=ACCOUNT)
    assert [intent.intent_id for intent in active] == ["intent-1"]


def test_r2_a_cas_whose_status_moved_before_its_savepoint_writes_nothing(book: _Book) -> None:
    """The CAS read sits before the savepoint (outside SQLite's transaction under pysqlite); it is
    re-checked inside the savepoint, after the INSERT took the write lock. A status that moved in
    between makes the CAS lose: False, and neither the event nor the status is written."""

    book.seed(OrderLifecycle.USER_CONFIRMED)
    fired = _once_at(
        book.a_sessions,
        "after_transaction_create",
        lambda: book.b.repo.record_transition(
            intent_id="intent-1",
            event_key="intent-1:cancel-before-submit",
            source="CANCEL",
            from_status=OrderLifecycle.USER_CONFIRMED,
            to_status=OrderLifecycle.CANCELLED,
            current_account_id=ACCOUNT,
            occurred_at=NOW,
            enforce_from_status=True,
        ),
    )

    written = book.a.repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:submit",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=ACCOUNT,
        broker_order_id=BROKER_ID,
        occurred_at=NOW,
        enforce_from_status=True,
    )

    assert fired == [True]
    assert written is False
    assert book.status() is OrderLifecycle.CANCELLED
    assert [to for to, _ in book.trail()] == ["CANCELLED"]


# --- reader r1 delta P3-1..P3-3: the guard's scope ----------------------------------------------


def test_r2_the_maximum_is_per_intent(book: _Book) -> None:
    """P3-1: another intent's fills never refuse this intent's terminal write."""

    book.seed(OrderLifecycle.PARTIALLY_FILLED, intent_id="intent-1")  # 3 recorded
    book.seed(OrderLifecycle.SUBMISSION_UNKNOWN, intent_id="intent-2")  # nothing recorded
    update = _update(OrderLifecycle.CANCELLED, "0").model_copy(update={"intent_id": "intent-2"})

    outcome = book.a.tracker.ingest(update, current_account_id=ACCOUNT, fills_floor=True)

    assert outcome.lifecycle_changed
    assert book.status("intent-2") is OrderLifecycle.CANCELLED


def test_r2_ingest_without_the_keyword_keeps_todays_terminal_write(book: _Book) -> None:
    """P3-2: ``fills_floor`` defaults off; every other ingest caller is unchanged."""

    book.seed(OrderLifecycle.PARTIALLY_FILLED)
    outcome = book.a.tracker.ingest(
        _update(OrderLifecycle.CANCELLED, "0"), current_account_id=ACCOUNT
    )
    assert outcome.lifecycle_changed and book.status() is OrderLifecycle.CANCELLED


def test_r2_the_floor_applies_to_terminal_updates_only(book: _Book) -> None:
    """P3-3: a non-terminal update below the maximum (CANCEL_PENDING at 0 with 3 recorded) is
    written as before, even with ``fills_floor`` on."""

    book.seed(OrderLifecycle.PARTIALLY_FILLED)
    outcome = book.a.tracker.ingest(
        _update(OrderLifecycle.CANCEL_PENDING, "0"), current_account_id=ACCOUNT, fills_floor=True
    )
    assert outcome.lifecycle_changed and book.status() is OrderLifecycle.CANCEL_PENDING
