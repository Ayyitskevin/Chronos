from datetime import timedelta
from decimal import Decimal

import pytest
from tests.conftest import FIXED_NOW

from chronos.broker.base import (
    Broker,
    BrokerConnectionError,
    BrokerDataError,
    BrokerSafetyError,
)
from chronos.broker.demo import DEMO_ACCOUNT_ID, DEMO_NOW, DemoBroker
from chronos.domain.enums import (
    DataQuality,
    DemoCase,
    DemoProfile,
    OptionRight,
    OrderIntent,
    OrderSide,
    SecurityType,
)
from chronos.domain.models import CryptoContract, OptionContractSpec, OrderRequest


def test_demo_broker_satisfies_protocol(demo_broker: DemoBroker) -> None:
    assert isinstance(demo_broker, Broker)


@pytest.mark.asyncio
async def test_demo_operations_require_connection(demo_broker: DemoBroker) -> None:
    with pytest.raises(BrokerConnectionError):
        await demo_broker.account_summary()


@pytest.mark.asyncio
async def test_demo_fixture_catalog_covers_required_cases(demo_broker: DemoBroker) -> None:
    await demo_broker.connect()

    assert {fixture.case for fixture in demo_broker.fixture_cases} == set(DemoCase)
    assert len({fixture.symbol for fixture in demo_broker.fixture_cases}) == 7


@pytest.mark.asyncio
async def test_demo_snapshot_is_repeatable(demo_broker: DemoBroker) -> None:
    await demo_broker.connect()
    first = await demo_broker.snapshot()
    await demo_broker.disconnect()
    await demo_broker.connect()
    second = await demo_broker.snapshot()

    assert first == second
    assert first.account.account_id == DEMO_ACCOUNT_ID
    assert first.captured_at == FIXED_NOW
    assert len(first.positions) == 5
    assert len(first.open_orders) == 1
    assert first.open_orders[0].filled_quantity == Decimal("1")


@pytest.mark.asyncio
async def test_default_demo_clock_is_stable_across_brokers() -> None:
    first = DemoBroker()
    second = DemoBroker()
    await first.connect()
    await second.connect()

    assert await first.snapshot() == await second.snapshot()
    assert await first.server_time() == DEMO_NOW


@pytest.mark.asyncio
async def test_empty_account_profile_has_honest_fixture_and_reachable_aapl_chain() -> None:
    broker = DemoBroker(clock=lambda: FIXED_NOW, profile=DemoProfile.EMPTY_ACCOUNT)
    await broker.connect()

    snapshot = await broker.snapshot()
    underlying = await broker.qualify_underlying("AAPL")
    chain = (await broker.option_chain_parameters(underlying))[0]
    requested = OptionContractSpec(
        symbol="AAPL",
        underlying_con_id=underlying.con_id,
        expiration=chain.expirations[0],
        strike=chain.strikes[0],
        right=OptionRight.PUT,
        multiplier=chain.multiplier,
        trading_class=chain.trading_class,
    )
    qualified = await broker.qualify_option_contracts((requested,))
    quotes = await broker.request_option_quotes(qualified)

    assert broker.profile is DemoProfile.EMPTY_ACCOUNT
    assert snapshot.positions == ()
    assert snapshot.open_orders == ()
    assert snapshot.executions == ()
    assert [(case.symbol, case.case) for case in broker.fixture_cases] == [
        ("AAPL", DemoCase.FLAT_PUT)
    ]
    assert "whole account is empty" in broker.fixture_cases[0].explanation
    assert chain.expirations == (qualified[0].expiration,)
    assert chain.strikes == (Decimal("185"),)
    assert qualified[0].con_id == 2002
    assert quotes[0].greeks is not None


@pytest.mark.asyncio
async def test_stale_fixture_is_explicit(demo_broker: DemoBroker) -> None:
    await demo_broker.connect()
    underlying = await demo_broker.qualify_underlying("NVDA")
    quote = await demo_broker.request_underlying_quote(underlying)

    assert quote.data_quality is DataQuality.STALE
    assert quote.timestamp == FIXED_NOW - timedelta(seconds=60)


