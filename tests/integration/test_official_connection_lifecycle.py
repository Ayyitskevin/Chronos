"""B1 U-a: the real OfficialIBKRBroker's connection lifecycle over the in-process FakeGateway.

Every case runs the unmodified adapter (its ``_App``, ``CallbackBridge`` and normalizers) and,
where the production path does, the real ``BrokerConnectionManager`` and the real
``ReconciliationReadiness`` latch. Each case asserts two things: the outcome fails closed, and
what an operator can SEE (``connection_status()`` fields, the exception text, the readiness
snapshot and its reason, the manager's sanitized observation, the bridge's notices).

The gateway behaviours driven here are knobs on ``tests/support/fake_ibapi.py``. Every IB
behaviour they encode (the 326 "client id in use" refusal before the handshake, 321 for the
Read-Only API, the error-callback shapes, the connectivity codes) is ASSUMED-IB: the author's
reading of the TWS API, to be confirmed at K4 from the installed ``ibapi`` or one capture. A
green case proves the adapter against that model, not against IB.

Real-adapter gaps are strict xfails with a ``B1R-F`` finding id (not src fixes). Nothing here
connects to anything real; every ALLOW_* flag is false except inside the one test-built
Settings object of the read-only submit case.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_sim_order_lifecycle import CROSSING, _ref, _Venue, harness
from tests.support import fake_ibapi
from tests.support.order_fakes import option_contract

import chronos.broker.official_ibkr as official_module
from chronos.broker.base import BrokerConnectionError, BrokerSafetyError
from chronos.broker.connection import BrokerConnectionManager
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.config.settings import Settings
from chronos.domain.enums import (
    ConnectionState,
    IBEnvironment,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    ReconciliationStatus,
)
from chronos.domain.models import OrderRequest
from chronos.orders.reconciliation_readiness import ReconciliationReadiness

CONNECTIVITY_CODES = (1100, 1101, 1102, 1300, 2110)
ERROR_STYLES = ("plain", "json", "timestamped")
OTHER_PAPER = "DU8888888"


# --- fixtures and helpers -------------------------------------------------------------------


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_ibapi.FakeGateway]:
    seeded = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, seeded)
    monkeypatch.setattr(official_module, "_CONNECT_TIMEOUT_S", 0.3)
    try:
        yield seeded
    finally:
        seeded.release_readers()  # a stuck reader must never outlive its test
    # A callback that raised on a reader thread is recorded, never lost (reader build-ua P3-2).
    assert seeded.callback_errors == []


class _SpyReadiness:
    """The real latch, with every invalidation reason recorded for the operator-view asserts."""

    def __init__(self) -> None:
        self.real = ReconciliationReadiness()
        self.reasons: list[str] = []

    def reconcile(self) -> None:
        assert self.real.complete(
            expected_generation=self.real.snapshot().generation,
            status=ReconciliationStatus.RECONCILED,
            reason="U-a: broker and local state start equal",
            reconciled_at=datetime.now(tz=UTC),
        )

    def invalidate(self, reason: str) -> object:
        self.reasons.append(reason)
        return self.real.invalidate(reason)

    @contextmanager
    def invalidating_transition(self, reason: str) -> Iterator[None]:
        self.reasons.append(f"scope: {reason}")
        with self.real.invalidating_transition(reason):
            yield

    @property
    def status(self) -> ReconciliationStatus:
        return self.real.snapshot().status


def _hooked(readiness: _SpyReadiness, settings: Settings | None = None) -> OfficialIBKRBroker:
    """The adapter as ``build_runtime`` wires its two connection hooks."""

    return OfficialIBKRBroker(
        settings or fake_ibapi.official_settings(),
        on_connection_uncertain=readiness.invalidate,
        on_managed_account_scope_change=readiness.invalidating_transition,
    )


def _reader_alive() -> bool:
    return any(t.name == fake_ibapi.READER_THREAD and t.is_alive() for t in threading.enumerate())


def _connects(gateway: fake_ibapi.FakeGateway) -> list[int | None]:
    return [client for client, name in gateway.transcript if name == "connect"]


def _settings(**update: Any) -> Settings:
    return fake_ibapi.official_settings().model_copy(update=update)


def _shows_an_account_id(text: str) -> bool:
    return fake_ibapi.ACCOUNT in text or OTHER_PAPER in text


def test_the_no_account_id_check_can_fail() -> None:
    """Positive control for the wrong-account case's disclosure assertion (reader P3-4)."""

    assert _shows_an_account_id(f"managed ({OTHER_PAPER},) vs {fake_ibapi.ACCOUNT}")
    assert not _shows_an_account_id("connected session does not manage the configured account")


