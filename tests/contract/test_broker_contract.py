"""The seam-A ``Broker`` contract (SIM-HARNESS U1): one suite, every implementation.

Seam A is ``chronos.broker.base.Broker``, the surface the live-Wheel order plane
(``chronos.orders``) drives and the one the B1 swap changes the implementation of.
These cases state, once, what any implementation must do on the read and order
surface, so a simulator (U2) and the real paper adapter can be held to the same
behaviour. They are written against ``BrokerHarness`` objects (see ``conftest.py``)
and run today against ``DemoBroker`` and the pipeline tests' ``FakeBroker``.

Where the contract comes from (the real adapter and the boundary that consumes it):

* every local, pre-send refusal raises ``BrokerRefusedBeforeSend``, a PROOF CLAIM that
  nothing reached the venue; the submission boundary resolves the intent to REJECTED
  on that class only and leaves SUBMISSION_UNKNOWN on any other ``BrokerError``
  (``orders/submission.py:775-792``; ``broker/base.py``);
* ``OfficialIBKRBroker`` refuses, before send: an unconnected session, a foreign
  account, ``submit_order`` without ``request.transmit`` or without a send guard (or with
  an unauthorized one), a transmitting preview, and ``modify_order`` outright
  (``official_ibkr.py`` ``_reverify_for_order_path``, ``submit_order``, ``preview_order``,
  ``modify_order``);
* a refusal changes no state; ids are unique and increasing; reads fail closed when
  disconnected.

A violation by an existing implementation is a strict xfail registered in
``conftest.KNOWN_VIOLATIONS`` with a finding id. Nothing in ``src`` is changed to make
a case pass, and no case is skipped.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chronos.broker.base import (
    Broker,
    BrokerError,
    BrokerRefusedBeforeSend,
    BrokerSafetyError,
)
from chronos.domain.enums import ConnectionState, OrderLifecycle, OrderSide
from chronos.domain.models import OrderPreview, OrderSubmission
from tests.contract import conftest as contract_conftest
from tests.contract.conftest import (
    FINDINGS,
    FOREIGN_ACCOUNT,
    KNOWN_VIOLATIONS,
    NOT_RUN,
    REGISTRY,
    UNKNOWN_ORDER_ID,
    Book,
    BrokerHarness,
    CountingGuard,
    OfficialBrokerNotRun,
    Outcome,
    attempt,
    book,
    connected,
    modification,
    official_ibkr_harness,
    order_request,
)

_TERMINAL = frozenset({OrderLifecycle.FILLED, OrderLifecycle.CANCELLED, OrderLifecycle.REJECTED})
_ACCEPTED = frozenset(
    {OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED, OrderLifecycle.FILLED}
)
_SURFACE = (
    "connect",
    "disconnect",
    "connection_status",
    "server_time",
    "account_summary",
    "positions",
    "executions",
    "open_orders",
    "preview_order",
    "submit_order",
    "modify_order",
    "cancel_order",
)


def _assert_refused_before_send(outcome: Outcome, what: str) -> None:
    assert outcome.error is not None, f"{what}: accepted instead of refusing ({outcome.value!r})"
    assert isinstance(outcome.error, BrokerRefusedBeforeSend), (
        f"{what}: raised {type(outcome.error).__name__}, not BrokerRefusedBeforeSend; the "
        "submission boundary would leave the intent in SUBMISSION_UNKNOWN"
    )


def _assert_preview_not_accepted(outcome: Outcome, what: str) -> None:
    """A preview that must not succeed either refuses before send or answers accepted=False."""

    if outcome.error is not None:
        assert isinstance(outcome.error, BrokerRefusedBeforeSend), (
            f"{what}: raised {type(outcome.error).__name__}, not BrokerRefusedBeforeSend"
        )
        return
    preview = outcome.value
    assert isinstance(preview, OrderPreview)
    assert preview.accepted is False, f"{what}: the preview was accepted"


# ---------------------------------------------------------------------------
# A. The read surface (every implementation).
# ---------------------------------------------------------------------------


async def test_connection_status_reports_the_connected_account(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        status = await broker.connection_status()

    assert status.connected is True
    assert status.state is ConnectionState.CONNECTED
    assert status.account_id == harness.account_id


async def test_reads_fail_closed_when_disconnected(harness: BrokerHarness) -> None:
    broker = harness.build()
    await broker.connect()
    await broker.disconnect()

    status = await broker.connection_status()
    assert status.connected is False, "connection_status still reports connected after disconnect"
    for name, make_call in (
        ("account_summary", lambda: broker.account_summary()),
        ("positions", lambda: broker.positions()),
        ("executions", lambda: broker.executions()),
        ("open_orders", lambda: broker.open_orders()),
        ("server_time", lambda: broker.server_time()),
        ("cancel_order", lambda: broker.cancel_order(harness.owned_order_id)),
    ):
        outcome = await attempt(make_call())
        assert isinstance(outcome.error, BrokerError), (
            f"{name} answered while disconnected instead of raising a BrokerError"
        )


async def test_account_identity_is_consistent_across_the_read_surface(
    harness: BrokerHarness,
) -> None:
    async with connected(harness) as broker:
        status = await broker.connection_status()
        summary = await broker.account_summary()
        positions = await broker.positions()
        orders = await broker.open_orders()
        executions = await broker.executions()

    seen = {status.account_id, summary.account_id}
    seen.update(position.account_id for position in positions)
    seen.update(order.account_id for order in orders)
    seen.update(execution.account_id for execution in executions)
    assert seen == {harness.account_id}


async def test_reads_are_idempotent(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        first = await book(broker)
        second = await book(broker)

    assert first == second


async def test_executions_since_is_an_inclusive_lower_bound(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        everything = await broker.executions()
        assert everything, "the harness seed must contain at least one execution"
        stamps = sorted(execution.timestamp for execution in everything)
        ids = {execution.execution_id for execution in everything}

        from_earliest = await broker.executions(since=stamps[0])
        at_latest = await broker.executions(since=stamps[-1])
        after_latest = await broker.executions(since=stamps[-1] + timedelta(seconds=1))

    assert {execution.execution_id for execution in from_earliest} == ids
    assert {execution.execution_id for execution in at_latest} == {
        execution.execution_id for execution in everything if execution.timestamp >= stamps[-1]
    }
    assert after_latest == (), "executions(since=...) returned executions older than the bound"


async def test_execution_ids_are_unique(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        executions = await broker.executions()

    ids = [execution.execution_id for execution in executions]
    assert len(ids) == len(set(ids))


async def test_every_open_order_is_still_working(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        orders = await broker.open_orders()

    assert orders, "the harness seed must contain at least one working order"
    for order in orders:
        assert order.lifecycle not in _TERMINAL, f"order {order.broker_order_id} is terminal"
        assert order.remaining_quantity > 0, f"order {order.broker_order_id} has nothing left"


async def test_open_order_fills_agree_with_executions(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        orders = await broker.open_orders()
        executions = await broker.executions()

    for order in orders:
        filled = sum(
            (e.quantity for e in executions if e.broker_order_id == order.broker_order_id),
            Decimal(0),
        )
        assert filled == order.filled_quantity, (
            f"order {order.broker_order_id}: executions sum to {filled}, "
            f"the order says {order.filled_quantity} filled"
        )


# ---------------------------------------------------------------------------
# B. Preview (what-if): never transmits, never changes state.
# ---------------------------------------------------------------------------


async def test_preview_never_accepts_a_transmitting_request(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.preview_order(order_request(harness, transmit=True)))
        after = await book(broker)

    _assert_preview_not_accepted(outcome, "a preview carrying transmit=True")
    assert after == before


async def test_a_valid_preview_echoes_the_request_and_changes_nothing(
    harness: BrokerHarness,
) -> None:
    request = order_request(harness, transmit=False)
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.preview_order(request))
        after = await book(broker)

    assert outcome.error is None, f"a valid preview raised {outcome.error!r}"
    preview = outcome.value
    assert isinstance(preview, OrderPreview)
    assert preview.request == request
    assert preview.previewed_at.tzinfo is not None
    assert after == before


async def test_preview_for_a_foreign_account_is_not_accepted(harness: BrokerHarness) -> None:
    request = order_request(harness, transmit=False, account_id=FOREIGN_ACCOUNT)
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.preview_order(request))
        after = await book(broker)

    _assert_preview_not_accepted(outcome, "a preview for a foreign account")
    assert after == before


# ---------------------------------------------------------------------------
# C. Order entry refusals: BrokerRefusedBeforeSend, and no state change.
# ---------------------------------------------------------------------------


async def test_submit_without_a_send_guard_is_refused_before_send(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(
            broker.submit_order(order_request(harness, transmit=True), send_guard=None)
        )
        after = await book(broker)

    _assert_refused_before_send(outcome, "submit without a send guard")
    assert after == before


async def test_submit_without_transmit_authority_is_refused_before_send(
    harness: BrokerHarness,
) -> None:
    guard = CountingGuard()
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(
            broker.submit_order(order_request(harness, transmit=False), send_guard=guard)
        )
        after = await book(broker)

    _assert_refused_before_send(outcome, "submit with request.transmit=False")
    assert after == before


async def test_submit_with_an_unauthorized_guard_is_refused_before_send(
    harness: BrokerHarness,
) -> None:
    guard = CountingGuard(authorized=False)
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(
            broker.submit_order(order_request(harness, transmit=True), send_guard=guard)
        )
        after = await book(broker)

    _assert_refused_before_send(outcome, "submit under an unauthorized send guard")
    assert after == before


async def test_submit_for_a_foreign_account_is_refused_before_send(harness: BrokerHarness) -> None:
    guard = CountingGuard()
    request = order_request(harness, transmit=True, account_id=FOREIGN_ACCOUNT)
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.submit_order(request, send_guard=guard))
        after = await book(broker)

    _assert_refused_before_send(outcome, "submit for a foreign account")
    assert after == before


async def test_modify_is_refused_before_send_and_changes_nothing(harness: BrokerHarness) -> None:
    """The B1 target refuses modify outright ("cancel and re-submit"), so a sim must too."""

    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.modify_order(modification(harness.owned_order_id)))
        after = await book(broker)

    _assert_refused_before_send(outcome, "modify_order")
    assert after == before


# ---------------------------------------------------------------------------
# D. An adapter that refuses all order entry.
# ---------------------------------------------------------------------------


async def test_a_refusing_adapter_never_enters_the_send_guard(
    refusing_harness: BrokerHarness,
) -> None:
    guard = CountingGuard()
    async with connected(refusing_harness) as broker:
        before = await book(broker)
        submit = await attempt(
            broker.submit_order(order_request(refusing_harness, transmit=True), send_guard=guard)
        )
        modify = await attempt(broker.modify_order(modification(refusing_harness.owned_order_id)))
        after = await book(broker)

    assert isinstance(submit.error, BrokerSafetyError), f"submit did not refuse: {submit.value!r}"
    assert isinstance(modify.error, BrokerSafetyError), f"modify did not refuse: {modify.value!r}"
    assert guard.entries == 0, "a refusing adapter entered the send guard"
    assert after == before


# ---------------------------------------------------------------------------
# E. An adapter that accepts orders.
# ---------------------------------------------------------------------------


async def test_an_authorized_submit_enters_the_guard_once_and_returns_a_submission(
    accepting_harness: BrokerHarness,
) -> None:
    guard = CountingGuard()
    request = order_request(accepting_harness, transmit=True)
    async with connected(accepting_harness) as broker:
        outcome = await attempt(broker.submit_order(request, send_guard=guard))

    assert outcome.error is None, f"an authorized submit raised {outcome.error!r}"
    submission = outcome.value
    assert isinstance(submission, OrderSubmission)
    assert guard.entries == 1, "the send guard must be entered exactly once"
    assert submission.correlation_id == request.correlation_id
    assert submission.broker_order_id > 0
    assert submission.client_id >= 0
    assert submission.lifecycle in _ACCEPTED


async def test_submitted_order_ids_are_unique_and_increasing(
    accepting_harness: BrokerHarness,
) -> None:
    ids: list[int] = []
    async with connected(accepting_harness) as broker:
        for number in range(3):
            request = order_request(
                accepting_harness, transmit=True, correlation_id=f"contract-seq-{number}"
            )
            outcome = await attempt(broker.submit_order(request, send_guard=CountingGuard()))
            assert outcome.error is None, f"submit #{number} raised {outcome.error!r}"
            ids.append(outcome.value.broker_order_id)

    assert ids == sorted(set(ids)), f"order ids are not unique and increasing: {ids}"


async def test_a_working_submission_appears_in_open_orders(
    accepting_harness: BrokerHarness,
) -> None:
    request = order_request(accepting_harness, transmit=True)
    async with connected(accepting_harness) as broker:
        outcome = await attempt(broker.submit_order(request, send_guard=CountingGuard()))
        assert outcome.error is None, f"an authorized submit raised {outcome.error!r}"
        submission = outcome.value
        working = {order.broker_order_id: order for order in await broker.open_orders()}

    if submission.lifecycle is OrderLifecycle.FILLED:
        assert submission.broker_order_id not in working
        return
    assert submission.broker_order_id in working, "the accepted order is not in open_orders"
    order = working[submission.broker_order_id]
    assert order.order_ref == request.order_ref
    assert order.account_id == request.account_id
    assert order.side is request.side
    assert order.quantity == request.quantity
    assert order.limit_price == request.limit_price


async def test_a_fill_moves_positions_executions_and_cash_consistently(
    accepting_harness: BrokerHarness,
) -> None:
    """Needs the harness's fill hook; the body past the hook is first exercised by U2."""

    hook = accepting_harness.cause_fill
    assert hook is not None, (
        "this harness has no fill hook, so it cannot show fill consistency (SURVEY G2)"
    )
    request = order_request(accepting_harness, transmit=True)
    assert request.side is OrderSide.SELL
    async with connected(accepting_harness) as broker:
        submission = await broker.submit_order(request, send_guard=CountingGuard())
        before = await book(broker)
        await hook(broker, submission.broker_order_id, request.quantity)
        after = await book(broker)

    new = [
        execution
        for execution in after.executions
        if execution not in before.executions
        and execution.broker_order_id == submission.broker_order_id
    ]
    assert new, "the fill produced no execution"
    assert sum((execution.quantity for execution in new), Decimal(0)) == request.quantity
    assert all(execution.price >= request.limit_price for execution in new), (
        "a SELL filled below its limit"
    )
    assert all(execution.commission is not None for execution in new)

    def held(snapshot: Book) -> Decimal:
        return sum(
            (p.quantity for p in snapshot.positions if p.contract == request.contract),
            Decimal(0),
        )

    assert held(after) - held(before) == -request.quantity, "the position did not move by the fill"
    assert after.cash[0] > before.cash[0], "selling did not credit cash"
    assert submission.broker_order_id not in {o.broker_order_id for o in after.open_orders}


