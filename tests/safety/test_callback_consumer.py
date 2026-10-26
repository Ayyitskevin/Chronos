"""CB-1: the supervised callback consumer (D-16 items 1-3 and 5; item 4 is not admitted).

Pins are numbered by the packet contract:

1. every unsolicited execution/orderStatus event for a known intent is journaled exactly once;
   replay is a no-op; divergent replay is a typed conflict;
2. an unknown intent or execution id is a typed outcome, never guessed or nearest-matched;
3. a crash mid-batch fails closed: no lifecycle advance or fill without its journal row first;
4. readiness is never published from consumer state; failure, overflow and cancellation demote
   it and are observable; nothing is dropped while it stays green;
5. no transmit, no admission change, the composite predicate is unchanged.

Each class of the sealed threat map's row 5 is named in a test (``_5a`` ... ``_5o``). Events
enter through the official adapter's ``_App`` EWrapper methods (``_load_ibapi`` replaced, as
RC-1's pins do) and through the Demo seam. Synthetic only; what IBKR streams on live is UNVERIFIED.
"""

from __future__ import annotations

import ast
import json
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from chronos import runtime as runtime_module
from chronos.broker import official_ibkr as official_module
from chronos.broker.callbacks import (
    UNSOLICITED_ORDER_STATUS_REASON,
    CallbackBridge,
    StreamEvent,
    StreamKind,
)
from chronos.broker.demo import DemoBroker
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import OrderLifecycle, OrderSide
from chronos.domain.models import BrokerExecution, UnderlyingContract
from chronos.orders import callback_consumer as consumer_module
from chronos.orders.callback_consumer import (
    CALLBACK_CONFLICT,
    CALLBACK_CONSUMER_HEALTH,
    CALLBACK_JOURNAL,
    CALLBACK_OUTCOME,
    CALLBACK_SOURCE,
    CONSUMER_FAILED_REASON,
    DIVERGENT_REPLAY_REASON,
    DROPPED_EVENT_REASON,
    UNPROVEN_CALLBACK_REASON,
    CallbackConsumer,
    CallbackJournal,
    CallbackOutcome,
    JournalVerdict,
)
from chronos.orders.ownership import PersistedOrderOwnershipLedger
from chronos.persistence.database import Database
from chronos.persistence.execution_repository import ExecutionRepository
from chronos.persistence.schema import ApplicationEventRow, CommissionRow, FillRow
from chronos.utils.time import utc_now
from tests.integration.test_order_pipeline import _drive_to_confirmed, _Harness, _short_put_intent
from tests.support.fake_ibapi import FakeCommissionReport, FakeContract, FakeExecution
from tests.support.order_fakes import FIXED_NOW, PAPER_ACCOUNT, FakeBroker, paper_settings
from tests.support.persistence_keys import TEST_PERSISTENCE_KEY

_ROOT = Path(__file__).resolve().parents[2]
_CLIENT = 17  # the FakeBroker's client id: the boundary persists it on the SUBMITTED event
_ORDER = 9001  # the FakeBroker's first broker order id
_PERM = 109001  # its permId (order id + 100000)
_EXEC_ID = "0001f4e8.65d4a5b8.01.01"
_OTHER_ACCOUNT = "DU7654321"  # synthetic: another account's execution


class _LyingInt(int):
    """An int subclass whose text is not its value (a hostile wrapper's shape)."""

    def __str__(self) -> str:
        return "1"


class _Crash(BaseException):
    """The process dies here: nothing after this line runs (not an Exception)."""


