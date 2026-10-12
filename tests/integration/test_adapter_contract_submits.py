"""S2: S1b's deferred submit-path contract cases, run for the REAL adapter over the FakeGateway.

S1b registered the real ``OfficialIBKRBroker`` in the U1 contract suite as ``official_fake``
but declared its submit path deferred (``tests/contract/conftest.py``, ``DEFERRED``), because
sending a transmitting order through the adapter needed Kevin's word. Round 3 (~01:28 EDT
2026-10-12) gives it, ONLY for a test-only ``Settings`` object built here, against the
in-process FakeGateway, with every socket forbidden by the installer. Nothing in
``tests/contract`` changes (S1b r1 owns it): the four accepting-harness cases are imported
and called with a harness whose ``build`` uses that object and whose deferral is lifted.

The fifth deferred name, ``test_a_refusing_adapter_never_enters_the_send_guard``, is the
refusing-harness case: the official adapter accepts orders, so it never applied to it (S1b
said so); :func:`test_the_refusing_case_does_not_apply_to_an_accepting_adapter` pins why.
"""

from __future__ import annotations

import dataclasses
import os
from decimal import Decimal

import pytest
from tests.contract import test_broker_contract as contract
from tests.contract.conftest import BrokerHarness, CountingGuard, book, connected, order_request
from tests.support import fake_ibapi

from chronos.broker import official_ibkr
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.config.settings import Settings, get_settings
from chronos.domain.enums import OrderLifecycle, OrderSide
from chronos.domain.models import OptionContract

#: The adapter's acknowledgement wait; the fake answers synchronously (test-only patch).
ACK_TIMEOUT_S = 2.0


def s2_settings() -> Settings:
    """S1b's ``official_settings()`` with ``allow_order_transmit=True`` -- the ONLY such object.

    ``ALLOW_LIVE_TRADING`` stays false; paper environment on the paper port; the FakeGateway's
    seed account and client id. Never written to the environment, a ``.env`` or any config.
    """

    base = fake_ibapi.official_settings()
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        broker_mode=base.broker_mode,
        broker_adapter=base.broker_adapter,
        ib_environment=base.ib_environment,
        ib_host=base.ib_host,
        ib_port=base.ib_port,
        ib_client_id=base.ib_client_id,
        ib_account_id=base.ib_account_id,
        allow_order_transmit=True,
        allow_live_trading=False,
    )


@pytest.fixture
def s2_harness(monkeypatch: pytest.MonkeyPatch) -> BrokerHarness:
    """The ``official_fake`` harness with its submit path un-deferred, over a fresh gateway."""

    gateway = fake_ibapi.FakeGateway.seeded()
    fake_ibapi.install(monkeypatch, gateway)  # patched loaders + the socket tripwire
    monkeypatch.setattr(official_ibkr, "_REQUEST_TIMEOUT_S", ACK_TIMEOUT_S)
    base = fake_ibapi.contract_harness()
    assert (
        base.submit_deferred is None
    )  # the deferral is applied by the contract conftest, not the hook
    harness: BrokerHarness = dataclasses.replace(
        base, build=lambda: OfficialIBKRBroker(s2_settings())
    )
    return harness


#: Spellings pydantic reads as False; the slice's run env pins ``ALLOW_*=false`` explicitly
#: (the standing stop), so the proof is "never true", not "absent".
_FALSE = {"", "0", "false", "f", "no", "n", "off"}


def _no_transmit_outside_the_test_object() -> None:
    assert os.environ.get("ALLOW_ORDER_TRANSMIT", "").strip().lower() in _FALSE
    assert os.environ.get("ALLOW_LIVE_TRADING", "").strip().lower() in _FALSE
    assert Settings(_env_file=None).transmission_possible is False  # type: ignore[call-arg]
    get_settings.cache_clear()
    try:
        assert get_settings().allow_order_transmit is False
        assert get_settings().allow_live_trading is False
    finally:
        get_settings.cache_clear()


# --- the four accepting-harness cases, as S1b deferred them --------------------------------


async def test_an_authorized_submit_enters_the_guard_once_and_returns_a_submission(
    s2_harness: BrokerHarness,
) -> None:
    _no_transmit_outside_the_test_object()
    await contract.test_an_authorized_submit_enters_the_guard_once_and_returns_a_submission(
        s2_harness
    )
    _no_transmit_outside_the_test_object()


async def test_submitted_order_ids_are_unique_and_increasing(s2_harness: BrokerHarness) -> None:
    await contract.test_submitted_order_ids_are_unique_and_increasing(s2_harness)


async def test_a_working_submission_appears_in_open_orders(s2_harness: BrokerHarness) -> None:
    await contract.test_a_working_submission_appears_in_open_orders(s2_harness)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "S2-A1 (artifact, not an adapter defect): U1's fill case compares "
        "p.contract == request.contract, but the adapter's normalizer rebuilds the option "
        "from IB's contract fields without underlying_con_id / deliverable_* (S1a reader "
        "P2-2); the sibling identity case below proves the position is real. A U1 edit to "
        "compare identity is the owner's call."
    ),
)
async def test_a_fill_moves_positions_executions_and_cash_consistently(
    s2_harness: BrokerHarness,
) -> None:
    await contract.test_a_fill_moves_positions_executions_and_cash_consistently(s2_harness)


async def test_a_fill_moves_the_position_by_contract_identity_through_the_real_adapter(
    s2_harness: BrokerHarness,
) -> None:
    """U1's fill case with the one assertion the adapter can honour: identity, not equality."""

    hook = s2_harness.cause_fill
    assert hook is not None
    request = order_request(s2_harness, transmit=True)
    assert request.side is OrderSide.SELL
    async with connected(s2_harness) as broker:
        submission = await broker.submit_order(request, send_guard=CountingGuard())
        assert submission.lifecycle in {OrderLifecycle.SUBMITTED, OrderLifecycle.PARTIALLY_FILLED}
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
    assert all(execution.price >= request.limit_price for execution in new)
    assert all(execution.commission is not None for execution in new)

    def identity(contract: object) -> tuple[object, ...] | None:
        # The seeded book also holds a stock position (AAPL, con_id 999): not this option.
        if not isinstance(contract, OptionContract):
            return None
        return (
            contract.con_id,
            contract.symbol,
            contract.right,
            contract.strike,
            contract.expiration,
        )

    wanted = identity(request.contract)
    assert wanted is not None

    def held(snapshot: object) -> Decimal:
        positions = snapshot.positions  # type: ignore[attr-defined]
        return sum(
            (p.quantity for p in positions if identity(p.contract) == wanted),
            Decimal(0),
        )

    assert held(after) - held(before) == -request.quantity, "the position did not move by the fill"
    assert after.cash[0] > before.cash[0], "selling did not credit cash"
    assert submission.broker_order_id not in {o.broker_order_id for o in after.open_orders}


# --- the fifth deferred name, and the scope ---------------------------------------------------


def test_the_refusing_case_does_not_apply_to_an_accepting_adapter() -> None:
    harness = fake_ibapi.contract_harness()
    assert harness.accepts_orders is True  # so the refusing-harness case was never its to run
    assert harness.name == "official_fake"


def test_transmit_true_exists_only_in_the_s2_settings_object() -> None:
    _no_transmit_outside_the_test_object()
    settings = s2_settings()
    assert settings.allow_order_transmit is True
    assert settings.allow_live_trading is False
    assert settings.transmission_possible is True
    get_settings.cache_clear()
    try:
        assert get_settings() is not settings
        assert get_settings().allow_order_transmit is False
    finally:
        get_settings.cache_clear()
    _no_transmit_outside_the_test_object()