# ---------------------------------------------------------------------------
# F. Cancel.
# ---------------------------------------------------------------------------


async def test_cancel_removes_a_cancelled_order_from_open_orders(harness: BrokerHarness) -> None:
    owned = harness.owned_order_id
    async with connected(harness) as broker:
        before = await book(broker)
        assert owned in {order.broker_order_id for order in before.open_orders}, (
            "seed lacks the order"
        )
        outcome = await attempt(broker.cancel_order(owned))
        after = await book(broker)

    assert outcome.error is None, (
        f"cancelling a Chronos-owned working order raised {outcome.error!r}"
    )
    result = outcome.value
    assert result.broker_order_id == owned
    assert result.requested is True
    assert result.lifecycle in {OrderLifecycle.CANCELLED, OrderLifecycle.CANCEL_PENDING}
    if result.lifecycle is OrderLifecycle.CANCELLED:
        assert owned not in {order.broker_order_id for order in after.open_orders}, (
            "reported CANCELLED but the order is still in open_orders"
        )
    assert after.positions == before.positions
    assert after.executions == before.executions


async def test_cancel_of_an_unknown_order_never_reports_cancelled(harness: BrokerHarness) -> None:
    async with connected(harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.cancel_order(UNKNOWN_ORDER_ID))
        after = await book(broker)

    if outcome.error is not None:
        assert isinstance(outcome.error, BrokerError)
    else:
        assert outcome.value.broker_order_id == UNKNOWN_ORDER_ID
        assert outcome.value.lifecycle is not OrderLifecycle.CANCELLED, (
            "an order that was never open was reported CANCELLED"
        )
    assert after == before