class _Clock:
    """Real time plus an offset, so a grace window can be crossed without sleeping."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self) -> Any:
        return utc_now() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@dataclass
class _Rig:
    harness: _Harness
    consumer: CallbackConsumer
    bridge: CallbackBridge
    app: Any
    clock: _Clock
    invalidations: list[str]
    order_ref: str
    record_calls: list[BrokerExecution] = field(default_factory=list)

    def status(self, *args: Any) -> None:
        """orderStatus(orderId, status, filled, remaining, avgFillPrice, permId, clientId)."""

        order_id, status, filled, remaining, average, perm_id, client_id = args
        self.app.orderStatus(
            order_id, status, filled, remaining, average, perm_id, 0, average, client_id, "", 0.0
        )

    def intent_status(self) -> OrderLifecycle:
        stored = self.harness.service.get("intent-1")
        assert stored is not None
        return stored.status

    def events(self) -> tuple[Any, ...]:
        return self.harness.tracker.events("intent-1", current_account_id=PAPER_ACCOUNT)


def _official_app(bridge: CallbackBridge, monkeypatch: pytest.MonkeyPatch) -> Any:
    class _EWrapper:
        pass

    class _EClient:
        def __init__(self, wrapper: object) -> None:
            del wrapper

    monkeypatch.setattr(official_module, "_load_ibapi", lambda: (_EClient, _EWrapper, None, None))
    return official_module._make_app(bridge)


def _submitted_harness() -> tuple[_Harness, str]:
    harness = _Harness(FakeBroker(), paper_settings())
    intent = _short_put_intent()
    _drive_to_confirmed(harness, intent, FIXED_NOW)
    harness.service.submit(intent, writer_lease_held=True, now=FIXED_NOW)  # type: ignore[arg-type]
    stored = harness.service.get("intent-1")
    assert stored is not None and stored.status is OrderLifecycle.SUBMITTED and stored.order_ref
    return harness, stored.order_ref


def _consumer(
    harness: _Harness,
    invalidations: list[str],
    clock: _Clock,
    *,
    tracker: Any = None,
    record: Any = None,
    **overrides: Any,
) -> CallbackConsumer:
    def invalidate(reason: str) -> object:
        invalidations.append(reason)
        return harness.readiness.invalidate(reason)

    writer = runtime_module._callback_execution_writer(ExecutionRepository(harness.db.sessions))
    return CallbackConsumer(
        sessions=harness.db.sessions,
        intents=harness.intents,
        tracker=tracker or harness.tracker,
        ownership=PersistedOrderOwnershipLedger(
            harness.intents, harness.tracker_repo, persistence_key=TEST_PERSISTENCE_KEY
        ),
        record_execution=record or writer,
        account_id=PAPER_ACCOUNT,
        persistence_key=TEST_PERSISTENCE_KEY,
        invalidate=invalidate,
        convert_execution=official_module.execution_from_pair,
        clock=clock,
        **overrides,
    )


def _make_rig(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> _Rig:
    harness, order_ref = _submitted_harness()
    invalidations: list[str] = []
    clock = _Clock()
    consumer = _consumer(harness, invalidations, clock, **overrides)
    bridge = CallbackBridge(
        RequestRegistry(),
        on_connection_uncertain=lambda reason: invalidations.append(reason),
        client_id=_CLIENT,
    )
    bridge.note_own_order(_ORDER)
    bridge.set_stream_sink(consumer.offer)
    consumer.open()
    return _Rig(
        harness,
        consumer,
        bridge,
        _official_app(bridge, monkeypatch),
        clock,
        invalidations,
        order_ref,
    )


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    made = _make_rig(monkeypatch)
    try:
        yield made
    finally:
        made.harness.close()


def _restart(rig: _Rig, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> _Rig:
    """A new process over the SAME database: new bridge (empty own set), new consumer."""

    consumer = _consumer(rig.harness, rig.invalidations, rig.clock, **overrides)
    bridge = CallbackBridge(
        RequestRegistry(),
        on_connection_uncertain=lambda reason: rig.invalidations.append(reason),
        client_id=_CLIENT,
    )
    bridge.set_stream_sink(consumer.offer)
    consumer.open()
    return _Rig(
        rig.harness,
        consumer,
        bridge,
        _official_app(bridge, monkeypatch),
        rig.clock,
        rig.invalidations,
        rig.order_ref,
    )


def _rows(harness: _Harness, event_type: str) -> list[dict[str, Any]]:
    with harness.db.sessions() as session:
        rows = session.scalars(
            select(ApplicationEventRow)
            .where(ApplicationEventRow.event_type == event_type)
            .order_by(ApplicationEventRow.id)
        ).all()
        return [
            {"key": row.correlation_id, "message": row.message, **dict(row.event_data)}
            for row in rows
        ]


def _outcomes(harness: _Harness) -> list[str]:
    return [row["outcome"] for row in _rows(harness, CALLBACK_OUTCOME)]


def _fills(harness: _Harness) -> list[tuple[str, Decimal, Decimal | None]]:
    with harness.db.sessions() as session:
        out = []
        for fill in session.scalars(select(FillRow).order_by(FillRow.execution_id)).all():
            commission = session.scalar(
                select(CommissionRow).where(CommissionRow.execution_id == fill.execution_id)
            )
            out.append(
                (
                    fill.execution_id,
                    Decimal(fill.price),
                    Decimal(commission.amount) if commission else None,
                )
            )
        return out


def _option_contract() -> FakeContract:
    contract = FakeContract()
    contract.conId = 111
    contract.symbol = "AAPL"
    contract.secType = "OPT"
    contract.lastTradeDateOrContractMonth = "20260821"
    contract.right = "P"
    contract.strike = 150.0
    contract.multiplier = "100"
    contract.localSymbol = "AAPL 260821P00150000"
    contract.tradingClass = "AAPL"
    return contract


def _execution(
    order_ref: str,
    *,
    exec_id: str = _EXEC_ID,
    account: str = PAPER_ACCOUNT,
    client_id: Any = _CLIENT,
    order_id: Any = _ORDER,
    perm_id: Any = _PERM,
    price: float = 1.25,
    side: str = "SLD",
) -> FakeExecution:
    execution = FakeExecution()
    execution.execId = exec_id
    execution.acctNumber = account
    execution.orderId = order_id
    execution.permId = perm_id
    execution.clientId = client_id
    execution.orderRef = order_ref
    execution.side = side
    execution.shares = 1.0
    execution.price = price
    execution.time = "20260717 15:00:01"
    return execution


# --- 1. journaled exactly once; replay is a no-op; divergent replay is a typed conflict --------


def test_1_an_own_order_status_on_the_official_path_is_journaled_once_and_advances_the_intent(
    rig: _Rig,
) -> None:
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert rig.consumer.drain() == 1

    assert rig.intent_status() is OrderLifecycle.FILLED
    callback_events = [event for event in rig.events() if event.source == CALLBACK_SOURCE]
    assert len(callback_events) == 1
    assert (callback_events[0].broker_order_id, callback_events[0].permanent_id) == (_ORDER, _PERM)
    journal = _rows(rig.harness, CALLBACK_JOURNAL)
    assert len(journal) == 1 and journal[0]["kind"] == "order_status"
    assert journal[0]["own_at_capture"] is True
    assert _outcomes(rig.harness) == [CallbackOutcome.APPLIED.value]
    assert rig.invalidations == []  # own and proven: nothing to demote


def test_1_a_replayed_order_status_is_a_no_op_even_after_a_reconnect(rig: _Rig) -> None:
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()
    before = len(rig.events())

    rig.bridge.reset_for_reconnect()  # IBKR re-sends statuses on reconnect
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.status(_ORDER, "Filled", 1.0, 0.0, 1.25, _PERM, _CLIENT)  # float spelling, same facts
    rig.status(_ORDER, " filled ", 1, 0, 1.25, _PERM, _CLIENT)  # status spelling, same status
    rig.consumer.drain()

    assert len(rig.events()) == before
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 1
    assert _outcomes(rig.harness) == [CallbackOutcome.APPLIED.value]
    assert _rows(rig.harness, CALLBACK_CONFLICT) == []


def test_1_a_divergent_order_status_replay_is_a_typed_conflict_that_mutates_nothing(
    rig: _Rig,
) -> None:
    rig.status(_ORDER, "Submitted", 0, 1, 0.0, _PERM, _CLIENT)
    rig.consumer.drain()
    before = len(rig.events())

    # same identity (status + cumulative fill), different remaining: a contradiction
    rig.status(_ORDER, "Submitted", 0, 5, 0.0, _PERM, _CLIENT)
    rig.consumer.drain()

    assert len(rig.events()) == before
    conflicts = _rows(rig.harness, CALLBACK_CONFLICT)
    assert len(conflicts) == 1 and conflicts[0]["fields"] == ["remaining"]
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 1  # the first row stands
    assert DIVERGENT_REPLAY_REASON in rig.invalidations


def test_1_an_own_execution_is_recorded_once_with_the_commission_that_came_first(
    rig: _Rig,
) -> None:
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    rig.consumer.drain()

    assert _fills(rig.harness) == [(_EXEC_ID, Decimal("1.25"), Decimal("1.05"))]
    assert sorted(_outcomes(rig.harness)) == sorted(
        [CallbackOutcome.APPLIED.value, CallbackOutcome.PAIRED.value]
    )
    # replay of both: nothing new
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    rig.consumer.drain()
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 2
    assert len(_outcomes(rig.harness)) == 2
    assert _fills(rig.harness) == [(_EXEC_ID, Decimal("1.25"), Decimal("1.05"))]


def test_1_a_divergent_execution_replay_is_a_typed_conflict(rig: _Rig) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, price=1.30))
    rig.consumer.drain()

    conflicts = _rows(rig.harness, CALLBACK_CONFLICT)
    assert len(conflicts) == 1 and conflicts[0]["fields"] == ["price"]
    assert DIVERGENT_REPLAY_REASON in rig.invalidations
    assert _fills(rig.harness) == []  # still waiting for its commission; never the 1.30 one


def test_1_the_demo_stream_is_journaled_typed_and_mutates_nothing(rig: _Rig) -> None:
    demo = DemoBroker()
    demo.set_stream_sink(rig.consumer.offer)
    foreign = BrokerExecution(
        execution_id="0000e0a1.foreign.01",
        account_id=PAPER_ACCOUNT,
        broker_order_id=0,
        client_id=0,
        contract=UnderlyingContract(con_id=265598, symbol="AAPL"),
        side=OrderSide.BUY,
        quantity=Decimal("100"),
        price=Decimal("190.25"),
        timestamp=FIXED_NOW,
    )
    demo.simulate_unsolicited_execution(foreign)
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.NOT_OWN_AT_CAPTURE.value]
    assert _fills(rig.harness) == []
    assert rig.intent_status() is OrderLifecycle.SUBMITTED


# --- 2. unknown / cross-account / cross-client identity: typed, never guessed --------------------


def test_2_5d_an_unknown_order_id_is_typed_and_never_attached_to_the_one_working_intent(
    rig: _Rig,
) -> None:
    rig.bridge.note_own_order(9555)  # own at capture, but no persisted intent carries it
    rig.status(9555, "Filled", 1, 0, 1.25, 109555, _CLIENT)
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.UNKNOWN_INTENT.value]
    assert rig.intent_status() is OrderLifecycle.SUBMITTED
    assert UNPROVEN_CALLBACK_REASON in rig.invalidations


@pytest.mark.parametrize(
    ("client_id", "perm_id", "outcome"),
    [
        (_CLIENT + 1, _PERM, CallbackOutcome.NOT_OWN_AT_CAPTURE),  # another client's order 9001
        (_CLIENT, _PERM + 1, CallbackOutcome.IDENTITY_MISMATCH),  # same order id, other permId
        (_CLIENT, 0, CallbackOutcome.IDENTITY_MISMATCH),  # no permId: order id alone never matches
    ],
)
def test_2_5d_cross_client_and_perm_mismatches_never_mutate_state(
    rig: _Rig, client_id: int, perm_id: int, outcome: CallbackOutcome
) -> None:
    rig.status(_ORDER, "Filled", 1, 0, 1.25, perm_id, client_id)
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [outcome.value]
    assert rig.intent_status() is OrderLifecycle.SUBMITTED
    assert [event for event in rig.events() if event.source == CALLBACK_SOURCE] == []
    assert rig.invalidations  # RC-1 (not own) or the consumer (unproven) demoted readiness


def test_2_5d_two_intents_carrying_one_order_id_are_ambiguous_not_first_match(rig: _Rig) -> None:
    second = _short_put_intent(
        "CHR-ORD-" + "B" * 32, intent_id="intent-2", limit_price=Decimal("1.10")
    )
    _drive_to_confirmed(rig.harness, second, FIXED_NOW)
    assert rig.harness.tracker_repo.record_transition(
        intent_id="intent-2",
        event_key="intent-2:9001:SUBMITTED",
        source="SUBMIT",
        from_status=OrderLifecycle.USER_CONFIRMED,
        to_status=OrderLifecycle.SUBMITTED,
        current_account_id=PAPER_ACCOUNT,
        broker_order_id=_ORDER,
        permanent_id=_PERM,
        client_id=_CLIENT,
        occurred_at=FIXED_NOW,
        enforce_from_status=True,
    )
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.AMBIGUOUS_INTENT.value]
    assert rig.intent_status() is OrderLifecycle.SUBMITTED


def test_2_5d_an_order_id_reused_after_a_restart_is_not_own_and_mutates_nothing(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarted = _restart(rig, monkeypatch)  # this process never submitted 9001
    restarted.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    restarted.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.NOT_OWN_AT_CAPTURE.value]
    assert rig.intent_status() is OrderLifecycle.SUBMITTED
    assert UNSOLICITED_ORDER_STATUS_REASON in rig.invalidations  # RC-1, unchanged


def test_2_a_cross_account_execution_is_typed_and_never_recorded(rig: _Rig) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, account=_OTHER_ACCOUNT))
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.CROSS_ACCOUNT.value]
    assert _fills(rig.harness) == []
    assert UNPROVEN_CALLBACK_REASON in rig.invalidations


def test_2_an_execution_whose_order_ref_is_unknown_is_unproven_and_never_recorded(
    rig: _Rig,
) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution("CHR-ORD-" + "Z" * 32))
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.OWNERSHIP_UNPROVEN.value]
    assert _fills(rig.harness) == []


def test_2_a_commission_for_an_unknown_execution_id_is_typed_after_its_grace(rig: _Rig) -> None:
    rig.app.commissionReport(FakeCommissionReport("ffff0000.unknown.01", 1.05, "USD"))
    rig.consumer.drain()
    assert _outcomes(rig.harness) == []  # inside its grace: it may still pair

    rig.clock.advance(31)
    rig.consumer.sweep()
    assert _outcomes(rig.harness) == [CallbackOutcome.UNKNOWN_EXECUTION.value]
    assert _fills(rig.harness) == []


def test_f6_an_unknown_execution_commission_demotes_like_an_unknown_intent(rig: _Rig) -> None:
    """CB1-FIX1 F6 (Fable probe P4): a commission for an execution this process never journaled
    is as unproven as an orderStatus for an unknown intent; both demote (tightening only)."""

    rig.app.commissionReport(FakeCommissionReport("ffff0000.orphan.01", 1.05, "USD"))
    rig.consumer.drain()
    rig.clock.advance(31)
    rig.consumer.sweep()
    assert _outcomes(rig.harness) == [CallbackOutcome.UNKNOWN_EXECUTION.value]
    assert rig.invalidations == [UNPROVEN_CALLBACK_REASON]


def test_2_a_status_for_an_intent_the_boundary_owns_is_deferred_never_ingested(
    rig: _Rig,
) -> None:
    assert rig.harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:SUBMISSION_UNKNOWN",
        source="TEST",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=OrderLifecycle.SUBMISSION_UNKNOWN,
        current_account_id=PAPER_ACCOUNT,
        occurred_at=FIXED_NOW,
        enforce_from_status=True,
    )
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()
    assert _outcomes(rig.harness) == []  # PENDING: the boundary / reconciler owns it for now

    rig.clock.advance(61)
    rig.consumer.sweep()
    assert _outcomes(rig.harness) == [CallbackOutcome.DEFERRED_TO_RECONCILER.value]
    assert rig.intent_status() is OrderLifecycle.SUBMISSION_UNKNOWN
    assert [event for event in rig.events() if event.source == CALLBACK_SOURCE] == []


# --- 3. a crash mid-batch fails closed -----------------------------------------------------------


class _Tracker:
    """The real tracker, with an injectable crash around ``ingest`` and a call log."""

    def __init__(self, real: Any, *, crash: str | None = None, log: list[str] | None = None):
        self._real = real
        self._crash = crash
        self.log = log if log is not None else []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def ingest(self, *args: Any, **kwargs: Any) -> Any:
        self.log.append("ingest")
        if self._crash == "before":
            raise _Crash
        result = self._real.ingest(*args, **kwargs)
        if self._crash == "after":
            raise _Crash
        return result


def test_3_5a_a_crash_between_the_journal_and_ingest_leaves_pending_and_restart_applies_it(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    crashing = _restart(rig, monkeypatch, tracker=_Tracker(rig.harness.tracker, crash="before"))
    crashing.bridge.note_own_order(_ORDER)
    crashing.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    with pytest.raises(_Crash):
        crashing.consumer.drain()
    assert rig.intent_status() is OrderLifecycle.SUBMITTED  # no advance
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 1 and _outcomes(rig.harness) == []

    recovered = _restart(rig, monkeypatch)
    assert recovered.consumer.sweep() == 1
    assert rig.intent_status() is OrderLifecycle.FILLED
    outcome = _rows(rig.harness, CALLBACK_OUTCOME)
    assert [o["outcome"] for o in outcome] == [CallbackOutcome.APPLIED.value]
    assert outcome[0]["recovered"] is True


def test_3_5a_a_crash_after_ingest_before_the_outcome_re_drives_as_a_no_op(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    crashing = _restart(rig, monkeypatch, tracker=_Tracker(rig.harness.tracker, crash="after"))
    crashing.bridge.note_own_order(_ORDER)
    crashing.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    with pytest.raises(_Crash):
        crashing.consumer.drain()
    assert rig.intent_status() is OrderLifecycle.FILLED  # the effect landed ...
    assert _outcomes(rig.harness) == []  # ... and the event is still PENDING, journaled
    events = len(rig.events())

    _restart(rig, monkeypatch).consumer.sweep()
    assert _outcomes(rig.harness) == [CallbackOutcome.NO_CHANGE.value]
    assert len(rig.events()) == events  # never applied twice


def test_3_5a_a_journal_failure_means_no_effect_at_all(rig: _Rig) -> None:
    calls: list[str] = []
    consumer = _consumer(
        rig.harness, rig.invalidations, rig.clock, tracker=_Tracker(rig.harness.tracker, log=calls)
    )

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    consumer.journal.append_pending = broken  # type: ignore[method-assign]
    rig.bridge.set_stream_sink(consumer.offer)
    consumer.open()
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    with pytest.raises(OperationalError):
        consumer.drain()
    assert calls == []
    assert rig.intent_status() is OrderLifecycle.SUBMITTED


def test_3_the_journal_is_written_before_the_effect_and_the_outcome_after_it(rig: _Rig) -> None:
    log: list[str] = []
    consumer = _consumer(
        rig.harness, rig.invalidations, rig.clock, tracker=_Tracker(rig.harness.tracker, log=log)
    )
    journal = consumer.journal
    pending, outcome = journal.append_pending, journal.append_outcome

    def spy_pending(*args: Any, **kwargs: Any) -> Any:
        log.append("journal")
        return pending(*args, **kwargs)

    def spy_outcome(*args: Any, **kwargs: Any) -> Any:
        log.append("outcome")
        return outcome(*args, **kwargs)

    journal.append_pending = spy_pending  # type: ignore[method-assign]
    journal.append_outcome = spy_outcome  # type: ignore[method-assign]
    rig.bridge.set_stream_sink(consumer.offer)
    consumer.open()
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    consumer.drain()
    assert log == ["journal", "ingest", "outcome"]


def test_3_5b_one_event_drives_one_writer_never_both(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingests: list[str] = []
    records: list[BrokerExecution] = []
    writer = runtime_module._callback_execution_writer(ExecutionRepository(rig.harness.db.sessions))

    def record(execution: BrokerExecution) -> Any:
        records.append(execution)
        return writer(execution)

    restarted = _restart(
        rig, monkeypatch, tracker=_Tracker(rig.harness.tracker, log=ingests), record=record
    )
    restarted.bridge.note_own_order(_ORDER)
    restarted.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    restarted.consumer.drain()
    assert (ingests, records) == (["ingest"], [])

    restarted.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    restarted.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    restarted.consumer.drain()
    assert ingests == ["ingest"] and len(records) == 1


def test_3_5a_a_crash_inside_the_fills_write_is_re_driven_once(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(execution: BrokerExecution) -> Any:
        raise _Crash

    crashing = _restart(rig, monkeypatch, record=crash)
    crashing.bridge.note_own_order(_ORDER)
    crashing.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    crashing.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    with pytest.raises(_Crash):
        crashing.consumer.drain()
    assert _fills(rig.harness) == []

    _restart(rig, monkeypatch).consumer.sweep()
    assert _fills(rig.harness) == [(_EXEC_ID, Decimal("1.25"), Decimal("1.05"))]
    assert CallbackOutcome.APPLIED.value in _outcomes(rig.harness)


def test_3_5n_a_restart_replays_from_the_journal_without_the_own_set_or_commission_cache(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    rig.consumer.drain()
    assert _fills(rig.harness) == [] and _outcomes(rig.harness) == []  # awaiting its commission

    restarted = _restart(rig, monkeypatch)  # empty own set, empty commission cache
    restarted.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    restarted.consumer.drain()
    assert _fills(rig.harness) == [(_EXEC_ID, Decimal("1.25"), Decimal("1.05"))]
    assert sorted(_outcomes(rig.harness)) == sorted(
        [CallbackOutcome.APPLIED.value, CallbackOutcome.PAIRED.value]
    )


# --- 5(e) out of order ----------------------------------------------------------------------------


def test_5e_a_late_working_status_after_fill_never_regresses_the_lifecycle(rig: _Rig) -> None:
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.status(_ORDER, "Submitted", 0, 1, 0.0, _PERM, _CLIENT)
    rig.consumer.drain()
    assert rig.intent_status() is OrderLifecycle.FILLED
    assert _outcomes(rig.harness) == [
        CallbackOutcome.APPLIED.value,
        CallbackOutcome.NO_CHANGE.value,
    ]


def test_5e_a_contradicting_terminal_is_typed_never_a_no_op(rig: _Rig) -> None:
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.status(_ORDER, "Cancelled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()
    assert rig.intent_status() is OrderLifecycle.FILLED
    assert _outcomes(rig.harness)[-1] == CallbackOutcome.LIFECYCLE_CONTRADICTION.value
    assert UNPROVEN_CALLBACK_REASON in rig.invalidations


def test_5e_execution_then_commission_pairs_and_a_late_commission_is_refused_and_kept(
    rig: _Rig,
) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    rig.consumer.drain()
    assert _fills(rig.harness) == [(_EXEC_ID, Decimal("1.25"), Decimal("1.05"))]

    second = "0001f4e8.65d4a5b8.02.01"
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, exec_id=second))
    rig.consumer.drain()
    rig.clock.advance(31)  # its commission never came inside the grace: recorded without
    rig.consumer.sweep()
    rig.app.commissionReport(FakeCommissionReport(second, 0.95, "USD"))
    rig.consumer.drain()

    fills = dict((exec_id, commission) for exec_id, _price, commission in _fills(rig.harness))
    assert fills == {_EXEC_ID: Decimal("1.05"), second: None}  # D-15: no loosening
    assert _outcomes(rig.harness)[-1] == CallbackOutcome.LATE_COMMISSION_REFUSED.value
    kept = [row for row in _rows(rig.harness, CALLBACK_JOURNAL) if row["kind"] == "commission"]
    assert {row["facts"]["amount"] for row in kept} == {"1.05", "0.95"}  # never lost


# --- CB1-FIX1 F1: an unrecognized status is refused to a typed unknown, never ingested ----------


def test_f1_an_unrecognized_status_is_typed_never_ingested_and_demotes(rig: _Rig) -> None:
    # "ApiPending" is real IBKR vocabulary the mapper does not know: it maps to SUBMISSION_UNKNOWN,
    # and SUBMITTED -> SUBMISSION_UNKNOWN is a legal transition (Fable probe P2)
    rig.status(_ORDER, "ApiPending", 0, 1, 0.0, _PERM, _CLIENT)
    rig.consumer.drain()

    outcomes = _rows(rig.harness, CALLBACK_OUTCOME)
    assert [row["outcome"] for row in outcomes] == ["unrecognized_status"]
    assert outcomes[0]["status"] == "apipending"  # the notice names the cause (D-43)
    assert rig.intent_status() is OrderLifecycle.SUBMITTED
    assert [event for event in rig.events() if event.source == CALLBACK_SOURCE] == []
    assert UNPROVEN_CALLBACK_REASON in rig.invalidations

    # control: a recognized status for the same intent still applies
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()
    assert rig.intent_status() is OrderLifecycle.FILLED
    assert _outcomes(rig.harness)[-1] == CallbackOutcome.APPLIED.value


# --- CB1-FIX1 F3: a CAS lost to a concurrent writer is re-driven, never closed as no_change ------


def test_f3_a_cas_lost_to_a_concurrent_writer_stays_pending_and_is_re_driven(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fable probe P3: a cancel moves the intent SUBMITTED -> CANCEL_PENDING inside the consumer's
    own record_transition call, so the consumer's CAS from SUBMITTED loses."""

    repo = rig.harness.tracker_repo
    real = repo.record_transition
    raced: list[OrderLifecycle] = []

    def racing(**kwargs: Any) -> bool:
        if kwargs.get("source") == CALLBACK_SOURCE and not raced:
            raced.append(kwargs["to_status"])
            assert real(
                intent_id="intent-1",
                event_key="intent-1:9001:CANCEL_PENDING:race",
                source="CANCEL",
                from_status=OrderLifecycle.SUBMITTED,
                to_status=OrderLifecycle.CANCEL_PENDING,
                current_account_id=PAPER_ACCOUNT,
                occurred_at=FIXED_NOW,
                enforce_from_status=True,
            )
        return real(**kwargs)

    monkeypatch.setattr(repo, "record_transition", racing)
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()

    assert raced == [OrderLifecycle.FILLED]
    assert rig.intent_status() is OrderLifecycle.CANCEL_PENDING
    assert _outcomes(rig.harness) == []  # the broker's Filled is not closed as no_change ...
    assert len(rig.consumer.journal.pending()) == 1  # ... it stays PENDING

    rig.consumer.sweep()  # re-driven against the status the other writer left
    assert rig.intent_status() is OrderLifecycle.FILLED
    assert _outcomes(rig.harness) == [CallbackOutcome.APPLIED.value]


