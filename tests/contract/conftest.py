"""Harness registry and helpers for the seam-A ``Broker`` contract (SIM-HARNESS U1).

The contract (``test_broker_contract.py``) is written once and run against every
registered implementation of :class:`chronos.broker.base.Broker`'s order-plane
and read surface. Tests only: nothing here changes ``src`` and nothing here can
reach a venue.

Registered implementations
--------------------------
* ``demo``  — :class:`chronos.broker.demo.DemoBroker` (display-only; refuses orders).
* ``fake``  — :class:`ContractFakeBroker` (isolated subclass of the pipeline double).
* ``sim``   — U2's ``SimBroker``. **Hook:** when ``tests/support/sim_broker.py`` exists
  and exposes ``contract_harness() -> BrokerHarness`` (named ``"sim"``), it is
  registered automatically and every case below runs against it. Until then
  nothing is registered, so no case is skipped. Import ``BrokerHarness`` lazily,
  inside ``contract_harness()``: this module imports the hook module while it is
  itself still importing. Give the harness a ``cause_fill`` hook and the fill case
  runs; entries for ``sim`` in :data:`KNOWN_VIOLATIONS` are not expected.

* ``official_fake`` — S1b: the REAL :class:`chronos.broker.official_ibkr.OfficialIBKRBroker`
  (unmodified ``_App``, ``CallbackBridge`` and normalizers) over ``tests/support/fake_ibapi.py``'s
  FakeGateway, an in-process TWS backed by SimBroker. **Hook:** registered when that module
  exists; its gateway is installed per case by :func:`_official_fake_gateway` (patched loaders,
  socket tripwire, ``ibapi`` never imported). ``ALLOW_*`` stay false. The cases that send a
  transmitting order through the adapter are **deferred to S2** by declaration
  (:data:`DEFERRED`, ``BrokerHarness.submit_deferred``), not skipped, for the same reason as
  :data:`NOT_RUN` below.

Declared, NOT RUN
-----------------
* ``official_ibkr`` — :class:`chronos.broker.official_ibkr.OfficialIBKRBroker` against a REAL
  gateway. Never constructed or imported here
  (``test_official_ibkr_is_declared_and_never_constructed`` pins that); the adapter itself runs
  as ``official_fake`` above. It is a
  declaration in :data:`NOT_RUN`, not a skipped test, because the S0 gate's
  coverage claim depends on the suite containing exactly its two by-design skips.

Known violations
----------------
Where a registered implementation genuinely departs from the contract, the
(harness, case) pair is listed in :data:`KNOWN_VIOLATIONS` with a finding id and
becomes a **strict xfail** that only absorbs an ``AssertionError``. Strict means
that the day an implementation is fixed the case XPASSes, fails the suite, and
forces the entry to be deleted. No ``src`` is changed to make a case pass.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import pytest

from chronos.broker.base import BrokerRefusedBeforeSend, BrokerSendGuard
from chronos.broker.demo import DEMO_ACCOUNT_ID, DemoBroker
from chronos.domain.enums import ConnectionState, OrderIntent, OrderLifecycle, OrderSide
from chronos.domain.models import (
    AccountSummary,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    CancellationResult,
    ConnectionStatus,
    OrderModification,
    OrderPreview,
    OrderRequest,
    OrderSubmission,
)
from tests.support.order_fakes import (
    PAPER_ACCOUNT,
    FakeBroker,
    option_contract,
    stock_contract,
)

FOREIGN_ACCOUNT = "DU7654321"
UNKNOWN_ORDER_ID = 987_654_321


class OrderPlaneBroker(Protocol):
    """The slice of ``chronos.broker.base.Broker`` this contract exercises.

    ``FakeBroker`` is deliberately partial (no quote or qualification methods), so
    the contract is stated over the read + order-plane surface every candidate
    shares. ``BrokerHarness.implements_full_protocol`` records which candidates
    also satisfy the whole runtime-checkable ``Broker`` protocol.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def connection_status(self) -> ConnectionStatus: ...

    async def server_time(self) -> datetime: ...

    async def account_summary(self) -> AccountSummary: ...

    async def positions(self) -> tuple[BrokerPosition, ...]: ...

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]: ...

    async def open_orders(self) -> tuple[BrokerOrder, ...]: ...

    async def preview_order(self, request: OrderRequest) -> OrderPreview: ...

    async def submit_order(
        self, request: OrderRequest, *, send_guard: BrokerSendGuard | None = None
    ) -> OrderSubmission: ...

    async def modify_order(self, request: OrderModification) -> OrderSubmission: ...

    async def cancel_order(self, broker_order_id: int) -> CancellationResult: ...