async def test_cancel_refuses_to_touch_a_foreign_order(foreign_harness: BrokerHarness) -> None:
    foreign = foreign_harness.foreign_order_id
    assert foreign is not None
    async with connected(foreign_harness) as broker:
        before = await book(broker)
        outcome = await attempt(broker.cancel_order(foreign))
        after = await book(broker)

    if outcome.error is not None:
        assert isinstance(outcome.error, BrokerSafetyError)
    else:
        assert outcome.value.requested is False, "Chronos cancelled an order it does not own"
    assert after == before
    assert foreign in {order.broker_order_id for order in after.open_orders}


# ---------------------------------------------------------------------------
# G. The suite's own wiring (placeholders, hook, xfail table).
# ---------------------------------------------------------------------------


async def test_the_read_and_order_surface_exists_on_every_harness(harness: BrokerHarness) -> None:
    broker = harness.build()
    for name in _SURFACE:
        assert inspect.iscoroutinefunction(getattr(broker, name, None)), f"{name} is not async"
    assert isinstance(broker, Broker) is harness.implements_full_protocol


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_official_ibkr_is_declared_and_never_constructed() -> None:
    """The placeholder is a declaration, not a skip, and nothing here can open a connection."""

    assert "official_ibkr" in NOT_RUN
    assert "official_ibkr" not in REGISTRY
    with pytest.raises(OfficialBrokerNotRun):
        official_ibkr_harness()

    forbidden = ("official_ibkr", "ibapi", "ib_async", "chronos.broker.ibkr", "socket")
    for path in (Path(contract_conftest.__file__), Path(__file__)):
        for module in _imported_modules(path):
            assert not any(part in module for part in forbidden), f"{path.name} imports {module}"


def test_the_sim_hook_registers_exactly_when_u2_exists() -> None:
    present = importlib.util.find_spec("tests.support.sim_broker") is not None
    assert ("sim" in REGISTRY) is present
    if present:
        assert REGISTRY["sim"].name == "sim"


def test_every_known_violation_names_a_real_case() -> None:
    cases = {name for name, obj in globals().items() if name.startswith("test_") and callable(obj)}
    for (harness_name, case), finding in KNOWN_VIOLATIONS.items():
        assert harness_name in REGISTRY, f"{harness_name} is not a registered harness"
        assert case in cases, f"{case} is not a case in this module"
        assert finding in FINDINGS, f"{finding} is not a recorded finding"
    assert {finding for finding in KNOWN_VIOLATIONS.values()} == set(FINDINGS), "orphan finding"
