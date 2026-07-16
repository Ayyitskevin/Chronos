import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from tests.conftest import FIXED_NOW

from chronos.broker.base import Broker, BrokerDataError
from chronos.broker.market_data import (
    MarketDataCancellationError,
    MarketDataManager,
    MarketDataPacingError,
    MarketDataPacingExhaustedError,
    MarketDataPermissionError,
    MarketDataUnavailableError,
)
from chronos.domain.enums import DataQuality, OptionRight
from chronos.domain.models import (
    MarketQuote,
    OptionChainParameters,
    OptionContract,
    OptionContractSpec,
    UnderlyingContract,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = FIXED_NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeMarketDataBroker:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.option_quote_calls: list[tuple[int, ...]] = []
        self.cancellation_calls: list[tuple[int, ...]] = []
        self.chain_calls = 0
        self.failures: list[Exception] = []
        self.cancellation_failures: list[Exception] = []
        self.cancellation_delay_seconds = 0.0
        self.omitted_contract_ids: set[int] = set()
        self.missing_values = False
        self.data_quality = DataQuality.LIVE
        self.crossed_market = False
        self.quote_age_seconds = 0.0
        self.active_requests = 0
        self.active_subscriptions = 0
        self.max_active_requests = 0
        self.max_active_subscriptions = 0

    async def option_chain_parameters(
        self,
        underlying: UnderlyingContract,
    ) -> tuple[OptionChainParameters, ...]:
        self.chain_calls += 1
        return (
            OptionChainParameters(
                exchange="SMART",
                underlying_con_id=underlying.con_id,
                trading_class=underlying.symbol,
                multiplier=Decimal("100"),
                expirations=(
                    self.clock.value.date() + timedelta(days=14),
                    self.clock.value.date() + timedelta(days=21),
                ),
                strikes=(Decimal("95"), Decimal("100"), Decimal("105")),
            ),
        )

    async def qualify_option_contracts(
        self,
        specs: tuple[OptionContractSpec, ...],
    ) -> tuple[OptionContract, ...]:
        return tuple(
            OptionContract(
                **spec.model_dump(),
                con_id=9_000 + index,
                local_symbol=f"{spec.symbol}-{index}",
            )
            for index, spec in enumerate(specs)
        )

    async def request_underlying_quote(
        self,
        contract: UnderlyingContract,
    ) -> MarketQuote:
        return self._quote(contract)

    async def request_option_quotes(
        self,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[MarketQuote, ...]:
        contract_ids = tuple(contract.con_id for contract in contracts)
        self.option_quote_calls.append(contract_ids)
        self.active_requests += 1
        self.active_subscriptions += len(contracts)
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        self.max_active_subscriptions = max(
            self.max_active_subscriptions,
            self.active_subscriptions,
        )
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            if self.failures:
                raise self.failures.pop(0)
            return tuple(
                self._quote(contract)
                for contract in contracts
                if contract.con_id not in self.omitted_contract_ids
            )
        finally:
            self.active_requests -= 1
            self.active_subscriptions -= len(contracts)

    async def cancel_market_data(self, contract_ids: tuple[int, ...]) -> None:
        self.cancellation_calls.append(tuple(contract_ids))
        if self.cancellation_delay_seconds:
            await asyncio.sleep(self.cancellation_delay_seconds)
        if self.cancellation_failures:
            raise self.cancellation_failures.pop(0)

    def _quote(self, contract: UnderlyingContract | OptionContract) -> MarketQuote:
        if self.missing_values:
            return MarketQuote(
                contract=contract,
                timestamp=self.clock() - timedelta(seconds=self.quote_age_seconds),
                data_quality=DataQuality.LIVE,
            )
        return MarketQuote(
            contract=contract,
            timestamp=self.clock() - timedelta(seconds=self.quote_age_seconds),
            data_quality=self.data_quality,
            bid=Decimal("1.10") if self.crossed_market else Decimal("1.00"),
            ask=Decimal("1.00") if self.crossed_market else Decimal("1.10"),
            last=Decimal("1.05"),
            volume=100,
            open_interest=500,
        )


def make_contract(contract_id: int) -> OptionContract:
    return OptionContract(
        con_id=contract_id,
        symbol="TEST",
        expiration=date(2026, 2, 20),
        strike=Decimal(contract_id),
        right=OptionRight.CALL,
        multiplier=Decimal("100"),
        trading_class="TEST",
        local_symbol=f"TEST-{contract_id}",
    )


def make_manager(
    broker: FakeMarketDataBroker,
    **kwargs: Any,
) -> MarketDataManager:
    return MarketDataManager(cast(Broker, broker), clock=broker.clock, **kwargs)


def test_narrowing_selects_target_expirations_and_nearest_strikes() -> None:
    as_of = date(2026, 1, 1)
    parameters = OptionChainParameters(
        exchange="SMART",
        underlying_con_id=100,
        trading_class="TEST",
        multiplier=Decimal("100"),
        expirations=tuple(as_of + timedelta(days=days) for days in (5, 10, 20, 30, 50)),
        strikes=tuple(Decimal(value) for value in (90, 95, 100, 105, 110)),
    )

    specs = MarketDataManager.narrow_option_specs(
        parameters,
        symbol="test",
        spot=Decimal("102"),
        right=OptionRight.PUT,
        as_of=as_of,
        min_dte=7,
        target_dte=21,
        max_dte=45,
        max_expirations=2,
        strikes_per_expiration=3,
    )

    assert {spec.expiration for spec in specs} == {
        as_of + timedelta(days=20),
        as_of + timedelta(days=30),
    }
    assert tuple(spec.strike for spec in specs[:3]) == (
        Decimal("95"),
        Decimal("100"),
        Decimal("105"),
    )
    assert all(spec.symbol == "TEST" and spec.right is OptionRight.PUT for spec in specs)


@pytest.mark.asyncio
async def test_option_quotes_are_batched_and_every_batch_is_cancelled() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    manager = make_manager(
        broker,
        batch_size=2,
        max_concurrent_requests=2,
        max_active_subscriptions=4,
    )
    contracts = tuple(make_contract(contract_id) for contract_id in range(101, 106))

    quotes = await manager.option_quotes(contracts)

    assert tuple(item.quote.contract.con_id for item in quotes) == tuple(
        contract.con_id for contract in contracts
    )
    assert sorted(len(batch) for batch in broker.option_quote_calls) == [1, 2, 2]
    assert sorted(broker.cancellation_calls) == sorted(broker.option_quote_calls)
    assert manager.active_subscription_count == 0


@pytest.mark.asyncio
async def test_concurrency_and_active_subscriptions_are_bounded() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    manager = make_manager(
        broker,
        batch_size=1,
        max_concurrent_requests=2,
        max_active_subscriptions=2,
    )

    await manager.option_quotes(tuple(make_contract(value) for value in range(201, 205)))

    assert broker.max_active_requests == 2
    assert broker.max_active_subscriptions == 2
    assert manager.active_subscription_count == 0


@pytest.mark.asyncio
async def test_concurrent_same_contract_misses_are_coalesced() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    manager = make_manager(broker)
    contract = make_contract(250)

    first, second = await asyncio.gather(
        manager.option_quotes((contract,)),
        manager.option_quotes((contract,)),
    )

    assert broker.option_quote_calls == [(250,)]
    assert broker.cancellation_calls == [(250,)]
    assert sorted((first[0].from_cache, second[0].from_cache)) == [False, True]


@pytest.mark.asyncio
async def test_request_error_still_cancels_and_releases_subscription_slots() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.failures.append(BrokerDataError("unclassified failure"))
    manager = make_manager(broker, max_attempts=3)
    contract = make_contract(301)

    with pytest.raises(BrokerDataError, match="unclassified failure"):
        await manager.option_quotes((contract,))

    assert broker.option_quote_calls == [(301,)]
    assert broker.cancellation_calls == [(301,)]
    assert manager.active_subscription_count == 0


@pytest.mark.asyncio
async def test_cancellation_failure_quarantines_manager_and_retains_capacity() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.cancellation_failures.append(BrokerDataError("simulated cancellation failure"))
    manager = make_manager(broker, batch_size=2, max_active_subscriptions=2)

    with pytest.raises(MarketDataCancellationError, match="locked until"):
        await manager.option_quotes((make_contract(350),))

    assert manager.active_subscription_count == 1
    with pytest.raises(MarketDataCancellationError, match="locked until"):
        await manager.option_quotes((make_contract(351),))
    assert broker.option_quote_calls == [(350,)]
    assert broker.cancellation_calls == [(350,)]


@pytest.mark.asyncio
async def test_cancellation_timeout_also_quarantines_and_retains_capacity() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.cancellation_delay_seconds = 0.05
    manager = make_manager(
        broker,
        batch_size=2,
        max_active_subscriptions=2,
        cancellation_timeout_seconds=0.001,
    )

    with pytest.raises(MarketDataCancellationError, match="locked until"):
        await manager.option_quotes((make_contract(375),))

    assert manager.active_subscription_count == 1
    assert broker.cancellation_calls == [(375,)]


@pytest.mark.asyncio
async def test_classified_pacing_errors_back_off_then_succeed() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.failures.extend([MarketDataPacingError("pace one"), MarketDataPacingError("pace two")])
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    manager = make_manager(
        broker,
        max_attempts=3,
        initial_backoff_seconds=0.25,
        max_backoff_seconds=1.0,
        sleep=record_delay,
    )

    result = await manager.option_quotes((make_contract(401),))

    assert result[0].quote.contract.con_id == 401
    assert delays == [0.25, 0.5]
    assert broker.option_quote_calls == [(401,), (401,), (401,)]
    assert broker.cancellation_calls == [(401,)]


@pytest.mark.asyncio
async def test_pacing_retry_exhaustion_is_finite_and_cancels() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.failures.extend(MarketDataPacingError("paced") for _ in range(3))
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    manager = make_manager(
        broker,
        max_attempts=3,
        initial_backoff_seconds=0.1,
        max_backoff_seconds=0.15,
        sleep=record_delay,
    )

    with pytest.raises(MarketDataPacingExhaustedError) as error:
        await manager.option_quotes((make_contract(501),))

    assert error.value.attempts == 3
    assert len(broker.option_quote_calls) == 3
    assert delays == [0.1, 0.15]
    assert broker.cancellation_calls == [(501,)]
    assert manager.active_subscription_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [MarketDataPermissionError("no entitlement"), BrokerDataError("ordinary data error")],
)
async def test_permissions_and_unclassified_errors_are_never_retried(
    failure: Exception,
) -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.failures.append(failure)
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    manager = make_manager(broker, max_attempts=5, sleep=record_delay)

    with pytest.raises(type(failure)):
        await manager.option_quotes((make_contract(601),))

    assert len(broker.option_quote_calls) == 1
    assert delays == []
    assert broker.cancellation_calls == [(601,)]


@pytest.mark.asyncio
async def test_incomplete_response_surfaces_missing_contract_and_cancels() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.omitted_contract_ids.add(702)
    manager = make_manager(broker, batch_size=2)

    with pytest.raises(MarketDataUnavailableError) as error:
        await manager.option_quotes((make_contract(701), make_contract(702)))

    assert error.value.contract_ids == (702,)
    assert broker.cancellation_calls == [(701, 702)]
    assert manager.active_subscription_count == 0


@pytest.mark.asyncio
async def test_quote_cache_reports_freshness_and_expires_deterministically() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    manager = make_manager(
        broker,
        quote_cache_ttl=timedelta(seconds=2),
        max_quote_age=timedelta(seconds=5),
    )
    contract = make_contract(801)

    first = (await manager.option_quotes((contract,)))[0]
    clock.advance(1)
    cached = (await manager.option_quotes((contract,)))[0]
    clock.advance(2)
    refreshed = (await manager.option_quotes((contract,)))[0]

    assert first.from_cache is False
    assert first.fresh is True
    assert first.transmission_eligible is True
    assert cached.from_cache is True
    assert cached.cache_age_seconds == 1
    assert cached.source_age_seconds == 1
    assert refreshed.from_cache is False
    assert refreshed.fresh is True
    assert broker.option_quote_calls == [(801,), (801,)]
    assert broker.cancellation_calls == [(801,), (801,)]


@pytest.mark.asyncio
async def test_missing_quote_values_are_preserved_without_fabrication() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.missing_values = True
    manager = make_manager(broker)

    managed = (await manager.option_quotes((make_contract(901),)))[0]

    assert managed.quote.bid is None
    assert managed.quote.ask is None
    assert managed.quote.last is None
    assert managed.quote.greeks is None
    assert managed.quote.volume is None
    assert managed.quote.open_interest is None
    assert managed.fresh is True
    assert managed.transmission_eligible is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quality", "crossed_market", "expected"),
    [
        (DataQuality.LIVE, False, True),
        (DataQuality.FROZEN, False, True),
        (DataQuality.DELAYED, False, False),
        (DataQuality.UNKNOWN, False, False),
        (DataQuality.LIVE, True, False),
    ],
)
async def test_transmission_eligibility_is_separate_from_temporal_freshness(
    quality: DataQuality,
    crossed_market: bool,
    expected: bool,
) -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.data_quality = quality
    broker.crossed_market = crossed_market
    manager = make_manager(broker)

    managed = (await manager.option_quotes((make_contract(950),)))[0]

    assert managed.fresh is True
    assert managed.transmission_eligible is expected