# --- 5(c) the key carries every identity ---------------------------------------------------------


def test_5c_account_client_perm_and_cumulative_fill_each_change_the_key(rig: _Rig) -> None:
    rig.bridge.note_own_order(_ORDER)
    base = (_ORDER, "PartiallyFilled", 0, 1, 0.0, _PERM, _CLIENT)
    variants = [
        base,
        (_ORDER, "PartiallyFilled", 0, 1, 0.0, _PERM + 7, _CLIENT),
        (_ORDER, "PartiallyFilled", 0, 1, 0.0, _PERM, _CLIENT + 1),
        (_ORDER, "PartiallyFilled", 1, 0, 0.0, _PERM, _CLIENT),
        (_ORDER, "Submitted", 0, 1, 0.0, _PERM, _CLIENT),
    ]
    for args in variants:
        rig.status(*args)
    rig.consumer.drain()
    assert len({row["key"] for row in _rows(rig.harness, CALLBACK_JOURNAL)}) == len(variants)
    assert _rows(rig.harness, CALLBACK_CONFLICT) == []


# --- 5(f) two writers race one key on a file database --------------------------------------------


def _file_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'cb1.db'}")
    database.initialize()
    database.bind_scope(
        broker_mode="ibkr", environment="paper", account_id=PAPER_ACCOUNT, key=TEST_PERSISTENCE_KEY
    )
    return database


