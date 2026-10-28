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
    ExecutionWriteOutcome,
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
    # CB1-FIX5 P2-2: own at capture, so RC-1 did not demote; the consumer's mapping is the only one
    assert rig.invalidations == [UNPROVEN_CALLBACK_REASON]


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
    # CB1-FIX5 P2-2: own at capture, so RC-1 did not demote; the consumer's mapping is the only one
    assert rig.invalidations == [UNPROVEN_CALLBACK_REASON]


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

    def journaled(self) -> int:
        with self.database.sessions() as session:
            rows = session.scalars(
                select(ApplicationEventRow).where(
                    ApplicationEventRow.event_type == CALLBACK_JOURNAL
                )
            ).all()
            return len(rows)

    def health_rows(self) -> list[dict[str, Any]]:
        with self.database.sessions() as session:
            rows = session.scalars(
                select(ApplicationEventRow)
                .where(ApplicationEventRow.event_type == CALLBACK_CONSUMER_HEALTH)
                .order_by(ApplicationEventRow.id)
            ).all()
            return [dict(row.event_data) for row in rows]


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


# --- CB1-FIX1 F4: Fable's surviving mutants X01-X04, each pinned -------------------------------


def test_x01_a_commission_never_pairs_with_an_execution_closed_unproven(rig: _Rig) -> None:
    # same account, a foreign client id on the OFFICIAL path: the execution closes not own ...
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, client_id=_CLIENT + 1))
    rig.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    rig.consumer.drain()

    outcomes = _rows(rig.harness, CALLBACK_OUTCOME)
    assert [row["outcome"] for row in outcomes] == [
        CallbackOutcome.NOT_OWN_AT_CAPTURE.value,
        CallbackOutcome.UNKNOWN_EXECUTION.value,
    ]
    assert outcomes[1]["execution_outcome"] == CallbackOutcome.NOT_OWN_AT_CAPTURE.value
    assert _fills(rig.harness) == []  # ... and its commission never writes it as a fill


def test_x02_an_event_that_exhausts_its_retries_is_dropped_loudly(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumer_module, "_RETRY_BASE_SECONDS", 0.0)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    rig.consumer.journal.append_pending = broken  # type: ignore[method-assign]
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    event = rig.consumer._queue.get_nowait()
    rig.consumer._handle_with_retry(event)  # five failed attempts

    assert rig.consumer.health().dropped == 1
    assert rig.invalidations[-1] == DROPPED_EVENT_REASON
    assert rig.invalidations.count(CONSUMER_FAILED_REASON) == 1
    assert rig.consumer.health().queued == 0
    assert rig.intent_status() is OrderLifecycle.SUBMITTED


def test_x03_a_foreign_client_execution_on_the_official_path_is_not_own(rig: _Rig) -> None:
    rig.app.execDetails(-1, _option_contract(), _execution(rig.order_ref, client_id=_CLIENT + 1))
    rig.consumer.drain()
    assert _outcomes(rig.harness) == [CallbackOutcome.NOT_OWN_AT_CAPTURE.value]
    assert _fills(rig.harness) == []


def test_x04_start_closes_a_pending_row_without_waiting_for_the_cadence(tmp_path: Path) -> None:
    file_rig = _FileRig(tmp_path, commission_grace=timedelta(seconds=0.3))
    try:
        file_rig.consumer.open()
        file_rig.consumer.offer(_orphan_commission(3))
        file_rig.consumer.drain()  # PENDING: inside its grace
        file_rig.consumer.stop()
        time.sleep(0.5)  # past the grace, before any start: only the start sweep can close it now
        assert file_rig.outcomes() == []

        file_rig.consumer.start(is_writer=lambda: True)
        assert _wait_until(lambda: file_rig.outcomes() == ["unknown_execution"], timeout=3.0)
    finally:
        file_rig.consumer.stop()
        file_rig.database.dispose()


# --- CB1-FIX2 P1-1 (Kimi's security review): a stop never reports "no loss" over a live worker --


def _blocked_handle(consumer: CallbackConsumer) -> tuple[threading.Event, threading.Event]:
    """Hold the worker inside ``handle`` with one accepted event (Kimi's probe shape)."""

    entered, release = threading.Event(), threading.Event()
    real_handle = consumer.handle

    def blocked(event: StreamEvent) -> Any:
        entered.set()
        assert release.wait(10)
        return real_handle(event)

    consumer.handle = blocked  # type: ignore[method-assign]
    return entered, release