@pytest.mark.asyncio
async def test_effective_quality_labels_an_old_source_quote_stale() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    broker.quote_age_seconds = 6
    manager = make_manager(broker, max_quote_age=timedelta(seconds=5))

    managed = (await manager.option_quotes((make_contract(975),)))[0]

    assert managed.quote.data_quality is DataQuality.LIVE
    assert managed.fresh is False
    assert managed.effective_data_quality is DataQuality.STALE
    assert managed.transmission_eligible is False


@pytest.mark.asyncio
async def test_option_chain_metadata_cache_has_explicit_age_and_ttl() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    manager = make_manager(broker, metadata_cache_ttl=timedelta(seconds=10))
    underlying = UnderlyingContract(con_id=1_001, symbol="TEST")

    first = await manager.option_chain_parameters(underlying)
    clock.advance(4)
    cached = await manager.option_chain_parameters(underlying)
    clock.advance(7)
    refreshed = await manager.option_chain_parameters(underlying)

    assert first.from_cache is False
    assert cached.from_cache is True
    assert cached.fresh is True
    assert cached.cache_age_seconds == 4
    assert refreshed.from_cache is False
    assert broker.chain_calls == 2


@pytest.mark.asyncio
async def test_expired_entries_are_evicted_when_the_manager_is_observed_again() -> None:
    clock = MutableClock()
    broker = FakeMarketDataBroker(clock)
    manager = make_manager(
        broker,
        quote_cache_ttl=timedelta(seconds=2),
        metadata_cache_ttl=timedelta(seconds=2),
    )

    await manager.option_quotes((make_contract(1_100),))
    assert manager.cache_entry_count == 1
    clock.advance(3)
    await manager.option_chain_parameters(UnderlyingContract(con_id=1_101, symbol="TEST"))

    assert manager.cache_entry_count == 1
