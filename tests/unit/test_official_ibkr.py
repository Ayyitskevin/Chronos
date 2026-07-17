"""Official-adapter guard rails and pure normalizers (no ibapi required).

The official TWS API package is deliberately absent from CI and this
environment; these tests prove (1) everything import-safe stays import-safe,
(2) construction fails closed with actionable guidance when the package is
missing or the account config is unsafe, and (3) the normalizers translate
recorded callback payloads into domain models faithfully.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from chronos.broker.base import BrokerDataError, BrokerError, BrokerSafetyError
from chronos.broker.callbacks import QuoteState
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
from chronos.config.settings import Settings
from chronos.domain.enums import DataQuality, IBEnvironment, OptionRight, OrderSide
from chronos.domain.models import OptionContract, UnderlyingContract

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
        contract = SimpleNamespace(secType="CRYPTO", symbol="BTC", conId=1, currency="USD")
        with pytest.raises(BrokerDataError, match="unsupported security type"):
            instrument_from_contract(contract)

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