def test_fix2_p1_a_stop_that_cannot_join_counts_the_inflight_event_lost_and_demotes(
    tmp_path: Path,
) -> None:
    """Kimi's ``test_stop_reports_an_inflight_event_as_neither_lost_nor_invalidated``, inverted."""

    file_rig = _FileRig(tmp_path)
    entered, release = _blocked_handle(file_rig.consumer)
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(1))
        assert entered.wait(2)
        thread = file_rig.consumer._thread
        assert thread is not None

        assert file_rig.consumer.stop(timeout=0.05) == 1
        assert thread.is_alive()
        assert file_rig.consumer.health().dropped == 1
        assert file_rig.invalidations == [DROPPED_EVENT_REASON]
        assert [row["state"] for row in file_rig.health_rows()] == ["stopped"]

        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert file_rig.journaled() == 0  # the abandoned worker writes nothing after the stop
    finally:
        release.set()
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix2_p1_a_stop_that_cannot_join_an_idle_worker_still_demotes_and_records(
    tmp_path: Path,
) -> None:
    file_rig = _FileRig(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_sweep = file_rig.consumer.sweep

    def blocked() -> int:
        entered.set()
        assert release.wait(10)
        return real_sweep()

    file_rig.consumer.sweep = blocked  # type: ignore[method-assign]
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        assert entered.wait(2)  # the start sweep holds the worker; no event is in hand
        thread = file_rig.consumer._thread
        assert thread is not None

        assert file_rig.consumer.stop(timeout=0.05) == 0
        assert thread.is_alive()
        assert file_rig.invalidations == [CONSUMER_FAILED_REASON]  # never green over a live worker
        assert [row["state"] for row in file_rig.health_rows()] == ["stopped"]
    finally:
        release.set()
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix2_p1_an_event_taken_after_the_stop_gave_up_is_counted_and_never_journaled(
    tmp_path: Path,
) -> None:
    file_rig = _FileRig(tmp_path)
    taken, release = threading.Event(), threading.Event()
    events = file_rig.consumer._queue
    real_get = events.get

    def slow_get(*args: Any, **kwargs: Any) -> Any:
        item = real_get(*args, **kwargs)
        if isinstance(item, StreamEvent):
            taken.set()
            assert release.wait(10)
        return item

    events.get = slow_get  # type: ignore[method-assign]
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(1))
        assert taken.wait(2)  # out of the queue, not yet in hand: stop() cannot see it
        thread = file_rig.consumer._thread
        assert thread is not None

        file_rig.consumer.stop(timeout=0.05)
        assert file_rig.invalidations == [CONSUMER_FAILED_REASON]

        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert file_rig.consumer.health().dropped == 1  # the worker counts it itself
        assert file_rig.journaled() == 0
    finally:
        release.set()
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix2_p1_runtime_close_never_disposes_under_a_live_worker_without_the_signal(
    tmp_path: Path,
) -> None:
    """``AppRuntime.close`` stops the consumer before it disposes the database: the loss is
    demoted and recorded first, and the worker it could not join writes nothing afterwards."""

    class _Closeable:
        def close(self) -> None:
            pass

        def clear_cache(self) -> None:
            pass

        def set_stream_sink(self, sink: Any) -> None:
            pass

    file_rig = _FileRig(tmp_path)
    entered, release = _blocked_handle(file_rig.consumer)
    runtime = runtime_module.AppRuntime.__new__(runtime_module.AppRuntime)
    runtime.database = file_rig.database
    runtime.connection = _Closeable()  # type: ignore[assignment]
    runtime.market_data = _Closeable()  # type: ignore[assignment]
    runtime.callback_source = _Closeable()
    runtime.callback_consumer = file_rig.consumer
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(1))
        assert entered.wait(2)
        thread = file_rig.consumer._thread
        assert thread is not None

        runtime.close()
        assert file_rig.invalidations == [DROPPED_EVENT_REASON]
        assert [row["state"] for row in file_rig.health_rows()] == ["stopped"]

        release.set()
        thread.join(2)
        assert file_rig.journaled() == 0
    finally:
        release.set()
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


# --- CB1-FIX2 P2-1: the writer lease bounds every database write, not only the next loop -------


def _lease(consumer: CallbackConsumer) -> list[bool]:
    """The lease the consumer re-checks, as a switch a test flips mid-event (synchronous mode)."""

    held = [True]
    consumer._is_writer = lambda: held[0]
    return held


def _ended_with(call: Any) -> str:
    """The type name of what ``call`` raised ('' when it returned)."""

    try:
        call()
    except BaseException as error:
        return type(error).__name__
    return ""


def _lose_after(held: list[bool], real: Any, *, when: Any = None) -> Any:
    """Wrap a write so the lease is lost right after it commits (``when`` picks the call)."""

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if when is None or when(*args):
            held[0] = False
        return result

    return wrapped


def test_fix2_p2_a_lease_lost_during_the_journal_append_writes_no_outcome_and_halts(
    tmp_path: Path,
) -> None:
    """Kimi's ``test_lease_loss_during_an_event_does_not_prevent_its_journal_and_outcome``,
    inverted. The append whose check passed before the loss still commits (residual R10)."""

    file_rig = _FileRig(tmp_path)
    writer = [True]
    witnesses: list[bool] = []
    journal = file_rig.consumer.journal
    real_pending, real_outcome = journal.append_pending, journal.append_outcome

    def lose_lease(*args: Any, **kwargs: Any) -> Any:
        writer[0] = False
        return real_pending(*args, **kwargs)

    def witness(*args: Any, **kwargs: Any) -> Any:
        witnesses.append(writer[0])
        return real_outcome(*args, **kwargs)

    journal.append_pending = lose_lease  # type: ignore[method-assign]
    journal.append_outcome = witness  # type: ignore[method-assign]
    try:
        file_rig.consumer.start(is_writer=lambda: writer[0])
        file_rig.consumer.offer(file_rig.foreign(1))
        assert _wait_until(lambda: file_rig.consumer.health().state == "halted")
        assert witnesses == []  # the outcome was never attempted without the lease
        assert file_rig.outcomes() == []
        assert file_rig.journaled() == 1  # PENDING, for the next writer's sweep
        assert CONSUMER_FAILED_REASON in file_rig.invalidations
    finally:
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix2_p2_without_the_lease_an_event_is_never_journaled(rig: _Rig) -> None:
    held = _lease(rig.consumer)
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    held[0] = False
    assert _ended_with(rig.consumer.drain) == "_WriteAuthorityLost"
    assert _rows(rig.harness, CALLBACK_JOURNAL) == []
    assert rig.intent_status() is OrderLifecycle.SUBMITTED


