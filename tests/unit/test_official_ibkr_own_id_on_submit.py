"""S1-F2: an order id is recorded as this process's own ONLY when it is submitted.

``CallbackBridge.start_order_ack`` used to record the id as own (the RC-1 classifier's memory of
what this adapter submitted) for every caller. Preview (a ``whatIf`` probe) and cancel use it
too, so an id they merely touched was remembered as submitted: a later execution on a foreign
or stale order with that id and this client id would read as OWN and skip the unsolicited-
execution invalidation.

These tests need no FakeGateway: the real ``CallbackBridge`` plus the same ``object.__new__``
adapter shell the neighbouring ``test_official_ibkr.py`` submit tests use.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.test_official_ibkr import NOW, _FakeContract, _FakeOrder

from chronos.broker import official_ibkr as official_ibkr_module
from chronos.broker.base import BrokerRefusedBeforeSend
from chronos.broker.callbacks import CallbackBridge
from chronos.broker.official_ibkr import OfficialIBKRBroker
from chronos.broker.order_ids import OrderIdAllocator
from chronos.broker.request_registry import RequestRegistry
from chronos.domain.enums import IBEnvironment, OrderIntent, OrderSide
from chronos.domain.models import CryptoContract, OrderRequest
from chronos.orders.kill_switch import LiveKillSwitch

_CLIENT = 17
_ACCOUNT = "DU7654321"


class _Allow:
    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield True


class _Deny:
    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield False


def _bridge() -> CallbackBridge:
    # an invalidator must be wired: with none, observe_execution returns False for EVERYTHING
    # (nothing to invalidate), which would make every id read as own
    return CallbackBridge(
        RequestRegistry(), on_connection_uncertain=lambda reason: None, client_id=_CLIENT
    )


def _reads_as_own(bridge: CallbackBridge, order_id: int) -> bool:
    """True when an execution for (this client, order_id) is NOT classified unsolicited.

    ``observe_execution`` returns True when it invalidated readiness (unsolicited) and False
    when the pair is own.
    """

    return bridge.unsolicited.observe_execution(client_id=_CLIENT, order_id=order_id) is False


class _ReplyingApp:
    """Connected app whose order calls answer the way the gateway does, and note ownership."""

    def __init__(self, bridge: CallbackBridge) -> None:
        self.bridge = bridge
        self.sent: list[tuple[str, int]] = []
        self.own_when_sent: dict[int, bool] = {}

    def isConnected(self) -> bool:
        return True

    def placeOrder(self, order_id: int, contract: object, order: object) -> None:
        del contract
        self.sent.append(("placeOrder", order_id))
        self.own_when_sent[order_id] = _reads_as_own(self.bridge, order_id)
        if getattr(order, "whatIf", False):
            state = SimpleNamespace(status="PreSubmitted")
            self.bridge.on_open_order(order_id, None, order, state)
        else:
            self.bridge.on_order_status(order_id, "Submitted", 0.0, 1.0, 0.0, 0)

    def cancelOrder(self, order_id: int, manual_time: str) -> None:
        del manual_time
        self.sent.append(("cancelOrder", order_id))
        self.bridge.on_order_status(order_id, "Cancelled", 0.0, 0.0, 0.0, 0)


def _shell(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_id: int,
    kill_switch: LiveKillSwitch | None = None,
) -> tuple[OfficialIBKRBroker, _ReplyingApp]:
    monkeypatch.setattr(
        official_ibkr_module, "_load_ibapi", lambda: (None, None, _FakeContract, None)
    )
    monkeypatch.setattr(official_ibkr_module, "_load_order_class", lambda: _FakeOrder)
    broker = object.__new__(OfficialIBKRBroker)
    broker._settings = SimpleNamespace(  # type: ignore[assignment]
        ib_environment=IBEnvironment.PAPER,
        ib_port=7497,
        ib_account_id=_ACCOUNT,
        allow_outside_rth=False,
        ib_client_id=_CLIENT,
    )
    broker._live_kill_switch = kill_switch
    # the S1-F1 cancel proof reads this attribute; None = no ledger = the cancel is refused.
    # Unused on a base without S1-F1, so this shell holds on both sides of that change.
    broker._order_ownership = None
    broker.bridge = _bridge()
    broker.bridge.managed_accounts = (_ACCOUNT,)
    broker.order_ids = OrderIdAllocator()
    broker.order_ids.seed(first_id)
    app = _ReplyingApp(broker.bridge)
    broker._app = app
    return broker, app


def _request(letter: str, *, transmit: bool) -> OrderRequest:
    ref = "CHR-ORD-" + letter * 32
    return OrderRequest(
        correlation_id=ref,
        account_id=_ACCOUNT,
        contract=CryptoContract(con_id=1, symbol="ETH"),
        intent=OrderIntent.OPEN_LONG_CRYPTO,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        limit_price=Decimal("3000"),
        order_ref=ref,
        transmit=transmit,
    )


# -- the bridge ------------------------------------------------------------------------------ #


def test_registering_an_ack_flight_does_not_record_ownership() -> None:
    bridge = _bridge()

    bridge.start_order_ack(4242)
    bridge.clear_order_ack(4242)

    assert not _reads_as_own(bridge, 4242)


def test_note_own_order_records_ownership_and_only_that_id() -> None:
    bridge = _bridge()

    bridge.note_own_order(4242)

    assert _reads_as_own(bridge, 4242)
    assert not _reads_as_own(bridge, 4243)


# -- the adapter ----------------------------------------------------------------------------- #


def test_a_preview_does_not_record_its_whatif_id_as_own(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, app = _shell(monkeypatch, first_id=9300)

    preview = asyncio.run(broker.preview_order(_request("P", transmit=False)))

    assert preview.accepted is True
    assert app.sent == [("placeOrder", 9300)]  # the whatIf really went out
    assert app.own_when_sent[9300] is False
    assert not _reads_as_own(broker.bridge, 9300)


def test_a_cancel_does_not_record_the_cancelled_id_as_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the S1-F2 property: whatever the cancel path decides, the id it touched is not own.

    Deliberately silent on whether the cancel is sent or refused and on what it reports; that is
    S1-F1's rule (the ownership proof), and this test must hold before and after it lands.
    """

    broker, _ = _shell(monkeypatch, first_id=9400)

    with contextlib.suppress(BrokerRefusedBeforeSend):  # S1-F1 refuses an unproven cancel
        asyncio.run(broker.cancel_order(777))

    assert not _reads_as_own(broker.bridge, 777)