#: Signature of the optional fill hook: make the broker fill ``quantity`` of the
#: working order ``broker_order_id`` at its limit price. A venue-free simulator
#: provides one; the canned test double and the display-only demo broker cannot.
FillHook = Callable[[OrderPlaneBroker, int, Decimal], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BrokerHarness:
    """One implementation under contract, plus what the cases need to know about it."""

    name: str
    #: Builds a fresh, seeded, UNCONNECTED broker. Never touches a network.
    build: Callable[[], OrderPlaneBroker]
    #: The account every seeded fact belongs to (and every valid request targets).
    account_id: str
    #: A seeded, working, Chronos-owned (``CHR-``) order that can be cancelled.
    owned_order_id: int
    #: True when ``submit_order`` may return a submission (a simulator or test
    #: double); False for an adapter that must refuse every submit.
    accepts_orders: bool
    #: True when ``isinstance(broker, Broker)`` must hold (the whole protocol).
    implements_full_protocol: bool
    #: A seeded working order NOT owned by Chronos, if the seed has one.
    foreign_order_id: int | None = None
    cause_fill: FillHook | None = None
    #: Why this harness's submit path is not exercised here (None: it is). A harness with a
    #: reason joins neither the accepting nor the refusing fixtures, by declaration.
    submit_deferred: str | None = None


class OfficialBrokerNotRun(RuntimeError):
    """The OfficialIBKRBroker harness is declared but must never be built in U1."""


def official_ibkr_harness() -> BrokerHarness:
    """NOT RUN placeholder for ``OfficialIBKRBroker`` against a REAL gateway.

    S1b runs the adapter over the FakeGateway as ``official_fake`` (see the module docstring);
    a real gateway is never constructed here, and B1 is the owner's.
    """

    raise OfficialBrokerNotRun(
        "OfficialIBKRBroker needs a gateway or a fake ibapi; U1 declares it and never builds it"
    )


NOT_RUN: dict[str, str] = {
    "official_ibkr": (
        "OfficialIBKRBroker against a real gateway: never constructed here (the adapter runs "
        "as official_fake over the FakeGateway)"
    )
}

#: The FakeGateway harness's module (registered when present).
_FAKE_GATEWAY_MODULE = "tests.support.fake_ibapi"

#: (harness, case) -> what a PASS there proves: the adapter against the FakeGateway's own
#: convention only, not against IB (fable r1 P2-1). Labels, not xfails; each names a K4 check.
FAKE_PINNED: dict[tuple[str, str], str] = {
    ("official_fake", "test_executions_since_is_an_inclusive_lower_bound"): (
        "proves the adapter against the fake's UTC execution-time convention only; candidate "
        "S1-F3 (K4): the zone of Execution.time and of the ExecutionFilter bound on a real "
        "gateway (the adapter assumes UTC on both sides)"
    ),
}

#: Harness name -> why its submit path is deferred (declaration, not a skip).
DEFERRED: dict[str, str] = {
    "official_fake": (
        "S2: sending a transmitting order through the real adapter (even to the in-process "
        "FakeGateway) is the next slice, on Kevin's word; S1b keeps ALLOW_* false and runs "
        "every refusal case"
    )
}


# ---------------------------------------------------------------------------
# The two implementations that exist today.
# ---------------------------------------------------------------------------


def _demo_harness() -> BrokerHarness:
    return BrokerHarness(
        name="demo",
        build=DemoBroker,
        account_id=DEMO_ACCOUNT_ID,
        owned_order_id=7001,  # the partial TSLA put, order_ref CHR-DEMO-PARTIAL
        accepts_orders=False,
        implements_full_protocol=True,
    )


_FAKE_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
_FAKE_OWNED_ID = 7001
_FAKE_FOREIGN_ID = 7002


class ContractFakeBroker(FakeBroker):
    """Stateful, venue-free contract harness; the shared pipeline fake stays canned.

    Fills are explicit test events at the fixed clock and limit price, with zero
    commission: no price discovery, slippage, liquidity, timing or fee model.
    Each fill overwrites average_cost and market_price with its price; there is
    no weighted cost basis or realized/unrealized P&L. Only total_cash moves;
    net_liquidation and buying_power stay at their canned values (no margin or
    valuation model). An unknown fill order id raises StopIteration inside the
    hook (surfaced as RuntimeError by the coroutine), not a typed broker refusal.
    This is not a market simulator or proof of real venue behavior. Cancellation
    ownership uses seeded account/client/ref checks, not the adapter's persisted
    ownership ledger or permanent-id proof.
    """

    _connected = False
    _cash_delta = Decimal("0")

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerRefusedBeforeSend("contract fake is disconnected")

    def _require_account(self, account_id: str) -> None:
        self._require_connected()
        if account_id != self.account_id:
            raise BrokerRefusedBeforeSend("foreign account")

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def connection_status(self) -> ConnectionStatus:
        status = await super().connection_status()
        return status.model_copy(
            update={
                "connected": self._connected,
                "state": ConnectionState.CONNECTED
                if self._connected
                else ConnectionState.DISCONNECTED,
            }
        )

    async def server_time(self) -> datetime:
        self._require_connected()
        return await super().server_time()

    async def account_summary(self) -> AccountSummary:
        self._require_connected()
        summary = await super().account_summary()
        return summary.model_copy(update={"total_cash": summary.total_cash + self._cash_delta})

    async def positions(self) -> tuple[BrokerPosition, ...]:
        self._require_connected()
        return self._positions

    async def executions(self, since: datetime | None = None) -> tuple[BrokerExecution, ...]:
        self._require_connected()
        return tuple(e for e in self._executions if since is None or e.timestamp >= since)

    async def open_orders(self) -> tuple[BrokerOrder, ...]:
        self._require_connected()
        return self._open_orders

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        self._require_account(request.account_id)
        if request.transmit:
            raise BrokerRefusedBeforeSend("preview cannot transmit")
        return await super().preview_order(request)

    async def submit_order(
        self, request: OrderRequest, *, send_guard: BrokerSendGuard | None = None
    ) -> OrderSubmission:
        self._require_account(request.account_id)
        if not request.transmit:
            raise BrokerRefusedBeforeSend("submit requires transmit authority")
        submission = await super().submit_order(request, send_guard=send_guard)
        self._open_orders += (
            BrokerOrder(
                broker_order_id=submission.broker_order_id,
                permanent_id=submission.permanent_id,
                client_id=submission.client_id,
                account_id=request.account_id,
                order_ref=request.order_ref,
                contract=request.contract,
                side=request.side,
                quantity=request.quantity,
                filled_quantity=Decimal("0"),
                remaining_quantity=request.quantity,
                limit_price=request.limit_price,
                lifecycle=submission.lifecycle,
            ),
        )
        return submission

    async def modify_order(self, request: OrderModification) -> OrderSubmission:
        raise BrokerRefusedBeforeSend("cancel and resubmit; modify is unsupported")

    async def cancel_order(self, broker_order_id: int) -> CancellationResult:
        self._require_connected()
        order = next((o for o in self._open_orders if o.broker_order_id == broker_order_id), None)
        if order is None:
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=OrderLifecycle.REJECTED,
                message="no such open order; no cancel was sent",
                timestamp=_FAKE_NOW,
            )
        if (
            order.account_id != self.account_id
            or order.client_id != 17
            or not order.order_ref.startswith("CHR-")
        ):
            return CancellationResult(
                broker_order_id=broker_order_id,
                requested=False,
                lifecycle=OrderLifecycle.REJECTED,
                message="not a Chronos order; refusing to cancel it; no cancel was sent",
                timestamp=_FAKE_NOW,
            )
        result = await super().cancel_order(broker_order_id)
        self._open_orders = tuple(
            o for o in self._open_orders if o.broker_order_id != broker_order_id
        )
        return result

    async def fill(self, broker_order_id: int, quantity: Decimal) -> None:
        """Apply one bounded execution to a working order, position and cash."""
        self._require_connected()
        order = next(o for o in self._open_orders if o.broker_order_id == broker_order_id)
        if not quantity.is_finite() or not Decimal("0") < quantity <= order.remaining_quantity:
            raise ValueError("fill quantity must be positive and no larger than the remainder")
        price = order.limit_price
        assert price is not None
        signed = quantity if order.side is OrderSide.BUY else -quantity
        multiplier = getattr(order.contract, "multiplier", Decimal("1"))
        self._cash_delta -= signed * price * multiplier
        old = next((p for p in self._positions if p.contract == order.contract), None)
        new_quantity = (old.quantity if old else Decimal("0")) + signed
        position = BrokerPosition(
            account_id=order.account_id,
            contract=order.contract,
            quantity=new_quantity,
            average_cost=price,
            market_price=price,
        )
        self._positions = tuple(p for p in self._positions if p.contract != order.contract)
        if new_quantity:
            self._positions += (position,)
        self._executions += (
            BrokerExecution(
                execution_id=f"CONTRACT-FILL-{len(self._executions) + 1}",
                account_id=order.account_id,
                broker_order_id=broker_order_id,
                permanent_id=order.permanent_id,
                client_id=order.client_id,
                order_ref=order.order_ref,
                contract=order.contract,
                side=order.side,
                quantity=quantity,
                price=price,
                timestamp=_FAKE_NOW,
                commission=Decimal("0"),
                commission_currency="USD",
            ),
        )
        remaining = order.remaining_quantity - quantity
        self._open_orders = tuple(
            o for o in self._open_orders if o.broker_order_id != broker_order_id
        )
        if remaining:
            self._open_orders += (
                order.model_copy(
                    update={
                        "filled_quantity": order.filled_quantity + quantity,
                        "remaining_quantity": remaining,
                        "lifecycle": OrderLifecycle.PARTIALLY_FILLED,
                    }
                ),
            )