@pytest.mark.asyncio
async def test_option_contracts_are_qualified_from_chain_metadata(
    demo_broker: DemoBroker,
) -> None:
    await demo_broker.connect()
    underlying = await demo_broker.qualify_underlying("AAPL")
    chain = (await demo_broker.option_chain_parameters(underlying))[0]
    requested = OptionContractSpec(
        symbol="AAPL",
        underlying_con_id=underlying.con_id,
        expiration=chain.expirations[1],
        strike=Decimal("185"),
        right=OptionRight.PUT,
        multiplier=chain.multiplier,
        trading_class=chain.trading_class,
    )

    qualified = await demo_broker.qualify_option_contracts((requested,))
    quotes = await demo_broker.request_option_quotes(qualified)

    assert qualified[0].con_id == 2002
    assert qualified[0].underlying_con_id == underlying.con_id
    assert qualified[0].deliverable_verified is True
    assert qualified[0].deliverable_shares == Decimal("100")
    assert quotes[0].greeks is not None
    assert quotes[0].greeks.delta == Decimal("-0.34")


@pytest.mark.asyncio
async def test_demo_preview_is_non_transmitting_and_submit_is_blocked(
    demo_broker: DemoBroker,
) -> None:
    await demo_broker.connect()
    underlying = await demo_broker.qualify_underlying("AAPL")
    chain = (await demo_broker.option_chain_parameters(underlying))[0]
    option = (
        await demo_broker.qualify_option_contracts(
            (
                OptionContractSpec(
                    symbol="AAPL",
                    underlying_con_id=underlying.con_id,
                    expiration=chain.expirations[1],
                    strike=Decimal("185"),
                    right=OptionRight.PUT,
                    multiplier=chain.multiplier,
                    trading_class=chain.trading_class,
                ),
            )
        )
    )[0]
    request = OrderRequest(
        correlation_id="CHR-TEST-001",
        account_id=DEMO_ACCOUNT_ID,
        contract=option,
        intent=OrderIntent.OPEN_SHORT_PUT,
        side=OrderSide.SELL,
        quantity=1,
        limit_price=Decimal("3.20"),
        order_ref="CHR-TEST-001",
    )

    preview = await demo_broker.preview_order(request)

    assert preview.accepted is True
    assert preview.estimated_commission == Decimal("0.65")
    with pytest.raises(BrokerSafetyError, match="cannot submit"):
        await demo_broker.submit_order(request)


@pytest.mark.asyncio
async def test_qualify_crypto_carries_venue_metadata(demo_broker: DemoBroker) -> None:
    await demo_broker.connect()
    contract = await demo_broker.qualify_crypto("btc")

    assert isinstance(contract, CryptoContract)
    assert contract.symbol == "BTC"
    assert contract.security_type is SecurityType.CRYPTO
    assert contract.exchange == "PAXOS"
    # Venue min-size/increment are populated (the CONFORMING path); the
    # absent-metadata UNKNOWN path is proven by the crypto risk unit tests.
    assert contract.min_size == Decimal("0.0001")
    assert contract.size_increment == Decimal("0.0001")


@pytest.mark.asyncio
async def test_request_crypto_quote_is_deterministic(demo_broker: DemoBroker) -> None:
    await demo_broker.connect()
    contract = await demo_broker.qualify_crypto("ETH")
    quote = await demo_broker.request_crypto_quote(contract)

    assert quote.contract.con_id == contract.con_id
    assert quote.bid == Decimal("2999.00")
    assert quote.ask == Decimal("3001.00")
    assert quote.data_quality is DataQuality.DEMO


@pytest.mark.asyncio
async def test_qualify_crypto_unknown_symbol_raises(demo_broker: DemoBroker) -> None:
    await demo_broker.connect()
    with pytest.raises(BrokerDataError, match="crypto"):
        await demo_broker.qualify_crypto("DOGE")