def test_5f_racing_journal_writers_on_a_file_database_write_one_row(tmp_path: Path) -> None:
    database = _file_database(tmp_path)
    try:
        journal = CallbackJournal(database.sessions)
        barrier = threading.Barrier(4)
        verdicts: list[JournalVerdict] = []

        def append() -> None:
            barrier.wait()
            verdicts.append(
                journal.append_pending(
                    "a" * 64,
                    StreamKind.ORDER_STATUS,
                    {"x": 1},
                    received_at=utc_now(),
                    own_at_capture=True,
                ).verdict
            )

        threads = [threading.Thread(target=append) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert sorted(verdicts) == sorted([JournalVerdict.NEW] + [JournalVerdict.REPLAY] * 3)
        with database.sessions() as session:
            rows = session.scalars(
                select(ApplicationEventRow).where(ApplicationEventRow.correlation_id == "a" * 64)
            ).all()
            assert len(rows) == 1
    finally:
        database.dispose()


# --- 5(g) malformed input never raises into the reader -------------------------------------------


@pytest.mark.parametrize(
    ("filled", "remaining", "average", "perm_id"),
    [
        (float("nan"), 0, 1.25, _PERM),
        (1, float("inf"), 1.25, _PERM),
        (-1, 0, 1.25, _PERM),
        (1, 0, 1.7976931348623157e308, _PERM),
        (1, 0, 1.25, 109001.0),
        (True, 0, 1.25, _PERM),
        (_LyingInt(0), 0, 1.25, _PERM),  # reads "1": only an exact type is a quantity
    ],
)
def test_5g_a_malformed_order_status_is_journaled_typed_and_never_raises(
    rig: _Rig, filled: Any, remaining: Any, average: Any, perm_id: Any
) -> None:
    rig.status(_ORDER, "Filled", filled, remaining, average, perm_id, _CLIENT)
    rig.consumer.drain()
    assert _outcomes(rig.harness) == [CallbackOutcome.MALFORMED.value]
    assert rig.intent_status() is OrderLifecycle.SUBMITTED
    assert UNPROVEN_CALLBACK_REASON in rig.invalidations


def test_5g_malformed_executions_and_commissions_are_typed_and_never_raise(rig: _Rig) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, side="XXX"))
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, order_id=9001.0))
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, float("nan"), "USD"))
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.7976931348623157e308, "USD"))
    rig.consumer.drain()
    assert _outcomes(rig.harness) == [CallbackOutcome.MALFORMED.value] * 4
    assert _fills(rig.harness) == []