async def _fill_fake(broker: OrderPlaneBroker, order_id: int, quantity: Decimal) -> None:
    assert isinstance(broker, ContractFakeBroker)
    await broker.fill(order_id, quantity)


def _build_fake() -> ContractFakeBroker:
    """A FakeBroker seeded with the same shape of evidence the demo broker carries."""

    contract = option_contract()
    owned = BrokerOrder(
        broker_order_id=_FAKE_OWNED_ID,
        permanent_id=9001,
        client_id=17,
        account_id=PAPER_ACCOUNT,
        order_ref="CHR-CONTRACT-OWNED",
        contract=contract,
        side=OrderSide.SELL,
        quantity=Decimal("2"),
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("1"),
        limit_price=Decimal("1.25"),
        lifecycle=OrderLifecycle.PARTIALLY_FILLED,
    )
    foreign = BrokerOrder(
        broker_order_id=_FAKE_FOREIGN_ID,
        permanent_id=9002,
        client_id=99,
        account_id=PAPER_ACCOUNT,
        order_ref="MANUAL-1",
        contract=contract,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        limit_price=Decimal("0.50"),
        lifecycle=OrderLifecycle.SUBMITTED,
    )
    execution = BrokerExecution(
        execution_id="FAKE-EXEC-0001",
        account_id=PAPER_ACCOUNT,
        broker_order_id=_FAKE_OWNED_ID,
        permanent_id=9001,
        client_id=17,
        order_ref=owned.order_ref,
        contract=contract,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("1.25"),
        timestamp=_FAKE_NOW - timedelta(minutes=3),
        commission=Decimal("0.65"),
        commission_currency="USD",
    )
    position = BrokerPosition(
        account_id=PAPER_ACCOUNT,
        contract=stock_contract(),
        quantity=Decimal("100"),
        average_cost=Decimal("150.00"),
        market_price=Decimal("151.00"),
    )
    return ContractFakeBroker(
        positions=(position,),
        open_orders=(owned, foreign),
        executions=(execution,),
        next_broker_order_id=9101,
    )


