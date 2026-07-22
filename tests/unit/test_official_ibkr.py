"""Official-adapter guard rails and pure normalizers (no ibapi required).

The official TWS API package is deliberately absent from CI and this
environment; these tests prove (1) everything import-safe stays import-safe,
(2) construction fails closed with actionable guidance when the package is
missing or the account config is unsafe, and (3) the normalizers translate
recorded callback payloads into domain models faithfully.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from chronos.broker import official_ibkr as official_ibkr_module
from chronos.broker.base import (
    BrokerConnectionError,
    BrokerDataError,
    BrokerError,
    BrokerRefusedBeforeSend,
    BrokerSafetyError,
)
from chronos.broker.callbacks import CallbackBridge, QuoteState
from chronos.broker.official_ibkr import (
    OfficialIBKRBroker,
    account_summary_from_rows,
    chain_parameters_from_row,
    execution_from_pair,
    instrument_from_contract,
    position_from_row,
    quote_from_state,
    verify_environment_port,
)
from chronos.broker.order_ids import OrderIdAllocator
from chronos.broker.request_registry import RequestRegistry
from chronos.config.settings import Settings
from chronos.domain.enums import (
    DataQuality,
    IBEnvironment,
    OptionRight,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
)
from chronos.domain.models import (
    CryptoContract,
    OptionContract,
    OrderRequest,
    UnderlyingContract,
)
from chronos.orders.kill_switch import LiveKillSwitch

NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)

_IBAPI_ABSENT = importlib.util.find_spec("ibapi") is None


def test_module_import_does_not_import_ibapi() -> None:
    # The module is already imported by this test file; the guarantee is that
    # importing it did NOT pull the official package into the process.
    assert "chronos.broker.official_ibkr" in sys.modules
    assert "ibapi" not in sys.modules
    assert "ibapi.client" not in sys.modules


@pytest.mark.skipif(not _IBAPI_ABSENT, reason="ibapi installed; guard covered elsewhere")
def test_construction_without_ibapi_gives_install_guidance() -> None:
    settings = Settings(_env_file=None, ib_account_id="DU111")
    with pytest.raises(BrokerError, match=r"docs/ibkr_setup\.md"):
        OfficialIBKRBroker(settings)


class TestEnvironmentPortConsistency:
    def test_paper_ports_accepted(self) -> None:
        verify_environment_port(IBEnvironment.PAPER, 7497)
        verify_environment_port(IBEnvironment.PAPER, 4002)

    def test_paper_config_on_live_port_refused(self) -> None:
        with pytest.raises(BrokerSafetyError, match="paper port"):
            verify_environment_port(IBEnvironment.PAPER, 7496)

    def test_live_config_on_paper_port_refused(self) -> None:
        with pytest.raises(BrokerSafetyError, match="live port"):
            verify_environment_port(IBEnvironment.LIVE, 7497)


class TestAccountSummaryNormalizer:
    def test_happy_path(self) -> None:
        rows = [
            ("DU111", "NetLiquidation", "25000.50", "USD"),
            ("DU111", "TotalCashValue", "12000.25", "USD"),
            ("DU111", "BuyingPower", "48000.00", "USD"),
        ]
        summary = account_summary_from_rows(rows, expected_account="DU111", as_of=NOW)
        assert summary.net_liquidation == Decimal("25000.50")
        assert summary.total_cash == Decimal("12000.25")
        assert summary.buying_power == Decimal("48000.00")
        assert summary.account_id == "DU111"

    def test_missing_tag_fails_closed(self) -> None:
        rows = [("DU111", "NetLiquidation", "25000", "USD")]
        with pytest.raises(BrokerDataError, match="missing tags"):
            account_summary_from_rows(rows, expected_account="DU111", as_of=NOW)

    def test_unparseable_value_fails_closed(self) -> None:
        rows = [
            ("DU111", "NetLiquidation", "not-a-number", "USD"),
            ("DU111", "TotalCashValue", "1", "USD"),
            ("DU111", "BuyingPower", "1", "USD"),
        ]
        with pytest.raises(BrokerDataError, match="unparseable"):
            account_summary_from_rows(rows, expected_account="DU111", as_of=NOW)

    def test_wrong_account_fails_closed(self) -> None:
        rows = [("DU999", "NetLiquidation", "1", "USD")]
        with pytest.raises(BrokerDataError, match="unexpected account"):
            account_summary_from_rows(rows, expected_account="DU111", as_of=NOW)


class TestInstrumentNormalizer:
    def test_stock_contract(self) -> None:
        contract = SimpleNamespace(
            secType="STK",
            symbol="AAPL",
            conId=265598,
            currency="USD",
            exchange="SMART",
            primaryExchange="NASDAQ",
        )
        instrument = instrument_from_contract(contract)
        assert isinstance(instrument, UnderlyingContract)
        assert instrument.con_id == 265598
        assert instrument.primary_exchange == "NASDAQ"

    def test_option_contract(self) -> None:
        contract = SimpleNamespace(
            secType="OPT",
            symbol="AAPL",
            conId=777,
            currency="USD",
            lastTradeDateOrContractMonth="20260821",
            right="P",
            strike=190.0,
            multiplier="100",
            localSymbol="AAPL  260821P00190000",
            tradingClass="AAPL",
        )
        instrument = instrument_from_contract(contract)
        assert isinstance(instrument, OptionContract)
        assert instrument.right is OptionRight.PUT
        assert instrument.strike == Decimal("190")
        assert instrument.expiration.isoformat() == "2026-08-21"
        assert instrument.deliverable_verified is False  # never assumed

    def test_unsupported_sec_type_fails_closed(self) -> None:
        contract = SimpleNamespace(secType="FUT", symbol="ES", conId=1, currency="USD")
        with pytest.raises(BrokerDataError, match="unsupported security type"):
            instrument_from_contract(contract)

    def test_crypto_sec_type_normalizes_without_venue_metadata(self) -> None:
        # ADR-0010 §7 commit 1: a held crypto position must normalize on the
        # read path (previously it wedged every positions()/executions() read).
        from chronos.domain.models import CryptoContract

        contract = SimpleNamespace(
            secType="CRYPTO", symbol="BTC", conId=479624278, currency="USD", exchange="PAXOS"
        )
        instrument = instrument_from_contract(contract)
        assert isinstance(instrument, CryptoContract)
        assert instrument.symbol == "BTC"
        assert instrument.exchange == "PAXOS"
        # Venue metadata is NEVER populated by the read path (qualification only).
        assert instrument.min_tick is None
        assert instrument.min_size is None
        assert instrument.size_increment is None

    def test_missing_con_id_fails_closed(self) -> None:
        contract = SimpleNamespace(secType="STK", symbol="AAPL", conId=0, currency="USD")
        with pytest.raises(BrokerDataError, match="missing identity"):
            instrument_from_contract(contract)


class TestPositionAndExecutionNormalizers:
    def test_position_row(self) -> None:
        contract = SimpleNamespace(
            secType="STK", symbol="MSFT", conId=2, currency="USD", exchange="SMART"
        )
        position = position_from_row(("DU111", contract, 100.0, 415.25), expected_account="DU111")
        assert position.quantity == Decimal("100")
        assert position.average_cost == Decimal("415.25")

    def test_position_wrong_account_fails(self) -> None:
        contract = SimpleNamespace(
            secType="STK", symbol="MSFT", conId=2, currency="USD", exchange="SMART"
        )
        with pytest.raises(BrokerDataError, match="unexpected account"):
            position_from_row(("DU999", contract, 1.0, 1.0), expected_account="DU111")

    def test_execution_pair_with_commission(self) -> None:
        contract = SimpleNamespace(
            secType="STK", symbol="SPY", conId=3, currency="USD", exchange="SMART"
        )
        execution = SimpleNamespace(
            execId="0001.abc",
            acctNumber="DU111",
            orderId=17,
            permId=987654,
            clientId=17,
            orderRef="chronos:paper:x",
            side="SLD",
            shares=1.0,
            price=2.15,
            time="20260717 14:30:00 America/New_York",
        )
        result = execution_from_pair(
            contract, execution, commission=Decimal("1.05"), commission_currency="USD"
        )
        assert result.side is OrderSide.SELL
        assert result.commission == Decimal("1.05")
        assert result.timestamp.tzinfo is not None

    def test_execution_bad_time_fails_closed(self) -> None:
        contract = SimpleNamespace(
            secType="STK", symbol="SPY", conId=3, currency="USD", exchange="SMART"
        )
        execution = SimpleNamespace(
            execId="1",
            acctNumber="DU111",
            orderId=1,
            permId=1,
            clientId=1,
            orderRef=None,
            side="BOT",
            shares=1.0,
            price=1.0,
            time="garbage",
        )
        with pytest.raises(BrokerDataError, match="unparseable execution time"):
            execution_from_pair(contract, execution, commission=None, commission_currency=None)


class TestChainAndQuoteNormalizers:
    def test_chain_parameters(self) -> None:
        row = (
            "SMART",
            265598,
            "AAPL",
            "100",
            ["20260821", "20260918"],
            [180.0, 185.0, 190.0],
        )
        params = chain_parameters_from_row(row)
        assert params.multiplier == Decimal("100")
        assert [e.isoformat() for e in params.expirations] == ["2026-08-21", "2026-09-18"]
        assert params.strikes == (Decimal("180"), Decimal("185"), Decimal("190"))

    def test_quote_from_state_greeks_and_quality(self) -> None:
        underlying = UnderlyingContract(con_id=5, symbol="SPY")
        state = QuoteState(
            bid=Decimal("500.10"),
            ask=Decimal("500.20"),
            data_quality=DataQuality.LIVE,
            delta=Decimal("-0.30"),
        )
        quote = quote_from_state(state, contract=underlying, timestamp=NOW)
        assert quote.midpoint == Decimal("500.15")
        assert quote.data_quality is DataQuality.LIVE
        assert quote.greeks is not None
        assert quote.greeks.delta == Decimal("-0.30")

    def test_quote_without_greeks_has_none(self) -> None:
        underlying = UnderlyingContract(con_id=5, symbol="SPY")
        quote = quote_from_state(QuoteState(), contract=underlying, timestamp=NOW)
        assert quote.greeks is None
        assert quote.bid is None  # missing stays missing — never invented


class _DisconnectableApp:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class _StubbornReaderThread:
    def __init__(self) -> None:
        self.join_timeout: float | None = None

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout


def test_disconnect_retains_stubborn_reader_and_fences_reconnect() -> None:
    broker = object.__new__(OfficialIBKRBroker)
    app = _DisconnectableApp()
    thread = _StubbornReaderThread()
    broker._app = app
    broker._thread = thread  # type: ignore[assignment]

    with pytest.raises(BrokerConnectionError, match="reader thread did not stop"):
        asyncio.run(broker.disconnect())

    assert app.disconnected is True
    assert thread.join_timeout == 5.0
    assert broker._app is app
    assert broker._thread is thread


class _FakeContract:
    conId = 0
    exchange = ""


class _FakeOrder:
    def __init__(self) -> None:
        self.action = ""
        self.orderType = ""
        self.totalQuantity: object = None
        self.lmtPrice = 0.0
        self.tif = ""
        self.outsideRth = False
        self.account = ""
        self.orderRef = ""
        self.whatIf = False
        self.transmit = False


class _ConnectedOrderApp:
    def __init__(self) -> None:
        self.place_calls: list[int] = []

    def isConnected(self) -> bool:
        return True

    def placeOrder(self, order_id: int, contract: object, order: object) -> None:
        del contract, order
        self.place_calls.append(order_id)


class _RaisingOrderApp(_ConnectedOrderApp):
    def placeOrder(self, order_id: int, contract: object, order: object) -> None:
        super().placeOrder(order_id, contract, order)
        raise RuntimeError("simulated client send failure")


class _ConcurrentEngageOrderApp:
    def __init__(self, switch: LiveKillSwitch, bridge: CallbackBridge) -> None:
        self.switch = switch
        self.bridge = bridge
        self.place_calls: list[int] = []
        self.engage_started = Event()
        self.engage_finished = Event()
        self.thread: Thread | None = None

    def isConnected(self) -> bool:
        return True

    def placeOrder(self, order_id: int, contract: object, order: object) -> None:
        del contract, order
        self.place_calls.append(order_id)

        def engage() -> None:
            self.engage_started.set()
            self.switch.engage(reason="operator halt", initiated_by="op", now=NOW)
            self.engage_finished.set()

        thread = Thread(target=engage)
        self.thread = thread
        thread.start()
        assert self.engage_started.wait(timeout=1)
        assert not self.engage_finished.wait(timeout=0.05)
        self.bridge.on_order_status(
            order_id,
            "Submitted",
            0.0,
            1.0,
            0.0,
            0,
        )


class _AllowSendGuard:
    @contextmanager
    def at_send(self) -> Iterator[bool]:
        yield True


def test_submit_rechecks_kill_switch_atomically_after_order_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        official_ibkr_module, "_load_ibapi", lambda: (None, None, _FakeContract, None)
    )
    monkeypatch.setattr(official_ibkr_module, "_load_order_class", lambda: _FakeOrder)
    switch = LiveKillSwitch(tmp_path / "ks.json")
    broker = object.__new__(OfficialIBKRBroker)
    broker._settings = SimpleNamespace(  # type: ignore[assignment]
        ib_environment=IBEnvironment.LIVE,
        ib_port=7496,
        ib_account_id="U7654321",
        allow_outside_rth=False,
    )
    broker._live_kill_switch = switch
    broker.bridge = CallbackBridge(RequestRegistry())
    broker.bridge.managed_accounts = ("U7654321",)
    broker.order_ids = OrderIdAllocator()
    broker.order_ids.seed(9001)
    app = _ConnectedOrderApp()
    broker._app = app

    original_build = broker._build_order_objects

    def build_then_engage(request: OrderRequest, *, what_if: bool) -> tuple[object, object]:
        built = original_build(request, what_if=what_if)
        switch.engage(reason="operator halt", initiated_by="op", now=NOW)
        return built

    monkeypatch.setattr(broker, "_build_order_objects", build_then_engage)
    request = OrderRequest(
        correlation_id="CHR-ORD-" + "F" * 32,
        account_id="U7654321",
        contract=CryptoContract(con_id=1, symbol="ETH"),
        intent=OrderIntent.OPEN_LONG_CRYPTO,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        limit_price=Decimal("3000"),
        order_ref="CHR-ORD-" + "F" * 32,
        transmit=True,
    )

    with pytest.raises(BrokerRefusedBeforeSend, match="kill switch engaged"):
        asyncio.run(broker.submit_order(request, send_guard=_AllowSendGuard()))

    assert app.place_calls == []


def test_concurrent_kill_engagement_linearizes_after_synchronous_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        official_ibkr_module, "_load_ibapi", lambda: (None, None, _FakeContract, None)
    )
    monkeypatch.setattr(official_ibkr_module, "_load_order_class", lambda: _FakeOrder)
    switch = LiveKillSwitch(tmp_path / "ks.json")
    broker = object.__new__(OfficialIBKRBroker)
    broker._settings = SimpleNamespace(  # type: ignore[assignment]
        ib_environment=IBEnvironment.LIVE,
        ib_port=7496,
        ib_account_id="U7654321",
        allow_outside_rth=False,
        ib_client_id=17,
    )
    broker._live_kill_switch = switch
    broker.bridge = CallbackBridge(RequestRegistry())
    broker.bridge.managed_accounts = ("U7654321",)
    broker.order_ids = OrderIdAllocator()
    broker.order_ids.seed(9100)
    app = _ConcurrentEngageOrderApp(switch, broker.bridge)
    broker._app = app
    request = OrderRequest(
        correlation_id="CHR-ORD-" + "G" * 32,
        account_id="U7654321",
        contract=CryptoContract(con_id=1, symbol="ETH"),
        intent=OrderIntent.OPEN_LONG_CRYPTO,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        limit_price=Decimal("3000"),
        order_ref="CHR-ORD-" + "G" * 32,
        transmit=True,
    )

    submission = asyncio.run(broker.submit_order(request, send_guard=_AllowSendGuard()))

    thread = app.thread
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert app.engage_finished.is_set()
    assert app.place_calls == [9100]
    assert submission.lifecycle is OrderLifecycle.SUBMITTED
    assert switch.is_engaged()


@pytest.mark.parametrize(
    ("send_raises", "expected_message"),
    [
        (False, "no broker acknowledgement; state unknown"),
        (True, "broker send raised; state unknown"),
    ],
)
def test_submit_ambiguity_returns_unknown_with_allocated_broker_id(
    monkeypatch: pytest.MonkeyPatch, send_raises: bool, expected_message: str
) -> None:
    monkeypatch.setattr(
        official_ibkr_module, "_load_ibapi", lambda: (None, None, _FakeContract, None)
    )
    monkeypatch.setattr(official_ibkr_module, "_load_order_class", lambda: _FakeOrder)
    monkeypatch.setattr(official_ibkr_module, "_REQUEST_TIMEOUT_S", 0.01)
    broker = object.__new__(OfficialIBKRBroker)
    broker._settings = SimpleNamespace(  # type: ignore[assignment]
        ib_environment=IBEnvironment.PAPER,
        ib_port=7497,
        ib_account_id="DU7654321",
        allow_outside_rth=False,
        ib_client_id=17,
    )
    broker._live_kill_switch = None
    broker.bridge = CallbackBridge(RequestRegistry())
    broker.bridge.managed_accounts = ("DU7654321",)
    broker.order_ids = OrderIdAllocator()
    broker.order_ids.seed(9200)
    app = _RaisingOrderApp() if send_raises else _ConnectedOrderApp()
    broker._app = app
    request = OrderRequest(
        correlation_id="CHR-ORD-" + "H" * 32,
        account_id="DU7654321",
        contract=CryptoContract(con_id=1, symbol="ETH"),
        intent=OrderIntent.OPEN_LONG_CRYPTO,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        limit_price=Decimal("3000"),
        order_ref="CHR-ORD-" + "H" * 32,
        transmit=True,
    )

    submission = asyncio.run(broker.submit_order(request, send_guard=_AllowSendGuard()))

    assert app.place_calls == [9200]
    assert submission.broker_order_id == 9200
    assert submission.lifecycle is OrderLifecycle.SUBMISSION_UNKNOWN
    assert submission.message == expected_message


class TestOrderObjectBuilding:
    """``_build_order_objects`` maps an ``OrderRequest`` onto the raw ibapi
    Order/Contract. The official package is absent here, so the two ibapi
    loaders are monkeypatched to lightweight stand-ins — the mapping logic
    (Decimal-preserving quantity, family TIF, transmit) is what matters, not
    the concrete ibapi classes.
    """

    @staticmethod
    def _patch_loaders(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            official_ibkr_module, "_load_ibapi", lambda: (None, None, _FakeContract, None)
        )
        monkeypatch.setattr(official_ibkr_module, "_load_order_class", lambda: _FakeOrder)

    def _build(
        self,
        request: OrderRequest,
        *,
        what_if: bool = False,
        allow_outside_rth: bool = False,
    ) -> tuple[object, object]:
        # The method reads only self._settings.allow_outside_rth (the F4 clamp);
        # a bare shell carrying that one attribute is enough.
        broker = object.__new__(OfficialIBKRBroker)
        broker._settings = SimpleNamespace(allow_outside_rth=allow_outside_rth)  # type: ignore[assignment]
        return OfficialIBKRBroker._build_order_objects(broker, request, what_if=what_if)

    def test_fractional_crypto_quantity_is_preserved_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_loaders(monkeypatch)
        request = OrderRequest(
            correlation_id="CHR-ORD-" + "C" * 32,
            account_id="U7654321",
            contract=CryptoContract(con_id=479624278, symbol="BTC"),
            intent=OrderIntent.OPEN_LONG_CRYPTO,
            side=OrderSide.BUY,
            quantity=Decimal("0.005"),
            limit_price=Decimal("64000"),
            order_ref="CHR-ORD-" + "C" * 32,
            time_in_force="IOC",
        )

        contract, order = self._build(request)

        # int() would have truncated 0.005 -> 0 and silently placed a 0-size order.
        assert order.totalQuantity == Decimal("0.005")
        assert isinstance(order.totalQuantity, Decimal)
        assert order.tif == "IOC"  # family-conditional TIF mapped from the request
        assert order.outsideRth is False
        assert order.transmit is False  # mapped from request.transmit (never literal True)
        assert contract.exchange == "PAXOS"  # crypto venue, never SMART
        assert contract.conId == 479624278

    def test_integral_option_quantity_and_day_tif_round_trip_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_loaders(monkeypatch)
        option = OptionContract(
            con_id=777,
            symbol="AAPL",
            underlying_con_id=999,
            expiration=date(2026, 8, 21),
            strike=Decimal("190"),
            right=OptionRight.PUT,
            multiplier=Decimal("100"),
            trading_class="AAPL",
            local_symbol="AAPL 260821P00190000",
        )
        request = OrderRequest(
            correlation_id="CHR-ORD-" + "A" * 32,
            account_id="U7654321",
            contract=option,
            intent=OrderIntent.OPEN_SHORT_PUT,
            side=OrderSide.SELL,
            quantity=Decimal("2"),
            limit_price=Decimal("3.20"),
            order_ref="CHR-ORD-" + "A" * 32,
        )

        contract, order = self._build(request)

        assert order.totalQuantity == Decimal("2")
        assert order.tif == "DAY"  # options are always DAY
        assert contract.exchange == "SMART"

    def test_what_if_forces_transmit_false_even_if_request_transmits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_loaders(monkeypatch)
        request = OrderRequest(
            correlation_id="CHR-ORD-" + "B" * 32,
            account_id="U7654321",
            contract=CryptoContract(con_id=1, symbol="ETH"),
            intent=OrderIntent.OPEN_LONG_CRYPTO,
            side=OrderSide.BUY,
            quantity=Decimal("0.1"),
            limit_price=Decimal("3000"),
            order_ref="CHR-ORD-" + "B" * 32,
            transmit=True,
        )

        _contract, order = self._build(request, what_if=True)

        assert order.whatIf is True
        assert order.transmit is False

    def test_outside_rth_request_is_clamped_when_the_setting_forbids_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # F4 defense in depth: even a request asking for outside-RTH is clamped
        # to RTH unless the owner setting allows it.
        self._patch_loaders(monkeypatch)
        request = OrderRequest(
            correlation_id="CHR-ORD-" + "D" * 32,
            account_id="U7654321",
            contract=CryptoContract(con_id=1, symbol="ETH"),
            intent=OrderIntent.OPEN_LONG_CRYPTO,
            side=OrderSide.BUY,
            quantity=Decimal("0.1"),
            limit_price=Decimal("3000"),
            order_ref="CHR-ORD-" + "D" * 32,
            outside_rth=True,
        )

        _contract, order = self._build(request, allow_outside_rth=False)

        assert order.outsideRth is False

    def test_outside_rth_request_passes_when_the_setting_allows_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_loaders(monkeypatch)
        request = OrderRequest(
            correlation_id="CHR-ORD-" + "E" * 32,
            account_id="U7654321",
            contract=CryptoContract(con_id=1, symbol="ETH"),
            intent=OrderIntent.OPEN_LONG_CRYPTO,
            side=OrderSide.BUY,
            quantity=Decimal("0.1"),
            limit_price=Decimal("3000"),
            order_ref="CHR-ORD-" + "E" * 32,
            outside_rth=True,
        )

        _contract, order = self._build(request, allow_outside_rth=True)

        assert order.outsideRth is True
