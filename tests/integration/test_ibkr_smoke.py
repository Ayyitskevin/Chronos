"""Opt-in, strictly read-only smoke coverage for TWS or IB Gateway.

The adapter import is intentionally inside the test body. Collecting this module while it is
skipped therefore does not construct an adapter or attempt any broker connection.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pytest

_RUN_FLAG = "CHRONOS_RUN_IBKR_SMOKE"

pytestmark = [
    pytest.mark.ibkr,
    pytest.mark.skipif(
        os.environ.get(_RUN_FLAG) != "1",
        reason=f"set {_RUN_FLAG}=1 to run the opt-in read-only IBKR smoke test",
    ),
]


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


@pytest.mark.asyncio
async def test_ibkr_read_only_smoke() -> None:
    """Read one bounded slice of broker state and market data, then cleanly disconnect."""

    # These imports must remain below the opt-in skip marker. The default test run must not load
    # the network adapter or initialize any ib_async resources.
    from chronos.broker.ibkr import IBKRBroker
    from chronos.broker.market_data import MarketDataManager
    from chronos.config.settings import Settings
    from chronos.domain.enums import BrokerMode, DataQuality

    settings = Settings()
    assert settings.broker_mode is BrokerMode.IBKR, "Set BROKER_MODE=ibkr for the smoke test"
    assert settings.allow_order_transmit is False, "Smoke testing requires transmission disabled"
    assert settings.allow_live_trading is False
    assert settings.symbol_allowlist

    broker = IBKRBroker(settings)
    market_data = MarketDataManager(broker)
    try:
        await asyncio.wait_for(broker.connect(), timeout=15)
        status = await asyncio.wait_for(broker.connection_status(), timeout=15)
        assert status.connected is True

        server_time = await asyncio.wait_for(broker.server_time(), timeout=15)
        assert _is_timezone_aware(server_time)

        account = await asyncio.wait_for(broker.account_summary(), timeout=15)
        assert account.account_id
        assert _is_timezone_aware(account.as_of)
        if settings.ib_account_id and account.account_id != settings.ib_account_id:
            raise AssertionError("Connected account does not match configured IB_ACCOUNT_ID")

        symbol = settings.symbol_allowlist[0]
        underlying = await asyncio.wait_for(broker.qualify_underlying(symbol), timeout=15)
        assert underlying.symbol == symbol

        chain_snapshot = await market_data.option_chain_parameters(
            underlying,
            force_refresh=True,
        )
        populated_chain = next(
            (
                chain
                for chain in chain_snapshot.parameters
                if chain.expirations
                and chain.strikes
                and chain.underlying_con_id == underlying.con_id
            ),
            None,
        )
        assert populated_chain is not None

        # One underlying contract is the entire market-data scope. The manager owns bounded
        # timeout/retry and cancellation as one operation, including failed request cleanup.
        managed_quote = await market_data.underlying_quote(underlying, force_refresh=True)
        quote = managed_quote.quote
        assert quote.contract.con_id == underlying.con_id
        assert _is_timezone_aware(quote.timestamp)
        assert quote.data_quality in {
            DataQuality.LIVE,
            DataQuality.FROZEN,
            DataQuality.DELAYED,
        }
        assert any(value is not None for value in (quote.bid, quote.ask, quote.last, quote.close))
    finally:
        await asyncio.wait_for(broker.disconnect(), timeout=15)

    final_status = await asyncio.wait_for(broker.connection_status(), timeout=15)
    assert final_status.connected is False
