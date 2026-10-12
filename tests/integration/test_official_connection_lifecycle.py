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

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
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
    finally:
        manager.close()
    assert readiness.status is not ReconciliationStatus.RECONCILED
    assert "official IBKR connection closed" in readiness.reasons
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
    assert any(note.startswith(f"[{fake_ibapi.CODE_CLIENT_ID_IN_USE}]") for note in broker.bridge.notices)


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
        with pytest.raises(BrokerConnectionError, match="did not complete the connection handshake"):
            manager.connect()
        status = asyncio.run(broker.connection_status())
    finally:
        manager.close()
    assert (status.connected, status.account_id) == (False, None)
    assert "disconnect" in [name for _, name in gateway.transcript]
    assert not _reader_alive()
    assert readiness.status is not ReconciliationStatus.RECONCILED


def test_a_session_that_does_not_manage_the_configured_account_is_refused(
    gateway: fake_ibapi.FakeGateway,
) -> None:
    gateway.managed_accounts = (OTHER_PAPER,)
    broker = _hooked(_SpyReadiness())
    with pytest.raises(BrokerSafetyError, match="does not manage the configured account") as raised:
        asyncio.run(broker.connect())
    message = str(raised.value)
    assert "managed count=1" in message
    assert fake_ibapi.ACCOUNT not in message and OTHER_PAPER not in message  # ids never shown
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
        gateway.change_managed_accounts((OTHER_PAPER,))
        removed = asyncio.run(broker.connection_status())
    finally:
        asyncio.run(broker.disconnect())
    assert readiness.reasons.count("scope: official IBKR managed-account scope changed") == 2
    assert readiness.status is not ReconciliationStatus.RECONCILED
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
        "B1R-F3: a HOOKLESS OfficialIBKRBroker (the capture harness, capture_readonly.py:296-297) "
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
    finally:
        asyncio.run(broker.disconnect())
    assert f"official IBKR connectivity event {code}" in readiness.reasons
    assert readiness.status is not ReconciliationStatus.RECONCILED


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
        "B1R-F2 (R-ARITY, ASSUMED-IB): if the installed ibapi calls error(reqId, errorTime:int, "
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
    finally:
        asyncio.run(broker.disconnect())
    assert readiness.status is not ReconciliationStatus.RECONCILED


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