def _fake_harness() -> BrokerHarness:
    return BrokerHarness(
        name="fake",
        build=_build_fake,
        account_id=PAPER_ACCOUNT,
        owned_order_id=_FAKE_OWNED_ID,
        accepts_orders=True,
        implements_full_protocol=False,
        foreign_order_id=_FAKE_FOREIGN_ID,
        cause_fill=_fill_fake,
    )


def _load_registry() -> dict[str, BrokerHarness]:
    harnesses = [_demo_harness(), _fake_harness()]
    # U2 hook: tests/support/sim_broker.py exposing contract_harness() -> BrokerHarness.
    if importlib.util.find_spec("tests.support.sim_broker") is not None:
        module = importlib.import_module("tests.support.sim_broker")
        harnesses.append(module.contract_harness())
    # S1b hook: the real OfficialIBKRBroker over the FakeGateway.
    if importlib.util.find_spec(_FAKE_GATEWAY_MODULE) is not None:
        module = importlib.import_module(_FAKE_GATEWAY_MODULE)
        harness = module.contract_harness()
        harnesses.append(dataclasses.replace(harness, submit_deferred=DEFERRED[harness.name]))
    return {harness.name: harness for harness in harnesses}


REGISTRY: dict[str, BrokerHarness] = _load_registry()