def test_a_submit_records_its_id_as_own_before_the_send(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, app = _shell(monkeypatch, first_id=9500)

    submission = asyncio.run(broker.submit_order(_request("S", transmit=True), send_guard=_Allow()))

    assert submission.broker_order_id == 9500
    assert app.sent == [("placeOrder", 9500)]
    # already own at the instant placeOrder ran: an immediate fill cannot race the record
    assert app.own_when_sent[9500] is True
    assert _reads_as_own(broker.bridge, 9500)
    assert not _reads_as_own(broker.bridge, 9501)


def test_a_submit_refused_before_send_records_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    switch = LiveKillSwitch(tmp_path / "ks.json")
    broker, app = _shell(monkeypatch, first_id=9600, kill_switch=switch)
    original_build = broker._build_order_objects

    def build_then_engage(request: OrderRequest, *, what_if: bool) -> tuple[object, object]:
        built = original_build(request, what_if=what_if)
        switch.engage(reason="operator halt", initiated_by="op", now=NOW)
        return built

    monkeypatch.setattr(broker, "_build_order_objects", build_then_engage)

    with pytest.raises(BrokerRefusedBeforeSend, match="kill switch engaged"):
        asyncio.run(broker.submit_order(_request("K", transmit=True), send_guard=_Allow()))

    assert app.sent == []
    assert not _reads_as_own(broker.bridge, 9600)


def test_a_submit_refused_by_the_send_guard_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, app = _shell(monkeypatch, first_id=9700)

    with pytest.raises(BrokerRefusedBeforeSend, match="authorization changed"):
        asyncio.run(broker.submit_order(_request("D", transmit=True), send_guard=_Deny()))

    assert app.sent == []
    assert not _reads_as_own(broker.bridge, 9700)


# -- the call-site invariant, structurally ----------------------------------------------------- #

_SRC = Path(__file__).resolve().parents[2] / "src" / "chronos"


def _calls_of(attribute: str) -> list[tuple[str, str, ast.Call, list[ast.AST]]]:
    """Every production call ``<x>.<attribute>(...)``: (file, enclosing def, node, parents)."""

    found: list[tuple[str, str, ast.Call, list[ast.AST]]] = []

    def visit(node: ast.AST, path: str, scope: list[str], parents: list[ast.AST]) -> None:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            scope = [*scope, node.name]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute
        ):
            found.append((path, ".".join(scope), node, parents))
        for child in ast.iter_child_nodes(node):
            visit(child, path, scope, [*parents, node])

    for file in sorted(_SRC.rglob("*.py")):
        visit(
            ast.parse(file.read_text(encoding="utf-8")), file.relative_to(_SRC).as_posix(), [], []
        )
    return found


def test_ownership_is_recorded_from_exactly_one_production_call_site() -> None:
    sites = {(path, scope) for path, scope, _, _ in _calls_of("note_own_order")}

    assert sites == {
        # the bridge method that delegates to the classifier ...
        ("broker/callbacks.py", "CallbackBridge.note_own_order"),
        # ... and the one adapter call: the real submit
        ("broker/official_ibkr.py", "OfficialIBKRBroker.submit_order"),
    }


def test_the_submit_records_inside_both_pre_send_guards_and_before_placeorder() -> None:
    (record,) = [
        site for site in _calls_of("note_own_order") if site[1] == "OfficialIBKRBroker.submit_order"
    ]
    (send,) = [
        site for site in _calls_of("placeOrder") if site[1] == "OfficialIBKRBroker.submit_order"
    ]
    _, _, record_call, record_parents = record
    _, _, send_call, _ = send

    assert ast.unparse(record_call.func) == "self.bridge.note_own_order"  # the bridge method
    guards = [node for node in record_parents if isinstance(node, ast.With)]
    assert len(guards) >= 2, "the record must sit inside the send guard AND the kill guard"
    assert record_call.lineno < send_call.lineno
    # and after the kill guard's own refusal, not merely inside its block
    refusals = [
        node.lineno
        for node in ast.walk(guards[-1])
        if isinstance(node, ast.Raise) and "kill switch engaged" in ast.unparse(node)
    ]
    assert refusals
    assert max(refusals) < record_call.lineno