def test_5g_a_raising_sink_never_reaches_the_reader_thread(rig: _Rig) -> None:
    def raising(event: StreamEvent) -> None:
        raise RuntimeError("sink down")

    rig.bridge.set_stream_sink(raising)
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)  # returns
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))  # returns
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))  # returns


# --- 5(j) requested vs unsolicited ---------------------------------------------------------------


def test_5j_a_requested_execution_stays_on_the_registry_and_never_reaches_the_consumer(
    rig: _Rig,
) -> None:
    request_id = rig.bridge.registry.open()
    rig.app.execDetails(request_id, _option_contract(), _execution(rig.order_ref))
    assert rig.consumer.drain() == 0
    assert _rows(rig.harness, CALLBACK_JOURNAL) == []

    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))  # unsolicited: retained
    assert rig.consumer.drain() == 1


# --- 4 / 5(h) / 5(i): dropped, failed, stopped, halted: never silently green ---------------------


def test_4_5h_overflow_demotes_readiness_on_every_dropped_event_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = _make_rig(monkeypatch, capacity=2)
    try:
        for perm in (_PERM, _PERM + 1, _PERM + 2, _PERM + 3):
            made.status(_ORDER, "Submitted", 0, 1, 0.0, perm, _CLIENT)
        assert made.invalidations.count(DROPPED_EVENT_REASON) == 2
        assert made.consumer.health().dropped == 2
        made.consumer.drain()
        made.consumer._note_drops()  # the consumer thread's tick: the drop becomes a durable row
        health = _rows(made.harness, CALLBACK_CONSUMER_HEALTH)
        assert len(health) == 1 and health[0]["dropped"] == 2
    finally:
        made.harness.close()