# --- 1. connect, and the refusals that never reach the gateway ------------------------------


def test_connect_reports_the_managed_paper_account_and_seeds_the_order_ids(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    manager = BrokerConnectionManager(broker, readiness=readiness)
    try:
        manager.connect()
        status = manager.connection_status()
        observation = manager.connection_observation()
        next_id = broker.order_ids.allocate()
        alive = _reader_alive()
    finally:
        manager.close()
    # the operator's view of a healthy session
    assert (status.connected, status.state) == (True, ConnectionState.CONNECTED)
    assert status.account_id == fake_ibapi.ACCOUNT
    assert status.managed_accounts == (fake_ibapi.ACCOUNT,)
    assert (observation.connected, observation.reason_code) == (True, "status_observed")
    assert alive
    # a new session never inherits evidence; the allocator starts at the gateway's nextValidId
    assert readiness.reasons[0] == "broker connection started; reconciliation required"
    assert next_id == gateway.next_valid_id


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"ib_port": 7496}, "requires a paper port"),
        ({"ib_port": 4001}, "requires a paper port"),
        ({"ib_environment": IBEnvironment.LIVE, "ib_port": 7497}, "requires a live port"),
        ({"ib_client_id": 0}, "master client"),
        ({"ib_account_id": ""}, "IB_ACCOUNT_ID is required"),
        ({"ib_account_allowlist": ("DU1111111",)}, "not in IB_ACCOUNT_ALLOWLIST"),
    ],
)
def test_a_wrong_port_environment_or_identity_is_refused_before_the_gateway(
    gateway: fake_ibapi.FakeGateway, update: dict[str, Any], message: str
) -> None:
    with pytest.raises(BrokerSafetyError, match=message):
        OfficialIBKRBroker(_settings(**update))
    assert gateway.transcript == []  # nothing reached the gateway, not even a connect


# --- 2. disconnect, a dropped session, reconnect on demand ---------------------------------