# ---------------------------------------------------------------------------
# Findings and the strict-xfail table.
# ---------------------------------------------------------------------------

FINDINGS: dict[str, str] = {
    "U1-F1": (
        "DemoBroker.cancel_order reports CANCELLED but leaves the order in open_orders "
        "(src/chronos/broker/demo.py:483-509 never touches self._orders). Demo is "
        "display-only; a fix is a separate one-line src unit for Kevin to rule on "
        "(SURVEY G2)."
    ),
}

#: (harness name, test function name) -> finding id. Each entry is a STRICT xfail.
KNOWN_VIOLATIONS: dict[tuple[str, str], str] = {
    ("demo", "test_cancel_removes_a_cancelled_order_from_open_orders"): "U1-F1",
}


def _harness_name(request: pytest.FixtureRequest) -> str | None:
    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return None
    names = [value for value in callspec.params.values() if isinstance(value, str)]
    return next((name for name in names if name in REGISTRY), None)


@pytest.fixture(autouse=True)
def _official_fake_gateway(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """For ``official_fake`` cases only: a fresh FakeGateway (one TWS) installed for this case.

    Installed before the case body, so before ``build()`` (the adapter's constructor already
    calls the loader). ``monkeypatch`` restores the loaders and sockets afterwards.
    """

    if _harness_name(request) != "official_fake":
        return
    module = importlib.import_module(_FAKE_GATEWAY_MODULE)
    module.install(monkeypatch, module.FakeGateway.seeded())


@pytest.fixture(autouse=True)
def _known_violation(request: pytest.FixtureRequest) -> None:
    """Turn a registered (harness, case) pair into a strict xfail on assertions only."""

    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return
    names = [value for value in callspec.params.values() if isinstance(value, str)]
    harness_name = next((name for name in names if name in REGISTRY), None)
    if harness_name is None:
        return
    finding = KNOWN_VIOLATIONS.get((harness_name, request.node.originalname))
    if finding is None:
        return
    request.applymarker(
        pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=f"{finding}: {FINDINGS[finding]}",
        )
    )