def test_4_5h_an_event_offered_while_not_running_is_dropped_red(rig: _Rig) -> None:
    rig.consumer.stop()
    rig.invalidations.clear()
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert rig.invalidations[-1] == DROPPED_EVENT_REASON
    assert rig.consumer.health().dropped == 1


def test_4_5h_a_stop_with_queued_events_counts_them_lost_and_demotes(rig: _Rig) -> None:
    rig.status(_ORDER, "Submitted", 0, 1, 0.0, _PERM, _CLIENT)
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert rig.consumer.stop() == 2
    assert rig.invalidations[-1] == DROPPED_EVENT_REASON
    assert _rows(rig.harness, CALLBACK_CONSUMER_HEALTH)[-1]["state"] == "stopped"


def test_4_5h_a_database_failure_is_retried_demotes_once_and_keeps_the_event(rig: _Rig) -> None:
    journal = rig.consumer.journal
    real = journal.append_pending
    failures = [OperationalError("INSERT", {}, Exception("database is locked"))]

    def flaky(*args: Any, **kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    journal.append_pending = flaky  # type: ignore[method-assign]
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    event = rig.consumer._queue.get_nowait()
    rig.consumer._handle_with_retry(event)  # the consumer thread's per-event supervision
    assert rig.invalidations.count(CONSUMER_FAILED_REASON) == 1
    assert rig.intent_status() is OrderLifecycle.FILLED
    assert rig.consumer.health().state == "running" and rig.consumer.health().failures == 0


class _FileRig:
    """A file database (the production topology) and a consumer with no intents behind it."""

    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.database = _file_database(tmp_path)
        self.invalidations: list[str] = []
        from chronos.orders.tracker import OrderTracker
        from chronos.persistence.order_repositories import (
            OrderIntentRepository,
            OrderTrackerRepository,
        )

        intents = OrderIntentRepository(self.database.sessions)
        tracker_repo = OrderTrackerRepository(self.database.sessions)
        self.consumer = CallbackConsumer(
            sessions=self.database.sessions,
            intents=intents,
            tracker=OrderTracker(intents, tracker_repo),
            ownership=PersistedOrderOwnershipLedger(
                intents, tracker_repo, persistence_key=TEST_PERSISTENCE_KEY
            ),
            record_execution=runtime_module._callback_execution_writer(
                ExecutionRepository(self.database.sessions)
            ),
            account_id=PAPER_ACCOUNT,
            persistence_key=TEST_PERSISTENCE_KEY,
            invalidate=self.invalidations.append,
            **overrides,
        )

    def foreign(self, index: int) -> StreamEvent:
        return StreamEvent(
            kind=StreamKind.ORDER_STATUS,
            received_at=utc_now(),
            own_at_capture=False,
            raw={
                "order_id": 500 + index,
                "status": "Submitted",
                "filled": 0,
                "remaining": 1,
                "avg_fill_price": 0.0,
                "perm_id": 900 + index,
                "client_id": 0,
            },
        )

    def outcomes(self) -> list[str]:
        with self.database.sessions() as session:
            rows = session.scalars(
                select(ApplicationEventRow).where(
                    ApplicationEventRow.event_type == CALLBACK_OUTCOME
                )
            ).all()
            return [str(row.event_data["outcome"]) for row in rows]


def _wait_until(predicate: Any, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_4_5h_the_supervised_thread_journals_and_stops_clean_on_a_file_database(
    tmp_path: Path,
) -> None:
    file_rig = _FileRig(tmp_path)
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(1))
        assert _wait_until(lambda: file_rig.outcomes() == ["not_own_at_capture"])
        assert file_rig.consumer.stop() == 0
        assert file_rig.consumer.health().state == "stopped"
        assert file_rig.invalidations == []
    finally:
        file_rig.database.dispose()


def test_4_5h_a_lost_writer_lease_halts_the_consumer_and_every_later_event_demotes(
    tmp_path: Path,
) -> None:
    file_rig = _FileRig(tmp_path)
    writer = [True]
    try:
        file_rig.consumer.start(is_writer=lambda: writer[0])
        writer[0] = False
        assert _wait_until(lambda: file_rig.consumer.health().state == "halted")
        assert CONSUMER_FAILED_REASON in file_rig.invalidations
        file_rig.consumer.offer(file_rig.foreign(2))
        assert file_rig.invalidations[-1] == DROPPED_EVENT_REASON
        file_rig.consumer.stop()
        assert file_rig.outcomes() == []  # no write after the lease was lost
    finally:
        file_rig.database.dispose()


def test_4_5h_a_dead_consumer_thread_is_loud_and_every_later_event_demotes(
    tmp_path: Path,
) -> None:
    file_rig = _FileRig(tmp_path)
    try:

        def die(event: StreamEvent) -> Any:
            raise _Crash

        file_rig.consumer.handle = die  # type: ignore[method-assign]
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(3))
        assert _wait_until(lambda: file_rig.consumer.health().state == "halted")
        assert CONSUMER_FAILED_REASON in file_rig.invalidations
        file_rig.consumer.offer(file_rig.foreign(4))
        assert file_rig.invalidations[-1] == DROPPED_EVENT_REASON
        file_rig.consumer.stop()
    finally:
        file_rig.database.dispose()