def test_disconnect_invalidates_before_the_broker_call_and_ends_the_reader(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    manager = BrokerConnectionManager(broker, readiness=readiness)
    manager.connect()
    readiness.reconcile()
    seen_at_disconnect: list[ReconciliationStatus] = []
    original = broker.disconnect

    async def recording_disconnect() -> None:
        seen_at_disconnect.append(readiness.status)
        await original()

    broker.disconnect = recording_disconnect  # type: ignore[method-assign]
    try:
        manager.disconnect()
        status = asyncio.run(broker.connection_status())
        observation = manager.connection_observation()
    finally:
        manager.close()
    assert seen_at_disconnect and seen_at_disconnect[0] is not ReconciliationStatus.RECONCILED
    assert (status.connected, status.account_id, status.managed_accounts) == (False, None, ())
    assert observation.reason_code == "connection_uncertain"
    assert not _reader_alive()


def test_a_dropped_session_fails_closed_and_is_not_reconnected_automatically(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """The fail-closed state after a drop. Its "no second connect" check is a SMOKE only (a
    0.2 s window); the structural pin is ``test_no_reconnect_loop_exists_in_the_broker_plane``."""

    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    manager = BrokerConnectionManager(broker, readiness=readiness)
    manager.connect()
    readiness.reconcile()
    try:
        gateway.drop()
        time.sleep(0.2)  # long enough for any (non-existent) reconnect loop to act
        status = asyncio.run(broker.connection_status())
        with pytest.raises(BrokerConnectionError, match="not connected"):
            manager.run(broker.account_summary())
        status_after_drop = readiness.status  # read BEFORE teardown: close() also drops it
        reasons_after_drop = list(readiness.reasons)
    finally:
        manager.close()
    assert status_after_drop is not ReconciliationStatus.RECONCILED
    assert "official IBKR connection closed" in reasons_after_drop
    assert (status.connected, status.account_id) == (False, None)
    assert len(_connects(gateway)) == 1  # no second connect: recovery is an operator restart


def test_a_reconnect_needs_a_fresh_handshake_old_events_never_vouch(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """``reset_for_reconnect`` (callbacks.py:489): the first session's nextValidId and
    managedAccounts must not satisfy the second handshake."""

    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    asyncio.run(broker.connect())
    gateway.drop()
    gateway.handshake = "no_next_valid_id"
    with pytest.raises(BrokerConnectionError, match="did not complete the connection handshake"):
        asyncio.run(broker.connect())
    assert len(_connects(gateway)) == 2
    assert not asyncio.run(broker.connection_status()).connected


# --- 3. the restart session: a gateway still holding the client id --------------------------


def test_a_held_client_id_fails_closed_at_the_handshake(gateway: fake_ibapi.FakeGateway) -> None:
    """The checklist's restart session after a crash: the gateway still holds client 17."""

    gateway.held_client_ids = {fake_ibapi.CLIENT_ID}
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    with pytest.raises(BrokerConnectionError, match="did not complete the connection handshake"):
        asyncio.run(broker.connect())
    status = asyncio.run(broker.connection_status())
    assert (status.connected, status.account_id) == (False, None)
    assert not _reader_alive()
    # the reason exists inside the bridge, where no operator surface reads it
    assert any(
        note.startswith(f"[{fake_ibapi.CODE_CLIENT_ID_IN_USE}]") for note in broker.bridge.notices
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "B1R-F1: a code the gateway sends before the handshake (326 client id in use; also "
        "502/504, ASSUMED-IB) lands only in bridge.notices (callbacks.py:397-399); the handshake "
        "timeout error (official_ibkr.py:982-987) says 'is the API enabled?' and omits it. The "
        "operator sees the wrong cause. Diagnostic gap, not a safety bypass; the fix (U-h) is "
        "src and Kevin's call."
    ),
)
def test_the_handshake_timeout_names_the_code_the_gateway_sent(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.held_client_ids = {fake_ibapi.CLIENT_ID}
    broker = _hooked(_SpyReadiness())
    with pytest.raises(BrokerConnectionError) as raised:
        asyncio.run(broker.connect())
    assert str(fake_ibapi.CODE_CLIENT_ID_IN_USE) in str(raised.value)


# --- 4. handshake timeouts, wrong account -------------------------------------------------


@pytest.mark.parametrize("handshake", ["no_next_valid_id", "no_managed_accounts"])
def test_an_incomplete_handshake_times_out_and_leaves_nothing_running(
    gateway: fake_ibapi.FakeGateway, handshake: str
) -> None:
    gateway.handshake = handshake
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    manager = BrokerConnectionManager(broker, readiness=readiness)
    try:
        with pytest.raises(
            BrokerConnectionError, match="did not complete the connection handshake"
        ):
            manager.connect()
        status = asyncio.run(broker.connection_status())
    finally:
        manager.close()
    assert (status.connected, status.account_id) == (False, None)
    assert "disconnect" in [name for _, name in gateway.transcript]
    assert not _reader_alive()
    # the manager marked the failed connect itself uncertain (connection.py:154-156)
    assert "broker call failed; connection state is uncertain" in readiness.reasons


def test_a_session_that_does_not_manage_the_configured_account_is_refused(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.managed_accounts = (OTHER_PAPER,)
    broker = _hooked(_SpyReadiness())
    with pytest.raises(BrokerSafetyError, match="does not manage the configured account") as raised:
        asyncio.run(broker.connect())
    message = str(raised.value)
    assert "managed count=1" in message
    assert not _shows_an_account_id(message)  # ids never shown
    status = asyncio.run(broker.connection_status())
    assert (status.connected, status.account_id) == (False, None)
    assert not _reader_alive()


# --- 5. the Read-Only API (ASSUMED-IB: 321 on placeOrder, including whatIf) ------------------


def _order_request(*, transmit: bool) -> OrderRequest:
    return OrderRequest(
        correlation_id="u-a-read-only",
        account_id=fake_ibapi.ACCOUNT,
        contract=option_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("1.25"),
        order_ref="CHR-UA-READ-ONLY",
        transmit=transmit,
    )


class _AllowGuard:
    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield True


def test_a_read_only_gateway_declines_a_preview_with_its_code(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.read_only = True
    broker = _hooked(_SpyReadiness())
    asyncio.run(broker.connect())
    try:
        preview = asyncio.run(broker.preview_order(_order_request(transmit=False)))
    finally:
        asyncio.run(broker.disconnect())
    assert preview.accepted is False
    assert any(str(fake_ibapi.CODE_READ_ONLY) in warning for warning in preview.warnings)
    assert gateway.sim.sent_order_count == 2  # the seed only: nothing reached the venue


def test_a_read_only_gateway_rejects_a_submit_and_the_venue_is_unchanged(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Transmit-true exists ONLY in this test-built Settings object, against the in-process fake."""

    gateway.read_only = True
    transmit = _settings(allow_order_transmit=True, ib_account_allowlist=(fake_ibapi.ACCOUNT,))
    assert transmit.allow_live_trading is False
    broker = _hooked(_SpyReadiness(), transmit)
    asyncio.run(broker.connect())
    try:
        submission = asyncio.run(
            broker.submit_order(_order_request(transmit=True), send_guard=_AllowGuard())
        )
    finally:
        asyncio.run(broker.disconnect())
    assert submission.lifecycle is OrderLifecycle.REJECTED
    assert gateway.sim.sent_order_count == 2


def test_an_ordinary_submit_does_reach_the_venue(gateway: fake_ibapi.FakeGateway) -> None:
    """Positive control for the read-only cases' "the seed only" count (reader P3-4): the same
    submit against a gateway that is NOT read-only makes the venue count 3."""

    transmit = _settings(allow_order_transmit=True, ib_account_allowlist=(fake_ibapi.ACCOUNT,))
    broker = _hooked(_SpyReadiness(), transmit)
    asyncio.run(broker.connect())
    try:
        submission = asyncio.run(
            broker.submit_order(_order_request(transmit=True), send_guard=_AllowGuard())
        )
    finally:
        asyncio.run(broker.disconnect())
    assert submission.lifecycle is not OrderLifecycle.REJECTED
    assert gateway.sim.sent_order_count == 3


# --- 6. the managed-account set changes mid-session ---------------------------------------


def test_a_changed_managed_set_locks_readiness_through_the_scope_hook(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    asyncio.run(broker.connect())
    readiness.reconcile()
    try:
        gateway.change_managed_accounts((fake_ibapi.ACCOUNT, OTHER_PAPER))
        added = asyncio.run(broker.connection_status())
        status_after_change = readiness.status  # read BEFORE teardown: disconnect also drops it
        gateway.change_managed_accounts((OTHER_PAPER,))
        removed = asyncio.run(broker.connection_status())
    finally:
        asyncio.run(broker.disconnect())
    assert readiness.reasons.count("scope: official IBKR managed-account scope changed") == 2
    assert status_after_change is not ReconciliationStatus.RECONCILED
    assert set(added.managed_accounts) == {fake_ibapi.ACCOUNT, OTHER_PAPER}
    assert removed.account_id is None  # the configured id is no longer managed: never reported


def test_a_hookless_adapter_stores_a_changed_managed_set_without_any_latch(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Characterization (Daybreak b1 P2-1): the capture harness's hookless shape. The change is
    stored and the configured id is still reported; nothing latches. Pinned as today's
    behaviour; B1R-F3 below states what fail-closed would be."""

    broker = OfficialIBKRBroker(fake_ibapi.official_settings())
    asyncio.run(broker.connect())
    try:
        gateway.change_managed_accounts((fake_ibapi.ACCOUNT, OTHER_PAPER))
        status = asyncio.run(broker.connection_status())
    finally:
        asyncio.run(broker.disconnect())
    assert set(status.managed_accounts) == {fake_ibapi.ACCOUNT, OTHER_PAPER}
    assert status.account_id == fake_ibapi.ACCOUNT


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "B1R-F3 (a REQUIREMENT GAP, not a defect against the adapter's stated contract, which "
        "reports the configured id while it is still managed, official_ibkr.py:1043-1067): a "
        "HOOKLESS OfficialIBKRBroker (the capture harness, capture_readonly.py:296-297) "
        "keeps reporting the configured account after its managed-account set grows "
        "(callbacks.py:346-364 stores the change; the hookless invalidation is a no-op, "
        ":414-417), so a session whose scope drifted still reads as the B1 account. Fail-closed "
        "would stop reporting the account (or latch) once the set differs from the first one. "
        "Owner call: the capture harness is hookless by design; U-g's probe closes it for itself."
    ),
)
def test_a_hookless_adapter_stops_vouching_for_the_account_after_its_scope_drifts(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    broker = OfficialIBKRBroker(fake_ibapi.official_settings())
    asyncio.run(broker.connect())
    try:
        gateway.change_managed_accounts((fake_ibapi.ACCOUNT, OTHER_PAPER))
        status = asyncio.run(broker.connection_status())
    finally:
        asyncio.run(broker.disconnect())
    assert status.account_id is None


# --- 7. the connection-uncertainty latch, through the wire -------------------------------


@pytest.mark.parametrize("style", ERROR_STYLES)
@pytest.mark.parametrize("code", CONNECTIVITY_CODES)
def test_a_connectivity_code_drops_readiness_with_the_code_in_the_reason(
    gateway: fake_ibapi.FakeGateway, code: int, style: str
) -> None:
    gateway.error_style = style
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    asyncio.run(broker.connect())
    readiness.reconcile()
    try:
        gateway.emit_error(-1, code, "connectivity notice")
        status_after = readiness.status  # read BEFORE teardown: disconnect also drops it
        reasons_after = list(readiness.reasons)
    finally:
        asyncio.run(broker.disconnect())
    assert f"official IBKR connectivity event {code}" in reasons_after
    assert status_after is not ReconciliationStatus.RECONCILED


def test_a_benign_farm_notice_is_recorded_and_does_not_drop_readiness(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    """Negative control: 2104 'market data farm connection is OK' is benign (callbacks.py:43)."""

    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    asyncio.run(broker.connect())
    readiness.reconcile()
    try:
        gateway.emit_error(-1, 2104, "Market data farm connection is OK")
        status_after = readiness.status
    finally:
        asyncio.run(broker.disconnect())
    assert status_after is ReconciliationStatus.RECONCILED
    assert any(note.startswith("[2104]") for note in broker.bridge.notices)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "B1R-F2 (CONDITIONAL: R-ARITY, ASSUMED-IB; flips to a real finding or a non-issue at K4 "
        "#1, or earlier by U-d's offline inspect.signature check): if the installed ibapi calls "
        "error(reqId, errorTime:int, "
        "errorCode, errorString, advancedOrderRejectJson), the adapter's error(*args) "
        "(official_ibkr.py:499-507) strips nothing (the first value is an int) and reads the "
        "TIME as the code, so a 1100 connection loss never drops readiness: fail-open. Whether "
        "that shape is real is the first K4 check (DESIGN K4 #1)."
    ),
)
def test_a_connection_loss_in_the_errortime_second_shape_still_drops_readiness(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.error_style = "errortime_second"
    readiness = _SpyReadiness()
    broker = _hooked(readiness)
    asyncio.run(broker.connect())
    readiness.reconcile()
    try:
        gateway.emit_error(-1, 1100, "Connectivity between IB and TWS has been lost")
        status_after = readiness.status  # read BEFORE teardown: disconnect also drops it
    finally:
        asyncio.run(broker.disconnect())
    assert status_after is not ReconciliationStatus.RECONCILED


# --- 8. a reader thread that will not stop --------------------------------------------------


def test_a_reader_that_will_not_stop_locks_reconnect(gateway: fake_ibapi.FakeGateway) -> None:
    gateway.stuck_reader = True
    broker = _hooked(_SpyReadiness())
    asyncio.run(broker.connect())
    with pytest.raises(BrokerConnectionError, match="reconnect is locked"):
        asyncio.run(broker.disconnect())
    with pytest.raises(BrokerConnectionError, match="reconnect is locked"):
        asyncio.run(broker.connect())  # the stale session is retained, never silently replaced
    assert len(_connects(gateway)) == 1
    assert not asyncio.run(broker.connection_status()).connected


# --- 9. the fake itself must fail a test, never hang it (reader build-ua P3-2) --------------


def test_a_raising_callback_fails_the_test_instead_of_hanging(
    gateway: fake_ibapi.FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A callback that raises on the reader thread is recorded, the reader survives, and the
    next dispatch barrier fails at once. Before r1 it killed the reader and every later
    ``wait_dispatched`` blocked forever (the reader's scope-hook mutant ran for 12 minutes)."""

    broker = _hooked(_SpyReadiness())
    asyncio.run(broker.connect())

    def broken(accounts_csv: str) -> None:
        raise TypeError("an adapter callback bug")

    monkeypatch.setattr(broker.bridge, "on_managed_accounts", broken)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match="callback raised"):
            gateway.change_managed_accounts((fake_ibapi.ACCOUNT, OTHER_PAPER))
        assert _reader_alive()  # the reader survived the raising callback
    finally:
        asyncio.run(broker.disconnect())
        gateway.callback_errors.clear()  # this test raised one on purpose
    assert time.monotonic() - started < 5


def test_a_dispatch_barrier_fails_after_its_bound(gateway: fake_ibapi.FakeGateway) -> None:
    """``wait_dispatched`` is bounded: a reader that never drains fails the test."""

    client = fake_ibapi.FakeEClient.bound_to(gateway)(fake_ibapi.RecordingWrapper())
    client.emit("currentTime", 1)  # queued, and no reader thread is running to dispatch it
    started = time.monotonic()
    with pytest.raises(AssertionError, match="not dispatched within"):
        client.wait_dispatched(timeout=0.2)
    assert time.monotonic() - started < 2


# --- 10. no automatic reconnect, structurally (reader build-ua P3-3) ------------------------

_REPO = Path(__file__).resolve().parents[2]
_BROKER_PLANE = (
    _REPO / "src" / "chronos" / "broker",
    _REPO / "src" / "chronos" / "runtime.py",
    _REPO / "src" / "chronos" / "service",
)


_CONNECT_METHODS = frozenset({"connect", "connectAsync"})


def _is_event_subscription(receiver: ast.expr) -> bool:
    """``errorEvent.connect(handler)`` subscribes a callback (eventkit); it opens nothing."""

    name = receiver.attr if isinstance(receiver, ast.Attribute) else getattr(receiver, "id", "")
    return str(name).lower().endswith("event")


def _connects_inside_a_loop(source: str) -> list[int]:
    """Lines of every session ``<x>.connect(...)``/``connectAsync(...)`` inside a loop body
    (event subscriptions excluded)."""

    found: list[int] = []

    def visit(node: ast.AST, in_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                visit(child, False)  # a nested function body is not this loop's body
                continue
            looping = in_loop or isinstance(child, ast.For | ast.AsyncFor | ast.While)
            if (
                looping
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _CONNECT_METHODS
                and not _is_event_subscription(child.func.value)
            ):
                found.append(child.lineno)
            visit(child, looping)

    visit(ast.parse(source), False)
    return found


def test_the_reconnect_loop_scanner_finds_a_loop_around_connect() -> None:
    """Positive control: the scanner flags the shape it exists to forbid."""

    flagged = (
        "async def f(broker):\n    while True:\n        await broker.connect()\n"
        "def g(m):\n    for _ in range(3):\n        m.connect()\n"
        "async def k(ib):\n    while True:\n        await ib.connectAsync()\n"
    )
    clean = (
        "def h(m):\n    m.connect()\n"
        "    for x in range(2):\n        def inner():\n            m.connect()\n"
        "    for t in range(2):\n        t.updateEvent.connect(print)\n"
    )
    assert _connects_inside_a_loop(flagged) == [3, 6, 9]
    assert _connects_inside_a_loop(clean) == []


def test_no_reconnect_loop_exists_in_the_broker_plane() -> None:
    """I5 stated structurally: no ``.connect()`` call inside a loop anywhere in the broker
    package, the runtime or the service package. Recovery after a drop is an operator restart
    (docs/IBKR_RUNBOOK.md §6); a future retry loop must change this pin on purpose."""

    offenders: list[str] = []
    for root in _BROKER_PLANE:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            for line in _connects_inside_a_loop(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(_REPO)}:{line}")
    assert files  # the scan read something
    assert offenders == []


# --- 11. B1R-F4: a successful two-step cancel never settles (reader build-ua P2-1) ----------


def _two_step_cancel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """S2's own lifecycle harness on the official venue: buy, confirm, submit, then cancel
    through the real OrderCancellationService. The fake answers PendingCancel THEN Cancelled
    (S1b r1; ASSUMED-IB, K4 #7)."""

    venue = _Venue(kind="official_fake", monkeypatch=monkeypatch, tmp_path=tmp_path)
    with harness(venue) as h:
        intent = h.buy(ref=_ref("A"), intent_id="intent-1")
        h.confirm(intent)
        if not h.submit(intent).submitted:
            raise RuntimeError("precondition: the submit did not go out")
        result = h.service.cancel("intent-1", now=h.now)
        yield h, result


def test_a_two_step_cancel_reaches_the_venue_and_the_order_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sibling of B1R-F4, with S2's first-ack assertions relaxed: everything up to the
    reconciliation is right on the official venue."""

    for h, result in _two_step_cancel(monkeypatch, tmp_path):
        assert result.requested is True
        assert result.lifecycle in {OrderLifecycle.CANCEL_PENDING, OrderLifecycle.CANCELLED}
        assert h.sim_open_order_ids() == set()  # the venue dropped the order
        assert h.venue_lifecycle(h.first_id) is OrderLifecycle.CANCELLED
        h.bar(**CROSSING)
        assert h.connection.run(h.broker.executions()) == ()  # a cancelled order never fills
        assert h.sim_position() == 0


def test_a_two_step_cancel_settles_after_reconciliation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for h, _result in _two_step_cancel(monkeypatch, tmp_path):
        if h.sim_open_order_ids() != set():  # preconditions raise, never AssertionError
            raise RuntimeError("precondition: the venue still holds the order")
        assert h.service.reconcile_on_restart_report(now=h.now).remaining_active == ()