# ---------------------------------------------------------------------------
# Parametrized harness fixtures. Narrower sets are derived from the registry, so a
# new implementation joins every case it is eligible for without edits here.
# ---------------------------------------------------------------------------


def _names(select: Callable[[BrokerHarness], bool]) -> list[str]:
    return [name for name, harness in REGISTRY.items() if select(harness)]


@pytest.fixture(params=_names(lambda h: True))
def harness(request: pytest.FixtureRequest) -> BrokerHarness:
    return REGISTRY[request.param]


@pytest.fixture(params=_names(lambda h: h.accepts_orders and h.submit_deferred is None))
def accepting_harness(request: pytest.FixtureRequest) -> BrokerHarness:
    return REGISTRY[request.param]


@pytest.fixture(params=_names(lambda h: not h.accepts_orders and h.submit_deferred is None))
def refusing_harness(request: pytest.FixtureRequest) -> BrokerHarness:
    return REGISTRY[request.param]


@pytest.fixture(params=_names(lambda h: h.foreign_order_id is not None))
def foreign_harness(request: pytest.FixtureRequest) -> BrokerHarness:
    return REGISTRY[request.param]


# ---------------------------------------------------------------------------
# Helpers shared by the cases.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def connected(harness: BrokerHarness) -> AsyncIterator[OrderPlaneBroker]:
    """A fresh, seeded, connected broker for one case; disconnected afterwards."""

    broker = harness.build()
    await broker.connect()
    try:
        yield broker
    finally:
        await broker.disconnect()


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a call did: a value, or the exception it raised."""

    value: Any
    error: Exception | None


async def attempt(call: Awaitable[Any]) -> Outcome:
    """Await ``call`` and capture an ordinary exception instead of propagating it.

    Contract cases assert on the captured outcome with plain ``assert`` so that a
    violation is an ``AssertionError`` (what the strict xfail absorbs), while a bug
    in the harness itself still surfaces as its own error.
    """

    try:
        return Outcome(value=await call, error=None)
    except Exception as error:
        return Outcome(value=None, error=error)


@dataclass(frozen=True, slots=True)
class Book:
    """Everything an order call could change, for before/after equality."""

    positions: tuple[BrokerPosition, ...]
    executions: tuple[BrokerExecution, ...]
    open_orders: tuple[BrokerOrder, ...]
    cash: tuple[Decimal, Decimal, Decimal]


async def book(broker: OrderPlaneBroker) -> Book:
    summary = await broker.account_summary()
    return Book(
        positions=await broker.positions(),
        executions=await broker.executions(),
        open_orders=await broker.open_orders(),
        cash=(summary.total_cash, summary.net_liquidation, summary.buying_power),
    )


class CountingGuard:
    """A ``BrokerSendGuard`` that counts entries and answers a fixed authorization."""

    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized
        self.entries = 0

    @contextmanager
    def at_send(self) -> Iterator[bool]:
        self.entries += 1
        yield self.authorized


def order_request(
    harness: BrokerHarness,
    *,
    transmit: bool,
    correlation_id: str = "contract-1",
    account_id: str | None = None,
    order_ref: str = "CHR-CONTRACT-1",
) -> OrderRequest:
    """A well-formed, Chronos-owned limit order for the harness's account."""

    return OrderRequest(
        correlation_id=correlation_id,
        account_id=account_id if account_id is not None else harness.account_id,
        contract=option_contract(),
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("1.25"),
        order_ref=order_ref,
        transmit=transmit,
    )


def modification(broker_order_id: int) -> OrderModification:
    return OrderModification(
        correlation_id="contract-modify-1",
        broker_order_id=broker_order_id,
        new_limit_price=Decimal("1.10"),
    )