def test_5i_offer_never_blocks_the_reader_while_the_consumer_is_stuck_on_the_database(
    tmp_path: Path,
) -> None:
    file_rig = _FileRig(tmp_path)
    release = threading.Event()
    real = file_rig.consumer.journal.append_pending

    def stuck(*args: Any, **kwargs: Any) -> Any:
        release.wait(10)
        return real(*args, **kwargs)

    file_rig.consumer.journal.append_pending = stuck  # type: ignore[method-assign]
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        started = time.monotonic()
        for index in range(1100):  # more than the queue holds
            file_rig.consumer.offer(file_rig.foreign(index))
        assert time.monotonic() - started < 2.0
        assert file_rig.consumer.health().dropped > 0
        assert DROPPED_EVENT_REASON in file_rig.invalidations
    finally:
        release.set()
        file_rig.consumer.stop(timeout=5)
        file_rig.database.dispose()


def _orphan_commission(index: int) -> StreamEvent:
    return StreamEvent(
        kind=StreamKind.COMMISSION,
        received_at=utc_now(),
        own_at_capture=False,
        raw={"execId": f"ffff0000.orphan.{index:02d}", "commission": 1.05, "currency": "USD"},
    )


def test_f5_idle_seconds_do_not_sweep_but_the_cadence_still_closes_a_pending_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CB1-FIX1 F5: the PENDING sweep is a full anti-join over an append-only journal, so it
    runs on start, after a failure streak clears, and every ``_SWEEP_SECONDS``, not on every
    idle second."""

    file_rig = _FileRig(tmp_path, commission_grace=timedelta(seconds=0.5))
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(_orphan_commission(1))  # PENDING: inside its grace when handled
        time.sleep(2.5)  # two idle seconds go by, past the grace
        assert file_rig.outcomes() == []  # not swept on an idle tick

        monkeypatch.setattr(consumer_module, "_SWEEP_SECONDS", 0.2)
        assert _wait_until(lambda: file_rig.outcomes() == ["unknown_execution"], timeout=3.0)
    finally:
        file_rig.consumer.stop()
        file_rig.database.dispose()


def test_f5_a_cleared_failure_streak_sweeps_without_waiting_for_the_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumer_module, "_RETRY_BASE_SECONDS", 0.0)
    file_rig = _FileRig(tmp_path, commission_grace=timedelta(seconds=0.5))
    journal = file_rig.consumer.journal
    real = journal.append_pending
    failures = [OperationalError("INSERT", {}, Exception("database is locked"))]

    def flaky(*args: Any, **kwargs: Any) -> Any:
        if failures and args[1] is StreamKind.ORDER_STATUS:  # only the foreign status fails
            raise failures.pop()
        return real(*args, **kwargs)

    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(_orphan_commission(2))  # PENDING inside its grace
        time.sleep(1.0)  # past the grace; the 30 s cadence is far away
        assert file_rig.outcomes() == []
        journal.append_pending = flaky  # type: ignore[method-assign]
        file_rig.consumer.offer(file_rig.foreign(7))  # fails once, then succeeds: streak cleared
        assert _wait_until(lambda: "unknown_execution" in file_rig.outcomes(), timeout=3.0)
        assert CONSUMER_FAILED_REASON in file_rig.invalidations
    finally:
        file_rig.consumer.stop()
        file_rig.database.dispose()


# --- 4 / 5(k) / 5: readiness is never published or inferred by the consumer ---------------------

_LATCH_NAMES = frozenset(
    {
        "reconcile_submission_readiness",
        "_reconcile_submission_readiness_generation",
        "complete",
        "begin_reconciliation",
        "reconciliation_session",
        "snapshot",
        "submission_claim_is_current",
        "invalidating_transition",
        "ready",
        "reconciliation_readiness",
        "ReconciliationReadiness",
    }
)


def _names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


def test_4_the_consumer_module_names_no_latch_method_and_no_composite() -> None:
    source = (_ROOT / "src/chronos/orders/callback_consumer.py").read_text(encoding="utf-8")
    assert _names(source) & _LATCH_NAMES == set()
    # positive control: the detector sees each shape
    for snippet in (
        "runtime.reconcile_submission_readiness(trigger='x')",
        "self._readiness.complete(expected_generation=1)",
        "from chronos.orders.reconciliation_readiness import ReconciliationReadiness",
        "latch.snapshot().ready",
    ):
        assert _names(snippet) & _LATCH_NAMES, snippet


def test_4_the_runtime_hands_the_consumer_only_the_invalidate_method() -> None:
    tree = ast.parse((_ROOT / "src/chronos/runtime.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_build_callback_consumer"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["invalidate"] == "reconciliation_readiness.invalidate"
    assert all(
        "reconciliation_readiness" not in value
        for name, value in keywords.items()
        if name != "invalidate"
    )


def test_4_5k_the_composite_keeps_its_callers_and_no_callback_trigger_exists() -> None:
    callers = set()
    for path in (_ROOT / "src/chronos").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".reconcile_submission_readiness(" in text:
            callers.add(path.relative_to(_ROOT).as_posix())
        assert "callback-convergence" not in text, path
    assert callers == {
        "src/chronos/api/main.py",
        "src/chronos/api/reconciliation_loop.py",
        "src/chronos/api/routes/orders.py",
    }
    from chronos.persistence.reconciliation_repository import TRIGGERS

    assert TRIGGERS == (
        "startup",
        "reconnect",
        "order_fill",
        "periodic",
        "operator",
        "unattributed",
    )


def _predicate_chain() -> list[tuple[str, str, str]]:
    tree = ast.parse((_ROOT / "src/chronos/runtime.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_reconcile_submission_readiness_generation"
    )
    chain: list[tuple[str, str, str]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.If):
            for branch in (node.body, node.orelse):
                assigned = {
                    target.id: ast.unparse(statement.value)
                    for statement in branch
                    if isinstance(statement, ast.Assign)
                    for target in statement.targets
                    if isinstance(target, ast.Name)
                }
                if "status" in assigned:
                    chain.append((ast.unparse(node.test), assigned["status"], assigned["reason"]))
    return chain


def test_5_the_composite_predicate_is_unchanged() -> None:
    # expected: the chain as it reads at the CB-1 base (572be6d), the same walk
    assert _predicate_chain() == [
        (
            "not scope_is_current",
            "ReconciliationStatus.PENDING",
            "'broker connection/account scope changed during reconciliation'",
        ),
        (
            "portfolio.status is ReconciliationStatus.MANUAL_REVIEW",
            "ReconciliationStatus.MANUAL_REVIEW",
            "'portfolio reconciliation requires manual review'",
        ),
        (
            "portfolio.status is not ReconciliationStatus.RECONCILED or portfolio.snapshot is None",
            "ReconciliationStatus.PENDING",
            "'portfolio reconciliation did not produce complete evidence'",
        ),
        (
            "portfolio.snapshot.open_orders",
            "ReconciliationStatus.PENDING",
            "'broker open orders remain; another opening action is locked'",
        ),
        (
            "not restart.permits_new_opening_order",
            "ReconciliationStatus.PENDING",
            "'sent-active order intents remain after restart reconciliation; "
            "another opening action is locked'",
        ),
        (
            "not restart.permits_new_opening_order",
            "ReconciliationStatus.RECONCILED",
            "'full broker/local parity proven with no sent-active order intents'",
        ),
    ]


def test_4_a_terminal_callback_with_no_open_orders_never_publishes_readiness(rig: _Rig) -> None:
    rig.harness.readiness.invalidate("test: a pass is due")
    before = rig.harness.readiness.snapshot()
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()
    after = rig.harness.readiness.snapshot()

    assert rig.intent_status() is OrderLifecycle.FILLED  # nothing sent-active is left ...
    assert (after.status, after.generation) == (before.status, before.generation)  # ... and still
    assert not after.ready


def test_5_the_consumer_reaches_no_transmit_or_admission_surface() -> None:
    source = (_ROOT / "src/chronos/orders/callback_consumer.py").read_text(encoding="utf-8")
    names = _names(source)
    assert (
        names
        & {
            "submit_order",
            "cancel_order",
            "modify_order",
            "placeOrder",
            "cancelOrder",
            "transmit",
            "OrderSubmissionBoundary",
            "submission_boundary",
            "risk",
            "autonomy",
            "connection",
            "run",
        }
        == set()
    )


# --- 5(m) raw account ids never persist or log ---------------------------------------------------


def test_5m_no_raw_account_id_reaches_the_journal_or_the_log(rig: _Rig) -> None:
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(level=logging.DEBUG)
    logger = logging.getLogger("chronos")
    logger.addHandler(handler)
    try:
        rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
        rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
        rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
        rig.app.execDetails(
            -1, _option_contract(), _execution(rig.order_ref, exec_id="x.2", account=_OTHER_ACCOUNT)
        )
        rig.status(_ORDER, "Filled", float("nan"), 0, 1.25, _PERM, _CLIENT)
        rig.consumer.drain()
    finally:
        logger.removeHandler(handler)
    with rig.harness.db.sessions() as session:
        rows = session.scalars(select(ApplicationEventRow)).all()
        persisted = json.dumps(
            [[row.message, row.correlation_id, row.event_data] for row in rows], default=str
        )
    logged = json.dumps([[r.getMessage(), vars(r)] for r in records], default=str)
    assert rows
    for raw in (PAPER_ACCOUNT, _OTHER_ACCOUNT):
        assert raw not in persisted
        assert raw not in logged


# --- the runtime wiring: Demo through the real lifespan, refusals ---------------------------------


def test_the_runtime_refuses_to_start_the_consumer_on_an_in_memory_database() -> None:
    started: list[object] = []

    class _Consumer:
        def start(self, *, is_writer: Any) -> None:
            started.append(is_writer)

        def offer(self, event: object) -> None:
            pass

    class _Source:
        sink: Any = None

        def set_stream_sink(self, sink: Any) -> None:
            self.sink = sink

    runtime = runtime_module.AppRuntime.__new__(runtime_module.AppRuntime)
    runtime.database = Database("sqlite:///:memory:")
    source = _Source()
    runtime.callback_consumer = _Consumer()  # type: ignore[assignment]
    runtime.callback_source = source
    assert runtime.start_callback_consumer(is_writer=lambda: True) is False
    assert started == [] and source.sink is None


def test_f2_a_callback_arriving_while_the_runtime_stops_the_consumer_is_never_silent(
    rig: _Rig,
) -> None:
    """CB1-FIX1 F2 (Fable probe P1): an own callback landing between the runtime's two shutdown
    steps must drop loudly, never vanish into a detached bridge."""

    class _CallbackLandsDuringStop:
        def __init__(self, real: CallbackConsumer) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        def stop(self, **kwargs: Any) -> int:
            lost = self._real.stop(**kwargs)
            rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)  # the window
            return lost

    runtime = runtime_module.AppRuntime.__new__(runtime_module.AppRuntime)
    runtime.callback_source = rig.bridge
    runtime.callback_consumer = _CallbackLandsDuringStop(rig.consumer)  # type: ignore[assignment]
    rig.invalidations.clear()
    runtime._stop_callback_consumer()

    assert rig.invalidations == [DROPPED_EVENT_REASON]
    assert rig.consumer.health().dropped == 1


def test_a_demo_writer_lifespan_starts_the_consumer_and_journals_the_demo_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from chronos.api.main import create_app
    from chronos.config.settings import get_settings

    monkeypatch.chdir(_ROOT)
    monkeypatch.setenv("BROKER_MODE", "demo")
    monkeypatch.setenv("DEMO_PROFILE", "empty_account")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'chronos.db'}")
    monkeypatch.setenv("LIVE_KILL_SWITCH_FILE", str(tmp_path / "live_kill_switch.json"))
    monkeypatch.setenv("SESSION_BASELINE_FILE", str(tmp_path / "session_drawdown.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "chronos.log"))
    monkeypatch.setenv("BACKEND_TOKEN_FILE", str(tmp_path / "backend_api_token"))
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app):
            runtime = app.state.backend.runtime
            assert runtime.callback_source is runtime.broker
            health = runtime.callback_consumer_health()
            assert health is not None and health.state == "running"
            runtime.broker.simulate_unsolicited_execution(
                BrokerExecution(
                    execution_id="0000e0a1.foreign.02",
                    account_id=runtime.order_management.account_id,
                    broker_order_id=0,
                    client_id=0,
                    contract=UnderlyingContract(con_id=265598, symbol="AAPL"),
                    side=OrderSide.BUY,
                    quantity=Decimal("100"),
                    price=Decimal("190.25"),
                    timestamp=FIXED_NOW,
                )
            )

            def journaled() -> bool:
                with runtime.database.sessions() as session:
                    return (
                        session.scalar(
                            select(ApplicationEventRow).where(
                                ApplicationEventRow.event_type == CALLBACK_OUTCOME
                            )
                        )
                        is not None
                    )

            assert _wait_until(journaled)
        assert runtime.callback_consumer_health().state == "stopped"
    finally:
        get_settings.cache_clear()