def test_fix2_p2_a_lease_lost_after_the_journal_applies_no_lifecycle_and_stays_pending(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _lease(rig.consumer)
    journal = rig.consumer.journal
    journal.append_pending = _lose_after(held, journal.append_pending)  # type: ignore[method-assign]
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert _ended_with(rig.consumer.drain) == "_WriteAuthorityLost"
    assert rig.intent_status() is OrderLifecycle.SUBMITTED  # no ingest without the lease
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 1 and _outcomes(rig.harness) == []

    assert _restart(rig, monkeypatch).consumer.sweep() == 1  # the next writer re-drives it
    assert rig.intent_status() is OrderLifecycle.FILLED


def test_fix2_p2_a_lease_lost_inside_ingest_writes_no_outcome(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _Tracker(rig.harness.tracker)
    restarted = _restart(rig, monkeypatch, tracker=tracker)
    held = _lease(restarted.consumer)
    tracker.ingest = _lose_after(held, tracker.ingest)  # type: ignore[method-assign]
    restarted.bridge.note_own_order(_ORDER)
    restarted.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert _ended_with(restarted.consumer.drain) == "_WriteAuthorityLost"
    assert rig.intent_status() is OrderLifecycle.FILLED  # the committed effect stands (R10) ...
    assert _outcomes(rig.harness) == []  # ... and the event stays PENDING: no close


def test_fix2_p2_a_lease_lost_after_the_journal_records_no_fill(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[BrokerExecution] = []
    writer = runtime_module._callback_execution_writer(ExecutionRepository(rig.harness.db.sessions))

    def record(execution: BrokerExecution) -> Any:
        records.append(execution)
        return writer(execution)

    restarted = _restart(rig, monkeypatch, record=record)
    held = _lease(restarted.consumer)
    journal = restarted.consumer.journal
    journal.append_pending = _lose_after(  # type: ignore[method-assign]
        held, journal.append_pending, when=lambda *args: args[1] is StreamKind.EXECUTION
    )
    restarted.bridge.note_own_order(_ORDER)
    restarted.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    restarted.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    assert _ended_with(restarted.consumer.drain) == "_WriteAuthorityLost"
    assert records == [] and _fills(rig.harness) == []
    assert _outcomes(rig.harness) == []


def test_fix2_p2_a_lease_lost_inside_the_fills_write_never_pairs_the_commission(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[bool] = [True]
    writer = runtime_module._callback_execution_writer(ExecutionRepository(rig.harness.db.sessions))
    restarted = _restart(rig, monkeypatch, record=_lose_after(held, writer))
    restarted.consumer._is_writer = lambda: held[0]
    restarted.bridge.note_own_order(_ORDER)
    restarted.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
    restarted.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    assert _ended_with(restarted.consumer.drain) == "_WriteAuthorityLost"
    assert _fills(rig.harness) == [(_EXEC_ID, Decimal("1.25"), Decimal("1.05"))]  # R10
    assert _outcomes(rig.harness) == []  # neither the PAIRED commission nor the execution closes


def test_fix2_p2_a_lease_lost_before_a_late_commission_never_calls_the_fills_writer(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[BrokerExecution] = []
    writer = runtime_module._callback_execution_writer(ExecutionRepository(rig.harness.db.sessions))

    def record(execution: BrokerExecution) -> Any:
        records.append(execution)
        return writer(execution)

    restarted = _restart(rig, monkeypatch, record=record)
    restarted.bridge.note_own_order(_ORDER)
    restarted.app.execDetails(-1, _option_contract(), _execution(rig.order_ref))
    restarted.consumer.drain()
    rig.clock.advance(31)  # past its commission grace: recorded commission-less
    restarted.consumer.sweep()
    assert len(records) == 1

    held = _lease(restarted.consumer)
    journal = restarted.consumer.journal
    journal.append_pending = _lose_after(held, journal.append_pending)  # type: ignore[method-assign]
    restarted.app.commissionReport(FakeCommissionReport(_EXEC_ID, 0.95, "USD"))
    assert _ended_with(restarted.consumer.drain) == "_WriteAuthorityLost"
    assert len(records) == 1  # the late commission never reached the writer
    assert _outcomes(rig.harness) == [CallbackOutcome.APPLIED.value]


def test_fix2_p2_a_lease_lost_on_a_divergent_replay_writes_no_conflict_row(rig: _Rig) -> None:
    rig.status(_ORDER, "Submitted", 0, 1, 0.0, _PERM, _CLIENT)
    rig.consumer.drain()
    held = _lease(rig.consumer)
    journal = rig.consumer.journal
    journal.append_pending = _lose_after(held, journal.append_pending)  # type: ignore[method-assign]
    rig.status(_ORDER, "Submitted", 0, 5, 0.0, _PERM, _CLIENT)  # same identity, other facts
    assert _ended_with(rig.consumer.drain) == "_WriteAuthorityLost"
    assert _rows(rig.harness, CALLBACK_CONFLICT) == []


def test_fix2_p2_a_lease_lost_on_a_refused_payload_writes_no_digest_row(rig: _Rig) -> None:
    held = _lease(rig.consumer)

    def refused(*args: Any, **kwargs: Any) -> Any:
        held[0] = False
        raise ValueError("raw account id")  # the raw-account guard's refusal

    rig.consumer.journal.append_pending = refused  # type: ignore[method-assign]
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert _ended_with(rig.consumer.drain) == "_WriteAuthorityLost"
    assert _rows(rig.harness, CALLBACK_JOURNAL) == []


def test_fix2_p2_a_lease_lost_after_a_refused_digest_row_writes_no_outcome(rig: _Rig) -> None:
    held = _lease(rig.consumer)
    journal = rig.consumer.journal
    real = journal.append_pending
    calls: list[str] = []

    def first_refused(*args: Any, **kwargs: Any) -> Any:
        calls.append("pending")
        if len(calls) == 1:
            raise ValueError("raw account id")
        result = real(*args, **kwargs)
        held[0] = False
        return result

    journal.append_pending = first_refused  # type: ignore[method-assign]
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    assert _ended_with(rig.consumer.drain) == "_WriteAuthorityLost"
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 1  # the digest row (R10) ...
    assert _outcomes(rig.harness) == []  # ... and no refused outcome without the lease


def test_fix2_p2_a_lease_lost_mid_sweep_closes_no_further_row(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*args: Any, **kwargs: Any) -> Any:
        raise _Crash

    rig.consumer.journal.append_outcome = crash  # type: ignore[method-assign]
    for order_id in (_ORDER + 1, _ORDER + 2):  # not own: no effect, an outcome row only
        rig.status(order_id, "Submitted", 0, 1, 0.0, _PERM + order_id, _CLIENT)
        with pytest.raises(_Crash):
            rig.consumer.handle(rig.consumer._queue.get_nowait())
    assert len(_rows(rig.harness, CALLBACK_JOURNAL)) == 2 and _outcomes(rig.harness) == []

    restarted = _restart(rig, monkeypatch)
    held = _lease(restarted.consumer)
    journal = restarted.consumer.journal
    journal.append_outcome = _lose_after(held, journal.append_outcome)  # type: ignore[method-assign]
    assert _ended_with(restarted.consumer.sweep) == "_WriteAuthorityLost"
    assert _outcomes(rig.harness) == [CallbackOutcome.NOT_OWN_AT_CAPTURE.value]  # one row, not two


def test_fix2_p2_a_stop_without_the_lease_demotes_but_writes_no_health_row(rig: _Rig) -> None:
    held = _lease(rig.consumer)
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)  # queued, never drained
    held[0] = False
    assert rig.consumer.stop() == 1
    assert rig.invalidations[-1] == DROPPED_EVENT_REASON
    assert _rows(rig.harness, CALLBACK_CONSUMER_HEALTH) == []


def test_fix2_p3_the_module_docstring_names_the_sweeps_the_consumer_runs() -> None:
    doc = consumer_module.__doc__ or ""
    assert "idle tick" not in doc  # CB1-FIX1 F5 retired the idle-tick sweep
    for trigger in ("every start", "failure streak", "``_SWEEP_SECONDS``"):
        assert trigger in doc


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


@pytest.mark.in_memory_sqlite_is_the_subject  # CB1-FIX5 P2-1: the lane would make it file-backed
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


# --- CB1-FIX3: no accepted event ends neither journaled nor counted (DESIGN §11) ---------------


class _OwnedLock:
    """A ``threading.Lock`` that records its holder, so a pin can see WHO holds the consumer's lock
    and whether ``stop()`` is waiting on a lock the offering thread holds (no sleeps, no timing)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner: threading.Thread | None = None
        self.offer_thread: threading.Thread | None = None
        self.stop_thread: threading.Thread | None = None
        self.progress = threading.Event()  # stop is waiting on the offer's lock, or stop returned

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            self.owner = threading.current_thread()
            return True
        if (
            threading.current_thread() is self.stop_thread
            and self.owner is not None
            and self.owner is self.offer_thread
        ):
            self.progress.set()
        if not self._lock.acquire(blocking, timeout):
            return False
        self.owner = threading.current_thread()
        return True

    def release(self) -> None:
        self.owner = None
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def _held_put(consumer: CallbackConsumer) -> tuple[threading.Event, threading.Event, list[Any]]:
    """Hold an offer inside ``put_nowait`` (Kimi's probe seam); record who held the lock there."""

    entered, release = threading.Event(), threading.Event()
    holders: list[Any] = []
    real_put = consumer._queue.put_nowait

    def held(item: object) -> None:
        holders.append(getattr(consumer._lock, "owner", None))
        entered.set()
        assert release.wait(10)
        real_put(item)

    consumer._queue.put_nowait = held  # type: ignore[method-assign]
    return entered, release, holders


def test_fix3_p1_the_accepting_check_and_the_put_are_one_critical_section(
    tmp_path: Path,
) -> None:
    """P1-1: the put happens under the lock stop() takes to flip ``_accepting``."""

    file_rig = _FileRig(tmp_path)
    lock = _OwnedLock()
    file_rig.consumer._lock = lock  # type: ignore[assignment]
    entered, release, holders = _held_put(file_rig.consumer)
    offered = threading.Thread(target=lambda: file_rig.consumer.offer(file_rig.foreign(1)))
    lock.offer_thread = offered
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        offered.start()
        assert entered.wait(2)
        assert holders == [offered]  # inside put_nowait, the offering thread holds the lock
    finally:
        release.set()
        offered.join(2)
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


@pytest.mark.parametrize("mode", ["worker", "synchronous"])
def test_fix3_p1_kimi_schedule_stop_cannot_complete_inside_the_offer(
    tmp_path: Path, mode: str
) -> None:
    """Kimi's probe (out/CB1/kimi-probes/test_stop_offer_race.py), ``stop()`` on its own thread.

    The offer is held after its accepting check and before its put; ``stop()`` runs; the offer is
    released once stop has returned (the f0736a3 schedule: the put lands after the final drain) or
    is waiting on the lock the offer holds. Either way the accepted event must end journaled, or
    counted lost with one DROPPED demotion; never queued behind no worker. ``synchronous`` is the
    ``open()`` mode with no worker: there stop's flip is the only lock between put and drain.
    """

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    lock = _OwnedLock()
    consumer._lock = lock  # type: ignore[assignment]
    entered, release, _ = _held_put(consumer)
    offered = threading.Thread(target=lambda: consumer.offer(file_rig.foreign(1)))
    result: dict[str, int] = {}

    def stopping() -> None:
        result["lost"] = consumer.stop(timeout=2)
        lock.progress.set()

    stopper = threading.Thread(target=stopping)
    lock.offer_thread, lock.stop_thread = offered, stopper
    try:
        if mode == "worker":
            consumer.start(is_writer=lambda: True)
        else:
            consumer.open()
        offered.start()
        assert entered.wait(2)  # accepted, not yet queued
        stopper.start()
        assert lock.progress.wait(5)
        release.set()
        offered.join(2)
        stopper.join(5)
        assert not offered.is_alive() and not stopper.is_alive()
        queued = consumer._queue.qsize()
        journaled = file_rig.journaled()
        observed = (result["lost"], consumer.health().dropped, list(file_rig.invalidations))
        assert queued == 0, f"an accepted event is stranded behind no worker: {observed}"
        if journaled:
            assert observed == (0, 0, [])
        else:
            assert observed == (1, 1, [DROPPED_EVENT_REASON])
    finally:
        release.set()
        offered.join(2)
        stopper.join(5)
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix3_a_lease_lost_with_events_queued_counts_every_one(tmp_path: Path) -> None:
    """I2 + I9: the event in hand when the lease goes and the one queued behind it are counted."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    entered, release = _blocked_handle(consumer)
    writer = [True]
    try:
        consumer.start(is_writer=lambda: writer[0])
        consumer.offer(file_rig.foreign(1))
        assert entered.wait(2)  # event 1 in hand, before its journal append
        consumer.offer(file_rig.foreign(2))  # event 2 queued behind it
        writer[0] = False
        release.set()  # handle re-checks the lease before its first write and halts
        assert _wait_until(lambda: consumer.health().state == "halted")
        assert consumer.health().dropped == 1  # event 1: in hand, never journaled
        assert consumer.health().queued == 1
        assert consumer.stop(timeout=1) == 1  # event 2: drained by the final accountant
        assert consumer.health().dropped == 2
        assert file_rig.journaled() == 0
        assert file_rig.invalidations[0] == CONSUMER_FAILED_REASON
        assert file_rig.invalidations[-1] == DROPPED_EVENT_REASON
    finally:
        release.set()
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix3_a_worker_that_dies_holding_an_event_counts_it_lost(tmp_path: Path) -> None:
    """I10: the thread dies inside ``handle``; the event it held is counted, not only demoted."""

    file_rig = _FileRig(tmp_path)

    def die(event: StreamEvent) -> Any:
        raise _Crash

    file_rig.consumer.handle = die  # type: ignore[method-assign]
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(3))
        assert _wait_until(lambda: file_rig.consumer.health().state == "halted")
        assert file_rig.consumer.health().dropped == 1
        assert CONSUMER_FAILED_REASON in file_rig.invalidations
        assert file_rig.consumer.stop(timeout=1) == 0  # nothing queued; counted once, not twice
        assert file_rig.consumer.health().dropped == 1
    finally:
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix3_an_abandoned_workers_event_is_counted_once_by_its_stop(tmp_path: Path) -> None:
    """P1-1b never double counts: the stop counted the held event; the worker, released afterwards,
    exits on its revoked token without counting it again."""

    file_rig = _FileRig(tmp_path)
    entered, release = _blocked_handle(file_rig.consumer)
    try:
        file_rig.consumer.start(is_writer=lambda: True)
        file_rig.consumer.offer(file_rig.foreign(1))
        assert entered.wait(2)
        thread = file_rig.consumer._thread
        assert thread is not None
        assert file_rig.consumer.stop(timeout=0.05) == 1
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert file_rig.consumer.health().dropped == 1
    finally:
        release.set()
        file_rig.consumer.stop(timeout=1)
        file_rig.database.dispose()


# --- CB1-FIX4 (Kimi HOLD P2=2 @ 9b1ad17): one accounting owner per event, one write bound -----


def _stopping(
    consumer: CallbackConsumer, results: list[int], *, timeout: float
) -> threading.Thread:
    """``stop()`` on its own thread (the lifecycle actor), its return appended to ``results``."""

    thread = threading.Thread(target=lambda: results.append(consumer.stop(timeout=timeout)))
    thread.start()
    return thread


def test_fix4_p2_1_concurrent_stops_count_the_inflight_event_once(tmp_path: Path) -> None:
    """I13, Kimi's ``test_concurrent_stops_count_one_inflight_event_once`` inverted: two stops over
    one held event count it once, and only one of them demotes and records the stop."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    entered, release = _blocked_handle(consumer)
    results: list[int] = []
    stoppers: list[threading.Thread] = []
    try:
        consumer.start(is_writer=lambda: True)
        consumer.offer(file_rig.foreign(1))
        assert entered.wait(2)
        worker = consumer._thread
        assert worker is not None
        stoppers = [_stopping(consumer, results, timeout=0.05) for _ in range(2)]
        for thread in stoppers:
            thread.join(2)
            assert not thread.is_alive()
        assert worker.is_alive()
        assert sorted(results) == [0, 1]
        assert consumer.health().dropped == 1
        assert file_rig.invalidations == [DROPPED_EVENT_REASON]
        assert [row["state"] for row in file_rig.health_rows()] == ["stopped"]
    finally:
        release.set()
        for thread in stoppers:
            thread.join(2)
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix4_p2_1_a_retry_exhaustion_drop_and_an_abandoning_stop_count_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I16, Kimi's ``test_retry_drop_and_abandoning_stop_count_event_once`` inverted: the worker's
    terminal drop already counted the event it still holds, so the abandoning stop counts 0."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    monkeypatch.setattr(consumer_module, "_RETRY_BASE_SECONDS", 0)
    dropped, release = threading.Event(), threading.Event()
    real_drop = consumer._drop

    def die(event: StreamEvent) -> Any:
        raise RuntimeError("synthetic failure")

    def blocked_drop(event: object, why: str) -> None:
        real_drop(event, why)
        dropped.set()
        assert release.wait(10)

    consumer.handle = die  # type: ignore[method-assign]
    consumer._drop = blocked_drop  # type: ignore[method-assign]
    try:
        consumer.start(is_writer=lambda: True)
        consumer.offer(file_rig.foreign(1))
        worker = consumer._thread
        assert worker is not None
        assert dropped.wait(5)  # counted by the worker, still held (_inflight is cleared later)
        assert (consumer.stop(timeout=0.05), consumer.health().dropped) == (0, 1)
        assert worker.is_alive()
    finally:
        release.set()
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix4_p2_1_an_exceptional_exit_and_an_abandoning_stop_count_it_once(tmp_path: Path) -> None:
    """I19, the sibling of I16 found while listing: the worker counted its held event on a lost
    lease and has not yet released it when a stop abandons it; the stop must not count it again."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    counted, release = threading.Event(), threading.Event()
    real_count = consumer._count_held_lost

    def held_count() -> None:
        real_count()
        counted.set()
        assert release.wait(10)

    def lease_lost(event: StreamEvent) -> Any:
        raise consumer_module._WriteAuthorityLost(abandoned=False)

    consumer.handle = lease_lost  # type: ignore[method-assign]
    consumer._count_held_lost = held_count  # type: ignore[method-assign]
    try:
        consumer.start(is_writer=lambda: True)
        consumer.offer(file_rig.foreign(1))
        worker = consumer._thread
        assert worker is not None
        assert counted.wait(5)
        assert (consumer.stop(timeout=0.05), consumer.health().dropped) == (0, 1)
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert consumer.health().dropped == 1
    finally:
        release.set()
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def _row_count(file_rig: _FileRig, *event_types: str) -> int:
    with file_rig.database.sessions() as session:
        return len(
            session.scalars(
                select(ApplicationEventRow).where(ApplicationEventRow.event_type.in_(event_types))
            ).all()
        )


def test_fix4_p2_2_a_stop_health_write_waits_for_the_workers_write_and_rechecks_the_lease(
    tmp_path: Path,
) -> None:
    """I18, Kimi's ``test_r10_bounds_worker_and_stop_health_writes_to_one_after_lease_loss``.

    Her schedule (the worker past its journal check, the stop past its health check, the lease lost,
    both released) cannot be reached any more: the stop's health write waits for the worker's
    in-flight write and re-checks the lease after it. So one write commits after the loss, not two.
    """

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    writer = [True]
    worker_entered, release_worker = threading.Event(), threading.Event()
    health_entered = threading.Event()
    real_pending = consumer.journal.append_pending
    real_health = consumer.journal.append_health

    def held_pending(*args: Any, **kwargs: Any) -> Any:
        worker_entered.set()
        assert release_worker.wait(10)
        return real_pending(*args, **kwargs)

    def seen_health(*args: Any, **kwargs: Any) -> Any:
        health_entered.set()
        return real_health(*args, **kwargs)

    consumer.journal.append_pending = held_pending  # type: ignore[method-assign]
    consumer.journal.append_health = seen_health  # type: ignore[method-assign]
    results: list[int] = []
    stopper: threading.Thread | None = None
    try:
        consumer.start(is_writer=lambda: writer[0])
        consumer.offer(file_rig.foreign(1))
        assert worker_entered.wait(2)  # past its lease check, inside its journal write
        worker = consumer._thread
        assert worker is not None
        stopper = _stopping(consumer, results, timeout=1)
        assert _wait_until(lambda: consumer._token is None)  # the stop abandoned the worker
        assert not health_entered.wait(0.3)  # and its health write waits for the in-flight one
        writer[0] = False
        release_worker.set()
        stopper.join(5)
        assert not stopper.is_alive()
        worker.join(5)
        assert not worker.is_alive()
        assert results == [1]
        assert not health_entered.is_set()
        written = (CALLBACK_JOURNAL, CALLBACK_OUTCOME, CALLBACK_CONSUMER_HEALTH)
        assert _row_count(file_rig, *written) == 1  # the in-flight journal row, nothing after it
        assert file_rig.invalidations == [DROPPED_EVENT_REASON]
    finally:
        release_worker.set()
        if stopper is not None:
            stopper.join(2)
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix4_p2_2_a_stop_over_a_sweep_write_records_no_health_row_after_the_lease_is_lost(
    tmp_path: Path,
) -> None:
    """I17: the worker is inside a sweep's close write (no accepted event in hand) when a stop
    abandons it and the lease goes: the sweep row is the one write, the stop records nothing."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    event = file_rig.foreign(1)
    normalized = consumer._normalize(event)
    consumer.journal.append_pending(
        consumer_module.event_key(normalized.identity),
        normalized.kind,
        normalized.facts,
        received_at=event.received_at,
        own_at_capture=event.own_at_capture,
    )  # one PENDING row for the start sweep to close
    writer = [True]
    worker_entered, release_worker = threading.Event(), threading.Event()
    health_entered = threading.Event()
    real_outcome = consumer.journal.append_outcome
    real_health = consumer.journal.append_health

    def held_outcome(*args: Any, **kwargs: Any) -> Any:
        worker_entered.set()
        assert release_worker.wait(10)
        return real_outcome(*args, **kwargs)

    def seen_health(*args: Any, **kwargs: Any) -> Any:
        health_entered.set()
        return real_health(*args, **kwargs)

    consumer.journal.append_outcome = held_outcome  # type: ignore[method-assign]
    consumer.journal.append_health = seen_health  # type: ignore[method-assign]
    results: list[int] = []
    stopper: threading.Thread | None = None
    try:
        consumer.start(is_writer=lambda: writer[0])
        assert worker_entered.wait(2)  # the start sweep, past its check, inside the close write
        worker = consumer._thread
        assert worker is not None
        stopper = _stopping(consumer, results, timeout=1)
        assert _wait_until(lambda: consumer._token is None)
        assert not health_entered.wait(0.3)
        writer[0] = False
        release_worker.set()
        stopper.join(5)
        assert not stopper.is_alive()
        worker.join(5)
        assert results == [0]
        assert consumer.health().dropped == 0  # a sweep row is journaled, not an accepted event
        assert not health_entered.is_set()
        assert _row_count(file_rig, CALLBACK_OUTCOME, CALLBACK_CONSUMER_HEALTH) == 1
        assert file_rig.invalidations == [CONSUMER_FAILED_REASON]
    finally:
        release_worker.set()
        if stopper is not None:
            stopper.join(2)
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix4_p2_2_with_the_lease_kept_the_stop_records_its_row_after_the_in_flight_write(
    tmp_path: Path,
) -> None:
    """I18's positive control: the stop's health write is serialized behind the worker's write, not
    skipped. With the lease kept it lands once the in-flight write commits, so I18's "no health row
    after a lost lease" comes from the lease re-check, not from a stop that never records."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    worker_entered, release_worker = threading.Event(), threading.Event()
    health_entered = threading.Event()
    real_pending = consumer.journal.append_pending
    real_health = consumer.journal.append_health

    def held_pending(*args: Any, **kwargs: Any) -> Any:
        worker_entered.set()
        assert release_worker.wait(10)
        return real_pending(*args, **kwargs)

    def seen_health(*args: Any, **kwargs: Any) -> Any:
        health_entered.set()
        return real_health(*args, **kwargs)

    consumer.journal.append_pending = held_pending  # type: ignore[method-assign]
    consumer.journal.append_health = seen_health  # type: ignore[method-assign]
    results: list[int] = []
    stopper: threading.Thread | None = None
    try:
        consumer.start(is_writer=lambda: True)
        consumer.offer(file_rig.foreign(1))
        assert worker_entered.wait(2)
        worker = consumer._thread
        assert worker is not None
        stopper = _stopping(consumer, results, timeout=1)
        assert _wait_until(lambda: consumer._token is None)
        assert not health_entered.wait(0.3)  # serialized behind the in-flight journal write
        release_worker.set()
        stopper.join(5)
        assert not stopper.is_alive()
        worker.join(5)
        assert results == [1]
        assert health_entered.is_set()
        assert file_rig.journaled() == 1  # the in-flight write; the abandoned worker adds nothing
        assert _row_count(file_rig, CALLBACK_OUTCOME) == 0
        assert [row["state"] for row in file_rig.health_rows()] == ["stopped"]
    finally:
        release_worker.set()
        if stopper is not None:
            stopper.join(2)
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix4_p2_1_the_next_held_event_is_counted_after_a_terminal_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counted flag belongs to ONE held event. Event 1 is dropped after its retries (the worker
    counts it); event 2 is then held when a stop abandons the worker, and must still be counted.
    A flag that outlived event 1 would make the stop skip event 2: an accepted event never counted.
    """

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    monkeypatch.setattr(consumer_module, "_RETRY_BASE_SECONDS", 0)
    entered, release = threading.Event(), threading.Event()
    first, second = file_rig.foreign(1), file_rig.foreign(2)

    def handle(event: StreamEvent) -> Any:
        if event is first:
            raise RuntimeError("synthetic failure")
        entered.set()
        assert release.wait(10)
        return None

    consumer.handle = handle  # type: ignore[method-assign]
    try:
        consumer.start(is_writer=lambda: True)
        consumer.offer(first)
        consumer.offer(second)
        assert entered.wait(5)  # event 1 dropped by the worker, event 2 in hand
        assert consumer.health().dropped == 1
        assert consumer.stop(timeout=0.05) == 1
        assert consumer.health().dropped == 2
    finally:
        release.set()
        consumer.stop(timeout=1)
        file_rig.database.dispose()


def test_fix4_an_event_taken_after_the_stop_gave_up_is_never_handled(tmp_path: Path) -> None:
    """I8, made explicit by FIX4: the worker counts an event it took after the revoke and does not
    start handling it. Since FIX4 the claim-based exit count would also count it, so the count alone
    no longer shows that the post-get token check runs; this pins that it does (M48)."""

    file_rig = _FileRig(tmp_path)
    consumer = file_rig.consumer
    taken, release = threading.Event(), threading.Event()
    events = consumer._queue
    real_get = events.get
    normalized: list[StreamEvent] = []
    real_normalize = consumer._normalize

    def slow_get(*args: Any, **kwargs: Any) -> Any:
        item = real_get(*args, **kwargs)
        if isinstance(item, StreamEvent):
            taken.set()
            assert release.wait(10)
        return item

    def spy_normalize(event: StreamEvent) -> Any:
        normalized.append(event)
        return real_normalize(event)

    events.get = slow_get  # type: ignore[method-assign]
    consumer._normalize = spy_normalize  # type: ignore[method-assign]
    try:
        consumer.start(is_writer=lambda: True)
        consumer.offer(file_rig.foreign(1))
        assert taken.wait(2)
        thread = consumer._thread
        assert thread is not None
        consumer.stop(timeout=0.05)
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert consumer.health().dropped == 1
        assert normalized == []  # never handled: counted at the take, under the revoked token
    finally:
        release.set()
        consumer.stop(timeout=1)
        file_rig.database.dispose()


# --- CB1-FIX5 (reader's independent read, HOLD P2=2 @ f6bd853) ------------------------------------
# P2-2: every _INVALIDATING member and the unsafe-payload path demote. An OWN event is never
# demoted by RC-1 at capture, so for each case below the consumer's mapping is the ONLY demotion:
# the exact list is the pin (mutants I4, I6, I7, I10, I11, R8).


def test_fix5_p2_2_an_identity_conflict_on_the_intent_is_typed_and_demotes(rig: _Rig) -> None:
    # two DISTINCT permIds persisted for intent-1: the tracker fails closed, never latest or first
    assert rig.harness.tracker_repo.record_transition(
        intent_id="intent-1",
        event_key="intent-1:test:CANCEL_PENDING",
        source="TEST",
        from_status=OrderLifecycle.SUBMITTED,
        to_status=OrderLifecycle.CANCEL_PENDING,
        current_account_id=PAPER_ACCOUNT,
        broker_order_id=_ORDER,
        permanent_id=_PERM + 7,
        client_id=_CLIENT,
        occurred_at=FIXED_NOW,
        enforce_from_status=True,
    )
    rig.status(_ORDER, "Filled", 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()

    outcome = _rows(rig.harness, CALLBACK_OUTCOME)
    assert [row["outcome"] for row in outcome] == [CallbackOutcome.IDENTITY_CONFLICT.value]
    assert outcome[0]["field"] == "permanent_id"
    assert rig.intent_status() is OrderLifecycle.CANCEL_PENDING
    assert rig.invalidations == [UNPROVEN_CALLBACK_REASON]


@pytest.mark.parametrize(
    ("written", "outcome"),
    [
        (ExecutionWriteOutcome.CONFLICT, CallbackOutcome.EXECUTION_CONFLICT),
        (ExecutionWriteOutcome.REFUSED, CallbackOutcome.EXECUTION_REFUSED),
    ],
)
def test_fix5_p2_2_a_fills_writer_conflict_or_refusal_is_typed_and_demotes(
    monkeypatch: pytest.MonkeyPatch, written: ExecutionWriteOutcome, outcome: CallbackOutcome
) -> None:
    made = _make_rig(monkeypatch, record=lambda execution: written)
    try:
        made.app.commissionReport(FakeCommissionReport(_EXEC_ID, 1.05, "USD"))
        made.app.execDetails(-1, _option_contract(), _execution(made.order_ref))
        made.consumer.drain()

        assert _outcomes(made.harness) == [outcome.value]  # the commission stays PENDING
        assert _fills(made.harness) == []
        assert made.invalidations == [UNPROVEN_CALLBACK_REASON]
    finally:
        made.harness.close()


def test_fix5_p2_2_a_raw_account_like_payload_is_refused_typed_and_demotes(rig: _Rig) -> None:
    raw = "DU2468024"  # synthetic: an account-shaped status string trips the journal's guard
    rig.status(_ORDER, raw, 1, 0, 1.25, _PERM, _CLIENT)
    rig.consumer.drain()

    assert _outcomes(rig.harness) == [CallbackOutcome.REFUSED_UNSAFE_PAYLOAD.value]
    journal = _rows(rig.harness, CALLBACK_JOURNAL)
    assert len(journal) == 1 and journal[0]["facts"]["refused"] == "raw-account-like value"
    assert raw.lower() not in json.dumps(journal, default=str).lower()
    assert rig.intent_status() is OrderLifecycle.SUBMITTED
    assert rig.invalidations == [UNPROVEN_CALLBACK_REASON]
